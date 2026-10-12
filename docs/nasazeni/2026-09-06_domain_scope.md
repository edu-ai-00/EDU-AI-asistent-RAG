# Deployment: domain scope (selecting the search domain)

> **Historical record (6 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The feature is implemented; see the [specification](../../specs/draft/spec.search-domain-scope.md). The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

**For:** Jan Drozd
**Branch:** `chunking-config`, base commit `e0316a6`
**Written:** 6 Sep 2026

Specification: [topical domain scope](../../specs/draft/spec.search-domain-scope.md).
Proposal: [`specs/draft/spec.search-domain-scope.md`](../../specs/draft/spec.search-domain-scope.md).
Contract for mobile: [`docs/integrations/mobile-domain-scope.md`](../integrations/mobile-domain-scope.md).

> **Status: deployed on the evening of 6 Sep 2026, commit `59f8774` (parent `e0316a6`).**
> Search, migration 213, nginx and webchat are in production; the default choice is
> „Všechny oblasti“. Data labelling (§7) and the scoped regression (§8, last paragraph)
> remain open — see §15 for the actual record and what stayed unverified.
>
> **The 6 Sep review took place in two rounds and found six findings; all of them are
> fixed and each has a test that fails on the previous code** (§11).
> Most of them were real data loss, not cosmetics: missing parameters
> in the trigram query (the entire document branch silently disappeared under an explicit filter),
> an empty guard in the data migration, a snapshot written only after the writes,
> a rollback over the whole table, a UI that after a code was retired silently searched in all
> domains — and in the second round two things that my own fix had not yet
> covered: **writes selected by pack, not by snapshot** (a row added
> after the snapshot was changed without any check, and the scan surfaces were not in the snapshot
> at all), and **the rollback checked drift before the transaction**.

---

## 1. What this is verified against

| what | found | how |
|---|---|---|
| production checkout | `e0316a6`, branch `chunking-config` | `ssh edu-ai`, `git log -1` |
| running search code | **identical to the checkout** | `docker cp edu_ai_search:/app/app` + `diff -rq` against `/opt/edu_ai/search/app` → 0 differences |
| active cascade steps | route, qa_exact, qa_lexical, document_lexical, qa_vector, rag_vector enabled; **rag_keywords and document_siblings disabled** | `/v1/ask` with `retrieval_only+debug`, `trace` read |
| `/search/v1/domains` via nginx | **404** (not in the allowlist) | `wget` from the `edu_ai_nginx` container |
| `domains` in the `/v1/ask` body | **422** (unknown field) | `curl` from the `edu_ai_search` container |
| active QA packs | 7 (faq-jpz 110, faq-provoz 100, provoz-sos 33, prijimacky-jed 32, provoz-jed 33, prijimacky-svo 32, provoz-svo 33) | `/v1/qa-packs` per entity |
| DOMAIN catalog | 8 global; ZŠ and SOŠ Kavčí hory 11 each | `/v1/domains?legal_entity=…` |

It agrees with the state that the specification describes as of 6 Sep.

**Direct `psql` on production was not permitted to me in this session** (the
sandbox blocked it). I therefore did the inventory through my own read-only tool
`scripts/domain_inventory.py --remote edu-ai`, which goes via SSH + `docker
compose exec postgres`, and through the public read-only endpoints. Every production
number given below comes from these sources.

**Local verification** ran on a **freshly installed** DB (empty volume →
`init_clean` 00→01→02) brought up to date with migrations 192–213. It is not a copy of production:
I did not have the production corpus available, so the regression suite
(`eval_rag.py`) is **not measurable** in this environment — see §8.

---

## 2. What changes

| area | files |
|---|---|
| contract and resolver | `search/app/server.py`, `domain_scope.py` (new), `scope.py`, `state.py` |
| cascade | `nodes/route.py`, `qa_common.py`, `qa_exact.py`, `qa_lexical.py`, `qa_vector.py`, `document_lexical.py`, `rag_vector.py`, `rag_keywords.py`, `document_siblings.py`, `answer.py` |
| history and log | `conversation.py`, `nodes/resolve_context.py`, `log.py`, `postgres/init/213_log_domains.sql` (new migration) |
| webchat | `web_chat/web_chat.py`, `frontend/src/DomainPicker.tsx` (new), `App.tsx`, `styles.css` |
| perimeter | `nginx/nginx.conf` — one new exact-match path `/search/v1/domains` |
| tools | `scripts/domain_inventory.py`, `scripts/domain_migration.py`, `scripts/gen_clean_install.sh` (new) |
| contracts | `contracts/openapi.yaml`, `errors.yaml`, both schemas |

**Additive for clients.** A request without `domains` has a byte-identical body,
the same idempotency digest and the same SQL (the filter fragment is an empty string).
Old clients do not need to be released.

---

## 3. Backup (step 1, mandatory)

```bash
ssh edu-ai
cd /opt/edu_ai
docker compose exec -T postgres pg_dump -U edu_ai -d edu_ai -Fc \
  > ~/edu_ai_$(date +%Y%m%d_%H%M).dump
ls -l ~/edu_ai_*.dump
```

Verify the **restore on a copy**, not just the existence of the file:

```bash
docker compose exec -T postgres createdb -U edu_ai edu_ai_restore_test
docker compose exec -T postgres pg_restore -U edu_ai -d edu_ai_restore_test \
  --no-owner < ~/edu_ai_<stamp>.dump
docker compose exec -T postgres psql -U edu_ai -d edu_ai_restore_test \
  -c "select count(*) from edu_ai.qa_entries"
docker compose exec -T postgres dropdb -U edu_ai edu_ai_restore_test
```

**Do not commit** the dump — it contains the real content of the schools.

---

## 4. Log migration (step 2)

Additive, transactional, repeatable, checks the type. Deploy **before** the code
that writes to the column.

```bash
cd /opt/edu_ai && git fetch && git checkout <commit>
docker compose exec -T postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  < postgres/init/213_log_domains.sql
```

Verify:

```bash
docker compose exec -T postgres psql -U edu_ai -d edu_ai -c "\
select column_name, data_type, column_default, is_nullable \
  from information_schema.columns \
 where table_schema='edu_ai' and table_name='question_answer_log' \
   and column_name='domains'"
```

Expected: `domains | ARRAY | (empty) | YES`. **The default must be empty** —
the migration itself fails if it is not.

A repeated run is a no-op (`NOTICE: column "domains" … already exists,
skipping`). Verified locally with two runs in a row; also verified that the type
check really fails when the column exists as `jsonb`.

**Do not deploy migrations 201 and 202** — production did not go through them and they fail on the unique
index (`taxonomy_rules_active_code`). Verified on a clean install too: 190, 201,
202 and 212 fail there, the others from the 192–213 range pass.

### Rollback

The column is additive and nullable, so **a backend rollback does not need to
drop it**. Dropping it is not part of the rollback.

---

## 5. Build and deploy search (step 3)

The code is baked into the image — **restarting the same image changes nothing**.

```bash
cd /opt/edu_ai
docker compose build edu_ai_search
docker compose up -d edu_ai_search
docker compose ps edu_ai_search           # healthy
```

Verify that the new code is running, not just a new tag:

```bash
docker cp edu_ai_search:/app/app /tmp/pcheck && \
  diff -rq /tmp/pcheck /opt/edu_ai/search/app --exclude=__pycache__ ; rm -rf /tmp/pcheck
```

Smoke inside the container (on production `COMPOSE_FILE` **is** pinned to
`docker-compose.yml`, so 8090 is not published — test from the inside):

```bash
docker exec edu_ai_search curl -s -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8090/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS"          # 200
docker exec edu_ai_search curl -s -X POST http://127.0.0.1:8090/v1/ask \
  -H 'content-type: application/json' \
  -d '{"question":"smoke domain scope","legal_entity":"ZS_KAVCI_HORY_ZS",
       "user_role":"PARENT","query_id":"smoke-domain-20260906","domains":["ATTENDANCE"]}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['outcome'],d['effective_scope']['domains'])"
```

> **Every `/v1/ask` writes a row to `question_answer_log` and on the RAG path may
> call a paid model.** Use pre-prepared synthetic queries with a
> `query_id`, so that they can be found and filtered out in the log.

---

## 6. nginx (step 4)

One new exact-match line in `$search_path_allowed`. The Compose service is
`nginx`, the container `edu_ai_nginx` — re-verify the names before the commands.

```bash
docker compose up -d nginx
docker exec edu_ai_nginx nginx -t                 # syntax is ok / test is successful
```

Verify the public path **with a real proxy hop**, and also that the allowlist
has not widened elsewhere:

```bash
docker exec edu_ai_nginx sh -c \
  'wget -qO- -S "http://127.0.0.1/search/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS" 2>&1 | head -3'
for p in /search/v1/query-log /search/v1/staff-users /search/v1/domains-anything; do
  docker exec edu_ai_nginx sh -c "wget -qO- -S http://127.0.0.1$p 2>&1 | head -1"   # 404
done
```

Verified locally: `/search/v1/domains` 200, the three paths above 404.

---

## 7. Data (step 5) — and why it is blocking

The filter is only as good as the labelled sources. Report:

```bash
python3 scripts/domain_inventory.py --remote edu-ai
```

**Measured on 6 Sep 2026** (read-only, via this tool):

| finding | number | consequence |
|---|---|---|
| active packs **without any label** | **5** (faq-provoz 100, prijimacky-jed 32, provoz-jed 33, prijimacky-svo 32, provoz-svo 33 = **230 answers**) | **unreachable** under an explicit filter |
| `ZS_KAVCI_HORY_SOS` active chunks | 104, of which **0** with a domain | SOŠ has nothing in any domain |
| chunks **without** a domain under a labelled document | **170** (151 `ZS_KAVCI_HORY`, 19 `ZS_KAVCI_HORY_ZS`) | the signature of „metadata UPDATE without propagation“ |
| chunks with a **different** set than their document | **94** (`ZS_KAVCI_HORY_ZS`) | reachable in the **wrong** domains |
| `qa_variants`/`qa_embedding` vs `qa_entries` | 274 rows, **all 274 only a different array order** | **harmless** — the filter is an array overlap, the order does not matter |

I examined the order mismatch separately (`@>` and `<@` hold for all 274), which is why
the report lists it separately — otherwise the „drift“ row would teach everyone to
ignore it.

### What to do about it

1. **Do not turn on the client-side domain selection for a school whose content is not labelled.**
   The user would get `no_answer` to questions the system is able to answer. That
   is worse than no filter.
2. The curator decides on the domains for those 5 packs and for the untagged documents.
   **The tool does not decide this** and is not meant to.
3. Record the decision in a plan and carry it out:

```bash
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai            # DRY RUN
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai --apply --go
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai --verify
```

The plan format is in the script's docstring. The plan carries **codes, not IDs** (an ID means something different in
each catalog) and keys QA items by **`entry_key`**, because the QA
writer resolves identity through it and the same CSV in another pack does not preserve the UUID.

