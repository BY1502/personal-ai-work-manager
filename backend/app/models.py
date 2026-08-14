from __future__ import annotations

from enum import StrEnum
from datetime import date
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class Intent(StrEnum):
    CAPTURE_WORK = "CAPTURE_WORK"
    QUERY_WORK = "QUERY_WORK"
    GENERATE_REPORT = "GENERATE_REPORT"
    GENERAL = "GENERAL"


class WorkStatus(StrEnum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    HOLD = "HOLD"
    DONE = "DONE"


class Priority(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class ActivityKind(StrEnum):
    WORK_PERFORMED = "WORK_PERFORMED"
    REQUEST_SENT = "REQUEST_SENT"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    DECISION = "DECISION"
    NOTE = "NOTE"


class Derivation(StrEnum):
    EXPLICIT = "EXPLICIT"
    RULE_DERIVED = "RULE_DERIVED"
    LLM_INFERRED = "LLM_INFERRED"


class QueryType(StrEnum):
    PROJECT_STATUS = "PROJECT_STATUS"
    FOCUSED_WORK_STATUS = "FOCUSED_WORK_STATUS"
    CURRENT_WORK = "CURRENT_WORK"
    WAITING_WORK = "WAITING_WORK"
    BLOCKED_WORK = "BLOCKED_WORK"
    NEXT_ACTIONS = "NEXT_ACTIONS"
    TODAY_ACTIVITY = "TODAY_ACTIVITY"
    WEEK_ACTIVITY = "WEEK_ACTIVITY"
    RECENT_COMPLETED = "RECENT_COMPLETED"
    RECOMMEND_NEXT = "RECOMMEND_NEXT"
    DAILY_REPORT = "DAILY_REPORT"
    WEEKLY_REPORT = "WEEKLY_REPORT"
    PROJECT_REPORT = "PROJECT_REPORT"
    RANGE_REPORT = "RANGE_REPORT"


REPORT_QUERY_TYPES: frozenset[QueryType] = frozenset(
    {
        QueryType.DAILY_REPORT,
        QueryType.WEEKLY_REPORT,
        QueryType.PROJECT_REPORT,
        QueryType.RANGE_REPORT,
    }
)


class LinkDecisionType(StrEnum):
    AUTO_LINK = "AUTO_LINK"
    CREATE_NEW = "CREATE_NEW"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    UNRESOLVED = "UNRESOLVED"


class ActivityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ActivityKind
    summary: str = Field(min_length=1, max_length=500)
    occurred_on: str = Field(
        default="TODAY",
        description="TODAY, YESTERDAY, or an ISO local date. Code resolves it.",
    )
    source_excerpt: str = Field(min_length=1, max_length=10_000)
    derivation: Derivation = Derivation.EXPLICIT
    rule_id: str | None = None


class WorkItemPatchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WorkStatus | None = None
    priority: Priority | None = None
    waiting_for: str | None = Field(default=None, max_length=500)
    blocked_reason: str | None = Field(default=None, max_length=500)
    next_action: str | None = Field(default=None, max_length=500)
    clear_waiting_for: bool = False
    clear_blocked_reason: bool = False

    def has_changes(self) -> bool:
        return any(
            [
                self.status is not None,
                self.priority is not None,
                self.waiting_for is not None,
                self.blocked_reason is not None,
                self.next_action is not None,
                self.clear_waiting_for,
                self.clear_blocked_reason,
            ]
        )


class FactGroupDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_mention: str | None = Field(default=None, max_length=200)
    work_item_mention: str | None = Field(default=None, max_length=300)
    activities: list[ActivityDraft] = Field(default_factory=list, max_length=20)
    proposed_patch: WorkItemPatchDraft = Field(default_factory=WorkItemPatchDraft)
    reference_terms: list[str] = Field(default_factory=list, max_length=20)
    source_excerpt: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def require_memory_fact(self) -> "FactGroupDraft":
        if not self.activities and not self.proposed_patch.has_changes():
            raise ValueError("fact group must contain an activity or a work item patch")
        return self


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_type: QueryType
    project_mention: str | None = Field(default=None, max_length=200)
    date_from: str | None = Field(default=None, max_length=32)
    date_to: str | None = Field(default=None, max_length=32)


class ExtractionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["work-fact-draft.v1"] = "work-fact-draft.v1"
    intent: Intent
    fact_groups: list[FactGroupDraft] = Field(default_factory=list, max_length=8)
    query: QueryDraft | None = None

    @model_validator(mode="after")
    def validate_intent_payload(self) -> "ExtractionEnvelope":
        if self.intent == Intent.CAPTURE_WORK:
            if not self.fact_groups or self.query is not None:
                raise ValueError("CAPTURE_WORK requires fact_groups and forbids query")
        elif self.intent == Intent.QUERY_WORK:
            if self.query is None or self.fact_groups:
                raise ValueError(
                    "QUERY_WORK requires query and forbids fact_groups"
                )
            if self.query.query_type in REPORT_QUERY_TYPES:
                raise ValueError("QUERY_WORK forbids report query types")
        elif self.intent == Intent.GENERATE_REPORT:
            if self.query is None or self.fact_groups:
                raise ValueError(
                    "GENERATE_REPORT requires query and forbids fact_groups"
                )
            if self.query.query_type not in REPORT_QUERY_TYPES:
                raise ValueError("GENERATE_REPORT requires a report query type")
        elif self.fact_groups or self.query is not None:
            raise ValueError("GENERAL forbids fact_groups and query")
        return self


class ValidatedActivity(BaseModel):
    kind: ActivityKind
    summary: str
    occurred_on_local: date
    source_excerpt: str
    derivation: Derivation
    rule_id: str | None = None


class ValidatedFactGroup(BaseModel):
    project_mention: str | None
    work_item_mention: str | None
    activities: list[ValidatedActivity]
    proposed_patch: WorkItemPatchDraft
    reference_terms: list[str]
    source_excerpt: str


class ChatRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, validation_alias=AliasChoices("conversation_id", "conversationId"))
    client_message_id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("client_message_id", "clientMessageId"),
    )
    content: str = Field(
        min_length=1,
        max_length=10_000,
        validation_alias=AliasChoices("content", "message", "user_message"),
    )


