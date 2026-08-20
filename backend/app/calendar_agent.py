from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.database import Database
from app.repository import ResourceNotFound, VersionConflict, WorkRepository
from app.tools.registry import ToolExecutionError
from app.utils import canonical_json, new_id, sha256_text, utc_iso


class CalendarError(ToolExecutionError):
    pass


class CalendarConfigurationError(CalendarError):
    pass


class CalendarProviderError(CalendarError):
    pass


class CalendarProviderTimeout(CalendarProviderError):
    pass


class CalendarDraftValidationError(CalendarError):
    pass


class CalendarOperationInProgress(CalendarError):
    pass


class CalendarExecutionUncertain(CalendarError):
    pass


class CalendarAction(StrEnum):
    LIST = "LIST"
    CREATE = "CREATE"


class CalendarActionDraft(BaseModel):
    """Untrusted Skill output before deterministic calendar validation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["calendar-action-draft.v1"]
    action: CalendarAction
    title: str | None = Field(default=None, max_length=300)
    start_at: str | None = Field(default=None, max_length=64)
    end_at: str | None = Field(default=None, max_length=64)
    date_from: str | None = Field(default=None, max_length=10)
    date_to: str | None = Field(default=None, max_length=10)
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"

    @model_validator(mode="after")
    def validate_shape(self) -> "CalendarActionDraft":
        if self.title is not None:
            self.title = self.title.strip() or None
        if self.action == CalendarAction.LIST:
            if not self.date_from or not self.date_to:
                raise ValueError("LIST requires date_from and date_to")
            if any((self.title, self.start_at, self.end_at)):
                raise ValueError("LIST forbids event write fields")
        elif self.action == CalendarAction.CREATE:
            if not self.title or not self.start_at or not self.end_at:
                raise ValueError("CREATE requires title, start_at and end_at")
            # Some structured-output models redundantly copy the event date to
            # date_from/date_to. These fields have no authority for CREATE and
            # deterministic code below ignores them completely.
        return self


class CalendarResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["APPROVE", "REJECT"]
    expected_version: int = Field(ge=1)


class CalendarEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    title: str
    start_at: str
    end_at: str | None = None


class CalendarProposalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    action: Literal["CREATE"] = "CREATE"
    status: str
    version: int
    title: str
    start_at: str
    end_at: str
    timezone: str
    requires_approval: bool


@dataclass(frozen=True)
class CalendarAgentResult:
    display_response: str
    voice_response: str
    data: dict[str, Any]
    memory_status: str


class CalendarGateway(Protocol):
    provider_name: str

    @property
    def configured(self) -> bool: ...

    def list_events(self, *, time_min: str, time_max: str, limit: int) -> list[dict[str, Any]]: ...

    def get_event(self, event_id: str) -> dict[str, Any] | None: ...

    def create_event(self, *, event_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class UnconfiguredCalendarGateway:
    provider_name = "google-calendar"
    configured = False

    @staticmethod
    def _raise() -> None:
        raise CalendarConfigurationError(
            "Google Calendar 연결 정보가 설정되지 않았습니다."
        )

    def list_events(self, *, time_min: str, time_max: str, limit: int) -> list[dict[str, Any]]:
        del time_min, time_max, limit
        self._raise()

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        del event_id
        self._raise()

    def create_event(self, *, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        del event_id, payload
        self._raise()


class GoogleCalendarGateway:
    """Small OAuth refresh-token adapter for the official Calendar v3 API."""

    provider_name = "google-calendar"
    token_url = "https://oauth2.googleapis.com/token"
    api_base = "https://www.googleapis.com/calendar/v3"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        values = (client_id.strip(), client_secret.strip(), refresh_token.strip())
        if not all(values):
            raise CalendarConfigurationError(
                "Google Calendar OAuth 설정 세 값이 모두 필요합니다."
            )
        self.client_id, self.client_secret, self.refresh_token = values
        self.calendar_id = calendar_id.strip() or "primary"
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return True

    def list_events(self, *, time_min: str, time_max: str, limit: int) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            self._events_url(),
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(max(1, min(50, limit))),
            },
        )
        body = _response_object(response)
        items = body.get("items", [])
        if not isinstance(items, list):
            raise CalendarProviderError("Google Calendar 응답 형식이 올바르지 않습니다.")
        return [item for item in items if isinstance(item, dict)]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"{self._events_url()}/{quote(event_id, safe='')}",
            allow_not_found=True,
        )
        if response is None:
            return None
        return _response_object(response)

    def create_event(self, *, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_event(event_id)
        if existing is not None:
            return existing
        event_payload = dict(payload)
        event_payload["id"] = event_id
        try:
            response = self._request("POST", self._events_url(), json=event_payload)
        except CalendarProviderError:
            # A 409 means a previous ambiguous attempt created the deterministic
            # provider event. Re-read instead of issuing a second write.
            existing = self.get_event(event_id)
            if existing is not None:
                return existing
            raise
        return _response_object(response)

    def _events_url(self) -> str:
        return f"{self.api_base}/calendars/{quote(self.calendar_id, safe='')}/events"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        try:
            response = self.client.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        except httpx.TimeoutException:
            raise CalendarProviderTimeout("Google Calendar 응답 시간이 초과됐습니다.") from None
        except httpx.HTTPError:
            raise CalendarProviderError("Google Calendar에 연결하지 못했습니다.") from None
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise CalendarProviderError(
                f"Google Calendar 요청이 실패했습니다 ({response.status_code})."
            )
        return response

    def _token(self) -> str:
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
            try:
                response = self.client.post(
                    self.token_url,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": self.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except httpx.TimeoutException:
                raise CalendarProviderTimeout("Google OAuth 응답 시간이 초과됐습니다.") from None
            except httpx.HTTPError:
                raise CalendarProviderError("Google OAuth에 연결하지 못했습니다.") from None
            if response.status_code != 200:
                raise CalendarProviderError("Google Calendar 인증에 실패했습니다.")
            body = _response_object(response)
            token = body.get("access_token")
            if not isinstance(token, str) or not token.strip():
                raise CalendarProviderError("Google OAuth 토큰 응답이 올바르지 않습니다.")
            try:
                expires_in = max(60, int(body.get("expires_in", 3600)))
            except (TypeError, ValueError):
                expires_in = 3600
            self._access_token = token.strip()
            self._token_expires_at = time.monotonic() + expires_in - 30
            return self._access_token


def build_calendar_gateway(environment: dict[str, str] | None = None) -> CalendarGateway:
    values = os.environ if environment is None else environment
    enabled = values.get("GOOGLE_CALENDAR_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return UnconfiguredCalendarGateway()
    try:
        timeout = float(values.get("GOOGLE_CALENDAR_TIMEOUT_SECONDS", "20"))
    except ValueError:
        raise CalendarConfigurationError(
            "GOOGLE_CALENDAR_TIMEOUT_SECONDS must be numeric"
        ) from None
    return GoogleCalendarGateway(
        client_id=values.get("GOOGLE_CALENDAR_CLIENT_ID", ""),
        client_secret=values.get("GOOGLE_CALENDAR_CLIENT_SECRET", ""),
        refresh_token=values.get("GOOGLE_CALENDAR_REFRESH_TOKEN", ""),
        calendar_id=values.get("GOOGLE_CALENDAR_ID", "primary"),
        timeout_seconds=timeout,
    )


def is_calendar_request(content: str) -> bool:
    text = " ".join(content.strip().split())
    if not text:
        return False
    if "캘린더" in text:
        return True
    calendar_nouns = ("일정", "약속", "미팅", "회의")
    calendar_verbs = (
        "알려",
        "보여",
        "뭐",
        "있어",
        "확인",
        "추가",
        "등록",
        "잡아",
        "만들",
        "넣어",
    )
    return any(noun in text for noun in calendar_nouns) and any(
        verb in text for verb in calendar_verbs
    )


class CalendarManager:
    """Deterministic validation, approval ledger and Google write boundary."""

    def __init__(
        self,
        *,
        database: Database,
        repository: WorkRepository,
        gateway: CalendarGateway,
    ) -> None:
        self.database = database
        self.repository = repository
        self.gateway = gateway

    def handle_draft(
        self,
        *,
        user_id: str,
        run_id: str,
        output: dict[str, Any],
        list_tool,
    ) -> CalendarAgentResult:
        try:
            draft = CalendarActionDraft.model_validate(output)
        except ValidationError:
            raise CalendarDraftValidationError(
                "Calendar Skill 결과가 의미 검증을 통과하지 못했습니다."
            ) from None
        if draft.action == CalendarAction.LIST:
            if not self.gateway.configured:
                raise CalendarConfigurationError(
                    "Google Calendar 연결 정보가 설정되지 않았습니다."
                )
            try:
                start, end = _validated_date_range(draft)
            except ValueError as exc:
                raise CalendarDraftValidationError(str(exc)) from None
            events = list_tool(
                {
                    "time_min": start.isoformat(),
                    "time_max": end.isoformat(),
                    "limit": 20,
                }
            )
            views = [_event_view(item) for item in events]
            if not views:
                display = (
                    f"{draft.date_from}부터 {draft.date_to}까지 등록된 일정이 없습니다."
                )
            else:
                lines = [
                    f"- {_display_datetime(item.start_at)} · {item.title}"
                    for item in views
                ]
                display = "확인한 일정입니다.\n" + "\n".join(lines)
            return CalendarAgentResult(
                display_response=display,
                voice_response=f"일정 {len(views)}건을 확인했습니다.",
                data={
                    "calendar": {
                        "kind": "EVENT_LIST",
                        "events": [item.model_dump(mode="json") for item in views],
                        "date_from": draft.date_from,
                        "date_to": draft.date_to,
                    }
                },
                memory_status="NOT_APPLICABLE",
            )

        try:
            payload = _validated_create_payload(draft)
        except ValueError as exc:
            raise CalendarDraftValidationError(str(exc)) from None
        proposal = self._create_proposal(
            user_id=user_id,
            run_id=run_id,
            payload=payload,
        )
        self.repository.append_skill_event(
            user_id=user_id,
            run_id=run_id,
            event_type="CALENDAR_APPROVAL_REQUESTED",
            public_summary="Google Calendar 일정 등록 승인을 기다리고 있습니다.",
            payload={
                "proposal_id": proposal.proposal_id,
                "action": proposal.action,
                "version": proposal.version,
            },
        )
        return CalendarAgentResult(
            display_response=(
                f"‘{proposal.title}’ 일정을 { _display_datetime(proposal.start_at) }에 "
                "등록할까요? 승인하기 전에는 Google Calendar를 변경하지 않습니다."
            ),
            voice_response="일정 등록 전 확인이 필요합니다.",
            data={
                "calendar": {
                    "kind": "ACTION_PROPOSAL",
                    "proposal": proposal.model_dump(mode="json"),
                    "provider_configured": self.gateway.configured,
                }
            },
            memory_status="PENDING_APPROVAL",
        )

    def resolve(
        self,
        *,
        user_id: str,
        proposal_id: str,
        request: CalendarResolutionRequest,
        idempotency_key: str,
    ) -> CalendarProposalView:
        if request.action == "APPROVE" and not self.gateway.configured:
            raise CalendarConfigurationError(
                "Google Calendar 연결 정보가 설정되지 않았습니다."
            )
        key_hash = sha256_text(idempotency_key)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM calendar_action_proposals WHERE id = ? AND user_id = ?",
                (proposal_id, user_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("calendar proposal not found")
            if row["version"] != request.expected_version and row["status"] not in {
                "COMPLETED",
                "REJECTED",
                "UNKNOWN",
            }:
                raise VersionConflict("calendar proposal version changed")
            if row["decision_key_hash"] and row["decision_key_hash"] != key_hash:
                raise VersionConflict("calendar proposal was already decided")
            if row["status"] in {"COMPLETED", "REJECTED"}:
                return _proposal_view(row)
            now = utc_iso(self.database.clock.now_utc())
            if request.action == "REJECT":
                if row["status"] != "PENDING_APPROVAL":
                    raise VersionConflict("calendar proposal can no longer be rejected")
                connection.execute(
                    """
                    UPDATE calendar_action_proposals
                    SET status = 'REJECTED', version = version + 1,
                        decision_key_hash = ?, updated_at = ?, completed_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'PENDING_APPROVAL'
                    """,
                    (key_hash, now, now, proposal_id, user_id),
                )
                updated = connection.execute(
                    "SELECT * FROM calendar_action_proposals WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
                return _proposal_view(updated)
            if row["status"] == "EXECUTING":
                raise CalendarOperationInProgress("일정 등록을 처리하고 있습니다.")
            if row["status"] == "PENDING_APPROVAL":
                claimed = connection.execute(
                    """
                    UPDATE calendar_action_proposals
                    SET status = 'EXECUTING', version = version + 1,
                        decision_key_hash = ?, updated_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'PENDING_APPROVAL'
                      AND version = ?
                    """,
                    (
                        key_hash,
                        now,
                        proposal_id,
                        user_id,
                        request.expected_version,
                    ),
                )
                if claimed.rowcount != 1:
                    raise VersionConflict("calendar proposal was decided concurrently")
            elif row["status"] != "UNKNOWN":
                raise VersionConflict("calendar proposal cannot be approved")

        current = self.get_proposal(user_id=user_id, proposal_id=proposal_id, internal=True)
        payload = json.loads(current["payload_json"])
        event_id = current["provider_event_id"]
        try:
            if current["status"] == "UNKNOWN":
                result = self.gateway.get_event(event_id)
                if result is None:
                    raise CalendarExecutionUncertain(
                        "이전 등록 결과를 확인할 수 없어 두 번째 쓰기를 차단했습니다."
                    )
            else:
                result = self.gateway.create_event(event_id=event_id, payload=payload)
        except CalendarConfigurationError:
            self._mark_failed(proposal_id, "CONFIGURATION_ERROR")
            raise
        except CalendarExecutionUncertain:
            raise
        except Exception as exc:
            self._mark_unknown(proposal_id, type(exc).__name__)
            raise CalendarExecutionUncertain(
                "Google Calendar 반영 여부가 불확실하여 중복 등록을 차단했습니다."
            ) from exc

        now = utc_iso(self.database.clock.now_utc())
        result_summary = {
            "provider_event_id": event_id,
            "status": "CONFIRMED",
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE calendar_action_proposals
                SET status = 'COMPLETED', version = version + 1,
                    result_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND user_id = ? AND status IN ('EXECUTING', 'UNKNOWN')
                """,
                (canonical_json(result_summary), now, now, proposal_id, user_id),
            )
            updated = connection.execute(
                "SELECT * FROM calendar_action_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        self.repository.append_skill_event(
            user_id=user_id,
            run_id=updated["run_id"],
            event_type="CALENDAR_WRITE_COMPLETED",
            public_summary="승인된 일정을 Google Calendar에 등록했습니다.",
            payload={"proposal_id": proposal_id, "action": "CREATE"},
        )
        del result
        return _proposal_view(updated)

    def get_proposal(
        self,
        *,
        user_id: str,
        proposal_id: str,
        internal: bool = False,
    ) -> CalendarProposalView | dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM calendar_action_proposals WHERE id = ? AND user_id = ?",
                (proposal_id, user_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ResourceNotFound("calendar proposal not found")
        return dict(row) if internal else _proposal_view(row)

    def status(self) -> dict[str, Any]:
        return {
            "provider": "google-calendar",
            "state": "READY" if self.gateway.configured else "UNCONFIGURED",
            "message": (
                "Google Calendar 연결 준비됨"
                if self.gateway.configured
                else "Google Calendar 연결 필요"
            ),
        }

    def _create_proposal(
        self,
        *,
        user_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> CalendarProposalView:
        payload_json = canonical_json(payload)
        payload_hash = sha256_text(payload_json)
        provider_event_id = "b" + hashlib.sha256(
            f"{user_id}:{run_id}:{payload_hash}".encode("utf-8")
        ).hexdigest()[:40]
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM calendar_action_proposals WHERE run_id = ? AND user_id = ?",
                (run_id, user_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise VersionConflict("calendar proposal payload changed")
                return _proposal_view(existing)
            proposal_id = new_id("calprop")
            connection.execute(
                """
                INSERT INTO calendar_action_proposals(
                    id, user_id, run_id, action, status, version,
                    payload_json, payload_hash, provider_event_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'CREATE', 'PENDING_APPROVAL', 1, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    user_id,
                    run_id,
                    payload_json,
                    payload_hash,
                    provider_event_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM calendar_action_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        return _proposal_view(row)

    def _mark_unknown(self, proposal_id: str, error_code: str) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE calendar_action_proposals
                SET status = 'UNKNOWN', version = version + 1,
                    error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'EXECUTING'
                """,
                (error_code[:100], now, proposal_id),
            )

    def _mark_failed(self, proposal_id: str, error_code: str) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE calendar_action_proposals
                SET status = 'FAILED', version = version + 1,
                    error_code = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'EXECUTING'
                """,
                (error_code[:100], now, now, proposal_id),
            )


def _validated_date_range(draft: CalendarActionDraft) -> tuple[datetime, datetime]:
    try:
        start_date = date.fromisoformat(draft.date_from or "")
        end_date = date.fromisoformat(draft.date_to or "")
    except ValueError:
        raise ValueError("calendar date range must use ISO dates") from None
    if end_date < start_date or end_date - start_date > timedelta(days=366):
        raise ValueError("calendar date range is invalid or too large")
    zone = ZoneInfo(draft.timezone)
    return (
        datetime.combine(start_date, datetime_time.min, zone),
        datetime.combine(end_date + timedelta(days=1), datetime_time.min, zone),
    )


def _validated_create_payload(draft: CalendarActionDraft) -> dict[str, Any]:
    try:
        start = datetime.fromisoformat(draft.start_at or "")
        end = datetime.fromisoformat(draft.end_at or "")
    except ValueError:
        raise ValueError("calendar event times must be ISO datetimes") from None
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("calendar event times require UTC offsets")
    if end <= start or end - start > timedelta(days=7):
        raise ValueError("calendar event time range is invalid or too large")
    return {
        "summary": draft.title,
        "start": {"dateTime": start.isoformat(), "timeZone": draft.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": draft.timezone},
    }


def _event_view(item: dict[str, Any]) -> CalendarEventView:
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    start_value = start.get("dateTime") or start.get("date")
    end_value = end.get("dateTime") or end.get("date")
    event_id = item.get("id")
    if not isinstance(event_id, str) or not isinstance(start_value, str):
        raise CalendarProviderError("Google Calendar 일정 응답이 불완전합니다.")
    title = item.get("summary")
    return CalendarEventView(
        event_id=event_id,
        title=title.strip() if isinstance(title, str) and title.strip() else "제목 없는 일정",
        start_at=start_value,
        end_at=end_value if isinstance(end_value, str) else None,
    )


def _proposal_view(row) -> CalendarProposalView:
    payload = json.loads(row["payload_json"])
    return CalendarProposalView(
        proposal_id=row["id"],
        status=row["status"],
        version=row["version"],
        title=payload["summary"],
        start_at=payload["start"]["dateTime"],
        end_at=payload["end"]["dateTime"],
        timezone=payload["start"].get("timeZone", "Asia/Seoul"),
        requires_approval=row["status"] == "PENDING_APPROVAL",
    )


def _response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise CalendarProviderError("Google Calendar가 잘못된 JSON을 반환했습니다.") from None
    if not isinstance(body, dict):
        raise CalendarProviderError("Google Calendar 응답 형식이 올바르지 않습니다.")
    return body


def _display_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
