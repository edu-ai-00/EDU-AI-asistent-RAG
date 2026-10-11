-- last change in code by Martin Safranek (2026-08-01).
-- 49_ui_label_product_name.sql
-- Put the product name in the test pages' titles instead of the bare "edu_ai".
--
-- The name as given — "EDU-AI asistent RAG module" — mixes two languages:
-- `asistent` is Czech, `module` is English (Czech is `modul`). Since the title is
-- localized copy, each language gets it written correctly in that language rather
-- than the mixed form being frozen into all four:
--
--   cs  EDU-AI asistent RAG modul
--   en  EDU-AI assistant RAG module
--   de  EDU-AI Assistent RAG-Modul
--   es  Módulo RAG del asistente EDU-AI     (natural Spanish word order)
--
-- Only "EDU-AI" itself is treated as a fixed brand token and never translated.
--
-- Idempotent (plain UPDATEs to fixed values); transactional.

BEGIN;

UPDATE edu_ai.ui_label SET label_text = 'EDU-AI asistent RAG modul — testovací vyhledávání'
 WHERE scope = 'search_test' AND label_key = 'title' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'EDU-AI assistant RAG module — search test'
 WHERE scope = 'search_test' AND label_key = 'title' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'EDU-AI Assistent RAG-Modul — Suchtest'
 WHERE scope = 'search_test' AND label_key = 'title' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Módulo RAG del asistente EDU-AI — prueba de búsqueda'
 WHERE scope = 'search_test' AND label_key = 'title' AND lang = 'es';

UPDATE edu_ai.ui_label SET label_text = 'EDU-AI asistent RAG modul — testovací nahrávání'
 WHERE scope = 'upload_test' AND label_key = 'title' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'EDU-AI assistant RAG module — upload test'
 WHERE scope = 'upload_test' AND label_key = 'title' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'EDU-AI Assistent RAG-Modul — Upload-Test'
 WHERE scope = 'upload_test' AND label_key = 'title' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Módulo RAG del asistente EDU-AI — prueba de carga'
 WHERE scope = 'upload_test' AND label_key = 'title' AND lang = 'es';

COMMIT;
