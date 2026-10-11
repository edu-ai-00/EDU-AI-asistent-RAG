#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-08-12).
"""Verify a running edu_ai stack actually works — not just that it started.

    python3 scripts/smoke.py

Costs nothing to run: no OpenAI call, no LLM token, no write to the application
tables. Cheap enough for cron, so the same check that gates a deployment can
keep watching it afterwards.

Checks, in order of how early they catch a bad deploy:

  1. container health   — every service reports healthy to Docker
  2. service /health    — each app answers, and reports no missing_config
  3. flow aliases       — all 6 flows carry the endpoint_name callers address
  4. database           — the edu_ai schema exists and is queryable
  5. dry-run ingest     — the upload path works end to end, short of Langflow
  6. variable resolution — a flow really reaches Postgres: global variables are
                          the two expected Credentials, and one flow run proves
                          the whole chain (Credential → SecretStr → psycopg2,
                          plus the os.getenv fallback on five free-text fields)

Checks that cannot run (a port not published, no API key) are reported as
[skip], not silently dropped: a green run must never mean "nothing was tested".

Exit code 0 = everything that ran passed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import eduai_layout  # noqa: E402
from envfile import is_placeholder, read_env  # noqa: E402
from http_safe import open_url  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
# The maintenance superuser of the role contract; smoke reads through it on the container's local socket.
MAINTENANCE_USER = "edu_ai_admin"
_layout: "eduai_layout.Layout | None" = None   # set by main() only when a layout flag or variable was given

EXPECTED_ALIASES = {
    "edu-ai-load-markdown",
    "edu-ai-load-pdf",
    "edu-ai-load-office",
    "edu-ai-load-web",
    "edu-ai-load-qa",
    "edu-ai-export-qa-pack",
}

_passed = 0
_failed = 0
_skipped = 0


def ok(message: str) -> None:
    global _passed
    _passed += 1
    print(f"  [pass] {message}")


def bad(message: str) -> None:
    global _failed
    _failed += 1
    print(f"  [FAIL] {message}")


def skip(message: str) -> None:
    global _skipped
    _skipped += 1
    print(f"  [skip] {message}")


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    if _layout is not None and args[:2] == ["docker", "compose"]:
        args = _layout.compose_prefix() + args[2:]
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def http_json(url: str, *, headers: dict[str, str] | None = None,
              data: bytes | None = None, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with open_url(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw) if raw else {}


# ───────────────────────────────────────────────────────────────
#  1. Container health
# ───────────────────────────────────────────────────────────────

def check_containers() -> None:
    print("\n1. container health")
    try:
        selected = run(["docker", "compose", "config", "--services"])
    except (OSError, subprocess.TimeoutExpired):
        bad("could not determine enabled Compose services")
        return
    expected = set(selected.stdout.split())
    if selected.returncode != 0 or not expected:
        bad("could not determine enabled Compose services")
        return
    if "cloudflared" not in expected:
        skip("Cloudflare Tunnel disabled (Compose profile)")
    result = run(["docker", "compose", "ps", "--all", "--format", "json"])
    if result.returncode != 0:
        bad(f"docker compose ps failed: {result.stderr.strip()[:200]}")
        return

    # Compose emits either one JSON array or newline-delimited objects,
    # depending on version.
    text = result.stdout.strip()
    if not text:
        bad("no containers are running — is the stack up?")
        return
    try:
        parsed = json.loads(text)
        services = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        services = [json.loads(line) for line in text.splitlines() if line.strip()]

    present = {service.get("Service") or service.get("Name", "?") for service in services}
    for name in sorted(expected - present):
        bad(f"{name}: enabled service has no container")
    for service in services:
        name = service.get("Service") or service.get("Name", "?")
        state = service.get("State", "?")
        health = (service.get("Health") or "").strip()
        if state != "running":
            bad(f"{name}: {state}")
        elif health in ("", "healthy"):
            ok(f"{name}: running{f' ({health})' if health else ''}")
        else:
            bad(f"{name}: running but {health}")


# ───────────────────────────────────────────────────────────────
#  2. Service health endpoints
# ───────────────────────────────────────────────────────────────

def _http_json_in(service: str, url: str, timeout: int = 10) -> dict:
    """GET inside the service's own container, like check_database does with psql.

    Backend ports are published only by docker-compose.override.yml, so on a
    server (which runs without it) every backend check used
    to skip — and a skip reads as "fine" while verifying nothing. From inside the
    container the port is always there, in both environments.
    """
    code = (f"import json,urllib.request;"
            f"print(urllib.request.urlopen('{url}',timeout={timeout}).read().decode())")
    result = run(["docker", "compose", "exec", "-T", service, "python", "-c", code])
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip()[:200])
    return json.loads(result.stdout.strip().splitlines()[-1])


def check_health_endpoints(langflow_base: str | None) -> None:
    print("\n2. service /health")

    # (compose service, url as seen from INSIDE that container)
    endpoints = [
        ("edu_ai_api", "http://127.0.0.1:8080/health"),
        ("edu_ai_search", "http://127.0.0.1:8090/health"),
        ("edu_ai_web_chat", "http://127.0.0.1:8081/chat/health"),
    ]

    if langflow_base:
        try:
            payload = http_json(f"{langflow_base}/health", timeout=10)
            ok(f"edu_ai (langflow): {payload.get('status', 'ok')}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            skip(f"edu_ai (langflow): not reachable at {langflow_base} ({exc})")

    for service, url in endpoints:
        try:
            payload = _http_json_in(service, url)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            bad(f"{service}: /health failed inside the container ({exc})")
            continue

        missing = payload.get("missing_config") or []
        if missing:
            bad(f"{service}: {payload.get('status')} — missing_config: {missing}")
        else:
            ok(f"{service}: {payload.get('status', 'ok')}")


# ───────────────────────────────────────────────────────────────
#  3. Flow aliases
# ───────────────────────────────────────────────────────────────

def check_flows(langflow_base: str | None, api_key: str) -> None:
    print("\n3. flow endpoint aliases")

    if not langflow_base:
        skip("Langflow not reachable")
        return
    if is_placeholder(api_key):
        skip("LANGFLOW_API_KEY is not set")
        return

    try:
        payload = http_json(f"{langflow_base}/api/v1/flows/?page=1&size=100",
                            headers={"x-api-key": api_key})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        bad(f"cannot list flows: {exc}")
        return

    flows = payload if isinstance(payload, list) else payload.get("items", [])
    found = {f.get("endpoint_name") for f in flows if f.get("endpoint_name")}

    for alias in sorted(EXPECTED_ALIASES):
        if alias in found:
            ok(f"{alias}")
        else:
            bad(f"{alias} — missing (run scripts/import_flows.py)")

    duplicates = [
        alias for alias in EXPECTED_ALIASES
        if sum(1 for f in flows if f.get("endpoint_name") == alias) > 1
    ]
    if duplicates:
        bad(f"duplicate endpoint_name: {duplicates}")


# ───────────────────────────────────────────────────────────────
#  4. Database
# ───────────────────────────────────────────────────────────────

def check_database(env: dict[str, str]) -> None:
    print("\n4. database")
    user = MAINTENANCE_USER
    database = env.get("POSTGRES_DB", "edu_ai")

    # Via `compose exec`, so this works whether or not 5432 is published.
    result = run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", user, "-d", database, "-tAc",
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='edu_ai';",
    ])
    if result.returncode != 0:
        bad(f"psql failed: {(result.stderr or result.stdout).strip()[:200]}")
        return

    try:
        tables = int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        bad(f"unexpected psql output: {result.stdout.strip()[:120]}")
        return

    if tables > 0:
        ok(f"schema edu_ai present ({tables} tables)")
    else:
        bad("schema edu_ai has no tables — did postgres/init/*.sql run?")

    result = run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", user, "-d", database, "-tAc",
        "SELECT count(*) FROM edu_ai.document_chunks;",
    ])
    if result.returncode == 0:
        ok(f"document_chunks queryable ({result.stdout.strip().splitlines()[-1]} rows)")
    else:
        bad("document_chunks is not queryable")


def check_db_roles(env: dict[str, str]) -> None:
    """The live catalog must match postgres/role_contract/matrix.json (roles, owners, grants, PUBLIC, default ACLs)."""
    print("\n4a. database role contract")
    if env.get("POSTGRES_DB_USER") != "edu_ai_ingest":
        bad("POSTGRES_DB_USER in .env is not edu_ai_ingest: the flows would connect as another role")
    cid = run(["docker", "compose", "ps", "-q", "postgres"]).stdout.strip().splitlines()[:1]
    if not cid:
        bad("postgres container not found; the role contract cannot be verified")
        return
    result = run([sys.executable, str(ROOT / "scripts" / "db_roles.py"), "verify", "--container", cid[0]], timeout=180)
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    if result.returncode == 0:
        ok("role contract: the live catalog matches the matrix")
        return
    bad("role contract " + ("deviates" if result.returncode == 1 else "could not be verified") + ": " + "; ".join(lines[:4])[:400])


CHAT_ENTITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CHAT_BASE = "http://127.0.0.1:8081"        # as seen from INSIDE edu_ai_web_chat


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _http_post_in(service: str, url: str, body: dict, timeout: int = 10) -> tuple[int, dict]:
    """POST JSON inside the service's container; (status, parsed body) for ANY HTTP status."""
    code = "\n".join([
        "import json,urllib.request,urllib.error",
        f"r=urllib.request.Request('{url}',data=json.dumps({body!r}).encode(),"
        "headers={'content-type':'application/json'})",
        "try:",
        f" x=urllib.request.urlopen(r,timeout={timeout});s=x.status;b=x.read()",
        "except urllib.error.HTTPError as e:",
        " s=e.code;b=e.read()",
        "print(json.dumps({'status':s,'body':json.loads(b or b'null')}))",
    ])
    result = run(["docker", "compose", "exec", "-T", service, "python", "-c", code])
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip()[:200])
    out = json.loads(result.stdout.strip().splitlines()[-1])
    return out["status"], out["body"] or {}


