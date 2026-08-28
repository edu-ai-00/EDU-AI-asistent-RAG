# Návrh automatizace CI/CD

## Shrnutí

Nasazovací mechaniku projektu lze automatizovat téměř úplně. Kód není příliš
komplikovaný; současná rizika vznikají hlavně tím, že produkce nemá jednoznačný
„desired state“ a část deploye závisí na ručním výběru souborů, znalosti vazeb
mezi soubory a Docker images a na ruční evidenci migrací.

Doporučený cílový proces:

```text
merge do main
      ↓
vytvoření prod-* tagu / GitHub Release
      ↓
testy + sestavení Docker images
      ↓
produkční deploy runner
      ↓
backup → migrace → deploy → healthcheck → smoke test
      ↓
úspěch: záznam release
chyba: automatický rollback images a konfigurace
```

Automatizace může spolehlivě ověřit, že se image sestavil, migrace proběhla,
služby jsou zdravé a smoke test prošel. Funkční nebo obsahovou správnost změny
ale dokáže potvrdit pouze tehdy, pokud pro ni existuje automatický test.

## Spouštění produkčního deploye

Text v commit message není vhodný řídicí mechanismus. Doporučený model:

1. Push nebo pull request do `main` spustí CI a sestavení.
2. Produkční deploy spustí až tag ve tvaru `prod-YYYY-MM-DD.N` nebo vytvoření
   GitHub Release.
3. Až bude pipeline dostatečně prověřená, lze přejít na automatický deploy
   každého merge do chráněné větve `main`.

Tag nebo Release je jednoznačný, auditovatelný pokyn „toto je update“ a přesně
určuje commit, který se má nasadit.

## Produkční runner

Nejjednodušší varianta pro současnou infrastrukturu je GitHub Actions
self-hosted runner na produkčním serveru nebo na samostatném stroji připojeném
do stejné VPN.

Runner navazuje odchozí spojení na GitHub, takže kvůli CI není nutné otevírat
produkční SSH do internetu. Produkční runner však má přes Docker prakticky
administrátorská oprávnění, a proto musí platit:

- runner smějí používat pouze produkční deploy workflow;
- na produkčním runneru se nikdy nespouští kód z běžného pull requestu;
- větev `main`, produkční tagy a GitHub Environment musí být chráněné;
- secrets se ukládají mimo Git, ideálně přímo na serveru nebo jako chráněné
  environment secrets;
- současně může běžet nejvýše jeden produkční deploy.

## Průběh deploye

Deployer by měl provést následující kroky:

1. Zjistit poslední úspěšně nasazený release.
2. Určit změněné soubory vůči tomuto release.
3. Namapovat změny na dotčené služby.
4. Spustit relevantní testy a `docker compose config --quiet`.
5. Sestavit immutable Docker images označené SHA commitu a uložit je do
   container registry.
6. Vytvořit databázový backup.
7. Spustit dosud neaplikované databázové migrace.
8. Nasadit pouze dotčené služby.
9. Pokud se změnily Langflow flows, importovat jejich novou verzi.
10. Spustit healthchecky, `scripts/smoke.py` a test skutečně změněné funkce.
11. Zapsat commit, čas, image digesty, migrace a výsledek do release záznamu.
12. Při chybě vrátit předchozí images a konfiguraci a označit deploy jako
    neúspěšný.

Build musí vycházet přímo z přesného Git commitu. Tím odpadne ruční SCP,
odstraňování CRLF a chyba, kdy byl zdrojový soubor zkopírován na server, ale
jeho Docker image nebyl přestavěn.

## Mapování změn na služby

Mapování musí být verzované například v `deploy/services.yml`. Neznámá cesta
má pipeline zastavit, nikoli být potichu ignorována.

| Změněná cesta | Akce |
|---|---|
| `api/**` | test a rebuild `edu_ai_api` |
| `search/app/**` | test a rebuild `edu_ai_search` |
| `search/requirements.txt` | rebuild `edu_ai_search` |
| `search/eduai_normalize/**` | rebuild `edu_ai_search` i Langflow image `edu_ai` |
| `web_chat/**` | pytest, frontend build a rebuild `edu_ai_web_chat` |
| `requirements-langflow.txt`, kořenový `Dockerfile` | rebuild `edu_ai` |
| `Dockerfile.postgres` | rebuild Postgres image; nasazovat se zvýšenou opatrností |
| `postgres/init/*.sql` | spustit migration runner |
| `langflow_flows/**` | test invariantů a import flows |
| `nginx/**` | validace `nginx -t` a nasazení nginx konfigurace/image |
| Compose soubory | validace konfigurace a vyhodnocení dotčených služeb |
| `docs/**`, `README.md` | bez produkčního deploye |

## Immutable Docker images

