# Edu AI web chat

A simple VPN-only web interface on top of the existing `edu_ai_search` API.

## Running locally

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:SEARCH_BASE_URL = "http://127.0.0.1:8090"
.\.venv\Scripts\uvicorn web_chat:app --host 127.0.0.1 --port 8081
```

Frontend for development:

```powershell
cd frontend
npm ci
npm run dev
```

The production frontend is built by a multi-stage `Dockerfile` and FastAPI serves
it under `/chat/`. The browser calls only `POST /chat/api/message`; the BFF then
calls the internal `POST http://edu_ai_search:8090/search`.

## Prototype limitations

- no login (access is restricted by VPN),
- history only in browser memory,
- no response streaming,
- no persistent thread storage,
- no Assistant Cloud.
