# Domains after answers and the faq-provoz reingest

> **Historical record (7 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

Deployed on 7 Sep 2026 (Europe/Prague) at the user's request. Implementation
`38b719e`, supplement of the normalizer's database inputs `474affe`.

## Change

The [QA flow](../../langflow_flows/ingestion/EDU_AI_QA.json) reads the optional column
`domains` of individual CSV rows. A non-empty set replaces the domains of the upload,
an empty cell inherits them and `[]` means no label. The codes are normalized and
validated against the entity's effective catalog before the QA write. The category `cat`
does not change. The binding is computed per answer; the manifest summarizes the entire
active pack even on a partial merge. The export returns the domains as a JSON array.
The detailed CSV format is recorded in `QA/packs_faq_provoz/README.md`
in the original repository; the `QA/` directory with school data is not part of the
distribution package. The [API contract](../../contracts/openapi.yaml) is
part of the change and of the distribution package.

The deployment included migration 214, an import of the QA flow only and a rebuild/recreate of only the
search service because of the CSV export. The work-in-progress webchat changes were preserved.

The first import ended before the QA writer: the original QA flow did not have in its stored
template the database inputs of the normalization component, even though its code already
required them. Also verified in the backup of the original live flow. The fix adds bindings
to the existing variables, not a password in the file; the test now includes the real
normalizer and checks both the presence and the wiring of these inputs.

## Production data

Pack `faq-provoz`, entity `ZS_KAVCI_HORY_ZS`, reingest in `merge` mode.
Completed processing UUID: `46d3bb85-bf4e-4c24-8fe1-7c9460cf75ba`.
HTTP 200, status `completed`, 1 file OK, 0 errors.

Before the change the files were compared with the active answers and the real
stable keys were added. The CSVs are in the original repository in
`QA/packs_faq_provoz/faq_provoz_v1.csv` and `QA/faq_provoz_v1.csv`, both identical;
school data is not bundled into the distribution package. The real uploader
was taken from the existing manifest only at runtime; its ID is not in the repository.

The backup of the original QA tables and of the live flow, the ingest responses and the measurements are on
the server in `/tmp/faq-provoz-entry-domains` with restricted access. It is a temporary
operational backup, not a long-term archive. The QA history remains in the database.

| Domain | Active answers including overlaps |
|---|---:|
| ATTENDANCE | 14 |
| GRADING | 33 |
| SCHOOL_EVENTS | 3 |
| CATERING | 1 |
| BULLYING | 3 |
| DATA_PROTECTION | 1 |
| SPECIAL_NEEDS | 4 |
| SCHOOL_OPERATIONS | 35 |
| SCHOOL_SAFETY | 13 |

## Verification

- The [integration test](../../tests/test_qa_entry_domains.py) passed against the local
  PostgreSQL: the real reader, exploder, normalizer, writer and export;
  different sets, inheritance, clearing, merge, stable IDs, propagation, rejection of an
  unknown code without a partial write, and malformed cell shapes.
- All 7 flow configuration tests and the documentation check passed.
- On production all 100 active answers match the CSV in domains and retained
  their ID, texts, categories, sources and permission metadata. All
  500 active question wordings were retained. The active variants and embeddings have domains
  identical to the answer; no answer is missing an active variant or embedding.
- 11 scoped API queries covered all 9 domains and one overlap. All returned
  `answer / qa_exact`; the query log confirmed the expected stable answer ID.
  The effective filter matched every request.
- The export of all 100 answers preserved the domain assignment. The webchat BFF for Kavčí
  hory returns both new domains. A visual browser test was not performed.
- Two questions with an unrelated domain via `/search/v1/ask` (an absence note under
  CATERING and the canteen under GRADING) returned `no_answer` with the correct effective filter.
- The production normalization parity passed before the reingest and on the new search
  service too: 30 vectors unchanged.

The regression measurement uses the same 42 cases three times before and after the change.
The original suite has the fictitious Nová Ves entities; for the production measurement they were replaced
in its temporary copy with the corresponding Kavčí hory entities. The original attempt
without this adjustment had 42 unknown-entity errors and was not used as a baseline.
The baseline for comparison is `20260906-224215.json`, the subsequent snapshot
`20260906-225617.json`. Both runs: 39 `answer`, 2 `no_answer`, 1 `suggest`,
no API errors. The resulting outcomes did not change. The only recorded difference
is the number of sources for N26 (2 → 1); the case fluctuated already in the baseline and in the new run.
Both runs have the same two fluctuating cases N11 and N26. The measurement confirms neither
a quality improvement nor a quality decline; it confirms the preservation of the resulting states
of this unrestricted suite. The functionality of the new filter is evidenced by separate scoped tests.
