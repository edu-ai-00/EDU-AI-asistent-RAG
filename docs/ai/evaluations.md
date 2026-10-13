---
id: docs.ai.evaluations
status: current
components: [evaluation, retrieval, qa]
sources:
  - tests/eval_rag.py
  - tests/eval_rag_regression.jsonl
  - tests/eval_rag_domains.jsonl
  - tests/eval_search.py
  - tests/run_all_evals.sh
  - tests/test_resilience.py
  - tests/test_route_guard.py
  - tests/test_resolve_context.py
  - tests/test_domain_scope.py
  - tests/test_domain_scope_api.py
  - tests/test_eval_payload.py
  - tests/domain_scope_fixture.py
  - contracts/parity/check_vectors.py
last_verified: 2026-09-06
---

# Evaluations

Public shipped cases are synthetic placeholders, not the private benchmark. Use an ignored local metadata set with a real active entity and matching synthetic corpus; do not compare it with historical private scores.

**Track:** human (AI-relevant). How search quality, behaviour and resilience are
checked, and where the gaps are. Control types as in [routing.md](routing.md).

## Two harnesses, for two different questions

| Harness | Answers |
|---|---|
| `tests/eval_rag.py` | "did this change move anything?" — a regression set with per-question metadata and a snapshot diff |
| `tests/eval_search.py` | "what does this batch of questions do?" — a flat list, outcome distribution, eyeballing |

### Regression set (`evaluated`) — the primary harness

`tests/eval_rag_regression.jsonl` holds one case per line, and **each case carries
the role, the year and the legal entity** alongside the question. That is the
point of the file: those three inputs decide the answer as much as the wording
does, so a bare question list cannot reproduce a reported problem.

```bash
python3 tests/eval_rag.py                          # run and write a snapshot
python3 tests/eval_rag.py --repeat 3               # ask each question 3x, keep the majority
python3 tests/eval_rag.py --baseline tests/eval_out/<file>.json
```

**The snapshot is the deliverable.** A fix is only credible if the whole set is
re-measured; `--baseline` prints exactly what moved — outcome, cited sources,
answer text.

**`--repeat` exists because the set has a noise floor.** Two runs of identical
code disagree on a handful of cases, because the answering model decides
borderline ones differently. A diff of one case therefore says nothing. With
`--repeat N` each question is asked N times, the case keeps the **majority**
result, and the report marks how many runs disagreed with it. Flapping cases are
printed separately instead of being mixed into the verdict.

This is the difference between measuring and guessing: without the repeat, a
one-case "improvement" is indistinguishable from the model changing its mind.

**What `--repeat` cannot do.** It measures the model's variance, not the
corpus's. The set is calibrated against a specific document and pack corpus, so
a snapshot is only comparable with another snapshot taken **on the same data**.
Two consequences worth stating because both have been assumed away before:
measuring a code change and measuring a data change are separate runs, and a
run against a database whose corpus differs (a stale restore, a fresh install
with only fixtures) produces `no_answer` almost everywhere — a number, but not a
measurement.

#### Scoped cases (`evaluated`)

A case may carry `domains`, a list of DOMAIN codes, which restricts the search
to those thematic areas. They live in **their own file** so the default
measurement keeps its meaning:

```bash
python3 tests/eval_rag.py --questions tests/eval_rag_domains.jsonl --repeat 3
```

The unrestricted set stays unrestricted, and that is what makes it the
compatibility measurement — a client that sends no `domains` must behave exactly
as before. `tests/test_eval_payload.py` asserts that property of the file
itself.

**The harness had to be extended, not just the JSONL.** `eval_rag.py` builds
its request body from a handful of named case fields, so a field added to the
JSONL and not to the builder is accepted, ignored and **never sent** — and the
run then measures the unrestricted cascade while the report says otherwise.
Every number in such a report is wrong and nothing about it looks wrong. So:

- `build_payload` is a separate function, and `tests/test_eval_payload.py`
  asserts against it directly (pure, no stack, milliseconds);
