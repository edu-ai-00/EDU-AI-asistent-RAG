# QA portál — JEDEN image pro JSON API i sestavené statické frontendy.
#
# Stejný princip jako web_chat/Dockerfile: Node pouze v build fázi, výsledný
# `dist` se zkopíruje do Python runtime. Za běhu neběží ani Node, ani vývojový
# server frontendu.
#
# Sdílený kontejner NEZNAMENÁ vazbu publikační logiky na konkrétní obrazovku:
# frontend a backend spolu mluví jen přes verzovaný HTTP kontrakt
# (contracts/qa-portal/openapi.yaml). Další frontendový build (např. /qa-simple/)
# přibude jako další build fáze a další statický adresář téhož procesu.
#
# Build z KOŘENE repozitáře, protože kontext potřebuje celý qa_portal/:
#     docker compose build edu_ai_qa_admin

FROM node:24.18.0-alpine AS frontend-build

WORKDIR /frontend
# Lockfile zvlášť a před zdrojáky: změna komponenty pak neruší cache npm vrstvy.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# base /qa/ je vlastnost nasazení za nginx, ne kódu — proto z build argumentu.
ARG PORTAL_BASE=/qa/
ENV VITE_BASE=${PORTAL_BASE}
# src/config.ts má USE_FIXTURES výchozí true, aby se frontend dal vyvíjet proti
# zmrazeným fixturám dřív, než backend existuje. V IMAGE to ale musí být vypnuté:
# jinak by se nasadil portál, který mluví s mockem a tváří se, že publikuje.
ENV VITE_QA_USE_FIXTURES=false
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app

# Statické buildy leží v oddělených adresářích, jeden na frontend. API router se
# registruje PŘED statickými mounty a SPA fallback nesmí vracet index.html pro
# neznámé /qa-api/ cesty — vynucuje app/main.py, ne tento soubor.
COPY --from=frontend-build /frontend/dist ./static/qa

# Stav operací, náhledů importu a dočasných exportů. Pojmenovaný svazek se sem
# připojuje z Compose; adresář existuje i bez něj, aby služba nastartovala.
# ZÁMĚRNĚ mimo ./static — nic z toho nesmí být dosažitelné jako statický soubor.
RUN mkdir -p /var/lib/qa_admin_state
ENV QA_ADMIN_STATE_DIR=/var/lib/qa_admin_state

# Jediný interní port. Na hosta se nepublikuje nic — přístup vede přes nginx
# na VPN listeneru 80 (/qa/ a /qa-api/v1/).
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/qa-api/v1/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