def _entity_lookup(env: dict[str, str], value: str) -> bool:
    """Report whether a fixed/default school code is an active legal entity; True when it is."""
    user = MAINTENANCE_USER
    database = env.get("POSTGRES_DB", "edu_ai")

    def query(sql: str):
        return run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", user, "-d", database, "-tAc", sql])

    # the value passed the whitelist by the caller, so it can be quoted safely
    found = query(f"SELECT 1 FROM edu_ai.legal_entity WHERE legal_entity_code = '{value}' AND is_active;")
    if found.returncode != 0:
        bad(f"could not look up CHAT_LEGAL_ENTITY: {(found.stderr or found.stdout).strip()[:160]}")
        return False
    if found.stdout.strip().splitlines()[-1:] == ["1"]:
        ok(f"chat entity {value} is an active legal entity")
        return True
    known = query("SELECT string_agg(legal_entity_code, ', ' ORDER BY legal_entity_code) FROM "
                  "(SELECT legal_entity_code FROM edu_ai.legal_entity WHERE is_active ORDER BY 1 LIMIT 12) t;")
    bad(f"CHAT_LEGAL_ENTITY={value} is not an active legal entity of this database"
        + (f" (active, first 12: {known.stdout.strip()[:300]})" if known.returncode == 0 and known.stdout.strip() else ""))
    return False


