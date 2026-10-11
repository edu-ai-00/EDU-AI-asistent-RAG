# R2: hranice důvěry proxy hlavičkám — návrh (varianta 2)

Veřejná kopie vynechává interní akceptační reporty a odkazy na ně; tento návrh není důkazem aktuálního nasazení.

Datum: 2026-10-04 (varianta 1: 2026-10-03), pracovní větev `clean_init`.
**Stav: opravená varianta 2 je laboratorně ověřená a revize R2-R1–R3 uzavřená v doloženém rozsahu. Je podkladem pro přípravu implementačního kandidáta; není implementovaná ani nasazená.**
**Následná revize Codex 2026-10-04:** R2-R3 potvrzené, vlastní selfcheck 38/38,
tři vadné varianty nezávisle odhalené; převzatých 105 PASS a devět hashů zkontrolováno. Výsledky po opravě.
**Implementační kandidát:** na `clean_init` jsou nástroje (`796f060085`) a tři commity kroků (`92bb1ac866`, `6d65862de0`, `4a8aff1920`), bajt po bajtu odvozené z tohoto návrhu; checkpoint,
výsledky nácviku. Kandidát je připravený k revizi, **ne k nasazení**; produkční `docker-compose.yml`, `nginx/conf` a aplikace se nezměnily.
**Revize Codex (2026-10-03):** původní variantu vrátila k dopracování adresace a bezpečnosti úklidu;
R2-R1 a R2-R2. Počet PASS není schválení žádné varianty k implementaci či nasazení.
Podklad: sekce R2 v b04a-review.md. Harness: [tests/nginx_r2](../../tests/nginx_r2/README.md).
Diff návrhu (generovaný z gitu, ne ručně psaný): r2-proposed-diff.md.
**Platné výsledky:** r2-lab-results-v3.md (varianta 2 po opravě R2-R3). Záznamy varianty 2 před opravou (97 PASS) a varianty 1
(r2-lab-results.md, JSON) zůstávají beze změny jako historické; varianta 1 se týká pevné podsítě `192.0.2.0/29`, která už není návrhem.

## 0. Co varianta 2 mění (oprava revize)

| Nález | Oprava | Kde je to ověřené |
|---|---|---|
| **R2-R1** pevná `192.0.2.0/29` (RFC 5737 je pro dokumentaci, ne pro lokální použití) a stejný CIDR pro každou instalaci | Žádná konstanta v repozitáři. CIDR je **nastavení instalace** `EDU_AI_TUNNEL_SUBNET`, přidělené z privátního rozsahu `10/8` bez známých bloků a **zkontrolované proti skutečnému stavu hostitele** (Docker sítě a pooly, routy, adresy rozhraní, známé cloudové/VPN/Kubernetes rozsahy, rozsahy zadané správcem). Při překryvu příprava odmítne pokračovat dřív, než cokoli vytvoří | oddíly 4.2–4.4; lab `subnet.*`, `static.*`, offline `subnet.*` |
| **R2-R1** dvě nezávislé konstanty (Compose, nginx) | **Jeden zdroj:** Compose bere tutéž proměnnou pro podsíť sítě `tunnel` i pro obsah konfigurace `tunnel_trust`, kterou nginx načte `include`; v `nginx.conf` ani v jiném souboru žádný CIDR není. Bez proměnné se model nevykreslí | `static.single_source.*`, `static.subnet_required`, `*.cidr_agreement` |
| **R2-R1** dvě instalace na jednom démonu (stejný CIDR → překryv) | Každá instalace má **vlastní** podsíť a vlastní `tunnel`; důvěryhodná síť se mezi instalacemi nesdílí | `multi.*` (dvě souběžné instalace) |
| **R2-R2** úklid podle části názvu `b04r2_` bez ověření vlastnictví | Úklid vyžaduje konkrétní **run ID**, vybírá jen podle štítků, před odstraněním ověří štítky (u Compose zdrojů přesný projekt běhu), odstraňuje podle ID, a co nelze ověřit nebo není vlastní, **zachová a nahlásí**. Ostatní místa, která odstraňovala podle plánovaného názvu, ověřují vlastnictví nebo odmítnou | oddíl 4.5; offline `cleanup.*` (včetně reprodukce původní chyby), lab `cleanup.*`, `final.nothing_else_changed` |

