---
id: spec.search-domain-scope
status: active
components: [api, retrieval, db, webchat, mobile, contracts]
sources:
  - search/app/server.py
  - search/app/state.py
  - search/app/scope.py
  - search/app/executor.py
  - search/app/settings.py
  - search/app/nodes/route.py
  - search/app/nodes/qa_common.py
  - search/app/nodes/qa_exact.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/qa_vector.py
  - search/app/nodes/document_lexical.py
  - search/app/nodes/rag_keywords.py
  - search/app/nodes/rag_vector.py
  - search/app/nodes/document_siblings.py
  - search/app/nodes/answer.py
  - search/app/nodes/resolve_context.py
  - search/app/conversation.py
  - search/app/log.py
  - search/app/idempotency.py
  - web_chat/web_chat.py
  - web_chat/frontend/src/App.tsx
  - nginx/conf/nginx.conf
  - contracts/schemas/search-request.schema.json
  - contracts/schemas/search-response.schema.json
  - contracts/openapi.yaml
  - contracts/errors.yaml
  - api/app/main.py
  - search/app/domain_scope.py
  - web_chat/frontend/src/DomainPicker.tsx
  - postgres/init/140_qa_pack_school_grades_domains.sql
  - postgres/init/144_qa_scan_surface_parity.sql
  - postgres/init/213_log_domains.sql
  - scripts/domain_inventory.py
  - scripts/domain_migration.py
  - tests/test_domain_scope.py
  - tests/test_domain_scope_api.py
  - docs/integrations/mobile-domain-scope.md
  - docs/nasazeni/2026-09-06_domain_scope.md
last_verified: 2026-09-06
---

# Výběr oblasti hledání pro mobilní aplikaci a webchat

> **Aktivní kontrakt backendu a webchatu.** Implementace a její historické nasazení jsou doložené
> ve zdrojích a [runbooku](../../docs/nasazeni/2026-09-06_domain_scope.md). Stavový nadpis
> zde není tvrzením o hotové mobilní integraci nebo regresi nejnovějšího SHA.
> Mobilní APP existuje, ale jeho doménová/offline QA integrace v prohlédnutém zdroji
> není doložená; viz [mobile-domain-scope.md](../../docs/integrations/mobile-domain-scope.md).
> Staré počty neoznačených dat a testovací výsledky v této specifikaci jsou historické snímky.

## 1. Doporučený výsledek

Mobil i webchat posílají stejnému search API volitelnou množinu kódů `domains`.
Mobil ji může nastavit podle otevřené sekce; webchat nabídne výběr oblastí školy
a položku „Všechny oblasti“. Vybraná oblast je v klientovi vždy viditelná.

První verze používá existující školní katalog DOMAIN. Nevytváří novou taxonomii,
novou produkční závislost ani nový přepínač v settings. Přesné omezení stojí na
správném označení zdrojů, nikoli na odhadování oblasti z textu dotazu.

Garance: odpovědi, podklady pro generování a navržené otázky pocházejí pouze
ze zdrojů označených alespoň jednou vybranou doménou a současně dostupných
podle dosavadních pravidel. Doména nepřidává oprávnění. Dokument nebo odpověď
s více doménami může obsahovat více souvisejících témat; kvalita a granularita
označení jsou součástí akceptace dat. Filtr sám neověří význam každé věty.

Po zvolení oblasti se hledání samo nerozšíří na všechny oblasti. Když nic
nevyhovuje, vrátí dosavadní `no_answer`; klient může nabídnout výslovnou akci
„Hledat ve všech oblastech“. Ta odešle nový požadavek s `domains: null`.
Bez filtru zůstává výběr zdrojů i pořadí kaskády dosavadní.

## 2. Ověřený výchozí stav

- Lokální i produkční checkout: `e0316a6`. Obsah 14 kontrolovaných souborů
  search služby odpovídal běžícímu kontejneru; BFF se lišil pouze komentářem.
- `/v1/ask` dnes `domains` odmítá HTTP 422 jako neznámé pole.
- Interní `/v1/domains` funguje; `/search/v1/domains` přes produkční nginx
  vrací 404. Bez entity katalog vrací GLOBAL.
- `CHAT_ENTITY_PICKER=true`. SOŠ i ZŠ Kavčí hory mají 11 domén; SOŠ má také
  `CATERING`. Jedovnice a Svobodná škola dědí osm globálních domén.
