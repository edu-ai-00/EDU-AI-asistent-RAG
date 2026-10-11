# Edu AI web chat

A simple VPN-only web interface on top of the existing `edu_ai_search` API.

## Running locally

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:SEARCH_BASE_URL = "http://127.0.0.1:8090"
# WHOSE documents this chat searches. There is no default in code: a wrong
# entity silently searches the wrong school. Either name one school here, or set
# CHAT_ENTITY_PICKER=true and leave this unset: the visitor then chooses the
# school in the chat (see "Choosing the school in the browser").
$env:CHAT_LEGAL_ENTITY = "SKOLA_NOVA_VES_ZS"
.\.venv\Scripts\uvicorn web_chat:app --host 127.0.0.1 --port 8081
```

Frontend for development:

```powershell
cd frontend
npm ci
npm run dev
```

Tests — `pytest` is deliberately **not** in `requirements.txt` (it is a runtime
file), so run them in a throwaway environment:

```bash
docker run --rm -e CHAT_LEGAL_ENTITY=SKOLA_TEST -v "$PWD:/w" -w /w \
  python:3.12-slim sh -c "pip install -q -r requirements.txt pytest && \
                          python -m pytest tests/test_web_chat.py \
                                           tests/test_school_selection.py -q"
```

The browser checks (`tests/test_school_selection_browser.py`) start their own
fake search and four chat processes and need only Playwright and a built
`static/` directory; see the docstring in that file. The older
`test_domain_picker_browser.py` needs the dev stack and a chat whose
`CHAT_LEGAL_ENTITY` is a valid default (it is preselected, so that test is
unaffected by the school menu).

`npm run build` type-checks and bundles, which is what CI needs; it does not
replace clicking through the running UI.

## What calls what

The production frontend is built by a multi-stage `Dockerfile` and FastAPI serves
it under `/chat/`. The browser calls three endpoints and nothing else:

| Browser → BFF | BFF → search |
|---|---|
| `GET /chat/api/config` | `GET /v1/entities` (only when the entity picker is on) |
| `GET /chat/api/domains` | `GET /v1/entities` (picker on, to validate the school), then `GET /v1/domains?legal_entity=…` |
| `POST /chat/api/message` | `GET /v1/entities` (picker on, to validate the school), then `POST /v1/ask` |

`POST /v1/ask` — **not** `POST /search`, which this file claimed until
2026-09-06 and which has not existed since the API moved under `/v1`
(2026-08-13). The BFF reaches search directly on the internal network
(`SEARCH_BASE_URL`), never through nginx.

## Choosing the school in the browser

With `CHAT_ENTITY_PICKER=true` the school is the **visitor's choice**, made in
the chat and remembered in that browser (`localStorage`, key
`edu_ai_chat_school`). The installer's usual setting is the picker on and
`CHAT_LEGAL_ENTITY` unset (a `CHANGE_ME_*` placeholder counts as unset).

- **Before a choice**: the menu shows "Vyberte školu" and the school names (from
  `GET /v1/entities` of search — the active organisations, no list of our own).
  Nothing is preselected, the composer is disabled, and **no** area-catalog and
  no message request leaves the browser.
- **After a choice**: the areas for that school load, the composer opens, and
  every question carries `legal_entity`. The choice is a client value only: it
  changes nothing for other visitors, writes no `.env` and no database, and
  there is no endpoint to "set the school".
- **Reload**: the remembered school is checked against a fresh list. If it was
  deactivated or removed it is forgotten, the user is told, and must choose again.
- **Changing school** resets the conversation and the areas; an in-flight
  question is cancelled and a late answer is dropped.
- **An explicit valid `CHAT_LEGAL_ENTITY` with the picker on** keeps its old
  meaning: it is preselected when nothing valid is remembered, and the visitor
  may still choose another school.
- **Picker off** is unchanged: the fixed school applies and a `legal_entity`
  sent by the client is ignored. With neither a picker nor a fixed school the
  chat reports itself **not configured** and sends nothing.

**Server rule.** With the picker on, the BFF checks the entity of every message
and catalog request against search's *current* list of active schools. No school,
a placeholder, an unknown or an inactive one is refused **before** any search
call — never replaced by the first school or by the deployment default. Search
keeps its own validation (400 on an unknown entity); the BFF's check is
additional, not a replacement, and a school choice is not authentication.

### `GET /chat/api/config` — readiness

```json
{"entity_picker": true, "setup": "awaiting_selection", "default": "",
 "entities": [{"code": "SKOLA_A", "name": "Základní škola A"}]}
