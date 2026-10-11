# QA portál — závazný kontrakt v1

Track: contract. Vlastník: lead. Verze kontraktu: **qa-portal/1.4.0** (1.3.0 + §5.1 zápis CSV čárkou při čtení obou oddělovačů + §10.1 rolování dialogu).
Základ: zdrojový HEAD `9fc015c`, integrační baseline `8211a85`.

Tento soubor je autoritativní pro implementaci portálu. Kde se rozchází
s `.claude/qa-portal/brief.md` nebo `category-db.md`, platí tento soubor:
návrhy vznikly bez čtení skutečného writeru a schématu, tento kontrakt je psán
proti nim. Každá odchylka je níže odůvodněná a označená **[ODCHYLKA]**.

Žádný worker nesmí měnit tvar API ani schématu. Potřeba změny jde na leada.

---

## 1. Architektura a hranice

```
prohlížeč (VPN)
   │  /qa/            statická SPA
   │  /qa-api/v1/     JSON API
   ▼
edu_ai_nginx (listener 80 = VPN strana; NIKDY listener 81 / search tunel)
   ▼
edu_ai_qa_admin   ← NOVÝ kontejner, jeden port 8082, žádné `ports:` na hosta,
   │                 sítě edge + app, BEZ PG_* a bez sítě `data`
   ├──► http://edu_ai_search:8090/internal/qa-admin/*   (čtení dat, katalogy, uploader)
   └──► http://edu_ai_api:8080/v1/ingest                 (publikace přes stávající ingest)
```

Portál **nemá** databázové připojení. Veškerý přístup k datům jde přes interní
API search služby, které nadále prochází `search/app/scope.py`.

Stav portálu (náhledy importů, operace, dočasné exporty) je v pojmenovaném
svazku `qa_admin_state`, mimo statický adresář frontendu. Není to druhá databáze
publikovaných QA dat — jen krátkodobý provozní stav.

---

## 2. Identita, policy a vlastnictví

### 2.1 Kontext administrátora

V MVP není přihlášení. Backend si vytvoří interní `Principal`:

```python
Principal(kind="vpn_admin", subject="vpn-admin", schools="ALL_ACTIVE")
```

Principal vzniká **výhradně serverově**. Identita se nikdy nečte z JSON těla ani
z klientských hlaviček. Policy je jediné rozhodovací místo:

```python
class Policy(Protocol):
    def allowed_schools(self, p: Principal) -> SchoolSelector: ...
    def may_read_pack(self, p: Principal, school: str) -> bool: ...
    def may_publish(self, p: Principal, school: str) -> bool: ...
    def may_replace(self, p: Principal, school: str) -> bool: ...   # import režim replace
    def may_manage_categories(self, p: Principal) -> bool: ...
```

MVP implementace `VpnAdminPolicy` vrací všechny aktivní školy. Budoucí přihlášení
vymění `PrincipalProvider` + `Policy`, nikoli publikační logiku.

**Povinný test policy** (dodá qa-portal-backend): s policy omezenou na jednu školu
musí požadavek na jinou školu skončit 403 i tehdy, když klient pošle cizí kód
v cestě, v těle i v manifestu ZIPu. Parametr nesmí rozšířit práva.

### 2.2 Technický uploader (mapování škola → staff_user)

Stávající ingest vyžaduje aktivní `staff_user` přesně pro zvolenou entitu
(`GET /v1/ingest-principal`). `staff_user.user_id` je **globálně unikátní**
(PRIMARY KEY na `user_id`), takže nelze použít stejné `user_id` pro více škol.

Kontrakt: search služba vystaví `GET /internal/qa-admin/uploader?legal_entity=…`,
které mapování **vyhledá, a pokud chybí, založí** (get-or-create) podle pevné
konvence:

| Pole | Hodnota |
|---|---|
| `user_id` | `qa_portal__{legal_entity_code}` |
| `legal_entity_code` | daná škola |
| `display_name` | `QA portál (technický účet)` |
| `is_active` | `true` |
| `notes` | `Technický vykonavatel administračních publikací QA portálu. Není fyzická osoba.` |

Zakládá **výhradně** search služba (má DB přístup), idempotentně
(`INSERT … ON CONFLICT DO NOTHING`), a jen pro entitu, která je aktivní.
Portál žádné UUID nevymýšlí a nevybírá náhodného zaměstnance.
**[ODCHYLKA]** brief počítal s ručně připravenými záznamy při nasazení; konvence
zde je deterministická a auditovatelná, nevyžaduje seed s fiktivními daty
a nelze ji splést s reálným člověkem. Není to migrace — jsou to aplikační zápisy
stejné třídy jako `ingestion_log`.

Když entita neexistuje nebo je neaktivní: `404`. Škola je pak čitelná jen pokud
existuje, ale publikace vrátí `PUBLISH_UPLOADER_UNAVAILABLE` (viz §7).

### 2.3 Zachování vlastnictví — POVINNÁ oprava writeru

**Zjištěný stav (uzel `PostgresInsertQA-qa004-QA`):** větev UPDATE nastavuje
`owner_user_id = meta.user_id`, a do `to_update` jde každý řádek CSV, jehož
`entry_key` už v packu existuje — bez ohledu na to, zda se obsah změnil. Každý
reimport tedy dnes přepíše vlastníka všech dotčených položek na toho, kdo právě
nahrával. Totéž platí pro `qa_variants` (přepisují se celé).

To je neslučitelné se zadáním „úpravou neměnit původního vlastníka“. CSV nemá
sloupec vlastníka a ingest přijímá jedno `user_id` na celý soubor, takže to
**nelze** obejít na straně portálu.