Produkce by neměla sestavovat neurčitý lokální tag typu `edu_ai_search:1.0`.
CI má vytvořit image a produkční Compose má používat konkrétní digest:

```yaml
services:
  edu_ai_search:
    image: ghcr.io/<ORG>/edu-ai-search@sha256:<digest>
```

Release manifest uchová digest každé služby. U nezměněných služeb se převezme
digest z předchozího release. Výhody:

- přesně víme, jaký kód v kontejneru běží;
- rollback pouze vrátí předchozí manifest;
- není možné omylem spustit starý lokální image pod stejným tagem;
- není potřeba kopírovat zdrojové soubory do `/opt/edu_ai`;
- nasazují se pouze skutečně změněné služby.

Pro vývoj lze nadále používat lokální `build:` přes development Compose
override. Produkční Compose má používat pouze hotové images.

## Databázové migrace

Adresář `postgres/init/` se na existující produkční databázi automaticky
nespouští. Je proto potřeba zavést migration ledger, například:

```sql
CREATE TABLE IF NOT EXISTS edu_ai.schema_migrations (
    migration_name text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    release_sha text NOT NULL
);
```

Nový `scripts/migrate.py` by měl:

1. Získat PostgreSQL advisory lock, aby nemohly běžet dva deploye současně.
2. Načíst migrace v deterministickém pořadí.
3. Porovnat jejich SHA-256 checksum s již aplikovanými migracemi.
4. Odmítnout historickou migraci, jejíž obsah byl po aplikaci změněn.
5. Každou novou migraci provést v transakci.
6. Po úspěchu zapsat název, checksum a release SHA.

Před prvním automatickým deployem se musí jednorázově vytvořit baseline:
ověřit stav produkčního schématu a označit již aplikované migrace.

Automaticky lze povolit dopředně kompatibilní změny, například:

- `CREATE TABLE IF NOT EXISTS`;
- nový nullable sloupec;
- nový index vytvořený způsobem vhodným pro produkční provoz;
- přidání nové hodnoty nebo seed dat pomocí idempotentního `ON CONFLICT`.

Destruktivní změny, přejmenování a okamžité odstranění sloupců nemají projít
jednokrokovým automatickým deployem. Mají používat postup expand/contract:

1. přidat novou strukturu kompatibilní se starým kódem;
2. nasadit kód, který umí starou i novou strukturu;
3. převést data;
4. teprve v pozdějším release odstranit starou strukturu.

Rollback aplikace databázovou migraci automaticky nevrací. Aditivní změna musí
být navržená tak, aby staršímu image nevadila.

## Langflow flows

Současný stav, kdy je živá Langflow DB zdrojem pravdy a JSON v repozitáři je
jen disaster-recovery export, není slučitelný s plně automatickým GitOps
deployem.

Je potřeba jednorázově:

1. Exportovat ověřené produkční flows do Git.
2. Spustit `tests/test_flow_invariants.py`.
3. Od této chvíle považovat Git za zdroj požadovaného stavu.
4. Produkční změnu provedenou přes UI nejprve exportovat, zkontrolovat a
   commitnout.

Potom může deployer použít existující idempotentní
`scripts/import_flows.py`, který aktualizuje flow podle stabilního
`endpoint_name`. Před importem se vytvoří backup dotčených DB řádků nebo export
flows a po importu se spustí nemutující end-to-end probe.

Při selhání lze znovu importovat flows z předchozího release.

## Nginx a produkční drift

Produkční `nginx.conf` nesmí obsahovat ruční hotfixy, které nejsou v Git.
Před zapnutím automatizace je nutné jednorázově porovnat produkční konfiguraci
s repozitářem a legitimní změny commitnout.

Ideální je zabalit verzovaný `nginx.conf` do vlastního nginx image. `.htpasswd`
a `auth_keys.conf` zůstanou v persistentních mounts mimo image.
Pipeline před nasazením spustí `nginx -t`.

Pokud konfigurace zůstane bind-mounted, deployer musí aktualizovat jen
verzovanou konfiguraci a nikdy nesmí přepisovat autentizační soubory.

## Secrets a persistentní data

Do release artefaktu ani Docker images nesmí vstoupit:

- `.env`;
- `LANGFLOW_SECRET_KEY`;
- OpenAI API klíče;
- TLS private keys;
- nginx autentizační soubory;
- `langflow_data/`;
- `uploads/`;
- `pgadmin_exports/`;
- databázové volume;
- existující backupy.

Persistentní cesty by měly být v produkčním Compose uvedené absolutně nebo přes
jednu proměnnou, například `DATA_ROOT=/opt/edu_ai/shared`. Tím se data oddělí
od verzovaných release souborů a nehrozí jejich smazání při úklidu starých
release adresářů.

