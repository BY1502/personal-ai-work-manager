# BY macOS Quick Capture

`BY.app`은 macOS Spotlight에서 찾을 수 있는 작은 Quick Capture 앱입니다.

1. `⌘Space`를 누릅니다.
2. `BY`를 검색하고 앱을 실행합니다.
3. 한 줄 업무 문장을 입력합니다.
4. BY가 기존 Backend의 `/api/v1/chat/runs`로 전송하고 응답을 보여줍니다.

앱은 Canonical Memory를 직접 수정하지 않습니다. Docker Backend가 처리하는 기존
Idempotency, LLM 검증, Context Linking, WorkManager 경계를 그대로 사용합니다.

## 설치

프로젝트 루트에서 다음을 실행합니다.

```bash
./scripts/install_by_spotlight.sh
```

설치 위치는 `~/Applications/BY.app`입니다. Spotlight 색인에 반영되기까지 잠시
걸릴 수 있습니다. 앱 실행 시 Backend가 꺼져 있으면 Docker Compose를 자동으로
시작하고 최대 30초 동안 `/health`를 확인합니다.

소스가 업데이트된 뒤에는 `./scripts/install_by_spotlight.sh --update`를 사용합니다.
기존 앱은 삭제하지 않고 `~/Library/Application Support/BY/previous/`에 보관합니다.

## 설정

기본값은 다음과 같습니다.

- Backend: `http://127.0.0.1:8100`
- Dashboard: `http://127.0.0.1:3100/personal-ai-work-manager/dashboard`
- 프로젝트 경로: 설치 시점의 현재 저장소 경로

다른 경로/주소가 필요하면 설치 후 실행할 때 `BY_PROJECT_DIR`,
`BY_API_BASE_URL`, `BY_DASHBOARD_URL` 환경 변수를 사용할 수 있습니다. 일반 사용은
Spotlight 앱 실행으로 충분합니다.

## 학습 포인트

이 앱은 별도의 Memory 저장 계층이 아닙니다. macOS UI는 입력과 표시만 담당하고,
실제 판단과 저장은 기존 Backend가 담당합니다. 따라서 Quick Capture를 추가해도
웹 Dashboard, API Provider, SQLite Backup/Restore와 같은 기존 경계가 변하지 않습니다.
