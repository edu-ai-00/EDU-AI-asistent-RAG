# tools — rozcestník

Ruční pomůcky pro vývoj a ladění. Nejsou součástí běhu aplikace ani instalace a runtime obrazy je neobsahují. Přesný
způsob spuštění je v docstringu každého souboru; tady je jen přehled. Provozní skripty jsou v [scripts/](../scripts/README.md),
kontrola dokumentace v [docs/tools/](../docs/tools/docs_check.py) (`python docs/tools/docs_check.py`).

| Cesta | K čemu | Poznámka |
|---|---|---|
| [lab/](lab/README.md) | laboratorní záloha a obnova izolovaného syntetického stacku | ověřeno jen pro uvedené SHA-256; nikdy na produkci ani proti cizímu stacku; [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md), oddíl 5 |
| `stopwords/` | export a načtení stoplistu (`qa_stopword`) a přepočet tvarů synonym (`qa_synonym`) | pouští se přes `docker exec -i edu_ai_search python - < …`; postup a důvody: [2026-09-05_stoplist_a_normalize_3.md](../docs/nasazeni/2026-09-05_stoplist_a_normalize_3.md) |
| `measure_related_chips.py` | porovnání dvou nastavení souvisejících návrhů na skutečných otázkách z logu | volá uzly přímo, nic nezapisuje, LLM neběží; souvislosti: [qa-fastpath.md](../docs/ai/qa-fastpath.md) |
| `report_chip_repeats.py` | co návrhy ve skutečnosti udělaly, čteno z `question_answer_log` | jen čtení; `tools/` se do obrazu `edu_ai_search` připojuje přes `-v` |
| `search_test.html`, `upload_test.html` | ruční vývojové stránky pro `/v1/ask` a nahrání dokumentu | otevírají se z disku a volají `127.0.0.1:8090` a `:8080`; z jiného původu je nutné lokálně nastavit `CORS_ORIGINS` ([.env.example](../.env.example)) |
| `test_inputs/pipeline_input_o_studiu.json` | vzorový vstup ingestní pipeline (anonymizovaný) | ve sledovaném stromu na něj nic neodkazuje |

Cesty zůstávají pevné: odkazují na ně dokumentace, komentáře v kódu a postupy nasazení.
