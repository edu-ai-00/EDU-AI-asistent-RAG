# Development and testing guide

> **Audience:** people changing the code, the flows or the configuration. Running an installation on a
> Linux server is a different job: see [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md). How a source package or an
> image is cut: [RELEASING.md](RELEASING.md).

Where things live:

| I need… | Go to |
|---|---|
| a local stack from a clean clone | [ONBOARDING.md](../ONBOARDING.md) |
| the execution contract for AI coding agents (commands, repo map, boundaries) | [AGENTS.md](../AGENTS.md) |
| every configuration parameter | [SETTINGS_REFERENCE.md](SETTINGS_REFERENCE.md) |
| the architecture and the data flow | [architecture/system.md](architecture/system.md) |
| how retrieval, routing and caching work | [ai/](ai/) |
| the HTTP and error contracts | [../contracts/](../contracts/) |
| a script or a manual tool, by purpose | [../scripts/README.md](../scripts/README.md), [../tools/README.md](../tools/README.md) |

Source precedence when sources disagree is defined in [AGENTS.md](../AGENTS.md#source-precedence); a conflict
is a defect to report, not something to resolve silently.

## 1. Working tree rules

- **Tests live in the repository, next to the code, and stay there.** A test that needs a fixture, a database,
  a running stack or a Linux host is not removed or skipped away for that reason; its prerequisites are
  documented (section 3). `tests/` is part of the **source checkout**; it is deliberately **not** inside the
  runtime images (for example `search/Dockerfile` copies only `app/` and `eduai_normalize/`), so tests that run
  in an image are copied or mounted in first.
- Never commit secrets, private QA data (`QA/` holds school content), runtime state or `tests/eval_out/` (ignored
  on purpose: evidence and handoff bundles stay local).
- Generated files (parity vectors, clean-install dumps, baseline and release manifests) are regenerated, not
  edited by hand; the commands are in [AGENTS.md](../AGENTS.md#commands).
- SQL and shell files keep LF endings, also on Windows ([`.gitattributes`](../.gitattributes)).
- New Ubuntu host preparation is `scripts/prepare_ubuntu.sh`; the application stays under the
  ordinary installation user. Its offline guards are covered by `tests/test_prepare_ubuntu.py`.

## 2. Local stack

For a fresh isolated Ubuntu installation use [CLEAN_INSTALL.md](CLEAN_INSTALL.md). The development walkthrough in [ONBOARDING.md](../ONBOARDING.md) is historical; `bootstrap.py` alone does not supply all current clean-install prerequisites. Search is a baked image: after a code
change run `docker compose build edu_ai_search && docker compose up -d edu_ai_search`.

On a Windows host without `make`, or where `python3` is only a stub, run the commands directly
(`python docs/tools/docs_check.py`); the Makefile targets are one-liners. If a test prints non-ASCII symbols and a legacy console code page raises `UnicodeEncodeError`, set `PYTHONUTF8=1`.

## 3. Tests by what they need

Each test file states its own prerequisite and exact command in its docstring; that is authoritative.
[../tests/README.md](../tests/README.md) adds the domain-scope, nginx S2 and R2 suites. Summary of the tiers:

| Tier | Needs | Examples (all in `tests/`) |
|---|---|---|
| **0. offline** | Python and the dependencies of the particular test (for example `pandas`, `pytest`, or API dependencies); some also use `git` or local SQLite. No running stack or provider API; fake APIs may use loopback | `test_flow_invariants.py`, `test_eval_payload.py`, `test_qa_export_csv.py`, `test_migrate_lint.py`, `test_migrate_fingerprint.py`, `test_flow_rollout.py` (fake Langflow on loopback), `test_sqlite_qa_export.py`, `test_qa_ingest_status.py` (`python3 -m pytest`), the `test_bootstrap_*` family, `test_layout.py`, `test_host_preflight.py`, `test_chat_entity_input.py`; `test_cloudflare_optional.py` also needs the `docker compose` CLI to render the model (no daemon is changed) |
| **0L. offline, Linux** | as tier 0, but written for Linux (permissions, signals, directory semantics) | the `test_install_*` family (`install.py` behaviour) |
| **1. inside the search image** | the search package importable: run in the `edu_ai_search` image; no DB | `test_route_guard.py`, `test_guard_negation.py`, `test_resolve_context.py`, `test_related_chips.py`, `test_qa_exact_related.py`, `test_route_identity.py`, `test_llm_variants.py`, `test_langdetect_caps.py`, `test_qa_admin_*` (fake cursor, no database; need the search dependencies); `test_server_log_privacy.py` also runs on the host |
| **2. dev stack** | the running dev stack (database, search, nginx, web chat) | `test_domain_scope.py` (in the search image), `test_domain_scope_api.py`, `test_domain_migration.py`, `test_resilience.py` (fault injection on the live dev stack), `test_qa_entry_domains.py` (Langflow dev container) and `test_qa_writer_*` (isolated schema in a live PostgreSQL; they skip cleanly when none is reachable), `test_school_domains_migration.py` |
| **3. isolated database** | its own throw-away PostgreSQL container, **synthetic data only** | `test_migrate_runner.py` (`tests/migrate_test_env.py up <name>`), `test_db_role_contract.py` live part (`EDU_AI_ROLE_TEST_CONTAINER`) |
| **4. isolated lab** | Docker plus an isolated Linux executor; own synthetic upstreams, no `.env` | `nginx_s2/`, `nginx_r2/` (see their READMEs) |
| **5. evaluation** | live stack and the production-like corpus; the harness has a noise floor | `eval_rag.py`, `eval_search.py`, `run_all_evals.sh` |

Rules that matter more than the table:

- **Do not claim a pass for a tier you did not run.** A test that cannot run for lack of its environment is
  reported as *not run, because* its prerequisite is missing, never as a pass and never as a defect of the code.
- The domain-scope suites share one fixture and must be run **one at a time**
  ([../tests/README.md](../tests/README.md)).
- `eval_rag.py` needs the production corpus; a backup from another date is not a baseline. Re-measure with
  `--repeat` before claiming an improvement: a single-case diff proves nothing.
- Migration runner and role tests touch only isolated synthetic databases, never production or a school
  installation.

## 4. Before you call a change done

1. Add or extend tests for the behaviour change; run the tiers it touches.
2. `python3 docs/tools/docs_check.py` (`make docs-check`) passes.
3. Normalization or stemming change: parity check **before and after**, vectors regenerated first
   ([AGENTS.md](../AGENTS.md#commands)).
4. `git diff --check` is clean and only intended paths are staged.
5. Docs you touched: every `sources:` path valid, `last_verified` bumped only after actually checking them.
6. Anything not verified is stated as such in the commit message or the handoff.
