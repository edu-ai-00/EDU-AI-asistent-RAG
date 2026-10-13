---
id: docs.architecture.deployment
status: current
components: [architecture, deployment, api, retrieval, qa]
sources:
  - docker-compose.yml
  - docker-compose.override.yml
  - nginx/conf/nginx.conf
  - nginx/conf/search_api.conf
  - api/app/main.py
  - search/app/server.py
  - search/app/executor.py
  - search/app/settings.py
  - search/app/scope.py
  - search/app/messages.py
  - search/app/nodes/qa_exact.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/qa_vector.py
  - search/app/nodes/answer.py
last_verified: 2026-10-11
---

# Component wiring, networks and operation

A read-only check of the sources on candidate `8296f2f` on 2026-10-11. The diagrams describe the base
`docker-compose.yml` and the logic of the retrieval service. No specific running VM or external perimeter
is verified by this document. Back to the [documentation index](../README.md).

## Component wiring

![Wiring of the nine base EDU-AI services and the optional Cloudflare tunnel](diagrams/components.svg)

[PNG](diagrams/components.png) · [editable Mermaid source](diagrams/components.mmd)

Solid arrows show calls implemented in the source configuration. Dashed arrows belong to the
optional `cloudflare` profile; the concrete hostname and Cloudflare Access are managed by the operator.
This is not a path that is switched on automatically in a clean installation.

- Nginx routes HTTP. The web chat calls search through its BFF; the QA portal reads through search and publishes through the ingest API.
- The ingest API selects the Langflow flow for the source type. It verifies the user registry, entities and code lists
  over HTTP in the search service; it has no direct access to PostgreSQL and no DB password.
- Langflow writes content to the EDU-AI DB and state to the separate Langflow DB. Search looks up QA
  and documents; the query log and the idempotency have their own writes.
- In the base Compose, both Langflow and search use the OpenAI-compatible endpoint of Bifrost.
  The actual provider, keys and any additional rerankers are determined by the configuration.
- The mobile APP is a separate project. The diagram does not promise a verified direct connection of the APP to the RAG
  or a finished offline QA engine; see the [QA/offline audit](../integrations/offline-qa-rules.md).

## Docker Compose networks

![Complete membership of the services in the six defined Compose networks](diagrams/networks.svg)

[PNG](diagrams/networks.png) · [editable Mermaid source](diagrams/networks.mmd)

Each cell lists the complete set of network members; a service can be in several networks at once.
Teal cells have an ordinary bridge with an outbound route, blue ones have `internal: true`.
Grey is used with the optional profile. The base installation has nine services on five used
networks; Compose also defines a sixth, `tunnel_egress`, for cloudflared.

| Network | Members | Meaning |
|---|---|---|
| `edge` | nginx, edu_ai_api, edu_ai, pgadmin, edu_ai_search, edu_ai_web_chat, edu_ai_qa_admin | Nginx upstreams and outbound access of the applications; nginx is not the only member here. |
| `app` | edu_ai_api, edu_ai, edu_ai_search, edu_ai_web_chat, edu_ai_qa_admin, bifrost; optionally cloudflared | Internal calls between the applications. |
| `data` | postgres, edu_ai, pgadmin, edu_ai_search | Internal database network; nginx, the ingest API, the chat and the QA portal are not here. |
| `bifrost_egress` | bifrost | Outbound access of the gateway to the providers and operational endpoints. |
| `tunnel` | nginx; optionally cloudflared | Internal shared hop; the subnet from `EDU_AI_TUNNEL_SUBNET` delimits the trust in `CF-Connecting-IP`. |
| `tunnel_egress` | optionally cloudflared | Outbound access to Cloudflare. |

The membership comes from the actual `services.*.networks`. An `internal` network on its own determines neither
the user's permissions nor the host firewall. The services on `edge` also have an outbound route in the base Compose;
the diagram therefore does not promise that all internet traffic passes through Bifrost.

## Ports and perimeter

![Published ports, the internal listener and the externally managed perimeter](diagrams/perimeter.svg)

[PNG](diagrams/perimeter.png) · [editable Mermaid source](diagrams/perimeter.mmd)

The solid paths are defined in the stack. The dashed paths require the optional Cloudflare profile
and an external hostname/Access policy. VPN, firewall, TLS and Access are outside Compose;
the diagram does not consider their presence verified.

| Entry | Routing in the base Compose |
|---|---|
| Host `80` → nginx `80` | `/search/`, `/api/`, `/chat/`, `/qa/`, `/qa-api/v1/` and the local nginx health. |
| Host `8443` → nginx `8443` | `/pgadmin/`; the port does not automatically mean TLS. |
| Host `8444` → nginx `8444` | Langflow UI on `/`; the port does not automatically mean TLS. |
| Default `127.0.0.1:8099` → Bifrost `8080` | Gateway administration and inference; the bind and port are determined by `BIFROST_BIND` / `BIFROST_PORT`. Inference has no authentication (internal callers); the UI/API need the admin login from `BIFROST_ADMIN_USERNAME` / `BIFROST_ADMIN_PASSWORD`. |
| Internal nginx `81` | Allowlist `/search/`; not published on the host. Cloudflared reaches it over `tunnel`. |

