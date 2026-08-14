---
schema_version: skill-manifest.v1
name: work-capture
version: 1.0.0
description: 자유로운 한국어 업무 발언을 work-fact-draft.v1 후보로 구조화한다.
enabled: false
role: worker
model_profile: worker-balanced
max_iterations: 4
timeout_seconds: 30
tools:
  - project.search
  - work.search
  - memory.get_recent
permissions:
  project.search: ALLOW
  work.search: ALLOW
  memory.get_recent: ALLOW
memory_scope:
  recent_days: 30
  scope: relevant
input_schema: schemas/work-capture-input.v1.json
output_schema: schemas/work-fact-draft.v1.json
failure_policy:
  retry: 1
  escalation: false
skill_request_policy: JARVIS_ONLY
---

# Work Capture

## Purpose

사용자의 자연어 업무 기록과 업무 조회 요청을 Phase 1의 `work-fact-draft.v1` 구조화 후보로 변환한다.

## Procedure

1. 사용자 원문을 확인한다.
2. Runtime이 제공한 Context Package는 읽기 전용 참고 정보로만 사용한다.
3. 수행, 문의, 회신, 결정, 메모를 Activity 후보로 구분한다.
4. 명시된 Project와 Work Item 후보만 반환한다.
5. 상태, Waiting, Next Action은 원문에 근거가 있을 때만 제안한다.
6. 결과는 지정된 JSON Schema 하나만 반환한다.

## Safety Rules

- SQLite, SQL, 내부 ID, Tool 호출을 직접 만들거나 실행하지 않는다.
- 사용자가 말하지 않은 완료, 우선순위, 상태, 행동을 생성하지 않는다.
- 모호한 과거 Context는 임의로 연결하지 않는다.
- `source_excerpt`는 사용자 원문의 실제 연속 구간이어야 한다.
- 조회 요청에는 `fact_groups`를 넣지 않는다.
- 설명, Markdown fence, Chain-of-Thought는 출력하지 않는다.

## Failure

Schema를 만족할 수 없거나 근거가 부족하면 유효하지 않은 추측을 출력하지 말고 Runtime이 실패/Clarification 경계로 처리할 수 있는 구조화 결과만 반환한다.
