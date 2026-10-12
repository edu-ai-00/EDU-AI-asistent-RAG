# QA rules and requirements for the offline pack

Verified on 2026-10-11 against the `main` code at `fa593d1` and a read-only snapshot of the QA
configuration of the production source `4a8aff1`. The document collects the decision rules, weights, current parameters
and their presence in the export. The mobile app exists in a separate public
[EDU-AI-asistent-APP](https://github.com/edu-ai-00/EDU-AI-asistent-APP); an offline QA-pack engine
was not implemented in the inspected main at `e653aea54d5e408bd3667f4ab087e0920814cc76`.

**The current SQLite QA pack does not carry all the rules needed for the same offline behaviour.**
It contains the QA content, variants, metadata and three manifest thresholds. It lacks the effective step profile,
the normalization configurations, the code lists and part of the inputs for scoring. The presence of an FTS index and
thresholds is therefore not proof of parity between the backend and a future phone. The CSV/ZIP export for editing does not
close this gap either. This document changes neither the exporter implementation nor its schema.

## Verified mobile app

On 2026-10-11, following the owner's link, the public APP main
`e653aea54d5e408bd3667f4ab087e0920814cc76` (commit 2026-07-20) was verified with a read-only clone; an init branch is also available.
It is an existing Flutter app with Drift/SQLite and offline storage of content/state. The findings
in this table are a static check of the sources at the given SHA, not a test of an installed mobile version.

| App part | Verified behaviour |
|---|---|
| [Chat repository](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/repositories/chat_repository.dart#L71) | Creating a chat and a new message both require the internet; offline returns an error. |
| [Remote chat](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/datasources/remote/chat_remote_datasource.dart#L59) | HTTP POST to the chat server and an SSE response; it does not perform a local QA lookup. |
| [Local database](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/core/database/app_database.dart#L30) | Stores courses, lessons, progress/practice, chat history and synchronization; the QA manifest/entries/variants and QA FTS are not here. |
| [Chat UI](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/pages/chat_detail_page.dart#L619) | Offline disables sending a message; the stored history is available. |
| [Bundled courses](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/datasources/local/bundled_courses_datasource.dart#L6) | Offline content is loaded as JSON courses from assets; it is not an import of the backend SQLite QA pack. |

No QA-pack importer, QA normalizer/lemma/stem, stoplist/synonym profile, IDF/coverage matching or QA FTS lookup
was found in lib/test/docs of the inspected main. The SQLite dependency alone
does not provide these functions. Checking the second public branch, init, did not turn up a QA-pack implementation either.
What the remote chat server performs is not proven by the APP source itself. The earlier wording about the non-existence of the
mobile app was too broad; the open task is its offline QA integration and parity.

## Where the rules originate

| Source | Authority for the actual behaviour |
|---|---|
| Cascade and parameters | The active `edu_ai.settings.config.pipeline`: `step`, `enabled`, `params`, `on_hit`, run conditions; executed by `executor.py`, not by the reference `graph.py`. |
| Organization | The settings of the root organization also apply to its units; without a matching row the global row is selected and finally the source fallback. |
| Thresholds of a specific pack | The active `qa_pack_manifest.t_answer`, `t_suggest`, `m_margin`; explicit `qa_lexical` parameters can override them. |
| Normalization | `normalization`, `engines`, the organization's languages, the normalization library and dictionaries; the index stoplist is added from `qa_stopword`. |
| Synonyms and concepts | The active `qa_synonym`, language, group type and the coverage stoplist. |
| Permissions and filters | `scope.py`, the entity hierarchy, the user registry, role, visibility/owner, validity, grades and domains. |
| Guard and helper rules | `qa_blocklist`, the stoplists `meta_filler/meta_verb/coverage`, taxonomy, code lists and the language `system_message`; do not put executable prompts into documentation. |
| Vector path | `qa_embedding`, the active `embedding_registry`, the embedding model and the `qa_vector` parameters. |
| Conversation | Contextual rewriting before query preparation and the history of questions already asked/offered. |

The configuration selection is in [settings.py](../../search/app/settings.py#L433), the step control in
[executor.py](../../search/app/executor.py#L99). The cache of the active settings is five seconds and is
keyed by subject. After a resolution the executor ends unless `on_hit` is `continue`; a step error is caught
and execution continues. When the steps are exhausted it uses the deferred suggestions, otherwise a safe `no_answer`.

## Current operating values

The read-only check on 2026-10-11 found four active settings rows: the platform one, Kavčí hory,
Svobodná škola Praha and Jedovnice. Their QA part listed below was identical. The units of Kavčí hory
inherit the profile of the root organization. The snapshot captures this moment; the configuration is not a constant
of the application. The identifiers and the full permitted listing are in the local ignored
`tests/eval_out/main_closeout_20261011/QA_ACTIVE_SETTINGS_METADATA.json`.

| Active profile parameter | Verified value |
|---|---|
| Exact and lexical `form` | `[norm, lemma, stem]` |
| Lexical `fetch_k` / `suggest_top_n` | `50 / 3` |
| Lexical `synonyms` | `true` |
| Lexical `trgm_suggest_min` | `0.40` |
| `other_grade_top_n` / `other_grade_min_ratio` | `2 / 0.50` |
| QA vector `cosine_min` | **`0.65`**, an explicit parameter of the active profiles |
| Exact / lexical / vector | all enabled, `on_hit=stop`; stop applies only when a result is resolved |
| Guard `phrase_slack` | `3` |
| Guard category order | `self_harm, hate, sexual, violence, profanity` |
| Guard minimum severity | `1` for each listed category |

The active profiles did not contain an explicit `t_answer`, `t_suggest` or `m_margin`. All **seven
active manifests** had `0.60 / 0.30 / 0.08`: two for Svobodná škola Praha, two for Jedovnice,
one for SOS Kavčí hory and two for ZS Kavčí hory. The Kavčí hory root had no active pack.
The counts are counts of active manifests, not of all historical packs or records.

The runtime fallbacks of the running search container were also read: A/B/C `0.60 / 0.30 / 0.08`,
vector `0.75`, vector `fetch_k=20`, `tier_band=0.001`, suggestions `3` and lexical `fetch_k=50`.
For vector, **0.65** now applies, because the active parameter overrides the fallback **0.75**.
The thresholds and fallbacks are in the local `QA_THRESHOLD_METADATA.json`; the check did not read QA texts,
prompt texts or keys and did not run queries with an LLM.

## Query preparation and normalization

Before the QA steps the backend may rewrite a follow-up query based on the conversation. `prepare` then creates
`norm`, `lemma` and `stem`; it picks an explicit language filter or detects the language among the permitted
languages of the organization. When detection is inconclusive it builds the union of the forms of the permitted languages. An offline
client needs the chosen language and context policy; the stored index form alone is not enough.

The normalization library in the source has version **3.0.0**. It uses Unicode NFC; lemmatization and stemming
run on the word with diacritics and folding only afterwards. `lowercase`, `strip_diacritics` and
`drop_stopwords` have a source default of `true`. A missing `stopwords_by_lang` is an error; a present
map without the given language means an empty stoplist for it. The stemmer depends on `engines`, the language,
the algorithm and `min_len` with a source default of `3`. Query lemmatization uses `simplemma`; on
error the surface form is returned. Language detection has in the source a seed `0`, a threshold `0.70` and special
handling of short inputs under `200` characters. These are not numeric weights from the QA manifest.

The index stoplist and the coverage stoplist are **two distinct inputs**. Synonyms have groups,
a language and a `kind`; entity groups are expanded, interrogative groups do not expand the OR query nor
the content concepts. A multi-word group is activated when the whole token set of at least
one of its terms occurs in some form of it, not all synonyms of the group. A phrase group additionally
creates a separate coverage concept from the remaining distinguishing tokens. Concepts use the
priority lemma → stem → norm, removal of coverage stopwords, interrogatives and duplicates.

Sources: [normalizer](../../search/eduai_normalize/eduai_normalize/normalize.py),
[lemmatizer](../../search/eduai_normalize/eduai_normalize/lemmatizer.py),
[prepare](../../search/app/nodes/prepare.py), [qa_common](../../search/app/nodes/qa_common.py#L110).
Examples of token parity are in [contracts/parity](../../contracts/parity/README.md).
They do not verify result order, scope, thresholds or identical answers. The APP is now known and statically
inspected; a corresponding Dart port of the QA normalization was not found in its main. The normative
Python/Dart parity requirement is thereby not passed off as a fulfilled client implementation.

## Permissions and pack selection

Before ranking, the following apply: activity, entity/organization/private scope, role, validity, language,
grade/level and the selected domains. `valid_from <= CURRENT_DATE` and `valid_to >= CURRENT_DATE`;
NULL is unlimited. Private material requires a verified owner; it does not use the audience-role branch
in the same way as more public material. An empty or non-permitted identity must not widen the scope.

Domains have three states: `None` does not filter, an empty list permits nothing and a non-empty list
requires an intersection. The manifest only preselects a pack; matching filters the metadata directly on the scanned
variants/embeddings. The exported entry and variant data are not composed by automatic inheritance
from the manifest. No permitted pack is represented in the backend by the sentinel value `~none~`;
an empty list would be read by some downstream steps as all packs.

The grade from the question takes precedence over the user profile when selecting packs. An exact grade and the compatible
universal `9999` are resolved via the code lists and `entity_grade_scope/grade_level`, not via a custom
fixed table of the mobile app. A pack without grades is universal. Other-grade offers relax the grade and
keep the other permissions.

Sources: [scope.py](../../search/app/scope.py), [route.py](../../search/app/nodes/route.py#L646),
[mobile domains](mobile-domain-scope.md). The device date alone does not ensure trustworthy validity
and a local filter alone does not protect already downloaded private content; an offline distribution must define the permitted
bundle, a trusted user profile and the handling of its validity.

## Router guard

The router resolves in sequence: unusable input → meta query → identity query → blocklist. A meta/identity
match uses the whole folded question with any filler words. Single-word blocklist terms
match whole norm/lemma tokens. Phrases count unique token hits in a window of the size
number of term tokens + phrase_slack; a special check preserves Czech negation. The result is determined by the
category order and minimum severity from the active profile; the records and answer texts are data.
A blocklist loading error skips this guard check; no hidden blocking by
another dictionary is performed. A future client must explicitly adopt these semantics or define a different profile.

Source: [route.py](../../search/app/nodes/route.py#L547). For a future bundle the policy,
the relevant code lists and the localized messages need to be carried over; server API keys and executable prompts
do not belong there.

## Exact answer

A match of the whole ordered token sequence in **any** configured form suffices. The selection prefers
own subject → subject in the organization → others, then a stable `entry_id`. It returns the authored
answer with a score and margin of `1.0`, without generation. A non-match or an error continues.

Related questions use the active lexical profile, not the isolated defaults of the exact step;
they remove the answered question and deduplicate the text. `suggest_top_n` is taken from the exact parameter,
then the lexical parameter, finally the fallback. The source form resolver has a default `[norm, lemma]`,
but the operating profile above explicitly uses stem too.

Source: [qa_exact.py](../../search/app/nodes/qa_exact.py).

## Lexical scoring and weights

The backend builds an OR query from the chosen forms and synonyms. Recall uses PostgreSQL
`ts_rank_cd(..., to_tsquery('simple', ...))`; it sends neither its own weight array nor a normalization argument.
This is not an implementation of SQLite BM25. The PostgreSQL default weights in the order D/C/B/A are
`0.1 / 0.2 / 0.4 / 1.0`, the rank normalization default is `0`; cover density takes the positions of lexemes into account.
[PostgreSQL 16 definition](https://www.postgresql.org/docs/16/textsearch-controls.html#TEXTSEARCH-RANKING).

`fetch_k` limits the number of **variants** before aggregation, not the number of unique answers. An entry takes
the maximum rank of its matched variants and the union of their tokens. The content weights follow:

```text
N       = max(number of unique active entries linked to the variants of the selected packs, 1)
df_i    = number of entries containing any token of concept i
w_i     = ln((N + 1) / (df_i + 1)) + 1
coverage = sum of weights of the hit concepts / sum of weights of all concepts
margin   = (rank_1 - rank_2) / rank_1
```

Without usable weights the coverage turns into a plain share of the hit concepts. With a single
candidate `rank_2=0`; with a non-positive `rank_1` the margin is `1.0`. The margin is a difference of ranks,
not a difference of coverage nor the probability of a correct answer.

The entry order is lexicographic `(grade_tier, entity_tier, -round(rank, 6), -coverage)`.
An exact grade has tier `0`, universal or without grades `1`, others `2`; without a known grade the
grade tier is the same. The entity tier is own `0`, organization `1`, others `2`. It is not a sum
of, for example, "70% lexical + 30% school"; a better tier takes precedence over the following score.

The thresholds are resolved **step parameter → manifest of the winning pack → environment/code fallback**.
The current four profiles have no A/B/C override, but a future settings change can add one without
re-exporting the pack. The exporter stores the manifest values, not their effective override.

| Branch | Condition and result |
|---|---|
| A | `coverage >= t_answer`, `margin >= m_margin` and the topical gate did not reject the hit → authored answer. |
| B-deferred | `coverage >= t_suggest`, not A → suggestions are stored and the **cascade continues**. The final offer happens when the further steps do not answer. |
| C | lower coverage → continues; fallback offers may remain for the resulting `no_answer`. |

Topical admission requires a suitable shared rare concept with `df/N` at most the permitted limit.
The helper flags `query_in_corpus` and missing concepts influence the downstream fallback.
With zero full-text recall, typo suggestions arise via `pg_trgm`, similarity
`1 - (norm_text <-> query_norm)`. The threshold is tested here on the best hit, not again on each
returned trigram suggestion; a direct answer A never arises from this fallback.

Sources: [qa_lexical.py](../../search/app/nodes/qa_lexical.py#L291),
[IDF and coverage](../../search/app/nodes/qa_common.py#L273).

### Why computing the IDF from a single SQLite file is not enough

The backend statistics query uses the selected pack IDs and active entries, always `ts_norm || ts_lemma`.
It does not have the same `v.is_active` or entity/role/validity/domain filter as the query selecting candidates.
The code comment records this asymmetry as previous behaviour; the audit does not fix it. The export, by contrast,
contains only the scoped active variants of **one** pack. With the same selected pack IDs the
backend does not narrow N/df by domains; a domain can change the statistics by way of the selection of other packs
in the router. If a client computes the statistics only from one pack or from a selected domain, it
uses a different corpus. Inactive stored variants affect the existence of an entry in the statistics join
and df. The same formula over different data therefore does not produce the same weights.

SQLite FTS5 indexes also do not reproduce `ts_rank_cd`, PostgreSQL trigram similarity, tiering
and margin on their own. [SQLite FTS5](https://www.sqlite.org/fts5.html). Exact parity requires a compatible
ranking and a precisely defined statistics corpus; there is no reason to carry unauthorized data to the mobile
merely to reconstruct the denominator.

## Other parameters and offers

The table distinguishes the source fallback from the explicit operating parameter above. An absent parameter
does not mean a disabled feature; the default of the respective step applies. The numeric values are not constants
of a future client.

| Parameter | Source default | Meaning |
|---|---|---|
| `t_answer / t_suggest / m_margin` | `0.60 / 0.30 / 0.08` | The last fallback for A/B/C. |
| `fetch_k / suggest_top_n` | `50 / 3` | Variant recall and the number of lexical suggestions. |
| `synonyms` | `true` | Entity synonym expansion. |
| `trgm_suggest_min` | `0.40` | Condition on the best typo hit. |
| `topical_admission / topical_max_df_share` | `true / 0.25` | Gate for a direct lexical answer. |
| `related_suggestions` | `true` | Related questions next to an exact answer. |
| `related_admission` | `off`, supports `rare` | Extra admission of related questions. |
| `related_cross_pack_min` | `0.70` | Higher coverage floor for related from another pack. |
| `related_category_fill` | `true` | Filling an empty lexical related row. |
| `related_category_topup` | `true` | Topping up a short exact/vector related row; false still allows filling an empty one. |
| `other_grade_top_n` | `2` | Maximum of separate offers of other grades. |
| `other_grade_min_ratio` | `0.50` | Share of the best rank for an other-grade offer. |
| `other_grade_max_df_share` | `0.10` | Removal of overly general tokens in the offered corpus. |
| `other_grade_min_rank` | `0.15` | Absolute minimum of the other-grade rank. |
| Vector `cosine_min / fetch_k / tier_band` | `0.75 / 20 / 0.001` | Source semantic fallback; the active floor is 0.65. |
| Vector `suggest_top_n` | `3` | Related suggestions next to a vector answer. |
| Route `phrase_slack / meta_max_words / identity_max_words` | `3 / 10 / 8` | Guard phrase window, short meta/identity question. |
| Route `meta_intent_guard` | `true` | Meta intent guard. |

A related question from another pack must pass both the lexical floor and the cross-pack floor. The category is
the same `tags.category` in the **pack of the answered entry**, preserving identity, grades, validity
and domain. The grade filter of siblings uses the grades of the **answered entry**, not the user
profile directly. With concepts passed in, siblings order by coverage, otherwise by a stable `entry_key`; lexical
A uses IDF weights, exact/vector finalizations unit weights of simple content tokens.
Other-grade suggestions use content without synonyms, their own df filter, a rank floor and a ratio to the best
match. The wrapper can add them even next to an already resolved answer.

Conversation dedupe removes already asked questions and moves already shown, unused offers to the end.
An offline client needs its own history. Sources: [qa_common](../../search/app/nodes/qa_common.py#L350),
[qa_exact](../../search/app/nodes/qa_exact.py#L137), [conversation](../../search/app/conversation.py),
[config fallbacks](../../search/app/config.py#L94).

## QA vector and the limits of offline mode

QA vector selects a model from the registry and searches by cosine distance over the available QA embeddings.
In the band `distance <= min(distance) + tier_band` it prefers own subject → organization → others,
then distance and `entry_id`. `similarity = 1 - distance`; only the selected hit must pass the cosine floor.
Without active vectors for the model it skips the query embedding, on error it continues.

The current SQLite pack contains neither the vectors, nor the model registry, nor the runtime embedding model. An offline
design must decide whether it supports this path with a compatible local model or publishes a limited
exact/lexical profile. Likewise the offline QA pack contains no RAG document chunks, no generator and no
LLM rewriting of follow-up queries. Source: [qa_vector.py](../../search/app/nodes/qa_vector.py).

## What is actually transferred

| Input | SQLite export today | Impact |
|---|---|---|
| Answers, canonical questions and variants | Yes: `entries`, `variants` | The content is available. |
| `t_answer`, `t_suggest`, `m_margin` | Yes in `pack_manifest` | Only the manifest thresholds, not the effective settings profile. |
| Norm/lemma/stem texts | Yes | The query normalizer and its configuration are missing. |
| FTS | `variants_fts` over norm+lemma, `variants_tri` over norm | Stem is not in the FTS index; the backend algorithm is not carried over by it. |
| Entity, visibility/owner, role, grade/domain and validity | Yes as row metadata | The client must evaluate them; the export now filters entity+active, not the date or the recipient's identity/role. |
| Category, tags and asset_refs | Yes | The related policy is missing; the cited files are not bundled automatically. |
| Pipeline and scoring parameters | No | Missing enabled/on_hit, form, limits, gates and the related policy. |
| Stoplists, synonyms, engines and language data | No | Identical processing of the query and concepts is missing. |
| Guard rules and language messages | No | The same guard policy is not guaranteed. |
| Hierarchy and code lists | No | The numeric IDs alone do not explain organizational relations, domains and grades. |
| IDF statistics corpus | No | A single exported pack is in general not enough for the backend N/df. |
| QA embedding/model | No | The semantic path is not ensured by this pack. |
| Runtime version/hash of the rules | No | `schema_version` is the version of the uploaded QA format, not of the scoring/normalizer profile. |
| Mobile import/download protocol | The inspected APP main has no QA import/lookup | The exporter returns a local `pack_path`; the existing chat is online, a backend → mobile offline QA path is not implemented here. |

The exact approved layout is the [SQLite contract](../../contracts/sqlite-qa-export.md) and the
[DDL](../../contracts/sqlite-qa-export.sql); the implementation is in the
[export flow](../../langflow_flows/ingestion/EDU_AI_EXPORT_QA_PACK.json).
The CSV `/v1/qa-export` and the portal ZIP `manifest.json + packs/*.csv` serve editing/reupload;
they contain neither the full runtime profile nor the token indexes. Sources:
[qa_csv](../../search/app/qa_csv.py), [export service](../../qa_portal/backend/app/services/export_service.py),
[ZIP](../../qa_portal/backend/app/services/zip_io.py), [qa_admin metadata](../../search/app/qa_admin.py).

## Requirements for a future offline contract

First determine the scope: an identical QA engine, or an explicitly versioned limited offline engine.
A custom SQLite ranking with the same parameter names is not automatically an identical QA engine.
Extending the export schema, the thresholds or the cascade requires a separate proposal and approval under
AGENTS.md; the following list is a requirement, not an already implemented format.

1. **Bound snapshot:** the content of the packs and the effective profile of the organization from the same export point;
   the identity of the settings, the versions of the content, the normalizer, the algorithm and the dictionaries, a hash and client compatibility.
2. **Effective parameters:** resolved A/B/C thresholds and also the other limits/gates/form and the related policy;
   rules for explicit override, fallback and the combination of multiple packs.
3. **Normalization:** engine/language configuration, stoplists of all scopes in use, synonym
   groups and dictionaries or a defined compatible runtime. No hard-coded tenant/language values in the client.
4. **Ranking:** an exact definition of rank, the IDF corpus/df, the tiers, the margin, the trigram fallback and the category
   ordering. For backend parity test a multi-pack corpus and do not carry unauthorized data for the sake of the statistics.
5. **Filters and permissions:** an authorization export/recipient profile, hierarchy, role/owner/validity,
   code lists and the three domain states, also for suggestions and the other-grade path.
6. **Capabilities:** exact/lexical/vector/context/guard and the behaviour when the model is missing,
   the bundle has expired, the version is unknown, data are missing or the answer is not found.
7. **Delivery and updates:** download/import, checksum verification, atomic replacement of the whole bound
   snapshot, preservation of the local conversation and a return to the last valid bundle.
8. **Parity suite:** synthetic queries and expected tokens, the chosen entry_id, rank/coverage/margin,
   decisions, offers and scope; typos, multiple packs, grades, validity, a profile change and rejection of an unknown version.

## Historical discrepancies and their correction

The table preserves the findings of the original audit. The listed wordings in the QA fast-path and the parity README were corrected in the documentation on 2026-10-12; the backend behaviour is not changed by this. The gaps of the offline export and of client parity remain open.

| Place | Description in the documentation | Examined code |
|---|---|---|
| `docs/ai/qa-fastpath.md` | B is a terminal stop | B-deferred continues; a resolved A ends according to on_hit. |
| The same page | manifest → params → fallback | params → manifest → fallback. |
| The same page | plain share of concepts | IDF weighted coverage, with a plain fallback. |
| The same page | category stably entry_key | with concepts coverage; otherwise entry_key. |
| The same page | "No time-based expiry" | the runtime WHERE checks valid_from/valid_to. |
| The same page | other-grade only when the own grade has nothing | the wrapper can add offers even next to an answer. |
| Parity README | an existing offline Dart engine in another repo | the APP exists; a QA normalization port/pack engine was not found in the inspected main, client parity is not PASS. |

`SETTINGS_REFERENCE` states a reference vector floor of `0.65`; the source fallback is `0.75`.
The read-only snapshot confirmed an explicit active `0.65`, so these are different configuration layers,
not a reason to overwrite one value with the other. A summary of the other configurations:
[SETTINGS_REFERENCE](../SETTINGS_REFERENCE.md). The audit evidence remains outside Git; server keys,
executable prompts or extended school content must not be bundled into the mobile package.