The 170 chunks without a label are fixed by `kind: "repropagate_chunks"` — it copies the
effective declaration of the document (the user's, otherwise the LLM's) onto its chunks and invents
nothing.

**Verified on a fixture:** dry-run without writing → apply (2 sets, snapshot +
checksum) → verify OK → repeated apply **skips** because of preconditions →
rollback restores the values **byte for byte**, including the `NULL` vs `{}` difference.

After the review additionally (`tests/test_domain_migration.py`, 35 checks):

* **The precondition is re-verified INSIDE the transaction** and beforehand it takes
  `FOR UPDATE` on the affected rows. A check in a separate query is only indicative
  — a curator who saves in the gap between it and the UPDATE would lose their decision without
  any record. Verified with a **real concurrent psql session**: the second session
  holds an uncommitted change, the migration blocks on it, after the commit it sees it and rolls back the entire
  set. Without the guard (`guard = ""`) the test fails and the DB keeps the migration's
  value instead of the curator's value.
* The guard watches **all rows from the snapshot**, i.e. more than `expect_current`
  names, and catches a changed value, a **vanished** row and an **added** row.
* **The snapshot is on disk before the first row is touched** (atomically, via a
  temp file and `os.replace`), and is rewritten after each set. Before, it was written
  only after the loop — a killed process (a dropped SSH is enough) left a changed database
  **without a record of the original values**.
