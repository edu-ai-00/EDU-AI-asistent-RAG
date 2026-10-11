# Parity contract — Czech normalization / lemma / stem (Python ↔ Dart)

**Track:** contract · **Language:** English

The server implements Czech normalization + the Dolamic–Savoy light stemmer
(Python, `search/app/normalize.py` · `stemmer.py` · `lemmatizer.py`, unified by
`search/app/text_norm.py`). A compatible offline Flutter/Dart QA engine MUST reduce
the same input to the same `norm` / `lemma` / `stem` tokens: this equality is a
**contract**, not proof that the client port already exists. The known
[EDU-AI APP main](https://github.com/edu-ai-00/EDU-AI-asistent-APP/tree/e653aea54d5e408bd3667f4ab087e0920814cc76),
inspected 2026-10-11, contains the mobile application but no corresponding QA pack
engine/normalizer port. See the [QA offline audit](../../docs/integrations/offline-qa-rules.md).
A future client sending `query_*_tokens` must satisfy these vectors before it can
reliably match the server-built index.

## Canonical definition

`cs-normalization-vectors.json` is the single source of truth for "normalized".
Each vector is `input → {norm, lemma, stem}`. Both language test suites MUST
consume this file; docs never restate the stemming rules in prose beyond a
one-paragraph summary (see [`../../docs/ai/retrieval.md`](../../docs/ai/retrieval.md)).

The vectors are **generated from the live Python normalizer** — never hand-edited.

## Regenerate

```bash
docker cp contracts/parity/. edu_ai_search:/tmp/parity/
docker exec -i -w /tmp/parity -e PYTHONPATH=/app edu_ai_search python - \
  < contracts/parity/gen_vectors.py \
  > contracts/parity/cs-normalization-vectors.json
```

Piped via stdin, the generator reads `stoplist.json` from its working
directory, hence `-w /tmp/parity` and the copy before it; `PYTHONPATH=/app`
keeps the `app` package importable from there.

Add Czech edge cases by editing `CASES` in `gen_vectors.py`, then regenerate.
Coverage today: diacritics, case folding, numerals/dates, hyphenation, mixed
CZ/SK, the `přijímačka` family, stopword dropping.

## Change rule (also an Always rule in `../../AGENTS.md`)

Any change to normalization or stemming MUST update these vectors first
(spec-style), then both implementations (Python + Dart). Order-of-operations
note: reducers run on the diacritics-intact word and ASCII folding is the LAST
step (`search/app/normalize.py`), because simplemma's dictionary and the
CzechStemmer rules both need the diacritics.

All example inputs are synthetic. Never add real learner content or real school
names.
