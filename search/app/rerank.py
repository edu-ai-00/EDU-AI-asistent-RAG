# last change in code by Martin Safranek (2026-08-22).
"""In-process cross-encoder reranker (CPU).

A cross-encoder reads the (query, chunk) pair TOGETHER and scores true relevance,
which the lexical/vector/keyword signals — each blind to the other side — cannot.
It resolves the cases BM25 can't: a blog ABOUT docházka vs the RULES for docházka,
"pomůcky do školy" vs "povolené pomůcky u zkoušky".

Loaded lazily on first use (the ~2 GB model download lands in HF_HOME=/models,
a named volume) and cached process-wide. Every failure degrades to None so the
caller simply keeps the fusion order — the reranker is never load-bearing.
"""
import logging
import os
import threading

from . import config
from .settings import platform_int

logger = logging.getLogger(__name__)

# The judge's instructions are DATA, like every other LLM prompt in this system
# (edu_ai.prompt_registry, hot-editable per request — no rebuild, no restart).
# Until now they lived in this file, so tuning the reranker meant a docker build;
# the constants below are only the fallback for a DB that cannot be read.
PROMPT_KEY_ORDER = "search_rerank_order"
PROMPT_KEY_GRADED = "search_rerank_graded"


_ORDER_PROMPT = ("Seřaď pasáže podle toho, jak dobře odpovídají na dotaz. "
                 "Vrať POUZE JSON pole indexů (čísel) od nejlepší po nejhorší,"
                 " všechny indexy.")


