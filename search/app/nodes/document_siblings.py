# last change in code by Martin Safranek (2026-08-09).
"""Node — document_siblings: complete a matched section from its own chunks.

A page element (one heading and the text under it) is split into several chunks
that share a `file_item_uuid`; only the FIRST of them carries the heading. So a
section whose meaning lives in its heading is findable through exactly one of its
chunks, and the rest are unreachable — "Seznam pomůcek na školní rok 2026/2027"
is chunk 0 of its element, while chunks 1 and 2 are bare lists ("4x 544, 2x 460,
… vodovky, tempery"), matching nothing and carrying no signal a retriever can
use (measured: coverage 0.000, cosine rank 25/503).

This step takes the sections the retrievers already chose and fills in their
remaining chunks. Gather-only, query-time — no re-ingest, and nothing about how
the documents were chunked has to change.

Two things it deliberately does NOT do:

  * It does not raise coverage. A sibling is CONTEXT, not evidence — it was not
    matched, it was invited. Counting it would let a long section win merely by
    having more chunks.
  * It does not pull in a whole section unconditionally. Elements here run from
    1 to 31 chunks (12 of them are over the cap and hold 183 chunks between
    them); dragging the largest one in whole would eat most of the context
    budget on a single match.

`max_chunks_per_element` is a BUDGET for the element, counting the chunks that
were matched. Below it the whole section comes in. Above it, chunks are taken in
order of DISTANCE from the nearest match — the immediate neighbours first, then
the next ring out — until the budget is used. So the reach follows the budget
instead of being a separate fixed number: with a budget of 6 and one match, a
long element contributes the match plus five chunks around it, roughly two on
each side, rather than the one-on-each-side a fixed window gave.

The outermost ring can end up one-sided, because the budget is filled rather than
kept symmetrical — a ring that does not fit is not a reason to leave the budget
unused. Ties go to the lower chunk_index, so the preceding chunk wins.

`window` remains as an optional HARD cap on that distance for a corpus where a
far-away chunk is never wanted; 0 or absent means the budget is the only limit.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope

logger = logging.getLogger(__name__)

_DEFAULTS = {"enabled": True, "max_chunks_per_element": 6, "window": 0,
             # grade-anchored recall: pull chunks tagged with the caller's
             # grade (or 9999) from the DOCUMENTS already in the pool, even
             # when they carry no query words. Without it a grade the query
             # cannot reach lexically ("2. třída" lists "Sešity", never the
             # word "pomůcky") never enters the pool, so the answer's grade
             # focus has nothing to narrow to. "off" | "from_pool_docs".
             "grade_recall": "from_pool_docs", "grade_recall_max": 8}


def _grade_recall(state, chunks, scope_cond, scope_params, cfg):
    """Append grade-matching chunks from the pool's documents (context, not
    evidence). Bounded; never raises. Returns the number added."""
    mode = str(cfg.get("grade_recall") or "off")
    # an explicitly asked grade wins over the profile grade (same rule as the
    # answer node's grade focus) — recall the grade the question is really about.
    grade = state.get("question_grade") or state.get("user_school_grade")
    if mode != "from_pool_docs" or not grade:
        return 0
    file_uuids = {c.get("file_uuid") for c in chunks if c.get("file_uuid")}
    have = {(c.get("file_item_uuid"), c.get("chunk_index")) for c in chunks}
    if not file_uuids:
        return 0
    # the document's best rank, so a recalled chunk sorts behind that doc's
    # genuine matches rather than at the bottom of the pool
    best_rank: dict = {}
    for c in chunks:
        fu = c.get("file_uuid")
        rk = _rank_key(c)
        if fu and (fu not in best_rank or rk < best_rank[fu]):
            best_rank[fu] = rk
    added = 0
    cap = int(cfg.get("grade_recall_max", 8))
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id FROM {config.PG_SCHEMA}.resolved_code_list(%s,"
            f" 'school_grade') WHERE code IN (%s, '9999')",
            (state.get("legal_entity"), str(grade)))
        gids = [r[0] for r in cur.fetchall()]
        if not gids:
            return 0
        cur.execute(
            f"""
            SELECT t.id, t.file_uuid, t.file_item_uuid, t.chunk_index,
                   t.output_row_number, t.file_name,
                   m.source_url, m.original_file_name, m.ldts_gmt,
                   t.chunk_text, t.section_heading, t.subsection_heading,
                   t.heading_path, t.school_grade_ids, t.school_level
            FROM {config.CHUNKS_TABLE} t
            JOIN {config.METADATA_TABLE} m
              ON t.file_uuid = m.file_uuid
             AND t.processing_uuid = m.processing_uuid
             AND m.is_active = true
            WHERE t.file_uuid = ANY(%s::uuid[])
              AND t.school_grade_ids && %s::bigint[]{scope_cond}
            ORDER BY t.file_uuid, t.output_row_number, t.chunk_index
            LIMIT %s
            """, [list(file_uuids), gids, *scope_params, cap])
        for (chunk_id, file_uuid, file_item_uuid, chunk_index, output_row_number, file_name,
             source_url, original_file_name, ldts_gmt, chunk_text,
             section_heading, subsection_heading, heading_path, school_grade_ids, school_level) in cur.fetchall():
            if (str(file_item_uuid), chunk_index) in have:
                continue
            rk = best_rank.get(str(file_uuid))
            chunks.append({
                "retrieval_type": "document_grade_recall",
                "chunk_id": chunk_id,
                "file_uuid": str(file_uuid), "file_item_uuid": str(file_item_uuid),
                "chunk_index": chunk_index, "output_row_number": output_row_number,
                "file_name": str(file_name), "source_url": source_url,
                "original_file_name": original_file_name, "ldts_gmt": ldts_gmt,
                "chunk_text": chunk_text or "",
                "section_heading": section_heading, "subsection_heading": subsection_heading,
                "heading_path": heading_path,
                "school_grade_ids": list(school_grade_ids or []),
                "school_level": school_level,
                "is_sibling": True, "sibling_of": None,
                "lexical_rank": rk if rk and rk < 10 ** 6 else None,
                "vector_rank": None,
            })
            have.add((str(file_item_uuid), chunk_index))
            added += 1
    return added


def _rank_key(c: dict):
    """Best (lowest) position this chunk earned in any retriever's list."""
    return min([r for r in (c.get("lexical_rank"), c.get("vector_rank")) if r]
               or [10 ** 6])


