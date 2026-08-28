# last change in code by Jan Drozd and Martin Safranek (2026-08-19).
"""Load the active search-algorithm configuration from edu_ai.settings.

The cascade is config-driven (RAG redesign — configurable search): its pipeline
(ordered, reorderable, with fallback), the stemmer/lemmatizer engines, and the
normalization + evaluation params all live in one versioned `config` jsonb, of
which exactly one row is is_active.

A tiny in-process cache avoids a DB round-trip per query; call `refresh()` after
switching the active setting (or `get_active_setting(force=True)`). If the table
is missing or empty the built-in `_FALLBACK` (today's baseline) is used so search
never hard-fails on a config problem.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import get_cursor
from . import config

logger = logging.getLogger(__name__)


# Baseline used only if the DB has no active setting (fresh/broken install). Kept
# byte-compatible with the v1 seed in postgres/init/10_settings.sql.
_FALLBACK: dict[str, Any] = {
    "tokenization": {"active": True},
    "normalization": {"lowercase": True, "strip_diacritics": True, "drop_stopwords": True},
    # Follow-up resolution. Not a pipeline step: it must run before `prepare`
    # builds the token forms, and the pipeline starts after that.
    "context": {"enabled": True, "max_turns": 4, "window": "2 hours",
                "model": config.CONTEXT_MODEL, "max_tokens": 120},
    "engines": [
        {"name": "simplemma", "kind": "lemmatizer", "column": "lemma_text",
         "active": {"ingest_qa": True, "ingest_docs": True}},
        {"name": "dolamic_savoy_light", "kind": "stemmer", "column": "stem_text",
         "params": {"min_len": 3, "aggressive": False},
         "active": {"ingest_qa": True, "ingest_docs": True}},
    ],
    "pipeline": [
        {"step": "route", "enabled": True,
         "note": "Deterministic guard (usable? + qa_blocklist; self_harm→Linka bezpečí) "
                 "and QA pack scope by grade. No LLM, no retrieval — just admit/deny + scope."},
        # QA block first (curated pairs): exact → lexical → vector.
        {"step": "qa_exact", "enabled": True, "on_hit": "stop"},
        {"step": "qa_lexical", "enabled": True, "on_hit": "stop",
         "params": {"form": ["norm", "lemma", "stem"], "t_answer": config.QA_T_ANSWER,
                    "t_suggest": config.QA_T_SUGGEST, "m_margin": config.QA_M_MARGIN,
                    "trgm_suggest_min": 0.40, "fetch_k": config.QA_LEXICAL_FETCH_K,
                    "suggest_top_n": config.QA_SUGGEST_TOP_N, "synonyms": True}},
        # Lexical/keyword first — QA lexical, then document fulltext + keywords.
        {"step": "document_lexical", "enabled": True, "on_hit": "continue",
         "params": {"form": ["lemma", "stem"], "fetch_k": config.RAG_LEXICAL_FETCH_K,
                    "min_coverage": 0.5}},
        {"step": "rag_keywords", "enabled": True, "on_hit": "continue", "only_if_empty": True,
         "params": {"form": ["lemma", "stem"], "min_overlap": 1, "fetch_k": config.KEYWORD_FETCH_K}},
        # Vectors grouped last: curated QA vector, then document vector (last resort).
        {"step": "qa_vector", "enabled": True, "on_hit": "stop",
         "params": {"cosine_min": config.QA_VECTOR_COSINE_MIN}},
        {"step": "rag_vector", "enabled": True, "on_hit": "continue", "only_if_empty": True,
         "params": {"top_k": config.VECTOR_TOP_K, "fetch_k": config.VECTOR_FETCH_K,
                    "cosine_min": 0.65}},
        {"step": "answer", "enabled": True},
    ],
}
_FALLBACK_ID = "00000000-0000-0000-0000-000000000000"

_cache: dict[str, Any] | None = None
# When the cache was last loaded. A process-lifetime cache made every settings
# edit need a container restart — measured on child_may_publish_organization: the
# switch was flipped in the DB and /ingest-principal kept answering by the old
# value. Nothing ever called refresh(); a short TTL keeps the per-query DB
# round-trip amortised while making "edit the settings row" actually take effect.
_cache_at: float = 0.0
_CACHE_TTL_S = 5.0


def _load_stopwords() -> dict[str, list[str]]:
    """Active stoplists from the qa_stopword číselník, per language (data, not
    code — MD §2.2). Kept in its own table because it changes more often than
    the search config. Empty → the token functions fall back to the in-code
    STOPWORDS/STOPWORDS_EN sets."""
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT lang, term FROM {config.PG_SCHEMA}.qa_stopword "
                "WHERE is_active AND scope = 'index'"
            )
            out: dict[str, list[str]] = {}
            for lang, term in cur.fetchall():
                out.setdefault(lang, []).append(term)
            return out
    except Exception as e:
        logger.warning("settings: stopword load failed (%s) — built-in fallback", e)
        return {}


def ancestor_entities(legal_entity: str | None) -> list[str]:
    """The entity itself and everything ABOVE it.

    This is the set a CALLER may legitimately act as. A user of the top-level school
    may search or ingest as one of its units; a user of a unit may not act as the
    school, and never as a sibling unit. organization_entities() is too loose for
    this — it includes descendants, so it would let a primary-school teacher act as
    the secondary school.
    """
    if not legal_entity:
        return []
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE up AS (
                    SELECT legal_entity_code, parent_code
                      FROM {config.PG_SCHEMA}.legal_entity
                     WHERE legal_entity_code = %s AND is_active
                    UNION ALL
                    SELECT p.legal_entity_code, p.parent_code
                      FROM {config.PG_SCHEMA}.legal_entity p
                      JOIN up ON up.parent_code = p.legal_entity_code
                     WHERE p.is_active
                )
                SELECT legal_entity_code FROM up
                """, (legal_entity,))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("ancestor_entities(%s) failed (%s) — returning none",
                       legal_entity, e)
        return []