def _registry_prompt(key: str) -> str | None:
    """Active prompt text for `key`, or None (caller falls back to the constant)."""
    try:
        from .db import get_cursor
        from . import config
        with get_cursor() as cur:
            cur.execute(
                f"SELECT prompt_text FROM {config.PG_SCHEMA}.prompt_registry "
                "WHERE prompt_key = %s AND is_active ORDER BY version DESC LIMIT 1",
                (key,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("rerank: prompt_registry lookup for %s failed (%s) —"
                       " code fallback", key, e)
    return None

_MODEL = None
_FAILED = False
_LOCK = threading.Lock()


def _load():
    global _MODEL, _FAILED
    if _MODEL is not None or _FAILED:
        return _MODEL
    with _LOCK:
        if _MODEL is not None or _FAILED:
            return _MODEL
        try:
            from sentence_transformers import CrossEncoder
            name = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
            # Platform-wide, NOT per school: the model is a process-lifetime
            # singleton, so a per-school length would be decided by whichever
            # school happened to trigger the load first.
            max_len = platform_int("rerank_max_length", 512)
            logger.info("rerank: loading cross-encoder %s (max_length=%d,"
                        " first call may download) …", name, max_len)
            _MODEL = CrossEncoder(name, max_length=max_len)
            logger.info("rerank: model ready (%s)", name)
        except Exception as e:
            _FAILED = True
            logger.warning("rerank: model unavailable (%s) — reranking disabled", e)
    return _MODEL


def _effort_kwargs(model: str, effort: str | None) -> dict:
    """reasoning_effort for the GPT-5.x tier, nothing for the older models.

    The 5.x models reject `temperature` and, left to their default effort, spend
    minutes on a judgement that has to fit inside a 3-second search. They are also
    the only reason this exists: an older model rejects `reasoning_effort` itself.
    """
    m = (model or "").lower()
    if effort and ("gpt-5" in m or "luna" in m):
        return {"reasoning_effort": effort}
    return {}


def rerank_scores(query: str, docs: list[str], backend: str = "local",
                  passage_chars: int = 500, model: str | None = None,
                  effort: str | None = None,
                  timeout: float | None = None) -> list[float] | None:
    """Cross-encoder relevance scores aligned to `docs`. None when the model can't
    load or scoring fails — the caller then keeps the existing fusion order.
    backend: "local" (bge cross-encoder in-process), "openai" (LLM listwise
    reranker) or "jina" (hosted cross-encoder at api.jina.ai)."""
    if not query or not docs:
        return None
    if backend == "openai":
        return _openai_scores(query, docs, passage_chars, model, effort)
    if backend == "jina":
        return _jina_scores(query, docs, passage_chars, model, timeout=timeout)
    model = _load()
    if model is None:
        return None
    try:
        scores = model.predict([[query, d or ""] for d in docs])
        return [float(s) for s in scores]
    except Exception as e:
        logger.warning("rerank: predict failed (%s) — keeping fusion order", e)
        return None


def _jina_scores(query: str, docs: list[str], passage_chars: int = 500,
                 model: str | None = None,
                 timeout: float | None = None) -> list[float] | None:
    """Relevance scores from the hosted cross-encoder, aligned to `docs`.

    Why a third backend at all: the LLM listwise reranker returns a PERMUTATION,
    and a position cannot carry a threshold — which is why our veto had to be a
    verdict (grade 2/1/0) that the model re-decides on every run. A cross-encoder
    returns a calibrated score, so the veto becomes one number in the settings.
    Measured 2026-08-27 on the pool that broke "můžeme ve škole fotit?": the SŠ
    photography rule scored 0.3224, the topically adjacent "Kavky Environmental
    Challenge" 0.0125, everything else negative — a separation of one order of
    magnitude. Three identical calls returned the identical score to four
    decimals, against an LLM order that differs from position 3 down.

    No SDK: one POST with urllib keeps the dependency list unchanged. On any
    failure returns None and the caller keeps the fusion order, exactly like the
    other two backends — a reranker is never allowed to break a search.
    """
    if not config.JINA_API_KEY:
        logger.warning("rerank(jina): JINA_API_KEY is empty — keeping fusion order")
        return None
    import json as _j
    import urllib.request as _u
    passages = [(d or "")[:max(120, int(passage_chars or 500))] for d in docs]
    body = _j.dumps({"model": (model or config.RERANK_JINA_MODEL),
                     "query": query,
                     "documents": passages,
                     "top_n": len(passages),
                     "return_documents": False}).encode()
    try:
        req = _u.Request(config.RERANK_JINA_URL, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.JINA_API_KEY})
        with _u.urlopen(req, timeout=float(timeout or 15)) as resp:
            data = _j.loads(resp.read().decode())
    except Exception as e:
        logger.warning("rerank(jina): call failed (%s) — keeping fusion order", e)
        return None
    # The API answers with results in RELEVANCE order, each carrying the index it
    # had on the way in. The caller aligns scores to `docs` positionally, so the
    # order has to be undone here — reading them in the returned order would
    # silently score the wrong passages.
    out = [None] * len(passages)
    for it in (data.get("results") or []):
        i = it.get("index")
        if isinstance(i, int) and 0 <= i < len(out):
            try:
                out[i] = float(it.get("relevance_score"))
            except (TypeError, ValueError):
                pass
    if all(v is None for v in out):
        logger.warning("rerank(jina): no usable scores — keeping fusion order")
        return None
    _miss = sum(1 for v in out if v is None)
    if _miss:
        logger.warning("rerank(jina): %d of %d passages unscored — treated as "
                       "lowest", _miss, len(out))
    _floor = min(v for v in out if v is not None)
    logger.info("rerank(jina:%s): %d passages, %d tokens, best %.4f",
                model or config.RERANK_JINA_MODEL, len(passages),
                (data.get("usage") or {}).get("total_tokens", -1),
                max(v for v in out if v is not None))
    return [(_floor - 1.0 if v is None else v) for v in out]


# Public name for the answer node's PREFILTER, which needs the raw calibrated
# score and nothing else — no order, no veto. Kept as an alias so the backend
# selection in rerank_scores() stays the single place that maps mode -> function.
jina_scores = _jina_scores


def _openai_scores(query: str, docs: list[str], passage_chars: int = 500,
                   model: str | None = None,
                   effort: str | None = None) -> list[float] | None:
    """LLM listwise reranker (no local model): one call asks an OpenAI chat model
    to order the candidate passages by relevance; scores are derived from rank.
    Model from RERANK_OPENAI_MODEL (default gpt-4o-mini). Degrades to None."""
    import json as _json
    import os as _os
    # per-school settings first (rerank_model), env as the fallback: the judge's
    # model was the last piece of the reranker still living in docker-compose,
    # while its mode sat in settings and its prompt in prompt_registry.
    model = model or _os.getenv("RERANK_OPENAI_MODEL", "gpt-4o-mini")
    passages = _passages(docs, passage_chars)
    sys = _registry_prompt(PROMPT_KEY_ORDER) or _ORDER_PROMPT
    usr = f"Dotaz: {query}\n\nPasáže:\n{passages}"
    try:
        from openai import OpenAI
        resp = OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": usr}],
            **_effort_kwargs(model, effort))
        txt = resp.choices[0].message.content or ""
        import re as _re
        m = _re.search(r"\[[\d,\s]*\]", txt)
        order = _json.loads(m.group(0)) if m else []
        # rank → score: best gets len, worst gets 1; unranked docs get 0
        n = len(docs)
        scores = [0.0] * n
        for rank, idx in enumerate(order):
            if isinstance(idx, int) and 0 <= idx < n:
                scores[idx] = float(n - rank)
        logger.info("rerank(openai:%s): ordered %d/%d passages", model, len(order), n)
        return scores
    except Exception as e:
        logger.warning("rerank(openai): failed (%s) — keeping fusion order", e)
        return None

