# Dokumentace EDU-AI

| Potřebuji | Stránka |
|---|---|
| Čistě instalovat na Ubuntu | [CLEAN_INSTALL.md](CLEAN_INSTALL.md) |
| Provozní kontroly, QA export a požadavky na zálohu (bez postupu zálohy a obnovy) | [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md) |
| Vyvíjet a testovat | [DEVELOPMENT.md](DEVELOPMENT.md) · [ONBOARDING.md](../ONBOARDING.md) |
| Připravit vydání | [RELEASING.md](RELEASING.md) · [rozsah ověření](releases/public-source.md) |
| Nastavit organizaci a data | [DATA_MODEL_AND_SETUP.md](DATA_MODEL_AND_SETUP.md) |
| Zjistit váhy a nastavení | [SETTINGS_REFERENCE.md](SETTINGS_REFERENCE.md) |
| Přidat jazyk | [ADDING_A_LANGUAGE.md](ADDING_A_LANGUAGE.md) |
| QA packy a mobilní offline provoz | [offline-qa-rules.md](integrations/offline-qa-rules.md) |
| Volat API | [OpenAPI](../contracts/openapi.yaml) · [veřejné chyby](../contracts/errors.yaml) |
| Najít skript | [scripts/README.md](../scripts/README.md) · [tools/README.md](../tools/README.md) |
| Pochopit architekturu | [system.md](architecture/system.md) · [deployment.md](architecture/deployment.md) · [rozhodnutí](architecture/decisions.md) |
| Pochopit QA/RAG pravidla | [routing](ai/routing.md) · [QA fast-path](ai/qa-fastpath.md) · [RAG](ai/retrieval.md) · [cache](ai/caching.md) · [eval](ai/evaluations.md) |
| Technické návrhy instalace a rolí | [plans/README.md](plans/README.md) |

## Obrázky

[Zapojení komponent](architecture/diagrams/components.svg),
[sítě a porty](architecture/diagrams/networks.svg),
[QA/RAG průchod](architecture/diagrams/query-flow.svg) a
[perimetr](architecture/diagrams/perimeter.svg).
Diagramy a jejich zdroje popisuje [deployment.md](architecture/deployment.md).
Interní provozní reporty a inventáře nejsou součástí veřejné dokumentace.
