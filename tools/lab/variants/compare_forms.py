"""Lemmatization or stemming? The curated-QA path under three index forms.

Runs INSIDE the edu_ai_search container like measure.py (the real nodes, the
live synonyms and stop-words, one transaction that is ROLLED BACK). The school's
packs go in as temporary packs; every test question goes through
prepare → qa_exact → qa_lexical three times, with the steps' "form" set to

  L   ["norm", "lemma"]          lemmatization only
  S   ["norm", "stem"]           stemming only
  LS  ["norm", "lemma", "stem"]  both (the shipped setting)

All other step params (thresholds, synonyms, IDF) stay as the active setting
has them — calibrated for LS, which the report states.

Test questions: written paraphrases with a known right answer (the question
they were written from) — categories gramatika, hovorove, zdvorile,
nezdvorile, klicova_slova, tvary, jina_slova — plus generated ones: the
original question (control), without diacritics, with a typo.

For every miss the analysis records WHY, to find data to add:
  * a query word the right answer's wording lacks, also through the synonyms
    (candidate synonym / alternative)
  * a word whose lemma and stem disagree with the right answer's (a form the
    lemmatizer or the stemmer gets wrong)
  * a confident WRONG answer (two curated questions the search confuses)

  python compare_forms.py --questions otazky.json --paraphrases prepisy.json --out /out
                          [--with-sql what_if.sql]   # e.g. new stop-words, rolled back too
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure                             # noqa: E402  (transaction, cursor patch, _ask)
import generators                          # noqa: E402

from app import config                     # noqa: E402
from app.settings import get_active_setting  # noqa: E402
from app.nodes.qa_common import load_synonym_groups, _COVERAGE_STOP  # noqa: E402
from eduai_normalize import build_forms    # noqa: E402

FORMS = {"L": ["norm", "lemma"], "S": ["norm", "stem"], "LS": ["norm", "lemma", "stem"]}
# a confident WRONG answer costs more than a right one gains: it says nothing
# like "Nechtěl ses zeptat…?", the asker takes it for the answer
SCORE = {"přesná shoda": 1.0, "odpověď": 1.0, "v nabídce": 0.5, "ŠPATNÁ odpověď": -3.0}
ORDER = ["přesná shoda", "odpověď", "v nabídce", "nabídka bez správné",
         "nic (C) – v záloze", "nic (C)", "ŠPATNÁ odpověď"]
# interrogatives carry the question type, not its topic: not a missing "word"
_QWORDS = {"kdy", "jak", "kde", "co", "kolik", "kdo", "jaky", "ktery", "proc", "cim", "kam", "odkud"}


def _insert(cur, questions, entity, cfg, thresholds):
    """The school's packs as temporary packs; {idx: entry_id}, {pack: pack_id}."""
    t_answer, t_suggest, m_margin = thresholds
    pack_ids, ids = {}, {}
    for pack in sorted({q["pack"] for q in questions}):
        pid = f"lab_forms_{pack}"
        pack_ids[pack] = pid
        cur.execute(
            f"INSERT INTO {config.QA_MANIFEST_TABLE} (pack_id, lang, version, schema_version,"
            f" legal_entity_code, visibility, is_active, t_answer, t_suggest, m_margin, entry_count)"
            f" VALUES (%s,'cs',1,1,%s,'entity',true,%s,%s,%s,%s)",
            (pid, entity, t_answer, t_suggest, m_margin, sum(q["pack"] == pack for q in questions)))
    for q in questions:
        eid = str(uuid.uuid4())
        ids[q["idx"]] = eid
        cur.execute(
            f"INSERT INTO {config.QA_ENTRIES_TABLE} (entry_id, pack_id, entry_key, canonical_q,"
            f" answer_md, legal_entity_code, visibility, is_active)"
            f" VALUES (%s,%s,%s,%s,%s,%s,'entity',true)",
            (eid, pack_ids[q["pack"]], q["entry_key"] or f"k{q['idx']}", q["q"], "(test)", entity))
        for phr in [q["q"], *q["alts"]]:
            f = build_forms(phr, cfg, "cs")
            cur.execute(
                f"INSERT INTO {config.QA_VARIANTS_TABLE} (entry_id, phrasing, norm_text,"
                f" lemma_text, stem_text, legal_entity_code, visibility, is_active)"
                f" VALUES (%s,%s,%s,%s,%s,%s,'entity',true)",
                (eid, phr, " ".join(f["norm"]), " ".join(f["lemma"]), " ".join(f["stem"]), entity))
    return ids, pack_ids


