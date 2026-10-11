-- last change in code by Martin Safranek (2026-07-28).
-- 38_system_messages.sql
-- System messages (guard + no_answer) move from code constants to a číselník,
-- per language (agreed 2026-07-28): the deterministic paths (guard, no_answer)
-- never call an LLM, so their texts must exist per language as DATA. Read per
-- request (hot-editable, same pattern as prompt_registry); the config.py
-- constants remain only as the code fallback when the DB is unreachable.
-- self_harm EN keeps Linka bezpečí 116 111 (the number works EU-wide as the
-- child helpline) + 112 — no foreign helplines invented. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.system_message (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_key text NOT NULL,   -- guard_profanity | guard_self_harm | guard_unusable | no_answer
    lang        text NOT NULL,   -- code_list 'language' code
    message     text NOT NULL,
    is_active   boolean NOT NULL DEFAULT true,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    notes       text
);

CREATE UNIQUE INDEX IF NOT EXISTS system_message_active_key_lang
    ON edu_ai.system_message (message_key, lang) WHERE is_active;

INSERT INTO edu_ai.system_message (message_key, lang, message, created_by)
SELECT * FROM (VALUES
    ('guard_profanity', 'cs',
     'Zkus se prosím zeptat slušně a bez neslušných slov. Rád ti pomůžu s otázkami k přijímačkám. 🙂',
     'migration-38'),
    ('guard_profanity', 'en',
     'Please ask politely, without rude words. I''ll be happy to help you with your questions. 🙂',
     'migration-38'),
    ('guard_self_harm', 'cs',
     'Je mi líto, že se teď necítíš dobře. Nejsi na to sám/sama a je v pořádku poprosit o pomoc. Zavolej prosím na **Linku bezpečí 116 111** (zdarma, nonstop) nebo si promluv s někým dospělým, komu důvěřuješ. Když jde o život, volej 112.',
     'migration-38'),
    ('guard_self_harm', 'en',
     'I''m sorry you''re not feeling well right now. You are not alone, and it''s okay to ask for help. Please call **Linka bezpečí 116 111** (free, 24/7) or talk to an adult you trust. If a life is in danger, call 112.',
     'migration-38'),
    ('guard_unusable', 'cs',
     'Nerozuměl jsem tvé otázce. Zkus ji prosím napsat trochu jinak.',
     'migration-38'),
    ('guard_unusable', 'en',
     'I didn''t understand your question. Please try phrasing it differently.',
     'migration-38'),
    ('no_answer', 'cs',
     'Na tuto otázku zatím nemám odpověď. Zkus ji prosím přeformulovat.',
     'migration-38'),
    ('no_answer', 'en',
     'I don''t have an answer to this question yet. Please try rephrasing it.',
     'migration-38')
) AS v(message_key, lang, message, created_by)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.system_message);

COMMIT;
