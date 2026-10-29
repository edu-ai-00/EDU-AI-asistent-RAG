# Kvalita hledání odpovědí bez OpenAI klíče — plán dalšího postupu

*Stav k 27. 10. 2026, větev `feat/neutral-answers-lab`. Technický popis měřicího nástroje je v [README.md](README.md).*

## 1. Cíl a omezení

**Cíl:** aby asistent co nejlépe našel správnou kurátorovanou odpověď **bez OpenAI klíče**. K OpenAI se nepřechází: směr je provoz, který klíč nepotřebuje (rozhodnutí 28. 10. 2026). Stávající `qa_vector` a `rag_vector` (vektory od OpenAI) proto zůstávají nečinné (dnes bez klíče nefunguje hledání podle významu, dokumenty přes embeddingy ani generování odpovědí; odpověď z QA se vrací doslova a spoléhá jen na slova).

**Omezení zadaná při práci — plán je musí dodržet:**

1. **Žádné nové odpovědi ani alternativy psané ručně.** Zlepšení se nesmí opírat o to, že někdo napíše další znění otázek.
2. **Žádná velká změna původního repozitáře.** Změny jen jako volitelný krok nebo parametr, ve výchozím stavu vypnutý, malé a vratné.
3. **Změřit, než navrhnu; změnu rozhodovací logiky vždy nechat schválit** (`AGENTS.md`, „Ask first“).
4. **Hlídat přeučení:** co vzniklo z rozboru nějakých dat, se ověřuje na jiných datech (sada B, skutečný log).
5. **Sebejistá chyba je horší než žádná odpověď** (skóre: jistě správně +1, správně v nabídce +0,5, jistě špatně −3).
6. **Skutečná data:** kopii produkčního logu (4 442 dotazů, 11. 8. – 22. 10.) smí nástroje použít lokálně (potvrzeno 27. 10. 2026). Texty dotazů se nevypisují ven, neukládají do Gitu ani do dokumentace; ven jdou jen počty a souhrny. Další export logu je možný jen s vědomím správce dat.

## 2. Kde jsme

### Hotovo a commitnuto (migrace 218–220, manifest `R220`)

| Změna | Co dělá |
|---|---|
| 218 | zdvořilostní a výplňová slova se nepočítají do pokrytí (21 slov) |
| 219 | QA hledá jen podle lemmatu, ne podle kmene |
| 220 | oprava překlepů v dotazu (`query_repair: ["typo"]`) |
| nástroje | regresní sada (`tools/lab/regression`), porovnání nastavení (`tools/lab/variants/compare_forms.py`) |

Změny 218–220 jsou **nasazené v lokální databázi a v lokálním obrazu vyhledávání** (28.–29. 10.). Migrace 221 (nabídka s 5 tlačítky) byla 29. 10. **zrušena**: počet tlačítek zůstává původní, **3**, jako v původním repozitáři (omezení uživatele: nejvýš 3 až 4, víc tlačítek není cesta).

### Naměřeno regresní sadou (jen QA; napsané dotazy, ne skutečné)

| Sada | Skóre dnes → po migracích | Jisté omyly | Uživatel uvidí správnou odpověď |
|---|---|---|---|
| A (1 470 přepisů) | 742 → 840,5 | 11 → 10 | 84 → 87 % |
| B (1 240, nezávislá) | 400,5 → 469,5 | 13 → 9 | 74 → 77 % |

„Uvidí“ = jistě, v nabídce, nebo v záloze (nejbližší otázky, které chat ukáže při slepé uličce, bez klíče vždy).

### Co se ukázalo a nepomáhá (neznovu zkoušet bez nového důvodu)

| Pokus | Výsledek |
|---|---|
| jen kmen místo lemmatu | více omylů v logu (pořadí → dohled) |
| hovorové koncovky („testama“) | přeučení (A +10, B +1,5) |
| pravidlo remíz podle rozdílu shody (`coverage_gap`) | skóre dolů, log +7/−9 |
| `coverage_reverse`, `stem_backoff` | více omylů / horší než lemma |
| prahy `t_answer`, `m_margin` | dnešní 0,60 a 0,08 jsou téměř optimální; 0,55 přidá +8 jistých omylů na A |
| `t_suggest` 0,2 | zisk jen na papíře (záloha se uživateli stejně ukáže), ne v „uvidí“ |
| hledání i v textu odpovědi | odpovědi jsou krátké (~138 znaků); jen 58 z 276 zmeškaných případů sdílí slovo, v top 5 by bylo 18 |
| synonyma uzpůsobení/český | na nezávislých datech neutrální; přínos +20 vznikl ze stejných dotazů (oprava dat, ne zlepšení) |

### Kde je strop hledání podle slov

Na sadě B: 21 % jistě správně, dalších asi 56 % v nabídce nebo záloze, **zbývá asi 23 %**, kde správná odpověď není vidět. Skupina „jinak“ (jiná slova, stejný význam) tvoří 17 % sady a slovními pravidly ji nevyřešíme. Dále ~8 z 19 „jistých omylů“ jsou věcně přijatelné záměny sousedních otázek (chyba měření, ne chat).

