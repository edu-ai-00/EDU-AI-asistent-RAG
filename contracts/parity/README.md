# Parity contract — Czech normalization / lemma / stem (Python ↔ Dart)

**Track:** contract · **Language:** English

The Czech normalization + Dolamic–Savoy light stemmer exist twice: in the server
(Python, `search/app/normalize.py` · `stemmer.py` · `lemmatizer.py`, unified by
`search/app/text_norm.py`) and in the offline Flutter engine (Dart, separate app
repo). Their behavioral equality is a **contract**: the same input MUST reduce to
the same `norm` / `lemma` / `stem` tokens on both sides, or the on-device query
tokens the app sends (`query_*_tokens`) stop matching the server-built index.

## Canonical definition

`cs-normalization-vectors.json` is the single source of truth for "normalized".
Each vector is `input → {norm, lemma, stem}`. Both language test suites MUST
consume this file; docs never restate the stemming rules in prose beyond a
one-paragraph summary (see [`../../docs/ai/retrieval.md`](../../docs/ai/retrieval.md)).

The vectors are **generated from the live Python normalizer** — never hand-edited.

## Regenerate

```bash
docker exec -i edu_ai_search python - \
  < contracts/parity/gen_vectors.py \
  > contracts/parity/cs-normalization-vectors.json
```

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
