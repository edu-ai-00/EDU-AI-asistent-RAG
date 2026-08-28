# QA test packs — pět škol, dva balíčky každá

Kurátorské QA balíčky pro **komplexní test QA fast-path na produkci**: 313 položek
v 10 balíčcích, otázky postavené na tom, co je na produkci naingestované, a k tomu
413 probe případů, které kontrolují, **který balíček odpověděl**.

Nahrazuje čtyři ruční balíčky `QA/pack_ZS_*.csv` (10 položek každý). Ty jsou pořád
v adresáři — než tyhle nahraješ, přečti si [Než nahraješ](#než-nahraješ).

## Co je v balíčcích

| entita | role v testu | `provoz-*` | `prijimacky-*` |
|---|---|---|---|
| `SKOLA_NOVA_VES` | organizace (rodič) | 33 pol., `organization`, 9999 | 32 pol., `organization`, 9999, **jen STUDENT/PARENT** |
| `SKOLA_NOVA_VES_ZS` | vlastní ZŠ | 33 pol., `entity`, 9999 | 32 pol., `organization`, ročník 5/7/9 |
| `SKOLA_NOVA_VES_SOS` | sourozenecká SOŠ | 33 pol., `entity`, 9999 | 20 pol., `organization`, ročník 12/13 |
| `ZAKLADNI_SKOLA_BOROVA` | cizí škola | 33 pol., `entity`, 9999 | 32 pol., `entity`, 5/7/9 |
| `ZAKLADNI_SKOLA_LIPOVA` | cizí škola | 33 pol., `entity`, 9999 | 32 pol., `entity`, 5/7/9 |

Každá odpověď začíná značkou scope — `[ZAKLADNI SKOLA]`, `[CELA ORGANIZACE]`,
`[STREDNI SKOLA]`, `[BOROVA]`, `[LIPOVA]`. To je celý princip testu:
otázka je ve všech pěti balíčcích **stejná**, takže o vítězi rozhoduje výhradně
scope a proximity tier, a značka v odpovědi řekne, který balíček to byl.

**Témata přijímaček** kopírují to, co produkce má naingestované: termíny a zkušební
schéma JPZ, obsah a délka didaktických testů, specifikace požadavků, přihláška
a DiPSy, pořadí oborů, váha JPZ v kritériích, uzpůsobení podmínek (PUP, cizinci),
testová zadání k procvičování, SCIO, víceletá gymnázia, odvolání. SOŠ balíček je
z druhé strany — přijímací řízení jako přijímající škola, plus maturita a VŠ.

**Provoz** kopíruje školní řád a *O studiu* z skola-nova-ves.example a metodický pokyn MŠMT
k šikaně: docházka, hodnocení, mobily, pomůcky, jídelna, šikana, GDPR, akce, podpora.

## Zdroje u odpovědí

Balíčky nesou sloupec `sources` → `qa_entries.asset_refs` → `SearchResponse.sources`,
takže QA odpověď se ve web chatu vykreslí i s rozbalovacím **Zdroje (n)**, stejně
jako odpověď z RAGu. Jeden zdroj na řádek buňky, `Titulek | URL`; titulek se smí
vynechat a zastoupí ho URL.

Mapování je v `packs_spec.py` (`SRC` / `NATIONAL` / `OWN` / `sources_for`) a drží
tři pravidla:

* **Jen reálné URL.** Každé je v `../reingest_sources.json`, tedy stránka, kterou
  produkce opravdu naingestovala. Odpovědi jsou syntetické, ale zdroj je tvrzení
  o světě — vymyšlený odkaz je od halucinace nerozeznatelný ve chvíli, kdy se
  objeví pod „Zdroje“.
* **Vlastní dokument jen u vlastní školy.** skola-nova-ves.example visí pouze na Nové Vsi
  horách, skola-lipova.example pouze na Lipové škole. Cizí odkaz by byl přesně
  ten únik napříč nájemci, který tahle sada hledá, jen v hávu odkazu místo značky.
* **Celostátní zdroje (Cermat, MŠMT, EduIn) jedou všem.**

Borová nemá naingestované nic vlastního, takže její provozní balíček je z 28
z 33 položek bez zdroje — což je zároveň jediný způsob, jak je v sadě pokrytý
i prázdný případ. Kategorie `Maturita` nemá zdroj u nikoho ze stejného důvodu.

`check_packs.py` odmítne řádek zdroje, který by reader zahodil nebo špatně
otitulkoval, i dvakrát citované totéž URL.

## Osy, které to testuje

| případ | co ověřuje |
|---|---|
| `scope_proximity_*` | vlastní škola před organizací — regrese z `b127ea9` |
| `scope_org_reach_*` | otázka jen v org balíčku doteče do obou jednotek |
| `scope_sibling_isolation` | ZŠ nedostane odpověď SOŠ balíčku (`entity` viditelnost) |
| `scope_tenant_isolation_*` | Nová Ves ↔ Borová, žádný přesah |
| `scope_grade_in/out` | šesťák nevidí balíček pro ročník 5/7/9, deváťák ano |
| `scope_level_in/out` | SS balíček neodpoví ZŠ volajícímu, ačkoli ho vidí |
| `scope_role_*` | `prijimacky-org` bez TEACHER — filtr rolí na úrovni balíčku |
| `exact_*` | jedna položka = jeden případ, každá položka je dosažitelná |
| `norm_*` | varianta bez diakritiky, malými písmeny — redukovaná forma `norm` |
| `para_*` | formulace, kterou balíček nemá — `qa_lexical` / `qa_vector` |
| `typo_*` | překlep — `pg_trgm` smí navrhnout, nikdy sebejistě odpovědět |
| `rag_*` | QA přebíjí RAG tam, kde obojí ví; a nepohltí to, co neví |

Každý případ navíc **zakazuje značky ostatních čtyř škol**, takže únik napříč
nájemci spadne bez ohledu na to, kde v kaskádě vznikl.

## Postup

```bash
python3 QA/build_packs.py     # QA/packs/*.csv + _manifest.json
python3 QA/build_probe.py     # QA/probe_cases.json
python3 QA/check_packs.py     # kontrola dřív, než to gateway vrátí jako 400

python3 scripts/qa_deactivate_packs.py --all --go   # smést stůl (viz níž)

python3 scripts/upload_qa_packs.py --dir QA/packs                 # dry run
python3 scripts/upload_qa_packs.py --dir QA/packs --go --mode replace

SEARCH_URL=http://127.0.0.1:8090 python3 scripts/qa_pack_probe.py
python3 scripts/qa_pack_probe.py --only scope     # jen scoping matice
python3 scripts/qa_pack_probe.py --json > qa_probe_run.json
```

`--mode replace` dělá z balíčku přesně to, co je v souboru — to chce reprodukovatelný
měřicí korpus. `merge` (výchozí) nechá být položky, které soubor nezmiňuje.

`check_packs.py` kontroluje i to, co selže tiše: BOM, sniffnutý oddělovač (odpovědi
jsou plné čárek, kdyby jich bylo víc než středníků, celý soubor by se načetl jako
jeden sloupec), neuzavřené uvozovky, kolizi odvozeného `entry_key` a překryv otázek
mezi dvěma balíčky jedné školy.

## Než nahraješ

**1. Staré balíčky se překrývají.** `provoz-*` používá záměrně stejné kanonické
otázky jako `QA/pack_ZS_*.csv`. Pokud zůstanou aktivní, `qa_duplicate_questions()`
(migrace 142) to nahlásí a `qa_exact` bude mezi nimi vybírat podle `entry_id` —
stabilně, ale libovolně. Nejčistší je smést stůl **před** nahráním:

```bash
python3 scripts/qa_deactivate_packs.py            # co je nahrané
python3 scripts/qa_deactivate_packs.py --all      # dry run
python3 scripts/qa_deactivate_packs.py --all --go # zneaktivnit vše
```

Kdyby byly nové balíčky nahrané dřív, `--keep provoz-zs --keep …` je ušetří.
Je to soft delete (`is_active = false`), stejná invalidace, jakou používá ingest,
takže historie i `question_answer_log.matched_entry_id` zůstávají celé.

**2. Přijímačky přebijí RAG.** Kurátorská odpověď vrací `answer_md` doslova
a zastaví kaskádu, takže na otázky z produkčního baseline `Q4`, `Q4b`, `N16`,
`N17`, `N18` a `Q6` (`eval/baseline_prod_20260820.json`) odpoví nově balíček,
ne dokumenty Cermatu. Než budeš znovu měřit RAG, `prijimacky-*` zneaktivni —
`provoz-*` na ty případy nesahá.

**3. Odpovědi jsou testovací data.** `qa_exact` je vrací uživateli doslova
a s autoritou kurátorské odpovědi. Značka `[SCOPE]` je dělá rozpoznatelnými,
ale nenechávej je na produkci déle, než trvá test.

**4. Domény deklarují jen jednotky Nové Vsi.** Organizace, Borová ani
Základní škola Lipová nemají v `taxonomy_rules` vlastní DOMAIN řádky, takže jejich
balíčky žádné domény neposílají.

## Soubory

| soubor | co je |
|---|---|
| `packs_spec.py` | provoz — otázky, varianty, odpovědi po entitách |
| `packs_spec_prijimacky.py` | přijímačky, maturita, VŠ |
| `build_packs.py` | → `packs/*.csv` + `packs/_manifest.json` |
| `build_probe.py` | → `probe_cases.json` |
| `check_packs.py` | validace proti pravidlům readeru a migrací 142/143/152 |
| `../scripts/qa_pack_probe.py` | runner — ptá se za volajícího, kontroluje značku |
| `../scripts/qa_deactivate_packs.py` | výpis a zneaktivnění balíčků (API na to není) |
