# Personal AI Work Manager Backend

개인 업무 문장을 Structured Work Memory로 기록하고, Context를 이어서 조회·추천·보고서로
제공하는 Phase 1 Backend입니다. 현재 범위는 M1–M8이며 Next.js Dashboard가 사용할
읽기 전용 projection API를 포함합니다.

Desktop UI 확장에 앞서 Phase 2의 첫 실행 경계로 Trigger/Event Engine,
Permission Engine, 범용 단일 Skill 실행을 추가했습니다.

## Phase 2 첫 Vertical Slice

`skills/*/SKILL.md`를 Skill Registry가 발견하고 YAML/Schema/Tool/Permission을
검증한 뒤 활성화합니다. 현재 `work-capture`는 기존 `work-fact-draft.v1` 계약으로
Chat에 연결되어 있으며, 범용 Skill은 내부 `SkillRuntime.invoke()`로 같은
입력·출력 검증과 실행 ledger를 사용합니다. Worker는 Canonical Memory를 직접
수정하지 않고 WorkManager/Context Linking/Receipt 경계만이 Structured Memory를
변경합니다.

읽기 전용 Tool Registry에는 `project.search`, `work.search`,
`memory.get_recent`만 등록되어 있습니다. `PermissionEngine`은 Skill manifest와
runtime Tool permission의 교집합을 평가하며 DENY, JARVIS_ONLY, manifest 외 Tool은
실행하지 않습니다.

Trigger/Event Engine은 `RUN_COMPLETED`·`ACTIVITY_RELINKED` 같은 구조화 Event를
중복 방지 ledger에 기록하고, 현재 업무 상태에서 최대 3개의 결정론적 제안을
만듭니다. 제안은 Canonical Memory를 쓰지 않습니다.

```bash
curl 'http://127.0.0.1:8100/api/v1/suggestions?limit=3'
curl -X POST 'http://127.0.0.1:8100/api/v1/suggestions/<suggestion-id>/dismiss'
curl 'http://127.0.0.1:8100/api/v1/skills'
```

Trigger 제안 정책 버전은 `trigger-v1`이며, 현재 WAITING 3일 이상,
BLOCKED 3일 이상, HIGH 우선순위 3일 이상 정체, IN_PROGRESS/TODO 14일 이상
정체를 관찰합니다. 자동 외부 작업, Skill 간 DAG/병렬 실행은 수행하지 않습니다.

## 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

환경 변수는 실행 Shell에서 export하거나 `.env` 내용을 별도로 로드해야 합니다.
애플리케이션 자체가 `.env` 파일을 자동으로 읽지는 않습니다.

운영 기본값은 Local Ollama `qwen3:4b`입니다. API Provider를 사용하려면
`EXTRACTION_PROVIDER=api`로 전환할 수 있으며, OpenAI Responses 호환 endpoint와
모델을 환경 변수로 지정합니다.

```bash
ollama serve
ollama pull qwen3:4b
EXTRACTION_PROVIDER=local
```

자동 테스트는 `EXTRACTION_PROVIDER=deterministic`을 명시해 외부 모델에 의존하지 않습니다.

## SQLite Backup / Restore

실행 중 백업은 SQLite Online Backup API로 수행하고, Restore는 Backend를 중지한
상태에서만 허용합니다. `.db` 파일만 직접 복사하지 마세요.

```bash
.venv/bin/jarvis-db backup \
  --database data/personal_ai.db \
  --output backups/personal_ai-20260811T120000Z.db

.venv/bin/jarvis-db verify \
  --database backups/personal_ai-20260811T120000Z.db
```

안전한 Offline Restore, 자동 `pre-restore` 보존본, 보관 정책과 복구 Drill은
[SQLite Backup / Restore Runbook](./SQLITE_BACKUP_RESTORE.md)을 따릅니다.

Ollama Local Provider:

```bash
ollama pull qwen3:4b
EXTRACTION_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:11434
LOCAL_LLM_MODEL=qwen3:4b
LOCAL_LLM_CONTEXT_LENGTH=16384
LOCAL_LLM_MAX_OUTPUT_TOKENS=4096
LOCAL_LLM_RETRY_ATTEMPTS=2
LOCAL_LLM_RETRY_BACKOFF_SECONDS=0.25
```

OpenAI Responses API Provider:

```bash
EXTRACTION_PROVIDER=api
EXTRACTION_API_KEY=your-key
EXTRACTION_API_MODEL=your-model
```

두 Provider 모두 같은 `work-fact-draft.v1` 출력 계약과 Pydantic/
deterministic validation을 통과해야 합니다. Provider를 바꿔도 Canonical Memory
Write 경계는 변하지 않습니다.

Local/API를 선택하면 Extraction과 선택적 추천·보고서 표현에 같은 Structured Output
transport를 사용합니다. LLM 출력 실패는 Canonical Memory를 변경하지 않으며 추천·보고서
표현 실패는 deterministic 문장으로 fallback합니다.

