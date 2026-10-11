-- last change in code by Martin Safranek (2026-08-01).
-- 48_ui_label_ingest_status.sql
-- Labels for the upload page's ingest-progress panel (polls /v1/ingest-status
-- every 10 s and lists each file of one processing_uuid).
--
-- status_note is deliberately part of the copy rather than a code comment: the
-- page must tell its user that ingestion_log carries only INFO rows, so a failed
-- ingest is invisible there and the chunk count is the real "stored" signal.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text) VALUES
  ('upload_test', 'status_heading', 'cs', 'Průběh nahrávání'),
  ('upload_test', 'status_heading', 'en', 'Upload progress'),
  ('upload_test', 'status_heading', 'de', 'Upload-Fortschritt'),
  ('upload_test', 'status_heading', 'es', 'Progreso de la carga'),
  ('upload_test', 'status_file', 'cs', 'Soubor / zdroj'),
  ('upload_test', 'status_file', 'en', 'File / source'),
  ('upload_test', 'status_file', 'de', 'Datei / Quelle'),
  ('upload_test', 'status_file', 'es', 'Archivo / origen'),
  ('upload_test', 'status_last_event', 'cs', 'Poslední událost'),
  ('upload_test', 'status_last_event', 'en', 'Last event'),
  ('upload_test', 'status_last_event', 'de', 'Letztes Ereignis'),
  ('upload_test', 'status_last_event', 'es', 'Último evento'),
  ('upload_test', 'status_elapsed', 'cs', 'Trvání'),
  ('upload_test', 'status_elapsed', 'en', 'Duration'),
  ('upload_test', 'status_elapsed', 'de', 'Dauer'),
  ('upload_test', 'status_elapsed', 'es', 'Duración'),
  ('upload_test', 'status_since_last', 'cs', 'Od poslední události'),
  ('upload_test', 'status_since_last', 'en', 'Since last event'),
  ('upload_test', 'status_since_last', 'de', 'Seit letztem Ereignis'),
  ('upload_test', 'status_since_last', 'es', 'Desde el último evento'),
  ('upload_test', 'status_chunks', 'cs', 'Chunků'),
  ('upload_test', 'status_chunks', 'en', 'Chunks'),
  ('upload_test', 'status_chunks', 'de', 'Chunks'),
  ('upload_test', 'status_chunks', 'es', 'Fragmentos'),
  ('upload_test', 'status_waiting', 'cs', 'Čekám na první událost…'),
  ('upload_test', 'status_waiting', 'en', 'Waiting for the first event…'),
  ('upload_test', 'status_waiting', 'de', 'Warte auf das erste Ereignis…'),
  ('upload_test', 'status_waiting', 'es', 'Esperando el primer evento…'),
  ('upload_test', 'status_polling', 'cs', 'Aktualizuji každých 10 s'),
  ('upload_test', 'status_polling', 'en', 'Refreshing every 10 s'),
  ('upload_test', 'status_polling', 'de', 'Aktualisierung alle 10 s'),
  ('upload_test', 'status_polling', 'es', 'Actualizando cada 10 s'),
  ('upload_test', 'status_stored', 'cs', 'uloženo'),
  ('upload_test', 'status_stored', 'en', 'stored'),
  ('upload_test', 'status_stored', 'de', 'gespeichert'),
  ('upload_test', 'status_stored', 'es', 'guardado'),
  ('upload_test', 'status_stopped', 'cs', 'Sledování ukončeno'),
  ('upload_test', 'status_stopped', 'en', 'Tracking stopped'),
  ('upload_test', 'status_stopped', 'de', 'Verfolgung beendet'),
  ('upload_test', 'status_stopped', 'es', 'Seguimiento finalizado'),
  ('upload_test', 'status_no_events', 'cs', 'Pro tento běh zatím nejsou žádné události.'),
  ('upload_test', 'status_no_events', 'en', 'No events for this run yet.'),
  ('upload_test', 'status_no_events', 'de', 'Für diesen Lauf gibt es noch keine Ereignisse.'),
  ('upload_test', 'status_no_events', 'es', 'Todavía no hay eventos para esta ejecución.'),
  ('upload_test', 'status_note', 'cs', 'Log má jen úroveň INFO, takže selhání se v něm neprojeví — zaseknutí poznáš podle rostoucího času od poslední události. Počet chunků je jediný spolehlivý signál, že se data opravdu uložila.'),
  ('upload_test', 'status_note', 'en', 'The log only ever has level INFO, so a failure does not show up there — a stall is visible as a growing time since the last event. The chunk count is the only reliable signal that the data actually landed.'),
  ('upload_test', 'status_note', 'de', 'Das Log hat ausschließlich die Stufe INFO, ein Fehler erscheint dort also nicht — ein Hängen erkennt man an der wachsenden Zeit seit dem letzten Ereignis. Die Chunk-Anzahl ist das einzige verlässliche Signal, dass die Daten wirklich gespeichert wurden.'),
  ('upload_test', 'status_note', 'es', 'El registro solo tiene nivel INFO, así que un fallo no aparece ahí — un bloqueo se nota por el tiempo creciente desde el último evento. El número de fragmentos es la única señal fiable de que los datos se guardaron.')
ON CONFLICT DO NOTHING;

COMMIT;