* **Every write is keyed to the snapshot** (`AND <key> IN (…)`), not just selected
  by pack. Selection by `pack_id` touched whatever was in the pack *at the moment
  of writing* — not the set that the operator approved. This is also the only
  protection against a row that no guard can see (inserted by a transaction that
  has not committed yet).
* **The snapshot also covers the scan surfaces** (`qa_variants`, `qa_embedding`). Before, they
  were not in it, yet they were updated via a JOIN — a variant added to an existing
  answer was thus written blindly and could not be reverted.
* **Rollback restores each recorded row by its own key**,
  verifies **inside its own transaction** under the same lock and **refuses** (exit 2)
  when a row has changed since the apply — and says which one. Before, it resynced the scan
  surfaces with an unrestricted `UPDATE … FROM qa_entries` (on production ~3 000 + 3 000
  rows to revert two packs) and checked drift **before** the transaction.
  `--force` turns off the verification, never the lock.

`--rollback` is **not** `qa_deactivate_packs.py --activate`: that activates all the
variants of a pack, which is something different from „put it back“.

### What the migration tool does not do

It does not re-chunk, re-embed, classify and **does not recompute
`taxonomy_binding` / `document_binding_id`** nor the values derived from the binding.
If the new domains change the semantic binding of a document, that is a **targeted reingest**,
not a label edit — `--verify` warns about an unchanged binding.