```

`setup` is derived on every call (no stored flag). These are HTTP-200 configuration
states, distinct from the BFF error envelopes documented in
[contracts/errors.yaml](../contracts/errors.yaml). Malformed upstream registry data is
`entities_unavailable`, never an empty active-school list. If a reload replaces a
remembered school with a different valid deployment default, the conversation is
reset just as it is for a manual school change. Questions also stay blocked until
the loaded area catalog belongs to the currently selected school. Expected scope
refusals and BFF HTTP errors return assistant-ui's explicit incomplete/error status:
the same inline error remains visible, while the composer send promise does not
reject globally. Unexpected parsing or programming errors are not suppressed.

Readiness states:

| `setup` | Meaning | Browser |
|---|---|---|
| `ready` | picker off with a fixed school, or picker on with an **active** default | usable |
| `awaiting_selection` | picker on, no usable default | menu, composer disabled |
| `entities_unavailable` | picker on, search/database could not list schools | error + retry |
| `no_entities` | picker on, no active school exists | error + retry |
| `unconfigured` | picker off and no fixed school | not configured, no send |

With the picker off `entities` is always `[]` and nothing is asked of search.
`default` is returned only when it names an active school.

### Errors added by the school choice

| Situation | Client gets |
|---|---|
| picker on, no school in the request | `422 school_required` (+ `fields`) |
| placeholder / unknown / inactive school | `422 invalid_school` (+ `fields`) |
| the list of schools cannot be read | `503 entities_unavailable`, `retryable: true` |
| picker off, no fixed school | `500 chat_misconfigured` (unchanged) |

### Manual browser test (about two minutes)

1. Open `/chat/` in a fresh private window. The **Škola** menu shows
   "Vyberte školu"; the question box is greyed out ("Nejdřív vyberte školu…");
   **Oblast** says "Nejdřív vyberte školu".
2. Pick a school. The box opens, **Oblast** loads, ask something — the answer is
   from that school.
3. Reload. The same school is still selected.
4. Pick a different school: the conversation clears and **Oblast** resets.
5. In DevTools → Application → Local Storage set `edu_ai_chat_school` to
   `NOPE`, reload: no school selected, a notice says the earlier one is no
   longer available, and the stored value is gone.

## Thematic areas (the "Oblast" picker)

The chat can restrict a question to areas of the school's material. The
contract is `spec.search-domain-scope`; what matters here is the four jobs this
service does and the lifecycle the browser has to respect.

**The BFF.** `GET /chat/api/domains` serves the picker's catalog for the
**effective** entity, and returns that entity alongside the codes. That is not
decoration: with `CHAT_ENTITY_PICKER` off, a claimed `?legal_entity=` is
**ignored** — exactly as it is for a message — and the response says which
school the codes really belong to, so the browser can key its stored selection
to it. A catalog fetched for one school and used to search in another produces
codes search then rejects with a 422.

The BFF validates only **shape** (a list of short non-empty strings) and
forwards the codes verbatim. Which codes exist is per-school data and search
owns it; a membership list here would be a second copy to keep in step with a
database, and it would reject a code a school added five minutes ago. It does
reject the shapes search certainly rejects — `[]`, an empty element, an
over-long list — so a body that cannot succeed is not forwarded.

Errors are **fixable or not, and say which**:

| Upstream | Client gets | Why |
|---|---|---|
| 422 (bad/retired area code) | `422 invalid_scope` with `fields` | the user can correct it in one click; it used to be flattened into a 500 saying "chyba na naší straně, opakování nepomůže" |
| 503 (catalog unreadable) | `503 scope_unavailable`, `retryable: true` | the narrower search could not be applied — and the BFF does **not** retry without the filter, because a wider answer is a different answer |
| 503 on `GET /chat/api/domains` (catalog could not be read) | `503 domains_unavailable`, `retryable: true` | the picker has no list to show; the code is not in `contracts/errors.yaml` (report only) |
| the BFF's own validation | `422 invalid_request` with `fields` | same status and shape as search, so a client switches on one thing |

`ChatResponse.effective_scope` carries what search actually ran with (five
fields; `identified` and the internal taxonomy ids stay server-side). The UI
compares its `domains` with what it selected — that is how a client notices its
stored choice no longer matches.

**The browser** (`frontend/src/DomainPicker.tsx`). Four failure modes, each
guarded on purpose:

1. **A late catalog never overwrites a newer one.** Switching school twice can
   land the first response last. Guarded by a generation counter, not by
   `AbortController` alone — an aborted fetch can still resolve.
2. **A stored choice is verified, never trusted, and reporting is not enough.**
   Codes are per school and a curator can retire one, so the remembered set is
   intersected with the arriving catalog and what fell away is **reported**.
   Reporting alone left a hole: with *every* stored code retired the selection
   falls back to `null` — which means **all areas** — and the next question
   searched the whole corpus while the user believed a filter was on. So a
   retired code also **blocks sending** (`needsChoice`) until the user chooses
   again or confirms; the notice carries that confirmation.

   Storage is keyed by the entity the BFF says it resolved, and is rewritten
   **only by an explicit choice** — never by the catalog effect. That effect
   runs twice in the normal configuration (once before `/chat/api/config`
   resolves the entity, once after), and a version that rewrote storage on the
   first run consumed the retired codes so the second found nothing: the warning
   flashed for one frame and the block never engaged at all. Caught by the
   browser test, which is the only place it was visible.
3. **An unreadable catalog is an error with a retry**, never an empty list. An
   empty list would render as "this school has no areas" and the filter would
   quietly become "everything".
4. **Nothing is sent while the scope is unsettled** — three cases, not one:
   the catalog has not loaded; it could not be loaded at all; or a stored code
   was retired and no new choice has been made. The composer's error names
   which, so the user is told the thing to fix rather than always "still
   loading".

"Všechny oblasti" is a real, always-visible choice and the default; picking it
means `domains: null`, and deselecting the last area returns to it — a picker
must not be able to build the empty array that is a 422.

**Changing school or area starts a new conversation.** `ScopeResetter` clears
the transcript and the in-flight request, and a new `conversation_id` is
generated. The server also filters history by area, because `conversation_id`
comes from the client and cannot be relied on — but the UI has its own reason:
the chips on screen belong to the old scope, and leaving them there offers
buttons that would search an area the user has just left. Changing the dropdown
sends **no** question; it only decides what the next one belongs to.

On an error the client never retries with the filter removed. It shows the
message; widening the search is the user's decision, not the client's.

## Prototype limitations

- no login (access is restricted by VPN),
- history only in browser memory,
- no response streaming (the BFF calls `/v1/ask`, not `/v1/ask/stream`),
- no persistent thread storage,
- no Assistant Cloud.
