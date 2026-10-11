# Čisté nasazení na nové Ubuntu VM

Postup je pro nový izolovaný Linux hostitel bez jiného stacku. Zdroj je Git checkout
přesného schváleného veřejného commitu; ZIP bez `.git` instalátor nepřijme.
Ověření konkrétního vydání musí uvádět jeho SHA. Offline testy nejsou VM akceptace.

## Rychlá cesta od čisté VM

Přihlaste se přes SSH jako běžný uživatel se `sudo`. Následující postup používá
veřejný HTTPS klon, nový adresář zdroje a oddělený config/state. Bloky spouštějte
postupně, při chybě nejprve vyřešte její příčinu.

1. **Git a přesný zdroj.**

   ```bash
   sudo apt-get update
   sudo apt-get install -y git ca-certificates openssh-client
   umask 022
   mkdir -p "$HOME/edu-ai"
   git clone --single-branch --branch main https://github.com/edu-ai-00/EDU-AI-asistent-RAG.git "$HOME/edu-ai/src"
   cd "$HOME/edu-ai/src"
   CANDIDATE_SHA=$(git rev-parse HEAD)
   git checkout --detach "$CANDIDATE_SHA"
   git rev-parse HEAD
   git status --short
   ```

   Zdrojový adresář musí být nový, strom čistý. Zaznamenejte celý SHA pro testy
   a předání. `umask 022` vytvoří nový rodičovský adresář s právy 0755; existující
   rodič se tím nemění a preflight jeho nevyhovující práva odmítne. `.env` ani
   config předem nevytvářejte, nekopírujte tajnosti a runtime z jiné instalace.
2. **Připravit hostitele a znovu se přihlásit.**

   ```bash
   bash scripts/prepare_ubuntu.sh --check
   bash scripts/prepare_ubuntu.sh --apply
   ```

   Skript doplní Python, Docker Engine, Compose, Buildx a další nástroje. Po
   přidání do skupiny `docker` se odhlaste a přihlaste přes SSH znovu; Docker
   musí fungovat bez `sudo`.
3. **Spustit průvodce.** Po novém přihlášení:

   ```bash
   cd "$HOME/edu-ai/src"
   python3 scripts/setup.py install
   ```

   Potvrďte plán Enterem. Průvodce připraví konfiguraci `~/edu-ai/config`, stav
   `~/edu-ai/state`, projekt `edu_ai`, bootstrap se smoke a kontrolu rolí.
   `NETWORK_POOLS` lze přijmout slovem `ano` jen jako jediný neověřený nález na
   izolované VM. Jiný problém instalaci zastaví. Volitelný čtecí přehled:
   `python3 scripts/setup.py plan`.
4. **Otevřít chat a ověřit výsledek.** Vyžadujte `BACKEND_READY: yes`, role
   `MATCH` a smoke bez FAIL. Otevřete vypsanou adresu `http://<adresa VM>/chat/`
   a vyberte školu; do výběru je odesílání vypnuté. Ověřte reload a změnu školy
   s vyčištěním konverzace. Osm služeb má být healthy, pgAdmin running bez
   healthchecku. Čistý seed nemá QA ani dokumentový corpus.

QA exact může po importu QA odpovědět bez placeného LLM. Embeddingy a generované
odpovědi vyžadují poskytovatele a klíč v Bifrostu. Instalace a kvalita odpovědí
jsou samostatné kontroly. [Rozsah doložených běhů](releases/public-source.md).

## Síť a konfigurace

Výchozí Compose publikuje porty 80, 8443 a 8444. Omezte přístup na síťovém
perimetru před startem. Průvodce nekontroluje firewall ani izolaci a nezapíná
Cloudflare. `NETWORK_POOLS` zůstává neověřené: automatické Docker sítě se neměří
proti LAN/VPN. Na izolované VM lze přijmout pouze tento jediný nález odpovědí `ano`.
Jiné odmítnutí nebo neověřený nález musí zastavit instalaci.

Neinteraktivní ekvivalent pro operátora, který toto omezení výslovně přijímá:

```bash
python3 scripts/setup.py install --no-input --accept-unmeasured-network-pools
```

Nová konfigurace dostane `CHAT_ENTITY_PICKER=true`; `CHAT_LEGAL_ENTITY` zůstává
nenastavené. Výběr školy není autentizace. Backend dál kontroluje entitu, role a
viditelnost. Pevně nastavenou školu ani existující volbu pickeru průvodce nepřepíná.
Nenastavený nebo neplatný registr škol má vlastní stav; první školu automaticky nevolí.

## Chyba a opakování

Nenulový návrat bootstrapu je chyba. Neobcházejte smoke. Průvodce nic nemaže a
nevrací; opakování připustí jen pro vlastní konfiguraci stejného commitu, klíče a
projektu. Ověřuje lokální Docker engine, vlastnictví kontejnerů, mountů, sítí a
volumes. Cizí konfiguraci, změněný HEAD, špinavý strom nebo Compose overrides odmítne.
Klíče se při opakování negenerují znovu.

Pro diagnostiku po úspěšném průchodu:

```bash
python3 scripts/smoke.py --source-root "$HOME/edu-ai/src" --config-dir "$HOME/edu-ai/config" --state-dir "$HOME/edu-ai/state" --project edu_ai
python3 scripts/db_roles.py verify --container edu_ai_postgres
docker compose --project-directory "$HOME/edu-ai/src" --env-file "$HOME/edu-ai/config/.env" -f "$HOME/edu-ai/src/docker-compose.yml" -p edu_ai ps
git rev-parse HEAD
```

LLM poskytovatel se nastavuje samostatně v Bifrostu; klíč v klientském `.env` sám
poskytovatele nenastaví. Administrační UI zpřístupněte jen oprávněnému správci.
Zdravé kontejnery ani smoke nedokládají placenou ingesci nebo kvalitu RAG.

Provoz a zálohy: [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md). Vývoj a testy:
[DEVELOPMENT.md](DEVELOPMENT.md). Balení: [RELEASING.md](RELEASING.md).
