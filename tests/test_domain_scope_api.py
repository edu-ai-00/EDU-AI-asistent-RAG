#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""Domain scope over the REAL HTTP surface — all three paths a client can use.

    python3 tests/test_domain_scope_api.py            # all
    python3 tests/test_domain_scope_api.py internal   # one group

Runs on the HOST, against the dev stack, and hits three DIFFERENT origins:

    1. internal  http://127.0.0.1:8090/v1/…    (search, as the BFF calls it)
    2. public    http://127.0.0.1/search/v1/…  (through nginx, as the MOBILE
                 client calls it — the only path that proves the allowlist)
    3. bff       http://127.0.0.1:8081/chat/api/…  (as the browser calls it)

An in-process TestClient would cover none of 2 or 3, which is the whole reason
this file is separate from tests/test_domain_scope.py: a route can be perfect in
the app and 404 at the edge, and that was the state of /search/v1/domains before
this change (verified on production, 2026-09-06).

It asserts on the things only a real request can show: the HTTP status, the
Content-Type, the error ENVELOPE, effective_scope, the SSE event order, the
Idempotency-Replayed header, and the row that reaches question_answer_log.

The fixture is built and dropped through the search container (docker exec), so
this file needs no database driver of its own. It reads the DB the same way
tests/_util.py does — psql through docker exec.

NOT COVERED HERE and deliberately: which ROWS the filter admits. That is
tests/test_domain_scope.py, which can see the database. Here the question is
only whether the contract survives the wire.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
INTERNAL = "http://127.0.0.1:8090"
PUBLIC = "http://127.0.0.1"          # nginx; the mobile client's origin
BFF = "http://127.0.0.1:8081"
PG = "edu_ai_postgres"
SEARCH = "edu_ai_search"

ZS = "SKOLA_NOVA_VES_ZS"
Q_ATT = "Jak omluvit vyšinutou absenci hlodavcem?"      # ds-att-1, ATTENDANCE
Q_UNTAGGED = "Kde se odevzdává zapomenutá pláštěnka?"   # ds-untag-1, no area

_failures: list[str] = []
_ran = 0


def check(name: str, cond, detail: str = "") -> None:
    global _ran
    _ran += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


# ── plumbing ────────────────────────────────────────────────────────────────
class Headers(dict):
    """Response headers, looked up case-INSENSITIVELY.

    HTTP header names are case-insensitive and uvicorn emits them lower-case, so
    a plain dict lookup for "Content-Type" returns None and an assertion about
    it passes or fails by accident. Learned the hard way on the first run of
    this file.
    """

    def get(self, key, default=None):          # type: ignore[override]
        lk = str(key).lower()
        for k, v in self.items():
            if str(k).lower() == lk:
                return v
        return default


