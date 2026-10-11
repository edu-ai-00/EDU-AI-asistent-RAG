# Nasazení: stoplist, eduai_normalize 3.0.0, oprava synonym

> **Historický záznam (5. 9. 2026).** Příkazy, tagy obrazů, SHA a stav produkce platí jen pro tehdejší stav; nepoužívejte je jako aktuální postup. Stav „init_clean nenese nové sloupce“ a „prompt v15 běží mimo repo“ je překonán: migrace 211 a 212 jsou v repu a součástí základu B216. Aktuální nginx konfigurace je `nginx/conf/nginx.conf` (Compose mountuje adresář `nginx/conf`), starší `nginx/nginx.conf` ji nenahrazuje.

**Pro:** Jan Drozd
**Od:** Martin Safránek
**Větev:** `chunking-config`, commit `3cc33ec`
**Sepsáno:** 5. 9. 2026, 23:05

---

## Proti čemu je to ověřené

| co | kdy | poznámka |
|---|---|---|
| dump produkční DB | **5. 9. 2026 22:40:41** | 58 967 653 B, `pg_dump` custom |
| poslední `git fetch` do notebooku | **5. 9. 2026 23:01:07** | |
| poslední Honzův commit v této větvi | **4. 9. 2026 18:28** | `1949487` — v mé větvi obsažen |
| můj `HEAD` | **5. 9. 2026 23:05** | `3cc33ec` |

Starší dumpy pro případ návratu: `2026_09_04` (4. 9. 08:39:33) a
`edu_ai_2026_09_03` (3. 9. 16:44:33).

Všechny kroky níž jsem **odzkoušel na obnovené kopii toho dumpu z 22:40**, ne
jen na svém prostředí. Kde píšu očekávané číslo, je to číslo naměřené na té
kopii.

---

## Co se od posledního exportu na serveru změnilo

Porovnáno sloupec po sloupci a řádek po řádku proti dumpu z 5. 9. 22:40:

* **struktura beze změny** — 483 sloupců na serveru, žádný, který bych neměl
* **žádné nové packy** — `qa_pack_manifest` 38, `qa_entries` 563,
  `qa_variants` 7094, `qa_embedding` 7094
* **žádné nové dokumenty** — `document_metadata` 45, `document_chunks` 1348
* shodné jsou i `settings`, `qa_synonym`, `qa_blocklist`, `code_list`,
  `grade_term`, `subject_term`, `ui_label`, `taxonomy_rules`, `ingestion_log`

Liší se jen dvě věci mimo log dotazů, obě tvoje:

* `system_message` — server má o 8 řádků víc (`no_answer_missing_topic`
  a `not_a_question`, každý ve 4 jazycích) z migrací 206–208, které já lokálně
  nemám
* `prompt_registry` — server má `search_answer_generation` **v15** aktivní
  (created_by `codex`, 4. 9. 17:25), já v14. **Nechává se být.**

---

## Než začneš

Produkce **nemá migrace 209 ani 210**. Ověřeno: `qa_stopword` má na serveru jen
`term, lang, is_active, note, created_at, scope` a PK už je
`(lang, term, scope)`.

**209 nenasazuj** — migrace 211 dělá totéž lépe (tři booleany místo jednoho
sloupce `form`) a ruší ji. 211 na to pamatuje: blok, který `form` odstraňuje, je
podmíněný jeho existencí. Bez té podmínky by celá migrace spadla a odrollovala
by se i naplněná data.

**210 ano**, ale až nakonec — krok 5.

Vezmi si dump, než začneš.

---

## 1) Migrace 211

Musí být **před** kódem: `settings.py` už čte `term_norm`.

```bash
docker cp postgres/init/211_stopword_blocklist_reduced_forms.sql edu_ai_postgres:/tmp/
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  -f /tmp/211_stopword_blocklist_reduced_forms.sql
```

Čekej `COMMIT`. Kontrola — musí dát **680 / 680 / 680**:

```bash
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -c \
 "select count(*), count(*) filter (where term_norm<>''), count(*) filter (where term_stem<>'') from edu_ai.qa_stopword;"
```

Migrace přidá `term_norm/term_lemma/term_stem` a `use_norm/use_lemma/use_stem`
do `qa_stopword`, chybějící `term_stem` a příznaky do `qa_blocklist`, a sama si
je naplní (823 UPDATE příkazů se spočítanými hodnotami).

## 2) Build OBOU images

`eduai_normalize` 3.0.0 je rozbíjecí. Jedna strana na staré knihovně znamená, že
index a dotaz redukují jinak.

```bash
docker compose build edu_ai_search edu_ai
docker compose up -d edu_ai_search edu_ai
docker exec -i edu_ai_search python -c "import eduai_normalize as e;print(e.__version__)"
docker exec -i edu_ai       python -c "import eduai_normalize as e;print(e.__version__)"
```

Obojí musí říct `3.0.0`.

## 3) Import flow

