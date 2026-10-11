# EDU-AI — provoz na Linuxu

Novou instalaci připravuje [průvodce](CLEAN_INSTALL.md). Zdroj, konfigurace a stav
mají samostatné adresáře; provozní tajnosti a data se nepřidávají do Git.
Tento návod ani laboratorní nástroje nezavádějí podporovaný produkční upgrade.

## Kontrola běžícího stacku

Pro výchozí layout:

```bash
SRC=$HOME/edu-ai/src
CFG=$HOME/edu-ai/config
STATE=$HOME/edu-ai/state
PROJECT=edu_ai
cd "$SRC"
python3 scripts/smoke.py --source-root "$SRC" --config-dir "$CFG" --state-dir "$STATE" --project "$PROJECT"
python3 scripts/db_roles.py verify --container edu_ai_postgres
docker compose --project-directory "$SRC" --env-file "$CFG/.env" -f "$SRC/docker-compose.yml" -p "$PROJECT" ps
```

Vyžadujte role `MATCH`, smoke bez FAIL a očekávané běžící služby. Vypnutý volitelný
Cloudflare nebo chat čekající na školu mají výslovný stav. Smoke je neplacená
funkční kontrola; nenahrazuje ingest, regresi na vlastním korpusu ani test obnovy.

## Škola a poskytovatel

Nová konfigurace umožňuje výběr aktivní školy v `/chat/`. Uživatel ji vybírá
v prohlížeči, žádný další SQL ani terminálový příkaz není potřeba. Změna školy
vyčistí konverzaci a oblasti. Výběr nepředstavuje autentizaci ani přidělení role.
Pevná instalace může mít `CHAT_LEGAL_ENTITY`; existující konfiguraci průvodce zachová.

Poskytovatele a jeho klíč spravuje Bifrost. Administrační přístup omezte; klíče
nevkládejte do Git ani do veřejných reportů. Placené ověření potřebuje vlastní
rozsah, testovací vstupy a zaznamenaný výsledek.

## QA export

Exportérem je Langflow flow `langflow_flows/export/export_csv_admin.json`.
Nástroje a rozhraní najdete v [scripts/README.md](../scripts/README.md),
[tools/README.md](../tools/README.md) a [indexu testů](../tests/README.md).
SQLite export obsahuje QA data; je soukromým výstupem konkrétní instalace.
[QA audit](integrations/offline-qa-rules.md) popisuje přenášená pravidla a mezery
pro mobilní offline provoz. FTS index sám nedokládá paritu.

## Záloha, obnova a změna zdroje

Záloha musí zahrnout obě databáze, role, externí config včetně klíčů, potřebné
bind mounty a named volumes, zdroj i identitu image. Obsahuje tajnosti; chraňte ji
a ověřte SHA-256 i skutečnou obnovu na vlastní izolované kopii. Změna souboru nebo
large object vložená po záloze musí po obnově zmizet. Aplikace spouštějte až po
kontrole rolí, dat, klíčů, souborů a správných image.

[`tools/lab/`](../tools/lab/README.md) obsahuje starší laboratorní pomocníky.
[`tools/lab/cycle/`](../tools/lab/cycle/README.md) je driver s přísnými vlastnickými
branami pro jediný layout; nelze ho vydávat za obecný updater. Před jeho použitím
ověřte všechny předpoklady z jeho README. Migrační runner B03e je schválen pouze
pro izolované syntetické databáze; nepoužívejte ho na produkci ani školní instalaci.
Kontrakt rolí: [B06 C2](plans/b06-c2-role-contract.md).

Vydání zaznamenává přesný commit, image, testy a omezení. Starý PASS pro jiný
commit nenahrazuje nový čistý průchod. Viz [RELEASING.md](RELEASING.md).

Skutečná VM evidence a hranice finálního zdroje: [public-source.md](releases/public-source.md).
QA exact může fungovat bez provider klíče; generované odpovědi a embeddingy potřebují Bifrost.
