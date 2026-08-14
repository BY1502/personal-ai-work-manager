from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.context_linking import MemoryManager
from app.event_engine import DomainEvent, EventEngine
from app.context_package import ContextPackageBuilder
from app.database import Database
from app.dashboard import DashboardReadService
from app.extraction import ExtractionProvider
from app.jarvis import JarvisOrchestrator, UnsupportedResolution
from app.narration import recommendation_narrator_for, report_narrator_for
from app.models import (
    ChatRunRequest,
    JarvisResponse,
    RelinkActivityRequest,
    RelinkActivityResponse,
    ResolveClarificationRequest,
)
from app.repository import (
    DuplicateMessageConflict,
    ResourceNotFound,
    RunInProgress,
    VersionConflict,
    WorkRepository,
)
from app.recommendation import (
    RecommendationPresentationService,
    RecommendationService,
)
from app.reporting import ReportManager, ReportNotFound, ReportValidationError
from app.skill_runtime import SkillRuntime, SkillRuntimeError
from app.skills.registry import SkillRegistry, SkillRegistryError
from app.tools.registry import build_default_tool_registry
from app.providers import (
    ExtractionProviderError,
    ExtractionTimeoutError,
    ExtractionConcurrencyError,
    build_extraction_provider,
)
from app.utils import Clock, SystemClock, canonical_json, sha256_text, utc_iso
from app.validation import DeterministicValidationError, ExtractionValidator
import sqlite3
from app.work_manager import WorkManager
from app.work_queries import StructuredWorkQueryService
from app.tts import LocalTTSBridge
from pydantic import ValidationError


