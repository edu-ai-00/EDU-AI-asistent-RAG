# B06: bootstrap hardening (Langflow API key, `.env`, secrets in the output)

The public copy omits internal acceptance reports and links to them; this proposal is not evidence of the current deployment.

**Status (2026-10-06): part of the B06-1 + bootstrap candidate, published on `origin/clean_init` (code and tests in commit `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, documentation in the following commit; its SHA cannot be stated inside itself, see `git log` of these files), after three independent reviews, the final Codex review (five findings closed) and lab verification of the API keys against Langflow 1.10.1. It is not an approved installation, a supported school update or a basis for deployment.**
The offline tests are mocks. The lab verification (section 5 of the review log) ran in a separate container without a network, with its own temporary database and a synthetic configuration, and covered only the API keys; the real `.env` was not read. The whole bootstrap against the stack, PostgreSQL and B06-2 are a further stage. On 2026-10-06 the owner explicitly kept the rule and requires no further confirmation: HTTP 403 is an unverifiable state (code 3, no automatic replacement), a manual `--force` may create a new key, and the original key is never deleted automatically. The older wording below ("after three reviews", "not verified") describes the state on the day it was written and is history.
Date: 2026-10-06. The original standalone candidate was created on top of the technical snapshot `3b62a25daff65af2d9d59986d6f045ed8859b39c` (commit `fec7d8150c1b20f27331a54a7f3ce610b1ddf7d6`);
this text describes the state after integrating the reviews on top of `c93527e0d0ab833d3e3f81cac1f2cf043b31a305`. It builds on the items "Langflow key" and "secrets summary" from the
[B06 proposal](b06-clean-install-proposal.md) (section 2 rows 6 and 11, section 4, section 10) and from the review of the proposal; reviews and the closing of findings: the log.

It addresses two items (the Langflow key and secrets in the output) and the resulting `.env` write. Everything else is in section 6 as the status of findings N1–N9.

## 1. What changed

| File | Change |
|---|---|
| [`scripts/create_api_key.py`](../../scripts/create_api_key.py) | three answers instead of two; an unverifiable state changes nothing; safe replacement order; a new key that was not stored is deleted only when reading `.env` proves it was not written; the old key is never deleted; 403 is an unverifiable state; a pasted key is verified; `env_path` without a default; messages without the response body; `getpass`; code 3 and the `--help` epilog |
| [`scripts/bootstrap.py`](../../scripts/bootstrap.py) | handling of the new state; `redact()` on everything printed (never fails open); stricter `relay()`; `.env` is created privately and written atomically; final summary; corrected `/health` messages; `--help` epilog |
| [`scripts/envfile.py`](../../scripts/envfile.py) | `write_atomic`, `create_private`, `set_env` rewrites all occurrences of a key; behaviour and parser otherwise unchanged |
| [`scripts/http_safe.py`](../../scripts/http_safe.py) | new: an opener that does not follow redirects |
| [`scripts/import_flows.py`](../../scripts/import_flows.py), [`scripts/smoke.py`](../../scripts/smoke.py) | authenticated calls through `open_url` (two lines in each) |
| [`ONBOARDING.md`](../../ONBOARDING.md) | §1 and §5: key behaviour, secrets summary, code 3 |
| `tests/test_bootstrap_key_safety.py` (57), `test_bootstrap_env_write.py` (24), `test_bootstrap_key_rotation.py` (30), `test_bootstrap_key_lifecycle.py` (39, new), `test_bootstrap_output.py` (13) | 163 offline tests |

Unchanged: Compose, nginx, R2, applications, databases, schema, shared status documents (outside the B06 passages). Changes to `install.py` are described in the B06 proposal, not here.

## 2. Langflow API key: three states and a safe replacement

`check_key()` calls `GET /api/v1/flows/?page=1&size=1` with the key in the `x-api-key` header and **fails safe**: only two answers are conclusive (`200` with JSON and `401`), everything else is "unknown".

| State of the key in `.env` | Evidence | What the script does |
|---|---|---|
| missing (no line, empty value, `CHANGE_ME…`) | no request | **first creation** (below); no old key is deleted, because none can be tied to a value |
| valid | `200` and the body is a JSON object or array | nothing changes |
| rejected | `401` | **replacement** without `--force` (below) |
| unverifiable | **`403`** (`HTTP 403, access denied: not proof that the key is invalid`), timeout (including on read), connection refused or interrupted, DNS, TLS, any other HTTP status (**including 3xx: a redirect is not followed**), `200` with a body that is not a JSON object/array, any other 2xx, corrupted gzip, **a key that cannot be sent as a header (non-ASCII character, control character)**, any other exception | **nothing**: no request to create or delete (not even a login), `.env` byte-for-byte the same, no prompt for manual replacement; `KeyUnverifiable`, the CLI exits with code **3**, bootstrap also 3 and stops before starting the remaining services |

### Replacement order (N4)

Never "delete the old one, create a new one". For first creation, replacement and `--force`:

1. superuser login and a **listing** of keys (read only; a failed listing is "unknown", never "empty"),
2. creating the new key,
3. verifying that Langflow **accepts** the new key (with the same request that rejected the old one),
4. **atomic write** to `.env` and reading it back.

**The old key is never deleted** (neither after `replaced` nor with `--force`). The masked value in the listing (`sk-m0fgI****`) does not reliably identify it: keys of the same name may share a visible prefix, and the rejected old key is no longer in the listing at all, so a prefix match could hit only somebody else's key.
After a replacement where `.env` held a previous key, the script says on stderr in one sentence (without values and `id`s) that the previous key remained in Langflow and that the operator removes it in Settings → API Keys if it is not in use.

**A failure before the `.env` write** (steps 1 to 3, also Ctrl-C) leaves `.env` and all old keys unchanged and the newly created key is deleted again: by the `id` from the create response, otherwise by the single new entry between two **successful** listings (before and after creation).
A failed listing, no new entry or more than one new entry means the key is not deleted and the script says so (a safe error). Non-acceptance of the new key (the proxy rejects everything, the address does not lead to Langflow) is `KeyUnverifiable`: code 3, nothing changed, no prompt to paste a key.

**A failure during or after the `.env` write** is not judged by the exception type (Ctrl-C says nothing about whether `os.replace` ran) but by a **fresh read of `.env`** after the failure:

| Fresh read of `.env` | State | New key | Result |
|---|---|---|---|
| contains the new key | stored | **not deleted** | `KeyStateUncertain` (code 1); Ctrl-C is re-raised unchanged after one line on stderr |
| cannot be read, or the file changed and the key is not in it | unknown | **not deleted** | the same |
| bytes are the same as just before the write | not stored (proven) | deleted | `KeyStoreError`: ".env is unchanged (compared byte for byte)" |

An error that occurred before the write began (`.env` could not even be read) says only "nothing was written to .env". The claim "unchanged" is never written without a byte comparison. `KeyStateUncertain` is a subclass of `KeyStoreError`, so it never leads to a prompt to paste a key; it says that the new key remained in Langflow, that nothing was deleted,
and that `LANGFLOW_API_KEY` in `.env` should be checked, the script run again (it first verifies the key in `.env`) and `docker compose up -d` run. The code is **1**, not 3: 3 promises that nothing changed, which does not hold here. The same logic applies to a manually pasted key (there is nothing in Langflow to delete).

A manually pasted key (only after a login or create failure and only for a missing or rejected key) is **verified** before it is stored; a non-accepted one is code 3 and `.env` stays.

`--force` in `create_api_key.py` remains an **explicit operator option** (it skips the check of the old key) and uses the same order. The automatic path does not pass it; a test guards this at run time and in the source (AST).
Message in the unverifiable state: the key could not be verified, the cause (only a status code or a fixed phrase), "Nothing was changed", run again once Langflow responds. Every bootstrap step is idempotent.

### Note on 401/403 and login

Only **401** is a reason for automatic replacement. **403** need not mean an invalid key (insufficient permission, a proxy response), and a successful superuser login gives no right to destroy or overwrite anything, so on the automatic path 403 is an **unverifiable state**:
code 3, a single request (the check), no login, listing, creation, deletion or `.env` write. The message mentions the explicit manual option: `scripts/create_api_key.py --force` (bootstrap deliberately does not have it).
For a new key with `--force` the same holds: only `200` with JSON is accepted, otherwise the key is "not accepted" (code 3, deleted by `id`, `.env` stays).
**Consequence, confirmed in the lab on Langflow 1.10.1 (section 5 of the review log):** for an invalid, missing and empty `x-api-key` alike Langflow returns **403**, not 401. Automatic replacement of a dead key (typically after `docker compose down -v`) therefore never triggers on this version and the operator must run `create_api_key.py --force`; code 3 and an unchanged configuration on 403 were confirmed on a real Langflow.

### Affected callers

| Caller | Impact |
|---|---|
| `bootstrap.step_api_key` | catches `KeyUnverifiable` (code 3, now also on 403), `KeyStateUncertain` and `KeyStoreError` as an ordinary failure (code 1, no further step, no "recreate" marking), `replaced` only after 401; it keeps passing the key to `step_langflow_vars` and `step_flows` in memory; passes `env_path` explicitly |
| `create_api_key.main` | the same; passes `env_path` explicitly |
| `ensure_api_key()` | `env_path` **has no default** (an AST test checks all callers) |
| `key_works()` | removed (its `False` merged three states) |
| `import_flows.py`, `smoke.py` | read the key from `.env`/environment; authenticated calls do not follow redirects |
| `install.py` | imports nothing (an existing test guards this) |

## 3. `.env`, redirects and secrets in the output

**`.env` (N2, N3).** The write is atomic: a temporary file in the same directory, `fsync`, `os.replace`; a failure (full disk, interruption) leaves the original bytes, mtime and directory listing. A symlinked `.env` stays a symlink (the file it points to is written). An existing mode is preserved.
A new `.env` is created 0600 from the first byte (temporary file + `link`, otherwise exclusive creation) and never overwrites an existing one; on Linux the mode is verified after creation. An existing `.env` with group or other permissions only produces a warning (`chmod 600 .env`), it is not changed.
On Windows `chmod` does not prove private ACLs, so nothing is claimed there.

**Redirects (N5).** `http_safe.open_url` rejects every 3xx on calls that carry `x-api-key` or `Authorization`; an `HTTPError` with the status code is raised and nothing is sent elsewhere. `LANGFLOW_URL` must therefore be the final address (the message says so).

**The progress output and errors never contain secrets.** Messages are composed of status codes and fixed phrases (`describe_error`), never of `str(exc)` or the response body. In addition, everything printed passes through `redact()`, which masks every value
that `.env` marks as secret by name (`PASSWORD`, `SECRET`, `TOKEN`, `KEY`, values of 6 or more characters), the key created in this run and **every value that `.env` held during the run** (an unreadable or vanished file does not switch masking off).
The output of subprocesses (`import_flows.py`, `smoke.py`) passes through `relay()`: only the line shapes of the child (result `[pass]`/`[skip]`/`[FAIL]`, numbered heading, totals, import lines), the rest of a multi-line message after the marker (the body) is discarded, `[FAIL]` is truncated before the first `:`,
control characters are turned into `?`, credentials in URLs are removed and lines are redacted **before** truncation. Credentials and the query are removed from addresses (`display_url`). A pasted key is entered via `getpass`. The first eight characters of a new key are not printed.

**Final summary** (the only place where secrets may appear):

| Situation | Summary |
|---|---|
| successful first run in an interactive terminal (stdin and stdout are TTYs, no `CI` and similar variables, no `--no-input`) | the path to `.env`, the names of the created values and a **one-time block** with their values at the very end |
| redirected stdout or stdin, CI (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`, `TF_BUILD`), `--no-input`, a run with errors | only the path to `.env` and the names of the created values |
| repeated run | only the path; nothing that already existed is printed |

