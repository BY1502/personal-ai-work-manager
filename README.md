# Personal AI Work Manager

대화로 업무를 기록하고 Structured Memory를 기준으로 현재 상태를 보여주는 개인용
BY 업무 매니저입니다.

## Docker 실행

~~~bash
brew services start ollama
ollama pull qwen3.5:35b-a3b-q4_K_M
docker compose up -d --build
~~~

처음 Dashboard를 열면 `계정 만들기`에서 아이디와 10자 이상의 비밀번호를
등록합니다. 첫 번째 계정은 기존 `local-user` 업무 기록을 그대로 인계받는 owner가
되고, 이후 계정은 Project·Work Item·Activity·대화·보고서가 완전히 분리됩니다.
필요한 계정을 모두 만든 뒤에는 `.env`에 `AUTH_ALLOW_REGISTRATION=false`를 설정하고
Backend를 다시 시작하는 편이 안전합니다.

가입 직후 표시되는 복구코드는 DB나 브라우저에 원문으로 저장되지 않으므로 안전한
비밀 저장소에 별도로 보관합니다. 계정 메뉴에서는 비밀번호 변경, 복구코드 재발급,
모든 기기 로그아웃을 사용할 수 있습니다. 로그인과 비밀번호 복구는 각각 15분 동안
5회 실패하면 15분간 제한되며, 이 제한은 Backend를 재시작해도 유지됩니다.

대화 본문은 브라우저 저장소에 복사하지 않습니다. Backend의 Conversation과 Run을
다시 조회해 새로고침 후 대화를 복원하며, 브라우저에는 사용자별 Conversation ID
힌트만 남깁니다. Dashboard에서 새 대화를 만들고 기존 대화를 전환할 수 있으며,
네트워크 재전송으로 빈 대화가 중복 생성되지 않습니다. 로그아웃하거나 DB를
Restore하면 기존 로그인 세션은 폐기됩니다. Restore된 복구코드도 재사용할 수
없으므로 비밀번호로 로그인한 뒤 새 코드를 발급합니다.

## BY 앱으로 설치

Dashboard는 PWA로 설치할 수 있습니다. 먼저 Docker를 실행한 뒤
`http://127.0.0.1:3100`을 Chrome에서 열고 주소창의 설치 아이콘 또는 메뉴의
`페이지를 앱으로 설치`를 선택하세요. 설치 후에는 Dock의 BY 아이콘을 눌러
브라우저 탭 없이 사용할 수 있습니다.

더 빠른 기록을 위해 macOS Spotlight Quick Capture도 제공합니다. 아래 설치 후
`⌘Space`에서 `BY`를 검색하면 한 줄 입력창이 바로 열리고, 입력 내용은 기존
Backend의 안전한 Chat 경로로 저장됩니다.

```bash
./scripts/install_by_spotlight.sh
```

자세한 동작과 설정은 [macOS Quick Capture 안내](desktop/README.md)를 참고하세요.

현재 Backend에는 Desktop UI보다 먼저 검증하는 Phase 2 실행 기반도 포함되어
있습니다. Skill Registry가 `SKILL.md`를 검증하고, Permission Engine이 읽기 Tool
권한을 제한하며, Trigger/Event Engine이 업무 상태를 바탕으로 결정론적 제안을
만듭니다. 제안과 Skill 상태는 로그인한 Dashboard에서 확인할 수 있습니다. 해당 API도
인증 Session이 필요하며 Cookie 값을 명령행이나 문서에 직접 남기지 않습니다.

현재는 `work-capture`와 `calendar-agent`가 단일 순차 Skill Runtime을 사용하며,
Worker가 Structured Memory나 Google Calendar를 직접 쓰지 않습니다. Calendar 조회는
읽기 Tool로 제한되고, 일정 생성은 사용자가 확인한 제안만 JARVIS 전용 Gateway가
실행합니다. Multi-Skill Planner, 병렬 실행, 무승인 외부 액션은 아직 범위에
포함하지 않습니다.

Google OAuth 로컬 연결 방법과 승인 안전 경계는
[Google Calendar 연결 안내](docs/GOOGLE_CALENDAR.md)를 참고하세요.

## LLM Provider 선택

기본값은 로컬 Ollama `qwen3.5:35b-a3b-q4_K_M` 하나입니다. 업무 추출, Skill 실행,
추천 설명, 보고서 요약은 Backend가 시작될 때 선택한 같은 Provider/모델을 공유합니다.
`SKILL.md`가 역할과 지침을 나누며 Skill별로 별도 모델을 선택하지 않습니다.

OpenAI Responses 호환 API를 사용할 때는 `.env`에
다음처럼 설정합니다. 두 경로 모두 같은 구조화 출력 검증을 통과해야 하므로
프로세스 전체의 단일 Provider/모델만 바뀌고 Memory 저장 규칙은 바뀌지 않습니다.

```dotenv
EXTRACTION_PROVIDER=api
EXTRACTION_API_BASE_URL=https://api.openai.com/v1
EXTRACTION_API_KEY=발급받은_키
EXTRACTION_API_MODEL=사용할_모델
```

키는 `.env`에만 두고 GitHub에는 절대 커밋하지 않습니다. 테스트에서는
`EXTRACTION_PROVIDER=deterministic`을 사용해 외부 API 없이 재현합니다.

로그인 후 Docker와 다운로드형 TTS 모델 컨테이너를 한 번에 시작하려면 다음 명령을 사용합니다.

