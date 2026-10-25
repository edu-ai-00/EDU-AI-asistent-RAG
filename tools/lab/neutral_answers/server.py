"""Neutral answers lab — a small local review app (standard library only).

    python3 server.py --input faq.xlsx [--sheet NAME] [--state decisions.json]
                      [--stoplist contracts/parity/stoplist.json] [--port 8770]

Normally started through run.sh, inside the edu_ai_search image, so that the
machine view runs the real normalizer. Serves one page and a tiny JSON API:

    GET  /api/items               items + proposals + decisions
    POST /api/decision            {id, status, final?, note?}
    GET  /export/review.csv       every item: original, final, status, rules
    GET  /api/queue               questions from the log (--queue), with decisions,
                                  topics and the subgroups inside each topic
    POST /api/queue/decision      {key, status, entry?, wording?, entries?, note?}
    POST /api/queue/topic-decision {topic, status, entry?, all_wordings?, entries?, note?}
    POST /api/queue/assign        {key, topic}: move a group (topic "__later__" = park it,
                                  "__alone__" = out of every topic, a question of its own)
    POST /api/queue/topic         {title} creates a topic, {id, title} renames one
    GET  /export/queue.csv        every group: topic, decision, the wording to add
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
import topics as topics_mod         # noqa: E402
from rules import ADDRESSES, RULES, neutralize  # noqa: E402
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

    def put(self, item_id: str, record: dict, section: str = "decisions") -> None:
        with self.lock:
            self.data.setdefault(section, {})[item_id] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


QUEUE_STATUSES = {"pending", "alternative", "ambiguous", "missing", "skip"}
LATER = "__later__"                 # parked groups: out of every topic until moved back
ALONE = "__alone__"                 # taken out of a topic as a question of its own (missing answer)
OUT_OF_TOPIC = (LATER, ALONE)       # no topic decision applies there


class QueueReview:
    """Questions from the log that got no confident answer (log_queue.py), and
    the curator's decision per group: an alternative for one offered entry, an
    ambiguous question (which entries the "myslel jste…?" list must hold), a
    missing answer, or skip. Stored next to the answer decisions, outside Git:
    the groups carry users' questions."""

    def __init__(self, queue_path: Path, state_path: Path, topics_path: Path | None = None):
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        self.groups = data.get("groups", data if isinstance(data, list) else [])
        self.by_key = {g["key"]: g for g in self.groups}
        self.store = Store(state_path)
        # topics: the file of topics.py (hand-made split + the search's own
        # reduction), else the reduction alone; a group the file does not know
        # goes to "Nezařazené"
        if topics_path:
            t = json.loads(topics_path.read_text(encoding="utf-8"))
        else:
            t = topics_mod.build(self.groups)
        self.base_topics = dict(t["topics"])
        self.base_assign = dict(t["assign"])
        self.concepts = dict(t["concepts"])
        self.weights = dict(t["weights"])
        for g in self.groups:
            if g["key"] not in self.base_assign:
                self.base_topics.setdefault(topics_mod.UNSORTED, {"title": topics_mod.UNSORTED, "area": ""})
                self.base_assign[g["key"]] = topics_mod.UNSORTED
            self.concepts.setdefault(g["key"], topics_mod.concepts(g.get("lemmas"), {}, topics_mod._COVERAGE_STOP))

    def decision(self, key: str) -> dict:
        return self.store.get(key) or {"status": "pending"}

    # ── topics ──
    def topics(self) -> dict:
        out = {tid: {**t, "custom": False} for tid, t in self.base_topics.items()}
        for tid, t in self.store.data.get("topics", {}).items():
            out[tid] = {**out.get(tid, {"area": "", "custom": True}), **t}
        return out

    def assign(self) -> dict:
        moved = self.store.data.get("assign", {})
        # a group marked missing on its own and never moved (marked before the
        # topics existed) counts as taken out of its topic, like one marked now
        return {g["key"]: moved.get(g["key"]) or
                (ALONE if self.decision(g["key"])["status"] == "missing" else self.base_assign[g["key"]])
                for g in self.groups}

    def topic_decision(self, tid: str) -> dict:
        return self.store.data.get("topic_decisions", {}).get(tid) or {"status": "pending"}

    def subgroups(self, assign: dict | None = None) -> dict:
        """key → subgroup number inside its topic (wordings the search treats alike)."""
        assign = assign or self.assign()
        members: dict[str, list[str]] = {}
        for g in self.groups:                      # queue order = most frequent first
            members.setdefault(assign[g["key"]], []).append(g["key"])
        out = {}
        for ks in members.values():
            for n, c in enumerate(topics_mod.cluster(ks, self.concepts, self.weights)):
                for k in c:
                    out[k] = n
        return out

    def effective(self, key: str, assign: dict) -> tuple[dict, str]:
        """(decision, where): the group's own decision wins over its topic's."""
        own = self.decision(key)
        if own["status"] != "pending":
            return own, "group"
        tid = assign[key]
        if tid not in OUT_OF_TOPIC:
            td = self.topic_decision(tid)
            if td["status"] != "pending":
                return td, "topic"
        return own, "group"

    def state(self) -> dict:
        assign = self.assign()
        decided = sum(1 for g in self.groups if self.effective(g["key"], assign)[0]["status"] != "pending")
        return {"topics": self.topics(), "assign": assign, "base": self.base_assign, "subgroup": self.subgroups(assign),
                "topic_decisions": self.store.data.get("topic_decisions", {}),
                "decisions": {g["key"]: self.decision(g["key"]) for g in self.groups},
                # shown on the page: WHERE decisions are kept and how many it holds
                "store": {"file": self.store.path.name, "decided": decided}}

    def payload(self) -> dict:
        st = self.state()
        return {"groups": [{**g, "decision": st["decisions"][g["key"]]} for g in self.groups], **st}

    def move(self, body: dict) -> dict:
        key, tid = str(body.get("key", "")), str(body.get("topic", ""))
        if key not in self.by_key or (tid not in OUT_OF_TOPIC and tid not in self.topics()):
            raise ValueError("unknown group or topic")
        self.store.put(key, tid, section="assign")
        own = self.store.data.get("topics", {}).get(tid)
        if own and own.get("custom"):
            # one's own topics are listed first, the last one added to on top
            self.store.put(tid, {**own, "touched_utc": dt.datetime.now(dt.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%S.%fZ")}, section="topics")
        return self.state()

    def save_topic(self, body: dict) -> dict:
        title = str(body.get("title") or "").strip()[:120]
        if not title:
            raise ValueError("empty title")
        tid = str(body.get("id") or "")
        known = self.topics()
        if tid:
            if tid not in known:
                raise ValueError("unknown topic")
            rec = {**self.store.data.get("topics", {}).get(tid, {}), "title": title}
        else:
            if any(t["title"] == title for t in known.values()):
                raise ValueError("a topic with this name exists")
            tid = "custom|" + title
            rec = {"title": title, "area": str(body.get("area") or "Vlastní skupiny")[:60], "custom": True,
                   "touched_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        self.store.put(tid, rec, section="topics")
        return {**self.state(), "id": tid}

    def _record(self, body: dict, offered_list: list, question: str, status: str) -> dict:
        offered = {o["entry"] for o in offered_list}
        record = {"status": status, "note": str(body.get("note") or "")[:500],
                  "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if status == "alternative":
            # an alternative can ALSO say the answer is missing or incomplete:
            # the question then lands on both lists (pack + answers to write)
            record["missing_too"] = bool(body.get("missing_too"))
            entry = str(body.get("entry") or "")
            wording = str(body.get("wording") or question).strip()[:300]
            if entry not in offered:
                raise ValueError("the alternative must go to an offered entry")
            if not wording:
                raise ValueError("empty wording")
            e = next(o for o in offered_list if o["entry"] == entry)
            record.update({"entry": entry, "entry_q": e["q"], "pack": e.get("pack", ""),
                           "wording": wording})
        elif status == "ambiguous":
            entries = [str(x) for x in (body.get("entries") or [])]
            if any(x not in offered for x in entries):
                raise ValueError("ambiguous: only offered entries")
            record["entries"] = entries
        return record

    def decide(self, body: dict) -> dict:
        g = self.by_key.get(str(body.get("key", "")))
        status = body.get("status")
        if g is None or status not in QUEUE_STATUSES:
            raise ValueError("unknown group or status")
        self.store.put(g["key"], self._record(body, g["offered"], g["question"], status))
        return self.decision(g["key"])

    def topic_offers(self, tid: str, assign: dict | None = None) -> list[dict]:
        """Entries offered to any group of the topic, best score first."""
        assign = assign or self.assign()
        best: dict[str, dict] = {}
        for g in self.groups:
            if assign[g["key"]] != tid:
                continue
            for o in g["offered"]:
                if o["entry"] not in best or o["score"] > best[o["entry"]]["score"]:
                    best[o["entry"]] = o
        return sorted(best.values(), key=lambda o: -o["score"])

    def decide_topic(self, body: dict) -> dict:
        tid, status = str(body.get("topic", "")), body.get("status")
        if tid not in self.topics() or status not in QUEUE_STATUSES:
            raise ValueError("unknown topic or status")
        record = self._record(body, self.topic_offers(tid), self.topics()[tid]["title"], status)
        if status == "alternative":
            record.pop("wording", None)              # one wording per subgroup, see additions()
            record["all_wordings"] = bool(body.get("all_wordings"))
        self.store.put(tid, record, section="topic_decisions")
        return self.state()

    def additions(self) -> list[dict]:
        """One row per group: topic, effective decision, the wording it adds.
        A topic's alternative adds the most frequent wording of each subgroup
        (the search finds the rest of a subgroup by itself), or every group's
        wording when the curator asked for all."""
        assign, sub, tops = self.assign(), self.subgroups(), self.topics()
        rep: dict[tuple, str] = {}
        for g in self.groups:                      # most frequent first
            rep.setdefault((assign[g["key"]], sub[g["key"]]), g["key"])
        rows = []
        for g in self.groups:
            d, where = self.effective(g["key"], assign)
            tid = assign[g["key"]]
            wording = ""
            if d["status"] == "alternative":
                if where == "group":
                    wording = d.get("wording", "")
                elif d.get("all_wordings") or rep[(tid, sub[g["key"]])] == g["key"]:
                    wording = g["question"]
            rows.append({"key": g["key"], "count": g["count"], "question": g["question"],
                         "topic": {LATER: "odloženo", ALONE: "vyjmuto ze skupiny"}.get(tid) or tops.get(tid, {}).get("title", tid),
                         "status": d["status"], "where": where, "entry_q": d.get("entry_q", ""),
                         "pack": d.get("pack", ""), "wording": wording,
                         "missing_too": bool(d.get("missing_too")), "note": d.get("note", "")})
        return rows

    def export_csv(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["skupina", "pocet", "dotaz", "stav", "rozhodnuto_za", "odpoved", "sada",
                    "pridat_alternativu", "chybi_odpoved", "poznamka"])
        names = {"group": "dotaz", "topic": "skupinu"}
        for r in self.additions():
            w.writerow([r["topic"], r["count"], r["question"], r["status"], names[r["where"]],
                        r["entry_q"], r["pack"], r["wording"],
                        "ano" if r["status"] == "missing" or r["missing_too"] else "", r["note"]])
        return buf.getvalue().encode("utf-8-sig")


class App:
    def __init__(self, args):
        self.source = Path(args.input).name
        self.subject = args.subject
        self.address = getattr(args, "osloveni", None) or "vy"
        if self.address not in ADDRESSES:
            raise SystemExit(f"--osloveni: one of {', '.join(ADDRESSES)}")
        items = load_items(args.input, args.sheet)
        self.store = Store(Path(args.state))
        self.items = []
        for it in items:
            questions = [it["canonical_q"], *it["alternatives"]]
            prop = neutralize(it["answer"], questions, it["sources"], subject=self.subject,
                              address=self.address)
            # a decision made before the address was a choice was made for vykání
            prop["proposed_vy"] = prop["proposed"] if self.address == "vy" else neutralize(
                it["answer"], questions, it["sources"], subject=self.subject)["proposed"]
            mach = None
            if machine.available():
                mach = {"answer": machine.analyse(it["answer"]),
                        "proposed": machine.analyse(prop["proposed"]),
                        "questions": [{"text": q, **(machine.analyse(q) or {})} for q in questions]}
            self.items.append({**it, **prop, "answer_sha": _sha(it["answer"]), "machine": mach})
        self.by_id = {it["id"]: it for it in self.items}
        self.has_category_code = any((it.get("pack") or {}).get("category_code") is not None
                                     for it in self.items)
        self.queue = None
        if getattr(args, "queue", None):
            st = Path(args.state)
            tp = getattr(args, "topics", None)
            self.queue = QueueReview(Path(args.queue), st.with_name(st.stem + ".queue.json"),
                                     Path(tp) if tp else None)

    def decision(self, it: dict) -> dict:
        d = self.store.get(it["id"])
        if not d:
            return {"status": "pending", "final": None, "note": "", "stale": False}
        stale = d.get("answer_sha") != it["answer_sha"]
        # approved as proposed, and the proposal is another text now (another
        # address): the approval was for that text, not for this one
        approved_text = d.get("proposed", it["proposed_vy"])
        readdressed = (not stale and d.get("status") == "approved" and not d.get("final")
                       and approved_text != it["proposed"])
        out = {**d, "stale": stale or readdressed, "readdressed": readdressed}
        out["status"] = "pending" if out["stale"] else d.get("status", "pending")
        return out

    def payload(self) -> dict:
        return {"meta": {"source": self.source, "subject": self.subject, "address": ADDRESSES[self.address],
                         "count": len(self.items),
                         "machine": machine.available(), "awaiting_review": self.awaiting_review(),
                         "queue": len(self.queue.groups) if self.queue else None},
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
                  "proposed": it["proposed"], "address": self.address,
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
            if path in ("/", "/index.html", "/queue.html"):
                name = "queue.html" if path == "/queue.html" else "index.html"
                return self._send(200, (static / name).read_bytes(), "text/html; charset=utf-8")
            if path == "/style.css":
                return self._send(200, (static / "style.css").read_bytes(), "text/css; charset=utf-8")
            if path == "/api/queue":
                if not app.queue:
                    return self._json(404, {"error": "started without --queue"})
                return self._json(200, app.queue.payload())
            if path == "/api/items":
                return self._json(200, app.payload())
            if path == "/export/queue.csv":
                if not app.queue:
                    return self._json(404, {"error": "started without --queue"})
                name = f"{Path(app.source).stem}-dotazy-z-provozu.csv"
                return self._send(200, app.queue.export_csv(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path in ("/export/review.csv", "/export/pack.csv"):
                kind = "pack" if "pack" in path else "review"
                wait = app.awaiting_review() if kind == "pack" else 0
                # the file name says it when rewrites are still unreviewed
                name = f"{Path(app.source).stem}-{kind}{f'-ceka-{wait}' if wait else ''}.csv"
                return self._send(200, app.export(kind), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            queue_api = {"/api/queue/decision": "decide", "/api/queue/topic-decision": "decide_topic",
                         "/api/queue/assign": "move", "/api/queue/topic": "save_topic"}
            if self.path != "/api/decision" and self.path not in queue_api:
                return self._json(404, {"error": "not found"})
            if self.path in queue_api and not app.queue:
                return self._json(404, {"error": "started without --queue"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > _MAX_BODY:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body size"})
            try:
                body = json.loads(self.rfile.read(n))
                if self.path in queue_api:
                    return self._json(200, getattr(app.queue, queue_api[self.path])(body))
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
    p.add_argument("--osloveni", default="vy", choices=sorted(ADDRESSES),
                   help="address: vy = tykání -> vykání (default), ty = vykání -> tykání, beze = unchanged")
    p.add_argument("--queue", help="questions from the log (log_queue.py --out queue.json)")
    p.add_argument("--topics", help="topics of the queue (topics.py --out topics.json)")
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
