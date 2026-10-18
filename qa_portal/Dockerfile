# QA portal — ONE image for the JSON API and the built static frontends.
#
# Same principle as web_chat/Dockerfile: Node only in the build stage, the
# resulting `dist` is copied into the Python runtime. Neither Node nor a frontend
# dev server runs at runtime.
#
# A shared container does NOT tie the publishing logic to a particular screen:
# frontend and backend talk only through the versioned HTTP contract
# (contracts/qa-portal/openapi.yaml). Another frontend build (e.g. /qa-simple/)
# would be one more build stage and one more static directory of the same process.
#
# Build from the REPOSITORY ROOT, because the context needs the whole qa_portal/:
#     docker compose build edu_ai_qa_admin

FROM node:26.12.0-alpine@sha256:6f08a523443ddedf52941874c203475b0ad503e859ad70cb26ab8be9a517dbd5 AS frontend-build

WORKDIR /frontend
# The lockfile separately and before the sources: a component change then keeps the npm layer cache.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# base /qa/ belongs to the deployment behind nginx, not to the code — hence a build argument.
ARG PORTAL_BASE=/qa/
ENV VITE_BASE=${PORTAL_BASE}
# src/config.ts defaults USE_FIXTURES to true, so the frontend could be developed against
# frozen fixtures before the backend existed. In the IMAGE it must be off: otherwise a
# portal would ship that talks to a mock and only pretends to publish.
ENV VITE_QA_USE_FIXTURES=false
RUN npm run build

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app

# Static builds live in separate directories, one per frontend. The API router is
# registered BEFORE the static mounts and the SPA fallback must not return index.html for
# unknown /qa-api/ paths — app/main.py enforces that, not this file.
COPY --from=frontend-build /frontend/dist ./static/qa

# State of operations, import previews and temporary exports. A named volume is mounted
# here from Compose; the directory exists without it too, so the service starts.
# DELIBERATELY outside ./static — none of it may be reachable as a static file.
RUN mkdir -p /var/lib/qa_admin_state
ENV QA_ADMIN_STATE_DIR=/var/lib/qa_admin_state

# The only internal port. Nothing is published on the host — access goes through nginx
# on the VPN listener 80 (/qa/ and /qa-api/v1/).
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/qa-api/v1/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
