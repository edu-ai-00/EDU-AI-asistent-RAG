# Stabilní výběr školy a oblasti — 7. 9. 2026

Navazuje na autorizované nasazení responzivity. Uživatel upřesnil, že na
počítači při výběru školy a oblasti ovládání skáče. Izolovaný Chromium test
reprodukoval posun výběru oblasti o 78 px při šířce viewportu 700 px.
Příčinou je flex rozložení se šířkami odvozenými od textu aktuální volby.

CSS nyní používá stabilní grid pro desktopovou hlavičku a její ovládání.
Tlačítko oblasti má omezenou šířku a jednořádkový text; celý název je v title.
Mobilní grid zůstává zachován. Scrollování a reset konverzace se neměnily.

Ověření před nasazením:

- TypeScript/Vite build prošel.
- `test_header_stability_browser.py`: 390, 700, 960 a 1280 px, všechny
  naměřené posuny 0 px při volbě jedné/více/všech oblastí a změně školy,
  včetně čekání na katalog. Kontroluje i pozici otevřeného menu.
- `test_responsive_browser.py`: 10/10 kombinací prošlo.
- `python docs/tools/docs_check.py` a `git diff --check -- web_chat` prošly.
- RAG se neměnil; omezení jeho lokální regrese viz předchozí záznam
  `2026-09-06_webchat_responsive.md`.

Nasazení přes SSH `edu-ai`: pouze `styles.css` a `DomainPicker.tsx` přes SCP,
poté `docker compose build edu_ai_web_chat` a
`docker compose up -d --no-deps edu_ai_web_chat` v `/opt/edu_ai`.
Zdrojové změny jsou necommitované. Před nasazením ověřena shoda výchozího
DomainPicker.tsx s lokálním HEAD; produkce obsahovala pouze předchozí CSS změnu.

Po nasazení: kontejner healthy; `/chat/health`, `/chat/`,
`/chat/assets/index-C6g3On8r.css` a `/chat/assets/index-Z8N3nBa0.js`
vracejí uvnitř kontejneru HTTP 200. Veřejná cesta přes Cloudflare nebyla
interaktivně ověřena. Produkční image manifest:
`sha256:aec0895d02762e22af827b529454bfe30033a80b56df6a9c7bbda391b2d951dc`.

Předchozí image: `edu_ai_web_chat:prev-stable-header-20260907`.
Zálohy na serveru:
`/tmp/edu-ai-styles-before-stable-header-20260907.css` a
`/tmp/edu-ai-picker-before-stable-header-20260907.tsx`.

## Kontrola nasazeného stavu — 13. 9. 2026

Read-only kontrola přes SSH `edu-ai` potvrdila produkční HEAD `9fc015c`
a bajtovou shodu obou zdrojových souborů v `/opt/edu_ai` s místním checkoutem:

- `styles.css`: SHA-256 `20bdad97bb55864abca07eff59e083c0693b22250c7789dbf92ded96a3a12d2f`.
- `DomainPicker.tsx`: SHA-256 `726a880b2c2b52b56efc5df211676912864bfc92399e6674a86249e30e9cdea0`.

Kontejner je `healthy`; jeho image ID odpovídá výše uvedenému nasazení ze
7. září a `/app/static/index.html` odkazuje na stejné CSS a JS assety.
Úpravy jsou tedy nasazené, přestože nebyly commitnuté na zdrojové větvi.
Tato kontrola pouze ověřila stav; nic na produkci nezměnila ani nepřestavěla.

Návrat z `/opt/edu_ai`:

```bash
cp /tmp/edu-ai-styles-before-stable-header-20260907.css web_chat/frontend/src/styles.css
cp /tmp/edu-ai-picker-before-stable-header-20260907.tsx web_chat/frontend/src/DomainPicker.tsx
docker tag edu_ai_web_chat:prev-stable-header-20260907 edu_ai_web_chat:0.1
docker compose up -d --no-deps edu_ai_web_chat
```
