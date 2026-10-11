# nginx S2: lokální laboratoř a testy

Dokládá kandidát S2 (historická laboratorní sada):
adresářový mount `nginx/conf` → `/etc/nginx`, dynamické DNS upstreamů (`resolver` + `zone` + `resolve`),
bez `depends_on`, healthcheck nginx. **Žádný přístup na produkci, žádná tajemství, žádný skutečný klíč, tunel ani LLM.**
Pokrývá jen S2; nic jiného z B04a (sítě, profily, `admin_proxy`, env, data) tu není testováno.

## Co je potřeba

| Požadavek | Použito při záznamu | Poznámka |
|---|---|---|
| Docker Engine + Compose v2 ≥ 2.24 (`!override`) | Docker 29.6.1, Compose 5.3.0 (Docker Desktop, WSL2) | stačí lokální démon; lab nic nepublikuje na host |
| Python ≥ 3.10, jen standardní knihovna | 3.11.9 | na Windows `python`, na Linuxu `python3`; run_s2.py při starším Pythonu skončí hned, s hláškou |
| git | – | baseline se čte z `git archive`, ne z pracovního stromu |
| bash + GNU coreutils (`timeout`, `sort -V`, `mktemp`) | Git Bash 5.x (Windows), na Linuxu systémový | jen příkaz `rehearsal` (spouští skutečný `scripts/nginx_s2_switch.sh`); musí v něm fungovat `docker` a `git`, jinak se `S2_BASH` nastaví na vhodný bash |
| image `nginx:1.27-alpine` **přesně jako produkce** | `sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10` (nginx 1.27.5) | test `static` selže, pokud je ID jiné; viz níže |
| image `python:3.12-slim` | `sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` | jen pro syntetické upstreamy a klienta; ID se jen zaznamená |

```bash
# když nginx image chybí nebo má jiné ID (tag 1.27-alpine se na Docker Hubu posouvá):
docker pull nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
docker tag  nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 nginx:1.27-alpine
docker pull python:3.12-slim
```

## Spuštění

Z kořene repozitáře, s kandidátem S2 v pracovním stromu a **ještě necommitovaném** na commitu, který je baseline:

```bash
python tests/nginx_s2/run_s2.py all --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7 --out b04a-s2-results.json
python tests/nginx_s2/run_s2.py static --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7   # ~20 s, bez běžících kontejnerů
```

**Výsledek se bere z čistého klonu**, ne z pracovního stromu s nesledovanými podklady (S2-R4). Obálka vytvoří čerstvý klon,
zkopíruje do něj jen výslovný seznam kandidátních souborů (měněné sledované soubory + nesledované `nginx/conf`, `tests/nginx_s2`,
`scripts/nginx_s2_switch.sh`; nic z `docs/plans`, `QA` apod.), ověří, že `git status` klonu je právě ten seznam, a v klonu spustí
`git diff --check`, docs-check a sadu s výslovným úplným SHA baseline:

```bash
python tests/nginx_s2/clean_clone.py --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7 --out tests/eval_out/s2_clean   # ~25 min
```

Bez `--baseline` s úplným SHA, který je zároveň `HEAD`, obálka skončí. Libovolný příkaz lze předat i jednotlivě (`... static selfcheck`). Výstup je čitelný, navíc JSON (`--out`) se strojovými výsledky,
ID image, SHA-256 kandidátních souborů a časovými řadami. Návratový kód je 0 jen když **všechno** je `PASS`;
`FAIL` (kontrola selhala) i `ERROR` (kontrola nemohla proběhnout) jsou nenulové.

