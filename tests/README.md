# Tests

Two suites with opposite requirements — run the static one first, it is free:

| suite | needs | catches |
|---|---|---|
| [`test_flow_invariants.py`](test_flow_invariants.py) | nothing (stdlib, no Docker) | flow JSON that lost the attributes its DB config depends on |
| [`test_resilience.py`](test_resilience.py) | running dev stack + `requests` | ungraceful failure under injected faults |

---

# Flow invariants (static)

Asserts that the Langflow flow JSON still carries the config the flows need to
reach Postgres. The flows hold 197 hand-maintained `load_from_db` attributes and
186 `override_skip` attributes; re-exporting a flow from the Langflow UI can drop
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

# Resilience tests (fault injection)

Black-box tests that inject real faults into the **running dev stack** and
assert the system fails *gracefully* — no crashes, no hangs, no partial/corrupt
state — then restore the stack.

## Run
```bash
# from the repo root; needs Docker + `requests` on the host (no pytest required)
python3 tests/test_resilience.py            # all
python3 tests/test_resilience.py r4 atom    # selected
```
Backends are hit on **loopback** (`127.0.0.1:8080` API, `:8090` search), which
bypasses the nginx auth edge. DB state is checked via `docker exec psql`.

## What each test does
| id | fault injected | asserts |
|----|----------------|---------|
| **r1** | upload a garbage `.pdf` | HTTP 200 handled, well-formed response, **0 chunk rows** (no partial/garbage data), no crash |
| **r4** | recreate `edu_ai_search` with an invalid `OPENAI_API_KEY` (`llmfault.override.yml`) | deterministic QA tier (`qa_exact`/`qa_lexical`) **still answers**; the vector/RAG (LLM) path **degrades to `no_answer`**, never 500/hang |
| **r8** | `docker stop edu_ai_postgres` | search returns a **bounded, handled** response (no hang); **recovers** after restart |
| **atom** | force a QA-insert to fail mid-transaction | the `is_active=false` deactivation **rolls back** — a failed upload never leaves the corpus "all-inactive, nothing-active" |

Each test restores what it changed in a `finally` block (recreates the search
container with the real key, restarts Postgres). Safe to re-run.

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

## eval_rag.py — regresní set s metadaty (od 2026-08-22)

`eval_search.py` posílá jen otázku; `eval_rag.py` posílá i **roli, ročník a entitu**,
protože ty rozhodují o odpovědi stejně jako formulace — a bez nich se vlastníkovy nálezy
nedaly reprodukovat. Set (`eval_rag_regression.jsonl`, 38 případů) je vlastníkových 9
otázek + 29 ručních dotazů z testování 21.–22. 8. 2026, každý se slovním očekáváním
(`expect`) a tím, co systém udělal tu noc (`observed_21_8`).

```bash
python3 tests/eval_rag.py                                      # běh + snapshot do eval_out/
python3 tests/eval_rag.py --baseline tests/eval_out/baseline-20260822.json
python3 tests/eval_rag.py --only M                             # jen vlastníkovy
```

`--baseline` vypíše, co se pohnulo (outcome, počet citovaných dokumentů, jiný 1. zdroj) —
to je smysl setu: oprava je věrohodná teprve tehdy, když je proměřená celá sada.

Čtení výstupu: sloupec **odkud** říká, kdo odpověděl. `qa_*` = kurátorská odpověď
z QA balíčku (nemá dokumentové zdroje, proto `dok 0`), `rag` = dokumenty + LLM,
`meta` = odpověď na „co umíš", `none` = no_answer. Baseline z 22. 8.: 8 z 38 dotazů
jde přes RAG (median 4.7 s), zbytek odpoví QA balíčky do ~0.3 s.