## 1. Problém, jak je dnes (produkční konfigurace, ne návrhy S5)

| Prvek | Dnes |
|---|---|
| Kdo je v síti `edge` | nginx, `edu_ai_api` (stahuje URL zadané uživatelem), `edu_ai` (Langflow, web crawl), `edu_ai_search`, `edu_ai_pgadmin`, `edu_ai_web_chat`, `edu_ai_qa_admin` a `cloudflared` |
| Kam nginx poslouchá | 80, 8443, 8444 publikované na host; **81 jen v kontejnerech, ale na všech rozhraních nginx**, tedy dosažitelné každým členem `edge` |
| Komu nginx věří adresu klienta | na portu 81 `set_real_ip_from 172.16.0.0/12` + `real_ip_header CF-Connecting-IP`; na ostatních portech nikomu |
| Co se posílá aplikacím | `X-Real-IP $remote_addr`, `X-Forwarded-For $proxy_add_x_forwarded_for` (**řetěz od volajícího se zachová a nginx připojí svou adresu**), `X-Forwarded-Proto $scheme` (vždy `http`) |
| Co aplikace s tím dělají | v `api/`, `search/`, `web_chat/` ani `qa_portal/` žádný kód nečte `X-Real-IP`, `X-Forwarded-*`, `CF-Connecting-IP` ani `request.client`; Uvicorn spouští bez příznaků proxy (výchozí `FORWARDED_ALLOW_IPS=127.0.0.1`) |

Privátní adresa odesílatele neprokazuje, že jde o důvěryhodnou proxy: kdokoli na `edge` (zejména API, které po zneužití stahuje cizí URL)
pošle na `nginx:81` libovolné `CF-Connecting-IP` a nginx ho uvěří. Laboratoř to na produkční konfiguraci **reprodukovala** (varianta 1 i 2).
Dopad dnes: obejití per-IP rate limitu (`api_limit`, 10 r/s), vyčerpání cizího kbelíku (útok na konkrétního klienta), zfalšovaný access log
a vadná hranice pro jakýkoli budoucí kód, který by hlavičkám uvěřil. Přímý dopad na identitu v aplikacích dnes není, protože ji ze hlaviček nečtou.

## 2. Čtyři vrstvy (v návrhu i v testech se nezaměňují)

| Vrstva | Co to je | Komu se věří |
|---|---|---|
| **TCP odesílatel** | adresa, ze které nginx skutečně přijal spojení | je to jediný fakt, který volající nemůže zvolit |
| **přijaté hlavičky** | co odesílatel napsal do požadavku (`CF-Connecting-IP`, `X-Forwarded-*`) | nikomu sama o sobě; věřit lze jen hlavičce od **prokázané** proxy |
| **adresa klienta v nginx** (`$remote_addr`, access log, `limit_req`) | TCP odesílatel, nebo hodnota `CF-Connecting-IP`, pokud odesílatel patří do `set_real_ip_from` | tomu, co nginx podle předchozích dvou vrstev rozhodl |
| **co vidí aplikace** | hlavičky z nginx (`X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`) a to, co z nich udělá Uvicorn (`scope["client"]`, `scope["scheme"]`) | Uvicorn jen odesílateli z `FORWARDED_ALLOW_IPS` (výchozí `127.0.0.1`, tj. nginx ne) |

Široký privátní rozsah, jméno služby ani přijetí hlavičky nejsou důkaz důvěryhodnosti. Návrh proto definuje důvěryhodného odesílatele
**členstvím v síti, do které smí patřit jen on**.

## 3. Zvažované volby