### Poslední měření: lokální model (27. 10.)

Model `intfloat/multilingual-e5-small` (471 MB, ve volume `edu_ai_reranker_models`, `sentence-transformers` už v repozitáři je). Vektory 1 063 znění a 2 710 dotazů za 33 s na CPU.

| Sada | Správná jen z modelu: 1. místo / top 5 | Dnes (uvidí) | Dnes + 3 z modelu | Dnes + 5 z modelu |
|---|---|---|---|---|
| B | 50 % / 71 % | 77 % | 85 % | 87 % |
| A | 67 % / 85 % | 86 % | 95 % | 96 % |

Jako rozhodovač **jisté** odpovědi model nestačí (B: při přísném prahu 129 správně / 5 špatně proti dnešním 261 / 9). **Hodí se jako doplněk nabídky.** Čísla předpokládají až 8 tlačítek a mé napsané sady; neověřeno na logu, při limitu 5 tlačítek bude přínos menší.

## 3. Pravidlo přijetí změny

Změna se přijme, jen když **všechno** platí (kontrola kroků je v části 4):

1. Regresní sada: skóre na **B** roste (rozdíly do ±5 bodů jsou šum), na A neklesá výrazně.
2. **Žádný nový jistě špatný případ**, který není věcně přijatelná záměna (`compare.py` končí kódem 1).
3. **Přehrání skutečného logu** (`tools/lab/replay`, kopie produkce): žádná jistá odpověď se nezhorší.
4. Změna je ve výchozím stavu vypnutá, má test a je v dokumentaci (`SETTINGS_REFERENCE.md`, `make docs-check`).
5. Co mění schéma, embedding model, pořadí kaskády nebo přidává závislost, **se předem schválí** (`AGENTS.md`).
6. Nastavení se mění migrací v `postgres/migrations/` s manifestem vydání (`migrate.py gen-release` na izolované databázi), nikdy ručně.

## 4. Kontrola po každém kroku

Po **každém** kroku (migrace, změna kódu, měření, nový nástroj) a **před tím, než ho označím za hotový**, proběhnou dvě kontroly. Další krok nezačnu, dokud první nedopadla dobře.

### A) Automatická: „funguje všechno?“

```bash
tools/lab/regression/check_step.sh              # po jakémkoli kroku
tools/lab/regression/check_step.sh --regression # i po změně hledání, nastavení nebo dat (~2 min)
```

| Kontrola | Co hlídá |
|---|---|
| překlad Pythonu | žádná syntaktická chyba v kódu, nástrojích, testech |
| testy na hostu | regresní sada, varianty, neutrální odpovědi, lint a otisk migrací |
| `make docs-check` | odkazy, zdroje a hlavičky dokumentace |
| testy v obrazu vyhledávání | záporná lemmata, stop-slova, diakritika, oprava překlepů — na kódu z tohoto checkoutu, ne ze starého obrazu |
| parita normalizace | `contracts/parity` (`AGENTS.md`: před i po změně normalizace) |
| regresní sada (`--regression`) | proti přijatému základu `accepted.json`, s migracemi 218–220 v transakci; končí chybou při novém jistě špatném případu |
| stav gitu | co je necommitnuté a nepushnuté, zapomenuté kontejnery po měření |

Skript skončí kódem 0 (`ALL CHECKS PASSED`), nebo vypíše, co selhalo. **Přijatý základ** (`accepted.json`) přepíšu jen po změně, kterou jsi schválil, a řeknu to.

### B) Úsudek: „jdeme správným směrem?“

Ke každému kroku odpovím na šest otázek, stručně a s čísly:

1. **Cíl:** přiblížilo to cíl (víc správných odpovědí bez klíče)? Kolik, na které sadě?
2. **Omezení:** neporušil jsem žádné z pěti omezení z části 1 (nové ručně psané odpovědi, velká změna repozitáře, nezměřená změna logiky)?
3. **Ověření mimo původní data:** platí to i na sadě B a, před nasazením, na logu — ne jen na datech, ze kterých změna vznikla?
4. **Vedlejší účinky:** co se zhoršilo (počet a příklady), jsou nové jisté omyly?
5. **Co jsem neověřil:** například že čísla jsou z napsaných dotazů nebo že se nic nenasadilo.
6. **Úklid:** zastavené kontejnery a smazané testovací databáze, necommitnuté soubory, počet nepushnutých commitů.

### Formát hlášení po kroku

> **Kontrola:** automatická ✔ / ✖ (co selhalo) · **směr:** přiblížilo / neutrální / vzdálilo (čísla) · **vedlejší účinky:** … · **neověřeno:** … · **úklid:** …

### Kdy se zastavím a zeptám

