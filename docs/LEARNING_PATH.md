# BY 학습 경로

이 문서는 BY를 사용하면서 개인 AI 시스템의 핵심 개념을 단계별로 공부하기 위한
지도입니다. 각 단계는 작은 변경, 테스트, 실행 확인 후 다음 단계로 넘어갑니다.

## 0. 실행과 관찰

배울 개념: Docker Compose, 환경 변수, FastAPI health check, Next.js와 Backend의
경계.

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8100/health
```

## 1. Structured Memory

배울 개념: Raw Conversation과 Canonical Memory 분리, Project/Work Item/Activity,
SQLite transaction, 상태 전이.

읽을 파일: `backend/app/models.py`, `backend/app/work_manager.py`,
`backend/app/migrations/001_initial.sql`.

## 2. LLM Provider와 안전 경계

배울 개념: Provider Adapter, structured output, Pydantic validation,
deterministic validation, 실패 시 rollback.

Local Ollama와 API Provider는 같은 `work-fact-draft.v1` 계약을 반환합니다. 모델이
어떤 답을 만들었는지와 DB에 무엇을 저장할지는 서로 다른 책임입니다.

## 3. Context Linking

배울 개념: 후보 검색, confidence, clarification, optimistic linking을 피하는 이유,
재연결과 correction fixture.

읽을 파일: `backend/app/context_linking.py`, `backend/app/stabilization.py`.

## 4. Skill Runtime

배울 개념: `SKILL.md`, Registry, Context Package, Tool Permission,
iteration/timeout, 실행 ledger.

흐름은 다음과 같습니다.

```text
사용자 입력
  -> JARVIS/BY Orchestrator
  -> Skill Registry
  -> Skill Runtime + Context Package
  -> LLM Worker
  -> Schema/Deterministic Validation
  -> WorkManager
  -> Structured Memory
```

읽을 파일: `backend/skills/work-capture/SKILL.md`,
`backend/app/skills/registry.py`, `backend/app/skill_runtime.py`.

## 5. Trigger/Event Engine

배울 개념: Domain Event, idempotency digest, 상태 기반 Trigger, suggestion과
Canonical Write의 분리.

`GET /api/v1/suggestions`는 상태를 읽어 제안을 만들지만 Project/Work Item/Activity를
수정하지 않습니다.

## 6. TTS Adapter

배울 개념: 표현 계층과 업무 데이터 계층 분리, HTTP adapter, 다운로드 모델과
named volume, 모델 라이선스.

기본 Piper 서비스는 `tts/piper-server`에 있습니다. TTS가 실패해도 텍스트 응답과
Structured Memory는 유지됩니다.

## 7. 기여 순서

새 기능을 추가할 때는 다음 질문에 답합니다.

1. 이 기능은 읽기인가 쓰기인가?
2. Canonical Memory를 바꾸는 경계는 어디인가?
3. LLM이 실패하거나 틀리면 무엇이 보호되는가?
4. 같은 요청 재전송은 어떻게 중복을 막는가?
5. 테스트가 사용자 원문 없이 문제를 재현할 수 있는가?
