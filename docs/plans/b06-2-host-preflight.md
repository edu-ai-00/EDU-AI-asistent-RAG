# B06-2: preflight hostitele a bezpečné předání bootstrapu

Veřejná kopie vynechává interní akceptační reporty a odkazy na ně; tento návrh není důkazem aktuálního nasazení.

Pozn. auditu (SHA c8fe0543): věta o „nikdy nespuštěno proti skutečnému Dockeru“ je historická; `host_preflight.py` běžel v průvodci `setup.py`
na skutečných VM ([public-source.md](../releases/public-source.md)), finální veřejný SHA ale vlastní VM průchod nemá.

Datum: 2026-10-07. **Stav: kandidát implementovaný jen offline, ověřený jen zástupnými objekty (fake Host). Nikdy nebyl spuštěn proti skutečnému Dockeru.**
Navazuje na [návrh B06](b06-clean-install-proposal.md) (oddíly 2, 4, 5, 6, 8, 10) a sdílené rozložení `scripts/eduai_layout.py`. Rozsah je **první instalace**;
podporované aktualizace nejsou součástí ([B06-U](b06-upgrade-strategy.md)). Nic z toho není schválení nasazení.

Skript [scripts/host_preflight.py](../../scripts/host_preflight.py) je samostatný: `scripts/install.py` zůstává offline a Docker nevolá. Skript jen standardní knihovna,
testy [tests/test_host_preflight.py](../../tests/test_host_preflight.py) a [tests/host_preflight_fixtures.py](../../tests/host_preflight_fixtures.py).

## 1. Příkazy

```text
host_preflight.py check   LAYOUT [--min-free-gb N] [--min-compose X.Y.Z] [--reserved CIDR ...] [--refuse-pulled-images] [--accept-unmeasured-network-pools]
host_preflight.py handoff LAYOUT [stejné volby] [--env-link-only] [--exec]
host_preflight.py pin-env LAYOUT [--tunnel-subnet CIDR | --allocate-tunnel-subnet] [--reserved CIDR ...] [--set NAME=HODNOTA ...]
```

`LAYOUT` jsou volby `--source-root --config-dir --state-dir --project` nebo proměnné `EDU_AI_*` (příklad z `eduai_layout`: vlajka > proměnná > výchozí). Konfigurace musí být **mimo checkout**.
Výstup jsou řádky `KÓD: ÚROVEŇ detail` (`OK`, `NOTE`, `REFUSED`, `UNVERIFIED`), potom `RESULT: ...` a vždy `INSTALLATION_NOT_STARTED`. Návratové kódy: **0** nic nenalezeno,
**2** `REFUSED` (prokázaný konflikt), **3** `UNVERIFIED` (nelze prokázat). Co nelze přečíst nebo rozparsovat, je `UNVERIFIED`, nikdy „volno“; výpis nikdy neříká „ready“, „volno“ ani „nainstalováno“ o neověřeném stavu.
Vypisují se jen jména, cesty a počty: hodnoty `.env`, klíče a tokeny ne, výstup potomků se nepřeposílá.

## 2. Co `check` ověřuje