def verify_user(user_id: str | None, org_entities: list[str]) -> bool:
    """Is this user_id a real, active user of this organisation?

    /search takes user_id from the request BODY and nothing has ever checked it.
    That was harmless while it only landed in the log; it stopped being harmless
    when `visibility = 'private'` started using it — `owner_user_id = %s` against an
    unverified value means anyone who knows or guesses another person's id reads
    their private documents.

    Both kinds of user count: a staff member who ingests, and an app installation
    that only asks. Both are looked up inside the RESOLVED ORGANISATION, so a real
    id from another tenant is as invalid as an invented one.

    Returns False rather than raising: an unverifiable id makes the caller
    anonymous — the private branch is simply not offered — instead of failing a
    search that would otherwise work. A refused request would tell an attacker
    which ids exist.
    """
    if not user_id or not org_entities:
        return False
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                SELECT 1 FROM {config.PG_SCHEMA}.staff_user
                 WHERE user_id = %s AND legal_entity_code = ANY(%s) AND is_active
                UNION ALL
                SELECT 1 FROM {config.PG_SCHEMA}.app_user
                 WHERE user_id = %s AND legal_entity_code = ANY(%s) AND is_active
                 LIMIT 1
                """, (user_id, org_entities, user_id, org_entities))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("verify_user(%s) failed (%s) — treating as anonymous",
                       user_id, e)
        return False


def subtree_entities(legal_entity: str | None) -> list[str]:
    """The entity itself and everything BELOW it.

    This is what `visibility = 'entity'` is read against, and the direction is
    upward from the document's point of view: a unit's own document is seen by that
    unit AND by the legal person above it, but not by a sibling unit. Turned around
    for the searcher, that is exactly "documents of me or of anyone under me".

    The head office therefore sees what its units publish for themselves, while a
    unit sees nothing of its siblings — and nothing the head office kept at
    `entity` level either, which is how the head office keeps an internal note
    internal.

    Returns [] for an unknown entity; the caller must treat that as "see nothing".
    """
    if not legal_entity:
        return []
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE down AS (
                    SELECT legal_entity_code
                      FROM {config.PG_SCHEMA}.legal_entity
                     WHERE legal_entity_code = %s AND is_active
                    UNION ALL
                    SELECT c.legal_entity_code
                      FROM {config.PG_SCHEMA}.legal_entity c
                      JOIN down ON c.parent_code = down.legal_entity_code
                     WHERE c.is_active
                )
                SELECT legal_entity_code FROM down
                """, (legal_entity,))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("subtree_entities(%s) failed (%s) — returning none",
                       legal_entity, e)
        return []


