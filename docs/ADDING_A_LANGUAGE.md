---
id: docs.adding-a-language
status: current
components: [normalization, code-lists, prompts]
sources:
  - search/app/languages.py
  - search/app/text_norm.py
  - search/app/messages.py
  - search/eduai_normalize/eduai_normalize/normalize.py
  - search/eduai_normalize/eduai_normalize/stemmer.py
  - search/eduai_normalize/eduai_normalize/lemmatizer.py
  - contracts/parity/
  - postgres/init_clean/02_seed_config.sql
last_verified: 2026-08-28
---

# Adding a language to edu_ai (worked example: German)

*Written 2026-07-28 after the CS+EN bilingual rollout; rewritten 2026-07-29 for
the language REGISTRY, config-driven reducers and langdetect. Verified against the
live local stack (Langflow 1.10.1, `eduai_normalize` 1.5.0) by activating German +
Spanish and deactivating Czech end to end.*

> **Step 0, before anything else: insert the row into `code_list('language')`.**
> That code list is the single registry of languages — the search API, the web chat
> and the ingestion API all read the active set from it at runtime, and
> `detect_lang` may only return a language listed there. Insert it
> **`is_active = false`**, fill in the layers below, and flip it to active LAST;
> every consumer filters on `is_active`, so a half-prepared language stays
> invisible instead of half-working.

Two distinct scenarios, and the difference matters:

| | Scenario | Effort |
|---|---|---|
| **A** | **ADD** German as a third language next to cs+en | the checklist below |
| **B** | **REPLACE** Czech with German (a German-school deployment) | the same checklist, plus §7 (retiring cs) |

The architecture is deliberately language-parametric — most work is DATA
(code lists), not code. Nothing needs installing: both reduction engines already
ship German in our images (verified: simplemma `de` dictionary 2.2 MB,
snowballstemmer has `german` among its 36 algorithms).

---

## 1. What needs NO change (already language-agnostic)

- **DB schema** — every language-bearing table already carries a `lang` column
  (`qa_stopword`, `qa_synonym`, `qa_blocklist`, `qa_pack_manifest`,
  `system_message`, `prompt_registry`), and `code_list` is a generic code list.
- **Chunk/QA storage** — `chunk_norm/lemma/stem_text` + `ts_*` are built with
  the `simple` Postgres config, i.e. no language logic in the index; the tokens
  arrive already reduced. `langdetect_result` records which language did it.
- **Pipeline plumbing** — the `language` filter on `/search`, `pack_filter`,
  the rag_* metadata JOIN, per-request language resolution in `prepare()`,
  the two-field metadata pattern, the answer-language injection.
- **Embeddings + answer LLM** — `text-embedding-3-small` and the answer model
  are multilingual; the answer language is instructed deterministically.
- **Ingestion flows** — ChunkLemmatizer resolves the language PER CHUNK (user
  code > the row's own text > the batch sample > the registry's
  `is_processing_fallback`) and reduces with it, recording where the answer came
  from in `langdetect_result_score` (1.0 user / 0.8 row / 0.6 batch / 0.4
  registry fallback — query the 0.4 rows to find documents whose language should
  be supplied by hand).
- **Reducer selection** — which stemmer/lemmatizer implements a language is DATA
  in `settings.config.engines`, so this is a config row, not a code change
  (see §2).

## 1b. Language DETECTION — where langdetect lives

One detector, in one place, used by every side. `eduai_normalize.detect_lang`
is the only entry point; nothing else may detect a language.

