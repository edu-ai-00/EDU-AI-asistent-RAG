"""Export a school's active QA packs from a database copy, as the application does.

Calls the search service's own /v1/qa-export handler (server.qa_export), so the
CSV is byte for byte what the portal and the review app read. Run INSIDE a
search image against the copy:

  python export_packs.py ENTITY /out/dir
"""
import sys
from pathlib import Path

import app.server as server
from app.db import get_cursor


def main() -> int:
    entity, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with get_cursor() as cur:
        cur.execute("SELECT DISTINCT pack_id FROM edu_ai.qa_entries"
                    " WHERE is_active AND legal_entity_code = %s ORDER BY 1", (entity,))
        packs = [r[0] for r in cur.fetchall()]
    for pack in packs:
        resp = server.qa_export(entity, pack)
        (out / f"{pack}.csv").write_bytes(resp.body)
        print(f"[export] {entity} {pack}: {len(resp.body)} B", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
