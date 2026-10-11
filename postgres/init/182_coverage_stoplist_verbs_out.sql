-- last change in code by Martin Safranek (2026-08-26).
--
-- Migrace 182: „přinést" není operátorové slovo, je to celé téma dotazu.
--
-- Naměřeno na vlastníkových nálezu (question_answer_log 2894, 26. 8.): dotaz
-- „co si mají děti přinést do školy" dostal kurátorskou odpověď o OMLOUVÁNÍ
-- ABSENCE. Příčina byla v coverage stoplistu, kde ležela slovesa přinést,
-- donést a nosit jako „operátorová". Po jejich odstranění z pokrytí zbyly
-- u toho dotazu jen koncepty `dite` a `skola` — obojí neurčité (df 10 % a 33 %
-- ze 130 záznamů) — pokrytí vyšlo 1,00 a rozhodlo se na slově „dítě", které
-- má ve své variantě pár o absenci („Co dělat, když dítě chybí ve škole?")
-- a správný pár o pomůckách ne.
--
-- Přitom správný pár v datech JE a jedna jeho varianta je dotazu téměř
-- totožná: „Co si mám přinýst do školy?". Aby vyhrála, musí se slovo, které
-- nese význam, počítat do pokrytí.
--
-- Operátorová slovesa mají v stoplistu své místo („chci vědět", „řekni mi"),
-- ale tato tři jsou v školním kontextu obsahová: dotaz na to, co si dítě má
-- přinést, je dotaz na seznam pomůcek. `mít` zůstává — to operátor je.
--
-- Ověřeno po změně: dotaz vrací seznam pomůcek (qa_vector), „Co si mám
-- přinýst do školy?" vrací totéž (qa_exact) a šest kontrolních dotazů
-- (absence, zapomenuté pomůcky, mobil, škola v přírodě, lyžařský výcvik,
-- „dítě chybí ve škole") odpovídá beze změny.
BEGIN;

UPDATE edu_ai.qa_stopword
   SET is_active = false
 WHERE scope = 'coverage'
   AND term IN ('prinest', 'přinést', 'donest', 'donést', 'nosit');

COMMIT;
