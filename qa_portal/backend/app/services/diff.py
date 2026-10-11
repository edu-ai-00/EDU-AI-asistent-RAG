"""Diff between the pack's current entries and a set of proposed writes.

Merge semantics (CONTRACT.md §3, §5.3): an entry_key absent from the proposed
set is UNCHANGED, never deleted. `is_active=false` on a proposed row is how a
deactivation is stated. Compared fields mirror the Entry/EntryWrite writable
subset — never owner/visibility/audience/grades/subjects/validity, which the
portal never lets the client set (CONTRACT.md §2.4).
"""
from __future__ import annotations

_COMPARED_FIELDS = ("canonical_q", "answer_md", "variants", "sources", "domains",
                    "category_code", "is_active")


def _normalized(entry: dict) -> dict:
    return {
        "canonical_q": entry.get("canonical_q", ""),
        "answer_md": entry.get("answer_md", ""),
        "variants": list(entry.get("variants") or []),
        "sources": list(entry.get("sources") or []),
        "domains": sorted(entry.get("domains") or []),
        "category_code": entry.get("category_code"),
        "is_active": bool(entry.get("is_active", True)),
    }


def compute_diff(current_by_key: dict[str, dict], proposed: list[dict], *,
                  mode: str = "merge") -> dict:
    """current_by_key: {entry_key: current-entry-dict}. proposed: list of
    entry dicts with resolved entry_key (server-generated for new ones).

    mode="replace": an existing key the proposed set never mentions is shown
    as deactivated (that is what qa_mode=replace does downstream), not
    unchanged — the preview must tell the truth about what confirming this
    import will do (CONTRACT.md §5.3).
    """
    added, changed, deactivated = [], [], []
    touched_keys: set[str] = set()
    changed_keys: set[str] = set()
    deactivated_keys: set[str] = set()

    for row in proposed:
        key = row["entry_key"]
        touched_keys.add(key)
        current = current_by_key.get(key)
        if current is None:
            added.append({"entry_key": key, "canonical_q": row.get("canonical_q", "")})
            continue
        old_n, new_n = _normalized(current), _normalized(row)
        if old_n["is_active"] and not new_n["is_active"]:
            deactivated.append({"entry_key": key, "canonical_q": current.get("canonical_q", "")})
            deactivated_keys.add(key)
            continue
        field_diffs = [
            {"field": f, "old": old_n[f], "new": new_n[f]}
            for f in _COMPARED_FIELDS if old_n[f] != new_n[f]
        ]
        if field_diffs:
            changed.append({"entry_key": key, "fields": field_diffs})
            changed_keys.add(key)
        # else: identical to current — falls into `unchanged` below.

    # Walk current_by_key in its existing order so `unchanged` is deterministic
    # for both untouched keys and touched-but-identical ones.
    unchanged = []
    for k, current in current_by_key.items():
        if k in changed_keys or k in deactivated_keys:
            continue
        if k not in touched_keys and mode == "replace" and current.get("is_active", True):
            deactivated.append({"entry_key": k, "canonical_q": current.get("canonical_q", "")})
            continue
        unchanged.append(k)
    return {"added": added, "changed": changed, "unchanged": unchanged,
            "deactivated": deactivated}