- Ve všech čtyřech aktivních settings jsou zapnuté route, všechny tři QA
  kroky, document_lexical, rag_vector a answer. rag_keywords a document_siblings
  jsou vypnuté, ale i jejich implementace musí nový filtr respektovat.
- Přepis návazných dotazů je zapnutý. Historie ani evidence již nabídnutých
  otázek dnes nerozlišují tematický scope.

| Aktivní QA pack | Aktivní odpovědi | Dnešní domény |
|---|---:|---|
| faq-jpz | 110 | Oba přijímací kódy na všech odpovědích |
| faq-provoz | 100 | Žádné |
| provoz-sos | 33 | Stejných sedm domén na všech odpovědích |
| prijimacky-jed | 32 | Žádné |
| provoz-jed | 33 | Žádné |
| prijimacky-svo | 32 | Žádné |
| provoz-svo | 33 | Žádné |

| Entita | Chunky napojené na aktivní verze dokumentů | Z toho s doménou |
|---|---:|---:|
| ZS_KAVCI_HORY_ZS | 365 | 346 |
| ZS_KAVCI_HORY_SOS | 104 | 0 |
| ZS_KAVCI_HORY | 186 | 29 |
| SVOBODNA_SKOLA_PRAHA | 82 | 19 |

Počty chunků nejsou počty výsledků pro konkrétní roli a datum; další filtry
mohou množinu zúžit. Stav je snímek, před přípravou dat se přeměří.

**Přeměřeno 6. 9. 2026** nástrojem `scripts/domain_inventory.py --remote edu-ai`
(read-only). Čísla výše potvrzena; navíc se ukázalo, co z aplikace vidět není:

| zjištění | číslo |
|---|---:|
| aktivní QA packy bez jakéhokoli označení | **5** = 230 odpovědí |
| chunky **bez** oblasti pod **označeným** dokumentem | **170** (151 `ZS_KAVCI_HORY`, 19 `ZS_KAVCI_HORY_ZS`) |
| chunky s **jinou** množinou než jejich dokument | **94** (`ZS_KAVCI_HORY_ZS`) |
| `qa_variants`/`qa_embedding` vs `qa_entries` | 274 řádků, **všech 274 jen jiné pořadí pole** → pro array overlap neškodné |

Poslední dva řádky jsou to, co §6 předpokládala: **samotné UPDATE metadat
nestačí**. Zároveň je to protiklad k prvnímu — rozchod pořadí je kosmetický a
report ho proto vypisuje odděleně, jinak by se řádek „drift“ naučil každý
ignorovat.

Dostupnost přes roli a platnost (to, co uživatel skutečně vidí, ne co je
označené) je v reportu samostatná sekce: pro `PARENT` 717 chunků, z toho 394
označených, a 3087 QA variant, z toho 973 označených.

## 3. Společný kontrakt

| Vstup `domains` | Význam |
|---|---|
| Pole chybí nebo `null` | Dosavadní hledání bez tematického omezení |
| Neprázdné pole známých kódů | OR mezi doménami, AND s ostatními filtry |
| `[]`, prázdný řetězec, neznámý/neaktivní kód, chybný typ | 422 `VALIDATION_ERROR` s `fields` |
| Platný kód bez vyhovujícího obsahu | Úspěšný dotaz, případně `no_answer` |
| Nedostupný katalog při validaci explicitního filtru | Retryable chyba služby; žádné hledání bez filtru |

Kódy server normalizuje stejnou konvencí jako ingest: trim a uppercase,
odstranění duplicit a stabilní seřazení. Prázdný prvek neodstraňuje potichu.
Členství validuje proti `resolved_taxonomy_rules(entity, 'DOMAIN')` po
dosavadním ověření entity a volajícího. Konkrétní kódy a jejich ID jsou data.

Ukázka pro katalog ZŠ Kavčí hory; klient přebírá entity i kódy z katalogu:

```json
{
  "question": "Kdy se podává přihláška?",
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "user_role": "PARENT",
  "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"]
}
```

`effective_scope.domains` vrací skutečně použitou normalizovanou množinu kódů,
nebo `null` při neomezeném hledání. Je aditivním polem odpovědi. Přenést jej
také přes BFF a do finální SSE odpovědi; nevracet interní databázová ID.
U odpovědí ukončených před retrievalem neprezentovat nevalidovaný vstup jako
ověřený použitý filtr.

