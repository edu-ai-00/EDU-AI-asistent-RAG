# last change in code by Martin Safranek (2026-08-18).
"""Shared helpers for the QA cascade stages (exact / lexical / vector).

Everything here is deterministic. The QA index (qa_variants.ts) is
`to_tsvector('simple', norm_text || ' ' || lemma_text)`, so a query built from
the union of {norm, lemma, synonym} tokens matches against either surface.
"""

import json
import logging

from .. import config
from .. import scope

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

    NOTE for anyone adding a caller: an empty pack_ids means NO PACK RESTRICTION,
    which is why route._scope returns the `~none~` sentinel rather than [] when
    it finds nothing or fails. Do not "simplify" that away.
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


def domain_filter(state, alias: str):
    """(fragment, params) restricting a SCANNED QA row to the chosen areas.

    Thin, and that is the point: every QA query in the cascade — the three
    stages, the trgm typo fallback, the IDF denominator, the category siblings
    and the other-ročník net — gets its area predicate from this one call, so a
    new query cannot be written without one and the six cannot drift apart.
    Which was the actual failure mode before: the visibility rule was correct in
    six places and absent in the seventh for months.

    `alias` is the table being SCANNED (v = qa_variants, q = qa_embedding,
    e = qa_entries). It is a constant at each call site, never a request value.
    The column is `domain_ids` on all three — migrations 140 and 144 denormalised
    it onto both scan surfaces precisely so a filter need not reach through a
    join, which for the ANN scan in qa_vector would mean filtering AFTER the
    index has already picked the neighbours.
    """
    return scope.domain_where(alias, "domain_ids", state.get("domain_ids"))


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

    NOT NARROWED BY THE CHOSEN AREAS, deliberately (2026-09-06). It is tempting:
    the numerator is a row drawn from the area-filtered candidates, so matching
    the denominator to them looks like the consistent thing to do. It measured
    worse, and badly. A weight here is "how discriminative is this word in the
    material route selected", and that feeds the A/B/C threshold — so
    re-measuring it inside a narrow area moves a calibrated bar with the user's
    choice of area. In the sibling case on the document side a three-chunk area
    made every query token exceed the genericity share, the gate marked them all
    generic and a correct filter turned into a no_answer. Eligibility is the
    ranked query's job and that IS filtered, so nothing out of area can be an
    answer; only the weights among admitted rows are computed pack-wide.
    See the long note in document_lexical for the measurement.

    Not scoped by entity/role either — PRE-EXISTING, not introduced here: this
    counts over the packs route already selected for the caller, and adding
    scope.where() would change the numbers for every existing request. Recorded
    as a known asymmetry rather than fixed under an unrelated change.
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


