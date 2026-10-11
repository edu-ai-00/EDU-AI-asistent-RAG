# Webchat: responzivní CSS — 6. 9. 2026

Na výslovnou žádost uživatele nasazena pouze změna
`web_chat/frontend/src/styles.css` přes SCP do `/opt/edu_ai`, následovaná
`docker compose build edu_ai_web_chat` a
`docker compose up -d --no-deps edu_ai_web_chat` přes SSH alias `edu-ai`.
Produkční checkout zůstal na `6997625`; CSS je necommitovaná změna.
Výchozí SHA-256 CSS odpovídalo lokálnímu HEAD:
`8c259935a304bf73e53f6b06eb13a5487d9a5e8eca9b15fe2fa6747e137a8b1f`.

Mobilní hlavička používá dvě kolony, organizace a oblast celou šířku.
Dlouhý obsah se zalamuje, textarea se může zúžit a výška používá dynamický
viewport. Backend ani konfigurace se neměnily.

Ověření:

- TypeScript a Vite build prošly lokálně i na produkci.
- Izolovaný Chromium test `web_chat/tests/test_responsive_browser.py`: všech
  10 kombinací šířek 320/390/640/768/1280 a zapnuté/vypnuté organizace prošlo.
  Testuje dlouhé zprávy, zdroje, odeslání a rozměry nabídky i composeru.
- Mobilní screenshot vizuálně zkontrolován.
- Produkční kontejner healthy; `/chat/health`, `/chat/` a
  `/chat/assets/index-CFTFGc5X.css` vracejí 200 uvnitř kontejneru.
  Veřejná cesta přes Cloudflare nebyla interaktivně ověřena.
- `python docs/tools/docs_check.py` prošel (make není nainstalován).
- Parita: 30/30; spuštěno jako soubor s PYTHONPATH=/app, protože varianta
  přes stdin chybně hledá stoplist.json v /app.
- Lokální `python -X utf8 tests/eval_rag.py --repeat 3` doběhl, ale nepotvrzuje
  úspěšnou RAG regresi: 39 no_answer, 3 answer, 1 ERROR ze 43 případů.
  Snapshot `tests/eval_out/20260906-235848.json`. CSS výsledek retrievalu neovlivňuje.

Nový image ID:
`sha256:ea65df359ff5f0d65f3033f6ef4d0ae47d50e2528450b2ecce20274391d7b757`.
Předchozí image uchován jako `edu_ai_web_chat:prev-responsive-20260906`;
původní CSS jako `/tmp/edu-ai-styles-before-responsive-20260906.css` na serveru.

Návrat na serveru z `/opt/edu_ai`:

```bash
cp /tmp/edu-ai-styles-before-responsive-20260906.css web_chat/frontend/src/styles.css
docker tag edu_ai_web_chat:prev-responsive-20260906 edu_ai_web_chat:0.1
docker compose up -d --no-deps edu_ai_web_chat
```