| Příkaz | Co dokládá |
|---|---|
| `static` | A: `nginx -t` bez upstreamů v produkčním image (a kontrola, že baseline stejným testem selže); diff Compose = jen služba `nginx` a jen klíče `volumes`, `depends_on`, `healthcheck`; v mountu jen `nginx.conf`, `search_api.conf`, `mime.types`, žádný default server ani `conf.d`; **efektivní konfigurace (`nginx -T`) se liší přesně o DNS (`resolver`, `zone`, `resolve`); `access_log` a každý `location` beze změny, staré `nginx/*.conf` bajtově shodné a nepoužité**; trusted proxy rozsah a pgAdmin hlavičky beze změny; `mime.types` bajtově shodný s image |
| `equivalence` | B: stejných 173 požadavků proti baseline a kandidátovi za týmiž syntetickými upstreamy; celé odpovědi (status, všechny hlavičky v pořadí kromě `Date`, celé tělo); `shodné + odlišné + nevyhodnocené`; explicitní aserce nezávislé na shodě; rate limit (tolerance); **skutečný SSE a skutečný WebSocket handshake** |
| `resilience` | C: start s chybějícím upstreamem, ostatní cesty, návrat bez reloadu a restartu, výměna na prokazatelně jinou IP, výpadek: zastavená služba / odebraný DNS záznam / mrtvá IP / nereagující proces (status a čas), šum v logu; healthcheck se loguje jako každý jiný požadavek (a běžný loopback i `CF-Connecting-IP: 127.0.0.1` se loguje také) |
| `atomic` | E: atomické nahrazení souboru v mountovaném adresáři + `nginx -t` + `nginx -s reload` skutečně změní odpověď; kontrola, že **dnešní mount jednotlivých souborů** po téže výměně zastará; prokázané omezení (nahrazení samotného adresáře se nenačte) |
| `selfcheck` | D: fault injection kontrol samotných (unhealthy, žádný healthcheck, `starting`, neběžící kontejner, selhání `docker exec`, chybějící interpreter, selhání HTTP klienta, komparátor) **kontrola citlivosti cílenými mutacemi** kandidátních souborů vytvořenými přímo v testu (chybějící pgAdmin `X-Forwarded-Port`, rozšířený trusted rozsah, změněný `/health`, znovu přidaný filtr logu): každá musí být nalezena a nemutovaná kontrola musí mít 0 rozdílů, bez jakýchkoli nesledovaných podkladů; úklid musí fungovat nezávisle na cwd a cizím `COMPOSE_FILE`/`.env` a hlídat i zbylé volumes |
| `rehearsal` | F: skutečný `scripts/nginx_s2_switch.sh` na jednorázových klonech od produkčního SHA `38a0bef` přes `3a2b1d6` a `c2355ae`, commit nástrojů a commit S2: preflight bez změny a nasazení s měřením výpadku (F1–F7); **odmítnutí před jakoukoli změnou** (F8: neznámý commit, lokální commit, lokální změna, kolize souboru, `nginx -t`, `daemon off;` v konfiguraci, jiná služba, dřívější změna starých souborů, chybějící/neběžící kontejner, nedostupný search i API upstream, změněný image v souboru i v override, přesunutý tag, porty, mount, healthcheck, chybějící soubor argumentů); automatický návrat po selhání health / smoke / deadline `compose up` / změně checkoutu (F9) a **přesunutý tag, který zůstane přesunutý po celý deploy i návrat**, pozdní ruční rollback v novém procesu po přesunu tagu a **skutečná smyčka restartů nového kontejneru** (F9); přerušení (F10); návrat při konfliktu, lokální změně, po S2b a při chybějícím upstreamu (F11); hash pojistka fallbacku (F12); jednotkové testy pojistek (F13); **skutečné signály INT/HUP/TERM** (F14); **kolize jména kontejneru** (F15: vlastní napůl vytvořený i přejmenovaný nginx se po ověření štítků projektu a služby odstraní; držitel jména s jiným projektem/službou nebo bez štítků zůstane beze změny a návrat skončí kódem 5 s chybou kolize); bez `reset --hard`, `clean`, `stash`, force push |

## Jak je to zapojené (aby výsledek něco znamenal)

