#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-21).
"""Build the QA test packs: ten CSVs, the upload manifest, and the probe cases.

    python3 QA/build_packs.py            # writes QA/packs/

CSV bytes match what /v1/qa-export produces (utf-8-sig, ';', CRLF), so a pack
opens in Excel like a downloaded one. entry_key is left EMPTY on purpose — the
reader derives it from the question, so re-uploading UPDATES entries instead of
duplicating them.
"""
from __future__ import annotations
import csv, io, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from packs_spec import (ENTITIES, PROVOZ, PROVOZ_ONLY, DOMAINS_PROVOZ,
                        DOMAINS_ADM_ZS, DOMAINS_ADM_SOS, sources_for)
from packs_spec_prijimacky import PRIJIMACKY, PRIJIMACKY_SOS

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
CSV_COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants",
               "sources", "is_active"]
ALL_ROLES = ["STUDENT", "PARENT", "TEACHER"]

# One pack per (entity, topic). Visibility is a test axis, not decoration:
#   the org's packs are `organization` — they reach both units, so a unit caller
#                                    has two candidates and the proximity tier
#                                    (own school before the organisation) decides;
#   unit provoz packs are `entity`  — a sibling unit must not see them;
#   unit prijimacky packs are `organization` — the SOŠ pack is then VISIBLE to a
#                                    ZŠ caller and only ročník/level keeps it out.
# prijimacky-org drops TEACHER so the pack-level role filter is observable.
def _packs():
    for code, e in ENTITIES.items():
        k = e["key"]
        provoz = [q for q in PROVOZ if k in q.answers] + PROVOZ_ONLY.get(code, [])
        yield dict(entity=code, pack_id=f"provoz-{k}", questions=provoz,
                   visibility="organization" if k == "org" else "entity",
                   grades=e["grades_all"],
                   domains=DOMAINS_PROVOZ if e["domains"] else [], roles=ALL_ROLES)

        if k == "sos":
            adm, doms = PRIJIMACKY_SOS, DOMAINS_ADM_SOS
        else:
            adm = [q for q in PRIJIMACKY if k in q.answers]
            doms = DOMAINS_ADM_ZS if e["domains"] else []
        roles = ["STUDENT", "PARENT"] if k == "org" else ALL_ROLES
        yield dict(entity=code, pack_id=f"prijimacky-{k}", questions=adm,
                   visibility="organization" if k in ("org", "zs", "sos") else "entity",
                   grades=e["grades_adm"], domains=doms, roles=roles)


def write_csv(path: str, tag: str, key: str, questions) -> int:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for q in questions:
        # `Titulek | URL`, one per line — the reader's format, and what
        # /v1/qa-export writes back, so a downloaded pack diffs against this one
        srcs = "\n".join(f"{t} | {u}" for t, u in sources_for(q.cat, key))
        w.writerow(["", q.q, f"[{tag}] {q.answers[key]}", q.cat,
                    "\n".join(q.variants), srcs, ""])
    with open(path, "wb") as fh:
        fh.write(buf.getvalue().encode("utf-8-sig"))
    return len(questions)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest, total = {}, 0
    for p in _packs():
        e = ENTITIES[p["entity"]]
        name = f"pack_{p['pack_id']}.csv"
        n = write_csv(os.path.join(OUT, name), e["tag"], e["key"], p["questions"])
        total += n
        meta = dict(legal_entity=p["entity"], user_id=e["user_id"],
                    visibility=p["visibility"], user_defined_pack_id=p["pack_id"],
                    entries=n, audience_roles=p["roles"],
                    school_subject_grades=p["grades"], document_language="cs")
        if p["domains"]:
            meta["domains"] = p["domains"]
        manifest[name] = meta
        print(f"  {name:<34} {p['entity']:<21} {p['visibility']:<13} "
              f"g={','.join(p['grades']):<9} pol={n}")

    with open(os.path.join(OUT, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"_about": "Scoping and audience for each pack — /v1/ingest takes "
                             "these as form fields. Codes, never ids.",
                   "packs": manifest}, fh, ensure_ascii=False, indent=2)
    print(f"\n  {len(manifest)} balíčků, {total} položek → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
