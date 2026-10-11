"""Minimal ASGI app for the R2 lab: reports what an application finally SEES.

Run with the REAL uvicorn of the image under test, started the way the production images start it
(`uvicorn <app> --host 0.0.0.0 --port <port>`, no proxy flags). Uvicorn's own ProxyHeadersMiddleware
has already rewritten scope["client"] / scope["scheme"] (or not) before this callable runs, so the
answer shows what a handler's request.client / request.url.scheme would be. Not one of the real services.
"""
import json
import os

try:
    import uvicorn
    VERSION = uvicorn.__version__
except Exception as e:  # noqa: BLE001 - reported, the lab treats a missing version as an error
    VERSION = f"unknown:{type(e).__name__}"


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    while True:
        msg = await receive()
        if not msg.get("more_body"):
            break
    client = scope.get("client")
    body = json.dumps({
        "uvicorn": VERSION,
        "forwarded_allow_ips_env": os.environ.get("FORWARDED_ALLOW_IPS"),
        "client": list(client) if client else None,
        "scheme": scope.get("scheme"),
        "path": scope.get("path"),
        "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in scope.get("headers", [])],
    }).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