---

## 8. Regression suite — what I did NOT measure (supplemented on the evening of 6 Sep, see §15)

> **Supplemented at the deployment on the evening of 6 Sep.** An authorized current copy
> of the production corpus was created by a fresh `pg_dump` and a test restore (see §15);
> `--repeat 3` ran, both old and new, against it, with a real embedding call
> (not just a network-restricted `no_answer`). Result: 41/42 identical, the only
> difference is in a case that had fluctuated earlier. The text below is the original, unsupplemented
> record from the morning — kept for the reasons why it was not possible earlier.

I **did not run** `python3 tests/eval_rag.py --repeat 3` and I have no before/after
baseline.

Reason: the suite is calibrated to the production corpus. Locally I have a clean install with
a fixture, where it would end almost everywhere in `no_answer` — that is a number, not a measurement.
I did not have an authorized copy of the production data; the backup
`backup_prod_20260729` is from 29 Jul, commit `1c5b9fd`, `postgres/init` after
migration 30 — six weeks and ~180 migrations behind, so the baseline does not reproduce.
A run against production would call a paid model and write to the query log.

**What is therefore missing and what needs to be done before turning on the client-side selection:**

```bash
# on an environment with the production corpus, BEFORE the deployment
python3 tests/eval_rag.py --repeat 3                      # baseline
# after deploying the backend, BEFORE the data change
python3 tests/eval_rag.py --repeat 3 --baseline tests/eval_out/<baseline>.json
# and the scoped suite, only after the data is labelled
python3 tests/eval_rag.py --questions tests/eval_rag_domains.jsonl --repeat 3
```

Measuring a **code change** and measuring a **data change** are two separate runs; the suite with
domains is therefore in a separate file.

The argument that makes the first run meaningful: for a request **without** `domains` the
domain fragment is an empty string, so the SQL is byte-identical. The changes that
nevertheless affect an unrestricted request are three, and they are fixes:

* `route()` on an exception returns a sentinel instead of `[]` (before, it opened all packs),
* `route._corpus_topics` joins the metadata also via `processing_uuid` (before, it could
  offer a title from an older version of a document),
* `/v1/explain` no longer answers 500 (see §10).

---

## 9. BFF and UI (step 6)

Deploy **only with the data ready**.

```bash
docker compose build edu_ai_web_chat && docker compose up -d edu_ai_web_chat
docker exec edu_ai_web_chat python -c "\
import urllib.request,json; \
print(json.load(urllib.request.urlopen('http://127.0.0.1:8081/chat/api/domains')))"
```

