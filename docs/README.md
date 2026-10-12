# EDU-AI documentation

| I need | Page |
|---|---|
| A clean install on Ubuntu | [CLEAN_INSTALL.md](CLEAN_INSTALL.md) |
| Operational checks, QA export and backup requirements (without the backup and restore procedure) | [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md) |
| To develop and test | [DEVELOPMENT.md](DEVELOPMENT.md) · [ONBOARDING.md](../ONBOARDING.md) |
| To prepare a release | [RELEASING.md](RELEASING.md) · [scope of verification](releases/public-source.md) |
| To set up an organisation and data | [DATA_MODEL_AND_SETUP.md](DATA_MODEL_AND_SETUP.md) |
| To look up weights and settings | [SETTINGS_REFERENCE.md](SETTINGS_REFERENCE.md) |
| To add a language | [ADDING_A_LANGUAGE.md](ADDING_A_LANGUAGE.md) |
| QA packs and mobile offline operation | [offline-qa-rules.md](integrations/offline-qa-rules.md) |
| To call the API | [OpenAPI](../contracts/openapi.yaml) · [public errors](../contracts/errors.yaml) |
| To find a script | [scripts/README.md](../scripts/README.md) · [tools/README.md](../tools/README.md) |
| To understand the architecture | [system.md](architecture/system.md) · [deployment.md](architecture/deployment.md) · [decisions](architecture/decisions.md) |
| To understand the QA/RAG rules | [routing](ai/routing.md) · [QA fast-path](ai/qa-fastpath.md) · [RAG](ai/retrieval.md) · [cache](ai/caching.md) · [eval](ai/evaluations.md) |
| Technical proposals for installation and roles | [plans/README.md](plans/README.md) |

## Diagrams

[Component wiring](architecture/diagrams/components.svg),
[networks and ports](architecture/diagrams/networks.svg),
[QA/RAG query flow](architecture/diagrams/query-flow.svg) and
[perimeter](architecture/diagrams/perimeter.svg).
The diagrams and their sources are described in [deployment.md](architecture/deployment.md).
Internal operational reports and inventories are not part of the public documentation.
