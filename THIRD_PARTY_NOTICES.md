# Third-Party Notices

**Scope and dates.** This file combines the source-declared components below
with historical package inventories recorded on 2026-07-30. Those inventories
are retained as snapshots, not a claim about every current image. Requirements
and image layers are not all locked; record the actual image digests and regenerate
package inventories with `scripts/dump_pkgs.py` for a specific built release.
Packages and components retain their upstream licences and supplied notices.
This list is not a complete current runtime licence inventory.

**Historical copyleft entries.** The recorded Langflow image included `grandalf`
with GPLv2/EPLv1 metadata and psycopg2 with LGPL exceptions. Consult the exact
upstream licence files supplied with the redistributed versions; the historical
inventory is not a blanket conclusion about redistribution obligations.

## Infrastructure components

| Component | Version | Licence | Author / maintainer |
|---|---|---|---|
| Langflow (base image of `edu_ai`) | 1.10.1 | MIT | Langflow (Carlos Coelho et al.) |
| PostgreSQL | 16.3 | PostgreSQL License | PostgreSQL Global Development Group |
| pgvector (extension; base image `pgvector/pgvector:0.7.4-pg16`) | 0.7.4 | PostgreSQL License | Andrew Kane |
| pg_search (installed by Dockerfile.postgres) | 0.25.1 | [AGPL-3.0](https://github.com/paradedb/paradedb/blob/v0.25.1/LICENSE) | ParadeDB contributors |
| Bifrost gateway | image v1.6.7 | [Apache-2.0](https://github.com/maximhq/bifrost/blob/4ba2ca5dfb6c2974b629211d8c5c021cd4273f30/LICENSE), upstream transports/v1.6.7 source | Maxim AI / Bifrost contributors |
| cloudflared (optional Compose profile) | unpinned image tag latest | [Apache-2.0](https://github.com/cloudflare/cloudflared/blob/master/LICENSE), upstream checked 2026-10-12 | Cloudflare contributors |
| pg_trgm, unaccent (extensions) | bundled with 16.3 | PostgreSQL License | PostgreSQL Global Development Group |
| pgAdmin 4 (image `dpage/pgadmin4`) | 9.11.0 | PostgreSQL License | pgAdmin Development Team (Dave Page et al.) |
| nginx | 1.30.5 | BSD-2-Clause | Igor Sysoev; F5/NGINX team |
| CPython runtime / base images | 3.11 / 3.12 slim | PSF-2.0 | Python Software Foundation |
| Node.js (frontend build stage only) | 24.18 alpine | MIT | OpenJS Foundation |
| Debian (image OS layer) | 12 | various | Debian contributors |

## `edu_ai — Langflow (ingestion engine)`

Base image `langflowai/langflow:1.10.1` (MIT). Packages WE add on top (`requirements-langflow.txt`) plus our own `eduai_normalize`:

| Package | Version | Licence | Author / maintainer |
|---|---|---|---|
| anytree | 2.13.0 | Apache-2.0 | c0fec0de |
| chardet | 7.4.3 | 0BSD | Ian Cordasco |
| databricks-sdk | 0.122.0 | Apache-2.0 | Databricks, Inc. |
| eduai_normalize | 1.5.0 | Apache-2.0 | EDU-AI, o.p.s. (this project — shared normalization library) |
| jsonschema | 4.26.0 | MIT | Julian Berman |
| langdetect | 1.0.9 | MIT | Michal Mimino Danilak |
| markitdown | 0.1.6 | MIT | Adam Fourney |
| omegaconf | 2.3.1 | BSD License | Omry Yadan |
| pdfplumber | 0.11.9 | MIT License | Jeremy Singer-Vine |
| psycopg2-binary | 2.9.12 | LGPL with exceptions | Federico Di Gregorio |
| simplemma | 1.2.0 | MIT License | Adrien Barbaresi, Juanjo Diaz |
| snowballstemmer | 3.1.1 | BSD-3-Clause | Snowball Developers |
| snowflake-connector-python | 4.7.1 | Apache-2.0 | Snowflake, Inc |
| snowflake-sqlalchemy | 1.11.0 | Apache-2.0 | Snowflake Inc. |
| sqlglot | 30.14.0 | MIT | Toby Mao |
| trafilatura | 2.1.0 | Apache-2.0 | Adrien Barbaresi |
| validators | 0.35.0 | MIT | Konsta Vesterinen |

## `edu_ai_search (retrieval microservice)`

Base image `python:3.12-slim` (PSF-2.0). The table below is the **historical installed-image inventory from 2026-07-30**, not the current runtime inventory — the direct dependencies declared in `search/requirements.txt` are in
**bold**, the rest are transitive. Licences and authors were read from the installed
package metadata (`License-Expression`, then trove classifiers, then the bundled
licence file); rows the metadata left empty were filled in by hand from the projects'
own repositories.

| Package | Version | Licence | Author / maintainer |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | Sebastián Ramírez |
| annotated-types | 0.8.0 | MIT | Adrian Garcia Badaracco, Samuel Colvin, Zac Hatfield-Dodds |
| anyio | 4.14.2 | MIT | Alex Grönholm |
| certifi | 2026.7.22 | MPL-2.0 | Kenneth Reitz |
| charset-normalizer | 3.5.1 | MIT | Ahmed R. TAHRI |
| click | 8.4.2 | BSD-3-Clause | Pallets |
| cuda-bindings | 13.3.1 | LicenseRef-NVIDIA-SOFTWARE-LICENSE | NVIDIA Corporation |
| cuda-pathfinder | 1.7.0 | Apache-2.0 | NVIDIA Corporation |
| cuda-toolkit | 13.0.3.0 | NVIDIA Software Licence (proprietary EULA) | NVIDIA Corporation |
| distro | 1.9.0 | Apache-2.0 | Nir Cohen |
| **eduai_normalize** | 1.5.0 | Apache-2.0 | EDU-AI, o.p.s. (this project — shared normalization library) |
| **fastapi** | 0.141.1 | MIT | Sebastián Ramírez |
| filelock | 3.32.4 | MIT | Bernát Gábor |
| fsspec | 2026.7.0 | BSD-3-Clause | Martin Durant |
| h11 | 0.16.0 | MIT | Nathaniel J. Smith |
| hf-xet | 1.6.0 | Apache-2.0 | Rajat Arya, Jared Sulzdorf, Di Xiao, Assaf Vayner, Hoyt Koepke |
| httpcore | 1.0.9 | BSD-3-Clause | Tom Christie |
| httpcore2 | 2.12.0 | BSD-3-Clause | Tom Christie |
| httptools | 0.8.0 | MIT | Yury Selivanov |
| httpx | 0.28.1 | BSD | Tom Christie |
| httpx2 | 2.12.0 | BSD-3-Clause | Tom Christie |
| huggingface_hub | 1.28.0 | Apache-2.0 | Hugging Face, Inc. |
| idna | 3.19 | BSD-3-Clause | Kim Davies |
| Jinja2 | 3.1.6 | BSD | Pallets |
| jiter | 0.16.0 | MIT | Samuel Colvin |
| joblib | 1.5.3 | BSD-3-Clause | Gael Varoquaux |
| jsonpatch | 1.33 | BSD | Stefan Kögl |
| jsonpointer | 3.1.1 | BSD | Stefan Kögl |
| **langchain-core** | 1.6.0 | MIT | LangChain, Inc. |
| **langchain-openai** | 1.6.0 | MIT | LangChain, Inc. |
| langchain-protocol | 0.0.18 | MIT | LangChain, Inc. |
| langdetect | 1.0.9 | Apache-2.0 | Michal Mimino Danilak |
| **langgraph** | 1.2.11 | MIT | LangChain, Inc. |
| langgraph-checkpoint | 4.2.0 | MIT | LangChain, Inc. |
| langgraph-prebuilt | 1.1.0 | MIT | LangChain, Inc. |
| langgraph-sdk | 0.4.3 | MIT | LangChain, Inc. |
| langsmith | 0.11.1 | MIT | LangChain |
| markdown-it-py | 4.2.0 | MIT | Chris Sewell |
| MarkupSafe | 3.0.3 | BSD-3-Clause | Pallets |
| mdurl | 0.1.2 | MIT | Taneli Hukkinen |
| mpmath | 1.3.0 | BSD | Fredrik Johansson |
| narwhals | 2.25.0 | MIT | Marco Gorelli |
| networkx | 3.6.1 | BSD-3-Clause | Aric Hagberg |
| **numpy** | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Travis E. Oliphant et al. |
| nvidia-cublas | 13.1.1.3 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-cuda-cupti | 13.0.85 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cuda-nvrtc | 13.0.88 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cuda-runtime | 13.0.96 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-cudnn-cu13 | 9.20.0.48 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-cufft | 12.0.0.61 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cufile | 1.15.1.6 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-curand | 10.4.0.35 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cusolver | 12.0.4.66 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cusparse | 12.6.3.3 | Other/Proprietary License | Nvidia CUDA Installer Team |
| nvidia-cusparselt-cu13 | 0.8.1 | NVIDIA Proprietary Software | NVIDIA Corporation |
| nvidia-nccl-cu13 | 2.29.7 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-nvjitlink | 13.3.33 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-nvshmem-cu13 | 3.4.5 | LicenseRef-NVIDIA-Proprietary | Nvidia CUDA Installer Team |
| nvidia-nvtx | 13.0.85 | Other/Proprietary License | Nvidia CUDA Installer Team |
| openai | 3.3.1 | Apache-2.0 | OpenAI |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | ijl |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT | Aviram Hassan, Emanuele Giaquinta |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Donald Stufft |
| **pandas** | 3.0.5 | BSD | The Pandas Development Team |
| pip | 25.0.1 | MIT | The pip developers |
| **psycopg2-binary** | 2.9.12 | GNU Library or Lesser General Public License (LGPL) | Federico Di Gregorio |
| **pydantic** | 2.13.4 | MIT | Samuel Colvin, Eric Jolibois, Hasan Ramezani, Adrian Garcia Badaracco, |
| pydantic_core | 2.46.4 | MIT | Samuel Colvin, Adrian Garcia Badaracco, David Montague, David Hewitt,  |
| Pygments | 2.21.0 | BSD-2-Clause | Georg Brandl |
| python-dateutil | 2.9.0.post0 | Apache-2.0 | Gustavo Niemeyer |
| python-dotenv | 1.2.3 | BSD-3-Clause | Saurabh Kumar |
| PyYAML | 6.0.3 | MIT | Kirill Simonov |
| regex | 2026.7.19 | Apache-2.0 AND CNRI-Python | Matthew Barnett |
| requests | 2.34.2 | Apache-2.0 | Kenneth Reitz |
| requests-toolbelt | 1.0.0 | Apache-2.0 | Ian Cordasco, Cory Benfield |
| rich | 15.0.0 | MIT | Will McGugan |
| safetensors | 0.8.0 | Apache-2.0 | Nicolas Patry, Luc Georges, Daniël De Kok |
| scikit-learn | 1.9.0 | BSD-3-Clause | scikit-learn developers |
| scipy | 1.18.1 | BSD | SciPy Developers |
| **sentence-transformers** | 6.0.0 | Apache-2.0 | Nils Reimers, Tom Aarsen |
| setuptools | 84.0.0 | MIT | Python Packaging Authority |
| shellingham | 1.5.4 | ISC | Tzu-ping Chung |
| **simplemma** | 2.0.0 | MIT | Adrien Barbaresi, Juanjo Diaz |
| six | 1.17.0 | MIT | Benjamin Peterson |
| sniffio | 1.3.1 | Apache-2.0 | Nathaniel J. Smith |
| snowballstemmer | 3.1.1 | BSD-3-Clause | Snowball Developers |
| starlette | 1.6.0 | BSD-3-Clause | Tom Christie |
| sympy | 1.14.0 | BSD | SymPy development team |
| tenacity | 9.1.4 | Apache-2.0 | Julien Danjou |
| threadpoolctl | 3.6.0 | BSD | Thomas Moreau |
| tiktoken | 0.14.0 | MIT | Shantanu Jain |
| tokenizers | 0.22.2 | Apache-2.0 | Nicolas Patry, Anthony Moi |
| torch | 2.13.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | PyTorch Team |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | tqdm developers |
| transformers | 5.15.1 | Apache 2.0 License | The Hugging Face team (past and future) with the help of all our contr |
| triton | 3.7.1 | MIT | Philippe Tillet |
| truststore | 0.10.4 | MIT | Seth Michael Larson, David Glick |
| typer | 0.27.1 | MIT | Sebastián Ramírez |
| typing-inspection | 0.4.4 | MIT | Victorien Plot |
| typing_extensions | 4.16.0 | PSF-2.0 | Guido van Rossum, Jukka Lehtosalo, Łukasz Langa, Michael Lee |
| urllib3 | 2.7.0 | MIT | Andrey Petrov |
| uuid_utils | 0.17.0 | BSD-3-Clause | Amin Alaee |
| **uvicorn** | 0.52.4 | BSD-3-Clause | Tom Christie |
| uvloop | 0.22.1 | MIT | Yury Selivanov |
| watchfiles | 1.2.0 | MIT | Samuel Colvin |
| websockets | 16.1.1 | BSD-3-Clause | Aymeric Augustin |
| xxhash | 4.0.1 | BSD-2-Clause | Yue Du |
| zstandard | 0.25.0 | BSD-3-Clause | Gregory Szorc |

## `edu_ai_api (ingestion gateway)`

Base image `python:3.11-slim` (PSF-2.0). Installed by `api/Dockerfile`:

| Package | Version | Licence | Author / maintainer |
|---|---|---|---|
| beautifulsoup4 | 4.12.3 | MIT License | Leonard Richardson |
| fastapi | 0.137.2 | MIT | Sebastián Ramírez |
| lxml | 6.1.1 | BSD-3-Clause | lxml dev team |
| python-multipart | 0.0.32 | Apache-2.0 | Andrew Dunham |
| requests | 2.34.2 | Apache-2.0 | Kenneth Reitz |
| uvicorn | 0.49.0 | BSD-3-Clause | Tom Christie |
| validators | 0.35.0 | MIT | Konsta Vesterinen |

## `edu_ai_web_chat (chat UI backend)`

Base image `python:3.12-slim` (PSF-2.0). Declared in `web_chat/requirements.txt`:

| Package | Version | Licence | Author / maintainer |
|---|---|---|---|
| fastapi | 0.137.2 | MIT | Sebastián Ramírez |
| httpx | 0.28.1 | BSD-3-Clause | Tom Christie |
| pydantic | 2.13.4 | MIT | Samuel Colvin |
| uvicorn | 0.49.0 | BSD-3-Clause | Tom Christie |

## Web-chat frontend (JavaScript, bundled at build time)

Declared in `web_chat/frontend/package.json`; the runtime container ships only the compiled bundle.

| Package | Version | Licence | Author / maintainer |
|---|---|---|---|
| @assistant-ui/react | 0.14.27 | MIT | assistant-ui (Simon Farshid et al.) |
| react / react-dom | 19.2.7 | MIT | Meta Platforms, Inc. |
| react-markdown | 10.1.0 | MIT | Espen Hovlandsdal; unified collective |
| rehype-sanitize | 6.0.0 | MIT | Titus Wormer; unified collective |
| remark-gfm | 4.0.1 | MIT | Titus Wormer; unified collective |
| vite + @vitejs/plugin-react (build only) | 6.x | MIT | Evan You; Vite team |
| typescript (build only) | 7.0.2 | Apache-2.0 | Microsoft Corporation |

## External services (not distributed, contractual)

| Service | Use |
|---|---|
| OpenAI API | embeddings (`text-embedding-3-small`) and LLM calls (chunking, classification, answer generation) — governed by OpenAI's terms, no OpenAI code is distributed |


## QA portal

`qa_portal/` is EDU-AI project code under this repository's LICENSE and NOTICE,
not a separate third-party product. Its backend declares FastAPI, uvicorn,
pydantic, python-multipart and requests; its frontend declares React, React DOM,
react-markdown, rehype-sanitize and remark-gfm. See the source requirements and
package/lock files for actual versions; shared components are attributed above.
Its build/test dependencies and their transitive licence files need to be retained
with any specific redistributed artifact just like the web chat's dependencies.

The pg_search, Bifrost and cloudflared source licence references above were read
from their official upstream LICENSE files on 2026-10-12. This records source
licences, not an audit of every package bundled in those container images.
