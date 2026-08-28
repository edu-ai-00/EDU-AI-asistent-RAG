# last change in code by Martin Safranek (2026-08-18).
"""Shared helpers for the QA cascade stages (exact / lexical / vector).

Everything here is deterministic. The QA index (qa_variants.ts) is
`to_tsvector('simple', norm_text || ' ' || lemma_text)`, so a query built from
the union of {norm, lemma, synonym} tokens matches against either surface.
"""

import json
import logging

from .. import config

logger = logging.getLogger(__name__)


# ── form selection (pipeline.params.form → which token form / tsvector column) ──
_FORM_TS = {"norm": "ts_norm", "lemma": "ts_lemma", "stem": "ts_stem"}
_FORM_TEXT = {"norm": "norm_text", "lemma": "lemma_text", "stem": "stem_text"}
_FORM_TOK = {"norm": "query_norm_tokens", "lemma": "query_lemma_tokens", "stem": "query_stem_tokens"}
_DEFAULT_FORM = ["norm", "lemma"]


def resolve_form(params) -> list[str]:
    """The token/index form(s) a lexical step matches on (default norm+lemma)."""
    form = (params or {}).get("form") or _DEFAULT_FORM
    if isinstance(form, str):
        form = [form]
    form = [f for f in form if f in _FORM_TS]
    return form or list(_DEFAULT_FORM)


def form_ts(form, alias: str) -> str:
    """tsvector expression = the chosen columns concatenated (rank/match as one)."""
    return "(" + " || ".join(f"{alias}.{_FORM_TS[f]}" for f in form) + ")"


def form_text_concat(form, alias: str) -> str:
    """SQL concat of the chosen form's text columns (for coverage token extraction)."""
    return "concat_ws(' ', " + ", ".join(f"{alias}.{_FORM_TEXT[f]}" for f in form) + ")"


def form_query_tokens(state, form) -> set[str]:
    toks: set[str] = set()
    for f in form:
        toks |= set(state.get(_FORM_TOK[f], []) or [])
    return toks


def form_text_col(f) -> str:
    return _FORM_TEXT[f]


def form_token_key(f) -> str:
    return _FORM_TOK[f]


def content_tokens(state, form) -> list[str]:
    """The reduced-form tokens used as coverage 'concepts' (lemma > stem > norm)."""
    for f in ("lemma", "stem", "norm"):
        if f in form:
            return state.get(_FORM_TOK[f], []) or []
    return state.get("query_lemma_tokens", []) or []


def pack_filter(pack_ids, alias: str = "e", language: str | None = None):
    """SQL fragment + params restricting to active entries in the scoped packs.

    Empty pack_ids → all active packs (single-pack today).
    `language` (číselník code, validated upstream) additionally restricts to
    packs whose manifest lang matches — the /search language filter.
    """
    frag = f"{alias}.is_active"
    params: list = []
    if pack_ids:
        frag += f" AND {alias}.pack_id = ANY(%s)"
        params.append(list(pack_ids))
    if language:
        frag += (f" AND {alias}.pack_id IN (SELECT pack_id FROM {config.QA_MANIFEST_TABLE} "
                 "WHERE is_active AND lang = %s)")
        params.append(language)
    return frag, params