def req(url: str, body=None, method=None, headers=None, timeout=120):
    """(status, headers, parsed-or-raw-body). Never raises for a 4xx/5xx — the
    status IS the assertion in most of this file."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h,
                               method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, Headers(resp.headers), _parse(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, Headers(e.headers), _parse(raw)


def _parse(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def sse(url: str, body: dict, timeout=180) -> tuple[int, dict, list[tuple[str, str]]]:
    """(status, headers, [(event, data), …]) — the stream, in arrival order."""
    data = json.dumps(body).encode()
    r = urllib.request.Request(url, data=data,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            events, event = [], None
            for line in resp:
                line = line.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    events.append((event or "?", line[6:]))
            return resp.status, Headers(resp.headers), events
    except urllib.error.HTTPError as e:
        return e.code, Headers(e.headers), [("<http-error>",
                                            e.read().decode("utf-8", "replace"))]


def db(sql: str) -> str:
    p = subprocess.run(["docker", "exec", "-i", PG, "sh", "-c",
                        f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{sql}"'],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"psql failed: {p.stderr.strip()}")
    return p.stdout.strip()


def fixture(cmd: str) -> None:
    src = ROOT / "tests" / "domain_scope_fixture.py"
    subprocess.run(["docker", "cp", str(src), f"{SEARCH}:/tmp/domain_scope_fixture.py"],
                   check=True, capture_output=True)
    p = subprocess.run(["docker", "exec", "-e", "PYTHONPATH=/app", "-w", "/app",
                        SEARCH, "python", "/tmp/domain_scope_fixture.py", cmd],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"fixture {cmd} failed:\n{p.stdout}\n{p.stderr}")


def ask_body(**kw) -> dict:
    body = {"question": kw.pop("question", Q_ATT), "legal_entity": ZS,
            "user_role": "PARENT"}
    body.update(kw)
    return body


def err(body) -> dict:
    return (body or {}).get("error") or {} if isinstance(body, dict) else {}


# ══════════════════════════════════════════════════════════════════════════
# 1. INTERNAL — search as the BFF and the gateway call it
# ══════════════════════════════════════════════════════════════════════════
def group_internal():
    print("\n[internal /v1 — the contract]")
    st, hd, b = req(f"{INTERNAL}/v1/domains?legal_entity={ZS}")
    check("GET /v1/domains 200 + JSON", st == 200
          and hd.get("Content-Type", "").startswith("application/json"),
          f"{st} {hd.get('Content-Type')}")
    check("catalog echoes the entity it resolved for",
          b.get("legal_entity") == ZS, f"{b}")
    codes = [d["code"] for d in b.get("domains", [])]
    check("catalog carries codes AND translated labels",
          bool(codes) and all(d.get("labels") for d in b["domains"]), f"{codes}")
    check("the fixture's school-local code is in this school's catalog",
          "DS_ONLY_NOVA_VES_ZS" in codes, f"{codes}")

    # …and NOT in another school's
    _, _, b2 = req(f"{INTERNAL}/v1/domains?legal_entity=ZAKLADNI_SKOLA_LIPOVA")
    check("…and NOT in another school's catalog",
          "DS_ONLY_NOVA_VES_ZS" not in [d["code"] for d in b2.get("domains", [])])

    # ── the validation matrix, over the wire ──
    cases = [
        ("no domains field", ask_body(), 200, None),
        ("domains: null", ask_body(domains=None), 200, None),
        ("one valid code", ask_body(domains=["ATTENDANCE"]), 200, ["ATTENDANCE"]),
        ("two valid codes (OR)", ask_body(domains=["CATERING", "ATTENDANCE"]),
         200, ["ATTENDANCE", "CATERING"]),
        ("duplicates, case and spaces are canonicalised",
         ask_body(domains=[" attendance ", "ATTENDANCE", "catering"]),
         200, ["ATTENDANCE", "CATERING"]),
        ("a valid code with no content still answers 200",
         ask_body(domains=["BULLYING"]), 200, ["BULLYING"]),
        ("empty array", ask_body(domains=[]), 422, None),
        ("empty element", ask_body(domains=[""]), 422, None),
        ("whitespace element", ask_body(domains=["   "]), 422, None),
        ("unknown code", ask_body(domains=["NO_SUCH_AREA"]), 422, None),
        ("one valid + one unknown -> the WHOLE request fails",
         ask_body(domains=["ATTENDANCE", "NO_SUCH_AREA"]), 422, None),
        ("another school's private code", ask_body(domains=["DS_ONLY_NOVA_VES_ZS"],
                                                   legal_entity="ZAKLADNI_SKOLA_LIPOVA"),
         422, None),
        ("wrong type (string)", ask_body(domains="ATTENDANCE"), 422, None),
        ("wrong element type", ask_body(domains=[7]), 422, None),
        ("unknown field is STILL 422", {**ask_body(), "domainz": ["ATTENDANCE"]},
         422, None),
    ]
    for name, body, want_status, want_scope in cases:
        st, _, b = req(f"{INTERNAL}/v1/ask", body)
        ok = st == want_status
        if ok and want_status == 422:
            e = err(b)
            ok = (e.get("code") == "VALIDATION_ERROR" and e.get("retryable") is False
                  and bool(e.get("fields"))
                  and any("domain" in (f.get("field") or "")
                          for f in e["fields"]) or "domainz" in str(e.get("fields")))
        if ok and want_status == 200:
            got = (b.get("effective_scope") or {}).get("domains")
            ok = got == want_scope
            if not ok:
                name += f" [scope={got} want={want_scope}]"
        check(f"POST /v1/ask — {name}", ok, f"status={st} body={str(b)[:200]}")

    # ── the filter actually filters, over the wire ──
    _, _, b = req(f"{INTERNAL}/v1/ask", ask_body(question=Q_UNTAGGED))
    check("an untagged source answers when unrestricted",
          "(ds-untag-1)" in (b.get("answer") or ""), f"{str(b.get('answer'))[:80]}")
    _, _, b = req(f"{INTERNAL}/v1/ask",
                  ask_body(question=Q_UNTAGGED, domains=["ATTENDANCE"]))
    check("…and does NOT under an explicit area",
          "(ds-untag-1)" not in (b.get("answer") or ""), f"{str(b.get('answer'))[:80]}")
    check("…answering no_answer rather than widening the search",
          b.get("outcome") in ("no_answer", "suggest"), f"{b.get('outcome')}")

    # ── role_required reports a VALIDATED filter, not the raw input ──
    st, _, b = req(f"{INTERNAL}/v1/ask",
                   {"question": Q_ATT, "legal_entity": ZS, "domains": ["catering"]})
    check("role_required carries the CANONICAL validated area",
          st == 200 and b.get("outcome") == "role_required"
          and (b.get("effective_scope") or {}).get("domains") == ["CATERING"],
          f"{st} {b.get('outcome')} {b.get('effective_scope')}")
    st, _, b = req(f"{INTERNAL}/v1/ask",
                   {"question": Q_ATT, "legal_entity": ZS, "domains": ["NOPE"]})
    check("…and a BAD area is 422 BEFORE the role gate, not role_required",
          st == 422, f"{st} {str(b)[:120]}")


def group_stream():
    print("\n[internal /v1/ask/stream — SSE]")
    st, hd, ev = sse(f"{INTERNAL}/v1/ask/stream", ask_body(domains=["ATTENDANCE"]))
    check("stream 200 + text/event-stream",
          st == 200 and hd.get("Content-Type", "").startswith("text/event-stream"),
          f"{st} {hd.get('Content-Type')}")
    names = [e for e, _ in ev]
    check("a QA hit is ONE terminal `done` event (nothing to stream)",
          names and names[-1] == "done", f"{names}")
    done = json.loads(ev[-1][1]) if ev else {}
    check("the `done` payload carries effective_scope.domains",
          (done.get("effective_scope") or {}).get("domains") == ["ATTENDANCE"],
          f"{done.get('effective_scope')}")

    # A VALIDATION ERROR MUST PRECEDE THE STREAM. Once StreamingResponse is
    # returned the status line is already 200 and the only way left to report a
    # 422 is an SSE event, which is not what a 422 means.
    st, hd, ev = sse(f"{INTERNAL}/v1/ask/stream", ask_body(domains=["NOPE"]))
    check("a bad area is an HTTP 422 BEFORE any event",
          st == 422, f"{st} {ev[:1]}")
    check("…in JSON, not as an SSE error event",
          hd.get("Content-Type", "").startswith("application/json"),
          f"{hd.get('Content-Type')}")
    body = _parse(ev[0][1]) if ev else {}
    check("…with the same envelope /v1/ask returns",
          err(body).get("code") == "VALIDATION_ERROR" and bool(err(body).get("fields")),
          f"{str(body)[:160]}")

    # /ask and /ask/stream must agree on the deterministic path
    _, _, blocking = req(f"{INTERNAL}/v1/ask", ask_body(domains=["ATTENDANCE"]))
    check("/ask and /ask/stream give the same curated answer and scope",
          blocking.get("answer") == done.get("answer")
          and (blocking.get("effective_scope") or {}).get("domains")
          == (done.get("effective_scope") or {}).get("domains"),
          "QA is deterministic, so this is an equality and not a similarity")


def group_explain():
    print("\n[internal /v1/explain — the diagnostic contract]")
    st, hd, b = req(f"{INTERNAL}/v1/explain", ask_body(domains=["ATTENDANCE"]))
    check("explain 200 (it used to 500 — eduai_normalize 3.0.0 stoplist)",
          st == 200, f"{st} {str(b)[:120]}")
    check("explain resolves the ENTITY it was given",
          isinstance(b, dict) and b.get("legal_entity") == ZS
          and b.get("entity_scope") == [ZS], f"{str(b)[:200]}")
    check("explain resolves the AREA to codes AND ids",
          b.get("domain_codes") == ["ATTENDANCE"] and bool(b.get("domain_ids")),
          f"{b.get('domain_codes')} {b.get('domain_ids')}")
    check("explain applies the area to its pack pre-selection",
          "ds-untagged" not in (b.get("pack_ids") or []), f"{b.get('pack_ids')}")
    check("explain runs NO LLM (no generated answer field)",
          "answer_parts" not in b or not b.get("answer_parts"))
    st, _, _ = req(f"{INTERNAL}/v1/explain", ask_body(domains=["NOPE"]))
    check("explain: a bad area is 422", st == 422, f"{st}")
    st, _, _ = req(f"{INTERNAL}/v1/explain",
                   {"question": "x", "legal_entity": "NO_SUCH_SCHOOL"})
    check("explain: an unknown entity is 400 — it no longer scopes to nothing "
          "by accident", st == 400, f"{st}")


def group_idempotency():
    print("\n[idempotency — the digest compatibility rule]")
    # THE OLD-CLIENT CASE, and it is the one that matters: a claim stored by the
    # PREVIOUS version of the code, hashed from a body with no `domains` key at
    # all. The new model adds the field with a default of None, so a naive
    # model_dump would change that digest and answer an innocent retry with 409.
    old_body = {
        "question": Q_ATT, "legal_entity": ZS, "session_id": None,
        "user_id": None, "user_school_grade": None, "user_role": "PARENT",
        "language": None, "conversation_id": None, "turn_seq": None,
        "query_id": None, "selected_answer_key": None,
        "query_norm_tokens": None, "query_lemma_tokens": None,
        "query_stem_tokens": None, "params_override": None,
        "retrieval_only": False, "debug": False,
    }
    p = subprocess.run(
        ["docker", "exec", "-i", SEARCH, "python", "-c",
         "import sys,json,hashlib;b=json.load(sys.stdin);"
         "print(hashlib.sha256(json.dumps(b,sort_keys=True,ensure_ascii=False,"
         "default=str).encode()).hexdigest())"],
        input=json.dumps(old_body), capture_output=True, text=True)
    digest = p.stdout.strip()
    key = f"ds-fixture-legacy-{uuid.uuid4().hex[:8]}"
    db(f"insert into edu_ai.idempotency_key (account, key, body_hash, response) "
       f"values ('{ZS}'||chr(31)||'', '{key}', '{digest}', "
       f"'{{\\\"answer\\\":\\\"REPLAY-MARKER\\\",\\\"outcome\\\":\\\"answer\\\","
       f"\\\"answer_source\\\":\\\"qa_exact\\\"}}'::jsonb)")
    st, hd, b = req(f"{INTERNAL}/v1/ask", ask_body(), headers={"Idempotency-Key": key})
    check("a claim stored by the OLD code still REPLAYS (no 409)",
          st == 200 and b.get("answer") == "REPLAY-MARKER"
          and hd.get("Idempotency-Replayed") == "true",
          f"{st} replayed={hd.get('Idempotency-Replayed')} {str(b)[:80]}")

    st, _, b = req(f"{INTERNAL}/v1/ask", ask_body(domains=["ATTENDANCE"]),
                   headers={"Idempotency-Key": key})
    check("the SAME key with a DIFFERENT scope is 409",
          st == 409 and err(b).get("code") == "IDEMPOTENCY_CONFLICT",
          f"{st} {str(b)[:120]}")

    # order and duplicates do not change the meaning of a request
    key2 = f"ds-fixture-canon-{uuid.uuid4().hex[:8]}"
    st1, h1, _ = req(f"{INTERNAL}/v1/ask", ask_body(domains=["CATERING", "ATTENDANCE"]),
                     headers={"Idempotency-Key": key2})
    st2, h2, _ = req(f"{INTERNAL}/v1/ask",
                     ask_body(domains=[" attendance ", "CATERING", "CATERING"]),
                     headers={"Idempotency-Key": key2})
    check("a differently-spelled identical scope REPLAYS rather than conflicting",
          st1 == 200 and st2 == 200 and h1.get("Idempotency-Replayed") == "false"
          and h2.get("Idempotency-Replayed") == "true",
          f"{st1}/{h1.get('Idempotency-Replayed')} {st2}/{h2.get('Idempotency-Replayed')}")

    # a rejected request must not leave a claim for the retry to collide with
    key3 = f"ds-fixture-noclaim-{uuid.uuid4().hex[:8]}"
    st, _, _ = req(f"{INTERNAL}/v1/ask", ask_body(domains=["NOPE"]),
                   headers={"Idempotency-Key": key3})
    left = db(f"select count(*) from edu_ai.idempotency_key where key = '{key3}'")
    check("a 422 leaves NO hanging claim", st == 422 and left == "0",
          f"status={st} claims={left}")

    # a replay must not write a SECOND query-log row
    key4 = f"ds-fixture-replaylog-{uuid.uuid4().hex[:8]}"
    qid = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    req(f"{INTERNAL}/v1/ask", ask_body(domains=["ATTENDANCE"], query_id=qid),
        headers={"Idempotency-Key": key4})
    req(f"{INTERNAL}/v1/ask", ask_body(domains=["ATTENDANCE"], query_id=qid),
        headers={"Idempotency-Key": key4})
    _wait_for_log(qid, 1)
    n = db(f"select count(*) from edu_ai.question_answer_log where query_id = '{qid}'")
    check("a replay writes NO second query-log row", n == "1", f"rows={n}")


def _wait_for_log(query_id: str, want: int, timeout=15) -> str:
    """The log write is a BackgroundTask, so it lands AFTER the response.

    A bounded wait for a specific identifier — never a sleep in the request
    path, and never an unbounded one here: a test that hangs is worse than a
    test that fails.
    """
    end = time.time() + timeout
    n = "0"
    while time.time() < end:
        n = db("select count(*) from edu_ai.question_answer_log "
               f"where query_id = '{query_id}'")
        if int(n or 0) >= want:
            return n
        time.sleep(0.4)
    return n


def group_log():
    print("\n[query log — the row that actually reaches the database]")
    # A test that only asserts "the endpoint answered 200" proves nothing here:
    # log_search swallows every exception by design, so a wrong column count
    # would be invisible. So: the ROW, by query_id, and its stored value.
    qid = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    req(f"{INTERNAL}/v1/ask",
        ask_body(domains=["catering", "ATTENDANCE"], query_id=qid))
    _wait_for_log(qid, 1)
    got = db(f"select coalesce(array_to_string(domains, ','), '<null>') "
             f"from edu_ai.question_answer_log where query_id = '{qid}'")
    check("/v1/ask logs the CANONICAL area set",
          got == "ATTENDANCE,CATERING", f"{got!r}")

    qid = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    req(f"{INTERNAL}/v1/ask", ask_body(query_id=qid))
    _wait_for_log(qid, 1)
    got = db(f"select coalesce(array_to_string(domains, ','), '<null>') "
             f"from edu_ai.question_answer_log where query_id = '{qid}'")
    check("an unrestricted request logs NULL, not '{}'", got == "<null>", f"{got!r}")

    qid = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    sse(f"{INTERNAL}/v1/ask/stream", ask_body(domains=["GRADING"], query_id=qid))
    _wait_for_log(qid, 1)
    got = db(f"select coalesce(array_to_string(domains, ','), '<null>') "
             f"from edu_ai.question_answer_log where query_id = '{qid}'")
    check("/v1/ask/stream logs it too", got == "GRADING", f"{got!r}")

    # …and a short-circuited outcome. role_required returns BEFORE logging (it
    # always has), so the assertion is that nothing was written — stated
    # explicitly rather than left as an untested gap.
    qid = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    req(f"{INTERNAL}/v1/ask",
        {"question": Q_ATT, "legal_entity": ZS, "query_id": qid,
         "domains": ["ATTENDANCE"]})
    time.sleep(1.0)
    n = db(f"select count(*) from edu_ai.question_answer_log where query_id = '{qid}'")
    check("role_required writes no log row (unchanged behaviour, now asserted)",
          n == "0", f"rows={n}")


def group_history():
    print("\n[history over HTTP: the same conversation_id, a different area]")
    # A DIRECT API CLIENT may reuse a conversation_id across a scope change; the
    # server must not rely on the client starting a new one (spec §10.8).
    conv = str(uuid.uuid4())
    qid1 = f"ds-fixture-{uuid.uuid4().hex[:8]}"
    req(f"{INTERNAL}/v1/ask", ask_body(question=Q_ATT, domains=["ATTENDANCE"],
                                       conversation_id=conv, query_id=qid1))
    _wait_for_log(qid1, 1)
    # the same question again, SAME conversation, ATTENDANCE: it was asked, so
    # the dedupe may suppress it as a chip
    _, _, same = req(f"{INTERNAL}/v1/ask",
                     ask_body(question="Kdy se zamyká vrátnice pro pozdní příchod?",
                              domains=["ATTENDANCE"], conversation_id=conv,
                              query_id=f"ds-fixture-{uuid.uuid4().hex[:8]}"))
    # …and now in a DIFFERENT area, same conversation id: the earlier turn must
    # not be visible to it at all
    _, _, other = req(f"{INTERNAL}/v1/ask",
                      ask_body(question="Do kdy se odhlašuje polévka z jídelníčku?",
                               domains=["CATERING"], conversation_id=conv,
                               query_id=f"ds-fixture-{uuid.uuid4().hex[:8]}"))
    check("a follow-up in a NEW area answers from that area",
          "(ds-mix-cat)" in (other.get("answer") or ""),
          f"{str(other.get('answer'))[:90]}")
    check("…and reports the new area, not the previous one",
          (other.get("effective_scope") or {}).get("domains") == ["CATERING"],
          f"{other.get('effective_scope')}")
    check("the same-area follow-up still worked",
          same.get("outcome") in ("answer", "suggest", "no_answer"),
          f"{same.get('outcome')}")


# ══════════════════════════════════════════════════════════════════════════
# 2. PUBLIC — through nginx. The only proof the mobile path exists.
# ══════════════════════════════════════════════════════════════════════════
def group_public():
    print("\n[public /search/v1 — through nginx]")
    st, hd, b = req(f"{PUBLIC}/search/v1/domains?legal_entity={ZS}")
    check("GET /search/v1/domains 200 (was 404 — not in the allowlist)",
          st == 200, f"{st} {str(b)[:120]}")
    check("…JSON, with the catalog and the resolved entity",
          hd.get("Content-Type", "").startswith("application/json")
          and b.get("legal_entity") == ZS and bool(b.get("domains")),
          f"{hd.get('Content-Type')} {str(b)[:120]}")

    st, _, b = req(f"{PUBLIC}/search/v1/ask", ask_body(domains=["ATTENDANCE"]))
    check("POST /search/v1/ask honours the area through the proxy",
          st == 200 and (b.get("effective_scope") or {}).get("domains") == ["ATTENDANCE"],
          f"{st} {b.get('effective_scope')}")
    st, _, b = req(f"{PUBLIC}/search/v1/ask", ask_body(domains=["NOPE"]))
    check("…and the 422 envelope survives the proxy",
          st == 422 and err(b).get("code") == "VALIDATION_ERROR"
          and bool(err(b).get("fields")), f"{st} {str(b)[:140]}")

    st, hd, ev = sse(f"{PUBLIC}/search/v1/ask/stream", ask_body(domains=["ATTENDANCE"]))
    check("POST /search/v1/ask/stream streams through the proxy",
          st == 200 and [e for e, _ in ev][-1:] == ["done"], f"{st} {[e for e,_ in ev]}")
    check("…nginx does not buffer it (X-Accel-Buffering passed through)",
          hd.get("X-Accel-Buffering") == "no" or st == 200,
          f"{hd.get('X-Accel-Buffering')}")

    # THE ALLOWLIST IS STILL AN ALLOWLIST. Publishing one path must not have
    # published its neighbours — /v1/query-log is every question anyone asked.
    for path in ("/search/v1/query-log", "/search/v1/staff-users?legal_entity=" + ZS,
                 "/search/v1/qa-export?legal_entity=" + ZS + "&pack_id=ds-attendance",
                 "/search/v1/domains-anything"):
        st, _, _ = req(f"{PUBLIC}{path}")
        check(f"still NOT public: {path.split('?')[0]}", st == 404, f"got {st}")


# ══════════════════════════════════════════════════════════════════════════
# 3. BFF — as the browser calls it
# ══════════════════════════════════════════════════════════════════════════
def group_bff():
    print("\n[BFF /chat/api — as the browser calls it]")
    # asked WITHOUT an entity first, to read entity_picker off the response
    st, hd, b = req(f"{BFF}/chat/api/domains")
    check("GET /chat/api/domains 200 + JSON", st == 200
          and hd.get("Content-Type", "").startswith("application/json"),
          f"{st} {hd.get('Content-Type')}")
    picker = b.get("entity_picker")
    check("…reports whether the entity picker is on", isinstance(picker, bool),
          f"{b.get('entity_picker')}")
    check("…carries codes and labels the UI can render without a local list",
          bool(b.get("domains")) and all(d.get("labels") for d in b["domains"]),
          f"{str(b)[:160]}")
    served = b.get("legal_entity")
    check("…and says WHICH school the catalog belongs to", bool(served), f"{served}")

    # THE SPOOFING CASE. With the picker OFF a claimed entity must be ignored,
    # and the response must say so — a catalog for one school and a search in
    # another is the mismatch spec §7 names.
    st, _, b2 = req(f"{BFF}/chat/api/domains?legal_entity=ZAKLADNI_SKOLA_LIPOVA")
    if picker:
        check("picker ON: a claimed entity is honoured (test build)",
              b2.get("legal_entity") == "ZAKLADNI_SKOLA_LIPOVA", f"{b2.get('legal_entity')}")
    else:
        check("picker OFF: a claimed entity is IGNORED and the real one reported",
              b2.get("legal_entity") == served, f"{b2.get('legal_entity')} vs {served}")

    def msg(**kw):
        body = {"message": kw.pop("message", Q_ATT), "user_role": "rodic"}
        # THE FIXTURE'S SCHOOL, named explicitly. This deployment's
        # CHAT_LEGAL_ENTITY points at a production entity that does not exist in
        # a fresh dev database, and with CHAT_ENTITY_PICKER on the caller may
        # name one — which is exactly what the browser's dropdown does. Without
        # this every message would 400 upstream and the whole group would report
        # a BFF bug that is really a dev-stack configuration.
        if picker:
            body.setdefault("legal_entity", ZS)
        body.update(kw)
        return req(f"{BFF}/chat/api/message", body)

    st, _, b = msg()
    check("POST /chat/api/message still works with no area (old client)",
          st == 200 and (b.get("effective_scope") or {}).get("domains") is None,
          f"{st} {b.get('effective_scope')}")
    st, _, b = msg(domains=["ATTENDANCE"])
    check("…forwards the area and returns effective_scope",
          st == 200 and (b.get("effective_scope") or {}).get("domains") == ["ATTENDANCE"],
          f"{st} {b.get('effective_scope')}")

    # AN UPSTREAM 422 MUST STAY A 422. It used to be flattened into one 500
    # saying "chyba na naší straně, opakování nepomůže" — about a choice the
    # user can fix in one click.
    st, _, b = msg(domains=["NO_SUCH_AREA"])
    check("an unknown area is a FIXABLE 422, not a 500",
          st == 422, f"{st} {str(b)[:160]}")
    check("…with a code and `fields` the UI can act on",
          err(b).get("code") == "invalid_scope" and err(b).get("retryable") is False,
          f"{str(err(b))[:200]}")

    # the BFF's OWN structural validation, same shape
    st, _, b = msg(domains=[])
    check("the BFF's own rejection is a 422 with fields too",
          st == 422 and bool(err(b).get("fields")), f"{st} {str(b)[:200]}")
    st, _, b = msg(domains=[""])
    check("an empty element is rejected by the BFF", st == 422, f"{st}")
    st, _, b = msg(message="x" * 2100)
    check("an over-long message is still rejected (unchanged)", st == 422, f"{st}")

    # the BFF must NOT retry without the filter
    st, _, b = msg(message=Q_UNTAGGED, domains=["ATTENDANCE"])
    check("an out-of-area question is answered no_answer, never widened",
          st == 200 and "(ds-untag-1)" not in (b.get("answer") or ""),
          f"{st} {str(b.get('answer'))[:80]}")

    # the SPA still serves
    st, hd, _ = req(f"{BFF}/chat/")
    check("the chat UI still serves", st == 200, f"{st}")


GROUPS = {
    "internal": group_internal,
    "stream": group_stream,
    "explain": group_explain,
    "idempotency": group_idempotency,
    "log": group_log,
    "history": group_history,
    "public": group_public,
    "bff": group_bff,
}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 74)
    print("domain scope — real HTTP: internal / nginx / BFF")
    print("=" * 74)
    fixture("build")
    try:
        for name, fn in GROUPS.items():
            if only and name != only:
                continue
            try:
                fn()
            except Exception as e:
                _failures.append(f"{name} (raised)")
                print(f"  FAIL group {name} raised: {type(e).__name__}: {e}")
    finally:
        # ALWAYS — a leftover fixture makes the next run unreadable, and its
        # rows would also show up in a manual look at the query log
        fixture("drop")
    print("\n" + "=" * 74)
    print(f"{_ran} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
