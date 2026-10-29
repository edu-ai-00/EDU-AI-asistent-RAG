# Neutrální odpovědi — dokumentace a plán dalšího vývoje

*Stav k 21. 10. 2026, větev `feat/neutral-answers-lab`. Technický popis spuštění je v [README.md](README.md).*

## 1. K čemu nástroj slouží

Kurátorovaná odpověď musí sedět na **jakoukoli formulaci otázky** a na **kohokoli, kdo se ptá** (žák, rodič, učitel).
Nemá začínat „Ano/Ne“, nemá tykat jednomu člověku, nemá mluvit v 1. osobě a u „kde najdu…“ má uvést odkaz.
Tak to bylo rozhodnuto na schůzce s vedoucím.

Nástroj ke každé odpovědi **navrhne neutrální znění podle pevných pravidel** (bez jazykového modelu). Původní a navržené znění ukáže vedle sebe a kurátor návrh schválí, zamítne nebo ručně upraví. Navíc ukazuje **strojový pohled**: jak z otázek a odpovědí vidí text vyhledávání (lemma, kmen, stop-slova, ztracený zápor).

## 2. Co je hotové

| Oblast | Stav |
|---|---|
| Načtení Excelu ve formátu QA sady (`canonical_q`, `answer`, `sources`, `Alternativa 1…n`) | hotovo |
| Načtení „volného“ listu (otázka ve sloupci A, odpověď ve sloupci G) | hotovo, ale sloupce jsou pevné (viz 7.) |
| Pravidla: typografie, smajlíky, „by jste“, 1. osoba, tykání → vykání, „Ano/Ne“ na začátku, slova pro roli → „uchazeč“ se shodou slovesa, chybějící odkaz (jen upozornění: zdroj bez adresy nebo jen s úvodní stránkou; adresa se do textu nepíše, chat zdroje zobrazuje sám), „nevím“ → rada, kde se doptat | hotovo |
| Tabulka: otázky a alternativy, původní a navržená odpověď s vyznačenými rozdíly, štítky pravidel, stav | hotovo |
| Schválit / zamítnout / ručně upravit / vrátit, hromadné schválení automatických změn | hotovo |
| Strojový pohled: norm / lemma / kmen, vyřazená stop-slova, varování (zápor, diakritika, neznámá slova) | hotovo |
| Ukládání rozhodnutí mimo Git, rozpoznání změny odpovědi ve zdroji | hotovo |
| Export: přehled změn (CSV) a celá QA sada ve formátu aplikace (CSV); nové znění jen u schválených | hotovo |
| Načtení CSV QA sady ve formátu aplikace (export z QA portálu, `/v1/qa-export`), převzetí `entry_key`, `cat`, `category_code`, `domains`, `is_active` | hotovo (22. 10.) |
| Testy (`tests/test_neutral_answers.py`, 31 testů) | hotovo |

Vyzkoušeno na `faq_jpz_9-26.xlsx` (110 odpovědí, 32 s navrženou změnou) a na listu „Přijímačkymaturita“ z `Otázky EDU.xlsx` (69 odpovědí, 52 se změnou nebo upozorněním).

## 3. Jak to funguje (stručně)

```
Excel ──► pravidla navrhnou neutrální text ──► kurátor v prohlížeči schválí / upraví
                                                   │
             soubor s rozhodnutími (~/edu-ai/state/neutral_answers/) ◄─┘
                                                   │
                         export CSV ──► nahrání QA sady do systému (scripts/upload_qa_packs.py / QA portál)
```

Aplikace běží v Docker kontejneru z obrazu vyhledávání (`edu_ai_search`), takže strojový pohled používá stejnou lemmatizaci jako asistent. Do databáze ani do vyhledávání nezasahuje.

## 4. Vstupy

### 4.1 Co nástroj čte dnes

| Vstup | Formát | Poznámka |
|---|---|---|
| Sada otázek a odpovědí | `.xlsx`, první list nebo `--sheet` | hlavička `canonical_q, answer, sources, Alternativa…`, nebo otázka v A a odpověď v G |
| Seznam stop-slov a nastavení stemmeru | `contracts/parity/stoplist.json` | snímek z knihovny, ne živá data z databáze |
| Neutrální podmět | parametr `--subject` | `uchazeč` (výchozí) nebo `žák` |

### 4.2 Co je potřeba doplnit