def load_synonym_groups(cur, lang: str | None = None):
    """Return (groups, kind_of, phrase_groups, terms):
        groups   group_id → set of all norm+lemma+stem tokens across the group's
                 terms. All three forms are unioned so expansion targets every
                 branch of the lexical tsexpr (ts_lemma || ts_stem) — a stem-only
                 chunk match is bridged just like a lemma one.
        kind_of  group_id → 'entity' | 'interrogative'

    `lang` (číselník code) restricts to that language's groups; None loads all.
    Cross-language groups are harmless for matching (the tokens never meet) but
    they inflate the tsquery and would collide on a homonym, so callers pass the
    resolved query language.
    """
    cur.execute(
        f"SELECT group_id, kind, term_norm, term_lemma, term_stem "
        f"FROM {config.QA_SYNONYM_TABLE} WHERE is_active "
        f"AND (%s IS NULL OR lang = %s)", (lang, lang)
    )
    groups: dict[str, set[str]] = {}
    kind_of: dict[str, str] = {}
    # A group is PHRASE-LIKE when any of its terms is more than one word
    # ("individuální vzdělávací plán", "ozdravný pobyt"). Such a group must not be
    # poured into every concept of the query — see expand_query.
    phrase_groups: set[str] = set()
    # PER-TERM TOKEN SETS, kept alongside the flat bag. The bag is what the
    # tsquery needs (expansion); the per-term sets are what MATCHING needs, and
    # collapsing the two was the bug: a group is a bag of tokens, so a single
    # common token fired the whole group. Measured 2026-08-26 on "co si mají děti
    # přinést do školy" (question_answer_log 2894): the token `skola` woke the
    # OZP group, which then counted as a covered concept, coverage hit 1.00 and a
    # curated answer about ABSENCE ended the cascade before the document path ran.
    # 22 % of logged queries contain "škol", so this is not an edge case.
    terms: dict[str, list[set]] = {}
    for gid, kind, term_norm, term_lemma, term_stem in cur.fetchall():
        parts = [(term_norm or "").split(), (term_lemma or "").split(),
                 (term_stem or "").split()]
        toks = set().union(*[set(p) for p in parts]) if parts else set()
        groups.setdefault(gid, set()).update(t for t in toks if t)
        for p in parts:                      # one entry per FORM of this term
            ts = {t for t in p if t}
            if ts:
                terms.setdefault(gid, []).append(ts)
        kind_of[gid] = kind
        if any(len(p) > 1 for p in parts):
            phrase_groups.add(gid)
    return groups, kind_of, phrase_groups, terms


# FALLBACK ONLY — the live list is qa_stopword with scope='coverage' (migration
# 60), so it can be extended without a rebuild. Kept here so a database that
# cannot be read still behaves sanely.
# Minimal Czech function-word stoplist. Excluded from COVERAGE only (they match
# almost every entry and inflate confidence) — they stay in the tsquery. NOT a
# semantic filter; interrogatives (kdy/kde/jak…) are intentionally NOT here, they
# carry intent and drive synonym expansion. Normalized (no diacritics) forms.
_COVERAGE_STOP = {
    "byt", "je", "jsou", "byl", "byla", "bylo", "budou", "bude",
    "a", "i", "o", "u", "v", "ve", "z", "ze", "s", "se", "si", "k", "ke",
    "na", "do", "od", "po", "za", "pro", "pri", "nad", "pod", "the",
    "to", "ten", "ta", "ty", "toto", "tak", "ale", "nebo", "ze", "co",
    "muj", "tvuj", "mi", "me", "ja", "my", "vy", "on", "ona", "ono",
}