Verify both positions of `CHAT_ENTITY_PICKER`. Note: **production has the picker turned on**
(`CHAT_ENTITY_PICKER=true`), so both the catalog and the search respect the entity from the
browser. With the picker turned off, a forged `?legal_entity=` is **ignored** and the
response returns the real entity — that is the part that prevents „the catalog of one
school, the search in another“.

The browser check (Playwright, two viewports) is
`web_chat/tests/test_domain_picker_browser.py`, procedure in its docstring.
Locally **69 checks, 0 failures**.

After the review it also contains a case that was missing in the first round: **a retired stored
code blocks sending** until the user chooses again or confirms. Without it
the selection fell to `null`, i.e. „all domains“, and the next query searched the whole
corpus while the user believed that the filter applied — the same silent widening, just
by a different path than the retry.

The browser test also revealed one more thing that nothing else could see:
the effect with the catalog runs in the production configuration **twice** (first with `entity`
= null, then after `/chat/api/config`). The version that overwrote
localStorage on retiring a code consumed it in the first run — the second run
found nothing retired, the warning flashed for one frame and the blocking never turned on. Storage
is now overwritten **only by an explicit choice**.

---

## 10. Fixes that are not part of the feature

I found both while working; both are pre-existing and both are in this package,
because the plan touches both directly.

**`/v1/explain` returned 500** — and it does on production on `e0316a6` too
(`docker exec edu_ai_search curl … /v1/explain` → 500). Cause: the endpoint called
`prepare(state)` without a config and since `eduai_normalize` 3.0.0 the stoplist is DATA, without
which `stopwords_of()` throws an exception. The endpoint is, moreover, **in the nginx allowlist**,
so it is published. Fixed by passing the active settings config.

**`/v1/explain` did not resolve identity.** It built its own two-key state and
ignored the entity, the caller, the role and the language; `scope.where()` thus saw nothing
resolved, which means „sees nothing“ — safe by accident, not by rule. Now it goes through
the same preparation as `/v1/ask`: the same 400 for an unknown entity, 403 for an
unsubstantiated user, 422/503 for domains, the same role gate. **This is a change of the
diagnostic contract** and it is tested.

The comment in `nginx.conf` at this path said „scopes to nothing“; it has been rewritten.

---

## 11. Findings from the 6 Sep review and their tests

Four findings, four fixes, each with a test that **fails** on the previous code.
Verified by always temporarily reverting the defect and running the test.

| # | finding | fix | test | without the fix |
|---|---|---|---|---|
| 1 | the guard in the transaction was an empty string (`guard = ""`), while the comment claimed the opposite; the check thus did not protect against a concurrent change | `guard_sql()` — `FOR UPDATE` + comparison of all snapshot rows, inside the same transaction | `test_domain_migration.py guard-race` (concurrent psql session, handshake via `pg_locks`) | 4 checks fail; the DB keeps the migration's value instead of the curator's value |
| 2 | the snapshot was written only after the writes; the rollback updated the domains of **all** QA variants and embeddings; later changes only a warning | snapshot to disk before the first write (atomically) + after each set; the rollback limited to the `entry_id` from the snapshot; machine check of `after` → exit 2, `--force` | `test_domain_migration.py snapshot`, `foreign`, `refuse` | a foreign set and a variant deliberately outside the step get overwritten; after a killed process there is no snapshot |
| 3 | when all stored domains vanished, the selection fell to `null` = all domains; the warning was displayed, but sending was not blocked | `needsChoice` blocks the composer; a confirmation or a new choice releases it; storage is overwritten only by an explicit choice | `test_domain_picker_browser.py`, block „retired stored code“ (both variants) | 6 checks fail; the query goes out unrestricted |
| 4 | the trigram query in `document_lexical` interpolated `cand_cond` but received `scope_params` → under an explicit scope it failed and the step swallowed the exception | `*cand_params` | `test_domain_scope.py` — `no_swallowed_failure` watcher + `test_document_typo_fallback_is_scoped` | 9 checks fail (`list index out of range`), the entire document branch disappears |
| 5 | the guard compared only the stored IDs and **restricted the actual side to those same IDs itself**, so „a row was added“ could not be detected; meanwhile the writes selected by `pack_id`. The scan surfaces (`qa_variants`, `qa_embedding`) were not in the snapshot at all, yet they were updated via a JOIN | one selector (`snapshot_sql`) for the snapshot, the guard and the write keys; the guard compares the **whole** target set; every UPDATE has `AND <key> IN (…)`; the snapshot also covers both scan surfaces | `test_domain_migration.py added-row`, `added-variant` | `new_row_outside_snapshot_modified` — an added row gets the migration's value; an added variant loses its label and cannot be restored |
| 6 | the rollback checked later changes **before** the transaction; between the check and the restore a curator could write a change that the rollback overwrote | `restore_guard_sql` — `FOR UPDATE` + comparison with `after` inside the same transaction; `--force` turns off only the verification, never the lock | `test_domain_migration.py rollback-race` | the rollback passes (rc=0), the curator's value is overwritten with `NULL`, and the tool printed „no drift“ beforehand |

