"""Typo repair of the query before the curated-QA match (`query_repair`, migration 220).

A word the lemmatizer leaves as it is and the dictionary does not know ("abesnce")
becomes the ONE word of the asker's curated questions within edit distance 1
(2 from 8 letters); a tie or nothing near leaves it unchanged.
"""

from __future__ import annotations

import logging
import time

from .. import config
from ..db import get_cursor

logger = logging.getLogger(__name__)

_VOCAB: dict[tuple, tuple[float, set[str]]] = {}
_TTL = 300.0
# scopes kept (school, role, areas and signed-in user); a miss costs one read
_VOCAB_MAX = 64

def _vocab(state: dict, pack_ids) -> set[str]:
    """Words of the curated questions the asker may get (packs, scope.where with
    validity, chosen areas); never a word of an entry outside that scope.
    Cached per scope for _TTL seconds."""
    from .. import scope
    from .qa_common import pack_filter, domain_filter
    frag, pack_params = pack_filter(pack_ids, alias="e", language=state.get("language_filter"))
    scope_cond, scope_params = scope.where(
        "v", state.get("entity_scope") or [], state.get("organization_entities") or [],
        state.get("user_id"), state.get("user_role"), validity=True)
    dom_cond, dom_params = domain_filter(state, "v")
    key = (frag, scope_cond, dom_cond, repr(pack_params), repr(scope_params), repr(dom_params))
    hit = _VOCAB.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    words: set[str] = set()
    with get_cursor() as cur:
        cur.execute(
            f"SELECT v.lemma_text, v.norm_text FROM {config.QA_VARIANTS_TABLE} v"
            f" JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id"
            f" WHERE v.is_active AND {frag}{scope_cond}{dom_cond}",
            [*pack_params, *scope_params, *dom_params])
        for lem, norm in cur.fetchall():
            words.update((lem or "").split())
            words.update((norm or "").split())
    if len(_VOCAB) >= _VOCAB_MAX:
        try:                               # oldest out; a concurrent eviction is harmless
            _VOCAB.pop(next(iter(_VOCAB)), None)
        except (StopIteration, RuntimeError):
            pass
    _VOCAB[key] = (time.time(), words)
    return words


def _dist(a: str, b: str, cap: int) -> int:
    """Damerau-Levenshtein (adjacent swap), early exit above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _known(word: str, langs) -> bool:
    """Whether any of `langs` knows the word; a word no language can judge counts as known."""
    try:
        from simplemma import is_known
    except Exception:
        return True
    judged = False
    for lang in langs or ():
        try:
            if is_known(word, lang=lang):
                return True
            judged = True
        except Exception:
            continue                       # a language simplemma has no dictionary for
    return not judged


def _repair_languages(state: dict) -> tuple:
    """The query's language when decided, else the school's languages (as prepare())."""
    if state.get("query_language"):
        return (state["query_language"],)
    from ..languages import languages_for_entity
    return tuple(languages_for_entity(state.get("legal_entity")))


def repair_state(state: dict, modes, pack_ids) -> dict:
    modes = set(modes if isinstance(modes, (list, tuple, set)) else [modes])
    norm = list(state.get("query_norm_tokens") or [])
    lemma = list(state.get("query_lemma_tokens") or [])
    if not norm or len(norm) != len(lemma):
        return state                       # not aligned: leave the query alone
    try:
        vocab = _vocab(state, pack_ids)
    except Exception as e:                 # a repair is a help: never fail the step
        logger.warning("query_repair: vocabulary failed (%s) — query unchanged", e)
        return state
    if not vocab:
        return state
    langs = _repair_languages(state)
    changed = []
    for i, (n, lm) in enumerate(zip(norm, lemma)):
        if lm in vocab or len(n) < 4 or n.isdigit():
            continue
        new = None
        if "typo" in modes and lm == n and not _known(n, langs):
            cap = 2 if len(n) >= 8 else 1
            best, ties = None, 0
            d_best = cap + 1
            for w in vocab:
                if w[0] != n[0] and _dist(n[:2], w[:2], 2) > 1:
                    continue
                d = _dist(n, w, cap)
                if d < d_best:
                    best, d_best, ties = w, d, 1
                elif d == d_best:
                    ties += 1
            if best is not None and ties == 1:
                new = best
        if new and new != lm:
            changed.append(f"{n}→{new}")
            norm[i], lemma[i] = new, new
    if not changed:
        return state
    logger.info("query_repair: %d word(s) repaired", len(changed))
    return {**state, "query_norm_tokens": norm, "query_lemma_tokens": lemma,
            "query_repair": changed}
