#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""The eval harness sends what the JSONL says. Pure — no stack, no DB, no HTTP.

    python3 tests/test_eval_payload.py

WHY THIS FILE EXISTS AT ALL. `tests/eval_rag.py` builds its request body from a
handful of named case fields, so a field added to the JSONL and not to that
builder is accepted, ignored and never sent — and the run then measures the
UNRESTRICTED cascade while the report says it measured a scoped one. Every
number in such a report is wrong and nothing about it looks wrong. That is not a
hypothetical: `domains` was the third field to be added to the case format, and
the plan for this change called the trap out by name.

So the builder is asserted directly. It is also the cheapest possible test —
milliseconds, no services — which is the other reason it is separate from the
eval run.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_rag import build_payload, load_cases   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
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


BASE = {"question": "Kdy?", "entity": "E", "role": "PARENT"}


def test_builder():
    print("\n[build_payload]")
    b = build_payload(dict(BASE))
    check("a case without domains sends NO `domains` key",
          "domains" not in b,
          "null would change the idempotency digest of every old request")
    check("…and still sends question, entity and role",
          b == {"question": "Kdy?", "legal_entity": "E", "user_role": "PARENT"}, f"{b}")

    b = build_payload({**BASE, "grade": 7})
    check("grade is sent as user_school_grade", b.get("user_school_grade") == 7, f"{b}")
    b = build_payload({**BASE, "grade": None})
    check("a null grade is omitted", "user_school_grade" not in b, f"{b}")

    # THE ASSERTION THIS FILE IS FOR.
    b = build_payload({**BASE, "domains": ["ATTENDANCE"]})
    check("a case WITH domains actually sends them",
          b.get("domains") == ["ATTENDANCE"], f"{b}")
    b = build_payload({**BASE, "domains": ["A", "B"]})
    check("…including several", b.get("domains") == ["A", "B"], f"{b}")
    # Sent VERBATIM, not normalised here: the server is the authority for
    # canonicalisation, and a harness that pre-canonicalised would hide a server
    # that does not (case D05 of the scoped set is exactly that check).
    b = build_payload({**BASE, "domains": [" catering ", "CATERING"]})
    check("…verbatim, so the SERVER's canonicalisation is what gets measured",
          b.get("domains") == [" catering ", "CATERING"], f"{b}")
    # An empty list is a real value and must reach the server, which answers 422.
    # Dropping it here would turn a client bug into a silent unrestricted run.
    b = build_payload({**BASE, "domains": []})
    check("an EMPTY list is sent (the server's 422 is the point)",
          b.get("domains") == [], f"{b}")


def test_case_files():
    print("\n[the case files parse, and mean what they say]")
    for name, scoped in (("eval_rag_regression.jsonl", False),
                         ("eval_rag_domains.jsonl", True)):
        path = ROOT / name
        check(f"{name} exists", path.is_file())
        if not path.is_file():
            continue
        try:
            cases = load_cases(path)
        except Exception as e:                                   # noqa: BLE001
            check(f"{name} parses", False, f"{type(e).__name__}: {e}")
            continue
        check(f"{name} parses ({len(cases)} cases)", bool(cases))
        check(f"{name}: every case has id, question, entity, role",
              all(c.get("id") and c.get("question") and c.get("entity")
                  and c.get("role") for c in cases),
              f"{[c.get('id') for c in cases if not c.get('role')]}")
        ids = [c["id"] for c in cases]
        check(f"{name}: ids are unique", len(ids) == len(set(ids)),
              f"{[i for i in ids if ids.count(i) > 1]}")
        # the unrestricted set MUST stay unrestricted — that is what makes it the
        # compatibility measurement
        if not scoped:
            check(f"{name}: no case carries `domains` (it is the UNRESTRICTED set)",
                  all(c.get("domains") is None for c in cases),
                  f"{[c['id'] for c in cases if c.get('domains') is not None]}")
        else:
            with_dom = [c for c in cases if c.get("domains") is not None]
            check(f"{name}: most cases carry `domains`",
                  len(with_dom) >= len(cases) - 2, f"{len(with_dom)}/{len(cases)}")
            check(f"{name}: it keeps ONE unrestricted control row",
                  any(c.get("domains") is None for c in cases),
                  "without it the file cannot show that the filter is what changed")
            check(f"{name}: every domains value is a list of strings",
                  all(isinstance(c["domains"], list)
                      and all(isinstance(x, str) for x in c["domains"])
                      for c in with_dom))
            # and the builder really would send each of them
            check(f"{name}: build_payload sends the codes of every scoped case",
                  all(build_payload(c).get("domains") == c["domains"] for c in with_dom))


def main() -> int:
    print("=" * 70)
    print("eval harness — the payload builder and the case files")
    print("=" * 70)
    for t in (test_builder, test_case_files):
        t()
    print("\n" + "=" * 70)
    print(f"{_ran} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
