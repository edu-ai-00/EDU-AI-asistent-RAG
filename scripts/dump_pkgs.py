# last change in code by Martin Safranek (2026-07-31).
"""Dump installed distributions as TSV: name, version, licence, author.

Feeds THIRD_PARTY_NOTICES.md. Run INSIDE each container so the list describes
what actually ships:

    docker exec -i edu_ai          /app/.venv/bin/python < scripts/dump_pkgs.py
    docker exec -i edu_ai_search   python < scripts/dump_pkgs.py
    docker exec -i edu_ai_api      python < scripts/dump_pkgs.py
    docker exec -i edu_ai_web_chat python < scripts/dump_pkgs.py

Then rebuild the notices file (assembly script in the session notes; the tables
are plain TSV → markdown). Regenerate after any dependency change."""
import importlib.metadata as md


def licence_of(m):
    exp = m.get("License-Expression")
    if exp:
        return exp
    lic = (m.get("License") or "").strip()
    if lic and len(lic) <= 60 and "\n" not in lic and lic.upper() != "UNKNOWN":
        return lic
    cls = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or []
           if c.startswith("License ::")]
    cls = [c for c in cls if c != "OSI Approved"]
    if cls:
        return "; ".join(sorted(set(cls)))
    return lic.splitlines()[0][:60] if lic else "?"


def author_of(m):
    for k in ("Author", "Maintainer"):
        v = (m.get(k) or "").strip()
        if v and v.upper() != "UNKNOWN":
            return v
    for k in ("Author-email", "Maintainer-email"):
        v = (m.get(k) or "").strip()
        if v:
            return v.split("<")[0].strip().strip('"') or v
    return "?"


seen = {}
for d in md.distributions():
    m = d.metadata
    name = (m.get("Name") or "").strip()
    if not name or name.lower() in seen:
        continue
    seen[name.lower()] = True
    print("\t".join([name, d.version or "?", licence_of(m),
                     author_of(m).replace("\t", " ")[:80]]))