`/ask` a `/ask/stream` sdílejí přípravu a validaci scope před spuštěním kaskády
a před otevřením SSE. `/explain` se napojí na stejnou přípravu entity,
volajícího, role a domén, ale zůstane diagnostickým deterministickým prefixem
bez generování. Dnes mu samotný sdílený request model shodné chování nedává;
jeho rozšíření výslovně popsat a otestovat jako změnu diagnostického kontraktu.

Katalog pro mobil: `GET /search/v1/domains?legal_entity=…`, nově v allowlistu
nginxu. Dosavadní interní chování bez entity zůstane kvůli kompatibilitě,
ale klientské hledání vždy použije skutečnou entitu.

## 4. Rozlišení kódu a databázového ID

Validace kódu a vyhledání ID pro filtr jsou dvě různé operace:

1. Ověřit, že zvolené kódy existují v katalogu volající školy.
2. Vyhledat všechna aktivní DOMAIN ID těchto kódů s vlastníkem uvnitř
   organizace volajícího nebo s globálním vlastníkem. Tím se pokryje i
   klasifikace zdrojů publikovaných nadřazenou organizací.
3. Na zdroje současně aplikovat původní scope entity/visibility/role/validity
   a filtr aktivní verze dokumentu. Rozšíření množiny ID nic z toho neuvolňuje.

Příklad z produkce: `SECONDARY_ADMISSIONS` je v katalogu ZŠ ID 13, globálně
ID 42. Pět aktivních chunků organizace nese `{42,24}` a je publikováno pro
rodiče a učitele. Rodič ze ZŠ je při volbě tohoto kódu nesmí ztratit.

Implementovat společný parametrizovaný helper v `scope.py`, například
`domain_where(alias, column, domain_ids)`. Alias/sloupec vybírá kód, nikdy
request. `None` znamená žádné omezení; prázdná vyřešená množina znamená
`AND false`, nikdy vynechání filtru. Při explicitním scope NULL/empty štítky
zdroje nevyhovují. Identita a oprávnění zůstávají v dosavadním `scope.where()`.

## 5. Místa v kódu a jejich povinnosti

| Místo | Změna |
|---|---|
| server.py, state.py | Sdílená validace, normalizované kódy a interní ID ve stavu, effective_scope |
| route._scope | Průnik domén manifestu; žádné domain-outside packy do offer_pack_ids |
| qa_common, qa_exact, qa_lexical, qa_vector | Společné QA fragmenty včetně filtru na doménách scanovaných rows; zachovat pack allowlist i dosavadní oprávnění |
| Kategorie a related chips v qa_common | Stejné domény jako odpověď; žádná cesta doplňování chips mimo filtr |
| route._corpus_topics | Filtrovat QA otázky i dokumentové nabídky přímo; běží před sestavením pack_ids |
| Other-grade suggestions | Mohou nadále uvolnit ročník podle dnešní funkce, nikoli doménu |
| document_lexical, rag_keywords, rag_vector, document_siblings | Filtr na document_chunks.document_domain_id před LIMIT, včetně doplňovaných sousedů |
| answer | Zpracuje již omezené kandidáty; žádné obnovení vyřazených zdrojů nebo suggestions |
| resolve_context, conversation | Historie a deduplikace nabídek ve shodném doménovém scope |
| log.py | Uložit normalizované kódy do query logu |
| web_chat.py, App.tsx | Katalog, výběr, přenos, chyby, životní cyklus konverzace |
| nginx, contracts | Katalog dostupný mobilu, popsaný request/response a chybové stavy |

Použít již existující doménové sloupce v QA tabulkách. Manifest slouží jako
předvýběr; není jediným dokladem relevance jednotlivé odpovědi. Společný QA
helper má zabránit rozchodu hlavní odpovědi, top-up chips a přímých nabídek.
Veškeré statistiky pro pořadí vztahované ke kandidátnímu korpusu prověřit
spolu s filtrací, aby se po zúžení nezměnily nechtěně prahy a denominátory.
Dokumentová nabídka v route._corpus_topics dnes spojuje metadata pouze přes
file_uuid. V této dotčené cestě doplnit také shodu processing_uuid, aby
aktivní metadata nepřipustila chunky ze starší verze téhož dokumentu.

Ponechat `domain_match_boost` a automatickou detekci jako dosavadní řazení
uvnitř povolených výsledků. Nejsou oprávněny rozšiřovat scope; jejich plošné
vypínání není nutnou součástí této změny.