| Vstup | Proč | Návrh |
|---|---|---|
| **Volitelné sloupce otázky a odpovědi** | List „Mat“ má odpovědi ve sloupci **H**, „Učitelé“ otázky ve sloupci C. Dnes je nástroj nenačte. | parametry `--q-col C --a-col H`, nebo automatické rozpoznání |
| **Řádky bez odpovědi** | viz kapitola 6; dnes se přeskakují | načíst je jako položky se stavem „chybí odpověď“ |
| **Kategorie, oblast (domains), ročník, škola** | QA sada je bez nich neúplná. Dnes je export nechává prázdné. | sloupce `cat`, `domains`, `school_grade` v Excelu, nebo jejich výběr v aplikaci |
| **Export z databáze** | po nahrání dat do systému by se mělo kontrolovat i to, co už v systému je | načtení přes `/v1/qa-export` (CSV) jedné QA sady |
| **Živá stop-slova z databáze** | aby strojový pohled odpovídal tomu, co je v `edu_ai.qa_stopword` | volitelné připojení k databázi jen pro čtení |
| **Vlastní tabulky pravidel** | slova pro roli, tvary sloves a šablony jsou teď přímo v kódu | přesunout je do souboru s daty (JSON/CSV), aby šly upravit bez programování |

## 5. Výstupy

### 5.1 Co nástroj vytváří dnes

| Výstup | Kde | Obsah |
|---|---|---|
| Soubor s rozhodnutími | `~/edu-ai/state/neutral_answers/<sešit>.decisions.json` | stav, ruční úprava, poznámka, otisk původní odpovědi, čas |
| Přehled změn | tlačítko „⇩ Přehled (CSV)“ | všechny položky: původní a výsledné znění, stav, pravidla, upozornění |
| QA sada | tlačítko „⇩ QA sada (CSV)“ | celá sada ve formátu `search/app/qa_csv.py`; nové znění jen u schválených, ostatní v původním. Z CSV se sloupce sady vrací beze změny; z Excelu zůstává `entry_key` prázdný (odvodí se z otázky) a `domains` prázdné (převezmou se oblasti z nahrávání) |

### 5.2 Co je potřeba doplnit

| Výstup | Proč | Návrh |
|---|---|---|
| **Kategorie a oblasti u sady z Excelu** | čtečka aplikace je nevyžaduje (prázdné `domains` = oblasti z nahrávání, chybějící `category_code` = kategorie beze změny), ale pro hledání podle oblastí a pro QA portál se hodí | doplnit podle 4.2 |
| **Manifest k nahrání** | `scripts/upload_qa_packs.py` čte metadata (škola, nahrávající, viditelnost, role, ročníky) z `_manifest.json` | vytvořit `_manifest.json` spolu s CSV |
| **Seznam otázek k zodpovězení** | kapitola 6: předat chybějící odpovědi někomu dalšímu (učiteli, vedení) | export „k zodpovězení“ (CSV/Excel) |
| **Návrhy pro strojovou část** | strojový pohled nachází chybějící synonyma, lemmata, zápory | export návrhů jako nová migrace v `postgres/migrations/` (od čísla 217, bez `BEGIN/COMMIT`, s vlastní kontrolou, s manifestem vydání) |
| **Souhrnná zpráva** | pro vedoucího: kolik odpovědí, kolik změn, která pravidla, co čeká | stránka nebo PDF se statistikami |

> Poznámka: kurátorované odpovědi (QA) se do systému **nedostávají migracemi**, ale nahráním QA sady.
> Migrace jsou jen pro nastavení: synonyma, stop-slova, prompty.

## 6. Nová funkce: otázky, na které je potřeba ručně zadat odpověď

### 6.1 Proč

Ve zdrojových datech je **většina otázek bez odpovědi**:

| Soubor / list | Otázek | S odpovědí | **Bez odpovědi** |
|---|---|---|---|
| `Otázky EDU.xlsx` / Přijímačkymaturita | 364 | 69 | **295** |
| `Otázky EDU.xlsx` / Mat | 392 | 104 | **288** |
| `Otázky EDU.xlsx` / ČJ | 337 | 0 | **337** |
| `Otázky EDU.xlsx` / Učitelé | 146 | 5 | **141** |
| `Otázky EDU.xlsx` / Brainly (anglicky) | 101 | 101 | 0 |
| `faq_jpz_9-26.xlsx` | 110 | 110 | 0 |

Dnes nástroj tyto řádky přeskočí, takže o nich kurátor neví.

### 6.2 Co má funkce umět

