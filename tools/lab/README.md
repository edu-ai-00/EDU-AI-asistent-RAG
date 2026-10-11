# tools/lab — laboratorní záloha a obnova (jen izolovaný syntetický stack)

> **Stav: laboratorní nástroje, ne produkční zálohovací systém.** Skripty jsou přenesené beze změny z akceptačních
> běhů `edu_ai_acc` na VM. Jsou napsané pro jeden pevně daný laboratorní layout (níže) a **nesmějí se spouštět na
> produkci, na školní instalaci ani proti cizímu stacku**. Obnova je destruktivní (maže projekt, config i state).
> Ověření platí jen pro přesné bajty uvedené v oddíle 1; každá úprava je nové, neověřené chování.

Provozní kontext: [docs/OPERATIONS_LINUX.md](../../docs/OPERATIONS_LINUX.md) (oddíl o záloze a obnově).
Pro layout `edu_ai_final` (izolované laboratoři) s ověřením vlastního stacku viz [`cycle/`](cycle/README.md); skripty níže zůstávají beze změny.

## 1. Soubory, původ a SHA-256

| Soubor | SHA-256 | Role |
|---|---|---|
| `acc_backup.sh` | `96676ac1356f711c190c1d076881d14b5b8dd455e21d0646ebcaa5b805ba5a20` | záloha |
| `acc_restore.sh` | `0e9b7fb890c5d39adc704c935df7cf2be6261ca8b0469346efefba5829555770` | úplná obnova |
| `acc_snap.sh` | `145f831fd29e168a1882cfa760034551ddf2e8fdd5a191f855d9462bde3d2784` | snapshot stavu (hashe, počty, ID; bez hodnot tajností); obnova z něj čte očekávaný stav |
| `acc_lib.sh` | `84798145554e93b112866ec3a172519c084bcbea24ef570ba4e02d5ed5b62825` | sdílené proměnné (`source` ze všech tří) |

Soubory jsou LF, bez změny obsahu; `sha256sum tools/lab/*.sh` musí dát hodnoty výše.

**Původ (ve všech třech akceptacích byte-identické, kromě jedné řádky v `acc_lib.sh`):**

| Akceptace (`tests/eval_out/…/tools/`) | Co s nimi prošlo |
|---|---|
| `acc_closeout_20261009` (zdroj `7ef6047`) | záloha `A1`, úplná obnova `A1`: raw baseline shodný, role `MATCH` před aplikacemi |
| `sqlite_export_acceptance_20261010` (zdroj `fbadbf0`) | záloha a obnova `CI_B3` (`rec/G4_candidate_backup_restore.txt`: `pg_restore` rc=0 u obou DB, raw baseline shodný, `DB_CONTRACT: MATCH`, `snap_CI_POST == snap_CI_PRE`, smoke 26/0/1); obnova `UPD_B2` |
| `field_order_acceptance_20261010` (zdroj `b78b2e0`) | obnova `UPD_B2` (`rec/S1_restore_UPD_B2.txt`: raw baseline shodný, role `MATCH`, RPO značky 0/0) |

Výběr nebyl podle názvu ani data souboru. `acc_backup.sh`, `acc_restore.sh` a `acc_snap.sh` mají ve všech třech
adresářích stejný SHA-256, takže nejde o rozhodnutí mezi verzemi. Jediný rozdíl je v `acc_lib.sh`: starší
`acc_closeout` (SHA-256 `8265cb92b4bc5e93e2fa5c49988de9c756329367b77f97ce85c7def898325275`) má `SRC=$A/src`, verzovaná
podoba má `SRC=${ACC_SRC:-$A/src}` a je v kopiích z obou akcí 2026-10-10. Bez nastavené `ACC_SRC` je chování shodné.
Důkazy výslovně nedokládají, kterou podobu VM načetla při běhu `CI_B3`; ověřeno je, že obě dávají při výchozím `SRC`
stejnou hodnotu.

