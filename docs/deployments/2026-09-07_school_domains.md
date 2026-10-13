# Code list: school operations and safety and health

> **Historical record (7 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

Subsequently deployed to production together with the
[domains of individual QA answers and the reingest](2026-09-07_qa_entry_domains.md).
Below is the original record of the migration preparation.

At the user's request, on 7 Sep 2026 the domains `SCHOOL_OPERATIONS`
(school operations, Czech label "Provoz školy") and `SCHOOL_SAFETY` (safety and health, "Bezpečnost a zdraví") were added. The translations are in cs,
en, de and es. The codes have the standard search role and have no new router
keywords. The change does not assign domains to existing FAQs or documents.

## Installation

[Migration 214](../../postgres/init/214_school_operations_safety_domains.sql)
adds the missing codes globally and to all owners of an active DOMAIN catalog.
It does not create a separate catalog for a school that inherits one: that would shadow the inherited
domains. A repeated run preserves existing rows, their IDs, curator
edits and inactive codes. When a separate catalog is created later, the
curator must include these domains as well, if the school needs them.

On an existing database the migration must be run explicitly; a commit,
a pull or a container restart alone does not apply it. For example from the repository root
in an sh/bash shell on the target server:

```sh
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
  < postgres/init/214_school_operations_safety_domains.sql
```

For a new empty data volume, after the consolidated seed the
[identical supplementary SQL migration](../../postgres/init_clean/03_school_operations_safety_domains.sql) is run.
The generated dumps do not change. A test checks that the two SQL files match;
when editing, the copy is regenerated from migration 214.

The `/v1/domains` API reads the catalog on every request. The webchat loads the same
catalog through the BFF, so it requires no frontend change; an already open chat
needs to be refreshed. On the target environment verify both codes via the catalog with a real
entity and then their availability in the domain selection. Without labelled content,
`no_answer` is to be expected when selecting the new domain.

## Verification

- `python tests/test_school_domains_migration.py`: passed. The PostgreSQL test
  uses an isolated schema with a rollback and the real catalog resolution function.
  It verifies an own, an inherited and a global catalog, preservation of the original domain,
  a repeated run, an inactive code, the translations and a curator edit.
- The local migration inserted 6 records: the global one and two own catalogs of Nová Ves.
  A GET of the catalog returned the new codes with correct diacritics. A POST `/v1/ask` for
  each new domain returned HTTP 200 and the corresponding `effective_scope.domains`;
  the outcome was `no_answer`.
- Normalization parity: all 30 vectors passed. Running the file in a container
  needs `PYTHONPATH=/app`; `stoplist.json` must stay next to the test.
- `python -X utf8 docs/tools/docs_check.py`: passed.
- `python -X utf8 tests/eval_rag.py --repeat 3`: 43 cases, 39 `no_answer`,
  3 meta answers, 1 error. Case N34 requires `ZS_KAVCI_HORY_ZS`, which does not exist
  in the local seed (HTTP 400). The regression is thus not fully green and
  without a real corpus does not confirm the quality of the answers. The current
  working regression file was used, whose already existing modification is not part of the commit.

Production was not changed. A browser test on production was not performed. The following
separate data step is labelling the answers of the `faq-provoz` pack.