- automatická kontrola selhala (neoznačím krok za hotový, napíšu příčinu),
- skóre na B kleslo nebo vznikl nový jistě špatný případ, který není přijatelná záměna,
- krok by měnil něco z „Ask first“ (`AGENTS.md`): závislost, schéma, embedding model, pořadí kaskády, nový příznak v nastavení,
- výsledek se liší od očekávání o víc než šum (±5 bodů skóre) nebo si čísla z různých měření neodpovídají.

### Po každé fázi navíc

Přehrání skutečného logu (`tools/lab/replay`) a **kontrola samotného plánu**: platí ještě tabulka v části 2 a pořadí fází v části 5, nebo ho měření změnilo? Když ano, upravím plán a řeknu to.

## 5. Cesta k nejlepšímu výběru odpovědí

### 5.1 Co znamená „co nejlépe vyladit“ — a kde je strop

Nejlepší dosažitelný výsledek ohraničují tři věci, které žádné ladění nezmění:

1. **Chybějící obsah.** V dřívějším rozboru fronty dotazů z logu (22.–24. 10.) u 2 218 z 2 240 dotazů bez jisté odpovědi neodpověděla ani stará verze, protože odpověď v datech chybí. Výběr odpovědí tomu nepomůže a bez nových odpovědí (omezení 1) to plán neřeší. Plán ale **měří a vykazuje** podíl takových dotazů, aby bylo vidět, co zbývá na obsahu.
2. **Dvojznačné dotazy a téměř shodné otázky** (kdo má nárok × jak požádat). U části z nich není jedna správná odpověď.
3. **Čísla ze sad A a B jsou optimistická** (napsané dotazy, na B už se rozhodovalo). Skutečný přínos je potřeba ověřit na datech, která se neladila.

Plán proto nesleduje „100 %“, ale **výchozí cíle**. Nezávislá sada D (napsaná člověkem podle odpovědí, 27. 10.) ukázala, že napsané sady A a B **nadhodnocovaly** skutečnost:

| Ukazatel | A (trénink) | B (validace) | **D (nezávislá)** | Výchozí cíl (na D) |
|---|---|---|---|---|
| uživatel uvidí správnou odpověď (jistě, v nabídce, v záloze) | 86 % | 77 % | **49 %** | ≥ 75 % |
| jistě správně | 41 % | 21 % | **13 %** | co nejvíc při omezení omylů |
| jistě špatně | 0,7 % | 0,7 % | **2,9 %** | ≤ 1 % |
| otázky mimo obsah dostanou jistou odpověď | N: 1 z 444 | | BEZ: 0 z 47 | 0 |

Důvod rozdílu: otázky v D nekopírují slova z odpovědi (autor je měl nekopírovat), takže jsou blíž tomu, jak se skutečně ptají lidé. U krátkých, hovorových otázek (`otazka_2`) uvidí správnou odpověď jen **39 %**, u normálních vět 58 %. Cíle se po fázi 3 přepíšou podle toho, co lokální model na D dosáhne.

### 5.2 Fáze 1 — základ měření (první, bez toho nelze nic poznat)

Dnešní měření má čtyři slabiny. Odstraní se dřív než jakékoli další ladění:

| Slabina | Oprava |
|---|---|
| **Chybí otázky mimo obsah.** Nevíme, jestli asistent neodpovídá jistě tam, kde odpověď neexistuje (nejhorší druh chyby). | **Sada N (negativní):** otázky, na které školní QA odpověď nemá. Zdroj: veřejně sesbírané otázky z `Otázky EDU.xlsx` (listy ChitChat, Brainly, Mat, ČJ, Učitelé; po rychlém projití, aby tam nebyly otázky, které školní QA umí zodpovědět), případně dotazy z logu, na které správce potvrdí, že odpověď chybí. Očekávání: žádná jistá odpověď (`entry_key: null` je v regresní sadě už podporované). |
| **B už se použila k rozhodování**, takže není zcela nezávislá. | **Zmrazená sada D:** nová sada od jiných autorů (kolegové, učitelé, žáci — ne osobní údaje, jen jak by se ptali). Nikdy se podle ní neladí, použije se jen k závěrečnému ověření. B se pro ladění „spotřebuje“, D zůstane čistá. |
| **Dvojice téměř shodných otázek se počítají jako omyl.** | **Skupiny ekvivalentních odpovědí:** automaticky navrhnout dvojice záznamů s téměř shodnou odpovědí (podobnost vektorů a textu), kurátor jen potvrdí ano/ne. Nejde o psaní odpovědí. Regresní sada pak bere kteroukoli z nich jako správnou. |
| **Nevíme, co je šum.** | **Interval spolehlivosti:** u každého srovnání přidat bootstrap a párový test. Rozdíl pod hranicí šumu se nepočítá jako zlepšení. Dnes je to hrubě ±5 bodů skóre. |

