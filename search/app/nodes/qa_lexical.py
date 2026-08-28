# last change in code by Martin Safranek (2026-08-20).
"""Node — qa_lexical: BM25-style QA lexical match with synonym expansion + A/B/C.

Deterministic (RAG redesign §2.3/§2.4). Builds an OR tsquery from the query's
{norm, lemma} tokens expanded via qa_synonym, ranks active variants with
ts_rank_cd, aggregates to entries, then decides:

    A (answer)   coverage ≥ t_answer AND margin ≥ m_margin → curated answer, END
    B (suggest)  coverage ≥ t_suggest (not A)             → "myslel jsi?" list, END
    C (fall)     coverage < t_suggest                     → continue cascade

coverage = fraction of query concepts present (directly or via synonym) in the
top entry; margin = relative gap between the top two entries' rank scores.
If the tsquery matches nothing, a pg_trgm similarity fallback catches typos and
can only ever produce suggestions (B), never a confident answer.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from ..messages import system_message
from ..settings import coverage_stopwords
from .qa_common import (
    pack_filter, load_synonym_groups, expand_query, coverage,
    concept_idf_weights, weighted_coverage,
    fetch_answer, sources_json,
    load_thresholds_by_pack, default_thresholds, resolve_form, form_ts, form_query_tokens,
    content_tokens, form_text_concat,
)

logger = logging.getLogger(__name__)

_TRGM_SUGGEST_MIN = 0.40   # default min trigram similarity (overridable via params)


def _tokens(text: str) -> set[str]:
    return {t for t in (text or "").split() if t}


def _qa_items(entries, top_n: int) -> list[dict]:
    """Suggestion items from ranked (entry_id, canonical_q, score) triples.

    Every suggestion the API returns is a text the client can send straight back
    to /search — that is the whole contract. `kind` says where it came from
    ("qa" = a curated entry), `entry_id`/`score` are extra context a client may
    ignore.
    """
    seen, items = set(), []
    for entry_id, canonical_q, score in entries:
        if entry_id in seen:
            continue
        seen.add(entry_id)
        items.append({"kind": "qa", "entry_id": entry_id, "question": canonical_q,
                      "score": round(float(score), 4) if score is not None else None})
        if len(items) >= top_n:
            break
    return items


def _suggest(entries, top_n: int, lang: str | None = None, min_score: float = 0.0) -> dict:
    """Build a B (suggest) terminal result from ranked (entry_id, canonical_q, score).

    `min_score` is the same t_suggest that decided this branch: an entry BELOW it
    was not good enough to reach suggestions in the first place, yet the list was
    built from the ranked top-N regardless. Measured on produktový vlastník's "můžeme ve škole
    fotit?" — two of the buttons scored 0.24 and 0.20 against a 0.30 bar ("Kde
    můžeme být o přestávkách?", "Kdy je škola v přírodě?").
    """
    if min_score:
        entries = [e for e in entries
                   if e[2] is None or float(e[2]) >= min_score]
    return {
        "resolved": True, "outcome": "suggest", "answer_source": "qa_lexical",
        "suggestions": _qa_items(entries, top_n),
        "answer": system_message("qa_suggest", lang),
        "sources_json": "[]", "user_language": lang,
    }


def _other_grade_suggestions(state: SearchState, params: dict | None = None) -> list[dict]:
    """Entries from the caller's OWN packs that the ročník filter excluded.

    They are never allowed to answer — a curated answer is returned verbatim, so a
    ročník-9 rule reaching a ročník-6 pupil is a wrong statement nothing downstream
    can qualify. Offering them is the alternative to dropping them silently, which
    is what happens today: a mistagged pack takes its whole content out of reach
    with nothing in the trace to say it existed.

    Deliberately its OWN query rather than a widening of the main one. Coverage is
    IDF-weighted over the packs in scope, so adding packs to the main query would
    move every score and could flip an A/B/C decision — the opposite of a safety
    net. This runs only when route found such a pack, which is never in the normal
    case, and it costs one indexed query when it does.

    No synonyms and no coverage: the bar for OFFERING a question is "the words
    match", not "the answer is right". Getting that wrong shows a button the user
    can ignore, so simple is correct here.
    """
    offer = state.get("offer_pack_ids") or []
    if not offer:
        return []
    params = params or {}
    top_n = int(params.get("other_grade_top_n") or 2)
    # Relative floor, not absolute: the tsquery here is a plain OR over the query
    # tokens, so an entry sharing one common word ranks non-zero. Measured on
    # "telefony ve škole?" — the second offer was "Do kdy musím nahlásit
    # nepřítomnost?", matched on "škole" alone. A button that claims "here is your
    # school's rule" and opens something else is worse than no button.
    min_ratio = float(params.get("other_grade_min_ratio") or 0.5)
    form = resolve_form(params)
    toks = form_query_tokens(state, form)
    if not toks:
        return []
    tsexpr = form_ts(form, "v")
    # A DISCRIMINATIVE WORD MUST BE THE REASON. The tsquery here is a plain OR, so
    # an entry sharing one common word ranks non-zero — and the relative floor
    # below cannot help when the BEST match is junk too. Measured 2026-08-26 on
    # produktový vlastník's "můžeme ve škole fotit?" (ročník 8): every candidate matched on
    # `skola` alone, so the two buttons offered were "Kolik škol si můžu dát do
    # přihlášky?" and "Jak funguje pořadí škol v přihlášce?" — a photography
    # question answered with admission buttons. Tokens the corpus considers
    # generic are therefore dropped from the OFFER query (they stay everywhere
    # else); if nothing discriminative is left, no button is shown, which is the
    # correct outcome: no related question exists.
    _max_df = float(params.get("other_grade_max_df_share") or 0.10)
    # CONTENT tokens only. The df filter below removes what the corpus considers
    # generic, but a rare SURFACE FORM of a function word slips through: measured
    # on "můžeme ve škole fotit?" the token `muzeme` has df 5/148 in the admission
    # packs (3 %) and pulled in "Můžu u přijímaček používat kalkulačku?". The
    # coverage stoplist already names such words — the same list that keeps them
    # out of confidence must keep them out of the offer.
    # LEMMA tokens, then the stoplist. The stoplist names lemmas ("moci"), so
    # filtering the surface forms left `muzeme` (df 5/148 = 3 % — the df filter
    # reads that as discriminative) and it pulled in "Můžu u přijímaček používat
    # kalkulačku?" for a question about photography. Coverage concepts are built
    # from the reduced form for exactly this reason; the offer now uses the same.
    _stop = coverage_stopwords().get(state.get("query_language") or "") or set()
    _content = content_tokens(state, form) or list(toks)
    _toks = sorted({t for t in _content if t and t not in _stop})
    if not _toks:
        return []
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT count(DISTINCT e.entry_id) FROM {config.QA_ENTRIES_TABLE} e"
                f" WHERE e.is_active AND e.pack_id = ANY(%s)", [list(offer)])
            _ntot = int((cur.fetchone() or [0])[0] or 0)
            if _ntot:
                _kept = []
                for _t in _toks:
                    cur.execute(
                        f"SELECT count(DISTINCT v.entry_id)"
                        f"  FROM {config.QA_VARIANTS_TABLE} v"
                        f"  JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id"
                        f" WHERE v.is_active AND e.is_active AND e.pack_id = ANY(%s)"
                        f"   AND {tsexpr} @@ to_tsquery('simple', %s)", [list(offer), _t])
                    _df = int((cur.fetchone() or [0])[0] or 0)
                    if _df / _ntot <= _max_df:
                        _kept.append(_t)
                if not _kept:
                    logger.info("qa_lexical: other-ročník offer skipped — every "
                                "query token is generic in the offered packs")
                    return []
                _toks = _kept
    except Exception as e:
        logger.warning("qa_lexical: other-grade df filter failed (%s) — "
                       "offering on all tokens", e)
    tsq = " | ".join(_toks)
    try:
        with get_cursor() as cur:
            scope_cond, scope_params = scope.where(
                "v", state.get("entity_scope") or [],
                state.get("organization_entities") or [], state.get("user_id"),
                state.get("user_role"), validity=True)
            cur.execute(
                f"SELECT DISTINCT ON (v.entry_id) v.entry_id::text, e.canonical_q,"
                f"       ts_rank_cd({tsexpr}, to_tsquery('simple', %s)) AS rank,"
                f"       (SELECT string_agg(c.code, ', ' ORDER BY c.code)"
                f"          FROM {config.PG_SCHEMA}.code_list c"
                f"         WHERE c.id = ANY(e.school_grade_ids)) AS grade_codes"
                f"  FROM {config.QA_VARIANTS_TABLE} v"
                f"  JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id"
                f" WHERE v.is_active AND e.is_active AND e.pack_id = ANY(%s)"
                f"   {scope_cond} AND {tsexpr} @@ to_tsquery('simple', %s)"
                f" ORDER BY v.entry_id, rank DESC",
                [tsq, list(offer), *scope_params, tsq])
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("qa_lexical: other-grade offer failed (%s) — skipped", e)
        return []
    rows.sort(key=lambda r: -float(r[2] or 0.0))
    # dedupe on the QUESTION TEXT, not the entry id: the same question legitimately
    # exists in several packs of one school (measured: "Můžu mít ve škole mobilní
    # telefon?" sits in both dochazka_zs_9 and test_provenience), and a suggestion
    # IS its text — two buttons that send the identical query are one button.
    _best = float(rows[0][2] or 0.0) if rows else 0.0
    # ABSOLUTE FLOOR, not only a relative one. The relative floor compares the
    # candidates to each other, so when the whole field is junk it keeps the junk:
    # measured 2026-08-26 on "můžeme ve škole fotit?" every offer scored 0.10,
    # which is what a ONE-token OR match yields — and the tokens that matched were
    # a modal ("můžeme") and "škola". A real relation scores far above it (the
    # mobile-phone entry scored 0.45 on the same query), so one number separates
    # them, whatever surface form the token happened to take.
    min_rank = float(params.get("other_grade_min_rank") or 0.15)
    out, seen = [], set()
    for entry_id, canonical_q, rank, grade_codes in rows:
        if float(rank or 0.0) < min_rank:
            break                       # rows are sorted, so the rest are weaker
        if _best > 0 and float(rank or 0.0) < min_ratio * _best:
            break
        key = (canonical_q or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"kind": "other_grade", "entry_id": entry_id,
                    "question": canonical_q,
                    "for_grades": grade_codes or None,
                    "score": round(float(rank or 0.0), 4)})
        if len(out) >= top_n:
            break
    if out:
        logger.info("qa_lexical: %d other-ročník suggestion(s) from packs %s",
                    len(out), offer)
    return out


def qa_lexical(state: SearchState, params: dict | None = None) -> dict:
    """Wrapper: the lexical QA decision, plus the other-ročník safety net.

    A wrapper and not an edit inside, because the decision has six return paths
    (A, B-deferred x2, C x2, and the error fall-through) and the net has to attach
    to every one of them — including the early returns that never reach the
    ranking.
    """
    out = _qa_lexical_decision(state, params) or {}
    offered = _other_grade_suggestions(state, params)
    if not offered:
        return out
    out["other_grade_suggestions"] = offered
    # when THIS step answers, the buttons belong next to that answer; otherwise
    # they travel in state and answer.py / resolve_unanswered pick them up
    if out.get("resolved"):
        out["suggestions"] = list(out.get("suggestions") or []) + offered
    return out


def _qa_lexical_decision(state: SearchState, params: dict | None = None) -> dict:
    params = params or {}
    fetch_k = int(params.get("fetch_k", config.QA_LEXICAL_FETCH_K))
    suggest_top_n = int(params.get("suggest_top_n", config.QA_SUGGEST_TOP_N))
    trgm_min = float(params.get("trgm_suggest_min", _TRGM_SUGGEST_MIN))
    use_synonyms = params.get("synonyms", True)
    form = resolve_form(params)

    norm = state.get("query_norm_tokens", []) or []   # surface — used by trgm fallback
    query_tokens = form_query_tokens(state, form)     # tsquery recall over the chosen form(s)
    content = content_tokens(state, form)             # coverage 'concepts' (reduced form)
    if not query_tokens:
        return {"resolved": False, "decision": "C"}

    pack_ids = state.get("pack_ids", [])
    lang = state.get("query_language")
    frag, pack_params = pack_filter(pack_ids, alias="e", language=state.get("language_filter"))
    # same three levels as documents, read off the variant row
    scope_cond, scope_params = scope.where(
        "v", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)   # migration 140
    tsexpr = form_ts(form, "v")

    try:
        with get_cursor() as cur:
            groups, kind_of, _phrase_g, _terms = (
                load_synonym_groups(cur, lang) if use_synonyms
                else ({}, {}, set(), {}))
            all_terms, concept_sets = expand_query(
                query_tokens, content, groups, kind_of,
                coverage_stop=(coverage_stopwords().get(lang or "") or None),
                phrase_groups=_phrase_g, terms=_terms)
            tsq = " | ".join(sorted(all_terms))
            # thresholds: manifest is the baseline, the active setting's params
            # (if present) win — so a version can retune A/B/C without a re-pack.
            # per-pack thresholds; the concrete values are picked AFTER ranking,
            # from the pack of the entry that actually matched (see below).
            pack_thresholds = load_thresholds_by_pack(cur, pack_ids, lang)
            fallback_thresholds = default_thresholds()

            # v.is_active: required, not belt-and-braces. The ts_*/trgm indexes are
            # PARTIAL WHERE is_active (migration 149) and Postgres uses a partial
            # index only when the query implies its predicate — `e.is_active` from
            # pack_filter sits on the JOINED table and does not. Both the ts query
            # and the trgm fallback below carry it.
            entry_text = form_text_concat(form, "v")
            sql = f"""
                SELECT v.entry_id, e.pack_id, e.canonical_q, {entry_text} AS entry_text,
                       ts_rank_cd({tsexpr}, to_tsquery('simple', %s)) AS rank,
                       v.legal_entity_code, v.school_grade_ids
                FROM {config.QA_VARIANTS_TABLE} v
                JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id
                WHERE v.is_active AND {frag}{scope_cond} AND {tsexpr} @@ to_tsquery('simple', %s)
                ORDER BY rank DESC
                LIMIT %s
            """
            cur.execute(sql, [tsq, *pack_params, *scope_params, tsq, fetch_k])
            rows = cur.fetchall()

            if not rows:
                # ── typo fallback: pg_trgm similarity → suggestions only ──
                joined_norm = " ".join(norm)
                trgm_sql = f"""
                    SELECT v.entry_id, e.pack_id, e.canonical_q,
                           1 - (v.norm_text <-> %s) AS sim
                    FROM {config.QA_VARIANTS_TABLE} v
                    JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id
                    WHERE v.is_active AND {frag}{scope_cond}
                    ORDER BY v.norm_text <-> %s
                    LIMIT %s
                """
                cur.execute(trgm_sql, [joined_norm, *pack_params, *scope_params,
                                       joined_norm, suggest_top_n])
                trows = cur.fetchall()
                best_sim = float(trows[0][3]) if trows else 0.0
                logger.info("qa_lexical: no ts match; trgm best sim=%.3f", best_sim)
                _triples = [(str(r[0]), r[2], float(r[3])) for r in trows]
                if trows and best_sim >= trgm_min:
                    # typo fallback: deferred like decision B — RAG gets a chance
                    return {"resolved": False, "decision": "B-deferred", "match_score": 0.0,
                            "deferred_suggest": _suggest(_triples, suggest_top_n, lang)}
                # below the bar these are not worth an outcome of their own, but a
                # no_answer that ends the conversation with an empty hand is worse
                # than one that offers the nearest curated questions — parked for
                # resolve_unanswered, used only when the whole cascade stays empty.
                return {"resolved": False, "decision": "C", "match_score": 0.0,
                        "fallback_suggestions": _qa_items(_triples, suggest_top_n)}

            # ── aggregate candidate variants to entries ──
            agg: dict[str, dict] = {}
            for entry_id, pack_id, canonical_q, entry_text, rank, _lec, _sg in rows:
                eid = str(entry_id)
                e = agg.setdefault(eid, {"canonical_q": canonical_q,
                                         "pack_id": pack_id, "entity": _lec,
                                         "grades": set(_sg or ()),
                                         "rank": 0.0, "tokens": set()})
                e["rank"] = max(e["rank"], float(rank))
                e["tokens"] |= _tokens(entry_text)

            # Rank by ts_rank_cd (relevance), then break near-ties by content
            # coverage: entries with the same BM25 score but more of the query's
            # content concepts win. This keeps ts_rank as the primary relevance
            # signal (coverage alone saturates — too many entries share the topic
            # word) while letting the discriminative content overlap settle ties.
            # IDF-weighted: a concept's weight = how discriminative it is in
            # the scoped packs (see concept_idf_weights). "skola"/"moci" weigh
            # little in a school FAQ; the rare concept carries the decision.
            idf_w = concept_idf_weights(cur, concept_sets, frag, pack_params)
            for e in agg.values():
                e["cov"] = weighted_coverage(concept_sets, e["tokens"], idf_w)
            # TIER before relevance (Martin, 2026-08-18) — the same two axes the
            # document side now uses, because QA had no notion of them at all: an
            # organisation-wide pack outranked the school's own entry purely on
            # ts_rank, which is the opposite of what a school wants. Ascending, so
            # it is negated against the descending relevance below.
            #   specificity 0 = the caller's exact ročník | 1 = a covering 9999
            #               2 = no ročník
            #   proximity   0 = the caller's own school | 1 = the organisation
            #               2 = anyone else
            _own = state.get("legal_entity")
            _org = set(state.get("organization_entities") or [])
            _ex = set(state.get("grade_exact_ids") or ())
            _un = set(state.get("grade_universal_ids") or ())

            def _tier(v):
                if not _ex and not _un:
                    gs = 0                      # no ročník known → no-op
                elif _ex & v["grades"]:
                    gs = 0
                elif _un & v["grades"] or not v["grades"]:
                    gs = 1
                else:
                    gs = 2
                ent = v.get("entity")
                ep = 0 if ent and ent == _own else (1 if ent in _org else 2)
                return (gs, ep)

            for _v in agg.values():
                _v["tier"] = _tier(_v)
            ranked = sorted(agg.items(),
                            key=lambda kv: (kv[1]["tier"],
                                            -round(kv[1]["rank"], 6), -kv[1]["cov"]))
            top_id, top = ranked[0]
            top_cov = top["cov"]
            r1 = top["rank"]
            r2 = ranked[1][1]["rank"] if len(ranked) > 1 else 0.0
            margin = (r1 - r2) / r1 if r1 > 0 else 1.0

            # thresholds of the pack the top entry belongs to; settings step
            # params still override them for experiments.
            t_answer, t_suggest, m_margin = pack_thresholds.get(
                top.get("pack_id"), fallback_thresholds)
            t_answer = float(params.get("t_answer", t_answer))
            t_suggest = float(params.get("t_suggest", t_suggest))
            m_margin = float(params.get("m_margin", m_margin))

            logger.info("qa_lexical: top=%s pack=%s cov=%.2f margin=%.2f (tA=%.2f tS=%.2f mM=%.2f)",
                        top_id, top.get("pack_id"), top_cov, margin,
                        t_answer, t_suggest, m_margin)
            # The score alone does not say whether it was close: the trace
            # carries the bars it was measured against, so "0.5137" reads as
            # "needed 0.6 to answer, 0.3 to suggest" rather than as a bare number.
            # WHY the coverage came out as it did — the one thing the trace could
            # not answer before. Weights are IDF over the packs in scope, so both
            # df and N move with the corpus; without them a logged match_score is
            # unreconstructable once a pack is added or replaced (measured: the
            # same query scored 0.584 at 17:11 with 2 packs in scope and 0.623
            # once there were 4). `hit` is per TOP entry, the one the thresholds
            # were applied to.
            _concepts = []
            try:
                _dfs = getattr(concept_idf_weights, "last_df", []) or []
                for _i, _cs in enumerate(concept_sets):
                    _concepts.append({
                        "c": "|".join(sorted(_cs)),
                        "df": _dfs[_i] if _i < len(_dfs) else None,
                        "w": round(idf_w[_i], 3) if _i < len(idf_w) else None,
                        "hit": bool(_cs & top["tokens"]),
                    })
            except Exception:
                _concepts = []
            _detail = {"t_answer": t_answer, "t_suggest": t_suggest,
                       "concepts": _concepts,
                       "n": getattr(concept_idf_weights, "last_total", None),
                       "m_margin": m_margin, "pack": top.get("pack_id"),
                       "tier": list(top.get("tier") or ()),
                       "candidates": len(ranked)}

            # every ranked entry as a (id, question, score) triple, best first —
            # the suggestion source for all three decisions below
            _triples = [(eid, e["canonical_q"], e["cov"]) for eid, e in ranked]

            # ── TOPICAL ADMISSION, the rule the document path already has ──
            # Coverage says HOW MUCH of the query an entry matched, never WHICH
            # PART. Measured 2026-08-26 on produktový vlastník's "co si mají děti přinést do
            # školy" (question_answer_log 2894): the covered concepts were `dite`
            # (df 10 %) and `skola` (df 33 %) — both generic — coverage came out
            # 1.00, and a curated answer about ABSENCE ended the cascade before
            # the document path ran. document_lexical refuses a chunk with no
            # heading match and no covered RARE concept; QA had no such rule, so a
            # query could reach full confidence on nothing but common words.
            # df share, not a word list: it is data, per pack, language-agnostic.
            _adm = bool(params.get("topical_admission", True))
            _adm_max_df = float(params.get("topical_max_df_share", 0.25))
            _ntot_adm = getattr(concept_idf_weights, "last_total", None) or 0
            _dfs_adm = getattr(concept_idf_weights, "last_df", []) or []
            _rare_hit = None
            if _adm and _ntot_adm:
                _rare_hit = False
                for _i, _cs in enumerate(concept_sets):
                    if not (_cs & top["tokens"]):
                        continue
                    _df = _dfs_adm[_i] if _i < len(_dfs_adm) else None
                    if _df is None:
                        continue
                    if (_df / _ntot_adm) <= _adm_max_df:
                        _rare_hit = True
                        break
                _detail["topical_admission"] = {"rare_hit": _rare_hit,
                                                "max_df_share": _adm_max_df,
                                                "n": _ntot_adm}
                if not _rare_hit:
                    logger.info("qa_lexical: topical admission — every covered "
                                "concept is generic (df share > %.2f of %d "
                                "entries); no curated answer, cascade continues",
                                _adm_max_df, _ntot_adm)

            # ── A ──
            if (top_cov >= t_answer and margin >= m_margin
                    and _rare_hit is not False):
                _q, answer_md, _tags, _refs = fetch_answer(cur, top_id)
                return {
                    "trace_detail": _detail,
                    "resolved": True, "outcome": "answer", "answer_source": "qa_lexical",
                    "decision": "A", "matched_entry_id": top_id,
                    "match_score": round(top_cov, 4), "match_margin": round(margin, 4),
                    "answer": answer_md or "", "sources_json": sources_json(_refs),
                    "user_language": lang,
                    # the runners-up that would themselves have been suggested,
                    # offered NEXT TO the answer ("related questions") — only those
                    # above t_suggest, so junk never rides along with a good answer
                    "suggestions": _qa_items(
                        [t for t in _triples if t[0] != top_id and t[2] >= t_suggest],
                        suggest_top_n),
                }
            # ── B — DEFERRED, not terminal (2026-07-31) ──
            # A weak QA hit used to close the cascade while the documents held
            # the real answer ("jak se mám omluvit ze školy?" matched an
            # unrelated FAQ entry while the school code's 11 chunks on
            # omlouvání were never consulted). The suggest is parked in state;
            # resolve_unanswered() emits it at the end IF the document
            # retrievers found nothing. Cost measured: +~330 ms when RAG stays
            # empty (no LLM call), ~1.7 s when RAG answers — which is the point.
            if top_cov >= t_suggest:
                out = _suggest(_triples, suggest_top_n, lang, min_score=t_suggest)
                out.update({"decision": "B", "match_score": round(top_cov, 4),
                            "match_margin": round(margin, 4), "matched_entry_id": top_id})
                logger.info("qa_lexical: suggest DEFERRED (cov=%.3f) — cascade continues", top_cov)
                return {"trace_detail": _detail,
                        "resolved": False, "decision": "B-deferred",
                        "match_score": round(top_cov, 4), "match_margin": round(margin, 4),
                        "deferred_suggest": out}
            # ── C ── (nearest entries parked for a possible no_answer — see the
            # trgm branch above for why)
            return {"trace_detail": _detail,
                    "resolved": False, "decision": "C",
                    "match_score": round(top_cov, 4), "match_margin": round(margin, 4),
                    "fallback_suggestions": _qa_items(_triples, suggest_top_n)}

    except Exception as e:
        logger.warning("qa_lexical: failed (%s) — falling through", e)
        return {"resolved": False, "decision": "C"}
