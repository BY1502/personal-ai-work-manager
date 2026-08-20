from datetime import date
import threading
import time

import pytest

from app.providers import (
    ConcurrencyLimitedExtractionProvider,
    ExtractionConcurrencyError,
    DeterministicTestProvider,
    ExtractionProvider,
    ExtractionEnvelope,
)
from app.models import (
    ActivityDraft,
    ActivityKind,
    Derivation,
    FactGroupDraft,
    WorkItemPatchDraft,
    WorkStatus,
)
from app.validation import ExtractionValidator


def test_future_delivery_note_drops_unsupported_wait_clear_candidate() -> None:
    source = (
        "오늘 알파 서비스 설치 스크립트 파일 정리해서 보내줬어.\n"
        "추후에 로그인 제거한 전체 소스랑 설치 스크립트 보내줘야 돼."
    )
    draft = FactGroupDraft(
        project_mention="알파 서비스",
        work_item_mention="설치 자료 전달",
        activities=[
            ActivityDraft(
                kind=ActivityKind.WORK_PERFORMED,
                summary="설치 스크립트 파일 정리 및 전달",
                occurred_on="TODAY",
                source_excerpt=source,
                derivation=Derivation.EXPLICIT,
            )
        ],
        proposed_patch=WorkItemPatchDraft(
            status=WorkStatus.IN_PROGRESS,
            waiting_for="로그인 제거 소스 준비",
            next_action="로그인 제거 소스와 설치 스크립트 전달",
            clear_waiting_for=True,
        ),
        source_excerpt=source,
    )

    validated = ExtractionValidator().validate_fact_group(
        draft,
        source_content=source,
        today=date(2026, 8, 11),
    )

    assert validated.proposed_patch.status == WorkStatus.IN_PROGRESS
    assert validated.proposed_patch.waiting_for is None
    assert validated.proposed_patch.clear_waiting_for is False
    assert (
        validated.proposed_patch.next_action
        == "로그인 제거 소스와 설치 스크립트 전달"
    )
    assert len(validated.activities) == 1


@pytest.mark.parametrize("relative_token", ["TOMORROW", "NEXT_WEEK", "NEXT_MONTH"])
def test_future_relative_activity_tokens_are_recorded_on_capture_day(
    relative_token: str,
) -> None:
    """A future plan must not make an otherwise valid work note return 422.

    Phase 1 has no due-date field.  The deterministic boundary therefore keeps
    the activity on the capture day while preserving the original wording in
    the source excerpt/summary.
    """
    source = "다음주에 ETRI연합트윈 로그인을 제거한 소스코드와 설치 스크립트를 전달해줄거야"
    draft = FactGroupDraft(
        project_mention="ETRI연합트윈",
        work_item_mention="로그인 제거 소스코드 및 설치 스크립트 전달",
        activities=[
            ActivityDraft(
                kind=ActivityKind.WORK_PERFORMED,
                summary="로그인 제거 소스코드 및 설치 스크립트 전달",
                occurred_on=relative_token,
                source_excerpt=source,
                derivation=Derivation.EXPLICIT,
            )
        ],
        proposed_patch=WorkItemPatchDraft(status=WorkStatus.IN_PROGRESS),
        source_excerpt=source,
    )

    validated = ExtractionValidator().validate_fact_group(
        draft,
        source_content=source,
        today=date(2026, 8, 14),
    )

    assert validated.activities[0].occurred_on_local == date(2026, 8, 14)


class _BlockingProvider(DeterministicTestProvider):
    def __init__(self, blocker: threading.Event) -> None:
        super().__init__()
        self.blocker = blocker

    def extract(self, content: str) -> ExtractionEnvelope:
        self.blocker.wait()
        return super().extract(content)


def test_concurrency_gate_blocks_additional_extract() -> None:
    gate = threading.Event()
    provider: ExtractionProvider = ConcurrencyLimitedExtractionProvider(
        _BlockingProvider(gate),
        max_concurrent=1,
        acquire_timeout_seconds=0.0,
    )

    def first_call() -> None:
        provider.extract("테스트")

    thread = threading.Thread(target=first_call)
    thread.start()
    try:
        time.sleep(0.05)
        with pytest.raises(ExtractionConcurrencyError):
            provider.extract("동시 처리")
    finally:
        gate.set()
        thread.join(timeout=1.0)


def test_concurrency_gate_waits_for_available_slot() -> None:
    gate = threading.Event()
    provider: ExtractionProvider = ConcurrencyLimitedExtractionProvider(
        _BlockingProvider(gate),
        max_concurrent=1,
        acquire_timeout_seconds=1.0,
    )

    results: list[str] = []

    def first_call() -> None:
        provider.extract("테스트")
        results.append("first")

    thread = threading.Thread(target=first_call)
    thread.start()
    try:
        time.sleep(0.05)

        done = threading.Event()

        def second_call() -> None:
            provider.extract("동시 처리")
            results.append("second")
            done.set()

        second_thread = threading.Thread(target=second_call)
        second_thread.start()
        time.sleep(0.2)
        gate.set()
        assert done.wait(2.0)
        second_thread.join(timeout=1.0)
        assert results == ["first", "second"]
    finally:
        gate.set()
        thread.join(timeout=1.0)
