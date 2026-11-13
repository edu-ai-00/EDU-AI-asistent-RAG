-- 217: synonym groups for words people use for the same thing (2026-10-21).
--
-- Words people use for the same thing were not connected: "syn" matched a
-- curated question, "dcera" did not. Measured with tools/lab/variants on the
-- JPZ set (110 questions): "Kde dcera píše JPZ?" reached "Kde uchazeč píše
-- JPZ?" only as a suggestion (coverage 0.44 < t_answer 0.60), the same for
-- syn / dítě / student; "Jak dlouho trvá test z čj?" did not reach the Czech
-- language entry at all.
--
-- All groups are SINGLE-WORD on purpose (qa_common.expand_query): a group with
-- a multiword term becomes a "phrase group" and is matched as one extra
-- concept instead of bridging the query word, which is not what these need.
-- That is also why "přijímací zkouška" is not added to syn_prijimacky: the
-- group already holds "přijímací" and the variant measurement found
-- přijímačky ↔ přijímací zkouška at 100 %.
--
-- Reduced forms computed with eduai_normalize 3.1.0 build_forms and the active
-- normalization config (2026-10-21), the same code as the QA ingest and the
-- query side. 3.1.0 keeps a negation in the lemma, see syn_znamka_5.
--
-- Not included: trojka ↔ dobrý ("dobrý" is far too common a word to stand for
-- a grade).
--
-- Idempotent: each group is replaced. No transaction control (runner, from 217).

DELETE FROM edu_ai.qa_synonym
 WHERE group_id IN ('syn_dite_zak', 'syn_stravovani', 'syn_ppp', 'syn_matematika',
                    'syn_cestina', 'syn_ucitel', 'syn_znamka_1', 'syn_znamka_2',
                    'syn_znamka_4', 'syn_znamka_5');

