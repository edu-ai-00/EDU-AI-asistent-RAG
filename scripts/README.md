# scripts — rozcestník

Provozní a vývojové skripty. **Je to jen přehled**: přesné příkazy, volby a předpoklady každého skriptu jsou v jeho
docstringu nebo úvodním komentáři a ty platí; tady se nic neduplikuje. Cesty zůstávají pevné: odkazují na ně
dokumentace, testy, `Makefile` i provozní postupy, proto se skripty podle skupin do podsložek nedělí.

Spouští se z kořene repozitáře. Většina skriptů používá jen standardní knihovnu; kde ne, je to v docstringu.
Před spuštěním ověřte konkrétní režim v docstringu: například `bootstrap.py` při běžném spuštění zapisuje konfiguraci a spouští služby. Čtecí režim nebo dry run použijte tam, kde jej konkrétní skript nabízí.

## Instalace a první spuštění

| Skript | K čemu | Návazná dokumentace |
|---|---|---|
| `setup.py` | průvodce čistou instalací: `plan` (jen čtení) a `install`; spojuje `install.py`, `host_preflight.py`, `bootstrap.py` a kontrolu rolí, školu se vybírá v prohlížeči | [CLEAN_INSTALL.md](../docs/CLEAN_INSTALL.md) |
| `prepare_ubuntu.sh` | příprava nového Ubuntu hostitele (Docker, Compose, nástroje) | [CLEAN_INSTALL.md](../docs/CLEAN_INSTALL.md) |
| `host_preflight.py` | kontroly hostitele před první instalací, předání do bootstrapu, zápis podsítě do `.env` | [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md) |
| `install.py` | offline příprava externí konfigurace instalace z konkrétního commitu | [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md) |
| `bootstrap.py` | čistý checkout → běžící stack jedním idempotentním příkazem | [ONBOARDING.md](../ONBOARDING.md) |
| `create_api_key.py` | klíč Langflow přes REST do `.env` (volá ho bootstrap) | [ONBOARDING.md](../ONBOARDING.md) |
| `import_flows.py` | import 6 flow z `langflow_flows/ingestion/`: bez `--missing-only` **přepíše** (PATCH) každý existující flow včetně ručních úprav, s `--missing-only` (tak ho volá bootstrap) vytvoří jen chybějící; cílená výměna změněných uzlů je `flow_rollout.py` | [ONBOARDING.md](../ONBOARDING.md) |
| `smoke.py` | ověření běžícího stacku bez placených volání | [ONBOARDING.md](../ONBOARDING.md) |
| `db_roles.py` | kontrakt databázových rolí: generátor SQL, kontrola a převzetí izolované syntetické instalace | [b06-c2-role-contract.md](../docs/plans/b06-c2-role-contract.md) |
| `eduai_layout.py`, `envfile.py`, `http_safe.py` | sdílené moduly (umístění instalace, úprava `.env`, HTTP bez následování přesměrování); nespouštějí se samostatně | docstringy |

## Databáze, migrace a flow (izolované syntetické databáze)

Migrační runner se nepoužívá na produkci ani na školní instalaci (viz [AGENTS.md](../AGENTS.md)).

| Skript | K čemu |
|---|---|
| `migrate.py`, `migrate.sh`, `eduai_migrate/` | registr migrací a runner (`status`, `plan`, `apply`, generátory); `migrate.sh` je tenký obal. Specifikace: [spec.migration-registry.md](../specs/active/spec.migration-registry.md) |
| `flow_rollout.py` | cílená výměna změněných uzlů flow z vydání A do B v nainstalovaném Langflow; co se rozešlo, odmítne |
| `gen_clean_install.sh` | regeneruje `postgres/init_clean/` z řízené referenční databáze (generované soubory se needitují ručně) |
| `align_existing_db.sh` | **nepodporovaný, nevratný historický skript pro jednu konkrétní databázi**: spouští `postgres/init` od 31 (neodskakuje 201/202), vkládá pevné entity `SKOLA_NOVA_VES…` a hromadně doplňuje `legal_entity_code`; nepoužívat na produkci ani na školní instalaci, podporovaný upgrade neexistuje |