1. **Načíst i otázky bez odpovědi** a ukázat je se stavem **„chybí odpověď“**. Mají vlastní kartu ve statistikách a vlastní filtr.
2. **Pole pro ruční odpověď** přímo v tabulce: větší textové pole a pod ním pole pro zdroj (titulek a adresa).
3. **Okamžitá kontrola napsaného textu.** Při psaní (nebo po kliknutí na „Zkontrolovat“) se na text pustí stejná pravidla a ukáže se navržené neutrální znění a upozornění. I ručně psaná odpověď tak bude v jednotném stylu.
4. **Seskupení podobných otázek.** Mnoho otázek se ptá na totéž („Jak se připravit na přijímačky?“, „Kdy se začít učit na příjmací zkoušky?“). Nástroj je podle společných lemmat seskupí a nabídne napsat **jednu odpověď pro celou skupinu**. Ostatní otázky se uloží jako alternativy.
5. **Další stavy:**
   - **mimo rozsah:** otázka na učivo („Proč je integrál opakem derivace?“) nebo názorová („Je správné, aby…?“). Nepatří do kurátorovaných odpovědí, řeší ji vyhledávání v dokumentech.
   - **chybí informace:** odpověď zatím nikdo nezná. Uloží se standardní text „Tato informace zatím není k dispozici…“ s odkazem, kde se doptat (podle rozhodnutí ze schůzky).
   - **duplicita:** stejná otázka už jinde odpověď má, nástroj na ni odkáže.
6. **Pořadí práce:** nejdřív otázky, které se ptají nejčastěji (nejvíc podobných ve skupině), a otázky z provozní oblasti školy (přijímačky, termíny, omluvenky) před otázkami na učivo.
7. **Export „k zodpovězení“:** seznam otevřených otázek do Excelu, aby šel poslat někomu, kdo odpověď zná. Vyplněný soubor půjde nahrát zpět.
8. **Ukládání** stejně jako dnes: do souboru s rozhodnutími, ruční odpověď s autorem a časem.

### 6.3 Návrh obrazovky

```
┌ Chybí odpověď 295 ┐  ┌ Mimo rozsah 0 ┐  ┌ Chybí informace 0 ┐  … ostatní karty jako dnes
Skupina 12 · 5 podobných otázek
  Kdy se začít učit na příjmací zkoušky?
  ↳ Jak se připravovat na přijímací zkoušky na šestileté gymnázium?   [ponechat ve skupině ✕]
  ↳ …
  Odpověď:  [                                                     ]
  Zdroj:    [ Cermat | https://prijimacky.cermat.cz/              ]
  Návrh po kontrole:  „Doporučuje se připravovat pravidelně od začátku školního roku.“  ⚠ …
  [Uložit odpověď]  [Mimo rozsah]  [Chybí informace]  [Duplicita…]
```

## 7. Nová funkce: alternativy ze skutečných dotazů

### 7.1 Proč

Záznam dotazů (`question_answer_log`) je nejvěrnější obraz toho, jak se lidé ptají. Přehrání záznamu z provozu (4 442 dotazů, nástroj `tools/lab/replay`) ukázalo, že velká část dotazů, které asistent nezodpoví jistě, nemá chybu ve vyhledávání, ale v datech:

- **chybí alternativa:** „Kdy jsou náhradní termíny přijímacích zkoušek?“ (12×) skončí jen nabídkou, správná odpověď „Kdy se konají náhradní termíny JPZ?“ existuje,
- **dotaz je dvojznačný:** „berou ve škole telefony“ může znamenat „smí mít dítě mobil?“ i „zvedá škola telefon?“; nemá být přiřazen k jedné odpovědi,
- **odpověď chybí úplně:** na dotaz žádná kurátorovaná odpověď není.

Úpravy rozhodovací logiky, které se nabízely (nerozhodné pořadí podle pokrytí, váha slova bez ohledu na zápor), byly změřeny a zamítnuty: zhoršily víc dotazů, než opravily. Zbylé případy se proto řeší daty, ne pořadím.

### 7.2 Co má funkce umět

1. **Vstup:** výsledek přehrání nové verze (`tools/lab/replay`) a QA sady škol (CSV z portálu nebo z dumpu). Vybírají se dotazy, které skončily **jen nabídkou** nebo **ničím**.
2. **Seskupení:** stejné a skoro stejné dotazy (stejná lemmata) se spojí do jednoho řádku s počtem výskytů a se všemi zněními. Řadí se od nejčastějších. Navazující dotazy („A kolik to stojí?“) se vynechají: bez konverzace nedávají smysl.
3. **Návrh:** u každé skupiny odpovědi, které vyhledávání nabídlo, s pokrytím. Navrhuje se jen z odpovědí školy, ke které dotaz patří.
4. **Tři volby kurátora:**
   - **přidat jako alternativu** k vybrané odpovědi; nástroj předem změří, že to pomůže a jiné otázky to nerozbije,
   - **dvojznačný:** nepřidá se nikam, zapíše se, které odpovědi má nabídka obsahovat; chybí-li některá (např. kontakt na školu), nástroj na to upozorní,
   - **chybí odpověď:** dotaz se zařadí k otázkám bez odpovědi (kapitola 6).
5. **Výstup:** QA sada s novými alternativami (stejný export jako dnes) a seznam dvojznačných dotazů a chybějících odpovědí (CSV).
6. **Ověření:** po nahrání upravené sady nové přehrání ukáže, kolik dotazů se zlepšilo.