| Pojistka | Kód řádku | Odmítnuto (2) | Neověřeno (3) |
|---|---|---|---|
| rozložení (`eduai_layout.validate`, chybějící odkazy povoleny) | `LAYOUT_*` | konflikt, špatný cíl odkazu, adresář v checkoutu, konfigurace v checkoutu (`CONFIG_NOT_EXTERNAL`) | nečitelné jméno |
| G9 architektura | `ARCH_HOST`, `ARCH_DOCKER` | `uname -m` nebo démon není x86_64/amd64 | nečitelné nebo nerozparsovatelné |
| démon, kontext | `DOCKER_DAEMON`, `DOCKER_CONTEXT`, `DOCKER_OS`, `DOCKER_TARGET` | OSType jiný než linux | `docker info` selže, `DOCKER_HOST`/`DOCKER_CONTEXT` jiné než výchozí, klientský kontext nedokázán jako výchozí |
| Compose | `COMPOSE_VERSION` | pod minimem | verze nerozparsována |
| zděděné proměnné | `INHERITED_ENV` | | `COMPOSE_*`, `DOCKER_CONFIG`... a proměnné, které `docker-compose.yml` interpoluje a které by přebily `.env` (jen jména) |
| externí sada (G2, K2) | `CONFIG_DIR`, `CONFIG_FILES`, `KEY_FINGERPRINT` | neúplná sada, cizí položky, adresář není 0700, soubory nejsou 0600, cizí vlastník, `MISMATCH` otisku | nečitelný soubor, `UNVERIFIABLE` otisk, módy na platformě bez nich |
| piny v `.env` (G3, G4) | `COMPOSE_FILE`, `PROJECT`, `TUNNEL_SUBNET_PIN` | `COMPOSE_FILE` nepřipnut (jinak by se načetl `docker-compose.override.yml` a publikoval vývojové porty) nebo jmenuje override; projekt se liší od vlajky; neplatná podsíť | chybí `EDU_AI_TUNNEL_SUBNET` (model ho na tomto SHA vyžaduje), projekt neznámý |
| model | `MODEL`, `MODEL_OVERRIDE`, `TUNNEL_SUBNET_MODEL` | porty override v modelu, podsíť modelu ≠ pin | `docker compose -p P --project-directory SRC --env-file CFG/.env config --format json` nejde vykreslit nebo má jiný tvar |
| G1/G3 existující zdroje | `EXISTING_CONTAINERS`, `_VOLUMES`, `_NETWORKS`, `_IMAGES` | jméno z modelu nebo štítek `com.docker.compose.project=<projekt>`; image tag, který instalace staví | výpis nelze rozparsovat, model chybí (jen štítky) |
| G1 data | `STATE_DIR` | neprázdný `state_dir`, `langflow_data` a ostatní, cizí položky, symlink, chybí rodič | nečitelný výpis |
| G4 porty | `PORTS`, `PORTS_UNLISTED` | listener na libovolné adrese (v4 i v6) na publikovaném portu modelu | `ss` chybí, selže nebo má řádek, kterému se nerozumí; UDP porty |
| G5 podsíť tunelu | `TUNNEL_SUBNET` | `tunnel_subnet.py check` skončí 2 | skončí 3 nebo jinak |
| G5 ostatní sítě | `NETWORK_POOLS` | | vždy, dokud operátor volbou `--accept-unmeasured-network-pools` nezapíše, že ví o neměřeném chování (pak `NOTE`) |
| disk | `DISK_DOCKER_ROOT`, `DISK_STATE_DIR` | pod prahem | kořen Dockeru ve VM (Docker Desktop), nelze stat, cesta chybí |

Porty, jména kontejnerů, sítí a svazků i image tagy se berou z vykresleného modelu, nic z toho není v kódu napevno. Veřejný image, který už na hostiteli je, je jen `NOTE` (`IMAGES_PRESENT_PULLED`),
pokud nezadáte `--refuse-pulled-images`; tagy, které instalace staví sama (`build:`), kolizi dělají.

**Provizorní hodnoty (neměřeno):** minimum Compose `2.24.0` (odvozeno z použitých vlastností, např. `configs.content`; ověřena je jen verze 5.3.1) a práh disku `20 GiB` pro kořen Dockeru i `state_dir`
(`--min-compose`, `--min-free-gb`). Produkční hostitel měl 96 GB a 51,7 GB build cache (audit-04a); skutečná potřeba se neměřila.

**Neměřeno a výslovně přiznáno:** kolize bloků z poolů Dockeru (5 automaticky přidělovaných sítí: `edge`, `app`, `data`, `bifrost_egress`, `tunnel_egress`) s trasami hostitele, LAN a VPN.
Chování Dockeru při takové kolizi nikdo neměřil; `tunnel` je pokryt `tunnel_subnet.py check` (všechny routovací tabulky a adresy rozhraní), ostatní sítě ne.

## 3. Bezpečnost příkazů

Jediný vstup do stroje je objekt `Host`. Povolen je pevný seznam čtecích příkazů: `docker info`, `docker compose version --short`, `docker ps -a`, `docker volume ls`, `docker network ls`, `docker image ls`
(vše s `--format {{json .}}`), `docker compose -p P --project-directory SRC --env-file ENV config --format json`, `ss -H -ltn`, `uname -m` a `scripts/tunnel_subnet.py check|allocate` jen s `--routes host`
(`--routes docker` by spustil kontejner). Jiný příkaz vyhodí `Forbidden` dřív, než se spustí; slova `down`, `rm`, `prune`, `-v`, `--remove-orphans`, `up`, `build`, `run`, `exec` a další jsou zakázána
i jako argument. Potomci dostávají pevné prostředí (bez zděděných `DOCKER_*`, `COMPOSE_*` a proměnných modelu). Testy hlídají seznam, pevné prostředí i zdroj (AST: žádné `unlink`, `rmtree`, `rename`, `replace`; procesy se spouštějí jen ve dvou funkcích).

