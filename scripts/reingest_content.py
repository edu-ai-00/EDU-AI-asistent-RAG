#!/usr/bin/env python3
"""Re-ingest a school's content after a normalization change (a deployment note says so).

Runs INSIDE the portal container, which already holds the clients and the queue:

    docker cp scripts/reingest_content.py edu_ai_qa_admin:/tmp/
    docker exec edu_ai_qa_admin python /tmp/reingest_content.py [--qa] [--web] [--school CODE] [--dry-run]

--qa   every active QA pack, as a portal publish with ALL its active entries (merge mode):
       the ingest flow writes their variants again with the current normalizer and
       deactivates the old rows. The content does not change.
--web  every active web document, refreshed through the portal queue (§14.14, forced: an
       unchanged page is ingested too) with the metadata of its current version.
Without --qa/--web both run. The uploader of the new versions is the school's technical
portal account (as for any portal publish). Prints identifiers and counts only.
"""
import argparse
import collections
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/app")
from app.api.deps import get_ingest_client, get_search_client  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import csv_io  # noqa: E402
from app.services.export_service import _pack_rows  # noqa: E402
from app.services.pack_service import fetch_all_entries, get_pack_state, list_packs  # noqa: E402
from app.services.publish_service import _ingest_domains_and_metadata  # noqa: E402

PORTAL = "http://127.0.0.1:8082/qa-api/v1"


def portal(method, path):
    headers = {"X-QA-Portal": "1"}
    if settings.allowed_origins:
        headers["Origin"] = settings.allowed_origins[0].rstrip("/")
    req = urllib.request.Request(PORTAL + path, method=method, headers=headers, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def reingest_qa(search, ingest, schools, dry):
    done = failed = 0
    for school in schools:
        uploader = search.get_uploader(school)
        for p in list_packs(search, school):
            pack_id = p["pack_id"]
            meta = (get_pack_state(search, school, pack_id) or {}).get("metadata") or {}
            if not meta.get("visibility") or not meta.get("audience_roles"):
                print(f"SKIP {school}/{pack_id}: pack metadata not confirmed")
                continue
            rows = _pack_rows(fetch_all_entries(search, school, pack_id, None)[0])
            if not rows:
                continue
            if dry:
                print(f"DRY  qa {school}/{pack_id}: {len(rows)} entries")
                continue
            m = _ingest_domains_and_metadata(meta)
            try:
                r = ingest.run_ingest(
                    csv_bytes=csv_io.write_pack_csv(rows, include_category_column=True),
                    filename=f"{pack_id}.csv", legal_entity=school, user_id=uploader["user_id"],
                    visibility=m["visibility"], pack_id=pack_id, qa_mode="merge", domains=m["domains"],
                    audience_roles=m["audience_roles"], school_subject_grades=m["school_subject_grades"],
                    valid_from=m["valid_from"], valid_to=m["valid_to"])
                ok = r.get("status") != "failed" and bool(r.get("ok_count") or not r.get("error_count"))
            except Exception as e:  # report and continue with the next pack
                ok = False
                print(f"FAIL qa {school}/{pack_id}: {type(e).__name__}")
            if ok:
                print(f"OK   qa {school}/{pack_id}: {len(rows)} entries")
            done += ok
            failed += not ok
    print(f"qa packs re-ingested {done}, failed {failed}")
    return failed


def refresh_web(search, schools, dry):
    queued, refused = [], 0
    for school in schools:
        docs, cursor = [], None
        while True:   # every page of the school's active documents
            page = search.list_documents(school, "active", None, 200, cursor)
            docs += page.get("documents") or []
            cursor = page.get("next_cursor")
            if not cursor:
                break
        for d in (d for d in docs if d.get("source_kind") == "web"):   # a linked PDF included
            if dry:
                print(f"DRY  web {school}/{d['file_uuid']}")
                continue
            st, body = portal("POST", f"/schools/{school}/documents/{d['file_uuid']}/refresh?force=true")
            if st == 202:
                queued.append((school, body["job"]["batch_id"]))
            else:
                refused += 1
                print(f"FAIL web {school}/{d['file_uuid']}: HTTP {st}")
    if dry or not queued:
        print(f"web documents queued {len(queued)}, refused {refused}")
        return refused
    print(f"web documents queued {len(queued)}, refused {refused}; waiting for the queue")
    states = collections.Counter()
    for _ in range(360):
        states = collections.Counter()
        for school, batch in queued:
            st, body = portal("GET", f"/schools/{school}/document-jobs?batch_id={batch}")
            for j in (body or {}).get("jobs") or []:
                states[j["status"]] += 1
        if not states.get("queued") and not states.get("processing"):
            break
        time.sleep(10)
    print(f"web jobs {dict(states)}")
    not_done = sum(n for s, n in states.items() if s != "completed")
    if not_done:
        print(f"web documents NOT re-ingested: {not_done} (see the portal's job list)")
    return refused + not_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa", action="store_true")
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--school")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    both = not a.qa and not a.web
    search, ingest = get_search_client(), get_ingest_client()
    schools = [a.school] if a.school else [e["legal_entity_code"] for e in search.list_active_entities()]
    bad = 0
    if a.qa or both:
        bad += reingest_qa(search, ingest, schools, a.dry_run)
    if a.web or both:
        bad += refresh_web(search, schools, a.dry_run)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
