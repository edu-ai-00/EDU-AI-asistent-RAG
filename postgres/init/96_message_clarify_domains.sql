-- last change in code by Martin Safranek (2026-08-02).
-- 96_message_clarify_domains.sql
-- The clarify suggestion's one sentence, per language.
--
-- /search now returns `suggestions` on every non-guard outcome. One of the kinds
-- is `clarify`: the retrieved documents span two different domains (e.g. the
-- school's own rules AND the entrance-exam rules both answer "jaké potřebuji
-- pomůcky?"), so the client may ask the user which one they meant. The sentence
-- shown for that choice is user-facing text on a deterministic path — no LLM
-- involved — so like the guard and no_answer texts it must exist per language as
-- DATA (system_message číselník), not as a code literal.
--
-- The sentence is deliberately generic: the concrete choices ride separately in
-- the suggestion's `options` (each one a ready-to-send refined query), built from
-- document_type_taxonomy labels in the query's language. Putting the domain names
-- into the sentence would mean per-language TEMPLATES with placeholders — more
-- machinery for no gain, since the options are shown right next to it.
--
-- Idempotent (per-key guard, unlike 38's whole-table one — the table is no longer
-- empty when this runs).

BEGIN;

INSERT INTO edu_ai.system_message (message_key, lang, message, created_by)
SELECT * FROM (VALUES
    ('clarify_domains', 'cs',
     'Tvoje otázka se může týkat více oblastí. Upřesni prosím, kterou myslíš:',
     'init-96'),
    ('clarify_domains', 'en',
     'Your question may concern more than one area. Please specify which one you mean:',
     'init-96'),
    ('clarify_domains', 'de',
     'Deine Frage kann mehrere Bereiche betreffen. Bitte gib an, welchen du meinst:',
     'init-96'),
    ('clarify_domains', 'es',
     'Tu pregunta puede referirse a varias áreas. Indica, por favor, a cuál te refieres:',
     'init-96')
) AS v(message_key, lang, message, created_by)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.system_message
                  WHERE message_key = 'clarify_domains');

COMMIT;
