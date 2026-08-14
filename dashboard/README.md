# BY Dashboard

개인 AI 업무 매니저의 Chat 중심 Next.js Dashboard입니다. 업무 데이터는
Frontend에 별도로 저장하지 않고 Personal AI Work Manager Backend의
Structured Memory를 기준으로 표시합니다.

## 실행

Node.js 22.13 이상과 실행 중인 Backend가 필요합니다.

```bash
pnpm install
pnpm dev
```

Chrome에서 `http://127.0.0.1:3100`을 연 뒤 `페이지를 앱으로 설치`를 선택하면
BY를 Dock의 독립 앱 창으로 사용할 수 있습니다. 앱 설치에는 Backend가 실행 중인
상태가 필요합니다.

기본 Backend 주소는 `현재 페이지의 hostname:8100`입니다. (로컬 테스트 환경에서는
`docker compose` 기본 포트가 이 값입니다.)
다른 주소를 사용할 때만 다음 값을 설정합니다.

```bash
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

배포 URL이 정해진 경우 Open Graph 절대 주소 생성을 위해
`NEXT_PUBLIC_SITE_URL`을 설정할 수 있습니다.

## 화면 구성

- BY Chat과 Clarification 선택
- Current Work, Waiting, Blocked, Next Actions
- Recent Activity와 Project Detail
- Daily, Weekly, Project, Range Report
- Local/API Provider의 사용자용 상태 표시

## 검증

```bash
pnpm lint
pnpm test
```

`pnpm test`는 배포 빌드, API contract와 polling 단위 테스트, 서버 렌더링
검증을 차례로 실행합니다.
