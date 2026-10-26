"""Verdict, score and baseline comparison of the regression set. Stdlib only.

A case is {"id", "set", "pack", "entry_key", "query", "category"}. `entry_key`
is the curated answer that must come back; None means "no confident answer is
right" (ambiguous or missing from the data): the offer is fine, a confident
answer is the failure.

A confident WRONG answer costs more than a right one gains: it says nothing
like "Nechtěl ses zeptat…?", the asker takes it for the answer.
"""

from __future__ import annotations

from collections import Counter, defaultdict

SCORE = {"přesná shoda": 1.0, "odpověď": 1.0, "v nabídce": 0.5,
         "správně bez jisté odpovědi": 1.0, "ŠPATNÁ odpověď": -3.0}
CONFIDENT = ("exact", "A")


def verdict(expect_key, r: dict, found_key=None, suggested_keys=()) -> str:
    """`r` is the cascade result {decision, entry, suggestions}; the keys are
    the entry keys of what it returned (entry ids differ between runs)."""
    d = r["decision"]
    if expect_key is None:
        return "ŠPATNÁ odpověď" if d in CONFIDENT else "správně bez jisté odpovědi"
    hit = found_key == expect_key
    in_sugg = expect_key in suggested_keys
    if d in CONFIDENT:
        return ("přesná shoda" if d == "exact" else "odpověď") if hit else "ŠPATNÁ odpověď"
    if d.startswith("B"):
        return "v nabídce" if (hit or in_sugg) else "nabídka bez správné"
    return "nic (C)" + (" – v záloze" if in_sugg else "")


def score_of(verdicts) -> float:
    return round(sum(SCORE.get(v, 0.0) for v in verdicts), 1)


def summarize(cases: list[dict], results: dict) -> dict:
    """{set: {"cases", "score", "confident_right", "offered", "confident_wrong", "by_verdict"}}"""
    by_set = defaultdict(list)
    for c in cases:
        res = results.get(c["id"])
        if res:
            by_set[c["set"]].append(res["verdict"])
    out = {}
    for name, vs in {**by_set, "ALL": [v for l in by_set.values() for v in l]}.items():
        cnt = Counter(vs)
        out[name] = {"cases": len(vs), "score": score_of(vs),
                     "confident_right": cnt["přesná shoda"] + cnt["odpověď"] + cnt["správně bez jisté odpovědi"],
                     "offered": cnt["v nabídce"], "confident_wrong": cnt["ŠPATNÁ odpověď"],
                     "by_verdict": dict(cnt)}
    return out


def compare(cases: list[dict], baseline: dict, current: dict) -> dict:
    """Case by case. A regression is a lower score for the same case; the worst
    are new confident wrong answers. Cases missing on one side are counted, not
    compared (the set grew or the packs changed)."""
    regress, improved, only_now, only_before = [], [], [], []
    for c in cases:
        b, n = baseline.get(c["id"]), current.get(c["id"])
        if b and not n:
            only_before.append(c["id"])
            continue
        if n and not b:
            only_now.append(c["id"])
            continue
        if not b or not n:
            continue
        delta = SCORE.get(n["verdict"], 0.0) - SCORE.get(b["verdict"], 0.0)
        row = {"id": c["id"], "set": c["set"], "before": b["verdict"], "now": n["verdict"],
               "delta": delta, "found_before": b.get("found_key"), "found_now": n.get("found_key")}
        if delta < 0:
            regress.append(row)
        elif delta > 0:
            improved.append(row)
    regress.sort(key=lambda r: r["delta"])
    return {"regressions": regress, "improvements": improved,
            "new_confident_wrong": [r for r in regress if r["now"] == "ŠPATNÁ odpověď"],
            "only_now": only_now, "only_before": only_before}
