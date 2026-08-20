# SQLite Backup / Restore Runbook

업무 DB는 WAL 모드로 실행되므로 실행 중인 `.db` 파일만 직접 복사하지 않습니다.
Backup은 SQLite Online Backup API를 사용하고, Restore는 Backend를 완전히 중지한
상태에서만 수행합니다. Backup 파일에는 실제 업무 내용이 있으므로 GitHub에 올리지
않습니다.

## 실행 중 Backup

```bash
mkdir -p backend/data/backups
docker compose exec -T backend jarvis-db backup \
  --database /app/data/personal_ai.db \
  --output /app/data/backups/personal_ai-YYYYMMDDTHHMMSSZ.db

docker compose exec -T backend jarvis-db verify \
  --database /app/data/backups/personal_ai-YYYYMMDDTHHMMSSZ.db
```

Backup 결과의 SHA-256과 크기를 별도 운영 기록에 남깁니다. 정기적으로 최근 Backup을
다른 로컬 디스크 또는 암호화된 저장소에도 복사합니다.

## Offline Restore

```bash
docker compose stop dashboard backend

docker compose run --rm --no-deps backend jarvis-db verify \
  --database /app/data/backups/personal_ai-YYYYMMDDTHHMMSSZ.db

docker compose run --rm --no-deps backend jarvis-db restore \
  --database /app/data/personal_ai.db \
  --backup /app/data/backups/personal_ai-YYYYMMDDTHHMMSSZ.db \
  --safety-backup /app/data/backups/pre-restore-YYYYMMDDTHHMMSSZ.db

docker compose up -d backend dashboard
```

Restore는 입력 Backup, 설치 직전 임시 DB, 설치된 DB에 대해 integrity·foreign key·필수
Schema 검사를 수행하고 원자적으로 교체합니다. 실패하면 설치 직전 보존본으로
되돌립니다.

## Restore 후 확인

```bash
docker compose ps
curl -fsS http://127.0.0.1:8100/health
docker compose exec -T backend jarvis-db verify \
  --database /app/data/personal_ai.db
```

보안을 위해 Restore 직후 모든 로그인 Session, 복구코드, 로그인/복구 제한 상태가
무효화됩니다. 기존 비밀번호로 다시 로그인한 뒤 새 복구코드를 발급하고 안전하게
보관합니다. 계정·Project·Work Item·Activity·대화 건수도 Restore 전 운영 기록과
비교합니다.

## 정기 복구 Drill

최소 월 1회 운영 DB가 아닌 별도 임시 경로에서 최근 Backup을 Restore하고 다음을
확인합니다.

- integrity 및 foreign key 검사 통과
- 모든 Migration 적용 가능
- Backend 기동 및 로그인 가능
- 사용자별 대화와 Structured Memory 분리 유지
- 기존 Session과 복구코드 재사용 불가
