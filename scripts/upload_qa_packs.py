#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-08-21).
"""Upload generated QA packs through /v1/ingest, metadata from _manifest.json.

The generator writes a flat array of {cat, q, a, variants}, i.e. JSON. A QA pack
is a CSV since 2026-08-19 — the gateway routes an upload by EXTENSION and maps
only `.csv` to the QA flow — so this converts each pack on the way out and posts
it as CSV. Converting HERE keeps the generator (which is not in this repo) out of
it, and it is a one-way adapter in dev tooling: NOT a second ingest path, which
is the thing deliberately not built.

Everything the upload needs BESIDES the phrasing lives in `_manifest.json`, keyed
by file name, because /v1/ingest takes scoping as form fields and validates it
against the register: entity, uploader, visibility, roles, ročník set, subject,
domains, validity.

Codes, not ids: the manifest already carries číselník CODES. The generator read ids
off the source chunks and they were translated once, here in the file, rather than
per upload — an id in a form field is a 400 at best and a wrong pack at worst.

Dry run by default. `--go` is the only way anything is written, because import is
the review gate: qa_exact returns answer_md VERBATIM with match_score 1.0 and stops
the cascade, so an unreviewed entry answers with the authority of a curated one.

    python3 scripts/upload_qa_packs.py                    # dry run, all
    python3 scripts/upload_qa_packs.py --only 13ee92f5
    python3 scripts/upload_qa_packs.py --go
    python3 scripts/upload_qa_packs.py --go --mode replace   # pack == the file
"""
from __future__ import annotations
import argparse, csv, io, json, os, re, sys, urllib.request, uuid

GW = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080") + "/v1/ingest"
DIR = "eval/qa_generated"

# The gateway validates the pack name with exactly this rule (migration 143) —
# checked here so a typo in the manifest is named locally instead of coming back
# as a 400 after the whole body has been sent.
PACK_ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# What the reader reads. entry_key is written EMPTY on purpose: the reader then
# derives it from the question, deterministically, so re-uploading the same
# generated pack UPDATES its entries instead of duplicating them — which is what
# makes a measurement corpus reproducible.
CSV_COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "is_active"]
_Q_KEYS = ("q", "question", "canonical_q")
_A_KEYS = ("a", "answer", "answer_md")
_C_KEYS = ("cat", "category")
_V_KEYS = ("variants", "phrasings", "alt", "alt_questions")


def _pick(item: dict, keys) -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _variants_cell(item: dict) -> str:
    """Alternative phrasings, one per line inside a single cell.

    Accepts strings or {q|text|phrasing} objects: the generator writes strings,
    but the JSON reader this replaces took both, and a conversion that silently
    dropped the object form would lose curated phrasings without a word.
    """
    for k in _V_KEYS:
        v = item.get(k)
        if not isinstance(v, (list, tuple)) or not v:
            continue
        out, seen = [], set()
        for it in v:
            if isinstance(it, dict):
                it = it.get("q") or it.get("text") or it.get("phrasing") or ""
            s = str(it).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        if out:
            return "\n".join(out)
    return ""


def to_csv(path: str) -> bytes:
    """One generated pack -> the exact CSV bytes the ingest reader accepts.

    Raises instead of skipping a malformed entry. The reader refuses one too (and
    names the row), so skipping here would only mean a pack that uploads as a
    success with fewer entries than the file has — the failure mode the CSV
    reader was changed to stop.

    A pack that is ALREADY CSV (QA/packs, authored as CSV since 2026-08-19) is
    sent as-is: converting it would only re-encode the bytes the reader wants.
    """
    if path.lower().endswith(".csv"):
        raw = open(path, "rb").read()
        head = raw.decode("utf-8-sig").split("\n", 1)[0].strip().lower()
        if "canonical_q" not in head and "otázka" not in head and "question" not in head:
            raise ValueError(f"CSV header has no question column: {head!r}")
        return raw

    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("qa") or data.get("pipeline_input", {}).get("qa") or data
    if not isinstance(data, list):
        raise ValueError("expected a flat array of QA objects")

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL,
                   lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    rows = 0
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i} is not an object")
        q, a = _pick(item, _Q_KEYS), _pick(item, _A_KEYS)
        if not q or not a:
            raise ValueError(f"entry {i} has no {'question' if not q else 'answer'}")
        w.writerow(["", q, a, _pick(item, _C_KEYS), _variants_cell(item), ""])
        rows += 1
    if not rows:
        raise ValueError("no QA entries")
    # utf-8-sig: the same bytes /v1/qa-export produces, so a pack dumped for
    # inspection opens in Excel like a downloaded one
    return buf.getvalue().encode("utf-8-sig")


