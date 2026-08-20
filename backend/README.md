# Personal AI Work Manager Backend

개인 업무 문장을 Structured Work Memory로 기록하고, Context를 이어서 조회·추천·보고서로
제공하는 Phase 1 Backend입니다. 현재 범위는 M1–M8이며 Next.js Dashboard가 사용할
읽기 전용 projection API를 포함합니다.

Desktop UI 확장에 앞서 Phase 2의 첫 실행 경계로 Trigger/Event Engine,
Permission Engine, 범용 단일 Skill 실행을 추가했습니다.

두 번째 Skill인 `calendar-agent`는 한국어 일정 조회와 일정 생성 제안을 처리합니다.
`calendar.events.list`만 Worker용 읽기 Tool로 등록하며, Calendar Write Tool은 제공하지
않습니다. 생성 제안은 별도 `calendar_action_proposals` 승인 Ledger에 저장되고 사용자가
승인한 뒤에만 Google Calendar Gateway가 실행합니다. OAuth 비밀정보는 환경변수로만
주입합니다.

## Phase 2 첫 Vertical Slice

`skills/*/SKILL.md`를 Skill Registry가 발견하고 YAML/Schema/Tool/Permission을
검증한 뒤 활성화합니다. 현재 `work-capture`는 기존 `work-fact-draft.v1` 계약으로
Chat에 연결되어 있으며, 범용 Skill은 내부 `SkillRuntime.invoke()`로 같은
입력·출력 검증과 실행 ledger를 사용합니다. Worker는 Canonical Memory를 직접
수정하지 않고 WorkManager/Context Linking/Receipt 경계만이 Structured Memory를
변경합니다.

읽기 전용 Tool Registry에는 `project.search`, `work.search`,
`memory.get_recent`, `calendar.events.list`가 등록되어 있습니다. `PermissionEngine`은 Skill manifest와
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

## 로그인과 사용자 격리

운영 기본값은 `AUTH_REQUIRED=true`입니다. 비밀번호는 사용자별 salt가 있는 scrypt
credential로 저장하고, 브라우저에는 `HttpOnly · SameSite=Lax` 세션 쿠키만
발급합니다. SQLite에는 원본 세션 토큰이 아닌 SHA-256 digest만 저장합니다.

- `POST /api/v1/auth/register`: 계정 생성. 첫 계정은 기존 `local-user` 데이터를 인계
- `POST /api/v1/auth/login`: 새 opaque session 발급
- `POST /api/v1/auth/logout`: 현재 session 폐기
- `POST /api/v1/auth/logout-all`: 현재 사용자의 모든 session 폐기
- `POST /api/v1/auth/password/change`: 현재 비밀번호 재확인 후 비밀번호·복구코드 교체
- `POST /api/v1/auth/password/reset`: 로그인할 수 없을 때 일회용 복구코드로 재설정
- `POST /api/v1/auth/recovery-code/rotate`: 현재 비밀번호 재확인 후 복구코드 재발급
- `GET /api/v1/auth/me`: 현재 사용자 확인
- `POST /api/v1/chat/conversations`: `Idempotency-Key` 기반 새 대화 생성
- `GET /api/v1/chat/conversations`: 로그인 사용자의 대화 목록
- `GET /api/v1/chat/conversations/{id}/messages`: 저장된 Run 결과를 포함한 대화 복원

등록·비밀번호 변경·비밀번호 재설정 응답의 `recovery_code`는 그 응답에서만
확인할 수 있습니다. DB에는 정규화한 코드의 digest만 저장하며, 재설정이 성공하면
기존 세션 전체와 기존 복구코드가 즉시 무효화됩니다. 로그인과 복구 시도는 서로
분리된 익명 identifier digest를 SQLite에 기록해 각각 15분 동안 5회 실패하면
15분간 `429`로 제한합니다. 사용자명이나 IP 원문은 제한 테이블에 저장하지 않습니다.

각 요청은 로그인 사용자의 ID로 새 `JarvisOrchestrator` facade를 구성합니다. 따라서
동시에 요청한 두 사용자가 singleton 상태를 공유하지 않으며, Run·Clarification·
Project·Activity·Report 조회도 소유권 조건을 통과해야 합니다. 현재 Google OAuth
자격증명은 프로세스 전역이므로 첫 owner 계정만 Calendar 기능을 사용할 수 있습니다.