**Nepřeneseno a proč:** `acc_verify.sh` a `acc_identity.sh` (závisí na nezverzovaných `probe.py`, `ask.py`,
`api_tools.py`, `b06_qa_fixture_cases.py`), `sbx.sh` a `sb_*.sh` (laboratorní sandbox: síťový namespace a vlastní
Docker démon na konkrétní VM). Zůstávají jen v ignorovaných důkazech `tests/eval_out/`.

## 2. Předpoklady (laboratorní, pevně zadrátované)

- **Vlastní izolovaný syntetický stack** Compose projektu `edu_ai_acc` na VM, uvnitř sandboxu (vlastní netns, vlastní
  `dockerd` a containerd; `sbx.sh` jim podstrčí socket jako výchozí démon). Bez sandboxu skripty mluví s výchozím Docker démonem hostitele.
- Adresáře: `$HOME/acc/{src,config,state,backup,rec,tools}`. `src` je čistý Git checkout (viz OPERATIONS_LINUX, oddíl 2),
  `config` obsahuje `.env` a `install-record.json`, `state` obsahuje `uploads`, `langflow_data`, `sqllite_export`, `bifrost_data`.
  `src` obsahuje symlinky na tyto čtyři adresáře a `.env`. `ACC_SRC` přepíše jen `src`.
- **Skripty se spouštějí z `$HOME/acc/tools/`**: `acc_lib.sh` se načítá pevnou cestou `$HOME/acc/tools/acc_lib.sh`. Zkopírujte
  `tools/lab/*.sh` tam (zachovat `+x`) a po kopii ověřte SHA-256.
- Pevná jména: projekt `edu_ai_acc`, DB uživatel `edu_ai_admin`, kontejnery `edu_ai_postgres` a `edu_ai`
  (`container_name` v `docker-compose.yml`), DB `edu_ai` a `langflow`.
- Zdroj obsahuje `scripts/db_roles.py` s příkazy `verify`, `globals`, `reapply --confirm-isolated`. `acc_snap.sh` volá bifrost
  na `127.0.0.1:8099`.
- `bash`, `python3`, `docker compose` (≥ 2.24), `git`, `tar`, `sha256sum`, **`sudo` bez hesla** (state patří uživatelům kontejnerů).
- Skripty nemají `set -e` ani parametrickou kontrolu: chybějící argument nebo prostředí se neodhalí předem.

## 3. Použití

Vše uvnitř sandboxu, jako běžný uživatel (`sbx.sh bash $HOME/acc/tools/<skript> …`). Stack musí běžet.

```bash
# 1) snapshot stavu těsně před zálohou (do ~/acc/rec/snap_<LABEL>.txt)
bash $HOME/acc/tools/acc_snap.sh BEFORE_X

# 2) záloha NAME do ~/acc/backup/NAME (psaní se na okno zálohy zastaví a pak znovu spustí; ~65–80 s)
bash $HOME/acc/tools/acc_backup.sh X

# 3) ÚPLNÁ obnova (DESTRUKTIVNÍ), očekávaný stav = snapshot z kroku 1
bash $HOME/acc/tools/acc_restore.sh X BEFORE_X
```

Snapshot z kroku 1 musí vzniknout těsně před zálohou bez zápisů mezi nimi, jinak se raw baseline po obnově nebude shodovat.
Vše, co vzniklo po záloze, se podle RPO ztratí.

**Záloha** `~/acc/backup/NAME` (adresář 0700, soubory 0600): `edu_ai.dump`, `langflow.dump` (custom formát), `globals.sql`
(role a granty; obsahuje hashe hesel, proto 0600), `role_verify_at_backup.txt`, `config.tgz` (`.env`, `install-record.json`),
`state.tgz` (uploads, `langflow_data` včetně `secret_key`, `sqllite_export`, `bifrost_data`), `META.txt`, `SHA256SUMS`.
Zapisuje jen jména a hashe, ne hodnoty tajností.

