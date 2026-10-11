-- last change in code by Martin Safranek (2026-07-15).
-- ════════════════════════════════════════════════════════════════════
--  edu_ai — KOMPLETNÍ SCHÉMA (jeden soubor, aktuální obraz)
-- ────────────────────────────────────────────────────────────────────
--  Konsolidovaný snapshot vygenerovaný přímo z BĚŽÍCÍ databáze
--  (pg_dump --schema-only) + seed konfiguračních číselníků. Nahrazuje nutnost
--  procházet postgres/init/01–16 (základ + inkrementální ALTERy) — tady je
--  rovnou finální stav: schéma edu_ai, 19 tabulek, indexy, extensions.
--
--  OBSAHUJE:
--    • CREATE EXTENSION vector / unaccent / pg_trgm
--    • CREATE SCHEMA edu_ai + všechny tabulky, indexy, constrainty, generated cols
--    • SEED číselníků: search_setting (vč. aktivní v3), qa_stopword, qa_blocklist,
--      embedding_registry  (NE testovací data — chunky/QA páry jsou prázdné)
--
--  POUŽITÍ (nová/prázdná DB, např. RDS):
--    createdb edu_ai         # nebo RDS instance database "edu_ai"
--    psql "postgresql://edu_ai:***@HOST:5432/edu_ai" -v ON_ERROR_STOP=1 -f edu_ai_schema.sql
--  Pozn.: CREATE EXTENSION vyžaduje superuser / rds_superuser.
--         Databáze "langflow" (interní stav Langflow) se zakládá zvlášť
--         (postgres/init/00_langflow_db.sql) — není součástí tohoto souboru.
--
--  REGENERACE (po změně schématu):
--    docker exec edu_ai_postgres pg_dump -U edu_ai -d edu_ai --schema-only \
--      --no-owner --no-privileges  > ddl && \
--    docker exec edu_ai_postgres pg_dump -U edu_ai -d edu_ai --data-only --column-inserts \
--      --table=edu_ai.search_setting --table=edu_ai.qa_stopword \
--      --table=edu_ai.qa_blocklist --table=edu_ai.embedding_registry  > seed
--
--  Toto je JEDINÝ schéma-soubor pro edu_ai — nahradil inkrementální
--  postgres/init/01–16 (základ + ALTERy). docker-compose ho spustí sám na
--  prázdném volume (po 00_langflow_db.sql, které zakládá DB "langflow").
--  Použitelný i pro manuální / RDS nasazení.
-- ════════════════════════════════════════════════════════════════════

--
-- PostgreSQL database dump
--

-- Dumped from database version 16.3 (Debian 16.3-1.pgdg120+1)
-- Dumped by pg_dump version 16.3 (Debian 16.3-1.pgdg120+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: edu_ai; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA edu_ai;


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA edu_ai;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: unaccent; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA edu_ai;


--
-- Name: EXTENSION unaccent; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION unaccent IS 'text search dictionary that removes accents';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA edu_ai;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: edu_ai_chunk_keywords; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.edu_ai_chunk_keywords (
    id bigint NOT NULL,
    processing_uuid uuid NOT NULL,
    file_uuid uuid NOT NULL,
    file_item_uuid uuid NOT NULL,
    file_name text,
    file_path text,
    chunk_index integer NOT NULL,
    chunk_root_id uuid,
    output_row_number integer,
    keywords text[],
    keyword_scores jsonb,
    keyword_source text,
    keyword_count integer,
    extracted_at timestamp without time zone DEFAULT now(),
    ai_model text
);


--
-- Name: edu_ai_chunk_keywords_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

CREATE SEQUENCE edu_ai.edu_ai_chunk_keywords_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: edu_ai_chunk_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: edu_ai; Owner: -
--

ALTER SEQUENCE edu_ai.edu_ai_chunk_keywords_id_seq OWNED BY edu_ai.edu_ai_chunk_keywords.id;


--
-- Name: edu_ai_document_chunks; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.edu_ai_document_chunks (
    id bigint NOT NULL,
    processing_uuid uuid NOT NULL,
    file_uuid uuid NOT NULL,
    file_item_uuid uuid NOT NULL,
    output_row_number integer NOT NULL,
    file_name text,
    file_path text,
    doc_title text,
    page_number integer,
    page_element_index integer,
    vertical_position double precision,
    header_detected text,
    txt_structure_type text,
    txt_should_chunk_reason text,
    source_name text,
    langdetect_result text,
    langdetect_result_score double precision,
    chunk_index integer NOT NULL,
    chunk_text text NOT NULL,
    chunk_granularity text DEFAULT 'document'::text,
    chunk_root_id uuid,
    chunk_parent_id uuid,
    parent_block_number integer,
    chunk_size_measured integer,
    chunk_size_used integer,
    chunk_overlap_used integer,
    chunking_mode_used text,
    embedding edu_ai.vector(1536) NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimension integer,
    embedding_provider text,
    embedding_created_at timestamp without time zone DEFAULT now(),
    ldts_local timestamp without time zone,
    ldts_gmt timestamp without time zone,
    chunk_norm_text text,
    chunk_lemma_text text,
    chunk_stem_text text,
    setting_id uuid,
    ts_norm tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_norm_text, ''::text))) STORED,
    ts_lemma tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_lemma_text, ''::text))) STORED,
    ts_stem tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(chunk_stem_text, ''::text))) STORED
);