운영 기본값은 Local Ollama `qwen3.5:35b-a3b-q4_K_M`입니다. 하나의 Backend
프로세스는 업무 추출, Skill 실행, 추천 설명, 보고서 요약에 같은 Provider와 모델을
공유합니다. `jarvis-reasoning`, `worker-balanced`, `worker-fast`는 SKILL.md와 실행
로그에 남는 논리적 역할 라벨일 뿐 별도 모델로 라우팅하지 않습니다. API Provider를 사용하려면
`EXTRACTION_PROVIDER=api`로 전환할 수 있으며, OpenAI Responses 호환 endpoint와
모델을 환경 변수로 지정합니다.

```bash
ollama serve
ollama pull qwen3.5:35b-a3b-q4_K_M
EXTRACTION_PROVIDER=local
```

자동 테스트는 `EXTRACTION_PROVIDER=deterministic`을 명시해 외부 모델에 의존하지 않습니다.

### Local 모델 품질 검증

모델을 교체하기 전에는 실제 사용자 DB와 분리된 합성 한국어 회귀 세트로
구조화 정확도와 지연시간을 비교합니다. 아래 스크립트는 DB를 열거나 수정하지 않고,
모델 출력이 기존 Pydantic Schema와 deterministic validator를 통과한 뒤의 의미만
평가합니다.

```bash
cd backend
.venv/bin/python scripts/benchmark_extraction_models.py \
  qwen3.5:35b-a3b-q4_K_M
```

결과에는 전체 사례 통과 수, semantic check rate, 평균/P95 지연시간과 실패한
case ID만 포함됩니다. 실제 업무 원문이나 Canonical Memory는 평가 로그에 넣지 않습니다.

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
ollama pull qwen3.5:35b-a3b-q4_K_M
EXTRACTION_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:11434
LOCAL_LLM_MODEL=qwen3.5:35b-a3b-q4_K_M
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
JSON 파싱 실패, Schema 검증 실패는 HTTP 전송 재시도를 일으키지 않습니다. 기존
`repair_attempts=1` 설정은 잘못된 모델 출력을 한 번 더 Schema에 맞춰 생성하도록
요청할 수 있지만, 그 출력도 실패하면 즉시 종료합니다. 모든 전송 재시도·수정 출력·
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

현재 개발 환경에서는 `qwen3.5:35b-a3b-q4_K_M`으로 승인된 3턴 시나리오를 실제 Ollama `/api/chat`에
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

Dashboard CORS 기본 allowlist는 localhost/127.0.0.1의 3000·3001·3100 포트입니다.
다른 Origin은
`DASHBOARD_ALLOWED_ORIGINS`에 쉼표로 구분해 명시적으로 추가합니다.

### TTS 경계

TTS는 Backend 외부의 HTTP 서비스이며 기본 Docker 구성은 별도 Piper 컨테이너를
사용합니다. `TTS_BRIDGE_URL`은 컨테이너 내부 주소, `TTS_PUBLIC_BASE_URL`은
브라우저가 재생할 주소입니다. TTS 실패는 `TTS_FAILED` 진단만 남기고 텍스트 응답과
Canonical Memory를 유지합니다. 모델 파일은 named volume으로 다운로드되며 GitHub에
포함되지 않습니다. 선택적인 `TTS_FALLBACK_*` 설정을 사용하면 로컬 개인 음성 실패 시
Piper로 음성 합성만 한 번 대체합니다. Run은 TTS보다 먼저 완료되므로 합성 중 재시작도
Canonical 작업 재적용을 유발하지 않습니다.

현재 전체 Backend Suite는 84개 테스트입니다.

## 주요 문서

- [Architecture](./ARCHITECTURE.md)
- [Phase 1 Stabilization Runbook](./PHASE1_STABILIZATION.md)
- [SQLite Backup / Restore Runbook](./SQLITE_BACKUP_RESTORE.md)
- [Phase 1 Validation Summary](./VALIDATION_METRICS.md)
- [Environment example](./.env.example)
- [Google Calendar 연결](../docs/GOOGLE_CALENDAR.md)
