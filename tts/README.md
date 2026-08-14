# Downloaded TTS service

BY의 기본 TTS는 Backend와 분리된 Piper HTTP 서비스입니다.

## 왜 별도 서비스인가?

TTS는 업무 기록의 Canonical Memory와 무관한 표현 계층입니다. 따라서 TTS 모델이
느리거나 실패해도 업무 저장과 텍스트 응답은 계속되어야 합니다. Backend는
`POST /api/generate`에 텍스트를 전달하고, 브라우저에서 재생할 audio URL만 받습니다.

## 모델 다운로드

기본 음성은 `ko_KR-kss-medium`입니다. Docker 첫 실행 시 모델과 설정 파일을
`piper_models` named volume에 다운로드합니다. 모델 파일과 생성 음성은 저장소에
커밋하지 않습니다.

```bash
docker compose up -d --build tts
curl http://127.0.0.1:8766/api/health
```

다른 Piper 음성은 `.env`에서 변경할 수 있습니다.

```dotenv
PIPER_VOICE=ko_KR-kss-medium
PIPER_AUTO_DOWNLOAD=true
```

기본 KSS 음성은 CC BY-NC-SA 4.0 조건이 있으므로 상업 배포 전 모델 라이선스를
확인해야 합니다. [Piper voice 다운로드 문서](https://tderflinger.github.io/piper-docs/about/voices/download/)와
[KSS voice model card](https://huggingface.co/rhasspy/piper-voices)에서 원문
조건을 확인하세요.

## 학습용 음성 연결

개인 학습 음성을 사용하고 싶다면 이 서비스와 호환되는 별도 HTTP 서버를 직접
운영하고 `TTS_BRIDGE_URL`, `TTS_PUBLIC_BASE_URL`만 바꿀 수 있습니다. 개인 모델
경로를 애플리케이션 코드에 하드코딩하지 않는 것이 공개 저장소를 유지하는 핵심
규칙입니다.
