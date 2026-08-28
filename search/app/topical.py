# last change in code by Martin Safranek (2026-08-22).
"""Topical labels for a retrieved chunk — the SHARED producer.

Two fields decide topical precision downstream, both read in `answer`:

  * `heading_match`      — a discriminative query concept sits in the chunk's own
                           HEADING (the heading tier compares on it),
  * `concepts_covered`   — which query concepts the chunk covers, each with its
                           corpus df share (the focus gate anchors the question's
                           rare ASPECT on them).

They used to be computed inline in document_lexical's main loop and NOWHERE else.
Every other retriever therefore delivered unlabelled chunks, and because a missing
label raises nothing, the gates in `answer` simply went quiet: on "můžeme ve škole
fotit?" the whole pool came from the trgm fallback and rag_vector, `focus` was []
and no tangential document could be refused. A label that three retrievers must
produce identically belongs in one function — hence this module.

`ctx` is built once per request by document_lexical (it already computes the
concept sets, the df map and the heading df shares) and travels in the state, so
the other retrievers label their chunks without repeating the queries.
"""

import re
import unicodedata

_TOK = re.compile(r"\S+")


def tokens(text: str) -> set:
    """Whitespace tokens of an already-reduced (norm/lemma/stem) text."""
    return {t for t in (text or "").split() if t}


def fold_token_set(terms) -> set:
    """Diacritics-folded ascii word tokens — the form df keys and stoplists use."""
    out: set = set()
    for t in terms or ():
        f = "".join(c for c in unicodedata.normalize("NFD", str(t).lower())
                    if unicodedata.category(c) != "Mn")
        for w in "".join(ch if ch.isalnum() else " " for ch in f).split():
            out.add(w)
    return out


def body_sql(form, alias: str) -> str:
    """Reduced body + heading text of a chunk, for coverage and labelling."""
    cols = [f"{alias}.{_BODY[f]}" for f in form if f in _BODY]
    for f in form:
        cols += [f"{alias}.{c}" for c in _HEAD.get(f, ())]
    return "concat_ws(' ', " + ", ".join(cols) + ")" if cols else "''"


def heading_sql(form, alias: str) -> str:
    """Reduced heading columns only."""
    cols = []
    for f in form:
        cols += [f"{alias}.{c}" for c in _HEAD.get(f, ())]
    return "concat_ws(' ', " + ", ".join(cols) + ")" if cols else "''"


_BODY = {"norm": "chunk_norm_text", "lemma": "chunk_lemma_text",
         "stem": "chunk_stem_text"}
_HEAD = {"norm": ("section_heading_norm", "subsection_heading_norm"),
         "lemma": ("section_heading_lemma", "subsection_heading_lemma"),
         "stem": ("section_heading_stem", "subsection_heading_stem")}


def context(*, form, concept_sets, max_concepts, df, ntot, generic,
            df_share, heading_pass=True, heading_numeric_pass=False,
            heading_max_df_share=0.25) -> dict:
    """Everything `label()` needs, computed once per request."""
    return {"form": list(form), "concept_sets": concept_sets,
            "max_concepts": max_concepts, "df": df, "ntot": ntot,
            "generic": set(generic or ()), "df_share": df_share,
            "heading_pass": heading_pass,
            "heading_numeric_pass": heading_numeric_pass,
            "heading_max_df_share": heading_max_df_share}


def label(body_reduced: str, heading_reduced: str, ctx: dict | None):
    """(heading_match, concepts_covered) for one chunk. No ctx → (None, [])."""
    if not ctx:
        return None, []
    concept_sets = ctx.get("concept_sets") or []
    cap = int(ctx.get("max_concepts") or 8)
    df_share = ctx.get("df_share") or []
    generic = ctx.get("generic") or set()

    heading_match = None
    if ctx.get("heading_pass", True):
        htok = tokens(heading_reduced)
        for i, cs in enumerate(concept_sets[:cap]):
            # a purely numeric concept is an ordinal, never a topic
            if not ctx.get("heading_numeric_pass") and all(
                    t.strip(".").isdigit() for t in cs if t):
                continue
            # a concept made only of corpus-generic tokens ("škola") is not a
            # discriminator — it must not grant a heading match
            if generic and fold_token_set(cs) <= generic:
                continue
            if (i < len(df_share)
                    and df_share[i] <= ctx.get("heading_max_df_share", 0.25)
                    and (cs & htok)):
                heading_match = sorted(cs)[0]
                break

    covered = []
    ntot = ctx.get("ntot") or 0
    if ntot:
        df = ctx.get("df") or {}
        ctoks = tokens(body_reduced) | tokens(heading_reduced)
        for cs in concept_sets[:cap]:
            hit = cs & ctoks
            if hit:
                share = min((df.get(t, ntot) / ntot) for t in fold_token_set(cs)) \
                    if fold_token_set(cs) else 1.0
                covered.append([sorted(hit)[0], round(share, 4)])
    return heading_match, covered
