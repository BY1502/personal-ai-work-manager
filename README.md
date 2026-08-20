# Personal AI Work Manager

대화로 업무를 기록하고 Structured Memory를 기준으로 현재 상태를 보여주는 개인용
BY 업무 매니저입니다.

## Docker 실행

~~~bash
brew services start ollama
ollama pull qwen3.5:35b-a3b-q4_K_M
docker compose up -d --build
~~~

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
만듭니다. 제안과 Skill 상태는 다음 API에서 확인할 수 있습니다.

```bash
curl 'http://127.0.0.1:8100/api/v1/suggestions?limit=3'
curl 'http://127.0.0.1:8100/api/v1/skills'
```

현재는 `work-capture`와 단일 순차 Skill 실행만 연결되어 있고 Worker가
Structured Memory를 직접 쓰지 않습니다. Multi-Skill Planner, 병렬 실행, 자동
외부 액션, Desktop UI는 아직 범위에 포함하지 않습니다.

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

기본 바인딩은 `0.0.0.0`이라 같은 PC/같은 네트워크에서 모두 접근 가능합니다.
보안을 위해 외부 노출이 필요 없으면 `.env`에서 아래처럼 loopback로 제한할 수 있습니다.

```bash
BACKEND_BIND=127.0.0.1
DASHBOARD_BIND=127.0.0.1
```

또는 브라우저 주소창에서 `localhost`/`127.0.0.1`로 접속하세요.

### 즉시 연결 점검

연결이 계속 끊기는 경우 아래를 순차 실행하세요.

```bash
cd 'personal-ai-work-manager'
./scripts/check_jarvis_connectivity.sh
```

대시보드 주소(:3100)로 직접 점검할 때는 아래처럼 사용하세요.

```bash
cd 'personal-ai-work-manager'
API_BASE_URL=http://127.0.0.1:3100 BACKEND_BASE_URL=http://127.0.0.1:8100 ./scripts/verify_chat_connectivity.sh 20 120
```

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
다운로드형 Piper 모델로 유지합니다.

## 함께 공부하고 기여하기

- [단계별 학습 경로](docs/LEARNING_PATH.md)
- [기여 가이드](CONTRIBUTING.md)
- [TTS 서비스와 모델 정책](tts/README.md)

각 기능은 설계 이유, 실패 시나리오, 테스트를 함께 변경하는 것을 원칙으로
합니다. 먼저 `backend` 테스트를 통과시키고 Docker에서 실행한 뒤 PR을 올려 주세요.
