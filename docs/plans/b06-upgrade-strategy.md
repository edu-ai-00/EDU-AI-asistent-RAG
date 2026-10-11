# B06-U: správa verzí a podporované aktualizace

Veřejná kopie vynechává interní akceptační reporty a odkazy na ně; tento návrh není důkazem aktuálního nasazení.

Datum: 2026-10-06. **Stav: požadavek vlastníka evidován, strategie je návrh. Aktualizační nástroj není implementovaný.**
Navazuje na [instalátor B06](b06-clean-install-proposal.md), B03e a celkový plán.
Tento zápis vznikl čtením konfigurace a dokumentace. Žádná verze, image, databáze, `.env`, klíč ani produkční služba se tím nemění.

## 1. Cíl a současná mezera

Správce má později aktualizovat konkrétní instalaci na konkrétní otestované vydání EDU-AI se zachováním dat, konfigurace a klíčů.
Vydání určuje společně ověřenou sadu verzí komponent. Kontrola dostupných nových verzí a jejich zavedení do produkce jsou dva samostatné kroky.
Automatické nasazení pohyblivých tagů `latest` není navrhovaný způsob aktualizace.

B06-1 připravuje novou konfiguraci; není aktualizátor existující školy. B03e-1 má migrační runner pouze pro izolované syntetické databáze.
Podporované školní a produkční aktualizace zůstávají otevřené; tuto hranici určuje [AGENTS.md](../../AGENTS.md).
Skripty S2/R2 jsou specializované přechody nginxu a síťového zapojení, nikoli obecný aktualizátor všech služeb.

## 2. Co je dnes připnuté

Tabulka popisuje deklarace v lokálním zdrojovém stromu, nikoli živě zjištěné verze na produkci. Cílové nové verze se zde nevybírají.

| Oblast | Deklarace a mezera | Zkontrolovaný zdroj |
|---|---|---|
| Langflow | základ `1.10.1`, lokální image `edu_ai:1.1.0`; tag základu bez digestu; přidané balíčky mají zčásti volné verze, constraints vznikají z `pip freeze` základního obrazu během buildu | [Dockerfile](../../Dockerfile), [requirements-langflow.txt](../../requirements-langflow.txt), [Compose](../../docker-compose.yml) |
| PostgreSQL a rozšíření | základ `pgvector:0.7.4-pg16` s digestem, `PG_SEARCH_VERSION=0.25.1`; `.deb` je vázaný na PostgreSQL 16, stahovaný bez kontroly součtu v Dockerfile | [Dockerfile.postgres](../../Dockerfile.postgres) |
| Proxy a tunel | `nginx:1.27-alpine` a `cloudflared:2026.8.1`; tagy samy nezaručují neměnný obsah | [Compose](../../docker-compose.yml) |
| Bifrost a pgAdmin | `bifrost:v1.6.7`, `pgadmin4:9.11.0`; persistentní stav a kompatibilitu je nutné ověřovat samostatně | [Compose](../../docker-compose.yml) |
| Ingest API | základ Python `3.11.16` s digestem a přesné Python závislosti; systémové balíčky apt se řeší při buildu | [API Dockerfile](../../api/Dockerfile), [API requirements](../../api/requirements.txt) |
| Search | základ Python `3.12-slim` s digestem; řada závislostí používá dolní mez `>=`, lemma engine `simplemma==2.0.0` je připnutý | [Search Dockerfile](../../search/Dockerfile), [Search requirements](../../search/requirements.txt) |
| Chat a QA portál | frontend má `package-lock.json`; základ Node `24.18.0-alpine`, runtime Python `3.12-slim` bez digestu; backend QA má také rozsahy `>=` | [chat Dockerfile](../../web_chat/Dockerfile), [chat lock](../../web_chat/frontend/package-lock.json), [QA Dockerfile](../../qa_portal/Dockerfile), [QA lock](../../qa_portal/frontend/package-lock.json), [QA requirements](../../qa_portal/backend/requirements.txt) |
| Hostitel | Linux nebo budoucí Windows/WSL cesta, Docker Engine/Desktop, Compose, Git a Python mají vlastní kompatibilitu; jejich upgrade může ovlivnit i jiné stacky na hostiteli | [návrh B06](b06-clean-install-proposal.md) |

