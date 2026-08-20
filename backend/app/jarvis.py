from __future__ import annotations

import json
import time
from datetime import timedelta

from app.context_linking import MemoryManager
from app.calendar_agent import CalendarManager, is_calendar_request
from app.event_engine import DomainEvent, EventEngine
from app.extraction import ExtractionProvider
from app.models import (
    ChatRunRequest,
    Intent,
    JarvisResponse,
    QueryType,
    ReceiptView,
    ResolveClarificationRequest,
)
from app.recommendation import (
    RecommendationPresentationService,
    RecommendationService,
)
from app.reporting import ReportManager
from app.repository import WorkRepository
from app.stabilization import detect_context_link_correction
from app.skill_runtime import SkillRuntime
from app.utils import local_date
from app.validation import ExtractionValidator
from app.work_manager import WorkManager
from app.work_queries import StructuredWorkQueryService
from app.tts import LocalTTSBridge, TTSResult


class UnsupportedResolution(ValueError):
    pass


class JarvisOrchestrator:
    """Coordinates extraction, deterministic policy, memory work, and synthesis.

    The extractor never receives a database handle. Every write is made by
    deterministic application code after validation and a link decision.
    """

    def __init__(
        self,
        *,
        repository: WorkRepository,
        memory: MemoryManager,
        work_manager: WorkManager,
        work_queries: StructuredWorkQueryService,
        recommendations: RecommendationService,
        recommendation_presentation: RecommendationPresentationService,
        reports: ReportManager,
        extractor: ExtractionProvider,
        skill_runtime: SkillRuntime | None = None,
        calendar_manager: CalendarManager | None = None,
        tts: LocalTTSBridge | None = None,
        event_engine: EventEngine | None = None,
        validator: ExtractionValidator,
        user_id: str,
        timezone_name: str,
    ) -> None:
        self.repository = repository
        self.memory = memory
        self.work_manager = work_manager
        self.work_queries = work_queries
        self.recommendations = recommendations
        self.recommendation_presentation = recommendation_presentation
        self.reports = reports
        self.extractor = extractor
        self.skill_runtime = skill_runtime
        self.calendar_manager = calendar_manager
        self.tts = tts
        self.event_engine = event_engine
        self.validator = validator
        self.user_id = user_id
        self.timezone_name = timezone_name

    def handle_chat(self, request: ChatRunRequest) -> JarvisResponse:
        intake = self.repository.create_or_get_run(
            user_id=self.user_id,
            conversation_id=request.conversation_id,
            client_message_id=request.client_message_id,
            content=request.content,
        )
        if intake.existing_response is not None:
            return intake.existing_response

        failure_stage = "ORCHESTRATION"
        provider_started_at: float | None = None
        provider_duration_ms: int | None = None
        try:
            correction = detect_context_link_correction(request.content)
            if correction is not None:
                failure_stage = "CORRECTION_RECORDING"
                correction_record = self.repository.record_context_link_correction(
                    user_id=self.user_id,
                    intake=intake,
                    signal=correction,
                )
                observed = correction_record.get("observed_link")
                if observed is None:
                    display = (
                        "정정 내용은 업무 연결 오류 사례로 기록했습니다. "
                        "다만 정정할 이전 업무 기록을 찾지 못해 기존 업무는 변경하지 "
                        "않았습니다. 프로젝트와 업무를 다시 알려주세요."
                    )
                else:
                    display = (
                        "정정 내용은 업무 연결 오류 사례로 기록했습니다. "
                        f"직전 기록의 ‘{observed['project_name']}’ 연결 대신 "
                        f"‘{correction.expected_project_mention}’ 프로젝트로 확인했습니다. "
                        "기존 기록은 안전한 재연결 확인 전까지 자동으로 옮기지 않았습니다."
                    )
                response = JarvisResponse(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    status="COMPLETED",
                    display_response=display,
                    voice_response=(
                        "프로젝트 정정 내용을 기록했습니다. 기존 업무 연결은 자동으로 "
                        "변경하지 않았습니다."
                    ),
                    data={
                        "correction": {
                            "type": "CONTEXT_LINK_CORRECTION_REPORTED",
                            "case_id": correction_record["case_id"],
                            "status": correction_record["status"],
                            "expected_project_mention": (
                                correction.expected_project_mention
                            ),
                            "requires_manual_relink": observed is not None,
                        }
                    },
                )
                return self._persist_response(
                    run_id=intake.run_id,
                    response=response,
                    memory_status="CORRECTION_RECORDED",
                )

            provider_name = intake.stored_provider_name or getattr(
                self.extractor, "provider_name", "unknown"
            )
            model_version = intake.stored_model_version or getattr(
                self.extractor, "model_name", None
            )
            prompt_version = intake.stored_prompt_version or getattr(
                self.extractor, "prompt_version", "unknown"
            )
            if (
                self.calendar_manager is not None
                and self.skill_runtime is not None
                and is_calendar_request(request.content)
            ):
                failure_stage = "CALENDAR_INTERPRETATION"
                self.repository.begin_interpretation(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    provider_name=provider_name,
                    model_version=model_version,
                    prompt_version=prompt_version,
                )
                provider_started_at = time.monotonic()
                skill_result = self.skill_runtime.invoke(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    skill_name="calendar-agent",
                    input_payload={"content": request.content},
                    step_key="calendar-agent",
                    allow_retry=intake.retry_skill,
                )
                provider_duration_ms = skill_result.duration_ms
                self.repository.complete_interpretation(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    duration_ms=provider_duration_ms,
                )
                failure_stage = "CALENDAR_POLICY"
                calendar_result = self.calendar_manager.handle_draft(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    output=skill_result.output,
                    list_tool=lambda payload: self.skill_runtime.execute_tool(
                        user_id=self.user_id,
                        run_id=intake.run_id,
                        skill_name="calendar-agent",
                        tool_name="calendar.events.list",
                        payload=payload,
                    ),
                )
                response = JarvisResponse(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    status="COMPLETED",
                    display_response=calendar_result.display_response,
                    voice_response=calendar_result.voice_response,
                    data=calendar_result.data,
                )
                failure_stage = "RESPONSE_PERSISTENCE"
                return self._persist_response(
                    run_id=intake.run_id,
                    response=response,
                    memory_status=calendar_result.memory_status,
                )
            if intake.stored_plan is None:
                failure_stage = "INTERPRETATION"
                self.repository.begin_interpretation(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    provider_name=provider_name,
                    model_version=model_version,
                    prompt_version=prompt_version,
                )
                provider_started_at = time.monotonic()
                if self.skill_runtime is not None:
                    skill_result = self.skill_runtime.invoke_work_capture(
                        user_id=self.user_id,
                        run_id=intake.run_id,
                        conversation_id=intake.conversation_id,
                        content=request.content,
                        allow_retry=intake.retry_skill,
                    )
                    envelope = skill_result.envelope
                    provider_duration_ms = skill_result.duration_ms
                else:
                    envelope = self.extractor.extract(request.content)
                    provider_duration_ms = max(
                        0,
                        round((time.monotonic() - provider_started_at) * 1_000),
                    )
                self.repository.complete_interpretation(
                    user_id=self.user_id,
                    run_id=intake.run_id,
                    duration_ms=provider_duration_ms,
                )
            else:
                envelope = intake.stored_plan
            today = local_date(
                self.repository.database.clock,
                self.timezone_name,
            )

            failure_stage = "DETERMINISTIC_VALIDATION"
            validated_groups = [
                self.validator.validate_fact_group(
                    group,
                    source_content=request.content,
                    today=today,
                )
                for group in envelope.fact_groups
            ]
            failure_stage = "CONTEXT_LINKING"
            decisions = [
                self.memory.decide_link(
                    user_id=self.user_id,
                    conversation_id=intake.conversation_id,
                    group=group,
                    today=today,
                )
                for group in validated_groups
            ]
            failure_stage = "PLAN_PERSISTENCE"
            planned = self.repository.persist_plan(
                user_id=self.user_id,
                intake=intake,
                envelope=envelope,
                validated_groups=validated_groups,
                decisions=decisions,
                extractor_version=self.extractor.version,
                provider_name=provider_name,
                model_version=model_version,
                prompt_version=prompt_version,
            )

            if envelope.intent == Intent.GENERATE_REPORT:
                failure_stage = "REPORT_GENERATION"
                response = self._generate_report(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    query=envelope.query,
                    today=today,
                )
                failure_stage = "RESPONSE_PERSISTENCE"
                return self._persist_response(
                    run_id=intake.run_id,
                    response=response,
                    memory_status="NOT_APPLICABLE",
                )

            if envelope.intent == Intent.QUERY_WORK:
                failure_stage = "STRUCTURED_MEMORY_QUERY"
                response = self._answer_query(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    query=envelope.query,
                    today=today,
                )
                failure_stage = "RESPONSE_PERSISTENCE"
                return self._persist_response(
                    run_id=intake.run_id,
                    response=response,
                    memory_status="NOT_APPLICABLE",
                )

            if envelope.intent == Intent.GENERAL:
                failure_stage = "RESPONSE_PERSISTENCE"
                response = JarvisResponse(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    status="COMPLETED",
                    display_response=(
                        "이 입력에서는 저장하거나 조회할 업무 정보를 찾지 못했습니다. "
                        "프로젝트와 수행·요청·답변 내용을 조금 더 구체적으로 말씀해주세요."
                    ),
                    voice_response="업무 정보를 조금 더 구체적으로 말씀해주세요.",
                )
                return self._persist_response(
                    run_id=intake.run_id,
                    response=response,
                    memory_status="NO_CHANGE",
                )

            failure_stage = "CANONICAL_MEMORY_APPLY"
            receipts: list[ReceiptView] = []
            clarification = None
            for group in planned:
                if group.clarification is not None:
                    clarification = clarification or group.clarification
                    continue
                receipts.append(
                    self.work_manager.apply_ready_group(
                        user_id=self.user_id,
                        fact_group_id=group.fact_group_id,
                        expected_state_version=group.state_version,
                        group=group.draft,
                        decision=group.decision,
                    )
                )

            if clarification is not None:
                response = JarvisResponse(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    status="NEEDS_CLARIFICATION",
                    display_response=self._clarification_text(clarification),
                    voice_response=clarification.question,
                    receipts=receipts,
                    clarification=clarification,
                )
                memory_status = "PENDING_CONFIRMATION"
            else:
                response = JarvisResponse(
                    run_id=intake.run_id,
                    conversation_id=intake.conversation_id,
                    status="COMPLETED",
                    display_response=self._receipt_text(receipts),
                    voice_response=self._receipt_voice(receipts),
                    receipts=receipts,
                )
                memory_status = "APPLIED"

            failure_stage = "RESPONSE_PERSISTENCE"
            return self._persist_response(
                run_id=intake.run_id,
                response=response,
                memory_status=memory_status,
            )
        except Exception as exc:
            if provider_started_at is not None and provider_duration_ms is None:
                provider_duration_ms = max(
                    0,
                    round((time.monotonic() - provider_started_at) * 1_000),
                )
            self.repository.fail_run(
                user_id=self.user_id,
                run_id=intake.run_id,
                error_code=type(exc).__name__,
                failure_stage=failure_stage,
                duration_ms=(
                    provider_duration_ms
                    if failure_stage == "INTERPRETATION"
                    else None
                ),
            )
            raise

    def resolve_clarification(
        self,
        clarification_id: str,
        request: ResolveClarificationRequest,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> JarvisResponse:
        clarification = self.repository.get_clarification(
            user_id=self.user_id,
            clarification_id=clarification_id,
        )
        if request.action == "CANCEL":
            run_id, conversation_id = self.repository.cancel_clarification(
                user_id=self.user_id,
                clarification_id=clarification_id,
                expected_state_version=clarification["state_version"],
            )
            response = JarvisResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                status="COMPLETED",
                display_response="확인 대기 중이던 업무 기록을 취소했습니다.",
                voice_response="업무 기록을 취소했습니다.",
            )
            return self._persist_response(
                run_id=run_id,
                response=response,
                memory_status="REJECTED",
            )

        if request.action == "SELECT_EXISTING" and request.work_item_id is None:
            raise UnsupportedResolution("work_item_id is required")
        receipt, run_id, conversation_id = self.work_manager.apply_confirmed_group(
            user_id=self.user_id,
            clarification_id=clarification_id,
            selected_work_item_id=request.work_item_id,
            expected_clarification_version=clarification["state_version"],
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            new_project_name=request.project_name,
            new_work_item_title=request.work_item_title,
        )
        response = JarvisResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            status="COMPLETED",
            display_response=(
                (
                    f"새로운 ‘{receipt.project_name} > {receipt.work_item_title}’ "
                    if request.action == "CREATE_NEW"
                    else f"확인해주신 ‘{receipt.project_name} > {receipt.work_item_title}’ "
                )
                + "업무에 기록했습니다. 현재는 "
                f"{self._status_ko(receipt.status_after.value)} 상태입니다."
                + (
                    f" 다음 작업은 {receipt.next_action}입니다."
                    if receipt.next_action
                    else ""
                )
            ),
            voice_response=(
                f"확인한 업무에 연결했습니다. 현재 상태는 "
                f"{self._status_ko(receipt.status_after.value)}입니다."
            ),
            receipts=[receipt],
        )
        return self._persist_response(
            run_id=run_id,
            response=response,
            memory_status="APPLIED",
        )

    def _persist_response(
        self,
        *,
        run_id: str,
        response: JarvisResponse,
        memory_status: str,
    ) -> JarvisResponse:
        """Attach optional voice presentation, then persist the final response.

        TTS is deliberately after all canonical work has been applied and is
        best-effort. A bridge outage can remove an audio affordance, but can
        never roll back or block Structured Memory.
        """
        response = self._attach_tts(run_id=run_id, response=response)
        self.repository.complete_run(
            user_id=self.user_id,
            run_id=run_id,
            response=response,
            memory_status=memory_status,
        )
        if self.event_engine is not None:
            try:
                self.event_engine.emit(
                    user_id=self.user_id,
                    event=DomainEvent(
                        event_type="RUN_COMPLETED",
                        aggregate_type="ORCHESTRATION_RUN",
                        aggregate_id=run_id,
                        payload={
                            "response_status": response.status,
                            "memory_status": memory_status,
                            "receipt_count": len(response.receipts),
                        },
                    ),
                )
            except Exception:
                # Trigger diagnostics must never turn a completed work write
                # into a failed chat request.
                pass
        return response

    def _attach_tts(self, *, run_id: str, response: JarvisResponse) -> JarvisResponse:
        if self.tts is None or not response.voice_response.strip():
            return response
        try:
            result: TTSResult = self.tts.synthesize(response.voice_response)
        except Exception as exc:  # presentation failure must not affect Memory
            try:
                self.repository.append_skill_event(
                    user_id=self.user_id,
                    run_id=run_id,
                    event_type="TTS_FAILED",
                    public_summary="Voice presentation was unavailable; text response was preserved.",
                    payload={"error_code": type(exc).__name__},
                )
            except Exception:
                # Diagnostics are best-effort too; a logging failure must not
                # turn a successful text/canonical operation into an error.
                pass
            return response
        return response.model_copy(
            update={
                "audio_url": result.audio_url,
                "audio_duration_seconds": result.duration_seconds,
            }
        )

    def get_run_response(self, run_id: str) -> dict:
        row = self.repository.get_run_context(user_id=self.user_id, run_id=run_id)
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "run_id": row["id"],
            "status": row["status"],
            "memory_status": row["memory_status"],
            "result": result,
        }

    def _answer_query(self, *, run_id, conversation_id, query, today) -> JarvisResponse:
        if query is None:
            raise ValueError("query payload is required")
        query_type = query.query_type

        if query_type == QueryType.RECOMMEND_NEXT:
            recommendations = self.recommendations.recommend(
                user_id=self.user_id,
                today=today,
            )
            presentation = self.recommendation_presentation.present(
                recommendations
            )
            explanation_by_id = {
                item["work_item_id"]: item["explanation"]
                for item in presentation["items"]
            }
            if not recommendations:
                display = "지금 바로 추천할 수 있는 활성 업무가 없습니다."
            else:
                first = recommendations[0]
                first_item = first["work_item"]
                lines = [
                    (
                        f"지금은 {first_item['project_name']}의 "
                        f"‘{first_item['title']}’부터 보는 게 좋겠습니다."
                    )
                ]
                for recommendation in recommendations:
                    item = recommendation["work_item"]
                    lines.append(
                        f"{recommendation['rank']}. {item['project_name']} > "
                        f"{item['title']} — {recommendation['recommended_action']}"
                    )
                    explanation = explanation_by_id.get(item["work_item_id"])
                    if explanation:
                        lines.append(f"   - {explanation}")
                display = "\n".join(lines)
            return JarvisResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                status="COMPLETED",
                display_response=display,
                voice_response=(
                    display.split("\n", 1)[0]
                    if recommendations
                    else display
                ),
                data={
                    "query_type": query_type.value,
                    "policy_version": "recommend-v1",
                    "recommendations": recommendations,
                    "presentation": presentation,
                },
            )

        if query_type in {QueryType.TODAY_ACTIVITY, QueryType.WEEK_ACTIVITY}:
            start_date = (
                today
                if query_type == QueryType.TODAY_ACTIVITY
                else today - timedelta(days=today.weekday())
            )
            activities = self.work_queries.activities_between(
                user_id=self.user_id,
                start_date=start_date,
                end_date=today,
            )
            if not activities:
                display = (
                    "오늘 기록된 업무 활동이 없습니다."
                    if query_type == QueryType.TODAY_ACTIVITY
                    else "이번 주에 기록된 업무 활동이 없습니다."
                )
            else:
                heading = (
                    "오늘 한 일입니다."
                    if query_type == QueryType.TODAY_ACTIVITY
                    else "이번 주에 진행한 일입니다."
                )
                lines = [heading]
                lines.extend(
                    f"- {item['project_name']} > {item['work_item_title']}: "
                    f"{item['summary']}"
                    for item in activities
                )
                display = "\n".join(lines)
            return JarvisResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                status="COMPLETED",
                display_response=display,
                voice_response=(
                    f"확인되는 업무 활동은 {len(activities)}건입니다."
                    if activities
                    else "해당 기간에 기록된 업무 활동이 없습니다."
                ),
                data={
                    "query_type": query_type.value,
                    "period": {
                        "start_date": start_date.isoformat(),
                        "end_date": today.isoformat(),
                    },
                    "activities": activities,
                },
            )

        if query_type == QueryType.FOCUSED_WORK_STATUS:
            item = self.work_queries.focused_work_item(
                user_id=self.user_id,
                conversation_id=conversation_id,
            )
            if item is None:
                display = "이어 볼 업무가 없습니다. 프로젝트나 업무 이름을 알려주세요."
            else:
                display = self._focused_work_text(item)
            return JarvisResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                status="COMPLETED",
                display_response=display,
                voice_response=display.split("\n", 1)[0],
                data={"query_type": query_type.value, "work_item": item},
            )

        list_config = {
            QueryType.CURRENT_WORK: (
                ("IN_PROGRESS",),
                "현재 진행 중인 업무",
                "진행 중인 업무가 없습니다.",
            ),
            QueryType.WAITING_WORK: (
                ("WAITING",),
                "회신이나 확인을 기다리는 업무",
                "현재 대기 중인 업무가 없습니다.",
            ),
            QueryType.BLOCKED_WORK: (
                ("BLOCKED",),
                "진행이 막혀 있는 업무",
                "현재 막혀 있는 업무가 없습니다.",
            ),
        }
        if query_type in list_config:
            statuses, heading, empty_text = list_config[query_type]
            items = self.work_queries.list_by_status(
                user_id=self.user_id,
                statuses=statuses,
            )
            return self._work_list_response(
                run_id=run_id,
                conversation_id=conversation_id,
                query_type=query_type,
                heading=heading,
                empty_text=empty_text,
                items=items,
            )

        if query_type == QueryType.NEXT_ACTIONS:
            items = self.work_queries.next_actions(user_id=self.user_id)
            return self._work_list_response(
                run_id=run_id,
                conversation_id=conversation_id,
                query_type=query_type,
                heading="다음 행동이 정리된 업무",
                empty_text="지금 바로 이어갈 다음 행동이 정리된 업무가 없습니다.",
                items=items,
            )

        if query_type == QueryType.RECENT_COMPLETED:
            items = self.work_queries.recent_completed(
                user_id=self.user_id,
                now_utc=self.repository.database.clock.now_utc(),
            )
            return self._work_list_response(
                run_id=run_id,
                conversation_id=conversation_id,
                query_type=query_type,
                heading="최근 30일 안에 완료한 업무",
                empty_text="최근 30일 안에 완료로 기록된 업무가 없습니다.",
                items=items,
            )

        if query_type != QueryType.PROJECT_STATUS:
            raise ValueError(f"unsupported query type: {query_type.value}")

        if not query.project_mention:
            item = self.work_queries.focused_work_item(
                user_id=self.user_id,
                conversation_id=conversation_id,
            )
            if item is not None:
                return JarvisResponse(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    status="COMPLETED",
                    display_response=self._focused_work_text(item),
                    voice_response=(
                        f"{item['project_name']} 업무 상태를 확인했습니다."
                    ),
                    data={"query_type": query_type.value, "work_item": item},
                )
            return JarvisResponse(
                run_id=run_id,
                conversation_id=conversation_id,
                status="COMPLETED",
                display_response="조회할 프로젝트 이름을 알려주세요.",
                voice_response="조회할 프로젝트 이름을 알려주세요.",
                data={"query_type": query_type.value},
            )
        project = self.memory.project_status(
            user_id=self.user_id,
            project_mention=query.project_mention,
        )
        if project is None:
            display = f"‘{query.project_mention}’ 프로젝트의 구조화된 업무 기록이 없습니다."
        elif not project["work_items"]:
            display = f"{project['project_name']} 프로젝트에 등록된 업무가 없습니다."
        else:
            lines = [f"{project['project_name']} 프로젝트의 현재 상태입니다."]
            for item in project["work_items"]:
                lines.append(
                    f"- {item['title']}: {self._status_ko(item['status'])}"
                )
                if item["waiting_for"]:
                    lines.append(f"  기다리는 내용: {item['waiting_for']}")
                if item["next_action"]:
                    lines.append(f"  다음 작업: {item['next_action']}")
                if item["activities"]:
                    history = " → ".join(
                        activity["summary"] for activity in item["activities"]
                    )
                    lines.append(f"  활동 이력: {history}")
            display = "\n".join(lines)
        return JarvisResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            status="COMPLETED",
            display_response=display,
            voice_response=(
                f"{query.project_mention} 프로젝트의 현재 업무 상태를 조회했습니다."
                if project
                else f"{query.project_mention} 프로젝트 기록이 없습니다."
            ),
            data={"query_type": query.query_type.value, "project": project},
        )

    def _generate_report(
        self,
        *,
        run_id: str,
        conversation_id: str,
        query,
        today,
    ) -> JarvisResponse:
        if query is None:
            raise ValueError("report query payload is required")
        report_types = {
            QueryType.DAILY_REPORT: "DAILY",
            QueryType.WEEKLY_REPORT: "WEEKLY",
            QueryType.PROJECT_REPORT: "PROJECT",
            QueryType.RANGE_REPORT: "RANGE",
        }
        report_type = report_types.get(query.query_type)
        if report_type is None:
            raise ValueError(f"unsupported report type: {query.query_type.value}")
        report = self.reports.generate(
            user_id=self.user_id,
            report_type=report_type,
            today=today,
            project_mention=query.project_mention,
            start_date=query.date_from,
            end_date=query.date_to,
            source_run_id=run_id,
        )
        return JarvisResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            status="COMPLETED",
            display_response=report["rendered_text"],
            voice_response=(
                f"{report['period']['start_date']}부터 "
                f"{report['period']['end_date']}까지의 업무 보고를 정리했습니다."
            ),
            data={
                "query_type": query.query_type.value,
                "report": report,
            },
        )

    def _work_list_response(
        self,
        *,
        run_id: str,
        conversation_id: str,
        query_type: QueryType,
        heading: str,
        empty_text: str,
        items: list[dict],
    ) -> JarvisResponse:
        if not items:
            display = empty_text
        else:
            lines = [f"{heading}은 {len(items)}건입니다."]
            lines.extend(self._work_item_line(item) for item in items)
            display = "\n".join(lines)
        return JarvisResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            status="COMPLETED",
            display_response=display,
            voice_response=(
                f"{heading}은 {len(items)}건입니다." if items else empty_text
            ),
            data={"query_type": query_type.value, "items": items},
        )

    @classmethod
    def _work_item_line(cls, item: dict) -> str:
        line = (
            f"- {item['project_name']} > {item['title']}: "
            f"{cls._status_ko(item['status'])}"
        )
        if item.get("waiting_for"):
            line += f" · 대기: {item['waiting_for']}"
        if item.get("blocked_reason"):
            line += f" · 막힌 이유: {item['blocked_reason']}"
        if item.get("next_action"):
            prefix = "회신 후" if item["status"] == "WAITING" else "다음"
            line += f" · {prefix}: {item['next_action']}"
        return line

    @classmethod
    def _focused_work_text(cls, item: dict) -> str:
        lines = [
            f"지금 이어서 보고 있는 건 {item['project_name']}의 "
            f"{item['title']} 업무입니다.",
            f"현재는 {cls._status_ko(item['status'])} 상태입니다.",
        ]
        if item.get("waiting_for"):
            lines.append(f"{item['waiting_for']}을 기다리고 있습니다.")
        if item.get("blocked_reason"):
            lines.append(f"현재 막힌 이유는 {item['blocked_reason']}입니다.")
        if item.get("next_action"):
            lines.append(f"다음은 {item['next_action']}입니다.")
        return "\n".join(lines)

    @staticmethod
    def _status_ko(status: str) -> str:
        return {
            "TODO": "할 일",
            "IN_PROGRESS": "진행 중",
            "WAITING": "회신 대기",
            "BLOCKED": "진행 막힘",
            "HOLD": "보류",
            "DONE": "완료",
        }.get(status, status)

    @staticmethod
    def _receipt_text(receipts: list[ReceiptView]) -> str:
        if not receipts:
            return "저장할 구조화된 업무 변경이 없습니다."
        sections: list[str] = []
        for receipt in receipts:
            target = (
                f"{receipt.project_name}의 ‘{receipt.work_item_title}’ 업무"
            )
            lines = ["기록했습니다."]
            if receipt.status_after.value == "WAITING":
                lines.append(f"{target}는 회신 대기 상태로 정리했습니다.")
            elif (
                receipt.status_before is not None
                and receipt.status_before.value == "WAITING"
                and receipt.status_after.value != "WAITING"
            ):
                lines.append(f"{target}의 회신 대기를 해제했습니다.")
            else:
                lines.append(
                    f"{target}는 "
                    f"{JarvisOrchestrator._status_ko(receipt.status_after.value)} 상태입니다."
                )
            if receipt.waiting_for:
                lines.append(
                    f"기다리는 내용은 ‘{receipt.waiting_for}’입니다."
                )
            elif receipt.next_action:
                lines.append(f"다음은 ‘{receipt.next_action}’입니다.")
            sections.append(" ".join(lines))
        return "\n".join(sections)

    @staticmethod
    def _receipt_voice(receipts: list[ReceiptView]) -> str:
        if not receipts:
            return "저장할 업무 변경이 없습니다."
        receipt = receipts[-1]
        return (
            f"{receipt.project_name}의 {receipt.work_item_title} 업무에 기록했습니다. "
            f"현재는 {JarvisOrchestrator._status_ko(receipt.status_after.value)} 상태입니다."
        )

    @staticmethod
    def _clarification_text(clarification) -> str:
        if not clarification.candidates:
            return clarification.question
        lines = [clarification.question]
        lines.extend(
            f"- {candidate.project_name} > {candidate.work_item_title} "
            f"({JarvisOrchestrator._status_ko(candidate.status.value)})"
            for candidate in clarification.candidates
        )
        return "\n".join(lines)