def create_app(
    *,
    database_path: Path | None = None,
    clock: Clock | None = None,
    extractor: ExtractionProvider | None = None,
    tts: LocalTTSBridge | None = None,
) -> FastAPI:
    resolved_database_path = database_path or Path(
        os.getenv("PERSONAL_AI_DB_PATH", "data/personal_ai.db")
    )
    database = Database(
        resolved_database_path,
        clock=clock or SystemClock(),
        default_user_id="local-user",
        timezone_name="Asia/Seoul",
    )
    repository = WorkRepository(database)
    event_engine = EventEngine(database)
    memory = MemoryManager(database)
    work_manager = WorkManager(database)
    work_queries = StructuredWorkQueryService(database)
    resolved_extractor = extractor or build_extraction_provider()
    tool_registry = build_default_tool_registry(database=database)
    skills_root = Path(
        os.getenv(
            "SKILLS_ROOT",
            str(Path(__file__).resolve().parents[1] / "skills"),
        )
    )
    phase2_runtime_enabled = os.getenv(
        "PHASE2_SKILL_RUNTIME_ENABLED", "true"
    ).strip().lower() not in {"0", "false", "no", "off"}
    auto_enable_names = {
        name.strip()
        for name in os.getenv("SKILL_AUTO_ENABLE", "work-capture").split(",")
        if name.strip()
    }
    skill_registry = SkillRegistry(
        root=skills_root,
        database=database,
        tool_registry=tool_registry,
        auto_enable_names=auto_enable_names if phase2_runtime_enabled else set(),
    )
    context_builder = ContextPackageBuilder(
        database=database,
        work_queries=work_queries,
    )
    skill_runtime = (
        SkillRuntime(
            database=database,
            repository=repository,
            registry=skill_registry,
            context_builder=context_builder,
            worker=resolved_extractor,
        )
        if phase2_runtime_enabled
        else None
    )
    recommendations = RecommendationService(work_queries)
    recommendation_presentation = RecommendationPresentationService(
        recommendation_narrator_for(resolved_extractor)
    )
    reports = ReportManager(
        database,
        narrator=report_narrator_for(resolved_extractor),
    )
    tts_enabled = os.getenv("TTS_ENABLED", "false").strip().lower() not in {
        "0", "false", "no", "off",
    }
    resolved_tts = tts or (
        LocalTTSBridge(
            base_url=os.getenv("TTS_BRIDGE_URL", "http://127.0.0.1:8765"),
            public_base_url=os.getenv(
                "TTS_PUBLIC_BASE_URL", "http://127.0.0.1:8766"
            ),
            timeout_seconds=float(os.getenv("TTS_TIMEOUT_SECONDS", "30")),
            provider_name=os.getenv("TTS_PROVIDER_NAME", "local-piper"),
            model_name=os.getenv("TTS_MODEL_NAME", "ko_KR-kss-medium"),
        )
        if tts_enabled
        else None
    )
    dashboard = DashboardReadService(
        database=database,
        work_queries=work_queries,
        recommendations=recommendations,
        extractor=resolved_extractor,
    )
    orchestrator = JarvisOrchestrator(
        repository=repository,
        memory=memory,
        work_manager=work_manager,
        work_queries=work_queries,
        recommendations=recommendations,
        recommendation_presentation=recommendation_presentation,
        reports=reports,
        extractor=resolved_extractor,
        skill_runtime=skill_runtime,
        tts=resolved_tts,
        event_engine=event_engine,
        validator=ExtractionValidator(),
        user_id=database.default_user_id,
        timezone_name=database.timezone_name,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with database.runtime_lock():
            database.initialize()
            skill_registry.refresh()
            yield

    application = FastAPI(
        title="Personal AI Work Manager",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database = database
    application.state.repository = repository
    application.state.memory = memory
    application.state.work_manager = work_manager
    application.state.work_queries = work_queries
    application.state.recommendations = recommendations
    application.state.recommendation_presentation = recommendation_presentation
    application.state.reports = reports
    application.state.dashboard = dashboard
    application.state.orchestrator = orchestrator
    application.state.tool_registry = tool_registry
    application.state.skill_registry = skill_registry
    application.state.skill_runtime = skill_runtime
    application.state.tts = resolved_tts
    application.state.event_engine = event_engine
    allowed_origins = [
        origin.strip()
        for origin in os.getenv(
            "DASHBOARD_ALLOWED_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,"
            "http://localhost:3001,http://127.0.0.1:3001,"
            "https://jarvis-personal-work-manager.pastel-hinny-4854.chatgpt.site",
        ).split(",")
        if origin.strip()
    ]
    base_origin_regex = (
        r"^https?://(?:localhost|127\.0\.0\.1|"
        r"\[[0-9a-f:]+\]|"
        r"[a-zA-Z0-9][a-zA-Z0-9.-]*|"
        r"\d{1,3}(?:\.\d{1,3}){3})"
        r"(?::(?:3000|3001|3100))$"
    )
    env_origin_regex = os.getenv("DASHBOARD_ALLOWED_ORIGIN_REGEX", "").strip()
    allowed_origin_regex = (
        f"(?:{base_origin_regex})|(?:{env_origin_regex})"
        if env_origin_regex
        else base_origin_regex
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=allowed_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/v1/chat/runs", response_model=JarvisResponse)
    def run_chat(request: dict[str, object]) -> JarvisResponse:
        normalized_request: dict[str, object] = dict(request)
        if (
            "content" not in normalized_request
            and (
                ("user_message" in normalized_request and normalized_request.get("user_message") is not None)
                or ("message" in normalized_request and normalized_request.get("message") is not None)
            )
        ):
            normalized_request["content"] = normalized_request.pop(
                "user_message",
                normalized_request.pop("message", None),
            )
        if (
            "content" not in normalized_request
            and normalized_request.get("text") is not None
        ):
            normalized_request["content"] = normalized_request.pop("text")
        normalized_request.pop("message", None)
        normalized_request.pop("user_message", None)
        if (
            "conversation_id" not in normalized_request
            and "conversationId" in normalized_request
        ):
            normalized_request["conversation_id"] = normalized_request.pop("conversationId")
        if (
            "client_message_id" not in normalized_request
            and "clientMessageId" in normalized_request
        ):
            normalized_request["client_message_id"] = normalized_request.pop(
                "clientMessageId"
            )
        if (
            "client_message_id" not in normalized_request
            and "message_id" in normalized_request
        ):
            normalized_request["client_message_id"] = normalized_request.pop("message_id")
        normalized_request.pop("user_id", None)
        normalized_request.pop("userId", None)
        # Older dashboard shells occasionally attach UI-only metadata. It is
        # not part of the canonical chat contract and must not turn a valid
        # request into a 422 merely because the shell was cached.
        normalized_request = {
            key: value
            for key, value in normalized_request.items()
            if key in {"conversation_id", "client_message_id", "content"}
        }
        if (
            "client_message_id" not in normalized_request
            and isinstance(normalized_request.get("content"), str)
            and normalized_request["content"].strip()
        ):
            conversation_key = normalized_request.get("conversation_id") or "default"
            normalized_request["client_message_id"] = (
                "legacy-"
                + sha256_text(
                    f"{conversation_key}:{normalized_request['content']}"
                )[:48]
            )
        if (
            "client_message_id" in normalized_request
            and normalized_request["client_message_id"] is not None
            and not isinstance(normalized_request["client_message_id"], str)
        ):
            normalized_request["client_message_id"] = str(
                normalized_request["client_message_id"]
            )
        if (
            "conversation_id" in normalized_request
            and normalized_request["conversation_id"] is not None
            and not isinstance(normalized_request["conversation_id"], str)
        ):
            normalized_request["conversation_id"] = str(
                normalized_request["conversation_id"]
            )
        try:
            payload = ChatRunRequest.model_validate(normalized_request)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors())

        return orchestrator.handle_chat(payload)

    @application.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return orchestrator.get_run_response(run_id)

    # Backward-compatible polling endpoint kept for clients that still use the
    # previous status URL shape. The canonical path is /api/v1/runs/{run_id}.
    @application.get("/api/v1/chat/runs/{run_id}")
    def get_run_legacy_alias(run_id: str) -> dict:
        return orchestrator.get_run_response(run_id)

    @application.get("/api/v1/runs")
    def get_run_missing_id() -> dict:
        return _problem(
            status_code=400,
            code="RUN_ID_REQUIRED",
            detail="run_id path parameter is required. Use /api/v1/runs/{run_id}",
        )

    @application.get("/api/v1/chat/runs")
    def get_legacy_run_missing_id() -> dict:
        return _problem(
            status_code=400,
            code="RUN_ID_REQUIRED",
            detail=(
                "run_id path parameter is required. "
                "Use /api/v1/runs/{run_id} or /api/v1/chat/runs/{run_id}"
            ),
        )

    @application.get("/api/v1/reports")
    def list_reports(limit: int = 50) -> list[dict]:
        return reports.list_reports(user_id=database.default_user_id, limit=limit)

    @application.get("/api/v1/reports/{report_id}")
    def get_report(report_id: str) -> dict:
        return reports.get_report(
            user_id=database.default_user_id,
            report_id=report_id,
        )

    @application.get("/api/v1/dashboard/summary")
    def dashboard_summary() -> dict:
        return dashboard.summary(user_id=database.default_user_id)

    @application.get("/api/v1/dashboard/activities")
    def dashboard_activities(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        return dashboard.recent_activities(
            user_id=database.default_user_id,
            limit=limit,
            offset=offset,
        )

    @application.get("/api/v1/dashboard/projects")
    def dashboard_projects(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        return dashboard.projects(
            user_id=database.default_user_id,
            limit=limit,
            offset=offset,
        )

    @application.get("/api/v1/dashboard/projects/{project_id}")
    def dashboard_project_detail(project_id: str) -> dict:
        return dashboard.project_detail(
            user_id=database.default_user_id,
            project_id=project_id,
        )

    @application.get("/api/v1/dashboard/provider")
    def dashboard_provider() -> dict:
        return dashboard.provider_status()

    @application.get("/api/v1/suggestions")
    def suggestions(limit: int = Query(default=3, ge=1, le=3)) -> dict:
        return {
            "items": [
                {
                    "id": item.id,
                    "trigger_type": item.trigger_type,
                    "title": item.title,
                    "detail": item.detail,
                    "status": item.status,
                }
                for item in event_engine.suggestions(
                    user_id=database.default_user_id,
                    limit=limit,
                )
            ],
            "policy_version": "trigger-v1",
        }

    @application.post("/api/v1/suggestions/{suggestion_id}/dismiss")
    def dismiss_suggestion(suggestion_id: str) -> dict:
        with database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE trigger_suggestions
                SET status = 'DISMISSED', updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'ACTIVE'
                """,
                (
                    utc_iso(database.clock.now_utc()),
                    suggestion_id,
                    database.default_user_id,
                ),
            ).rowcount
        if updated != 1:
            raise HTTPException(status_code=404, detail="suggestion not found")
        return {"id": suggestion_id, "status": "DISMISSED"}

    @application.get("/api/v1/provider")
    def dashboard_provider_compat() -> dict:
        # Backward-compatible alias for older clients/automation scripts.
        return dashboard.provider_status()

    @application.get("/api/v1/skills")
    def list_skills() -> dict:
        items: list[dict] = []
        for name, definition in sorted(skill_registry.definitions().items()):
            connection = database.connect()
            try:
                row = connection.execute(
                    """
                    SELECT state, validation_errors_json
                    FROM skill_registry
                    WHERE name = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (name,),
                ).fetchone()
            finally:
                connection.close()
            items.append(
                {
                    "name": name,
                    "version": definition.manifest.version,
                    "description": definition.manifest.description,
                    "state": row["state"] if row else "DISABLED",
                    "validation_errors": (
                        json.loads(row["validation_errors_json"])
                        if row
                        else []
                    ),
                }
            )
        return {"items": items, "errors": skill_registry.errors}

    @application.post(
        "/api/v1/clarifications/{clarification_id}/resolve",
        response_model=JarvisResponse,
    )
    def resolve_clarification(
        clarification_id: str,
        request: ResolveClarificationRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JarvisResponse:
        payload = request.model_dump(mode="json") | {
            "clarification_id": clarification_id
        }
        return orchestrator.resolve_clarification(
            clarification_id,
            request,
            idempotency_key=idempotency_key,
            request_hash=sha256_text(canonical_json(payload)),
        )

    @application.post(
        "/api/v1/activities/{activity_id}/relink",
        response_model=RelinkActivityResponse,
    )
    def relink_activity(
        activity_id: str,
        request: RelinkActivityRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> RelinkActivityResponse:
        payload = request.model_dump(mode="json") | {"activity_id": activity_id}
        result = work_manager.relink_activity(
            user_id=database.default_user_id,
            activity_id=activity_id,
            target_work_item_id=request.target_work_item_id,
            expected_activity_version=request.expected_activity_version,
            expected_link_version=request.expected_link_version,
            reason=request.reason,
            correction_run_id=request.correction_run_id,
            idempotency_key=idempotency_key,
            request_hash=sha256_text(canonical_json(payload)),
        )
        try:
            event_engine.emit(
                user_id=database.default_user_id,
                event=DomainEvent(
                    event_type="ACTIVITY_RELINKED",
                    aggregate_type="ACTIVITY",
                    aggregate_id=activity_id,
                    payload={"correction_run_id": request.correction_run_id},
                ),
            )
        except Exception:
            pass
        return result

    @application.exception_handler(DuplicateMessageConflict)
    async def duplicate_message_conflict(_, exc: DuplicateMessageConflict):
        return _problem(409, "DUPLICATE_MESSAGE_CONFLICT", str(exc))

    @application.exception_handler(RunInProgress)
    async def run_in_progress(_, exc: RunInProgress):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=202,
            content={
                "code": "RUN_IN_PROGRESS",
                "detail": str(exc),
                "run_id": exc.run_id,
                "status_url": f"/api/v1/runs/{exc.run_id}",
            },
        )

    @application.exception_handler(VersionConflict)
    async def version_conflict(_, exc: VersionConflict):
        return _problem(409, "VERSION_CONFLICT", str(exc))

    @application.exception_handler(ResourceNotFound)
    async def resource_not_found(_, exc: ResourceNotFound):
        return _problem(404, "NOT_FOUND", str(exc))

    @application.exception_handler(DeterministicValidationError)
    async def validation_error(_, exc: DeterministicValidationError):
        return _problem(422, "DETERMINISTIC_VALIDATION_FAILED", str(exc))

    @application.exception_handler(UnsupportedResolution)
    async def unsupported_resolution(_, exc: UnsupportedResolution):
        return _problem(422, "UNSUPPORTED_RESOLUTION", str(exc))

    @application.exception_handler(ReportValidationError)
    async def report_validation_error(_, exc: ReportValidationError):
        return _problem(422, "REPORT_VALIDATION_FAILED", str(exc))

    @application.exception_handler(ReportNotFound)
    async def report_not_found(_, exc: ReportNotFound):
        return _problem(404, "REPORT_NOT_FOUND", str(exc))

    @application.exception_handler(ExtractionTimeoutError)
    async def extraction_timeout(_, exc: ExtractionTimeoutError):
        return _problem(504, "EXTRACTION_TIMEOUT", str(exc))

    @application.exception_handler(ExtractionConcurrencyError)
    async def extraction_concurrency_error(_, exc: ExtractionConcurrencyError):
        return _problem(
            503,
            "EXTRACTION_CONCURRENCY_EXCEEDED",
            str(exc),
        )

    @application.exception_handler(ExtractionProviderError)
    async def extraction_provider_error(_, exc: ExtractionProviderError):
        return _problem(503, "EXTRACTION_PROVIDER_FAILED", str(exc))

    @application.exception_handler(SkillRuntimeError)
    async def skill_runtime_error(_, exc: SkillRuntimeError):
        return _problem(503, exc.code, str(exc))

    @application.exception_handler(SkillRegistryError)
    async def skill_registry_error(_, exc: SkillRegistryError):
        return _problem(503, "SKILL_RUNTIME_FAILED", str(exc))

    @application.exception_handler(sqlite3.OperationalError)
    async def sqlite_operational_error(_, exc: sqlite3.OperationalError):
        message = str(exc).lower()
        retryable = any(token in message for token in ("database is locked", "database is busy", "busy", "locked", "disk i/o error", "readonly", "database table is locked"))
        code = "DATABASE_BUSY" if retryable else "DATABASE_ERROR"
        status = 503 if retryable else 500
        return _problem(status, code, str(exc))

    return application


def _problem(status_code: int, code: str, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "detail": detail}},
    )


app = create_app()