The block may contain **only values created by this run**: `POSTGRES_PASSWORD`, `PGADMIN_DEFAULT_PASSWORD`, `LANGFLOW_SUPERUSER_PASSWORD`, `LANGFLOW_SECRET_KEY` (if they were `CHANGE_ME` and got generated) and `LANGFLOW_API_KEY` after creation or replacement.
Never: supplied external values (`OPENAI_API_KEY`, the Cloudflare token, `JINA_API_KEY`), a pasted key, anything that `.env` contained earlier. The Bifrost provider keys are not in `.env` at all. The decision whether to print the block is inside `step_summary` (`interactive_terminal`).
The values are **re-read from `.env`** when displayed; `.env` remains the permanent source. CI detection differs from `install.py` (which knows 13 variables); both are kept according to the owner's confirmed decision, the comparison is in the [proposal, section 10.5](b06-clean-install-proposal.md#10-implementation-status-of-b06-1-and-bootstrap-candidate).

## 4. Verification (offline)

A synthetic `.env` in a temporary directory, mocked HTTP (replacement of `OpenerDirector.open` and `urlopen`), a ban on `subprocess`, sockets and access to the real `.env` (a violation fails the test). For redirects, a fake transport under the real `urllib` redirect machinery
(a control test proves that an ordinary opener forwards the key). Synthetic passwords and keys are searched for, including with the eight-character prefix, in both stdout and stderr. The `.env` has mixed line endings and lacks the final newline.