| Volba | Verdikt | Důvod |
|---|---|---|
| ponechat nebo rozšířit privátní CIDR (`172.16.0.0/12`, návrh S5 navíc `192.168/16`, `10/8`) | zamítnuto | v rozsahu je každý člen `edge`; širší rozsah problém jen zvětšuje |
| jméno služby v `set_real_ip_from` | zamítnuto | nginx jméno překládá jen při načtení konfigurace (dokumentace modulu `realip`), ne za běhu: po výměně kontejneru je adresa stará až do reloadu; neřešitelné jméno při startu by navíc znemožnilo start nginx, čemuž S2 záměrně předchází |
| nevěřit nikomu | zamítnuto | rate limit na portu 81 by se stal společným pro celý internet (původní důvod funkce) |
| pevná IP cloudflared v `edge` | zamítnuto | vyžaduje připnout podsíť a adresu sdílené sítě `edge` (přetvoření sítě všech osmi kontejnerů) a výměna proxy by musela obsadit tutéž adresu |
| sdílené tajemství v hlavičce (Cloudflare Transform Rule) | zamítnuto | tajemství a konfigurace jen v dashboardu; každý, kdo ho zná, je „proxy“; složitější než síťová hranice |
| UNIX socket (`listen unix:`, `set_real_ip_from unix:;`, ingress `unix:`) | **silnější alternativa, neověřeno** | důvěra plyne z držení svazku, bez adres a podsítí (a tedy bez celé otázky přidělování z R2-R1). Cloudflared UNIX službu v ingress pravidlech podporuje, ale dokumentace výslovně nepotvrzuje typ UNIX u tunelu spravovaného dashboardem; vyžaduje změnu veřejného jména v dashboardu a nezkoušelo se chování zbylého socketu po pádu. Do této laboratoře nepatří |
| cloudflared v síťovém jmenném prostoru nginx (`network_mode: service:nginx`) | zamítnuto | spojí životní cykly (po výměně nginx zůstane cloudflared v zaniklém jmenném prostoru, což S2 odstranilo) a odebere cloudflared síť `app` pro Bifrost |
| `FORWARDED_ALLOW_IPS=*` v aplikacích | zamítnuto | volající z `edge` pak může identitu měnit přímo, i bez nginx; laboratoř to ukázala se skutečným Uvicornem |
| pevná podsíť v repozitáři (varianta 1: `192.0.2.0/29`) | **zamítnuto revizí R2-R1** | rozsah je vyhrazený pro dokumentaci, ne pro lokální použití; stejný CIDR pro každou instalaci se na jednom démonu překrývá; dvě nezávislé konstanty se mohou rozejít |
| **vyhrazená síť `tunnel`, jen nginx + cloudflared; CIDR je nastavení instalace s jedním zdrojem, přidělené a zkontrolované proti stavu hostitele** | **doporučeno (varianta 2)** | beze změny v dashboardu, bez změn aplikací, malý rozsah; selhává bezpečně |

## 4. Doporučený návrh (varianta 2)

### 4.1 Sítě a hranice důvěry

**Síť `tunnel`** (`internal: true`, jen nginx a cloudflared) a síť `tunnel_egress` (cloudflared → Cloudflare, nikdo jiný). nginx je v `edge` + `tunnel`;
cloudflared v `app` + `tunnel` + `tunnel_egress` (**už ne v `edge`**). Veřejné jméno v dashboardu zůstává `http://edu_ai_nginx:81`: cloudflared sdílí s nginx jen
`tunnel`, takže se jméno přeloží právě na adresu nginx v této síti. `X-Forwarded-For` se posílá jako `$remote_addr` (sedm míst), řetěz od volajícího se zahazuje.

### 4.2 Jeden zdroj CIDR (R2-R1)

```text
EDU_AI_TUNNEL_SUBNET=<podsíť instalace>   # nastavení instalace; v repozitáři žádná hodnota ani výchozí
        │
        ├─ docker-compose.yml: networks.tunnel.ipam.config[].subnet: ${EDU_AI_TUNNEL_SUBNET:?...}
        └─ docker-compose.yml: configs.tunnel_trust.content:  set_real_ip_from ${EDU_AI_TUNNEL_SUBNET};
                  └─ připojeno do nginx jako /etc/edu_ai/tunnel/trust.conf ── nginx.conf (server 81):  include /etc/edu_ai/tunnel/*.conf;
```

* V `nginx.conf` ani v jiném sledovaném souboru **není žádný CIDR**; `docker compose config` bez proměnné model **nevykreslí** (výchozí hodnota by skrývala konstantu).
* **Bez souboru nginx nikomu nevěří a startuje** (glob bez shody je v nginx přípustný; ověřeno `nginx -t`): bezpečný pád, ne výpadek.
* Obě použití proměnné vznikají jedním vykreslením Compose, takže se nemohou rozejít. Přesto je ověřuje laboratoř ze **skutečně běžícího** systému: nastavení = podsíť Docker sítě =
  rozsah, se kterým nginx běží (`nginx -T`). Záměrně rozbité varianty (druhá konstanta v `nginx.conf`, odlišný literál v konfiguraci) kontroly odhalí.
