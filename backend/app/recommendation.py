from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.work_queries import StructuredWorkQueryService


RECOMMENDATION_POLICY_VERSION = "recommend-v1"
ELIGIBLE_STATUSES = frozenset({"BLOCKED", "IN_PROGRESS", "TODO", "WAITING"})
PRIORITY_ORDER = {"HIGH": 0, "NORMAL": 1, "LOW": 2}
PRIORITY_POINTS = {"HIGH": 20, "NORMAL": 10, "LOW": 0}
MAX_RECOMMENDATION_CANDIDATES = 10_000
WAITING_FOLLOW_UP_DAYS = 3

# Narration is presentation-only, so a conservative false negative is safer
# than allowing a fluent sentence to reverse a code-owned fact. Legitimate
# negative facts keep the deterministic Korean copy instead.
NEGATIVE_POLARITY_PATTERNS = (
    re.compile(r"(?<![가-힣])안(?:\s|$|됨|됩|되|하|합|했|할|한|맞)"),
    re.compile(r"(?<![가-힣])못(?:\s|$|함|합|하|했|할|한|해|되|됨|됩)"),
    re.compile(
        r"(?<![가-힣])미(?:완료|완성|처리|확인|수신|응답|승인|정|해결|진행|실행|작업|구현|반영|착수|제출|배포)"
    ),
    re.compile(r"(?:없음|없습니다|없는|없어|없고|없다|없을|없었|없겠)"),
    re.compile(
        r"(?:않음|않습니다|않는|않아|않고|않다|않았|않을|않게|않기로)"
    ),
    re.compile(r"(?:아님|아닙니다|아닌|아니다|아닐)"),
    re.compile(r"(?:불가|불가능|금지|취소|실패|중단|제외)"),
)