Ollama는 thinking을 끄고 제한된 context/output budget으로 호출합니다. Ollama의
schema-to-grammar 제한 때문에 generation schema에서는 `maxLength`/`maxItems`만 제거하지만,
응답은 원본 Pydantic Schema로 다시 검증하므로 애플리케이션 수용 한도는 그대로입니다.

Local Ollama 전송은 일시적인 timeout/연결 오류와 HTTP 408, 425, 429, 5xx만
최대 `LOCAL_LLM_RETRY_ATTEMPTS`회(전체 시도 횟수)까지 재시도합니다. 400/401/403/404/422,
JSON 파싱 실패, Schema 검증 실패는 재시도하지 않고 즉시 실패합니다. 재시도와
Schema 검증은 Canonical Memory 저장 전에 끝나므로 최종 실패가 업무 기록을 오염시키지
않습니다. `LOCAL_LLM_RETRY_BACKOFF_SECONDS`는 지수 backoff의 시작값이며 테스트에서는
`0`으로 둘 수 있습니다.

## 테스트

```bash
.venv/bin/python -m compileall -q app tests
.venv/bin/pytest -q
```

자동 테스트는 Provider HTTP contract에 Mock Transport를 사용합니다. 실제 Local 모델이나
API 자격증명을 사용하는 품질 평가는 환경 의존적인 opt-in 단계이며 기본 Suite에서는
실행하지 않습니다.

## Validation Summary

실사용 품질은 사용자 업무 내용이나 Raw Conversation을 출력하지 않는 주간
`validation-summary.v1`으로 집계합니다.

```bash
.venv/bin/python scripts/validation_summary.py \
  --database data/personal_ai.db \
  --week-containing 2026-08-12
```

지표 정의, local-date 경계, completeness와 privacy boundary는
[Phase 1 Validation Summary](./VALIDATION_METRICS.md)를 참고하세요.

실사용 오류를 local-only Regression Fixture와 privacy-safe Finding으로 함께
기록할 때는 다음 명령을 사용합니다. `source-ref` 원문은 저장되지 않고 즉시
SHA-256으로 변환됩니다.

```bash
.venv/bin/python scripts/validation_regressions.py record \
  --database data/personal_ai.db \
  --input case.json \
  --source-type USER_CORRECTION \
  --source-ref '<fact_group_id-or-report_id>'

.venv/bin/python scripts/validation_regressions.py export \
  --database data/personal_ai.db \
  > validation-regressions.local.jsonl
```

Export에는 실제 업무 문장이 포함될 수 있으므로 외부 저장소에 바로 Commit하거나
통계 Summary에 첨부하지 않습니다.

현재 개발 환경에서는 `qwen3:4b`로 승인된 3턴 시나리오를 실제 Ollama `/api/chat`에
호출해 모두 200, 동일 Work Item 연결, `WAITING → IN_PROGRESS`, Next Action, Activity
3건, 조회 응답을 확인했습니다. 주간보고도 실제 LLM narration으로 생성했습니다.
API Provider는 자격증명이 없어 HTTP contract까지만 자동 검증했습니다.

## Dashboard API

- `GET /api/v1/dashboard/summary`: Current Work, Waiting, Blocked, 추천 Next Actions
- `GET /api/v1/dashboard/activities?limit=20&offset=0`: 최근 Activity
- `GET /api/v1/dashboard/projects?limit=20&offset=0`: Project 목록
- `GET /api/v1/dashboard/projects/{project_id}`: 현재/완료 업무와 최근 Activity
- `GET /api/v1/dashboard/provider`: UI용으로 정제된 Provider 상태
- `GET /api/v1/reports`, `GET /api/v1/reports/{report_id}`: Report Snapshot

추천 API는 내부 점수와 score breakdown을 반환하지 않습니다. Provider 상태는 모델명과
사용자용 상태만 반환하며 endpoint, 환경 변수, API key를 노출하지 않습니다.

Dashboard CORS 기본 allowlist는 localhost/127.0.0.1의 3000·3001 포트와 배포된
비공개 JARVIS Dashboard 주소입니다. 다른 Origin은
`DASHBOARD_ALLOWED_ORIGINS`에 쉼표로 구분해 명시적으로 추가합니다.

### TTS 경계

TTS는 Backend 외부의 HTTP 서비스이며 기본 Docker 구성은 별도 Piper 컨테이너를
사용합니다. `TTS_BRIDGE_URL`은 컨테이너 내부 주소, `TTS_PUBLIC_BASE_URL`은
브라우저가 재생할 주소입니다. TTS 실패는 `TTS_FAILED` 진단만 남기고 텍스트 응답과
Canonical Memory를 유지합니다. 모델 파일은 named volume으로 다운로드되며 GitHub에
포함되지 않습니다.

현재 전체 Backend Suite는 22개 테스트입니다.

## 주요 문서

- [Architecture](./ARCHITECTURE.md)
- [Phase 1 Stabilization Runbook](./PHASE1_STABILIZATION.md)
- [SQLite Backup / Restore Runbook](./SQLITE_BACKUP_RESTORE.md)
- [Phase 1 Validation Summary](./VALIDATION_METRICS.md)
- [Environment example](./.env.example)