`LANGFLOW_SECRET_KEY` se nikdy negeneruje během deploye. Produkční deploy také
nikdy nespouští `scripts/bootstrap.py` a nikdy nepoužívá
`docker compose down -v`.

## Validace a rollback

Pořadí kontrol má být od nejlevnějších:

1. statické testy a lint;
2. `tests/test_flow_invariants.py`, pokud se mění flows;
3. testy `web_chat`;
4. sestavení dotčených Docker images;
5. validace výsledného produkčního Compose;
6. DB backup a migrace;
7. `docker compose up -d --wait` pro dotčené služby;
8. ověření skutečných `/health` endpointů;
9. `scripts/smoke.py`;
10. test konkrétní nové funkce.

Nestačí pouze stav `(healthy)` z Dockeru. Kontrola musí ověřit endpoint a kódovou
cestu, která se v daném release měnila.

Při neúspěchu:

- vrátit předchozí image digesty a Compose manifest;
- znovu spustit předchozí verzi služby;
- u flows importovat předchozí Git verzi;
- znovu spustit healthcheck a smoke test;
- ponechat aditivní DB migrace na místě;
- uchovat log a backup neúspěšného deploye.

Rollback neřeší destruktivní migraci. Taková migrace nesmí být do plně
automatické větve vůbec přijata.

## Navržené soubory

```text
.github/workflows/ci.yml
.github/workflows/deploy-production.yml
deploy/services.yml
deploy/docker-compose.prod.yml
scripts/deploy.sh
scripts/migrate.py
```

### `ci.yml`

- spouští se pro pull requesty a push do `main`;
- provádí testy, validaci Compose a build;
- nesmí používat produkční runner ani produkční secrets;
- může publikovat images pouze z chráněné větve.

### `deploy-production.yml`

- spouští se vytvořením `prod-*` tagu nebo GitHub Release;
- používá produkční environment a produkční runner;
- má concurrency lock;
- stáhne nebo ověří images pro konkrétní commit;
- spustí jediný verzovaný deploy skript;
- vždy publikuje deploy log a release manifest.

### `deploy/services.yml`

- mapuje změněné cesty na služby, testy a speciální kroky;
- explicitně označuje změny vyžadující migraci nebo import flows;
- při neznámé cestě selže bezpečně.

### `deploy/docker-compose.prod.yml`

- neobsahuje lokální `build:`;
- používá immutable image digesty;
- používá produkční VPN port bindingy;
- persistentní data a secrets mountuje z oddělených cest;
- je jedinou verzovanou produkční Compose konfigurací.

### `scripts/deploy.sh`

- je idempotentní;
- používá vždy explicitní produkční Compose soubory;
- ověřuje absolutní cílové cesty;
- vytváří backup;
- spouští migrace, nasazení, healthchecky a rollback;
- nevytváří ani nemění secrets.

## Doporučené fáze zavedení

### Fáze 1: Deterministický deploy aplikací

- zavést CI;
- zavést explicitní produkční tag;
- zavést produkční runner;
- sestavovat immutable images;
- automaticky nasazovat pouze změny `api`, `search` a `web_chat`;
- spouštět healthcheck a smoke test;
- automaticky vracet předchozí images.

Tato fáze odstraní většinu ruční práce a nejčastější chybu se zapomenutým
rebuildem.

### Fáze 2: Migrace a konfigurace

- vytvořit migration ledger a baseline;
- přesunout produkční VPN Compose konfiguraci do Git;
- sjednotit nginx konfiguraci s Git;
- oddělit persistentní data od release souborů;
- automatizovat aditivní migrace a bezpečné Compose změny.

### Fáze 3: Langflow GitOps

- prohlásit Git za zdroj pravdy pro flows;
- automatizovat backup, import, probe a rollback flows;
- zabránit neauditovaným změnám pouze v produkčním UI.

### Fáze 4: Plně bezobslužné běžné releasy

- doplnit unit a integrační testy zejména pro API a search logiku;
- po období paralelního ověřování odstranit approval pro běžné aplikační změny;
- ponechat schválení jen pro destruktivní DB změny, secrets a infrastrukturu.

## Závěr

Běžné aplikační updaty mohou být plně automatické. Migrace, flows a konfigurace
lze automatizovat také, jakmile mají evidenci, jednoznačný zdroj pravdy a
bezpečné rollback chování.

Ruční nebo schvalovaná brána má zůstat pouze pro:

- destruktivní databázové změny;
- změny secrets;
- síťovou a cloudovou infrastrukturu;
- změny, jejichž správnost zatím nepokrývají testy.

Ruční postup pak může zůstat jako recovery runbook pro
nouzové ruční nasazení. Standardní cestou má být verzovaný a opakovatelný
deploy workflow, nikoli ruční SCP session řízená člověkem nebo AI asistentem.
