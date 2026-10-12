# EDU-AI — releasing and public sources

The source commit, the runtime images, the ZIP and the acceptance evidence are
different outputs. The installer requires an actual Git checkout: it reads
`.env.example` and the Compose file from the blobs of the given SHA. A source ZIP
without `.git` is therefore not an installation checkout.

## Public Git

The public repository carries a sanitised source tree, technical documentation,
contracts, tests and synthetic eval examples. Private history cannot be published
by merely deleting files in the last commit. The transfer therefore copies the
reviewed tree without private Git objects and continues with an ordinary
fast-forward onto the already existing public history.

The original two public commits contain old paths under `QA/`; they remain among
the ancestors. The current tree does not contain them. This source transfer does
not rewrite the already published history and does not claim that it was completely
sanitised. The imported objects come exclusively from the existing public repo, not
from the history of the non-public development.

Private QA packs, questions from correspondence, uploads, `.env`, keys, operational
data, internal inventories and acceptance evidence do not belong in the public
repo. The configuration seed, the approved public organisation metadata and the
synthetic fixtures are part of the sources. Generated SQL, manifests and parity
vectors are not changed by hand.

## 2. What the runtime images contain

From the Dockerfiles (`COPY`) it follows that the images carry only the application
code and its dependencies:

| Image | Source copied into the image |
|---|---|
| `edu_ai` (Langflow, `Dockerfile`) | `requirements-langflow.txt`, `search/eduai_normalize` |
| `edu_ai_langgraph` (`Dockerfile.langgraph`; not a service in `docker-compose.yml`, a standalone dev container) | `langgraph_entrypoint.sh` |
| `edu_ai_api` | `api/requirements.txt`, `api/app` |
| `edu_ai_search` | `search/requirements.txt`, `search/eduai_normalize`, `search/app` |
| `edu_ai_web_chat` | `web_chat/frontend` (build), `web_chat/requirements.txt`, `web_chat/web_chat.py` |
| `edu_ai_qa_admin` | `qa_portal/frontend` (build), `qa_portal/backend/requirements.txt`, `qa_portal/backend/app` |
| `edu_ai_postgres` (`Dockerfile.postgres`) | nothing (only the base image and packages) |

The tests are **not** in the image. Tests that run in the image are copied or mounted into it (`docker cp`,
`-v …/tests:/app/tests`); `tests/` must therefore remain in the source checkout.

Operational files taken from the **source checkout** (not from the image) during installation and operation:
`docker-compose.yml` and `.env.example` (the installer), `scripts/` (`setup.py`, `install.py`, `host_preflight.py`,
`bootstrap.py`, `smoke.py`, `import_flows.py`, `create_api_key.py`, `db_roles.py`, `tunnel_subnet.py`, the shared modules
`eduai_layout.py`, `envfile.py`, `http_safe.py`, `prepare_ubuntu.sh`; `flow_rollout.py` serves the flow update, not the installation),
`langflow_flows/ingestion` (flow import), `postgres/init_clean`, `postgres/role_contract` and `postgres/seed` (database
initialisation, the roles contract, the approved organisation data), `nginx/conf` (bind mount) and the build contexts of the services. The list is completed according to the imports
in `setup.py` and `host_preflight.py`; omitting any of them from the checkout breaks the installation.

## Release verification

1. Record the exact SHA of the public candidate and the clean Git state. The tests
   stay in the source checkout; they are not part of the runtime image.
2. Run `python3 docs/tools/docs_check.py`, `git diff --check` and the relevant tests
   from [DEVELOPMENT.md](DEVELOPMENT.md). When normalisation changes, verify parity.
3. Check secrets, private inputs, links, file totals and history.
   Keep a manifest of the explicit omissions and modifications.
4. Verify the new public checkout with the wizard and an actual clean VM run:
   bootstrap, roles MATCH, smoke, chat and a synthetic QA export. Offline tests
   and an older VM result are not an acceptance of the new public SHA.
5. State `NOT_RUN` for a RAG regression, an ingest, paid LLM and a restore that
   were not performed. The public eval examples are not a private regression
   benchmark; for a comparison you need your own same test corpus and the same
   parameters.

Backups and evidence are kept outside Git. A new fix means a new commit and new
totals. Installation procedure: [CLEAN_INSTALL.md](CLEAN_INSTALL.md). Operational
boundaries: [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md). The license and the
mandatory attribution notices remain in [LICENSE](../LICENSE), [NOTICE](../NOTICE)
and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
