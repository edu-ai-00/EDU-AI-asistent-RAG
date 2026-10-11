-- last change in code by Martin Safranek (2026-08-22).
--
-- Migration 165: prompty rerankeru do prompt_registry.
--
-- Každý LLM prompt v systému je verzovaná DATA (prompt_registry, DB > kód, bez
-- restartu) — kromě dvou, které až dosud sedely v search/app/rerank.py. Ladit
-- soudce tedy znamenalo docker build, což je u komponenty, jejíž chování se ladí
-- formulací, ta nejdražší možná cesta.
--
--   search_rerank_order    listwise přeřazení: model vrátí permutaci indexů
--   search_rerank_graded   verdikt na pasáž (grade 0/1/2 + rank)
--
-- Text 'graded' je oproti kódu z 22. 8. přepsaný, protože měření ukázalo, čím ta
-- původní formulace škodila: model rozdal přesně JEDNU dvojku na dotaz (1 z 8
-- pasáží, 1 z 20) — čte "2" jako "nejlepší pasáž", ne jako "obsahuje odpověď".
-- Na "Z čeho se skládá jednotná přijímací zkouška?" tak chunk MŠMT s délkami
-- testů (60 a 70 minut, uvnitř 900znakového okna, takže je soudce VIDĚL) dostal
-- jedničku, a protože dokument potřebuje chunk s grade 2, aby založil odstavec,
-- odpověď ztratila druhý zdroj. Nová verze proto říká dvě věci navíc: stačí ČÁST
-- odpovědi, a počet dvojek není nijak omezený.
INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT v.prompt_key, v.version, v.prompt_text, true, v.component, v.description,
       v.model_hint, 'cs', 'migration-165', v.notes
  FROM (VALUES
        ('search_rerank_order', 1,
         'Seřaď pasáže podle toho, jak dobře odpovídají na dotaz. Vrať POUZE JSON pole indexů (čísel) od nejlepší po nejhorší, všechny indexy.',
         'search/rerank.rerank_scores(backend=openai)',
         'Listwise reranker: z permutace indexů se odvodí skóre n-pozice.',
         'gpt-4o-mini',
         'Přeneseno z kódu bez změny textu.'),
        ('search_rerank_graded', 1,
         'Posuď každou pasáž vůči dotazu. Ke KAŽDÉ pasáži vrať grade a rank.' || chr(10) ||
         'grade 2 = pasáž obsahuje údaj, který je součástí odpovědi — stačí ČÁST odpovědi (termín, délka, počet, podmínka, jeden z vyjmenovaných prvků). Grade 2 může dostat libovolný počet pasáží, i všechny; nehledáš nejlepší pasáž, ale každou, ze které lze odpovědět.' || chr(10) ||
         'grade 1 = pasáž je o stejném tématu, ale žádný takový údaj v ní není.' || chr(10) ||
         'grade 0 = pasáž s dotazem nesouvisí, i kdyby opakovala jeho slova.' || chr(10) ||
         'Příklad 1: na dotaz „můžeme ve škole fotit?“ je pasáž o kariérovém poradenství grade 0, pasáž o pravidlech mobilů grade 2.' || chr(10) ||
         'Příklad 2: na dotaz „z čeho se skládá jednotná přijímací zkouška?“ je pasáž „test z českého jazyka trvá 60 minut, z matematiky 70 minut“ grade 2 (údaj o testech, byť jen část odpovědi) a pasáž „výsledky se zveřejní 15. května“ grade 1.' || chr(10) ||
         'rank = pořadí od 1 (nejlepší) výš, každé číslo použij jednou.',
         'search/rerank.rerank_graded',
         'Verdikt na pasáž (grade 0/1/2 + rank); skóre = grade*1000 + (n-rank).',
         'gpt-4o-mini',
         'v1: stačí ČÁST odpovědi + počet dvojek neomezen (měřeno 22. 8. 2026: jedna dvojka na dotaz brala druhý zdroj).')
       ) AS v(prompt_key, version, prompt_text, component, description,
              model_hint, notes)
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry p
                    WHERE p.prompt_key = v.prompt_key AND p.version = v.version);