Chyba pomocného kroku nesmí změnit explicitní filtr na neomezené hledání.
Opravit také relevantní fallback v route, který dnes může vrátit `pack_ids=[]`
s významem „všechny packy“. Při chybě výběru použít existující sentinel pro
žádný pack. Chyba retrieval kroku nadále padá do dalších omezených kroků
a nakonec do `no_answer`. Neměnit pořadí kaskády ani `on_hit`/run conditions.

## 6. Příprava dat bez nového klasifikačního modelu

Před uživatelským spuštěním vyrobit report po škole a zdroji: aktuální domény,
chybějící/neaktivní/cizí ID, šíře označení, počty odpovědí a aktivních chunků.
Report musí zahrnout také dostupné organizační zdroje. Chybějící oblast není
důvodem zkopírovat jiný školní katalog nebo označit zdroj všemi doménami.

Doporučený první postup pro QA je rozdělit smíšené packy na tematicky
soudržné packy přes existující import/build/export cestu. Zachovat otázky,
odpovědi, role, platnost, ročníky a citace. Více domén na jednom packu je
přípustné pouze tehdy, když označení odpovídá jeho odpovědím.
Nepovažovat automaticky všechny otázky o JPZ za relevantní pro obě přijímací
domény; takové zařazení musí zkontrolovat kurátor.

Před rozdělením na testovací kopii ověřit tvorbu entry_id, přesun mezi packy,
chování opakovaného importu a přenos kategorií/thresholds. Změna identity
nebo packu může změnit výběr shodných otázek a related chips. Připravit mapu
starých a nových identifikátorů, pokud build identitu nezachová. Staré packy
vyřadit až při aktivaci náhrad tak, aby nikdy nezůstala dvojí aktivní sada.

Pokud rozdělení narazí na množství packů nebo nestabilní identity, samostatný
následující návrh může rozšířit import o kurátorské domény jednotlivých
odpovědí. Z existujících sloupců nelze usuzovat, že takový import už funguje.
Do první implementace tento neověřený předpoklad nepatří.

U dokumentů doplnit deklarované domény přes stávající ingest a přepočítat
označení dotčených chunků. Ruční oprava pouze v document_metadata nestačí.
Velké dokumenty se kvůli této funkci nedělí: školní řád může mít například
docházku i hodnocení a být dostupný v obou oblastech. Chunky zdědí efektivní
domény dokumentu; relevantní pasáže vybírá dosavadní retrieval podle otázky.
Nehromadit na dokumentu všechny domény jen pro zvýšení pokrytí.

Stávající ingest tuto propagaci má. Pro nové ingestování zachovat cestu přes
ingest. Pro existující obsah před plošným reingestem nejdřív prověřit cílenou
migraci metadat a odvozených štítků v jedné transakci, včetně závislých bindingů.
Pokud jejich konzistenci nelze zajistit bez buildu, použít ověřený cílený
reingest. Není požadována změna textů, chunkingu ani embeddingů; nevytvářet
nový LLM klasifikátor chunků. Nedotčené dokumenty nepřeingestovat.

Datová akceptace není „každý řádek má libovolnou doménu“. Kurátor musí potvrdit
zařazení, vyřešit pět neoznačených aktivních QA packů a zdroje bez domén
v rozsahu spuštění; případná vědomá vyloučení výslovně uvést v reportu.
Historické verze ani neaktivní packy nemusí být přetagovány pro tuto funkci.

## 7. Klienti, historie a kompatibilita

Webchat dostane `GET /chat/api/domains?legal_entity=…`. BFF určí účinnou entitu
stejně jako pro message: pokud je picker vypnutý, použije svou pevnou entitu.
Nemůže načíst katalog jedné školy a potom hledat ve druhé.

Při načítání a změně školy klient zruší starý fetch nebo ignoruje jeho pozdní
odpověď. Uloženou volbu drží po entitách a ověřuje proti novému katalogu.
Do dokončení ověření neodešle dotaz se starými kódy. Pokud některý kód zmizel,
zobrazí potřebu nové volby; dotaz automaticky neopakuje s širším scope.
Při nedostupném katalogu nabídne chybu a nové načtení, ne tichý reset filtru.

Změna školy nebo množiny domén zahájí novou konverzaci; UI oddělí starou
historii a staré chips. Rozpracovaný request se zruší nebo se jeho pozdní
odpověď nepřidá do nové konverzace. Stejná pravidla dostane mobil.

