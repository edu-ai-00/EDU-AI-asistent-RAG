# last change in code by Martin Safranek (2026-07-24).
"""Facade — re-exports the shared `eduai_normalize` contract for the search service.

The per-word normalization / lemma / stem logic now lives in the shared package
`eduai_normalize` (source in search/eduai_normalize/, pip-installed into BOTH the
search and the Langflow images at build time). The query side (this service) and
the ingestion ChunkLemmatizer component therefore run byte-identical code —
query↔index parity by construction, no hand-synced copies.

Callers keep importing from this module. Canonical output = the parity vectors in
contracts/parity/cs-normalization-vectors.json.
"""

from eduai_normalize import (  # noqa: F401
    build_forms,
    normalize_tokens,
    lemma_tokens,
    stem_tokens,
    STOPWORDS,
    fold,
    tokenize,
    strip_diacritics,
    cz_stem_light,
)
