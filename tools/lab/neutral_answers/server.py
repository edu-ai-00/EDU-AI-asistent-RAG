"""Neutral answers lab — a small local review app (standard library only).

    python3 server.py --input faq.xlsx [--sheet NAME] [--state decisions.json]
                      [--stoplist contracts/parity/stoplist.json] [--port 8770]

Normally started through run.sh, inside the edu_ai_search image, so that the
machine view runs the real normalizer. Serves one page and a tiny JSON API:

    GET  /api/items               items + proposals + decisions
    POST /api/decision            {id, status, final?, note?}
    GET  /export/review.csv       every item: original, final, status, rules
    GET  /export/pack.csv         the WHOLE set in the QA pack CSV layout; only an
                                  approved item carries the new wording

Decisions live in ONE JSON file outside the repository (QA packs are private
data, see docs/RELEASING.md). Each decision remembers the original answer it was
made for; when the workbook changes that answer, the decision is shown as stale
instead of being silently carried over to a different text.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import machine                      # noqa: E402
from rules import RULES, neutralize  # noqa: E402
from xlsx_read import load_items    # noqa: E402

_STATUSES = {"pending", "approved", "rejected"}
_MAX_BODY = 64 * 1024
# the QA pack CSV header, as search/app/qa_csv.py writes it (legacy 8 columns);
# category_code is appended only when the input CSV had it (CONTRACT.md 5.1: a
# PRESENT empty cell clears an entry's category, an ABSENT column keeps it)
PACK_HEADER = ["entry_key", "canonical_q", "answer_md", "cat", "variants",
               "sources", "is_active", "domains"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Store:
    """Decisions file: {"source": name, "decisions": {id: {...}}}."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"decisions": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, item_id: str) -> dict | None:
        return self.data["decisions"].get(item_id)

    def put(self, item_id: str, record: dict) -> None:
        with self.lock:
            self.data["decisions"][item_id] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


