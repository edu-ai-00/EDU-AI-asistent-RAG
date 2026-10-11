-- 133: answer_exclusion_rule číselník (2026-08-10, Martin).
-- Deterministic backstop for the prompt's EARNED-paragraph rule: the LLM
-- occasionally still emits paragraphs whose only content is "this document
-- does not cover the question" (measured on Q2/Q5 even with prompt v8). The
-- meta-sentence vocabulary now lives in its own číselník — the same shape as
-- chunk_exclusion_rule for ingest — because it changes more often than the
-- search config (MD §2.2, same reasoning as qa_stopword). Each row is ONE
-- regex FRAGMENT; search composes the active fragments with '|' into a
-- sentence-level matcher. A paragraph is dropped only when EVERY sentence of
-- its body matches AND at least one substantive paragraph remains.
-- Empty table = backstop off (kill switch); DB error = built-in fallback.

CREATE TABLE IF NOT EXISTS edu_ai.answer_exclusion_rule (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name text NOT NULL UNIQUE,
    lang text NOT NULL DEFAULT 'cs',
    match_regex text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    description text,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

COMMENT ON TABLE edu_ai.answer_exclusion_rule IS
  'Regex fragments (composed with |) marking "document doesn''t answer" meta-sentences; an answer paragraph made ONLY of such sentences is dropped when substantive paragraphs remain. Mirror of chunk_exclusion_rule for the answer side.';

INSERT INTO edu_ai.answer_exclusion_rule (rule_name, lang, match_regex, description) VALUES
  ('netyka',           'cs', 'netýk',              'dokument se otázky netýká / netýkají'),
  ('neuvadi',          'cs', 'neuvád',             'dokument neuvádí / neuvádějí'),
  ('neobsahuje',       'cs', 'neobsahuj',          'dokument neobsahuje / neobsahují'),
  ('nezminuje',        'cs', 'nezmiňuj',           'dokument nezmiňuje / nezmiňují'),
  ('nenalezeno',       'cs', 'nenalez',            'informace nenalezena / nenalezeny'),
  ('nevyplyva',        'cs', 'nevyplýv',           'z dokumentu nevyplývá'),
  ('neposkytuje',      'cs', 'neposkytuj',         'dokument neposkytuje informace'),
  ('nikoli_informace', 'cs', 'nikoli\s+informac',  '„…, nikoli informace o X" deflexe'),
  ('neni_uvedeno',     'cs', 'nen[ií]\s+(?:zde\s+)?uveden|nejsou\s+(?:zde\s+)?uveden|uveden[oay]?\s+(?:zde\s+)?nen[ií]|uvedeny\s+(?:zde\s+)?nejsou',
                             'není (zde) uvedeno / uvedeno není, všechny rody a čísla'),
  ('nestanovi',        'cs', 'nestanov',           'dokument postup/pravidlo nestanoví'),
  ('resi_nikoli',      'cs', '(?:řeš[ií]|uvád[ií]|popisuj|zabýv)[^.!?]*\bnikoli\b',
                             '„část řeší Y, nikoli Z" deflexe'),
  ('nevztahuje',       'cs', 'nevztahuj',          'dokument se na otázku nevztahuje'),
  ('neupravuje',       'cs', 'neupravuj',          'dokument postup neupravuje'),
  ('nezabyva',         'cs', 'nezabýv',            'dokument se tématem nezabývá'),
  ('neresi',           'cs', 'neřeš',              'dokument téma neřeší'),
  ('nepopisuje',       'cs', 'nepopisuj',          'dokument postup nepopisuje'),
  ('tento_dokument_nikoli', 'cs', '(?:tento|tato|toto)\s+(?:dokument|postup|část|sekce|kapitola|text|úryvek|oddíl)[^.!?]*\bnikoli\b',
                             'tento dokument/postup/cast ... nikoli X - podmet je dokument sam, ne obsah')
ON CONFLICT (rule_name) DO NOTHING;
