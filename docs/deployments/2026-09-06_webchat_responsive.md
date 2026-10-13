# Webchat: responsive CSS — 6 Sep 2026

> **Historical record (6 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

At the user's explicit request only the change to
`web_chat/frontend/src/styles.css` was deployed via SCP to `/opt/edu_ai`, followed by
`docker compose build edu_ai_web_chat` and
`docker compose up -d --no-deps edu_ai_web_chat` via the SSH alias `edu-ai`.
The production checkout stayed at `6997625`; the CSS is an uncommitted change.
The original SHA-256 of the CSS matched the local HEAD:
`8c259935a304bf73e53f6b06eb13a5487d9a5e8eca9b15fe2fa6747e137a8b1f`.

The mobile header uses two columns, the organization and the domain take the full width.
Long content wraps, the textarea may shrink and the height uses the dynamic
viewport. Neither the backend nor the configuration changed.

Verification:

- The TypeScript and Vite build passed both locally and on production.
- Isolated Chromium test `web_chat/tests/test_responsive_browser.py`: all
  10 combinations of the widths 320/390/640/768/1280 and the organization on/off passed.
  It tests long messages, sources, sending and the dimensions of the menu and the composer.
- The mobile screenshot was checked visually.
- The production container is healthy; `/chat/health`, `/chat/` and
  `/chat/assets/index-CFTFGc5X.css` return 200 inside the container.
  The public path through Cloudflare was not verified interactively.
- `python docs/tools/docs_check.py` passed (make is not installed).
- Parity: 30/30; run as a file with PYTHONPATH=/app, because the variant
  via stdin wrongly looks for stoplist.json in /app.
- The local `python -X utf8 tests/eval_rag.py --repeat 3` finished, but does not confirm
  a successful RAG regression: 39 no_answer, 3 answer, 1 ERROR out of 43 cases.
  Snapshot `tests/eval_out/20260906-235848.json`. The CSS does not affect the retrieval result.

New image ID:
`sha256:ea65df359ff5f0d65f3033f6ef4d0ae47d50e2528450b2ecce20274391d7b757`.
The previous image was kept as `edu_ai_web_chat:prev-responsive-20260906`;
the original CSS as `/tmp/edu-ai-styles-before-responsive-20260906.css` on the server.

Rollback on the server from `/opt/edu_ai`:

```bash
cp /tmp/edu-ai-styles-before-responsive-20260906.css web_chat/frontend/src/styles.css
docker tag edu_ai_web_chat:prev-responsive-20260906 edu_ai_web_chat:0.1
docker compose up -d --no-deps edu_ai_web_chat
```