* Změna hodnoty u existující instalace: dokud existuje síť s jinou podsíti, kontrola ohlásí `NETWORK_MISMATCH` a síť se nemění (ověřeno). Jak Compose při takové změně přestaví
  síť a `nginx`, se **neověřovalo**; změna podsítě existující instalace proto není součástí tohoto návrhu a patří do samostatného postupu.
* Vyžaduje Compose, který umí `configs` s `content` a interpolaci (od 2.23.1); laboratoř 5.3.0, produkce podle předchozích čtecích kontrol 5.3.1.

### 4.3 Přidělení podsítě instalaci a preflight (R2-R1)

Nástroj [tunnel_subnet.py](../../tests/nginx_r2/tunnel_subnet.py) (v harnessu; do repozitáře provozních skriptů patří až při implementaci) je **jen čtecí** a má dva příkazy:
`allocate --project NAME` (vypíše `EDU_AI_TUNNEL_SUBNET=<hodnota>`) a `check CIDR --project NAME` (povinný preflight při každém vytvoření nebo aktualizaci).
Návratový kód 0 = volné; **2 = odmítnuto (kolize nebo neplatná hodnota)**; **3 = odmítnuto, stav hostitele nelze ověřit** (neověřené se nepovažuje za volné).
**Každé povinné čtení (routy, adresy, Docker) má vlastní návratový kód a selhání kteréhokoli je kód 3**; úspěch jednoho čtení nikdy nezakryje selhání druhého (R2-R3). Pomocný kontejner proto
vrací 11 při selhání čtení rout a 12 při selhání čtení adres, a příkaz, který `ip` odmítne (např. `table all`), se nikdy nenahrazuje čtením jen tabulky `main`.
Prázdný výstup úspěšného čtení je výsledek; žádné routy a zároveň žádné adresy, nebo úspěšné čtení s nečitelným výstupem, se odmítá (kód 3).

| Co se čte (jen čtení) | Co se odmítne |
|---|---|
| `docker network ls/inspect` (všechny sítě všech projektů) | překryv se sítí jiného projektu (`DOCKER_NETWORK`); vlastní síť `<projekt>_tunnel` se stejnou podsíti kolize není, s jinou podsíti je `NETWORK_MISMATCH` a síť se nemění |
| adresní pooly démona (`docker info`, vestavěné `172.16.0.0/12` a `192.168.0.0/16`) | překryv s poolem (`DOCKER_POOL`) |
| routy hostitele **ze všech IPv4 tabulek** (`ip -4 route show table all`: main, local i policy tabulky, např. VPN) | překryv s trasou v kterékoli tabulce (`HOST_ROUTE`); vlastní bridge instalace (`br-<id sítě>`, včetně jeho local a broadcast tras) se vyjímá, jinak by se běžící instalace při kontrole odmítla sama |
| adresy rozhraní hostitele včetně VPN a WireGuard (`ip -4 addr`) | překryv s adresou/sítí rozhraní (`HOST_ADDRESS`) |
| vestavěný seznam známých rozsahů | `DENYLIST`: Docker pooly `172.16.0.0/12`, `192.168.0.0/16`; Swarm/Azure/domácí LAN `10.0.0.0/16`; OpenVPN `10.8.0.0/16`; k3s `10.42.0.0/15`; **vyhrazený příklad WireGuard `10.66.0.0/16`**; kubeadm `10.96.0.0/12`; GCP `10.128.0.0/9`; flannel/AKS `10.244.0.0/16`; `100.64.0.0/10`; `169.254.0.0/16` |
| `--reserved CIDR` (správce) | `RESERVED`: LAN, VPN, VPC a jiné rozsahy, které hostitel nevidí |
| vlastní hodnota | `INVALID` (není kanonická síť: nastavené bity hostitele, jiná délka než /28–/29, IPv6) a `NOT_PRIVATE` (mimo RFC 1918) |

