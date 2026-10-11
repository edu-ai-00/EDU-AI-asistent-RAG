# tools/lab/cycle — záloha, update a obnova jednoho ověřeného laboratorního layoutu

> **Stav: laboratorní driver pro jediný layout, bez podpory obecného produkčního upgradu.**
> Nepoužívat na produkci, školní instalaci ani cizí stack. Migrační runner B03e se zde nepoužívá.
> Veřejný checkout neobsahuje soukromé důkazy skutečné obnovy.

Na rozdíl od starších [`tools/lab/acc_*.sh`](../README.md) před každým přístupem k cílovému stacku ověří hostname, výchozí Docker engine
(root dir), Compose projekt a štítky všech kontejnerů, pracovní adresář, mounty (jen uvnitř `src`/`config`/`state`),
symlinky a absolutní kanonické cesty. Cizí kontejner na enginu, jiný adresář nebo jiné jméno hostitele = odmítnutí.

## Příkazy (`cycle.py`)

Layout: `--src --cfg --state --project --expect-host --backup-root` nebo `EDU_AI_SOURCE_ROOT`, `EDU_AI_CONFIG_DIR`,
`EDU_AI_STATE_DIR`, `EDU_AI_COMPOSE_PROJECT`, `CYCLE_EXPECT_HOST`, `CYCLE_BACKUP_ROOT`; dále `--evidence-dir` a
`--running-from` (adresář, ze kterého běžící kontejnery vznikly, výchozí `--src`).

| Příkaz | Co dělá |
|---|---|
| `gate` | jen ověří vlastnictví stacku |
| `egress ensure|check|probe` | DROP pravidla pro sítě `edge` a `bifrost_egress` v `DOCKER-USER`, ověření sondami zevnitř kontejnerů a kladnou kontrolou |
| `backup NAME` | uloží image (`docker save`), zastaví všechny služby kromě `postgres`, pořídí raw baseline, dumpy, globals, role, soubory, named volumes a vše ověří; služby znovu spustí |
| `verify-backup NAME` | úplná kontrola zálohy (SHA-256, úplnost, bezpečnost archivů, TOC dumpů) |
| `update --from-src A --backup NAME --sha SHA` | build a `up --force-recreate` z cílového checkoutu nad stejným configem/state/projektem |
| `markers put|check ID` | syntetické značky po záloze (soubory v každém mountu, large object v obou DB) |
| `compare-live NAME` | rozdíl živého stavu proti záloze (počty, ne obsah) |
| `restore NAME --confirm-overwrite-own-stack` | zastaví a odstraní kontejnery stacku, uloží původní stav stranou (`pre-restore-*`), načte image, obnoví soubory a čistý PostgreSQL klastr, spustí `db_roles.py reapply` a `verify`, porovná syrový stav; **aplikace nespouští** |
| `verify-restore NAME` | opakuje porovnání dokončené obnovy proti celé záloze, jen když aplikace neběží; při shodě vytvoří novou značku |
| `start-apps NAME` | ověří značku téže zálohy i host/projekt/cesty, znovu provede úplné porovnání a kontrolu původních image; značku spotřebuje před spuštěním aplikací |

`cycle_checks.py` (neplacené funkční kontroly, volitelně `--smoke --export`) a `cycle_export.py` (skutečný SQLite export).
Samostatné `cycle_export.py` a čtecí `cycle_flowdiff.py` nyní také vyžadují úplný layout
`--src --cfg --state --project --expect-host --backup-root`, případně `--running-from`.
Flow diff navíc používá `.env` z `--cfg` a odmítá Langflow URL mimo loopback nginx `http://127.0.0.1:8444` tohoto laboratorního layoutu.
`cycle_checks.py --export` předává exporteru stejný ověřený layout.

## Záruky a hranice

- Zálohu nelze použít, dokud neprojde `verify_bundle`: seznam souborů přesně odpovídá `SHA256SUMS`, všechny hashe sedí,
  žádný symlink, žádný archiv s absolutní cestou, `..`, hardlinkem, zařízením nebo zápisem přes symlink.
- Chyba před bodem bez návratu nic nezničí. Pozdější chyba obnovy (image, soubory, `pg_restore`, role, porovnání)
  ukončí příkaz bez spuštění aplikací; `restore_ok.json` se na začátku maže a bez něj `start-apps` odmítne.
- Značka je jednorázová, vázaná na celý layout. `start-apps` znovu kontroluje SHA-256 zálohy,
  živá data, konfiguraci, soubory, zdroj a původní image/tagy; stará značka sama není důkaz aktuálního stavu.
  Update a zápis syntetických značek ji zneplatní. Značka starší verze bez layoutu se odmítne;
  novou lze získat jen přes `verify-restore` s vypnutými aplikacemi a úplnou shodou.
- Cache (`reranker_models`) se nezálohuje; `postgres_data` kryjí logické dumpy (fyzický PGDATA jen kopie stranou při obnově).
- Zálohy obsahují tajnosti (`.env`, klíče, hashe hesel): adresář 0700, soubory 0600, jen na VM; výstup nástrojů nese jen názvy,
  počty a otisky.
- Chyby subprocessů zveřejňují pouze návratový kód nebo typ selhání. Argumenty, stderr/stdout,
  third-party exception texty a neznámá pole probe odpovědí se do konzole ani JSON evidence nekopírují.
  Rozdílné položky schématu se v JSON uvádějí otiskem; soukromý bundle ponechává podklady pro diagnostiku.
- Názvy záloh a syntetických značek jsou jeden bezpečný ASCII segment o nejvýše 64 znacích.

Testy: `python3 tests/test_cycle_driver.py` (offline, bez Dockeru; bundle, cesty, odmítnutí, pořadí obnovy).

## Ověření

Offline testy kontrolují bundle, cesty, odmítnutí, pořadí obnovy a brány CLI.
Konkrétní nasazení vyžaduje vlastní skutečný cyklus se zaznamenaným SHA, layoutem,
image a součty. Offline PASS neprokazuje živou obnovu ani obecný produkční upgrade.