**Sada C ze skutečného logu (slabé štítky).** Klik na tlačítko se nezapisuje (`selected_answer_key` je prázdný), ale chat pošle text kliknutého tlačítka jako další otázku, takže se dá dohledat nepřímo. **Pozor:** většina nabídek v logu je *vedle jisté odpovědi* (související otázky, stage `answer`: 271 + 217 + 91 + 72 + 49 záznamů u školy) a klik na ně není správná odpověď na první dotaz. Skutečnou nabídku *místo odpovědi* mají jen záznamy `suggest` (25) a `no_answer` (116). Z nich vyšlo **17 použitelných kliků**. Je to na statistiku málo, sada C je jen orientační kontrola. *(Dřívější údaj o 158 klicích počítal i kliky na související otázky; opraveno 27. 10.)* Texty zůstávají v `~/edu-ai/state/regression/`.

**Zásady práce se sadami** (podrobně v [README.md](README.md), „Sady a zásady“):

- **Trénink × test:** A je trénovací, B validační (už spotřebovaná), D a záporná sada N zmrazené.
- **Dělení podle otázek školy** (70 % train / 30 % holdout), ne podle vět, protože A i B jsou přepisy stejných 210 otázek.
- **Ladí se jen na `train`**, závěrečné číslo je z `holdout` a z D.
- **Šum** se vyhodnocuje bootstrapem u každé změny.
- **Deník pokusů** [LEDGER.md](LEDGER.md).

**Stav fáze 1 (27. 10.):**

| Položka | Stav |
|---|---|
| dělení train/holdout, `split` u každého případu | hotovo |
| sada N (445 otázek mimo obsah) | hotovo; 443 bez jisté odpovědi, 2 k ručnímu posouzení |
| měření se šumem (bootstrap, `--by-split`) | hotovo |
| sada C | hotovo, jen 17 případů (viz výše) |
| kandidáti na ekvivalentní odpovědi (40 dvojic) | **potvrzeno**: 6× ano, 31× ne, 3 prázdné; použito v měření (zlepšeny 4 případy, nový základ) |
| sada D | **hotová** (221 otázek: 174 s odpovědí, 47 „BEZ“); změřeno poprvé, viz 5.1; dvě chybné štítky opraveny souborem oprav (`--fixes`, záznam v deníku) |
| testy (18), deník pokusů, `check_step.sh` s `--by-split` | hotovo |

*Výstup fáze 1:* sady A, B, C, D, N s popsaným účelem, skupiny ekvivalencí, měření se šumem. Nové výchozí cíle z 5.1 se přepíšou po napsání D a potvrzení ekvivalencí. Kontrola kroku podle části 4.

**Sada L ze skutečného logu (28. 10.):** dotazy, které produkce vyřešila hledáním podle významu přes OpenAI, s jeho volbou jako *slabým štítkem* (`make_log_set.py`; 85 různých znění, zeptáno 262×; 30 se silnou shodou ≥ 0,75, 55 hraničních). **Výběrové zkreslení:** do tohoto kroku se dotaz dostane, jen když slovní hledání selhalo, takže L jsou ze své podstaty těžké dotazy a nereprezentují všechen provoz. Štítek není pravda: u 7 „jistých omylů“ se asi polovina liší jen blízkou otázkou (např. „V kolik začíná JPZ?“ × „Kdy se konají řádné termíny JPZ?“).

| L (85 znění) | Dnes | s lokálním modelem (lex2, τ 0,88) |
|---|---|---|
| uživatel uvidí správnou odpověď | **48 %** | **66 %** (+18 bodů) |
| jistě správně | 5 % | beze změny |
| jistě špatně | 8 % (7), z toho asi polovina blízká otázka | beze změny |
| model sám: správná v top 3 / top 5 | 62 % / 74 % | |

Na skutečných formulacích je přínos modelu **větší než na napsaných sadách** (+18 proti +7), což podporuje směr sémantické nabídky. Je to ale malá sada slabých štítků, proto jde o směr, ne o přesné číslo.

**Přehrání skutečného logu s migracemi 218–221 (28. 10., 4 442 dotazů, kopie produkce, bez klíče):**

Vyhodnocuje se proti **volbě produkce** jako slabému štítku (dotazy, které produkce vyřešila z QA, 3 403 řádků). Štítek má zkreslení: starý kód souhlasí sám se sebou, takže *změna* chování vypadá jako neshoda. Řádky logu jsou vážené počtem opakování, proto jsou uvedena i různá znění.

| Verze | uživatel vidí produkční odpověď (jistě nebo v nabídce) | jistá odpověď jiná než produkce |
|---|---|---|
| produkční kód (`main`) | 48,4 % | 11,7 % |
| po migraci 217 | 48,4 % | 11,8 % |
| **218–221** | **48,8 %** | **11,5 %** |