**Volba rozsahu a její zdůvodnění.** Přiděluje se malá podsíť `/29` (6 použitelných adres: nginx, cloudflared a jeho výměna za běhu) z `10.0.0.0/8` **po odečtení známých bloků**.
Rozsahy `172.16.0.0/12` a `192.168.0.0/16` se nevolí: leží v nich Docker pooly, domácí a kancelářské LAN i VM Docker Desktopu (`192.168.65.0/24`), takže by kolidovaly nejčastěji. Pozice
v poolu se odvozuje z názvu projektu (jen aby se instalace rozprostřely; nic to nedokazuje) a **o použitelnosti rozhoduje výhradně preflight proti skutečnému stavu hostitele**.
Neobvyklé číslo není důkaz, že rozsah nekoliduje, a volný rozsah dnes může zítra obsadit nová VPN či trasa: proto se `check` spouští při **každém** vytvoření a aktualizaci a hodnota
se po přidělení ukládá do nastavení instalace, ne znovu počítá při každém startu. Neviditelné rozsahy (LAN a VPN, o kterých hostitel neví) musí správce uvést přes `--reserved`.
Když je pool vyčerpán, nástroj odmítne (`NoFreeSubnet`) a rozhodnutí je na vlastníkovi.

**Produkce.** V tomto kroku se produkční hostitel **nekontroloval ani nečetl**. Kolize s jeho routami, WireGuardem, VPC/LAN a existujícími Docker sítěmi jsou tedy **neověřené**;
`allocate` a `check` na produkčním hostiteli (s přímým čtením `ip`, `--routes host`) jsou **povinný preflight před pozdějším zavedením** a jejich výstup se zaznamená.

**Nesdílení.** Každá instalace (Compose projekt) má vlastní síť `tunnel` s vlastní podsíti; kontrola odmítne, když si jiná instalace žádá už přidělený CIDR, takže důvěryhodná síť se mezi
instalacemi nesdílí a hranice důvěry jedné instalace se druhé netýká (laboratoř: nginx každé instalace běží s jediným rozsahem, a to svým; spojení přes hranici vyprší).

### 4.4 Dopady

| Oblast | Dopad |
|---|---|
| Sítě | +`tunnel`, +`tunnel_egress`; cloudflared opouští `edge` (už nedosáhne na služby v `edge`, viz předpoklady); nginx dostane druhé rozhraní; nová konfigurace `tunnel_trust` |
| Nastavení instalace | nová proměnná `EDU_AI_TUNNEL_SUBNET` v nastavení instalace (u produkce `.env`; **v tomto kroku se nemění**); bez ní se model nevykreslí |
| Listenery | beze změny: 80, 8443, 8444 publikované stejně, 81 stejně jako dnes. Člen `edge` se na port 81 stále dostane, ale **jako sám sebe** (jako na port 80); jeho `CF-Connecting-IP` se ignoruje |
| Hlavičky | `X-Forwarded-For` = jedna adresa (klient z důvěryhodné proxy, jinak TCP odesílatel); `X-Real-IP` a `X-Forwarded-Proto` beze změny (nginx je vždy přepíše, `X-Forwarded-Proto` je `http`). Hlavičky `CF-Connecting-IP`, `Forwarded`, `True-Client-IP` od volajícího se dál předávají beze změny: aplikace je nesmějí číst |
| Aplikace | beze změny kódu; žádná nečte tyto hlavičky; Uvicorn jim bez `FORWARDED_ALLOW_IPS` nevěří (ověřeno ve skutečných verzích 0.52.4 a 0.35.0) |
| Rate limit a log | klíčem je klient z důvěryhodné proxy, jinak skutečný TCP odesílatel; podvrh už nemění ani jedno |

### 4.5 Bezpečný úklid laboratoře (R2-R2)

