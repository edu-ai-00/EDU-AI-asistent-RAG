# B06: zpevnění bootstrapu (Langflow API klíč, `.env`, tajnosti ve výstupu)

Veřejná kopie vynechává interní akceptační reporty a odkazy na ně; tento návrh není důkazem aktuálního nasazení.

**Stav (2026-10-06): součást kandidáta B06-1 + bootstrap, publikováno na `origin/clean_init` (kód a testy v commitu `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, dokumentace v následujícím commitu; jeho SHA nelze uvést uvnitř něj, viz `git log` těchto souborů), po třech nezávislých revizích, závěrečné revizi Codex (pět nálezů uzavřeno) a laboratorním ověření API klíčů proti Langflow 1.10.1. Není to schválená instalace, podporovaná aktualizace školy ani podklad pro nasazení.**
Offline testy jsou mocky. Laboratorní ověření (oddíl 5 záznamu revizí) proběhlo v samostatném kontejneru bez sítě s vlastní dočasnou databází a syntetickou konfigurací a pokrylo jen API klíčů; skutečný `.env` se nečetl. Celý bootstrap proti stacku, PostgreSQL a B06-2 jsou další etapa. Vlastník 2026-10-06 výslovně ponechal pravidlo a další potvrzení nevyžaduje: HTTP 403 je neověřitelný stav (kód 3, žádná automatická výměna), ruční `--force` může vytvořit nový klíč a původní klíč se automaticky nemaže. Starší formulace níže („po třech revizích“, „neověřeno“) popisují stav v den vzniku a jsou historie.
Datum: 2026-10-06. Původní samostatný kandidát vznikl nad technickým snapshotem `3b62a25daff65af2d9d59986d6f045ed8859b39c` (commit `fec7d8150c1b20f27331a54a7f3ce610b1ddf7d6`);
tento text popisuje stav po integraci revizí nad `c93527e0d0ab833d3e3f81cac1f2cf043b31a305`. Navazuje na body „klíč Langflow" a „souhrn tajností" z
[návrhu B06](b06-clean-install-proposal.md) (oddíl 2 řádky 6 a 11, oddíl 4, oddíl 10) a z revize návrhu; revize a uzavření nálezů: záznam.

Řeší dva body (klíč Langflow a tajnosti ve výstupu) a z nich plynoucí zápis `.env`. Všechno ostatní je v oddílu 6 jako stav nálezů N1–N9.

## 1. Co se změnilo

| Soubor | Změna |
|---|---|
| [`scripts/create_api_key.py`](../../scripts/create_api_key.py) | tři odpovědi místo dvou; neověřitelný stav nic nezmění; bezpečné pořadí výměny; neuložený nový klíč se smaže, jen když čtení `.env` prokáže, že se nezapsal; starý klíč se nemaže nikdy; 403 je neověřitelný stav; vložený klíč se ověří; `env_path` bez defaultu; zprávy bez těla odpovědi; `getpass`; kód 3 a epilog `--help` |
| [`scripts/bootstrap.py`](../../scripts/bootstrap.py) | ošetření nového stavu; `redact()` na všem vypsaném (nikdy neselže otevřeně); `relay()` tvrdší; `.env` vzniká soukromě a zapisuje se atomicky; závěrečný přehled; opravené zprávy o `/health`; epilog `--help` |
| [`scripts/envfile.py`](../../scripts/envfile.py) | `write_atomic`, `create_private`, `set_env` přepisuje všechny výskyty klíče; chování a parser jinak beze změny |
| [`scripts/http_safe.py`](../../scripts/http_safe.py) | nový: opener, který nenásleduje přesměrování |
| [`scripts/import_flows.py`](../../scripts/import_flows.py), [`scripts/smoke.py`](../../scripts/smoke.py) | autentizovaná volání přes `open_url` (dva řádky v každém) |
| [`ONBOARDING.md`](../../ONBOARDING.md) | §1 a §5: chování klíče, přehled tajností, kód 3 |
| `tests/test_bootstrap_key_safety.py` (57), `test_bootstrap_env_write.py` (24), `test_bootstrap_key_rotation.py` (30), `test_bootstrap_key_lifecycle.py` (39, nový), `test_bootstrap_output.py` (13) | 163 offline testů |

Neměněno: Compose, nginx, R2, aplikace, databáze, schéma, společné stavové dokumenty (mimo B06 pasáže). Změny `install.py` jsou popsány v návrhu B06, ne tady.

## 2. Langflow API klíč: tři stavy a bezpečná výměna

`check_key()` volá `GET /api/v1/flows/?page=1&size=1` s klíčem v hlavičce `x-api-key` a **selhává bezpečně**: závěrečné jsou jen dvě odpovědi (`200` s JSON a `401`), všechno ostatní je „nevím".

| Stav klíče v `.env` | Důkaz | Co skript udělá |
|---|---|---|
| chybí (žádný řádek, prázdná hodnota, `CHANGE_ME…`) | bez požadavku | **první vytvoření** (níže); žádný starý klíč se nemaže, protože se žádný nedá spojit s hodnotou |
| platný | `200` a tělo je JSON objekt nebo pole | nic se nemění |
| odmítnutý | `401` | **výměna** bez `--force` (níže) |
| neověřitelný | **`403`** (`HTTP 403, access denied: not proof that the key is invalid`), timeout (i při čtení), spojení odmítnuto či přerušeno, DNS, TLS, jiný HTTP stav (**včetně 3xx: přesměrování se nenásleduje**), `200` s tělem, které není JSON objekt/pole, jiný 2xx, poškozený gzip, **klíč, který nejde poslat jako hlavička (znak mimo ASCII, řídicí znak)**, jakákoli jiná výjimka | **nic**: žádný požadavek na vytvoření či smazání (ani login), `.env` bajtově stejný, žádná výzva k ruční náhradě; `KeyUnverifiable`, CLI končí kódem **3**, bootstrap také 3 a zastaví se před startem zbylých služeb |

### Pořadí výměny (N4)

Nikdy „smazat starý, vytvořit nový". Pro první vytvoření, výměnu i `--force`:

1. přihlášení superusera a **výpis** klíčů (jen čtení; selhaný výpis je „neznámý", nikdy „prázdný"),
2. vytvoření nového klíče,
3. ověření, že Langflow nový klíč **přijímá** (stejným požadavkem, který odmítl starý),
4. **atomický zápis** do `.env` a přečtení zpět.

**Starý klíč se nemaže nikdy** (ani po `replaced`, ani při `--force`). Maskovaná hodnota ve výpisu (`sk-m0fgI****`) ho spolehlivě neidentifikuje: klíče téhož jména mohou sdílet viditelnou předponu a odmítnutý starý klíč ve výpisu už vůbec není, takže shoda předpony může zasáhnout jen cizí klíč.
Po výměně, při které `.env` měl předchozí klíč, skript na stderr jednou větou (bez hodnot a `id`) řekne, že předchozí klíč zůstal v Langflow a že ho operátor odstraní v Settings → API Keys, pokud ho nepoužívá.

**Selhání před zápisem `.env`** (kroky 1 až 3, i Ctrl-C) nechá `.env` i všechny staré klíče beze změny a nově vytvořený klíč se smaže znovu: podle `id` z odpovědi na vytvoření, jinak podle jediné nové položky mezi dvěma **úspěšnými** výpisy (před a po vytvoření).
Selhaný výpis, žádná nebo více nových položek vedou k tomu, že se klíč nesmaže a skript to řekne (bezpečná chyba). Nepřijetí nového klíče (proxy odmítá všechno, adresa nevede na Langflow) je `KeyUnverifiable`: kód 3, nic se nezměnilo, žádná výzva k vložení klíče.

**Selhání při zápisu `.env` nebo po něm** se neposuzuje podle typu výjimky (Ctrl-C nevypovídá, zda `os.replace` proběhl), ale podle **čerstvého čtení `.env`** po selhání:

| Čerstvé čtení `.env` | Stav | Nový klíč | Výsledek |
|---|---|---|---|
| obsahuje nový klíč | uloženo | **nemaže se** | `KeyStateUncertain` (kód 1); Ctrl-C se po jedné řádce na stderr vyhodí beze změny |
| nejde přečíst, nebo se soubor změnil a klíč v něm není | neznámo | **nemaže se** | totéž |
| bajty jsou stejné jako těsně před zápisem | neuloženo (prokázáno) | smaže se | `KeyStoreError`: „.env is unchanged (compared byte for byte)" |

Chyba, která nastala dřív, než zápis začal (`.env` nešel ani přečíst), říká jen „nothing was written to .env". Tvrzení „unchanged" se nikde nepíše bez porovnání bajtů. `KeyStateUncertain` je podtřída `KeyStoreError`, takže nikdy nevede k výzvě k vložení klíče; říká, že nový klíč v Langflow zůstal, že nic nebylo smazáno,
a že se má zkontrolovat `LANGFLOW_API_KEY` v `.env`, spustit skript znovu (nejdřív ověří klíč v `.env`) a `docker compose up -d`. Kód je **1**, ne 3: 3 slibuje, že se nic nezměnilo, což tu neplatí. Stejná logika platí pro ručně vložený klíč (tam není v Langflow co mazat).

Ručně vložený klíč (jen po selhání loginu nebo vytvoření a jen pro chybějící či odmítnutý klíč) se před uložením **ověří**; nepřijatý je kód 3 a `.env` zůstane.

`--force` v `create_api_key.py` zůstává **výslovnou volbou operátora** (kontrolu starého klíče přeskočí) a používá stejné pořadí. Automatická cesta ho nepředává; test to hlídá za běhu i ve zdroji (AST).
Zpráva při neověřitelném stavu: klíč nešlo ověřit, příčina (jen stavový kód nebo pevná fráze), „Nothing was changed", spustit znovu, až Langflow odpovídá. Každý krok bootstrapu je idempotentní.

### Poznámka k 401/403 a loginu

Jen **401** je důvod k automatické výměně. **403** nemusí znamenat neplatný klíč (nedostatečné oprávnění, odpověď proxy) a úspěšný login superusera nedává právo cokoli zničit ani přepsat, proto je 403 v automatické cestě **neověřitelný stav**:
kód 3, jediný požadavek (kontrola), žádný login, výpis, vytvoření, mazání ani zápis `.env`. Zpráva zmiňuje výslovnou ruční možnost: `scripts/create_api_key.py --force` (bootstrap ji záměrně nemá).
Pro nový klíč při `--force` platí totéž: přijme se jen `200` s JSON, jinak je klíč „nepřijatý" (kód 3, smaže se podle `id`, `.env` zůstane).
**Důsledek, laboratorně potvrzený na Langflow 1.10.1 (oddíl 5 záznamu revizí):** Langflow vrací pro neplatný, chybějící i prázdný `x-api-key` **403**, ne 401. Automatická výměna mrtvého klíče (typicky po `docker compose down -v`) se proto na této verzi nikdy nespustí a operátor musí spustit `create_api_key.py --force`; kód 3 a nezměněná konfigurace při 403 se na skutečném Langflow potvrdily.

### Dotčení volající

| Volající | Dopad |
|---|---|
| `bootstrap.step_api_key` | zachytává `KeyUnverifiable` (kód 3, nově i při 403), `KeyStateUncertain` a `KeyStoreError` jako běžné selhání (kód 1, žádný další krok, žádné označení „recreate“), `replaced` jen po 401; klíč dál předává `step_langflow_vars` a `step_flows` v paměti; `env_path` předává výslovně |
| `create_api_key.main` | totéž; `env_path` předává výslovně |
| `ensure_api_key()` | `env_path` **nemá default** (test nad AST kontroluje všechny volající) |
| `key_works()` | odstraněna (její `False` slévalo tři stavy) |
| `import_flows.py`, `smoke.py` | čtou klíč z `.env`/prostředí; autentizovaná volání nenásledují přesměrování |
| `install.py` | nic neimportuje (stávající test hlídá) |

## 3. `.env`, přesměrování a tajnosti ve výstupu

**`.env` (N2, N3).** Zápis je atomický: dočasný soubor ve stejném adresáři, `fsync`, `os.replace`; selhání (plný disk, přerušení) nechá původní bajty, mtime i výpis adresáře. Symlinkovaný `.env` zůstane symlinkem (zapisuje se soubor, na který míří). Existující mód se zachová.
Nový `.env` vzniká 0600 od prvního bajtu (dočasný soubor + `link`, jinak výlučné vytvoření) a nikdy nepřepíše existující; na Linuxu se mód po vytvoření ověří. Existující `.env` s právy pro skupinu nebo ostatní se jen varuje (`chmod 600 .env`), nemění se.
Na Windows `chmod` soukromé ACL nedokazuje, proto se tam nic netvrdí.

**Přesměrování (N5).** `http_safe.open_url` odmítá každý 3xx u volání, která nesou `x-api-key` nebo `Authorization`; vznikne `HTTPError` se stavovým kódem a nic se neposílá jinam. `LANGFLOW_URL` proto musí být konečná adresa (hláška to říká).

**Průběh a chyby tajnosti nikdy neobsahují.** Zprávy se skládají ze stavových kódů a pevných frází (`describe_error`), nikdy z `str(exc)` ani z těla odpovědi. Navíc prochází vše vypsané `redact()`, která maskuje každou hodnotu,
kterou `.env` označuje jako tajnou podle názvu (`PASSWORD`, `SECRET`, `TOKEN`, `KEY`, hodnoty od 6 znaků), klíč vytvořený v tomto běhu a **každou hodnotu, kterou `.env` za běh držel** (nečitelný nebo zmizelý soubor maskování nevypne).
Výstup podprocesů (`import_flows.py`, `smoke.py`) prochází `relay()`: jen tvary řádků potomka (výsledek `[pass]`/`[skip]`/`[FAIL]`, očíslovaný nadpis, součty, řádky importu), zbytek víceřádkové zprávy za značkou (tělo) se zahodí, `[FAIL]` se useká před prvním `:`,
řídicí znaky se mění na `?`, přihlašovací údaje v URL se odstraní a řádky se redigují **před** zkrácením. Z adres se odstraní přihlašovací údaje a dotaz (`display_url`). Vložený klíč se zadává přes `getpass`. První osmice znaků nového klíče se nevypisuje.

**Závěrečný přehled** (jediné místo, kde tajnosti smí být):

| Situace | Přehled |
|---|---|
| úspěšný první běh v interaktivním terminálu (stdin i stdout jsou TTY, bez `CI` a obdobných proměnných, bez `--no-input`) | cesta k `.env`, jména vytvořených hodnot a **jednorázový blok** s jejich hodnotami na úplném konci |
| přesměrovaný stdout nebo stdin, CI (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`, `TF_BUILD`), `--no-input`, běh s chybami | jen cesta k `.env` a jména vytvořených hodnot |
| opakovaný běh | jen cesta; nic z toho, co už existovalo, se nevypíše |

