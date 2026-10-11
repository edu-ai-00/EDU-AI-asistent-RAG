# C2: role a oprávnění v PostgreSQL (smluvní návrh, ověřeno jen na izolované syntetické laboratoři)

**Rozsah:** izolované syntetické instalace (laboratorní VM). Neplatí pro produkci ani školní instalace; ty tento kontrakt zatím nepokrývá. Schválil vlastník 2026-10-09 (záznam `C2_ROLES_APPROVAL.txt`). Nahrazuje starší návrh `db-roles-proposal-20261009.md` (fáze 1, nebyl schválen k provedení).

## 1. Role

| Role | Přihlášení | Smí |
|---|---|---|
| `edu_ai_admin` | ano, superuživatel | údržba, obnova, správa kontraktu; **nikdy přihlašovací údaj aplikace**, heslo zůstává v `.env`, kontejnery aplikací ho nedostanou |
| `edu_ai_owner` | ne | vlastní schéma `edu_ai` a objekty aplikace; nikdo není jeho členem kromě migrátora (jen `SET`, bez dědění) |
| `edu_ai_search` | ano | `edu_ai_search` a QA administrace: `SELECT` všude, zápis jen podle matice |
| `edu_ai_ingest` | ano | komponenty flow v Langflow (`POSTGRES_DB_USER` / `POSTGRES_PASSWORD`): `SELECT` všude, zápis jen podle matice |
| `edu_ai_langflow` | ano | vlastní jen databázi `langflow` a její objekty; do databáze `edu_ai` se nepřipojí |
| `edu_ai_migrator` | ano | pouze `SET ROLE edu_ai_owner`; po `SET ROLE` má práva vlastníka |

Žádná z rolí kromě admina nemá `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS`. Aplikační role nemají vlastnictví, `WITH GRANT OPTION`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, `CREATE` ani `TEMPORARY`; na sekvence mají jen `USAGE` a `SELECT`. `PUBLIC` nemá žádné oprávnění na databáze, schémata `edu_ai`, `public`, `paradedb`, `pdb`, ani na tabulky a funkce v nich. Pět přihlašovacích hesel je různých a generují se do externího `.env` (`POSTGRES_ADMIN_PASSWORD`, `POSTGRES_PASSWORD` = heslo `edu_ai_ingest`, `POSTGRES_SEARCH_PASSWORD`, `POSTGRES_LANGFLOW_PASSWORD`, `POSTGRES_MIGRATOR_PASSWORD`).

**Cesta pro vyhledávání názvů (`search_path`):** `pg_trgm` (`word_similarity`, operátor `<->`) žije ve schématu `edu_ai` a kód ji volá bez kvalifikace. Dřívější jediná role `edu_ai` ji nacházela přes „$user“. Proto mají `edu_ai_search` a `edu_ai_ingest` nastaveno `search_path = edu_ai, public` (`ALTER ROLE … SET`); ostatní role (admin, owner, migrator, langflow) cestu nemají a objekty v `edu_ai` je nemohou zachytit. Bez tohoto nastavení tiše selhával překlepový záložní krok `document_lexical` (chyba `function word_similarity(unknown, text) does not exist` v logu PostgreSQL; krok chybu zachytí a pokračuje bez kandidátů). `db_roles.py verify` toto nastavení kontroluje.

**Zdroj pravdy:** `postgres/role_contract/matrix.json` (role, členství, databáze, `table_dml`, očekávané názvy objektů). Soubory `postgres/init_clean/_role_contract_*.psql` se z něj **generují** (`python3 scripts/db_roles.py gen-sql`), ručně se neupravují; test `tests/test_db_role_contract.py` ověřuje shodu.

## 2. Co kde běží

| Situace | Postup |
|---|---|
| Čistá instalace | `postgres/init_clean`: `00` (role, hesla z prostředí kontejneru, databáze `langflow` s vlastníkem `edu_ai_langflow`), `01`–`03` schéma a seed, `04` (práva, vlastnictví, značka `role-contract:2` na databázi). Healthcheck postgres je zdravý až po značce. |
| Existující instalace s jedinou rolí `edu_ai` | `db_roles.py prepare-env`, `db_roles.py adopt --confirm-isolated`, nový compose, `db_roles.py demote-old` (staré role se nestane `SUPERUSER` → `NOLOGIN`, heslo `NULL`; bootstrap superuživatel o `SUPERUSER` přijít nemůže) |
| Po obnově (`pg_restore --no-owner`) a po každé migraci | `db_roles.py reapply --confirm-isolated`, poté `verify` |
| Kontrola | `db_roles.py verify`; `smoke.py` (bod 4a) |

Adopce i `reapply` **odmítnou zápis**, pokud inventář (názvy tabulek, pohledů, sekvencí, funkcí, rolí, rozšíření, členství) neodpovídá známému základu z `matrix.json`; kontrola běží dřív než první zápis. Nové tabulky a funkce se musí zapsat do matice, jinak je kontrakt odmítne (žádné automatické výchozí oprávnění).

## 3. Známá omezení a rozhodnutí (z nezávislé revize)

- `edu_ai_migrator` je z hlediska dopadu rovnocenný adminovi pro objekty vlastníka: může přes `SET ROLE` založit spouštěče a funkce, které se spustí s právy admina při ruční údržbě. Údržbu tabulek vlastníka dělat jako `SET ROLE edu_ai_owner`.
- `edu_ai_ingest` a `edu_ai_search` mají `SELECT` na všechny tabulky `edu_ai` (širší než zápisová matice; i na `question_answer_log`, `staff_user`, `app_user`). Zúžení po změření.
- Hesla všech rolí zůstávají v prostředí kontejneru `postgres` (`docker inspect`). Po inicializaci se nepoužijí.
- `pg_hba.conf` obrazu pouští `edu_ai_admin` ze sítě `data` stejně jako ostatní role; zamítnutí admina přes síť je doporučení, ne součást kontraktu (pgAdmin by přestal fungovat pro údržbu).
- Migrační nástroj B03e (`scripts/migrate.py`) běží jako uživatel kontejneru (admin), ne jako migrátor; vzniklé objekty vlastní admin, dokud se neprovede `reapply`.
- `template1` je zbaven `PUBLIC` oprávnění; role ho přesto nepoužívají.
- `ALTER DEFAULT PRIVILEGES` jsou nastaveny jen restriktivně (žádný automatický grant); po migraci vždy `reapply`.