Úklid je omezen na jediný běh a vlastnictví dokazují štítky, ne jméno: zdroj je vlastní, jen když `edu_ai.purpose` = štítek laboratoře, `edu_ai.run` = ID běhu a u Compose zdrojů se
`com.docker.compose.project` rovná projektu zaznamenanému pro tento běh (`edu_ai.project`) a má přesný tvar projektu tohoto běhu. `cleanup --run-id ID` vybírá kandidáty **jen podle štítku
běhu**, ověří vlastnictví každého, odstraňuje podle **ID** (kontejnery, sítě, svazky v tomto pořadí) a co není vlastní, co nelze zkontrolovat (selhání inspekce) nebo co jen nese podobný
název, **zachová a nahlásí**. ID běhu je povinné a musí mít přesný tvar (předpona, vzor ani `*` nestačí); `--dry-run` nic neodstraňuje. Ostatní místa harnessu, která dřív odstraňovala podle
plánovaného jména (předčištění kontejneru před spuštěním, `compose down`, adopce existující sítě), dnes před zásahem ověřují vlastnictví, jinak odmítnou a nic nemění.

## 5. Chování v nestandardních stavech

| Stav | Chování |
|---|---|
| tunel zapnutý | cloudflared na `tunnel` → nginx věří `CF-Connecting-IP` → klient zůstává zachován |
| **tunel vypnutý** (kontejner zastaven nebo chybí) | nginx běží beze změny (healthy, RestartCount 0, 80/8443/8444 odpovídají); `tunnel` nemá žádného člena kromě nginx; veřejné jméno v Cloudflare nemá origin, jinak se nic nemění; podvrh z `edge` se dál ignoruje |
| výměna kontejneru cloudflared (jiná adresa v podsíti) | důvěra je členství v síti, ne adresa: nová adresa je opět důvěryhodná bez reloadu nginx |
| výměna nginx | adresa nginx na `tunnel` se nikde nepíše; jméno `edu_ai_nginx` se překládá znovu |
| cloudflared omylem v jiné síti (např. zpět v `edge`) | hlavička se **ignoruje** (bezpečný pád): rate limit se pro tunel stane společným, nikdy ne zfalšovaným |
| cloudflared v `edge` **i** `tunnel` | laboratoř: jméno `edu_ai_nginx` se přeložilo jen na adresu v `edge` a všech 12 požadavků se ignorovalo (pozorování jedné verze Dockeru, ne záruka); proto cloudflared `edge` opouští |
| kontejner `tunnel` navíc | stane se důvěryhodným: hranice je členství. Do `tunnel` se nesmí připojit nic dalšího; pravomoc připojit síť má správce Dockeru (stejně jako root na hostiteli) |
| podsíť koliduje (síť, pool, trasa, adresa, známý rozsah, rozsah správce) nebo ji nelze ověřit | **příprava se odmítne před vytvořením čehokoli**; existující sítě ani routování se nemění |
| soubor `trust.conf` chybí (např. starý compose) | nginx startuje a nevěří nikomu (rate limit tunelu společný, nikdy zfalšovaný) |
| dvě instalace na jednom hostiteli | každá má vlastní podsíť a `tunnel`; odstranění jedné druhou nezasáhne |

## 6. Předpoklady a pořadí zavedení (implementační krok, ne dnes)

Předpoklady, které **nelze ověřit z repozitáře** a musí je potvrdit vlastník:
1. Dashboard Cloudflare: žádné veřejné jméno tunelu necílí na službu v `edge` jinou než `nginx:81`. Po odebrání `edge` by takové jméno přestalo fungovat. Podle repozitáře
   jsou cíle jen `edu_ai_nginx:81` a `bifrost:8080`; skutečný dashboard se tu nečetl.
2. **Povinný preflight podsítě na produkčním hostiteli** (`allocate`/`check`, `--routes host`, s `--reserved` pro LAN/VPN/VPC, které hostitel nevidí); kolize musí být nulové a hodnota
   se uloží jako nastavení instalace. Produkční stav je v tomto návrhu **neověřený**.
3. Rozhodnutí o konci návratového okna S2: skript `scripts/nginx_s2_switch.sh` počítá s tím, že S2 mění jen blok `nginx`; po změně sítí nginx v dalším commitu by jeho návrat
   (`git revert` S2, obnova bloku) musel být znovu posouzen.
4. Compose na hostiteli umí `configs` s `content` a interpolací (od 2.23.1); skutečné připojení konfigurace do kontejneru se ověřilo jen na Docker Desktop (WSL2), ne na Linuxové VM produkce.

