-- last change in code by Martin Safranek (2026-08-01).
-- 52_ui_label_since_prev.sql
-- The progress panel now has ONE ROW PER EVENT, so this column is the gap from
-- the PREVIOUS event of that file, not "since the last event of the run".
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.ui_label SET label_text = 'Od předchozí'
 WHERE scope = 'upload_test' AND label_key = 'status_since_last' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'Since previous'
 WHERE scope = 'upload_test' AND label_key = 'status_since_last' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'Seit vorherigem'
 WHERE scope = 'upload_test' AND label_key = 'status_since_last' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Desde el anterior'
 WHERE scope = 'upload_test' AND label_key = 'status_since_last' AND lang = 'es';

COMMIT;
