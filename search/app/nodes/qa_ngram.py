"""The last chip of a qa_lexical offer by character n-grams (`offer_ngram`, migration 222).

Without a confident answer, the last place of the offer goes to the curated question
closest in character 3-5-grams (TF-IDF over the packs in scope, each variant scored
separately), if it reaches `min_sim`: it catches colloquial and derived forms the
lemma misses ("matika" / "matematika"). Generic words are left out; ties go by text.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict

from .. import config
from .. import scope
from ..settings import coverage_stopwords
from .qa_common import pack_filter, domain_filter, load_synonym_groups, _COVERAGE_STOP

logger = logging.getLogger(__name__)

DEFAULTS = {"min_sim": 0.30, "max_df_share": 0.10, "min_len": 3}
_INDEX: dict[tuple, tuple[float, "NgramIndex"]] = {}
# scopes kept (the key includes the signed-in user); a rebuild costs ~0.1 s per school
_INDEX_MAX = 16
_TTL = 300.0          # a pack edit shows within 5 min


def options(spec) -> dict:
    """`offer_ngram` as set in the step params: true, or a dict overriding DEFAULTS."""
    out = dict(DEFAULTS)
    if isinstance(spec, dict):
        out.update({k: spec[k] for k in DEFAULTS if k in spec})
    return out


def grams(words) -> Counter:
    """Character 3-, 4- and 5-grams of each word, the word marked with < and >."""
    out: Counter = Counter()
    for w in words:
        w2 = f"<{w}>"
        for n in (3, 4, 5):
            for i in range(len(w2) - n + 1):
                out[w2[i:i + n]] += 1
    return out


def _vector(counts: Counter, idf: dict, idf_unseen: float) -> dict:
    v = {g: (1.0 + math.log(c)) * idf.get(g, idf_unseen) for g, c in counts.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {g: x / norm for g, x in v.items()}


class NgramIndex:
    """The variants of the packs in scope as normalised n-gram vectors.

    rows: (entry_id, canonical_q, norm_text, eligible). Every row counts for the
    statistics, only an eligible row (chosen areas) can become a chip.
    """

    def __init__(self, rows, stop: set[str], max_df_share: float, min_len: int):
        self.min_len = min_len
        self.question = {}
        words_of = []
        entry_words: dict[str, set] = {}
        for eid, q, text, ok in rows:
            ws = [w for w in (text or "").split()
                  if len(w) >= min_len and not w.isdigit() and w not in stop]
            words_of.append(ws)
            entry_words.setdefault(eid, set()).update(ws)
            self.question[eid] = q
        n_entries = max(len(entry_words), 1)
        df_word = Counter(w for ws in entry_words.values() for w in ws)
        self.generic = {w for w, d in df_word.items() if d / n_entries > max_df_share} | set(stop)
        counts = [grams([w for w in ws if w not in self.generic]) for ws in words_of]
        entry_grams: dict[str, set] = {}
        for (eid, _q, _t, _ok), c in zip(rows, counts):
            entry_grams.setdefault(eid, set()).update(c)
        df = Counter(g for gs in entry_grams.values() for g in gs)
        self.idf = {g: math.log((n_entries + 1) / (d + 0.5)) for g, d in df.items()}
        self.idf_unseen = math.log((n_entries + 1) / 0.5)
        # inverted: n-gram -> [(variant, weight)], so a query touches only the
        # variants that share one of its n-grams (most n-grams are rare)
        self.owner: list[str] = []
        self.postings: dict[str, list] = defaultdict(list)
        for (eid, _q, _t, ok), c in zip(rows, counts):
            if not (ok and c):
                continue
            i = len(self.owner)
            self.owner.append(eid)
            for g, w in _vector(c, self.idf, self.idf_unseen).items():
                self.postings[g].append((i, w))

    def query_words(self, tokens) -> list[str]:
        return [w for w in tokens or () if len(w) >= self.min_len and not w.isdigit()
                and w not in self.generic]

    def rank(self, tokens, min_sim: float, limit: int = 10) -> list[tuple]:
        """[(entry_id, canonical_q, similarity)] best first, similarity ≥ min_sim."""
        words = self.query_words(tokens)
        if not words:
            return []
        q = _vector(grams(words), self.idf, self.idf_unseen)
        acc: dict[int, float] = defaultdict(float)
        for g, x in q.items():
            for i, w in self.postings.get(g, ()):
                acc[i] += x * w
        best: dict[str, float] = {}
        for i, s in acc.items():
            eid = self.owner[i]
            if s > best.get(eid, -1.0):
                best[eid] = s
        # exact tie: question text, then id (never the row order); rounding absorbs float noise
        out = sorted(((eid, s) for eid, s in best.items() if s >= min_sim),
                     key=lambda t: (-round(t[1], 9), self.question.get(t[0]) or "", str(t[0])))
        return [(eid, self.question.get(eid), s) for eid, s in out[:limit]]


def merge_chip(items: list[dict], ranked: list[tuple], top_n: int) -> tuple[list[dict], dict | None]:
    """The first `top_n - 1` chips stay; the last place goes to the best n-gram
    entry that is not already among them (by entry or by question text); the
    rest of the old row follows. Returns (chips, the n-gram chip or None)."""
    items = list(items or [])
    if top_n < 1 or not ranked:
        return items[:max(top_n, 0)], None
    head = items[:top_n - 1]
    ids = {i.get("entry_id") for i in head}
    texts = {(i.get("question") or "").strip().casefold() for i in head}
    chip = None
    for eid, q, s in ranked:
        if eid in ids or (q or "").strip().casefold() in texts:
            continue
        chip = {"kind": "qa", "entry_id": eid, "question": q, "score": round(float(s), 4)}
        break
    if chip is None:
        return items[:top_n], None
    rest = [i for i in items[top_n - 1:]
            if i.get("entry_id") != chip["entry_id"]
            and (i.get("question") or "").strip().casefold() != (chip["question"] or "").strip().casefold()]
    return (head + [chip] + rest)[:top_n], chip


def _stop_words(cur, lang) -> set[str]:
    stop = set(coverage_stopwords().get(lang or "") or _COVERAGE_STOP)
    groups, kind_of, _p, _t = load_synonym_groups(cur, lang)
    for gid, toks in groups.items():
        if kind_of.get(gid) == "interrogative":
            stop |= set(toks)
    return stop


def ranked_for(cur, state: dict, opts: dict) -> list[tuple]:
    """The n-gram ranking of the query over the packs in scope; the index is
    cached per scope (packs, language, identity, areas, options) for _TTL seconds."""
    lang = state.get("query_language")
    frag, pack_params = pack_filter(state.get("pack_ids", []), alias="e",
                                    language=state.get("language_filter"))
    scope_cond, scope_params = scope.where(
        "v", state.get("entity_scope") or [], state.get("organization_entities") or [],
        state.get("user_id"), state.get("user_role"), validity=True)
    dom_cond, dom_params = domain_filter(state, "v")
    key = (frag, scope_cond, dom_cond, repr(pack_params), repr(scope_params), repr(dom_params),
           lang, float(opts["max_df_share"]), int(opts["min_len"]))
    hit = _INDEX.get(key)
    if hit and time.time() - hit[0] < _TTL:
        index = hit[1]
    else:
        cur.execute(
            f"SELECT v.entry_id::text, e.canonical_q, v.norm_text, (TRUE{dom_cond}) AS ok"
            f"  FROM {config.QA_VARIANTS_TABLE} v"
            f"  JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id"
            f" WHERE v.is_active AND {frag}{scope_cond}"
            f" ORDER BY v.entry_id, v.norm_text",
            [*dom_params, *pack_params, *scope_params])
        rows = [(r[0], r[1], r[2], bool(r[3])) for r in cur.fetchall()]
        if not rows:
            return []
        index = NgramIndex(rows, _stop_words(cur, lang), float(opts["max_df_share"]),
                           int(opts["min_len"]))
        if len(_INDEX) >= _INDEX_MAX:
            try:                           # oldest out; a concurrent eviction is harmless
                _INDEX.pop(next(iter(_INDEX)), None)
            except (StopIteration, RuntimeError):
                pass
        _INDEX[key] = (time.time(), index)
    return index.rank(state.get("query_norm_tokens") or [], float(opts["min_sim"]))


def with_ngram_chip(out: dict, state: dict, params: dict, cur) -> dict:
    """Put the n-gram chip into the offer qa_lexical parked (deferred list, else the
    dead-end fallback); a confident answer or a result without an offer stays as it is."""
    if out.get("resolved"):
        return out
    has_deferred = isinstance(out.get("deferred_suggest"), dict)
    if not has_deferred and "fallback_suggestions" not in out:
        return out
    top_n = int(params.get("suggest_top_n", config.QA_SUGGEST_TOP_N))
    opts = options(params.get("offer_ngram"))
    try:
        ranked = ranked_for(cur, state, opts)
    except Exception as e:
        logger.warning("qa_ngram: failed (%s) — offer unchanged", e)
        return out
    out = dict(out)
    if has_deferred:
        ds = dict(out["deferred_suggest"])
        ds["suggestions"], chip = merge_chip(ds.get("suggestions"), ranked, top_n)
        out["deferred_suggest"] = ds
    else:
        out["fallback_suggestions"], chip = merge_chip(out.get("fallback_suggestions"), ranked, top_n)
    if isinstance(out.get("trace_detail"), dict):
        out["trace_detail"] = {**out["trace_detail"],
                               "ngram_chip": {"entry_id": chip and chip["entry_id"],
                                              "score": chip and chip["score"],
                                              "min_sim": opts["min_sim"],
                                              "candidates": len(ranked)}}
    return out