**Obnova** v tomto pořadí: kontrola SHA256SUMS → `down -v` projektu → smazání `config` a `state` → rozbalení → jen `postgres` →
`pg_restore` obou DB → **`db_roles.py reapply` a `verify` před jakoukoli aplikační službou** → porovnání raw baseline
(počty, alembic head, md5 každého flow, drafty, proměnné) proti snapshotu → teprve potom ostatní služby.

## 4. Omezení a nálezy (kontrola ochran, 2026-10-10)

Skripty nebyly kvůli nim měněny (ověřená logika, viz oddíl 1). Ochrana v kódu skriptů chybí v těchto bodech; v laboratoři ji
dnes nahrazuje sandbox a pozorný operátor.

1. **Žádná kontrola, že běží proti vlastnímu stacku.** Kontejnery se oslovují pevným jménem (`docker exec/start edu_ai_postgres`,
   `docker inspect edu_ai`) a nic neověřuje, že patří projektu `edu_ai_acc` ani že démon je sandbox. Mimo sandbox by při kolizi
   jmen `up -d postgres` pravděpodobně selhal (netestováno), skript by pokračoval a `pg_restore --clean --if-exists` by šel do **cizí** databáze. Jediné,
   co omezuje `down -v`, je `-p edu_ai_acc`. **Spouštět jen přes `sbx.sh`.** (`reapply` vyžaduje `--confirm-isolated`, ale
   neověřuje izolaci.)
2. **Kontrola `SHA256SUMS` při obnově není brána.** Její výsledek se vypíše a obnova pokračuje; smazání projektu, `config` i `state`
   proběhne i při neshodě nebo chybějícím archivu. Před obnovou ručně ověřte `sha256sum -c` a že všech sedm souborů je OK.
3. **Výsledky kroků se nevynucují.** `pg_restore` rc, `reapply`/`verify` role i diff raw baseline se tisknou, ale služby se
   spustí i po selhání. Pořadí (role před aplikacemi) je správné, jeho dodržení při chybě je na čtení výstupu.
4. **Rozsah mazání obnovy:** `find $SRC -maxdepth 1 -type l -exec rm -f` smaže **všechny** symlinky v kořeni `src`, ne jen pět vlastních;
   `ACC_SRC` může ukázat jinam. `rm -rf $CFG` a `sudo rm -rf $ST` předcházejí jakékoli kontrole.
5. **Neobsažený stav:** Docker svazky `pgadmin_data`, `qa_admin_state` a `reranker_models` se nezálohují a `down -v` je smaže.
   Postgres data se obnovují z dumpů, ne ze svazku. Zdrojový checkout, image ani Cloudflare se nezálohují
   (identita image je jen v `META.txt`).
6. **Drobnosti:** `META.txt` a `SHA256SUMS` vznikají po `chmod 600` a zůstávají 0644 (uvnitř adresáře 0700, bez tajností);
   při selhání `pg_dump` skript skončí s `exit 1` a writery zůstanou zastavené (bez `trap`); snapshoty v `rec/` nesou 16znakové
   prefixy SHA-256 `.env`, `LANGFLOW_SECRET_KEY` a API klíče (hash, ne hodnota) a `rec/` vzniká s výchozí umask.

Nezasahují do cizího stacku ani do globálního stavu Dockeru: skripty nepoužívají žádný `prune`, `rm`/`stop` mimo projekt
`edu_ai_acc` a vlastní adresáře (s výhradami z bodů 1 a 4).

## 5. Co tento adresář neslibuje

Záloha produkce nebo školní instalace, podporovaný upgrade, automatizaci (cron, rotace, off-site kopie), šifrování záloh,
ověření obnovy mimo laboratoř. Změna skriptů vyžaduje novou akceptaci obnovy na izolovaném syntetickém stacku.
