-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 167: zkratky ze školního řádu jako synonymní skupiny.
--
-- Školní dokumenty píšou zkratky a uživatel se ptá slovy. U LVK (migrace 163) to
-- byla náhoda jednoho chunku, tady je to vzorec: ve školním řádu Nové Vsi je
-- IVP šestkrát, ŠPZ čtyřikrát, SVP třikrát, OMJ třikrát, PLPP dvakrát — a chunk
-- 556, který jako jediný popisuje, CO IVP obsahuje, slovo „individuální" nemá
-- vůbec, jen zkratku. Dotaz „individuální vzdělávací plán" ho tedy lexikálně
-- nemůže potkat. Rozšíření běží i na dokumentové cestě (document_lexical volá
-- expand_query stejně jako qa_lexical), takže to jsou data, žádný kód a žádná
-- re-ingesce.
--
-- Co ZÁMĚRNĚ chybí a proč:
--   ZŠ / SŠ    stupeň řeší school_level; skupina by musela obsahovat token
--              „škola", a ten je v tomhle korpusu tak častý, že by rozšíření
--              nastartoval každý druhý dotaz
--   ŠVP        po odstranění diakritiky je z „ŠVP" i „SVP" token svp, takže by
--              obě skupiny kolidovaly na jednom tokenu; SVP je v řádu 3x, ŠVP 1x
--   MZ         v řádu jde o Ministerstvo zdravotnictví, ale MZ běžně znamená
--              maturitní zkoušku — nejednoznačnou zkratku do synonym nedáváme
--   ČR, ČNR, SIM, MUF, SCIO, SOŠS  vlastní jména, historické odkazy nebo názvy
--              kabinetů; nikdo se na ně neptá zkratkou ani slovem
--
-- Skupina „OZP" navíc zavírá M6 („Kdy je nejbližší škola v přírodě?"): v termínech
-- akcí je ozdravný pobyt jen jako OZP. Je v ní „ozdravný pobyt", „pobyt v přírodě"
-- I „škola v přírodě" — poslední term nese generický token „škola" a tedy riziko,
-- že expanzi nastartuje i nesouvisející dotaz; to se řeší měřením a případně
-- v kódu (generický token by expanzi startovat neměl), ne vyhozením toho, jak se
-- lidé skutečně ptají.
INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem,
                               is_key, lang, source)
SELECT v.group_id, 'entity', v.term, v.term_norm, v.term_lemma, v.term_stem,
       v.is_key, 'cs', 'seed'
  FROM (VALUES
        ('syn_ivp','IVP','ivp','ivp','ivp',true),
        ('syn_ivp','individuální vzdělávací plán','individualni vzdelavaci plan',
         'individualni vzdelavaci plan','individualn vzdelavac plan',false),
        ('syn_plpp','PLPP','plpp','plpp','plpp',true),
        ('syn_plpp','plán pedagogické podpory','plan pedagogicke podpory',
         'plan pedagogicky podpora','plan pedagogick podpor',false),
        ('syn_spz','ŠPZ','spz','spz','spz',true),
        ('syn_spz','školské poradenské zařízení','skolske poradenske zarizeni',
         'skolsky poradensky zarizeni','skolsk poradensk zarizen',false),
        ('syn_svp','SVP','svp','svp','svp',true),
        ('syn_svp','speciální potřeby','specialni potreby','specialni potreba',
         'specialn potreb',false),
        ('syn_omj','OMJ','omj','omj','omj',true),
        ('syn_omj','odlišný mateřský jazyk','odlisny matersky jazyk',
         'odlisny matersky jazyk','odlisn matersk jazyk',false),
        ('syn_msmt','MŠMT','msmt','msmt','msmt',true),
        ('syn_msmt','ministerstvo školství','ministerstvo skolstvi',
         'ministerstvo skolstvi','ministerstv skolstv',false),
        ('syn_ozp','OZP','ozp','ozp','ozp',true),
        ('syn_ozp','ozdravný pobyt','ozdravny pobyt','ozdravny pobyt',
         'ozdravn pobyt',false),
        ('syn_ozp','pobyt v přírodě','pobyt prirode','pobyt priroda',
         'pobyt prirod',false),
        -- „škola v přírodě" je to, jak se na OZP lidi ptají, takže v skupině být
        -- MUSÍ (Martinovo rozhodnutí 22. 8.). Nese s sebou token „škola", který je
        -- v tomhle korpusu generický, takže rozšíření nastartuje i dotaz, který
        -- s ozdravným pobytem nesouvisí — proto se to po zavedení měří na dotazech
        -- se slovem „škola" a v případě potřeby se řeší v kódu (generický token by
        -- neměl startovat expanzi), ne vyhozením termu.
        ('syn_ozp','škola v přírodě','skola prirode','skola priroda',
         'skol prirod',false)
       ) AS v(group_id, term, term_norm, term_lemma, term_stem, is_key)
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_synonym q
                    WHERE q.group_id = v.group_id AND q.term = v.term);
