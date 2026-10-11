-- Migrace 192: synonymická skupina pro fotografování
--
-- Markův nález S2: „můžeme ve škole fotit?“ Slovo „fotit“ se v celém korpusu
-- nevyskytuje ani jednou — školní řád mluví o „fotografování či pořizování
-- jiných záznamů“. Lemmatizace to nespojí (fotit ≠ fotografování), takže
-- lexikální ani klíčoslovný kanál pasáž nenajde a zbyde vektor s kosinem 0,54.
--
-- Změřeno 27. 8. 2026, se skupinou a bez ní, na dotazu žáka 8. ročníku ZŠ:
--   bez skupiny: signals [(rag_vector, coverage 0.0, cosine 0.5406), (…, 0.5172)]
--                → žádná evidence nad prahem → mlčení
--   se skupinou: signals [(document_lexical, coverage 0.544), …]
--                → věta o rozsahu z migrace 191
--
-- Tohle je učebnicový příklad toho, proč je průchod dokumentů na synonyma a
-- zkratky povinný krok iniciálního nastavení školy: bez něj je pasáž lexikálně
-- nedosažitelná a žádné ladění vyhledávání to nenahradí.

INSERT INTO edu_ai.qa_synonym
       (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
VALUES
 ('syn_foto','entity','fotografování','fotografovani','fotografovani','fotografovan',false,'cs','seed',true),
 ('syn_foto','entity','fotografovat','fotografovat','fotografovat','fotografova',false,'cs','seed',true),
 ('syn_foto','entity','fotit','fotit','fotit','fot',true,'cs','seed',true),
 ('syn_foto','entity','focení','foceni','foceni','focen',false,'cs','seed',true),
 ('syn_foto','entity','pořizování záznamů','porizovani zaznamu','porizovani zaznam','porizovan zaznam',false,'cs','seed',true);