Závěr ze zdrojů: u některých komponent může i rebuild stejného commitu vyřešit jiné balíčky. Před vydáváním aktualizací potřebujeme evidenci skutečných sestavených artefaktů a závislostí.

## 3. Orientační náročnost

Jde o hrubý plánovací odhad přípravy a ověření jednoho balíku, ne o měření ani dobu výpadku. Velikost dat, počet přeskočených verzí a breaking changes odhad změní.

| Druh změny | Orientační práce | Hlavní důvod |
|---|---|---|
| Menší aktualizace nginxu nebo cloudflared | hodiny až 1–2 pracovní dny | konfigurace, trasy, důvěra proxy, skutečný tunel a návrat |
| Knihovny API, chatu, QA portálu nebo search | 1–3 pracovní dny za související balík; při změně veřejného rozhraní více | build, API kontrakty, UI a chování vyhledávání |
| Langflow nebo Bifrost | několik pracovních dnů až týdny při velkém skoku | flows, vlastní komponenty, poskytovatelé, API a případné migrace jejich stavových DB |
| Hlavní verze PostgreSQL a databázová rozšíření | několik pracovních dnů až týdny | kompatibilita rozšíření, migrace dat/indexů, obnova a změřené přerušení |
| Embedding model, dimenze, stemming nebo normalizace | samostatný odhad po návrhu | přepočet indexů/vektorů, parita a nové měření retrieval výsledků |

