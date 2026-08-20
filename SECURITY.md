# Security and privacy

이 저장소는 공개 저장소입니다. 코드, Schema, 합성 테스트와 운영 문서는 공개할 수
있지만 다음 항목은 Commit하거나 Issue/PR에 첨부하지 않습니다.

- `.env`와 API/OAuth Key, Client Secret, Refresh/Access Token
- SQLite 업무 DB, Backup, WAL/SHM 파일
- 실제 Raw Conversation과 local-only Regression JSONL
- 개인 음성, Speaker embedding, 직접 학습한 모델·Checkpoint
- 실제 회사 문서, 고객 정보, 이메일·전화번호 등 개인 식별 정보

## 로컬 인증 운영

- 첫 계정은 기존 업무 DB를 인계받는 owner이므로 loopback 환경에서 본인이 먼저 생성합니다.
- 필요한 계정을 만든 뒤에는 `AUTH_ALLOW_REGISTRATION=false`를 권장합니다.
- 기본 바인딩 `127.0.0.1`을 LAN/인터넷으로 넓힐 때는 허용 Origin을 정확히 지정합니다.
- HTTPS를 사용하는 배포에서는 `AUTH_COOKIE_SECURE=true`로 설정합니다.
- DB Restore 후 모든 세션이 무효화되어 다시 로그인하는 것은 의도된 동작입니다.
- DB Restore 후에는 과거 복구코드도 무효화됩니다. 비밀번호로 로그인한 뒤 새
  복구코드를 발급하여 안전한 곳에 보관합니다.
- 복구코드는 등록·비밀번호 변경·재발급·재설정 직후 한 번만 표시되며 DB에는
  digest만 남습니다. 화면을 닫기 전에 별도 비밀 저장소에 보관합니다.
- 비밀번호와 session cookie를 Issue, 로그, 점검 결과에 붙이지 않습니다.

## 로컬 TTS 경계

Piper 음성 파일은 기본적으로 loopback에만 공개되는 추측하기 어려운 임의 URL로
제공되지만, 현재 TTS 서비스 자체에는 사용자 인증과 자동 보존기간이 없습니다.
`piper_audio` Volume도 업무 DB와 같은 민감 데이터로 취급하고 외부에 공개하지
않습니다. LAN/인터넷 또는 여러 OS 사용자가 함께 쓰는 배포 전에는 인증된 Backend
Proxy와 짧은 보존기간 삭제 정책을 추가해야 합니다.

## 공개 전 확인

```bash
git status --short
git ls-files | rg '(\.env$|\.db|\.sqlite|credentials|client_secret|\.npz|\.gguf|\.safetensors)'
git grep -n -I -E '(BEGIN (RSA|OPENSSH) PRIVATE KEY|refresh_token|client_secret|api[_-]?key)'
```

두 번째 명령은 결과가 없어야 합니다. 세 번째 명령은 예제 변수명과 문서 설명만
나올 수 있으며 실제 값이 포함되면 안 됩니다.

비밀정보가 이미 Commit되었다면 단순히 파일을 삭제하는 것으로 충분하지 않습니다.
먼저 해당 Credential을 폐기·재발급하고 Git history 정리 여부를 별도로 판단합니다.

보안 취약점에는 실제 업무 내용이나 Credential을 넣지 말고, 재현 가능한 합성 입력과
안전한 오류 코드만 사용해 보고해주세요.