## 4. `handoff`

1. Spustí `check`. Cokoli jiného než kód 0 (REFUSED i UNVERIFIED) znamená, že se **nic nevytvoří** (`HANDOFF: NOTE not performed`).
2. Při čistém `check` vytvoří jen **chybějící** jména: `state_dir` (rodič musí existovat), podadresáře `uploads`, `langflow_data`, `sqllite_export`, `bifrost_data` s módy z `bootstrap.DATA_DIRS`
   (vlastníka uid:gid nastaví krok bootstrapu) a symlinky v checkoutu (`.env` a stavové adresáře) přes `os.symlink`, který selže na existujícím jménu. Nic nenahradí ani nesmaže;
   při selhání uprostřed zůstane vytvořené platné a další běh dokončí zbytek.
3. Znovu ověří rozložení s povinnými odkazy a vypíše přesný příkaz: `NEXT_COMMAND: NOTE python3 .../scripts/bootstrap.py --source-root ... --config-dir ... --state-dir ...` (z `Layout.argv()`, bez tajností).
4. `--exec` spustí `bootstrap.py` s týmiž volbami a s pevným prostředím (zachová jen proměnné, podle kterých bootstrap pozná CI). Výstup `check` končí řádkem `INSTALLATION_NOT_STARTED` **před** spuštěním;
   potom následují `BOOTSTRAP_LAUNCHED` a `BOOTSTRAP_EXIT: n`. Kód bootstrapu 3 je „zopakuj později“ (nic se nezměnilo), kód 1 je chyba po případné částečné změně (handoff vrací 3, ne 2).

`--env-link-only`: Compose vykresluje `env_file: .env` relativně k checkoutu, takže model se bez odkazu `.env` nejspíš nevykreslí (nezměřeno) a `check` zůstane neověřený. Volba vytvoří **jen** odkaz `.env`,
a to jen když není žádné `REFUSED` a jediné `UNVERIFIED` je model a to, co z něj plyne (nebo `NETWORK_POOLS`); potom spustí `check` znovu a nic dalšího nevytvoří.

Dokud Docker při kolizi poolů nikdo neměřil, projde `check` i `handoff` jen s `--accept-unmeasured-network-pools`.

## 5. `pin-env`

Připojí na konec externího `.env` **jen chybějící** klíče (`COMPOSE_FILE=docker-compose.yml`, `COMPOSE_PROJECT_NAME`, `EDU_AI_TUNNEL_SUBNET` z `--tunnel-subnet` nebo z `tunnel_subnet.py allocate`, a `--set NAME=HODNOTA`).
Existující klíč (i prázdný) se nikdy nezmění, jen se jmenuje (`PIN_ENV_KEPT`, `PIN_ENV_DIFFERS`). Tajnosti nevznikají: názvy s `PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `CREDENTIAL` se odmítnou, hodnota nesmí obsahovat `$` ani konec řádku.
Zápis je `envfile.write_atomic` (dočasný soubor, `fsync`, `os.replace`); po selhání nebo přerušení zůstane `.env` bajt po bajtu stejný a výsledek se hlásí podle disku, ne podle výjimky. Soubor s CR, nesoukromý nebo cizí se odmítne.

## 6. Co se neověřilo a co je potřeba od vlastníka

Viz zprávu k předání práce. Shrnutí: nic z toho nikdy nebylo spuštěno proti skutečnému Dockeru, Compose ani `ss`; chování `--env-file` s `COMPOSE_FILE`, vykreslení modelu bez odkazu `.env`,
tvar `docker info`/`ps`/`network ls --format {{json .}}` a prahy jsou předpoklady z dokumentace a zástupných objektů. Příkaz a testy: `python tests/test_host_preflight.py` (Linux plně, Windows přeskočí testy s odkazy a módy).
