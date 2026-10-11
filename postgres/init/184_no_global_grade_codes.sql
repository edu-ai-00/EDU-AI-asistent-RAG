-- last change in code by Martin Safranek (2026-08-26).
--
-- Migrace 184: ročníky bez entity se deaktivují — migrace 108 je zrušila a tyhle
-- jí unikly.
--
-- V code_list('school_grade') leží jedenáct řádků s legal_entity_code IS NULL:
-- kódy 1–9 (id 52, 49, 48, 47, 50, 45, 44, 46, 51), 9999 (id 227) a UNKNOWN
-- (id 24). V DATECH je nepoužívá nikdo — ověřeno 26. 8. 2026 přes všechna id ve
-- document_chunks, qa_entries a qa_pack_manifest: každé použité id patří některé
-- jednotce. Jsou to zbytky z doby před migrací 108 („no global codebooks").
--
-- Rozsah této migrace je ÚKLID, ne oprava — a je fér to napsat. Nejdřív jsem ji
-- odůvodnil rizikem, že budoucí ingesce zapíše globální id a chunk se pak
-- s dotazem šesťáka nepotká (porovnává se ID, ne kód). Ověřeno 26. 8.: to riziko
-- neexistuje. Na document_chunks je od migrace 107 trigger chunk_codes_valid,
-- který každé zapisované ročníkové id ověří proti resolved_code_list dané entity
-- a jinak zapíše výjimku. Databáze si to tedy hlídá sama.
--
-- Co tedy tato migrace přináší: /v1/code-lists tyhle řádky přestane nabízet
-- (aktivní řádky bez entity by se do rozlišeného seznamu dostat mohly) a čitelnost
-- číselníku. Nic víc.
--
-- Zobrazování: `tools/search_test.html` bere ročníky z /v1/code-lists pro danou
-- entitu, takže po deaktivaci nabídne přesně to, co má. (Chat ve web_chat je
-- generuje natvrdo jako 1–9 a to je samostatná vada na straně webu — žák SOŠ tam
-- svůj ročník 10–13 vybrat nemůže.)
--
-- Bezpečnostní pojistka: kdyby data ta id přece používala, migrace SELHÁVÁ
-- a nic nedeaktivuje.
BEGIN;

DO $$
DECLARE
    _used bigint;
BEGIN
    SELECT count(*) INTO _used
      FROM (
        SELECT unnest(school_grade_ids) AS id FROM edu_ai.document_chunks
        UNION ALL SELECT unnest(school_grade_ids) FROM edu_ai.qa_entries
        UNION ALL SELECT unnest(school_grade_ids) FROM edu_ai.qa_variants
        UNION ALL SELECT unnest(school_grade_ids) FROM edu_ai.qa_embedding
        UNION ALL SELECT unnest(school_grade_ids) FROM edu_ai.qa_pack_manifest
      ) u
      JOIN edu_ai.code_list c ON c.id = u.id
     WHERE c.list_code = 'school_grade' AND c.legal_entity_code IS NULL;

    IF _used > 0 THEN
        RAISE EXCEPTION 'migrace 184: % odkazů na ročník bez entity — nejdřív je '
                        'přemapovat na kód jednotky, teprve pak deaktivovat', _used;
    END IF;
END $$;

UPDATE edu_ai.code_list
   SET is_active = false,
       notes = coalesce(notes || ' | ', '')
               || 'deaktivováno migrací 184: ročník patří jednotce, ne platformě'
 WHERE list_code = 'school_grade'
   AND legal_entity_code IS NULL
   AND is_active;

COMMIT;