- **Celkově se nic nezhoršilo ani nezlepšilo** (shoda 48,4 → 48,8 %).
- **Dotazy, které produkce vyřešila přes OpenAI (447 řádků), vidí správnou o 6 bodů víc**: 28,6 → 34,7 % (jistě 0,4 → 3,8 %). Jde o skutečné formulace, které slovní pravidla dřív nechytila.
- **Dotazy vyřešené slovním hledáním (211 řádků): −7 bodů** (72,0 → 64,9 %). Za tím stojí **jediné znění zeptané 13× (jistá odpověď se změnila na jinou jistou)** a **jediné znění 12× (jistá odpověď se změnila na nabídku)**. Dvě různá znění z 211 řádků, takže jde o volumen, ne o plošný problém.
- **Přechody mezi stavem po 217 a 218–221** (řádky, různá znění): jistá jiná → nic 31 / 7; nic → v nabídce 30 / 8; jistá shodná → jistá jiná 13 / 1; jistá shodná → v nabídce 12 / 1; nic → jistá jiná 7 / 2. Změny ve prospěch (odstraněná jistá jiná, nová nabídka) převažují nad změnami v neprospěch.
- **Dokumenty:** u 8,2 % dotazů je jiný první úryvek než u produkčního kódu, ale to je účinek nové normalizace 3.3.0, ne migrací 218–221 (oproti stavu po 217 se úryvky shodují u 99,8 %). Kvalita dokumentů se dál neměří.

### 5.3 Fáze 2 — rozbor chyb a pořadí práce (hotovo 27. 10.)

Nástroj `analyze_errors.py` (běží v kontejneru) zařadí každý případ, u kterého uživatel **nevidí správnou odpověď** (ani v nabídce, ani v záloze), nebo dostane **jistě špatnou** odpověď, do jedné skupiny. Analyzují se jen **A, B a D:train**; `D:holdout` se nepoužil. Slovo je „tematické“, když není obecné (lemma ve více než 5 % otázek, např. „škola“, „žák“, „dítě“). Výsledek ze 27. 10. (521 případů):

| Skupina | A | B | D:train | Celkem | Podíl | Správná v top 5 u lokálního modelu |
|---|---|---|---|---|---|---|
| neznámé slovo (hovorové tvary, odvozeniny: „reparát“, „matika“, „třídnické“) | 54 | 77 | 14 | 145 | 28 % | 48 % |
| žádné tematické společné slovo (jiná slova, např. „zlomila prst“ × „úraz“) | 56 | 69 | 15 | 140 | 27 % | 40 % |
| jedno tematické společné slovo (zbytek dotazu zřeďuje shodu) | 71 | 103 | 5 | 179 | 34 % | 61 % |
| dvě a více tematických slov, přesto nic | 9 | 27 | 0 | 36 | 7 % | 67 % |
| jistě špatně: téměř shodná otázka (data, ne chyba) | 2 | 4 | 1 | 7 | 1 % | 86 % |
| jistě špatně: jiné téma (obecné slovo zavede jinam) | 8 | 5 | 1 | 14 | 3 % | 86 % |

**Co z toho plyne:**

1. **Většina zmeškaných případů není „jiný význam“, ale slovní hledání, které se nechá zmást tvarem nebo délkou dotazu.** Neznámé tvary a dlouhé dotazy, kde jedno tematické slovo zapadne mezi ostatními, jsou dohromady 62 %.
2. **Lokální model by u více než poloviny z nich správnou odpověď nabídl** (spočteno vážené na 521 případů: asi 53 %). Zbytek (~47 %) nezachrání ani on, takže to je strop pro samotnou nabídku.
3. **Jistě špatných odpovědí je málo (21 z 521, 4 %)** a třetina z nich je věcně přijatelná záměna (kandidáti na ekvivalenci). Jisté omyly nejsou hlavní problém; hlavní problém je, že správná odpověď chybí v nabídce.

**Pořadí práce (podle velikosti skupiny a dosahu):**

1. **Fáze 3, sémantická nabídka** zasáhne všechny čtyři velké skupiny (53 % případů). Je to největší jednotlivá páka.
2. **Neznámá slova (28 %):** lokální model je zčásti pokryje. **Tabulka tvarů se vyzkoušela a zamítla (28. 10.):** z 370 různých neznámých slov se jen u 3 dvojic opakuje stejný partner (≥ 3 případy), takže jde o dlouhý chvost jednorázových slov, ne o systematiku. Volnější tabulka (37 dvojic) zlepšila trénovací otázky, ale na zadržených otázkách je rozdíl šum (A:holdout +2, B:holdout −0,5, D:train −0,5) a přibylo 5 jistých omylů. Zbytek neznámých slov proto nechat na sémantické nabídce a opravě překlepů.
3. **Dlouhé dotazy (34 % + 7 %):** nabídku řadit podle váhy **tematických** slov místo podílu všech slov dotazu. Jen pro nabídku, jisté odpovědi se nemění; nejdřív změřit.
4. **Jisté omyly (4 %):** ekvivalence potvrdí kurátor (40 dvojic čeká). Zbytek jsou záměny přes obecné slovo, kde pomůže bod 3.

*Zbývá z fáze 2:* po každé další fázi tabulku obnovit (`analyze_errors.py` s novým `accepted.json`).

