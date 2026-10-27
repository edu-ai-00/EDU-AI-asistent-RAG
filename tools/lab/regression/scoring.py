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


def verdict(expect_key, r: dict, found_key=None, suggested_keys=(), accept=()) -> str:
    """`r` is the cascade result {decision, entry, suggestions}; the keys are
    the entry keys of what it returned (entry ids differ between runs).
    `accept`: further keys that are as right as `expect_key` (equivalent answers)."""
    d = r["decision"]
    if expect_key is None:
        return "ŠPATNÁ odpověď" if d in CONFIDENT else "správně bez jisté odpovědi"
    right = {expect_key, *accept}
    hit = found_key in right
    in_sugg = bool(right & set(suggested_keys))
    if d in CONFIDENT:
        return ("přesná shoda" if d == "exact" else "odpověď") if hit else "ŠPATNÁ odpověď"
    if d.startswith("B"):
        return "v nabídce" if (hit or in_sugg) else "nabídka bez správné"
    return "nic (C)" + (" – v záloze" if in_sugg else "")


def score_of(verdicts) -> float:
    return round(sum(SCORE.get(v, 0.0) for v in verdicts), 1)


def summarize(cases: list[dict], results: dict, by_split: bool = False) -> dict:
    """{set: {"cases", "score", "confident_right", "offered", "confident_wrong", "by_verdict"}}
    `by_split`: also "SET:train" / "SET:holdout" (the split of the curated entry)."""
    by_set = defaultdict(list)
    for c in cases:
        res = results.get(c["id"])
        if res:
            by_set[c["set"]].append(res["verdict"])
            if by_split and c.get("split"):
                by_set[f"{c['set']}:{c['split']}"].append(res["verdict"])
    out = {}
    all_v = [v for k, l in by_set.items() if ":" not in k for v in l]
    for name, vs in {**by_set, "ALL": all_v}.items():
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


def bootstrap_delta(cases: list[dict], baseline: dict, current: dict, n: int = 2000, seed: int = 7):
    """Paired bootstrap of the score difference (current − baseline) over the cases
    both runs have: {"delta", "lo", "hi", "n", "significant"} with a 95 % interval.
    A difference whose interval holds 0 is noise, not an improvement."""
    import random
    diffs = []
    for c in cases:
        b, c2 = baseline.get(c["id"]), current.get(c["id"])
        if b and c2:
            diffs.append(SCORE.get(c2["verdict"], 0.0) - SCORE.get(b["verdict"], 0.0))
    if not diffs:
        return {"delta": 0.0, "lo": 0.0, "hi": 0.0, "n": 0, "significant": False}
    rnd = random.Random(seed)
    m = len(diffs)
    sums = sorted(sum(diffs[rnd.randrange(m)] for _ in range(m)) for _ in range(n))
    lo, hi = sums[int(0.025 * n)], sums[int(0.975 * n) - 1]
    return {"delta": round(sum(diffs), 1), "lo": round(lo, 1), "hi": round(hi, 1), "n": m,
            "significant": lo > 0 or hi < 0}
