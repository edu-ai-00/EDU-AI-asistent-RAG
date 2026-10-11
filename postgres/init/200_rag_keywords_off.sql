-- Migrace 200: vypnutí kroku rag_keywords
--
-- Změřeno 27. 8. 2026 na 42 otázkách s aktuální konfigurací — kdo dodal PRVNÍ
-- pasáž kontextu, tedy tu, která zakládá odpověď:
--
--   document_lexical   563 chunků do poolu · 103 v kontextu · 25 prvních pasáží
--   rag_vector         159 chunků do poolu ·  35 v kontextu · 11 prvních pasáží
--   rag_keywords       134 chunků do poolu ·  10 v kontextu ·  2 první pasáže
--
-- Klíčová slova tedy natáhnou do poolu srovnatelně jako vektor a dodají odpověď
-- pětkrát méně často. Obě otázky, které dodala („Co se hodnotí u přijímaček?" a
-- „Jak funguje pořadí škol v přihlášce?"), odpovídají i bez nich — u první
-- dokonce lépe.
--
-- Eval 42 × 3 s vypnutou QA po vypnutí: 40/42 odpovědí, tři změněné případy a
-- všechny tři u otázek, které kolísaly už v baseline. Nic se neztratilo, pool je
-- o 134 chunků na 42 dotazů menší a prefiltr i soudce dostávají méně balastu.
--
-- Co se tím NEDĚLÁ: klíčová slova zůstávají na chuncích (document_chunks.keywords)
-- a fúze je pořád umí vážit. Vypíná se jen samostatný vyhledávací krok. Sloučení
-- klíčových slov do bm25_text je jiná změna — mění IDF všem dotazům, takže patří
-- do samostatného měření, ne k tomuhle.

UPDATE edu_ai.settings s
SET config = jsonb_set(s.config, '{pipeline}', (
  SELECT jsonb_agg(CASE WHEN st->>'step' = 'rag_keywords'
    THEN jsonb_set(st, '{enabled}', 'false'::jsonb, true)
    ELSE st END ORDER BY i)
  FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY t(st, i)), true)
WHERE s.is_active;
