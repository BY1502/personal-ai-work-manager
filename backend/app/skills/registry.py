from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database import Database
from app.model_profiles import LOGICAL_MODEL_PROFILE_NAMES
from app.models import ExtractionEnvelope
from app.tools.registry import Permission, ToolRegistry
from app.utils import canonical_json, new_id, utc_iso


class SkillRegistryError(RuntimeError):
    pass


class SkillManifestError(SkillRegistryError):
    pass


class SkillDisabledError(SkillRegistryError):
    pass


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry: int = Field(default=1, ge=0, le=3)
    escalation: bool = False


class MemoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_days: int = Field(default=30, ge=0, le=365)
    scope: Literal["relevant", "current", "recent", "none"] = "relevant"


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["skill-manifest.v1"] = "skill-manifest.v1"
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=500)
    enabled: bool = False
    role: Literal["worker", "jarvis"] = "worker"
    model_profile: str = Field(min_length=1, max_length=100)
    max_iterations: int = Field(default=4, ge=1, le=4)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    tools: list[str] = Field(default_factory=list, max_length=20)
    permissions: dict[str, Permission] = Field(default_factory=dict)
    memory_scope: MemoryScope = Field(default_factory=MemoryScope)
    input_schema: str = Field(min_length=1, max_length=300)
    output_schema: str = Field(min_length=1, max_length=300)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)
    skill_request_policy: Literal["JARVIS_ONLY"] = "JARVIS_ONLY"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,99}", value):
            raise ValueError("skill name must be lowercase kebab-case")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not re.fullmatch(r"0|[1-9][0-9]*\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", value):
            raise ValueError("skill version must be semver-like")
        return value


@dataclass(frozen=True)
class SkillDefinition:
    manifest: SkillManifest
    body: str
    path: Path
    content_hash: str


