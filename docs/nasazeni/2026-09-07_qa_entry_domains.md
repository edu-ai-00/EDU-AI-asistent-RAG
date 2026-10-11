# Domény po odpovědích a reingest faq-provoz

Nasazeno 7. 9. 2026 (Europe/Prague) na žádost uživatele. Implementace
`38b719e`, doplnění databázových vstupů normalizátoru `474affe`.

## Změna

[QA flow](../../langflow_flows/ingestion/EDU_AI_QA.json) čte volitelný sloupec
`domains` jednotlivých řádků CSV. Neprázdná sada nahrazuje domény uploadu,
prázdná buňka je dědí a `[]` znamená bez označení. Kódy se normalizují a
ověřují proti účinnému katalogu entity před zápisem QA. Kategorie `cat`
se nemění. Binding se počítá pro jednotlivé odpovědi; manifest shrnuje celý
aktivní pack i při částečném merge. Export vrací domény jako JSON pole.
Podrobný formát CSV je zaznamenán v `QA/packs_faq_provoz/README.md`
v původním repozitáři; adresář `QA/` se školními daty není součástí
distribučního balíčku. [API kontrakt](../../contracts/openapi.yaml) je
součástí změny i distribučního balíčku.

Nasazení zahrnulo migraci 214, import pouze QA flow a rebuild/recreate pouze
search služby kvůli CSV exportu. Rozpracované změny webchatu zůstaly zachované.

První import skončil před QA writerem: původní QA flow nemělo v uloženém
template databázové vstupy normalizační komponenty, přestože je její kód už
vyžadoval. Ověřeno také v záloze původního živého flow. Oprava doplňuje vazby
na existující proměnné, nikoli heslo do souboru; test nyní zahrnuje skutečný
normalizátor a kontroluje přítomnost i zapojení těchto vstupů.

## Produkční data

Pack `faq-provoz`, entita `ZS_KAVCI_HORY_ZS`, reingest v režimu `merge`.
Dokončený processing UUID: `46d3bb85-bf4e-4c24-8fe1-7c9460cf75ba`.
HTTP 200, status `completed`, 1 soubor v pořádku, 0 chyb.

Před změnou byly soubory porovnány s aktivními odpověďmi a doplněny skutečné
stabilní klíče. CSV jsou v původním repozitáři v
`QA/packs_faq_provoz/faq_provoz_v1.csv` a `QA/faq_provoz_v1.csv`, obě shodné;
do distribučního balíčku se školní data nepřibalují. Skutečný uploader
byl převzat z existujícího manifestu pouze za běhu; v repozitáři není jeho ID.

Záloha původních QA tabulek a živého flow, odpovědi ingestu i měření jsou na
serveru v `/tmp/faq-provoz-entry-domains` s omezeným přístupem. Je to dočasná
provozní záloha, nikoli dlouhodobý archiv. Historie QA zůstává v databázi.

| Doména | Aktivní odpovědi včetně překryvů |
|---|---:|
| ATTENDANCE | 14 |
| GRADING | 33 |
| SCHOOL_EVENTS | 3 |
| CATERING | 1 |
| BULLYING | 3 |
| DATA_PROTECTION | 1 |
| SPECIAL_NEEDS | 4 |
| SCHOOL_OPERATIONS | 35 |
| SCHOOL_SAFETY | 13 |

## Ověření

- [Integrační test](../../tests/test_qa_entry_domains.py) prošel proti lokálnímu
  PostgreSQL: skutečný reader, exploder, normalizátor, writer a export;
  různé sady, dědění, vymazání, merge, stabilní ID, propagace, odmítnutí
  neznámého kódu bez částečného zápisu a chybné tvary buněk.
- Všech 7 testů konfigurace flow a kontrola dokumentace prošly.
- Na produkci všech 100 aktivních odpovědí odpovídá CSV doménami a zachovalo
  své ID, texty, kategorie, zdroje i metadata oprávnění. Zachováno všech
  500 aktivních formulací otázek. Aktivní varianty i embeddings mají domény
  shodné s odpovědí; žádné odpovědi nechybí aktivní varianta ani embedding.
- 11 scoped API dotazů pokrylo všech 9 domén a jeden překryv. Všechny vrátily
  `answer / qa_exact`; query log potvrdil očekávané stabilní ID odpovědi.
  Účinný filtr odpovídal každému požadavku.
- Export všech 100 odpovědí zachoval přiřazení domén. BFF webchatu pro Kavčí
  hory vrací obě nové oblasti. Vizuální browser test nebyl proveden.
- Dvě otázky s nesouvisející oblastí přes `/search/v1/ask` (omluvenka pod
  CATERING a jídelna pod GRADING) vrátily `no_answer` se správným účinným filtrem.
- Produkční normalizační parita prošla před reingestem i na nové search
  službě: 30 vektorů beze změny.

Regresní měření používá stejných 42 případů třikrát před a po změně.
Původní sada má fiktivní entity Nové Vsi; pro produkční měření byly v její
dočasné kopii nahrazeny odpovídajícími entitami Kavčích hor. Původní pokus
bez této úpravy měl 42 chyb neznámé entity a nebyl použit jako baseline.
Baseline pro porovnání je `20260906-224215.json`, následný snapshot
`20260906-225617.json`. Oba běhy: 39 `answer`, 2 `no_answer`, 1 `suggest`,
žádné API chyby. Výsledné outcomes se nezměnily. Jediný zaznamenaný rozdíl
je počet zdrojů u N26 (2 → 1); případ kolísal už v baseline i v novém běhu.
Oba běhy mají stejné dva kolísavé případy N11 a N26. Měření nepotvrzuje
zlepšení kvality ani pokles kvality; potvrzuje zachování výsledných stavů
této neomezené sady. Funkčnost nového filtru dokládají samostatné scoped testy.