### 7.3 Postup

| krok | výsledek |
|---|---|
| 1 | seskupení a návrh jako tabulka CSV, samostatný modul s testy — **hotovo** (`log_queue.py`) |
| 2 | karta v nástroji: seznam skupin, volby, ukládání rozhodnutí — **hotovo** (`/queue.html`, spuštění s `--queue`) |
| 2b | **skupiny podle tématu** — **hotovo** (`topics.py`, spuštění s `--topics`): rozhoduje se jednou za skupinu; uvnitř skupiny modrý pruh spojuje dotazy, které vyhledávání bere jako stejné (stejná metoda jako QA: lemmata, synonyma, váha slov); dotaz lze přesunout do jiné nebo nové skupiny, zapsat jako „chybí odpověď“ nebo odložit; přehled `/export/queue.csv` |
| 3 | export upravené sady, nové přehrání a srovnání |

### 7.4 Ochrana dat

Dotazy uživatelů zůstávají jen v lokálním stavu (`~/edu-ai/state/`), do Gitu ani do exportu se jako text nedostanou, s výjimkou těch, které kurátor sám schválí jako alternativu.

## 8. Známá omezení a co dodělat

Podle priority:

1. **Funkce z kapitoly 6** (otázky bez odpovědi).
2. **Volitelné sloupce** u volného listu (list „Mat“ má odpovědi ve sloupci H a dnes se nenačte).
3. **Doplnění kategorie a oblastí** pro export QA sady a vytvoření `_manifest.json`.
4. **Shoda ve složitějších větách:** pravidla vymění „dcera“ za „uchazeč“, ale „byla nemocná“ neopraví. Tyto změny jsou proto označené „zkontrolovat“. Úplné řešení by potřebovalo nástroj, který umí tvary slov vytvářet (např. MorphoDiTa; pozor, modely mají licenci jen pro nekomerční použití).
5. **Krátké odpovědi po odstranění „Ano/Ne“** („Můžete.“): pravidlo jen upozorní, přeformulovat musí kurátor.
6. **Tabulky pravidel do datového souboru**, aby je šlo upravit bez programování.
7. **Dvě sady najednou:** druhé spuštění potřebuje jiný port a název kontejneru (`NEUTRAL_ANSWERS_PORT`, `NEUTRAL_ANSWERS_NAME`).
8. **Strojový pohled používá snímek stop-slov**, ne živá data z databáze (pro češtinu jsou dnes stejná).
9. **Bez přihlašování:** nástroj je dostupný jen z vlastního počítače (`127.0.0.1`), pro sdílení s dalšími lidmi by potřeboval přihlášení.
10. **Změny jsou v Gitu ve větvi `feat/neutral-answers-lab`**, zatím nesloučené do `main`.

## 9. Otevřené otázky pro vedoucího

- Tykání, nebo vykání? Nástroj umí obojí (`--osloveni ty|vy|beze`); je potřeba rozhodnout, co platí pro které sady.
- Jaký podmět použít mimo přijímačky? „Uchazeč“ sedí k JPZ, jinde spíš „žák“, nebo neosobně? (Provozní sada jedné ZŠ: „dítě samo rozvrhne“ → „uchazeč samo…“ je chybně tvarem i smyslem.)
- Jak zacházet s dvojznačnými dotazy: nabídka obou výkladů, nebo rozcestník (jedna odpověď pokrývající oba)?
- Kdo bude odpovídat na otázky bez odpovědi a v jakém formátu je má dostat?
- Patří otázky na učivo (Mat, ČJ) do kurátorovaných odpovědí, nebo je nechat na vyhledávání v dokumentech?
- Které kategorie a oblasti (taxonomie) přiřadit sadě k JPZ před nahráním?
- Má být nástroj časem součástí QA portálu (`qa_portal/`), nebo zůstat samostatný?

## 10. Soubory

| Soubor | Co obsahuje |
|---|---|
| [`run.sh`](run.sh) | spuštění v Docker kontejneru |
| [`server.py`](server.py) | webový server, ukládání rozhodnutí, exporty |
| [`rules.py`](rules.py) | pravidla neutrálního přepisu |
| [`machine.py`](machine.py) | strojový pohled (lemma, kmen, varování) |
| [`xlsx_read.py`](xlsx_read.py) | čtení Excelu bez dalších knihoven |
| [`static/index.html`](static/index.html) | webová stránka |
| [`log_queue.py`](log_queue.py) | dotazy z provozu bez jisté odpovědi, seskupené |
| [`topics.py`](topics.py) | skupiny podle tématu a dotazy, které vyhledávání bere jako stejné |
| [`static/queue.html`](static/queue.html) | karta Dotazy z provozu |
| [`../../../tests/test_neutral_answers.py`](../../../tests/test_neutral_answers.py) | testy |
