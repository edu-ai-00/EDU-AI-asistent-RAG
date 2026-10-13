# Stable selection of school and domain — 7 Sep 2026

> **Historical record (7 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

Follows on from the authorized deployment of the responsive layout. The user clarified that on a
computer the controls jump when selecting a school and a domain. An isolated Chromium test
reproduced a shift of the domain selection by 78 px at a viewport width of 700 px.
The cause is a flex layout with widths derived from the text of the current choice.

The CSS now uses a stable grid for the desktop header and its controls.
The domain button has a limited width and single-line text; the full name is in title.
The mobile grid is preserved. Scrolling and conversation reset did not change.

Verification before the deployment:

- The TypeScript/Vite build passed.
- `test_header_stability_browser.py`: 390, 700, 960 and 1280 px, all
  measured shifts 0 px when choosing one/several/all domains and changing the school,
  including waiting for the catalog. It also checks the position of the open menu.
- `test_responsive_browser.py`: 10/10 combinations passed.
- `python docs/tools/docs_check.py` and `git diff --check -- web_chat` passed.
- RAG did not change; for the limitations of its local regression see the previous record
  `2026-09-06_webchat_responsive.md`.

Deployment via SSH `edu-ai`: only `styles.css` and `DomainPicker.tsx` via SCP,
then `docker compose build edu_ai_web_chat` and
`docker compose up -d --no-deps edu_ai_web_chat` in `/opt/edu_ai`.
The source changes are uncommitted. Before the deployment the match of the original
DomainPicker.tsx with the local HEAD was verified; production contained only the previous CSS change.

After the deployment: the container is healthy; `/chat/health`, `/chat/`,
`/chat/assets/index-C6g3On8r.css` and `/chat/assets/index-Z8N3nBa0.js`
return HTTP 200 inside the container. The public path through Cloudflare was not
verified interactively. Production image manifest:
`sha256:aec0895d02762e22af827b529454bfe30033a80b56df6a9c7bbda391b2d951dc`.

Previous image: `edu_ai_web_chat:prev-stable-header-20260907`.
Backups on the server:
`/tmp/edu-ai-styles-before-stable-header-20260907.css` and
`/tmp/edu-ai-picker-before-stable-header-20260907.tsx`.

## Check of the deployed state — 13 Sep 2026

A read-only check via SSH `edu-ai` confirmed the production HEAD `9fc015c`
and a byte-for-byte match of both source files in `/opt/edu_ai` with the local checkout:

- `styles.css`: SHA-256 `20bdad97bb55864abca07eff59e083c0693b22250c7789dbf92ded96a3a12d2f`.
- `DomainPicker.tsx`: SHA-256 `726a880b2c2b52b56efc5df211676912864bfc92399e6674a86249e30e9cdea0`.

The container is `healthy`; its image ID matches the deployment from
7 Sep stated above and `/app/static/index.html` references the same CSS and JS assets.
The changes are thus deployed even though they were not committed on the source branch.
This check only verified the state; it changed and rebuilt nothing on production.

Rollback from `/opt/edu_ai`:

```bash
cp /tmp/edu-ai-styles-before-stable-header-20260907.css web_chat/frontend/src/styles.css
cp /tmp/edu-ai-picker-before-stable-header-20260907.tsx web_chat/frontend/src/DomainPicker.tsx
docker tag edu_ai_web_chat:prev-stable-header-20260907 edu_ai_web_chat:0.1
docker compose up -d --no-deps edu_ai_web_chat
```
