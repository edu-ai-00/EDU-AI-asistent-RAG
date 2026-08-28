FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared CZ normalization/lemma/stem — installed into BOTH this image and the
# Langflow image so the query side and the ingestion ChunkLemmatizer run
# byte-identical code (query↔index parity). Single source: search/eduai_normalize/.
COPY eduai_normalize/ ./eduai_normalize/
RUN pip install --no-cache-dir ./eduai_normalize

COPY app/ ./app/

# Reranker model cache (HF hub) — a named volume mounts here in compose so the
# ~2 GB bge-reranker-v2-m3 download survives rebuilds; must be writable by the
# non-root user, so create + chown it before dropping privileges.
ENV HF_HOME=/models \
    RERANK_MODEL=BAAI/bge-reranker-v2-m3

# ── Non-root user ─────────────────────────────────
RUN useradd -m appuser \
 && mkdir -p /models && chown -R appuser:appuser /models
USER appuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8090/health || exit 1

CMD ["uvicorn", "app.server:api", "--host", "0.0.0.0", "--port", "8090", "--workers", "2"]