def post(path: str, meta: dict, dry: bool, mode: str) -> tuple[int, str]:
    """Multipart POST — the pack as CSV plus the manifest's fields, repeated per value."""
    fields: list[tuple[str, str]] = [
        ("legal_entity", meta["legal_entity"]),
        ("user_id", meta["user_id"]),
        ("visibility", meta["visibility"]),
        ("user_defined_pack_id", meta["user_defined_pack_id"]),
        ("qa_mode", mode),
        ("dry_run", "true" if dry else "false"),
    ]
    for k in ("document_language", "school_subject", "valid_from", "valid_to"):
        if meta.get(k):
            fields.append((k, str(meta[k])))
    for r in meta.get("audience_roles") or []:
        fields.append(("audience_roles", r))
    for g in meta.get("school_subject_grades") or []:
        fields.append(("school_subject_grades", g))
    for d in meta.get("domains") or []:
        fields.append(("domains", d))

    boundary = "----edu-ai-" + uuid.uuid4().hex
    body = b""
    for k, v in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    # The gateway takes the extension from the name it RECEIVES, so this must end
    # in .csv even though the file on disk is the generator's .json.
    fname = os.path.splitext(os.path.basename(path))[0] + ".csv"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\";"
             f" filename=\"{fname}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
    body += to_csv(path) + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        GW, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DIR)
    ap.add_argument("--go", action="store_true", help="skutečně nahrát (jinak dry run)")
    ap.add_argument("--only", help="jen soubory obsahující tento text")
    # merge is the API's own default and the safe one: entries the file does not
    # mention are left alone. replace makes the pack exactly the file, which is
    # what a reproducible measurement corpus wants — and it deactivates whatever
    # a regenerated pack no longer contains, so it is not the default here either.
    ap.add_argument("--mode", choices=("merge", "replace"), default="merge",
                    help="co znamená chybějící řádek (výchozí merge = nechá být)")
    a = ap.parse_args()

    man = json.load(open(f"{a.dir}/_manifest.json", encoding="utf-8"))["packs"]
    items = sorted(man.items())
    if a.only:
        items = [(k, v) for k, v in items if a.only in k]
    dry = not a.go
    print(f"  {'DRY RUN' if dry else '*** NAHRÁVÁM ***'} — {len(items)} balíčků"
          f" → {GW}\n")

    ok = bad = 0
    for name, meta in items:
        path = os.path.join(a.dir, name)
        if not os.path.exists(path):
            print(f"  ! {name}: soubor chybí"); bad += 1; continue
        if not meta.get("user_id"):
            print(f"  ! {name}: v manifestu není user_id pro {meta['legal_entity']}")
            bad += 1; continue
        # Both of these are gateway rules; checking them here names the pack
        # instead of returning a 400 that says only what was wrong, not where.
        if not PACK_ID_RX.match(meta.get("user_defined_pack_id") or ""):
            print(f"  ! {name}: pack_id {meta.get('user_defined_pack_id')!r} "
                  f"— povoleno jen [A-Za-z0-9_-], max 64 znaků")
            bad += 1; continue
        if meta.get("visibility") != "private" and not (meta.get("audience_roles") or []):
            print(f"  ! {name}: chybí audience_roles (povinné mimo private)")
            bad += 1; continue
        try:
            code, resp = post(path, meta, dry, a.mode)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  ! {name}: {e}"); bad += 1; continue
        good = 200 <= code < 300
        ok, bad = ok + good, bad + (not good)
        print(f"  {'✓' if good else '✗'} {name:<28} {meta['legal_entity']:<21}"
              f" pol={meta['entries']:<3} HTTP {code}"
              f"{'' if good else '  ' + resp.replace(chr(10), ' ')[:150]}")
    print(f"\n  ok={ok}  chyb={bad}")
    if dry:
        print("  (nic nenahráno — pro skutečné nahrání přidej --go)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