Server navíc filtruje historii pro resolve_context a conversation.load_seen
podle normalizovaných `domains`. Nelze spoléhat jen na klientovo nové UUID.
Starší řádky s NULL patří pouze neomezenému hledání. QA sondy rozhodující
o přepisu návazného dotazu mají při explicitním filtru respektovat doménu;
shoda v jiné oblasti nesmí zabránit potřebnému přepisu.

Pro tento účel návrh obsahuje jednu aditivní migraci:
`question_answer_log.domains text[] NULL`, bez zpětného odhadování hodnot
historických dotazů a bez nového indexu, dokud jeho potřebu neprokáže měření.
Logovací kódy musí číst všechny cesty včetně streamu. Selhání logu nezmění
scope; při nedostupné historii se dotaz zpracuje samostatně.

BFF přenese validované strukturální pole `domains` do search; obsah katalogu
autoritativně ověřuje search. Upstream 422 pro domény musí BFF vrátit jako
opravitelnou 422 s `fields`, místo dnešní obecné 500. Přizpůsobit i chybový
obal pro strukturální chyby samotného ChatRequest. Klient obnoví katalog
a požádá o novou volbu, nikdy sám nezmění dotaz na „vše“.

Při aktualizaci idempotence zachovat digest starých neomezených požadavků:
nový model přidá default `domains: null`, takže prosté model_dump by změnilo
hash i pro původní klienty. Před hashováním vynechat právě nové pole, pokud
je NULL; ostatní dosavadní pole a jejich defaulty ponechat. Explicitní
neprázdné domains vstupují do hashe v kanonickém tvaru. Stejný klíč s jiným
scope musí vrátit 409, nikoli cizí odpověď. TTL/claim/replay pravidla se nemění.
Starší replay bez effective_scope.domains je kompatibilní s neomezeným scope.

`selected_answer_key` zůstává pouze logovaným údajem, jak říká dnešní kontrakt.
Kliknutí odešle text nabídnuté otázky a aktuální scope. Tato změna nezavádí
přímé načítání odpovědi podle entry_id.

## 8. Pokud produkt potřebuje právě „Přijímačky / Provoz“

Tyto dvě položky nejsou totéž co dnešní katalog DOMAIN. První verze výše
dovoluje vybrat skutečné oblasti; sama nezavádí dvě univerzální zkratky.
Mobil nemá odhadovat skupinu podle názvu kódu a „Provoz“ nesmí znamenat
automaticky doplněk všech ostatních domén.

Pokud jsou tyto zkratky podmínkou vydání, doplnit před klientskou etapou
samostatně schválené pojmenované profily: kód profilu, přeložené názvy,
vlastník/entita a explicitní množina domén. Backend vydá jejich vyřešené
mapování pro školu; obě aplikace používají shodné mapování a stále posílají
`domains`. Umístění profilů do nové tabulky nebo per-org konfigurace vyžaduje
rozhodnutí o správě dat a migraci; tento návrh žádné takové úložiště nezavádí.
Počet profilů není technickým důvodem pro odklad, rozhoduje produktová potřeba.

## 9. Pořadí realizace a návrat

| Etapa | Konkrétní výstup | Podmínka pokračování |
|---|---|---|
| A — potvrdit kontrakt a inventuru | Tento draft, čerstvý report zdrojů, rozhodnutí o skutečných doménách versus pojmenovaných profilech | Dohodnutý rozsah UI a dat |
| B — backend na testovací kopii | Migrace logu, scope helpery, endpointy, katalog přes nginx, kontrakty a testy | Negativní scope testy i staré neomezené requesty projdou |
| C — data na testovací kopii | Kurátorsky ověřené packy a dokumenty, exporty pro nasazení, mapa identit a vratný postup | Obsahová kontrola a opakovaná regrese |
| D — web a mobil | Stejné volby, chyby, přepnutí konverzace, API příklady pro mobilní tým | Oba klienti projdou stejnými scénáři |
| E — produkce | Záloha, aditivní migrace, build search, konfigurace nginx, ověření API, připravená data, BFF/UI a následně mobil | Ověření proti skutečnému nasazenému API před zapnutím klientského výběru |

Technické přípravy dat lze dělat souběžně s backendem. Klientské spuštění
počká na akceptaci dat v cílových školách; přepínač samotný nedoplní chybějící
obsah. Nový backend je nejprve kompatibilní se starými klienty bez domains.
Migraci logu nasadit před kódem, který do nového sloupce zapisuje.

