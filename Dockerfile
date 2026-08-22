FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY admin ./admin
COPY corpora ./corpora
COPY ingest ./ingest
COPY scripts ./scripts
COPY config.py storage.py main.py ./

# /data 하나에 벡터·문서·운영 DB가 모두 담긴다. 백업 대상은 이 볼륨 하나다.
RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /data/chroma_db /data/docs \
    && chown -R appuser:appuser /app /data

USER appuser
EXPOSE 8000
VOLUME ["/data"]

# --workers 1 은 의도적이다. Chroma는 SQLite 기반이라 다중 프로세스 쓰기가
# 위험하고, 색인 잡 러너도 이 프로세스 안의 단일 워커 스레드로 돈다.
# 처리량을 늘려야 하면 워커 수가 아니라 SEARCH_CONCURRENCY 를 조정한다.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
