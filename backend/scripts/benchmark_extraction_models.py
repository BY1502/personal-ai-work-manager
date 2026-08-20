from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.models import ActivityKind, Intent, QueryType, WorkStatus
from app.providers import LocalLLMProvider
from app.validation import ExtractionValidator


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    content: str
    intent: Intent
    query_type: QueryType | None = None
    project_terms: tuple[str, ...] = ()
    activity_kinds: tuple[ActivityKind, ...] = ()
    status: WorkStatus | None = None
    minimum_groups: int = 0
    require_waiting_for: bool = False
    require_clear_waiting: bool = False
    next_action_term: str | None = None


CASES: tuple[EvaluationCase, ...] = (
    EvaluationCase(
        "simple-capture",
        "오늘 예측매니저 설치 가이드 수정했어.",
        Intent.CAPTURE_WORK,
        project_terms=("예측매니저",),
        activity_kinds=(ActivityKind.WORK_PERFORMED,),
        status=WorkStatus.IN_PROGRESS,
        minimum_groups=1,
    ),
    EvaluationCase(
        "combined-waiting",
        "오늘 예측매니저 설치 문서 좀 손봤고 로그인 없앨지는 박사님한테 물어봤어.",
        Intent.CAPTURE_WORK,
        project_terms=("예측매니저",),
        activity_kinds=(ActivityKind.WORK_PERFORMED, ActivityKind.REQUEST_SENT),
        status=WorkStatus.WAITING,
        minimum_groups=1,
        require_waiting_for=True,
    ),
    EvaluationCase(
        "synonym-waiting",
        "예측매니저 가이드 수정하다가 로그인 기능 어떻게 할지 문의 넣어놨어.",
        Intent.CAPTURE_WORK,
        project_terms=("예측매니저",),
        activity_kinds=(ActivityKind.WORK_PERFORMED, ActivityKind.REQUEST_SENT),
        status=WorkStatus.WAITING,
        minimum_groups=1,
        require_waiting_for=True,
    ),
    EvaluationCase(
        "response-login",
        "그때 물어본 로그인 건 답 왔는데 빼달래.",
        Intent.CAPTURE_WORK,
        activity_kinds=(ActivityKind.RESPONSE_RECEIVED,),
        status=WorkStatus.IN_PROGRESS,
        minimum_groups=1,
        require_clear_waiting=True,
        next_action_term="로그인",
    ),
    EvaluationCase(
        "response-pronoun",
        "어제 문의했던 거 제거하라고 답변 왔어.",
        Intent.CAPTURE_WORK,
        activity_kinds=(ActivityKind.RESPONSE_RECEIVED,),
        status=WorkStatus.IN_PROGRESS,
        minimum_groups=1,
        require_clear_waiting=True,
        next_action_term="제거",
    ),
    EvaluationCase(
        "blocked-work",
        "해양 AI 플랫폼 API 연동이 인증 오류 때문에 막혀 있어.",
        Intent.CAPTURE_WORK,
        project_terms=("해양", "AI", "플랫폼"),
        status=WorkStatus.BLOCKED,
        minimum_groups=1,
    ),
    EvaluationCase(
        "completed-work",
        "애즈웰 이미지 비교 데이터셋 평가를 완료했어.",
        Intent.CAPTURE_WORK,
        project_terms=("애즈웰",),
        activity_kinds=(ActivityKind.WORK_PERFORMED,),
        status=WorkStatus.DONE,
        minimum_groups=1,
    ),
    EvaluationCase(
        "another-waiting",
        "충청대학교 입학 시스템 개발 방향을 담당자에게 문의했어.",
        Intent.CAPTURE_WORK,
        project_terms=("충청대학교",),
        activity_kinds=(ActivityKind.REQUEST_SENT,),
        status=WorkStatus.WAITING,
        minimum_groups=1,
        require_waiting_for=True,
    ),
    EvaluationCase(
        "future-plan",
        "다음주에 ETRI연합트윈 로그인을 제거한 소스코드와 설치 스크립트를 전달해줄 거야.",
        Intent.CAPTURE_WORK,
        project_terms=("ETRI연합트윈",),
        activity_kinds=(ActivityKind.WORK_PERFORMED,),
        status=WorkStatus.TODO,
        minimum_groups=1,
    ),
    EvaluationCase(
        "two-projects",
        "예측매니저 가이드를 고쳤고 해양 AI 플랫폼 인증 오류는 담당자에게 문의했어.",
        Intent.CAPTURE_WORK,
        project_terms=("예측매니저", "해양", "AI", "플랫폼"),
        activity_kinds=(ActivityKind.WORK_PERFORMED, ActivityKind.REQUEST_SENT),
        minimum_groups=2,
        require_waiting_for=True,
    ),
    EvaluationCase(
        "current-query",
        "지금 진행 중인 업무 뭐 있어?",
        Intent.QUERY_WORK,
        query_type=QueryType.CURRENT_WORK,
    ),
    EvaluationCase(
        "recommend-query",
        "지금 뭐부터 해야 돼?",
        Intent.QUERY_WORK,
        query_type=QueryType.RECOMMEND_NEXT,
    ),
    EvaluationCase(
        "project-query",
        "예측매니저 지금 진행 상황 뭐였지?",
        Intent.QUERY_WORK,
        query_type=QueryType.PROJECT_STATUS,
        project_terms=("예측매니저",),
    ),
    EvaluationCase(
        "today-query",
        "오늘 한 거 정리해봐.",
        Intent.QUERY_WORK,
        query_type=QueryType.TODAY_ACTIVITY,
    ),
    EvaluationCase(
        "weekly-report",
        "이번 주 업무보고 만들어줘.",
        Intent.GENERATE_REPORT,
        query_type=QueryType.WEEKLY_REPORT,
    ),
    EvaluationCase(
        "general",
        "안녕하세요. 오늘도 잘 부탁해.",
        Intent.GENERAL,
    ),
)


