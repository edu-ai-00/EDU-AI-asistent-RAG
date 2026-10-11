# last change in code by Jan Drozd (2026-09-18).
"""Node — route: deterministic guard + pack scope (no LLM).

guard
  1. usable query?  empty / punctuation-only tokens → short-circuit "unusable".
  2. meta-question  the WHOLE query equals a qa_blocklist category='meta' phrase
                    ("Co umíš?") → answer with capabilities, not a block.
  2b. identity      same equality against category='identity' ("Jak se jmenuje
                    naše škola?") → answer from legal_entity.display_name. The
                    name is metadata and is in no index, so retrieval can never
                    answer it (measured on production, 2026-09-11).
  3. blocklist      any query token matches qa_blocklist (whole-token) OR every
                    token of a blocklist phrase occurs inside one window of the
                    query → short-circuit. self_harm gets a calm, supportive
                    response + Linka bezpečí 116 111; all other categories get a
                    polite "ask nicely" refusal. Matching is whole-token only
                    (never substring — avoids the Scunthorpe problem).

  Phrases are matched against the query's UNSTOPWORDED surface+lemma tokens, in
  any order within a window. The stored term_norm ("zabit se") kept its stopword
  while the cascade's query tokens have theirs dropped ("chtel", "zabit"), so a
  substring test over those could never fire: "chtěl bych se zabít" and "přemýšlím
  že si ublížím" both reached the RAG and were answered as school questions
  (measured 2026-08-04). Czech inflects and reorders — "zabít se" / "se zabít" —
  so order-free over a window is the matching this list needs, and the reflexive
  is exactly what must survive stopwording to keep "zabít se" off "zabít čas".

scope
  Select QA pack(s) by user_school_grade. Today there is a single pack
  (cs-prijimacky-core, grade NULL = applies to all), so this resolves to "all
  active"; the hook is real for future per-grade / per-subject packs.

Intent routing (QA vs. document-discovery) is intentionally NOT done here — it
would need an explicit request field or an LLM. Deferred (TODO): every usable,
clean query proceeds to the QA→RAG cascade.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from ..messages import system_message
from ..text_norm import fold, tokenize, lemma_tokens

logger = logging.getLogger(__name__)

# How far apart the tokens of one phrase may sit. "chtěl bych se zabít" puts two
# words between "se" and "zabít"; past that the words are no longer one phrase.
_PHRASE_SLACK = 3


# Negation prefixes, per language. Belongs in the číselník like context_leading
# does, and is kept in code on purpose: _load_blocklist already SKIPS the guard
# when its DB read fails, so making negation handling depend on a second read
# would mean a DB hiccup silently inverts what the guard matches.
_NEGATION_PREFIX = {"cs": ("ne",), "de": (), "en": (), "es": ()}


def _negation_lost(surface: str, lemma: str, lang: str) -> bool:
    """True when the lemma dropped a negation the surface form carries.

    simplemma 2.0.0 lemmatizes "nechci" to "chtit" — the opposite meaning. On a
    guard that is not a ranking nuisance but a wrong verdict: measured
    2026-09-03, the phrase rule "nechci žít" matched "chci žít" and "chci žít
    zdravě" through exactly this bridge, so a pupil asking how to live healthily
    got the suicide-crisis reply. simplemma 1.2.0 gave "nechtít" and the bridge
    did not exist, so this is version-dependent and cannot be left to the
    library. Guards against the reverse too: "nechci umřít" no longer trips the
    rule for "chci umřít".
    """
    for p in _NEGATION_PREFIX.get(lang or "", ()):
        if surface.startswith(p) and not lemma.startswith(p):
            return True
    return False


def token_forms(text: str, lang: str, cfg: dict | None) -> list[set[str]]:
    """One set of accepted surface forms per token, stopwords KEPT.

    `cfg` carries the stoplist and the engines (settings + qa_stopword). It is
    REQUIRED in every running path: eduai_normalize 3.0.0 has no built-in list,
    and until 2026-09-05 this function passed none — so the lemma branch ran on
    the library's own Czech/English sets (which happened to equal the číselník)
    and on NOTHING for German and Spanish, whose blocklist phrases therefore
    carried un-dropped function words that the query side had already removed.

    Both sides of a phrase match go through this, so query and blocklist are
    reduced identically. Each token carries its own lemma because simplemma is
    inconsistent across a verb's forms ("ublížit"→ublizovat, "ublížím"→ublizit) —
    comparing form sets rather than single strings absorbs that.

    A lemma that lost a negation is left OUT — see _negation_lost. Dropping it on
    BOTH sides keeps a negated rule matching its own wording (via the surface
    form) while it stops meaning its own opposite.
    """
    out: list[set[str]] = []
    for word in tokenize(text or ""):
        surface = fold(word)
        if not surface:
            continue
        # lemmatize the RAW word: simplemma needs the diacritics to recognise it
        # ("ublížím"→ublizit, but the folded "ublizim" is not a word it knows).
        # It also drops stopwords, so a stopword yields [] → surface only.
        lemmas = {l for l in lemma_tokens(word, cfg, lang)
                  if not _negation_lost(surface, l, lang)}
        out.append({surface} | lemmas)
    return out


def _phrase_hit(term: list[set[str]], query: list[set[str]],
                slack: int = _PHRASE_SLACK) -> bool:
    """Does every token of `term` occur in one window of `query`, any order?"""
    if not term or len(term) > len(query):
        return False
    width = len(term) + slack
    for start in range(len(query) - len(term) + 1):
        window = query[start:start + width]
        taken: set[int] = set()
        for forms in term:
            hit = next((i for i, qf in enumerate(window)
                        if i not in taken and forms & qf), None)
            if hit is None:
                break
            taken.add(hit)
        else:
            return True
    return False


def _load_blocklist(lang: str, cfg: dict | None):
    """Return (single_tokens: dict[str,(cat,sev)], phrases: list[(forms,cat,sev)]).

    `cfg` is threaded through to token_forms so a blocklist phrase is reduced
    with the same stoplist and engines as the query it will be compared with."""
    singles: dict[str, tuple[str, int]] = {}
    phrases: list[tuple[list[set[str]], str, int]] = []
    with get_cursor() as cur:
        cur.execute(
            f"SELECT term, term_norm, term_lemma, category, severity, is_phrase "
            f"FROM {config.QA_BLOCKLIST_TABLE} "
            # 'meta' and 'identity' are DETECTION vocabulary for the two answer
            # paths above, not terms to block on.
            "WHERE is_active AND category NOT IN ('meta', 'identity')"
        )
        for term, term_norm, term_lemma, category, severity, is_phrase in cur.fetchall():
            if is_phrase:
                # tokenize the ORIGINAL term: term_norm has lost its diacritics,
                # which is what the lemmatizer needs to recognise the word
                phrases.append((token_forms(term or term_norm, lang, cfg),
                                category, int(severity)))
            else:
                # index by both norm and lemma surface so either query form hits
                for key in filter(None, (term_norm, term_lemma)):
                    singles.setdefault(key, (category, int(severity)))
    return singles, phrases


# ── meta-questions ("Co umíš?") — deterministic, no LLM (blok 2) ────────────
# Detection is DATA: qa_blocklist rows with category='meta' hold the folded
# phrases ("co umis", "kdo jsi", "help", per language — extending is an
# INSERT). Matching is deliberately stricter than the blocklist: the WHOLE
# short query must equal a phrase — "co umíš spočítat" is a real question,
# not a meta one. The answer is the `capabilities` system message with the
# school's name and LIVE topics from its corpus, so it grows with the KB.

def _fold_q(q: str) -> str:
    from eduai_normalize import strip_diacritics
    import re as _re
    return " ".join(_re.sub(r"[^a-z0-9á-ž ]+", " ",
                            strip_diacritics((q or "").lower())).split())


_META_FILLER_FALLBACK = {"jeste", "vsechno", "vse", "tak", "prosim", "vlastne",
                         "uz", "jinak", "te", "ti", "mi", "mne", "nam"}
_META_VERB_FALLBACK = {"umis", "umite", "dokazes", "dokazete", "zvladnes", "zvladnete",
                       "pomuzes", "pomuzete", "delas", "delate", "jsi", "jste",
                       "fungujes", "fungujete", "nabizis", "nabizite"}


def _meta_fillers(lang: str | None) -> set:
    """Words that do not change what a meta-question ASKS (qa_stopword,
    scope='meta_filler').

    Detection is an exact match of the whole folded query against the phrase
    list, which is deliberate — "co umíš spočítat" is a real question, not a
    meta one. But it also means every polite or emphatic variant needs its own
    row: measured 2026-08-26, "co ještě umíš?" folded to `co jeste umis`, which
    is not in the list, so it fell through to retrieval and came back with
    "Umíš z textu získat informace" — a sentence from the school's CURRICULUM,
    which speaks in the second person ABOUT THE PUPIL. Stripping fillers first
    keeps the strict equality (spočítat is not a filler) while covering the
    variants. A číselník, not a code list, so a new language extends by INSERT.
    """
    from ..settings import term_list
    return (term_list("meta_filler").get(lang or "") or _META_FILLER_FALLBACK)


def _meta_verbs(lang: str | None) -> set:
    """Second-person ability verbs (qa_stopword, scope='meta_verb')."""
    from ..settings import term_list
    return (term_list("meta_verb").get(lang or "") or _META_VERB_FALLBACK)


def _meta_phrases() -> set:
    with get_cursor() as cur:
        cur.execute(
            f"SELECT term_norm FROM {config.QA_BLOCKLIST_TABLE} "
            "WHERE is_active AND category = 'meta'")
        return {r[0] for r in cur.fetchall()}


def _identity_phrases() -> set:
    with get_cursor() as cur:
        cur.execute(
            f"SELECT term_norm FROM {config.QA_BLOCKLIST_TABLE} "
            "WHERE is_active AND category = 'identity'")
        return {r[0] for r in cur.fetchall()}


def _corpus_topics(state: SearchState, top_n: int = 6):
    """(topics, suggestions) — the most frequent section headings of the
    organisation's non-private documents. Live: the message grows with the
    corpus and an empty corpus yields an honest 'zatím žádná témata'."""
    orgs = state.get("organization_entities") or []
    if not orgs:
        return [], []
    # SAME scope as retrieval, not a hand-written WHERE. The old filter asked only
    # for "any entity of the organisation, not private", so a ZŠ caller was offered
    # headings of the SOŠ beside it ("2. Co je udržitelná uplatnitelnost?") and then
    # got no_answer when they took the offer up — the retrieval that followed DID
    # apply the entity and role rules. One sentence, two different scopes. The role
    # column lives on document_chunks, hence the alias c.
    from .. import scope  # local, as elsewhere in route: scope pulls settings
    scope_cond, scope_params = scope.where(
        "c", state.get("entity_scope") or [], orgs, state.get("user_id"),
        state.get("user_role"), validity=True)
    # Runs BEFORE pack_ids exists (it is _meta_check's own answer, not a
    # retrieval result), so it cannot inherit the pack pre-selection and has to
    # filter both halves itself — the curated questions on the ENTRY row and the
    # headings on the CHUNK row. An offer is a question the user can click; one
    # from an area they excluded is the same automatic widening as an answer.
    _dom = state.get("domain_ids")
    dom_c, dom_cp = scope.domain_where("c", "document_domain_id", _dom)
    dom_e, dom_ep = scope.domain_where("e", "domain_ids", _dom)
    with get_cursor() as cur:
        # OFFER WHAT CAN BE ANSWERED. Headings are what the corpus is ORGANISED
        # by, not what it can answer: measured 2026-08-26 the offer read "Stupeň 4
        # (dostatečný)", "(Klasifikační řád)" and "2. Co je udržitelná
        # uplatnitelnost?" — and taking that last one up returned "na tuto otázku
        # zatím nemám odpověď". A curated QA question is the opposite: it exists
        # BECAUSE an answer exists, and qa_exact will match it verbatim. So the
        # curated questions in the caller's own scope come first; headings only
        # fill what is missing, for a school whose packs are still empty.
        _qa_scope, _qa_params = scope.where(
            "e", state.get("entity_scope") or [], orgs, state.get("user_id"),
            state.get("user_role"), validity=True)
        heads: list = []
        try:
            cur.execute(
                # DISTINCT on the TEXT: one question legitimately sits in
                # several packs of a school (a shared FAQ plus a grade pack), and
                # a topic IS its text — the first offer listed "Jak se platí
                # obědy?" twice.
                f"""SELECT e.canonical_q
                      FROM {config.QA_ENTRIES_TABLE} e
                     WHERE e.is_active AND e.canonical_q IS NOT NULL
                       {_qa_scope}{dom_e}
                     GROUP BY e.canonical_q
                     ORDER BY min(length(e.canonical_q)) LIMIT %s""",
                (*_qa_params, *dom_ep, top_n))
            heads = [r[0] for r in cur.fetchall()]
        except Exception as e:
            logger.warning("route.meta: curated topics failed (%s) — headings", e)
        if len(heads) < top_n:
            cur.execute(
                # file_uuid AND processing_uuid, like every other chunk query in
                # the service. USING (file_uuid) alone joined the ACTIVE metadata
                # row to the chunks of EVERY processing run of that file, so a
                # re-ingested document offered headings from a superseded
                # version — the section may have been deleted from the document
                # since. Same pair the retrievers use, so the offer and the
                # answer now speak about the same rows.
                f"""SELECT c.section_heading, count(*) AS n
                    FROM {config.CHUNKS_TABLE} c
                    JOIN {config.METADATA_TABLE} m
                      ON c.file_uuid = m.file_uuid
                     AND c.processing_uuid = m.processing_uuid
                    WHERE m.is_active
                      AND c.section_heading IS NOT NULL
                      AND length(c.section_heading) BETWEEN 4 AND 70
                      {scope_cond}{dom_c}
                    GROUP BY 1 ORDER BY n DESC LIMIT %s""",
                (*scope_params, *dom_cp, top_n - len(heads)))
            heads += [r[0] for r in cur.fetchall()]
    sugg = [{"kind": "related", "question": h} for h in heads[:4]]
    return heads, sugg


def _meta_check(state: SearchState, params: dict | None = None):
    """Terminal capabilities answer for a meta-question, else None."""
    params = params or {}
    q = _fold_q(state.get("user_question", ""))
    # 10, not 6. The cap is belt-and-braces — the real filter is the intent guard
    # below, which requires that after fillers, function words and interrogatives
    # are stripped, ONLY an ability verb remains and no domain keyword appears.
    # At 6 the belt was doing the work and doing it wrong: "A na co mi ještě
    # dokážeš odpovědět?" (7 words) and "A co se týká třeba školy do které chodím"
    # (8) were searched in the corpus, and a school corpus answers them — the
    # first came back with what a JPZ invigilator may explain (2026-09-04). The
    # same question at five words was recognised.
    _max_words = int(params.get("meta_max_words") or 10)
    if not q or len(q.split()) > _max_words:
        return None
    lang = state.get("query_language")
    try:
        _fill = _meta_fillers(lang)
        _stripped = " ".join(t for t in q.split() if t not in _fill)
        _phrases = _meta_phrases()
        _hit = q in _phrases or (_stripped and _stripped in _phrases)
        # A QUESTION ABOUT THE ASSISTANT IS NOT A QUESTION FOR THE CORPUS. When
        # the phrase list misses a wording, the query used to be searched like any
        # other — and a school corpus answers it, because curricula are written in
        # the second person about the pupil. So a short query whose ONLY remaining
        # words are an ability verb and function words is treated as meta even
        # without a matching phrase: there is nothing in it to look up.
        if not _hit and bool(params.get("meta_intent_guard", True)):
            _verbs = _meta_verbs(lang)
            # WHAT COUNTS AS "nothing lookupable" comes from data, per language,
            # not from a Czech list in this file: function words are the coverage
            # stoplist (qa_stopword scope='coverage') and question words are the
            # interrogative groups of qa_synonym — the same two sources the
            # lexical steps already use, so a new language needs no code change.
            from ..settings import coverage_stopwords
            _fn = set(coverage_stopwords().get(lang or "") or set())
            _interro: set = set()
            try:
                with get_cursor() as cur:
                    cur.execute(
                        f"SELECT term_norm FROM {config.QA_SYNONYM_TABLE} "
                        "WHERE is_active AND kind = 'interrogative' "
                        "AND (%s IS NULL OR lang = %s)", (lang, lang))
                    for (t,) in cur.fetchall():
                        _interro.update((t or "").split())
            except Exception as e:
                logger.warning("route.meta: interrogatives load failed (%s)", e)
            _rest = [t for t in _stripped.split()
                     if t not in _verbs and t not in _fn and t not in _interro]
            # IS THERE A SCHOOL TOPIC IN THE SENTENCE? That is the real question,
            # and the curated domain keywords answer it: "přijímačky" is one,
            # "spočítat" is not. Martin, 2026-08-26: "co (ty) umíš spočítat" is a
            # form of ADDRESS — the ability verb governs it, so the whole sentence
            # is about the assistant. "Umíš mi říct, kdy jsou přijímačky?" wraps a
            # real question in the same politeness, and there the topic word is
            # what gives it away. The earlier test ("nothing lookupable remains")
            # could not tell them apart, because `spocitat` DOES occur in the
            # corpus — in the curriculum sentence about what a pupil can do.
            _has_topic = False
            try:
                with get_cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT unnest(domain_keywords)"
                        f"  FROM {config.PG_SCHEMA}.taxonomy_rules"
                        f" WHERE is_active AND domain_keywords IS NOT NULL"
                        f"   AND (legal_entity_code IS NULL"
                        f"        OR legal_entity_code = ANY(%s))",
                        [list(state.get("entity_scope") or [])
                         + list(state.get("organization_entities") or [])])
                    # match on LEMMAS too: the keyword is curated in base form
                    # ("oběd") and the question inflects it ("s obědy?"), so
                    # comparing surface tokens alone said "no topic" and swallowed
                    # a real question about lunches.
                    _lem = [t for t in (state.get("query_lemma_tokens") or []) if t]
                    _pool = set(q.split()) | {_fold_q(t) for t in _lem}
                    _joined = " ".join([q] + [_fold_q(t) for t in _lem])
                    for (kw,) in cur.fetchall():
                        _k = _fold_q(kw or "")
                        if not _k:
                            continue
                        if (" " in _k and _k in _joined) or (" " not in _k
                                                            and _k in _pool):
                            _has_topic = True
                            break
            except Exception as e:
                logger.warning("route.meta: domain keyword check failed (%s) — "
                               "treating the query as topical", e)
                _has_topic = True
            if (any(t in _verbs for t in _stripped.split())
                    and not _has_topic):
                _hit = True
                logger.info("route.meta: intent guard fired on %r "
                            "(no lookupable word besides an ability verb)", q)
        if not _hit:
            return None
        school = state.get("legal_entity") or ""
        topics: list = []
        sugg: list = []
        try:
            with get_cursor() as cur:
                cur.execute(
                    f"SELECT display_name FROM {config.PG_SCHEMA}.legal_entity "
                    "WHERE legal_entity_code = %s", (state.get("legal_entity"),))
                row = cur.fetchone()
                if row:
                    school = row[0]
            topics, sugg = _corpus_topics(state)
        except Exception as e:
            logger.warning("route.meta: topics lookup failed (%s)", e)
        msg = (system_message("capabilities", lang)
               .replace("{{SCHOOL}}", school)
               .replace("{{TOPICS}}", "; ".join(topics) if topics else "—"))
        logger.info("route.meta: capabilities answer (%d topics)", len(topics))
        return {
            "trace_detail": {"guard": "meta", "topics": len(topics)},
            "resolved": True, "outcome": "answer", "answer_source": "meta",
            "answer": msg, "suggestions": sugg,
            "sources_json": "[]", "user_language": lang,
        }
    except Exception as e:
        logger.warning("route.meta: check failed (%s) — continuing to cascade", e)
        return None


# ── who the school IS — metadata, never corpus ─────────────────────────────
# The name lives in legal_entity.display_name and in no searchable text, so no
# retrieval step can reach it: measured on production 2026-09-11, where the
# entity whose documents DO name it (10 chunks) still answered no_answer —
# `skola` is above bm25_max_df_share and `jmenovat` has df 0. Detection is data
# (qa_blocklist category='identity'), matching is the same whole-query equality
# as the meta path, so "jak se jmenuje školník" stays a corpus question.

def _identity_check(state: SearchState, params: dict | None = None):
    """Terminal answer naming the school, else None."""
    params = params or {}
    q = _fold_q(state.get("user_question", ""))
    _max_words = int(params.get("identity_max_words") or 8)
    if not q or len(q.split()) > _max_words:
        return None
    lang = state.get("query_language")
    try:
        _fill = _meta_fillers(lang)
        _stripped = " ".join(t for t in q.split() if t not in _fill)
        _phrases = _identity_phrases()
        if not (q in _phrases or (_stripped and _stripped in _phrases)):
            return None
        school = ""
        with get_cursor() as cur:
            cur.execute(
                f"SELECT display_name FROM {config.PG_SCHEMA}.legal_entity "
                "WHERE legal_entity_code = %s", (state.get("legal_entity"),))
            row = cur.fetchone()
            if row:
                school = (row[0] or "").strip()
        # NOTHING TO SAY IS NOT AN ANSWER. Without a name or without the message
        # row (new code, migration 216 not yet applied) the rendered text would
        # be "Tahle škola se jmenuje ." or empty — the cascade is better.
        msg = system_message("school_identity", lang) if school else ""
        if not school or not msg.strip():
            logger.info("route.identity: no name or no message for %r — "
                        "continuing to cascade", state.get("legal_entity"))
            return None
        logger.info("route.identity: named the entity")
        return {
            "trace_detail": {"guard": "identity"},
            "resolved": True, "outcome": "answer", "answer_source": "meta",
            "answer": msg.replace("{{SCHOOL}}", school),
            "sources_json": "[]", "user_language": lang,
        }
    except Exception as e:
        logger.warning("route.identity: check failed (%s) — continuing to cascade", e)
        return None


# Default policy — self_harm always wins over other categories if both match.
# Every value below is overridable from settings.config->pipeline[route].params,
# so the reaction to a risky query is data, not a rebuild. The defaults reproduce
# the behaviour that shipped before the policy existed:
#   * any hit blocks, whatever its severity (blocklist severity is NOT NULL
#     DEFAULT 1, so min_severity 1 admits every row);
#   * self_harm answers with the helpline, everything else with "ask politely".
_CATEGORY_PRIORITY = {"self_harm": 0, "hate": 1, "sexual": 2, "violence": 3, "profanity": 4}
_CATEGORY_MESSAGE = {"self_harm": "guard_self_harm"}      # else → guard_profanity
_CATEGORY_OUTCOME = {"self_harm": "self_harm"}            # else → blocked


def _guard_policy(params: dict | None) -> tuple[dict, dict, dict, dict, int]:
    """(priority, min_severity, message_key, outcome, phrase_slack) from params.

    A malformed value must not disarm the guard, so each override is merged ONTO
    the default rather than replacing it, and anything that is not a dict is
    ignored with a warning. The one thing that could silently weaken the block —
    a min_severity above 3 — is therefore still possible, but only by writing it
    explicitly for that one category.
    """
    p = params or {}

    def _merged(key: str, base: dict) -> dict:
        v = p.get(key)
        if v is None:
            return base
        if not isinstance(v, dict):
            logger.warning("route.guard: %s is %s, not an object — ignored",
                           key, type(v).__name__)
            return base
        return {**base, **v}

    prio = _CATEGORY_PRIORITY
    order = p.get("guard_category_order")
    if isinstance(order, list) and order:
        prio = {str(c): i for i, c in enumerate(order)}
    elif order is not None:
        logger.warning("route.guard: guard_category_order is not a list — ignored")

    try:
        slack = int(p.get("phrase_slack", _PHRASE_SLACK))
    except (TypeError, ValueError):
        logger.warning("route.guard: phrase_slack is not a number — using %d",
                       _PHRASE_SLACK)
        slack = _PHRASE_SLACK

    return (prio,
            _merged("guard_block_min_severity", {}),
            _merged("guard_message_key", _CATEGORY_MESSAGE),
            _merged("guard_outcome", _CATEGORY_OUTCOME),
            slack)


def _guard_text(key: str, lang: str | None) -> str:
    """Guard message for `key`, never empty.

    system_message() ends its fallback chain at _CODE_FALLBACK, which knows only
    the keys that ship with the code. A policy pointing at a NEW key
    (guard_message_key: {"hate": "guard_hate"}) whose rows were not inserted yet
    would therefore answer a blocked query with an empty string — the worst
    failure this branch can produce, because the user sees nothing at all and the
    outcome still says "blocked". So an empty result falls back to the message
    that is guaranteed to exist.
    """
    txt = system_message(key, lang)
    if txt:
        return txt
    logger.warning("route.guard: message %r missing in every language"
                   " — falling back to guard_profanity", key)
    return system_message("guard_profanity", lang)


def _guard(state: SearchState, params: dict | None = None):
    """Return (terminal dict | None, soft_hits) for the query.

    `soft_hits` are blocklist matches that were BELOW the configured severity
    threshold for their category: the query continues to the cascade, but the
    match is reported in the trace and the log. Without that, a policy that
    stops blocking mild profanity would make those queries indistinguishable
    from clean ones, and nobody could tell whether the threshold is set right.
    """
    norm = state.get("query_norm_tokens", []) or []
    lemma = state.get("query_lemma_tokens", []) or []

    # 1) usable?
    if not norm and not lemma:
        logger.info("route.guard: unusable query")
        return ({
            "trace_detail": {"guard": "unusable"},
            "resolved": True, "outcome": "unusable", "answer_source": "guard",
            "answer": system_message("guard_unusable", state.get("query_language")),
            "sources_json": "[]", "user_language": state.get("query_language"),
        }, [])

    # 2) meta-question? ("Co umíš?") — an answer, not a block
    meta = _meta_check(state, params)
    if meta:
        return meta, []

    # 2b) a question about the school's identity — metadata, not corpus
    identity = _identity_check(state, params)
    if identity:
        return identity, []

    # 3) blocklist
    prio, min_sev, msg_key, outcome_by_cat, slack = _guard_policy(params)

    lang = state.get("query_language") or "cs"
    try:
        # One resolution for both sides of the phrase match — the blocklist term
        # and the query must be reduced by the same stoplist, or _phrase_hit
        # compares token sets that were never built the same way.
        from ..settings import get_active_setting
        _cfg = get_active_setting(state.get("legal_entity"))["config"]
        singles, phrases = _load_blocklist(lang, _cfg)
    except Exception as e:  # never let a guard DB hiccup break search
        logger.warning("route.guard: blocklist unavailable (%s) — skipping", e)
        return None, []

    token_set = set(norm) | set(lemma)
    hits: list[tuple[str, int]] = [singles[t] for t in token_set if t in singles]

    query_forms = token_forms(state.get("user_question", ""), lang, _cfg)
    for term, category, severity in phrases:
        if _phrase_hit(term, query_forms, slack):
            hits.append((category, severity))

    # A hit below its category's threshold does not block — it is reported.
    blocking, soft = [], []
    for cat, sev in hits:
        try:
            floor = int(min_sev.get(cat, 1))
        except (TypeError, ValueError):
            logger.warning("route.guard: min severity for %s is not a number"
                           " — treating as 1", cat)
            floor = 1
        (blocking if sev >= floor else soft).append((cat, sev))

    if blocking:
        category = min((c for c, _ in blocking), key=lambda c: prio.get(c, 9))
        severity = max(s for c, s in blocking if c == category)
        logger.info("route.guard: blocked (%s, severity %d)", category, severity)
        return {
            "trace_detail": {"guard": "blocked", "category": category,
                             "severity": severity},
            "resolved": True,
            "outcome": outcome_by_cat.get(category, "blocked"),
            "answer_source": "guard",
            "block_category": category,
            "answer": _guard_text(msg_key.get(category, "guard_profanity"),
                                  state.get("query_language")),
            "sources_json": "[]", "user_language": state.get("query_language"),
        }, soft

    if soft:
        logger.info("route.guard: %d hit(s) below threshold — not blocking (%s)",
                    len(soft), ", ".join(f"{c}:{s}" for c, s in soft))
    return None, soft


# A pack_id no manifest row can hold. _scope() returns it when the caller may see
# NO pack, because pack_filter() reads an empty list as "all active packs" — the
# one case where "nothing matched" must not fall open.
#
# The tildes are what make it impossible to collide: pack_id is validated on the
# way in (migration 143 / gateway) to [A-Za-z0-9_-] only, so any other character
# cannot appear in a real pack_id. It used to be "\x00none", which Postgres
# rejects outright — "A string literal cannot contain NUL (0x00) characters" —
# so on an empty QA base qa_lexical threw on EVERY query and fell through. Caught
# only after the QA tables were emptied: with any pack present the sentinel was
# never reached.
_NO_PACK = "~none~"


def _scope(state: SearchState) -> list[str]:
    """The QA pack(s) THIS caller may see (migration 140).

    Until 139 this filtered on `grade = %s OR grade IS NULL` and nothing else:
    packs were picked across schools, and the entity/visibility/role columns the
    manifest already carried were never read. No leak — qa_entries carry their own
    scope and every node applies scope.where() — but the pack list was wrong, and
    a scalar `grade` could not express a multi-grade or SŠ pack.

    Now: the same predicate as everywhere else (entity + visibility + role +
    validity) plus ročník by ARRAY OVERLAP and, when the caller's level is known,
    the pack's declared level. A pack with no ročníky (NULL/empty) applies to all
    of them — the counterpart of a grade-less chunk.
    """
    from .. import scope  # local: route is imported early, scope pulls settings

    _grade = state.get("question_grade") or state.get("user_school_grade")
    grade_ids: list = []
    _exact_ids: set = set()
    _universal_ids: set = set()
    caller_level = None
    entity = state.get("legal_entity")
    try:
        with get_cursor() as cur:
            if _grade:
                # The caller's ročník as CODE_LIST IDS — and this is where it gets
                # subtle. A ročník is a row of ONE entity's číselník, so the same
                # meaning has several ids: "9999" exists once per entity and means
                # "all ročníky OF THAT ENTITY" — 1..9 under a ZŠ, 10..13 under a SŠ,
                # 1..13 under the umbrella organisation.
                #
                # Resolving only in the CALLER's entity (as this did until now) made
                # an organisation-wide pack invisible to anyone whose ročník was
                # known: the pack carried the ORG's 9999 (id 239) while the caller
                # resolved the UNIT's 9999 (id 241), and `&&` compares ids, not
                # meaning. Measured: the same question answered from the org pack
                # without a ročník and lost it with ročník=9.
                #
                # So: take every row whose code IS the caller's ročník, plus every
                # 9999 row whose OWN entity's grade scope CONTAINS that ročník.
                # entity_grade_scope() already expands an entity to its subtree, so
                # the org's 9999 covers ročník 9 while a SŠ's 9999 does not — which
                # is exactly the semantics and keeps ZŠ and SŠ apart.
                #
                # NARROWED to the caller's own tenant (plus the global rows, which
                # carry no entity). scope.where() on the manifest below would drop a
                # foreign tenant's pack anyway, but leaning on that alone means the
                # list grows with every school in the database (~2 ids per school on
                # every query) and one future grade filter written without the scope
                # predicate would turn it into a cross-tenant path. Defence in depth
                # is cheaper than the audit.
                # Cost: one correlated entity_grade_scope() call per 9999 row inside
                # the tenant — a handful.
                # ROČNÍK PATŘÍ JEDNOTCE, NE PLATFORMĚ (migrace 184). Defence in
                # depth only, and measured as such: the entity-less rows are
                # deactivated and every query here already filters is_active, so
                # dropping the IS NULL branch changed no result (pack selection is
                # identical for grade 6 / 9 / 13 / none). It is here so a
                # reactivated stray row cannot silently re-enter the grade set.
                cur.execute(
                    f"SELECT c.id FROM {config.PG_SCHEMA}.code_list c"
                    f" WHERE c.list_code = 'school_grade' AND c.is_active"
                    f"   AND c.legal_entity_code = ANY(%s)"
                    f"   AND (c.code = %s"
                    f"     OR (c.code = '9999' AND EXISTS ("
                    f"          SELECT 1 FROM {config.PG_SCHEMA}.entity_grade_scope(c.legal_entity_code) s"
                    f"            JOIN {config.PG_SCHEMA}.code_list c2 ON c2.id = s.id"
                    f"           WHERE c2.code = %s)))",
                    (list(state.get("organization_entities") or [entity]),
                     str(_grade), str(_grade)))
                grade_ids = [r[0] for r in cur.fetchall()]
                # split for the QA tier in qa_lexical: an EXACT ročník row ranks
                # above a covering 9999, which ranks above no ročník at all
                cur.execute(
                    f"SELECT c.id, c.code FROM {config.PG_SCHEMA}.code_list c"
                    f" WHERE c.id = ANY(%s)", (grade_ids,))
                for _rid, _rcode in cur.fetchall():
                    (_universal_ids if str(_rcode) == "9999" else _exact_ids).add(_rid)
            # level: the caller's UNIT is authoritative; only an entity without one
            # (an organisation spanning ZŠ+SŠ) falls back to the ročník map
            cur.execute(
                f"SELECT (SELECT level FROM {config.PG_SCHEMA}.legal_entity"
                f"         WHERE legal_entity_code = %s),"
                f"       {config.PG_SCHEMA}.grade_level(%s, %s)",
                (entity, entity, str(_grade) if _grade else None))
            _row = cur.fetchone() or (None, None)
            caller_level = (_row[0] or _row[1] or None)
            if caller_level not in ("MS", "ZS", "SS"):
                caller_level = None

            scope_cond, scope_params = scope.where(
                "m", state.get("entity_scope") or [],
                state.get("organization_entities") or [], state.get("user_id"),
                state.get("user_role"), validity=True)
            # THE PACK IS A PRE-SELECTION, and the manifest's own domain label is
            # what makes it one: a pack labelled only "Přijímačky" holds no
            # answer for a caller who chose "Stravování", so it must not enter
            # pack_ids at all — nor offer_pack_ids below, which is a list of
            # buttons and would otherwise offer questions from an excluded area.
            #
            # This does NOT replace the per-entry filter in qa_common. A mixed
            # pack passes here on any one of its areas and is then narrowed row
            # by row; the manifest overlap only avoids scanning packs that cannot
            # contribute. An unlabelled pack (domain_ids NULL) therefore does NOT
            # pass an explicit filter — it is unclassified material, and spec §6
            # makes tagging it a data-acceptance task, not something this query
            # guesses at.
            dom_cond, dom_params = scope.domain_where("m", "domain_ids",
                                                      state.get("domain_ids"))

            sql = (f"SELECT pack_id FROM {config.QA_MANIFEST_TABLE} m"
                   f" WHERE m.is_active{scope_cond}{dom_cond}")
            params = [*scope_params, *dom_params]
            if grade_ids:
                sql += (" AND (m.school_grade_ids IS NULL"
                        " OR cardinality(m.school_grade_ids) = 0"
                        " OR m.school_grade_ids && %s::bigint[])")
                params.append(grade_ids)
            if caller_level:
                sql += " AND (m.school_level IS NULL OR m.school_level = %s)"
                params.append(caller_level)
            cur.execute(sql, params)
            packs = [r[0] for r in cur.fetchall()]

            # ── SAFETY NET: own-school packs excluded ONLY by the ročník ────────
            # They must never answer — a curated answer written for ročník 9 is
            # simply wrong for a ročník-6 pupil, and it is returned verbatim, so
            # nothing downstream can soften it. But dropping them silently means a
            # mistagged pack makes its content unreachable with no trace of it ever
            # having existed, and once schools tag their own packs that will be the
            # common failure, not the rare one. So they are collected here and
            # OFFERED as suggestions instead (qa_lexical).
            #
            # Restricted to the caller's OWN entity on purpose. A sibling unit's
            # pack must not even be offered: in this very corpus the SOŠ answer to
            # "Můžu mít ve škole mobilní telefon?" is the OPPOSITE of the ZŠ one
            # (breaks allowed vs forbidden), so showing it to a ZŠ pupil would be
            # worse than showing nothing. The level guard is kept for the same
            # reason; only the ročník condition is relaxed.
            offer_packs: list[str] = []
            if grade_ids and entity:
                # The net relaxes the ROČNÍK and nothing else — the same rule the
                # domain filter follows: a question from an area the caller did
                # not choose is not a helpful button, it is the automatic
                # widening spec §1 forbids. So scope, role, level and now the
                # area all still apply; only the year condition is inverted.
                osql = (f"SELECT pack_id FROM {config.QA_MANIFEST_TABLE} m"
                        f" WHERE m.is_active{scope_cond}{dom_cond}"
                        f"   AND m.legal_entity_code = %s"
                        f"   AND m.school_grade_ids IS NOT NULL"
                        f"   AND cardinality(m.school_grade_ids) > 0"
                        f"   AND NOT (m.school_grade_ids && %s::bigint[])")
                oparams = [*scope_params, *dom_params, entity, grade_ids]
                if caller_level:
                    osql += " AND (m.school_level IS NULL OR m.school_level = %s)"
                    oparams.append(caller_level)
                cur.execute(osql, oparams)
                offer_packs = [r[0] for r in cur.fetchall() if r[0] not in packs]
    except Exception as e:
        # QA is one branch of the cascade; a manifest problem must not take the
        # whole search down. Fall CLOSED — the document branch still answers.
        logger.warning("route.scope: pack selection failed (%s) — QA branch skipped", e)
        return [_NO_PACK], set(), set(), []

    logger.info("route.scope: grade=%s level=%s → packs=%s (offer-only: %s)",
                _grade, caller_level, packs or "NONE", offer_packs or "none")
    return (packs or [_NO_PACK]), _exact_ids, _universal_ids, offer_packs


def route(state: SearchState, params: dict | None = None) -> dict:
    blocked, soft_hits = _guard(state, params)
    if blocked is not None:
        return blocked
    try:
        pack_ids, _ex_ids, _un_ids, _offer = _scope(state)
    except Exception as e:
        # FALL CLOSED. This said "falling back to all packs" and meant it:
        # pack_filter reads an empty pack_ids as "no pack restriction", so an
        # exception here opened every active pack in the database to the caller —
        # every school's, since the manifest scope predicate had not run either.
        # _scope's own except already returns the sentinel for exactly this
        # reason; this outer handler was the one path that did not, and with a
        # thematic filter in play it would also have answered from areas the
        # caller excluded. The document branch still answers.
        logger.warning("route.scope: failed (%s) — QA branch skipped "
                       "(no pack in scope)", e)
        pack_ids, _ex_ids, _un_ids, _offer = [_NO_PACK], set(), set(), []
    # guard passed: say so explicitly — "the blocklist was checked and nothing
    # matched" is a result, and an empty cell would not distinguish it from
    # "this step does nothing".
    _pass = {"guard": "pass", "packs": len(pack_ids)}
    if _offer:
        # the diagnostic that was missing: "packs: 1" never said WHY it was 1
        _pass["packs_offer_only_grade"] = _offer
    if soft_hits:
        # the query was allowed through DESPITE a blocklist match — say so, or a
        # threshold set too low looks exactly like a clean query
        _pass["guard_soft_hits"] = [{"category": c, "severity": s}
                                    for c, s in soft_hits]
    return {"trace_detail": _pass,
            "pack_ids": pack_ids, "offer_pack_ids": _offer, "resolved": False,
            # consumed by qa_lexical's tier — resolved once, here, rather than
            # repeated per QA step
            "grade_exact_ids": sorted(_ex_ids),
            "grade_universal_ids": sorted(_un_ids)}