class RecommendationNarrationFact(BaseModel):
    """Code-owned facts that a narrator may only phrase more naturally."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1)
    work_item_id: str = Field(min_length=1, max_length=200)
    project_name: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    recommended_action: str = Field(min_length=1, max_length=500)
    reason_facts: list[str] = Field(min_length=1, max_length=10)


class RecommendationNarrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["recommendation-narration-input.v1"] = (
        "recommendation-narration-input.v1"
    )
    items: list[RecommendationNarrationFact] = Field(max_length=20)


class RecommendationNarrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_id: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("explanation")
    @classmethod
    def explanation_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("explanation must not be blank")
        return normalized


class RecommendationNarrationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["recommendation-narration.v1"] = (
        "recommendation-narration.v1"
    )
    items: list[RecommendationNarrationItem] = Field(max_length=20)


class RecommendationNarrator(Protocol):
    """Optional presentation-only port; it cannot read or write work memory."""

    provider_name: str
    model_name: str | None
    version: str

    def narrate(
        self,
        request: RecommendationNarrationRequest,
    ) -> RecommendationNarrationEnvelope: ...


class RecommendationNarratorTimeoutError(TimeoutError):
    """Transport adapters should normalize provider timeouts to this error."""


class RecommendationNarrationMismatchError(ValueError):
    pass


class RecommendationNarrationGroundingError(ValueError):
    pass


class RecommendationPresentationService:
    """Validates optional narration without giving it control over ranking.

    Any failure invalidates the whole narrated response. This prevents a
    partially accepted response from mixing reordered or missing LLM output
    with deterministic recommendation facts.
    """

    def __init__(self, narrator: RecommendationNarrator | None = None) -> None:
        self.narrator = narrator

    def present(self, recommendations: list[dict]) -> dict:
        request = self._request_from(recommendations)
        deterministic_items = self._deterministic_items(recommendations)
        if self.narrator is None or not recommendations:
            return {
                "mode": "DETERMINISTIC",
                "provider_name": None,
                "model_name": None,
                "fallback_reason": None,
                "items": deterministic_items,
            }

        try:
            raw: Any = self.narrator.narrate(request)
            if isinstance(raw, str):
                envelope = RecommendationNarrationEnvelope.model_validate_json(raw)
            else:
                envelope = RecommendationNarrationEnvelope.model_validate(raw)
            expected_ids = [item.work_item_id for item in request.items]
            received_ids = [item.work_item_id for item in envelope.items]
            if received_ids != expected_ids:
                raise RecommendationNarrationMismatchError(
                    "narrator item ids must exactly match code-owned rank order"
                )
            for fact, item in zip(request.items, envelope.items, strict=True):
                if not self._explanation_is_grounded(fact, item.explanation):
                    raise RecommendationNarrationGroundingError(
                        "narrator explanation introduced an unknown fact"
                    )
            return {
                "mode": "NARRATED",
                "provider_name": self.narrator.provider_name,
                "model_name": self.narrator.model_name,
                "fallback_reason": None,
                "items": [item.model_dump(mode="json") for item in envelope.items],
            }
        except TimeoutError:
            fallback_reason = "NARRATOR_TIMEOUT"
        except RecommendationNarrationMismatchError:
            fallback_reason = "NARRATOR_ORDER_OR_ID_MISMATCH"
        except RecommendationNarrationGroundingError:
            fallback_reason = "NARRATOR_UNGROUNDED"
        except ValidationError:
            fallback_reason = "NARRATOR_SCHEMA_INVALID"
        except Exception:
            # Provider-specific HTTP/JSON errors are presentation failures only.
            # Do not leak their messages because they may contain provider data.
            fallback_reason = "NARRATOR_FAILED"

        return {
            "mode": "DETERMINISTIC_FALLBACK",
            "provider_name": getattr(self.narrator, "provider_name", None),
            "model_name": getattr(self.narrator, "model_name", None),
            "fallback_reason": fallback_reason,
            "items": deterministic_items,
        }

    @staticmethod
    def _request_from(
        recommendations: list[dict],
    ) -> RecommendationNarrationRequest:
        return RecommendationNarrationRequest(
            items=[
                RecommendationNarrationFact(
                    rank=recommendation["rank"],
                    work_item_id=recommendation["work_item"]["work_item_id"],
                    project_name=recommendation["work_item"]["project_name"],
                    title=recommendation["work_item"]["title"],
                    recommended_action=recommendation["recommended_action"],
                    reason_facts=[
                        reason["detail"] for reason in recommendation["reasons"]
                    ],
                )
                for recommendation in recommendations
            ]
        )

    @staticmethod
    def _deterministic_items(recommendations: list[dict]) -> list[dict[str, str]]:
        items = []
        for recommendation in recommendations:
            reason_facts = [
                reason["detail"] for reason in recommendation.get("reasons", [])
            ]
            explanation = " ".join(reason_facts[:2])
            if not explanation:
                explanation = (
                    f"다음 행동은 {recommendation['recommended_action']}입니다."
                )
            items.append(
                {
                    "work_item_id": recommendation["work_item"]["work_item_id"],
                    "explanation": explanation,
                }
            )
        return items

    @classmethod
    def _explanation_is_grounded(
        cls,
        fact: RecommendationNarrationFact,
        explanation: str,
    ) -> bool:
        if cls._contains_negative_polarity(explanation):
            return False
        source_text = " ".join(
            [
                fact.project_name,
                fact.title,
                fact.recommended_action,
                *fact.reason_facts,
            ]
        )
        allowed_tokens = cls._tokens(source_text) | {
            "업무",
            "작업",
            "현재",
            "먼저",
            "우선",
            "우선순위가",
            "높고",
            "이유",
            "다음",
            "행동",
            "행동이",
            "최근",
            "후속",
            "확인",
            "진행",
            "이어갈",
            "이어서",
            "진행할",
            "다음으로",
            "시작",
            "좋습니다",
            "있습니다",
            "있어",
            "때문에",
            "정리되어",
            "기록되어",
            "기다리고",
            "시점입니다",
        }
        if any(token not in allowed_tokens for token in cls._tokens(explanation)):
            return False
        assertion_markers = (
            "완료",
            "승인",
            "회신",
            "막",
            "대기",
            "긴급",
            "우선순위",
        )
        if any(
            marker in explanation and marker not in source_text
            for marker in assertion_markers
        ):
            return False
        source_numbers = set(re.findall(r"\d+", source_text))
        return all(
            number in source_numbers for number in re.findall(r"\d+", explanation)
        )

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9가-힣]+", value)
            if len(token) > 1
        }

    @staticmethod
    def _contains_negative_polarity(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value.strip().casefold())
        return any(pattern.search(normalized) for pattern in NEGATIVE_POLARITY_PATTERNS)


class RecommendationPolicy:
    def rank(self, items: list[dict], *, today: date, limit: int = 5) -> list[dict]:
        if limit <= 0:
            return []

        ranked = [
            self._score(item, today=today)
            for item in items
            if self._is_recommendation_candidate(item, today=today)
        ]
        ranked.sort(
            key=lambda item: (
                -item["score"],
                PRIORITY_ORDER[item["work_item"]["priority"]],
                self._reference_date_for_sort(item["work_item"]),
                item["work_item"]["work_item_id"],
            )
        )
        selected = ranked[:limit]
        for index, item in enumerate(selected, start=1):
            item["rank"] = index
        return selected

    def _score(self, item: dict, *, today: date) -> dict:
        status = item["status"]
        next_action = item.get("next_action")
        age_days, reference_kind = self._age_days(item, today=today)

        score_breakdown = {
            "status": 0,
            "priority": 0,
            "next_action": 0,
            "recent_response": 0,
            "staleness": 0,
        }
        reasons: list[dict[str, str]] = []
        if status == "BLOCKED" and next_action:
            score_breakdown["status"] = 55
            reasons.append(
                {
                    "code": "EXPLICIT_UNBLOCK_ACTION",
                    "detail": "막힌 업무지만 실행할 다음 행동이 정리되어 있습니다.",
                }
            )
        elif status == "BLOCKED":
            score_breakdown["status"] = 10
            reasons.append(
                {
                    "code": "BLOCKED_NEEDS_DIAGNOSIS",
                    "detail": "막힌 이유를 먼저 확인해야 합니다.",
                }
            )
        elif status == "IN_PROGRESS":
            score_breakdown["status"] = 45
            reasons.append(
                {"code": "IN_PROGRESS", "detail": "현재 진행 중인 업무입니다."}
            )
        elif status == "TODO":
            score_breakdown["status"] = 30
            reasons.append(
                {"code": "TODO", "detail": "아직 시작하지 않은 업무입니다."}
            )
        elif status == "WAITING":
            score_breakdown["status"] = 25
            reasons.append(
                {
                    "code": "WAITING_FOLLOWUP_DUE",
                    "detail": f"회신 대기 후 {age_days}일이 지나 확인할 시점입니다.",
                }
            )

        score_breakdown["priority"] = PRIORITY_POINTS[item["priority"]]
        if item["priority"] == "HIGH":
            reasons.append(
                {"code": "HIGH_PRIORITY", "detail": "높은 우선순위로 지정되었습니다."}
            )

        if next_action:
            score_breakdown["next_action"] = 15
            reasons.append(
                {
                    "code": "NEXT_ACTION_PRESENT",
                    "detail": f"다음 행동은 ‘{next_action}’입니다.",
                }
            )

        if (
            item.get("latest_activity_kind") == "RESPONSE_RECEIVED"
            and status != "WAITING"
            and item.get("last_activity_on") is not None
            and age_days <= 7
        ):
            score_breakdown["recent_response"] = 15
            reasons.append(
                {
                    "code": "RECENT_RESPONSE_RECEIVED",
                    "detail": "최근 회신이 기록되어 후속 작업을 진행할 수 있습니다.",
                }
            )

        if age_days >= 14:
            score_breakdown["staleness"] = 15
            reasons.append(
                {
                    "code": "STALE_14D",
                    "detail": self._staleness_detail(
                        reference_kind, age_days, "동안 업데이트가 없습니다."
                    ),
                }
            )
        elif age_days >= 7:
            score_breakdown["staleness"] = 10
            reasons.append(
                {
                    "code": "STALE_7D",
                    "detail": self._staleness_detail(
                        reference_kind, age_days, "이 지났습니다."
                    ),
                }
            )
        elif age_days >= 3:
            score_breakdown["staleness"] = 5
            reasons.append(
                {
                    "code": "STALE_3D",
                    "detail": self._staleness_detail(
                        reference_kind, age_days, "이 지났습니다."
                    ),
                }
            )

        if status == "WAITING":
            waiting_for = item.get("waiting_for") or "요청 회신"
            action = f"{waiting_for} 확인"
        elif next_action:
            action = next_action
        elif status == "BLOCKED":
            action = f"{item.get('blocked_reason') or '막힌 원인'}의 해결 조건 확인"
        elif status == "TODO":
            action = f"{item['title']} 시작 조건 확인"
        else:
            action = f"{item['title']} 작업 이어서 진행"

        score = sum(score_breakdown.values())
        return {
            "rank": 0,
            "score": score,
            "score_breakdown": score_breakdown,
            "age_days": age_days,
            "policy_version": RECOMMENDATION_POLICY_VERSION,
            "recommended_action": action,
            "reason_codes": [reason["code"] for reason in reasons],
            "reasons": reasons,
            "work_item": item,
        }

    @classmethod
    def _is_recommendation_candidate(cls, item: dict, *, today: date) -> bool:
        status = item.get("status")
        if status not in ELIGIBLE_STATUSES:
            return False
        if status != "WAITING":
            return True
        age_days, _ = cls._age_days(item, today=today)
        return age_days >= WAITING_FOLLOW_UP_DAYS

    @classmethod
    def _age_days(cls, item: dict, *, today: date) -> tuple[int, str | None]:
        reference_date, reference_kind = cls._reference_date(item)
        age_days = (
            max(0, (today - reference_date).days) if reference_date else 0
        )
        return age_days, reference_kind

    @staticmethod
    def _reference_date(item: dict) -> tuple[date | None, str | None]:
        raw = item.get("last_activity_on")
        kind = "ACTIVITY"
        if not raw:
            raw = item.get("updated_at")
            kind = "UPDATE"
        if not raw:
            return None, None
        try:
            return date.fromisoformat(str(raw)[:10]), kind
        except ValueError:
            return None, None

    @classmethod
    def _reference_date_for_sort(cls, item: dict) -> str:
        reference_date, _ = cls._reference_date(item)
        # Unknown dates are not treated as abandoned work. The stable id
        # tie-breaker still makes repeated ranking byte-for-byte deterministic.
        return reference_date.isoformat() if reference_date else "9999-12-31"

    @staticmethod
    def _staleness_detail(
        reference_kind: str | None,
        age_days: int,
        suffix: str,
    ) -> str:
        subject = "마지막 활동" if reference_kind == "ACTIVITY" else "마지막 업무 갱신"
        separator = " " if suffix.startswith("동안") else ""
        return f"{subject} 이후 {age_days}일{separator}{suffix}"


class RecommendationService:
    def __init__(
        self,
        query_service: StructuredWorkQueryService,
        policy: RecommendationPolicy | None = None,
    ) -> None:
        self.query_service = query_service
        self.policy = policy or RecommendationPolicy()

    def recommend(self, *, user_id: str, today: date, limit: int = 5) -> list[dict]:
        candidates = self.query_service.list_by_status(
            user_id=user_id,
            statuses=("BLOCKED", "IN_PROGRESS", "TODO", "WAITING"),
            # Candidate selection must not let the SQL presentation ordering
            # decide the final priority. The policy owns the ranking.
            limit=MAX_RECOMMENDATION_CANDIDATES,
        )
        return self.policy.rank(candidates, today=today, limit=limit)
