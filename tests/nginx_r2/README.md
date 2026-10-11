# nginx R2: laboratoř hranice důvěry proxy hlavičkám

Dokládá **návrh** R2, varianta 2 ([návrh](../../docs/plans/r2-trust-boundary-proposal.md)):
komu smí nginx věřit adresu klienta (`CF-Connecting-IP`) a co z `X-Forwarded-*` smí dojít k aplikacím; jak se instalaci přidělí a ověří podsíť
důvěryhodné sítě; jak se laboratoř bezpečně uklízí.
Návrh **není nasazený**; laboratoř (`r2lab.py`) nemění `docker-compose.yml`, `nginx/conf` ani žádný běžící stack.
**Žádný přístup na produkci, žádné `.env`, klíče, tunelové údaje ani LLM.**

Implementační kandidát leží ve třech samostatných commitech konfigurace (kroky 1 až 3) a v provozních nástrojích `scripts/nginx_r2_switch.sh`
a `scripts/tunnel_subnet.py` (viz runtime zdroje v tomto checkoutu). Tato složka je jejich laboratoř a nácvik:
`candidate.py verify` dokládá, že commity kroků jsou bajt po bajtu odvozené varianty návrhu, `rehearsal_linux.py` zkouší skutečný skript na
skutečných commitech (zavedení, odmítnutí, selhání, přerušení, návrat), `host_iproute2.py` čte routy skutečným iproute2.

## Soubory

| Soubor | Účel |
|---|---|
| `candidate.py` | odvodí návrh z **nasazeného S2 commitu** (`S2_BASE`, ne z HEAD) přesnými počítanými náhradami (nic se neupravuje ručně); jeden zdroj CIDR; varianty a záměrně špatné mutanty; `render`, `check` (bez Dockeru); `apply --step N` zapíše soubory kroku, `verify` dokáže, že tři commity kroků jsou přesně odvozené varianty `transition`, `transition2`, `candidate` |
| `tunnel_subnet.py` | jen zástupce: provozní nástroj je [`scripts/tunnel_subnet.py`](../../scripts/tunnel_subnet.py), **jedna implementace** (kód 0 volné, 2 odmítnuto, 3 nelze ověřit; volající pokračuje jen při 0; nečitelný Docker pool je 3; kontroly efektivního Compose modelu `model-subnet`, `model-check`, `render-trust`, `wiring`, `image`). Import i spuštění odsud fungují dál |
| `ownership.py` | vlastnictví zdrojů laboratoře podle štítků a úklid, který se o něj opírá (`cleanup_run`, `safe_remove`) |
| `offline_tests.py` | testy předchozích tří a `switch_offline.py` bez Dockeru, s falešným Dockerem a falešným hostitelem (skript pomocného kontejneru běží ve **skutečném POSIX shellu**, falešné je jen `ip`; potřeba je `sh`, na Windows jej dává Git for Windows; bez něj je test `ERROR`, nikdy PASS); spouští je i `r2lab.py selfcheck` |
| `switch_offline.py` | úsudky skriptů **`scripts/nginx_r2_switch.sh`** a **`scripts/nginx_s2_guard.sh`** bez démona (bash, git a skutečný klient `docker compose config`; `docker` je `fakedocker.py`): přesná změna modelu kroku 2 (R2-R4), rozsah commitu a rychlého posunu, vstupy návratu (R2-R5), pojistka návratu S2 (odmítne i neověřitelný stav: selhání `docker inspect`, seznamu kontejnerů, nečitelný soubor; prokazatelně chybějící kontejner nebrání obnově); první posuzované verze (`59c1e02` skript, `8751bff` pojistka) se spouštějí proti týmž případům a musí je přijmout, opravené je odmítnou |
| `fakedocker.py` | falešný `docker` pro předchozí: `compose config` jde skutečnému klientu (s cache), ostatní příkazy odpovídá malý „svět“ v JSON, každé volání se zapisuje a označí jako měnící; neznámý příkaz končí 99 |
| `r2lab.py` | laboratoř návrhu: `selfcheck`, `static`, `run`, `cleanup --run-id` |
| `rehearsal_linux.py` | spustí nácvik uvnitř izolovaného Linuxu (Ubuntu 24.04, GNU bash, skutečné signály, **Compose 5.3.1** s ověřeným sha256) proti témuž Docker démonu; kandidátem jsou skutečné commity (`--tools --step1 --step2 --step3`) nebo pracovní strom |
| `rehearsal.py` | vlastní nácvik: sedm scénářů (zavedení všech kroků a návrat, návrat z každé fáze, odmítnutí před změnou, selhání během kroku, přerušení signály, dvě instalace, návaznost na návrat S2) se **skutečným** `scripts/nginx_r2_switch.sh` na jednorázových klonech |
| `host_iproute2.py` | režim `--routes host` se **skutečným iproute2** v izolovaném jmenném prostoru (`unshare -Urn`): policy tabulka 51820, bridge-like rozhraní, skutečné chyby `ip` |
| `egress_check.py` | cesta ven kontejneru na sítích [internal, internal, egress] (jako cloudflared po kroku 2): výchozí route z ordinary sítě, bez ní žádná; dosažení okraje Cloudflare je jen pozorování (bez tokenu, bez tunelového protokolu; bez internetu `NOT_RUN`, nikdy PASS) |
| `asgi_echo.py` | ASGI aplikace pro skutečný Uvicorn |