```bash
./scripts/start_by.sh
```

Docker Desktop의 `로그인 시 Docker Desktop 시작`을 켜두면 컨테이너는
`restart: unless-stopped` 정책으로 함께 복구됩니다. PWA는 업무 데이터를
오프라인에 저장하지 않으며, Backend가 준비되지 않은 경우 연결 대기 화면을
표시합니다.

- Dashboard: http://127.0.0.1:3100 (또는 예비 주소 http://127.0.0.1:3001)
- 참고: http://localhost:3000은 이전 데모 서비스와 충돌할 수 있으므로 사용하지 마세요.
- API 문서: http://127.0.0.1:8100/docs
- SQLite: `backend/data/personal_ai.db`

참고: 한 번에 여러 요청이 몰릴 때는 잠시 대기 후 재시도되며,
`503 EXTRACTION_CONCURRENCY_EXCEEDED`는 "서버 과부하"로 인한 정상 동작 상태입니다.
UI에서는 바로 재시도 메시지와 함께 반영되므로 연결 끊김으로 보지 마세요.
최악의 경우에도 `503 EXTRACTION_CONCURRENCY_EXCEEDED`가 반환될 수 있습니다.
로컬 Ollama 응답이 느린 환경에서는 먼저 아래 값을 올려 보세요.

```bash
LOCAL_LLM_TIMEOUT_SECONDS=60
LOCAL_LLM_RETRY_ATTEMPTS=2
LOCAL_LLM_RETRY_BACKOFF_SECONDS=0.25
EXTRACTION_API_TIMEOUT_SECONDS=60
```

그리고 필요하면 `EXTRACTION_MAX_CONCURRENT`(기본 6)와
`EXTRACTION_CONCURRENCY_WAIT_SECONDS`(기본 10초)를 조정해주세요.

기본 바인딩은 `127.0.0.1`이라 이 Mac에서만 접근할 수 있습니다. 같은 네트워크의
다른 기기에서 접근해야 할 때만 `.env`에서 바인딩과 허용 Origin을 명시적으로
추가하세요. 인터넷에 공개하려면 HTTPS reverse proxy와
`AUTH_COOKIE_SECURE=true`가 필수입니다.

```bash
BACKEND_BIND=127.0.0.1
DASHBOARD_BIND=127.0.0.1
```

### 즉시 연결 점검

연결이 계속 끊기는 경우 아래를 순차 실행하세요.

```bash
cd personal-ai-work-manager
./scripts/check_jarvis_connectivity.sh
```

대시보드 주소(:3100)로 직접 점검할 때는 아래처럼 사용하세요.

```bash
cd personal-ai-work-manager
BY_USERNAME='내 아이디' BY_PASSWORD='내 비밀번호' \
API_BASE_URL=http://127.0.0.1:3100 \
BACKEND_BASE_URL=http://127.0.0.1:8100 \
./scripts/verify_chat_connectivity.sh 20 120
```

아이디와 비밀번호는 이 명령의 실행 환경에서만 사용되며 저장소나 로그에 기록하지
마세요. 점검 스크립트는 권한을 `0600`으로 제한한 임시 cookie jar를 사용하고 종료 시
로그아웃합니다.

결과가 모두 `ok`/`passed`가 아니면 바로 스크린샷/오류 메시지와 함께 공유해 주세요.

상태는 `docker compose ps`, 로그는 `docker compose logs -f`로 확인합니다. 종료는
`docker compose down`을 사용하며 호스트의 SQLite 파일은 유지됩니다. 설정 변경이
필요하면 `docker.env.example`을 `.env`로 복사해 수정합니다.
# 다운로드형 TTS

BY는 개인 학습 산출물을 저장소나 기본 실행 경로에 포함하지 않습니다. Docker의
별도 `tts` 서비스가 처음 실행될 때 Piper 한국어 음성 `ko_KR-kss-medium`을
다운로드하고, 모델은 Docker named volume에 보관합니다. 모델 파일은 GitHub에
커밋하지 않습니다.

```bash
docker compose up -d --build
curl http://127.0.0.1:8766/api/health
```

기본 음성은 KSS 데이터셋 기반의 CC BY-NC-SA 4.0 모델이므로 상업 배포 전에는
모델 라이선스와 대체 음성을 별도로 검토해야 합니다. [Piper voice 목록과
다운로드 방식](https://tderflinger.github.io/piper-docs/about/voices/download/)을
참고하세요. TTS가 중단되어도 텍스트 응답과 Structured Memory 저장은 계속됩니다.

학습한 개인 음성을 사용하려면 별도 브리지를 직접 연결할 수 있지만, 공개 기본값은
다운로드형 Piper 모델로 유지합니다. 로컬 브리지 우선 + Piper 대체 구성은
[TTS 서비스와 모델 정책](tts/README.md)에 설명되어 있습니다.

공개 Push 전에는 개인 경로와 모델·음성 산출물이 추적되지 않는지 확인합니다.

```bash
./scripts/check_public_safety.sh
```

## 함께 공부하고 기여하기

- [단계별 학습 경로](docs/LEARNING_PATH.md)
- [기여 가이드](CONTRIBUTING.md)
- [TTS 서비스와 모델 정책](tts/README.md)

각 기능은 설계 이유, 실패 시나리오, 테스트를 함께 변경하는 것을 원칙으로
합니다. 먼저 `backend` 테스트를 통과시키고 Docker에서 실행한 뒤 PR을 올려 주세요.
