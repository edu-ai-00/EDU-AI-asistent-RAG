# Výběr oblasti hledání — kontrakt pro mobilní aplikaci

Připraveno 6. 9. 2026. Popisuje **API, které je hotové a otestované** proti
skutečnému nasazenému rozhraní; mobilní klient podle něj **ještě není
implementovaný** a tento dokument je zadání pro mobilní tým, ne popis
vydané funkce. Mobilní zdrojový kód není v tomto repozitáři. Existující Flutter
aplikace je v [EDU-AI-asistent-APP](https://github.com/edu-ai-00/EDU-AI-asistent-APP);
čtecí kontrola 11. 10. 2026 ověřila main `e653aea54d5e408bd3667f4ab087e0920814cc76`.
Formulace „ještě není implementovaný“ se zde vztahuje k této integraci, nikoli
k existenci aplikace jako celku. [QA offline audit](offline-qa-rules.md) popisuje
ověřený online chat, lokální cache a chybějící QA-pack engine.

Návrh a rozhodnutí: [`specs/draft/spec.search-domain-scope.md`](../../specs/draft/spec.search-domain-scope.md).
Chování serveru: [`docs/ai/routing.md`](../ai/routing.md) §4–5.
Chybové obálky: [`contracts/errors.yaml`](../../contracts/errors.yaml).

---

## 1. Co to dělá

Uživatel vybere jednu nebo více **oblastí** (docházka, stravování, přijímačky…)
a hledání pak čerpá **jen ze zdrojů označených alespoň jednou z nich**. Mobil
může oblast nastavit i podle otevřené sekce aplikace, aniž by uživatel volil
ručně — je to obyčejný parametr požadavku.

Tři věci, které to **nedělá**, protože každá z nich už byla někým očekávána:

* **Není to oprávnění.** Filtr může zdroje jen odebírat. Škola, role,
  viditelnost a platnost platí za ním nezměněné, takže volbou oblasti se nikdo
  nedostane k ničemu, co by bez ní neviděl.
* **Neroztřiďuje text.** Označení je na *dokumentu* a na *odpovědi*, ne na
  jednotlivém odstavci. Dlouhý dokument (školní řád) může mít několik oblastí,
  **nedělí se** a je dosažitelný v každé z nich celý; který odstavec odpoví,
  rozhoduje jako dosud dotaz. Praktický důsledek: dotaz na docházku zadaný v
  oblasti „Stravování“ může stále najít docházkovou pasáž takového dokumentu —
  ten dokument je opravdu i o stravování.
* **Samo se nerozšíří.** Když ve vybraných oblastech nic není, přijde
  dosavadní `no_answer`. Nikdy se automaticky nehledá znovu bez filtru.

---

## 2. Katalog oblastí

```
GET https://<host>/search/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS
```

```json
{
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "domains": [
    {"code": "SECONDARY_ADMISSIONS",
     "labels": {"cs": "Přijímačky na střední školu", "en": "Secondary-school admissions",
                "de": "Aufnahme an die Mittelschule", "es": "Admisión a la escuela secundaria"}},
    {"code": "ATTENDANCE",
     "labels": {"cs": "Docházka a absence", "en": "Attendance & absence"}}
  ]
}
```

**Kódy i názvy ber odsud.** V aplikaci nesmí být vlastní seznam kódů ani vlastní
překlady: katalog je **per škola** (v produkci 8 globálních, 11 u obou jednotek
Kavčích hor) a vlastní kopie by se s databází rozešla. Nový jazyk je v `labels`
další klíč, ne nová verze aplikace; chybějící jazyk řeš fallbackem
`labels[jazyk] → labels["cs"] → code`.

`legal_entity` v odpovědi je škola, pro kterou se katalog **skutečně** vyřešil.
Ulož si volbu **pod tuto hodnotu**, ne pod tu, na kterou jsi se ptal. Katalog
načtený pro jednu školu a použitý při hledání v jiné dá kódy, které server
odmítne 422.

Endpoint je nově v allowlistu nginxu (6. 9. 2026); do té doby vracel 404. Je to
referenční data jako `/search/v1/code-lists` — jde cachovat, žádný obsah
uživatele neobsahuje. Bez `legal_entity` vrátí globální sadu, což je jen
kompatibilita pro jiné volající; **klientské hledání vždy posílá skutečnou
školu**.

Nedostupný katalog je `503 UNAVAILABLE` s `retryable: true`. Ukaž chybu a
zkusit znovu — **ne** prázdný seznam: prázdný seznam se v UI přečte jako „škola
nemá žádné oblasti“ a filtr tím potichu zmizí.

---

## 3. Dotaz

```
POST https://<host>/search/v1/ask
POST https://<host>/search/v1/ask/stream        (SSE, stejná sémantika)
```

```json
{
  "question": "Kdy se podává přihláška?",
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "user_role": "PARENT",
  "user_id": "…",
  "conversation_id": "11111111-1111-1111-1111-111111111111",
  "query_id": "app-42",
  "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"]
}
```

| `domains` | Význam |
|---|---|
| pole chybí, nebo `null` | bez tematického omezení — **přesně dosavadní chování** |
| neprázdné pole známých kódů | OR mezi oblastmi, AND se všemi dosavadními filtry |

**Pole vynechávej, když filtr není** — neposílej `null`. Tělo je pak bajt po
bajtu stejné jako dnes, což je to, co drží kompatibilitu `Idempotency-Key`
(viz §6).

Server kódy **normalizuje**: trim, velká písmena, odstranění duplicit, stabilní
seřazení. `[" attendance ", "ATTENDANCE", "CATERING"]` je totéž jako
`["ATTENDANCE", "CATERING"]`, takže pořadí ani duplicity nemusíš řešit.

### Odpověď

Přidané je jedno pole v `effective_scope`; ostatní se nemění.

```json
{
  "answer": "…",
  "outcome": "answer",
  "answer_source": "qa_exact",
  "effective_scope": {
    "legal_entity": "ZS_KAVCI_HORY_ZS",
    "role": "PARENT",
    "school_grade": null,
    "language": null,
    "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"],
    "identified": true
  }
}
```

`effective_scope.domains` je **kanonická množina, kterou server skutečně
použil**, nebo `null` při neomezeném hledání. Vždy je to ověřená hodnota — i u
`outcome: "role_required"`, kde validace oblastí schválně proběhla dřív než
kontrola role. **Porovnávej ji s normalizovanou odeslanou množinou**:
odstraň okrajové mezery, převeď kódy na velká písmena, odstraň duplicity
a seřaď je. Například `[" attendance ", "ATTENDANCE"]` se správně vrátí
jako `["ATTENDANCE"]`; to není změna scope ani problém s katalogem.
Vynechané pole a `null` porovnávej jako stejnou neomezenou volbu.

Neznámý nebo neaktivní kód server odmítne odpovědí **422**; v úspěšné odpovědi
jej potichu neodebere. Pokud se skutečná množina liší i po normalizaci,
považuj to za nesoulad požadavku a odpovědi k prověření, ne automaticky za
změnu katalogu. Filtr kvůli tomu sám neuvolňuj.

Interní databázová ID oblastí se nikdy neposílají a nepotřebuješ je.

---

## 4. Chyby

| Stav | Kód | Kdy | Co má klient udělat |
|---|---|---|---|
| 422 | `VALIDATION_ERROR` | `[]`; prázdný nebo mezerový prvek; **neznámý/neaktivní kód pro tuhle školu**; špatný typ; jeden špatný kód mezi správnými | znovu načíst katalog a nechat uživatele vybrat; `fields[].field` je `domains` nebo `domains[1]` |
| 503 | `UNAVAILABLE` | katalog se nepodařilo přečíst při validaci **explicitního** filtru | zkusit znovu se **stejnými** `domains` |
| 409 | `IDEMPOTENCY_CONFLICT` | stejný `Idempotency-Key`, jiný scope | nový klíč — je to jiný dotaz |
| 200 | `outcome: "no_answer"` | platný kód, ve kterém nic není | nabídnout výslovnou akci „Hledat ve všech oblastech“ (§5) |

**Jeden špatný kód shodí celý dotaz.** Server množinu potichu nezmenší — odpověď
na dotaz o třech oblastech vyřízená dvěma by byla odpověď na jinou otázku.

```json
{"error": {
  "code": "VALIDATION_ERROR",
  "message": "domains: unknown or inactive domain code(s) 'PRIJIMACKY'",
  "retryable": false,
  "fields": [{"field": "domains[0]",
              "message": "unknown or inactive domain code 'PRIJIMACKY' for this school; see GET /v1/domains"}]
}}
```

**Nikdy neopakuj dotaz bez `domains`.** Ani po 422, ani po 503. Širší odpověď je
jiná odpověď a vydávat ji za tu vyžádanou je přesně ta chyba, kvůli které tenhle
filtr existuje. Rozšíření je rozhodnutí uživatele.

**U SSE (`/ask/stream`) přijde validační chyba jako obyčejná HTTP 422 ještě
před prvním eventem** — stream se vůbec neotevře. Až po otevření streamu je
selhání `event: error`.

---

## 5. Životní cyklus v aplikaci

Pět pravidel; každé z nich odpovídá jedné konkrétní chybě, kterou webová verze
musela ošetřit (`web_chat/frontend/src/DomainPicker.tsx` je referenční
implementace).

1. **Pozdní katalog nepřepíše novější.** Když uživatel dvakrát rychle přepne
   školu, může první odpověď dorazit poslední. Hlídej si generaci požadavku —
   samotné zrušení nestačí, zrušený požadavek se ještě může dokončit.
2. **Uloženou volbu ověř, nikdy jí nevěř.** Kódy jsou per škola a kurátor je
   může vyřadit. Průnik s novým katalogem, a co vypadlo, **uživateli řekni** —
   nevyhazuj to potichu, on si to vybral. Ukládej pod účinnou entitu.
3. **Dokud katalog neznáš, scoped dotaz neposílej.** Ani se starými kódy (422),
   ani bez filtru (potichu se hledá všude).
4. **Změna školy nebo oblasti = nová konverzace.** Nové `conversation_id`,
   oddělená historie, zrušený rozpracovaný dotaz. Server historii filtruje
   podle oblasti také sám — na klientovo nové UUID se nespoléhá, protože ho
   posílá klient — ale staré chips na obrazovce patří starému scope a nabízely
   by dotazy do oblasti, kterou uživatel právě opustil.
5. **„Všechny oblasti“ je skutečná, stále viditelná volba**, ne absence volby, a
   je výchozí. Odznačení poslední oblasti se na ni vrací; **prázdné pole nikdy
   neposílej** (je to 422). Samotná změna výběru **žádný dotaz neodesílá** —
   jen určuje, kam bude patřit ten příští.

Když po `no_answer` nabídneš „Hledat ve všech oblastech“, kliknutí odešle
**nový** požadavek: `domains: null`, nové `conversation_id` a nový
`Idempotency-Key`, pokud ho používáš.

---

## 6. Idempotence a historie

**`Idempotency-Key` starých požadavků funguje dál.** Server před hašováním
vynechá `domains`, když je `null`, takže digest neomezeného požadavku je stejný
jako před touto změnou a opakování dotazu odeslaného starou verzí aplikace se
korektně replayne. Ověřeno testem, který vytvoří claim hašovaný *starým* kódem
(`tests/test_domain_scope_api.py`).

Explicitní filtr do digestu vstupuje v kanonické podobě: `["B","a"]` a
`["A","b"]` jsou tentýž dotaz a replaynou se navzájem, **stejný klíč s jiným
scope je 409**. TTL, retence ani chování streamu se nemění.

**Historii server filtruje podle oblasti.** Návazný dotaz vidí jen předchozí
kola téže konverzace zadaná **ve stejném scope**; kola bez filtru patří jen
neomezenému hledání. Platí i tehdy, když `conversation_id` recykluješ.

---

## 7. Co ještě není a co je potřeba domluvit

* **Mobilní klient.** Neexistuje. API je hotové a ověřené přes skutečný
  proxy hop; UI, ukládání volby a vydání je na mobilním týmu.
* **Data.** Filtr je jen tak dobrý, jak jsou označené zdroje. Ke 6. 9. 2026 je
  na produkci **pět aktivních QA packů (230 odpovědí) bez jakéhokoli označení** —
  ty jsou pod explicitním filtrem nedosažitelné — a `ZS_KAVCI_HORY_SOS` má 104
  aktivních chunků, z nichž **žádný** nemá oblast. Report:
  `python3 scripts/domain_inventory.py --remote edu-ai`. **Nezapínejte výběr
  oblastí ve škole, jejíž obsah není označený** — uživatel dostane `no_answer` na
  otázky, na které systém odpovědět umí.
* **„Přijímačky / Provoz“** jako dvě zkratky nejsou totéž co katalog DOMAIN.
  Pokud je produkt potřebuje, jsou to samostatně schválené *pojmenované profily*
  (kód, přeložené názvy, výslovná množina domén) — mobil je **nesmí** hádat z
  názvu kódu a „Provoz“ nesmí znamenat doplněk všeho ostatního. Viz spec §8.
