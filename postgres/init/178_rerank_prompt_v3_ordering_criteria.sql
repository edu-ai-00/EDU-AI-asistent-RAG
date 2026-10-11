-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 178: prompt soudce v3 — pořadí dostává kritérium, rozpor mizí.
--
-- v2 zadává tři různé úlohy naráz a jednu si protiřečí: "nehledáš nejlepší
-- pasáž, ale každou, ze které lze odpovědět", o dva řádky dál "rank = pořadí od
-- 1 (nejlepší) výš" a nakonec "Hledej pasáž, která nese samotný údaj". Model má
-- tedy současně klasifikovat každou pasáž nezávisle, seřadit všechny globálně a
-- najít tu nejlepší.
--
-- Horší je, že kritéria existují jen pro ZNÁMKU. Pro pořadí UVNITŘ stejné známky
-- prompt neříká nic — jediné ordering pravidlo v celém textu je věta o
-- rozcestnících. Dvacet pasáží tedy má dostat úplné pořadí podle kritéria na tři
-- třídy, takže zbytek je libovolný. Naměřeno na třech identických bězích
-- (question_answer_log 5416/5417/5418): pozice 1 a 2 stejné, od pozice 3 tři
-- různá pořadí, a s nimi tři různé konteksty. A oba příklady v v2 učí známku,
-- ani jeden pořadí.
--
-- v3 proto (a) říká výslovně, že známka není soutěž a pořadí soutěž je, takže si
-- ty dvě věty přestanou odporovat, (b) dává pořadí explicitní kritérium ve třech
-- krocích, (c) doplňuje příklad na pořadí, ne jen na známku, a (d) říká, co
-- znamená značka "…(zkráceno)" — pasáž pokračuje, chybějící údaj tedy není důkaz
-- o jeho neexistenci.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_rerank_graded' AND version = 2 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 3,
'Posuď každou pasáž vůči dotazu. Ke KAŽDÉ pasáži vrať grade a rank.

ZNÁMKA — není soutěž, každou pasáž posuzuj samostatně:
grade 2 = pasáž obsahuje údaj, který je součástí odpovědi — stačí ČÁST odpovědi (termín, délka, počet, podmínka, jeden z vyjmenovaných prvků). Dvojku může dostat libovolný počet pasáží, i všechny.
grade 1 = pasáž je o stejném tématu, ale žádný takový údaj v ní není.
grade 0 = pasáž s dotazem nesouvisí, i kdyby opakovala jeho slova.
Pasáž, která jen POPISUJE, co nějaký dokument je, k čemu slouží nebo co vymezuje, na věcný dotaz NEODPOVÍDÁ — i když opakuje jeho slova; taková pasáž je nejvýš 1.
Příklad: na dotaz „můžeme ve škole fotit?" je pasáž o kariérovém poradenství 0, pasáž o pravidlech mobilů 2. Na dotaz „z čeho se skládá jednotná přijímací zkouška?" je pasáž „test z českého jazyka trvá 60 minut, z matematiky 70 minut" 2 a pasáž „výsledky se zveřejní 15. května" 1.

POŘADÍ — soutěž je, každé číslo od 1 použij právě jednou. Řaď takto:
1. nejdřív všechny dvojky, pak jedničky, pak nuly;
2. uvnitř stejné známky dřív ta pasáž, která odpovídá na to, NA CO SE PTÁ dotaz, před pasáží o témže tématu z jiné strany;
3. při stejné užitečnosti dřív ta konkrétnější (více údajů, méně obecných formulací). Rozcestník s odkazy jinam patří vždy za pasáže s údaji.
Příklad pořadí: na dotaz „kdy jsou přijímačky?" jde pasáž „jednotná zkouška se koná 10. a 13. dubna" před pasáž „náhradní termín stanoví ředitel" (obě 2, ale první odpovídá na dotazovanou věc) a ta před pasáž „přihlášku podáte do 1. března" (jednička, jiný aspekt).

Značka „…(zkráceno)" na konci pasáže znamená, že text pokračuje. Chybějící údaj v takové pasáži není důkaz, že v ní není — je-li o správném tématu, nedávej 0.',
       true, component,
       'v3: pořadí má kritérium; známka není soutěž; značka o zkrácení',
       model_hint, lang, 'martin',
       'Migrace 178 — measured on log 5416/5417/5418 (identical pool, three different orders below position 2)'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_rerank_graded' AND version = 2;

COMMIT;
