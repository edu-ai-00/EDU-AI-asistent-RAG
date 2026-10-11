-- last change in code by Jan Drozd (2026-08-24).
--
-- Migrace 181: dopsat, k čemu je qa_entries.asset_refs.
--
-- Sloupec existuje od prvního QA schématu a nikdy do něj nic nezapisovalo:
-- INSERT ani UPDATE v ingestu ho neuváděly, kurátorské CSV pro něj nemělo
-- sloupec a jediný čtenář — export balíčku do mobilu — kopíroval NULLy do
-- telefonu. Když zbyde jen název, čte se "asset_refs" jako provenience. Tou
-- není: provenience je processing_uuid + file_uuid, které migrace 147 kvůli
-- tomu přesunula na začátek tabulky.
--
-- Od 2026-08-24 nese balíčkové CSV sloupec `sources`, ingest ho sem zapisuje
-- a QA zásah ho vrací jako SearchResponse.sources. Žádnou změnu DDL to
-- nepotřebovalo — chyběla jen ta věta níž.
BEGIN;

COMMENT ON COLUMN edu_ai.qa_entries.asset_refs IS
    'Zdroje, které ke své odpovědi uvedl KURÁTOR: [{"title": …, "url": …}], ze sloupce `sources` balíčkového CSV (jeden na řádek, `Titulek | URL`). Při QA zásahu se vrací beze změny jako SearchResponse.sources a kopíruje se do offline balíčku pro mobil. NENÍ to provenience — tou je processing_uuid/file_uuid. Odkazy nic neověřuje: mrtvý zůstane mrtvý a odpověď se doručí tak jako tak.';

COMMIT;
