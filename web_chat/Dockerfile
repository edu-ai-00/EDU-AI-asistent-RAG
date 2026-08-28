FROM node:24.18.0-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY web_chat.py ./
COPY --from=frontend-build /frontend/dist ./static

EXPOSE 8081
CMD ["uvicorn", "web_chat:app", "--host", "0.0.0.0", "--port", "8081"]
