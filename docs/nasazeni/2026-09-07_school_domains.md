# Číselník: Provoz školy a Bezpečnost a zdraví

Následně nasazeno na produkci společně s
[doménami jednotlivých QA odpovědí a reingestem](2026-09-07_qa_entry_domains.md).
Níže je původní záznam přípravy migrace.

Na žádost uživatele byly 7. 9. 2026 doplněny domény `SCHOOL_OPERATIONS`
(Provoz školy) a `SCHOOL_SAFETY` (Bezpečnost a zdraví). Překlady jsou v cs,
en, de a es. Kódy mají standardní vyhledávací roli a nemají nová routerová
klíčová slova. Změna nepřiřazuje domény existujícím FAQ ani dokumentům.

## Instalace

[Migrace 214](../../postgres/init/214_school_operations_safety_domains.sql)
přidá chybějící kódy globálně a ke všem vlastníkům aktivního DOMAIN katalogu.
Nevytváří vlastní katalog škole, která jej dědí: tím by zastínila zděděné
oblasti. Opakovaný běh zachovává existující řádky, jejich ID, kurátorské
úpravy i neaktivní kódy. Při pozdějším založení vlastního katalogu musí
kurátor zahrnout i tyto domény, pokud je škola potřebuje.

Na existující databázi je nutné migraci spustit explicitně; samotný commit,
pull ani restart kontejneru ji neaplikuje. Například z kořene repozitáře
v shellu sh/bash na cílovém serveru:

```sh
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
  < postgres/init/214_school_operations_safety_domains.sql
```

Pro nový prázdný datový svazek se po konsolidovaném seedu provede
[identická doplňková SQL migrace](../../postgres/init_clean/03_school_operations_safety_domains.sql).
Generované dumpy se nemění. Shodu obou SQL souborů kontroluje test;
při úpravě se kopie obnoví z migrace 214.

API `/v1/domains` čte katalog při každém požadavku. Webchat načítá stejný
katalog přes BFF, takže nevyžaduje změnu frontendu; již otevřený chat je
třeba obnovit. Na cílovém prostředí ověřit oba kódy přes katalog se skutečnou
entitou a potom jejich dostupnost ve výběru oblastí. Bez označeného obsahu
je `no_answer` při výběru nové oblasti očekávatelný.

## Ověření

- `python tests/test_school_domains_migration.py`: prošlo. PostgreSQL test
  používá izolované schéma s rollbackem a skutečnou funkci řešení katalogu.
  Ověřuje vlastní, zděděný i globální katalog, zachování původní domény,
  opakovaný běh, neaktivní kód, překlady a kurátorskou úpravu.
- Lokální migrace vložila 6 záznamů: globál a dva vlastní katalogy Nové Vsi.
  GET katalogu vrátil nové kódy se správnou diakritikou. POST `/v1/ask` pro
  každou novou doménu vrátil HTTP 200 a odpovídající `effective_scope.domains`;
  outcome byl `no_answer`.
- Normalizační parita: všech 30 vektorů prošlo. Spuštění souboru v kontejneru
  potřebuje `PYTHONPATH=/app`; `stoplist.json` musí zůstat vedle testu.
- `python -X utf8 docs/tools/docs_check.py`: prošlo.
- `python -X utf8 tests/eval_rag.py --repeat 3`: 43 případů, 39 `no_answer`,
  3 meta odpovědi, 1 chyba. Případ N34 požaduje `ZS_KAVCI_HORY_ZS`, která
  v lokálním seedu neexistuje (HTTP 400). Regrese tedy není plně zelená a
  bez reálného korpusu nepotvrzuje kvalitu odpovědí. Použit byl aktuální
  pracovní regresní soubor, jehož již existující úprava není součástí commitu.

Produkce nebyla změněna. Browser test na produkci nebyl proveden. Následujícím
samostatným datovým krokem je označení odpovědí packu `faq-provoz`.
