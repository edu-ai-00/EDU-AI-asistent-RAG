-- Add the approved school operations and safety DOMAIN codes.
-- Also mirrored in init_clean/03_school_operations_safety_domains.sql;
-- tests/test_school_domains_migration.py checks that the copies agree.
-- Existing declarations (including retired codes) are curator-owned and preserved.
BEGIN;

LOCK TABLE edu_ai.taxonomy_rules IN SHARE ROW EXCLUSIVE MODE;

WITH owners AS (
    -- Do not create a partial own catalog for schools inheriting from a parent.
    SELECT DISTINCT legal_entity_code
    FROM edu_ai.taxonomy_rules
    WHERE taxonomy_type_code = 'DOMAIN' AND is_active
    UNION SELECT NULL::text
), domains(type_code, position, labels, llm_instruction) AS (
    VALUES
    ('SCHOOL_OPERATIONS', 1,
     '{"cs":"Provoz školy","en":"School operations","de":"Schulbetrieb","es":"Funcionamiento del centro escolar"}'::jsonb,
     'Everyday school operations and internal rules: opening hours, daily timetable, breaks, after-school care, mobile devices, supplies, property, school-family communication, disciplinary measures and pupil representation. Attendance and excuses, grading and re-sits, meals, school trips, bullying, personal-data protection, special educational needs and health/safety belong to their specific domains; add this domain only when the content also substantively covers school operations.'),
    ('SCHOOL_SAFETY', 2,
     '{"cs":"Bezpečnost a zdraví","en":"Safety and health","de":"Sicherheit und Gesundheit","es":"Seguridad y salud"}'::jsonb,
     'Health and safety at school: illness during the school day, injuries and first aid, infectious diseases, hygiene and epidemic measures, dangerous objects and substances, safe use of facilities, and prevention of dangerous or violent conduct. Bullying may additionally belong to BULLYING; school-trip organisation belongs to SCHOOL_EVENTS, absence excuses to ATTENDANCE, and educational support to SPECIAL_NEEDS.')
)
INSERT INTO edu_ai.taxonomy_rules
    (type_code, taxonomy_type_code, sort_order, is_active, created_by,
     labels, llm_instruction, domain_keywords, search_role, legal_entity_code)
SELECT d.type_code, 'DOMAIN', ordering.last_order + d.position, true,
       'migration-214', d.labels, d.llm_instruction, NULL, 'normal', o.legal_entity_code
FROM owners o
CROSS JOIN domains d
CROSS JOIN LATERAL (
    SELECT coalesce(max(t.sort_order), 0) AS last_order
    FROM edu_ai.taxonomy_rules t
    WHERE t.taxonomy_type_code = 'DOMAIN'
      AND t.legal_entity_code IS NOT DISTINCT FROM o.legal_entity_code
) ordering
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.taxonomy_rules existing
    WHERE existing.type_code = d.type_code
      AND existing.legal_entity_code IS NOT DISTINCT FROM o.legal_entity_code
);

COMMIT;