class ReceiptView(BaseModel):
    receipt_id: str
    fact_group_id: str
    project_id: str
    project_name: str
    work_item_id: str
    work_item_title: str
    link_decision: LinkDecisionType
    link_score: float | None
    created_project: bool
    created_work_item: bool
    activity_ids: list[str]
    activity_summaries: list[str]
    status_before: WorkStatus | None
    status_after: WorkStatus
    waiting_for: str | None
    next_action: str | None
    changed_fields: list[str]


class CandidateView(BaseModel):
    project_id: str
    project_name: str
    work_item_id: str
    work_item_title: str
    status: WorkStatus
    waiting_for: str | None
    next_action: str | None
    version: int
    score: float
    evidence: list[str]


class ClarificationView(BaseModel):
    clarification_id: str
    question: str
    candidates: list[CandidateView]


class JarvisResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: Literal["COMPLETED", "NEEDS_CLARIFICATION", "FAILED"]
    display_response: str
    voice_response: str
    audio_url: str | None = None
    audio_duration_seconds: float | None = None
    receipts: list[ReceiptView] = Field(default_factory=list)
    clarification: ClarificationView | None = None
    data: dict[str, Any] | None = None


class ResolveClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["SELECT_EXISTING", "CREATE_NEW", "CANCEL"]
    work_item_id: str | None = None
    project_name: str | None = Field(default=None, min_length=1, max_length=200)
    work_item_title: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_selection(self) -> "ResolveClarificationRequest":
        if self.action == "SELECT_EXISTING" and not self.work_item_id:
            raise ValueError("SELECT_EXISTING requires work_item_id")
        if self.action == "CREATE_NEW" and (
            not self.project_name
            or not self.project_name.strip()
            or not self.work_item_title
            or not self.work_item_title.strip()
        ):
            raise ValueError(
                "CREATE_NEW requires project_name and work_item_title"
            )
        if self.action != "SELECT_EXISTING" and self.work_item_id is not None:
            raise ValueError("work_item_id is only valid for SELECT_EXISTING")
        if self.action != "CREATE_NEW" and (
            self.project_name is not None or self.work_item_title is not None
        ):
            raise ValueError(
                "project_name and work_item_title are only valid for CREATE_NEW"
            )
        if self.project_name is not None:
            self.project_name = self.project_name.strip()
        if self.work_item_title is not None:
            self.work_item_title = self.work_item_title.strip()
        return self


class RelinkActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_work_item_id: str
    expected_activity_version: int
    expected_link_version: int
    reason: str = Field(min_length=1, max_length=500)
    correction_run_id: str | None = Field(default=None, min_length=1, max_length=128)


class RelinkActivityResponse(BaseModel):
    activity_id: str
    previous_work_item_id: str
    target_work_item_id: str
    new_link_id: str
    status: Literal["COMPLETED"] = "COMPLETED"
