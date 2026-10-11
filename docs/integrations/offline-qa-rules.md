# QA pravidla a požadavky na offline pack

Dokument soustřeďuje pravidla, váhy, parametry a jejich přítomnost v exportu.
Účinné hodnoty jsou data konkrétní organizace; veřejný zdroj ani fallback není
výpis aktuální produkční konfigurace. Mobilní aplikace má samostatný veřejný
[repozitář](https://github.com/edu-ai-00/EDU-AI-asistent-APP).

**Současný SQLite QA pack nepřenáší všechna pravidla potřebná ke stejnému offline chování.**
Obsahuje QA obsah, varianty, metadata a tři manifestové prahy. Chybí účinný profil kroků,
normalizační konfigurace, číselníky a část podkladů pro scoring. Přítomnost FTS indexu a prahů
proto není důkazem parity backendu a budoucího telefonu. CSV/ZIP export pro editaci tuto mezeru
také neřeší. Implementace exportéru ani jeho schéma se tímto dokumentem nemění.

## Ověřená mobilní aplikace

Dne 11. 10. 2026 byl podle odkazu vlastníka čtecím klonem ověřen veřejný APP main
`e653aea54d5e408bd3667f4ab087e0920814cc76` (commit 20. 7. 2026); dostupná je také větev init.
Jde o existující Flutter aplikaci s Drift/SQLite a offline ukládáním obsahu/stavu. Zjištění
v této tabulce jsou statickou kontrolou zdrojů daného SHA, nikoli testem instalované mobilní verze.

| Část aplikace | Ověřené chování |
|---|---|
| [Chat repository](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/repositories/chat_repository.dart#L71) | Vytvoření chatu i nová zpráva vyžadují internet; offline vrací chybu. |
| [Remote chat](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/datasources/remote/chat_remote_datasource.dart#L59) | HTTP POST na chat server a SSE odpověď; neprovádí místní QA lookup. |
| [Lokální databáze](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/core/database/app_database.dart#L30) | Ukládá kurzy, lekce, průběh/procvičování, chat historii a synchronizaci; QA manifest/entries/variants a QA FTS zde nejsou. |
| [Chat UI](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/pages/chat_detail_page.dart#L619) | Offline vypne odesílání zprávy; uložená historie je dostupná. |
| [Bundled courses](https://github.com/edu-ai-00/EDU-AI-asistent-APP/blob/e653aea54d5e408bd3667f4ab087e0920814cc76/lib/data/datasources/local/bundled_courses_datasource.dart#L6) | Offline obsah načítá jako JSON kurzy z assets; není to import backendového SQLite QA packu. |

V lib/test/docs prohlédnutého main nebyl nalezen QA-pack importer, QA normalizer/lemma/stem,
stoplist/synonymní profil, IDF/coverage matching ani QA FTS lookup. Samotná SQLite závislost
tyto funkce nezajišťuje. Kontrola druhé veřejné větve init také nepřinesla QA-pack implementaci.
Co vykonává vzdálený chat server, APP zdroj sám neprokazuje. Předchozí formulace o neexistenci
mobilní aplikace byla příliš široká; otevřeným úkolem je její offline QA integrace a parita.

## Kde pravidla vznikají

| Podklad | Autorita pro skutečné chování |
|---|---|
| Kaskáda a parametry | Aktivní `edu_ai.settings.config.pipeline`: `step`, `enabled`, `params`, `on_hit`, podmínky běhu; vykonává je `executor.py`, nikoli referenční `graph.py`. |
| Organizace | Nastavení kořenové organizace se uplatní i na její jednotky; bez odpovídajícího řádku se vybírá globální řádek a nakonec zdrojový fallback. |
| Prahy konkrétního packu | Aktivní `qa_pack_manifest.t_answer`, `t_suggest`, `m_margin`; explicitní parametry `qa_lexical` je mohou přepsat. |
| Normalizace | `normalization`, `engines`, jazyky organizace, normalizační knihovna a slovníky; indexový stoplist se přidává z `qa_stopword`. |
| Synonyma a koncepty | Aktivní `qa_synonym`, jazyk, typ skupiny a coverage stoplist. |
| Oprávnění a filtry | `scope.py`, hierarchie entit, registr uživatelů, role, visibility/owner, platnost, ročníky a oblasti. |
| Guard a pomocná pravidla | `qa_blocklist`, stoplisty `meta_filler/meta_verb/coverage`, taxonomy, číselníky a jazykové `system_message`; nevkládat do dokumentace executable prompty. |
| Vektorová cesta | `qa_embedding`, aktivní `embedding_registry`, embedding model a parametry `qa_vector`. |
| Konverzace | Kontextové přepsání před přípravou dotazu a historie již položených/nabídnutých otázek. |

Výběr konfigurace je v [settings.py](../../search/app/settings.py#L433), řízení kroků v
[executor.py](../../search/app/executor.py#L99). Cache aktivního nastavení je pět sekund a má
klíč subjektu. Po vyřešení executor končí, pokud `on_hit` není `continue`; chybu kroku zachytí
a pokračuje. Po vyčerpání využije odložené návrhy, jinak bezpečný `no_answer`.

## Účinný profil a váhy

Před exportem odvoďte účinné `settings`, aktivní pipeline a manifesty pro danou
organizaci. Zaznamenejte pořadí kroků, `enabled`, `on_hit`, formy normalizace,
lexikální váhy, coverage, synonyma, prahy a guard. Parametr kroku může přepsat
packový práh; zdrojový fallback je až poslední vrstva. Hodnoty se nepřebírají
z historického profilu jiné instalace.

Čistý seed je veřejný zdroj konfiguračních dat, nikoli živá konfigurace všech
škol. Následující tabulky rozlišují mechanismus a zdrojové fallbacky; budoucí
offline kontrakt musí přenést účinné hodnoty i potřebné slovníky.

## Příprava dotazu a normalizace

Před QA kroky může backend přepsat navazující dotaz podle konverzace. `prepare` pak vytváří
`norm`, `lemma` a `stem`; volí explicitní jazykový filtr nebo detekuje jazyk mezi povolenými
jazyky organizace. Při nerozhodné detekci vytváří sjednocení forem povolených jazyků. Offline
klient potřebuje zvolenou politiku jazyka a kontextu; samotná uložená indexová forma nestačí.

Normalizační knihovna ve zdroji má verzi **3.0.0**. Používá Unicode NFC; lemmatizace a stemming
proběhnou nad slovem s diakritikou a folding až potom. `lowercase`, `strip_diacritics` a
`drop_stopwords` mají zdrojový default `true`. Chybějící `stopwords_by_lang` je chyba; přítomná
mapa bez daného jazyka znamená pro něj prázdný stoplist. Stemmer závisí na `engines`, jazyku,
algoritmu a `min_len` se zdrojovým defaultem `3`. Query lemmatizace používá `simplemma`; při
chybě se vrátí povrchový tvar. Detekce jazyka má ve zdroji seed `0`, práh `0.70` a zvláštní
zpracování krátkých vstupů pod `200` znaků. To nejsou číselné váhy z QA manifestu.

Indexový stoplist a coverage stoplist jsou **dva rozdílné podklady**. Synonyma mají skupiny,
jazyk a `kind`; expandují se entity skupiny, interrogative skupiny nerozšiřují OR dotaz ani
obsahové koncepty. Víceslovná skupina se aktivuje při výskytu celého tokenového setu alespoň
jednoho jejího termu v některé jeho formě, nikoli všech synonym skupiny. Frázová skupina navíc
vytváří samostatný coverage koncept ze zbývajících rozlišujících tokenů. Koncepty používají
prioritu lemma → stem → norm, odstranění coverage stopwords, interrogativ a duplicit.

Zdroje: [normalizer](../../search/eduai_normalize/eduai_normalize/normalize.py),
[lemmatizer](../../search/eduai_normalize/eduai_normalize/lemmatizer.py),
[prepare](../../search/app/nodes/prepare.py), [qa_common](../../search/app/nodes/qa_common.py#L110).
Příklady tokenové parity jsou v [contracts/parity](../../contracts/parity/README.md).
Neověřují pořadí výsledků, scope, prahy ani stejné odpovědi. APP je nyní známý a staticky
prohlédnutý; odpovídající Dart port QA normalizace v jeho main nalezen nebyl. Normativní
požadavek Python/Dart parity se tím nevydává za splněnou klientskou implementaci.

## Oprávnění a výběr packů

Před rankingem se uplatní aktivita, entity/organization/private scope, role, platnost, jazyk,
ročník/úroveň a zvolené oblasti. `valid_from <= CURRENT_DATE` a `valid_to >= CURRENT_DATE`;
NULL je neomezené. Private materiál vyžaduje ověřeného vlastníka; nepoužívá audience-role větev
stejným způsobem jako veřejnější materiál. Prázdná nebo nepovolená identita nesmí rozšířit scope.

Oblasti mají tři stavy: `None` nefiltruje, prázdný seznam nepovoluje nic a neprázdný seznam
vyžaduje průnik. Manifest pouze předvybírá pack; matching filtruje metadata přímo na skenovaných
variants/embeddings. Exportované entry a variant údaje se neposkládají automatickým děděním
z manifestu. Žádný povolený pack se v backendu reprezentuje sentinelovou hodnotou `~none~`;
prázdný seznam by některé navazující kroky četly jako všechny packy.

Ročník z otázky má při volbě packů přednost před profilem uživatele. Exact ročník a kompatibilní
universal `9999` se řeší přes číselníky a `entity_grade_scope/grade_level`, nikoli přes vlastní
pevnou tabulku mobilu. Pack bez ročníků je univerzální. Other-grade nabídky ročník uvolňují,
ostatní oprávnění zachovávají.

Zdroje: [scope.py](../../search/app/scope.py), [route.py](../../search/app/nodes/route.py#L646),
[mobilní oblasti](mobile-domain-scope.md). Datum zařízení samo nezajišťuje důvěryhodnou platnost
a lokální filtr sám nechrání již stažený soukromý obsah; offline distribuce musí vymezit oprávněný
balík, důvěryhodný profil uživatele a zacházení s jeho platností.

## Router guard

Router postupně řeší nepoužitelný vstup → meta dotaz → identity dotaz → blocklist. Meta/identity
shoda používá celou folded otázku s případnými výplňovými slovy. Jednoslovné blocklist termy
matchují celé norm/lemma tokeny. Fráze počítají unikátní tokenové zásahy v okně velikosti
počet tokenů termu + phrase_slack; zvláštní kontrola zachovává českou negaci. Výsledek určí
pořadí kategorií a minimální severity z aktivního profilu; záznamy a texty odpovědí jsou data.
Chyba načtení blocklistu tuto guard kontrolu přeskočí, neprovádí se skryté blokování podle
jiného slovníku. Tyto sémantiky musí budoucí klient výslovně převzít nebo vymezit odlišný profil.

Zdroj: [route.py](../../search/app/nodes/route.py#L547). Pro budoucí balík je třeba přenést
politiku, relevantní číselníky a lokalizované zprávy; serverové API klíče ani executable prompty
k tomu nepatří.

## Exact odpověď

Stačí shoda celé pořadové tokenové sekvence **kterékoliv** nakonfigurované formy. Výběr preferuje
vlastní subjekt → subjekt v organizaci → ostatní, potom stabilní `entry_id`. Vrátí autorskou
odpověď se skóre a margin `1.0`, bez generování. Neshoda nebo chyba pokračuje.

Související otázky používají aktivní lexical profil, nikoli izolované defaulty exact kroku;
odstraňují zodpovězenou otázku a deduplikují text. `suggest_top_n` se bere z exact parametru,
potom lexical parametru, nakonec fallbacku. Zdrojový resolver formy má default `[norm, lemma]`,
aktivní profil může výslovně zapnout i stem.

Zdroj: [qa_exact.py](../../search/app/nodes/qa_exact.py).

## Lexikální scoring a váhy

Backend sestaví OR query ze zvolených forem a synonym. Recall používá PostgreSQL
`ts_rank_cd(..., to_tsquery('simple', ...))`; neposílá vlastní pole vah ani normalizační argument.
Nejde o implementaci SQLite BM25. PostgreSQL default vah je v pořadí D/C/B/A
`0.1 / 0.2 / 0.4 / 1.0`, normalizace ranku default `0`; cover-density zohledňuje pozice lexémů.
[Definice PostgreSQL 16](https://www.postgresql.org/docs/16/textsearch-controls.html#TEXTSEARCH-RANKING).

`fetch_k` omezuje počet **variant** před agregací, nikoli počet unikátních odpovědí. Entry vezme
maximum ranku nalezených variant a sjednocení jejich tokenů. Následují obsahové váhy:

```text
N       = max(počet unikátních aktivních entries spojených s variants vybraných packů, 1)
df_i    = počet entries obsahujících libovolný token konceptu i
w_i     = ln((N + 1) / (df_i + 1)) + 1
coverage = součet vah zasažených konceptů / součet všech vah konceptů
margin   = (rank_1 - rank_2) / rank_1
```

Bez použitelných vah se coverage změní na prostý podíl zasažených konceptů. Při jediném
kandidátovi je `rank_2=0`; při nekladném `rank_1` je margin `1.0`. Margin je rozdíl ranků,
nikoli rozdíl coverage ani pravděpodobnost správné odpovědi.

Pořadí entries je lexikografické `(grade_tier, entity_tier, -round(rank, 6), -coverage)`.
Ročník exact má tier `0`, universal nebo bez ročníků `1`, ostatní `2`; bez známého ročníku je
ročníkový tier stejný. Entity tier je vlastní `0`, organizace `1`, ostatní `2`. Nejde o součet
například „70 % lexical + 30 % škola“; lepší tier má přednost před následujícím skóre.

Prahy se řeší **parametr kroku → manifest vítězného packu → environment/code fallback**.
Účinný profil může A/B/C override přidat změnou settings i bez reexportu packu. Exporter ukládá manifestové hodnoty, nikoli jejich účinný přepis.

| Větev | Podmínka a výsledek |
|---|---|
| A | `coverage >= t_answer`, `margin >= m_margin` a topical gate neodmítl hit → autorská odpověď. |
| B-deferred | `coverage >= t_suggest`, ne A → uložení návrhů a **pokračování kaskády**. Konečná nabídka nastane, když další kroky neodpovědí. |
| C | nižší coverage → pokračování; mohou zůstat fallback nabídky pro výsledný `no_answer`. |

Topical admission vyžaduje vhodný společný vzácný koncept s `df/N` nejvýše povolenou hranicí.
Pomocné příznaky `query_in_corpus` a chybějící koncepty ovlivňují navazující fallback.
Při nulovém fulltext recall vznikají typo návrhy přes `pg_trgm`, similarity
`1 - (norm_text <-> query_norm)`. Práh se zde testuje na nejlepším hitu, nikoli znovu na každém
vráceném trigram návrhu; z tohoto fallbacku nikdy nevzniká přímá odpověď A.

Zdroje: [qa_lexical.py](../../search/app/nodes/qa_lexical.py#L291),
[IDF a coverage](../../search/app/nodes/qa_common.py#L273).

### Proč nestačí dopočítat IDF z jednoho SQLite souboru

Backendový statistický dotaz používá vybrané pack IDs a aktivní entries, vždy `ts_norm || ts_lemma`.
Nemá stejný `v.is_active` ani entity/role/validity/domain filtr jako dotaz vybírající kandidáty.
Komentář kódu tuto asymetrii eviduje jako předchozí chování; audit ji neopravuje. Export naproti
tomu obsahuje jen scoped aktivní variants **jednoho** packu. Při stejných vybraných pack IDs
backend nezúží N/df oblastí; oblast může změnit statistiky prostřednictvím výběru jiných packů
v routeru. Pokud klient dopočítá statistiky pouze z jednoho packu nebo z vybrané oblasti,
použije jiný korpus. Neaktivní uložené variants ovlivňují existenci entry ve statistickém joinu
a df. Stejná formule nad jinými daty proto nevytvoří stejné váhy.

SQLite FTS5 indexy také samy nereprodukují `ts_rank_cd`, PostgreSQL trigram similarity, tiering
a margin. [SQLite FTS5](https://www.sqlite.org/fts5.html). Přesná parita vyžaduje kompatibilní
ranking i přesně definovaný statistický korpus; není důvod přenášet do mobilu neoprávněná data
jen kvůli rekonstrukci denominatoru.

## Další parametry a nabídky

Tabulka uvádí zdrojový fallback, který může účinný parametr organizace přepsat. Nepřítomný parametr
neznamená vypnutou funkci; nastupuje default příslušného kroku. Číselné hodnoty nejsou konstantami
budoucího klienta.

| Parametr | Zdrojový default | Význam |
|---|---|---|
| `t_answer / t_suggest / m_margin` | `0.60 / 0.30 / 0.08` | Poslední fallback A/B/C. |
| `fetch_k / suggest_top_n` | `50 / 3` | Variant recall a počet lexical návrhů. |
| `synonyms` | `true` | Entity synonym expansion. |
| `trgm_suggest_min` | `0.40` | Podmínka nejlepšího typo hitu. |
| `topical_admission / topical_max_df_share` | `true / 0.25` | Gate pro přímou lexical odpověď. |
| `related_suggestions` | `true` | Related otázky vedle exact odpovědi. |
| `related_admission` | `off`, podporuje `rare` | Extra admission related otázek. |
| `related_cross_pack_min` | `0.70` | Vyšší coverage floor pro related z jiného packu. |
| `related_category_fill` | `true` | Naplnění prázdné lexical related řady. |
| `related_category_topup` | `true` | Doplňování krátké exact/vector related řady; false stále dovolí naplnění prázdné. |
| `other_grade_top_n` | `2` | Maximum oddělených nabídek jiných ročníků. |
| `other_grade_min_ratio` | `0.50` | Podíl nejlepšího ranku pro other-grade nabídku. |
| `other_grade_max_df_share` | `0.10` | Odstranění příliš obecných tokenů v offered korpusu. |
| `other_grade_min_rank` | `0.15` | Absolutní minimum other-grade ranku. |
| Vector `cosine_min / fetch_k / tier_band` | `0.75 / 20 / 0.001` | Zdrojový semantic fallback; účinný floor určuje profil organizace. |
| Vector `suggest_top_n` | `3` | Related návrhy vedle vektorové odpovědi. |
| Route `phrase_slack / meta_max_words / identity_max_words` | `3 / 10 / 8` | Guard phrase okno, krátká meta/identity otázka. |
| Route `meta_intent_guard` | `true` | Meta intent guard. |

Related otázka z jiného packu musí projít jak lexical floor, tak cross-pack floor. Kategorie je
stejný `tags.category` v **packu zodpovězené entry**, se zachováním identity, ročníků, platnosti
a oblasti. Ročníkový filtr siblings používá ročníky **zodpovězené entry**, nikoli přímo profil
uživatele. Při předaných konceptech siblings řadí coverage, jinak stabilní `entry_key`; lexical
A používá IDF váhy, exact/vector finalizace jednotkové váhy jednoduchých obsahových tokenů.
Other-grade návrhy používají obsah bez synonym, vlastní df filtr, rank floor a poměr k nejlepší
shodě. Wrapper je může přidat i vedle již vyřešené odpovědi.

Konverzační dedupe vyřazuje již položené otázky a posouvá již zobrazené, nepoužité nabídky na konec.
Offline klient potřebuje vlastní historii. Zdroje: [qa_common](../../search/app/nodes/qa_common.py#L350),
[qa_exact](../../search/app/nodes/qa_exact.py#L137), [conversation](../../search/app/conversation.py),
[config fallbacky](../../search/app/config.py#L94).

## QA vector a hranice offline režimu

QA vector z registry vybere model a hledá cosine distance nad dostupnými QA embeddings.
V pásmu `distance <= min(distance) + tier_band` preferuje vlastní subjekt → organizaci → ostatní,
potom distance a `entry_id`. `similarity = 1 - distance`; až vybraný hit musí projít cosine floor.
Bez aktivních vektorů pro model přeskočí embedding dotazu, při chybě pokračuje.

Vektory, model registry ani běhový embedding model současný SQLite pack neobsahuje. Offline
návrh musí určit, zda tuto cestu podporuje kompatibilním lokálním modelem, nebo zveřejní omezený
exact/lexical profil. Obdobně offline QA pack neobsahuje RAG dokumentové chunky, generátor ani
LLM přepsání follow-up dotazu. Zdroj: [qa_vector.py](../../search/app/nodes/qa_vector.py).

## Co se skutečně přenáší

| Podklad | SQLite export dnes | Dopad |
|---|---|---|
| Odpovědi, canonical otázky a varianty | Ano: `entries`, `variants` | Obsah je dostupný. |
| `t_answer`, `t_suggest`, `m_margin` | Ano v `pack_manifest` | Jen manifestové prahy, ne účinný profil settings. |
| Norm/lemma/stem texty | Ano | Query normalizátor a jeho konfigurace chybí. |
| FTS | `variants_fts` nad norm+lemma, `variants_tri` nad norm | Stem není ve FTS indexu; backend algoritmus se tím nepřenáší. |
| Entity, visibility/owner, role, grade/domain a platnost | Ano jako metadata řádků | Klient je musí vyhodnotit; export nyní filtruje entity+active, nikoli datum či identitu/roli příjemce. |
| Category, tags a asset_refs | Ano | Related politika chybí; citované soubory nejsou automaticky přibalené. |
| Pipeline a scoring parametry | Ne | Chybí enabled/on_hit, form, limity, gates a related politika. |
| Stoplisty, synonyma, engines a jazyková data | Ne | Chybí shodné zpracování dotazu a konceptů. |
| Guard pravidla a jazykové zprávy | Ne | Nezaručená stejná guard politika. |
| Hierarchie a číselníky | Ne | Samotná číselná ID nevysvětlují organizační vztahy, oblasti a ročníky. |
| IDF statistický korpus | Ne | Jediný exportovaný pack obecně nestačí pro backendové N/df. |
| QA embedding/model | Ne | Semantic cesta není tímto packem zajištěná. |
| Běhová verze/hash pravidel | Ne | `schema_version` je verze nahraného QA formátu, nikoli scoring/normalizer profile. |
| Mobilní import/download protokol | V prohlédnutém APP main není QA import/lookup | Exportér vrací místní `pack_path`; existující chat je online, backend → mobilní offline QA cesta zde není implementovaná. |

Přesný schválený layout je [SQLite kontrakt](../../contracts/sqlite-qa-export.md) a
[DDL](../../contracts/sqlite-qa-export.sql); implementace je v
[exportním flow](../../langflow_flows/ingestion/EDU_AI_EXPORT_QA_PACK.json).
CSV `/v1/qa-export` a portálový ZIP `manifest.json + packs/*.csv` slouží editaci/reuploadu;
neobsahují úplný runtime profil ani tokenové indexy. Zdroje:
[qa_csv](../../search/app/qa_csv.py), [export service](../../qa_portal/backend/app/services/export_service.py),
[ZIP](../../qa_portal/backend/app/services/zip_io.py), [qa_admin metadata](../../search/app/qa_admin.py).

## Požadavky na budoucí offline kontrakt

Nejprve určit rozsah: shodný QA engine, nebo výslovně verzovaný omezený offline engine.
Vlastní SQLite ranking se stejnými názvy parametrů není automaticky shodný QA engine.
Rozšíření exportního schématu, prahů nebo kaskády vyžaduje samostatný návrh a souhlas podle
AGENTS.md; následující seznam je požadavek, nikoli již implementovaný formát.

1. **Svázaný snapshot:** obsah packů a účinný profil organizace ze stejného exportního bodu;
   identita settings, verze obsahu, normalizátoru, algoritmu a slovníků, hash a kompatibilita klienta.
2. **Účinné parametry:** resolved A/B/C prahy i ostatní limity/gates/form a related politika;
   pravidla pro explicitní override, fallback a kombinaci více packů.
3. **Normalizace:** engine/language konfigurace, stoplisty všech používaných scopes, synonymní
   skupiny a slovníky nebo definovaný kompatibilní runtime. Žádné pevné tenant/jazyk hodnoty v klientu.
4. **Ranking:** přesná definice ranku, IDF corpus/df, tierů, marginu, trigram fallbacku a category
   řazení. Pro backendovou paritu testovat korpus více packů a nepřenášet neoprávněná data kvůli statistikám.
5. **Filtry a oprávnění:** autorizační export/profil příjemce, hierarchie, role/owner/validity,
   číselníky a tři stavy oblasti, i pro návrhy a other-grade cestu.
6. **Capabilities:** exact/lexical/vector/context/guard a způsob chování při chybějícím modelu,
   expirovaném balíku, neznámé verzi, chybějících datech či nenalezené odpovědi.
7. **Doručení a aktualizace:** download/import, kontrola součtů, atomické nahrazení celého svázaného
   snapshotu, zachování místní konverzace a návrat k poslednímu platnému balíku.
8. **Parity sada:** syntetické dotazy a očekávané tokeny, vybraný entry_id, rank/coverage/margin,
   rozhodnutí, nabídky a scope; překlepy, více packů, ročníky, platnost, změna profilu a odmítnutí neznámé verze.

## Rozpory nalezené při ověření

Zdroje se nesmějí zaměnit bez přiznání rozporu. Následující jsou závady dokumentace proti
aktuálnímu kódu; tento audit nemění kaskádu ani scoring, nezavírá je změnou backendového chování.

| Místo | Popis v dokumentaci | Zkoumaný kód |
|---|---|---|
| `docs/ai/qa-fastpath.md` | B je terminální stop | B-deferred pokračuje; vyřešená A skončí podle on_hit. |
| Stejná stránka | manifest → params → fallback | params → manifest → fallback. |
| Stejná stránka | prostý podíl konceptů | IDF weighted coverage, s plain fallbackem. |
| Stejná stránka | category stabilně entry_key | při konceptech coverage; jinak entry_key. |
| Stejná stránka | „No time-based expiry“ | runtime WHERE kontroluje valid_from/valid_to. |
| Stejná stránka | other-grade pouze když vlastní ročník nic nemá | wrapper může nabídky přidat i k odpovědi. |
| Parity README | existující offline Dart engine v jiném repo | APP existuje; QA normalizační port/pack engine v prohlédnutém main nalezen nebyl, klientská parita není PASS. |

`SETTINGS_REFERENCE` uvádí referenční vector floor `0.65`; zdrojový fallback je `0.75`.
Jde o různé konfigurační vrstvy; účinnou hodnotu určuje konkrétní profil.
Veřejná kopie nevypisuje hodnoty živé instalace. Souhrn ostatních konfigurací:
[SETTINGS_REFERENCE](../SETTINGS_REFERENCE.md). Důkazy auditu zůstávají mimo Git; do mobilního
balíku se nesmějí přibalit serverové klíče, executable prompty nebo rozšířený školní obsah.
