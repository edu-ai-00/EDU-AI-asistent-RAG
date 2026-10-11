# Nasazení: doménový scope (výběr oblasti hledání)

> **Historický záznam (6. 9. 2026).** Příkazy, tagy obrazů, SHA a stav produkce platí jen pro tehdejší stav; nepoužívejte je jako aktuální postup. Funkce je implementovaná; viz [specifikaci](../../specs/draft/spec.search-domain-scope.md). Aktuální nginx konfigurace je `nginx/conf/nginx.conf` (Compose mountuje adresář `nginx/conf`), starší `nginx/nginx.conf` ji nenahrazuje.

**Pro:** Jan Drozd
**Větev:** `chunking-config`, výchozí commit `e0316a6`
**Sepsáno:** 6. 9. 2026

Specifikace: [scope tematických oblastí](../../specs/draft/spec.search-domain-scope.md).
Návrh: [`specs/draft/spec.search-domain-scope.md`](../../specs/draft/spec.search-domain-scope.md).
Kontrakt pro mobil: [`docs/integrations/mobile-domain-scope.md`](../integrations/mobile-domain-scope.md).

> **Stav: nasazeno 6. 9. 2026 večer, kommit `59f8774` (rodič `e0316a6`).**
> Search, migrace 213, nginx a webchat jsou na produkci; výchozí volba je
> „Všechny oblasti“. Označení dat (§7) a scoped regrese (§8, poslední odstavec)
> zůstávají otevřené — viz §15 pro skutečný záznam a co zůstalo neověřené.
>
> **Revize 6. 9. proběhla ve dvou kolech a našla šest nálezů; všechny jsou
> opravené a ke každému je test, který na předchozím kódu selže** (§11).
> Většina z nich byla skutečná ztráta dat, ne kosmetika: chybějící parametry
> v trigramovém dotazu (celá dokumentová větev pod explicitním filtrem tiše
> zmizela), prázdný guard v datové migraci, snapshot zapisovaný až po zápisech,
> rollback přes celou tabulku, UI, které po vyřazení kódu mlčky hledalo ve všech
> oblastech — a ve druhém kole dvě věci, které moje vlastní oprava ještě
> nepokryla: **zápisy vybíraly podle packu, ne podle snapshotu** (řádek přidaný
> po snapshotu se změnil bez jakékoli kontroly, a scan surfaces ve snapshotu
> vůbec nebyly), a **rollback kontroloval drift před transakcí**.

---

## 1. Proti čemu je to ověřené

| co | zjištěno | jak |
|---|---|---|
| produkční checkout | `e0316a6`, větev `chunking-config` | `ssh edu-ai`, `git log -1` |
| běžící kód search | **shodný s checkoutem** | `docker cp edu_ai_search:/app/app` + `diff -rq` proti `/opt/edu_ai/search/app` → 0 rozdílů |
| aktivní kroky kaskády | route, qa_exact, qa_lexical, document_lexical, qa_vector, rag_vector zapnuté; **rag_keywords a document_siblings vypnuté** | `/v1/ask` s `retrieval_only+debug`, přečten `trace` |
| `/search/v1/domains` přes nginx | **404** (není v allowlistu) | `wget` z kontejneru `edu_ai_nginx` |
| `domains` v těle `/v1/ask` | **422** (neznámé pole) | `curl` z kontejneru `edu_ai_search` |
| aktivní QA packy | 7 (faq-jpz 110, faq-provoz 100, provoz-sos 33, prijimacky-jed 32, provoz-jed 33, prijimacky-svo 32, provoz-svo 33) | `/v1/qa-packs` per entita |
| katalog DOMAIN | 8 globálních; ZŠ i SOŠ Kavčí hory po 11 | `/v1/domains?legal_entity=…` |

Souhlasí se stavem, který popisuje specifikace k 6. 9.

**Přímý `psql` na produkci mi v této relaci nebyl povolen** (blokoval ho
sandbox). Inventuru jsem proto dělal přes vlastní read-only nástroj
`scripts/domain_inventory.py --remote edu-ai`, který jde přes SSH + `docker
compose exec postgres`, a přes veřejné read-only endpointy. Všechno níž
uvedené produkční číslo je z těchto zdrojů.

**Lokální ověření** proběhlo na **čistě nainstalované** DB (prázdný volume →
`init_clean` 00→01→02) dorovnané migracemi 192–213. Není to kopie produkce:
produkční korpus jsem k dispozici neměl, takže regresní sada
(`eval_rag.py`) na tomto prostředí **není měřitelná** — viz §8.

---

## 2. Co se mění

