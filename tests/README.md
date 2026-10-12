# Tests

Ordered by what they need — run the free ones first:

| suite | needs | catches |
|---|---|---|
| [`test_flow_invariants.py`](test_flow_invariants.py) | nothing (stdlib, no Docker) | flow JSON that lost the attributes its DB config depends on |
| [`test_eval_payload.py`](test_eval_payload.py) | nothing (stdlib) | an eval case field that is accepted and never sent |
| [`test_setup_wizard.py`](test_setup_wizard.py) | stdlib, full suite on Linux/POSIX, fake children; no live Docker/VM | clean-install ordering, own resume, inherited overrides, refusals and source identity |
| [`test_chat_entity_input.py`](test_chat_entity_input.py) | stdlib, fake services | browser-selection readiness vs a real smoke failure |
| [`test_cycle_driver.py`](test_cycle_driver.py) | stdlib only, full suite on Linux/POSIX; no Docker or VM | lab backup/restore ownership gates, unsafe identifiers and archives, secret-free errors, stale restore markers and failures that must not start applications |
| [`test_domain_scope.py`](test_domain_scope.py) | dev stack; runs **in** the search image | which rows the thematic-area filter admits |
| [`test_domain_scope_api.py`](test_domain_scope_api.py) | dev stack incl. nginx + BFF; runs on the host | the area contract over three real HTTP origins |
| [`test_domain_migration.py`](test_domain_migration.py) | dev stack; runs on the host | a label migration under a concurrent write, a killed process, and a rollback's blast radius |
| [`test_resilience.py`](test_resilience.py) | running dev stack + `requests` | ungraceful failure under injected faults |
| [`nginx_s2/run_s2.py`](nginx_s2/README.md) | Docker + Python, own isolated lab (synthetic upstreams, no `.env`, no ports) | nginx config drift vs the production behaviour, DNS/IP-change fragility, stale file mounts; the deploy/return script `scripts/nginx_s2_switch.sh` (`nginx_s2/clean_clone.py` runs the suite from a clean clone) |
| [`nginx_r2/r2lab.py`](nginx_r2/README.md) | Docker + Python, own isolated lab (synthetic upstreams, no `.env`, no ports); `selfcheck` and `candidate.py check` need no Docker but read fixed historical Git SHAs that a public checkout lacks (they fail there) | R2 trust boundary of the proxy headers (`CF-Connecting-IP`, `X-Forwarded-*`): the S2 config vs the design (variant 2; its implementation candidate is rehearsed by `rehearsal_linux.py` below, present in this tree's `docker-compose.yml` and `nginx/conf`; deployment on a host cannot be verified from the repository), with forged headers, a replaced proxy, a stopped tunnel, real uvicorn, per-installation subnet allocation/preflight, two concurrent installations and a cleanup that only removes what a run owns (labels verified) |
| [`nginx_r2/rehearsal_linux.py`](nginx_r2/README.md) | Docker + isolated Linux executor (Compose 5.3.1), own throw-away servers, no `.env`, no tunnel token; `host_iproute2.py` needs a Linux with `unshare -Urn` | the R2 implementation candidate: the REAL step commits and the REAL `scripts/nginx_r2_switch.sh` rehearsed through install, refusals before any change, failures during a step, interruptions with real signals, two installations and the return from every phase; `nginx_r2/candidate.py verify` proves the commits equal the derived design |

---

# Thematic area scope (domain scope)

Two files, because they answer two questions that need different vantage points.

**`test_domain_scope.py` — which ROWS become candidates.** Runs inside the
search image, against a synthetic fixture, and asserts on database answers:
which entry replied, which chunk texts are in the pool, which chips were
offered. It never asserts on the shape of a SQL string — a test that greps for
`domain_ids &&` passes when the fragment is built and its parameters are
interpolated in the wrong order, which is how this kind of change actually
breaks.

**`test_domain_scope_api.py` — does the contract survive the wire.** Runs on the
host against *three* origins: internal `:8090`, the nginx edge
`:80/search/v1/…`, and the BFF `:8081/chat/api/…`. A route can be perfect in the
app and 404 at the edge; `/search/v1/domains` was exactly that before this
change, so an in-process TestClient would have proved nothing. It asserts status,
`Content-Type`, the error envelope, `effective_scope`, SSE event order, the
`Idempotency-Replayed` header, and the row that reaches `question_answer_log`.

## Run
```bash
# 0) free, no services
python3 tests/test_eval_payload.py

# 1) row level — inside the search image (tests/ is not in the image)
docker cp tests/domain_scope_fixture.py edu_ai_search:/tmp/
docker cp tests/test_domain_scope.py    edu_ai_search:/tmp/
docker exec -e PYTHONPATH=/app -w /app edu_ai_search python /tmp/test_domain_scope.py

# 2) real HTTP — on the host, needs nginx and the BFF up
docker compose up -d postgres edu_ai_search edu_ai_web_chat nginx
python3 tests/test_domain_scope_api.py            # all groups
python3 tests/test_domain_scope_api.py public     # one group

# the fixture on its own, to look at it
docker exec -e PYTHONPATH=/app -w /app edu_ai_search python /tmp/domain_scope_fixture.py show
```

On Windows use `MSYS_NO_PATHCONV=1` before `docker exec` — Git Bash otherwise
rewrites `/tmp/...` into a Windows path.

> **Run these three one at a time.** They share ONE fixture
> (`domain_scope_fixture.py`), each builds and drops it, and the drop also
> deletes the `ds-fixture-%` rows from `question_answer_log`. Run two
> concurrently and they sabotage each other — observed exactly that way:
> the API suite lost `/v1/ask/stream logs it too` because the migration suite
> had just deleted the log row it was waiting for, and the migration suite lost
> two precondition assertions to a fixture rebuilt under it. Both passed
> immediately when run alone. Serialising them is cheaper than making the
> fixture reentrant, and this note is cheaper than debugging it twice.

```bash
# 3) the migration tool: concurrency, crash safety, blast radius
python3 tests/test_domain_migration.py                 # all
python3 tests/test_domain_migration.py guard-race      # one group
```

`test_domain_migration.py` drives `scripts/domain_migration.py` through
`subprocess`, exactly as an operator would, and asserts on the **database**
afterwards. Driving it that way is deliberate: the `--go` gate and the exit
codes are part of what is being tested, and a test that imported `apply()`
would pass while `--apply` without `--go` wrote rows.

Three of its groups win a race, and they all use the same instrument, because
it is the only one that works: a second psql session holds an **uncommitted**
change, so the migration's own pre-read (a plain SELECT, MVCC) sees the old
value and passes — exactly the window a pre-check cannot cover — and then
blocks on `FOR UPDATE` until that session commits. The handshake is a query
against `pg_locks` for a blocked request, never a sleep. A trigger cannot test
this: it fires inside the migration's own transaction, which no lock protects
against.

| group | the race |
|---|---|
| `guard-race` | a curator's UPDATE lands during the apply transaction |
| `added-row` | an INSERT into the same pack lands during it — the guard must see a row it has no record of |
| `rollback-race` | a curator's UPDATE lands during the **restore** transaction |

`added-variant` is not a race but the same family: it pins down the guarantee as
**per snapshot** — a row absent from snapshot S is neither written nor restored
by S — and asserts both halves, including that a row which *was* visible when a
later snapshot was taken is legitimately written *and* recorded, so it can still
be undone.

### A note on why the log matters here

`tests/test_domain_scope.py` carries a `no_swallowed_failure` watcher, and it
exists because of a real miss. Every pipeline step must catch and fall through
(AGENTS.md), so a broken step logs a warning and returns nothing. A test
asserting "no out-of-area rows" then passes because there are **no** rows —
the branch is gone, not filtered. That is how a fragment/parameter mismatch in
`document_lexical`'s trigram fallback survived twenty-two green area
assertions. Anything a step logs about catching its own exception now fails the
test.

## The fixture

`domain_scope_fixture.py` builds and drops itself, in a `finally`, including
after a failure — a leftover fixture makes the next run's failures unreadable.
Everything in it is **invented**: the seed's fictional Nová Ves / Lipová schools,
questions about a school that does not exist, every row prefixed `ds-` so
teardown removes exactly what build created.

Two things about it are not incidental:

- It runs **inside the image** because the QA rows carry `norm_text` /
  `lemma_text` / `stem_text` and `qa_exact` compares them for **equality**. Built
  by hand they would be a guess about what the normalizer produces, the guess
  would be wrong for one form, and the test would pass by never reaching the
  branch it claims to test. So it builds them with the same `eduai_normalize`
  and the same active settings row the cascade uses.
- Its embeddings are **hand-made unit vectors** (one hot dimension per row), so
  cosine distance is arithmetic. The vector steps are therefore tested with no
  `OPENAI_API_KEY` and with a neighbour order that is decidable on paper rather
  than being a model's opinion.

What it covers, and why each row is distinct rather than a variation:

| row | the case it is the only one for |
|---|---|
| `ds-attendance` | one area — the baseline positive |
| `ds-mixed` | pack labelled two areas, entries labelled ONE each: proves the **entry** filter, which a manifest-only filter would pass |
| `ds-untagged` | no label at all — reachable unrestricted, never under an explicit filter |
| `ds-off` | inactive pack — never, in any area |
| `ds-foreign` | another **legal person**, labelled with the same global code |
| `ds-org-global` | the organisation's pack, labelled with the GLOBAL id only: a ZŠ caller picking that code must still reach it |
| `ds-teacher` | `audience_roles = {TEACHER}` — an area is a preference, never a permission |
| `ds-school-only` | a code in ONE school's catalog and nowhere else |
| `ds-doc-long` | one long document, THREE areas, three sections — not split, chunks inherit all three |
| `ds-doc-private` / `-expired` / `-foreign` | owner, validity and tenant, none of them relaxed by an area |
| `ds-doc-stale` | one file, two processing runs — the superseded-version case |

---

# Flow invariants (static)

Asserts that the Langflow flow JSON still carries the config the flows need to
reach Postgres. The flows hold 220 hand-maintained `load_from_db` attributes and
210 `override_skip` attributes; re-exporting a flow from the Langflow UI can drop
them, and every way of dropping them **fails silently at runtime** — an unresolved
`pg_host` becomes a local unix socket, an unresolved `pg_password` becomes "no
password supplied". No error names the variable, so it reads as a Postgres fault.

## Run
```bash
# from the repo root; no Docker, no network, no pytest — under a second
python3 tests/test_flow_invariants.py               # all
python3 tests/test_flow_invariants.py hidden        # selected
```

## What each invariant asserts
| id | asserts | why breaking it is not obvious |
|----|---------|-------------------------------|
| **free_text** | every non-secret `load_from_db` field names one of the five non-secret connectivity variables | a Credential-typed variable in a `StrInput` is rejected outright — and env-sourcing only ever creates Credential |
| **secrets** | every password field names `OPENAI_API_KEY` or `POSTGRES_PASSWORD` | those are the only names that exist as Credential global variables |
| **hidden** | `show=False` ⇒ `override_skip=True` | `should_skip_field()` drops a hidden field without it, so the field is never resolved at all |
| **named** | no `load_from_db` field has an empty value | an empty name is skipped just as quietly as a hidden field |
| **signals** | `password` and `_input_type` agree | the runtime trusts `password`, a human reading the JSON trusts `_input_type` |
| **bound** | every `pg_*` / `*api_key` field still has `load_from_db` | a field that loses the flag drops out of the four checks above instead of failing them — see below |
| **edges** | no edge targets a `load_from_db` field | edge params win over field params, so the edge value replaces the *variable name* |

### What actually breaks these fields (measured against the live stack)

`show`, `override_skip`, `load_from_db` and the variable name in `value` appear
**only** in the stored template — the component code declares none of them. That
makes it tempting to assume any code edit in the UI destroys them. It does not:

| what happens | `load_from_db` + variable name | `show` / `override_skip` | flows still work? |
|---|---|---|---|
| move a node, edit a description, save | kept | kept | yes *(verified in the UI)* |
| edit component code, Check & Save | **kept** | reset to `true` / `false` | **yes** — hidden fields just become visible |
| hide a field via the UI | kept | `show=false`, `override_skip` stays `false` | **NO — field is never resolved** |
| bare rebuild with an empty `frontend_node` | lost | reset | no — but the UI never sends this |

`/api/v1/custom_component` merges the rebuild with the existing node
(`update_frontend_node_with_template_values`), which is why the code path is
survivable. The dangerous row is the third one: **the UI cannot set
`override_skip`** — the merge resets it to `false` even when the stored node had
`true` — so hiding a field through the UI always produces the one combination
that silently skips resolution. `override_skip=True` can only come from the JSON.

That is the whole story behind the original half-day of debugging: the variables
were fine (Generic resolves into `StrInput` without complaint), the *hiding* was
what stopped anyone from ever reading them.

`bound` therefore guards a narrower case than the rest: a hand-edited flow, or a
future Langflow whose merge keeps less. It is the only check that notices a field
which stopped being variable-bound instead of failing — the others iterate
`load_from_db` fields, so such a field disappears from their view entirely.

Adding a legitimate new variable means updating `NON_SECRET_VARS` / `SECRET_VARS`
at the top of the file. That is deliberate — it forces the "is this a secret?"
decision to be written where a reviewer sees it. The background is the two rules
at the top of the Postgres block in `.env.example`.

Runtime cover for the same area lives in `scripts/smoke.py` check 6, which runs a
real flow against Postgres. The two are complementary: this file guards the flow
JSON, check 6 guards everything around it (`.env`, Langflow settings, upgrades).

---

# Historical resilience tests (unsupported current helpers)

> Legacy request/DB helpers do not match current required entity, C2 roles or log names. The scenarios below are historical, not supported commands. Do not run them against production or a current installation.

Black-box tests that inject real faults into the **running dev stack** and
assert the system fails *gracefully* — no crashes, no hangs, no partial/corrupt
state — then restore the stack.

## Historical run prerequisites

These scenarios require repaired request/DB helpers and a separately owned isolated
stack. The former run commands are intentionally not offered as a current workflow.

## What each test does
| id | fault injected | asserts |
|----|----------------|---------|
| **r1** | upload a garbage `.pdf` | HTTP 200 handled, well-formed response, **0 chunk rows** (no partial/garbage data), no crash |
| **r4** | recreate `edu_ai_search` with an invalid `OPENAI_API_KEY` (`llmfault.override.yml`) | deterministic QA tier (`qa_exact`/`qa_lexical`) **still answers**; the vector/RAG (LLM) path **degrades to `no_answer`**, never 500/hang |
| **r8** | `docker stop edu_ai_postgres` | search returns a **bounded, handled** response (no hang); **recovers** after restart |
| **atom** | force a QA-insert to fail mid-transaction | the `is_active=false` deactivation **rolls back** — a failed upload never leaves the corpus "all-inactive, nothing-active" |

Each test restores what it changed in a `finally` block (recreates the search
container with the real key, restarts Postgres). Historical claim, not current support: safe to re-run.

> ⚠️ Destructive against the *dev* stack: r4/r8 briefly recreate/stop containers.
> Don't run against production.

## Findings surfaced by this suite
- **Fixed:** `answer.py` called the RAG answer LLM with no error handling → an
  OpenAI outage 500-crashed `/search`. Now it degrades to `no_answer` (r4).
- **Observation (open):** with the DB down, search degrades *silently* to
  `no_answer` (HTTP 200) because every node swallows DB errors. It never hangs
  or crashes (good), but an outage is **masked** — consider a distinct `503`
  so monitoring can see it. (r8 asserts only "handled + no hang".)

## Next tests to add (see the proposal)
R2 (empty/oversized/mismatched file), R5 (LLM 429), R7 (malformed LLM output),
R11 (partial: tables ok / embeddings fail), plus a fast **unit** layer
(`decode_html`, cz-scoring, blocklist) run in-container.

## eval_rag.py — metadata regression harness

This public checkout ships three synthetic unrestricted examples and three
scoped examples with placeholder ENTITY_CODE, not the private regression corpus.
Copy them to an ignored local set and replace the entity for your synthetic corpus.
Historical 38/46-case measurements and M cases are not reproducible from these examples.

```bash
python3 tests/eval_rag.py --questions <local_metadata.jsonl> --repeat 3
python3 tests/eval_rag.py --baseline tests/eval_out/baseline-20260822.json
# Historical --only M assumes private cases; public examples have no M IDs.
```

`--baseline` vypíše, co se pohnulo (outcome, počet citovaných dokumentů, jiný 1. zdroj) —
to je smysl setu: oprava je věrohodná teprve tehdy, když je proměřená celá sada.

Čtení výstupu: sloupec **odkud** říká, kdo odpověděl. `qa_*` = kurátorská odpověď
z QA balíčku (nemá dokumentové zdroje, proto `dok 0`), `rag` = dokumenty + LLM,
`meta` = odpověď na „co umíš", `none` = no_answer. Baseline z 22. 8.: 8 z 38 dotazů
jde přes RAG (median 4.7 s), zbytek odpoví QA balíčky do ~0.3 s.

### Případy s oblastmi (`domains`) — od 2026-09-06

Případ může nést `domains` (seznam DOMAIN kódů) a hledání se pak omezí na tyto
tematické oblasti. Scoped případy mají **vlastní soubor**, aby výchozí měření
nezměnilo význam:

```bash
python3 tests/eval_rag.py --questions tests/eval_rag_domains.jsonl --repeat 3
```

Neomezená sada zůstává neomezená — právě to z ní dělá kompatibilitní měření
(klient, který `domains` neposílá, se musí chovat úplně stejně jako dřív).
`tests/test_eval_payload.py` tuhle vlastnost souboru sám kontroluje.

**Nestačilo přidat pole do JSONL.** `eval_rag.py` skládá tělo požadavku z několika
pojmenovaných polí případu, takže pole přidané jen do JSONL se přijme, ignoruje
a **nikdy neodešle** — a běh potom měří neomezenou kaskádu, zatímco report tvrdí
opak. Proto:

* `build_payload` je samostatná funkce a `test_eval_payload.py` ji testuje přímo;
* každý výsledek nese `domains_sent` **i** `domains_effective` (z `effective_scope`
  odpovědi) a běh vypíše **scope failure**, když se ty dvě neshodují;
* bez `domains` se pole do těla **nepřidá** (ne jako `null`), takže neomezené
  tělo je bajt po bajtu stejné jako před změnou a starší baseline zůstává
  srovnatelný;
* 4xx se zaznamená s `http_status` a `error_code` — vyřazený kód (422) je jiný
  nález než timeout.

`expect` u scoped případu je **pár**: outcome a **který zdroj** odpověděl. Případ,
který vrátí správnou větu ze zdroje mimo vybranou oblast, je přesně ta chyba,
kvůli které filtr existuje, a pozná se jen podle identity zdroje.

**Omezení, které platí pro celou sadu:** snapshot je srovnatelný jen se snapshotem
z **týchž dat**. Sada je kalibrovaná na konkrétní korpus dokumentů a packů, takže
běh proti databázi s jiným korpusem (starší restore, čistá instalace jen s
fixtures) vrátí téměř všude `no_answer` — je to číslo, ale není to měření. Měření
změny kódu a měření změny dat jsou dva samostatné běhy.

This index is a selection of the main suites, not an exhaustive inventory. Legacy flat eval/resilience scripts have stale request/role/log-table assumptions; use the metadata eval harness and do not treat those scripts as supported fault-injection commands until repaired.

Do not execute legacy resilience Run blocks below/above against a current installation. They are retained historical examples and require helper repair plus a separately isolated stack.
