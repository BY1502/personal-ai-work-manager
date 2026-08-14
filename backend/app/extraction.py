from __future__ import annotations

import re
from typing import Protocol

from app.models import (
    ActivityDraft,
    ActivityKind,
    Derivation,
    ExtractionEnvelope,
    FactGroupDraft,
    Intent,
    QueryDraft,
    QueryType,
    WorkItemPatchDraft,
    WorkStatus,
)


class ExtractionProvider(Protocol):
    provider_name: str
    version: str
    model_name: str | None
    prompt_version: str

    def extract(self, content: str) -> ExtractionEnvelope: ...


class DeterministicTestProvider:
    """Acceptance-slice adapter that emits the same strict schema as an LLM.

    It intentionally covers the approved vertical-slice language only. A real
    Local/API LLM transport can replace this provider without receiving a DB
    handle or bypassing validation.
    """

    provider_name = "deterministic"
    version = "deterministic-ko-v3"
    model_name = None
    prompt_version = "deterministic-rules-v3"

    def extract(self, content: str) -> ExtractionEnvelope:
        text = content.strip()
        project = self._project_mention(text)

        range_match = re.search(
            r"(\d{4}-\d{2}-\d{2})\s*(?:부터|~|〜)\s*"
            r"(\d{4}-\d{2}-\d{2})(?:까지)?",
            text,
        )
        report_request = re.search(
            r"(?:보고서|(?:일일|주간|업무)\s*보고(?:서)?)",
            text,
        )
        if report_request and range_match:
            return self._report(
                QueryType.RANGE_REPORT,
                project,
                date_from=range_match.group(1),
                date_to=range_match.group(2),
            )

        if report_request and ("오늘" in text or "일일" in text):
            return self._report(QueryType.DAILY_REPORT, project)

        if report_request and (
            "주간" in text or re.search(r"이번\s*주", text)
        ):
            return self._report(QueryType.WEEKLY_REPORT, project)

        if project and report_request:
            return self._report(QueryType.PROJECT_REPORT, project)

        if (
            "지금 뭐부터" in text
            or "뭐부터 해야" in text
            or "우선 해야" in text
            or "처리해야 할 업무" in text
            or re.search(
                r"(?:지금|내일).*(?:뭐|뭘)\s*(?:부터\s*)?"
                r"(?:해야|하면|하는\s*게|할까)",
                text,
            )
        ):
            return self._query(QueryType.RECOMMEND_NEXT)

        if "진행 중인 업무" in text or "진행 중인 일" in text:
            return self._query(QueryType.CURRENT_WORK)

        if "대기 중" in text or "기다리는 업무" in text:
            return self._query(QueryType.WAITING_WORK)

        if "막혀" in text or "막힌 업무" in text:
            return self._query(QueryType.BLOCKED_WORK)

        if "다음에 해야" in text or "다음 할 일" in text:
            return self._query(QueryType.NEXT_ACTIONS)

        if "최근" in text and ("완료" in text or "끝낸" in text):
            return self._query(QueryType.RECENT_COMPLETED)

        if "이번 주" in text and ("뭐" in text or "어떤 업무" in text):
            return self._query(QueryType.WEEK_ACTIVITY)

        if (
            "오늘 뭐 했" in text
            or "오늘 뭐 했어" in text
            or "오늘 한 거" in text
        ):
            return ExtractionEnvelope(
                intent=Intent.QUERY_WORK,
                query=QueryDraft(query_type=QueryType.TODAY_ACTIVITY),
            )

        if project and (
            "어디까지" in text
            or "진행 상황" in text
            or "현재 상태" in text
            or "뭐 하고 있었" in text
        ):
            return ExtractionEnvelope(
                intent=Intent.QUERY_WORK,
                query=QueryDraft(
                    query_type=QueryType.PROJECT_STATUS,
                    project_mention=project,
                ),
            )

        if not project and ("그거 어디까지" in text or "그때 거 어디까지" in text):
            return self._query(QueryType.FOCUSED_WORK_STATUS)

        if re.search(r"답(?:변|이)?\s*(?:왔|받)|회신\s*(?:왔|받)", text):
            return self._response_received(text)

        if not project and "로그인" in text and any(
            phrase in text for phrase in ("확인 요청", "문의", "물어", "어떻게 할지")
        ):
            return self._work_update(text, None)

        if project and (
            "확인 요청" in text
            or "문의" in text
            or "물어" in text
            or "수정했" in text
            or "수정했고" in text
            or "손봤" in text
            or "수정하다" in text
        ):
            return self._work_update(text, project)

        return ExtractionEnvelope(intent=Intent.GENERAL)

    def _project_mention(self, text: str) -> str | None:
        known_projects = ("예측매니저", "해양 AI 플랫폼", "AI Agent")
        for project in known_projects:
            if project in text:
                return project

        match = re.search(r"([A-Za-z0-9가-힣]+프로젝트)", text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _query(query_type: QueryType) -> ExtractionEnvelope:
        return ExtractionEnvelope(
            intent=Intent.QUERY_WORK,
            query=QueryDraft(query_type=query_type),
        )

    @staticmethod
    def _report(
        query_type: QueryType,
        project: str | None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ExtractionEnvelope:
        return ExtractionEnvelope(
            intent=Intent.GENERATE_REPORT,
            query=QueryDraft(
                query_type=query_type,
                project_mention=project,
                date_from=date_from,
                date_to=date_to,
            ),
        )

    def _work_update(self, text: str, project: str | None) -> ExtractionEnvelope:
        activities: list[ActivityDraft] = []
        patch = WorkItemPatchDraft()

        has_install_doc = any(
            phrase in text for phrase in ("설치 가이드", "설치 문서", "가이드")
        )
        did_edit = any(
            phrase in text
            for phrase in ("수정했", "수정했고", "손봤", "수정하다", "고쳤")
        )
        if has_install_doc and did_edit:
            activities.append(
                ActivityDraft(
                    kind=ActivityKind.WORK_PERFORMED,
                    summary="설치 가이드 수정",
                    occurred_on="TODAY",
                    source_excerpt=text,
                    derivation=Derivation.EXPLICIT,
                )
            )

        is_request = any(
            phrase in text
            for phrase in ("확인 요청", "문의", "물어", "어떻게 할지")
        )
        if is_request:
            if "로그인" in text:
                request_summary = "로그인 제거 여부 확인 요청"
                waiting_for = "로그인 제거 여부 회신"
                next_action = "회신 결과에 따라 소스코드 및 설치 가이드 수정"
            else:
                request_summary = f"{project} 관련 확인 요청"
                waiting_for = f"{project} 문의 회신"
                next_action = "회신 내용 확인 후 후속 작업"
            activities.append(
                ActivityDraft(
                    kind=ActivityKind.REQUEST_SENT,
                    summary=request_summary,
                    occurred_on="TODAY",
                    source_excerpt=text,
                    derivation=Derivation.EXPLICIT,
                )
            )
            patch = WorkItemPatchDraft(
                status=WorkStatus.WAITING,
                waiting_for=waiting_for,
                next_action=next_action,
            )

        if "로그인" in text or (
            project == "예측매니저" and has_install_doc
        ):
            work_item_title = "설치 가이드 및 로그인 기능 처리"
        elif is_request:
            work_item_title = f"{project} 문의 처리"
        else:
            work_item_title = f"{project} 업무 진행"
            patch = WorkItemPatchDraft(status=WorkStatus.IN_PROGRESS)

        return ExtractionEnvelope(
            intent=Intent.CAPTURE_WORK,
            fact_groups=[
                FactGroupDraft(
                    project_mention=project,
                    work_item_mention=work_item_title,
                    activities=activities,
                    proposed_patch=patch,
                    reference_terms=(
                        ["로그인", "제거", "문의"] if "로그인" in text else []
                    ),
                    source_excerpt=text,
                )
            ],
        )
    def _response_received(self, text: str) -> ExtractionEnvelope:
        if (
            "로그인" in text
            or "제거" in text
            or "빼달" in text
            or "없애" in text
        ):
            summary = "로그인 제거 요청 회신 수신"
            next_action = "로그인 기능 제거를 위해 소스코드 및 설치 가이드 수정"
            references = ["어제", "로그인", "제거", "답변"]
        else:
            summary = "문의 회신 수신"
            next_action = "회신 내용 확인 및 후속 작업"
            references = ["아까", "문의", "답변"]

        return ExtractionEnvelope(
            intent=Intent.CAPTURE_WORK,
            fact_groups=[
                FactGroupDraft(
                    activities=[
                        ActivityDraft(
                            kind=ActivityKind.RESPONSE_RECEIVED,
                            summary=summary,
                            occurred_on="TODAY",
                            source_excerpt=text,
                            derivation=Derivation.EXPLICIT,
                        )
                    ],
                    proposed_patch=WorkItemPatchDraft(
                        status=WorkStatus.IN_PROGRESS,
                        next_action=next_action,
                        clear_waiting_for=True,
                    ),
                    reference_terms=references,
                    source_excerpt=text,
                )
            ],
        )


# Backward-compatible import used by the approved M1-M4 baseline.
DeterministicKoreanExtractionProvider = DeterministicTestProvider