Nginx on port 81 does not add a general login; the path is restricted by the allowlist.
`/api/`, the chat and the QA portal on port 80 rely on a restricted external perimeter.
Langflow and pgAdmin have their own login. See the [nginx configuration](../../nginx/conf/nginx.conf)
and the [shared search include](../../nginx/conf/search_api.conf).

The local `docker-compose.override.yml` additionally publishes the backends on the loopback:
API 8080, Langflow 7860, PostgreSQL 5432, pgAdmin 5050, search 8090 and web chat 8081.
The diagrams show the base Compose; when reading a specific deployment, the automatic dev override
must be taken into account.

### Identified discrepancy in older sources

The Compose header and the dev override comment claim that only three nginx ports are on the host.
The actual Bifrost section additionally publishes the default loopback 8099. An older nginx comment lists
cloudflared on `edge`, but the current Compose uses `app`, `tunnel` and `tunnel_egress`.
This is comment drift; the diagrams are based on the configuration and the discrepancy is stated explicitly here.
The local topology image from 2026-08-27 is not part of this Git checkout and is not an authority
for today's networks and the Bifrost port. This task does not change the runtime configuration.

## How a QA/RAG query is handled

![The executor's sequential loop, a direct QA answer and deferred suggestions](diagrams/query-flow.svg)

[PNG](diagrams/query-flow.png) · [editable Mermaid source](diagrams/query-flow.mmd)

**The executor performs one step per iteration, in the order of the active configuration; the steps do not run in parallel.**
The live cascade is determined by `executor.py` and the organisation's `settings.config.pipeline`, not by the reference
`graph.py`. Without an organisation profile, the global profile is selected and then the source fallback.
Enabled, thresholds, `on_hit` and conditions change the specific path. The document steps and the QA vector
may interleave in the pipeline; the diagram does not introduce any universal order of all retrievers.

1. The API validates the entity, the caller, the domains and the role before retrieval. An idempotent replay returns
   the stored answer. `resolve_context` can rewrite a follow-up query or resolve a reaction;
   `prepare` builds the token forms.
2. The executor selects the next enabled step whose conditions are met. Route handles the guard and the pack scope.
   Content queries use `scope.py` for entity, visibility, role and domains;
   the document paths add active/validity filters.
3. A confident QA hit (`qa_exact`, lexical A or a successful `qa_vector`) returns the authored answer.
   No answer is generated from documents for it; the vector may need an embedding for that.
   The cascade ends at `resolved` unless `on_hit` is `continue`; the default value is `stop`.
4. Lexical B and typo suggestions are stored in `deferred_suggest`. They do not resolve the query terminally:
   the executor continues with the next step so that documents can supply a better answer.
   The weaker fallback offers are not the same as B.
5. The gatherers only add candidates to the pool. `answer` assembles the context, the order and any
   rerank; with sufficient evidence it generates an answer with citations. It may add related questions,
   including the deferred suggestions.
6. Without an answer, the deferred `suggest` is returned, otherwise `no_answer`. If the answer model fails,
   the deferred `suggest` again takes precedence, otherwise it is `llm_unavailable`.
   An unresolved, exhausted pipeline is closed by the executor's safety fallback. If the state is
   already resolved (for example after the last step with on_hit continue), the executor returns it unchanged.

The diagram shows the synchronous `/v1/ask`. The stream variant uses the same retrieval state; when
`answer` is reached, the executor may stop before this step and the server streams the generation.
An earlier QA/guard result is returned directly.

A normally processed query is written to the query log in the background; an idempotent result is stored
by the server when the answer is assembled. This is not a promise of a new retrieval or log for every replay or
rejection. An error in a registered pipeline step is caught and the executor continues;
the diagram does not claim that an arbitrary API or initialisation error cannot fail.

Details: [retrieval service](system.md), [settings](../SETTINGS_REFERENCE.md),
[routing](../ai/routing.md), [document search](../ai/retrieval.md) and
[QA rules and the offline audit](../integrations/offline-qa-rules.md). The older
[QA fast-path](../ai/qa-fastpath.md) already warns about drift; we do not adopt its description of B as an immediate stop here.

## State for backup

| Base Compose storage | Content |
|---|---|
| `postgres_data` | EDU-AI data, settings, registries and the separate Langflow DB. |
| `langflow_data/` | Langflow configuration and file state; preserving the key is important for restore. |
| `bifrost_data/` | Provider configuration and keys in the config DB, the operational log DB. |
| `pgadmin_data` + `pgadmin_exports/` | pgAdmin accounts/settings and exports. |
| `uploads/` | Files uploaded to the gateway. |
| `sqllite_export/` | Langflow SQLite QA exports. |
| `qa_admin_state` | Temporary state of the QA portal's previews/imports/exports. |
| `reranker_models` | Model cache; not a source of school data. |
| Private `.env` and the installation config | Credentials, encryption keys and deployment parameters. |

The table is a map of state, not a new procedure for a supported production restore. The completeness and consistency
of a specific backup are proven by a separate backup/update/restore run;
the verification of a specific release is described in [RELEASING.md](../RELEASING.md).

## Editing the diagrams

The primary sources are `diagrams/*.mmd`; the SVG and PNG are generated outputs.
When changing them, regenerate both formats and check them visually. Renderer used:
Mermaid 11.12.1, Playwright and Chrome; no production dependency was added.
Procedure and an optional helper: [rendering the diagrams](../tools/architecture-diagrams.md).
