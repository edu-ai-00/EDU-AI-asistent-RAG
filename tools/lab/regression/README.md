# tools/lab/regression — regression set for the curated-QA search

> **Status: lab tool.** It runs inside the search container against the live
> database, but in one transaction that is rolled back; it writes nothing to the
> database. The cases and the results are private data (a school's questions) —
> keep them outside the repository, e.g. in `~/edu-ai/state/regression/`.

A search change is accepted or rejected by numbers, not by single examples: the
same cases are run before and after, and the cases that got worse are listed.

| Script | Runs | Does |
|---|---|---|
| [`build_cases.py`](build_cases.py) | host | builds `cases.json` from the school's curated questions and sets of written test queries whose right answer is known; `--extra` adds hand-approved cases |
| [`run.sh`](run.sh) / [`run.py`](run.py) | search container | puts the curated questions in as temporary packs, runs every case through prepare → qa_exact → qa_lexical, writes a verdict per case |
| [`compare.py`](compare.py) | host | baseline × now: score per set, cases that got worse (new confident wrong answers first), CSV of the changes; exit code 1 on a new confident wrong answer |
| [`check_step.sh`](check_step.sh) | host | kontrola po každém kroku: testy, docs-check, parita, regresní sada proti přijatému základu, stav gitu (viz [PLAN.md](PLAN.md) §4) |
| `make_negatives.py`, `make_clicks.py`, `make_d_template.py`, `make_equiv_candidates.py` | host / kontejner | příprava sad N, C, D a kandidátů na ekvivalentní odpovědi (viz Sady a zásady) |
| `make_log_set.py` | host | sada L: skutečná znění z logu se slabým štítkem (volba OpenAI) |
| `analyze_errors.py` | search container | rozbor chyb: zařadí nevidené a jistě špatné případy do skupin (jen A, B, D:train), s podílem, který by lokální model našel |
| `eval_semantic.py` | search container | fáze 3: zkouší, kolik správných odpovědí by do nabídky přidal lokální model (politiky fill / lex2 / rrf, práh τ), a kolik tlačítek přidá otázkám mimo obsah; `D:holdout` odmítá |
| [`scoring.py`](scoring.py) | both | verdict and score (stdlib only, unit-tested in `tests/test_lab_regression.py`) |

## Case

`{id, set, pack, entry_key, query, category}`. `entry_key` is the curated answer
that must come back. `null` means no confident answer is right (ambiguous
question, or the answer is missing from the data): the offer "Nechtěl ses
zeptat…?" is correct, a confident answer is the failure.

Score per case: confident right **+1**, right one in the offer **+0.5**, nothing
or the offer without the right one 0, confident **wrong −3** (it says nothing
like "Nechtěl ses zeptat…?", the asker takes it for the answer).

## Use

```bash
# 1. once: the cases (A = written paraphrases, B = an independent set of other authors)
python3 tools/lab/regression/build_cases.py --questions otazky_idx.json \
    --set A=prepisy.json --set B=heldout.json --out cases.json

# 2. the baseline of the code as it is now
tools/lab/regression/run.sh otazky_idx.json cases.json base.json

# 3. after a change (code in this checkout, a setting, or SQL tried in the transaction)
tools/lab/regression/run.sh otazky_idx.json cases.json now.json \
    --config '{"query_repair": ["typo"]}' --with-sql what_if.sql
python3 tools/lab/regression/compare.py --cases cases.json --baseline base.json --now now.json --out diff.csv
```

The search code of this checkout is copied into the container, so an experiment
that is not in the image yet can be run. `--with-sql` (repeatable) is applied FIRST
and the active setting is read afterwards, so a migration that changes the
settings is measured exactly as written; `meta.qa_lexical` in the result file
holds the values the run used. `--config` overrides `qa_lexical` params on top,
for the run only.

Plán dalšího zlepšování hledání (cíl, omezení, pravidlo přijetí změny, etapy):
[PLAN.md](PLAN.md).

## Sady a zásady (trénink × test)

| Sada | Co to je | Použití |
|---|---|---|
| **A** | 1 470 napsaných přepisů 210 otázek školy | **trénovací**: ladí se na ní |
| **B** | 1 240 přepisů jiného stylu | **validační**: už se podle ní rozhodovalo, počítá se za „spotřebovanou“ |
| **D** | otázky, které napíše člověk podle *odpovědí* (viz `make_d_template.py`) | **testovací, zmrazená**: nikdy se podle ní neladí, jen závěrečné ověření |
| **N** | otázky mimo obsah školy (`make_negatives.py`) | očekává se žádná jistá odpověď; trénovací a zadržená půlka |
| **D:train / D:holdout** | půlky D podle otázky školy | `train` smí sloužit k volbě mezi variantami (např. modelů), `holdout` jen na závěr |
| **L** | skutečné dotazy z logu, které produkce vyřešila přes OpenAI (`make_log_set.py`); štítek = jeho volba | jen test (slabý štítek, výběrově zkreslená); neladí se na ní |
| **C** | dotazy z logu, kde uživatel klikl na nabídku *místo odpovědi* (`make_clicks.py`) | jen orientační (17 případů), zkreslené štítky |

1. **Dělení podle otázek školy, ne podle vět.** Každá z 210 kurátorovaných otázek je deterministicky `train` (70 %) nebo `holdout` (30 %), `build_cases.split_of`. Přepisy A i B jsou ze stejných otázek, takže dělení podle vět by nechalo ladění prosvítat do měření. Ladí se jen na `train`; závěrečné číslo se bere z `holdout`. `compare.py --by-split` ukáže obě půlky zvlášť.
2. **Zmrazené sady (D, `holdout` a N) se nepoužívají k rozhodování**, jen k závěrečnému ověření. Jakmile se podle sady rozhodne, je „spotřebovaná“ (B už je).
3. **Šum:** `compare.py` vypisuje u každé sady interval spolehlivosti změny skóre (párový bootstrap). Změna, jejíž interval obsahuje 0, je šum a nepočítá se jako zlepšení.
4. **Ekvivalentní odpovědi:** záznamy s téměř shodnou odpovědí navrhne `make_equiv_candidates.py`, člověk potvrdí ano/ne a `build_cases.py --equiv-csv` je bere jako stejně správné.
5. **Deník pokusů:** každý přijatý i zamítnutý pokus se zapíše do [LEDGER.md](LEDGER.md) s čísly.

Opravy **chybných štítků** po prohlídce výsledků (otázka „bez odpovědi“, na kterou škola odpověď má) se dělají souborem `--fixes` (`drop` / `relabel`) a zapisují se do [LEDGER.md](LEDGER.md); výsledky se tím neupravují.

```bash
# všechny sady najednou (soubory s dotazy zůstávají mimo Git)
python3 tools/lab/regression/build_cases.py --questions otazky_idx.json \
    --set A=prepisy.json --set B=heldout.json --extra clicks.json --negatives negatives.json \
    [--d-csv sada_D_vyplnena.xlsx --d-map sada_D_mapa.json] [--equiv-csv shodne_odpovedi_kandidati.xlsx] [--fixes sada_fixes.json] --out cases.json
```

## Limits

- Cases only cover what the school's curated questions are written from; a
  question nobody wrote an answer for is not a case until it gets `entry_key: null`
  or an answer through the curator's decision (the queue of logged questions).
- A written set measures how well search copes with *those* paraphrases. Hold
  one set (B) back when tuning, otherwise the numbers only describe the set.
- Production decisions of the curator (queue of logged questions) are not wired
  in yet: add them through `--extra`.
- Set C is tiny: in the log most offers stand BESIDE a confident answer (related
  questions); only a click on an offer that replaces the answer is a label.