### 5.4 Fáze 3 — sémantická nabídka (lokální model)

Cíl: doplnit nabídku „Nechtěl ses zeptat…?“ i tam, kde slova nesedí. **Jisté odpovědi se nemění.**

1. **Porovnat 2–3 modely** podobné velikosti (např. `multilingual-e5-small`, `paraphrase-multilingual-MiniLM-L12-v2`) na A, B, **D:train** (vývojová půlka D) a N; **D:holdout** (zadržené otázky školy) se na modely nesmí použít, zůstane na závěrečné ověření: úspěšnost v top 3 a top 5, čas na dotaz, paměť, velikost (disk dnes ~2,2 GB volných).
2. **Politika sloučení do 5 tlačítek:** pořadí z dnešního hledání, doplnění modelem, bez duplicit; změřit, kolik z přínosu zbude (naměřeno bez limitu: B 77 → 87 %).
3. **Kontrola na N:** model nesmí do nabídky vnášet nesmysly u otázek mimo obsah (nabídka u nich je přijatelná jen s nízkým skóre; předem určit práh pro „raději nic“).
4. **Kontrola na C:** zvolená odpověď z logu je v nové nabídce ve stejné nebo lepší pozici.
5. **Rozhodnutí (schvaluje uživatel):** nový model a změna chování nabídky patří do „Ask first“ v `AGENTS.md`.
6. **Implementace jako vypnutý krok (navrženo, zatím NEprovedeno, viz rozhodnutí níže):** vektory v paměti procesu (bez změny schématu), vše přes `search/app/scope.py`, selhání modelu = tiše přeskočit, migrace zapne parametr jen u schválených škol; test + `SETTINGS_REFERENCE.md`.

**Výsledek měření s modelem `multilingual-e5-small` (28. 10., A, B, `D:train`, N; `D:holdout` nepoužit):**

Měří se, kolikrát uživatel uvidí správnou odpověď (jistě, nebo mezi nejvýš 5 tlačítky). Jisté odpovědi se nemění. Tři způsoby, jak model do 5 tlačítek zapojit (práh τ = nejnižší podobnost, od které model tlačítko nabídne):

| Způsob | A (dnes 86,5 %) | B (dnes 77,3 %) | D:train (dnes 53,8 %) | otázky mimo obsah s tlačítkem od modelu |
|---|---|---|---|---|
| **fill**: model jen doplní volná místa, τ ≤ 0,84 | 91,0 | 81,8 | 60,3 | 60–66 % |
| **lex2**: první dvě slovní tlačítka zůstanou, model další tři, τ = 0,88 | 93,4 | 83,1 | 60,3 | **22 %** |
| **lex2**, τ = 0,84 | 93,7 | 82,8 | **66,7** | 91 % |
| **rrf** (sloučení pořadí), τ = 0,88 | 93,4 | 84,3 | 60,3 | 22 % |

- **Přínos při 5 tlačítkách je +6 až +7 bodů na A a B a +6,5 až +13 bodů na `D:train`**, podle prahu. Dřívější odhad +10 platil bez limitu tlačítek.
- **Cena prahu:** nižší τ zvýší zisk, ale model pak přidá tlačítko skoro ke každé otázce mimo obsah (τ = 0,84: 91 %). Dnešní systém ukazuje alespoň jedno tlačítko u **69 %** takových otázek (průměr 2,5), takže nejde o novou věc, jen o víc tlačítek. **Výchozí volba τ = 0,88 s politikou `lex2`**: zachová většinu zisku a model se u mimoobsahových otázek ozve jen ve 22 %.
- **Náklady:** 11 ms na dotaz, načtení modelu 8–10 s, špička paměti ~1,1 GB (celý proces s torch). To je pro stroj školy třeba ověřit.
- **Práh na obou půlkách N (τ = 0,88, `lex2`):** trénovací 24,5 %, zadržená 17,6 %, takže se na zadržené půlce nechová hůř. Nižší τ = 0,87 dá na `D:train` 65,4 % místo 60,3 %, ale model by pak přidal tlačítko 44 % mimoobsahových otázek.

**Porovnání se druhým modelem (28. 10.):** `paraphrase-multilingual-MiniLM-L12-v2` (471 MB) má jinou škálu podobnosti (τ 0,30–0,70), proto se porovnává při stejné míře tlačítek u otázek mimo obsah (kolem 20 %, politika `lex2`):

| Model | sám v top 5 (A / B / D:train) | A | B | D:train | čas na dotaz | špička paměti |
|---|---|---|---|---|---|---|
| **multilingual-e5-small** (τ = 0,88) | 85,1 / 71,5 / 53,8 | **93,4** | **83,1** | **60,3** | 9–11 ms | ~1,1 GB |
| paraphrase-multilingual-MiniLM-L12-v2 (τ ≈ 0,65) | 82,7 / 68,0 / 50,0 | ~91 | ~80,5 | ~57,7 | 13–35 ms | ~1,45 GB |