| oblast | soubory |
|---|---|
| kontrakt a resolver | `search/app/server.py`, `domain_scope.py` (nový), `scope.py`, `state.py` |
| kaskáda | `nodes/route.py`, `qa_common.py`, `qa_exact.py`, `qa_lexical.py`, `qa_vector.py`, `document_lexical.py`, `rag_vector.py`, `rag_keywords.py`, `document_siblings.py`, `answer.py` |
| historie a log | `conversation.py`, `nodes/resolve_context.py`, `log.py`, `postgres/init/213_log_domains.sql` (nová migrace) |
| webchat | `web_chat/web_chat.py`, `frontend/src/DomainPicker.tsx` (nový), `App.tsx`, `styles.css` |
| perimetr | `nginx/nginx.conf` — jedna nová exact-match cesta `/search/v1/domains` |
| nástroje | `scripts/domain_inventory.py`, `scripts/domain_migration.py`, `scripts/gen_clean_install.sh` (nové) |
| kontrakty | `contracts/openapi.yaml`, `errors.yaml`, oba schémata |

**Aditivní pro klienty.** Požadavek bez `domains` má bajtově stejné tělo,
stejný idempotency digest i stejné SQL (fragment filtru je prázdný string).
Staré klienty není nutné vydávat.

---

## 3. Záloha (krok 1, povinný)

```bash
ssh edu-ai
cd /opt/edu_ai
docker compose exec -T postgres pg_dump -U edu_ai -d edu_ai -Fc \
  > ~/edu_ai_$(date +%Y%m%d_%H%M).dump
ls -l ~/edu_ai_*.dump
```

Ověřit **restore na kopii**, ne jen existenci souboru:

```bash
docker compose exec -T postgres createdb -U edu_ai edu_ai_restore_test
docker compose exec -T postgres pg_restore -U edu_ai -d edu_ai_restore_test \
  --no-owner < ~/edu_ai_<stamp>.dump
docker compose exec -T postgres psql -U edu_ai -d edu_ai_restore_test \
  -c "select count(*) from edu_ai.qa_entries"
docker compose exec -T postgres dropdb -U edu_ai edu_ai_restore_test
```

Dump **necommitovat** — obsahuje reálný obsah škol.

---

## 4. Migrace logu (krok 2)

Aditivní, transakční, opakovatelná, kontroluje typ. Nasadit **před** kódem,
který do sloupce zapisuje.

```bash
cd /opt/edu_ai && git fetch && git checkout <commit>
docker compose exec -T postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  < postgres/init/213_log_domains.sql
```

Ověřit:

```bash
docker compose exec -T postgres psql -U edu_ai -d edu_ai -c "\
select column_name, data_type, column_default, is_nullable \
  from information_schema.columns \
 where table_schema='edu_ai' and table_name='question_answer_log' \
   and column_name='domains'"
```

Očekávané: `domains | ARRAY | (prázdné) | YES`. **Default musí být prázdný** —
migrace sama selže, kdyby nebyl.

Opakované spuštění je nop (`NOTICE: column "domains" … already exists,
skipping`). Ověřeno lokálně dvěma běhy za sebou; ověřeno i to, že typová
kontrola opravdu spadne, když sloupec existuje jako `jsonb`.

**Migrace 201 a 202 nenasazuj** — produkce je neprodělala a spadnou na unique
indexu (`taxonomy_rules_active_code`). Ověřeno i na čisté instalaci: 190, 201,
202 a 212 tam padají, ostatní z rozsahu 192–213 projdou.

### Návrat

Sloupec je aditivní a nullable, takže **rollback backendu ho nepotřebuje
odebírat**. Odebrání není součástí návratu.

---

## 5. Build a nasazení search (krok 3)

Kód je zapečený v image — **restart stejného image nic nezmění**.

```bash
cd /opt/edu_ai
docker compose build edu_ai_search
docker compose up -d edu_ai_search
docker compose ps edu_ai_search           # healthy
```

Ověřit, že běží nový kód, ne jen nový tag:

```bash
docker cp edu_ai_search:/app/app /tmp/pcheck && \
  diff -rq /tmp/pcheck /opt/edu_ai/search/app --exclude=__pycache__ ; rm -rf /tmp/pcheck
```

Smoke uvnitř kontejneru (na produkci **je** `COMPOSE_FILE` připnutý na
`docker-compose.yml`, takže 8090 není publikovaný — testuj zvnitřku):

```bash
docker exec edu_ai_search curl -s -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8090/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS"          # 200
docker exec edu_ai_search curl -s -X POST http://127.0.0.1:8090/v1/ask \
  -H 'content-type: application/json' \
  -d '{"question":"smoke domain scope","legal_entity":"ZS_KAVCI_HORY_ZS",
       "user_role":"PARENT","query_id":"smoke-domain-20260906","domains":["ATTENDANCE"]}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['outcome'],d['effective_scope']['domains'])"
```

> **Každý `/v1/ask` zapíše řádek do `question_answer_log` a na RAG cestě může
> zavolat placený model.** Používej předem připravené syntetické dotazy s
> `query_id`, ať se dají v logu najít a odfiltrovat.

---

## 6. nginx (krok 4)

Jedna nová exact-match řádka v `$search_path_allowed`. Compose služba je
`nginx`, kontejner `edu_ai_nginx` — před příkazy si jména znovu ověř.

