# Contributing to BY

BY는 기능을 빠르게 늘리는 것보다, 작은 변경을 이해하고 재현할 수 있는 것을
우선합니다. 처음 기여한다면 먼저 [README](README.md)와
[학습 경로](docs/LEARNING_PATH.md)를 읽어 주세요.

## 개발 원칙

1. 사용자 업무 원문을 테스트 로그나 통계에 남기지 않습니다.
2. LLM은 후보를 만들고, 결정론적 검증 코드가 저장 여부를 결정합니다.
3. Worker나 Skill이 SQLite를 직접 수정하지 않습니다.
4. Canonical Memory 변경에는 테스트와 Audit/Receipt 경계가 필요합니다.
5. 외부 Provider가 실패해도 텍스트 응답과 DB 무결성이 우선입니다.
6. 새 모델 파일, API key, 개인 DB, 생성 음성은 커밋하지 않습니다.

## 로컬 확인

```bash
cd backend
python -m compileall -q app tests
pytest -q

cd ..
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

## 변경 제출 방식

- 한 커밋은 하나의 학습 가능한 주제에 집중합니다.
- PR 본문에 문제, 설계 선택, 실패 시나리오, 테스트 결과를 적습니다.
- UI 변경보다 먼저 Backend contract와 regression test를 업데이트합니다.
- Provider/TTS 모델은 adapter와 환경 변수로 교체 가능하게 유지합니다.