Při návratu lze vrátit klientské UI a ponechat aditivní backend i sloupec.
Vrácení search na starší verzi je možné až po zastavení scoped požadavků
ze všech klientů, včetně již vydaného mobilu; starší API je odmítá 422.
Filtr se nesmí na proxy/BFF zahazovat jako nouzový workaround.
Datový návrat používá ověřené exporty a aktivaci původní sady, nikoli souběh
starých a nových packů. Odebrání logovacího sloupce není součástí rollbacku.

## 10. Akceptační scénáře

1. Chybějící domains a NULL zachovají dosavadní scope, outcomes a starý
   idempotency digest; nezměněné retrieval fixture dávají stejné kandidáty.
2. Jedna doména a OR více domén fungují v QA exact/lexical/vector i dokumentech.
   Prázdné a cizí hodnoty vracejí stejný chybový obal přes API i BFF.
3. Platný kód s nulovým obsahem a netagované zdroje vedou k prázdné omezené
   množině, nikoli k fallbacku bez filtru. Totéž při výjimce route/helperu.
4. Kód s odlišnými lokálními/globálními ID zachová dostupné organizační
   dokumenty; jiná organizace, nepovolená role, soukromý cizí zdroj,
   neplatný dokument a neaktivní verze se nezpřístupní.
5. Smíšený pack ve fixture prokáže entry filtr; produkční datová kontrola
   prokáže, že „Stravování“ nemá za kandidáty celý nesouvisející provoz.
6. Meta otázka, related chips, category top-up, other-grade nabídky a případně
   zapnuté document_siblings/rag_keywords zachovají vybranou doménu.
7. Změna školy/domény, pomalý katalog, pozdní odpověď a odstraněný kód
   nezpůsobí dotaz se starou volbou ani automatické rozšíření na „vše“.
8. Návazný dotaz po změně scope nepoužije předchozí oblast ani při znovupoužití
   conversation_id přímým API klientem; deduplikace nabídky má stejnou hranici.
9. /ask a /ask/stream použijí stejný scope a deterministické QA výsledky;
   u generování se posuzují kandidáti a dodržení scope, ne shoda LLM textu.
   /explain ukáže odpovídající deterministický prefix se správnou identitou.
10. Idempotency replay při stejném scope funguje; jiný scope se stejným
    klíčem vrací 409. Pořadí domén/duplicity nemění význam požadavku.
11. Veřejná cesta katalogu přes nginx funguje pro mobil. Mobil i BFF přenesou
    filtry, effective_scope a opravitelné chyby podle kontraktu.
12. Velký dokument s více doménami je dostupný v každé své doméně, bez dělení
    souboru. Test posuzuje přípustnost zdroje a výběr relevantní pasáže podle
    dotazu, nikoli neexistující samostatný tematický štítek každého chunku.

Před nasazením: `make docs-check`, kontraktové a cílené behaviorální testy,
parity check a regresní sada `python3 tests/eval_rag.py --repeat 3`.
Oddělit měření změny kódu na stejných datech od měření po rozdělení packů.
Syntetické/fault-injection scénáře provádět na testovací kopii, nikoli v produkci.
Nasazení neprohlašovat za hotové pouze po jednotlivém úspěšném dotazu.

## 11. Rozsah schválení a zbývající nejistoty

Tento dokument nic nenasazuje. Ve vykonávací relaci respektovat již udělenou
autorizaci; neopakovat schválení, které už vlastník pro konkrétní změnu dal.
Pokud ještě není schválena navržená migrace logu, předložit konkrétní SQL
a test migrace podle pravidla AGENTS.md pro schema changes. Úprava request
hashe má zachovat stávající replay; případná skutečná změna jeho sémantiky
vyžaduje rozhodnutí vlastníka. Případné profily jsou další samostatné
rozhodnutí o schématu/config. Přidání domén nepotřebuje měnit embedding model,
normalizaci, indexy ani precedence kaskády.

Mobilní zdrojový kód není v tomto repozitáři; jeho skutečnou klientskou cestu,
uložení voleb a vydání musí ověřit mobilní tým. Při implementaci také změřit
plány SQL po přidání filtrů a ověřit chování QA buildu při dělení packů.
Případná potřeba nového indexu, změny buildu či profilu se předloží jako
konkrétní rozšíření, nikoli předpoklad skrytý v odhadu.

Odhad 1,5 dne není podložený pro celý rozsah. Závazný odhad rozdělit na
backend, kurátorská data a oba klienty po etapě A a po ověření importu;
datová práce a mobilní release jsou dnes hlavní neznámé.