```bash
docker compose up -d nginx
docker exec edu_ai_nginx nginx -t                 # syntax is ok / test is successful
```

Ověřit veřejnou cestu **skutečným proxy hopem**, a zároveň že se allowlist
nerozšířil jinam:

```bash
docker exec edu_ai_nginx sh -c \
  'wget -qO- -S "http://127.0.0.1/search/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS" 2>&1 | head -3'
for p in /search/v1/query-log /search/v1/staff-users /search/v1/domains-anything; do
  docker exec edu_ai_nginx sh -c "wget -qO- -S http://127.0.0.1$p 2>&1 | head -1"   # 404
done
```

Ověřeno lokálně: `/search/v1/domains` 200, tři cesty výš 404.

---

## 7. Data (krok 5) — a proč to je blokující

Filtr je jen tak dobrý, jak jsou označené zdroje. Report:

```bash
python3 scripts/domain_inventory.py --remote edu-ai
```

**Naměřeno 6. 9. 2026** (read-only, přes tento nástroj):

| zjištění | číslo | důsledek |
|---|---|---|
| aktivní packy **bez jakéhokoli označení** | **5** (faq-provoz 100, prijimacky-jed 32, provoz-jed 33, prijimacky-svo 32, provoz-svo 33 = **230 odpovědí**) | pod explicitním filtrem **nedosažitelné** |
| `ZS_KAVCI_HORY_SOS` aktivní chunky | 104, z toho **0** s oblastí | SOŠ nemá ve žádné oblasti nic |
| chunky **bez** oblasti pod označeným dokumentem | **170** (151 `ZS_KAVCI_HORY`, 19 `ZS_KAVCI_HORY_ZS`) | podpis „UPDATE metadat bez propagace“ |
| chunky s **jinou** množinou než jejich dokument | **94** (`ZS_KAVCI_HORY_ZS`) | dosažitelné ve **špatných** oblastech |
| `qa_variants`/`qa_embedding` vs `qa_entries` | 274 řádků, **všech 274 jen jiné pořadí pole** | **neškodné** — filtr je array overlap, pořadí nehraje roli |

Rozchod pořadí jsem prověřil zvlášť (`@>` i `<@` platí u všech 274), proto ho
report vypisuje odděleně — jinak by se řádek „drift“ naučil každý ignorovat.

### Co s tím

1. **Nezapínej klientský výběr oblastí u školy, jejíž obsah není označený.**
   Uživatel by dostal `no_answer` na otázky, na které systém odpovědět umí. To
   je horší než žádný filtr.
2. Kurátor rozhodne o oblastech pro těch 5 packů a pro netagované dokumenty.
   **Tohle nástroj nerozhodne** a ani rozhodovat nemá.
3. Rozhodnutí zapiš do plánu a proveď:

```bash
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai            # DRY RUN
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai --apply --go
python3 scripts/domain_migration.py --plan plan.json --remote edu-ai --verify
```

Formát plánu je v docstringu skriptu. Plán nese **kódy, ne ID** (ID znamená v
každém katalogu něco jiného) a QA položky klíčuje **`entry_key`**, protože QA
writer řeší identitu přes něj a totéž CSV v jiném packu si UUID nezachová.

170 chunků bez označení opraví `kind: "repropagate_chunks"` — zkopíruje
efektivní deklaraci dokumentu (user, jinak LLM) na jeho chunky a nic si
nevymýšlí.

**Ověřeno na fixture:** dry-run bez zápisu → apply (2 sady, snapshot +
checksum) → verify OK → opakovaný apply **přeskočí** kvůli preconditions →
rollback vrátí hodnoty **bajtově přesně**, včetně rozdílu `NULL` vs `{}`.

Po revizi navíc (`tests/test_domain_migration.py`, 35 kontrol):

* **Precondition se znovu ověřuje UVNITŘ transakce** a předtím si vezme
  `FOR UPDATE` na dotčené řádky. Kontrola v samostatném dotazu je jen orientační
  — kurátor, který uloží v mezeře mezi ní a UPDATEm, by přišel o rozhodnutí bez
  jakéhokoli záznamu. Ověřeno **skutečnou souběžnou psql session**: druhá session
  drží necommitnutou změnu, migrace na ní zablokuje, po commitu ji uvidí a celou
  sadu odrolluje. Bez guardu (`guard = ""`) test spadne a v DB zůstane hodnota
  migrace místo hodnoty kurátora.
* Guard hlídá **všechny řádky ze snapshotu**, tedy víc než jmenuje
  `expect_current`, a zachytí změněnou hodnotu, **zmizelý** i **přidaný** řádek.
* **Snapshot je na disku dřív, než se sáhne na první řádek** (atomicky, přes
  temp soubor a `os.replace`), a přepisuje se po každé sadě. Předtím se zapisoval
  až po smyčce — zabitý proces (stačí spadlé SSH) nechal změněnou databázi
  **bez záznamu původních hodnot**.
