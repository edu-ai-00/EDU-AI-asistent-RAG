# B06-A: návrh čisté instalace EDU-AI na nový Linux server

Veřejná kopie vynechává interní akceptační reporty a odkazy na ně; tento návrh není důkazem aktuálního nasazení.

**Následná revize Codex (2026-10-05):** záznam. Návrh je podle ní sjednocen: B06-1 je jen offline příprava konfigurace
nad explicitním SHA a cílovým adresářem (R1), identitní test je oddělen od QA retrievalu (R2), zveřejnění páru souborů a pravidlo tajností jsou
upřesněny (R3) a faktická upřesnění jsou zapracována. Nejde o schválení Docker instalace, změn Compose ani produkčního provozu.

**Revize implementace (Codex, 2026-10-05):** záznam. Čtyři nálezy (R1 záměna rezervace, R2 pozdní kontrola partial clone,
R3 `git replace`, R4 signál po `rename`) jsou opraveny ve třech kolech a pokryty regresními testy (oddíl 10). Druhé kolo (2026-10-06) opravilo mezery prvního;
třetí kolo byly tři nezávislé revize (záznam), z nichž revize A prokázala, že úklid mohl smazat cizí adresář (nesplněné R1). To je uzavřeno.

Pozn. auditu (SHA c8fe0543): stav níže je historický. B06-2 existuje (`scripts/host_preflight.py`, `scripts/setup.py`) a průvodce byl doložen
na skutečných VM, viz [public-source.md](../releases/public-source.md); tvrzení „B06-2 a B06-3 neexistují“ a „bootstrap neproběhl“ už neplatí.
Odkazy na neveřejné soubory (`production-readiness.md`, `b03*-progress.md`, `audit-04a`, `r2-lab-results-v3.md`) nelze ve veřejném stromu ověřit.

