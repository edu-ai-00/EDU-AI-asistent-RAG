# EDU-AI asistent RAG module

Multilingual QA and retrieval over school documents. The Docker Compose stack contains
nginx, the ingestion gateway, Langflow, PostgreSQL, pgAdmin, search, web chat, QA portal
and Bifrost. Cloudflare is an optional tenth service.

Retrieval behaviour comes from organisation settings, pack manifests and versioned
database configuration. Every search request requires a legal entity. Entity, role,
visibility and active-document filters are enforced by the backend.

## Start here

| Task | Guide |
|---|---|
| Install on a fresh Ubuntu host | [Clean installation](docs/CLEAN_INSTALL.md) |
| Operate an installation | [Linux operations](docs/OPERATIONS_LINUX.md) |
| Develop and test | [Development](docs/DEVELOPMENT.md) · [Local onboarding](ONBOARDING.md) |
| Understand components, networks and execution | [Deployment diagrams](docs/architecture/deployment.md) · [Search architecture](docs/architecture/system.md) |
| Configure organisations and languages | [Settings](docs/SETTINGS_REFERENCE.md) · [Data model](docs/DATA_MODEL_AND_SETUP.md) · [Languages](docs/ADDING_A_LANGUAGE.md) |
| Understand QA weights and mobile offline requirements | [QA rules and offline pack audit](docs/integrations/offline-qa-rules.md) |
| Call the API | [OpenAPI contract](contracts/openapi.yaml) |
| Find a script | [Scripts](scripts/README.md) · [Tools](tools/README.md) |
| Prepare a release | [Release rules](docs/RELEASING.md) |
| Browse all documentation | [Markdown index](docs/README.md) |

## Clean installation

Use a real Git checkout of the approved public commit, on a fresh isolated Ubuntu
host. Follow [the full guide](docs/CLEAN_INSTALL.md), including host preparation and
a new login after joining the Docker group. Then run from the checkout:

```bash
python3 scripts/setup.py plan
python3 scripts/setup.py install
```

The wizard prepares external configuration and state, runs bootstrap with smoke
checks, and verifies the database role contract. A new configuration enables the
school picker: open `/chat/` and choose the school in the browser. No terminal
command is needed to select the school. LLM providers are configured separately in
Bifrost; smoke checks do not demonstrate paid ingestion or answer quality.

## How it works

- Ingestion through nginx: `POST /api/v1/ingest` selects a Langflow flow that writes chunks and
  embeddings to PostgreSQL.
- Answers: `POST /search/v1/ask` executes the configured QA/RAG cascade.
- Chat: the browser calls its BFF, which checks the selected active school before
  passing the request to search.

Code, templates, configuration seed, flows, contracts and tests live in Git.
Runtime databases, uploaded files, credentials and private school QA packs belong
outside Git. The seed contains configuration and approved organisation metadata,
not an installation's document or QA corpus. Public evaluation cases are synthetic
examples; prepare a corpus-specific set before measuring retrieval quality.

The SQLite QA export currently lacks some rules needed for equivalent mobile
offline answers. Its existence is not a parity guarantee; see the QA audit above.
The source repository is not an approval for production migration or a supported
school upgrade. Lab backup tools retain their own layout and ownership guards.

## Repository map

| Path | Responsibility |
|---|---|
| `api/` | Ingestion gateway |
| `search/` | Retrieval service and normalisation |
| `web_chat/` | Browser chat and BFF |
| `qa_portal/` | QA administration |
| `langflow_flows/` | Versioned ingestion and export flows |
| `postgres/init_clean/` | Generated clean-install baseline |
| `postgres/role_contract/` | Database role contract |
| `contracts/`, `specs/` | Public interfaces and specifications |
| `scripts/`, `tools/` | Installation, diagnostics and laboratory utilities |
| `tests/`, `docs/` | Tests and documentation |

AI coding agents should read [AGENTS.md](AGENTS.md).

## License

Copyright 2026 **EDU-AI, o.p.s.**, IČO 01674846. Licensed under the **Apache
License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE), which carries
the attribution notices required by §4(d) of the License.

The work was commissioned and funded from a state subsidy by EDU-AI, o.p.s.

Third-party components (all containers: Langflow, PostgreSQL + pgvector,
pgAdmin, nginx, FastAPI stack, …) are listed with licences and authors in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Anyone redistributing this work, or a derivative of it, must keep the copyright,
patent and attribution notices and the `NOTICE` file (License §4). Most component
logic lives inside `langflow_flows/*.json` rather than in `.py` files, so every
component authored for this project carries a one-line header naming who last
changed its code and when — Langflow's built-in and derived components carry
Langflow's MIT notice as well (see NOTICE).

---

*Repo: `github.com/edu-ai-00/EDU-AI-asistent-RAG` · Contact: info@edu-ai.eu*

The [source verification record](docs/releases/public-source.md) separates real
VM installation/resume evidence from the final combined source's offline checks.
