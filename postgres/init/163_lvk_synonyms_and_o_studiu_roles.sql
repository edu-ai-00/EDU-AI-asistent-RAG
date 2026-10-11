-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 163: two data fixes found by testing the RAG cascade on 22. 8. 2026.
--
-- 1) ZKRATKA LVK. The events table writes the ski course only as an abbreviation
--    ("**LVK a zimní OZP** | LVK 7., 8. ročník - 16. - 20.3.2026 (Benecko)"); the
--    words "lyžařský výcvik" appear nowhere in the chunk, so no lexical path could
--    ever match the question and the answer depended on the vector step happening
--    to bridge it. It did for "kdy je lyžařský výcvik?" and did not for "kdy má
--    naše třída naplánovaný lyžařský výcvik?" — same corpus, same day, opposite
--    outcome. A synonym group closes it on BOTH the tsquery side (the chunk is
--    retrieved) and the coverage side (it is not dropped as off-topic), because
--    document_lexical expands the query through qa_synonym exactly like qa_lexical.
--    Measured: ročník 7 and 8 went from no_answer to the correct term, and ročník 9
--    now honestly says nothing instead of serving a course meant for 7th and 8th.
--    Note the group is PLATFORM-WIDE (qa_synonym has no entity) and its terms are
--    split into TOKENS, so "výcvik" in another context matches the group too.
--
-- 2) SEZNAM POMŮCEK BYL SKRYTÝ UČITELŮM. The o-studiu document (supply lists,
--    testing results) carried audience_roles = {PARENT, STUDENT} in the metadata
--    AND on all of its chunks. scope.where() reads the role from
--    document_chunks.audience_roles, so for a teacher the document did not exist
--    and "jaké pomůcky potřebuji do školy" answered with the exam rules from
--    CERMAT. A supply list is not parent-only information.
INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem,
                               is_key, lang, source)
SELECT v.group_id, v.kind, v.term, v.term_norm, v.term_lemma, v.term_stem,
       v.is_key, v.lang, v.source
  FROM (VALUES
        ('syn_lvk','entity','LVK','lvk','lvk','lvk',true,'cs','seed'),
        ('syn_lvk','entity','lyžařský výcvik','lyzarsky vycvik','lyzarsky vycvik',
         'lyzarsk vycvik',false,'cs','seed'),
        ('syn_lvk','entity','lyžařský výcvikový kurz','lyzarsky vycvikovy kurz',
         'lyzarsky vycvikovy kurs','lyzarsk vycvik kurz',false,'cs','seed'),
        ('syn_lvk','entity','lyžák','lyzak','lyzak','lyzak',false,'cs','seed')
       ) AS v(group_id, kind, term, term_norm, term_lemma, term_stem,
              is_key, lang, source)
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_synonym q
                    WHERE q.group_id = v.group_id AND q.term = v.term);

UPDATE edu_ai.document_metadata
   SET audience_roles = ARRAY['PARENT','STUDENT','TEACHER']
 WHERE is_active
   AND source_url LIKE '%o-studiu%'
   AND NOT ('TEACHER' = ANY(audience_roles));

UPDATE edu_ai.document_chunks c
   SET audience_roles = ARRAY['PARENT','STUDENT','TEACHER']
  FROM edu_ai.document_metadata m
 WHERE c.file_uuid = m.file_uuid
   AND c.processing_uuid = m.processing_uuid
   AND m.is_active
   AND m.source_url LIKE '%o-studiu%'
   AND NOT ('TEACHER' = ANY(c.audience_roles));
