-- last change in code by Martin Safranek (2026-08-01).
-- 50_ui_label_poll_interval.sql
-- Poll interval of the upload page's progress panel goes 10 s -> 5 s.
--
-- The number moves OUT of the copy into a {s} placeholder the page fills from
-- its own POLL_MS. Baking "10" into four translations meant every interval
-- change was a database migration in four languages — and a chance for one of
-- them to keep saying 10 while the page polls at 5.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.ui_label SET label_text = 'Aktualizuji každých {s} s'
 WHERE scope = 'upload_test' AND label_key = 'status_polling' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'Refreshing every {s} s'
 WHERE scope = 'upload_test' AND label_key = 'status_polling' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'Aktualisierung alle {s} s'
 WHERE scope = 'upload_test' AND label_key = 'status_polling' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'Actualizando cada {s} s'
 WHERE scope = 'upload_test' AND label_key = 'status_polling' AND lang = 'es';

COMMIT;
