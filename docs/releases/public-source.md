# Veřejný zdroj a rozsah ověření

Zdroj publikace z 11. 10. 2026: upstream integrační commit
`c079bd8065061a0a6e58f8468f4233014d0d71f1`. Veřejný Git má vlastní SHA; pro instalaci
použijte skutečný commit tohoto repozitáře z `git rev-parse HEAD`.

Runtime kód, konfigurace a role, čistý DB seed, flow, kontrakty, normalizace,
modely a závislosti jsou převzaté z uvedeného upstreamu. Veřejná kopie zobecňuje
jednu síťovou popisku a laboratorní docstring, nikoli algoritmy. Interní inventáře,
provozní reporty a původní eval dotazy z korespondence nejsou ve veřejném stromu.
Testovací harnessy zůstávají; veřejné otázky jsou syntetické příklady, které se
nesrovnávají s historickým privátním benchmarkem. Licence a attribution notices zůstaly.

## Doložený skutečný VM průchod

| Kontrola | Původní zdroj e40a1c0 | Opravený zdroj 06de66d |
|---|---|---|
| Instalace, 9 služeb, role MATCH | PASS, studený build | PASS, build cache |
| Smoke | 26 PASS / 0 FAIL / 3 SKIP | 26 / 0 / 3 |
| Browser: škola, reload, změna, neplatná uložená volba | PASS | PASS |
| QA exact bez provider klíče a izolace jiné školy | PASS | PASS |
| Autentizovaný Langflow SQLite export/FTS a úklid fixture | PASS | PASS |
| Opakování stejného průvodce | FAIL, RESUME_STACK_OTHER_LAYOUT | PASS, zachované klíče, flow, image a kontejnery |

Tyto krátké SHA identifikují upstream důkazy, nejsou instrukcí checkoutu tohoto
veřejného repa. 54 původních souborů podkladů bylo kontrolováno SHA-256; chráněné
provozní důkazy uchovává správce mimo veřejný Git. První FAIL se nepřeznačuje na PASS.
Druhý browser test měl samostatný origin a localStorage, nikoli samostatný profil.
Opravený běh neprošel úplně studeným buildem. Firewall/izolace, placené LLM/RAG/ingest,
Cloudflare a nový VM průchod finálního kombinovaného veřejného SHA jsou NOT_RUN.

## Konečná integrace

Upstream spojuje opravu souhrnu preflightu, přesnou kontrolu zkopírovaných inline
Compose configů a odmítnutí komprimovaného taru v `docker cp`. Společné testy průvodce
64/64, další instalační offline sady, BFF 57/57 a skutečný SQLite export 25/25 prošly.
Veřejný checkout má vlastní kontrolu součtů, dokumentace a instalačního preflightu.
Tato offline kontrola není nový VM PASS ani schválení produkčního upgradu.

Historický `source.base_commit` v B216 zůstává původní informativní proveniencí.
Jeho přítomnost v Git se výslovně přeskočí, pokud čistý veřejný nebo mělký checkout
historický objekt neobsahuje; chyby samotného Git se nepřeskakují. Testy hashů,
úplnosti SQL a kanonického manifestu zůstávají povinné. Generované databázové
podklady a parity vektory jsou byte-identické s upstreamem.

Veřejná historie pokračuje od původních dvou commitů a obsahuje jejich historické
QA cesty, i když aktuální strom `QA/` nemá. Privátní Git objekty nebyly přeneseny.
Viz [RELEASING.md](../RELEASING.md), [QA/offline audit](../integrations/offline-qa-rules.md),
[čistá instalace](../CLEAN_INSTALL.md) a [Markdown index](../README.md).
