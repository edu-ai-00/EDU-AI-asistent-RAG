-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 175: titulek odstavce nesmí být opsaný nadpis sekce — a u jednoho
-- odstavce nemá být vůbec.
--
-- Naměřeno na "Co se hodnotí u přijímaček?" (question_answer_log 5388, 5395,
-- 5399): odpověď o kritériích a procentech začala tučným "Kdy se jednotná
-- přijímací zkouška bude konat:", což je nadpis sekce Cermatu opsaný slovo za
-- slovem — čtenář z titulku čeká termíny a dostane váhy. U 5392 totéž s
-- "Testová zadání k procvičování:" a "METODICKÝ MATERIÁL:".
--
-- v10 to přitom už zakazuje ("A section heading is information about the
-- content, not text to reuse"). Zákaz sám nestačil, protože nadpis má model
-- přímo v kontextu na řádku "| section: …" a je to nejlevnější dostupný text.
-- v11 proto přidává (a) situaci, kdy label nemá existovat vůbec, protože
-- pravidlo 1 mluví o JEDNOM odstavci, ale model labeluje i ten, a (b) test,
-- podle kterého se špatný label pozná: neodpovídá na "kdy/kde/pro koho to
-- platí", ale na "jak se ta sekce jmenuje".
--
-- Kód label jen odstřihává v _split_lead před atribucí odstavce ke zdroji a bez
-- něj funguje dál (vrací None, text), takže je to čistě promptová změna.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 10 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 11,
       replace(prompt_text,
'1. If the context answers the question from ONE topic, write one paragraph.',
'1. If the context answers the question from ONE topic, write one paragraph —
   with NO lead-in label at all. Open it with the answer itself. A label belongs
   ONLY to an answer that has two or more paragraphs, where it is what tells them
   apart; on a single paragraph there is nothing to tell apart and a label only
   promises a topic the paragraph does not deliver.'),
       true, component,
       'v11: paragraph label may not repeat a section heading; single paragraph has none',
       model_hint, lang, 'martin',
       'Migrace 175 — measured on log 5388/5392/5395/5399'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 10;

-- (b) how to recognise a wrong label — appended to the same rule 2
UPDATE edu_ai.prompt_registry
   SET prompt_text = replace(prompt_text,
'   called. A section heading is information about the content, not text to reuse,
   and a document''s section numbering or internal structure never belongs in it.',
'   called. A section heading is information about the content, not text to reuse,
   and a document''s section numbering or internal structure never belongs in it.
   TEST the label before you write it: it must answer "when, where or for whom
   does this apply to the reader", never "what is this section called". A label
   that repeats, translates, shortens or paraphrases the "| section: …" line is
   WRONG even when it reads well — and a label that announces something the
   paragraph does not deliver (a section named after dates above a paragraph
   about criteria) is worse than no label. If you cannot name the situation in
   your own words, write no label and start with the answer.')
 WHERE prompt_key = 'search_answer_generation' AND version = 11;

COMMIT;