def _tests(questions, paraphrases):
    """(idx, category, text): written paraphrases + control + generated."""
    by_idx = defaultdict(list)
    for p in paraphrases:
        by_idx[p["idx"]].append(p)
    out = []
    for q in questions:
        out.append((q["idx"], "puvodni", q["q"]))
        mine = by_idx.get(q["idx"], [])
        for p in mine:
            out.append((q["idx"], p["category"], p["text"]))
        talk = next((p["text"] for p in mine if p["category"] == "hovorove"), None)
        if talk:
            nd = generators.strip_diacritics(talk).lower()
            if nd != talk.lower():
                out.append((q["idx"], "bez_diakritiky", nd))
        ty = generators.typos(q["q"], str(q["idx"]))
        if ty:
            out.append((q["idx"], "preklep", ty[0][2]))
    return out


class Words:
    """Per-word forms and the synonym groups, for the analysis of misses."""

    def __init__(self, cfg, cur):
        self.cfg, self.cache = cfg, {}
        groups, _kind, _phr, _terms = load_synonym_groups(cur, "cs")
        self.groups_of = defaultdict(set)
        self.tokens = groups
        for gid, toks in groups.items():
            for t in toks:
                self.groups_of[t].add(gid)

    def forms(self, word):
        if word not in self.cache:
            f = build_forms(word, self.cfg, "cs")
            self.cache[word] = (f["lemma"][0] if f["lemma"] else "", f["stem"][0] if f["stem"] else "")
        return self.cache[word]

    def covered(self, lemma, entry_tokens):
        """Directly or through a synonym group."""
        if lemma in entry_tokens:
            return True
        return any(self.tokens[g] & entry_tokens for g in self.groups_of.get(lemma, ()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--paraphrases", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-written", action="store_true",
                    help="no generated tests (original, without diacritics, typo): an independent set")
    ap.add_argument("--configs", help='JSON {name: {"form": [...], <other qa_lexical params>}} '
                    "instead of the three forms")
    ap.add_argument("--with-sql", action="append", default=[], metavar="FILE",
                    help="run this SQL inside the measuring transaction first (rolled back)")
    a = ap.parse_args()
    global FORMS
    configs = {n: {"form": f} for n, f in FORMS.items()}
    if a.configs:
        configs = json.loads(a.configs)
        FORMS = {n: c.get("form", ["norm", "lemma", "stem"]) for n, c in configs.items()}
    import logging
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    questions = json.load(open(a.questions, encoding="utf-8"))
    paraphrases = json.load(open(a.paraphrases, encoding="utf-8"))
    if a.limit:
        questions = questions[:a.limit]
    q_by_idx = {q["idx"]: q for q in questions}
    setting = get_active_setting(a.entity)
    cfg = setting["config"]
    p_exact, _ = measure._step_params(cfg, "qa_exact")
    p_lex, _ = measure._step_params(cfg, "qa_lexical")
    thresholds = (float(p_lex.get("t_answer", config.QA_T_ANSWER)),
                  float(p_lex.get("t_suggest", config.QA_T_SUGGEST)),
                  float(p_lex.get("m_margin", config.QA_M_MARGIN)))

    import psycopg2
    measure._conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                                     user=os.environ.get("LAB_PG_USER", config.PG_USER),
                                     password=os.environ.get("LAB_PG_PASSWORD", config.PG_PASSWORD))
    measure._conn.autocommit = False
    measure._patch_cursors()
    rows, misses = [], []
    try:
        with measure._conn.cursor() as cur:
            if os.environ.get("LAB_PG_ROLE"):
                cur.execute("SET LOCAL ROLE " + os.environ["LAB_PG_ROLE"])
            for path in a.with_sql:
                with open(path, encoding="utf-8") as fh:
                    cur.execute(fh.read())
                print(f"[forms] provedeno v transakci: {os.path.basename(path)}", file=sys.stderr)
            measure.search_path_as_search_role(cur)
            ids, pack_ids = _insert(cur, questions, a.entity, cfg, thresholds)
            words = Words(cfg, cur)
        q_of_eid = {ids[i]: q_by_idx[i]["q"] for i in ids}
        # the right answer's wording, as index tokens (lemma and stem), for the analysis
        entry_tok = {}
        for q in questions:
            lem, st = set(), set()
            for phr in [q["q"], *q["alts"]]:
                f = build_forms(phr, cfg, "cs")
                lem |= set(f["lemma"]) | set(f["norm"])
                st |= set(f["stem"])
            entry_tok[q["idx"]] = (lem, st)
        tests = _tests(questions, paraphrases)
        if a.only_written:
            tests = [t for t in tests if t[1] not in ("puvodni", "bez_diakritiky", "preklep")]
        print(f"[forms] {len(questions)} otázek, {len(tests)} testovacích dotazů × {len(FORMS)} nastavení; "
              f"setting {setting['setting_id']}; prahy {thresholds}", file=sys.stderr)
        for n, (idx, cat, text) in enumerate(tests, 1):
            want = ids[idx]
            row = {"idx": idx, "sada": q_by_idx[idx]["pack"], "kategorie": cat, "dotaz": text,
                   "spravna_otazka": q_by_idx[idx]["q"]}
            for name, form in FORMS.items():
                r = _ask_list(text, a.entity, cfg, {**p_exact, "form": form},
                              {**p_lex, **configs[name], "form": form}, list(pack_ids.values()))
                verdict = measure._verdict("B1", r, want)
                row[f"{name}_vysledek"] = verdict
                row[f"{name}_nalezeno"] = q_of_eid.get(r.get("entry")) or ""
                row[f"{name}_pokryti"] = r.get("score")
                if name == next(iter(FORMS)):
                    row["lemma"] = " ".join(r["tokens"]["lemma"] or [])
                    row["stem"] = " ".join(r["tokens"]["stem"] or [])
                if verdict not in ("přesná shoda", "odpověď"):
                    misses.append({"config": name, "idx": idx, "kategorie": cat, "dotaz": text,
                                   "verdict": verdict, "found": row[f"{name}_nalezeno"],
                                   "norm": r["tokens"]["norm"] or []})
            rows.append(row)
            if n % 200 == 0:
                print(f"[forms] {n}/{len(tests)}", file=sys.stderr)
    finally:
        measure._conn.rollback()
        measure._conn.close()
        print("[forms] ROLLBACK — v databázi nic nezůstalo", file=sys.stderr)

    os.makedirs(a.out, exist_ok=True)
    _write_csv(os.path.join(a.out, "vysledky.csv"), rows)

    # ── summary: config × category ──
    summary = {name: defaultdict(Counter) for name in FORMS}
    for r in rows:
        for name in FORMS:
            summary[name][r["kategorie"]][r[f"{name}_vysledek"]] += 1
            summary[name]["CELKEM"][r[f"{name}_vysledek"]] += 1
    cats = ["CELKEM"] + sorted({r["kategorie"] for r in rows})
    table = []
    for cat in cats:
        line = {"kategorie": cat, "dotazu": sum(summary[next(iter(FORMS))][cat].values())}
        for name in FORMS:
            c, tot = summary[name][cat], sum(summary[name][cat].values()) or 1
            line[f"{name}_spravne_jiste_%"] = round(100 * (c["přesná shoda"] + c["odpověď"]) / tot, 1)
            line[f"{name}_v_nabidce_%"] = round(100 * c["v nabídce"] / tot, 1)
            line[f"{name}_spatne_jiste_%"] = round(100 * c["ŠPATNÁ odpověď"] / tot, 1)
            line[f"{name}_nic_%"] = round(100 * (c["nic (C)"] + c["nic (C) – v záloze"] + c["nabídka bez správné"]) / tot, 1)
            line[f"{name}_skore"] = round(sum(SCORE.get(k, 0) * v for k, v in c.items()), 1)
        table.append(line)
    _write_csv(os.path.join(a.out, "souhrn.csv"), table)

    # ── the analysis of misses (all three configs; a word counted once per query) ──
    missing = defaultdict(lambda: {"n": 0, "configs": Counter(), "examples": [], "uncovered": Counter()})
    extra = defaultdict(lambda: {"n": 0, "questions": set(), "examples": []})
    form_issues = defaultdict(lambda: {"n": 0, "examples": []})
    confusions = defaultdict(lambda: {"n": 0, "configs": Counter(), "examples": []})
    seen_q = set()
    for m in misses:
        lem_set, stem_set = entry_tok[m["idx"]]
        right_q = q_by_idx[m["idx"]]["q"]
        if m["verdict"] == "ŠPATNÁ odpověď":
            c = confusions[(right_q, m["found"])]
            c["n"] += 1
            c["configs"][m["config"]] += 1
            if len(c["examples"]) < 3 and m["dotaz"] not in c["examples"]:
                c["examples"].append(m["dotaz"])
        key = (m["idx"], m["dotaz"])
        first = key not in seen_q
        seen_q.add(key)
        # the right question's content words the query does NOT have: if there
        # are some, a missing query word may be their synonym; if none, the
        # query word is simply EXTRA (nothing in the question to pair it with)
        q_lem = set()
        for w in m["norm"]:
            lm, _ = words.forms(w)
            q_lem |= {lm, w}
        canon = build_forms(right_q, cfg, "cs")["lemma"]
        uncovered = [t for t in canon if t not in _COVERAGE_STOP and t not in _QWORDS
                     and not words.covered(t, q_lem)]
        for w in m["norm"]:
            lem, stem = words.forms(w)
            if not lem or lem in _COVERAGE_STOP or lem in _QWORDS or w in _QWORDS or lem.isdigit():
                continue
            if words.covered(lem, lem_set) or words.covered(w, lem_set):
                continue
            in_stem = stem and stem in stem_set
            if in_stem:
                # the stem meets the right answer, the lemma does not: a form issue
                fi = form_issues[(w, lem, stem)]
                if first:
                    fi["n"] += 1
                    if len(fi["examples"]) < 3:
                        fi["examples"].append(f"{m['dotaz']} → {right_q}")
                continue
            if not uncovered:
                ex = extra[lem]
                if first:
                    ex["n"] += 1
                    ex["questions"].add(right_q)
                    if len(ex["examples"]) < 3:
                        ex["examples"].append(f"{m['dotaz']} → {right_q}")
                continue
            mi = missing[(lem, right_q)]
            mi["configs"][m["config"]] += 1
            if first:
                mi["n"] += 1
                mi["uncovered"].update(uncovered)
                if len(mi["examples"]) < 3:
                    mi["examples"].append(m["dotaz"])
    _write_csv(os.path.join(a.out, "analyza_mozna_synonyma.csv"), sorted(
        [{"slovo_z_dotazu (lemma)": k[0], "spravna_otazka": k[1], "pocet_dotazu": v["n"],
          "slova_otazky_bez_protejsku": " ".join(t for t, _ in v["uncovered"].most_common(4)),
          "nastaveni": " ".join(f"{c}:{n}" for c, n in sorted(v["configs"].items())),
          "priklady": " | ".join(v["examples"])} for k, v in missing.items()],
        key=lambda r: -r["pocet_dotazu"]))
    _write_csv(os.path.join(a.out, "analyza_slova_navic.csv"), sorted(
        [{"slovo_z_dotazu (lemma)": k, "pocet_dotazu": v["n"], "ruznych_otazek": len(v["questions"]),
          "priklady": " | ".join(v["examples"])} for k, v in extra.items()],
        key=lambda r: -r["pocet_dotazu"]))
    _write_csv(os.path.join(a.out, "analyza_tvary.csv"), sorted(
        [{"slovo": k[0], "lemma": k[1], "kmen": k[2], "pocet_dotazu": v["n"],
          "priklady": " | ".join(v["examples"])} for k, v in form_issues.items()],
        key=lambda r: -r["pocet_dotazu"]))
    _write_csv(os.path.join(a.out, "analyza_zameny.csv"), sorted(
        [{"spravna_otazka": k[0], "chat_odpovedel": k[1], "pocet": v["n"],
          "nastaveni": " ".join(f"{c}:{n}" for c, n in sorted(v["configs"].items())),
          "priklady": " | ".join(v["examples"])} for k, v in confusions.items()],
        key=lambda r: -r["pocet"]))

    print("\nkategorie            dotazů |  " + " | ".join(f"{n:^33}" for n in FORMS))
    print(" " * 28 + "|  " + " | ".join(f"{'jistě ✓':>8}{'nabídka':>9}{'špatně':>8}{'skóre':>8}" for _ in FORMS))
    for line in table:
        cells = " | ".join(f"{line[f'{n}_spravne_jiste_%']:>7}%{line[f'{n}_v_nabidce_%']:>8}%{line[f'{n}_spatne_jiste_%']:>7}%"
                           f"{line[f'{n}_skore']:>8}" for n in FORMS)
        print(f"{line['kategorie']:<20} {line['dotazu']:>6} |  {cells}")
    print(f"\nVýstup: {a.out}/vysledky.csv, souhrn.csv, analyza_*.csv")
    return 0