* nginx se spouští **skutečnou** službou `nginx` z `docker-compose.yml` (baseline z `git archive`, kandidát z kopie
  pracovního stromu) přes `docker compose up --no-deps nginx`; přepsáno je jen `container_name`, `ports` (nic se nepublikuje)
  a `networks` (laboratorní sítě). Compose se spouští s **prázdným** `--env-file` a prázdným `.env` v dočasné kopii:
  skutečné `.env` repozitáře se nečte.
* upstreamy jsou syntetické ([stub.py](stub.py)): echo celého přijatého požadavku, časovaný SSE, skutečný WebSocket.
  Každý je samostatný kontejner s aliasem jména z `nginx.conf`, takže jde jednotlivě zastavit, odpojit od sítě, pozastavit,
  nahradit s jinou IP.
* dvě sítě: `172.x` (v rozsahu, kterému `set_real_ip_from 172.16.0.0/12` věří) a `192.168.x` (ne). Klient z každé.
* klient ([client.py](client.py)) běží uvnitř sítě, nesleduje redirecty, vrací celé odpovědi; transportní chyba je `error`,
  nikdy status.
* atomické nahrazení běží na skutečném Linuxovém souborovém systému (named volume uvnitř VM Dockeru, bind mount z jeho
  `Mountpoint`), ne přes sdílení Windows disku, kde inody nemají stejnou sémantiku.

Vše nese štítky `edu_ai.purpose=b04s2-nginx-lab` a `edu_ai.run=<id>` a jména `b04s2_<id>_*`. **Úklid maže jen zdroje vlastního
běhu** (kontejnery, sítě, **volumes**, Compose projekty podle jména a štítku tohoto běhu, dočasný adresář včetně klonů gitu)
a hlásí, pokud něco zůstalo. Pozor: `docker compose up --no-deps nginx` vytvoří **všechny** sítě a volumes projektu
(3 sítě a 4 volumes), ne jen ty, které nginx používá; proto se úklid dělá přes `down -v` s toutéž explicitní definicí
jako start a zbytky se hlídají i podle volumes. Po přerušeném běhu (Compose projekty štítky nenesou, jen jméno):

```bash
python tests/nginx_s2/cleanup_lab.py                  # vypíše run ID, která mají zbytky, nic nemaže
python tests/nginx_s2/cleanup_lab.py <run id>         # vypíše a smaže kontejnery, sítě a volumes jen toho jednoho běhu
```

## Skript nasazení a návratu

`rehearsal` neprovádí vlastní napodobeninu postupu: spouští **skutečný** [`scripts/nginx_s2_switch.sh`](../../scripts/nginx_s2_switch.sh)
(`smoke`, `preflight`, `deploy`, `rollback`) na jednorázových klonech. Klon začíná na `38a0bef`, nginx v něm je vytvořen z
jeho vlastního `docker-compose.yml`, a vydání prochází `3a2b1d6` → `c2355ae` → commit nástrojů → commit S2. Dva poslední
commity se staví z kandidátních souborů pracovního stromu (nástroje a S2 zvlášť, aby se při návratu vracel jen S2).
Varianty s jednou závadou (`bad_conf`, `bad_compose`, `bad_health`, `bad_smoke`, `bad_mount`, `bad_mount_wide`, `bad_image`, `bad_ports`, `bad_healthcheck`, předchozí změna starých souborů) a pozdější vydání (`l1` jiné soubory, `l2` konflikt s revertem a prázdný řádek uvnitř bloku, `l3` = S2b odstraní staré soubory) se vyrábějí v testu.
Compose argumenty skriptu čte `S2_COMPOSE_ARGS_FILE` (jeden argument na řádek); řádek `@BASE@` označuje základní compose soubor: pro `up` je to soubor checkoutu, pro preflight se na jeho místo čte kandidát ze stdin (`-f -`), s týmiž argumenty, env a override. Bez souboru musí `.env` připínat `COMPOSE_FILE=docker-compose.yml`.
Skript vytváří nginx z **ID obrazu**, nikdy ze štítku (override `image-pin.yaml` jako poslední `-f`), a ukládá `images.env` (původní a validované ID, kandidát) a soukromé štítky `S2_EVIDENCE_REPO:original-<id>` / `validated-<id>`; pozdější `rollback` v novém procesu čte tuto evidenci. Laboratoř používá vlastní repozitář štítků a po sobě je maže.
Pomocný přepínač: `S2_REHEARSAL_ONLY=scn_refusals,scn_interruptions python tests/nginx_s2/run_s2.py rehearsal ...`;
`S2_EXPORT_PATCHES=<adresář>` (nebo `clean_clone.py --patches`) vyexportuje oba commity jako patche.