| Area | Coverage |
|---|---|
| Classification | valid (object, array), 401 (rejected), 403 (unverifiable), 8 status codes 4xx/5xx, all 5 redirects, timeouts, refused/interrupted connection, DNS, TLS, disconnect, incomplete read, 201, 204, empty/HTML/truncated/`null`/string/numeric/binary body, corrupted gzip, unknown exception, a key that cannot be sent |
| Unverifiable state (403 included) | `.env` identical in bytes and in time, no request other than the single `GET` (no login, listing, creation, deletion), no prompt, code 3; at the level of the function, the CLI, `step_api_key` and `main()`; 401 still replaces, `--force` remains |
| Lifecycle | order of calls; failure of login, creation, verification (401, 403, 500, timeout, HTML) and write; failure of the read-back and a **real SIGINT right after and right before `os.replace`** (`signal.raise_signal`): after the write nothing is deleted and the state is admitted, before the write the new key is deleted only when the bytes are identical; a fresh read that fails or shows a third value is "unknown"; deletion of the new key by `id` and by the difference of two successful listings, never guessing; **no old key is deleted in any listing shape** (two identical prefixes, a single match, a different name, missing, incomparable mask, failed listing); a failed listing is not empty; `--force`; a manual key |
| `.env` | all five write failures, symlink, mode, umask, creation without overwrite, duplicate line, `step_env` |
| Output and summary | `redact` (unreadable `.env`), `relay` (multi-line bodies, control characters, URLs), `step_summary` (interactive × redirected × CI × `--no-input` × run with errors × repeated run), `main()` |
| Callers and code 3 | `env_path` without a default and in all calls, `bootstrap.main()` returns 3 and does not start further steps, `--help` |