def _ask_list(question, entity, cfg, p_exact, p_lex, packs):
    """measure._ask over the school's packs instead of one workbook pack."""
    from app.nodes.prepare import prepare
    from app.nodes import qa_exact as qa_exact_mod, qa_lexical as qa_lexical_mod
    state = {"user_question": question, "legal_entity": entity,
             "entity_scope": [entity], "organization_entities": [entity],
             "pack_ids": packs, "offer_pack_ids": []}
    state.update(prepare(state, cfg) or {})
    toks = {"norm": state.get("query_norm_tokens"), "lemma": state.get("query_lemma_tokens"),
            "stem": state.get("query_stem_tokens")}
    out = qa_exact_mod.qa_exact(state, {**p_exact, "related_suggestions": False})
    if out.get("resolved"):
        return {"decision": "exact", "entry": out.get("matched_entry_id"), "score": 1.0,
                "suggestions": [], "tokens": toks}
    res = qa_lexical_mod._qa_lexical_decision(state, p_lex) or {}
    dec = res.get("decision") or "C"
    sugg = [s.get("entry_id") for s in
            (res.get("suggestions") or (res.get("deferred_suggest") or {}).get("suggestions")
             or res.get("fallback_suggestions") or [])]
    top = res.get("matched_entry_id") or (res.get("deferred_suggest") or {}).get("matched_entry_id")
    return {"decision": dec, "entry": top, "score": res.get("match_score"),
            "margin": res.get("match_margin"), "suggestions": sugg, "tokens": toks}


def _write_csv(path, rows):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