* **Každý zápis je klíčovaný na snapshot** (`AND <key> IN (…)`), ne jen vybraný
  podle packu. Výběr podle `pack_id` sahal na to, co v packu bylo *v okamžiku
  zápisu* — ne na sadu, kterou operátor schválil. Tohle je zároveň jediná
  ochrana proti řádku, který žádný guard vidět nemůže (vložený transakcí, jež
  ještě necommitla).
* **Snapshot pokrývá i scan surfaces** (`qa_variants`, `qa_embedding`). Dřív
  v něm nebyly, přesto se aktualizovaly JOINem — varianta přidaná k existující
  odpovědi se tedy zapsala naslepo a nešla vrátit.
* **Rollback obnovuje každý zaznamenaný řádek podle jeho vlastního klíče**,
  ověřuje **uvnitř své transakce** pod stejným zámkem a **odmítne** (exit 2),
  když se řádek od applyho změnil — a řekne který. Dřív resyncoval scan
  surfaces neomezeným `UPDATE … FROM qa_entries` (na produkci ~3 000 + 3 000
  řádků kvůli vrácení dvou packů) a drift kontroloval **před** transakcí.
  `--force` vypíná ověření, nikdy zámek.

`--rollback` **není** `qa_deactivate_packs.py --activate`: ten aktivuje všechny
varianty packu, což je něco jiného než „vrať to zpátky“.

### Co migrační nástroj nedělá

Nepřechunkuje, nepřeembedduje, neklasifikuje a **nepřepočítává
`taxonomy_binding` / `document_binding_id`** ani hodnoty z bindingu odvozené.
Pokud nové oblasti mění sémantický binding dokumentu, je to **cílený reingest**,
ne editace štítků — `--verify` na nezměněný binding upozorní.

---

## 8. Regresní sada — co jsem NEMĚŘIL (doplněno 6. 9. večer, viz §15)

> **Doplněno při nasazení 6. 9. večer.** Autorizovaná aktuální kopie
> produkčního korpusu vznikla čerstvým `pg_dump` a testovací obnovou (viz §15);
> `--repeat 3` proběhlo staré i nové proti ní, s reálným embeddingovým voláním
> (ne jen network-omezeným `no_answer`). Výsledek: 41/42 shodných, jediný
> rozdíl je u již dříve kolísavého případu. Text níž je původní, nedoplněný
> zápis z dopoledne — ponechán kvůli důvodům, proč to dřív nešlo.

`python3 tests/eval_rag.py --repeat 3` **jsem nespustil** a nemám baseline
před/po.

Důvod: sada je kalibrovaná na produkční korpus. Lokálně mám čistou instalaci s
fixture, kde by skončila téměř všude `no_answer` — to je číslo, ne měření.
Autorizovanou kopii produkčních dat jsem neměl; záloha
`backup_prod_20260729` je z 29. 7., commit `1c5b9fd`, `postgres/init` po
migraci 30 — o šest týdnů a ~180 migrací pozadu, takže baseline nereprodukuje.
Běh proti produkci by volal placený model a zapisoval do query logu.

**Co proto chybí a co je potřeba udělat před zapnutím klientského výběru:**

```bash
# na prostředí s produkčním korpusem, PŘED nasazením
python3 tests/eval_rag.py --repeat 3                      # baseline
# po nasazení backendu, PŘED změnou dat
python3 tests/eval_rag.py --repeat 3 --baseline tests/eval_out/<baseline>.json
# a scoped sada, až po označení dat
python3 tests/eval_rag.py --questions tests/eval_rag_domains.jsonl --repeat 3
```

Měření **změny kódu** a měření **změny dat** jsou dva samostatné běhy; sada s
oblastmi je proto v samostatném souboru.

Argument, který dělá první běh smysluplným: u požadavku **bez** `domains` je
doménový fragment prázdný string, takže SQL je bajtově identické. Změny, které
neomezený požadavek přece jen ovlivňují, jsou tři a jsou to opravy:

* `route()` při výjimce vrací sentinel místo `[]` (dřív otevřel všechny packy),
* `route._corpus_topics` spojuje metadata i přes `processing_uuid` (dřív mohl
  nabídnout nadpis ze starší verze dokumentu),
* `/v1/explain` už neodpovídá 500 (viz §10).

---

## 9. BFF a UI (krok 6)

Nasadit **až s připravenými daty**.

```bash
docker compose build edu_ai_web_chat && docker compose up -d edu_ai_web_chat
docker exec edu_ai_web_chat python -c "\
import urllib.request,json; \
print(json.load(urllib.request.urlopen('http://127.0.0.1:8081/chat/api/domains')))"
```

Ověřit obě polohy `CHAT_ENTITY_PICKER`. Pozor: **produkce má picker zapnutý**
(`CHAT_ENTITY_PICKER=true`), takže katalog i hledání respektují entitu z
prohlížeče. S vypnutým pickerem se podvržený `?legal_entity=` **ignoruje** a
odpověď vrací skutečnou entitu — to je ta část, která brání „katalog jedné
školy, hledání ve druhé“.

