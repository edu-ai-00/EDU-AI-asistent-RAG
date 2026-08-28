#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-07-27).
"""Import the edu_ai Langflow flows from langflow_flows/ into a Langflow
instance.

Each flow JSON carries a stable `endpoint_name` alias (see
langflow_flows/ingestion/*.json), so callers (edu_ai_api, and the flows'
own loopback self-call into the markdown sub-flow) address flows by that
alias, not by the UUID Langflow assigns on import. Flow IDs are therefore
opaque and never need to leave this script or be pasted into .env.

Re-running this script is idempotent: a flow whose endpoint_name already
exists on the instance is PATCHed in place instead of creating a duplicate.

Usage:
    python3 scripts/import_flows.py

Credentials come from .env, the same as every other script here — this is the
recovery command for a flow that was broken in the UI, so it must work on its
own. The environment still wins when set, which is how bootstrap.py hands over
the key it has just minted.

    LANGFLOW_API_KEY=sk-...  python3 scripts/import_flows.py   # override
    LANGFLOW_URL=http://host:7860  python3 scripts/import_flows.py

Requires only the Python standard library.
"""

import gzip
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from envfile import is_placeholder, read_env  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
ROOT = REPO / "langflow_flows"
_ENV = read_env(REPO / ".env")


def _setting(name: str, default: str) -> str:
    """Environment first, then .env, then the default.

    A CHANGE_ME placeholder counts as unset, so a half-configured .env falls
    through to the default rather than sending a nonsense value at the API.
    """
    for candidate in (os.getenv(name), _ENV.get(name)):
        if candidate and not is_placeholder(candidate):
            return candidate
    return default


BASE = _setting("LANGFLOW_URL", "http://127.0.0.1:7860").rstrip("/")
KEY = _setting("LANGFLOW_API_KEY", "")

FLOW_FILES = [
    "ingestion/EDU_AI_LOAD_MARKDOWN.json",
    "ingestion/EDU_AI_LOAD_PDF.json",
    "ingestion/EDU_AI_LOAD_OFFICE.json",
    "ingestion/EDU_AI_LOAD_WEB_PAGE.json",
    "ingestion/EDU_AI_QA.json",
    "ingestion/EDU_AI_EXPORT_QA_PACK.json",
]


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "x-api-key": KEY},
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR {method} {path}: HTTP {e.code} — {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: cannot reach {BASE} ({e.reason}). Is Langflow running?")


def list_flows() -> list[dict]:
    """GET /api/v1/flows/, following pagination if the API returns one."""
    flows: list[dict] = []
    page = 1
    while True:
        res = call("GET", f"/api/v1/flows/?page={page}&size=100")
        if isinstance(res, list):
            return res  # unpaginated — everything comes back at once
        items = res.get("items", [])
        flows.extend(items)
        if page >= res.get("pages", 1) or not items:
            return flows
        page += 1


def main() -> None:
    if not KEY:
        sys.exit(
            "No LANGFLOW_API_KEY. Expected it in .env (scripts/bootstrap.py puts "
            "it there), or pass it in the environment for a one-off run."
        )

    print(f"Importing {len(FLOW_FILES)} flows into {BASE}")
    existing = {
        f.get("endpoint_name"): f["id"]
        for f in list_flows()
        if f.get("endpoint_name")
    }

    for rel in FLOW_FILES:
        flow = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        alias = flow.get("endpoint_name")
        for k in ("id", "user_id", "folder_id", "updated_at"):
            flow.pop(k, None)

        if alias in existing:
            body = {"name": flow.get("name"), "data": flow.get("data"), "endpoint_name": alias}
            call("PATCH", f"/api/v1/flows/{existing[alias]}", body)
            print(f"  ~ {flow.get('name', rel):40s} endpoint_name={alias} (updated)")
        else:
            res = call("POST", "/api/v1/flows/", flow)
            print(f"  + {flow.get('name', rel):40s} endpoint_name={alias} -> {res['id']} (created)")

    # No reminder about Global Variables here: compose sets
    # LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT, and Langflow really does
    # create OPENAI_API_KEY and POSTGRES_PASSWORD from .env on startup —
    # verified on a fresh instance 2026-07-25. The old unconditional reminder
    # described a manual step that no longer exists.
    print("\nDone. Flows are addressed by endpoint_name (see docker-compose.yml),")
    print("no env vars to paste.")


if __name__ == "__main__":
    main()
