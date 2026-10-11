# EDU-AI — vydávání a veřejné zdroje

Zdrojový commit, runtime image, ZIP a akceptační důkazy jsou různé výstupy.
Instalátor vyžaduje skutečný Git checkout: čte `.env.example` a Compose z blobů
zadaného SHA. Zdrojový ZIP bez `.git` proto není instalační checkout.

## Veřejný Git

Veřejný repozitář nese očištěný zdrojový strom, technickou dokumentaci, kontrakty,
testy a syntetické eval příklady. Privátní historii nelze zveřejnit pouhým smazáním
souborů v posledním commitu. Přenos proto kopíruje prověřený strom bez privátních
Git objektů a navazuje běžným fast-forward na již existující veřejnou historii.

Původní dva veřejné commity obsahují staré cesty pod `QA/`; zůstávají v předcích.
Aktuální strom je neobsahuje. Tento zdrojový přenos nepřepisuje již zveřejněnou
historii a netvrdí její kompletní očištění. Převzaté objekty pocházejí výhradně
z dosavadního veřejného repa, nikoli z historie neveřejného vývoje.

Soukromé QA packy, dotazy z korespondence, uploady, `.env`, klíče, provozní data,
interní inventáře a akceptační důkazy do veřejného repa nepatří. Konfigurační seed,
schválená veřejná metadata organizací a syntetické fixture jsou součástí zdrojů.
Generované SQL, manifesty a parity vektory se nemění ručně.

## 2. Co obsahují runtime image

Z Dockerfilů (`COPY`) vychází, že image nesou pouze aplikační kód a jeho závislosti:

| Image | Zdroj kopírovaný do image |
|---|---|
| `edu_ai` (Langflow, `Dockerfile`) | `requirements-langflow.txt`, `search/eduai_normalize` |
| `edu_ai_langgraph` (`Dockerfile.langgraph`; není službou v `docker-compose.yml`, samostatný dev kontejner) | `langgraph_entrypoint.sh` |
| `edu_ai_api` | `api/requirements.txt`, `api/app` |
| `edu_ai_search` | `search/requirements.txt`, `search/eduai_normalize`, `search/app` |
| `edu_ai_web_chat` | `web_chat/frontend` (build), `web_chat/requirements.txt`, `web_chat/web_chat.py` |
| `edu_ai_qa_admin` | `qa_portal/frontend` (build), `qa_portal/backend/requirements.txt`, `qa_portal/backend/app` |
| `edu_ai_postgres` (`Dockerfile.postgres`) | nic (jen základní image a balíčky) |

Testy v image **nejsou**. Testy, které běží v image, se do něj kopírují nebo připojují (`docker cp`,
`-v …/tests:/app/tests`); proto musí `tests/` zůstat ve zdrojovém checkoutu.

Provozní soubory, které se berou ze **zdrojového checkoutu** (ne z image) při instalaci a provozu:
`docker-compose.yml` a `.env.example` (instalátor), `scripts/` (`setup.py`, `install.py`, `host_preflight.py`,
`bootstrap.py`, `smoke.py`, `import_flows.py`, `create_api_key.py`, `db_roles.py`, `tunnel_subnet.py`, sdílené moduly
`eduai_layout.py`, `envfile.py`, `http_safe.py`, `prepare_ubuntu.sh`; `flow_rollout.py` slouží aktualizaci flow, ne instalaci),
`langflow_flows/ingestion` (import flow), `postgres/init_clean`, `postgres/role_contract` a `postgres/seed` (inicializace
databáze, kontrakt rolí, schválená data organizací), `nginx/conf` (bind mount) a build kontexty služeb. Výčet je doplněný podle importů
v `setup.py` a `host_preflight.py`; vynechání kteréhokoli z nich z checkoutu rozbije instalaci.

## Ověření vydání

1. Zaznamenejte přesný SHA veřejného kandidáta a čistý Git stav. Testy zůstávají
   ve zdrojovém checkoutu; nejsou součástí runtime image.
2. Spusťte `python3 docs/tools/docs_check.py`, `git diff --check` a příslušné testy
   z [DEVELOPMENT.md](DEVELOPMENT.md). Při změně normalizace ověřte paritu.
3. Zkontrolujte tajnosti, soukromé vstupy, odkazy, součty souborů a historii.
   Uchovejte manifest výslovných vynechání a úprav.
4. Ověřte nový veřejný checkout průvodcem a skutečným čistým VM průchodem:
   bootstrap, role MATCH, smoke, chat a syntetický QA export. Offline testy
   ani starší VM výsledek nejsou akceptací nového veřejného SHA.
5. Uveďte `NOT_RUN` pro neprovedenou regresi RAG, ingest, placené LLM a obnovu.
   Veřejné eval příklady nejsou soukromý regresní benchmark; pro srovnání je
   potřeba vlastní stejné testovací corpus a stejné parametry.

Zálohy a důkazy se uchovávají mimo Git. Nová oprava znamená nový commit a nové
součty. Postup instalace: [CLEAN_INSTALL.md](CLEAN_INSTALL.md). Provozní hranice:
[OPERATIONS_LINUX.md](OPERATIONS_LINUX.md). Licence a povinné attribution notices
zůstávají v [LICENSE](../LICENSE), [NOTICE](../NOTICE) a
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
