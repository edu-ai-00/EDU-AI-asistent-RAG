#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-08-19).
"""Does every tunable in settings.config->pipeline actually DO something?

A parameter that can be moved to an extreme with no observable effect is one of
three things, and all three are worth knowing about:

  * DEAD      — read into a variable and never used. Three were found by hand this
                way (grade_bonus, grade_hard_boost, level_bonus, all removed);
                nothing guarantees there are no more.
  * UNREACHED — live in code, but the step never runs on the probe query, so the
                test says nothing. Reported separately from DEAD on purpose:
                "no effect observed" and "no effect possible" are different claims
                and conflating them is how a dead knob survives an audit.
  * SATURATED — live, but the corpus makes it irrelevant (a fetch_k above the
                number of matching rows).

Method: snapshot the active settings row, then for each parameter set one extreme
value, wait out the settings cache, run the probe queries, and compare a
fingerprint of the trace — outcome, answer source, per-step status/decision/score
and the chunk ids. Restore after every parameter, and restore again at the end.

The fingerprint deliberately reads the TRACE, not the answer text: an LLM answer
varies between runs, so comparing prose would report noise as signal.

    python3 scripts/param_liveness.py                 # all params, all probes
    python3 scripts/param_liveness.py --step qa_lexical
    python3 scripts/param_liveness.py --bad-values     # robustness: junk in settings

Needs the dev stack up. Writes nothing permanent — but it DOES mutate the active
settings row while running, so do not point it at a shared environment.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time, urllib.request

SEARCH = "http://127.0.0.1:8090/v1/ask"
PG = ["docker", "exec", "-i", "edu_ai_postgres", "psql", "-U", "edu_ai", "-d", "edu_ai"]
ENTITY = "SKOLA_NOVA_VES"          # the settings row under test
CACHE_WAIT = 7.0                   # settings cache TTL is 5 s (settings.py:_CACHE_TTL_S)

# Probes are chosen to reach different parts of the cascade: a curated QA hit, a
# document answer with a known ročník, and a question the corpus cannot answer.
PROBES = [
    {"name": "qa", "question": "Jak se omlouvá absence?",
     "legal_entity": "SKOLA_NOVA_VES_ZS", "user_id": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
     "user_role": "STUDENT", "user_school_grade": 9},
    {"name": "rag", "question": "Jaké pomůcky si máme vzít do školy?",
     "legal_entity": "SKOLA_NOVA_VES_ZS", "user_id": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
     "user_role": "STUDENT", "user_school_grade": 6},
    {"name": "none", "question": "Kolik stojí lyžařský výcvik v roce 2031?",
     "legal_entity": "SKOLA_NOVA_VES_ZS", "user_id": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
     "user_role": "STUDENT"},
]


def sql(query: str) -> str:
    out = subprocess.run(PG + ["-tA", "-c", query], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


def get_config() -> dict:
    return json.loads(sql(
        f"SELECT config::text FROM edu_ai.settings"
        f" WHERE is_active AND legal_entity_code = '{ENTITY}'"))


def put_config(cfg: dict) -> None:
    js = json.dumps(cfg, ensure_ascii=False).replace("'", "''")
    sql(f"UPDATE edu_ai.settings SET config = '{js}'::json"
        f" WHERE is_active AND legal_entity_code = '{ENTITY}'")


def ask(probe: dict) -> dict:
    body = {k: v for k, v in probe.items() if k != "name"}
    body["debug"] = True
    req = urllib.request.Request(SEARCH, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.load(r)
    except Exception as e:                      # a 4xx/5xx is itself a fingerprint
        return {"_error": f"{type(e).__name__}: {e}"}


def fingerprint(resp: dict) -> tuple:
    """outcome, source, per-step facts, chunk ids IN ORDER, and the ORDERED sources.

    The sources list is what makes ranking parameters visible at all. The first
    version of this compared only which chunks were RETRIEVED, so every parameter
    that merely re-orders — rrf_k, content_top_n, priority_min_ratio, the rerank
    mode, the tiebreak weights — came out as "no effect" and could have been read
    as dead. Ordering happens in `answer`, after every gatherer has reported, so no
    per-step field can show it; the ordered sources can.
    """
    if "_error" in resp:
        return ("ERROR", resp["_error"][:60])
    steps, ids = [], []
    for st in (resp.get("trace") or []):
        steps.append((st.get("step"), st.get("status"), st.get("decision"),
                      None if st.get("match_score") is None
                      else round(float(st["match_score"]), 3),
                      st.get("pool")))
        # order preserved on purpose: a step that returns the same chunks in a
        # different order is a different result
        for i in (st.get("added_chunk_ids") or []):
            if i not in ids:
                ids.append(i)
    srcs = tuple((s.get("title") or s.get("url") or "")[:60]
                 for s in (resp.get("sources") or []))
    return (resp.get("outcome"), resp.get("answer_source"), tuple(steps),
            tuple(ids), srcs)


def flips(name: str, val) -> list:
    """BOTH extremes, most informative first — not one.

    The first version of this tried a single end, chosen by the parameter's name on
    the theory that "admit nothing" must always show. It does not: a floor that is
    ALREADY rejecting on the probe (qa_vector.cosine_min at 0.65 rejects 0.624)
    does not change anything when raised, so the test called a live parameter dead.
    A false "dead" is the worst outcome an audit like this can produce — it invites
    deleting something that works — so both ends are tried and the parameter counts
    as live as soon as either one moves the fingerprint. The second run only happens
    when the first shows nothing, so the usual cost is unchanged.
    """
    if isinstance(val, bool):
        return [not val]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if any(k in name for k in ("_k", "top_n", "_turns", "_chunks", "window",
                                   "budget", "tokens", "depth", "chars", "overlap")):
            return [1, 999]
        return [0.0, 1.0]
    if isinstance(val, str):
        alt = {"focus_if_match": "boost", "boost": "focus_if_match",
               "A": "D", "B": "D", "all": "any", "cloud": "off",
               "none": "low"}.get(val)
        return [alt] if alt else []
    if isinstance(val, list) and val and isinstance(val[0], str):
        return [val[:1]] if len(val) > 1 else []
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step")
    ap.add_argument("--bad-values", action="store_true")
    ap.add_argument("--probe", choices=[p["name"] for p in PROBES])
    a = ap.parse_args()
    probes = [p for p in PROBES if not a.probe or p["name"] == a.probe]

    original = get_config()
    print(f"  settings row: {ENTITY}  (snapshot taken)")
    base = {}
    for p in probes:
        base[p["name"]] = fingerprint(ask(p))
        print(f"  baseline {p['name']:<5} outcome={base[p['name']][0]} "
              f"source={base[p['name']][1]}")

    targets = []
    for i, st in enumerate(original.get("pipeline", [])):
        if a.step and st.get("step") != a.step:
            continue
        for k, v in (st.get("params") or {}).items():
            targets.append((i, st.get("step"), k, v))
    print(f"\n  parametrů k prověření: {len(targets)}\n")
    print(f"  {'krok':<18}{'parametr':<26}{'z':>14} → {'na':<16}verdikt")
    print("  " + "─" * 92)

    live, dead, skipped = [], [], []
    try:
        for idx, step, key, val in targets:
            cands = ["___JUNK___"] if a.bad_values else [
                c for c in flips(key, val) if c != val]
            if not cands:
                skipped.append((step, key, "nelze zvolit extrém"))
                continue
            diffs, used = [], None
            for new in cands:
                cfg = json.loads(json.dumps(original))
                cfg["pipeline"][idx].setdefault("params", {})[key] = new
                put_config(cfg)
                time.sleep(CACHE_WAIT)
                used = new
                for p in probes:
                    fp = fingerprint(ask(p))
                    if fp != base[p["name"]]:
                        b, n = base[p["name"]], fp
                        if b[0] != n[0]:
                            what = f"outcome {b[0]}->{n[0]}"
                        elif b[1] != n[1]:
                            what = f"zdroj {b[1]}->{n[1]}"
                        elif b[2] != n[2]:
                            what = "kroky"
                        elif b[3] != n[3]:
                            add = [i for i in n[3] if i not in b[3]]
                            rem = [i for i in b[3] if i not in n[3]]
                            what = (f"chunky +{add or ''}-{rem or ''}" if (add or rem)
                                    else "chunky: totéž, jiné POŘADÍ")
                        else:
                            what = ("zdroje: jiné POŘADÍ"
                                    if sorted(b[4]) == sorted(n[4]) else "zdroje: jiné")
                        diffs.append(f"{p['name']}:{what}")
                put_config(original)
                if diffs:
                    break            # live already proven; second extreme unneeded
            verdict = ("ŽIVÝ  " + ", ".join(diffs)) if diffs else "bez efektu"
            (live if diffs else dead).append((step, key, verdict))
            print(f"  {step:<18}{key:<26}{str(val):>14} → {str(used):<16}{verdict}")
    finally:
        put_config(original)
        time.sleep(CACHE_WAIT)
        after = get_config()
        print(f"\n  settings obnoveny: {'OK' if after == original else '‼ NESHODA'}")

    print(f"\n  ŽIVÝCH: {len(live)}   BEZ EFEKTU: {len(dead)}   NEOVĚŘENO: {len(skipped)}")
    if dead:
        print("\n  bez efektu na těchto sondách (mrtvý, nedosažený, nebo saturovaný):")
        for s, k, _ in dead:
            print(f"    {s}.{k}")
    if skipped:
        print("\n  neověřeno (nešlo zvolit extrém):")
        for s, k, why in skipped:
            print(f"    {s}.{k} — {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
