"""Check NEW or CHANGED QA content before it goes live: does it break what already works?

  python3 content_check.py --packs-dir DIR_WITH_THE_NEW_PACK_CSVS [--out-dir DIR] [--sweep]

DIR holds the school's pack CSVs as they WILL be after the change (the old packs plus the new or
edited ones). The script

  1. builds the question index from them (build_index.py, order and idx of the old entries kept),
  2. runs the whole regression set (A, B, C, D, L, N) against that index with the deployed settings
     and compares it with the accepted baseline (accepted.json),
  3. reports what the new entries DO to the old cases: confident answers they STEAL from a right
     answer, questions outside the content that now get a confident answer from them, and which
     new entries attract the most,
  4. writes a worksheet (xlsx) in which a person writes test questions for the new entries
     (answer-based, like set D), and a short report,
  5. with --sweep, tries t_answer / m_margin around today's values on the new index.

Exit code 1 when a new entry takes a confident right answer away or answers an out-of-content
question confidently (the limits are --max-stolen and --max-n-confident, default 0).
Private data (queries, answers, results) stays in the state directory, never in the repository.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_index                                                   # noqa: E402
import scoring                                                       # noqa: E402

CONF = ("exact", "A")


def run_regression(index_path, cases_path, out_path, config=None):
    cmd = [os.path.join(HERE, "run.sh"), index_path, cases_path, out_path]
    if config:
        cmd += ["--config", json.dumps(config)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("run.sh failed:\n" + (p.stdout + p.stderr)[-1500:])
    return json.load(open(out_path, encoding="utf-8"))


def analyse(cases, base, now, new_keys, q_by_key):
    """The effect of the new entries on the old cases."""
    by_id = {c["id"]: c for c in cases}
    stolen, n_conf, attracted = [], [], collections.Counter()
    for cid, r in now.items():
        c = by_id.get(cid)
        b = base.get(cid)
        if not c or not b:
            continue
        found = r.get("found_key")
        is_new = found in new_keys
        if r["decision"] in CONF and is_new and r["verdict"] == "ŠPATNÁ odpověď":
            if c["set"] == "N":
                n_conf.append(c)
            elif b["verdict"] in ("přesná shoda", "odpověď"):
                stolen.append(c)
            attracted[found] += 1
    return stolen, n_conf, attracted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs-dir", required=True, help="the pack CSVs as they will be (old + new/edited)")
    ap.add_argument("--state-dir", default=os.environ.get("EDU_AI_STATE_DIR", os.path.expanduser("~/edu-ai/state")))
    ap.add_argument("--out-dir")
    ap.add_argument("--max-stolen", type=int, default=0)
    ap.add_argument("--max-n-confident", type=int, default=0)
    ap.add_argument("--sweep", action="store_true", help="also try t_answer / m_margin around the defaults")
    a = ap.parse_args()
    R = os.path.join(a.state_dir, "regression")
    old_idx_path = os.path.join(a.state_dir, "stem_vs_lemma", "otazky_idx.json")
    out_dir = a.out_dir or os.path.join(R, "content_check")
    os.makedirs(out_dir, exist_ok=True)
    cases = json.load(open(os.path.join(R, "cases.json"), encoding="utf-8"))
    base = json.load(open(os.path.join(R, "accepted.json"), encoding="utf-8"))["results"]
    old_idx = json.load(open(old_idx_path, encoding="utf-8"))

    idx, stat = build_index.build(a.packs_dir, old_idx)
    idx_path = os.path.join(out_dir, "index_new.json")
    json.dump(idx, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    new_keys = {k[1] for k in stat["new"]} | {k[1] for k in stat["changed"]}
    print(f"index: {len(idx)} záznamů | nové {len(stat['new'])}, změněné {len(stat['changed'])}, odebrané {len(stat['removed'])}")
    if stat["collisions"]:
        print(f"POZOR: stejný entry_key je ve dvou balíčcích ({len(stat['collisions'])}×): testy rozlišují záznam podle klíče, přejmenujte je")
    if not (stat["new"] or stat["changed"] or stat["removed"]):
        print("Žádná změna obsahu proti dnešnímu seznamu: není co kontrolovat.")
        return 0

    out = run_regression(idx_path, os.path.join(R, "cases.json"), os.path.join(out_dir, "now.json"))
    now = out["results"]
    q_by_key = {k[1]: next((e["q"] for e in idx if (e["pack"], e["entry_key"]) == k), k[1]) for k in stat["new"] + stat["changed"]}
    sb, sn = scoring.summarize(cases, base), scoring.summarize(cases, now)
    print(f"\n{'sada':<6}{'případů':>8}{'skóre před':>12}{'skóre teď':>11}{'jistě špatně před → teď':>26}")
    for name in sn:
        b = sb.get(name, {"score": 0, "confident_wrong": 0})
        n = sn[name]
        print(f"{name:<6}{n['cases']:>8}{b['score']:>12}{n['score']:>11}{b['confident_wrong']:>14} →{n['confident_wrong']:>4}")
    d = scoring.compare(cases, base, now)
    stolen, n_conf, attracted = analyse(cases, base, now, new_keys, q_by_key)
    print(f"\nzhoršeno {len(d['regressions'])}, zlepšeno {len(d['improvements'])}, nové jisté špatné {len(d['new_confident_wrong'])}")
    print(f"nový/změněný záznam UKRADL jistou správnou odpověď: {len(stolen)}×")
    print(f"otázka mimo obsah dostala jistou odpověď z nového záznamu: {len(n_conf)}×")
    if attracted:
        print("\nnové záznamy, které přitáhly nejvíc cizích dotazů (jistě, chybně):")
        for k, n in attracted.most_common(8):
            print(f"  {n:>3}×  {q_by_key.get(k, k)}")

    if a.sweep:
        print("\nprahy v okolí dnešních hodnot na novém seznamu:")
        for ta, mm in ((0.55, 0.08), (0.60, 0.08), (0.65, 0.08), (0.60, 0.04), (0.60, 0.12)):
            o = run_regression(idx_path, os.path.join(R, "cases.json"), os.path.join(out_dir, f"sweep_{ta}_{mm}.json"),
                               {"t_answer": ta, "m_margin": mm})["results"]
            s = scoring.summarize(cases, o)["ALL"]
            print(f"  t_answer {ta} m_margin {mm}: skóre {s['score']}, jistě správně {s['confident_right']}, jistě špatně {s['confident_wrong']}")

    # worksheet for the new entries
    keys_file = os.path.join(out_dir, "nove_klice.json")
    json.dump(sorted(new_keys), open(keys_file, "w"), ensure_ascii=False)
    ws = subprocess.run([sys.executable, os.path.join(HERE, "make_d_template.py"), "--questions", idx_path,
                         "--packs-dir", a.packs_dir, "--only-keys", keys_file, "--none-rows", "0",
                         "--out", os.path.join(out_dir, "testovaci_otazky_nove.xlsx"),
                         "--map-out", os.path.join(out_dir, "testovaci_otazky_nove_mapa.json")] +
                        ["--packs"] + sorted({k[0] for k in stat["new"] + stat["changed"]}),
                        capture_output=True, text=True)
    print("\n" + (ws.stdout.strip() or ws.stderr.strip()[-300:]))

    bad = len(stolen) > a.max_stolen or len(n_conf) > a.max_n_confident
    verdict = "NEPROŠLO: nový obsah bere jistou správnou odpověď nebo odpovídá mimo obsah" if bad else "PROŠLO: nic se nezhoršilo"
    report = [f"# Kontrola nového obsahu", "", f"- nové záznamy: {len(stat['new'])}, změněné: {len(stat['changed'])}, odebrané: {len(stat['removed'])}",
              f"- ukradené jisté správné odpovědi: {len(stolen)}", f"- jisté odpovědi na otázky mimo obsah: {len(n_conf)}",
              f"- zhoršeno {len(d['regressions'])}, zlepšeno {len(d['improvements'])}", f"- **{verdict}**"]
    open(os.path.join(out_dir, "zprava.md"), "w", encoding="utf-8").write("\n".join(report) + "\n")
    print(f"\n{verdict}\nzpráva a soubory: {out_dir}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
