-- last change in code by Codex (2026-09-04).
--
-- Prompt v15: stejne tema jeste neni odpoved na dotazovany aspekt.
--
-- Produkcni nalez "Co obsahuje skolni vzdelavaci program?" mel pri shodnem
-- retrieval poolu odpoved i NO_ANSWER. Kontext obsahoval pouze jine udaje o SVP
-- (zamereni programu, pouziti pri prijimacim rizeni), zadnou cast jeho obsahu.
-- V14 si odporovala: nejprve prikazovala NO_ANSWER, kdyz odpoved v kontextu neni,
-- ale pozdeji omezila tento protokol jen na pripad, kdy zadna pasaz nemluvi o
-- danem predmetu. Model proto mohl libovolny fakt o SVP povazovat za castecnou
-- odpoved.
--
-- Jde jen o hot-editovatelny prompt. Retrieval, reranker, model i settings se
-- nemeni. Navrat je prepnuti is_active z verze 15 zpet na 14; verze se nemazou.

BEGIN;

UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'search_answer_generation'
   AND version = 14
   AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key, 15,
       replace(prompt_text,
$old$   No apology, no advice, no explanation, no other text. If only PART of the
   question is answerable, answer that part normally and do not mention what
   is missing. When NO document contains the answer, the protocol above is the
   ENTIRE output — never write per-document paragraphs saying a document does
   not contain the information.
5. SUBJECT CHECK (SILENT — apply it, but never mention it or its outcome
   in the output): before answering, identify the subject the question asks
   about, and use ONLY passages whose body or section heading shows they
   speak about that subject. When the subject is a SPECIFIC NAMED thing (an
   event, a document, a programme), a similarly themed but DIFFERENTLY
   NAMED item is NOT the subject — a seminar "Učíme o přírodě" is not
   "škola v přírodě" — even when it is the ONLY passage provided; an
   on-topic-looking context is not evidence that the answer exists. A
   passage about the right subject that answers only PART of the question
   (a general rule covering the asked-about case) is still an answer —
   answer from it normally. Only when NO passage speaks about the subject
   at all does rule 4 apply, and then its protocol is the ENTIRE output —
   never an answer with NO_ANSWER appended to it.$old$,
$new$   No apology, no advice, no explanation, no other text. If only PART of the
   question is answerable, answer that part only when it satisfies the exact-
   aspect check below; a different fact about the same subject is not a partial
   answer. When NO document contains the answer, the protocol above is the
   ENTIRE output — never write per-document paragraphs saying a document does
   not contain the information.
5. EXACT SUBJECT AND ASPECT CHECK (SILENT — apply it, but never mention it
   or its outcome in the output): before answering, identify both (a) the
   subject of the question and (b) the exact requested aspect — what, when,
   where, who, how, under what conditions, or which items it contains. Use a
   passage only when it addresses BOTH. Merely mentioning the right subject is
   not an answer, and a different fact about it is not a partial answer.
   When the question asks what something contains, consists of, includes or
   lists, the context must explicitly name at least one requested component.
   Its purpose, use, property, variant or specialisation is not a component.
   Example: for "What does the school educational programme contain?", the
   passage "One field may have several specialisations of the school
   educational programme" does NOT answer and requires NO_ANSWER.
   When the subject is a SPECIFIC NAMED thing (an event, a document, a
   programme), a similarly themed but DIFFERENTLY NAMED item is NOT the
   subject — a seminar "Učíme o přírodě" is not "škola v přírodě" — even
   when it is the ONLY passage provided. An on-topic-looking context is not
   evidence that the answer exists. A passage about the right subject that
   truly answers one explicit part of the requested aspect (for example one
   requested list item or a general rule covering the asked-about case) is a
   partial answer and may be used. If no passage answers the requested aspect,
   rule 4 applies even when passages mention the subject; its protocol is the
   ENTIRE output — never append NO_ANSWER to an answer.$new$),
       true, component,
       'v15: odpoved musi pokryt predmet i presne dotazovany aspekt',
       placeholders, model_hint, lang, 'codex',
       'Minimalni vratny prompt-only experiment pro nalez SVP; v14 zustava pro rollback.'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation'
   AND version = 14;

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n
    FROM edu_ai.prompt_registry
   WHERE prompt_key = 'search_answer_generation'
     AND version = 15
     AND is_active
     AND prompt_text LIKE '%EXACT SUBJECT AND ASPECT CHECK%'
     AND prompt_text LIKE '%different fact about the same subject is not a partial%'
     AND prompt_text !~ 'Only when NO passage speaks about the subject[[:space:]]+at all';
  IF n <> 1 THEN
    RAISE EXCEPTION '212: kotva v promptu v14 nenalezena nebo prompt v15 neni uplny';
  END IF;
END $$;

COMMIT;
