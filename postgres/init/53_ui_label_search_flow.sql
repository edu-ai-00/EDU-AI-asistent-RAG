-- last change in code by Martin Safranek (2026-08-01).
-- 53_ui_label_search_flow.sql
-- Labels for the search page's cascade trace and its qa_query_log row.
--
-- The trace answers the question `strategies` cannot: which steps ran, which
-- were skipped and WHY, what each returned, and where the walk ended. The log
-- panel next to it shows what was actually persisted — written in a background
-- task after the answer is sent, hence the note about the delay.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text) VALUES
  ('search_test', 'flow_heading', 'cs', 'Průběh vyhledávání'),
  ('search_test', 'flow_heading', 'en', 'Search flow'),
  ('search_test', 'flow_heading', 'de', 'Suchablauf'),
  ('search_test', 'flow_heading', 'es', 'Flujo de búsqueda'),
  ('search_test', 'flow_step', 'cs', 'Krok'),
  ('search_test', 'flow_step', 'en', 'Step'),
  ('search_test', 'flow_step', 'de', 'Schritt'),
  ('search_test', 'flow_step', 'es', 'Paso'),
  ('search_test', 'flow_status', 'cs', 'Stav'),
  ('search_test', 'flow_status', 'en', 'Status'),
  ('search_test', 'flow_status', 'de', 'Status'),
  ('search_test', 'flow_status', 'es', 'Estado'),
  ('search_test', 'flow_detail', 'cs', 'Výsledek kroku'),
  ('search_test', 'flow_detail', 'en', 'Step result'),
  ('search_test', 'flow_detail', 'de', 'Ergebnis des Schritts'),
  ('search_test', 'flow_detail', 'es', 'Resultado del paso'),
  ('search_test', 'flow_ms', 'cs', 'Čas'),
  ('search_test', 'flow_ms', 'en', 'Time'),
  ('search_test', 'flow_ms', 'de', 'Zeit'),
  ('search_test', 'flow_ms', 'es', 'Tiempo'),
  ('search_test', 'flow_ran', 'cs', 'proběhl'),
  ('search_test', 'flow_ran', 'en', 'ran'),
  ('search_test', 'flow_ran', 'de', 'ausgeführt'),
  ('search_test', 'flow_ran', 'es', 'ejecutado'),
  ('search_test', 'flow_skipped', 'cs', 'přeskočen'),
  ('search_test', 'flow_skipped', 'en', 'skipped'),
  ('search_test', 'flow_skipped', 'de', 'übersprungen'),
  ('search_test', 'flow_skipped', 'es', 'omitido'),
  ('search_test', 'flow_disabled', 'cs', 'vypnutý'),
  ('search_test', 'flow_disabled', 'en', 'disabled'),
  ('search_test', 'flow_disabled', 'de', 'deaktiviert'),
  ('search_test', 'flow_disabled', 'es', 'desactivado'),
  ('search_test', 'flow_error', 'cs', 'chyba'),
  ('search_test', 'flow_error', 'en', 'error'),
  ('search_test', 'flow_error', 'de', 'Fehler'),
  ('search_test', 'flow_error', 'es', 'error'),
  ('search_test', 'flow_stopped_here', 'cs', '← zde kaskáda skončila'),
  ('search_test', 'flow_stopped_here', 'en', '← the cascade ended here'),
  ('search_test', 'flow_stopped_here', 'de', '← hier endete die Kaskade'),
  ('search_test', 'flow_stopped_here', 'es', '← aquí terminó la cascada'),
  ('search_test', 'flow_pool', 'cs', 'chunků v poolu'),
  ('search_test', 'flow_pool', 'en', 'chunks in pool'),
  ('search_test', 'flow_pool', 'de', 'Chunks im Pool'),
  ('search_test', 'flow_pool', 'es', 'fragmentos en el conjunto'),
  ('search_test', 'log_heading', 'cs', 'Záznam v qa_query_log'),
  ('search_test', 'log_heading', 'en', 'Row in qa_query_log'),
  ('search_test', 'log_heading', 'de', 'Eintrag in qa_query_log'),
  ('search_test', 'log_heading', 'es', 'Registro en qa_query_log'),
  ('search_test', 'log_waiting', 'cs', 'Záznam se zapisuje po odeslání odpovědi…'),
  ('search_test', 'log_waiting', 'en', 'The row is written after the answer is sent…'),
  ('search_test', 'log_waiting', 'de', 'Der Eintrag wird nach dem Senden der Antwort geschrieben…'),
  ('search_test', 'log_waiting', 'es', 'El registro se escribe tras enviar la respuesta…'),
  ('search_test', 'log_note', 'cs', 'Log se zapisuje na pozadí až po odeslání odpovědi, takže se objeví se zpožděním. Kaskáda výše je z běhu samotného, tenhle řádek je to, co se uložilo do databáze.'),
  ('search_test', 'log_note', 'en', 'The log is written in the background after the answer is sent, so it appears with a delay. The cascade above comes from the run itself; this row is what was persisted.'),
  ('search_test', 'log_note', 'de', 'Das Log wird im Hintergrund nach dem Senden der Antwort geschrieben und erscheint daher verzögert. Die Kaskade oben stammt aus dem Lauf selbst, diese Zeile ist das, was gespeichert wurde.'),
  ('search_test', 'log_note', 'es', 'El registro se escribe en segundo plano tras enviar la respuesta, por lo que aparece con retraso. La cascada de arriba viene de la ejecución; esta fila es lo que se guardó.')
ON CONFLICT DO NOTHING;

COMMIT;
