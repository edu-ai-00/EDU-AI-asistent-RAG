---
id: docs.ai.evaluations
status: current
components: [evaluation, retrieval, qa]
sources:
  - tests/eval_rag.py
  - tests/eval_rag_regression.jsonl
  - tests/eval_search.py
  - tests/run_all_evals.sh
  - tests/test_resilience.py
  - tests/test_route_guard.py
  - tests/test_resolve_context.py
  - contracts/parity/check_vectors.py
last_verified: 2026-08-28
---

# Evaluations

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

### Flat question sets (`evaluated`)

`tests/eval_*_questions.txt` — one question per line, blank lines and `#`
comments ignored. Useful for sweeping a topic quickly.

```bash
bash tests/run_all_evals.sh                        # all sets + CSV export
python3 tests/eval_search.py tests/eval_prijimacky_questions.txt
```

`run_all_evals.sh` exports the combined per-query table from the query log, so a
run can be read as a table rather than as scrollback.

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

## Parity contract (`enforced`)

`contracts/parity/check_vectors.py` asserts that the token forms the running
service builds still match the committed vectors. Run it **before** a change to
normalization or stemming (it must pass, proving the vectors describe today's
behaviour) and **after** (it must still pass, proving the change was
behaviour-neutral and no re-ingest is needed).

```bash
docker cp contracts/parity/. edu_ai_search:/tmp/parity/
docker exec -i edu_ai_search python - /tmp/parity < contracts/parity/check_vectors.py
```

A failure is not cosmetic: the stored index and what a query builds have diverged,
so either the change is reverted or every affected language is re-ingested.

## Resilience tests (`evaluated`)

`tests/test_resilience.py` injects real faults into a running stack and asserts
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
