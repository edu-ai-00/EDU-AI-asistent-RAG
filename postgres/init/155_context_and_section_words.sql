-- last change in code by Martin Safranek (2026-08-20).
--
-- 155 — three word lists move out of the program into the qa_stopword číselník.
--
-- They were the last search-behaviour word lists still living in code, and word
-- lists are the wrong thing to keep there: they grow every time someone notices a
-- missing word, and they differ per language. Adding one meant a programmer, a
-- rebuild and a restart. The stoplist left code for this reason already
-- (scope='index'/'coverage'/'bm25'); these three simply had not followed.
--
--   context_leading   — openers that mark a follow-up question ("A co potom?").
--                       search/app/nodes/resolve_context.py _LEADING
--   context_anaphora  — pro-forms pointing at something said earlier ("Kde to je?")
--                       search/app/nodes/resolve_context.py _ANAPHORA
--   section_word      — section labels stripped from a heading before deciding
--                       whether it can be offered as a clickable question
--                       ("Článek 5" is a label, not a topic).
--                       search/app/nodes/answer.py _SECTION_WORDS
--
-- The code keeps its own copies as a DB-outage fallback only; whenever the
-- číselník returns anything for a scope, the číselník wins entirely (no merge —
-- a merge would make a deliberate DELETE from the číselník do nothing).
--
-- All languages are merged when the lists are read: resolve_context decides
-- BEFORE the query language is known, and a heading carries the DOCUMENT's
-- language, not the asker's.
--
-- Czech and English rows below are EXACTLY what the code held, so behaviour does
-- not change. German and Spanish are inserted INACTIVE: the gap they close was
-- invisible while the lists were in code (a German "Und dann?" was never
-- recognised as a follow-up), but frequency, not translation, decides whether a
-- pronoun belongs here — German "das" and Spanish "la" occur in almost every
-- sentence and would mark every question as context-dependent. Activating them
-- is one UPDATE, after someone who speaks the language reviews them.

BEGIN;

INSERT INTO edu_ai.qa_stopword (term, lang, scope, is_active, note) VALUES
-- ── context_leading ────────────────────────────────────────────────────────
 ('a','cs','context_leading',true,'z kódu _LEADING'),
 ('ale','cs','context_leading',true,'z kódu _LEADING'),
 ('no','cs','context_leading',true,'z kódu _LEADING'),
 ('tak','cs','context_leading',true,'z kódu _LEADING'),
 ('takze','cs','context_leading',true,'z kódu _LEADING (bez diakritiky)'),
 ('takže','cs','context_leading',true,'z kódu _LEADING'),
 ('and','en','context_leading',true,'z kódu _LEADING'),
 ('but','en','context_leading',true,'z kódu _LEADING'),
 ('so','en','context_leading',true,'z kódu _LEADING'),
 ('und','de','context_leading',false,'návrh, čeká na potvrzení'),
 ('aber','de','context_leading',false,'návrh, čeká na potvrzení'),
 ('also','de','context_leading',false,'návrh, čeká na potvrzení'),
 ('y','es','context_leading',false,'návrh, čeká na potvrzení'),
 ('pero','es','context_leading',false,'návrh, čeká na potvrzení'),
 ('entonces','es','context_leading',false,'návrh, čeká na potvrzení'),
-- ── context_anaphora ───────────────────────────────────────────────────────
 ('to','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('ta','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('ten','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('ty','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('toho','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('tomu','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('tim','cs','context_anaphora',true,'z kódu _ANAPHORA (bez diakritiky)'),
 ('tím','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('tam','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('tehdy','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('on','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('ona','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('oni','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('jeho','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('její','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('jejich','cs','context_anaphora',true,'z kódu _ANAPHORA'),
 ('it','en','context_anaphora',true,'z kódu _ANAPHORA'),
 ('that','en','context_anaphora',true,'z kódu _ANAPHORA'),
 ('this','en','context_anaphora',true,'z kódu _ANAPHORA'),
 ('there','en','context_anaphora',true,'z kódu _ANAPHORA'),
 ('they','en','context_anaphora',true,'z kódu _ANAPHORA'),
 ('dies','de','context_anaphora',false,'návrh; POZOR na frekvenci, "das" schválně chybí'),
 ('dieser','de','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('diese','de','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('dort','de','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('dann','de','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('danach','de','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('eso','es','context_anaphora',false,'návrh; POZOR na frekvenci, "la"/"lo" schválně chybí'),
 ('esto','es','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('ese','es','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('esta','es','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('ahí','es','context_anaphora',false,'návrh, čeká na potvrzení'),
 ('allí','es','context_anaphora',false,'návrh, čeká na potvrzení'),
-- ── section_word ───────────────────────────────────────────────────────────
 ('článek','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('čl','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('hlava','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('oddíl','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('odstavec','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('část','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('bod','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('příloha','cs','section_word',true,'z kódu _SECTION_WORDS'),
 ('section','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('article','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('chapter','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('annex','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('paragraph','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('part','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('appendix','en','section_word',true,'z kódu _SECTION_WORDS'),
 ('artikel','de','section_word',false,'návrh, čeká na potvrzení'),
 ('abschnitt','de','section_word',false,'návrh, čeká na potvrzení'),
 ('kapitel','de','section_word',false,'návrh, čeká na potvrzení'),
 ('absatz','de','section_word',false,'návrh, čeká na potvrzení'),
 ('anlage','de','section_word',false,'návrh, čeká na potvrzení'),
 ('anhang','de','section_word',false,'návrh, čeká na potvrzení'),
 ('artículo','es','section_word',false,'návrh, čeká na potvrzení'),
 ('sección','es','section_word',false,'návrh, čeká na potvrzení'),
 ('capítulo','es','section_word',false,'návrh, čeká na potvrzení'),
 ('párrafo','es','section_word',false,'návrh, čeká na potvrzení'),
 ('anexo','es','section_word',false,'návrh, čeká na potvrzení'),
 ('apéndice','es','section_word',false,'návrh, čeká na potvrzení')
ON CONFLICT (lang, term, scope) DO NOTHING;

-- ── invariant: the ACTIVE cs+en sets must equal what the code held ──────────
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM edu_ai.qa_stopword
     WHERE is_active AND scope = 'context_leading' AND lang IN ('cs','en');
    IF n <> 9 THEN
        RAISE EXCEPTION '155: context_leading cs+en active = %, expected 9', n;
    END IF;

    SELECT count(*) INTO n FROM edu_ai.qa_stopword
     WHERE is_active AND scope = 'context_anaphora' AND lang IN ('cs','en');
    IF n <> 21 THEN
        RAISE EXCEPTION '155: context_anaphora cs+en active = %, expected 21', n;
    END IF;

    SELECT count(*) INTO n FROM edu_ai.qa_stopword
     WHERE is_active AND scope = 'section_word' AND lang IN ('cs','en');
    IF n <> 15 THEN
        RAISE EXCEPTION '155: section_word cs+en active = %, expected 15', n;
    END IF;

    -- de/es must NOT be live yet — activating them is a reviewed decision
    SELECT count(*) INTO n FROM edu_ai.qa_stopword
     WHERE is_active AND lang IN ('de','es')
       AND scope IN ('context_leading','context_anaphora','section_word');
    IF n <> 0 THEN
        RAISE EXCEPTION '155: % de/es row(s) are active but must await review', n;
    END IF;

    RAISE NOTICE '155: cs+en live (9/21/15); de/es inserted inactive, awaiting review';
END $$;

COMMIT;