def log_passages(docs: list[str], passage_chars: int, chunks=None) -> None:
    """What the judge is about to read, one line per passage.

    Off by default (rerank_log_passages): the input to the judge was the one thing
    in this pipeline that could not be inspected — the diagnostics show the ORDER
    it returned, never the text it saw, so "why did it rank that first" could only
    be guessed at.
    """
    n = max(120, int(passage_chars or 500))
    for i, d in enumerate(docs):
        head, _, body = (d or "").partition("\n")
        c = (chunks[i] if chunks and i < len(chunks) else {}) or {}
        _full = len((c.get("chunk_text") or "").strip())
        logger.info("rerank IN [%02d] id=%-4s body=%4dz%s grades=%-12s lvl=%-4s "
                    "cov=%-5s lex=%-4s vec=%-4s | %s || %s",
                    i, c.get("chunk_id"), _full, " CUT" if _full > n else "    ",
                    ",".join(str(g) for g in (c.get("school_grade_ids") or [])) or "-",
                    c.get("school_level") or "-",
                    c.get("coverage"), c.get("lexical_rank"), c.get("vector_rank"),
                    head[:70].replace("\n", " "), body[:70].replace("\n", " "))
    logger.info("rerank IN: %d passages, cut at %d chars", len(docs), n)


def log_order(cand, docs=None) -> None:
    """What the judge returned, joined back to what it read.

    The IN position (tier order) beside the OUT position (the judge's order) is the
    only way to see whether the judge MOVED anything, and where the passage holding
    the answer ended up. Without it the diagnostics show a result and no reasoning.
    """
    _by_in = {id(c): i for i, c in enumerate(cand)}
    for c in sorted(cand, key=lambda x: (x.get("rerank_pos") or 999)):
        head = ((c.get("heading_path") or c.get("section_heading") or "")
                .replace("\n", " "))[:70]
        logger.info("rerank OUT [%02d] id=%-4s rerank=%-7s grade=%-4s | %s",
                    c.get("rerank_pos") or 0, c.get("chunk_id"),
                    c.get("rerank"), c.get("rerank_grade"), head)


def _passages(docs: list[str], passage_chars: int) -> str:
    """The candidate list as the reranker sees it. `passage_chars` is a parameter
    because the cut decides what the model can judge: 500 was fine while its
    output only REORDERED, but a graded verdict may VETO a passage, and vetoing on
    a third of a chunk is a different kind of mistake."""
    n = max(120, int(passage_chars or 500))
    # THE CUT MUST BE VISIBLE. The judge is asked to look for "číslo, podmínku,
    # termín, postup, výčet" — a criterion a truncated passage can fail for a
    # reason it cannot see, which biases exactly the LONG chunks (the ones that
    # carry facts) toward grade 1. Saying the text continues lets it treat a
    # missing fact as unknown instead of absent.
    out = []
    for i, d in enumerate(docs):
        t = (d or "")
        out.append(f"[{i}] {t[:n]}" + (" …(zkráceno)" if len(t) > n else ""))
    return "\n".join(out)


