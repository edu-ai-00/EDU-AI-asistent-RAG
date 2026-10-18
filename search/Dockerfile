# Pinned by DIGEST, not by tag: `python:3.12-slim` moves upstream, and every move
# invalidates this layer and every layer under it — so a rebuild that changed
# nothing in this repo re-ran pip from scratch and re-downloaded gigabytes.
# Bump this deliberately (docker pull python:3.12.<patch>-slim, read RepoDigests).
FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# CPU torch FIRST, from PyTorch's own CPU index. sentence-transformers pulls
# torch, and on linux/amd64 PyPI serves the CUDA build — ~3 GB of nvidia-*
# wheels that can never run here (torch.cuda.is_available() is False), which is
# also what requirements.txt already says the reranker is: in-process, CPU.
# Installing it here satisfies the dependency, so the next step will not fetch
# the CUDA variant. Keep the version in step with what requirements.txt resolves.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch==2.14.0
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
