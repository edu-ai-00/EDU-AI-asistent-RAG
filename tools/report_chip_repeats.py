#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-04).
"""What the chips actually did, read back from question_answer_log.

    docker compose run --rm -v /opt/edu_ai/tools:/app/tools         --entrypoint python edu_ai_search tools/report_chip_repeats.py [DAYS]

(the image copies app/ only, so tools/ has to be mounted — same as the tests)

Read-only. Answers the two questions the suggestions_offered column (migration
205) was added to make answerable, and which nothing measured before it:

  near-duplicates  a chip whose text is nearly the question just asked. Reported
                   because one was seen in production on 2026-09-03 ("Kdy se
                   zveřejní výsledky JPZ?" offered "Kde se zveřejní výsledky
                   JPZ?", ratio 0.97) and the decision was to watch rather than
                   filter — a similarity filter would cut the BEST chips first,
                   which are near-siblings differing in one word ("…test z
                   matematiky" → "…z českého jazyka", measured at 0.7322).
  taken            a chip offered in one turn that arrives as the next turn's
                   question. The closest thing to a click-through this data can
                   give, and it needs no client change: a chip is resent
                   verbatim. (selected_answer_key would say it directly, but no
                   client has ever sent one — 0 rows in 14 days, 2026-09-03.)

A high near-duplicate rate argues for demoting them; a low one says the case that
prompted this was an outlier. Neither is worth acting on at n=1.
"""
from __future__ import annotations

import difflib
import sys

sys.path.insert(0, "/app")

from app.db import get_cursor  # noqa: E402

NEAR = 0.85

_SQL = """
SELECT conversation_id, created_at, raw_query, suggestions_offered
  FROM edu_ai.question_answer_log
 WHERE suggestions_offered IS NOT NULL
   AND created_at > now() - %s::interval
 ORDER BY conversation_id, created_at
"""


def ratio(a, b) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def main(argv):
    days = int(argv[0]) if argv else 7
    with get_cursor() as cur:
        cur.execute(_SQL, (f"{days} days",))
        rows = cur.fetchall()

    chips = [(q, s.get("question")) for _c, _t, q, ss in rows
             for s in (ss or []) if s.get("question")]
    near = sorted(((ratio(q, c), q, c) for q, c in chips), reverse=True)
    near = [n for n in near if n[0] >= NEAR]

    # a chip is "taken" when the NEXT turn of the same conversation asks it
    by_conv: dict = {}
    for conv, created, q, ss in rows:
        by_conv.setdefault(str(conv), []).append((created, q, ss or []))
    offered = taken = 0
    for turns in by_conv.values():
        for i, (_t, _q, ss) in enumerate(turns[:-1]):
            nxt = (turns[i + 1][1] or "").strip().casefold()
            for s in ss:
                offered += 1
                if (s.get("question") or "").strip().casefold() == nxt:
                    taken += 1

    print(f"posledních {days} dní: {len(rows)} turnů s chipy, {len(chips)} chipů, "
          f"{len(by_conv)} konverzací")
    print()
    print(f"── skoro shodné s dotazem (>= {NEAR}) ── {len(near)} z {len(chips)}")
    for r, q, c in near[:20]:
        print(f"    {r:.2f}  {q[:52]!r}")
        print(f"          → {c[:52]!r}")
    print()
    print(f"── vzaté chipy ── {taken} z {offered} nabídnutých v turnu, "
          f"po kterém následoval další "
          f"({100 * taken / offered:.0f} %)" if offered else "── vzaté chipy ── žádná data")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
