"""Measure how the curated-QA path copes with rule-based question variants.

Runs INSIDE the edu_ai_search container, against the real database, with the
real nodes: prepare → qa_exact → qa_lexical, the active setting's step params,
the live synonyms and stop-words. Nothing is persisted:

  * one connection, one transaction; the QA workbook is inserted as a
    temporary pack and every node's get_cursor is pointed at that connection
  * the transaction is ROLLED BACK at the end (also on error / Ctrl+C), so no
    other connection ever sees the rows

qa_vector (embeddings) and the document path are NOT measured: they need the
OpenAI key and documents, and the question here is what the lexical side does.

Index forms are built with eduai_normalize.build_forms and the active setting's
normalization config — the same library and stoplist the Langflow QA ingest
uses (normalize_tokens / lemma_tokens / stem_tokens).

  python measure.py --input faq.xlsx --out /out [--entity CODE] [--limit N]
                    [--with-sql postgres/migrations/217_….sql]   # a migration, rolled back too
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import uuid
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neutral_answers"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xlsx_read import load_items          # noqa: E402  (stdlib-only reader)
import generators                         # noqa: E402

from app import config, db                # noqa: E402
from app.settings import get_active_setting  # noqa: E402
from app.nodes.prepare import prepare     # noqa: E402
from app.nodes import qa_exact as qa_exact_mod, qa_lexical as qa_lexical_mod  # noqa: E402
from eduai_normalize import build_forms   # noqa: E402

PACK = "lab_variants_measure"

# ── one connection, one transaction ──────────────────────────────────────────
_conn = None


@contextlib.contextmanager
def _tx_cursor():
    """get_cursor() stand-in: same connection, never commits.

    A savepoint per block, so a failing statement inside a node (which the node
    catches and falls through on) does not abort the whole transaction.
    """
    with _conn.cursor() as cur:
        cur.execute("SAVEPOINT lab")
        try:
            yield cur
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT lab")
            raise
        cur.execute("RELEASE SAVEPOINT lab")


def search_path_as_search_role(cur):
    """After `SET LOCAL ROLE` (what-if SQL as the owner) the queries must resolve names as
    the search role does: its search_path holds the schema with pg_trgm, the owner's does
    not, and the trigram fallback then fails with "operator does not exist: text <-> unknown"
    (the dead-end chips silently vanish). A no-op for a run without a role switch."""
    if os.environ.get("LAB_PG_ROLE"):
        cur.execute(f"SET LOCAL search_path TO {config.PG_SCHEMA}, public")


def _patch_cursors():
    orig = db.get_cursor          # captured first: app.db itself is in the loop
    for name, mod in list(sys.modules.items()):
        if name.startswith("app") and getattr(mod, "get_cursor", None) is orig:
            mod.get_cursor = _tx_cursor


# ── load the workbook as a temporary pack ────────────────────────────────────
def _insert_pack(cur, items, entity, cfg, thresholds):
    t_answer, t_suggest, m_margin = thresholds
    cur.execute(
        f"INSERT INTO {config.QA_MANIFEST_TABLE} (pack_id, lang, version, schema_version,"
        f" legal_entity_code, visibility, is_active, t_answer, t_suggest, m_margin, entry_count)"
        f" VALUES (%s,'cs',1,1,%s,'entity',true,%s,%s,%s,%s)",
        (PACK, entity, t_answer, t_suggest, m_margin, len(items)))
    ids = {}
    n_var = 0
    for it in items:
        eid = str(uuid.uuid4())
        ids[it["id"]] = eid
        cur.execute(
            f"INSERT INTO {config.QA_ENTRIES_TABLE} (entry_id, pack_id, entry_key, canonical_q,"
            f" answer_md, legal_entity_code, visibility, is_active)"
            f" VALUES (%s,%s,%s,%s,%s,%s,'entity',true)",
            (eid, PACK, it["id"], it["canonical_q"], it["answer"], entity))
        for phr in [it["canonical_q"], *it["alternatives"]]:
            f = build_forms(phr, cfg, "cs")
            cur.execute(
                f"INSERT INTO {config.QA_VARIANTS_TABLE} (entry_id, phrasing, norm_text,"
                f" lemma_text, stem_text, legal_entity_code, visibility, is_active)"
                f" VALUES (%s,%s,%s,%s,%s,%s,'entity',true)",
                (eid, phr, " ".join(f["norm"]), " ".join(f["lemma"]), " ".join(f["stem"]), entity))
            n_var += 1
    return ids, n_var


def _step_params(cfg, name):
    for st in cfg.get("pipeline") or []:
        if st.get("step") == name:
            return dict(st.get("params") or {}), st.get("enabled", True)
    return {}, False


# ── one query through the real nodes ─────────────────────────────────────────
def _ask(question, entity, cfg, p_exact, p_lex):
    state = {"user_question": question, "legal_entity": entity,
             "entity_scope": [entity], "organization_entities": [entity],
             "pack_ids": [PACK], "offer_pack_ids": []}
    state.update(prepare(state, cfg) or {})
    toks = {"norm": state.get("query_norm_tokens"), "lemma": state.get("query_lemma_tokens"),
            "stem": state.get("query_stem_tokens")}
    out = qa_exact_mod.qa_exact(state, {**p_exact, "related_suggestions": False})
    if out.get("resolved"):
        return {"decision": "exact", "entry": out.get("matched_entry_id"),
                "score": 1.0, "suggestions": [], "tokens": toks}
    res = qa_lexical_mod._qa_lexical_decision(state, p_lex) or {}
    dec = res.get("decision") or "C"
    sugg = [s.get("entry_id") for s in
            (res.get("suggestions") or (res.get("deferred_suggest") or {}).get("suggestions")
             or res.get("fallback_suggestions") or [])]
    # B-deferred keeps its top entry inside the parked suggestion result
    top = res.get("matched_entry_id") or (res.get("deferred_suggest") or {}).get("matched_entry_id")
    return {"decision": dec, "entry": top,
            "score": res.get("match_score"), "margin": res.get("match_margin"),
            "suggestions": sugg, "tokens": toks}


def _verdict(technique, r, want):
    """Positive tests: did the RIGHT curated answer come back, and how surely?

    B4 is the negative test: the flipped question asks the opposite, so the
    original answer returned as a confident answer is the failure.
    """
    d, hit = r["decision"], r.get("entry") == want
    in_sugg = want in (r.get("suggestions") or [])
    if technique == "B4":
        if d in ("exact", "A") and hit:
            return "stejná odpověď s jistotou"
        if d.startswith("B") and (hit or in_sugg):
            return "jen v nabídce"
        return "nenalezeno"
    if d in ("exact", "A"):
        return ("přesná shoda" if d == "exact" else "odpověď") if hit else "ŠPATNÁ odpověď"
    if d.startswith("B"):
        return "v nabídce" if (hit or in_sugg) else "nabídka bez správné"
    return "nic (C)" + (" – v záloze" if in_sugg else "")


ORDER_POS = ["přesná shoda", "odpověď", "v nabídce", "nabídka bez správné",
             "nic (C) – v záloze", "nic (C)", "ŠPATNÁ odpověď"]
ORDER_NEG = ["stejná odpověď s jistotou", "jen v nabídce", "nenalezeno"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    ap.add_argument("--limit", type=int, default=0, help="first N questions only")
    ap.add_argument("--with-sql", action="append", default=[], metavar="FILE",
                    help="run this SQL (e.g. a new migration) inside the measuring "
                         "transaction first — rolled back with everything else")
    ap.add_argument("--debug", action="store_true", help="node log lines to stderr")
    a = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.INFO if a.debug else logging.ERROR, stream=sys.stderr,
                        format="  %(name)s: %(message)s")

    items = load_items(a.input, a.sheet)
    if a.limit:
        items = items[:a.limit]
    global _conn
    # The search role is read-only (correctly); the rows go in as the role the
    # QA ingest itself writes with. Credentials come from the environment only.
    import psycopg2
    _conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                             user=os.environ.get("LAB_PG_USER", config.PG_USER),
                             password=os.environ.get("LAB_PG_PASSWORD", config.PG_PASSWORD))
    _conn.autocommit = False
    _patch_cursors()
    rows = []
    try:
        with _conn.cursor() as cur:
            if os.environ.get("LAB_PG_ROLE"):
                # migrator logs in, owner holds the rights; LOCAL = this transaction only
                cur.execute("SET LOCAL ROLE " + os.environ["LAB_PG_ROLE"])
            for path in a.with_sql:
                with open(path, encoding="utf-8") as fh:
                    cur.execute(fh.read())
                print(f"[measure] provedeno v transakci: {os.path.basename(path)}", file=sys.stderr)
            search_path_as_search_role(cur)
            # the settings are read AFTER the SQL: a migration that changes them (219, 220, 221) is
            # measured as written. force=True because the same transaction holds the change.
            setting = get_active_setting(a.entity, force=True)
            cfg = setting["config"]
            p_exact, on_exact = _step_params(cfg, "qa_exact")
            p_lex, on_lex = _step_params(cfg, "qa_lexical")
            thresholds = (float(p_lex.get("t_answer", config.QA_T_ANSWER)),
                          float(p_lex.get("t_suggest", config.QA_T_SUGGEST)),
                          float(p_lex.get("m_margin", config.QA_M_MARGIN)))
            ids, n_var = _insert_pack(cur, items, a.entity, cfg, thresholds)
        print(f"[measure] {len(items)} otázek, {n_var} uložených formulací "
              f"(dočasně, ROLLBACK na konci); setting {setting['setting_id']}; "
              f"qa_exact={'on' if on_exact else 'OFF'} qa_lexical={'on' if on_lex else 'OFF'}; "
              f"prahy A/B/marže = {thresholds}", file=sys.stderr)
        q_by_eid = {ids[it["id"]]: it["canonical_q"] for it in items}
        for n, it in enumerate(items, 1):
            want = ids[it["id"]]
            tests = [("B0", "původní otázka", it["canonical_q"])]
            tests += generators.all_variants(it["canonical_q"], it["id"])
            for tech, label, text in tests:
                r = _ask(text, a.entity, cfg, p_exact, p_lex)
                rows.append({
                    "id": it["id"], "technika": tech, "pravidlo": label,
                    "otazka_puvodni": it["canonical_q"], "varianta": text,
                    "vysledek": _verdict(tech, r, want), "rozhodnuti": r["decision"],
                    "pokryti": r.get("score"), "marze": r.get("margin"),
                    "nalezena_otazka": q_by_eid.get(r.get("entry")) or "",
                    "norm": " ".join(r["tokens"]["norm"] or []),
                    "lemma": " ".join(r["tokens"]["lemma"] or []),
                    "stem": " ".join(r["tokens"]["stem"] or []),
                })
            if n % 10 == 0:
                print(f"[measure] {n}/{len(items)}", file=sys.stderr)
    finally:
        _conn.rollback()
        _conn.close()
        print("[measure] ROLLBACK — v databázi nic nezůstalo", file=sys.stderr)

    # ── outputs ──
    os.makedirs(a.out, exist_ok=True)
    base = os.path.splitext(os.path.basename(a.input))[0]
    with open(os.path.join(a.out, f"{base}.variants.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    summary = defaultdict(Counter)
    by_rule = defaultdict(Counter)
    for r in rows:
        summary[r["technika"]][r["vysledek"]] += 1
        by_rule[(r["technika"], r["pravidlo"])][r["vysledek"]] += 1
    with open(os.path.join(a.out, f"{base}.summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"input": base, "questions": len(items), "thresholds": thresholds,
                   "by_technique": {k: dict(v) for k, v in summary.items()},
                   "by_rule": {f"{k[0]} {k[1]}": dict(v) for k, v in by_rule.items()}},
                  fh, ensure_ascii=False, indent=2)

    names = {"B0": "B0 původní otázka (kontrola)", "B1": "B1 synonyma",
             "B2": "B2 bez diakritiky", "B3": "B3 překlepy", "B4": "B4 otočený zápor (negativní test)",
             "B5": "B5 pravopisné chyby"}
    for tech in sorted(summary):
        c = summary[tech]
        tot = sum(c.values())
        print(f"\n{names.get(tech, tech)} — {tot} variant")
        for k in (ORDER_NEG if tech == "B4" else ORDER_POS):
            if c.get(k):
                print(f"  {k:<28} {c[k]:>4}  {100 * c[k] / tot:5.1f} %")
    print("\nPodle pravidel (B1, B3, B5):")
    for (tech, label), c in sorted(by_rule.items()):
        if tech not in ("B1", "B3", "B5"):
            continue
        tot = sum(c.values())
        good = c.get("přesná shoda", 0) + c.get("odpověď", 0)
        print(f"  {tech} {label:<34} {tot:>4} variant, odpověď {100 * good / tot:5.1f} %, "
              f"v nabídce {100 * c.get('v nabídce', 0) / tot:5.1f} %")
    print(f"\nVýstup: {a.out}/{base}.variants.csv, {base}.summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