## Síť a nginx (R2, S2)

Záznam nasazení tento checkout neobsahuje, [docs/plans](../docs/plans/README.md) má jen návrhy a historické záznamy. Skripty se na produkci nespouštějí bez schválení.

| Skript | K čemu |
|---|---|
| `nginx_s2_switch.sh` | přepnutí jen služby nginx mezi stavem před S2 a S2, s kontrolami a automatickým návratem |
| `nginx_s2_guard.sh` | provozní vstup návratu S2: pustí nezměněný S2 skript jen tehdy, když je ověřeno, že R2 není aktivní |
| `nginx_r2_switch.sh` | tři kroky zapojení nginx/cloudflared (hranice důvěry `CF-Connecting-IP`) a návrat po krocích |
| `tunnel_subnet.py` | přidělení a kontrola podsítě `tunnel` a kontrola modelu Compose (jen čtení) |
| `perimeter_check.py` | kontrola z vnějšku, že je vystaveno jen to, co má být; musí běžet mimo VPN |

## Obsah a data (QA, dokumenty, oblasti)

| Skript | K čemu |
|---|---|
| `reingest.py` | hromadné znovunahrání dokumentů přes `POST /v1/ingest` (po změně chunkování) |
| `upload_qa_packs.py` | nahrání vygenerovaných QA packů přes `/v1/ingest`; metadata z `_manifest.json` |
| `qa_deactivate_packs.py` | výpis a vyřazení QA packů (API na to není) |
| `qa_set_categories.py` | nastavení `tags->>'category'` jednoho packu z CSV |
| `restore_declared_metadata.py` | SQL, které po reingestu vrátí ručně zadaná metadata dokumentů z exportu inventáře |
| `domain_inventory.py`, `domain_migration.py` | přehled oblastí na školu a reprodukovatelná, vratná migrace rozhodnutí kurátora; historický záznam postupu: [2026-09-06_domain_scope.md](../docs/nasazeni/2026-09-06_domain_scope.md) |

Historické postupy zmiňují externí skill `qa-pack-variants`, který tento checkout nedistribuuje. Skripty výše pracují s již připravenými packy nebo opravují jejich metadata.

## Měření a kontroly kvality

Aktuální regresní harness je **[tests/eval_rag.py](../tests/eval_rag.py)** (viz [DEVELOPMENT.md](../docs/DEVELOPMENT.md), oddíl 3, vrstva 5).
Skripty níže jsou starší a čtou vstupní soubory z `eval/` nebo `QA/`. Ty jsou ignorované Gitem a ve sledovaném stromu
nejsou, takže bez vlastních vstupů se nespustí.

| Skript | K čemu |
|---|---|
| `eval_search.py` | zlatá sada případů proti `/search` (retrieval a výsledek); jiný soubor než `tests/eval_search.py` |
| `eval_snapshot.py` | snapshot chunků, ze kterých odpovědi vznikly, a jejich diff před/po změně |
| `run_doc_questions.py` | otázky odvozené z korpusu: zasáhl se očekávaný dokument a pomohla nebo uškodila větev QA |
| `run_negative.py` | negativní případy: každý musí skončit `no_answer` (únik mezi školami) |
| `run_selfvariants.py` | každá uložená varianta QA musí vrátit vlastní záznam (kontrola ingestu) |
| `qa_pack_probe.py` | které QA odpovědělo volajícímu; vstup `QA/probe_cases.json` |
| `param_liveness.py` | má každý parametr `settings.config->pipeline` pozorovatelný účinek; za běhu **mění aktivní řádek `settings`** (nespouštět na sdíleném prostředí ani na produkci) |
| `chunk_analysis.sql`, `chunk_analysis_summary.sql` | kvalita chunků jednoho dokumentu a celého korpusu (spouští se v `psql`) |

## Údržba závislostí

| Skript | K čemu |
|---|---|
| `dump_pkgs.py` | výpis nainstalovaných balíčků (název, verze, licence) uvnitř kontejneru; podklad pro [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) |