| Where | What it does |
|---|---|
| `eduai_normalize/normalize.py` — `_langdetect_probe` + `detect_lang` | THE detector. langdetect first (restricted to the registry's active codes, probability >= `_LANGDETECT_MIN_PROB` = 0.70), then a simplemma dictionary vote (margin >= 2) for fragments, then `None`. |
| `eduai_normalize/pyproject.toml` | declares `langdetect==1.0.9`, which is why BOTH images have it. Do NOT add it per image — one declaration is what guarantees the ingest side and the query side detect identically. |
| `search/app/nodes/prepare.py` | query side: `/search`'s `language` filter wins, else `detect_lang`; `None` → the query is reduced in EVERY active language and the tokens unioned. |
| flow `ChunkLemmatizer-1-MD` (tables) + `-2-MD` (text) | index side, per chunk row. Both branches, same function. |
| flow `LanguageSegmenter-MD` | per LINE, to find where a document changes language (§1c). |
| flow `ParseDocumentMetadataJSON-1UQ97-MD` | the document-level `document_language_llm`. |
| flow `QAJsonReader-qa001-QA` | the QA pack's language (decided once, consumed by `ChunkLemmatizer-qa003-QA`). |
| flow `PageTextChunkDecisionValidator-S6zLX-MD` | ⚠️ the ONE place that calls `langdetect` **directly**, because it needs the raw probability as a quality gate (`min_lang_score` 0.55 — drop text too incoherent to chunk), which `detect_lang` does not return. It does **not** pin the seed, so its gate can wobble on short text. Unify it behind a shared helper returning (lang, prob) when you touch it. |

**The seed.** langdetect is *not* deterministic by default: `detector.py` runs
`n_trial = 7` passes, perturbs its smoothing alpha with `random.gauss` and picks
n-grams with `random.choice`, seeded from system entropy because
`DetectorFactory.seed` is `None`. Long text converges anyway; short text can
flip. `normalize.py` pins `DetectorFactory.seed = 0` at call time — that is the
only reason this is usable for us, since index and query must resolve the same
text to the same language or the stored tokens stop matching.

**What a new language needs from the detector: nothing.** langdetect ships 55
languages and `allowed` comes from the registry, so activating a code list row is
enough. It abstains rather than guessing, and an abstention is handled (ingest
falls back and records 0.4; the query unions languages).

**The device (Dart) is the open question.** The offline engine reproduces
`eduai_normalize` to reduce query tokens on-device, and langdetect has no Dart
equivalent we ship. Either the device sends `language` explicitly, or it lets the
server decide. Do not assume the device can reproduce the detector.

## 1c. Mixed-language documents (`LanguageSegmenter`)

A document that switches language mid-file cannot be handled by per-chunk
detection alone: detection is only ever as fine-grained as the chunker's
boundaries, and the LLM chunker happily put a German and a Spanish paragraph in
one 690-char chunk (2026-07-29), which then got ONE language and stemmed the
Spanish half as German.

`LanguageSegmenter-MD` sits before `DataFrameRecursiveTextSplitter` and emits one
row per language-homogeneous segment, so the chunker never sees mixed input.
Points that matter when adding a language:

- The unit is a **line**, not a paragraph: text extracted from a PDF has only
  single newlines, no blank lines at all. Same-language lines merge back, so
  blank-line markdown behaves the same.
- Undecidable lines **inherit** from their neighbours; a language nothing can
  decide never starts a segment.
- `min_segment_chars = 40` is measured on 1042 real paragraphs (folds 37 % of
  headings, wrongly folds 2.3 % of real paragraphs; 80 cost 12 %). Re-measure for
  a corpus with different typography.
- A **monolingual** document yields exactly one segment holding its whole text,
  so its chunks are unchanged — only documents that really switch are affected.
- `chunk_index` is renumbered continuously per `file_item_uuid` in the splitter,
  because segments would otherwise each start at 0.
- Only the TEXT branch is segmented. Splitting a TABLE by the language of its
  rows would sever the row↔header relation; tables rely on per-row detection.

## 2. Layer 1 — the shared library `search/eduai_normalize/`

The only place with real per-language algorithms — and for a language that
Snowball and simplemma already cover, **there is nothing to edit here**.

| File | State |
|---|---|
| `eduai_normalize/stemmer.py` | **Already generalized.** The Snowball branch resolves `snowball_<language>` from the configured engine name through a per-language singleton cache, so any of snowballstemmer's ~30 languages is a NAME in configuration, not code. Czech deliberately stays on the hand-written light stemmer, which measured better on our corpus than Snowball Czech. |
| `eduai_normalize/lemmatizer.py` | Nothing structural — lemmatization already accepts any simplemma language. |
| `eduai_normalize/normalize.py` | **Already rewritten.** Detection is langdetect, restricted to the registry's active codes, with a simplemma dictionary vote for fragments and abstention when neither decides (§1b). The old two-way Czech-versus-English marker score is gone. There is no built-in stop-word list: normalisation requires the supplied map; an absent map raises ValueError and a missing language in an existing map uses an empty list. Supply the real list from `qa_stopword`. |
| `pyproject.toml` | Bump the version only if you actually changed the library. |

So the library is a **zero-hour item** for a language Snowball already covers.
Code is needed only for a genuinely new *algorithm*.

⚠️ **Parity rule:** any change here changes the stored tokens. Regenerate the
parity vectors (`contracts/parity/`) and **re-ingest documents of the
affected languages** — old rows keep tokens reduced by the old rules.

## 3. Layer 2 — code lists (pure data, migrations)

| Table | Rows to add |
|---|---|
| `code_list` (`list_code='language'`) | `de` / `Němčina` / `German` — activates it for ingest validation, the `/search` filter and select menus |
| `qa_stopword` | German stoplist (`lang='de'`, ~100 rows). Do NOT include interrogatives (`was/wann/wie/wo/warum/welche`) — same design as cs/en |
| `qa_synonym` | interrogative group `kind='interrogative'` for German wh-words (excluded from QA coverage), plus any domain entity groups |
| `qa_blocklist` | German profanity / self-harm / hate / sexual terms. ⚠️ phrase `term_norm` must be stored **pipeline-normalized** (stopwords stripped) — see the comment in migration 35 |
| `system_message` | all active message keys × `de`; the current seed keys are listed below. **self_harm must not be a translation** — a German deployment needs a German helpline number, not Linka bezpečí |
| `settings.config.engines` | two more entries `{"kind":"lemmatizer","name":"simplemma","language":"de"}` and `{"kind":"stemmer","name":"snowball_german","language":"de"}` with `active: {ingest_qa: true, ingest_docs: true}` |
| `taxonomy_rules`, `code_list` labels | see §5 — this is where the current design does not scale |

## 4. Layer 3 — prompts (`prompt_registry`)

Some active prompts still name Czech explicitly and must be reviewed. Find them
rather than trusting this list, because prompts are versioned and the set moves:

```sql
SELECT prompt_key, version FROM edu_ai.prompt_registry
 WHERE is_active AND prompt_text ~* 'czech' ORDER BY 1;
```

At the time of writing that returns `chunk_enrichment` and
`table_metadata_extraction` — both are ingestion prompts asked to work "in
Czech". `search_answer_generation` and `document_metadata_extraction` are already
language-neutral; the answering prompt gets its language from a deterministic
injection rather than from the prompt text.

Fix by inserting a new version and deactivating the old one — no code change, and
it takes effect on the next request.

## 5. Layer 4 — labels (DONE, nothing to do)

`taxonomy_rules` and `code_list` used to carry fixed `label_cs` +
`label_en` columns, which made every new language a DDL change. Migration 40
replaced them with a single `labels jsonb` and migration 41 dropped the old
columns, so a language is now:

```sql
UPDATE edu_ai.code_list SET labels = labels || '{"de": "Deutsch"}' WHERE …;
```

No DDL, no code. The two Search API endpoints and both test pages read `labels`.
The classification component reads `labels->>'en'` on purpose — the classification
prompt is English, which keeps it independent of the document's language.

## 6. Layer 5 — remaining code touch points

| Where | What |
|---|---|
| `settings.config.engines` | the reducer for the language — **data, not code**: `{"kind":"stemmer","name":"snowball_german","language":"de","params":{"min_len":3}}` + a `simplemma` lemmatizer row. `snowball_<language>` is parsed by the library, so any of snowballstemmer's ~30 languages works with no code at all (verified with German and Spanish). Only a genuinely new ALGORITHM needs code. |
| `api/app/main.py` | `_CZ_LETTERS` scoring in HTML **charset** detection (`decode_html`) — byte encoding, not content language; add the language's accented letters so cp1252 pages decode. |
| `contracts/parity/` | generate `<lang>-normalization-vectors.json` and keep `check_vectors.py` passing. Run it BEFORE and AFTER any change to reduction: drift there means every stored row for that language needs re-ingesting. |
| mobile client | ships the simplemma dictionary for on-device reduction → must ship the new one too, and see the Dart caveat in §1b. |

### 6b. Presentation layer — legitimate Czech, but Czech-anchored

These are **display** decisions, not retrieval ones. A canned message has to be in
*some* language even when nothing resolved one, so a fallback here is legitimate —
unlike on the reduction path, where a fallback silently picks the wrong stemmer.
Still Czech-anchored today, so check them when Czech stops being the house
language:

| Where | What to change |
|---|---|
| `system_message` code list | needs ALL active keys × the new language; the current seed contains 13 keys listed below. **`guard_self_harm` must not be a translation** — a German deployment needs a German helpline, not Linka bezpečí. Missing rows try the registry's UI-default language. Only five keys have a code fallback; other missing keys may return an empty string. Do not assume every missing message becomes Czech text. |
| `search/app/messages.py` | the last literal `"cs"` in the service, deliberately: the guard must never crash, so if the registry read fails it degrades to Czech rather than raising. Change the constant when Czech is retired. |
| `web_chat/web_chat.py` | error strings ("Odpověď nyní trvá příliš dlouho…") are hardcoded Czech in code, bypassing `system_message`. Move them to the code list. |
| `web_chat/frontend/index.html`, `tools/*.html` | `<html lang="cs">` and the UI labels. The web chat also does not send `language` at all, so it searches across every language — see the separate web-chat task. |
| `tools/*.html` — `pickLabel` | fallback chain UI language → `en` → `cs` → code. Harmless, but the `cs` tail is an assumption. |
| `search/app/languages.py` — `ui_default_language()` | the ONLY sanctioned reader of `is_ui_default`. If a processing path ever reaches for it, that is the bug this split exists to prevent. |

## 7. Scenario B extras — replacing Czech with German

1. `code_list`: set `cs` `is_active=false` (keeps history, removes it from
   filters, validation and menus) — do **not** delete the rows.
2. Set the intended registry `is_ui_default` and `is_processing_fallback` fields for the target language; there is no current `is_default` field.
3. Historical, unimplemented proposal: introducing a new `DEFAULT_LANGUAGE` flag and refactoring code is not a current configuration step. Use the registry UI-default and processing-fallback fields; a new flag requires a separately accepted design.
4. `qa_pack_manifest`: build/upload a `de-core` pack; deactivate `cs-core`
   (per-pack deactivation is already scoped, so uploads won't collide).
5. Re-ingest all documents (Czech-reduced tokens are useless for a German
   corpus) or archive them.
6. Guard messages: the German self-harm message MUST carry a locally valid
   contact valid for the deployment country and audience; verify local coverage instead of assuming a number works throughout the EU.

## 8. Checklist (order matters)

Verified end to end on 2026-07-29 by activating German + Spanish, deactivating
Czech, and ingesting German / Spanish / bilingual PDFs.

1. [ ] `code_list('language')`: INSERT the row **`is_active = false`** — the
       registry entry comes FIRST, everything else hangs off it
2. [ ] `labels` on `code_list` + `taxonomy_rules`: `labels || '{"de": …}'`
3. [ ] `settings.config.engines`: a `simplemma` lemmatizer row + a
       `snowball_<language>` stemmer row for the language, as a NEW setting
       version in every affected active organisation settings row (chunks carry `setting_id`; editing the active row in place makes
       every stored row's provenance a lie)
4. [ ] `qa_stopword` and a supplied stop-word map for the language. There is no built-in language list; a missing map fails, a missing language entry in a valid map is empty.
5. [ ] `qa_synonym`: an interrogative group, plus any domain entity groups
6. [ ] `qa_blocklist`: profanity / self-harm / hate, `term_norm` stored
       pipeline-normalized
7. [ ] `system_message`: all keys × the language — **`guard_self_harm` is not a
       translation**, it needs a local helpline
8. [ ] parity vectors: the committed generator currently supports only cs/en. Adding another language needs its own verified generation path; passing `<lang>` alone does not implement it. Run existing parity before and after library changes.
9. [ ] **only now** flip `code_list` to `is_active = true`
10. [ ] rebuild **both** images if the library changed (it should NOT need to —
        see §2; `snowball_<language>` needs no code)
11. [ ] E2E: ingest a monolingual document (chunks `langdetect_result` = the
        language, `langdetect_result_score` = 0.8, morphology folded by the right
        stemmer), a **bilingual** one (must split into one chunk per language,
        `chunk_index` continuous), a question with `language=<code>` answers in
        that language, another language's filter excludes it, profanity →
        `blocked`, and the existing languages still regress clean
12. [ ] upload a `<lang>-core` QA pack and verify multi-pack coexistence
13. [ ] check `langdetect_result_score = 0.4` rows: those fell back to the
        registry because nothing could tell — supply their language by hand

## 9. Effort estimate

| Part | Estimate |
|---|---|
| Library | **0 h** — `snowball_<language>` and simplemma resolve from configuration; verified in code, not assumed |
| Code lists + engines + prompts (data; sourcing a stoplist/blocklist is the work) | 3–4 h |
| Parity vectors + `check_vectors.py` | 1 h |
| Presentation layer (§6b) if the language becomes the house language | 2 h |
| E2E incl. a bilingual document | 2 h |
| **Total** | **~1 working day** (scenario A); scenario B adds the re-ingest |

The second added language is much cheaper than the first: EN cost us a full day
of discovery (the per-run fallback flag, hidden prompt overrides, single-pack
deactivation). Those traps are now closed and documented.

---

*Updated by Martin Šafránek on 2026-08-28.*


Current clean-seed message keys: `capabilities`, `clarify_domains`, `guard_profanity`, `guard_self_harm`, `guard_unusable`, `llm_unavailable`, `no_answer`, `no_answer_missing_topic`, `not_a_question`, `qa_suggest`, `role_required`, `school_identity`, `unknown_caller`. These are a source snapshot, not proof that every deployed organisation has all translations.