- each result records `domains_sent` **and** `domains_effective` (read from the
  response's `effective_scope`), and the run prints a **scope failure** when the
  two disagree, rather than a set of outcomes;
- `domains` is **omitted** from the body when a case does not name it, so an
  unrestricted body is byte-identical to the pre-feature one and an older
  baseline stays comparable;
- a 4xx is recorded with its `http_status` and `error_code` instead of collapsing
  into one `ERROR` string — a retired code (422) is a different finding from a
  timeout.

**`expect` on a scoped case is a pair, not a phrasing:** the outcome *and which
source answered*. A case that answers the right sentence from a source outside
the chosen area is the defect the whole filter exists to prevent, and only the
source identity shows it. Do **not** use the majority eval for the 422 and
failure cases — those are deterministic and belong in
`tests/test_domain_scope_api.py`; the scoped set keeps one of each only so that
the harness is shown to surface them.

**On an installed server** port 8090 is not published (only the development
override publishes it), so `eval_rag.py` defaults to an address that is not there.
Copy the script and an ignored case file into the search container and run it there,
where `127.0.0.1:8090` is the service itself; the snapshot then stays in the
container, not in the checkout (a dirty checkout stops the wizard):

```bash
docker cp tests/eval_rag.py edu_ai_search:/tmp/eval_rag.py
docker cp <local-cases>.jsonl edu_ai_search:/tmp/cases.jsonl
docker exec -w /tmp edu_ai_search python eval_rag.py --questions cases.jsonl --repeat 3
```

The cases must name entity codes that are active in that database; a retired code
returns `400 unknown or inactive legal entity` for every case, before any paid call.
The cost of a run can be read from Bifrost (`/api/logs/stats`, `total_cost`).

### Legacy flat question sets

`eval_search.py` and `run_all_evals.sh` are retained historical tools. They do not
send the required legal entity and still assume obsolete DB roles/log names;
they are not supported commands for the current stack. Use `eval_rag.py` with
an explicit metadata JSONL carrying entity and role. Public sample sets are
synthetic examples; a corpus-specific regression set remains private.

## Metrics

- **Outcome distribution** — how many questions ended as an answer, a
  suggestion, or silence.
- **Per-question row** — the matched entry, latency, and the answer's origin on
  two axes: the source (curated QA, documents, guard, none) and the strategy that
  found it (exact, lexical, keywords, vector). A relevance score in 0..1 is
  recorded for every answering path, disambiguated by the strategy that produced
  it.
- **Instability** — with `--repeat`, how many runs disagreed with the majority.

There are **no hard pass/fail thresholds** in the harness (`intended`): results
are read as a distribution and compared against a baseline, not asserted.
Automated thresholds and CI wiring are a known gap, and the curated-QA decision
thresholds are themselves uncalibrated (see [qa-fastpath.md](qa-fastpath.md)).

## Behavioural tests (`evaluated`)

| Test | Asserts |
|---|---|
| `tests/test_route_guard.py` | the guard admits and blocks the right questions, and a self-harm hit answers with support rather than a refusal |
| `tests/test_resolve_context.py` | follow-up rewriting, including that it stays off when disabled |
| `tests/test_flow_invariants.py` | the ingestion flows keep the invariants their components rely on |
| `tests/test_domain_scope.py` | which ROWS the area filter admits, against a real database |
| `tests/test_domain_scope_api.py` | the area contract over three real HTTP origins |
| `tests/test_eval_payload.py` | the eval harness sends what the case files say |

### Thematic area scope (`evaluated`)

Two files, and the split is the point.

`tests/test_domain_scope.py` runs **inside the search image** against a
synthetic fixture (`tests/domain_scope_fixture.py`) and asserts **which rows
become candidates** — which entry answered, which chunk texts are in the pool,
which chips were offered. It deliberately never asserts on the shape of a SQL
string: a test that greps for `domain_ids &&` passes when the fragment is built
and its parameters are interpolated in the wrong order, which is exactly how
this kind of change breaks.

The fixture is built and dropped in a `finally`, including after a failure, and
everything in it is invented — the seed's fictional schools, questions about a
school that does not exist, every row prefixed `ds-` so teardown is exact. Its
QA rows are built with the **same** `eduai_normalize` and the same active
settings the cascade uses, because `qa_exact` compares stored reduced forms for
equality and a hand-written guess would silently never match the branch the test
claims to cover. Its vectors are hand-made unit vectors, so the ANN steps are
exercised with no embedding API call and an order that is arithmetic.

`tests/test_domain_scope_api.py` runs **on the host** against three different
origins — internal `:8090`, the nginx edge `:80/search/…`, and the BFF `:8081` —
because an in-process TestClient is no evidence that a path is publicly
reachable. `/search/v1/domains` answered 404 at the edge while being perfectly
correct in the app, which is the whole reason this file exists separately. It
asserts what only a real request shows: status, `Content-Type`, the error
envelope, `effective_scope`, SSE event order, the `Idempotency-Replayed` header,
and **the row that reaches `question_answer_log`** — `log_search` swallows every
exception by design, so a wrong column count would otherwise be invisible.

Fault injection covers the cases where a filter would be most tempting to drop:
an unreadable catalog with an explicit filter is a declared 503 and never an
unfiltered search; a failing retrieval step falls through to the next
*restricted* step; a lost history costs the history and not the filter.

## Parity contract (`enforced`)

`contracts/parity/check_vectors.py` asserts that the token forms the running
service builds still match the committed vectors. Run it **before** a change to
normalization or stemming (it must pass, proving the vectors describe today's
behaviour) and **after** (it must still pass, proving the change was
behaviour-neutral and no re-ingest is needed).

```bash
docker cp contracts/parity/. edu_ai_search:/tmp/parity/
docker exec -i -w /tmp/parity -e PYTHONPATH=/app edu_ai_search \
  python - /tmp/parity < contracts/parity/check_vectors.py
```

Piped via stdin, the script finds `stoplist.json` in its working directory, so
`-w /tmp/parity` is required; `PYTHONPATH=/app` keeps the `app` package importable.

A failure is not cosmetic: the stored index and what a query builds have diverged,
so either the change is reverted or every affected language is re-ingested.

## Legacy resilience harness (not a current release gate)

`tests/test_resilience.py` is historical fault-injection code whose request/DB helpers require repair. Do not run it against production or use its current commands as a supported release gate. Its intended checks assert
graceful failure: a garbage upload, the model provider down (deterministic curated
answers still work, generation degrades to silence), Postgres down (a bounded
handled response, then recovery), and a rolled-back QA insert. A covered subset
only; the full matrix and CI are gaps.

## Known gaps

- **No golden answers.** The sets carry questions, not expected answers;
  correctness is judged by reading and by diffing against a baseline.
- **No relevance metrics** (precision, recall, nDCG) over document retrieval.
- **A masked provider outage looks like an empty corpus.** Because generation
  failure degrades to silence, a run full of `no_answer` can mean either. Check
  the service log before concluding the corpus is at fault.