def organization_entities(legal_entity: str | None) -> list[str]:
    """Every entity of the same organisation: the root legal person and all
    entities under it, including the one asked about.

    `visibility = 'organization'` means "everyone in this legal person", so the
    question is not "who is above me" but "who shares my root". Answered by walking
    UP to the root and then DOWN over its whole subtree, in one statement — a
    document is visible to a sibling unit, to the head office and to a unit below,
    which implicit ancestor-inheritance could not express.

    Returns [] for an unknown entity. The caller must treat that as "see nothing";
    scope.where() does.
    """
    if not legal_entity:
        return []
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE up AS (
                    SELECT legal_entity_code, parent_code
                      FROM {config.PG_SCHEMA}.legal_entity
                     WHERE legal_entity_code = %s AND is_active
                    UNION ALL
                    SELECT p.legal_entity_code, p.parent_code
                      FROM {config.PG_SCHEMA}.legal_entity p
                      JOIN up ON up.parent_code = p.legal_entity_code
                     WHERE p.is_active
                ),
                root AS (
                    SELECT legal_entity_code FROM up WHERE parent_code IS NULL
                     LIMIT 1
                ),
                down AS (
                    SELECT legal_entity_code FROM root
                    UNION ALL
                    SELECT c.legal_entity_code
                      FROM {config.PG_SCHEMA}.legal_entity c
                      JOIN down ON c.parent_code = down.legal_entity_code
                     WHERE c.is_active
                )
                SELECT legal_entity_code FROM down
                """, (legal_entity,))
            found = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("organization_entities(%s) failed (%s) — returning none",
                       legal_entity, e)
        return []
    # No fallback to [legal_entity]: an unknown entity must resolve to NOTHING, and
    # a fallback here would hand it its own name and let scope.where build a real
    # predicate around it. Every existing row reaches a root — a unit is required to
    # have a parent (legal_entity_unit_has_parent) — so an empty result means the
    # entity does not exist or is inactive.
    return found


def coverage_stopwords() -> dict[str, set[str]]:
    """Terms excluded from COVERAGE but kept in the tsquery (qa_stopword,
    scope='coverage').

    A different job from the index stoplist above: "mít" should still help
    retrieve a passage, it just must not raise confidence — counting it made
    "Jaké mám mít pomůcky?" score 1/3 and fall under min_coverage even though its
    one content word matched (see migration 60).
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT lang, term FROM {config.PG_SCHEMA}.qa_stopword "
                "WHERE is_active AND scope = 'coverage'"
            )
            out: dict[str, set[str]] = {}
            for lang, term in cur.fetchall():
                out.setdefault(lang, set()).add(term)
            return out
    except Exception as e:
        logger.warning("settings: coverage stoplist load failed (%s) — code set", e)
        return {}


_terms_cache: dict[str, tuple[float, dict[str, set[str]]]] = {}


def term_list(scope: str) -> dict[str, set[str]]:
    """Active qa_stopword terms for one `scope`, grouped by language.

    Cached with the settings TTL because two callers ask per HEADING, not per
    request (_usable_as_query runs over every candidate chunk), and a DB round
    trip each time would show up in latency.

    Never raises: an empty dict makes the caller keep its built-in list, which is
    the behaviour that shipped before the číselník held these words.
    """
    import time
    now = time.monotonic()
    hit = _terms_cache.get(scope)
    if hit and (now - hit[0]) <= _CACHE_TTL_S:
        return hit[1]
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT lang, term FROM {config.PG_SCHEMA}.qa_stopword "
                "WHERE is_active AND scope = %s", (scope,))
            out: dict[str, set[str]] = {}
            for lang, term in cur.fetchall():
                out.setdefault(lang, set()).add(term)
    except Exception as e:
        logger.warning("settings: term_list(%s) load failed (%s) — caller default",
                       scope, e)
        return {}
    _terms_cache[scope] = (now, out)
    return out


