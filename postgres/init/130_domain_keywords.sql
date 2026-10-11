-- 130: query→domain mapping keywords (2026-08-09, produktový vlastník priority follow-up).
-- domain_keywords on DOMAIN taxonomy rows: raw, human-editable terms that map a
-- QUESTION onto the domain deterministically (reduced through the same
-- build_forms() contract as the query — no LLM in the retrieval path). Guards
-- at match time: a keyword token found in the coverage stoplist or corpus-
-- generic by df share is IGNORED, so a generic word can never switch a domain
-- on. Consumed by answer.py as a SMALL additive fusion boost (domain_match_
-- boost, default 0.05) — deliberately gentle for now.
-- Seeded on GLOBAL rows only (NULL legal_entity_code); per-entity rows can
-- override by carrying their own list.

ALTER TABLE edu_ai.taxonomy_rules
    ADD COLUMN IF NOT EXISTS domain_keywords text[];

COMMENT ON COLUMN edu_ai.taxonomy_rules.domain_keywords IS
  'DOMAIN rows: raw terms mapping a question onto this domain (reduced at match time; stop/generic tokens ignored). NULL = domain not detectable from text.';

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['přijímačky','přijímací zkouška','přijímací řízení','jednotná přijímací zkouška','JPZ','cermat','přijmačky','příjmačky','přijimačky','prijmacky']
WHERE taxonomy_type_code='DOMAIN' AND type_code='SECONDARY_ADMISSIONS' AND legal_entity_code IS NULL;

-- MULTIYEAR/ENROLLMENT/CATERING have no global row — seed their per-entity rows
UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['víceleté gymnázium','osmileté gymnázium','šestileté gymnázium']
WHERE taxonomy_type_code='DOMAIN' AND type_code='MULTIYEAR_GYMNASIUM_ADMISSIONS';

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['šikana','kyberšikana']
WHERE taxonomy_type_code='DOMAIN' AND type_code='BULLYING' AND legal_entity_code IS NULL;

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['docházka','omluvenka','uvolnění','absence']
WHERE taxonomy_type_code='DOMAIN' AND type_code='ATTENDANCE' AND legal_entity_code IS NULL;

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['známka','klasifikace','vysvědčení','hodnocení prospěchu']
WHERE taxonomy_type_code='DOMAIN' AND type_code='GRADING' AND legal_entity_code IS NULL;

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['gdpr','osobní údaje','pověřenec']
WHERE taxonomy_type_code='DOMAIN' AND type_code='DATA_PROTECTION' AND legal_entity_code IS NULL;

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['zápis do první třídy','zápis do školy']
WHERE taxonomy_type_code='DOMAIN' AND type_code='ENROLLMENT';

UPDATE edu_ai.taxonomy_rules SET domain_keywords = ARRAY['jídelna','oběd','stravování']
WHERE taxonomy_type_code='DOMAIN' AND type_code='CATERING';