def expand_query(query_tokens, lemma_tokens, groups, kind_of, coverage_stop=None,
                 phrase_groups=None, terms=None):
    """Expand query tokens via synonym groups.

    query_tokens  norm ∪ lemma — drives the tsquery (recall) and group matching.
    lemma_tokens  lemma only — the CONTENT concepts for coverage. Using lemma
                  (canonical) avoids double-counting a word's surface + root form
                  as two concepts (e.g. "matematice" vs "matematika"), which
                  otherwise halved coverage and caused false-negative fallthrough.

    Returns:
      all_terms       set of tsquery terms. ONLY entity groups are expanded
                      (JPZ=přijímačky…). Interrogative groups (kdy→termín/den…)
                      are NOT expanded: doing so floods the OR-query, flattens
                      ts_rank across every time-mentioning entry and collapses
                      the top1↔top2 margin. Interrogatives stay as their literal
                      token so a "kdy" question still ranks entries that contain
                      "kdy" (the date rows) above the rest.
      concept_sets    per CONTENT query token, the set of tokens that "cover" it.
                      Function words (_COVERAGE_STOP) AND interrogative tokens are
                      dropped — coverage measures CONTENT-word overlap, so it is
                      not inflated by the (undiscriminative) question word.
    """
    # Coverage stoplist comes from the číselník (qa_stopword scope='coverage');
    # _COVERAGE_STOP below is only the fallback when the table cannot be read.
    stop = coverage_stop if coverage_stop is not None else _COVERAGE_STOP
    query_set = {t for t in query_tokens if t}
    # A GROUP WAKES ON A TERM, NOT ON A TOKEN. "škola v přírodě" needs both of its
    # words present; the single word "škola" (in 22 % of all queries) must wake
    # nothing. A one-word term (OZP, LVK, IVP) still wakes on its own token, so
    # abbreviations keep working. Without `terms` the old token-level behaviour
    # remains, so a caller that has not been updated does not break.
    if terms:
        matched_groups = [gid for gid, tl in terms.items()
                          if any(ts and ts <= query_set for ts in tl)]
    else:
        matched_groups = [gid for gid, toks in groups.items() if toks & query_set]

    # interrogative tokens across ALL such groups — never counted as content
    interrogatives: set[str] = set()
    for gid, toks in groups.items():
        if kind_of.get(gid) == "interrogative":
            interrogatives |= toks

    expansion: set[str] = set()
    for gid in matched_groups:
        if kind_of.get(gid) == "entity":
            expansion |= groups[gid]

    # A MULTIWORD synonym must not be poured into every concept it touches.
    # Measured 2026-08-23 on "Jak požádat o individuální vzdělávací plán?": the
    # group syn_ivp holds the term "individuální vzdělávací plán", so its token set
    # {ivp, individualni, vzdelavaci, plan} touched ALL THREE concepts of the query
    # and made them IDENTICAL — after which a chunk holding only the word
    # "vzdělávací" covered 3 of 3 concepts, exactly like the school-rules chunk
    # holding the whole term. CERMAT's admission guide won on that tie and the
    # answer came back empty; with the group switched off the school rules covered
    # three distinct concepts (df 5.1 %, 9.9 %, 0.35 %) against CERMAT's one and
    # answered correctly. So a phrase group is appended as ONE EXTRA concept ("the
    # term as a whole") instead: the phrase keeps discriminating, and a chunk that
    # writes only the abbreviation still covers that one concept and stays
    # reachable. Single-word groups (LVK, OZP, ŠPZ, MŠMT) behave as before.
    _phrase = set(phrase_groups or ())
    concept_sets: list[set[str]] = []
    seen: set[str] = set()
    for qt in lemma_tokens:
        if not qt or qt in stop or qt in interrogatives or qt in seen:
            continue
        seen.add(qt)
        ms = {qt}
        for gid in matched_groups:
            if (kind_of.get(gid) == "entity" and gid not in _phrase
                    and qt in groups[gid]):
                ms |= groups[gid]
        concept_sets.append(ms)
    for gid in matched_groups:
        if kind_of.get(gid) == "entity" and gid in _phrase:
            # THE GROUP CONCEPT MUST BE CONFIRMED BY A WORD THAT MEANS SOMETHING.
            # Its token bag holds the phrase's common words too ("škola"), so an
            # entry mentioning only those covered the concept — the query's own
            # word was counted twice, once as itself and once as "the term". Words
            # already standing as their own concept are therefore removed, leaving
            # the distinctive ones (ozp, ozdravn, pobyt, prirod). An empty
            # remainder means the group added nothing and no concept is appended.
            _extra = {t for t in groups[gid] if t not in stop and t not in seen}
            if _extra:
                concept_sets.append(_extra)

    return (query_set | expansion), concept_sets


def coverage(concept_sets, entry_tokens: set[str]) -> float:
    """Fraction of query concepts present (directly or via synonym) in an entry."""
    if not concept_sets:
        return 0.0
    covered = sum(1 for ms in concept_sets if ms & entry_tokens)
    return covered / len(concept_sets)


def concept_idf_weights(cur, concept_sets, frag, params) -> list[float]:
    """One weight per query concept: how DISCRIMINATIVE it is inside the scoped
    QA packs, computed from the data (ln((N+1)/(df+1)) + 1, plain IDF).

    Unweighted coverage treats every concept as equal, and in a school FAQ that
    is exactly wrong: "skola" appears in most of the 72 entries and "moci" is a
    modal that appears everywhere, so the query "Může se ve škole fotit?"
    covered 2/3 of its concepts on a совершенно unrelated entry — while the ONE
    concept that carried the meaning ("vyfotit", df=0) was the uncovered third
    (2026-07-31, Martin's question: why does the rare term not weigh more?).
    IDF answers it from the data itself — no hand-maintained modal-verb list,
    and it works for every language and pack automatically.

    df = number of ACTIVE entries whose variants contain any of the concept's
    tokens; one grouped query over the scoped packs, so the cost is one cheap
    aggregate per request.
    """
    import math
    if not concept_sets:
        return []
    selects = []
    args: list = []
    for cs in concept_sets:
        selects.append(
            "count(DISTINCT e.entry_id) FILTER (WHERE v.ts_norm || v.ts_lemma "
            "@@ to_tsquery('simple', %s))")
        args.append(" | ".join(sorted(cs)))
    cur.execute(
        f"SELECT count(DISTINCT e.entry_id), {', '.join(selects)} "
        f"FROM {config.QA_VARIANTS_TABLE} v "
        f"JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id "
        f"WHERE {frag}", args + list(params))
    row = cur.fetchone()
    total = max(int(row[0] or 0), 1)
    dfs = [int(df or 0) for df in row[1:]]
    weights = [math.log((total + 1) / (d + 1)) + 1.0 for d in dfs]
    # The df/N pair is the ONLY way to read a coverage number afterwards: the
    # weights depend on what was in scope at request time, so a score logged
    # today cannot be reconstructed tomorrow (the corpus moves). Stash them for
    # the trace instead of recomputing later — see qa_lexical's `concepts`.
    concept_idf_weights.last_df = dfs          # type: ignore[attr-defined]
    concept_idf_weights.last_total = total     # type: ignore[attr-defined]
    return weights


