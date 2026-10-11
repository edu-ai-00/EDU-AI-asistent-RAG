-- last change in code by Martin Safranek (2026-08-01).
-- 51_ui_label_events_column.sql
-- The progress panel lists the WHOLE event sequence of each file, not just where
-- it currently stands, so the column is no longer "last event".
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.ui_label SET label_text = 'Průběh (ingestion_log)'
 WHERE scope = 'upload_test' AND label_key = 'status_last_event' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'Events (ingestion_log)'
 WHERE scope = 'upload_test' AND label_key = 'status_last_event' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'Verlauf (ingestion_log)'
 WHERE scope = 'upload_test' AND label_key = 'status_last_event' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Eventos (ingestion_log)'
 WHERE scope = 'upload_test' AND label_key = 'status_last_event' AND lang = 'es';

COMMIT;
