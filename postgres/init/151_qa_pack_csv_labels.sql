-- 151: the QA pack format switched from JSON to CSV — update the two UI
-- labels that still name the old one (2026-08-19).
--
-- The QA ingest reader was rewritten to parse CSV instead of JSON (see
-- langflow_flows/ingestion/EDU_AI_QA.json, "QA: Read CSV") — a curator who
-- does not read JSON can open a CSV in Excel/Sheets. Routing was already
-- keyed off nothing but the file extension (api/app/main.py
-- detect_source_type), so the switch touches no schema and no other table.
-- These two labels are the only user-visible text that named the old format.

UPDATE edu_ai.ui_label SET label_text = 'Soubor (PDF / DOCX / PPTX / XLSX / QA CSV)'
 WHERE scope = 'upload_test' AND label_key = 'label_file' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'File (PDF / DOCX / PPTX / XLSX / QA CSV)'
 WHERE scope = 'upload_test' AND label_key = 'label_file' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'Datei (PDF / DOCX / PPTX / XLSX / QA CSV)'
 WHERE scope = 'upload_test' AND label_key = 'label_file' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Archivo (PDF / DOCX / PPTX / XLSX / QA CSV)'
 WHERE scope = 'upload_test' AND label_key = 'label_file' AND lang = 'es';

UPDATE edu_ai.ui_label SET label_text = '— platí jen pro CSV s otázkami a odpověďmi'
 WHERE scope = 'upload_test' AND label_key = 'qa_hint' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = '— applies only to a CSV of questions and answers'
 WHERE scope = 'upload_test' AND label_key = 'qa_hint' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = '— gilt nur für CSV mit Fragen und Antworten'
 WHERE scope = 'upload_test' AND label_key = 'qa_hint' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = '— solo se aplica al CSV con preguntas y respuestas'
 WHERE scope = 'upload_test' AND label_key = 'qa_hint' AND lang = 'es';

DO $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n FROM edu_ai.ui_label
   WHERE scope = 'upload_test' AND label_key IN ('label_file', 'qa_hint')
     AND label_text ILIKE '%JSON%';
  IF n <> 0 THEN
    RAISE EXCEPTION 'mig 151: % ui_label row(s) still mention JSON after the update', n;
  END IF;
END $$;