e5-small je lepší na všech třech sadách (o ~2,5 bodu při stejné míře tlačítek), rychlejší a nenáročnější na paměť. **Volba: `multilingual-e5-small`.** MiniLM už není potřeba (471 MB k uvolnění ve volume).
- **Rozhodnutí (28. 10. 2026): implementace se do `search/app` zatím nedělá, zůstává v plánu.** Krok by potřeboval úpravu `executor.py` (seznam kroků kaskády) a nový příznak v nastavení, tedy zásah do jádra, které není součástí tohoto úkolu. Měření, výběr modelu, politika a práh jsou hotové a zapsané, takže se na ně dá kdykoli navázat. Případný návrh pro vlastníky jádra a samostatný prototyp v `tools/lab` jsou možné další kroky, ale nezačaly.
- **Co by implementace obnášela** (pro posouzení): nový soubor `search/app/nodes/semantic_offer.py`, řádek v `executor.py`, parametr v nastavení (vypnutý), migrace, test a řádek v `SETTINGS_REFERENCE.md`; bez změny databáze, bez zásahu do `qa_lexical.py`, `qa_common.py` a `qa_vector.py`.

### 5.5 Fáze 4 — lepší rozlišení u jistých odpovědí

Cíl: víc jistých odpovědí **bez** nárůstu jistých omylů. Zkouší se vždy jako vypnutý parametr, jedno po druhém:

| Návrh | Myšlenka | Podmínka přijetí |
|---|---|---|
| **Shoda dvou signálů** | jistá odpověď jen když ji dává pravidlo (slova) **i** model ve stejném top 3; nebo přísný model sám + slovní kandidát | jisté omyly na N a D nerostou, jistě správných přibude |
| **Nejistota u sousedů** | když první a druhá odpověď jsou si velmi podobné (skupina ekvivalencí nebo dvojznačné), místo jisté odpovědi nabídnout obě | jistě špatně klesne, jistě správně neklesne o víc než šum |
| **Malý interpretovatelný model skóre** | logistická regrese nad signály, které už máme (pokrytí, náskok, počet shodných pojmů, podobnost vektorů, shoda tázacího slova); učí se na A, ověřuje na B, D, C a N; omezení: jisté omyly ≤ cíl | změna rozhodovací logiky, **vždy schválit**; jen pokud předchozí dva návrhy nestačí |
| **Znovu práhy** | mřížka `t_answer`, `m_margin`, `t_suggest` po každé větší změně (~30 min) | jako dosud |

### 5.6 Fáze 5 — data bez psaní odpovědí (průběžně, levné)

- Tabulka oprav lemmat, přednostně podle skupin z fáze 2; ověřit na B a D.
- Jednotlivá synonyma ze skutečných dotazů **jen s ověřením mimo dotazy, ze kterých vznikla** (jinak přeučení).
- **Zpráva o mezerách v obsahu:** dotazy z logu bez odpovědi seskupené podle tématu (nástroj už existuje, karta Dotazy z provozu). Není to psaní odpovědí, jen přehled, co chybí, pro správce obsahu.
- Regresní sada z rozhodnutí kurátora, až jich bude víc.

### 5.7 Fáze 6 — dokumenty bez klíče (samostatné rozhodnutí)

Dnes se kvalita dokumentů neměří (u 8 % dotazů se po změně normalizace změnil první úryvek). Nejdřív sada dotazů s očekávaným dokumentem; teprve pak případné lokální embeddingy úryvků místo OpenAI. To mění schéma (dimenze, indexy), proto se rozhoduje zvlášť.

### 5.8 Nasazení a sledování bez dalších osobních dat

- Přehrání skutečného logu před každým nasazením (část 3, bod 3).
- Postupně: vypnuto → jedna škola → ostatní. Při horším výsledku návrat změnou nastavení (migrace ve verzích zpět).
- **Souhrnné počty bez textů** z provozu: podíl odpovědí „Nenašel jsem přesnou odpověď“, podíl kliknutí na tlačítka a na kterém místě, podíl dotazů bez odpovědi. Sledují se před a po nasazení.
- Doporučení pro další sběr (rozhodnutí uživatele a správce): zapisovat klik do `selected_answer_key` (sloupec existuje a je prázdný), aby štítky nebyly jen nepřímé.

### 5.9 Kdy je hotovo

Ladění se zastaví, když **tři po sobě jdoucí kroky přinesou méně než šum** na D a C a rozbor chyb ukáže, že zbývající skupiny jsou obsah nebo skutečná dvojznačnost. Do té doby se po každé fázi přepíše tabulka v části 2, výchozí cíle z 5.1 a pořadí z 5.3.

## 6. Rizika a otevřené otázky