def _contains_all(haystack: Iterable[str], needles: tuple[str, ...]) -> bool:
    normalized = " ".join(haystack).casefold().replace(" ", "")
    return all(needle.casefold().replace(" ", "") in normalized for needle in needles)


def _evaluate(model: str, base_url: str, timeout: float) -> dict[str, object]:
    provider = LocalLLMProvider(
        base_url=base_url,
        model_name=model,
        timeout_seconds=timeout,
        repair_attempts=1,
        retry_attempts=1,
        context_length=16_384,
        max_output_tokens=4_096,
    )
    validator = ExtractionValidator()
    results: list[dict[str, object]] = []
    latencies: list[int] = []

    for index, case in enumerate(CASES, start=1):
        print(f"[{model}] {index}/{len(CASES)} {case.case_id}", file=sys.stderr)
        started = time.monotonic()
        failures: list[str] = []
        checks = 1
        passed_checks = 0
        try:
            envelope = provider.extract(case.content)
            duration_ms = round((time.monotonic() - started) * 1_000)
            latencies.append(duration_ms)

            if envelope.intent == case.intent:
                passed_checks += 1
            else:
                failures.append(f"intent:{envelope.intent}")

            checks += 1
            actual_query = envelope.query.query_type if envelope.query else None
            if actual_query == case.query_type:
                passed_checks += 1
            else:
                failures.append(f"query_type:{actual_query}")

            validated = [
                validator.validate_fact_group(
                    group,
                    source_content=case.content,
                    today=date.today(),
                )
                for group in envelope.fact_groups
            ]
            checks += 1
            if len(validated) >= case.minimum_groups:
                passed_checks += 1
            else:
                failures.append(f"groups:{len(validated)}")

            if case.project_terms:
                checks += 1
                projects = [group.project_mention or "" for group in validated]
                query_projects = [
                    envelope.query.project_mention
                    if envelope.query and envelope.query.project_mention
                    else ""
                ]
                if _contains_all(projects + query_projects, case.project_terms):
                    passed_checks += 1
                else:
                    failures.append("project_terms")

            if case.activity_kinds:
                checks += 1
                kinds = {
                    activity.kind
                    for group in validated
                    for activity in group.activities
                }
                if set(case.activity_kinds) <= kinds:
                    passed_checks += 1
                else:
                    failures.append(
                        "activity_kinds:"
                        + ",".join(sorted(kind.value for kind in kinds))
                    )

            if case.status is not None:
                checks += 1
                statuses = {group.proposed_patch.status for group in validated}
                if case.status in statuses:
                    passed_checks += 1
                else:
                    failures.append(
                        "status:"
                        + ",".join(
                            sorted(status.value for status in statuses if status)
                        )
                    )

            if case.require_waiting_for:
                checks += 1
                if any(
                    group.proposed_patch.status == WorkStatus.WAITING
                    and bool(group.proposed_patch.waiting_for)
                    for group in validated
                ):
                    passed_checks += 1
                else:
                    failures.append("waiting_for")

            if case.require_clear_waiting:
                checks += 1
                if any(group.proposed_patch.clear_waiting_for for group in validated):
                    passed_checks += 1
                else:
                    failures.append("clear_waiting_for")

            if case.next_action_term:
                checks += 1
                if _contains_all(
                    [group.proposed_patch.next_action or "" for group in validated],
                    (case.next_action_term,),
                ):
                    passed_checks += 1
                else:
                    failures.append("next_action")

            results.append(
                {
                    "case_id": case.case_id,
                    "passed": not failures,
                    "checks_passed": passed_checks,
                    "checks_total": checks,
                    "duration_ms": duration_ms,
                    "failures": failures,
                }
            )
        except Exception as exc:  # noqa: BLE001 - benchmark records safe class only
            duration_ms = round((time.monotonic() - started) * 1_000)
            latencies.append(duration_ms)
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": False,
                    "checks_passed": 0,
                    "checks_total": checks,
                    "duration_ms": duration_ms,
                    "failures": [type(exc).__name__],
                }
            )

    total_checks = sum(int(result["checks_total"]) for result in results)
    passed_checks = sum(int(result["checks_passed"]) for result in results)
    sorted_latencies = sorted(latencies)
    p95_index = max(0, min(len(sorted_latencies) - 1, (95 * len(sorted_latencies) + 99) // 100 - 1))
    return {
        "model": model,
        "case_count": len(results),
        "passed_cases": sum(bool(result["passed"]) for result in results),
        "semantic_check_rate": round(passed_checks / total_checks, 4),
        "average_latency_ms": round(statistics.fmean(latencies)),
        "p95_latency_ms": sorted_latencies[p95_index],
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare local Ollama models against synthetic Korean extraction cases."
    )
    parser.add_argument("models", nargs="+", help="Ollama model tags")
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:11434"
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    report = {
        "schema_version": "extraction-model-benchmark.v1",
        "generated_on": date.today().isoformat(),
        "models": [
            _evaluate(model, args.base_url, args.timeout) for model in args.models
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
