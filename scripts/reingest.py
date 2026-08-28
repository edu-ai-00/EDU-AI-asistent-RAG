#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-11).
"""Re-ingest a list of documents through POST /v1/ingest.

Why this exists: the chunking work (heading columns, element split, versioned
thresholds) only takes effect on rows the CURRENT flows produced. Chunks ingested
by an older version keep the old shape — on the server that showed as
section_heading populated in 0 of 740 rows — so the material has to go through
ingestion again. Doing that for a whole corpus by hand through
tools/upload_test.html is not realistic, and nothing else in the repo drives the
endpoint in bulk.

WHERE TO RUN IT. Production publishes only nginx,
so the gateway is not reachable on 8080 from the host, and through nginx /api/ it
sits behind Basic auth. The simple way is to run this INSIDE the gateway
container, where the port is local and no auth is involved:

    docker cp scripts/reingest.py    edu_ai_api:/tmp/reingest.py
    docker cp reingest_sources.json  edu_ai_api:/tmp/sources.json
    docker exec edu_ai_api python /tmp/reingest.py /tmp/sources.json \
        --legal-entity SKOLA_NOVA_VES --user-id <uuid> --visibility organization

The work list is a JSON array of {"url": ...} or {"file": ...} objects, each
optionally with "roles": [...] (audience roles, init 127 — mandatory at the
gateway; --roles supplies the fallback). Build it
from what was ingested before rather than from the files on disk: uploads/ also
holds saved HTML copies and markdown conversions, which are ARTEFACTS of earlier
runs — feeding those back in would ingest the same document several times.

    SELECT json_agg(json_build_object('url', source_url))
      FROM edu_ai.document_metadata WHERE source_url IS NOT NULL AND is_active;

Requires only the standard library. Ingestion is synchronous per document, so a
corpus takes as long as the flows take — minutes per document is normal.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _multipart(fields: dict[str, str | list[str]],
               file_name: str | None = None,
               file_bytes: bytes | None = None) -> tuple[bytes, str]:
    """Encode a multipart/form-data body. Bounded by a random boundary.

    A list value becomes one part PER element under the same field name — that is
    how FastAPI fills a `list[str] = Form(...)` parameter (audience_roles); a
    single comma-joined part would arrive as one bogus role string.
    """
    boundary = f"----edu-ai-{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        if value is None:
            continue
        for one in (value if isinstance(value, list) else [value]):
            out += f"--{boundary}\r\n".encode()
            out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            out += f"{one}\r\n".encode()
    if file_name is not None and file_bytes is not None:
        ctype = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="file"; '
                f'filename="{file_name}"\r\n').encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += file_bytes + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post(api: str, body: bytes, content_type: str, timeout: float) -> tuple[bool, str]:
    req = urllib.request.Request(f"{api.rstrip('/')}/v1/ingest", data=body,
                                 headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        # The gateway reports the flow's own verdict in the body, so a 200 alone is
        # not success — a failed pipeline comes back 200 with a status saying so.
        # Judged by what FAILURE looks like, not by a list of accepted words: the
        # first version whitelisted {ok, success, completed} and marked every
        # perfectly good dry run as a failure, because that status is "dry_run".
        status = str(payload.get("pipeline_status")
                     or payload.get("status") or "ok").lower()
        detail = payload.get("processing_uuid") or json.dumps(payload)[:120]
        failed = any(word in status for word in ("error", "fail", "refus", "reject"))
        return not failed, f"{status} {detail}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except json.JSONDecodeError as exc:
        return False, f"unparseable response: {exc}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sources", help="JSON array of {'url':...} or {'file':...}")
    p.add_argument("--api-url", default="http://127.0.0.1:8080")
    p.add_argument("--legal-entity", required=True)
    p.add_argument("--user-id", required=True, help="must be an active user of that entity")
    p.add_argument("--visibility", default="entity",
                   choices=["private", "entity", "organization"])
    # init 127: the gateway rejects an ingest with no role (400), so a work list
    # whose entries carry no "roles" needs a default here.
    p.add_argument("--roles", default="STUDENT,TEACHER,PARENT",
                   help="comma-separated fallback for entries without their own")
    p.add_argument("--timeout", type=float, default=900.0, help="per document, seconds")
    p.add_argument("--sleep", type=float, default=2.0, help="pause between documents")
    p.add_argument("--dry-run", action="store_true",
                   help="ask the gateway to validate without ingesting")
    p.add_argument("--limit", type=int, default=0, help="stop after N (0 = all)")
    args = p.parse_args()

    items = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("nothing to do — the work list is empty")
        return 1

    print(f"{len(items)} document(s) -> {args.api_url}")
    print(f"entity={args.legal_entity} visibility={args.visibility} "
          f"user={args.user_id}{' DRY RUN' if args.dry_run else ''}\n")

    default_roles = [r.strip().upper() for r in args.roles.split(",") if r.strip()]
    common = {"legal_entity": args.legal_entity, "user_id": args.user_id,
              "visibility": args.visibility,
              "dry_run": "true" if args.dry_run else "false"}

    failures: list[tuple[str, str]] = []
    started = time.monotonic()
    for i, item in enumerate(items, 1):
        label = item.get("url") or item.get("file") or "?"
        roles = [r.strip().upper() for r in (item.get("roles") or default_roles)]
        print(f"[{i}/{len(items)}] {label[:88]}\n      roles={','.join(roles)}", flush=True)
        t0 = time.monotonic()
        fields = {**common, "audience_roles": roles}

        if item.get("url"):
            body, ctype = _multipart({**fields, "url": item["url"]})
        elif item.get("file"):
            path = Path(item["file"])
            if not path.is_file():
                print("      SKIP - file not found")
                failures.append((label, "file not found"))
                continue
            body, ctype = _multipart(fields, path.name, path.read_bytes())
        else:
            print("      SKIP - neither url nor file")
            failures.append((label, "malformed entry"))
            continue

        good, detail = _post(args.api_url, body, ctype, args.timeout)
        print(f"      {'ok  ' if good else 'FAIL'} {detail}  ({time.monotonic() - t0:.0f}s)",
              flush=True)
        if not good:
            failures.append((label, detail))
        if args.sleep and i < len(items):
            time.sleep(args.sleep)

    mins = (time.monotonic() - started) / 60
    print(f"\n{len(items) - len(failures)}/{len(items)} ingested in {mins:.1f} min")
    if failures:
        print("\nfailed:")
        for label, why in failures:
            print(f"  {label[:70]}\n    {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
