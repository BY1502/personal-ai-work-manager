FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY server.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /models /data/audio

ENV PIPER_MODEL_DIR=/models \
    PIPER_OUTPUT_DIR=/data/audio \
    PIPER_VOICE=ko_KR-kss-medium \
    PIPER_AUTO_DOWNLOAD=true \
    TTS_PORT=8765

EXPOSE 8765
ENTRYPOINT ["/app/entrypoint.sh"]