def document_siblings(state: SearchState, params: dict | None = None) -> dict:
    if state.get("resolved"):
        return {}
    params = params or {}
    cfg = {**_DEFAULTS, **params}
    chunks = list(state.get("rag_chunks") or [])
    if not cfg.get("enabled", True) or cfg.get("skip_siblings") or not chunks:
        return {"rag_chunks": chunks}

    # a sibling is context for a chunk the caller already sees, but it is a
    # ROW of its own and has to pass the same test — otherwise completing a
    # section would be a way to read what the predicate just refused
    scope_cond, scope_params = scope.where(
        "t", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)
    # A SIBLING IS STILL A SOURCE. This step widens a hit to its neighbouring
    # chunks, and a neighbour reached by file_item_uuid would otherwise skip the
    # area filter entirely — the one shape of leak a "we only expand what we
    # already found" step invites. In practice a document's chunks share its
    # labels, so this changes nothing for a correctly-labelled corpus and
    # everything for a half-migrated one. Appended to scope_cond, so the grade
    # recall query below (which receives that string) is covered too.
    # Off in every active setting today (migration 164); tested forced on.
    dom_cond, dom_params = scope.domain_where("t", "document_domain_id",
                                              state.get("domain_ids"))
    scope_cond += dom_cond
    scope_params = [*scope_params, *dom_params]
    max_per = int(cfg.get("max_chunks_per_element", 6))
    window = int(cfg.get("window", 0) or 0)      # 0 = no hard distance cap

    # what the retrievers already hold, per element, and the best ranks that
    # element earned — a sibling inherits them so it sorts immediately BEHIND the
    # chunk that invited it. Without this it carries no rank at all and the fused
    # ordering drops it to the bottom, which is the same as not adding it.
    have: dict[str, set] = {}
    anchor: dict[str, dict] = {}
    for c in chunks:
        if c.get("chunk_index") is None:
            continue
        fiu = c["file_item_uuid"]
        have.setdefault(fiu, set()).add(c["chunk_index"])
        best = anchor.get(fiu)
        if best is None or _rank_key(c) < _rank_key(best):
            anchor[fiu] = c
    if not have:
        return {"rag_chunks": chunks}

    added = 0
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                SELECT t.file_uuid, t.file_item_uuid, t.chunk_index,
                       t.output_row_number, t.file_name,
                       m.source_url, m.original_file_name, m.ldts_gmt,
                       t.chunk_text, t.section_heading, t.subsection_heading,
                       t.heading_path, t.school_grade_ids, t.school_level,
                       t.id,
                       count(*) OVER (PARTITION BY t.file_item_uuid) AS n_in_element
                FROM {config.CHUNKS_TABLE} t
                JOIN {config.METADATA_TABLE} m
                  ON t.file_uuid = m.file_uuid
                 AND t.processing_uuid = m.processing_uuid
                 AND m.is_active = true
                WHERE t.file_item_uuid = ANY(%s::uuid[]){scope_cond}
                ORDER BY t.file_item_uuid, t.chunk_index
                """, [list(have.keys()), *scope_params])
            rows = cur.fetchall()

        # Order the candidates of each element by distance from the nearest match,
        # so filling the budget means "the closest context first". Done here rather
        # than in SQL because the matched set is per element and lives in Python.
        by_element: dict = {}
        for r in rows:
            by_element.setdefault(str(r[1]), []).append(r)
        ordered_rows = []
        for fiu, rs in by_element.items():
            matched = have.get(fiu) or set()
            rs_sorted = sorted(
                rs,
                key=lambda r: (min((abs(r[2] - m) for m in matched), default=10 ** 6),
                               r[2] if r[2] is not None else 0))
            ordered_rows.extend(rs_sorted)

        # budget per element, counting what was already matched
        room = {fiu: max(0, max_per - len(have.get(fiu) or set()))
                for fiu in by_element}
        for (file_uuid, file_item_uuid, chunk_index, output_row_number, file_name,
             source_url, original_file_name, ldts_gmt, chunk_text,
             section_heading, subsection_heading, heading_path, school_grade_ids, school_level,
             chunk_id, n_in_element) in ordered_rows:
            fiu = str(file_item_uuid)
            matched = have.get(fiu) or set()
            if chunk_index in matched:
                continue
            # a section larger than the budget is completed from the match OUTWARDS
            # until the budget is spent; below the budget it comes in whole
            if n_in_element > max_per:
                if room.get(fiu, 0) <= 0:
                    continue
                dist = min((abs(chunk_index - m) for m in matched), default=10 ** 6)
                if window and dist > window:
                    continue
                room[fiu] -= 1
            chunks.append({
                "retrieval_type": "document_siblings",
                "chunk_id": chunk_id,
                "file_uuid": str(file_uuid),
                "file_item_uuid": fiu,
                "chunk_index": chunk_index,
                "output_row_number": output_row_number,
                "file_name": str(file_name),
                "source_url": source_url,
                "original_file_name": original_file_name,
                "ldts_gmt": ldts_gmt,
                "chunk_text": chunk_text or "",
                "section_heading": section_heading,
                "subsection_heading": subsection_heading,
                "heading_path": heading_path,
                "school_grade_ids": list(school_grade_ids or []),
                "school_level": school_level,
                # No coverage and no score: context, not evidence. It does carry
                # the ranks of the chunk that invited it, marked `is_sibling`, so
                # the fused ordering keeps it just behind that chunk instead of
                # at the bottom of the pool.
                "is_sibling": True,
                "sibling_of": sorted(matched)[0] if matched else None,
                "lexical_rank": (anchor.get(fiu) or {}).get("lexical_rank"),
                "vector_rank": (anchor.get(fiu) or {}).get("vector_rank"),
            })
            added += 1
        logger.info("document_siblings: +%d chunks over %d sections (pool=%d, "
                    "max_per_element=%d, window=%d)",
                    added, len(have), len(chunks), max_per, window)
    except Exception as e:
        logger.warning("document_siblings: failed (%s)", e)

    # grade-anchored recall (own try — a failure here must not lose the
    # siblings just gathered)
    try:
        g_added = _grade_recall(state, chunks, scope_cond, scope_params, cfg)
        if g_added:
            logger.info("document_siblings: grade_recall +%d chunk(s) for grade %s",
                        g_added, state.get("question_grade") or state.get("user_school_grade"))
    except Exception as e:
        logger.warning("document_siblings: grade_recall failed (%s)", e)
    return {"rag_chunks": chunks}