def terms_any_language(scope: str, fallback: set[str]) -> set[str]:
    """Every term for `scope`, all languages merged; `fallback` when empty.

    Merged on purpose. Both callers run where the query language is not yet
    known — resolve_context decides BEFORE the cascade, and a heading carries the
    DOCUMENT's language, not the asker's. Merging can only make the check more
    eager, and for both of them being eager is the harmless direction: an extra
    context rewrite costs one LLM call, and an over-cleaned heading is simply not
    offered as a suggestion. Same reasoning as the blocklist, which also checks
    every language at once so a Czech child typing an English word is caught.
    """
    per_lang = term_list(scope)
    merged = set().union(*per_lang.values()) if per_lang else set()
    return merged or fallback


def terms_for_language(scope: str, lang: str | None,
                      fallback: set[str]) -> set[str]:
    """Terms for `scope` in ONE language; merged across languages as a last resort.

    Prefer this over terms_any_language() wherever the language is actually
    known — which is almost everywhere, because retrieval already filters
    documents by it (`coalesce(langdetect_result, document_language_user,
    document_language_llm) = %s`), so a chunk in the pool has the query's
    language by construction.

    It matters because the Czech and English lists overlap on real words: `to`,
    `on`, `ta`, `ty` and `tam` are Czech pro-forms AND ordinary English words, so
    a merged list marks nearly every English question as context-dependent.

    Order: the language's own rows → the merged set (a language with no rows of
    its own is better served by another language's than by nothing) → `fallback`
    when the číselník is silent about this scope entirely.
    """
    per_lang = term_list(scope)
    if not per_lang:
        return fallback
    own = per_lang.get((lang or "").lower())
    if own:
        return own
    return set().union(*per_lang.values()) or fallback


def bm25_stopwords(scope: str = "bm25") -> dict[str, set[str]]:
    """Terms force-dropped from the BM25 QUERY (qa_stopword), by scope.

    Two scopes:
      * 'bm25'       — dropped in EVERY lexical round. The explicit, human-curated
        half of BM25 stopping; the automatic half is the df-gate in document_lexical
        (a term in more than `bm25_max_df_share` of the visible corpus is generic).
        Empty is normal — the df-gate already handles domain-common words like "škola".
      * 'bm25_grade' — dropped ONLY in the GRADE round, where school_grade_ids is a
        HARD filter: every chunk there already carries the grade, so "ročník/třída/
        stupeň" (and bare digits) distinguish nothing and only reward a chunk that
        repeats the grade number (the event calendar beating the supply list). Grade
        is a metadata filter, not a topic — keep it out of the round's BM25 scoring.

    Its own číselník because it changes more often than the search config (MD §2.2)."""
    return term_list(scope)


def answer_exclusion_patterns(lang: str = "cs") -> list[str] | None:
    """Regex FRAGMENTS marking "document doesn't answer" meta-sentences
    (answer_exclusion_rule číselník — the answer-side mirror of
    chunk_exclusion_rule). The caller composes them with '|'.

    Returns [] when the číselník has no active rows for the language — that is
    the KILL SWITCH, the backstop stays off. Returns None only when the table
    cannot be read at all, so the caller can fall back to the built-in set
    instead of silently losing the guard on a transient DB hiccup."""
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT match_regex FROM {config.PG_SCHEMA}.answer_exclusion_rule "
                "WHERE is_active AND lang = %s ORDER BY id", (lang,))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("settings: answer exclusion rules load failed (%s) — built-in", e)
        return None


