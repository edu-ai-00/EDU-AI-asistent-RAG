#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-21).
"""Validate the built packs the way the ingest reader and the DB guards will.

Refuses locally what would otherwise come back as a 400 (or, worse, as a silent
duplicate): the reader's CSV rules, the pack_id rule from migration 143, the
derived entry_key collision from 152, and qa_duplicate_questions() from 142 —
two active answers to one question within a school make qa_exact's winner
arbitrary, so the two packs of an entity must not overlap.
"""
from __future__ import annotations
import csv, glob, io, json, os, re, sys, unicodedata, collections

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "packs")
PACK_ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "sources",
           "is_active"]
# Only what the reader will actually keep. A line it silently drops is worse than
# a rejected pack: the curator sees the source in the file and never in the chat.
SOURCE_RX = re.compile(r"^(?:[^|]*\|\s*)?https?://\S+$")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _slug(text: str, taken: set) -> str:
    s = _fold(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:48] or "entry"
    cand, n = s, 2
    while cand in taken:
        cand, n = f"{s}-{n}", n + 1
    taken.add(cand)
    return cand


def main() -> int:
    man = json.load(open(os.path.join(OUT, "_manifest.json"), encoding="utf-8"))["packs"]
    errors, warn = [], []
    per_entity = collections.defaultdict(list)   # entity -> (folded q, pack, file)
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(OUT, "*.csv")))

    if set(files) != set(man):
        errors.append(f"manifest and directory disagree: {set(files) ^ set(man)}")

    for name in files:
        meta = man.get(name) or {}
        raw = open(os.path.join(OUT, name), "rb").read()
        if not raw.startswith(b"\xef\xbb\xbf"):
            errors.append(f"{name}: missing utf-8 BOM (Excel would mangle it)")
        text = raw.decode("utf-8-sig")

        if not PACK_ID_RX.match(meta.get("user_defined_pack_id") or ""):
            errors.append(f"{name}: pack_id {meta.get('user_defined_pack_id')!r} rejected by mig 143")
        if meta.get("visibility") != "private" and not meta.get("audience_roles"):
            errors.append(f"{name}: audience_roles required outside private")

        # The reader INFERS the delimiter from the first 4096 chars, so a pack
        # whose answers carry more commas than the header carries semicolons
        # would parse as one column. Cheap to check, silent and total if missed.
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=";,\t").delimiter
        except csv.Error:
            delim = ";"
        if delim != ";":
            errors.append(f"{name}: the reader would sniff {delim!r}, not ';'")
        if text.count('"') % 2:
            errors.append(f"{name}: odd number of double quotes — one field never closes")

        rdr = csv.DictReader(io.StringIO(text), delimiter=delim)
        if [h.strip().lower() for h in (rdr.fieldnames or [])] != COLUMNS:
            errors.append(f"{name}: header is {rdr.fieldnames}, expected {COLUMNS}")
            continue

        taken, rows = set(), 0
        for i, item in enumerate(rdr, start=2):
            if item.get(None):
                errors.append(f"{name} row {i}: unquoted ';' inside a cell — {item[None]}")
            q, a = (item.get("canonical_q") or "").strip(), (item.get("answer_md") or "").strip()
            if not q or not a:
                errors.append(f"{name} row {i}: empty {'question' if not q else 'answer'}")
                continue
            if not a.startswith("["):
                warn.append(f"{name} row {i}: answer has no [SCOPE] tag")
            key = _slug(q, taken)                     # mimics the reader's derivation
            variants = [v.strip() for v in (item.get("variants") or "").splitlines() if v.strip()]
            if len(variants) != len({v.lower() for v in variants}):
                errors.append(f"{name} row {i}: duplicate variant phrasing")
            if _fold(q) in {_fold(v) for v in variants}:
                errors.append(f"{name} row {i}: a variant repeats the canonical question")
            srcs = [s.strip() for s in (item.get("sources") or "").splitlines() if s.strip()]
            for s in srcs:
                if not SOURCE_RX.match(s):
                    errors.append(f"{name} row {i}: source line the reader would "
                                  f"drop or mis-title — {s!r}")
            if len({s.rsplit("|", 1)[-1].strip() for s in srcs}) != len(srcs):
                errors.append(f"{name} row {i}: the same source URL cited twice")
            rows += 1
            per_entity[meta.get("legal_entity")].append((_fold(q), meta.get("user_defined_pack_id"), name))
        if rows != meta.get("entries"):
            errors.append(f"{name}: {rows} rows but manifest says {meta.get('entries')}")
        # the derived entry_key must be unique inside the pack (mig 152 index)
        if len(taken) != rows:
            errors.append(f"{name}: derived entry_key collision")

    for entity, items in per_entity.items():
        seen = collections.defaultdict(set)
        for q, pack, _ in items:
            seen[q].add(pack)
        for q, packs in seen.items():
            if len(packs) > 1:
                errors.append(f"{entity}: {q!r} answered by {sorted(packs)} — "
                              "qa_duplicate_questions() would report it")

    for w in warn:
        print(f"  ! {w}")
    for e in errors:
        print(f"  ✗ {e}")
    n = sum(len(v) for v in per_entity.values())
    print(f"\n  {len(files)} packs, {n} entries, {len(errors)} errors, {len(warn)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