Závazná změna (vlastník: qa-portal-data, soubor `EDU_AI_QA.json`):

- Dotaz na existující položky načte i `owner_user_id`:
  `SELECT entry_key, entry_id, owner_user_id FROM qa_entries WHERE …`.
- Ve větvi **UPDATE** se `owner_user_id` **nepřepisuje** — zapíše se původní
  hodnota položky. `visibility`, `audience_roles` a ostatní scope sloupce se
  chovají beze změny (viz §2.4 pro jejich ochranu na hranici portálu).
- Ve větvi **INSERT** se `owner_user_id` zapíše z metadat uploadu (nová položka
  vzniká pod tím, kdo ji zakládá) — beze změny.
- `qa_variants` dostávají `owner_user_id` **své položky** (nikoli uploadera),
  aby denormalizovaný scope odpovídal řádku v `qa_entries`.
- Pokud původní vlastník už není aktivní `staff_user` dané entity, hodnota se
  přesto zachová: FK míří na `staff_user`, který se nemaže, jen deaktivuje.

Tato změna se dotýká **všech** cest ingestu, nejen portálu. Je to oprava:
dosavadní chování tiše přepisovalo provenienci při každém reimportu. Musí být
pokryta testem (stará položka + reimport pod jiným uploaderem → vlastník beze
změny; nová položka → vlastník = uploader).

### 2.4 Metadata, která portál nesmí vymyslet

Writer aplikuje metadata uploadu na **všechny** řádky: `visibility`,
`audience_roles`, `school_grade_ids`, `school_subject_ids`, `school_level`,
`valid_from`, `valid_to`. Portál je proto **načte serverově z aktuálního stavu
packu a pošle beze změny zpět**; nikdy nepoužije výchozí hodnoty formuláře.

**Divergence metadat uvnitř packu.** Protože ingest umí poslat jen jednu sadu na
celý pack, pack, jehož položky mají dnes různá metadata, by publikací dostal
jednotná metadata a tiše by se změnil rozsah viditelnosti nebo ročníků.

Kontrakt: interní API vrací per-položková metadata i příznak `metadata_uniform`.
Pokud `metadata_uniform == false`, portál **odmítne publikovat** chybou
`PACK_METADATA_DIVERGENT` (409) se seznamem lišících se položek a polí. Čtení,
export a náhled zůstávají dostupné. **[ODCHYLKA]** brief tuto možnost nechával
otevřenou; odmítnutí je jediné chování, které nemůže tiše rozšířit viditelnost.
Řešení divergentních packů je samostatná etapa mimo rozsah MVP.

### 2.4a Nový pack — metadata potvrzuje administrátor (v1.1.0)

§2.4 platí beze změny pro **existující** pack: portál jeho metadata čte ze stavu
packu a posílá beze změny zpět. Nový pack ale žádný stav nemá, takže se nebylo
odkud načíst a první publikace skončila na `audience_roles: at least one role is
required`. Chybějící data nelze doplnit výchozími hodnotami formuláře — tím by
portál rozhodl o dosahu obsahu za administrátora.

Kontrakt proto rozlišuje podle toho, zda pack existuje:

| Stav packu | `pack_metadata` v `POST …/preview` | Zdroj metadat pro ingest |
|---|---|---|
| **neexistuje** (`pack-state` nevrací nic) | **povinné** | uložený náhled (`preview.pack_metadata`) |
| **existuje** | **zakázané** (422) | stav packu, jako dosud (§2.4) |

- Chybí u nového packu → `422 VALIDATION_ERROR`, `fields[].field = "pack_metadata"`.
- Posláno u existujícího packu → `422 VALIDATION_ERROR`, tamtéž. Odmítnutí je
  záměrné: jedna sada metadat se aplikuje na **všechny** řádky packu, takže
  přijetí by tiše přepsalo viditelnost celého existujícího packu.
- Publikace nového packu bere metadata **jen** z uloženého náhledu, nikdy z těla
  požadavku na publikaci (§8).

**Tvar `pack_metadata`:**

```json
{"visibility": "entity|organization",
 "audience_roles": ["TEACHER"],
 "school_grade_codes": ["1","2"],
 "valid_from": "2026-09-01", "valid_to": null}
```

- `visibility` — portál nabízí **jen** `entity` a `organization`. `private` je
  pro QA pack nesmysl (viděl by ho jen technický uploader) a portál ho odmítá.
  Zda smí jednotka publikovat `organization`, rozhoduje dál nastavení
  `child_may_publish_organization` v bráně; odmítnutí se hlásí jako `UPSTREAM_ERROR`.
- `audience_roles` — nejméně jedna položka, každá kód číselníku `role`.
- `school_grade_codes` — kódy číselníku `school_grade`, smí být prázdné pole.
- `valid_from` / `valid_to` — ISO datum nebo `null`; `from <= to`.
- `school_subject_ids` a `school_level` portál nenastavuje: současná ingest
  hranice pro QA je nepřijímá. Dokumentované omezení, ne opomenutí.

**Validace proti číselníkům.** Kódy se ověřují proti `GET /v1/code-lists?legal_entity=`
(`role`, `school_grade`) — tentýž zdroj, který používá brána. Neznámý kód →
`422` s `field` `pack_metadata.audience_roles[i]` / `pack_metadata.school_grade_codes[i]`
(index ukazuje na konkrétní chybnou položku). Nečitelné datum je `422`
s `pack_metadata.valid_from` / `…valid_to` — portál ho nepustí do brány.
**[ODCHYLKA]** brána při nedostupném číselníku vstup propustí neověřený; portál
v tom případě vrací `503 UNAVAILABLE` (`retryable: true`). Administrátorský
nástroj nemá důvod zapsat neověřená metadata, a opakování je tu levné.