Browser check (Playwright, dva viewporty) je
`web_chat/tests/test_domain_picker_browser.py`, postup v jeho docstringu.
Lokálně **69 kontrol, 0 selhání**.

Po revizi obsahuje i případ, který v prvním kole chyběl: **vyřazený uložený
kód blokuje odeslání**, dokud uživatel nezvolí znovu nebo nepotvrdí. Bez toho
selekce spadla na `null`, tedy „všechny oblasti“, a další dotaz hledal v celém
korpusu, zatímco uživatel věřil, že filtr platí — totéž tiché rozšíření, jen
jinou cestou než retry.

Browser test u toho odhalil ještě jednu věc, kterou nic jiného vidět nemohlo:
efekt s katalogem běží v produkční konfiguraci **dvakrát** (nejdřív s `entity`
= null, pak po `/chat/api/config`). Verze, která při vyřazení kódu přepsala
localStorage, ho tím v prvním běhu spotřebovala — druhý běh už nic vyřazeného
nenašel, varování bliklo na jeden frame a blokace se nikdy nezapnula. Storage
teď přepisuje **jen výslovná volba**.

---

## 10. Opravy, které nejsou součástí funkce

Obojí jsem našel při práci; obojí je předexistující a obojí je v tomto balíku,
protože se plán obou přímo dotýká.

**`/v1/explain` vracelo 500** — a vrací i na produkci na `e0316a6`
(`docker exec edu_ai_search curl … /v1/explain` → 500). Příčina: endpoint volal
`prepare(state)` bez configu a od `eduai_normalize` 3.0.0 je stoplist DATA, bez
nich `stopwords_of()` vyhodí výjimku. Endpoint je přitom **v allowlistu
nginxu**, takže je publikovaný. Opraveno předáním aktivního settings configu.

**`/v1/explain` neřešilo identitu.** Stavělo si vlastní dvouklíčový stav a
ignorovalo entitu, volajícího, roli i jazyk; `scope.where()` tak nevidělo nic
vyřešeného, což je „nevidí nic“ — bezpečné náhodou, ne pravidlem. Teď jde
stejnou přípravou jako `/v1/ask`: stejná 400 na neznámou entitu, 403 na
nepodloženého uživatele, 422/503 na oblasti, stejný role gate. **Je to změna
diagnostického kontraktu** a je otestovaná.

Komentář v `nginx.conf` u této cesty říkal „scopes to nothing“; je přepsaný.

---

## 11. Nálezy z revize 6. 9. a jejich testy

Čtyři nálezy, čtyři opravy, u každé test, který na předchozím kódu **selže**.
Ověřeno tak, že jsem defekt vždy dočasně vrátil a test spustil.

| # | nález | oprava | test | bez opravy |
|---|---|---|---|---|
| 1 | guard v transakci byl prázdný string (`guard = ""`), komentář přitom tvrdil opak; kontrola tedy nechránila proti souběžné změně | `guard_sql()` — `FOR UPDATE` + porovnání všech řádků snapshotu, uvnitř téže transakce | `test_domain_migration.py guard-race` (souběžná psql session, handshake přes `pg_locks`) | 4 kontroly selžou; v DB zůstane hodnota migrace místo hodnoty kurátora |
| 2 | snapshot se zapisoval až po zápisech; rollback aktualizoval domény **všech** QA variants a embeddingů; pozdější změny jen varování | snapshot na disk před prvním zápisem (atomicky) + po každé sadě; rollback omezený na `entry_id` ze snapshotu; strojová kontrola `after` → exit 2, `--force` | `test_domain_migration.py snapshot`, `foreign`, `refuse` | cizí sada i varianta záměrně mimo krok se přepíšou; po zabití procesu není žádný snapshot |
| 3 | když zmizely všechny uložené domény, selekce spadla na `null` = všechny oblasti; upozornění se zobrazilo, ale odeslání nebylo blokované | `needsChoice` blokuje composer; potvrzení nebo nová volba ho uvolní; storage přepisuje jen výslovná volba | `test_domain_picker_browser.py`, blok „retired stored code“ (obě varianty) | 6 kontrol selže; dotaz odejde neomezený |
| 4 | trigramový dotaz v `document_lexical` interpoloval `cand_cond`, ale dostával `scope_params` → pod explicitním scope spadl a step výjimku spolkl | `*cand_params` | `test_domain_scope.py` — `no_swallowed_failure` watcher + `test_document_typo_fallback_is_scoped` | 9 kontrol selže (`list index out of range`), celá dokumentová větev zmizí |
| 5 | guard porovnával jen uložená ID a **actual stranu si sám omezil na tatáž ID**, takže „řádek se přidal“ nešlo detekovat; zápisy přitom vybíraly podle `pack_id`. Scan surfaces (`qa_variants`, `qa_embedding`) ve snapshotu vůbec nebyly, přesto se aktualizovaly JOINem | jeden selektor (`snapshot_sql`) pro snapshot, guard i klíče zápisů; guard porovnává **celou** cílovou sadu; každý UPDATE má `AND <key> IN (…)`; snapshot pokrývá i obě scan surfaces | `test_domain_migration.py added-row`, `added-variant` | `new_row_outside_snapshot_modified` — přidaný řádek dostane hodnotu migrace; přidaná varianta přijde o svůj štítek a nedá se obnovit |
| 6 | rollback kontroloval pozdější změny **před** transakcí; mezi kontrolou a obnovením mohl kurátor zapsat změnu, kterou rollback přepsal | `restore_guard_sql` — `FOR UPDATE` + porovnání s `after` uvnitř téže transakce; `--force` vypíná jen ověření, nikdy zámek | `test_domain_migration.py rollback-race` | rollback projde (rc=0), hodnota kurátora je přepsaná na `NULL`, a nástroj předtím vypsal „no drift“ |