V bloku mohou být **jen hodnoty, které vytvořil tento běh**: `POSTGRES_PASSWORD`, `PGADMIN_DEFAULT_PASSWORD`, `LANGFLOW_SUPERUSER_PASSWORD`, `LANGFLOW_SECRET_KEY` (pokud byly `CHANGE_ME` a vygenerovaly se) a `LANGFLOW_API_KEY` po vytvoření nebo výměně.
Nikdy: dodané externí hodnoty (`OPENAI_API_KEY`, token Cloudflare, `JINA_API_KEY`), vložený klíč, cokoli, co `.env` obsahovalo dřív. Provider klíče Bifrostu v `.env` nejsou vůbec. Rozhodnutí, zda blok vypsat, je uvnitř `step_summary` (`interactive_terminal`).
Hodnoty se při zobrazení **znovu čtou z `.env`**; `.env` zůstává trvalým zdrojem. Detekce CI se liší od `install.py` (ten zná 13 proměnných); obě se zachovávají podle potvrzeného rozhodnutí vlastníka, srovnání je v [návrhu, oddíl 10.5](b06-clean-install-proposal.md#10-stav-implementace-b06-1-a-bootstrapu-kandidát).

## 4. Ověření (offline)

Syntetický `.env` v dočasném adresáři, mockované HTTP (náhrada `OpenerDirector.open` a `urlopen`), zákaz `subprocess`, soketů a přístupu ke skutečnému `.env` (porušení shodí test). Pro přesměrování fake transport pod skutečnou přesměrovací mechanikou `urllib`
(kontrolní test dokazuje, že běžný opener klíč přeposílá). Syntetická hesla a klíče se hledají i s osmiznakovou předponou ve stdout i stderr. `.env` má smíšené konce řádků a chybí mu poslední nový řádek.

| Oblast | Pokrytí |
|---|---|
| Klasifikace | platný (objekt, pole), 401 (odmítnutý), 403 (neověřitelný), 8 stavových kódů 4xx/5xx, všech 5 přesměrování, timeouty, odmítnuté/přerušené spojení, DNS, TLS, odpojení, neúplné čtení, 201, 204, prázdné/HTML/useknuté/`null`/řetězcové/číselné/binární tělo, poškozený gzip, neznámá výjimka, klíč, který nejde poslat |
| Neověřitelný stav (403 včetně) | `.env` bajtově i časem stejný, žádný požadavek mimo jediný `GET` (žádný login, výpis, vytvoření, mazání), žádná výzva, kód 3; na úrovni funkce, CLI, `step_api_key` i `main()`; 401 dál vymění, `--force` zůstává |
| Životní cyklus | pořadí volání; selhání loginu, vytvoření, ověření (401, 403, 500, timeout, HTML) a zápisu; selhání čtení zpět a **skutečný SIGINT těsně po i těsně před `os.replace`** (`signal.raise_signal`): po zápisu se nic nemaže a stav se přizná, před zápisem se nový klíč smaže jen při shodných bajtech; čerstvé čtení, které selže nebo ukáže třetí hodnotu, je „neznámo“; smazání nového klíče podle `id` i podle rozdílu dvou úspěšných výpisů, nikdy hádání; **žádný starý klíč se nemaže v žádném tvaru výpisu** (dvě shodné předpony, jediná shoda, jiné jméno, chybí, nesrovnatelná maska, selhaný výpis); selhaný výpis není prázdný; `--force`; ruční klíč |
| `.env` | všech pět selhání zápisu, symlink, mód, umask, vytvoření bez přepsání, duplicitní řádek, `step_env` |
| Výstup a přehled | `redact` (nečitelný `.env`), `relay` (víceřádková těla, řídicí znaky, URL), `step_summary` (interaktivně × přesměrováno × CI × `--no-input` × běh s chybami × opakovaný běh), `main()` |
| Volající a kód 3 | `env_path` bez defaultu a ve všech voláních, `bootstrap.main()` vrací 3 a nespustí další kroky, `--help` |

Výsledky po závěrečné revizi Codex: viz záznam revizí a [návrh, oddíl 10.6](b06-clean-install-proposal.md#10-stav-implementace-b06-1-a-bootstrapu-kandidát). Dřívější kolo: Windows 127 testů OK, 11 přeskočeno, WSL 127 OK; cílená mutační kontrola nových chování 23 z 23 zachyceno
(sdílená s B06-1, viz [návrh, oddíl 10.6](b06-clean-install-proposal.md#10-stav-implementace-b06-1-a-bootstrapu-kandidát)). Původní kandidát hlásil 57 testů a 18 mutantů; to jsou starší čísla.

## 5. Omezení a co ověřeno není

- Offline testy jsou mocky. Proti skutečnému Langflow 1.10.1 se v laboratoři (2026-10-06, oddíl 5 záznamu revizí) potvrdilo: platný klíč dostane na `GET /api/v1/flows/?page=1&size=1` `200` (gzip, JSON list), neplatný **403**, odpověď na vytvoření nese `id` (UUID jako řetězec), výpis je maskovaný (`sk-` a 5 znaků, zbytek `*`). **Neověřeno zůstává:** proxy před Langflow, PostgreSQL, skutečný průchod `bootstrap.py`, práh omezení loginu (429).
- 401 neprokáže, že odpověděl Langflow (stejně by vypadl front-end proxy). Mírní to pořadí: nový klíč musí být přijat stejným požadavkem a uložen a stará hodnota se nemaže nikdy.
- Přerušení mezi návratem `create_key` a vstupem do `try` kolem `check_key` (a druhé Ctrl-C během posouzení nebo mazání) ztratí `id` a klíč `edu_ai-bootstrap` zůstane v Langflow bez hlášení; okno je několik bajtkódů.
- Po `KeyStateUncertain` ve stavu „uloženo“ kontrola při dalším běhu vyjde „platný“ a krok se přeskočí; kontejnery klíč přečtou až po `docker compose up -d` (zpráva to říká). „Uloženo“ se pozná podřetězcem hodnoty v bajtech, ne parsováním řádku (falešná shoda znamená jen, že se nic nesmaže).
- Hodnoty vytvořené během, který pak skončil chybou, se **už nikdy nenabídnou** (při opakování už existují); zůstávají v `.env`. Důsledek pravidla „existující hodnoty nevypisovat".
- Jednorázový blok skončí v historii terminálu operátora; skript to jen připomíná. `redact()` zná jen hodnoty, které `.env` označuje názvem. Výstup `docker compose up` jde přímo na terminál mimo `redact()`.
- Po `replaced` se pro všechny služby spouští `up -d --force-recreate`; mezi souběžnými běhy není zámek; `check_key` nemá celkový termín ani limit velikosti odpovědi. Přesměrování HTTPS→HTTP, proxy přes `HTTP(S)_PROXY` a souběh dvou běhů se nezkoušely.
- Skutečný průchod `bootstrap.py` (Docker, živý Langflow) neproběhl a tento dokument ho netvrdí.

## 6. Nálezy N1–N9: stav

| # | Stav | Poznámka |
|---|---|---|
| N1 | část otevřena | výstup, který bootstrap předává, je uzavřen (`relay`, `redact`); samostatné `import_flows.py` (`:84`) a `smoke.py` dál tisknou těla odpovědí a payloady: samostatný úkol |
| N2 | uzavřeno | atomický zápis `.env` (`write_atomic`) |
| N3 | uzavřeno na Linuxu | `.env` 0600 od vytvoření; Windows se netvrdí |
| N4 | uzavřeno, po revizi Codex změněno | pořadí výměny; starý klíč se nemaže nikdy; nový se maže jen podle `id` nebo při dvou úspěšných výpisech; po možném zápisu `.env` se nemaže nic a stav se přizná (oddíl 2) |
| N5 | uzavřeno | `http_safe` ve všech čtyřech skriptech s autentizovaným voláním |
| N6 | uzavřeno | zprávy o `/health` opraveny; test hlídá |
| N7 | uzavřeno | `ONBOARDING.md` §1 a §5, návrh B06 oddíl 2 a 10 |
| N8 | uzavřeno | `env_path` bez defaultu |
| N9 | otestováno | kód 3 se propaguje (`bootstrap.main()`, CLI, `--help`); obal B06-2 nevznikl, mapování kódů je v [návrhu, oddíl 10.1](b06-clean-install-proposal.md#10-stav-implementace-b06-1-a-bootstrapu-kandidát) |

## 7. Jak kandidáta začlenit

Závěrečná revize kódu, rozhodnutí o publikaci a laboratorní ověření API klíčů proti Langflow 1.10.1 proběhly (2026-10-06); kandidát je publikováno na `origin/clean_init` (kód a testy v commitu `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, dokumentace v následujícím commitu; jeho SHA nelze uvést uvnitř něj, viz `git log` těchto souborů). Zbývá celý bootstrap proti stacku, PostgreSQL a B06-2 ([návrh, oddíl 10.8](b06-clean-install-proposal.md#108-další-krok)).
Nic z tohoto dokumentu není schválení nasazení ani podporované aktualizace školy. Pravidla [AGENTS.md](../../AGENTS.md) (žádné tajnosti v logu, žádná ruční úprava generovaných souborů) kandidát dodržuje; kontrakty, schéma ani Compose se nemění.
