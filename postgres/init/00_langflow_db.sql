-- last change in code by Martin Safranek (2026-07-07).
-- Internal Langflow state database (flows, users, API keys, variables).
-- Langflow bootstraps its own schema into it via automatic Alembic
-- migrations on first start (LANGFLOW_DATABASE_URL in docker-compose.yml).
CREATE DATABASE langflow OWNER edu_ai;