INSERT INTO edu_ai.qa_synonym
       (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
VALUES
 -- the person a question is about: a parent writes "syn/dcera", a curated
 -- answer says "uchazeč" (JPZ) or "žák"
 ('syn_dite_zak','entity','žák',       'zak',       'zak',       'zak',       true, 'cs','init',true),
 ('syn_dite_zak','entity','žákyně',    'zakyne',    'zakyne',    'zakyn',     false,'cs','init',true),
 ('syn_dite_zak','entity','student',   'student',   'student',   'student',   false,'cs','init',true),
 ('syn_dite_zak','entity','studentka', 'studentka', 'studentka', 'studentk',  false,'cs','init',true),
 ('syn_dite_zak','entity','uchazeč',   'uchazec',   'uchazec',   'uchazec',   false,'cs','init',true),
 ('syn_dite_zak','entity','uchazečka', 'uchazecka', 'uchazec',   'uchazeck',  false,'cs','init',true),
 ('syn_dite_zak','entity','syn',       'syn',       'syn',       'syn',       false,'cs','init',true),
 ('syn_dite_zak','entity','dcera',     'dcera',     'dcera',     'dcer',      false,'cs','init',true),
 ('syn_dite_zak','entity','dítě',      'dite',      'dite',      'dit',       false,'cs','init',true),
 ('syn_dite_zak','entity','děti',      'deti',      'dite',      'det',       false,'cs','init',true),

 ('syn_stravovani','entity','stravování','stravovani','stravovani','stravovan', true, 'cs','init',true),
 ('syn_stravovani','entity','strava',    'strava',    'strava',    'strav',     false,'cs','init',true),
 ('syn_stravovani','entity','oběd',      'obed',      'obed',      'obed',      false,'cs','init',true),
 ('syn_stravovani','entity','obědy',     'obedy',     'obed',      'obed',      false,'cs','init',true),
 ('syn_stravovani','entity','jídelna',   'jidelna',   'jidelna',   'jideln',    false,'cs','init',true),

 -- pedagogicko-psychologická poradna; ŠPZ (the umbrella term) stays its own
 -- group syn_spz, PPP is only one kind of it
 ('syn_ppp','entity','PPP',     'ppp',     'ppp',     'ppp',    true, 'cs','init',true),
 ('syn_ppp','entity','poradna', 'poradna', 'poradna', 'poradn', false,'cs','init',true),

 ('syn_matematika','entity','matematika','matematika','matematika','matematik', true, 'cs','init',true),
 ('syn_matematika','entity','matika',    'matika',    'matika',    'matik',     false,'cs','init',true),

 ('syn_cestina','entity','čeština','cestina','cestina','cesk', true, 'cs','init',true),
 ('syn_cestina','entity','čj',     'cj',     'cj',     'cj',   false,'cs','init',true),

 -- feminine forms are separate lemmas (učitelka ≠ učitel)
 ('syn_ucitel','entity','učitel',    'ucitel',    'ucitel',    'ucitel',   true, 'cs','init',true),
 ('syn_ucitel','entity','učitelka',  'ucitelka',  'ucitelka',  'ucitelk',  false,'cs','init',true),
 ('syn_ucitel','entity','vyučující', 'vyucujici', 'vyucujist', 'vyucujic', false,'cs','init',true),
 ('syn_ucitel','entity','pedagog',   'pedagog',   'pedagog',   'pedagog',  false,'cs','init',true),

 ('syn_znamka_1','entity','jednička',   'jednicka',   'jednicka',   'jednick',    true, 'cs','init',true),
 ('syn_znamka_1','entity','výborný',    'vyborny',    'vyborny',    'vyborn',     false,'cs','init',true),
 ('syn_znamka_2','entity','dvojka',     'dvojka',     'dvojka',     'dvojk',      true, 'cs','init',true),
 ('syn_znamka_2','entity','chvalitebný','chvalitebny','chvalitebny','chvalitebn', false,'cs','init',true),
 ('syn_znamka_4','entity','čtyřka',     'ctyrka',     'ctyrka',     'ctyrk',      true, 'cs','init',true),
 ('syn_znamka_4','entity','dostatečný', 'dostatecny', 'dostatecny', 'dostatecn',  false,'cs','init',true),
 -- simplemma alone lemmatizes "nedostatečný" to "dostatečný" (the negation is
 -- lost, as with nesmím → smět); eduai_normalize 3.1.0 puts the "ne" back. With
 -- the old lemma this group's token bag would hold `dostatecny` and the query
 -- "dostatečný" (čtyřka) would wake the pětka group too — the self-check below
 -- guards exactly that.
 ('syn_znamka_5','entity','pětka',       'petka',       'petka',       'petk',        true, 'cs','init',true),
 ('syn_znamka_5','entity','nedostatečný','nedostatecny','nedostatecny','nedostatecn', false,'cs','init',true);

-- self-check: every group present with its term count, and the override held
DO $$
DECLARE
  want CONSTANT jsonb := '{"syn_dite_zak":10,"syn_stravovani":5,"syn_ppp":2,
    "syn_matematika":2,"syn_cestina":2,"syn_ucitel":4,"syn_znamka_1":2,
    "syn_znamka_2":2,"syn_znamka_4":2,"syn_znamka_5":2}';
  g text;
  n int;
BEGIN
  FOR g IN SELECT jsonb_object_keys(want) LOOP
    SELECT count(*) INTO n FROM edu_ai.qa_synonym
     WHERE group_id = g AND is_active AND lang = 'cs' AND kind = 'entity';
    IF n <> (want ->> g)::int THEN
      RAISE EXCEPTION '217: group % has % terms, expected %', g, n, want ->> g;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM edu_ai.qa_synonym
              WHERE group_id = 'syn_znamka_5'
                AND 'dostatecny' IN (term_norm, term_lemma, term_stem)) THEN
    RAISE EXCEPTION '217: syn_znamka_5 carries the token dostatecny (negation lost in the lemma)';
  END IF;
END $$;