## Co se testuje

| Varianta | Obsah |
|---|---|
| `baseline` | soubory, které produkce skutečně běží (čtou se z gitu, `--ref`, výchozí `HEAD`) |
| `candidate` | návrh: síť `tunnel`, `EDU_AI_TUNNEL_SUBNET` jako jediný zdroj CIDR pro síť i pro důvěru nginx, `X-Forwarded-For` = `$remote_addr` |
| `transition`, `transition2` | kroky nasazení (starý rozsah ještě důvěryhodný; compose starý, pak nový) |
| `mut_broad`, `mut_notrust`, `mut_xff`, `mut_literal`, `mut_cidr` | záměrně špatné návrhy (znovu rozšířená důvěra, žádná důvěra, zachovaný XFF, druhá konstanta v `nginx.conf`, odlišný literál v konfiguraci); příslušná kontrola je musí odhalit |

Dále živé scénáře: `subnet.*` (přidělení a preflight proti skutečnému démonu, odmítnutí kolizí; `subnet.read.*`: čtení všech routovacích tabulek skutečným pomocným image, policy tabulka v izolovaném jmenném prostoru jednorázového kontejneru, skutečné selhání čtení rout nebo adres přes `ip` v kontejneru, opakovaný preflight vlastní běžící instalace), `multi.*` (dvě souběžné instalace s různými projekty a vlastními nepřekrývajícími se
podsítěmi), `cleanup.*` (úklid jednoho běhu při zachování cizích a jiných běhů), `final.nothing_else_changed` (vše mimo laboratoř je po běhu stejné jako před ním).

## Nácvik zavedení a návratu

```bash
python tests/nginx_r2/candidate.py verify --repo . --step1 <SHA> --step2 <SHA> --step3 <SHA>      # commity kroků = odvozené varianty
python tests/nginx_r2/rehearsal_linux.py --compose-binary docker-compose-linux-x86_64 --compose-sha256 <hex> --out DIR \
    --tools <SHA> --step1 <SHA> --step2 <SHA> --step3 <SHA> [--only forward_return,refusals,...]       # cca 40 minut, Docker
wsl -d Ubuntu -e unshare -Urn python3 tests/nginx_r2/host_iproute2.py --out FILE                       # skutečný iproute2, bez Dockeru
python tests/nginx_r2/egress_check.py --out FILE                                                    # cesta ven kontejneru, Docker
```

