# edu_ai Langflow image — built on the OFFICIAL langflow image (plan B1).
# Rationale: pip-installing langflow from scratch (the old python:3.11-slim
# approach) breaks on 1.10.x — the resolver runs for tens of minutes and
# dies under QEMU. The official image ships Langflow preinstalled with a
# locked dependency set (Python 3.14, venv at /app/.venv); we only add
# system packages and our extra Python deps WITHOUT moving anything
# Langflow pinned (see the freeze-as-constraints trick below).
FROM langflowai/langflow:1.10.1

USER root

# System deps (parity with the 1.7.1 image: pdf tooling + libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Our extra deps — constrained by the base image's own freeze, so the
# Langflow dependency set cannot be upgraded/downgraded by this step.
COPY requirements-langflow.txt /tmp/requirements-langflow.txt
RUN /app/.venv/bin/pip freeze > /tmp/langflow-lock.txt \
    && /app/.venv/bin/pip install --no-cache-dir \
        -c /tmp/langflow-lock.txt \
        -r /tmp/requirements-langflow.txt

# Shared CZ normalization/lemma/stem — the SAME package the search image installs,
# so the ingestion ChunkLemmatizer component runs byte-identical code to the query
# side (query↔index parity). Source: search/eduai_normalize/. simplemma (its only
# dep) is already installed above; --no-deps keeps the Langflow locked set intact.
COPY search/eduai_normalize /tmp/eduai_normalize
RUN /app/.venv/bin/pip install --no-cache-dir --no-deps /tmp/eduai_normalize

ENV PYTHONUNBUFFERED=1

# ── Non-root user with the SAME paths the compose file mounts ──────────
# (./langflow_data -> /home/appuser/.cache/langflow, sqlite DB + secret_key)
# uid pinned (1000 is taken by the langflow base image): every bind mount below
# is owned by this uid on the host, so it must not drift on a base-image bump.
RUN useradd -m -u 1001 appuser \
    && mkdir -p /home/appuser/.cache/langflow \
    && chown -R appuser:appuser /home/appuser/.cache \
    && chmod -R o+w /app/.venv/lib/python3.14/site-packages/langflow/alembic \
    && mkdir -p /data/uploads /data/sqllite_export \
    && chown -R appuser:appuser /data
USER appuser

CMD ["langflow", "run", "--host", "0.0.0.0", "--port", "7860"]