def _check_chat_picker(env: dict[str, str], value: str, unset: bool) -> None:
    """Browser selection on: the chat is usable once the school list loads; the choice itself is human."""
    if not unset:
        # an explicit default must still be a real, active school
        if not CHAT_ENTITY_RE.match(value):
            bad("CHAT_LEGAL_ENTITY is not a legal entity code (letters, digits, '_' and '-' only, up to 64)")
            return
        if not _entity_lookup(env, value):
            return
    try:
        cfg = _http_json_in("edu_ai_web_chat", f"{CHAT_BASE}/chat/api/config")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        bad(f"chat /chat/api/config failed inside the container ({exc})")
        return
    if not cfg.get("entity_picker"):
        bad("CHAT_ENTITY_PICKER is on in .env but the running chat has browser selection off: "
            "recreate edu_ai_web_chat")
        return
    setup = cfg.get("setup")
    entities = cfg.get("entities") or []
    if setup == "entities_unavailable":
        bad("the chat could not load the list of active schools (search service or database error)")
        return
    if setup == "no_entities" or not entities:
        bad("the chat's school list is empty: the database has no active legal entity")
        return
    if not unset:
        if setup == "ready" and cfg.get("default") == value:
            ok(f"chat default school {value} is offered; the browser may still choose another "
               f"({len(entities)} active)")
        else:
            bad(f"the running chat does not accept CHAT_LEGAL_ENTITY={value} as a default "
                f"(setup={setup}): recreate edu_ai_web_chat")
        return

    if setup != "awaiting_selection" or cfg.get("default"):
        bad(f"CHAT_LEGAL_ENTITY is unset but the running chat reports setup={setup}, "
            f"default={cfg.get('default')!r}: recreate edu_ai_web_chat")
        return
    ok(f"CHAT_AWAITS_SCHOOL_SELECTION: {len(entities)} active school(s) listed, no default school; "
       "the visitor chooses in the browser")
    # the server, not the dropdown, must refuse a question that names no school
    try:
        status, body = _http_post_in("edu_ai_web_chat", f"{CHAT_BASE}/chat/api/message",
                                     {"message": "smoke"})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        bad(f"chat /chat/api/message probe failed inside the container ({exc})")
        return
    code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
    if status == 422 and code == "school_required":
        ok("a question without a school is refused before any search (school_required)")
    else:
        bad(f"a question without a school was not refused as school_required (HTTP {status}, code={code})")
    skip("browser search before the school is chosen: needs a person; try it in the chat after install")