## Izolovaný Linux (skutečné signály)

```bash
python tests/nginx_s2/clean_clone.py --baseline <úplné SHA> --linux rehearsal static     # asi 40 minut
```

`--linux` pustí sadu **uvnitř Linuxového kontejneru** nad stejným Docker démonem: Ubuntu 24.04 (`mcr.microsoft.com/playwright/python:v1.49.0-noble`, GNU bash,
coreutils, git, Python 3.12), `docker` CLI a plugin Compose zkopírované z oficiálního image `docker:cli` (stažen jednou se souhlasem vlastníka; `docker pull docker:cli`),
Docker socket připojený dovnitř. Jména, která démon mountuje, musí platit i pro démona: vše laboratorní leží na pojmenovaném volume připojeném na vlastní
Mountpoint, takže stejná absolutní cesta platí uvnitř kontejneru i pro démona. Skutečný checkout se nepřipojuje, kontejner dostane kopii čistého klonu.
Konkrétní vydání Compose (např. produkční 5.3.1): `--compose-binary <soubor> --compose-sha256 <hex>` nahradí plugin z `docker:cli`; soubor se ověří kontrolním součtem. Úklid: `cleanup_lab.py <run id>` (jména `b04s2_<id>_linux`, `b04s2_<id>_lxdata`, štítky obrazů `b04s2_<id>_evidence/nginx:*`).

## Omezení

* Měřeno na Docker Desktop (WSL2), ne na Linuxové VM školy ani na produkčním stroji. Latence a chování jádra
  (ARP, SYN timeouty) se mohou lišit; výpadky „mrtvá IP“ ukazují, že výsledek kolísá mezi koly (503 po desítkách sekund /
  504 po 60 s) stejně u obou variant.
* Stub není skutečná aplikace: dokazuje chování nginx, ne aplikací (např. SSE skutečného `search`, přihlášení pgAdmin).
* Rate limit se posuzuje statisticky (první 429 kolem požadavku č. 21, tolerance ±3), ne přesnou shodou.
* Skript běžel v Git Bash (MSYS) na Windows a v izolovaném Linuxu (viz výše); na produkčním stroji ani na Linux VM školy nespuštěn. Signály se posílají skutečně (`kill -s`), ne zastavením po fázi; produkční hostitel se může lišit verzí bashe, gitu a Compose.
* Nezkouší se Cloudflare tunel, AWS security group, produkční oprávnění, zálohy ani migrace (mimo S2).
* Kontroly R2 (hranice důvěry `CF-Connecting-IP`) a R3–R7 z revize B04a se tímto S2 **neřeší**. Skripty v
  `docs/plans/archive/audit-04a-proposals/verify/` (pro S5) mají stále falešné PASS z R4 a pro S2 se nepoužívají.

## Veřejný checkout

Historické odvozování kandidátů přes pevné baseline Git SHA vyžaduje odpovídající
předky. Čistý veřejný kořen privátní historii neobsahuje. Tyto historické varianty
proto nejsou podporovaným testem nového veřejného checkoutu; potřebují vlastní
prověřený baseline. Instalace, runtime konfigurace a běžné offline unit testy
na této historické odvozovací cestě nezávisí.
