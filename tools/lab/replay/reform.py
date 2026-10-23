"""Recompute the stored norm / lemma / stem forms with the image's normalizer.

What a re-ingest does to the search forms, without re-ingesting: QA variants
from their phrasing, document chunks from their text and headings. Embeddings,
texts and everything else stay. Run INSIDE the search image whose
eduai_normalize version is to be simulated, against a COPY of the data — it
rewrites rows. The tsvector / bm25 columns are generated from these and follow.

The language of a QA variant is its pack's (qa_pack_manifest.lang); a chunk's is
langdetect_result, Czech when missing. The configuration is the active
setting's, as the Langflow ingest reads it.

  python reform.py
"""

from __future__ import annotations

import sys
import time

import eduai_normalize as en
from eduai_normalize import build_forms
from app.db import get_cursor
from app.settings import get_active_setting


def _j(tokens):
    return " ".join(tokens)


def main() -> int:
    cfg = get_active_setting(None)["config"]
    t0 = time.time()
    with get_cursor() as cur:
        # one row per variant: a pack has several manifest versions
        cur.execute("SELECT DISTINCT ON (v.variant_id) v.variant_id, v.phrasing,"
                    "       coalesce(m.lang, 'cs')"
                    "  FROM edu_ai.qa_variants v JOIN edu_ai.qa_entries e USING (entry_id)"
                    "  LEFT JOIN edu_ai.qa_pack_manifest m ON m.pack_id = e.pack_id"
                    "   AND m.legal_entity_code IS NOT DISTINCT FROM e.legal_entity_code"
                    " ORDER BY v.variant_id, m.is_active DESC")
        rows = cur.fetchall()
        n = 0
        for vid, phrasing, lang in rows:
            f = build_forms(phrasing or "", cfg, lang)
            cur.execute("UPDATE edu_ai.qa_variants SET norm_text=%s, lemma_text=%s, stem_text=%s"
                        " WHERE variant_id=%s",
                        (_j(f["norm"]), _j(f["lemma"]), _j(f["stem"]), vid))
            n += 1
        print(f"[reform] qa_variants: {n}", file=sys.stderr)

        cur.execute("SELECT id, chunk_text, section_heading, subsection_heading,"
                    "       coalesce(nullif(langdetect_result, ''), 'cs')"
                    "  FROM edu_ai.document_chunks")
        rows = cur.fetchall()
        for cid, text, sec, sub, lang in rows:
            f, fs, fb = (build_forms(x or "", cfg, lang) for x in (text, sec, sub))
            cur.execute(
                "UPDATE edu_ai.document_chunks SET chunk_norm_text=%s, chunk_lemma_text=%s,"
                " chunk_stem_text=%s, section_heading_norm=%s, section_heading_lemma=%s,"
                " section_heading_stem=%s, subsection_heading_norm=%s,"
                " subsection_heading_lemma=%s, subsection_heading_stem=%s WHERE id=%s",
                (_j(f["norm"]), _j(f["lemma"]), _j(f["stem"]),
                 _j(fs["norm"]), _j(fs["lemma"]), _j(fs["stem"]),
                 _j(fb["norm"]), _j(fb["lemma"]), _j(fb["stem"]), cid))
        print(f"[reform] document_chunks: {len(rows)}", file=sys.stderr)
    print(f"[reform] eduai_normalize {en.__version__}, {time.time() - t0:.0f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