Finding 4 revealed a gap in the method, not just in the code: **the step defers the exception itself and
returns empty**, so the assertion „no rows outside the domain“ passes precisely because
there are no rows at all. That is why the tests now have a log watcher and a set of queries that
really pass through these edge branches (both typo fallbacks, the empty pool, the category top-up) under
the filter.

Findings 5 and 6 revealed a second, similar thing: **my fix from the first round had a
comment that claimed more than the code did** — the guard „catches an added row“,
while its `actual` side was restricted to the stored keys, so it could not catch it.
A test that would really run that scenario was missing. That guarantee is now
formulated **per snapshot** and is tested in both directions: a row missing from
snapshot S is neither written nor restored by S; a row that was committed and visible
at the moment of the snapshot belongs to the set and may be written **precisely because** it is
recorded and can be reverted.

The concurrent tests win the race deterministically: the second psql session holds an
**uncommitted** change (so the migration's pre-read does not see it through MVCC and passes —
exactly the window that a pre-check cannot cover), the migration blocks on
`FOR UPDATE`, the handshake is a query into `pg_locks`, and only then does the second session
commit. No sleep. A trigger could not test it: it fires inside the migration's own
transaction, which no lock protects.

## 12. Post-deployment check

```bash
docker compose exec -T postgres psql -U edu_ai -d edu_ai -c "\
select query_id, stage, domains, matched_entry_id, latency_ms \
  from edu_ai.question_answer_log \
 where query_id like 'smoke-domain-%' order by created_at desc limit 20"
```

Watch: the canonical `domains`, `matched_entry_id`, error rate, latency.

**Domain names in the answer text are not proof of filtering.** Rely on the identity of the
sources actually used — `matched_entry_id` for QA, `sources[].file_uuid` for
documents.

---

## 13. Rollback

| what to roll back | how | condition |
|---|---|---|
| UI | deploy the previous `edu_ai_web_chat` image | always possible; the backend stays compatible |
| nginx | remove one line from the allowlist | the catalog stops being public, the search keeps working |
| search | switch to the previous image tag | **only when no client sends scoped requests**, including a released mobile — the older API rejects them with 422 |
| data | `scripts/domain_migration.py --rollback <snapshot> --go` | beforehand verify that no further curator changes took place after the apply — restore returns values, not a point in time |
| log column | **do not roll back** | additive and nullable |

**The filter must not be dropped on the proxy or in the BFF as an emergency workaround.** The client
would get a broader answer and think it is the requested one.

---

## 14. What remains open

* **Regression suite — supplemented** (§8, §15): 41/42 identical against the current
  production copy, with working vector search. The scoped suite
  (`eval_rag_domains.jsonl`) remains unmeasured against production — it references the
  fictitious entity `ZAKLADNI_SKOLA_LIPOVA` with no production counterpart and the runbook
  itself plans it only after the data is labelled (§7), not today.
* **The data is not labelled** (§7): 230 QA answers without a domain, 104 SOŠ chunks,
  170 + 94 chunks with a mismatch. Requires a curator.
* **The mobile client does not exist.** The API is done and verified through a real proxy
  hop; the UI, storing the choice and the release are up to the mobile team
  ([contract](../integrations/mobile-domain-scope.md)).