--
-- Name: edu_ai_document_chunks_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.edu_ai_document_chunks ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.edu_ai_document_chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: edu_ai_document_elements; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.edu_ai_document_elements (
    id bigint NOT NULL,
    file_item_uuid uuid NOT NULL,
    processing_uuid uuid NOT NULL,
    file_uuid uuid NOT NULL,
    page_number integer NOT NULL,
    page_element_index integer NOT NULL,
    output_row_number integer NOT NULL,
    record_type text,
    table_index integer,
    table_metadata text,
    header_detected text,
    header_row_reference integer,
    text text,
    text_extracted text,
    ldts_local timestamp without time zone,
    ldts_gmt timestamp without time zone,
    inserted_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: edu_ai_document_elements_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.edu_ai_document_elements ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.edu_ai_document_elements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: edu_ai_document_metadata; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.edu_ai_document_metadata (
    file_uuid uuid NOT NULL,
    processing_uuid uuid NOT NULL,
    file_name text,
    file_path text,
    document_type text,
    document_type_orig text,
    document_type_orig_status text,
    document_type_reason text,
    school_subject text,
    school_subject_local text,
    school_subject_orig text,
    school_subject_orig_status text,
    school_subject_grade integer,
    school_subject_grade_orig text,
    school_subject_grade_orig_status text,
    document_language text,
    document_language_local text,
    document_language_orig text,
    document_language_orig_status text,
    user_name text,
    private_public_flag text,
    ldts_local timestamp without time zone,
    ldts_gmt timestamp with time zone,
    inserted_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_active boolean DEFAULT true NOT NULL,
    source_url text,
    original_file_name text
);


--
-- Name: edu_ai_document_tables; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.edu_ai_document_tables (
    id bigint NOT NULL,
    file_item_uuid uuid NOT NULL,
    processing_uuid uuid NOT NULL,
    file_uuid uuid NOT NULL,
    file_name text,
    file_path text,
    page_number integer,
    tbl_index integer,
    record_type text,
    output_row_number integer,
    page_element_index integer,
    tbl_metadata text,
    tbl_metadata_score double precision,
    header_detected text,
    header_row_reference integer,
    tbl_table_merged boolean,
    tbl_merge_group_id text,
    tbl_merge_size integer,
    tbl_json_empty_ratio double precision,
    tbl_json_null_ratio double precision,
    tbl_json_numeric_ratio double precision,
    tbl_json_quality_score double precision,
    tbl_json_valid boolean,
    tbl_json_rows integer,
    tbl_json_columns integer,
    tbl_json_column_profile jsonb,
    tbl_json_row_profile jsonb,
    ldts_local timestamp without time zone,
    ldts_gmt timestamp without time zone,
    langdetect_result text,
    langdetect_result_score double precision,
    enrichment_status text,
    json_summary jsonb,
    json_categorisation jsonb,
    json_categorisation_reason text,
    ai_model text,
    enriched_at timestamp without time zone,
    tbl_json jsonb,
    inserted_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    user_name text,
    private_public_flag text,
    document_type_orig text,
    document_language_orig text,
    school_subject_orig text,
    school_subject_grade_orig text,
    page_height double precision,
    tbl_cluster_signature text,
    tbl_size_bucket text,
    tbl_shape_bucket text,
    tbl_numeric_bucket text,
    tbl_density_bucket text
);


--
-- Name: edu_ai_document_tables_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

CREATE SEQUENCE edu_ai.edu_ai_document_tables_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: edu_ai_document_tables_id_seq; Type: SEQUENCE OWNED BY; Schema: edu_ai; Owner: -
--

ALTER SEQUENCE edu_ai.edu_ai_document_tables_id_seq OWNED BY edu_ai.edu_ai_document_tables.id;


--
-- Name: embedding_registry; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.embedding_registry (
    generation integer NOT NULL,
    embedding_model text NOT NULL,
    dimension integer NOT NULL,
    vector_type text DEFAULT 'vector'::text NOT NULL,
    target_table text NOT NULL,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    notes text,
    CONSTRAINT embedding_registry_status_check CHECK ((status = ANY (ARRAY['building'::text, 'active'::text, 'retired'::text]))),
    CONSTRAINT embedding_registry_vector_type_check CHECK ((vector_type = ANY (ARRAY['vector'::text, 'halfvec'::text])))
);


--
-- Name: ingestion_log; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.ingestion_log (
    id bigint NOT NULL,
    log_timestamp timestamp without time zone NOT NULL,
    processing_uuid uuid,
    file_uuid uuid,
    file_name text,
    log_level text NOT NULL,
    component_name text NOT NULL,
    log_message text,
    inserted_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: ingestion_log_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

CREATE SEQUENCE edu_ai.ingestion_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ingestion_log_id_seq; Type: SEQUENCE OWNED BY; Schema: edu_ai; Owner: -
--

ALTER SEQUENCE edu_ai.ingestion_log_id_seq OWNED BY edu_ai.ingestion_log.id;


--
-- Name: langchain_pg_collection; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.langchain_pg_collection (
    name character varying,
    cmetadata json,
    uuid uuid NOT NULL
);


--
-- Name: langchain_pg_embedding; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.langchain_pg_embedding (
    collection_id uuid,
    embedding edu_ai.vector,
    document character varying,
    cmetadata json,
    custom_id character varying,
    uuid uuid NOT NULL
);


--
-- Name: qa_blocklist; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_blocklist (
    id bigint NOT NULL,
    term text NOT NULL,
    term_norm text NOT NULL,
    term_lemma text,
    category text NOT NULL,
    severity smallint DEFAULT 1 NOT NULL,
    is_phrase boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    lang text DEFAULT 'cs'::text NOT NULL,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT qa_blocklist_category_check CHECK ((category = ANY (ARRAY['profanity'::text, 'hate'::text, 'sexual'::text, 'self_harm'::text, 'violence'::text])))
);


--
-- Name: qa_blocklist_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

CREATE SEQUENCE edu_ai.qa_blocklist_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: qa_blocklist_id_seq; Type: SEQUENCE OWNED BY; Schema: edu_ai; Owner: -
--

ALTER SEQUENCE edu_ai.qa_blocklist_id_seq OWNED BY edu_ai.qa_blocklist.id;


--
-- Name: qa_query_log; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_query_log (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    conversation_id uuid,
    session_id text,
    user_id text,
    user_school_grade integer,
    channel text,
    raw_query text NOT NULL,
    norm_query text,
    lemma_query text,
    answer_source text,
    stage text,
    matched_entry_id uuid,
    match_score real,
    answer_md text,
    sources jsonb,
    strategies text[],
    answer_language text,
    latency_ms integer,
    setting_id uuid,
    stem_query text
);


--
-- Name: qa_conversation_v; Type: VIEW; Schema: edu_ai; Owner: -
--

CREATE VIEW edu_ai.qa_conversation_v AS
 SELECT conversation_id,
    min(created_at) AS started_at,
    max(created_at) AS last_activity_at,
    count(*) AS turn_count,
    max(user_id) AS user_id,
    max(user_school_grade) AS user_school_grade
   FROM edu_ai.qa_query_log
  WHERE (conversation_id IS NOT NULL)
  GROUP BY conversation_id;


--
-- Name: qa_embedding; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_embedding (
    variant_id bigint NOT NULL,
    entry_id uuid NOT NULL,
    embedding edu_ai.vector(1536),
    embedding_model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    processing_uuid uuid,
    file_uuid uuid
);


--
-- Name: qa_entries; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_entries (
    entry_id uuid NOT NULL,
    pack_id text NOT NULL,
    canonical_q text NOT NULL,
    answer_md text NOT NULL,
    asset_refs jsonb,
    tags jsonb,
    processing_uuid uuid,
    file_uuid uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qa_feedback; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_feedback (
    id bigint NOT NULL,
    log_id bigint,
    feedback text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qa_feedback_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.qa_feedback ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.qa_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: qa_pack_manifest; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_pack_manifest (
    pack_id text NOT NULL,
    subject text NOT NULL,
    grade integer,
    lang text NOT NULL,
    version integer NOT NULL,
    schema_version integer NOT NULL,
    built_at timestamp with time zone DEFAULT now() NOT NULL,
    entry_count integer NOT NULL,
    t_answer real NOT NULL,
    t_suggest real NOT NULL,
    m_margin real NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: qa_query_log_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.qa_query_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.qa_query_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: qa_stopword; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_stopword (
    term text NOT NULL,
    lang text DEFAULT 'cs'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qa_synonym; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_synonym (
    id bigint NOT NULL,
    group_id text NOT NULL,
    kind text NOT NULL,
    term text NOT NULL,
    term_norm text NOT NULL,
    term_lemma text NOT NULL,
    is_key boolean DEFAULT true NOT NULL,
    lang text DEFAULT 'cs'::text NOT NULL,
    source text DEFAULT 'seed'::text,
    is_active boolean DEFAULT true NOT NULL,
    term_stem text DEFAULT ''::text NOT NULL,
    setting_id uuid
);


--
-- Name: qa_synonym_id_seq; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.qa_synonym ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.qa_synonym_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: qa_variants; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.qa_variants (
    variant_id bigint NOT NULL,
    entry_id uuid NOT NULL,
    phrasing text NOT NULL,
    norm_text text NOT NULL,
    lemma_text text DEFAULT ''::text NOT NULL,
    stem_text text DEFAULT ''::text NOT NULL,
    ts_norm tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, norm_text)) STORED,
    ts_lemma tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, lemma_text)) STORED,
    ts_stem tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, stem_text)) STORED,
    setting_id uuid,
    processing_uuid uuid,
    file_uuid uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qa_variants_variant_id_seq1; Type: SEQUENCE; Schema: edu_ai; Owner: -
--

ALTER TABLE edu_ai.qa_variants ALTER COLUMN variant_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME edu_ai.qa_variants_variant_id_seq1
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: search_setting; Type: TABLE; Schema: edu_ai; Owner: -
--

CREATE TABLE edu_ai.search_setting (
    setting_id uuid DEFAULT gen_random_uuid() NOT NULL,
    label text NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    config jsonb NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: edu_ai_chunk_keywords id; Type: DEFAULT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_chunk_keywords ALTER COLUMN id SET DEFAULT nextval('edu_ai.edu_ai_chunk_keywords_id_seq'::regclass);


--
-- Name: edu_ai_document_tables id; Type: DEFAULT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_tables ALTER COLUMN id SET DEFAULT nextval('edu_ai.edu_ai_document_tables_id_seq'::regclass);


--
-- Name: ingestion_log id; Type: DEFAULT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.ingestion_log ALTER COLUMN id SET DEFAULT nextval('edu_ai.ingestion_log_id_seq'::regclass);


--
-- Name: qa_blocklist id; Type: DEFAULT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_blocklist ALTER COLUMN id SET DEFAULT nextval('edu_ai.qa_blocklist_id_seq'::regclass);


--
-- Name: edu_ai_chunk_keywords edu_ai_chunk_keywords_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_chunk_keywords
    ADD CONSTRAINT edu_ai_chunk_keywords_pkey PRIMARY KEY (id);


--
-- Name: edu_ai_document_chunks edu_ai_document_chunks_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_chunks
    ADD CONSTRAINT edu_ai_document_chunks_pkey PRIMARY KEY (id);


--
-- Name: edu_ai_document_elements edu_ai_document_elements_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_elements
    ADD CONSTRAINT edu_ai_document_elements_pkey PRIMARY KEY (id);


--
-- Name: edu_ai_document_metadata edu_ai_document_metadata_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_metadata
    ADD CONSTRAINT edu_ai_document_metadata_pkey PRIMARY KEY (file_uuid, processing_uuid);


--
-- Name: edu_ai_document_tables edu_ai_document_tables_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_tables
    ADD CONSTRAINT edu_ai_document_tables_pkey PRIMARY KEY (id);


--
-- Name: embedding_registry embedding_registry_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.embedding_registry
    ADD CONSTRAINT embedding_registry_pkey PRIMARY KEY (generation);


--
-- Name: ingestion_log ingestion_log_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.ingestion_log
    ADD CONSTRAINT ingestion_log_pkey PRIMARY KEY (id);


--
-- Name: langchain_pg_collection langchain_pg_collection_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.langchain_pg_collection
    ADD CONSTRAINT langchain_pg_collection_pkey PRIMARY KEY (uuid);


--
-- Name: langchain_pg_embedding langchain_pg_embedding_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_pkey PRIMARY KEY (uuid);


--
-- Name: qa_blocklist qa_blocklist_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_blocklist
    ADD CONSTRAINT qa_blocklist_pkey PRIMARY KEY (id);


--
-- Name: qa_embedding qa_embedding_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_embedding
    ADD CONSTRAINT qa_embedding_pkey PRIMARY KEY (variant_id);


--
-- Name: qa_entries qa_entries_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_entries
    ADD CONSTRAINT qa_entries_pkey PRIMARY KEY (entry_id);


--
-- Name: qa_feedback qa_feedback_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_feedback
    ADD CONSTRAINT qa_feedback_pkey PRIMARY KEY (id);


--
-- Name: qa_query_log qa_query_log_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_query_log
    ADD CONSTRAINT qa_query_log_pkey PRIMARY KEY (id);


--
-- Name: qa_stopword qa_stopword_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_stopword
    ADD CONSTRAINT qa_stopword_pkey PRIMARY KEY (lang, term);


--
-- Name: qa_synonym qa_synonym_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_synonym
    ADD CONSTRAINT qa_synonym_pkey PRIMARY KEY (id);


--
-- Name: qa_variants qa_variants_pkey1; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_variants
    ADD CONSTRAINT qa_variants_pkey1 PRIMARY KEY (variant_id);


--
-- Name: search_setting search_setting_pkey; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.search_setting
    ADD CONSTRAINT search_setting_pkey PRIMARY KEY (setting_id);


--
-- Name: edu_ai_chunk_keywords uq_chunk_keywords; Type: CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_chunk_keywords
    ADD CONSTRAINT uq_chunk_keywords UNIQUE (file_item_uuid, chunk_index);


--
-- Name: chunks_setting_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX chunks_setting_idx ON edu_ai.edu_ai_document_chunks USING btree (setting_id);


--
-- Name: chunks_ts_lemma_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX chunks_ts_lemma_gin ON edu_ai.edu_ai_document_chunks USING gin (ts_lemma);


--
-- Name: chunks_ts_norm_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX chunks_ts_norm_gin ON edu_ai.edu_ai_document_chunks USING gin (ts_norm);


--
-- Name: chunks_ts_stem_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX chunks_ts_stem_gin ON edu_ai.edu_ai_document_chunks USING gin (ts_stem);


--
-- Name: idx_document_chunks_embedding_hnsw; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_document_chunks_embedding_hnsw ON edu_ai.edu_ai_document_chunks USING hnsw (embedding edu_ai.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_document_metadata_is_active; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_document_metadata_is_active ON edu_ai.edu_ai_document_metadata USING btree (is_active) WHERE (is_active = true);


--
-- Name: idx_elements_file_item_uuid; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_elements_file_item_uuid ON edu_ai.edu_ai_document_elements USING btree (file_item_uuid);


--
-- Name: idx_elements_file_uuid; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_elements_file_uuid ON edu_ai.edu_ai_document_elements USING btree (file_uuid);


--
-- Name: idx_elements_page_number; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_elements_page_number ON edu_ai.edu_ai_document_elements USING btree (page_number);


--
-- Name: idx_elements_processing_uuid; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_elements_processing_uuid ON edu_ai.edu_ai_document_elements USING btree (processing_uuid);


--
-- Name: idx_elements_record_type; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_elements_record_type ON edu_ai.edu_ai_document_elements USING btree (record_type);


--
-- Name: idx_ingestion_log_file_uuid; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_ingestion_log_file_uuid ON edu_ai.ingestion_log USING btree (file_uuid);


--
-- Name: idx_ingestion_log_level; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_ingestion_log_level ON edu_ai.ingestion_log USING btree (log_level);


--
-- Name: idx_ingestion_log_processing_uuid; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_ingestion_log_processing_uuid ON edu_ai.ingestion_log USING btree (processing_uuid);


--
-- Name: idx_ingestion_log_timestamp; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_ingestion_log_timestamp ON edu_ai.ingestion_log USING btree (log_timestamp);


--
-- Name: idx_tables_cluster_only; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX idx_tables_cluster_only ON edu_ai.edu_ai_document_tables USING btree (tbl_cluster_signature) WHERE (record_type = 'table'::text);


--
-- Name: qa_blocklist_lemma_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_blocklist_lemma_idx ON edu_ai.qa_blocklist USING btree (term_lemma) WHERE is_active;


--
-- Name: qa_blocklist_norm_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_blocklist_norm_idx ON edu_ai.qa_blocklist USING btree (term_norm) WHERE is_active;


--
-- Name: qa_embedding_entry_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_embedding_entry_idx ON edu_ai.qa_embedding USING btree (entry_id);


--
-- Name: qa_embedding_hnsw_active; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_embedding_hnsw_active ON edu_ai.qa_embedding USING hnsw (embedding edu_ai.vector_cosine_ops) WHERE is_active;


--
-- Name: qa_entries_active_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_entries_active_idx ON edu_ai.qa_entries USING btree (is_active);


--
-- Name: qa_entries_file_uuid_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_entries_file_uuid_idx ON edu_ai.qa_entries USING btree (file_uuid);


--
-- Name: qa_entries_pack_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_entries_pack_idx ON edu_ai.qa_entries USING btree (pack_id);


--
-- Name: qa_pack_manifest_active_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_pack_manifest_active_idx ON edu_ai.qa_pack_manifest USING btree (is_active) WHERE is_active;


--
-- Name: qa_query_log_conv_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_query_log_conv_idx ON edu_ai.qa_query_log USING btree (conversation_id, created_at);


--
-- Name: qa_query_log_created_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_query_log_created_idx ON edu_ai.qa_query_log USING btree (created_at);


--
-- Name: qa_query_log_setting_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_query_log_setting_idx ON edu_ai.qa_query_log USING btree (setting_id);


--
-- Name: qa_query_log_source_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_query_log_source_idx ON edu_ai.qa_query_log USING btree (answer_source, stage);


--
-- Name: qa_stopword_active_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_stopword_active_idx ON edu_ai.qa_stopword USING btree (lang, is_active);


--
-- Name: qa_synonym_group_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_synonym_group_idx ON edu_ai.qa_synonym USING btree (group_id);


--
-- Name: qa_synonym_lemma_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_synonym_lemma_idx ON edu_ai.qa_synonym USING btree (term_lemma);


--
-- Name: qa_variants_entry_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_entry_idx ON edu_ai.qa_variants USING btree (entry_id);


--
-- Name: qa_variants_norm_trgm; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_norm_trgm ON edu_ai.qa_variants USING gin (norm_text edu_ai.gin_trgm_ops);


--
-- Name: qa_variants_setting_idx; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_setting_idx ON edu_ai.qa_variants USING btree (setting_id);


--
-- Name: qa_variants_ts_lemma_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_ts_lemma_gin ON edu_ai.qa_variants USING gin (ts_lemma);


--
-- Name: qa_variants_ts_norm_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_ts_norm_gin ON edu_ai.qa_variants USING gin (ts_norm);


--
-- Name: qa_variants_ts_stem_gin; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE INDEX qa_variants_ts_stem_gin ON edu_ai.qa_variants USING gin (ts_stem);


--
-- Name: search_setting_one_active; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE UNIQUE INDEX search_setting_one_active ON edu_ai.search_setting USING btree (is_active) WHERE is_active;


--
-- Name: uq_embedding_registry_one_active; Type: INDEX; Schema: edu_ai; Owner: -
--

CREATE UNIQUE INDEX uq_embedding_registry_one_active ON edu_ai.embedding_registry USING btree ((true)) WHERE (status = 'active'::text);


--
-- Name: edu_ai_document_chunks edu_ai_document_chunks_setting_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.edu_ai_document_chunks
    ADD CONSTRAINT edu_ai_document_chunks_setting_id_fkey FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting(setting_id) ON UPDATE CASCADE;


--
-- Name: langchain_pg_embedding langchain_pg_embedding_collection_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES edu_ai.langchain_pg_collection(uuid) ON DELETE CASCADE;


--
-- Name: qa_embedding qa_embedding_variant_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_embedding
    ADD CONSTRAINT qa_embedding_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES edu_ai.qa_variants(variant_id) ON DELETE CASCADE;


--
-- Name: qa_feedback qa_feedback_log_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_feedback
    ADD CONSTRAINT qa_feedback_log_id_fkey FOREIGN KEY (log_id) REFERENCES edu_ai.qa_query_log(id);


--
-- Name: qa_query_log qa_query_log_setting_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_query_log
    ADD CONSTRAINT qa_query_log_setting_id_fkey FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting(setting_id) ON UPDATE CASCADE;


--
-- Name: qa_synonym qa_synonym_setting_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_synonym
    ADD CONSTRAINT qa_synonym_setting_id_fkey FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting(setting_id) ON UPDATE CASCADE;


--
-- Name: qa_variants qa_variants_entry_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_variants
    ADD CONSTRAINT qa_variants_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES edu_ai.qa_entries(entry_id) ON DELETE CASCADE;


--
-- Name: qa_variants qa_variants_setting_id_fkey; Type: FK CONSTRAINT; Schema: edu_ai; Owner: -
--

ALTER TABLE ONLY edu_ai.qa_variants
    ADD CONSTRAINT qa_variants_setting_id_fkey FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting(setting_id) ON UPDATE CASCADE;


--
-- PostgreSQL database dump complete
--


-- ═══ SEED: konfigurační číselníky ═══
--
-- PostgreSQL database dump
--

-- Dumped from database version 16.3 (Debian 16.3-1.pgdg120+1)
-- Dumped by pg_dump version 16.3 (Debian 16.3-1.pgdg120+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: embedding_registry; Type: TABLE DATA; Schema: edu_ai; Owner: -
--

INSERT INTO edu_ai.embedding_registry (generation, embedding_model, dimension, vector_type, target_table, status, created_at, activated_at, notes) VALUES (1, 'text-embedding-3-small', 1536, 'vector', 'edu_ai.edu_ai_document_chunks', 'active', '2026-07-08 06:02:01.152465+00', '2026-07-08 06:02:01.152465+00', 'initial generation; OpenAI hosted; baseline for the §4.6 bake-off');


--
-- Data for Name: qa_blocklist; Type: TABLE DATA; Schema: edu_ai; Owner: -
--

INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (1, 'kokot', 'kokot', 'kokot', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (2, 'kunda', 'kunda', 'kunda', 'profanity', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (3, 'píča', 'pica', 'pica', 'profanity', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (4, 'píčo', 'pico', 'pica', 'profanity', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (5, 'kurva', 'kurva', 'kurva', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (6, 'čurák', 'curak', 'curak', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (7, 'debil', 'debil', 'debil', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (8, 'idiot', 'idiot', 'idiot', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (9, 'kretén', 'kreten', 'kreten', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (10, 'hovno', 'hovno', 'hovno', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (11, 'sráč', 'srac', 'srac', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (12, 'mrdka', 'mrdka', 'mrdka', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (13, 'zmrd', 'zmrd', 'zmrd', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (14, 'hajzl', 'hajzl', 'hajzl', 'profanity', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (15, 'prcat', 'prcat', 'prcat', 'profanity', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (16, 'šoustat', 'soustat', 'soustat', 'sexual', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (17, 'mrdat', 'mrdat', 'mrdat', 'sexual', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (18, 'mrdám', 'mrdam', 'mrdat', 'sexual', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (19, 'penis', 'penis', 'penis', 'sexual', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (20, 'vagína', 'vagina', 'vagina', 'sexual', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (21, 'porno', 'porno', 'porno', 'sexual', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (22, 'sex', 'sex', 'sex', 'sexual', 1, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (23, 'cikán', 'cikan', 'cikan', 'hate', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (24, 'cikáni', 'cikani', 'cikan', 'hate', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (25, 'negr', 'negr', 'negr', 'hate', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (26, 'buzna', 'buzna', 'buzna', 'hate', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (27, 'buzerant', 'buzerant', 'buzerant', 'hate', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (28, 'žid', 'zid', 'zid', 'hate', 2, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (29, 'sebevražda', 'sebevrazda', 'sebevrazda', 'self_harm', 3, false, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (30, 'zabít se', 'zabit se', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (31, 'zabiju se', 'zabiju se', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (32, 'chci umřít', 'chci umrit', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (33, 'nechci žít', 'nechci zit', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (34, 'ublížit si', 'ublizit si', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (35, 'podřezat si', 'podrezat si', NULL, 'self_harm', 3, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (36, 'řezat se', 'rezat se', NULL, 'self_harm', 2, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');
INSERT INTO edu_ai.qa_blocklist (id, term, term_norm, term_lemma, category, severity, is_phrase, is_active, lang, source, created_at) VALUES (37, 'skočit z', 'skocit z', NULL, 'self_harm', 2, true, true, 'cs', 'seed', '2026-07-12 17:05:55.614971+00');


--
-- Data for Name: qa_stopword; Type: TABLE DATA; Schema: edu_ai; Owner: -
--

INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('a', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('aby', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ale', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ani', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ano', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('asi', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('avsak', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('behem', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bez', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bude', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('budeme', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('budes', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('budete', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('budou', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('budu', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('by', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bych', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bychom', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byl', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byla', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byli', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bylo', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byly', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('bys', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byste', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('byt', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ci', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('do', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ho', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('i', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ja', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('je', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jeho', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jej', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jeji', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jelikoz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jen', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jenom', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jeste', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ji', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jich', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jim', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jimi', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jiz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jsem', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jsi', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jsme', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jsou', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('jste', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('k', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('kazdy', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('kdyz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ke', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('kolem', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('krom', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('kvuli', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('me', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('meho', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('mezi', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('mi', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('mne', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('moje', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('muj', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('my', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('mych', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('na', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nad', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nam', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nas', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ne', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nebo', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nebot', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nejaka', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('nejaky', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('no', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('o', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('od', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('okolo', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('on', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ona', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('oni', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ono', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ony', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('pak', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('po', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('pod', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('podle', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('prave', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('prece', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('pred', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('pri', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('pro', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('proto', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('protoze', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('s', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('sam', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('sama', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('samo', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('se', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('sem', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('si', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('skrz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('sme', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('snad', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('sve', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('svuj', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('svych', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('svym', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ta', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tady', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tak', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('take', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('takze', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tam', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tato', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('te', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tebe', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tech', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ted', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tedy', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ten', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tento', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('teto', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tez', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('the', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ti', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tim', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('to', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tobe', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tom', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tomto', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('totiz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('toto', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tu', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tve', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tvuj', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ty', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('tyto', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('u', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('uz', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('v', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vam', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vas', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ve', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vedle', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vlastne', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vsak', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vse', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vsechno', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vuci', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('vy', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('z', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('za', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('zde', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');
INSERT INTO edu_ai.qa_stopword (term, lang, is_active, note, created_at) VALUES ('ze', 'cs', true, NULL, '2026-07-14 12:37:34.165177+00');


--
-- Data for Name: search_setting; Type: TABLE DATA; Schema: edu_ai; Owner: -
--

INSERT INTO edu_ai.search_setting (setting_id, label, is_active, config, notes, created_at) VALUES ('cf9332ea-e566-45ab-9f43-bd5d4bc670ef', 'v2 test: form=[norm,lemma,stem] (all forms, no re-ingest)', false, '{"engines": [{"kind": "lemmatizer", "name": "simplemma", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "lemma_text"}, {"kind": "stemmer", "name": "dolamic_savoy_light", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "stem_text", "params": {"min_len": 3, "aggressive": false}}], "pipeline": [{"step": "route", "enabled": true}, {"step": "qa_exact", "on_hit": "stop", "enabled": true}, {"step": "qa_lexical", "on_hit": "stop", "params": {"form": ["norm", "lemma", "stem"], "fetch_k": 50, "synonyms": true, "suggest_top_n": 3, "trgm_suggest_min": 0.40}, "enabled": true}, {"step": "rag_lexical", "on_hit": "continue", "params": {"form": ["norm", "lemma", "stem"], "fetch_k": 30}, "enabled": true}, {"step": "qa_vector", "on_hit": "stop", "params": {"cosine_min": 0.75}, "enabled": true}, {"step": "rag_vector", "on_hit": "continue", "params": {"top_k": 5, "fetch_k": 30}, "enabled": true}, {"step": "answer", "enabled": true}], "tokenization": {"active": true}, "normalization": {"lowercase": true, "drop_stopwords": true, "strip_diacritics": true}}', 'form-switch demo', '2026-07-14 11:58:51.473242+00');
INSERT INTO edu_ai.search_setting (setting_id, label, is_active, config, notes, created_at) VALUES ('a66b4bd9-8d4e-4d34-beee-9b59b92ccae0', 'v1 baseline: norm + lemma(simplemma) + stem(dolamic-savoy light)', false, '{"engines": [{"kind": "lemmatizer", "name": "simplemma", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "lemma_text"}, {"kind": "stemmer", "name": "dolamic_savoy_light", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "stem_text", "params": {"min_len": 3, "aggressive": false}}], "pipeline": [{"step": "route", "enabled": true}, {"step": "qa_exact", "on_hit": "stop", "enabled": true}, {"step": "qa_lexical", "on_hit": "stop", "params": {"form": ["norm", "lemma", "stem"], "fetch_k": 50, "synonyms": true, "suggest_top_n": 3, "trgm_suggest_min": 0.40}, "enabled": true}, {"step": "rag_lexical", "on_hit": "continue", "params": {"form": ["norm", "lemma", "stem"], "fetch_k": 30}, "enabled": true}, {"step": "qa_vector", "on_hit": "stop", "params": {"cosine_min": 0.75}, "enabled": true}, {"step": "rag_vector", "on_hit": "continue", "params": {"top_k": 5, "fetch_k": 30}, "enabled": true}, {"step": "answer", "enabled": true}], "tokenization": {"active": true}, "normalization": {"lowercase": true, "drop_stopwords": true, "strip_diacritics": true}}', 'Reproduces the pre-config cascade exactly. Thresholds mirror qa_pack_manifest (0.60/0.30/0.08).', '2026-07-14 11:33:12.411109+00');
INSERT INTO edu_ai.search_setting (setting_id, label, is_active, config, notes, created_at) VALUES ('73c9a3e2-d737-4748-8149-3a2957a91d6f', 'v3: QA-first + sequential RAG fallback (lexical→keywords→vector)', true, '{"engines": [{"kind": "lemmatizer", "name": "simplemma", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "lemma_text"}, {"kind": "stemmer", "name": "dolamic_savoy_light", "active": {"ingest_qa": true, "ingest_docs": true}, "column": "stem_text", "params": {"min_len": 3, "aggressive": false}}], "pipeline": [{"note": "Deterministic guard (usable? + qa_blocklist; self_harm->Linka bezpeci 116 111) and QA pack scope by grade. No LLM, no retrieval - just admit/deny + which packs to search.", "step": "route", "enabled": true}, {"note": "Exact curated Q match. Whole QA block runs before any document retrieval.", "step": "qa_exact", "on_hit": "stop", "enabled": true}, {"step": "qa_lexical", "on_hit": "stop", "params": {"form": ["norm", "lemma", "stem"], "fetch_k": 50, "synonyms": true, "suggest_top_n": 3, "trgm_suggest_min": 0.40}, "enabled": true}, {"note": "Document chunks by fulltext on ts_lemma||ts_stem (reduced forms).", "step": "rag_lexical", "on_hit": "continue", "params": {"form": ["lemma", "stem"], "fetch_k": 30, "min_coverage": 0.5}, "enabled": true}, {"note": "Fallback: match query tokens against edu_ai_chunk_keywords.keywords[] (reduced same way). Runs only if rag_lexical was empty.", "step": "rag_keywords", "on_hit": "continue", "params": {"form": ["lemma", "stem"], "fetch_k": 30, "min_overlap": 1}, "enabled": true, "only_if_empty": true}, {"note": "Curated QA vector. Ends the QA block; documents only if QA found nothing.", "step": "qa_vector", "on_hit": "stop", "params": {"cosine_min": 0.75}, "enabled": true}, {"note": "Last resort: semantic neighbours over document_chunks.embedding. cosine_min drops off-topic so the pool stays empty -> no_answer.", "step": "rag_vector", "on_hit": "continue", "params": {"top_k": 5, "fetch_k": 30, "cosine_min": 0.65}, "enabled": true, "only_if_empty": true}, {"note": "Single terminal LLM. Empty pool -> no_answer (no LLM call).", "step": "answer", "enabled": true}], "tokenization": {"active": true}, "normalization": {"lowercase": true, "drop_stopwords": true, "strip_diacritics": true}}', 'Document RAG becomes a real fallback chain (only_if_empty) with a vector relevance gate (cosine_min). cosine_min uncalibrated.', '2026-07-14 21:23:49.18734+00');


--
-- Name: qa_blocklist_id_seq; Type: SEQUENCE SET; Schema: edu_ai; Owner: -
--

SELECT pg_catalog.setval('edu_ai.qa_blocklist_id_seq', 37, true);


--
-- PostgreSQL database dump complete
--

