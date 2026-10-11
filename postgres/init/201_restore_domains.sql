-- Migrace 201: obnovení devíti domén, které jsem 27. 8. 2026 omylem smazal
--
-- CO SE STALO. Při vracení migrace 199 jsem doménové řádky mazal podmínkou
-- created_by='martin'. Migrace 199 ale předtím povýšila pět domén z úrovně
-- jednotky na globální — a ty povýšené řádky nesou created_by='martin', zatímco
-- jejich původní jednotkové verze táž migrace smazala. Můj DELETE tak odstranil
-- i domény, které v systému byly PŘED dneškem. Ze třinácti zbyly čtyři.
--
-- ODKUD SE OBNOVUJE. Klíčová slova jsou z migrace 130, která je v gitu a je tedy
-- autoritativní. Klasifikační pravidla a české popisky jsou z výpisu katalogu,
-- který jsem dnes pořizoval — obsahově tedy původní, ale opsané, ne obnovené ze
-- záložního souboru; dump serverové databáze, ze kterého řádky původně přišly,
-- na disku není.
--
-- CO SE NEOBNOVUJE. Popisky v angličtině, němčině a španělštině u těchto devíti
-- domén; ve výpisu jsem si bral jen českou variantu. Uživatelské rozhraní tedy
-- u nich zobrazí jen češtinu, dokud se nedoplní. Vyhledávání ani ingesce na
-- těch popiscích nestojí — LLM dostává kód a pravidlo, detekce klíčová slova.
--
-- CO SE OBNOVUJE JINAK, NEŽ TO BYLO. Řádky vznikají jako GLOBÁLNÍ, ne per
-- jednotka. Původní stav měl u Nové Vsi jednotkové kopie, které stejně jen
-- zastiňovaly globální sadu a část jich byla bez klíčových slov (změřeno dnes).
-- Globální řádek je funkčně lepší a je to jediná varianta, kterou umím vytvořit
-- bez uhádnutí, co v jednotkových řádcích stálo.

INSERT INTO edu_ai.taxonomy_rules
  (type_code, taxonomy_type_code, sort_order, is_active, created_by, labels,
   llm_instruction, domain_keywords, search_role)
VALUES
('SECONDARY_ADMISSIONS','DOMAIN',2,true,'restore-201',
 '{"cs":"Přijímačky na střední školu"}'::jsonb,
 'Admission / entrance to a FOUR-year secondary school (přijímací řízení na střední školu, čtyřleté obory) — applicants finishing the 9th grade of basic school. NOT a multi-year gymnasium (separate domain), NOT a university.',
 ARRAY['přijímačky','přijímací zkouška','přijímací řízení','jednotná přijímací zkouška','JPZ','cermat','přijmačky','příjmačky','přijimačky','prijmacky'],'normal'),

('ATTENDANCE','DOMAIN',5,true,'restore-201',
 '{"cs":"Docházka a absence"}'::jsonb,
 'Content about school attendance: absences, excuses, leave of absence, late arrivals and truancy rules.',
 ARRAY['docházka','omluvenka','uvolnění','absence'],'normal'),

('GRADING','DOMAIN',6,true,'restore-201',
 '{"cs":"Klasifikace a hodnocení"}'::jsonb,
 'Content about assessment and grading: marks, report cards, evaluation rules, honours, re-sits.',
 ARRAY['známka','klasifikace','vysvědčení','hodnocení prospěchu'],'normal'),

('SCHOOL_EVENTS','DOMAIN',7,true,'restore-201',
 '{"cs":"Školní akce a výlety"}'::jsonb,
 'Content about school events, trips, excursions and outdoor school (škola v přírodě): organisation, dates, consent, payment.',
 NULL,'normal'),

('CATERING','DOMAIN',8,true,'restore-201',
 '{"cs":"Školní stravování"}'::jsonb,
 'Content about school meals and the canteen: menus, payments, allergens, sign-up and rules.',
 ARRAY['jídelna','oběd','stravování'],'normal'),

('ENROLLMENT','DOMAIN',9,true,'restore-201',
 '{"cs":"Zápis a přestup"}'::jsonb,
 'Content about enrolment and transfers: enrolment to the first grade, transfers between schools, required documents.',
 ARRAY['zápis do první třídy','zápis do školy'],'normal'),

('MULTIYEAR_GYMNASIUM_ADMISSIONS','DOMAIN',11,true,'restore-201',
 '{"cs":"Přijímačky na víceletá gymnázia"}'::jsonb,
 'Admission to a MULTI-YEAR gymnasium (přijímací řízení na víceleté gymnázium — šestileté / osmileté), applicants from the 5th grade (eight-year) or 7th grade (six-year) of basic school.',
 ARRAY['víceleté gymnázium','osmileté gymnázium','šestileté gymnázium'],'normal'),

('UNIVERSITY_ADMISSIONS','DOMAIN',12,true,'restore-201',
 '{"cs":"Přijímačky na vysokou školu"}'::jsonb,
 'Admission / entrance to a UNIVERSITY or college (přijímací řízení na vysokou školu) — for secondary-school graduates / maturanti (final year of secondary school).',
 NULL,'normal'),

('GRADUATION_EXAM','DOMAIN',1,true,'restore-201',
 '{"cs":"Maturita"}'::jsonb,
 'Content about the maturita (secondary-school leaving examination): its organisation, subjects, dates, rules, results or appeals.',
 NULL,'normal');