`--tools` je commit, ze kterého operátor kopíruje skripty; musí být potomkem S2, ale nemusí předcházet kroku 1 (opravený skript leží za posledním krokem; varianty kroků visí na `step1^`).
Nácvik potřebuje Docker, lokální image `nginx:1.27-alpine` (přesně produkční), `python:3.12-slim`, `mcr.microsoft.com/playwright/python:v1.49.0-noble` (executor)
a `docker:cli` (jen jako zdroj klienta `docker`). Binárka Compose se předává výslovně a ověřuje kontrolním součtem (produkce: 5.3.1). Server nácviku je klon
vydání (nasazený S2 + nástroje + tři commity kroků) se skutečným `docker-compose.yml`; laboratorní override jen vymění osm služeb za stuby, cloudflared za
stand-in (loguje trasy ve formátu skutečného logu a je klientem, ze kterého se posílá `CF-Connecting-IP`) a jména kontejnerů. **Stand-in není důkaz skutečného
provozu Cloudflare**; token ani Access se nepoužívají. Offline testy a `selfcheck` potřebují `sh` (na Windows Git for Windows).

## Co je potřeba

| Požadavek | Poznámka |
|---|---|
| Docker Engine + Compose v2 ≥ 2.24 (`!override`; `configs` s `content` od 2.23.1) | nic se nepublikuje na host; zdroje laboratoře nesou štítky `edu_ai.purpose`, `edu_ai.run`, `edu_ai.project` |
| image `nginx:1.27-alpine` **přesně jako produkce** (`sha256:65645c7bb6a0…`) | jinak předpoklad selže (návod v [tests/nginx_s2/README.md](../nginx_s2/README.md)); používá se i jako pomocný kontejner pro čtení tras démona |
| image `python:3.12-slim` | syntetické upstreamy a klienti (znovu se používají `stub.py` a `client.py` z laboratoře S2) |
| volitelně lokální image s Uvicornem | `--asgi-image LABEL=IMAGE`; bez něj je ASGI část `NOT_RUN`, nikdy PASS |
| Python ≥ 3.10, jen standardní knihovna | |

## Spuštění

```bash
python tests/nginx_r2/candidate.py check                 # bez Dockeru: odvození kandidáta, jeden zdroj CIDR, diffy se aplikují na baseline
python tests/nginx_r2/offline_tests.py                   # bez Dockeru: úklid, vlastnictví, přidělení a preflight podsítě, jeden zdroj CIDR, skripty zavedení a pojistka S2
python tests/nginx_r2/switch_offline.py                  # jen úsudky skriptů (R2-R4, R2-R5, pojistka S2): potřebuje bash, git a klient `docker` (démon ne)
python tests/nginx_r2/r2lab.py selfcheck                 # bez Dockeru: totéž + selhání sond a komparátorů nesmí být PASS
python tests/nginx_r2/r2lab.py static                    # nginx -t, delta konfigurace a Compose modelu, jeden zdroj CIDR
python tests/nginx_r2/r2lab.py run --out r2.json \
    --asgi-image api=edu_ai_api:1.0 --asgi-image chat=edu_ai_web_chat:0.1 [--reserved 10.20.0.0/16]
python tests/nginx_r2/r2lab.py cleanup --list-runs       # jen čtení: ID běhů nalezená na štítcích
python tests/nginx_r2/r2lab.py cleanup --run-id ID [--dry-run]
python tests/nginx_r2/tunnel_subnet.py allocate --project NAME --routes docker --helper-image nginx:1.27-alpine
python tests/nginx_r2/tunnel_subnet.py check 10.x.y.z/29 --project NAME [--reserved CIDR ...]
python tests/nginx_r2/candidate.py render --out DIR      # diffy návrhu a odvozené soubory
```

Návratový kód `r2lab.py` je 0 jen když je **každá** kontrola `PASS`. `FAIL` je splněná podmínka s opačným výsledkem, `ERROR` kontrola, která nemohla proběhnout (sonda, Docker,
klient), `NOT_RUN` test, který nebylo čím spustit. `run` na začátku vypíše **ID běhu**; po přerušení se uklidí příkazem `cleanup --run-id ID`.

## Úklid: co smí a nesmí