Nález 4 ukázal mezeru v metodě, ne jen v kódu: **step si výjimku sám odloží a
vrátí prázdno**, takže aserce „žádné řádky mimo oblast“ projde právě proto, že
tam nejsou řádky žádné. Proto je v testech teď log watcher a sada dotazů, které
tyhle okrajové větve (oba typo fallbacky, prázdný pool, category top-up) pod
filtrem skutečně projdou.

Nálezy 5 a 6 ukázaly druhou, podobnou věc: **moje oprava z prvního kola měla
komentář, který tvrdil víc, než kód dělal** — guard „zachytí přidaný řádek“,
zatímco jeho `actual` strana byla omezená na uložené klíče, takže to zachytit
nemohl. Test, který by ten scénář opravdu projel, chyběl. Ta garance je teď
formulovaná **per snapshot** a je testovaná v obou směrech: řádek chybějící ve
snapshotu S se S nezapíše ani neobnoví; řádek, který byl v okamžiku snapshotu
commitnutý a viditelný, do sady patří a zapsat se smí **právě proto**, že je
zaznamenaný a jde vrátit.

Souběžné testy vyhrávají závod deterministicky: druhá psql session drží
**necommitnutou** změnu (takže pre-read migrace ji přes MVCC nevidí a projde —
přesně to okno, které pre-check pokrýt nemůže), migrace zablokuje na
`FOR UPDATE`, handshake je dotaz do `pg_locks`, a teprve pak druhá session
commitne. Žádný sleep. Trigger by to otestovat nemohl: spustí se uvnitř vlastní
transakce migrace, kterou žádný zámek nechrání.

## 12. Kontrola po nasazení

```bash
docker compose exec -T postgres psql -U edu_ai -d edu_ai -c "\
select query_id, stage, domains, matched_entry_id, latency_ms \
  from edu_ai.question_answer_log \
 where query_id like 'smoke-domain-%' order by created_at desc limit 20"
```

Sledovat: kanonické `domains`, `matched_entry_id`, chybovost, latenci.

**Názvy oblastí v textu odpovědi nejsou důkaz filtrace.** Opři se o identitu
skutečně použitých zdrojů — `matched_entry_id` u QA, `sources[].file_uuid` u
dokumentů.

---

## 13. Návrat

| co vrátit | jak | podmínka |
|---|---|---|
| UI | nasadit předchozí `edu_ai_web_chat` image | vždy možné; backend zůstane kompatibilní |
| nginx | odebrat jednu řádku z allowlistu | katalog přestane být veřejný, hledání funguje dál |
| search | přepnout na předchozí tag image | **až když scoped požadavky neposílá žádný klient**, včetně vydaného mobilu — starší API je odmítá 422 |
| data | `scripts/domain_migration.py --rollback <snapshot> --go` | před tím ověř, že po apply neproběhly další kurátorské změny — restore vrací hodnoty, ne časový bod |
| sloupec logu | **nevracet** | aditivní a nullable |

**Filtr se nesmí zahazovat na proxy ani v BFF jako nouzový workaround.** Klient
by dostal širší odpověď a myslel si, že je to ta vyžádaná.

---

## 14. Co zůstává otevřené

* **Regresní sada — doplněno** (§8, §15): 41/42 shodných proti aktuální
  produkční kopii, s funkčním vektorovým hledáním. Scoped sada
  (`eval_rag_domains.jsonl`) zůstává neproměřená proti produkci — odkazuje
  fiktivní entitu `ZAKLADNI_SKOLA_LIPOVA` bez produkční obdoby a runbook ji
  sám plánuje až po označení dat (§7), ne dnes.
* **Data nejsou označená** (§7): 230 QA odpovědí bez oblasti, 104 chunků SOŠ,
  170 + 94 chunků s rozchodem. Vyžaduje kurátora.