def _split_front_matter(text: str) -> tuple[str, str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise SkillManifestError("SKILL.md must start with YAML front matter")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise SkillManifestError("SKILL.md YAML front matter is not closed")
    return normalized[4:marker], normalized[marker + len("\n---\n") :]


class SkillRegistry:
    """Filesystem discovery plus persisted activation state.

    Discovery is intentionally independent from Tool Registry. A manifest can
    be parsed without being executable; activation is granted only after all
    references and permissions are validated.
    """

    def __init__(
        self,
        *,
        root: Path,
        database: Database,
        tool_registry: ToolRegistry,
        auto_enable_names: set[str] | None = None,
    ) -> None:
        self.root = root
        self.database = database
        self.tool_registry = tool_registry
        self.auto_enable_names = set(auto_enable_names or set())
        self._definitions: dict[str, SkillDefinition] = {}
        self._errors: dict[str, str] = {}
        self._loaded = False

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def definitions(self) -> dict[str, SkillDefinition]:
        if not self._loaded:
            self.refresh()
        return dict(self._definitions)

    def refresh(self) -> None:
        definitions: dict[str, SkillDefinition] = {}
        errors: dict[str, str] = {}
        files = sorted(self.root.glob("*/SKILL.md")) if self.root.exists() else []
        parsed: list[SkillDefinition] = []
        for path in files:
            key = str(path)
            try:
                definition = self._parse(path)
                self._validate_references(definition)
                parsed.append(definition)
            except Exception as exc:
                errors[key] = _safe_error(exc)

        name_groups: dict[str, list[SkillDefinition]] = {}
        for definition in parsed:
            name_groups.setdefault(definition.manifest.name, []).append(definition)
        for name, group in name_groups.items():
            if len(group) > 1:
                message = "duplicate skill name"
                for definition in group:
                    errors[str(definition.path)] = message
                continue
            definitions[name] = group[0]

        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            for name, definition in definitions.items():
                manifest = definition.manifest
                row = connection.execute(
                    """
                    SELECT state, content_hash
                    FROM skill_registry
                    WHERE name = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (name,),
                ).fetchone()
                same_content = bool(row and row["content_hash"] == definition.content_hash)
                state = row["state"] if same_content and row["state"] == "ENABLED" else "DISABLED"
                if row is None and name in self.auto_enable_names:
                    state = "ENABLED"
                validation_errors = []
                connection.execute(
                    """
                    INSERT INTO skill_registry(
                        id, name, version, schema_version, state,
                        manifest_json, content_hash, validation_errors_json,
                        discovered_at, validated_at, enabled_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name, version, content_hash) DO UPDATE SET
                        state = excluded.state,
                        manifest_json = excluded.manifest_json,
                        validation_errors_json = excluded.validation_errors_json,
                        validated_at = excluded.validated_at,
                        enabled_at = excluded.enabled_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("skill"),
                        name,
                        manifest.version,
                        manifest.schema_version,
                        state,
                        canonical_json(manifest.model_dump(mode="json")),
                        definition.content_hash,
                        canonical_json(validation_errors),
                        now,
                        now,
                        now if state == "ENABLED" else None,
                        now,
                    ),
                )
            for path, message in errors.items():
                # Invalid manifests are intentionally not executable. Keeping
                # them in memory makes diagnostics available without inventing
                # a fake registry name or storing untrusted YAML.
                del path, message
        self._definitions = definitions
        self._errors = errors
        self._loaded = True

    def get(self, name: str) -> SkillDefinition | None:
        if not self._loaded:
            self.refresh()
        definition = self._definitions.get(name)
        if definition is None:
            return None
        row = self.database.connect()
        try:
            state_row = row.execute(
                "SELECT state FROM skill_registry WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
                (name,),
            ).fetchone()
        finally:
            row.close()
        if state_row is None or state_row["state"] != "ENABLED":
            return None
        return definition

    def require_enabled(self, name: str) -> SkillDefinition:
        definition = self.get(name)
        if definition is None:
            raise SkillDisabledError(f"skill is not enabled: {name}")
        return definition

    def set_state(self, name: str, state: Literal["DISABLED", "ENABLED"]) -> None:
        if not self._loaded:
            self.refresh()
        definition = self._definitions.get(name)
        if definition is None:
            raise SkillRegistryError(f"unknown skill: {name}")
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            if state == "ENABLED":
                conflict = connection.execute(
                    """
                    SELECT name FROM skill_registry
                    WHERE name = ? AND state = 'ENABLED'
                      AND content_hash <> ?
                    """,
                    (name, definition.content_hash),
                ).fetchone()
                if conflict:
                    raise SkillRegistryError("another version of skill is enabled")
            connection.execute(
                """
                UPDATE skill_registry
                SET state = ?, enabled_at = CASE WHEN ? = 'ENABLED' THEN ? ELSE enabled_at END,
                    updated_at = ?
                WHERE name = ? AND content_hash = ?
                """,
                (state, state, now, now, name, definition.content_hash),
            )

    @staticmethod
    def _parse(path: Path) -> SkillDefinition:
        text = path.read_text(encoding="utf-8")
        front_matter, body = _split_front_matter(text)
        try:
            raw = yaml.safe_load(front_matter)
        except yaml.YAMLError as exc:
            raise SkillManifestError("invalid YAML front matter") from exc
        if not isinstance(raw, dict):
            raise SkillManifestError("YAML front matter must be an object")
        try:
            manifest = SkillManifest.model_validate(raw)
        except Exception as exc:
            raise SkillManifestError("invalid skill metadata") from exc
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return SkillDefinition(manifest=manifest, body=body.strip(), path=path, content_hash=digest)

    def _validate_references(self, definition: SkillDefinition) -> None:
        manifest = definition.manifest
        if set(manifest.permissions) != set(manifest.tools):
            raise SkillManifestError("permissions must match tools exactly")
        if manifest.model_profile not in LOGICAL_MODEL_PROFILE_NAMES:
            raise SkillManifestError(
                f"unknown model profile: {manifest.model_profile}"
            )
        for tool_name in manifest.tools:
            if not self.tool_registry.has(tool_name):
                raise SkillManifestError(f"unknown tool: {tool_name}")
            permission = manifest.permissions[tool_name]
            if permission not in Permission:
                raise SkillManifestError(f"invalid permission: {permission}")
        for reference in (manifest.input_schema, manifest.output_schema):
            schema_path = (definition.path.parent / reference).resolve()
            if definition.path.parent.resolve() not in schema_path.parents:
                raise SkillManifestError("schema reference escapes skill directory")
            if not schema_path.is_file():
                raise SkillManifestError(f"schema reference does not exist: {reference}")
            try:
                payload = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SkillManifestError(f"invalid schema reference: {reference}") from exc
            if not isinstance(payload, dict) or payload.get("type") not in {"object", None}:
                raise SkillManifestError(f"schema reference must be a JSON object: {reference}")
        if manifest.output_schema.endswith("work-fact-draft.v1.json"):
            # The first slice must remain on the approved Phase 1 contract.
            ExtractionEnvelope.model_json_schema()


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300] or type(exc).__name__
