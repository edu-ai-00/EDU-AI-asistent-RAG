-- last change in code by Martin Safranek (2026-07-18).
-- ============================================================
-- 17_ingestion_log_flow_id.sql
-- Add flow_id to ingestion_log so each audit row records which
-- Langflow flow wrote it (PDF 861cb59e vs MARKDOWN 97cdd47b, …).
-- Written by the IngestionLogger passthrough component (self.flow_id).
--
-- Incremental migration: init SQL runs only on an empty volume, so
-- apply this by hand on a running DB.
-- ============================================================

ALTER TABLE edu_ai.ingestion_log
  ADD COLUMN IF NOT EXISTS flow_id uuid;

COMMENT ON COLUMN edu_ai.ingestion_log.flow_id IS
  'Langflow flow id (self.flow_id) — which flow wrote this audit row.';