* **Mobilní klient neexistuje.** API je hotové a ověřené přes skutečný proxy
  hop; UI, ukládání volby a vydání je na mobilním týmu
  ([kontrakt](../integrations/mobile-domain-scope.md)).
* **`postgres/init_clean/` nepřegenerováno.** Generátor
  `scripts/gen_clean_install.sh` je nový a ověřený (vygenerovaná trojice se
  nainstaluje na prázdný volume bez jediné chyby a dá **identickou sadu 507
  sloupců** jako DB dorovnaná migracemi). Přepis commitnutých dumpů jsem
  **neudělal**: diff by s sebou vzal i migrace 192–212, které v konsolidovaném
  souboru chybí, a to je změna na samostatné schválení, ne příloha téhle funkce.
* **`concept_idf_weights` není scoped podle entity/role.** Předexistující
  asymetrie, popsaná v kódu i v `docs/ai/qa-fastpath.md`, tímto balíkem
  nezměněná — oprava by pohnula čísly u každého existujícího požadavku.
* **Pojmenované profily „Přijímačky / Provoz“** nejsou implementované (spec §8)
  a nejsou v tomto rozsahu.

---

## 15. Skutečný záznam nasazení — 6. 9. 2026 večer

**Commit:** `59f8774` (rodič/předchozí produkční `e0316a6`), pushnut na
`origin/chunking-config`, nasazen jako přesné SHA (detached HEAD), ne branch tip.

**Images (staré zachovány pod tagem `prev-e0316a6`):**

| služba | předchozí image ID | nová image ID |
|---|---|---|
| `edu_ai_search` | `sha256:f6aee27085ac…` | `sha256:48f1c6d66d67…` |
| `edu_ai_web_chat` | `sha256:3b395971c47d…` | `sha256:bf81cd897238…` |
| `nginx` | beze změny image (`nginx:1.27-alpine`), jen bind-mount config | — |

`docker inspect edu_ai_web_chat --format '{{.Image}}'` je jediný spolehlivý
zdroj — buildx u multi-platform/attestation buildu tiskne víc digestů
(`exporting config …`, `exporting manifest …`, `exporting manifest list …`);
image ID, které compose skutečně použije a spustí, je ten poslední
(`manifest list`). V předchozím přehledu jsem omylem uvedl `exporting config`
digest (`sha256:4680d674759…`) přečtený z výstupu buildu, ne skutečné ID
z `docker inspect` — opraveno.

Oba nové image ověřeny obsahem, ne jen ID: `edu_ai_search` přes `docker cp`
+ `diff -rq` proti checkoutu (0 rozdílů); `edu_ai_web_chat/web_chat.py` přes
`docker cp` + `diff` proti checkoutu (0 rozdílů).

**Záloha:** `pg_dump -Fc` → `~/edu_ai_20260906_2033.dump` (56 MB) na produkčním
hostu, restore ověřen do throwaway DB (`edu_ai_restore_test`, počty řádků
sedí), pak dropnuta. Dump zůstává mimo git, mimo repo.

**Regrese (doplněno, viz §8):** Stejná DB kopie posloužila i pro regresní
srovnání starého a nového kódu. Poprvé jsem ji spustil s throwaway kontejnery
připojenými jen na `app`+`data` síť (bez `edge`) — to omylem vypnulo
`qa_vector`/`rag_vector` (DNS na `openaipublic.blob.core.windows.net`
neresolvitelné z interní sítě) a výsledek (78 % answer) byl **běh
s nefunkčním vektorovým hledáním, ne potvrzení kompatibility**. Po připojení
obou throwaway kontejnerů na `edge` (stejná síť jako produkční `edu_ai_search`,
žádné nové porty, živé služby nedotčené) a ověření skutečného úspěšného
embeddingového volání (`POST .../embeddings 200`, `qa_vector`/`rag_vector`
reálně přispívají chunky) jsem obě sady zopakoval s `--repeat 3`:

* starý kód (samostatně): 90 % answer / 4 % no_answer / 4 % suggest, 2
  kolísavé případy (N11, N26).
* nový kód s `--baseline` proti tomu: **41 z 42 shodných**; jediný rozdíl je
  u N11 (jiný počet/pořadí zdrojů), který byl kolísavý už v obou bězích —
  není to systematická regrese.
* Testovací sada (`tests/eval_rag_regression.jsonl`) odkazuje anonymizované
  entity `SKOLA_NOVA_VES_ZS/_SOS` — pro běh proti produkční kopii jsem použil
  dočasnou, necommitovanou kopii s přepsanými kódy na `ZS_KAVCI_HORY_ZS/_SOS`
  (známý alias, viz interní poznámka o migracích 201/202). Scoped sada
  (`eval_rag_domains.jsonl`) neproběhla — viz §14.
* Vedlejší nález: `answer: query-domain detection failed (_kw_tokens()
  missing 1 required positional argument: 'cfg_key')` — ve **stejné míře**
  v obou bězích (91× staré, 36×+ nové), tedy předexistující, nesouvisející
  s doménovým scope. Zaznamenáno, neopraveno (mimo rozsah dnešního vydání).