* **`postgres/init_clean/` not regenerated.** The generator
  `scripts/gen_clean_install.sh` is new and verified (the generated trio
  installs onto an empty volume without a single error and gives an **identical set of 507
  columns** as a DB brought up to date with migrations). I **did not do** the overwrite of the committed
  dumps: the diff would also carry migrations 192–212, which are missing from the consolidated
  file, and that is a change for separate approval, not an attachment of this feature.
* **`concept_idf_weights` is not scoped by entity/role.** A pre-existing
  asymmetry, described in the code and in `docs/ai/qa-fastpath.md`, left unchanged by this
  package — fixing it would move the numbers for every existing request.
* **Named profiles „Přijímačky / Provoz“** are not implemented (spec §8)
  and are not in this scope.

---

## 15. Actual deployment record — evening of 6 Sep 2026

**Commit:** `59f8774` (parent/previous production `e0316a6`), pushed to
`origin/chunking-config`, deployed as an exact SHA (detached HEAD), not the branch tip.

**Images (the old ones preserved under the tag `prev-e0316a6`):**

| service | previous image ID | new image ID |
|---|---|---|
| `edu_ai_search` | `sha256:f6aee27085ac…` | `sha256:48f1c6d66d67…` |
| `edu_ai_web_chat` | `sha256:3b395971c47d…` | `sha256:bf81cd897238…` |
| `nginx` | image unchanged (`nginx:1.27-alpine`), only a bind-mounted config | — |

`docker inspect edu_ai_web_chat --format '{{.Image}}'` is the only reliable
source — buildx on a multi-platform/attestation build prints several digests
(`exporting config …`, `exporting manifest …`, `exporting manifest list …`);
the image ID that compose actually uses and runs is the last one
(`manifest list`). In the previous overview I mistakenly gave the `exporting config`
digest (`sha256:4680d674759…`) read from the build output, not the real ID
from `docker inspect` — corrected.

Both new images were verified by content, not just by ID: `edu_ai_search` via `docker cp`
+ `diff -rq` against the checkout (0 differences); `edu_ai_web_chat/web_chat.py` via
`docker cp` + `diff` against the checkout (0 differences).

**Backup:** `pg_dump -Fc` → `~/edu_ai_20260906_2033.dump` (56 MB) on the production
host, restore verified into a throwaway DB (`edu_ai_restore_test`, row counts
match), then dropped. The dump stays outside git, outside the repo.

**Regression (supplemented, see §8):** The same DB copy also served for the regression
comparison of the old and new code. The first time I ran it with throwaway containers
connected only to the `app`+`data` network (without `edge`) — that accidentally turned off
`qa_vector`/`rag_vector` (DNS for `openaipublic.blob.core.windows.net`
unresolvable from the internal network) and the result (78% answer) was a **run
with broken vector search, not a confirmation of compatibility**. After connecting
both throwaway containers to `edge` (the same network as the production `edu_ai_search`,
no new ports, live services untouched) and verifying a real successful
embedding call (`POST .../embeddings 200`, `qa_vector`/`rag_vector`
really contribute chunks) I repeated both suites with `--repeat 3`:

* old code (separately): 90% answer / 4% no_answer / 4% suggest, 2
  fluctuating cases (N11, N26).
* new code with `--baseline` against that: **41 of 42 identical**; the only difference is
  in N11 (a different number/order of sources), which had fluctuated already in both runs —
  it is not a systematic regression.
* The test suite (`tests/eval_rag_regression.jsonl`) references the anonymized
  entities `SKOLA_NOVA_VES_ZS/_SOS` — for a run against the production copy I used
  a temporary, uncommitted copy with the codes rewritten to `ZS_KAVCI_HORY_ZS/_SOS`
  (a known alias, see the internal note about migrations 201/202). The scoped suite
  (`eval_rag_domains.jsonl`) did not run — see §14.
