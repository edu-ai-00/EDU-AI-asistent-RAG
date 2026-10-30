"""The question index (otazky_idx.json) from the school's QA pack CSVs — for new or changed content.

  python3 build_index.py --packs-dir DIR --out idx.json [--like otazky_idx.json]

Reads every CSV in DIR that has the QA pack columns (`entry_key`, `canonical_q`, `variants`,
`is_active`); the pack is the file name. Inactive rows are skipped. `--like` keeps the order and
the `idx` of the entries that are already in an index and appends the new ones, so the sets that
refer to an entry by (pack, entry_key) stay valid. Prints how many entries are new / removed /
changed compared to `--like`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_cases                                                   # noqa: E402


def load_pack(path: str) -> list[dict]:
    pack = os.path.splitext(os.path.basename(path))[0]
    out = []
    for r in build_cases.read_table(path):
        key = (r.get("entry_key") or "").strip()
        q = (r.get("canonical_q") or "").strip()
        if not key or not q:
            continue
        if (r.get("is_active") or "TRUE").strip().upper() in ("FALSE", "0", "NO", "N"):
            continue
        alts = [a.strip() for a in (r.get("variants") or "").replace("\r", "").split("\n") if a.strip()]
        out.append({"pack": pack, "entry_key": key, "q": q, "alts": alts})
    return out


def build(packs_dir: str, like: list[dict] | None = None) -> tuple[list[dict], dict]:
    rows = []
    for f in sorted(os.listdir(packs_dir)):
        if f.lower().endswith(".csv"):
            try:
                rows += load_pack(os.path.join(packs_dir, f))
            except Exception as e:                       # a CSV that is not a QA pack
                print(f"přeskočeno {f}: {e}", file=sys.stderr)
    by_key = {(r["pack"], r["entry_key"]): r for r in rows}
    out, seen = [], set()
    for old in like or []:
        k = (old["pack"], old["entry_key"])
        if k in by_key:
            out.append({**by_key[k], "idx": old["idx"]})
            seen.add(k)
    nxt = max((o["idx"] for o in like or []), default=-1) + 1
    for r in rows:
        k = (r["pack"], r["entry_key"])
        if k not in seen:
            out.append({**r, "idx": nxt})
            nxt += 1
            seen.add(k)
    old_by = {(o["pack"], o["entry_key"]): o for o in like or []}
    stat = {"new": [k for k in by_key if k not in old_by],
            "removed": [k for k in old_by if k not in by_key],
            "changed": [k for k in by_key if k in old_by and
                        (by_key[k]["q"] != old_by[k]["q"] or by_key[k]["alts"] != old_by[k]["alts"])]}
    seen_keys = {}
    stat["collisions"] = []
    for r in rows:
        if r["entry_key"] in seen_keys and seen_keys[r["entry_key"]] != r["pack"]:
            stat["collisions"].append(r["entry_key"])
        seen_keys.setdefault(r["entry_key"], r["pack"])
    return out, stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--like")
    a = ap.parse_args()
    like = json.load(open(a.like, encoding="utf-8")) if a.like else None
    idx, stat = build(a.packs_dir, like)
    json.dump(idx, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(idx)} záznamů → {a.out} | nové {len(stat['new'])}, odebrané {len(stat['removed'])}, "
          f"změněné {len(stat['changed'])}" + (f" | POZOR stejný entry_key ve dvou balíčcích: {len(stat['collisions'])}" if stat['collisions'] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
