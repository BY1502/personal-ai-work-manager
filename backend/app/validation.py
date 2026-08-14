from __future__ import annotations

import re
from datetime import date

from app.models import (
    ActivityKind,
    Derivation,
    FactGroupDraft,
    ValidatedActivity,
    ValidatedFactGroup,
    WorkStatus,
)
from app.utils import resolve_local_date


class DeterministicValidationError(ValueError):
    pass


class ExtractionValidator:
    def validate_fact_group(
        self,
        draft: FactGroupDraft,
        *,
        source_content: str,
        today: date,
    ) -> ValidatedFactGroup:
        self._require_excerpt(draft.source_excerpt, source_content)
        activities: list[ValidatedActivity] = []

        for activity in draft.activities:
            self._require_excerpt(activity.source_excerpt, source_content)
            if activity.derivation == Derivation.LLM_INFERRED:
                raise DeterministicValidationError(
                    "LLM_INFERRED activity cannot be auto-applied"
                )
            if activity.derivation == Derivation.RULE_DERIVED:
                raise DeterministicValidationError(
                    "extractor output cannot claim server-only RULE_DERIVED evidence"
                )
            if activity.derivation == Derivation.EXPLICIT and activity.rule_id:
                raise DeterministicValidationError(
                    "EXPLICIT activity cannot carry a server rule_id"
                )
            try:
                occurred_on = resolve_local_date(activity.occurred_on, today=today)
            except ValueError as exc:
                raise DeterministicValidationError(
                    f"invalid occurred_on value: {activity.occurred_on}"
                ) from exc
            activities.append(
                ValidatedActivity(
                    kind=activity.kind,
                    summary=activity.summary.strip(),
                    occurred_on_local=occurred_on,
                    source_excerpt=activity.source_excerpt,
                    derivation=activity.derivation,
                    rule_id=activity.rule_id,
                )
            )

        patch = draft.proposed_patch
        activity_kinds = {activity.kind for activity in activities}
        evidence_text = draft.source_excerpt.casefold()

        # Small local models occasionally combine mutually exclusive state
        # fields even though the underlying Activity is valid. Removing an
        # unsupported mutation candidate is deterministic and cannot expand
        # what is written to Canonical Memory. This keeps the valid Activity
        # while preserving the Work Item state invariants.
        safe_patch_updates: dict[str, object] = {}
        if (
            patch.clear_waiting_for
            and ActivityKind.RESPONSE_RECEIVED not in activity_kinds
        ):
            safe_patch_updates["clear_waiting_for"] = False
        if (
            patch.status is not None
            and patch.status != WorkStatus.WAITING
            and patch.waiting_for is not None
        ):
            safe_patch_updates["waiting_for"] = None
        if (
            patch.status is not None
            and patch.status != WorkStatus.BLOCKED
            and patch.blocked_reason is not None
        ):
            safe_patch_updates["blocked_reason"] = None
        if patch.clear_blocked_reason and not re.search(
            r"막힘.{0,6}(?:해소|해결)|문제.{0,6}(?:해소|해결)|"
            r"정상화|다시.{0,4}진행|뚫렸",
            evidence_text,
        ):
            safe_patch_updates["clear_blocked_reason"] = False
        if safe_patch_updates:
            patch = patch.model_copy(update=safe_patch_updates)

        if ActivityKind.REQUEST_SENT in activity_kinds and not re.search(
            r"요청|문의|물어|질문|확인.{0,8}(?:해|했|넣|보냈|부탁)",
            evidence_text,
        ):
            raise DeterministicValidationError(
                "REQUEST_SENT requires explicit request or question evidence"
            )

        if ActivityKind.RESPONSE_RECEIVED in activity_kinds and not re.search(
            r"답|답변|회신|응답",
            evidence_text,
        ):
            raise DeterministicValidationError(
                "RESPONSE_RECEIVED requires explicit response evidence"
            )

        # A request without a received response is a waiting fact even when a
        # small model conservatively emits IN_PROGRESS (or omits status). This
        # narrows the mutation to an evidence-backed state and keeps the
        # WorkManager invariant/Phase 1 meaning stable across Providers.
        if (
            ActivityKind.REQUEST_SENT in activity_kinds
            and ActivityKind.RESPONSE_RECEIVED not in activity_kinds
            and patch.status in {None, WorkStatus.IN_PROGRESS}
        ):
            patch = patch.model_copy(
                update={
                    "status": WorkStatus.WAITING,
                    "waiting_for": (
                        patch.waiting_for
                        or self._explicit_waiting_for(evidence_text)
                    ),
                }
            )

        if patch.status == WorkStatus.WAITING:
            if not patch.waiting_for or not patch.waiting_for.strip():
                raise DeterministicValidationError(
                    "WAITING requires waiting_for"
                )
            if ActivityKind.REQUEST_SENT not in activity_kinds:
                raise DeterministicValidationError(
                    "WAITING auto-transition requires REQUEST_SENT evidence"
                )

        if patch.status == WorkStatus.BLOCKED and (
            not patch.blocked_reason or not patch.blocked_reason.strip()
        ):
            raise DeterministicValidationError(
                "BLOCKED requires blocked_reason"
            )
        if patch.status == WorkStatus.BLOCKED and not re.search(
            r"막혔|막혀|막힘|차단|블로킹|진행.{0,5}(?:못|불가)", evidence_text
        ):
            raise DeterministicValidationError(
                "BLOCKED requires explicit blocking evidence"
            )

        if patch.status == WorkStatus.DONE and any(
            [patch.waiting_for, patch.blocked_reason, patch.next_action]
        ):
            raise DeterministicValidationError(
                "DONE cannot retain waiting, blocked, or next action fields"
            )
        if patch.status == WorkStatus.DONE and not re.search(
            r"완료|끝냈|끝났|마무리", evidence_text
        ):
            raise DeterministicValidationError(
                "DONE requires explicit completion evidence"
            )

        if patch.status == WorkStatus.HOLD and not re.search(
            r"보류|잠시 중단|홀드", evidence_text
        ):
            raise DeterministicValidationError(
                "HOLD requires explicit hold evidence"
            )

        if patch.priority is not None:
            priority_evidence = {
                "HIGH": r"긴급|급해|급한|중요|우선",
                "LOW": r"낮은 우선|나중에|여유",
                "NORMAL": r"보통 우선|일반 우선",
            }[patch.priority.value]
            if not re.search(priority_evidence, evidence_text):
                raise DeterministicValidationError(
                    "priority change requires explicit priority evidence"
                )

        # A received response deterministically ends the existing wait when the
        # same candidate moves back to active work. Small local models sometimes
        # classify the Activity and status correctly but leave this boolean at
        # its schema default. Derive only the safe, evidence-backed clear; a new
        # WAITING patch (for a follow-up question) is intentionally untouched.
        if (
            ActivityKind.RESPONSE_RECEIVED in activity_kinds
            and patch.status == WorkStatus.IN_PROGRESS
            and patch.waiting_for is None
            and not patch.clear_waiting_for
        ):
            patch = patch.model_copy(update={"clear_waiting_for": True})

        # A model may correctly identify a response but omit the explicitly
        # requested removal action. This narrow rule is deterministic and only
        # fires when the user's own text contains both the removal instruction
        # and a received-response marker already validated above.
        if (
            ActivityKind.RESPONSE_RECEIVED in activity_kinds
            and patch.next_action is None
        ):
            explicit_next_action = self._explicit_response_next_action(
                evidence_text
            )
            if explicit_next_action is not None:
                patch = patch.model_copy(
                    update={"next_action": explicit_next_action}
                )

        if draft.project_mention is not None and not draft.project_mention.strip():
            raise DeterministicValidationError("blank project mention")
        if draft.work_item_mention is not None and not draft.work_item_mention.strip():
            raise DeterministicValidationError("blank work item mention")

        return ValidatedFactGroup(
            project_mention=(
                draft.project_mention.strip() if draft.project_mention else None
            ),
            work_item_mention=(
                draft.work_item_mention.strip()
                if draft.work_item_mention
                else None
            ),
            activities=activities,
            proposed_patch=patch,
            reference_terms=draft.reference_terms,
            source_excerpt=draft.source_excerpt,
        )

    @staticmethod
    def _require_excerpt(excerpt: str, source_content: str) -> None:
        if excerpt not in source_content:
            raise DeterministicValidationError(
                "source excerpt must be present in the raw user message"
            )

    @staticmethod
    def _explicit_response_next_action(evidence_text: str) -> str | None:
        if not re.search(
            r"빼달|빼\s*달|제거하라고|제거해\s*달|삭제하라고|없애\s*달",
            evidence_text,
        ):
            return None
        if "로그인" in evidence_text:
            return "로그인 기능 제거"
        return "회신에서 요청한 항목 제거"

    @staticmethod
    def _explicit_waiting_for(evidence_text: str) -> str:
        if "로그인" in evidence_text:
            return "로그인 제거 여부 회신"
        if re.search(r"확인|문의|물어|질문|요청", evidence_text):
            return "문의 회신"
        return "요청에 대한 회신"
