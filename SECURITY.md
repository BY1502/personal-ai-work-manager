# Security and privacy

이 저장소는 공개 저장소입니다. 코드, Schema, 합성 테스트와 운영 문서는 공개할 수
있지만 다음 항목은 Commit하거나 Issue/PR에 첨부하지 않습니다.

- `.env`와 API/OAuth Key, Client Secret, Refresh/Access Token
- SQLite 업무 DB, Backup, WAL/SHM 파일
- 실제 Raw Conversation과 local-only Regression JSONL
- 개인 음성, Speaker embedding, 직접 학습한 모델·Checkpoint
- 실제 회사 문서, 고객 정보, 이메일·전화번호 등 개인 식별 정보

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