def rerank_graded(query: str, docs: list[str], passage_chars: int = 900,
                  model: str | None = None, effort: str | None = None):
    """Per-passage VERDICT + rank, or None. Cloud (LLM) only.

    Why a verdict and not a 0..1 score: our listwise reranker returns a
    permutation, and the scores derived from it (n, n-1, … 1) are positions in
    disguise — "6.0" means eleventh of sixteen, not "weak", so no absolute
    threshold can be written on them and every cut becomes a guess about pool
    size. An LLM is reliable at a 3-way judgement and poorly calibrated at a
    continuous one, so the verdict is what we ask for; the rank stays as the
    tie-break INSIDE a verdict.

      grade 2 — the passage contains a fact that answers the question
      grade 1 — same topic, but no answer in it
      grade 0 — unrelated (even when it repeats the query's words)

    Returns a list aligned to `docs` of {"grade": int, "rank": int}. A passage the
    model forgot gets grade 1 / last rank — neutral, never vetoed by omission.
    """
    import json as _json
    import os as _os
    model = model or _os.getenv("RERANK_OPENAI_MODEL", "gpt-4o-mini")
    n = len(docs)
    # Measured 2026-08-22: with the earlier wording the model handed out exactly ONE
    # grade 2 per query (1 of 8 passages, 1 of 20) — it read "2" as "the best
    # passage" instead of "contains an answer". On "Z čeho se skládá jednotná
    # přijímací zkouška?" the MŠMT chunk stating the test durations (60 and 70
    # minutes, and inside the 900-char window, so the judge did see them) got a 1,
    # and because a document needs a grade-2 chunk to seed a paragraph, the answer
    # lost its second source. Hence the two additions below: a PARTIAL fact counts,
    # and the number of 2s is explicitly unlimited.
    sys = _registry_prompt(PROMPT_KEY_GRADED) or (
          "Posuď každou pasáž vůči dotazu. Ke KAŽDÉ pasáži vrať grade a rank.\n"
           "grade 2 = pasáž obsahuje údaj, který je součástí odpovědi — stačí "
           "ČÁST odpovědi (termín, délka, počet, podmínka, jeden z vyjmenovaných "
           "prvků). Grade 2 může dostat libovolný počet pasáží, i všechny; "
           "nehledáš nejlepší pasáž, ale každou, ze které lze odpovědět.\n"
           "grade 1 = pasáž je o stejném tématu, ale žádný takový údaj v ní není.\n"
           "grade 0 = pasáž s dotazem nesouvisí, i kdyby opakovala jeho slova.\n"
           "Příklad 1: na dotaz „můžeme ve škole fotit?“ je pasáž o kariérovém "
           "poradenství grade 0, pasáž o pravidlech mobilů grade 2.\n"
           "Příklad 2: na dotaz „z čeho se skládá jednotná přijímací zkouška?“ je "
           "pasáž „test z českého jazyka trvá 60 minut, z matematiky 70 minut“ "
           "grade 2 (údaj o testech, byť jen část odpovědi) a pasáž „výsledky se "
           "zveřejní 15. května“ grade 1.\n"
           "rank = pořadí od 1 (nejlepší) výš, každé číslo použij jednou.")
    usr = f"Dotaz: {query}\n\nPasáže:\n{_passages(docs, passage_chars)}"
    schema = {"type": "object", "additionalProperties": False,
              "required": ["passages"],
              "properties": {"passages": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["i", "grade", "rank"],
                  "properties": {"i": {"type": "integer"},
                                 "grade": {"type": "integer", "enum": [0, 1, 2]},
                                 "rank": {"type": "integer"}}}}}}
    try:
        from openai import OpenAI
        resp = OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": usr}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "rerank_graded", "strict": True, "schema": schema}},
            **_effort_kwargs(model, effort))
        data = _json.loads(resp.choices[0].message.content or "{}")
        # SILENCE IS NOT A PASS. The default used to be grade 1 — and 1 is exactly
        # the threshold rerank_require_grade admits, so a passage the judge never
        # mentioned was treated as "same topic" and could found a paragraph. The
        # prompt does demand a verdict for every passage, but the JSON schema
        # cannot enforce it (no minItems, no per-index requirement), so the safe
        # default has to carry it: unjudged = 0 = not admitted.
        out = [{"grade": 0, "rank": n, "judged": False} for _ in range(n)]
        seen = 0
        for it in data.get("passages") or []:
            i = it.get("i")
            if isinstance(i, int) and 0 <= i < n:
                out[i] = {"grade": int(it.get("grade", 0)),
                          "rank": int(it.get("rank", n)), "judged": True}
                seen += 1
        if not seen:
            logger.warning("rerank(graded): no usable verdicts — falling back")
            return None
        if seen < n:
            logger.warning("rerank(graded:%s): INCOMPLETE — %d of %d passages "
                           "judged, the rest default to grade 0", model, seen, n)
        # RANK MUST BE A PERMUTATION. The prompt says "každé číslo použij jednou",
        # the schema cannot say it, and duplicates used to be resolved by the
        # stability of sorted() — silently, and by pool position. Re-deriving is
        # both honest and traceable.
        _ranks = [o["rank"] for o in out]
        if sorted(_ranks) != list(range(1, n + 1)):
            logger.warning("rerank(graded:%s): rank is not a permutation (%s) — "
                           "re-deriving from grade, then from the returned rank",
                           model, _ranks)
            for pos, o in enumerate(sorted(out, key=lambda o: (-o["grade"], o["rank"])), 1):
                o["rank"] = pos
        logger.info("rerank(graded:%s): %d/%d judged, grades %s",
                    model, seen, n,
                    {g: sum(1 for o in out if o["grade"] == g) for g in (2, 1, 0)})
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("rerank(graded): failed (%s) — keeping fusion order", e)
        return None
