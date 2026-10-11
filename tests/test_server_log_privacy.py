#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-28).
"""The question text stays out of the container log, and log_search still gets it.

MOCK test: the cascade, the scope helpers and log_search are replaced, so this
proves what server.py HANDS to log_search — not that a row reaches
question_answer_log (that is tests/test_domain_scope_api.py, on the dev stack).

    python tests/test_server_log_privacy.py              # host, no stack

In the search image — tests/ is NOT in it (search/Dockerfile copies only app/ and
eduai_normalize/), so copy the file in first; needs a running edu_ai_search:

    docker cp tests/test_server_log_privacy.py edu_ai_search:/tmp/
    docker exec -e PYTHONPATH=/app -w /app edu_ai_search python /tmp/test_server_log_privacy.py

A freshly built image, before it replaces the container — bind-mount the file,
no network, no env file (the mocks need neither):

    docker run --rm --network none -w /app -e PYTHONPATH=/app \
      -v "$PWD/tests/test_server_log_privacy.py:/tmp/t.py:ro" \
      --entrypoint python <image> /tmp/t.py

On Windows Git Bash prefix these with MSYS_NO_PATHCONV=1 (it rewrites /tmp/...).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEARCH = os.path.join(os.path.dirname(_HERE), "search")
for _p in (_SEARCH, os.path.join(_SEARCH, "eduai_normalize"), "/app"):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
# DB/LLM drivers are only stubbed where absent (host); the image has the real ones
for _mod in ("psycopg2", "psycopg2.extras", "psycopg2.pool", "langchain_core",
             "langchain_core.messages", "langchain_openai"):
    try:
        _missing = importlib.util.find_spec(_mod) is None
    except (ImportError, ValueError):
        _missing = True
    if _missing:
        sys.modules[_mod] = mock.MagicMock(name=_mod)

from fastapi import BackgroundTasks, Response  # noqa: E402
from app import server  # noqa: E402

QUESTION = "Kdy má fiktivní třída 9.Z výlet na Sněžku? zkušební-7f3a"
FAKE_UID = "test-user-0000-fiktivni"
_failures: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        _failures.append(name)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _patches(db_log, uid):
    result = {"outcome": "answer", "answer": "Výlet je v červnu.",
              "answer_source": "qa", "sources_json": "[]", "user_language": "cs"}
    prep = {"initial": {"question": QUESTION}, "_uid": uid, "_role": "STUDENT",
            "_role_err": None, "_domain_codes": []}
    return [mock.patch.object(server, "_prepare_search", return_value=prep),
            mock.patch.object(server, "run_search", return_value=(dict(result), 7)),
            mock.patch.object(server, "_apply_result_document_cap", lambda r: r),
            mock.patch.object(server, "_public_suggestions", return_value=[]),
            mock.patch.object(server, "_effective_scope", return_value=None),
            mock.patch.object(server, "log_search", db_log)]


def _run(endpoint: str, uid: str | None):
    cap, db_log, root = _Capture(), mock.MagicMock(name="log_search"), logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        ps = _patches(db_log, uid)
        for p in ps:
            p.start()
        try:
            req = server.SearchRequest(question=QUESTION, legal_entity="SKOLA_TEST",
                                       user_role="STUDENT", user_id=uid)
            if endpoint == "ask":
                bt = BackgroundTasks()
                server.search(req, bt, Response(), idempotency_key=None)
                for t in bt.tasks:         # what FastAPI runs after the response
                    t.func(*t.args, **t.kwargs)
            else:
                async def drain(it):
                    return [chunk async for chunk in it]
                asyncio.run(drain(server.search_stream(req).body_iterator))
        finally:
            for p in reversed(ps):
                p.stop()
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    return cap.lines, db_log


def main() -> int:
    for endpoint, uid in (("ask", None), ("stream", None),
                          ("ask", FAKE_UID), ("stream", FAKE_UID)):
        print(f"[{endpoint}, {'anonymous' if uid is None else 'user_id'}]")
        lines, db_log = _run(endpoint, uid)
        leaked = [l for l in lines if "Sněžku" in l or "zkušební-7f3a" in l]
        check("no question text in any log record", not leaked, repr(leaked))
        check("request is still logged with identifiers",
              any("request (entity=SKOLA_TEST, role=STUDENT, chars=" in l for l in lines),
              repr(lines))
        check("log_search called once", db_log.call_count == 1, str(db_log.call_count))
        if db_log.call_count == 1:
            req, resp, duration_ms, setting_id = db_log.call_args.args
            check("log_search gets the FULL question", req.question == QUESTION)
            check("log_search gets entity and role",
                  (req.legal_entity, req.user_role) == ("SKOLA_TEST", "STUDENT"))
            if uid is None:
                check("anonymous caller stays anonymous", req.user_id is None,
                      repr(req.user_id))
            else:
                check("log_search gets the user_id unchanged", req.user_id == FAKE_UID,
                      repr(req.user_id))
            check("log_search gets the answer and setting",
                  resp.answer == "Výlet je v červnu." and setting_id == 7)
    print(f"\n{'FAIL' if _failures else 'ok'} — {len(_failures)} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