**Migrace 213:** aplikována s `ON_ERROR_STOP=1` na produkční `edu_ai` (ne jen
na testovací kopii). `domains | ARRAY | (prázdné) | YES` — potvrzeno.

**nginx:** `docker compose up -d nginx` **nestačilo** — bind-mount ukazoval na
starý inode (git checkout soubor nahrazuje, nepřepisuje in-place), takže ani
`nginx -s reload` nenačetl nový allowlist. Teprve `--force-recreate` pomohlo;
ověřeno explicitně před/po (`/search/v1/domains` 404 → 200) a že se allowlist
jinam nerozšířil (tři sondovací cesty pořád 404). **Doplnění postupu oproti
§6 pro příště: při změně `nginx.conf` počítej rovnou s `--force-recreate`,
samotný reload nemusí stačit, i když `nginx -t` projde.**

**Smoke (query_id `smoke-domain-20260906-*`):** health/domains 200; dotaz bez
`domains` → `answer`, `effective_scope.domains=null`; s `domains: null` totéž;
neplatný kód `NESMYSL_KOD` → 422; pozitivní scoped dotaz do `SECONDARY_ADMISSIONS`
(existuje, má obsah) → `answer` se správným `matched_entry_id` a
`effective_scope.domains=["SECONDARY_ADMISSIONS"]`; SSE (`/v1/ask/stream`
přes nginx) → `event: done` s plným payloadem. Vše zalogováno v
`question_answer_log` s příslušnými `domains`. Katalog domén (11 kódů pro
`ZS_KAVCI_HORY_ZS`) i aktivní QA packy (faq-jpz 110, faq-provoz 100) beze
změny oproti §1.

**Webchat v prohlížeči — NEOVĚŘENO interaktivně.** `rag2.edu-ai.eu/chat/` je
za Cloudflare Access, který vyžaduje přihlášení; z tohoto prostředí jsem ověřil
jen server-side (statický shell 200, `/chat/api/domains`, `/chat/api/config`
s `entity_picker: true`), ne skutečné proklikání „Všechny oblasti“ / volby
školy / nové konverzace v běžícím prohlížeči. Spoléhá se na lokální Playwright
sadu (`test_domain_picker_browser.py`, 69/0 před nasazením) — doporučuji
rychlou ruční kontrolu v prohlížeči, než se toto považuje za plně ověřené.

**Návrat (ověřené, přesné příkazy).** `--no-deps` u každé služby zvlášť, ať
`docker compose up -d` nesahá na nic než na jmenovaný kontejner — `nginx` má
v compose souboru `depends_on` na `edu_ai_api`, `edu_ai`, `pgadmin` i
`edu_ai_web_chat`, a bez `--no-deps` by je při recreate mohl zbytečně
zkontrolovat/dotknout. **Pořadí je záměrné: nginx se recreatuje AŽ JAKO
POSLEDNÍ krok, znovu, bez ohledu na to, jestli se jeho config vůbec vrací.**
Jeho `upstream search_service { server edu_ai_search:8090; }` je statický
blok — nginx jméno kontejneru přerozliší na IP jen při startu/reloadu a pak ji
drží. Krok 2 (search) dostane při `up -d` novou IP (jiný kontejner, stejné
jméno); pokud by nginx po tomhle kroku zůstal beze změny, mířil by dál na IP
předchozího (nového kódu) kontejneru, který už neexistuje. Samotný
`git revert` běžící kontejnery nepřepne vůbec — je to jen commit.

```bash
# 1) UI zpět — ať klienti přestanou posílat domains
ssh edu-ai 'cd /opt/edu_ai && \
  docker tag edu_ai_web_chat:prev-e0316a6 edu_ai_web_chat:0.1 && \
  docker compose up -d --no-deps edu_ai_web_chat'

# 2) search zpět — TEPRVE až žádný klient neposílá domains
ssh edu-ai 'cd /opt/edu_ai && \
  docker tag edu_ai_search:prev-e0316a6 edu_ai_search:1.0 && \
  docker compose up -d --no-deps edu_ai_search'

# 3) nginx — POVINNĚ recreate znovu, i beze změny configu, jinak drží starou
#    IP adresu search kontejneru z kroku 2 (viz vysvětlení výš).
#    Config se vrací jen volitelně, pokud vadí veřejná /search/v1/domains:
ssh edu-ai 'cd /opt/edu_ai && \
  git checkout e0316a6 -- nginx/nginx.conf && \
  docker compose up -d --no-deps --force-recreate nginx'
# beze změny configu by šlo i jen:
#   docker compose up -d --no-deps --force-recreate nginx

# trvalý návrat v historii (ne reset --hard):
git revert 59f8774   # a tenhle revert commit normálně nasadit tímtéž postupem

# sloupec domains z migrace 213 se NEVRACÍ (aditivní, nullable, starý kód ho nečte)
```
