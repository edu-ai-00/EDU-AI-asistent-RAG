CREATE TABLE pack_manifest (
    pack_id TEXT NOT NULL, subject TEXT, grade INTEGER, lang TEXT NOT NULL,
    version INTEGER NOT NULL, schema_version INTEGER NOT NULL, built_at TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    t_answer REAL NOT NULL, t_suggest REAL NOT NULL, m_margin REAL NOT NULL,
    processing_uuid TEXT, file_uuid TEXT,
    school_grade_ids TEXT, school_level TEXT, school_subject_ids TEXT,
    domain_ids TEXT, binding_id INTEGER,
    legal_entity_code TEXT NOT NULL, visibility TEXT NOT NULL, owner_user_id TEXT,
    audience_roles TEXT, valid_from TEXT, valid_to TEXT,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)));

CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY, canonical_q TEXT NOT NULL, answer_md TEXT NOT NULL,
    asset_refs TEXT, tags TEXT,
    entry_key TEXT, category_id INTEGER,
    school_grade_ids TEXT, school_level TEXT, school_subject_ids TEXT,
    domain_ids TEXT, binding_id INTEGER,
    legal_entity_code TEXT NOT NULL, visibility TEXT NOT NULL, owner_user_id TEXT,
    audience_roles TEXT, valid_from TEXT, valid_to TEXT,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    processing_uuid TEXT, file_uuid TEXT, created_at TEXT);

CREATE TABLE variants (
    variant_id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id),
    phrasing TEXT NOT NULL, norm_text TEXT NOT NULL, lemma_text TEXT NOT NULL,
    stem_text TEXT NOT NULL,
    school_grade_ids TEXT, school_level TEXT, school_subject_ids TEXT,
    domain_ids TEXT, binding_id INTEGER,
    legal_entity_code TEXT NOT NULL, visibility TEXT NOT NULL, owner_user_id TEXT,
    audience_roles TEXT, valid_from TEXT, valid_to TEXT,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    setting_id TEXT, processing_uuid TEXT, file_uuid TEXT, created_at TEXT);

CREATE VIRTUAL TABLE variants_fts USING fts5(
    norm_text, lemma_text, content='variants', content_rowid='variant_id',
    tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE variants_tri USING fts5(
    norm_text, content='variants', content_rowid='variant_id', tokenize='trigram');