**Nejdřív si vyexportuj těch 6 flow ze serveru a porovnej je s repem.**
`import_flows.py` přepíše **všech šest**, ne jen ty dvě změněné, a Langflow si
nedrží historii — `flow_version` je prázdná. Co je upravené jen v serverovém UI
a není v repu, zmizí nevratně.

```bash
python3 scripts/import_flows.py
```

Změnila se jediná komponenta: `ChunkLemmatizer-qa003-QA` v `EDU_AI_QA`. Měla dvě
tiché únikové cesty — bez hesla k DB vracela `(None, True, True)` a výjimku při
načítání spolkla stejně. Knihovna pak podstrčila vestavěný stoplist. Ten už
neexistuje, takže by to spadlo o kus dál a nesrozumitelně; teď odmítne rovnou.
Markdownové komponenty to měly správně už předtím, mění se jim jediný řádek
komentáře.

## 4) Oprava `qa_synonym`

Data, ne schéma. Nejdřív bez `--apply`, ať vidíš co:

```bash
docker cp tools/stopwords/recompute_synonym_forms.py edu_ai_search:/tmp/rs.py
docker exec -i -e PG_PASSWORD=$POSTGRES_PASSWORD edu_ai_search python /tmp/rs.py
docker exec -i -e PG_PASSWORD=$POSTGRES_PASSWORD edu_ai_search python /tmp/rs.py --apply
```

Čekej **27 řádků k opravě** (naměřeno na kopii produkce). Skript bere `PG_DB`,
takže jde nanečisto pustit proti obnovené kopii dřív, než sáhne na ostrou DB.

Proč: 27 ze 100 aktivních řádků má uložené tvary, které neodpovídají tomu, co
kód počítá dnes, a `qa_common.py:100` je čte při každém dotazu. Sedmnáct
německých a španělských se **nikdy nezastemovalo** — seedovalo se s `cfg=None`
a tabulka `_DEFAULT_ENGINE` v kódu pro `de`/`es` záznam neměla, takže
`engine_name` vrátil prázdno a slovo prošlo beze změny (`donde` → `donde` místo
`dond`). Uložený stem se pak s dotazovým nikdy nepotkal.

## 5) Migrace 210 — samostatně, až po zbytku

```bash
docker cp postgres/init/210_qa_exact_form_stem.sql edu_ai_postgres:/tmp/
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  -f /tmp/210_qa_exact_form_stem.sql
```

Čekej `UPDATE 4` (čtyři aktivní řádky `settings`).

Dělá dvě věci: vypíše do konfigurace default, který dosud platil tiše
(`["norm","lemma"]` z `qa_common.py:21`), a přidá třetí tvar — `stem`.

**Je to jediný krok, který doopravdy mění chování hledání**, proto až nakonec:
kdyby se něco zhoršilo, víš čím. Vrátit se dá jedním UPDATE zpět na
`["norm","lemma"]`.

Změřeno na 482 unikátních reálných dotazech: 260 už dnes trefí `qa_exact` přes
norm|lemma, přes stem by přibyl **právě jeden** („kdo resi neomluvenu absenci"),
a ten dnes stejně zodpoví `qa_vector` se skóre 0,9759. Kolizí je mezi 2 092
variantami 7 skupin a všech 7 jsou diakritické dvojice téže otázky, kde je
shoda na kořeni správná.

---

## Co se změnit NEMÁ

Stoplist zůstane **cs 155, de 111, en 112, es 86**. `term_norm` je znak po znaku
to, co je dnes v `term` — `fold("během")` dá `behem`, což je přesně stará
hodnota. Změnil se sloupec, ze kterého se čte, ne obsah.

Ověřeno deterministickým testem: 39 QA-zodpovězených otázek z
`question_answer_log` po nahrání posledního packu, přehráno přes `/v1/ask` —
**39 z 39 trefilo tutéž entry**. Dva běhy téhož testu dají totéž (0 rozdílů),
protože otázka zodpovězená z QA se ke generování nedostane.

Pozor na testování přes korpusy v `tests/eval_*_questions.txt`: ty jdou přes
krok `answer`, tedy přes model, a **nejsou reprodukovatelné** — dva běhy na
nezměněném stavu mi daly jiný výsledek u 5 z 32 otázek. A/B se na nich měřit
nedá.

---

## Návrat

* `postgres/init/211` je transakční — když spadne, nezmění nic
* zálohy tabulek mám lokálně: `qa_stopword_backup_20260905`,
  `qa_synonym_backup_20260905`
* 210 se vrací jedním UPDATE
* build se vrací přepnutím na předchozí tag image

## Co zůstává otevřené

* **`postgres/init_clean/` nenese moje nové sloupce** — konsolidovaný init je
  pozadu za `postgres/init/211`
* lokálně mi chybí tvoje migrace **206–208** (8 hlášek); soubory mám, jen jsem
  je nespustil
* prompt **v15** běží na serveru mimo repo jako vratný experiment