def _load(legal_entity: str | None = None) -> dict[str, Any]:
    """The active setting for `legal_entity`'s ORGANISATION, else the global one.

    A settings row belongs to a top-level entity and applies to its whole tree
    (init 98); legal_entity may be any entity — the query walks up to its root.
    One query, org row wins over global (ORDER BY ... NULLS LAST): the same
    resolution the chunking components use on the ingest side, so a document is
    chunked and searched under the same organisation's config.
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE up AS (
                    SELECT legal_entity_code, parent_code
                    FROM {config.PG_SCHEMA}.legal_entity
                    WHERE legal_entity_code = %s
                    UNION ALL
                    SELECT l.legal_entity_code, l.parent_code
                    FROM {config.PG_SCHEMA}.legal_entity l
                    JOIN up ON l.legal_entity_code = up.parent_code
                )
                SELECT s.setting_id, s.label, s.config
                FROM {config.PG_SCHEMA}.settings s
                WHERE s.is_active
                  AND (s.legal_entity_code IS NULL
                       OR s.legal_entity_code IN (SELECT legal_entity_code
                                                  FROM up WHERE parent_code IS NULL))
                ORDER BY s.legal_entity_code NULLS LAST
                LIMIT 1
                """,
                (legal_entity,),
            )
            row = cur.fetchone()
        if row and isinstance(row[2], dict) and row[2].get("pipeline"):
            cfg = row[2]
            # inject the stoplist from its separate číselník into the config the
            # token functions read (search-parity: index side loads the same table)
            sw = _load_stopwords()
            if sw:
                norm = cfg.setdefault("normalization", {})
                norm["stopwords_by_lang"] = sw
                # No legacy flat `stopwords` key any more: it held the CZECH list
                # by construction, so Czech was the one language served from a
                # differently-shaped key. stopwords_of() reads stopwords_by_lang
                # only (eduai_normalize 1.4.0).
            logger.info("settings: active setting %s (%s), stopwords per lang: %s",
                        row[0], row[1], {k: len(v) for k, v in sw.items()})
            return {"setting_id": str(row[0]), "label": row[1], "config": cfg}
    except Exception as e:
        logger.warning("settings: load failed (%s) — using built-in fallback", e)
    return {"setting_id": _FALLBACK_ID, "label": "built-in fallback", "config": _FALLBACK}


def get_active_setting(legal_entity: str | None = None,
                       force: bool = False) -> dict[str, Any]:
    """{setting_id, label, config} for the entity's organisation (global when
    the organisation has no row of its own, or when no entity is given).
    Cached per entity with the shared TTL."""
    global _cache, _cache_at
    import time
    now = time.monotonic()
    if _cache is None or force or (now - _cache_at) > _CACHE_TTL_S:
        _cache = {}
        _cache_at = now
    key = legal_entity or ""
    if key not in _cache:
        _cache[key] = _load(legal_entity)
    return _cache[key]


def platform_setting(key: str, default: Any = None) -> Any:
    """One value from config.search on the GLOBAL row — same home as
    search_requires_user and max_result_documents.

    Platform-wide by construction: it reads legal_entity=None, so a school row
    cannot override it. Use this for anything a single school must not be able to
    change (input validation ranges, operational windows, process-singleton model
    settings) and per-step `params` for everything tunable per school.

    Never raises. A missing key, a NULL config, or a DB blip all return
    `default`, because every caller here is on a path where failing closed would
    be worse than running on the shipped value.
    """
    try:
        sec = (get_active_setting(None)["config"] or {}).get("search") or {}
    except Exception as e:
        logger.warning("platform_setting(%s): settings unavailable (%s)"
                       " — using default %r", key, e, default)
        return default
    v = sec.get(key)
    return default if v is None else v


def platform_int(key: str, default: int) -> int:
    """platform_setting() coerced to int; junk in the config falls back."""
    v = platform_setting(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        logger.warning("platform_setting(%s)=%r is not a number — using %d",
                       key, v, default)
        return default


def refresh() -> dict[str, Any]:
    """Reload the global active setting from the DB (drops the whole cache)."""
    return get_active_setting(force=True)