Pro nabídku v dialogu slouží `GET /schools/{school}/metadata-options`
(kódy + popisky s fallbackem podle §4). Popisky viditelnosti jsou frontendový
token (§10), ne databázový číselník.

**Mimo rozsah N4 (otevřené):** import CSV/ZIP do dosud neexistujícího packu
zůstává neřešený — tam se metadata potvrzují stejně, ale náhled importu je
nenese. Dnes to skončí chybou ingestu; oprava patří do samostatného řezu.

### 2.4c Import do dosud neexistujícího packu (v1.2.0)

§2.4a řeší pack založený v dialogu. CSV/ZIP import měl stejnou díru: soubor
projde validací, ale pack, který ještě neexistuje, nemá metadata a publikace
skončí chybou brány. Import proto používá **stejnou validaci i stejné výslovné
potvrzení** jako dialog — nic se nedědí z manifestu ani z výchozích hodnot.

**Pole `pack_metadata_by_pack`** (multipart, JSON objekt) v
`POST /schools/{school}/imports/preview`:

```json
{"faq-provoz": {"visibility": "entity", "audience_roles": ["PARENT"],
                "school_grade_codes": ["1"]}}
```

Jeden tvar pro CSV i ZIP: **klíčem je vždy `pack_id`**, takže u ZIPu jsou
metadata přiřazená ke konkrétnímu packu a nemůže je omylem dostat jiný.
U CSV musí klíč odpovídat potvrzenému `pack_id`.

Pravidla pro každý pack v importu:

| Pack | Metadata poslána | Chování |
|---|---|---|
| nový | ano | validace proti číselníkům (stejná funkce jako §2.4a), uloží se k tomuto packu do náhledu |
| nový | ne | náhled je **200** a ukazuje rozdíl, ale pack má `metadata_required: true` a `publishable: false`; publikace ho odmítne |
| existující | ano | `422 VALIDATION_ERROR`, pole `pack_metadata_by_pack.<pack_id>` — existující pack si metadata zachovává (§2.4) |
| není v importu | ano | `422`, pole `pack_metadata_by_pack.<pack_id>` („no such pack in this import") |

- **[ODCHYLKA]** od §5.3: chybějící potvrzení metadat **není** chyba obsahu
  souboru, takže CSV náhled zůstává `200` a nevrací `422`. Jinak by klient
  neměl jak zjistit, pro které packy se má administrátora zeptat. Publikovat
  ale nejde: publikace takového packu skončí `failed` se zprávou
  `PACK_METADATA_REQUIRED` a **ingest se pro něj vůbec nezavolá**.
- Chybné kódy hlásí `422` se stejnými indexovanými jmény polí jako §2.4a,
  navíc s `file`. Nedostupný číselník → `503 UNAVAILABLE`.
- **Manifest ZIPu není potvrzení.** `manifest.json` z exportu portálu smí
  náhled vrátit jako `suggested_metadata` u nového packu, aby UI mohlo
  předvyplnit formulář, ale závazná je **jen** hodnota, kterou administrátor
  pošle v `pack_metadata_by_pack`. Import z cizího souboru tak nemůže tiše
  určit dosah obsahu.
- Publikace bere metadata nového packu **výhradně** z uloženého náhledu;
  tělo požadavku na publikaci je nikdy neobsahuje. Existující pack je dál čte
  ze stavu packu.

---

## 3. Verze packu, souběh a serializace

`qa_pack_manifest.version` je celé číslo, které writer zvyšuje při každém
ingestu. Samo o sobě nestačí: nezachytí změnu provedenou jiným nástrojem mezi
verzemi, a portál potřebuje i identitu **obsahu**, který uživatel viděl.

Kontrakt zavádí `pack_etag`:

```
pack_etag = "sha256:" + sha256(
    canonical_json({
      "version": <manifest.version>,
      "built_at": <manifest.built_at ISO8601>,
      "entries": [ {entry_key, entry_id, canonical_q, answer_md, category_code,
                    domains(sorted), variants(sorted), sources, is_active}
                   … seřazeno podle entry_key ]
    })
).hexdigest()
```

Počítá ho **search služba** v `/internal/qa-admin/pack-state`, aby měl jednoho
vlastníka a nešel rozbít odlišnou serializací v portálu.

Pravidla:

- Každé čtení položek vrací `pack_etag` a `pack_version`.
- `POST …/preview` přijímá `base_etag`; pokud nesouhlasí s aktuálním → `409
  PACK_VERSION_CONFLICT` (+ aktuální `pack_etag`).
- `POST …/publish` přijímá `preview_id` **a** `expected_etag`. Server znovu
  ověří etag těsně před zápisem, uvnitř zámku.
- **Serializace:** backend drží zámek per `(school, pack)` po celou dobu
  „ověř etag → zapiš → ověř výsledek“. Zámek je v `qa_admin_state` (soubor
  s exkluzivním vytvořením + PID/čas, s TTL pro osiřelý zámek), takže přežije i
  souběžné požadavky ve stejném procesu. Samotný předběžný GET verze závod neřeší
  a portál se na něj nespoléhá.
- **Vlastnictví zámku:** zámek nese nezaměnitelný token držitele. Uvolnit ho smí
  **jen** ten, kdo ho drží: `release()` odstraní soubor pouze tehdy, když v něm
  je pořád jeho vlastní token. Převzetí osiřelého zámku po TTL tím předchozího
  držitele **zneplatní** — jeho pozdější `release()` je bez efektu a nesmí
  smazat zámek nového držitele.
- **Souběžné převzetí:** když o osiřelý zámek soupeří víc požadavků, uspěje
  právě jeden; ostatní dostanou `409 PUBLISH_IN_PROGRESS`, nikdy ne 500 a nikdy
  ne druhý současný držitel. Platí i pro `import/publish`, kde se zamyká víc packů.
- **Známé omezení (dokumentovat, ne zamlčet):** zámek je lokální pro jednu
  instanci `edu_ai_qa_admin`. MVP běží v jedné instanci. Škálování na víc replik
  by vyžadovalo sdílený zámek (např. poradní zámek v Postgresu přes search) — mimo
  rozsah, ale musí být v provozní dokumentaci.
- Žádný automatický rebase a žádné přepsání cizí novější změny.

---

## 4. Kategorie (globální číselník `qa_category`)

### 4.1 Model

Přesně podle `category-db.md`: `edu_ai.code_list` s `list_code='qa_category'`,
`legal_entity_code` vždy `NULL`, popisky v `labels` (jsonb `{"cs": "Družina"}`),
stabilní `code`. `qa_entries.category_id` je nullable FK na `code_list(id)`.

Fallback popisku: požadovaný jazyk → výchozí jazyk rozhraní → stabilní `code`.
**Nezadaný `lang` znamená výchozí jazyk rozhraní**, ne „jakýkoli jazyk": když
pro něj překlad existuje, je to jazyk, který se ptal, a `label_fallback` je
proto `false`. `true` je vyhrazené pro **skutečně chybějící** překlad
v žádaném jazyce. Chybějící překlad **nikdy** nevyřadí kategorii z nabídky
a nemění přiřazení. Totéž platí pro oblasti (domény).

Přesné DDL je v `contracts/qa-portal/proposed-215_qa_category.sql`.
Migrace se nespustí bez schválení uživatelem (AGENTS.md „Ask first“).

### 4.2 Dvouetapový přechod z `tags.category` — **[ODCHYLKA]**

`category-db.md` §4 chce přepnout čtení na `category_id` v jedné koordinované
verzi. To v této dodávce **nelze** provést bezpečně:

`search/app/nodes/qa_common.py::category_siblings` dnes seskupuje související
otázky textovou rovností `tags->>'category'`. Po přepnutí na `category_id` by
všechny existující položky (kde `category_id IS NULL` až do schváleného
backfillu nad skutečnými produkčními daty) přestaly nabízet související otázky.
Zkontrolované mapování `(škola, pack, entry, starý cat) → code` vyžaduje
produkční inventuru, kterou tato dodávka nemá k dispozici.

Proto:

| Etapa | Obsah | Tato dodávka |
|---|---|---|
| **1** | Schéma, katalog, API, editor, CSV `category_code`, export/import, zápis `category_id` z portálu | **ANO** |
| **2** | Zkontrolovaný backfill `category_id` + přepnutí `category_siblings` a exportu na `category_id` jako jediný zdroj pravdy | **NE** — samostatné schválení nad reálnými daty |

V etapě 1 tedy:

- `category_siblings` zůstává **beze změny** (čte `tags.category`). Runtime se
  nemění, související otázky nezmizí, `--repeat` baseline je porovnatelný.
- Portál při zápisu drží obojí v souladu: `category_id` je autoritativní pro API
  a nový CSV sloupec; `tags.category` dostane popisek přiřazené kategorie
  v jazyce packu (nebo prázdno při `null`). Nevzniká druhé autoritativní
  přiřazení — `tags.category` je odvozený text, který runtime zatím používá
  pro seskupení a etapa 2 ho degraduje na čistě legacy údaj.
- Migrace 215 **nedělá žádný backfill** a **nemá seed taxonomie**. Kategorie
  zakládá administrátor explicitně přes API. Příklady kódů z `category-db.md`
  (AFTER_SCHOOL_CARE, SCHOOL_PROPERTY, SCHOOL_SUPPLIES) nejsou schválený seed.

### 4.3 Pravidla zápisu

- API přijímá `category_code: string | null`. Interní `category_id` se resolvuje
  serverově a v API ani CSV se nikdy nevystavuje.
- Neznámý kód nebo **nově přiřazovaná** neaktivní kategorie → `422` před zápisem.
- Neaktivní kód **už přiřazený téže položce** smí přežít round-trip beze změny.
- Zakládání kategorií: `POST /qa-api/v1/categories` → interní
  `POST /internal/qa-admin/categories`. Kód, `labels`, `sort_order`, `notes`.
  `code`/`list_code`/`id` jsou po vytvoření neměnné, přejmenování jde přes `labels`.

---

## 5. CSV a ZIP

### 5.1 Sloupce jednoho packu

Pořadí sloupců zůstává. Formát: UTF-8 **BOM**, CRLF, RFC 4180 uvozování.
Přidává se **jeden** sloupec `category_code` na konec:

```
entry_key,canonical_q,answer_md,cat,variants,sources,is_active,domains,category_code
```

#### Oddělovač — zápis čárkou, čtení obojího (v1.4.0)

- **Zápis.** Každý CSV, který systém nově vytvoří, používá oddělovač `,`.
  Platí pro export packu z portálu, packy v exportním ZIPu, CSV, které portál
  předává ingestu při publikaci, i pro `/v1/qa-export`. Jeden formát na všech
  produkčních výstupech — oddělovač se nemění jen na jedné straně.
- **Čtení.** Import packu i sdílený QA ingest přijímají `,` i `;`. Starý
  středníkový pack tedy zůstává platným vstupem bez konverze.
- **Detekce v portálu je deterministická, ne heuristická.** Parser zkusí
  hlavičku postupně jako `,` a jako `;` a vybere oddělovač, se kterým hlavička
  odpovídá známé sadě sloupců (nová devítisloupcová nebo legacy osmisloupcová).
  Když neodpovídá ani jedna, hlásí se dosavadní chyba `unexpected header`
  — nikdy se nehádá podle četnosti znaků.
- **Uvozování řeší `csv` modul, ne ruční skládání.** Buňka s `,`, `;`, `"`
  nebo koncem řádku (víceřádková odpověď, varianty, zdroje) se uzavře do
  uvozovek a vnitřní uvozovka se zdvojí. BOM a CRLF zůstávají kvůli Excelu.
- `domains` zůstává JSON polem v jedné buňce. Protože JSON pole obsahuje
  čárky, je tato buňka v čárkovém CSV vždy uvozená — round trip to nemění.

- `cat` zůstává jako legacy čitelný text (popisek kategorie), nerozhoduje.
- `category_code` přítomný a neprázdný → určuje kategorii.
- `category_code` přítomný a prázdný → **výslovně bez kategorie**.
- Sloupec `category_code` **zcela chybí** (starý formát) → ingest ponechá
  `category_id` beze změny u UPDATE a `NULL` u INSERT. Překlad starého `cat` na
  kód dělá **výhradně náhled importu v portálu** proti zkontrolovanému mapování
  nebo dotazem na uživatele; sdílený ingest nikdy nehádá a nezakládá kategorie.
- Rozpor mezi `cat` a `category_code` se v náhledu ukáže; `category_code` vyhrává.
- Přenášejí se **kódy, nikdy databázová ID**.

`domains` si ponechává dnešní význam na importní hranici: prázdná buňka dědí
metadata uploadu, `[]` výslovně vyprazdňuje, neprázdná sada nahrazuje.
**Editor posílá domény vždy explicitně jako pole** (i prázdné) — dědění je jen
vlastnost CSV hranice, ne API.

### 5.2 ZIP

`packs.zip`:
```
manifest.json
packs/<pack_id>.csv
```
`manifest.json` nese `legal_entity_code`, `exported_at`, `portal_version`,
`contract_version` a pro každý pack `pack_id`, `lang`, `version`, `pack_etag`,
`entry_count`, `metadata` (grades/subject/level/visibility/audience_roles/validity).

**Nikdy** neobsahuje hesla, tokeny ani `user_id`. `user_id` při importu určí
serverové mapování technického uploadera (§2.2).

Import ZIPu s **cizí** `legal_entity_code` v manifestu → `422
IMPORT_ENTITY_MISMATCH`. V MVP se packy mezi školami nepřenášejí.

CSV export se v UI i dokumentaci označuje jako **ne úplná záloha** (neobsahuje
všechna packová metadata).

### 5.3 Hromadný import — bez částečného zápisu uvnitř packu

- Výchozí režim `merge`. Chybějící řádky nic nemažou.
- Pack, který v importu **ještě neexistuje**, potřebuje potvrzená metadata
  (§2.4c). Bez nich je náhled čitelný, ale publikace toho packu se odmítne.
- `replace` jen pro správce, s explicitním seznamem deaktivovaných položek
  a samostatným potvrzením (`confirm_replace: true`).
- **Celý vstup se validuje před jakýmkoli zápisem**: struktura, povinné
  otázka/odpověď, duplicitní `entry_key` uvnitř packu, doménový katalog entity,
  `category_code`, přípustná metadata. Jediný chybný kód domény nebo kategorie
  → žádná QA změna v tom packu.
- U ZIPu se **negarantuje jedna transakce přes všechny packy**. Report uvádí
  stav per pack (`ok` / `failed` / `skipped`) a chybné packy lze bezpečně
  zopakovat samostatně. Úspěch se netvrdí dřív, než je pack potvrzený.
- Chyby uvádějí `file`, `row` (číslo řádku v souboru), `field` a srozumitelnou opravu.

**Upřesnění v1.0.0 (implementace C4a, statusy už byly v openapi.yaml):**

- Jeden CSV = právě jeden pack, takže chybný řádek znamená, že z tohoto importu
  nelze publikovat nic. `POST /imports/preview` proto vrací **422** s `fields`
  (`file`, `row`, `field`, `message`), nikoli 200 s `ok: false`. Klient tak
  nemůže omylem považovat odpověď za použitelný náhled.
- ZIP zůstává **200** s per-pack `ok`/`errors` — chybný pack lze zopakovat
  samostatně, zdravé packy se tím neblokují.
- `GET /exports/{export}/download` nemá v cestě školu a uložená metadata
  exportu vlastníka nenesou. Při dnešní `VpnAdminPolicy` (jeden správce pro
  všechny aktivní školy) to nic neomezuje; **jakákoli budoucí per-school
  policy musí nejdřív doplnit vlastníka do `export_service`**, jinak by
  kontrola na downloadu byla jen zdánlivá.

---

## 6. Operace a stav publikace

`POST …/publish` a `POST …/imports/{id}/publish` vracejí `202` s `operation_id`
(UUID minted portálem **před** voláním ingestu) a stavem.

```
validating → accepted → processing → completed | failed
```

- Stav je uložený v `qa_admin_state` (perzistentní svazek), klíčovaný
  `operation_id`, spolu s `school`, `pack`, `preview_id`, `base_etag`,
  `processing_uuid` (jakmile ho ingest vrátí) a shrnutím výsledku.
- `GET /qa-api/v1/operations/{id}` vrací **jen tuto** uloženou korelaci.
  **Zakázáno** odvozovat stav vlastní publikace z `GET /v1/ingest-status`
  podle `since_seconds` — to je globální hledání posledních běhů, ne párování.
  `ingest-status` se smí použít výhradně dotazem na **známé** `processing_uuid`.
- Stávající `POST /v1/ingest` je synchronní. Portál ho volá v odděleném
  vlákně/executoru, aby neblokoval obsluhu UI a API, a průběžně aktualizuje stav.
  Neslibuje se procentní průběh.
- **Hromadný import: stav operace musí odpovídat packům.** `completed` jen tehdy,
  když **žádný** pack neskončil `failed`. Jakmile selže aspoň jeden, operace je
  `failed` s kódem `IMPORT_PACKS_FAILED` a zprávou s počty; detail zůstává
  v `packs[]`. Dřívější chování hlásilo `completed` i tehdy, když selhaly
  všechny packy — správce viděl úspěch a data se nezměnila.
- **Nikdy nedoplňovat chybějící metadata existujícího packu.** Pokud stav packu
  nemá `visibility` nebo `audience_roles` (pack bez aktivních položek), portál
  publikaci **odmítne** `422` s polem `pack_metadata` a vyžádá si potvrzení jako
  u nového packu. Výchozí hodnota by tiše určila dosah obsahu (§2.4).
- `completed` se nastaví **až po ověření**, že ingest skončil úspěchem **a**
  následné čtení packu vrací očekávaný nový `pack_etag`. Zavření HTTP požadavku
  ani animace nejsou důkaz úspěchu.
- **Po restartu služby:** operace ve stavu `accepted`/`processing` se označí
  `unknown` a **nejprve dohledají** (podle `processing_uuid`, jinak porovnáním
  `pack_etag`). Nikdy se nespouštějí naslepo znovu. Při timeoutu se nezakládá
  další import.
- Do standardních logů se nepíší texty otázek a odpovědí — jen identifikátory
  (`operation_id`, `school`, `pack`, počty).

---

## 7. Chyby

Stejná obálka jako `contracts/errors.yaml`:
`{"error": {"code": …, "message": …, "retryable": bool}}`, u `422` navíc
`fields: [{field, message}]`. Klient větví na `code`, nikdy na text.

| HTTP | code | Kdy |
|---|---|---|
| 400 | `BAD_REQUEST` | Chybný tvar cesty/parametru |
| 403 | `FORBIDDEN` | Policy nepovolila školu nebo operaci |
| 404 | `NOT_FOUND` | Neznámá/neaktivní škola, pack, preview, operace, export |
| 409 | `PACK_VERSION_CONFLICT` | `base_etag`/`expected_etag` ≠ aktuální stav |
| 409 | `PACK_METADATA_DIVERGENT` | Položky packu mají různá metadata (§2.4) |
| 502 | `IMPORT_PACKS_FAILED` | Hromadný import: aspoň jeden pack skončil `failed` (detail v `packs[]`) |
| 409 | `PACK_ID_TAKEN` | `POST /packs` na už existující `pack_id` (kanonický kód, ne `PACK_EXISTS`) |
| 409 | `PUBLISH_IN_PROGRESS` | Pro (škola, pack) běží jiná publikace |
| 409 | `DELETE_CONFIRMATION_MISMATCH` | Opsaný identifikátor packu nesouhlasí (§13) |
| 413 | `PAYLOAD_TOO_LARGE` | Import nad limit (nginx i aplikace) |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Import není CSV/ZIP |
| 422 | `VALIDATION_ERROR` | Obsahová validace (pole `fields`) |
| 422 | `IMPORT_ENTITY_MISMATCH` | Manifest ZIPu míří na jinou školu |
| 422 | `UNKNOWN_CATEGORY` / `INACTIVE_CATEGORY` | Kód kategorie (§4.3) |
| 422 | `UNKNOWN_DOMAIN` | Kód domény mimo katalog entity |
| 500 | `INTERNAL_ERROR` | Neočekávaná chyba |
| 502 | `UPSTREAM_ERROR` | search/ingest nedosažitelný nebo selhal |
| 503 | `PUBLISH_UPLOADER_UNAVAILABLE` | Chybí/nelze ověřit technický uploader školy (retryable: false) |
| 503 | `UNAVAILABLE` | Závislost dočasně nedostupná (retryable: true) |

`retryable` odpovídá tomu, zda identické zopakování může pomoct.

---

## 8. Veřejné API portálu (`/qa-api/v1`)

Autoritativní tvary v `contracts/qa-portal/openapi.yaml`. Přehled:

| Metoda a cesta | Účel |
|---|---|
| `GET /context` | Režim, verze kontraktu, dostupné akce |
| `GET /schools` | Aktivní školy podle policy |
| `GET /schools/{school}/domains` | Účinný doménový katalog školy |
| `GET /schools/{school}/packs` | Packy, verze, `pack_etag`, `entry_count` |
| `GET /schools/{school}/metadata-options` | Číselníky pro metadata nového packu (§2.4a) |
| `POST /schools/{school}/packs` | Ověření nového `pack_id` + volitelná kontrola metadat (§2.4a); materializuje se až publikací |
| `GET /schools/{school}/packs/{pack}/entries` | Stránkovaný seznam položek + `pack_etag` |
| `GET /schools/{school}/packs/{pack}/entries/{entry_key}` | Detail jedné položky |
| `GET /categories` | Globální katalog `qa_category` s popisky |
| `POST /categories` | Založení globální kategorie |
| `POST /schools/{school}/packs/{pack}/preview` | Validace + rozdíl proti `base_etag` |
| `POST /schools/{school}/packs/{pack}/publish` | Publikace zkontrolovaného náhledu |
| `POST /schools/{school}/imports/preview` | CSV/ZIP: mapování, chyby, rozdíly |
| `POST /schools/{school}/imports/{preview}/publish` | Publikace zkontrolovaného importu |
| `GET /operations/{operation}` | Stav konkrétní operace |
| `POST /schools/{school}/exports` | Založení exportu (CSV/ZIP) |
| `GET /exports/{export}/download` | Stažení hotového exportu |

Pravidla: stránkované seznamy (`limit` ≤ 200, `cursor`), API nikdy nevrací HTML,
publikace použije **jen** obsah uložený v náhledu, nikdy tělo dodané při publikaci.

### CSRF / origin

UI a API jsou na stejném originu, CORS se neotevírá. Každý **zápisový** požadavek
musí být „nejednoduchý“: `Content-Type: application/json` (nebo `multipart/form-data`
u importu) **a** hlavička `X-QA-Portal: 1`. Server ověřuje `Origin`/`Referer`
proti povolenému seznamu a chybějící hlavičku odmítá `403`. Platí i bez loginu.

---

## 9. Interní API search služby (`/internal/qa-admin`)

Samostatný router, **mimo** nginx allowlist (`$search_path_allowed`), nesmí ho
publikovat žádný gateway route. Všechny dotazy nadále přes `scope.py`;
administrátorská potřeba se řeší **zde explicitně**, ne obcházením filtru
v portálu. Admin smí spravovat školy, ale při čtení konkrétního packu musí
odpovídat jeho vlastnická entita.

| Endpoint | Vrací |
|---|---|
| `GET /internal/qa-admin/pack-state?legal_entity&pack_id` | `pack_version`, `pack_etag`, `built_at`, packová metadata, `metadata_uniform`, `divergent_fields` |
| `GET /internal/qa-admin/entries?legal_entity&pack_id&limit&cursor` | Položky se vším: `entry_key`, `entry_id`, `canonical_q`, `answer_md`, `variants[]`, `sources[]`, `domains[]` (kódy), `category_code`, `category_label`, `category_is_active`, `cat_legacy`, `owner_user_id`, `visibility`, `audience_roles`, `school_grade_ids`, `school_subject_ids`, `valid_from`, `valid_to`, `is_active` |
| `GET /internal/qa-admin/uploader?legal_entity` | `{user_id, display_name}` — get-or-create dle §2.2 |
| `GET /internal/qa-admin/categories?lang` | Celý `qa_category` katalog včetně neaktivních |
| `POST /internal/qa-admin/categories` | Založení kategorie (kód, labels, sort_order, notes) |

Tvary v `contracts/qa-portal/internal-qa-admin.yaml`.

---

## 10. Frontend

- Vlastní build (Vite + React), base `/qa/`, vzor podle `web_chat/frontend`.
- Světlý krémový vzhled: `#fdf6ec` pozadí, `#1a1410` text, `#aea2f5` akcent,
  Nunito, jemný zelený akcent pro administrační kontext. Barvy a texty jako
  **frontendové tokeny** (`src/tokens.css`, `src/strings.cs.ts`), ne natvrdo.
- Tmavý režim je volitelný a **nesmí** sám přebít výchozí světlý vzhled podle
  systémového nastavení.
- Desktop: seznam vlevo, editor vpravo. Mobil: seznam → detail postupně.
- Markdown náhled bezpečně (`react-markdown` + `rehype-sanitize`), žádné
  spustitelné HTML. Raw HTML se **nepovoluje** (žádný `rehype-raw`): surová
  značka se nevykreslí.
- **Náhled nesmí obsah spolknout mlčky.** Když se kus odpovědi kvůli bezpečnosti
  nevykreslí, náhled to musí **viditelně říct** (krátká poznámka u náhledu),
  aby administrátor nedostal prázdnou plochu bez vysvětlení a nemyslel si, že
  přišel o text. Platí i pro odpověď načtenou z API, nejen pro rozepsanou.
- Rozpracované změny v paměti prohlížeče, výrazně signalizované. Přepnutí otázky
  úpravy zachová; přepnutí školy/packu nebo zavření stránky před tichou ztrátou
  chrání potvrzením. Odpověď staršího načtení školy **nesmí** přepsat novější
  výběr (řízeno request generací/AbortController).
- Dostupnost klávesnicí, viditelný focus.

### 10.1 Modální dialog — skutečné rolování obsahu (v1.4.0)

Platí pro společný plášť `Dialog`, tedy i pro **Nový balíček**.

- Dialog má tři pásma: **hlavička**, **rolovatelný obsah**, **patička s
  potvrzovacími tlačítky**. Hlavička a patička zůstávají viditelné vždy;
  roluje se jen prostřední pásmo.
- Rolování musí skutečně fungovat, ne jen být nastavené v CSS. Grid dialogu
  proto definuje řádky explicitně (`auto minmax(0, 1fr) auto`), aby prostřední
  stopa mohla být nižší než její obsah; samotné `min-height: 0` na položce
  na to nestačí a obsah se jen ořízne.
- Obsah se musí dát posunout **kolečkem, dotykem i klávesnicí**. Rolovací
  pásmo je proto vlastní fokusovatelná oblast (`tabindex="0"`, pojmenovaná),
  ale **nepřebírá počáteční fokus** — ten patří dál prvnímu ovládacímu prvku
  formuláře.
- Na nízkém desktopovém okně i na mobilu musí být dosažitelná **poslední
  položka nejdelšího výčtu (ročníky)** i **potvrzovací tlačítko**. Důkazem je
  browser test s reálným rozměrem okna, ne jednotkový test v jsdom — ten
  rozměry neměří a dosažitelnost dokázat nemůže.
- Bez redesignu: rozložení, barvy ani texty se nemění.
- Do integrace staví proti **zmrazeným JSON fixturám** odvozeným z tohoto
  kontraktu (`qa_portal/frontend/src/fixtures/`), ne proti živému backendu.

---

## 13. Trvalé smazání (otázka a pack)

Vedle deaktivace (`is_active=false`), která zůstává výchozí cestou, umí portál
obsah **trvale odstranit z databáze**. Je to nevratné a nemá undo.

### 13.1 Co se maže a co ne

| Maže se | Zůstává |
|---|---|
| `qa_entries` vybrané položky (u packu všechny jeho položky) | `code_list` a všechny sdílené číselníky |
| `qa_variants` — přes FK `ON DELETE CASCADE`, tedy i neaktivní | `question_answer_log` včetně `matched_entry_id` (FK neexistuje, řádek zůstane s odkazem na již neexistující položku — provozní audit se nepřepisuje) |
| `qa_embedding` podle `entry_id`, **všechny generace**, aktivní i ne | `embedding_registry` (globální registr generací, ne historie položky) |
| u celého packu navíc **všechny** řádky `qa_pack_manifest` pro danou dvojici (škola, pack) | dokumenty, chunky a cokoli mimo QA |

Neaktivní řádky se mažou taky: „trvale" znamená včetně historie, jinak by
zůstaly matchovatelné přes částečné indexy po reaktivaci.

### 13.2 Pravidla

- **Transakce.** Celé smazání je jeden `BEGIN`/`COMMIT`. Chyba uprostřed nesmí
  nechat položku bez variant nebo pack bez manifestu.
- **Kontrola školy.** Každý příkaz má v `WHERE` `legal_entity_code`. Pack ani
  položka jiné školy se nesmí smazat ani zobrazit — stejné pravidlo jako u čtení.
- **Ochrana zápisu.** Platí `X-QA-Portal` i kontrola originu (§8), stejně jako
  u publikace.
- **Zámek.** Smazání drží stejný `PackLock` pro (škola, pack) jako publikace,
  po celou dobu. Souběžná publikace dostane `409 PUBLISH_IN_PROGRESS` a naopak.
- **Potvrzení.** Klient musí před smazáním zobrazit školu, název a rozsah
  (počet položek). U **celého packu** musí administrátor **opsat jeho
  identifikátor**; server neshodu odmítne `409 DELETE_CONFIRMATION_MISMATCH`.
- **Zneplatnění náhledů.** Po smazání server zahodí všechny uložené náhledy pro
  danou dvojici (škola, pack). Publikace staršího náhledu tak nemůže smazaný
  pack ani otázku vzkřísit. Pokud by přesto dorazila, skončí na neshodě etagu
  (§3), nikdy tichým znovuvytvořením.
- **Bez archivace.** Nic se nekopíruje do jiné tabulky. Zálohou je záloha databáze.

### 13.3 API

| Metoda | Cesta | Význam |
|---|---|---|
| `DELETE` | `/qa-api/v1/schools/{school}/packs/{pack}/entries/{entry_key}` | jedna otázka |
| `DELETE` | `/qa-api/v1/schools/{school}/packs/{pack}` | celý pack; tělo `{"confirm_pack_id": "<pack>"}` |

Obě vrací `200` s počty smazaných řádků
(`{"deleted": {"entries": n, "variants": n, "embeddings": n, "manifests": n}}`),
`404 NOT_FOUND` pro cizí nebo neexistující školu/pack/otázku.
Interně portál volá `DELETE /internal/qa-admin/entries` a
`DELETE /internal/qa-admin/packs` search služby; ta jediná sahá na databázi.

---

## 11. Co je mimo rozsah

AI generování odpovědí, schvalovací workflow, XLSX, slučování duplicit, široká
editace taxonomie, trvalé serverové koncepty, historie verzí položek, přenos
packů mezi školami, backfill kategorií (etapa 2), přepnutí `category_siblings`
na `category_id` (etapa 2), sdílený zámek pro více replik.

---

## 12. Povinné kontroly před dokončením

`make docs-check`; frontend build a unit testy; testy policy, scope a domén;
CSV/ZIP round-trip včetně diakritiky, Markdownu, uvozovek, středníků, zdrojů,
domén a `category_code`; neúplný import nezapíše nic; `merge` nedeaktivuje
vynechané řádky; zastaralý etag nepropíše zápis; zachování `entry_key`/`entry_id`,
vlastníka a metadat; integrita kategorií a překladů; `tests/eval_rag.py --repeat 3`
před i po (včetně souvisejících otázek); paritní kontrola normalizace, pokud by se
normalizace dotklo (jinak se normalizace kvůli portálu **nemění**);
browser flow podle §10.