První zavedení podporovaného aktualizačního procesu je samostatná práce nad těmito odhady. Pravidelná údržba menších změn má snížit budoucí velké skoky.
PostgreSQL rozlišuje minor aktualizace se zachovaným formátem dat a major upgrady, které vyžadují migrační postup; viz [oficiální dokumentace](https://www.postgresql.org/docs/current/upgrading.html).

## 4. Návrh pravidel vydání

- Každé vydání má neměnný Git SHA, registry digesty image pro podporovanou platformu, skutečné verze balíčků, verzi schématu a kompatibilní verze hostitelských nástrojů. Evidenci generovat z buildu, navázat na B03; existující generované manifesty ručně neupravovat.
- Pro komponenty s volnými závislostmi doplnit reprodukovatelné zamčení výsledných verzí. Připnutí digestů doplnit o plán jejich pravidelné obnovy, aby nezamrazilo bezpečnostní opravy.
- Aktualizace komponent připravovat v oddělené větvi nebo kopii z `clean_init`, s malými souvisejícími změnami. Vydat až společně otestovaný výsledek.
- Definovat přesně podporovaný přechod vydání A → B. Přeskočení jiných vydání se nepovažuje automaticky za podporované.
- Zachovat dodanou konfiguraci, hesla, API klíče a `LANGFLOW_SECRET_KEY`. Aktualizace je nesmí tiše přegenerovat.
- Změny modelů, dimenzí, databázového schématu/indexů a pipeline precedence mají samostatné schválení podle AGENTS.md. Změna balíčku je nesmí zavést skrytě.
- Kontrola nových verzí a bezpečnostních oprav může být později automatizovaná. Nasazení probíhá v řízeném okně; v tomto kroku se žádná automatizace nezřizuje.

## 5. Postup jednoho upgradu

1. **Inventář:** zjistit přesné aktuální artefakty a stav dat, přečíst release notes komponent i všech přeskočených hlavních verzí. Určit cílové vydání a podporovaný pár A → B.
2. **Plán:** vypsat dotčené služby, změny konfigurace, potřebné migrace, diskové místo, zálohu, očekávané přerušení a návrat. Ukázat, které kontroly zatím nelze ověřit.
3. **Nácvik:** na vlastní izolované instalaci se syntetickými daty ověřit skutečný přechod, zachování konfigurace/klíčů, opakované spuštění a selhání v každé nevratné fázi.
4. **Akceptace:** provést příslušné buildy, API/UI smoke, scope a retrieval kontroly, flows a dešifrování credential proměnných. Při změně normalizace parita před/po; při změně retrieval chování regrese s `--repeat`. Placené volání až po výslovném zadání s limitem.
5. **Záloha a obnova:** uchovat odpovídající DB/data, konfiguraci a klíče, obrazy i metadata starého vydání. Klíče uložit chráněně; kopii úschovy držet odděleně od DB zálohy. Obnovu skutečně nacvičit v laboratoři.
6. **Nasazení:** po kontrole aktuálního stavu provést jen schválený plán a dotčené služby. Zachovat ostatní stacky, rozložení sítí a konfiguraci Cloudflare; změny těchto oblastí patří do konkrétního plánu, nikoli do vedlejších účinků upgradu.
7. **Kontrola a evidence:** funkční ověření a sledování, záznam verzí/hashů a výsledků, otevřené návratové okno. Staré artefakty mazat až po samostatném uzavření okna.

Návrat obrazu aplikace není automaticky návrat dat: nový Langflow, Bifrost nebo DB může změnit schéma či formát svého stavu.
Kde zpětná kompatibilita není prokázaná, návrat znamená obnovu odpovídající zálohy; plán musí určit zacházení se zápisy vzniklými po upgradu.
Žádný obecný `down -v`, `prune` ani regenerace `.env` nepatří do aktualizačního postupu. Současný migrační runner zůstává jen pro izolované syntetické DB.

## 6. Budoucí nástroj a rozdělení práce

Navrhované funkce: zobrazení verzí, čtecí plán přechodu, preflight, záloha/evidence, provedení konkrétního přechodu, verify a návrat podle typu dat.
Tyto funkce dnes nejsou implementované; neuvádí se spustitelný příkaz aktualizace školy. Windows vstup má později použít stejný prověřený postup přes WSL, s vlastním ověřením Docker Desktopu a souborového systému.

- **B06-U1:** inventář a návrh kompatibility vydání; jde připravovat souběžně s dokončením první instalace, pouze z kódu a veřejných podkladů.
- **B06-U2:** generovaný popis vydání a zamčení závislostí, navázané na B03; prototyp/testy v izolaci.
- **B06-U3:** první konkrétní přechod A → B, laboratorní migrace a obnova; před školním/produkčním použitím vyřešit otevřenou podporu migrací B03e-2/D11.
- **B06-U4:** společný aktualizační vstup a provozní runbook, včetně Windows/WSL; hotovo až po nácviku reálného přechodu a návratu.

Akceptace aktualizátoru vyžaduje prokázaný přechod i návrat, zachované klíče a data, bezpečné odmítnutí nepodporované výchozí verze,
pravdivé výsledky neověřených kontrol a dokumentaci přesného podporovaného prostředí. Úspěšná čistá instalace tuto akceptaci nenahrazuje.

## 7. Veřejné podklady

- [PostgreSQL: upgrading a cluster](https://www.postgresql.org/docs/current/upgrading.html) — rozdíl minor/major a migrační metody.
- [Docker: Compose in production](https://docs.docker.com/compose/how-tos/production/) — build a výměna dotčených služeb.
- [Langflow: deployment on Docker](https://docs.langflow.org/deployment-docker) — oficiální cesta k verzování, upgrade a rollback guidance; aktuální veřejná stránka není kontraktem našeho obrazu 1.10.1.

Před provedením konkrétního upgradu znovu zkontrolovat oficiální podklady pro přesné výchozí a cílové verze. Tento návrh nezjišťuje nejnovější verzi každé komponenty a žádnou nevolí k nasazení.