| Riziko / otázka | Poznámka |
|---|---|
| Všechna dosavadní čísla jsou z napsaných dotazů, ne z provozu | proto je přehrání logu povinná podmínka (část 3) |
| Disk: ~2,2 GB volných | větší modely se nevejdou; model ve volume jde smazat |
| Čas a paměť na stroji školy | změřit v etapě 1 na cílovém hardwaru |
| Model je slabší než OpenAI embedding | očekává se; proto jen doplněk nabídky |
| Produkce lemmatizuje starší simplemma | po nasazení 3.3.0 se změní všechna lemma, data je nutné znovu nahrát (viz nasazovací poznámka) |
| Migrace 218–220 se v existujících instalacích dosud nespouštějí | postup pro existující instalace (D11) není rozhodnutý; dnes ručně přes `psql` |
| Role uživatelů (učitel × žák) | QA dnes podle role nefiltruje; zatím se neřeší |
| Osobní údaje v logu | kopie se používá lokálně, texty se nevypisují ani neukládají do Gitu; další export jen s vědomím správce |
| Strop je dán obsahem | výběr odpovědí nenahradí chybějící odpovědi (část 5.1) |

## 7. Co od tebe potřebuji

1. **Nic k blokujícímu rozhodnutí.** Sémantická nabídka zůstává v plánu; pokud ji někdo z vlastníků jádra bude chtít převzít, podklady jsou v části 5.4 a v deníku.
2. Volitelně: smazat model MiniLM, který už není potřeba (uvolní 471 MB): `docker exec edu_ai_search rm -rf /models/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2`.
3. ~~Nasadit migrace 218–220~~ — hotovo 28. 10. do lokální databáze; zbývá přestavět obraz vyhledávání (kvůli `query_repair`) a nasadit na další instalace podle rozhodnutí o převzetí existujících instalací (D11).
4. Volitelně: seznam dvojic ke sloučení pro kurátora (6 potvrzených dvojic).

## 8. Přidání nového obsahu bez zhoršení (postup, 29. 10. 2026)

Nové nebo upravené odpovědi mohou starým záznamům **ukrást jistou odpověď** (sourozenecké otázky, podobná témata) a mění váhy slov. Naše sady to umí změřit, takže přidání obsahu má jednotný postup:

1. **Připravit balíčky.** Adresář s CSV balíčky školy **tak, jak budou po změně** (stará QA + nová nebo upravená), ve formátu exportu z QA portálu.
2. **Spustit kontrolu:**
   ```bash
   python3 tools/lab/regression/content_check.py --packs-dir DIR [--sweep]
   ```
   Skript převede balíčky na seznam otázek (`build_index.py`, stará `idx` zůstávají), pustí celou regresní sadu (A, B, C, D, L, N) s nasazeným nastavením a porovná s přijatým základem. Vypíše:
   - skóre před a po po sadách,
   - **kolikrát nový záznam ukradl jistou správnou odpověď** a **kolikrát jistě odpověděl na otázku mimo obsah**,
   - **které nové záznamy přitáhly nejvíc cizích dotazů** (k přepsání nebo sloučení),
   - s `--sweep` i prahy `t_answer` a `m_margin` v okolí dnešních hodnot.
   Končí kódem 1, když je ukradená odpověď nebo jistá odpověď mimo obsah (meze `--max-stolen`, `--max-n-confident`).
3. **Rozhodnout u přitahujících záznamů:** zpřesnit znění otázky, označit věcně shodné dvojice jako ekvivalentní (`build_cases.py --equiv-csv`), nebo záznam přijmout, pokud je skutečně lepší odpovědí.
4. **Testovací otázky k novým záznamům.** Kontrola připraví `testovaci_otazky_nove.xlsx` (člověk píše otázky podle odpovědí jako u sady D); po vyplnění `build_cases.py --d-csv … --d-map …` je přidá do sad.
5. **Po přijetí:** přegenerovat `otazky_idx.json` (`build_index.py --like`), nový základ `accepted.json`, zapsat do deníku a **znovu nahrát celou QA aktuálním obrazem** (normalizace 3.3.0), aby staré i nové záznamy měly stejné tvary.
6. **Prahy** projít znovu (`--sweep`), když obsah naroste o víc než pětinu.

**Vyzkoušeno na simulaci:** přidání 65 záznamů z jiné školy (podobná témata, jiné odpovědi) zhoršilo skóre z 1 835 na 1 715 a jisté omyly z 31 na 61; kontrola našla 6 ukradených jistých odpovědí a uvedla záznamy, které cizí dotazy přitahují nejvíc. Simulace je záměrně nepříznivá (jde o věcné duplicity), ukazuje ale, že kontrola riziko opravdu odhalí. Změna prahů škodu nezachrání: při `t_answer` 0,55 až 0,65 a `m_margin` 0,04 až 0,12 vychází skóre 1 683 až 1 708 (dnešní nastavení 1 704), tedy hodnota 0,65 sníží jisté omyly (61 → 50), ale ztratí 113 jistých správných odpovědí. Řešit se musí obsahem (zpřesnit znění, sloučit duplicity), ne prahy.

