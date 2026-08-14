from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.context_package import ContextPackageBuilder
from app.database import Database
from app.model_profiles import ModelProfile, ModelProfileResolver
from app.models import ExtractionEnvelope
from app.permissions import PermissionEngine
from app.providers import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionTimeoutError,
)
from app.repository import WorkRepository
from app.skills.registry import SkillDefinition, SkillRegistry
from app.tools.registry import Permission
from app.utils import canonical_json, sha256_text


class SkillRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillOutputValidationError(SkillRuntimeError):
    def __init__(self) -> None:
        super().__init__("SKILL_OUTPUT_INVALID", "Skill Worker output failed schema validation")


class SkillInputValidationError(SkillRuntimeError):
    def __init__(self) -> None:
        super().__init__("SKILL_INPUT_INVALID", "Skill input failed schema validation")


class SkillIterationLimitError(SkillRuntimeError):
    def __init__(self) -> None:
        super().__init__("SKILL_ITERATION_LIMIT", "Skill Worker iteration budget was exhausted")


class SkillTimeoutError(SkillRuntimeError):
    def __init__(self) -> None:
        super().__init__("SKILL_TIMEOUT", "Skill Worker exceeded its time budget")


class SkillPermissionError(SkillRuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__("PERMISSION_DENIED", f"Skill tool permission denied: {reason_code}")


@dataclass(frozen=True)
class SkillExecutionResult:
    envelope: ExtractionEnvelope | None
    output: Any
    skill_name: str
    skill_version: str
    model_profile: ModelProfile
    context_digest: str
    duration_ms: int
    execution_id: str


class SkillRuntime:
    """Generic single-step Skill runtime with a Phase 1-safe write boundary."""

    def __init__(
        self,
        *,
        database: Database,
        repository: WorkRepository,
        registry: SkillRegistry,
        context_builder: ContextPackageBuilder,
        worker: ExtractionProvider,
    ) -> None:
        self.database = database
        self.repository = repository
        self.registry = registry
        self.context_builder = context_builder
        self.worker = worker
        self.profiles = ModelProfileResolver(worker)
        self.permissions = PermissionEngine(registry.tool_registry)

    def invoke_work_capture(
        self,
        *,
        user_id: str,
        run_id: str,
        conversation_id: str,
        content: str,
        allow_retry: bool = False,
    ) -> SkillExecutionResult:
        result = self.invoke(
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            skill_name="work-capture",
            input_payload={"content": content},
            step_key="work-capture",
            allow_retry=allow_retry,
        )
        if result.envelope is None:
            raise SkillOutputValidationError()
        return result

    def invoke(
        self,
        *,
        user_id: str,
        run_id: str,
        conversation_id: str,
        skill_name: str,
        input_payload: dict[str, Any],
        step_key: str | None = None,
        allow_retry: bool = False,
    ) -> SkillExecutionResult:
        definition = self.registry.require_enabled(skill_name)
        try:
            profile = self.profiles.resolve(definition.manifest.model_profile)
        except ValueError as exc:
            raise SkillRuntimeError(
                "MODEL_PROFILE_UNAVAILABLE",
                "Skill model profile is not available",
            ) from exc

        content = _context_content(input_payload)
        context = self.context_builder.build(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            recent_days=definition.manifest.memory_scope.recent_days,
        )
        input_schema = _load_schema(definition, definition.manifest.input_schema)
        output_schema = _load_schema(definition, definition.manifest.output_schema)
        validation_input = dict(input_payload)
        declares_context = "context_package" in input_schema.get("properties", {})
        if declares_context:
            # Context is runtime-owned; a caller cannot spoof or replace it.
            validation_input["context_package"] = context.payload
        try:
            # Validate the caller-owned contract, plus context only when the
            # Skill explicitly declares that runtime-provided field.
            validate_json_schema(validation_input, input_schema)
        except ValueError as exc:
            raise SkillInputValidationError() from exc
        runtime_input = dict(input_payload)
        if declares_context:
            runtime_input["context_package"] = context.payload

        input_digest = sha256_text(canonical_json(runtime_input))
        execution = self.repository.begin_skill_execution(
            user_id=user_id,
            run_id=run_id,
            step_key=step_key or skill_name,
            skill_name=definition.manifest.name,
            skill_version=definition.manifest.version,
            model_profile=profile.name,
            max_iterations=definition.manifest.max_iterations,
            input_digest=input_digest,
            context_digest=context.digest,
            allow_retry=allow_retry,
        )
        self.repository.append_skill_event(
            user_id=user_id,
            run_id=run_id,
            event_type="SKILL_LOADED",
            public_summary=f"{definition.manifest.name} SKILL.md를 로드했습니다.",
            payload={
                "skill": definition.manifest.name,
                "version": definition.manifest.version,
                "model_profile": profile.name,
                "context_digest": context.digest,
            },
        )
        if execution["output_json"]:
            output = _decode_output(execution["output_json"])
            try:
                validate_json_schema(output, output_schema)
            except ValueError as exc:
                raise SkillOutputValidationError() from exc
            return self._result(
                output=output,
                definition=definition,
                profile=profile,
                context_digest=context.digest,
                duration_ms=0,
                execution_id=execution["id"],
            )

        max_attempts = min(
            definition.manifest.max_iterations,
            definition.manifest.failure_policy.retry + 1,
        )
        worker_context = {
            "skill": {
                "name": definition.manifest.name,
                "version": definition.manifest.version,
                "instructions": definition.body,
            },
            "context_package": context.payload,
        }
        started = time.monotonic()
        final_error: SkillRuntimeError | None = None
        for iteration in range(1, max_attempts + 1):
            self.repository.update_skill_execution(
                user_id=user_id,
                execution_id=execution["id"],
                state="RUNNING",
                iteration=iteration,
            )
            self.repository.append_skill_event(
                user_id=user_id,
                run_id=run_id,
                event_type="SKILL_ITERATION_STARTED",
                public_summary=f"{definition.manifest.name} Skill Worker를 실행하고 있습니다.",
                payload={
                    "skill": definition.manifest.name,
                    "version": definition.manifest.version,
                    "model_profile": profile.name,
                    "iteration": iteration,
                },
            )
            try:
                raw = self._run_worker(
                    definition=definition,
                    profile=profile,
                    input_payload=runtime_input,
                    content=content,
                    worker_context=worker_context,
                )
                output = _normalize_output(raw)
                validate_json_schema(output, output_schema)
            except SkillRuntimeError as exc:
                final_error = exc
            except ExtractionTimeoutError as exc:
                final_error = SkillRuntimeError(
                    "EXTRACTION_TIMEOUT", "Skill Worker provider timed out"
                )
            except ExtractionProviderError as exc:
                final_error = SkillRuntimeError(
                    "EXTRACTION_PROVIDER_FAILED", "Skill Worker provider failed"
                )
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                final_error = SkillOutputValidationError()
            except Exception as exc:
                final_error = SkillRuntimeError(type(exc).__name__, "Skill Worker failed")

            if final_error is None:
                duration_ms = max(0, round((time.monotonic() - started) * 1_000))
                output_json = canonical_json(output)
                self.repository.complete_skill_execution(
                    user_id=user_id,
                    execution_id=execution["id"],
                    output_json=output_json,
                    output_digest=sha256_text(output_json),
                    duration_ms=duration_ms,
                )
                self.repository.append_skill_event(
                    user_id=user_id,
                    run_id=run_id,
                    event_type="SKILL_COMPLETED",
                    public_summary=f"{definition.manifest.name} Skill Worker 결과를 검증했습니다.",
                    payload={
                        "skill": definition.manifest.name,
                        "iteration": iteration,
                        "duration_ms": duration_ms,
                        "schema_version": output.get("schema_version"),
                    },
                )
                return self._result(
                    output=output,
                    definition=definition,
                    profile=profile,
                    context_digest=context.digest,
                    duration_ms=duration_ms,
                    execution_id=execution["id"],
                )

            self.repository.append_skill_event(
                user_id=user_id,
                run_id=run_id,
                event_type="SKILL_ITERATION_FAILED",
                public_summary=f"{definition.manifest.name} Skill Worker 결과를 검증하지 못했습니다.",
                payload={
                    "skill": definition.manifest.name,
                    "iteration": iteration,
                    "error_code": final_error.code,
                },
            )

        duration_ms = max(0, round((time.monotonic() - started) * 1_000))
        error = final_error or SkillIterationLimitError()
        if max_attempts >= definition.manifest.max_iterations:
            error = SkillIterationLimitError()
        self.repository.fail_skill_execution(
            user_id=user_id,
            execution_id=execution["id"],
            error_code=error.code,
            duration_ms=duration_ms,
        )
        self.repository.append_skill_event(
            user_id=user_id,
            run_id=run_id,
            event_type="SKILL_FAILED",
            public_summary=f"{definition.manifest.name} Skill 실행에 실패했습니다.",
            payload={"skill": definition.manifest.name, "error_code": error.code},
        )
        raise error

    def execute_tool(
        self,
        *,
        user_id: str,
        run_id: str,
        skill_name: str,
        tool_name: str,
        payload: dict[str, Any],
        allow_guarded: bool = False,
    ) -> Any:
        definition = self.registry.require_enabled(skill_name)
        manifest_permission = definition.manifest.permissions.get(
            tool_name, Permission.DENY
        )
        decision, result = self.permissions.execute(
            tool_name=tool_name,
            payload=payload,
            user_id=user_id,
            manifest_tools=set(definition.manifest.tools),
            manifest_permission=manifest_permission,
            allow_guarded=allow_guarded,
        )
        self.repository.append_skill_event(
            user_id=user_id,
            run_id=run_id,
            event_type="TOOL_PERMISSION_DECIDED",
            public_summary="Skill Tool 권한을 평가했습니다.",
            payload={
                "skill": skill_name,
                "tool": tool_name,
                "permission": decision.permission.value,
                "allowed": decision.allowed,
                "reason_code": decision.reason_code,
            },
        )
        if not decision.allowed:
            raise SkillPermissionError(decision.reason_code)
        return result

    def _run_worker(
        self,
        *,
        definition: SkillDefinition,
        profile: ModelProfile,
        input_payload: dict[str, Any],
        content: str,
        worker_context: dict[str, Any],
    ) -> Any:
        if definition.manifest.output_schema.endswith("work-fact-draft.v1.json"):
            return _with_timeout(
                lambda: _extract_with_context(self.worker, content, worker_context),
                definition.manifest.timeout_seconds,
            )
        method = getattr(self.worker, "execute_skill", None)
        if callable(method):
            return _with_timeout(
                lambda: method(
                    skill_name=definition.manifest.name,
                    model_profile=profile.name,
                    input_payload=input_payload,
                    context=worker_context,
                ),
                definition.manifest.timeout_seconds,
            )
        raise SkillRuntimeError(
            "SKILL_WORKER_UNSUPPORTED",
            "configured Worker does not support this Skill output contract",
        )

    @staticmethod
    def _result(
        *,
        output: Any,
        definition: SkillDefinition,
        profile: ModelProfile,
        context_digest: str,
        duration_ms: int,
        execution_id: str,
    ) -> SkillExecutionResult:
        envelope = None
        try:
            envelope = ExtractionEnvelope.model_validate(output)
        except (ValidationError, TypeError, ValueError):
            pass
        return SkillExecutionResult(
            envelope=envelope,
            output=output,
            skill_name=definition.manifest.name,
            skill_version=definition.manifest.version,
            model_profile=profile,
            context_digest=context_digest,
            duration_ms=duration_ms,
            execution_id=execution_id,
        )


def _with_timeout(callback, timeout_seconds: float) -> Any:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(callback)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise SkillTimeoutError() from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _extract_with_context(worker: Any, content: str, context: dict[str, Any]) -> Any:
    method = getattr(worker, "extract_with_context", None)
    if callable(method):
        return method(content, context)
    return worker.extract(content)


def _context_content(input_payload: dict[str, Any]) -> str:
    content = input_payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return canonical_json(input_payload)


def _load_schema(definition: SkillDefinition, reference: str) -> dict[str, Any]:
    try:
        payload = json.loads((definition.path.parent / reference).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SkillRuntimeError("SKILL_SCHEMA_INVALID", "Skill schema could not be loaded") from exc
    if not isinstance(payload, dict):
        raise SkillRuntimeError("SKILL_SCHEMA_INVALID", "Skill schema must be an object")
    return payload


def _decode_output(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillOutputValidationError() from exc


def _normalize_output(raw: Any) -> dict[str, Any]:
    if isinstance(raw, BaseModel):
        output = raw.model_dump(mode="json")
    elif isinstance(raw, str):
        output = _decode_output(raw)
    else:
        output = raw
    if not isinstance(output, dict):
        raise SkillOutputValidationError()
    return output


def validate_json_schema(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Small dependency-free strict validator for Skill manifest schemas."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            raise ValueError(f"{path}: type mismatch")
    elif expected and not _matches_type(value, expected):
        raise ValueError(f"{path}: type mismatch")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: const mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: enum mismatch")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            raise ValueError(f"{path}: string length mismatch")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            raise ValueError(f"{path}: array length mismatch")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                validate_json_schema(item, schema["items"], path=f"{path}[{index}]")
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise ValueError(f"{path}: missing required property")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValueError(f"{path}: additional property")
        for key, subschema in properties.items():
            if key in value and isinstance(subschema, dict):
                validate_json_schema(value[key], subschema, path=f"{path}.{key}")


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)