Results after the final Codex review: see the review log and the [proposal, section 10.6](b06-clean-install-proposal.md#10-implementation-status-of-b06-1-and-bootstrap-candidate). Earlier round: Windows 127 tests OK, 11 skipped, WSL 127 OK; targeted mutation check of the new behaviours 23 of 23 caught
(shared with B06-1, see the [proposal, section 10.6](b06-clean-install-proposal.md#10-implementation-status-of-b06-1-and-bootstrap-candidate)). The original candidate reported 57 tests and 18 mutants; those are older numbers.

## 5. Limitations and what is not verified

- The offline tests are mocks. Against a real Langflow 1.10.1 the lab (2026-10-06, section 5 of the review log) confirmed: a valid key gets `200` on `GET /api/v1/flows/?page=1&size=1` (gzip, JSON list), an invalid one **403**, the create response carries `id` (a UUID as a string), the listing is masked (`sk-` and 5 characters, the rest `*`). **Still not verified:** a proxy in front of Langflow, PostgreSQL, a real run of `bootstrap.py`, the login throttling threshold (429).
- 401 does not prove that Langflow answered (a front-end proxy would produce the same). The order mitigates this: the new key must be accepted by the same request and stored, and the old value is never deleted.
- An interruption between the return of `create_key` and entering the `try` around `check_key` (and a second Ctrl-C during the assessment or deletion) loses the `id` and the `edu_ai-bootstrap` key stays in Langflow without a report; the window is a few bytecodes.
- After `KeyStateUncertain` in the "stored" state, the check on the next run comes out "valid" and the step is skipped; containers read the key only after `docker compose up -d` (the message says so). "Stored" is recognised by a substring of the value in the bytes, not by parsing the line (a false match only means that nothing is deleted).
- Values created during a run that then ended in an error are **never offered again** (on a repeat they already exist); they stay in `.env`. A consequence of the rule "do not print existing values".
- The one-time block ends up in the operator's terminal history; the script only reminds about this. `redact()` knows only the values that `.env` marks by name. The output of `docker compose up` goes directly to the terminal, outside `redact()`.
- After `replaced`, `up -d --force-recreate` is run for all services; there is no lock between concurrent runs; `check_key` has no overall deadline or response size limit. HTTPS→HTTP redirects, a proxy via `HTTP(S)_PROXY` and two concurrent runs were not tried.
- A real run of `bootstrap.py` (Docker, a live Langflow) did not take place and this document does not claim it.

## 6. Findings N1–N9: status

| # | Status | Note |
|---|---|---|
| N1 | partly open | the output that bootstrap forwards is closed (`relay`, `redact`); the standalone `import_flows.py` (`:84`) and `smoke.py` still print response bodies and payloads: a separate task |
| N2 | closed | atomic `.env` write (`write_atomic`) |
| N3 | closed on Linux | `.env` 0600 from creation; Windows is not claimed |
| N4 | closed, changed after the Codex review | replacement order; the old key is never deleted; the new one is deleted only by `id` or with two successful listings; after a possible `.env` write nothing is deleted and the state is admitted (section 2) |
| N5 | closed | `http_safe` in all four scripts with an authenticated call |
| N6 | closed | `/health` messages corrected; a test guards this |
| N7 | closed | `ONBOARDING.md` §1 and §5, B06 proposal sections 2 and 10 |
| N8 | closed | `env_path` without a default |
| N9 | tested | code 3 propagates (`bootstrap.main()`, CLI, `--help`); the B06-2 wrapper was not created, the code mapping is in the [proposal, section 10.1](b06-clean-install-proposal.md#10-implementation-status-of-b06-1-and-bootstrap-candidate) |

## 7. How to integrate the candidate

The final code review, the publication decision and the lab verification of the API keys against Langflow 1.10.1 took place (2026-10-06); the candidate is published on `origin/clean_init` (code and tests in commit `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, documentation in the following commit; its SHA cannot be stated inside itself, see `git log` of these files). What remains is the whole bootstrap against the stack, PostgreSQL and B06-2 ([proposal, section 10.8](b06-clean-install-proposal.md#108-next-step)).
Nothing in this document is a deployment approval or a supported school update. The candidate follows the rules in [AGENTS.md](../../AGENTS.md) (no secrets in the log, no manual editing of generated files); contracts, schema and Compose are not changed.
