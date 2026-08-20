# Google Calendar 연결

BY의 `calendar-agent`는 일정 조회와 일정 생성 제안을 담당합니다. 조회는 읽기 전용
Tool을 사용하고, 생성은 사용자가 승인하기 전까지 Google Calendar를 변경하지 않습니다.

## 안전 경계

- Local LLM은 `calendar-action-draft.v1`만 반환합니다.
- Worker에는 Calendar Write Tool이 등록되어 있지 않습니다.
- 일정 생성은 SQLite의 `PENDING_APPROVAL` 제안으로 먼저 저장됩니다.
- 승인 요청에는 `Idempotency-Key`가 필요합니다.
- Google Event ID는 제안에서 결정적으로 생성되어 중복 클릭과 모호한 재시도에서
  같은 일정을 두 번 만들지 않습니다.
- OAuth Client Secret과 Refresh Token은 `.env`에만 저장하며 Git에 올리지 않습니다.

## Google Cloud 설정

1. Google Cloud Console에서 프로젝트를 만들고 Google Calendar API를 활성화합니다.
2. OAuth 동의 화면을 설정하고 본인 Google 계정을 Test user로 추가합니다.
3. Desktop app 또는 Web application OAuth Client를 생성합니다.
4. `https://www.googleapis.com/auth/calendar.events` Scope로 본인 계정의 Refresh Token을
   발급합니다. Google OAuth Playground를 사용한다면 설정에서 자신의 OAuth Client
   ID/Secret을 사용해야 합니다.
5. 저장소 루트의 추적되지 않는 `.env`에 아래 값을 넣습니다.

```dotenv
GOOGLE_CALENDAR_ENABLED=true
GOOGLE_CALENDAR_CLIENT_ID=로컬에만_저장
GOOGLE_CALENDAR_CLIENT_SECRET=로컬에만_저장
GOOGLE_CALENDAR_REFRESH_TOKEN=로컬에만_저장
GOOGLE_CALENDAR_ID=primary
GOOGLE_CALENDAR_TIMEOUT_SECONDS=20
```

변경 후 Backend만 다시 생성합니다.

```bash
docker compose up -d --build --force-recreate backend
curl http://127.0.0.1:8100/api/v1/calendar/status
```

응답의 `state`가 `READY`이면 연결된 상태입니다. endpoint, Client Secret, Refresh
Token은 Dashboard 상태 응답과 실행 로그에 포함되지 않습니다.

## 사용 예

```text
오늘 일정 알려줘.
내일 오후 2시에 박사님 미팅 1시간 등록해줘.
```

일정 등록 요청은 먼저 확인 카드로 표시됩니다. 승인 시에만 Google API Write가
수행되며, 거절하면 제안만 `REJECTED`로 남습니다.
