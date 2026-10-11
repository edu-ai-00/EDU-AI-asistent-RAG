"""Request set for the nginx S2 equivalence check.

The search allow-list is read from the `map $uri $search_path_allowed` block of the
nginx configuration under test, and the paths that must be refused are derived from
contracts/openapi.yaml (every contract path that is not allowed), so the set is
not a hand-kept copy that can drift.
"""
from __future__ import annotations

import re

HOST = "edu.example"
BASE_HEADERS = [("Host", HOST), ("User-Agent", "s2-equivalence"), ("Accept", "*/*")]
JSON = ("Content-Type", "application/json")
SPOOF = [("X-Forwarded-For", "198.51.100.7, 198.51.100.8"), ("X-Real-IP", "198.51.100.9"),
         ("X-Forwarded-Proto", "https"), ("X-Forwarded-Port", "444"), ("Forwarded", "for=198.51.100.10"),
         ("CF-Connecting-IP", "203.0.113.9")]
PGADMIN_SPOOF = [("X-Forwarded-Port", "444"), ("X-Forwarded-Host", "evil.example"),
                 ("X-Forwarded-Prefix", "/evil"), ("X-Script-Name", "/evil"), ("X-Forwarded-Proto", "https")]
MiB = 1024 * 1024


def parse_allowed(conf_text):
    m = re.search(r"map \$uri \$search_path_allowed \{(.*?)\n\s*\}", conf_text, re.S)
    if not m:
        raise ValueError("search allow-list map not found")
    allowed = []
    for line in m.group(1).splitlines():
        mm = re.match(r"\s*(/\S+)\s+1\s*;", line.split("#")[0])
        if mm:
            allowed.append(mm.group(1))
    return allowed


