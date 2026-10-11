#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-03).
"""Compare two related-chip settings on REAL questions from the log.

Replays each question through prepare -> route -> qa_exact -> qa_lexical by
calling the nodes DIRECTLY. Two consequences, both on purpose: nothing is written
(no log row, no idempotency claim) and no LLM runs, because only QA-answered
questions are sampled and those never reach generation.

Edit MODES to whatever pair is being decided; the first key is the baseline the
rest are diffed against. What to read is the PRIDANO/ODEBRANO lists, not the
counts — three settings were rejected here because the count looked fine and the
questions underneath it did not:

  a higher t_suggest         cut good and bad at the same rate; a right and a
                             wrong chip routinely score identically (0.3482)
  a top-concept gate         cut the BEST chips, which are siblings differing in
                             exactly that concept ("…z matematiky" -> "…z cestiny")
  a below-threshold backfill filled every hole with junk ("Jak funguje odhlasovani
                             obedu?" -> "Jak funguje dalsi kolo prijimaciho rizeni?")
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from app.db import get_cursor                                    # noqa: E402
from app.settings import organization_entities, get_active_setting  # noqa: E402
from app.nodes.prepare import prepare                             # noqa: E402
from app.nodes.route import route                                 # noqa: E402
from app.nodes.qa_exact import qa_exact                           # noqa: E402
from app.nodes.qa_lexical import qa_lexical                       # noqa: E402

# One row per DISTINCT question: the same question asked twenty times would
# otherwise weight the result by traffic, and what is being calibrated is the
# ranking, not the popularity.
#
# MOST RECENT first, and the DISTINCT ON is wrapped for it. Until 2026-09-04 the
# LIMIT applied to `ORDER BY lower(raw_query)`, i.e. the alphabetically first N
# questions — so every measurement in this file's history saw A through K and
# nothing after it, and the case being investigated ("V kolik se otevírá škola?")
# could not appear in its own measurement.
# The first key is the baseline every other mode is diffed against.
MODES = {"dnes": {"related_cross_pack_min": 0.0},
         "cizí pack >= 0.5": {"related_cross_pack_min": 0.5},
         "cizí pack >= 0.7": {"related_cross_pack_min": 0.7}}

_SAMPLE_SQL = """
SELECT raw_query, legal_entity_code, user_role, user_school_grade, user_id
  FROM (
    SELECT DISTINCT ON (lower(raw_query))
           raw_query, legal_entity_code, user_role, user_school_grade, user_id,
           created_at
      FROM edu_ai.question_answer_log
     WHERE answer_source IN ('qa_exact', 'qa_lexical')
       AND raw_query IS NOT NULL AND length(trim(raw_query)) > 0
       AND legal_entity_code IS NOT NULL
     ORDER BY lower(raw_query), created_at DESC
  ) d
 ORDER BY created_at DESC
 LIMIT %s
"""


def lexical_params(entity):
    """The production qa_lexical step params — the ranking under measurement."""
    for step in (get_active_setting(entity)["config"].get("pipeline") or []):
        if step.get("step") == "qa_lexical":
            return dict(step.get("params") or {})
    return {}


def chips(state, params):
    """The chips this question would come back with, as (score, question).

    qa_exact FIRST, exactly as the cascade runs it — most curated answers are
    exact hits and their chips are built on that path, so measuring qa_lexical
    alone measured a path the user rarely reaches.
    """
    ex = qa_exact(dict(state), params) or {}
    items = ex.get("suggestions") if ex.get("resolved") else None
    if items is None:
        res = qa_lexical(dict(state), params) or {}
        items = (res.get("suggestions")
                 or (res.get("deferred_suggest") or {}).get("suggestions") or [])
    return [(s.get("score") or 0.0, s.get("question")) for s in items
            if s.get("kind") == "qa"]


def main(argv):
    limit = int(argv[0]) if argv else 80
    with get_cursor() as cur:
        cur.execute(_SAMPLE_SQL, (limit,))
        rows = cur.fetchall()
    print(f"{len(rows)} distinct questions sampled from question_answer_log\n")

    measured = []          # (question, {mode: [(score, chip), …]})
    for q, ent, role, grade, uid in rows:
        org = organization_entities(ent)
        if not org:
            continue
        # mirrors server.py's initial state — entity_scope and organization_entities
        # are resolved there, BEFORE the cascade, so a replay that skips them
        # searches with no scope and every question comes back empty
        state = {"user_question": q, "legal_entity": ent, "entity_scope": [ent],
                 "organization_entities": org, "user_id": uid,
                 "user_role": role, "user_school_grade": grade}
        try:
            state.update(prepare(state) or {})
            state.update(route(state) or {})
            if state.get("resolved"):
                continue
            base = lexical_params(ent)
            got = {m: chips(state, {**base, **extra}) for m, extra in MODES.items()}
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {q[:60]!r}: {type(e).__name__}: {e}")
            continue
        if any(got.values()):
            measured.append((q, got))

    n = len(measured)
    print(f"{n} questions produced chips under at least one mode")
    print()
    print("── admission modes ───────────────────────────────────")
    print(f"{'mode':>12} {'chips':>7} {'avg/q':>7} {'q with 0 chips':>16}")
    for m in MODES:
        total = sum(len(g[m]) for _q, g in measured)
        empty = sum(1 for _q, g in measured if not g[m])
        print(f"{m:>12} {total:>7} {total / n:>7.2f} {empty:>10} / {n}")

    # WHICH chips each mode takes away, not just how many. A count cannot say
    # whether it removed the junk or the one good chip, and that is the question.
    base = list(MODES)[0]
    for m in list(MODES)[1:]:
        removed = [(sc, c, q) for q, g in measured
                   for sc, c in g[base] if (sc, c) not in g[m]]
        added = [(sc, c, q) for q, g in measured
                 for sc, c in g[m] if (sc, c) not in g[base]]
        removed.sort(key=lambda t: -t[0])
        added.sort(key=lambda t: -t[0])
        lost = [q for q, g in measured if g[base] and not g[m]]
        print()
        print(f"── '{m}' vs '{base}': +{len(added)} / -{len(removed)} chipů; "
              f"{len(lost)} odpovědí přišlo o všechny ──")
        for label, group in (("PŘIDÁNO", added), ("ODEBRÁNO", removed)):
            if not group:
                continue
            print(f"  {label}:")
            for sc, c, q in group[:20]:
                print(f"    {sc:.4f}  {c[:56]!r}")
                print(f"             (dotaz: {q[:56]!r})")
            if len(group) > 20:
                print(f"    … a dalších {len(group) - 20}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
