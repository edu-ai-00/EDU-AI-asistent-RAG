-- last change in code by Martin Safranek (2026-08-02).
-- 88_deactivate_ms_grades.sql
-- The kindergarten will not use the system, so its grades leave the UI.
--
-- Deactivated, not deleted: code_list is read WHERE is_active everywhere, so an
-- inactive row is invisible to every form and every classification prompt while
-- the labels stay translated. If a kindergarten ever joins, it is one UPDATE — and
-- deleting would mean re-deriving the four-language labels for nothing.
--
-- The MŠ entry in legal_entity.units stays. That is registry fact — the legal
-- person does operate a kindergarten (IZO 1234) — and the register should
-- describe the organisation, not only the part of it that logs in.
--
-- The grade codes keep their level prefix, and this does not change that: the
-- entity still spans a primary school and a secondary one (ZŠ 1234,
-- SOŠ 1234), so "2" inside it is still two different years. A bare number
-- becomes resolvable only if grades are ever scoped to the unit rather than the
-- entity — see migration 87.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.code_list
   SET is_active = false
 WHERE list_code = 'school_grade' AND code IN ('MS1', 'MS2', 'MS3');

COMMIT;