class App:
    def __init__(self, args):
        self.source = Path(args.input).name
        self.subject = args.subject
        items = load_items(args.input, args.sheet)
        self.store = Store(Path(args.state))
        self.items = []
        for it in items:
            questions = [it["canonical_q"], *it["alternatives"]]
            prop = neutralize(it["answer"], questions, it["sources"], subject=self.subject)
            mach = None
            if machine.available():
                mach = {"answer": machine.analyse(it["answer"]),
                        "proposed": machine.analyse(prop["proposed"]),
                        "questions": [{"text": q, **(machine.analyse(q) or {})} for q in questions]}
            self.items.append({**it, **prop, "answer_sha": _sha(it["answer"]), "machine": mach})
        self.by_id = {it["id"]: it for it in self.items}
        self.has_category_code = any((it.get("pack") or {}).get("category_code") is not None
                                     for it in self.items)

    def decision(self, it: dict) -> dict:
        d = self.store.get(it["id"])
        if not d:
            return {"status": "pending", "final": None, "note": "", "stale": False}
        stale = d.get("answer_sha") != it["answer_sha"]
        return {**d, "stale": stale, "status": "pending" if stale else d.get("status", "pending")}

    def payload(self) -> dict:
        return {"meta": {"source": self.source, "subject": self.subject, "count": len(self.items),
                         "machine": machine.available(), "awaiting_review": self.awaiting_review()},
                "rules": {k: {"title": t, "description": d} for k, (t, d) in RULES.items()},
                "items": [{**it, "decision": self.decision(it)} for it in self.items]}

    def decide(self, body: dict) -> dict:
        it = self.by_id.get(str(body.get("id", "")))
        status = body.get("status")
        if it is None or status not in _STATUSES:
            raise ValueError("unknown id or status")
        final = body.get("final")
        if final is not None and not isinstance(final, str):
            raise ValueError("final must be text")
        record = {"status": status,
                  "final": (final.strip() if final else None),
                  "note": str(body.get("note") or "")[:500],
                  "answer_sha": it["answer_sha"],
                  "original": it["answer"],
                  "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.store.put(it["id"], record)
        return self.decision(it)

    def final_text(self, it: dict) -> str:
        """Rejected keeps the original; a stale decision's edit is not reused."""
        d = self.decision(it)
        if d["status"] == "rejected":
            return it["answer"]
        return (None if d["stale"] else d.get("final")) or it["proposed"]

    def pack_text(self, it: dict) -> str:
        """The answer that goes into the QA pack: the new wording only when a
        curator approved it, the ORIGINAL otherwise (unchanged, rejected, not
        yet reviewed). An unreviewed rewrite never reaches the application, and
        no answer drops out of the pack — uploaded in replace mode, a missing
        entry would be switched off."""
        return self.final_text(it) if self.decision(it)["status"] == "approved" else it["answer"]

    def awaiting_review(self) -> int:
        """Items with a proposed change nobody has decided on yet."""
        return sum(1 for it in self.items
                   if it["proposed"] != it["answer"] and self.decision(it)["status"] == "pending")

    def export(self, kind: str) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        if kind == "pack":
            # Same bytes as /v1/qa-export: comma, CRLF, UTF-8 BOM. From a
            # workbook, entry_key stays EMPTY (the reader derives a stable key
            # from the question, as for scripts/upload_qa_packs.py — a key from
            # the row position duplicated entries once rows were reordered) and
            # domains stays EMPTY (= inherit the upload's areas; "[]" would
            # CLEAR them). From a pack CSV, those columns come back unchanged.
            w.writerow(PACK_HEADER + (["category_code"] if self.has_category_code else []))
            for it in self.items:
                p = it.get("pack") or {}
                row = [p.get("entry_key", ""), it["canonical_q"], self.pack_text(it),
                       p.get("cat", ""), "\n".join(it["alternatives"]), it["sources"],
                       p.get("is_active") or "TRUE", p.get("domains", "")]
                if self.has_category_code:
                    row.append(p.get("category_code") or "")
                w.writerow(row)
        else:
            w.writerow(["id", "canonical_q", "puvodni", "vysledna", "stav", "rucne_upraveno",
                        "pravidla", "upozorneni", "poznamka"])
            for it in self.items:
                d = self.decision(it)
                w.writerow([it["id"], it["canonical_q"], it["answer"], self.final_text(it),
                            d["status"], "ano" if d.get("final") else "",
                            ", ".join(sorted({c["rule"] for c in it["changes"]})),
                            " | ".join(f["note"] for f in it["flags"]), d.get("note", "")])
        # BOM: Excel otherwise reads Czech as the system codepage
        return buf.getvalue().encode("utf-8-sig")


def make_handler(app: App):
    static = HERE / "static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):          # no request bodies in the log
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, (static / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/items":
                return self._json(200, app.payload())
            if path in ("/export/review.csv", "/export/pack.csv"):
                kind = "pack" if "pack" in path else "review"
                wait = app.awaiting_review() if kind == "pack" else 0
                # the file name says it when rewrites are still unreviewed
                name = f"{Path(app.source).stem}-{kind}{f'-ceka-{wait}' if wait else ''}.csv"
                return self._send(200, app.export(kind), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/decision":
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > _MAX_BODY:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body size"})
            try:
                body = json.loads(self.rfile.read(n))
                return self._json(200, app.decide(body))
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--input", required=True, help="QA workbook (.xlsx) or QA pack (.csv)")
    p.add_argument("--sheet", default=None, help="sheet name (default: the first)")
    p.add_argument("--state", default=str(Path.home() / "edu-ai/state/neutral_answers/decisions.json"))
    # default = the repository layout (tools/lab/neutral_answers -> repo root);
    # run.sh mounts the directory elsewhere and passes --stoplist explicitly
    repo = HERE.parents[2] if len(HERE.parents) > 2 else HERE
    p.add_argument("--stoplist", default=str(repo / "contracts/parity/stoplist.json"))
    p.add_argument("--subject", default="uchazeč", help="neutral subject (uchazeč | žák)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    args = p.parse_args(argv)
    if Path(args.stoplist).exists():
        machine.configure(args.stoplist)
    app = App(args)
    print(f"neutral-answers: {len(app.items)} items from {app.source}, "
          f"machine view {'on' if machine.available() else 'off'} -> http://{args.host}:{args.port}/",
          flush=True)
    ThreadingHTTPServer((args.host, args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