`cleanup` **vyžaduje konkrétní ID běhu** (`MMDDhhmm` + 4 hexadecimální číslice; předpona, vzor či `*` se odmítnou kódem 2). Kandidáty vybírá **jen podle štítku `edu_ai.run`**,
nikdy podle jména. Zdroj odstraní až po ověření štítků: `edu_ai.purpose` = štítek laboratoře, `edu_ai.run` = toto ID a u zdrojů Compose se `com.docker.compose.project` rovná projektu
zaznamenanému pro tento běh (`edu_ai.project`) a má přesný tvar projektu tohoto běhu. Odstraňuje podle **ID**, kontejnery před sítěmi před svazky. Co není vlastní (jiný běh, cizí síť s podobným
názvem), co nelze ověřit (selhala inspekce) a co nese štítky tohoto běhu, ale s nesouhlasícím projektem, se **zachová a nahlásí** (kód 1). Stejně jako úklid se chová i každé jiné
odstranění v laboratoři (`safe_rm`, `compose down` jen po ověření všech zdrojů projektu); plánované jméno, které už drží cizí zdroj, laboratoř **neadoptuje a nesmaže**, ale odmítne.

## Čtyři vrstvy, které se nezaměňují

Pro každý případ se zaznamená a porovná: **skutečný TCP odesílatel** (adresa kontejneru, kterou nginx vidí; ověřuje ji párový požadavek bez hlavičky a access log),
**přijaté hlavičky**, **adresa klienta v nginx** (`$remote_addr`, první pole access logu) a **co vidí aplikace** (echo upstream a pro ASGI skutečný `uvicorn` ve skutečném image:
`scope["client"]`, `scope["scheme"]`).

## Zapojení

* nginx je **skutečná** služba `nginx` z `docker-compose.yml` dané varianty (`docker compose up --no-deps nginx`); přepsáno je jen jméno kontejneru, žádné porty na host, DNS alias
  `edu_ai_nginx` (v produkci ho dává `container_name`) a štítky laboratoře. Sítě, podsítě, `internal` a `configs` pocházejí ze souboru. Compose běží s prázdným `.env` a s
  **jednorázovým `--env-file`, který obsahuje jen `EDU_AI_TUNNEL_SUBNET` dané instalace**; skutečné `.env` se nečte.
* každá instalace dostane podsíť z `tunnel_subnet.py` (`allocate` + `check` proti skutečnému démonu) **před** vytvořením čehokoli; odmítnutí vyvolá chybu bez vedlejších účinků.
* sítě vytváří Compose podle souboru; zbylé sítě (`app`, `tunnel_egress`, …) laboratoř doplní z efektivního modelu se stejnými štítky. Existující síť se plánovaným jménem se **nepoužije
  ani nezmění**. Protějšky jsou kontejnery na týchž sítích: **člen `edge`** (představuje API, Langflow, search, pgAdmin…), **klient jen v `app`**, **zástupce cloudflared** se sítěmi,
  které mu soubor přiděluje, šest syntetických upstreamů s aliasy jmen z `nginx.conf`. Stand-iny nesou projektový štítek své instalace (přepíše štítek zděděný z image postaveného Compose).
* varianty běží po jedné; scénář `multi` drží dvě instalace s různými projekty současně.
* ASGI: `asgi_echo.py` pod skutečným `uvicorn` z daného image, spuštěný jako v produkci (`uvicorn … --host 0.0.0.0 --port …`, bez příznaků proxy),
  jednou bez a jednou s `FORWARDED_ALLOW_IPS=*` (záporná kontrola: proč `*` nestačí).

## Omezení

Docker Desktop (WSL2), ne Linuxová VM produkce; routy a adresy se čtou přes jmenný prostor démona (pomocný kontejner), ne přes hostitelský Windows ani produkční Linux; syntetické upstreamy,
ne skutečné služby; pgAdmin (`ProxyFix`) a Langflow se netestují; skutečný cloudflared a Cloudflare nejsou součástí (zástupce je kontejner se stejnou sítí a jménem); padělání zdrojové adresy
na L2 (raw sockety, ARP) se netestuje; preflight vidí jen to, co hostitel ví. Výsledek konkrétního běhu uchovávejte mimo veřejné Git.

## Veřejný checkout

Historické odvozování kandidátů přes pevné baseline Git SHA vyžaduje odpovídající
předky. Čistý veřejný kořen privátní historii neobsahuje. Tyto historické varianty
proto nejsou podporovaným testem nového veřejného checkoutu; potřebují vlastní
prověřený baseline. Instalace, runtime konfigurace a běžné offline unit testy
na této historické odvozovací cestě nezávisí.