def check_chat_entity(env: dict[str, str]) -> None:
    """The chat has one school fixed by .env, or lets the visitor choose; each must be a real, active one."""
    print("\n4b. chat entity")
    value = (env.get("CHAT_LEGAL_ENTITY") or "").strip()
    unset = not value or is_placeholder(value)
    if _truthy(env.get("CHAT_ENTITY_PICKER")):
        _check_chat_picker(env, value, unset)
        return
    if unset:
        bad("CHAT_LEGAL_ENTITY is not set and CHAT_ENTITY_PICKER is off: the chat is not configured "
            "(it has no school to search); enable browser selection or name one legal entity, "
            "then recreate edu_ai_web_chat")
        return
    if not CHAT_ENTITY_RE.match(value):
        bad("CHAT_LEGAL_ENTITY is not a legal entity code (letters, digits, '_' and '-' only, up to 64)")
        return
    _entity_lookup(env, value)


# ───────────────────────────────────────────────────────────────
#  5. Dry-run ingest
# ───────────────────────────────────────────────────────────────

def _multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body using only the standard library."""
    boundary = f"----edu-ai-smoke-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n".encode()
        + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _a_real_principal(env: dict[str, str]) -> tuple[str, str] | None:
    """Any active (entity, user) pair the register knows, for the dry run to use.

    A made-up pair no longer reaches the upload path at all: /v1/ingest calls
    _validate_principal FIRST and answers 400 for an unknown entity or a user who
    does not belong to it. The old hardcoded ("SMOKE", "smoke") therefore stopped
    testing what it was written to test, and would fail rather than skip.
    """
    result = run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", MAINTENANCE_USER,
        "-d", env.get("POSTGRES_DB", "edu_ai"), "-tAc",
        "SELECT s.legal_entity_code || ' ' || s.user_id FROM edu_ai.staff_user s "
        "JOIN edu_ai.legal_entity e ON e.legal_entity_code = s.legal_entity_code "
        "WHERE s.is_active AND e.is_active LIMIT 1;",
    ])
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    parts = line.split()
    return (parts[0], parts[1]) if len(parts) == 2 else None


def check_dry_run_ingest(env: dict[str, str]) -> None:
    print("\n5. dry-run ingest")

    principal = _a_real_principal(env)
    if principal is None:
        # NOT a pass: with no users the ingest path cannot be exercised at all,
        # and on a freshly migrated database that is the state to notice.
        skip("no active staff_user in an active entity — nothing may ingest yet")
        return
    entity, user = principal

    # Built and posted INSIDE the gateway container: 8080 is not published on a
    # server, and through nginx /api/ would need the Basic-auth password.
    # audience_roles is required by the gateway for entity/organization visibility
    # (init 127) — without it this check fails 400 and reads like a broken deploy.
    # A list value becomes one part PER element, which is how FastAPI fills a
    # `list[str] = Form(...)`; one comma-joined part arrives as a single bogus role.
    code = (
        "import json,urllib.request,urllib.error,uuid\n"
        "b='----edu-ai-smoke-'+uuid.uuid4().hex\n"
        f"f={{'legal_entity':{entity!r},'user_id':{user!r},"
        "'visibility':'entity','dry_run':'true',"
        "'audience_roles':['STUDENT','TEACHER','PARENT']}\n"
        "p=b''\n"
        "for k,v in f.items():\n"
        "    for one in (v if isinstance(v,list) else [v]):\n"
        "        p+=('--'+b+'\\r\\nContent-Disposition: form-data; name=\"'+k+'\"'"
        "+'\\r\\n\\r\\n'+one+'\\r\\n').encode()\n"
        "p+=('--'+b+'\\r\\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"edu-ai-smoke.txt\"\\r\\nContent-Type: text/plain\\r\\n\\r\\n').encode()\n"
        "p+=b'edu_ai smoke test - dry run, nothing is ingested.\\r\\n'\n"
        "p+=('--'+b+'--\\r\\n').encode()\n"
        "r=urllib.request.Request('http://127.0.0.1:8080/v1/ingest',data=p,"
        "headers={'Content-Type':'multipart/form-data; boundary='+b})\n"
        "try:\n"
        "    print(urllib.request.urlopen(r,timeout=60).read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(json.dumps({'_http':e.code,'_body':e.read().decode()[:200]}))\n"
    )
    result = run(["docker", "compose", "exec", "-T", "edu_ai_api", "python", "-c", code])
    if result.returncode != 0:
        bad(f"could not run the dry run inside edu_ai_api: "
            f"{(result.stderr or result.stdout).strip()[:200]}")
        return

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError, json.JSONDecodeError):
        bad(f"/v1/ingest gave an unparseable response: {result.stdout.strip()[:160]}")
        return

    if "_http" in payload:
        bad(f"/v1/ingest returned HTTP {payload['_http']}: {payload['_body']}")
    elif payload:
        ok(f"upload path works (dry run as {entity}) — {json.dumps(payload)[:120]}")
    else:
        bad("/v1/ingest returned an empty response")


# ───────────────────────────────────────────────────────────────
#  6. Variable resolution
# ───────────────────────────────────────────────────────────────

#: Required platform-managed Global Variables and their types. User-created
#: variables may coexist. A managed non-secret connection name carrying type
#: Credential makes the StrInput bound to it reject its value (see .env).
EXPECTED_VARIABLES = {"OPENAI_API_KEY": "Credential", "POSTGRES_PASSWORD": "Credential"}

#: Created as Generic by bootstrap.py step 7b. Optional: without them the flows
#: fall back to os.getenv, which is how a hand-built stack resolves them. What
#: must never happen is any of these carrying type Credential.
OPTIONAL_GENERIC_VARIABLES = ("POSTGRES_DB_SERVER", "POSTGRES_SERVER_PORT",
                              "POSTGRES_DB", "POSTGRES_DB_SCHEMA", "POSTGRES_DB_USER")

#: A flow that carries all six variables in ONE component (pg_host, pg_port,
#: pg_user, pg_password, pg_database, pg_schema), so a single run exercises both
#: resolution paths. Called with a pack_id that cannot exist: the component then
#: raises before it creates any file, which makes this check non-mutating —
#: nothing is written to the DB or to sqllite_export/.
PROBE_FLOW = "edu-ai-export-qa-pack"
PROBE_NODE = "ExportQAPack-qa010"
PROBE_PACK_ID = "__edu_ai_smoke_probe__"
#: Reaching this error means connect() + SELECT both succeeded — which is the
#: whole point. It is the component's own message, so it is also the thing most
#: likely to go stale if the flow is refactored; see the failure hint below.
PROBE_EXPECTED = f"no manifest for pack_id='{PROBE_PACK_ID}'"


def check_global_variables() -> None:
    result = run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", MAINTENANCE_USER, "-d", "langflow", "-tAc",
        "SELECT name || '=' || type FROM variable ORDER BY name;",
    ])
    if result.returncode != 0:
        # Falls back to the role that owns the langflow database.
        result = run([
            "docker", "compose", "exec", "-T", "postgres",
            "psql", "-U", "edu_ai_langflow",
            "-d", "langflow", "-tAc",
            "SELECT name || '=' || type FROM variable ORDER BY name;",
        ])
    if result.returncode != 0:
        skip(f"cannot read Langflow's variable table ({(result.stderr or '').strip()[:120]})")
        return

    found = dict(
        line.split("=", 1)
        for line in (l.strip() for l in result.stdout.splitlines())
        if "=" in line
    )
    managed = {**EXPECTED_VARIABLES,
               **{n: "Generic" for n in OPTIONAL_GENERIC_VARIABLES}}
    user_variable_count = sum(n not in managed for n in found)
    wrong_type = {n: t for n, t in found.items()
                  if n in managed and t != managed[n]}
    missing = [n for n in EXPECTED_VARIABLES if n not in found]

    if wrong_type:
        bad(f"Global Variables have the wrong type: {wrong_type} — a Credential "
            f"where Generic is expected makes every StrInput bound to that name "
            f"reject its value (see the rules in .env)")
    elif missing:
        bad(f"Global Variables missing: {missing} — env-sourcing did not run; "
            f"check LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT and restart edu_ai")
    else:
        ok("Global Variables have the expected types: "
           + ", ".join(f"{n}={found[n]}" for n in sorted(found) if n in managed)
           + f"; user variables preserved: {user_variable_count}")


def probe_request_body() -> dict:
    """Fresh synthetic scope; this deliberately nonexistent pack creates no file."""
    return {"output_type": "any", "output_component": "ChatOutput-qa011",
            "tweaks": {PROBE_NODE: {"pack_id": PROBE_PACK_ID,
                                     "legal_entity": "SMOKE_" + uuid.uuid4().hex}}}


def check_connectivity_probe(langflow_base: str | None, api_key: str) -> None:
    if not langflow_base:
        skip("Langflow not reachable — cannot run the connectivity probe")
        return
    if not api_key or api_key.startswith("CHANGE_ME"):
        skip("LANGFLOW_API_KEY is not set — cannot run the connectivity probe")
        return

    body = json.dumps(probe_request_body()).encode()

    try:
        payload = http_json(
            f"{langflow_base}/api/v1/run/{PROBE_FLOW}?stream=false",
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            data=body, timeout=120,
        )
    except urllib.error.HTTPError as exc:
        bad(f"probe flow {PROBE_FLOW} returned HTTP {exc.code}: "
            f"{exc.read().decode(errors='replace')[:300]}")
        return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        skip(f"probe flow not reachable ({exc})")
        return

    text = json.dumps(payload, ensure_ascii=False)

    if PROBE_EXPECTED in text:
        ok("flow reached Postgres — Credential password and all five env-resolved "
           "connectivity fields work")
        return

    # Name the two failures this check exists for, so the message is actionable
    # rather than just "unexpected output".
    if "Credential-typed global variable" in text:
        bad("a non-secret field is bound to a Credential variable — check "
            "LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT against the rules in .env")
    elif "variable not found" in text:
        bad("a load_from_db variable resolved to nothing — the os.getenv fallback "
            "is off (LANGFLOW_FALLBACK_TO_ENV_VAR) or the key is missing from .env")
    elif "no password supplied" in text or "connection to server" in text:
        # The signature of a field that was never resolved at all: should_skip_field
        # drops any show=False field unless override_skip=True, and the component
        # then falls back to its own default — empty, for pg_password. Hiding a
        # field through the Langflow UI produces exactly this, because the UI
        # cannot set override_skip.
        bad("a connectivity field was not resolved at all — a hidden field "
            "(show=False) without override_skip=True is skipped outright. Was a "
            "field hidden in the Langflow UI? Restore with scripts/import_flows.py")
    else:
        bad(f"probe flow did not report {PROBE_EXPECTED!r}. Either connectivity is "
            f"broken, or this probe is stale (flow/node renamed — see PROBE_* in "
            f"this file). Got: {text[:300]}")


def check_variable_resolution(langflow_base: str | None, api_key: str) -> None:
    print("\n6. variable resolution")
    check_global_variables()
    check_connectivity_probe(langflow_base, api_key)


# ───────────────────────────────────────────────────────────────

def resolve_langflow_url(override: str | None) -> str | None:
    """Same order as bootstrap.py: flag, .env, published port, dev default."""
    candidates = [override.rstrip("/")] if override else []
    if not override:
        from_env = read_env(ENV_PATH).get("LANGFLOW_URL", "").strip()
        if from_env:
            candidates.append(from_env.rstrip("/"))
        published = run(["docker", "compose", "port", "edu_ai", "7860"]).stdout.strip()
        if published:
            candidates.append(f"http://127.0.0.1:{published.rsplit(':', 1)[-1]}")
        candidates.append("http://127.0.0.1:7860")

    for candidate in candidates:
        try:
            with urllib.request.urlopen(f"{candidate}/health", timeout=5) as response:
                if response.status == 200:
                    return candidate
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--langflow-url", default=None)
    eduai_layout.add_arguments(parser)
    args = parser.parse_args()

    global ROOT, ENV_PATH, _layout
    try:
        chosen = eduai_layout.resolve(args, os.environ, ROOT)
    except eduai_layout.LayoutError as exc:
        bad(f"invalid layout ({exc.code}: {exc.field})")
        return 1
    if any(origin != "default" for origin in chosen.origin.values()):
        findings = eduai_layout.validate(chosen, links="require")
        for finding in findings:
            bad(f"layout: {finding.code} ({finding.level}) {finding.detail}")
        if findings:
            return 1
        ROOT, ENV_PATH, _layout = chosen.source_root, chosen.env_path, chosen

    env = read_env(ENV_PATH)
    langflow_base = resolve_langflow_url(args.langflow_url)

    print("edu_ai smoke test")

    check_containers()
    check_health_endpoints(langflow_base)
    check_flows(langflow_base, env.get("LANGFLOW_API_KEY", ""))
    check_database(env)
    check_db_roles(env)
    check_chat_entity(env)
    check_dry_run_ingest(env)
    check_variable_resolution(langflow_base, env.get("LANGFLOW_API_KEY", ""))

    print(f"\n{_passed} passed, {_failed} failed, {_skipped} skipped")
    if _skipped:
        print("Skipped checks were not run — they are not passes.")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