Pořadí (každý krok samostatně ověřit a lze se vrátit; S2 skript se na R2 nepoužije, preflight S2 odmítá jiné změny než S2):
0. Preflight a uložení `EDU_AI_TUNNEL_SUBNET` (jediná změna nastavení; bez ní se nový compose nevykreslí).
1. `nginx.conf` s `include` souboru od Compose a `X-Forwarded-For $remote_addr`, **starý rozsah `172.16.0.0/12` ponechán**; `nginx -t`; `nginx -s reload` (adresářový mount to umožňuje,
   bez výměny kontejneru). Zatím se nemění, komu nginx věří (soubor ještě není připojen). Laboratoř: varianta `transition`.
2. Compose se sítěmi a konfigurací: znovu vytvořit jen `nginx` a `cloudflared` (explicitní definice a připnuté image ID jako u S2, žádné obecné `up`); nginx teď věří novému i starému rozsahu,
   cloudflared je už v `tunnel`; ověřit veřejné jméno tunelu a zachování klientské adresy. Laboratoř: varianta `transition2`.
3. Odebrat řádek `172.16.0.0/12`; `nginx -t`; reload; ověřit, že podvrh z `edge` už neprochází. Laboratoř: varianta `candidate`.
Návrat opačným pořadím; po kroku 1 nic nezávisí na síti, po kroku 3 vrátit řádek.

## 7. Co návrh neřeší a rizika

* **Přímé volání aplikací členem `edge`.** Každý člen `edge` může volat `edu_ai_api:8080` a další přímo a poslat jim `X-Real-IP` i `X-Forwarded-*`. Uzavřít to může jen oddělení aplikací
  od nepovolaných odesílatelů (S5, dedikovaná síť nginx → aplikace). Proto **žádná aplikace nesmí těmto hlavičkám věřit** a `FORWARDED_ALLOW_IPS=*` se nepoužije.
* Port 81 zůstává dostupný z `edge` (jako sám sebe). Zúžení `listen` na adresu v `tunnel` by vyžadovalo pevnou adresu nginx; nenavrhuje se.
* `X-Forwarded-Proto` je vždy `http` (TLS končí před nginx); skutečné schéma se nepředává.
* Padělání zdrojové adresy na vrstvě L2 (raw sockety, ARP) a pgAdmin (`ProxyFix`) a Langflow se netestovaly; u pgAdmin nový `X-Forwarded-For` obsahuje stejnou poslední položku jako dosud.
* Preflight vidí jen to, co hostitel ví. Síť, kterou hostitel nezná (vzdálená LAN za jinou bránou, VPN později), může kolidovat až později; proto se `check` opakuje a správce uvádí `--reserved`.
  Podsíť se při překryvu s pozdější trasou nemění sama, kontrola jen odmítne další vytvoření či aktualizaci.
* Laboratoř (a tedy preflight) četla routy a adresy přes jmenný prostor démona Docker Desktopu, ne přes hostitelský Windows ani produkční Linux. `table all` je ověřený pro pinned pomocný image
  (BusyBox 1.37); `--routes host` se skutečným iproute2 na Linuxu, tabulky s číslem nad 255 (např. `wg-quick` 51820) ve skutečném BusyBoxu a pomocný image, který by `table all` tiše ignoroval, jsou neověřené
  (omezení).

## 8. Doporučený minimální implementační krok

Jeden změnový celek ze dvou diffů výše (compose + `nginx/conf`) a z nástroje pro přidělení/preflight podsítě, nasazený podle oddílu 6 po potvrzení předpokladů 1–4 a výslovném souhlasu
vlastníka; bez změny aplikací. Před ním přesunout kontroly shody CIDR, členství v `tunnel` a preflight do běžných testů a opravit komentáře v `docker-compose.yml`. Širší S5 a UNIX socket
zůstávají samostatná rozhodnutí. **Laboratorní návrh je po revizi R2-R1–R3 potvrzený v doloženém rozsahu.
Implementační kandidát (oddíl 6 jako skript, s nácvikem přechodu a návratu) je připraven k revizi: checkpoint;
produkční nasazení vyžaduje revizi kandidáta, uzavření neověřených bodů (Cloudflare/Access, skutečný cloudflared) a výslovný souhlas vlastníka.**