Stav (2026-10-06): **kandidát B06-1 + bootstrap (oddíl 10), publikováno na `origin/clean_init` (kód a testy v commitu `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, dokumentace v následujícím commitu; jeho SHA nelze uvést uvnitř něj, viz `git log` těchto souborů). B06-1 je implementováno offline a prošlo třemi nezávislými revizemi a závěrečnou revizí Codex (pět nálezů uzavřeno); bootstrap je zpevněn (klíč Langflow, tajnosti ve výstupu, atomický zápis `.env`) a API klíčů bylo laboratorně ověřeno proti Langflow 1.10.1. Není to hotová instalace ani podporovaná aktualizace školy; celý bootstrap, PostgreSQL a B06-2 jsou další etapa, B06-3 neexistuje a zbytek je návrh. Podporované aktualizace řeší jen návrh [B06-U](b06-upgrade-strategy.md), který nic neimplementuje.** Starší stavy v tomto dokumentu jsou historie.
Datum: 2026-10-06 (původní návrh 2026-10-05). Výchozí SHA návrhu: `a19f9716dfb681e2cfb39a5ff6c74bd0dcc09250` (`clean_init`); opravy R1–R4 vznikly nad publikovaným `cfd6f7de5b23d37e61efbfba119d6c905bde147d`.
Vlastník 2026-10-06 schválil publikaci tohoto kandidáta a ponechání pravidel pro API klíč (oddíl 10.7); jinak nic neschválil. D12–D20 se považují za neschválené; podklady z B04a jsou uvedeny jen jako neschválený vstup.
Rozsah je **první instalace**. Podporované aktualizace existující školy hotové nejsou a runner B03e-1 smí jen na izolované syntetické DB
([AGENTS.md](../../AGENTS.md), production-readiness.md řádky 47 a 158), takže tento návrh žádnou aktualizaci neslibuje.

Podklady byly jen čtené. Implementace B06-1 nepoužila SSH, AWS, Cloudflare, produkci, síť ani databázi a nic neinstalovala; testy běžely lokálně na Windows a ve WSL2. Docker se použil jen v laboratorním ověření API klíčů proti Langflow 1.10.1 (2026-10-06, oddíl 10.6: samostatný kontejner bez sítě, syntetická konfigurace). `.env`, klíče, dumpy a zálohy nečetla.
Nezasáhla do R2 (skripty, nginx, Compose, `r2-*` dokumenty), `bootstrap.py`, `envfile.py`, aplikací ani schématu.

Legenda: **[S]** současné chování doložené čtením zdroje (v této relaci nespuštěno), **[N]** návrh změny, **[?]** neověřený předpoklad.
„Převzato" je výsledek dřívější relace, tady neopakovaný. Zkratky zdrojů (`BS:147` = soubor a řádek) jsou vysvětleny na konci.

## 1. Závěr v kostce

1. **[S]** `bootstrap.py` už pokrývá kroky 3–10 z tabulky v oddílu 2. Z existujícího `.env` zachová již nastavené neplaceholderové hodnoty, ale soubor může změnit: doplní klíče chybějící oproti šabloně a řádky s `CHANGE_ME` přepíše přímo (`set_env`, `EF:57-83`, `BS:238-262`). Kontrolu dat, svazků, jiných kontejnerů a většiny portů nemá.
2. **[S], závěr z kódu [?]:** na výchozím SHA bootstrap na čistém klonu nedoběhne. `DC:537` vyžaduje `EDU_AI_TUNNEL_SUBNET`, `EX` ji nezná a bootstrap ji nedoplní.
   Compose bez ní model nevykreslí (převzatý výsledek laboratoře R2, `R2L:66`). Bootstrap pak jen varuje a přeskočí kontrolu pg_search (`BS:195-198`) a selže na `up`. Nespuštěno.
3. **[S]** Klíč Langflow je v tomto návrhu zdrojem explicitně spravovaným v `.env` (`DC:140-141`). Zda a s jakou prioritou existuje záložní soubor v `langflow_data/`, je neověřeno (oddíl 4). Bootstrap nepozná, že byl klíč ztracen nebo změněn proti existujícím datům.
4. **[S]** `smoke.py` neověří vyhledávání. Search `/health` vrací statický JSON a `/v1/ask` smoke nevolá (oddíl 7). **[N]** Bez LLM i bez zápisu lze přes publikovaný `/v1/explain` ověřit identitu a router, ne čtení QA záznamů a jejich filtrů; to chce izolovanou syntetickou fixture (V3b).
5. **Implementováno offline (oddíl 10):** B06-1 je `scripts/install.py` s příkazy `preflight`, `plan`, `init-config`. Každý bere explicitní SHA a cílový adresář, nevolá Docker, SSH, síť ani databázi, kontroly hostitele odkládá (`HOST_NOT_CHECKED`) a úspěšný zápis je jen `CONFIG_PREPARED`, ne instalace (oddíl 8).

## 2. Kroky první instalace a co umí bootstrap

| # | Krok | Bootstrap dnes [S] | Mezera → návrh [N] |
|---|---|---|---|
| 0 | Hostitel: Linux x86_64, Docker Engine + Compose plugin, git, python3, odchozí internet | Jen `docker compose version` (`BS:147-152`). `edu_ai` má `platform: linux/amd64` (`DC:129`). Build stahuje z Docker Hub, GitHub releases (`DP:22-25`), apt a PyPI/npm; Bifrost bez internetu při startu skončí (`BS:601-603`) | Preflight: `uname -m` = x86_64 (jinak odmítnout), dosažitelný démon (`docker info`), místo na disku v Docker root a state-dir, dosažitelnost zdrojů. Minimální verzi Compose a práh disku určit měřením **[?]**; ověřená verze je Compose 5.3.1 (`PR:129,138`) |
| 1 | Kód na přesném SHA | Neřeší. `ON:22-25` jen `git clone`, URL je `<ORG>` | `git clone` + `git checkout --detach <SHA>`, ověřit `rev-parse HEAD` a čistý strom. SHA a URL zadá správce (Q1) |
| 2 | Preflight | `BS:144-217`: compose přítomen, porty 80/8443/8444 přes `connect_ex` na 127.0.0.1 (`BS:167-170`), pg_search v už existujícím image. Běží **před** načtením `.env` (`BS:587-591`) | Viz oddíl 6. Dnes se „běží už stack?" hledá v projektu odvozeném z názvu adresáře, ne z `COMPOSE_PROJECT_NAME` |
| 3 | `.env` | `BS:224-262`: kopie `EX` jen když chybí, doplní klíče chybějící oproti `EX`, vygeneruje 4 tajemství jen u `CHANGE_ME` (`BS:88-93`, `245-250`) | Soubor do externí konfigurace (oddíl 5). Navíc `COMPOSE_FILE`, `EDU_AI_TUNNEL_SUBNET`, `CHAT_LEGAL_ENTITY`, `QA_PORTAL_ALLOWED_ORIGINS` (v `EX` chybí) |
| 4 | Datové adresáře | `BS:55-61`, `269-313`: pět adresářů v checkoutu; vlastnictví přes kontejner `alpine:3` s mountem celého ROOT, jen na Linuxu (`BS:292`, `306-307`) | Data mimo checkout. Práva nastavit před bootstrapem: symlink míří mimo mount ROOT, takže `chown` v kontejneru by selhal |
| 5 | Build a start jádra | `BS:604-606`: `up -d --wait --wait-timeout 900 postgres bifrost edu_ai nginx`. Prázdný volume spustí `init_clean/00-03` (`DC:323`, `ON:176-188`) | Beze změny, ale až po pojistkách. Bez tokenu cloudflared se chování `--wait` neověřovalo **[?]**, proto vyjmenovat služby bez něj |
| 6 | Langflow API klíč | `BS:396-424`, `AK:121-162` (kandidát bootstrapu, oddíl 10.4): klíč v `.env` se nejdřív ověří a odpověď má tři stavy. **Platný**: beze změny. **Odmítnutý (jen 401) nebo žádný klíč**: výměna v pořadí výpis → vytvoření → ověření stejným požadavkem → atomický zápis; starý klíč se nemaže nikdy (maskovaná předpona ho spolehlivě neidentifikuje). **Neověřitelný** (**403**, timeout, 5xx, přesměrování, divná odpověď): nic se nezmění a kód 3. Dřív: **jakákoli chyba** = „nefunguje" → smazání starého `edu_ai-bootstrap` a vytvoření nového (`AK:63-75`, `93-102`) | Hotovo v kandidátu; skutečný Langflow 1.10.1 neověřen |
| 7 | Start zbytku | `BS:614`: `up -d --wait` všech služeb; `--force-recreate` jen po změně klíče nebo mountu | Beze změny |
| 8 | Langflow proměnné (Generic) | `BS:461-501` | Beze změny |
| 9 | Import 6 flows | `BS:508-518`, `IF:60-67`; idempotentní PATCH podle `endpoint_name` (`IF:124-127`) | Beze změny |
| 10 | Smoke | `BS:525-533` → `SM` (oddíl 7) | Doplnit o V0, V2–V5, V8 |
| 11 | Souhrn | Dřív vypsal hesla Langflow a pgAdmin na stdout (`BS:556-560`). Kandidát: cesta k `.env` a jména vytvořených hodnot, hodnoty jednorázově jen v interaktivním terminálu po úspěšném běhu (`step_summary`) | Nevypisovat tajnosti v logu ani v chybách; jednorázový přehled jen v interaktivním terminálu (K7), jinak jen cesta k souboru |
| 12 | Ručně po instalaci | OpenAI klíč do `.env` (`BS:256-262`, ale viz R-3). Dále provider v Bifrost UI, Cloudflare token a Access politika (`EX:131-142`), perimetr zvenku (`PC`), první škola (`A3A:46`) | Instalátor je vypíše jako **nedokončené**, nepředstírá hotovo |

Kroky 0–2 a kontroly hostitele v oddílu 6 patří do B06-2. B06-1 je nedělá ani nehlásí jako splněné.

Dále: čistá DB z `init_clean` **nemá registr migrací** (`schema_migration` v `init_clean` není; `B3E`). Instalace tak končí na B216 bez podporované cesty aktualizace.
Instalátor `scripts/migrate.py` nevolá (`AG`, „Never"); test hlídá, že ho neimportuje.

## 3. Konfigurace

Původ: **gen** = generuje instalátor jednou, **dodá** = správce, **konst** = konstanta ze šablony, **spočt** = spočítá se pro instalaci, **služba** = vytvoří běžící služba.

| Proměnná | Účel | Povinnost | Původ | Čte | Zdroj |
|---|---|---|---|---|---|
| `COMPOSE_PROJECT_NAME` | identita projektu, prefix volumes a sítí | povinná | konst `edu_ai` | Compose | `EX:17`, `DC` (pevné `container_name`) |
| `COMPOSE_FILE` | které soubory se načtou; bez něj se sám přibere override a publikuje dalších 6 portů | povinná na serveru | spočt `docker-compose.yml` (v `EX` zakomentováno) | Compose | `EX:22`, `DO:24-46`, `A4A:232` |
| `EDU_AI_TUNNEL_SUBNET` | podsíť sítě `tunnel` a rozsah důvěry nginx pro `CF-Connecting-IP` | povinná na SHA s kandidátem R2; **v `EX` chybí** | spočt `tunnel_subnet.py allocate` (jen čtení, exit 0/2/3) | Compose → nginx, cloudflared | `DC:537,545`, `TS:11-13,222-252` |
| `POSTGRES_DB_SERVER`, `_SERVER_PORT`, `POSTGRES_DB`, `_DB_SCHEMA`, `_DB_USER` | konektivita a identita DB | povinné | konst | postgres, edu_ai (flows), search (`PG_*`), skripty | `EX:48-63`, `DC:152,311-316,394-399` |
| `POSTGRES_PASSWORD` | heslo jediné DB role | povinná, tajná | gen | postgres (jen první start prázdného volume), edu_ai (DB URL a Global Variable), search | `EX:68`, `DC:152,312-316,397` |
| `PGADMIN_DEFAULT_EMAIL` | login pgAdmin | povinná; výchozí `admin@example.com` | dodá (bootstrap se neptá) | pgadmin | `EX:71`, `DC:343` |
| `PGADMIN_DEFAULT_PASSWORD` | heslo pgAdmin | povinná, tajná | gen | pgadmin | `BS:90` |
| `LANGFLOW_SUPERUSER`, `_PASSWORD` | superuser Langflow, vznikne při prvním startu | povinné | jméno konst `admin`, heslo gen | edu_ai, `AK` (login) | `EX:78-79`, `DC:157-158` |
| `LANGFLOW_SECRET_KEY` | Fernet klíč, šifruje Global Variables | povinná, tajná, **nikdy neměnit** | gen (`BS:73-80`) | edu_ai jen přes `env_file` | oddíl 4, `DC:185-190` |
| `LANGFLOW_API_KEY` | REST klíč Langflow | povinná po kroku 6 | služba (Langflow ho vydá), zapíše skript | api, edu_ai (extrakční flows), skripty | `AK`, `DC:93` |
| `LANGFLOW_URL` | adresa Langflow pro skripty na hostu | volitelná; bez override `http://127.0.0.1:8444` | dodá / instalátor | skripty, api; přes `env_file` i Langflow | `EX:94`, `BS:365-393`, `A4A:237` |
| `LANGFLOW_PIPELINE_INPUT_NODE_{MARKDOWN,WEB,PDF,OFFICE,QA}`, `LANGFLOW_MARKDOWN_LOGGER_NODES` | ID uzlů ve flows | povinné | konst | api (WEB/PDF/OFFICE/QA), edu_ai (MARKDOWN, logger) | `EX:170-178`, `DC:104-107` |
| `OPENAI_API_KEY` | klíč pro SDK; skutečný klíč poskytovatele drží Bifrost | povinná (neprázdná) | dodá, nebo formální hodnota (Q4) | edu_ai (Global Variable), search | `EX:111`, `DC:182-183,379` |
| `OPENAI_BASE_URL` | směr na Bifrost | volitelná | default v Compose | edu_ai, search | `EX:117`, `DC:184,387` |
| klíč poskytovatele LLM | skutečné LLM a embedding volání | povinný pro LLM cesty | dodá v **UI Bifrostu**, ne v `.env` | bifrost (`bifrost_data/config.db`) | `EX:160-161` |
| `BIFROST_BIND`, `_PORT`, `_LOG_LEVEL` | publikace UI brány (bez autentizace) | volitelné; výchozí 127.0.0.1:8099 | konst / dodá | bifrost | `EX:127-129`, `DC:234-238` |
| `CLOUDFLARE_TUNNEL_TOKEN` | tunel | volitelná; prázdná = cloudflared se spustí a jen loguje chybu | dodá | cloudflared; přes `env_file` i Langflow | `EX:131-142`, `DC:267`, `A4A:229` |
| `CHAT_LEGAL_ENTITY` | škola webchatu; bez ní chat dostane 422 | povinná pro chat; **v `EX` chybí** | dodá | web_chat | `DC:436-440`, `WC:40` |
| `CHAT_ENTITY_PICKER`, `CHAT_COOKIE_SECURE` | volba školy (jen test); Secure cookie | volitelné; picker v `EX` chybí | konst `false` | web_chat | `DC:444-448`, `EX:103` |
| `QA_PORTAL_ALLOWED_ORIGINS` | allow-list kontroly Origin v QA portálu. Prázdná = kontrola Origin se **přeskočí** (`QS:34-35`) | povinná pro bezpečný provoz portálu; **v `EX` chybí** | dodá (adresa, kterou vidí prohlížeč) | qa_admin | `DC:487`, R-4 |
| `CORS_ORIGINS` | prázdná = zavřeno | volitelná | konst prázdná | api, search | `EX:159`, `DC:112,409` |
| `JINA_API_KEY` | reranker `jina` | volitelná; v `EX` chybí | dodá | search | `DC:382` |
| `LANGGRAPH_MODULE` | nemá čtenáře v Compose | zbytková | konst | — | `EX:164`, `A4A:237` |

## 4. Tajné hodnoty

| Třída | Hodnoty | Pravidlo při opakovaném spuštění |
|---|---|---|
| **Generované jednou** | `POSTGRES_PASSWORD`, `PGADMIN_DEFAULT_PASSWORD`, `LANGFLOW_SUPERUSER_PASSWORD`, `LANGFLOW_SECRET_KEY` | **[S]** generuje se všude, kde je `CHANGE_ME` (`BS:245-250`), bez ohledu na existující data. **[N]** jen v `init-config` do dosud neexistujícího adresáře; podruhé nikdy |
| **Vydá služba** | `LANGFLOW_API_KEY` | rotace jen po 401 (ne po 403 ani po jakékoli jiné chybě) a nepřímo: nový klíč se vytvoří, ověří a uloží; starý klíč se nemaže nikdy (kandidát bootstrapu, oddíl 10.4) |
| **Dodané správcem** | provider klíč v Bifrostu, `OPENAI_API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`, `JINA_API_KEY` | **[N]** nikdy se negenerují ani nepřepisují; instalátor ověří jen „neprázdné, není `CHANGE_ME`" a hodnotu nevypíše |
| **Zachované** | vše výše, dále `langflow_data/`, `bifrost_data/config.db`, volume `postgres_data` | pojistky G1, G2 |

### Trvalost šifrovacího klíče Langflow

**[S]** Klíč se do Langflow dostává z `.env` přes `env_file` (`DC:140-141`); tento zdroj návrh spravuje. `langflow_data/` je připojený jako konfigurační adresář Langflow a audit ho označuje za „záložní zdroj" klíče (`DL:36`, `A2A:23,27`), ale jeho existenci ani prioritu vůči `.env` tato práce neověřila, proto je netvrdím jako fakt.
Klíč šifruje Global Variables v DB `langflow`; po změně klíče jsou existující proměnné nedešifrovatelné a musí se smazat a vytvořit znovu (`DM:728-733`, `DC:185-190`).
Rizika dnes: (a) `.env` leží v checkoutu, `.gitignore` ho jen ignoruje a `git clean -fdx` ho smaže (`GI:2-10`, `A4A:234`); (b) při chybějícím `.env` vznikne nový klíč, i když data existují (`BS:245-250`, žádná kontrola);
(c) zápis `.env` není atomický (`EF:83` `write_text`), přerušení může soubor useknout; (d) záloha DB bez klíče nestačí k obnově proměnných, DB a klíč pohromadě naopak stačí k dešifrování, proto se uchovávají odděleně.

**[N]** Pravidla (v B06-1 implementovaná a testovaná, oddíl 8):

- **K1** Klíč vznikne právě jednou v `init-config` (stejný tvar jako `_fernet_key` v `BS:73-80`) a jen do nově vytvořené sady. Žádný příkaz ho později nepřepíše ani nevygeneruje znovu. Není `--force`, rotace ani `reveal-key`; test kontroluje seznam voleb.
- **K2** `install-record.json` nese **plný SHA-256** klíče (64 hex). `preflight` a `plan` nad existujícím cílem hlásí `EXISTING_KEY_FINGERPRINT: MATCH`, `MISMATCH` nebo `UNVERIFIABLE` bez hodnoty a cíl vždy odmítnou (exit 2).
- **K3** Sada se zveřejní celá nebo vůbec: privátní staging adresář ve stejném rodiči, `fsync`, kontrola přečtením zpět a **jediné** `renameat2(RENAME_NOREPLACE)` stagingu na cíl (`ctypes`, bez záložní cesty). Cíl, který existuje jakkoli (prázdný adresář, soubor, symlink i visící symlink), jádro nenahradí a vrátí `EEXIST`. Cíl se předem nerezervuje, takže není co zaměnit ani uklízet: cizí adresář na cílové cestě se nikdy nepřepíše ani nesmaže. Staging adresář vznikne pod umaskem 0077, hned se otevře přes deskriptor (`O_DIRECTORY|O_NOFOLLOW`) a přijme se jen prázdný adresář téhož uživatele s módem přesně 0700 (jméno se otevírá, ne inode, který vytvořil `mkdtemp`; cokoli jiného se nepoužije, `fchmod` se nevolá); oba soubory se zapisují, ověřují i mažou přes tento deskriptor, ne podle jména. Těsně před publikací se jméno stagingu porovná s inodem, který skript vytvořil: cokoli jiného pod jménem je `STAGING_REPLACED`, nic se nezveřejní a nic se nemaže. Po `rename` se ověří, že cíl je náš inode a obsah čtený přes deskriptor je celá sada. Mezera mezi kontrolou jména a voláním `renameat2` se zavřít nedá (jádro bere jména): cizí objekt, který se v ní objeví na jménu stagingu, se přesune na cíl (nic se nesmaže ani nepřepíše), a to se zjistí až po faktu (K4). Identita rodiče (`st_dev`, `st_ino`) i jeho přeložená cesta se ověří před vytvořením stagingu a po zveřejnění; změna je `TARGET_PARENT_CHANGED` a hodnoty se nezobrazí. Záměně rodiče se tím nezabrání, jen se přizná. Cíl je tak nepřítomný, nebo úplný, nikdy poloviční (test sleduje cíl po dobu zápisu). Kde `RENAME_NOREPLACE` není (jiný OS, souborový systém bez podpory) je výsledek `UNVERIFIED PUBLISH_NOREPLACE_UNSUPPORTED` a nic se nezveřejní; prostý `rename` se nepoužije, protože by nahradil prázdný adresář.
- **K4** Přerušení a chyby: výsledek určuje disk, ne proměnná. Po každém pokusu o `rename`, po výjimce jakéhokoli druhu (`except Exception`), I/O chybě nebo signálu (SIGINT, SIGTERM) skript porovná identitu (`st_dev`, `st_ino`) našeho staging adresáře s cílem a se jménem stagingu. Naše sada je na cíli: po ověření obou souborů a otisku klíče, čtených přes deskriptor, `CONFIG_PREPARED` s `INTERRUPTED_AFTER_PUBLICATION` (signál) nebo `ERROR_AFTER_PUBLICATION` (jiná výjimka); selhání `fsync` rodiče je `DURABILITY_UNCONFIRMED`. Naše sada zůstala ve stagingu: nic není zveřejněno a uklidí se jen vlastní práce: skript drží deskriptor každého souboru, který vytvořil, a **obsah zničí přes něj** (`ftruncate`), takže žádný krok podle jména nemůže zasáhnout cizí položku. **Žádné jméno se nemaže**, ani soubor, ani adresář (`unlink` a `rmdir` podle jména nejdou zabezpečit proti objektu, který mezitím převzal jméno; zákaz hlídá test nad AST). Staging se dvěma soubory nulové délky zůstane a ohlásí se jako `STAGING_LEFT_EMPTIED` s plnou cestou, počtem vyprázdněných souborů a počtem cizích položek (příští běh ho vypíše jako `STALE_STAGING`, operátor ho po prohlídce odstraní sám); když se `ftruncate` nepodaří, je to `STAGING_LEFT_NOT_EMPTIED` s výslovným varováním, že adresář může držet tajemství. Cokoli jiného (naše sada není na cíli ani pod jménem stagingu, nebo cíl po `rename` není náš): `UNVERIFIED PUBLICATION_STATE_UNKNOWN` s cestou, kde naše sada teď leží (z Linuxu `/proc`, jinak „neznámé místo“); nic se nemaže ani nemění. Obsluha SIGTERM a SIGHUP (zavřený terminál, SSH) zůstává až do konce úklidu; přerušení při zjišťování výsledku nebo při úklidu je `INTERRUPTED_WHILE_FINISHING` (staging může zůstat; stačí už první signál, protože obsluha vyhazuje výjimku i tam). Po SIGKILL před `rename` zůstane v rodiči jen staging s nezveřejněnými tajnostmi (příští běh ho ohlásí jako `STALE_STAGING`, nesmaže ani nepoužije); po SIGKILL těsně po `rename` je na cíli úplná sada a příští běh ji odmítne jako existující (`TARGET_EXISTS_CONFIG_SET`, otisk `MATCH`). Po zveřejnění: zavřená rouře na stdout výsledek nezmění (exit `0`, `2` nebo `3`, `emit` je ošetřen a `flush` leží uvnitř ošetření); SIGTERM a SIGINT po návratu z `init_config`, při tisku, výsledek také nezmění, sada je na disku a hodnoty v `.env`, jen hlášení chybí.
- **K5** Souběh: kdo první dokončí `RENAME_NOREPLACE`, publikuje. Ostatní dostanou `EEXIST`, skončí exit 2 (`TARGET_EXISTS_*`), uklidí svůj staging a jejich tajnosti se zahodí. Test: pozastavený první proces prohraje s druhým; šest souběžných procesů dá právě jednu sadu a žádné zbytky.
- **K6** Existující cíl jakéhokoli druhu (soubor, prázdný, cizí, částečný i úplný adresář, symlink, visící symlink) se nepřepíše, neotevře k zápisu a nic se z něj nepřegeneruje. Částečná sada se nikdy nepovažuje za použitelnou a její klíč se nepřegeneruje.
- **K7** Výpis tajností (rozhodnutí vlastníka 2026-10-06, nahrazuje dřívější „nikdy žádný výpis“): chráněný `.env` zůstává trvalým zdrojem. Čtyři hodnoty, které vytvořil právě tento běh `init-config`, se jednou vypíšou v **závěrečném přehledu úspěšného prvního běhu**, a to jen v interaktivním terminálu: stdout i stdin jsou tty a není nastavené prostředí CI (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL` a další, hodnota `0`, `false`, `no` nebo prázdná CI nezakládá). Blok `GENERATED_SECRETS:` a čtyři řádky `NÁZEV=hodnota` stojí těsně před řádky `HOST_NOT_CHECKED` a `INSTALLATION_NOT_STARTED`, takže každý výsledek dál končí těmito dvěma řádky. V CI, při přesměrovaném výstupu nebo bez tty na stdin se vypíše jen cesta (`CONFIG_FILE`) a řádek `GENERATED_SECRETS_NOT_SHOWN`. Nikdy se nevypíše: existující hodnota při opakovaném běhu (ten cíl odmítne, K6), dodaný externí token nebo provider klíč, `LANGFLOW_API_KEY`, hodnota z neúspěšného, odmítnutého nebo nejasného běhu ani z běhu přerušeného či skončeného chybou po zveřejnění (sada je tehdy na disku a hodnoty se čtou ze souboru). Hodnoty žijí odděleně od řádků výstupu (`Report.reveal`), vypíše je jen `emit()` a jen jednou; chybové zprávy, průběžné řádky, `preflight` ani `plan` je obsahovat nesmějí. Beze změny zůstává: otisk klíče v `install-record.json` (bez hodnoty), žádná volba `--force`, rotace ani `reveal-key`. Uživatel si hodnoty musí uložit do svého trezoru v tu chvíli; skutečná úschova kopie klíče je podmínkou pozdějšího provozního startu (Q6), ne důvodem blokovat offline etapu. Terminál, který zachytává výstup (záznam relace, scrollback sdílené obrazovky), hodnoty zachytí také; skript to nepozná.
- **K8** Po instalaci test trvalosti V8: znovu vytvořit `edu_ai` a opakovat probe flow, který dešifruje Credential proměnnou (pozdější etapa).
- **K9** Čtení z Gitu: dřív než se přečte jakýkoli objekt, čte se jen konfigurace a promisor/partial clone se odmítne (`PARTIAL_CLONE_UNSUPPORTED`): jakýkoli výskyt klíče `extensions.partialclone` (i s prázdnou či bílou hodnotou) nebo `remote.*.promisor` či `remote.*.partialclonefilter`, bez ohledu na hodnotu (konzervativně i `false`), protože hledání chybějícího objektu může spustit lazy fetch a Git bere i samotný `partialclonefilter` nebo prázdný název rozšíření jako promisor. Každý git proces dostává `GIT_NO_REPLACE_OBJECTS=1`, `GIT_NO_LAZY_FETCH=1` a zákaz všech vestavěných transportů: `-c protocol.allow=never` a navíc `-c protocol.<p>.allow=never` pro `file`, `git`, `ssh`, `http`, `https`, `ftp`, `ftps`, `ext` a `fd`. Nastavení jednoho protokolu (v jakékoli úrovni konfigurace, i přes `GIT_CONFIG_COUNT` a `GIT_CONFIG_PARAMETERS`) a `GIT_ALLOW_PROTOCOL` mají přednost před `protocol.allow`, proto se druhá proměnná z prostředí podprocesu odstraňuje a každý protokol se zakazuje zvlášť. Přehlédnutý promisor tak nemůže použít žádný vestavěný transport. Vlastní remote helper (`foo::`) s `protocol.foo.allow=always` v uživatelské konfiguraci tím zakázán není; na Gitu ≥ 2.44 je kvůli `GIT_NO_LAZY_FETCH` nedosažitelný a na starším Gitu před ním chrání jen odmítnutí promisor repozitáře (revize B ukázala, že díra detektoru, `partialclonefilter`, helper na emulaci Gitu < 2.44 spouštěla; je uzavřena). Obsah je k úplnému SHA vázán **přepočtem**, ne důvěrou v object store: commit, jeho kořenový strom a oba soubory se čtou jako surové objekty a každý musí mít SHA-1 shodný se svým jménem (podvržený loose objekt, objekt v `info/alternates` nebo náhrada, kterou by Git použil, se tak odhalí i bez `GIT_NO_REPLACE_OBJECTS`); oba soubory musí být v kořenovém stromu běžné soubory (`100644` nebo `100755`), symlink či gitlink se odmítne (`SOURCE_FILE_NOT_A_REGULAR_FILE`); nesoulad je `SOURCE_OBJECTS_FAILED_VERIFICATION` (exit 3). Povolených tvarů příkazu `git` je sedm: `rev-parse --verify --quiet`, `rev-parse --show-toplevel`, `cat-file blob`, `cat-file commit`, `cat-file tree`, `config --get` a `config --get-regexp`.

**[?]** Neověřeno: (i) který zdroj vyhraje, když existuje klíč v `.env` i soubor v `langflow_data/` (`A2A:27` tvrdí, že `.env`); (ii) zda Langflow 1.10.1 při nastaveném env soubor vůbec vytváří;
(iii) co klíč šifruje kromě Global Variables. Test později v laboratoři: čistý Langflow s klíčem v env, zjistit jen **existenci** souboru (obsah nečíst) a dešifrování po re-create.

## 5. Externí uložení konfigurace a provozních dat

```
/opt/edu_ai/                     checkout detached na zadaném SHA (vydání, zaměnitelné)
  .env -> /srv/edu_ai/config/.env                       symlink
  uploads  langflow_data  bifrost_data  sqllite_export  -> /srv/edu_ai/data/<název>   symlinky
/srv/edu_ai/config/              sada z init-config (příklad hodnoty --target-dir; 0700): `.env` a `install-record.json`, obojí 0600
/srv/edu_ai/data/<název>/        vlastník uid:gid podle BS:55-61
Docker volumes projektu edu_ai:  postgres_data, pgadmin_data, reranker_models, qa_admin_state   beze změny (DC:550-554)
```

- **[S]** Dnes vše leží v checkoutu (`BS:55-61`, `DC:113,191-194`) a jen ignorováno gitem (`GI:12-19,66`). `CI:221-224` doporučuje data mimo release.
- **[N]** `--target-dir` je přímo adresář sady. `init-config` ho vytvoří, rodič musí existovat, nesmí být zapisovatelný ostatním (kromě sticky bitu) a cíl nesmí ležet v repozitáři (rodič se rozřeší přes symlinky).
- **[N]** Bez změny Compose to jde přes symlinky: Compose čte `.env` z adresáře projektu a bind zdroje jsou relativní k němu. `env_file: .env` je v Compose pevně (`DC:140-141`) **[?]**,
  `--env-file` ho nenahradí. Symlink jako zdroj bind mountu musí laboratoř potvrdit **[?]**. `pgadmin_exports/` zůstává v checkoutu: je v něm sledovaný `.gitkeep` (`GI:85-86`), symlink by Git vykázal jako změnu a obsahuje jen exporty.
- **[N]** Čisté řešení (proměnná kořene dat a env souboru v Compose) je neschválený návrh B04a S5 (`A4A:293-309`) a sahá na Compose, který právě mění R2. Do B06-A nepatří (Q2).

| Data | Dnes → návrh | Ztráta znamená |
|---|---|---|
| `.env` | checkout → `/srv/edu_ai/config` | ztráta hesel a klíče: DB nepřístupná bez ručního zásahu, Langflow proměnné nedešifrovatelné |
| DB `edu_ai` a `langflow` (jeden cluster) | volume `postgres_data` v Docker root, beze změny | ztráta dokumentů, QA, historie dotazů, flows, uživatelů a klíčů Langflow |
| `langflow_data/` | → data | záložní zdroj klíče, cache |
| `bifrost_data/` | → data | provider klíče v `config.db` bez šifrování, proto 0750 (`BS:51-54`) |
| `uploads/` | → data | nahrané zdroje pro reingest |
| `sqllite_export/` | → data | výstup QA pack builderu, regenerovatelný **[?]** |
| `pgadmin_data`, `qa_admin_state`, `reranker_models` | volumes beze změny | účty pgAdmin; krátkodobý stav portálu (`DC:495-498`); cache modelu |

Docker root: **[N]** preflight vypíše `DockerRootDir` a volné místo. Přesun je rozhodnutí hostitele (`daemon.json`), ne instalátoru.
Zálohy: jednorázová záloha před S2 je hotová (záznam). Odložena je automatická politika a zkouška obnovy (D01–D04 neschváleny). Návrh jen určuje, že budoucí záloha musí pokrýt řádky tabulky a klíč držet odděleně od DB.

## 6. Pojistky

Pravidlo pro všechna čtení: co nelze přečíst nebo rozparsovat, je **odmítnutí**, ne „volno" (exit 3), stejně jako u `TS:11-13`.

**Rozsah B06-1:** soubory a cílový adresář (G2, G8, odmítnutí existujícího cíle) a žádný podproces mimo lokální `git`. Kontroly hostitele (G1, G3, G4, G5, G9) zůstávají povinné před pozdějším startem; B06-1 je nespouští a nehlásí je jako splněné, výsledek vždy nese `HOST_NOT_CHECKED`. Připravená konfigurace se nesmí připojit k existujícím datům, dokud je B06-2 neprojde.

| # | Riziko | Dnes [S] | Pojistka [N] |
|---|---|---|---|
| G1 | přepsání existujících dat | Žádná kontrola svazků ani dat (`BS:144-217`). Init běží jen na prázdném volume, jinak se tiše přeskočí a po neúspěšném prvním startu zůstane napůl inicializované schéma (`ON:335`) | **B06-2, host:** preflight odmítne: existuje volume, síť, kontejner nebo image tag instalace; neprázdný state-dir nebo `langflow_data`; `.env` kdekoli; neinicializovatelný stav |
| G2 | přepsání hesel a klíčů | `.env` se nepřepisuje, ale ztracený `.env` při existujícím volume dá nová hesla (Postgres hodnotu u existujícího volume ignoruje, `DC:312-314`) a nový klíč (viz oddíl 4) | **B06-1:** K1–K7, plný SHA-256 klíče v `install-record.json`, zveřejnění celé sady najednou |
| G3 | zásah do jiné instalace | `container_name` pevné u všech 10 služeb (`DC:27,81,130,226,263,303,339,368,430,471`), mutable tagy `edu_ai_api:1.0`, `edu_ai:1.1.0`, `edu_ai_postgres:cs`, `edu_ai_search:1.0`, `edu_ai_web_chat:0.1`, `edu_ai_qa_admin:0.1`; druhá instalace na hostiteli nejde (`A4A:233`). Stejný `COMPOSE_PROJECT_NAME` míří na cizí projekt. Preflight běží před `.env` (`BS:587-591`) | **B06-2, host:** jedna instalace na hostitele, projekt `edu_ai`. Projekt číst z konfigurace před kontrolou. Jména a tagy brát z **vykresleného modelu** (`docker compose config --format json`), ne tvrdě. Každý příkaz Compose s `-p`, `--project-directory` a explicitním `--env-file`. Cizí kontejnery jen vypsat |
| G4 | kolize portů | Kontrola jen 80/8443/8444 spojením na 127.0.0.1 (`BS:167-170`): listener na jiné adrese ani Bifrost 8099 (`DC:234`) nevidí. Nepřipnutý `COMPOSE_FILE` přibere override a publikuje 5432, 7860, 8080, 8081, 8090, 5050 (`DO:24-46`) | **B06-2, host:** `ss -H -ltn` (v4 i v6, všechny adresy). Porty odvodit z vykresleného modelu. Zapsat `COMPOSE_FILE`. Nečitelné `ss` = exit 3 |
| G5 | kolize sítí | Pět sítí volí Docker z poolů, `tunnel` má zadaný CIDR. Kontrolu `tunnel` umí `TS` (jen čtení, „nezjistitelné není volné", `TS:11-13`) | **B06-2, host:** model vyžaduje `EDU_AI_TUNNEL_SUBNET` → `tunnel_subnet.py allocate --project edu_ai --reserved <LAN/VPN správce>`, hodnotu do konfigurace, před každým `up` `check`. U ostatních sítí vypsat pooly a trasy hostitele a odmítnout kolizi s `--reserved` **[?]** (chování Dockeru při kolizi nikdo neměřil) |
| G6 | veřejná expozice | nginx publikuje 80/8443/8444 na všech rozhraních (`DC:28-31`); `/api/` zapisuje bez přihlášení (`NX:168-174`); perimetr drží firewall a Cloudflare mimo repo (`ON:121-148`) | Instalátor o perimetru nic netvrdí. Výsledek nese `PERIMETR NEOVĚŘEN`, dokud správce nedoloží `perimeter_check.py --strict --ip` zvenku. Skript má výchozí hostname produkce (`PC:55`), nutné `--host`. Bind proměnné v Compose = Q7 |
| G7 | destruktivní příkazy | `ON:337` doporučuje `down -v`; `CI:226-228` ho zakazuje při deployi | **B06-1:** nevolá Docker, SSH, síť ani databázi; jediný podproces je lokální `git` se sedmi povolenými čtecími tvary. Později whitelist sloves Dockeru, v B06-A nikdy `down`, `rm`, `volume rm`, `network rm`, `prune`, `-v`, `--remove-orphans` |
| G8 | přerušený nebo nejasný stav | `set_env` neatomický (`EF:83`) a `key_works` bral chybu jako neplatnost (`AK:63-75`); obojí opravuje kandidát bootstrapu (oddíl 10.4) | **B06-1:** zveřejnění celé sady najednou, nejasné = exit 3. Zařazení rotace API klíče do instalátoru patří do B06-2 |
| G9 | architektura | `edu_ai` je vynucen `linux/amd64` (`DC:129`) | **B06-2, host:** `uname -m` ≠ x86_64 → odmítnout |
| G10 | migrační runner | `AG` zakazuje na školní instalaci | Instalátor ho nevolá a výslovně hlásí „bez podporované aktualizace" |

## 7. Ověření funkční instalace bez placeného LLM

Samotné `up --wait` není důkaz (`PR:50`). Níže je, co `smoke.py` dnes ověřuje a co přibývá. Přeskočená kontrola není splněná (`SM:23-24`).

| ID | Co se ověřuje | Jak | LLM | Zápis | Stav |
|---|---|---|---|---|---|
| V0 | stav a identita | `compose ps`: očekávaná množina služeb, `running`/`healthy`, RestartCount 0, štítek projektu; image ID do `install-record.json` | ne | ne | **[S]** část `SM:98-126`; zbytek **[N]** |
| V1 | služby odpovídají | `SM:149-177` (api s `missing_config`, search, chat) + nginx `/health` (`NX:163-166`). Search `/health` je statický (`SV:350-352,803-805`) a `missing_config` má jen API (`API:158-198`), takže pro search **nic neprokazuje** | ne | ne | **[S]** |
| V2 | seed kompletní | read-only SQL: `qa_stopword` bez sloupce `form` a s PK `(lang,term,scope)`; `search_answer_generation` v15 aktivní a žádný klíč se dvěma aktivními; počty řádků seed tabulek rovné počtu `INSERT` v `02` (generátor používá `--column-inserts`, `B216.json`) plus řádky `03`; obsahové tabulky 0. **Ne přes runner** | ne | ne | **[N]**, opřeno o `B3C:29-37` (izolovaná DB) |
| V3 | identita a router | `POST /search/v1/explain` přes nginx (cesta je publikovaná, `NX:68-91`): identitní otázka pro entitu **vybranou z DB**, text z `qa_blocklist` (kategorie identity), ne tvrdě. Očekávat 200, `resolved`, `answer_source=meta`, `answer` obsahující `display_name`. `explain` končí po `qa_lexical` (`SV:1841-1897`), embedding je až v `qa_vector`/`rag_vector`; route guard `RT:432-471`. **Neprokazuje** čtení QA záznamů ani jejich filtry entity, visibility, role a aktivity: guard skončí dřív | ne | ne | **[N]**; `/ask` doloženo `B3C:60-78`, `/explain` **[?]** |
| V3b | QA vyhledávání a scope | izolovaná syntetická QA fixture (ne na školní DB): pozitivní `qa_exact` nebo `qa_lexical` a negativní případy jiné entity, role/visibility a neaktivního záznamu; zdroj odpovědi z veřejného deterministického prefixu. Bez změny cascade, thresholdů, normalizace a scope filtrů | ne | jen izolovaná DB | **[N]**, pozdější etapa |
| V4 | zápis query logu | jedno `POST /search/v1/ask` s unikátním `session_id`; řádek v `question_answer_log` s entitou a odpovědí. Důvod: `log_search` chybu spolkne (`A3A:40`) | ne (guard, `B3C:74-78`) | **1 řádek** | **[N]**; zachovat × smazat = Q5 |
| V5 | hrana nginx | přes nginx `/search/v1/query-log` a `/search/v1/staff-users` → 404 s obálkou; publikované cesty 200 (`ON:150-154`, `NX:68-91`) | ne | ne | **[N]** |
| V6 | Langflow | login superusera, platný API klíč, 6 aliasů, typy Global Variables, probe flow (dešifrování Credential a spojení do Postgres) | ne | ne | **[S]** `AK`, `SM:184-215,392-494` |
| V7 | ingest brána | dry-run `/v1/ingest` jako aktivní `staff_user` ze seedu | ne | ne | **[S]** `SM:303-359` (bez uživatele jen skip) |
| V8 | trvalost | `up -d --force-recreate edu_ai`, znovu V6; `stop` + `up -d --wait`, znovu V2–V3. Druhé spuštění instalátoru je no-op: hash externího `.env`, ID kontejnerů a seznam volumes shodné. Bez `down`, bez `-v` | ne | ne | **[N]** |
| V9 | Bifrost | kontejner healthy (`DC:245-250`); platnost provider klíče se **neověřuje** | ne | ne | **[S]** živost |

**Bez placeného volání se neprokáže:** platný provider klíč v Bifrostu, embedding (`qa_vector`, `rag_vector`), generování odpovědi, ingest dokumentů a QA přes Langflow (embedding), cloud reranker. QA retrieval se bez V3b také neprokáže.
To ověří jedno vědomě schválené placené volání mimo automatickou akceptaci. Výsledek instalace je do té doby `FUNKČNÍ BEZ LLM`, ne `HOTOVO`.

## 8. B06-1: offline příprava externí konfigurace (implementováno)

Nový [scripts/install.py](../../scripts/install.py), jen standardní knihovna. Každý příkaz bere povinně `--source-sha` (celé 40 hex malými písmeny, žádný `HEAD`, zkratka ani výchozí hodnota) a `--target-dir` (absolutní cesta bez `..`) a volitelně `--repo` (výchozí je checkout skriptu). Z repozitáře se čtou jen objekty zadaného commitu, nikdy pracovní strom ani `.env`.

| Příkaz | Co dělá | Zápis |
|---|---|---|
| `preflight` | ověří commit, přečte z něj `docker-compose.yml` a `.env.example`, staticky je prohlédne a zkontroluje cíl | žádný |
| `plan` | `preflight` + výpis: co vznikne, čtyři generované názvy, `MISSING_MANUAL`, `SERVICE_ISSUED_LATER`, `UNRESOLVED_REQUIRED`, `OPTIONAL_NOT_IN_TEMPLATE`, `NOT_EVALUATED` | žádný |
| `init-config` | `preflight` + vznik nové chráněné sady | jen nový `--target-dir` s `.env` a `install-record.json` (0700, soubory 0600) |

**Výstup.** Každý výsledek končí řádky `HOST_NOT_CHECKED` a `INSTALLATION_NOT_STARTED`. Úspěšný zápis vypíše `CONFIG_PREPARED`, cestu k chráněnému souboru a plný SHA-256 klíče Langflow; hodnoty tajností vypíše jen jednorázový závěrečný přehled v interaktivním terminálu podle K7, jinak žádná. Stav „READY" neexistuje: sada s chybějícími vstupy je `CONFIG_PREPARED` a v záznamu má `ready_for_install: false` s `blockers` (vždy `HOST_NOT_CHECKED`, dále `MISSING_MANUAL:<název>` a `UNRESOLVED_REQUIRED:<název>`). Návratové kódy: 0, 2 odmítnuto, 3 nelze ověřit nebo dokončit (konvence `TS:11-13`).

**Vstupy.** Generují se jen čtyři tajemství, která už zavádí bootstrap (`BS:88-93`); `LANGFLOW_API_KEY` vydá Langflow později. Ručně dodávané hodnoty (zbylé `CHANGE_ME_*`, dnes `OPENAI_API_KEY`) se hlásí názvem. Povinná proměnná Compose, kterou šablona nedává (dnes `EDU_AI_TUNNEL_SUBNET`), se hlásí jako `UNRESOLVED_REQUIRED`; podsíť se bez inventáře hostitele nevymýšlí a do `.env` se nezapisuje. Nepřibývá žádná aplikační proměnná ani settings flag: `.env` je šablona z commitu s přepsanými čtyřmi řádky.

**Statický sken Compose** zná `$VAR`, `${VAR}`, `:-`, `-`, `:?`, `?`, `:+`, `+` a `$$`, komentáře přeskakuje, text blokových skalárů ne. Vnořené, neukončené a vícerádkové konstrukce s uvozovkami, `include:` a `extends:` hlásí `UNVERIFIED` (exit 3), nehádá. Nalezené ani chybějící povinné proměnné neznamenají úplnost: model Compose se nevykresluje a `INPUT_GAPS` je jen statický počet. Soubory mimo zadaný commit, override a `env_file` se nečtou.

**Co B06-1 nedělá:** nevolá Docker, SSH, síť ani databázi; nekontroluje porty, sítě, svazky, kontejnery ani image; nespouští ani nemění bootstrap; nevytváří API ani provider klíče; nezakládá školy; neupravuje Compose, nginx, R2, aplikace ani schéma. `init-config` vyžaduje Linux (`renameat2`) a jinde odmítá (`UNSUPPORTED_PLATFORM`); `preflight` a `plan` běží všude, kde je `git`.

**Ověřitelné podmínky a testy** ([test_install_preflight.py](../../tests/test_install_preflight.py), [test_install_config.py](../../tests/test_install_config.py), [test_install_findings.py](../../tests/test_install_findings.py) pro nálezy R1–R4 z prvního kola, [test_install_publication_regression.py](../../tests/test_install_publication_regression.py) (R1 a R4) a [test_install_git_regression.py](../../tests/test_install_git_regression.py) (R2 a R3) z druhého kola, [test_install_reveal.py](../../tests/test_install_reveal.py) (jednorázový přehled tajností, K7) a z nezávislých revizí [test_install_cleanup_safety.py](../../tests/test_install_cleanup_safety.py), [test_install_git_binding.py](../../tests/test_install_git_binding.py), [test_install_parent.py](../../tests/test_install_parent.py) a [test_install_closeout.py](../../tests/test_install_closeout.py), pomocné [install_fixtures.py](../../tests/install_fixtures.py) a [install_child.py](../../tests/install_child.py); jen syntetická data a dočasné adresáře):

1. **Explicitní vstupy:** chybějící `--source-sha` nebo `--target-dir` je chyba; `HEAD`, větev, zkratka, velká písmena, 39 a 41 znaků, 64 hex, mezera a nový řádek se odmítnou dřív, než se zavolá `git`; cíl musí být absolutní, bez `..`, ne kořen; v seznamu voleb není `--force`, rotace ani `reveal-key`.
2. **Zdroj je commit:** čte se zadaný commit (starší commit vs. `HEAD` vs. špinavý pracovní strom), tag, strom, neexistující SHA a částečný klon se odmítnou, chybějící soubor, CRLF, NUL, ne-UTF-8 a obří soubor mají definovaný výsledek, `GIT_DIR` z prostředí nic nepřesměruje, chybějící `git` je exit 3. Promisor/partial clone se odmítne před jakýmkoli čtením objektu (test pořadí volání), i s lokálním syntetickým promisor remote a při ignorovaném `GIT_NO_LAZY_FETCH`, a repozitář ani remote se přitom nezmění (úplné snímky `.git` před a po jsou shodné, také nad skutečným `git clone --filter=blob:none` přes `file://`); lazy fetch nepovolí ani zděděné `protocol.file.allow=always` (systémová nebo globální konfigurace, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`) ani `GIT_ALLOW_PROTOCOL=file`; u každého „nic se nestalo“ test ověřuje, že nechráněný git by objekt dotáhl; `GIT_NO_REPLACE_OBJECTS=1` dostane každý git proces i při zděděné jiné hodnotě a replacement ref na commit, strom i blob, `packed-refs` a vlastní `GIT_REPLACE_REF_BASE` nezmění Compose ani šablonu.
3. **Statický sken a šablona:** tabulka podporovaných i nepodporovaných konstrukcí; parser šablony je shodný s `envfile.read_env`; duplicity, nerozpoznané řádky, `$` v hodnotě a chybějící či neplaceholderové generované klíče se hlásí.
4. **Cíl:** nepřítomný, prázdný, cizí, částečný a úplný adresář, soubor, symlink i visící symlink; vše se odmítne a zůstane beze změny (snímek stromu před a po); otisk klíče existující sady MATCH, MISMATCH, UNVERIFIABLE bez výpisu hodnoty; cíl v repozitáři, chybějící rodič, rodič zapisovatelný ostatním, cizí vlastník rodiče.
5. **Zápis a zveřejnění:** právě dva soubory s módy 0600 a adresář 0700, LF; `.env` se od šablony liší přesně čtyřmi řádky; plný SHA-256 klíče a hash `.env` v záznamu; sada není nikdy viditelná napůl; chyba, přerušení a `fsync` selhání v každém kroku nic nezveřejní a uklidí; `renameat2(RENAME_NOREPLACE)` nenahradí prázdný ani plný adresář, soubor ani symlink; cizí adresář, soubor nebo symlink, který se objeví na cíli, se nepřepíše ani neuklidí (zveřejnění, chyba i přerušení); staging je připnutý deskriptorem: cizí objekt pod jménem stagingu (adresář s obsahem, prázdný adresář, symlink, objekt jiného vlastníka) nedostane žádné tajemství, nezveřejní se a nezmění se jeho obsah, inode, mód, vlastník ani mtime, ani se neuklízí (`STAGING_REPLACED`, `STAGING_LEFT_EMPTIED`, `LEFT_IN_PLACE`); signál (SIGTERM, SIGINT) doručený hned po `rename` i výjimka unikající z volání po úspěšném `rename` dají `CONFIG_PREPARED` odpovídající disku (`INTERRUPTED_AFTER_PUBLICATION`, `ERROR_AFTER_PUBLICATION`); neočekávaná výjimka v kterémkoli kroku nic nezveřejní a uklidí; záměna výsledku po `rename` dá `PUBLICATION_STATE_UNKNOWN` s cestou a bez mazání; druhý skutečný signál při zjišťování výsledku nebo úklidu je `INTERRUPTED_WHILE_FINISHING`, ne traceback ani tiché ukončení; skutečné SIGINT, SIGTERM i SIGKILL zvenčí v oknech před syscallem, po něm, při `fsync` rodiče a při úklidu; tvrdé ukončení před i po `rename`; opakovaný běh po přerušení nic nepřegeneruje; prohraný souběh; šest souběžných procesů dá jednu sadu.
6. **Hygiena a rozsah:** žádná generovaná hodnota ve výstupu, ani při chybách, kromě jednorázového závěrečného přehledu úspěšného `init-config` v interaktivním terminálu (K7); podproces je jen `git` v sedmi čtecích tvarech s pevným prostředím a zákazem všech vestavěných transportů (K9); zdroj neimportuje síťové, databázové ani Docker moduly, `bootstrap` ani `migrate` (`ctypes` slouží jen k `renameat2`); checkout se nemění a `.env` z něj se nečte.
6a. **Jednorázový přehled tajností (K7)** ([test_install_reveal.py](../../tests/test_install_reveal.py), aserce jmenují uniklou tajnost, nikdy její hodnotu): rozhodnutí o interaktivním terminálu (stdout a stdin tty, žádné CI prostředí) a výpis v `Report.emit`; přehled stojí těsně před koncovými řádky, vypíše se jednou a jen čtyři generované názvy; v CI, při přesměrovaném stdout nebo stdin se vypíše jen cesta; nevypíše se při odmítnutí, selhání v kterémkoli kroku (ani když text výjimky obsahuje hodnotu), po přerušení a chybě po zveřejnění, při opakovaném běhu, v `preflight` a `plan`; dodané tokeny a provider klíče se nikdy nevypíšou; zobrazená hodnota je shodná s chráněným `.env` (0600), který přehled nemění; vše také celým příkazovým řádkem přes skutečný pseudoterminál (pty).
7. **Parita:** názvy a tvary čtyř tajemství odpovídají `bootstrap.GENERATED_SECRETS`, pravidlo placeholderů je `envfile.is_placeholder`.
8. `python docs/tools/docs_check.py` projde. Parita normalizace a regresní sada se nespouští: B06-1 nemění normalizaci ani retrieval.

**Další etapy (každá samostatné zadání, žádná není schválená):** B06-2 hostitelský preflight (G1, G3–G5, G9, inventář podsítě přes `TS`) a `apply` (checkout na SHA, adresáře, symlinky, `COMPOSE_FILE`, delegace na bootstrap s opravami bez výpisu hesel mimo jednorázový přehled podle K7 a s rotací jen po 401/403; vyžaduje root). B06-3 `verify` V0–V9 a V3b, nejdřív proti laboratornímu stacku. B06-4 plný běh v izolovaném Linuxu s Dockerem včetně laboratorního ověření klíče (`[?]` z oddílu 4). B06-5 až po rozhodnutích Q3 a Q7.

## 9. Otevřená rozhodnutí a rozpory ve zdrojích

### Rozhodnutí vlastníka (nic z toho není schválené)

| ID | Otázka | Doporučení |
|---|---|---|
| Q1 | Který SHA se instaluje a odkud? Před-R2 `af5f491` (shodný s produkcí) × stav s R2 (vyžaduje `EDU_AI_TUNNEL_SUBNET`, R2 není schválené k nasazení). URL nového GitHubu a způsob přístupu | SHA je povinný vstup všech příkazů a nemá výchozí hodnotu; B06-1 funguje nad oběma stavy |
| Q2 | Rozložení `/opt/edu_ai` + `/srv/edu_ai` se symlinky, nebo čisté řešení proměnnými v Compose (neschválené S5)? | symlinky do B06-4, Compose až po R2 |
| Q3 | `02_seed_config.sql` obsahuje **fiktivní** entity a `staff_user` (`SD:12-15`, `B3B:69-71`). Zůstanou na školním serveru, a jak se založí skutečná škola (`A3A:46`)? | pro etapy 1–4 ponechat (V3, V7 je potřebují); zavedení skutečné školy vlastní úkol |
| Q4 | `OPENAI_API_KEY`: skutečný klíč, nebo formální hodnota, když poskytovatele drží Bifrost (`DC:182-183`)? Dostačuje formální hodnota všem komponentám? **[?]** | rozhodnout po laboratorním ověření |
| Q5 | Řádek query logu z V4: ponechat označený, nebo smazat? | ponechat (mazání dat je nevratné) |
| Q6 | Kde a kdo drží úschovu klíče Langflow a hesel? | mimo server, jiné místo než záloha DB |
| Q7 | Expozice 80/8443/8444: pouze `PERIMETR NEOVĚŘEN` + `perimeter_check.py`, nebo bind proměnné v Compose? | až po R2 |
| Q8 | Tunel Cloudflare na novém serveru: ano (token, Access politika), nebo vypnuto? | výchozí vypnuto; službu při prázdném tokenu nespouštět |

### Rozpory ve zdrojích (podle `AG` jde o vady, tady se jen hlásí)

- **R-1** `EX` × `DC`: Compose vyžaduje `EDU_AI_TUNNEL_SUBNET` (`DC:537`) a čte `CHAT_LEGAL_ENTITY`, `CHAT_ENTITY_PICKER`, `QA_PORTAL_ALLOWED_ORIGINS`, `JINA_API_KEY`; šablona je neobsahuje (`A4A:237`). Instalátor šablonu neupravuje (R2), doplňuje mimo ni.
- **R-2** `BS:83-87,261` tvrdí, že `/health` search hlásí chybějící OpenAI klíč. Kód (`SV:350-352`) vrací jen `status` a `service`, `missing_config` má jen API (`API:158-198`). `SM:173-177` pro search tedy vždy projde.
- **R-3** `ON:37-44` a `BS:256-262` chtějí skutečný OpenAI klíč v `.env`, `DC:182-183` a `EX:160-161` říkají, že poskytovatele drží Bifrost a klíč v `.env` je formální. `ON:3` uvádí 7 kontejnerů, Compose má 10 služeb.
- **R-4** Komentář `DC:484-486` říká, že zápis z cizího origin je zamítnut. `QS:34-35` při prázdné allow-listě kontrolu origin přeskočí; zbývá jen hlavička `X-QA-Portal`.

Po revizi vyřazeno: `ON:337` (`down -v` při ruční obnově) a `CI:226-228` (zákaz při deployi) jsou různé kontexty, ne rozpor; pro instalátor platí ochrana existujících dat. Chybějící sledovaný `postgres/releases/` není vada mapy v `AG`: generátory výstup vytvoří, na tomto SHA jen není publikovaný manifest vydání. To je omezení distribuce, ne překážka pro práci s přesným Git SHA.

Další omezení: image se na hostiteli staví, takže výsledek závisí na nepřipnutých základech a stahování (`DL:8` jen tag; `web_chat/Dockerfile` a `qa_portal/Dockerfile` bez digestu) a `.deb` pg_search se stahuje bez kontrolního součtu (`DP:22-25`). Verzovaná vydání s hotovými image jsou mimo B06-A.

## 10. Stav implementace B06-1 a bootstrapu (kandidát)

**Kandidát B06-1 + bootstrap, 2026-10-06.** Není to hotová instalace ani podporovaná aktualizace školy. Hostitel se nekontroluje (`HOST_NOT_CHECKED`), nic se nespouští (`INSTALLATION_NOT_STARTED`),
B06-2 a B06-3 neexistují. Vše níže je publikováno na `origin/clean_init` (kód a testy v commitu `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, dokumentace v následujícím commitu; jeho SHA nelze uvést uvnitř něj, viz `git log` těchto souborů). Podklad: čistý `cfd6f7de5b23d37e61efbfba119d6c905bde147d` plus tato práce;
tři nezávislé revize posuzovaly neměnný snapshot `c93527e0d0ab833d3e3f81cac1f2cf043b31a305`; závěrečná revize Codex pak vedla k pěti dalším opravám (záznam revizí, oddíl 4).

### 10.1 Externí konfigurace B06-1 a `.env` bootstrapu

Jsou to dvě oddělené věci, které se zatím nepropojují. `install.py init-config` vytvoří **novou chráněnou sadu** (`.env` + `install-record.json`) v externím adresáři.
`scripts/bootstrap.py` dál pracuje s `.env` v kořeni checkoutu (nebo se symlinkem na něj) a externí sadu nečte; `init-config` bootstrap nevolá. Provozní propojení
(symlink `.env` → externí sada, delegace na bootstrap, začlenění do `apply`) patří do B06-2. Co ho čeká a dnes je zdokumentované a otestované:

- Návratové kódy: bootstrap a `create_api_key.py` mají `0` hotovo, `1` chyba, `3` klíč nešlo ověřit a **nic se nezměnilo** (zkus znovu); `install.py` má `0`, `2` odmítnuto (nic se nezměnilo), `3` nelze ověřit nebo dokončit.
  Mapování pro B06-2: bootstrap `3` je „opakuj“, bootstrap `1` je chyba po případné částečné změně, tedy nikoli „odmítnuto“ (kód 2 instalátoru říká, že se nic nezměnilo).
  `langflow_url() is None` dnes vrací `1`, přestože je to typické „zkus znovu“; neměněno.
- `bootstrap.step_env` dnes vytváří `.env` v kořeni checkoutu s právy 0600, atomicky a bez přepsání; sada z `init-config` je jiný soubor s jinými hodnotami. Dokud se nepropojí, nesmí se oba nasazovat jako by byly totéž.

### 10.2 Soubory a hashe

Změněné nebo nové soubory (hash `install.py` a ostatních skriptů po závěrečné revizi Codex; testy v tabulce níže). Všechny mají LF a žádný NUL. Hashe všech 29 přenesených souborů jsou v `tests/eval_out/b06_codex_fix_20261006/final_transfer_sha256.txt` (ignorováno gitem).

```text
7dae1aa79118b2a1ba2b043e377f52e71eeb0a4d06674531a813dfa6f1290202  scripts/install.py
4c168a3515d50e9ed5bd65bf7e501b75be3f84fa67d0e0c44c06af4f4ea596c1  scripts/bootstrap.py
11aca6c3dbc07fb01ddc9fd7d2a8e3b7768ef53ee4e024d7e34fc9118a164d6c  scripts/create_api_key.py
97b9e6c65b156d95b17a6bac90733bc5e9d64aa0bec5b8a50c15b3060800cbd5  scripts/envfile.py
33eaec4a83b05fabf338df7ad09515f1abbe77c5e84323c8ad8259863e6e7f9e  scripts/http_safe.py                (nový)
5c304fe622b48b673c8db6f702cc8a607353e88163ce869c51790f3d6f921e8a  scripts/import_flows.py
19a2b48b55908b8dd66ec645f6f7aeeb8d7d6f3e54477692d8e5765785125f53  scripts/smoke.py
```

| Soubor | Testů | Co hlídá |
|---|---|---|
| `tests/test_install_preflight.py`, `test_install_config.py`, `test_install_findings.py` | 42, 29, 20 | základ B06-1, R1–R4 z prvního kola |
| `tests/test_install_publication_regression.py` | 36 | R1 a R4 z druhého kola (identita stagingu, signály); od kola 4 i zákaz odstranění jména ve zdroji (AST, běží i na Windows) |
| `tests/test_install_git_regression.py` | 17 | R2 a R3 z druhého kola (transporty, `git replace`) |
| `tests/test_install_reveal.py` | 29 | jednorázový přehled tajností (K7) |
| `tests/test_install_cleanup_safety.py` | 6 | nikdy `rmdir` podle jména, adopce stagingu, FIFO, SIGHUP (revize A) |
| `tests/test_install_discard_race.py` | 28 | bod 1 revize Codex: druhý proces zamění položku před každým voláním úklidu (112 scénářů), inode, symlink, adresář, FIFO, `ftruncate` selže, deskriptory se vždy zavřou |
| `tests/test_install_git_env.py` | 19 | bod 2 revize Codex: prostředí Git potomků, zděděný `GIT_CONFIG` (soubor prázdný, chybějící, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`), pořadí volání, emulace starého Gitu |
| `tests/test_install_git_binding.py` | 12 | `partialclonefilter`, `fd`, symlink, přepočet SHA (revize B) |
| `tests/test_install_parent.py` | 6 | záměna rodiče (`TARGET_PARENT_CHANGED`) |
| `tests/test_install_closeout.py` | 3 | zavřená rouře po zveřejnění |
| `tests/test_bootstrap_key_safety.py` | 57 | tři stavy klíče, tajnosti ve výstupu, přehled bootstrapu |
| `tests/test_bootstrap_env_write.py` | 24 | N2 a N3: atomický zápis a soukromá práva `.env` |
| `tests/test_bootstrap_key_rotation.py` | 30 | N4, N5, N8, N9: pořadí rotace, přesměrování, `env_path`, kód 3 |
| `tests/test_bootstrap_key_lifecycle.py` | 39 | body 3 až 5 revize Codex: stav po možném zápisu `.env`, skutečný SIGINT, 403, starý klíč se nemaže, selhaný výpis |
| `tests/test_bootstrap_output.py` | 13 | N1: `redact`, `relay`, duplicitní řádek v `set_env` |
| `tests/install_fixtures.py`, `install_child.py` | | pomocné |

Dokumentace: tento návrh, revize implementace, [zpevnění bootstrapu](b06-bootstrap-hardening-20261006.md),
tři nezávislé revize, `ONBOARDING.md` §1 a §5. Společné plány: jen B06 pasáže v production-readiness.md a clean-init-progress.md.

### 10.3 Opravy R1–R4 (čtyři kola)

| Nález | Kolo 1 (snapshot `3b62a25`) | Kolo 2 (dva pracovníci) | Kolo 3 (nezávislé revize A a B) | Kolo 4 (závěrečná revize Codex) |
|---|---|---|---|---|
| R1 záměna rezervovaného cíle | rezervace `mkdir` zrušena; zveřejnění jediným `renameat2(RENAME_NOREPLACE)` | staging připnutý deskriptorem, kontrola identity před publikací a po ní, úklid podle inodu, `STAGING_REPLACED` | **`rmdir` podle jména mazal cizí prázdný adresář, adopce cizího adresáře mezi `mkdtemp` a otevřením mazala cizí soubory**: adresář se podle jména nikdy nemaže, staging jen prázdný 0700 bez `fchmod`, vlastní soubory jen podle inodu, zbytek `STAGING_LEFT_EMPTY` a `STALE_STAGING_EMPTY`; identita rodiče `TARGET_PARENT_CHANGED` | **závod `os.stat` → `os.unlink` v úklidu** (druhý proces zamění soubor mezi voláními): úklid nemaže **žádné jméno**, obsah vlastních souborů ničí přes držený deskriptor (`ftruncate`), staging zůstane a hlášení nese plnou cestu a stav (`STAGING_LEFT_EMPTIED`, `STAGING_LEFT_NOT_EMPTIED`; nahrazuje `STAGING_LEFT_EMPTY`) |
| R2 pozdní kontrola partial clone | konfigurace promisor před objekty, `protocol.allow=never` | zákaz všech vestavěných protokolů a odstranění `GIT_ALLOW_PROTOCOL` | detektor zná i `remote.*.partialclonefilter` a jakýkoli výskyt `extensions.partialclone`, transport `fd` | **zděděný `GIT_CONFIG`** skryl promisor repozitáře před detektorem: Git potomci nedědí `GIT_CONFIG`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n`, `GIT_CONFIG_VALUE_n`; detektor čte celou skutečnou konfiguraci a běží před jakýmkoli čtením objektu |
| R3 podstrčení obsahu přes `git replace` | `GIT_NO_REPLACE_OBJECTS=1` | beze změny kódu, rozšířené testy | symlink v commitu se odmítá, obsah je vázán na SHA **přepočtem** řetězu commit → strom → blob | beze změny |
| R4 signál po `rename` | výsledek podle disku | výjimky podle disku, `INTERRUPTED_WHILE_FINISHING`, `ERROR_AFTER_PUBLICATION` | SIGHUP, FIFO, zavřená rouře po zveřejnění (exit stav zůstává výsledkem) | beze změny; druhé přerušení při dokončování nově vyprázdní soubory, když je jisté, že nic nebylo zveřejněno |

### 10.4 Bootstrap: stav N1–N9

Původ značení N1–N9: [zpevnění bootstrapu](b06-bootstrap-hardening-20261006.md), oddíl 6. Všechny testy jsou mockované (viz oddíl 10.7), ne proti Langflow 1.10.1.

| # | Stav | Co se stalo |
|---|---|---|
| N1 | část uzavřena, část **otevřena** | výstup, který bootstrap předává (`relay`, `redact`), je tvrdší (jen tvary řádků potomka, zbytek víceřádkové zprávy pryč, řídicí znaky a credentials v URL pryč, `redact` nikdy neselže otevřeně). Samostatné `import_flows.py` a `smoke.py` dál tisknou těla odpovědí: samostatný úkol |
| N2 | uzavřeno | `envfile.write_atomic`: dočasný soubor ve stejném adresáři, `fsync`, `os.replace`, symlink zůstane, existující mód se zachová; selhání nechá bajty, mtime i výpis adresáře. `set_env` přepisuje všechny výskyty duplicitního klíče |
| N3 | uzavřeno na Linuxu | `.env` vzniká 0600 od prvního bajtu a nikdy nepřepíše existující (`create_private`); existující soubor s právy pro skupinu nebo ostatní se jen varuje. Na Windows `chmod` soukromé ACL nedokazuje a nic se netvrdí |
| N4 | uzavřeno, po revizi Codex změněno | pořadí výpis → vytvoření → ověření → atomický zápis; starý klíč se nemaže nikdy; nový klíč se maže jen podle `id` odpovědi nebo při dvou úspěšných výpisech, a po možném zápisu `.env` jen když čtení `.env` prokáže, že se nezapsal; 403 je neověřitelný stav (kód 3); platí i pro `--force`; ruční klíč se před uložením ověří |
| N5 | uzavřeno | `http_safe.py` odmítá přesměrování u všech volání, která nesou klíč nebo token |
| N6 | uzavřeno | zpráva o `OPENAI_API_KEY` už netvrdí, že ji hlásí `/health` |
| N7 | uzavřeno | `ONBOARDING.md` §1 a §5, tabulka bootstrapu v oddílu 2 a tento oddíl |
| N8 | uzavřeno | `ensure_api_key(env_path)` bez defaultu, všichni volající ho předávají |
| N9 | otestováno, obal nevznikl | propagace kódu 3 přes `bootstrap.main()` a CLI, `--help` obou příkazů; mapování pro B06-2 v oddílu 10.1 |

### 10.5 Změna požadavku: jednorázový přehled tajností (2026-10-06)

Vlastník změnil dřívější pravidlo „nikdy žádný výpis“: po úspěšném prvním běhu v interaktivním terminálu se jednorázově vypíšou místní hesla a klíče, které vytvořil právě tento běh, aby si je uživatel uložil do trezoru. Pravidlo je v K7;
implementace je v `scripts/install.py` (`interactive_terminal`, `Report.reveal`, `Report.emit`, napojení v `init_config`) a nemění zveřejnění sady, `.env` ani záznam. Rozsah je `init-config`, protože B06-1 jiné tajnosti nevytváří;
„úspěšná první instalace“ tu znamená úspěšný první běh `init-config` (`CONFIG_PREPARED`), ne instalaci. Stejný princip má přehled v `bootstrap.py` (`step_summary`) pro hodnoty, které vytvořil bootstrap.

Potvrzené volby vlastníka (zachovány, další potvrzení nevyžadují): interaktivní je současně stdin i stdout; detekce CI se zachovává (níže); přehled stojí před koncovými řádky; po přerušení nebo chybě po zveřejnění se hodnoty nezobrazují; tajnosti zůstávají v chráněném `.env`.

Současná detekce CI se liší podle nástroje a tak se zachovává:

| Nástroj | Interaktivní, když | CI poznává podle |
|---|---|---|
| `install.py` | stdout i stdin jsou tty a není CI | 13 proměnných: `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `BUILDKITE`, `JENKINS_URL`, `TF_BUILD`, `TEAMCITY_VERSION`, `CIRCLECI`, `TRAVIS`, `APPVEYOR`, `DRONE`, `CODEBUILD_BUILD_ID`, `BITBUCKET_BUILD_NUMBER` |
| `bootstrap.py` | stdin i stdout jsou tty, není CI a není `--no-input` | 6 proměnných: `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`, `TF_BUILD` |

U obou platí: hodnota `0`, `false`, `no` nebo prázdná CI nezakládá; jiný CI systém bez těchto proměnných by s terminálem na obou proudech hodnoty zobrazil. `install.py` nemá `--no-input` (test hlídá seznam jeho voleb).

### 10.6 Výsledky

Celou instalační a bootstrapovou sadu provedl koordinátor na výsledném kandidátu jednou na platformu po poslední změně kódu a testů závěrečné revize Codex (hashe kódu a testů před a po běhu shodné); opakování bez změny kódu se nedělalo. Přeskočený test se nevykazuje jako splněný.

| Běh | Výsledek |
|---|---|
| Windows 11, Python 3.11.9, Git 2.54.0.windows.1: `test_install_*.py` | 247 testů, OK, **139 přeskočeno**, 108 provedeno. Přeskočené: 136× vyžadují Linux (`renameat2`, `fork`, `/proc`, signály, módy, pty, pipes, `sh`), 2× nelze vytvořit symlink, 1× POSIX módy mimo root |
| Windows: `test_bootstrap_*.py` | 163 testů, OK, **11 přeskočeno** (POSIX módy a linky), 152 provedeno. Mód, umask a symlink se na Windows neověřují |
| WSL2 Ubuntu (Python 3.14.4, Git 2.53.0, UID 1000), kopie i `TMPDIR` na ext4: `test_install_*.py` | 247 testů, OK, 0 přeskočeno |
| WSL: `test_bootstrap_*.py` | 163 testů, OK, 0 přeskočeno |
| Dvouprocesové reprodukce nálezů revize Codex (koordinátor, WSL ext4): úklid, `GIT_CONFIG`, readback a SIGINT, dvě shodné předpony | snapshot: cizí objekt smazán, promisor přehlédnut a vlastní helper spuštěn, nový klíč smazán, oba klíče smazány; opravený kandidát: nic z toho. Podrobně v záznamu revizí |
| Cílená mutační kontrola oprav kola 4 (pracovníci, kopie zdrojů) | úklid 10 mutantů, Git 4, klíč 19: všech 33 zachyceno. Dřívější kolo: 23 vad (koordinátor) a dřívějších 10 vad přehledu tajností |
| Laboratorní ověření proti Langflow 1.10.1 (2026-10-06, image `edu_ai:1.1.0` `sha256:b85e298b6380…`, samostatný kontejner bez sítě, vlastní dočasná databáze, testovací klíče) | všech osm kontrol provedeno, žádná prokázaná kompatibilitní vada, kód ani testy se neměnily; hlavní zjištění: **neplatný klíč dává 403**, `check_key` musí rozbalit gzip, přihlášení je omezené (429). Oddíl 5 záznamu revizí |
| Tři nezávislé revize a závěrečná revize Codex | záznam v b06-independent-review-20261006.md |
| `python docs/tools/docs_check.py` (Windows), `git diff --check` proti snapshotu | docs_check OK (v čisté kopii kandidáta: 14 stránek, 71 souborů), diff-check čistý |

Starší běhy (kola 1 až 3, 10 mutantů přehledu, 18 a 10 mutantů pracovníků, 23 mutantů koordinátora) jsou historie a v tomto kole se neopakovaly.

### 10.7 Neověřeno a omezení

- **Skutečný Langflow 1.10.1:** laboratorně ověřeno 2026-10-06 (oddíl 5 záznamu revizí): neplatný klíč dostane **403** (ne 401), odpověď na vytvoření nese `id` (UUID jako řetězec), výpis je maskovaný (`sk-` a 5 znaků, zbytek `*`), `GET /api/v1/flows/` vrací gzipovaný JSON list. Neověřeno zůstává, zda jsou 401/403 vždy z Langflow (proxy před ním), PostgreSQL místo SQLite a skutečný průchod bootstrapu. Offline testy jsou dál z mocků a z lokálního fake transportu pod skutečnou přesměrovací mechanikou `urllib`.
- **Důsledek 403 na Langflow 1.10.1:** `replaced` se nikdy nespustí; po `down -v` je třeba `create_api_key.py --force` (bootstrap ho nemá). Pravidlo i tento důsledek vlastník 2026-10-06 potvrdil: HTTP 403 je neověřitelný stav (kód 3, žádná automatická výměna), ruční `--force` může vytvořit nový klíč, původní klíč se automaticky nemaže; další potvrzení se nevyžaduje.
  Neznámý tvar výpisu vede jen k tomu, že se nic nesmaže (bezpečná chyba); neznámé `id` k hledání nového klíče rozdílem dvou úspěšných výpisů.
- **Starý klíč zůstává:** maskovaná předpona ho neidentifikuje (klíče téhož jména mohou sdílet viditelnou předponu a odmítnutý klíč ve výpisu vůbec není), proto se nemaže nikdy. Po výměně i po ztraceném `.env` zůstávají v Langflow staré klíče jména `edu_ai-bootstrap`; skript to řekne a operátor je odstraní ručně (Settings → API Keys).
- **403 se automaticky nevyměňuje** (kód 3, `--force` je ruční volba). Pokud Langflow vrací 403 i pro neplatný klíč, automatická výměna po `down -v` nenastane nikdy; ověřit v laboratoři. Po `replaced` se pro všechny služby spouští `up -d --force-recreate`; mezi souběžnými běhy bootstrapu není zámek; `check_key` nemá celkový termín ani limit velikosti odpovědi. Neměněno.
- **R1, zbytky:** `renameat2` bere jména, takže cizí objekt na jménu stagingu mezi kontrolou a voláním se přesune na cíl (nic se nesmaže ani nepřepíše, výsledek `PUBLICATION_STATE_UNKNOWN`, příští běh `TARGET_EXISTS_*`); záměna rodiče se jen přizná, nezabrání se jí;
  prázdný adresář stejného uživatele s módem 0700, který převzal jméno mezi `mkdtemp` a otevřením, se nerozliší (použije se jako staging, nic se v něm nemaže, soubory vytvořené během se vyprázdní). Po neúspěšném běhu zůstane `.name.staging-*` se dvěma soubory nulové délky (obsah zničen přes deskriptor, `ftruncate` nepřepisuje uvolněné bloky); jména se nemažou a operátor adresář po prohlídce odstraní sám.
- **R4, zbytky:** SIGTERM a SIGINT po návratu z `init_config` (při tisku) zabijí proces bez hlášení, respektive tracebackem; sada je kompletní a hodnoty jsou v `.env`. SIGQUIT se neobsluhuje, volání z jiného vlákna než hlavního se nezkoušelo.
- **Přehled tajností:** terminál se záznamem relace hodnoty zachytí a skript to nepozná. Objekt `Report` před `emit` hodnoty drží (`repr` ne). Chyby argparse a `--help` nemají koncové markery. Windows ConPTY, `tmux`, `screen` a `ssh -t` se nezkoušely.
- **Úklid, zbytky kola 4:** okno mezi návratem `os.open` a zápisem deskriptoru do `run.held` (soubor je tehdy prázdný, nic neuniká; po přerušení zůstane jako cizí položka); třetí přerušení uvnitř obsluhy druhého unikne z `init_config`; `_locate` závisí na `/proc`; SIGKILL a `os._exit` nechají ve stagingu plná tajemství (příští běh `STALE_STAGING`).
- **Git, kolo 4:** `safe.directory` předané přes `GIT_CONFIG_COUNT` už nefunguje (patří do globální konfigurace, `GIT_CONFIG_GLOBAL` zůstává), selže bezpečně jako `GIT_FAILED`. Mimo rozsah B06 spouští `git` i `scripts/eduai_migrate/manifest.py` (`git show`, `git -C`) bez čištění prostředí; neměněno, nepatří do instalační cesty.
- **Git:** testován Git 2.54.0 (Windows) a 2.53.0 (WSL); skutečný Git starší než 2.44 jen emulací. Vlastní remote helper je dosažitelný jen na Gitu < 2.44 a po uzavření detektoru se nespustil. Testy vyžadují `git` v `PATH`. SHA-256 repozitáře: 40hex SHA se odmítne, 64hex odmítá regexp.
- **Souborový systém:** `renameat2(RENAME_NOREPLACE)` ověřeno jen na jádru 6.18 (ext4, tmpfs). NFS, overlayfs, FUSE, `/mnt/d` (drvfs), běh jako root, ACL a SELinux se nezkoušely. Mód 0700 po `mkdtemp` může na souborovém systému, který ignoruje mód při `mkdir`, způsobit selhání.
- **Windows:** `init-config` tam odmítá (`UNSUPPORTED_PLATFORM`); publikace, signály, módy a pty se tam neověřují. U `.env` vytvořeného bootstrapem na Windows se soukromí netvrdí.
- Statický sken je omezen na podmnožinu YAML, kterou Compose dnes používá; třídění `LANGFLOW_API_KEY` jako „vydá Langflow později“ je konstanta ve skriptu. Po SIGKILL před `rename` zůstává staging s nezveřejněnými tajnostmi (příští běh ho ohlásí jako `STALE_STAGING`).
- Nic z hostitelských kontrol (G1, G3–G5, G9), `apply`, `verify`, celý bootstrap ani PostgreSQL nebylo zkoušeno (chování API klíčů proti Langflow ano, oddíl 10.6). Parita normalizace a retrieval regresní sada se nespouštěly: kandidát je nemění.
- **Revize:** nezávislé revize a závěrečná revize Codex proběhly (oddíl 10.3 a záznam revizí); body závěrečné revize jsou uzavřeny a ověřeny offline; kompatibilita s Langflow 1.10.1 je ověřena v laboratoři (oddíl 10.6). Kandidát je publikován (oddíl 10); pravidlo pro 403 je potvrzeno.

### 10.8 Další krok

1. ~~Rozhodnutí o commitu kandidáta~~ Provedeno 2026-10-06: kandidát po závěrečné revizi Codex (pět bodů uzavřeno, oddíl 10.3 a záznam revizí) je publikováno na `origin/clean_init` (kód a testy v commitu `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, dokumentace v následujícím commitu; jeho SHA nelze uvést uvnitř něj, viz `git log` těchto souborů).
2. ~~Laboratorní ověření proti skutečnému Langflow 1.10.1~~ Provedeno 2026-10-06 (oddíl 5 záznamu revizí). Pravidlo pro 403 (v Langflow 1.10.1 jediný signál mrtvého klíče) a ruční `--force` vlastník potvrdil; zbývají další ověření: proxy před Langflow, PostgreSQL, celý bootstrap proti stacku.
3. Až potom samostatné zadání B06-2 (hostitelský preflight, `apply`, mapování kódů z oddílu 10.1 a propojení externí sady s `.env`). B06-3 a B06-4 beze změny. [B06-U](b06-upgrade-strategy.md) je samostatný návrh podporovaných aktualizací; nic z něj není implementováno.

## Zdroje

Zkratky v textu odkazují na tyto soubory (číslo za dvojtečkou je řádek).

| Zkr. | Soubor | Zkr. | Soubor |
|---|---|---|---|
| AG | [AGENTS.md](../../AGENTS.md) | NX | [nginx/conf/nginx.conf](../../nginx/conf/nginx.conf) |
| PR | production-readiness.md | SV | [search/app/server.py](../../search/app/server.py) |
| ON | [ONBOARDING.md](../../ONBOARDING.md) | RT | [search/app/nodes/route.py](../../search/app/nodes/route.py) |
| BS | [scripts/bootstrap.py](../../scripts/bootstrap.py) | API | [api/app/main.py](../../api/app/main.py) |
| EF | [scripts/envfile.py](../../scripts/envfile.py) | WC | [web_chat/web_chat.py](../../web_chat/web_chat.py) |
| AK | [scripts/create_api_key.py](../../scripts/create_api_key.py) | QS | [qa_portal/backend/app/api/security.py](../../qa_portal/backend/app/api/security.py) |
| IF | [scripts/import_flows.py](../../scripts/import_flows.py) | SD | [postgres/init_clean/02_seed_config.sql](../../postgres/init_clean/02_seed_config.sql) |
| SM | [scripts/smoke.py](../../scripts/smoke.py) | B216 | [postgres/baseline/B216.json](../../postgres/baseline/B216.json) |
| TS | [scripts/tunnel_subnet.py](../../scripts/tunnel_subnet.py) | GI | [.gitignore](../../.gitignore) |
| PC | [scripts/perimeter_check.py](../../scripts/perimeter_check.py) | CI | [CI-CD.md](../../CI-CD.md) |
| EX | [.env.example](../../.env.example) | DM | [DATA_MODEL_AND_SETUP.md](../DATA_MODEL_AND_SETUP.md) |
| DC | [docker-compose.yml](../../docker-compose.yml) | B3B | b03b-progress.md |
| DO | [docker-compose.override.yml](../../docker-compose.override.yml) | B3C | b03c-progress.md |
| DP | [Dockerfile.postgres](../../Dockerfile.postgres) | B3E | b03e-progress.md |
| DL | [Dockerfile](../../Dockerfile) (Langflow) | A3A | audit-03a-installation-baseline.md |
| R2L | r2-lab-results-v2.md | A4A | audit-04a-containers-networks.md |
| INS | [scripts/install.py](../../scripts/install.py) | A2A | audit-02a-backup-assessment.md |