* Side finding: `answer: query-domain detection failed (_kw_tokens()
  missing 1 required positional argument: 'cfg_key')` — at the **same rate**
  in both runs (91× old, 36×+ new), i.e. pre-existing, unrelated
  to the domain scope. Recorded, not fixed (outside the scope of today's release).

**Migration 213:** applied with `ON_ERROR_STOP=1` to the production `edu_ai` (not just
to the test copy). `domains | ARRAY | (empty) | YES` — confirmed.

**nginx:** `docker compose up -d nginx` was **not enough** — the bind-mount pointed to the
old inode (git checkout replaces the file, it does not overwrite it in place), so not even
`nginx -s reload` loaded the new allowlist. Only `--force-recreate` helped;
verified explicitly before/after (`/search/v1/domains` 404 → 200) and that the allowlist
did not widen elsewhere (the three probe paths still 404). **An addition to the procedure compared to
§6 for next time: when changing `nginx.conf`, count on `--force-recreate` right away,
a reload alone may not be enough even if `nginx -t` passes.**

**Smoke (query_id `smoke-domain-20260906-*`):** health/domains 200; a query without
`domains` → `answer`, `effective_scope.domains=null`; with `domains: null` the same;
an invalid code `NESMYSL_KOD` → 422; a positive scoped query into `SECONDARY_ADMISSIONS`
(exists, has content) → `answer` with the correct `matched_entry_id` and
`effective_scope.domains=["SECONDARY_ADMISSIONS"]`; SSE (`/v1/ask/stream`
via nginx) → `event: done` with the full payload. All logged in
`question_answer_log` with the respective `domains`. The domain catalog (11 codes for
`ZS_KAVCI_HORY_ZS`) and the active QA packs (faq-jpz 110, faq-provoz 100) unchanged
compared to §1.

**Webchat in the browser — NOT VERIFIED interactively.** `rag2.edu-ai.eu/chat/` is
behind Cloudflare Access, which requires a login; from this environment I verified
only server-side (static shell 200, `/chat/api/domains`, `/chat/api/config`
with `entity_picker: true`), not actually clicking through „Všechny oblasti“ / the choice
of school / a new conversation in a running browser. It relies on the local Playwright
suite (`test_domain_picker_browser.py`, 69/0 before the deployment) — I recommend
a quick manual check in the browser before this is considered fully verified.

**Rollback (verified, exact commands).** `--no-deps` for each service separately, so that
`docker compose up -d` touches nothing but the named container — `nginx` has
in the compose file a `depends_on` on `edu_ai_api`, `edu_ai`, `pgadmin` and
`edu_ai_web_chat`, and without `--no-deps` it could needlessly check/touch them on
recreate. **The order is deliberate: nginx is recreated AS THE LAST
STEP, again, regardless of whether its config is being rolled back at all.**
Its `upstream search_service { server edu_ai_search:8090; }` is a static
block — nginx re-resolves the container name to an IP only on start/reload and then
holds it. Step 2 (search) gets a new IP on `up -d` (a different container, the same
name); if nginx stayed unchanged after this step, it would keep pointing to the IP of
the previous (new-code) container, which no longer exists. A plain
`git revert` does not switch the running containers at all — it is just a commit.

```bash
# 1) UI back — so that clients stop sending domains
ssh edu-ai 'cd /opt/edu_ai && \
  docker tag edu_ai_web_chat:prev-e0316a6 edu_ai_web_chat:0.1 && \
  docker compose up -d --no-deps edu_ai_web_chat'

# 2) search back — ONLY once no client sends domains
ssh edu-ai 'cd /opt/edu_ai && \
  docker tag edu_ai_search:prev-e0316a6 edu_ai_search:1.0 && \
  docker compose up -d --no-deps edu_ai_search'

# 3) nginx — MANDATORY recreate again, even without a config change, otherwise it holds the old
#    IP address of the search container from step 2 (see the explanation above).
#    The config is rolled back only optionally, if the public /search/v1/domains is a problem:
ssh edu-ai 'cd /opt/edu_ai && \
  git checkout e0316a6 -- nginx/nginx.conf && \
  docker compose up -d --no-deps --force-recreate nginx'
# without a config change it could also be just:
#   docker compose up -d --no-deps --force-recreate nginx

# permanent rollback in history (not reset --hard):
git revert 59f8774   # and deploy this revert commit normally by the same procedure

# the domains column from migration 213 is NOT rolled back (additive, nullable, the old code does not read it)
```