def contract_paths(openapi_text):
    out, in_paths = [], False
    for line in openapi_text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if in_paths:
            if line and not line.startswith(" "):
                break
            m = re.match(r"^  (/[^\s:]*):\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def denied_paths(allowed, contract):
    """Every contract path that is not on the allow-list, plus near misses."""
    denied = ["/search" + p for p in contract if "/search" + p not in allowed]
    extra = ["/search", "/search/", "/search/v1/", "/search/v1/ask-anything", "/search/v1/ask/",
             "/search/v1/ask/stream/x", "/search/v1/entities/", "/search/V1/entities", "/search/docs",
             "/search/openapi.json", "/search/metrics"]
    return sorted(set(denied + extra))


def build(allowed, contract):
    cases = []

    def add(cid, port, method, path, *, headers=(), body=None, client="172", cat):
        h = list(BASE_HEADERS) + list(headers)
        if body is not None and not any(k.lower() == "content-type" for k, _ in h):
            h.append(JSON)
        cases.append({"id": cid, "port": port, "method": method, "path": path, "headers": h,
                      "body": body, "client": client, "cat": cat})

    small = {"fill": "a", "len": 3}
    deny = denied_paths(allowed, contract)

    # ── :80 ────────────────────────────────────────────────
    for m in ("GET", "HEAD", "POST"):
        add(f"80 {m} /health", 80, m, "/health", cat="health")
    add("80 GET /health/", 80, "GET", "/health/", cat="health")
    add("80 GET /HEALTH", 80, "GET", "/HEALTH", cat="health")
    for path in ("/api/", "/api/ping", "/api/ping?x=1&y=%20z", "/api", "/apix", "/api/%"):
        add(f"80 GET {path}", 80, "GET", path, cat="api-prefix")
    for m in ("PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"):
        add(f"80 {m} /api/x", 80, m, "/api/x", cat="api-methods")
    add("80 POST /api/v1/ingest", 80, "POST", "/api/v1/ingest", body=small, cat="api-body")
    add("80 POST /api 50MiB+1 declared", 80, "POST", "/api/v1/ingest",
        body={"fill": "a", "len": 0, "declare_len": 50 * MiB + 1}, cat="body-limit")
    for p in allowed:
        add(f"80 GET {p}", 80, "GET", p, cat="search-allowed")
        add(f"80 GET {p}?q", 80, "GET", p + "?q=1&r=%20s", cat="search-allowed-query")
        add(f"80 POST {p}", 80, "POST", p + ("?n=1&delay=0.01" if p.endswith("/stream") else ""),
            body={"fill": "b", "len": 7}, cat="search-allowed")
    add("80 GET /search/v1/%65ntities (encoded, allowed)", 80, "GET", "/search/v1/%65ntities", cat="search-edge")
    add("80 GET /search//v1/entities (merge slashes)", 80, "GET", "/search//v1/entities", cat="search-edge")
    add("80 GET /search/../api/ping (dot segments)", 80, "GET", "/search/../api/ping", cat="search-edge")
    add("80 GET /search/%zz (invalid escape)", 80, "GET", "/search/%zz", cat="search-edge")
    add("80 GET long URI", 80, "GET", "/search/health?" + "a" * 9000, cat="search-edge")
    for p in deny:
        add(f"80 GET {p} (denied)", 80, "GET", p, cat="search-denied")
    add("80 POST /search/v1/query-log (denied)", 80, "POST", "/search/v1/query-log", body=small, cat="search-denied")
    add("80 GET entities pad+gzip", 80, "GET", "/search/v1/entities?pad=3000",
        headers=[("Accept-Encoding", "gzip")], cat="gzip")
    add("80 GET entities pad no gzip", 80, "GET", "/search/v1/entities?pad=3000", cat="gzip")
    add("80 GET api pad+gzip (not gzip type?)", 80, "GET", "/api/ping?pad=3000",
        headers=[("Accept-Encoding", "gzip")], cat="gzip")
    add("80 POST /search 1MiB (limit, ok)", 80, "POST", "/search/v1/ask", body={"fill": "a", "len": MiB}, cat="body-limit")
    add("80 POST /search 1MiB+1 (413)", 80, "POST", "/search/v1/ask", body={"fill": "a", "len": MiB + 1}, cat="body-limit")
    add("80 POST /search 2MiB declared", 80, "POST", "/search/v1/ask",
        body={"fill": "a", "len": 0, "declare_len": 2 * MiB}, cat="body-limit")
    for code in (404, 500, 503):
        add(f"80 GET /search/v1/entities upstream {code}", 80, "GET", "/search/v1/entities",
            headers=[("X-Stub-Status", str(code))], cat="upstream-status")
    add("80 GET /api/ping upstream 503", 80, "GET", "/api/ping", headers=[("X-Stub-Status", "503")], cat="upstream-status")
    for path in ("/chat", "/chat/", "/chat/a/b?c=1", "/chat/x/stream?n=1&delay=0.01"):
        add(f"80 GET {path}", 80, "GET", path, cat="chat")
    add("80 POST /chat/x", 80, "POST", "/chat/x", body=small, cat="chat")
    for path in ("/qa", "/qa/", "/qa/assets/app.js?v=1", "/qa-api/v1/health"):
        add(f"80 GET {path}", 80, "GET", path, cat="qa")
    add("80 POST /qa-api/v1/schools", 80, "POST", "/qa-api/v1/schools", body=small, cat="qa")
    add("80 POST /qa-api 50MiB+1 declared", 80, "POST", "/qa-api/v1/schools",
        body={"fill": "a", "len": 0, "declare_len": 50 * MiB + 1}, cat="body-limit")
    for path in ("/", "/unknown", "/favicon.ico", "/robots.txt", "/search-x", "/chat-x", "/qa-x"):
        add(f"80 GET {path}", 80, "GET", path, cat="catch-all")
    for m in ("OPTIONS", "DELETE", "PUT", "PATCH", "HEAD"):
        add(f"80 {m} /search/health", 80, m, "/search/health", cat="methods")
    for client in ("172", "192"):
        for path in ("/api/ping", "/search/health", "/chat/x"):
            add(f"80 GET {path} spoof from {client}", 80, "GET", path, headers=SPOOF, client=client, cat="header-spoof")
    add("80 GET Host with port+case", 80, "GET", "/api/ping", headers=[], client="172", cat="host")
    cases[-1]["headers"] = [("Host", "EDU.Example:8080"), ("User-Agent", "s2-equivalence"), ("Accept", "*/*")]
    add("80 GET without Host (HTTP/1.1)", 80, "GET", "/api/ping", client="172", cat="host")
    cases[-1]["headers"] = [("User-Agent", "s2-equivalence"), ("Accept", "*/*")]

    # ── :81 tunnel listener ────────────────────────────────
    for p in allowed:
        add(f"81 GET {p}", 81, "GET", p, cat="tunnel-allowed")
        add(f"81 POST {p}", 81, "POST", p + ("?n=1&delay=0.01" if p.endswith("/stream") else ""),
            body={"fill": "b", "len": 7}, cat="tunnel-allowed")
    for p in deny:
        add(f"81 GET {p} (denied)", 81, "GET", p, cat="tunnel-denied")
    for path in ("/health", "/api/ping", "/api/", "/chat/", "/qa/", "/qa-api/v1/health", "/pgadmin/", "/"):
        add(f"81 GET {path} (not on the tunnel)", 81, "GET", path, cat="tunnel-denied")
    add("81 POST /search 1MiB+1 (413)", 81, "POST", "/search/v1/ask", body={"fill": "a", "len": MiB + 1}, cat="body-limit")
    for client in ("172", "192"):
        add(f"81 GET /search/health CF header from {client}", 81, "GET", "/search/health",
            headers=[("CF-Connecting-IP", "203.0.113.9")], client=client, cat="real-ip")
        add(f"81 GET /search/health spoof from {client}", 81, "GET", "/search/health", headers=SPOOF, client=client,
            cat="real-ip")
    add("81 GET /search/health no CF header", 81, "GET", "/search/health", cat="real-ip")

    # ── :8443 pgAdmin ─────────────────────────────────────
    for path in ("/pgadmin/", "/pgadmin/login?next=x", "/pgadmin", "/", "/search/health"):
        add(f"8443 GET {path}", 8443, "GET", path, cat="pgadmin")
    add("8443 POST /pgadmin/browser/x", 8443, "POST", "/pgadmin/browser/x", body=small, cat="pgadmin")
    for client in ("172", "192"):
        add(f"8443 GET /pgadmin/ spoofed forwarding headers from {client}", 8443, "GET", "/pgadmin/",
            headers=PGADMIN_SPOOF, client=client, cat="pgadmin-spoof")

    # ── :8444 Langflow UI ─────────────────────────────────
    for path in ("/", "/api/v1/version?x=1", "/health", "/pgadmin/"):
        add(f"8444 GET {path}", 8444, "GET", path, cat="langflow")
    add("8444 POST /api/v1/run/x", 8444, "POST", "/api/v1/run/x", body=small, cat="langflow")
    add("8444 PUT /x", 8444, "PUT", "/x", body=small, cat="langflow")
    add("8444 GET / with Upgrade headers (no real handshake)", 8444, "GET", "/",
        headers=[("Connection", "Upgrade"), ("Upgrade", "websocket")], cat="langflow")
    add("8444 POST 100MiB+1 declared", 8444, "POST", "/api/v1/run/x",
        body={"fill": "a", "len": 0, "declare_len": 100 * MiB + 1}, cat="body-limit")
    return cases