def category_siblings(cur, entry_id, frag, pack_params, scope_cond, scope_params,
                      form=None, concept_sets=None, weights=None) -> list[tuple]:
    """Other curated questions a CURATOR filed under the same category.

    The last resort when the ranking offers nothing: for "Co když dítě přijde až
    po 8.00" the pack holds eleven more questions about Docházka, and the lexical
    ranking found none of them above the bar (production 2026-09-04, Marek). This
    does not ask what the query resembles — it asks what a person decided belongs
    together, which is the one signal in the corpus that scoring cannot fake.

    Ranked by overlap with the query, because the category says which entries
    belong together and never which of fifteen to show. `weights` (IDF, when the
    caller has them) makes a rare shared concept count for more than "škola".
    Ordering them by entry_key looked stable and was arbitrary: on "Co když dítě
    přijde až po 8.00" it surfaced the three alphabetically first entries of
    Provoz (teacher absence, epidemics, breaks) and buried "V kolik se otevírá
    škola?" and "V kolik začíná vyučování?" under `v` (2026-09-04).

    WITHIN ONE PACK. A category name means whatever its own pack's author meant
    by it, and the names collide across packs: faq-provoz files school report
    cards under "Hodnocení" and retake exams under "Zkoušky", so a JPZ question
    about points would draw siblings about vysvědčení. Worse with the per-school
    packs, where 31 of 33 questions are the same text in three packs — matching
    across them offers the same question back under another entity's answer.
    Pack-local is also what the data IS: `cat` is a column in one pack's CSV.

    Empty when the entry has no category — a pack whose authors left it blank
    gets no siblings, and inventing a grouping belongs in the pack, not here.
    """
    cur.execute(
        f"SELECT nullif(trim(tags->>'category'), ''), pack_id, school_grade_ids "
        f"FROM {config.QA_ENTRIES_TABLE} WHERE entry_id = %s", (entry_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return []
    # A sibling is offered as this school's own material, so it passes the same
    # pack/scope/validity predicate as an answer would — and, since 2026-09-06,
    # the same AREA predicate: `scope_cond` arrives from _scope_frags with
    # domain_where already appended, so the category top-up cannot reach outside
    # the chosen areas. A curator's category is a grouping WITHIN a pack and says
    # nothing about areas: in a mixed pack, "Provoz" legitimately holds both a
    # catering and an attendance question, so without this a "Stravování" search
    # would have offered the attendance one as a related chip.
    #
    # Grade is tied to the ANSWERED ENTRY, not to the caller. Matching the
    # caller's ročník ids was tried and was wrong twice over: route resolves them
    # for pack selection and the sets do not line up entry-side, so a caller with
    # a ročník got NO siblings at all — and it was redundant anyway, because every
    # active pack carries exactly one ročník set across its variants (checked
    # 2026-09-04), and route already picked the pack by the caller's year. Keyed
    # to the answer, it stays right if a mixed pack ever appears: what is offered
    # is for the same years as the answer just given.
    grade_cond, grade_param = "", []
    if row[2]:
        grade_cond = (" AND (e.school_grade_ids IS NULL"
                      " OR cardinality(e.school_grade_ids) = 0"
                      " OR e.school_grade_ids && %s::bigint[])")
        grade_param = [list(row[2])]
    # No LIMIT: the caller ranks the whole category, and a category is small
    # (the largest in production is 20). Cutting to top_n here would cut by the
    # arbitrary order this function no longer claims to have.
    text = form_text_concat(form or _DEFAULT_FORM, "v")
    cur.execute(
        f"""
        SELECT e.entry_id, e.canonical_q, string_agg({text}, ' ')
          FROM {config.QA_ENTRIES_TABLE} e
          JOIN {config.QA_VARIANTS_TABLE} v ON v.entry_id = e.entry_id
         WHERE v.is_active AND {frag}{scope_cond}{grade_cond}
           AND e.entry_id <> %s
           AND e.pack_id = %s
           AND nullif(trim(e.tags->>'category'), '') = %s
         GROUP BY e.entry_id, e.canonical_q, e.entry_key
         ORDER BY e.entry_key
        """,
        [*pack_params, *scope_params, *grade_param, entry_id, row[1], row[0]])
    cands = [(str(r[0]), r[1], {t for t in (r[2] or "").split() if t})
             for r in cur.fetchall()]
    if not concept_sets:
        return [(eid, q, None) for eid, q, _toks in cands]
    w = weights or [1.0] * len(concept_sets)
    scored = sorted(((weighted_coverage(concept_sets, toks, w), eid, q)
                     for eid, q, toks in cands), key=lambda t: -t[0])
    return [(eid, q, cov) for cov, eid, q in scored]


def _scope_frags(state):
    """(frag, pack_params, scope_cond, scope_params) as every QA query builds them.

    scope_cond now carries the AREA predicate too, appended to the identity one.
    Both are AND-fragments on the same scanned `v` row, and every caller
    interpolates the pair together — so adding it here reaches the paths that
    build their SQL through this helper (category_siblings via finalize_chips,
    i.e. the related chips on all three answering paths) without each of them
    having to remember.
    """
    frag, pack_params = pack_filter(state.get("pack_ids", []), alias="e",
                                    language=state.get("language_filter"))
    scope_cond, scope_params = scope.where(
        "v", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)
    dom_cond, dom_params = domain_filter(state, "v")
    return (frag, pack_params, scope_cond + dom_cond,
            [*scope_params, *dom_params])


def packs_of(cur, entry_ids) -> dict:
    """{entry_id: pack_id} — which body of material each chip came from."""
    ids = [e for e in (entry_ids or []) if e]
    if not ids:
        return {}
    cur.execute(f"SELECT entry_id::text, pack_id FROM {config.QA_ENTRIES_TABLE} "
                f"WHERE entry_id::text = ANY(%s)", (ids,))
    return {r[0]: r[1] for r in cur.fetchall()}


def finalize_chips(cur, state, params, entry_id, parked, top_n, form=None) -> list[dict]:
    """The chip row for an answer, from whatever chips were parked for it.

    One place for the three rules a chip row obeys, because they were landing on
    one path at a time and the paths drifted: qa_exact had all three while
    qa_vector — which answers the vaguest questions, the ones most in need of a
    good chip — had none (2026-09-04, "Co když přijde dítě pozdě?" came back with
    two chips, one of them from the JPZ pack, in the wrong order).

      1. a chip from ANOTHER pack needs a higher score (related_cross_pack_min);
         within the answer's own pack the ranking's bar already applied
      2. a short row is topped up from the answered entry's own category
      3. the row is ordered by the number it shows

    `parked` is whatever the caller gathered — the ranking's runners-up, or the
    deferred list a later step inherited.
    """
    items = list(parked or [])
    xmin = float(params.get("related_cross_pack_min", 0.7) or 0.0)
    by_pack = packs_of(cur, [i.get("entry_id") for i in items] + [entry_id])
    own = by_pack.get(str(entry_id))
    if own and xmin:
        items = [i for i in items
                 if by_pack.get(str(i.get("entry_id") or "")) == own
                 or (i.get("score") or 0.0) >= xmin]
    want = top_n if params.get("related_category_topup", True) else (
        0 if items else top_n)
    if len(items) < want:
        seen = {i.get("entry_id") for i in items}
        seen_q = {(i.get("question") or "").strip().casefold() for i in items}
        extra = category_siblings(cur, entry_id, *_scope_frags(state), form,
                                  [{t} for t in content_tokens(state, form or
                                                               _DEFAULT_FORM) if t])
        items += [{"kind": "qa", "entry_id": eid, "question": q,
                   "score": round(float(cov), 4) if cov is not None else None}
                  for eid, q, cov in extra
                  if eid not in seen
                  and (q or "").strip().casefold() not in seen_q][:want - len(items)]
    if items and all(i.get("score") is not None for i in items):
        items.sort(key=lambda i: -float(i["score"]))
    return items[:top_n]


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