concept_idf_weights.last_df = []       # type: ignore[attr-defined]
concept_idf_weights.last_total = 0     # type: ignore[attr-defined]


def weighted_coverage(concept_sets, entry_tokens: set[str], weights) -> float:
    """Coverage with per-concept IDF weights; falls back to plain coverage."""
    if not concept_sets:
        return 0.0
    if not weights or len(weights) != len(concept_sets):
        return coverage(concept_sets, entry_tokens)
    covered = sum(w for ms, w in zip(concept_sets, weights) if ms & entry_tokens)
    return covered / sum(weights)


def fetch_answer(cur, entry_id: str):
    """(canonical_q, answer_md, tags, asset_refs) for one entry."""
    cur.execute(
        f"SELECT canonical_q, answer_md, tags, asset_refs FROM {config.QA_ENTRIES_TABLE} "
        f"WHERE entry_id = %s", (entry_id,),
    )
    row = cur.fetchone()
    return row if row else (None, None, None, None)


def sources_json(asset_refs) -> str:
    """qa_entries.asset_refs -> the wire's `sources` array, as the state's JSON string.

    A QA source is the CURATOR'S citation, not retrieval provenance: it is what
    the pack's `sources` column named, so there is no file_uuid and no score to
    put on it. Only the two keys every client already renders are emitted (rank +
    title, url when there is one), which is exactly the RagSource subset
    web_chat's map_sources reads.

    Anything malformed collapses to "[]": a curated answer must still be
    delivered when its citation is junk.
    """
    if isinstance(asset_refs, str):
        try:
            asset_refs = json.loads(asset_refs)
        except (ValueError, TypeError):
            return "[]"
    if not isinstance(asset_refs, list):
        return "[]"
    out = []
    for item in asset_refs:
        if not isinstance(item, dict):
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else None
        title = item.get("title") if isinstance(item.get("title"), str) else None
        title = (title or url or "").strip()
        if not title:
            continue
        out.append({"rank": len(out) + 1, "title": title, "url": url})
    return json.dumps(out, ensure_ascii=False)


def default_thresholds() -> tuple[float, float, float]:
    """(t_answer, t_suggest, m_margin) from config — the last-resort fallback."""
    return config.QA_T_ANSWER, config.QA_T_SUGGEST, config.QA_M_MARGIN


def load_thresholds_by_pack(cur, pack_ids, language: str | None = None) -> dict:
    """{pack_id: (t_answer, t_suggest, m_margin)} for the active packs in scope.

    Thresholds belong to the PACK, not to the run: several packs can be active
    at once (pack_id = language-subject-grade, so one language may hold many),
    and each may be calibrated differently — an English pack needs a lower
    coverage bar than a Czech one (shorter queries), a narrow-subject pack a
    lower one than a general FAQ. The caller therefore looks the values up by
    the pack of the entry it actually matched. (Until 2026-07-28 this was a
    single `LIMIT 1` row, i.e. an arbitrary pack's thresholds applied to every
    query once more than one pack existed — tech-debt C5.)
    """
    out: dict[str, tuple[float, float, float]] = {}
    try:
        cur.execute(
            f"SELECT pack_id, t_answer, t_suggest, m_margin FROM {config.QA_MANIFEST_TABLE} "
            f"WHERE is_active "
            f"AND (%s IS NULL OR lang = %s) "
            f"AND (%s IS NULL OR pack_id = ANY(%s))",
            (language, language, list(pack_ids) if pack_ids else None,
             list(pack_ids) if pack_ids else None),
        )
        for pack_id, t_a, t_s, m_m in cur.fetchall():
            if None in (t_a, t_s, m_m):
                continue
            out[pack_id] = (float(t_a), float(t_s), float(m_m))
    except Exception as e:
        logger.warning("load_thresholds_by_pack: %s", e)
    return out
