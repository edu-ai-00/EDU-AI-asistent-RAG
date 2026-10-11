#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-07-27).
"""Create a Langflow API key over REST and write it into .env.

Why this exists: with LANGFLOW_AUTO_LOGIN=false — our real-auth switch — the
documented CLI route refuses to work:

    $ docker compose exec edu_ai langflow api-key
    Auto login is disabled. API keys cannot be created through the CLI.

That left "open the UI, log in, Settings → API Keys → copy" as the only way to
get the first key, which is the single step that kept a fresh deployment from
being scriptable. The REST endpoints the UI itself calls have no such
restriction, so this does the same thing headlessly:

    POST /api/v1/login      (form-encoded superuser credentials) → access_token
    POST /api/v1/api_key/   (bearer)                             → api_key

Verified against Langflow 1.10.1 (2026-07-25).

A key already in .env is checked before anything is touched, and the check has
three answers, not two:

    valid         Langflow accepted it             → nothing changes
    rejected      Langflow answered 401            → a new key replaces it
    unverifiable  anything else (403, timeout, refused connection, 5xx, an odd
                  status or body) → NOTHING changes: no key created or removed,
                  .env left as it is, no prompt. A flaky Langflow is not evidence
                  that the key is dead, a 403 can be a proxy or a lack of rights,
                  and replacing a live key breaks every container that holds it.

No key at all (missing line, empty value, CHANGE_ME placeholder) is the first
creation and always goes ahead. `--force` skips the check: the operator's explicit
choice, for example for a key that is known to be dead behind a 403.

A new key is never made by "delete the old one, then create": the order is
  1. log in and LIST the keys named edu_ai-bootstrap (the name this script gives its keys),
  2. create the new key,
  3. check that Langflow accepts it (any answer but a valid one, a 403 included, is "not accepted"),
  4. write it to .env (atomically; the file is read back).
The previous key is never deleted: a masked listing (`sk-m0fgI****`) cannot identify it, so
it is left in Langflow and the operator is told. Only the key created in step 2 is ever deleted.

Until step 4 starts, a failure leaves .env untouched and the new key is deleted again. From
the moment the write begins .env may already hold it (a rename is atomic, an interrupt can land
right after it), so the new key is deleted only when a fresh read of .env proves the write did
not happen; if .env holds it or cannot be read, the key is kept and the state is reported.
The new key is deleted by the id of the creation reply, or else by the one entry a listing
shows after the creation that the listing before it did not; never by a guess. A failed listing
is "unknown", never "empty". If the new key is not accepted, nothing changes and the exit
status is 3 (the address probably does not lead to Langflow itself).

Every call that carries the key or a token refuses an HTTP redirect (see http_safe.py):
a Location header never receives a copy of x-api-key or Authorization.

Usage:
    python3 scripts/create_api_key.py            # skip if .env already works
    python3 scripts/create_api_key.py --force    # operator's choice: always mint a new key
    python3 scripts/create_api_key.py --no-input # never fall back to a prompt

Exit status: 0 done, 1 error (also when .env may or may not hold a new key: the message says
what is known), 3 the key in .env could not be verified or the new key was not accepted
(nothing was changed — run it again later).

Nothing here prints a key, a password, an id or a response body; failures are described
by status code or a fixed phrase only.
"""

from __future__ import annotations

import argparse
import getpass
import gzip
import http.client
import json
import pathlib
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from envfile import is_placeholder, read_env, set_env  # noqa: E402
from http_safe import open_url  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# The name this script gives its keys: the only keys it may ever look for or delete.
KEY_NAME = "edu_ai-bootstrap"

# The three answers a check of the key in .env can give, plus "nothing to check".
KEY_VALID = "valid"
KEY_REJECTED = "rejected"
KEY_UNVERIFIABLE = "unverifiable"
KEY_ABSENT = "absent"

# A 403 says this caller is refused (rights, a proxy), not that the key is dead.
DETAIL_ACCESS_DENIED = "HTTP 403, access denied: not proof that the key is invalid"

# "Could not tell, nothing was changed, try again later" — distinct from a plain
# failure so a wrapper can retry instead of giving up.
EXIT_RETRY_LATER = 3

CHECK_TIMEOUT = 30


class KeyCheck(NamedTuple):
    status: str
    detail: str  # fixed vocabulary only — never exception text or a response body


class KeyUnverifiable(RuntimeError):
    """The key in .env could be neither confirmed nor shown to be rejected.

    A RuntimeError so a caller that predates this class still fails closed.
    """


class KeyServiceError(RuntimeError):
    """A failure whose message this module wrote itself, safe to print."""


class KeyStoreError(KeyServiceError):
    """.env could not take the new key. Asking the operator to paste one would fail the same way."""


class KeyStateUncertain(KeyStoreError):
    """The write to .env may have happened (or cannot be told): the new key was kept, not deleted.

    Exit status 1, not 3: 3 promises that nothing was changed, and here .env may hold the key.
    """


def display_url(url: str) -> str:
    """`url` without credentials, query or fragment, for messages."""
    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "<unparseable url>"


def describe_error(exc: BaseException) -> str:
    """A short description of a failure that cannot carry a secret.

    Built from the status code or a fixed phrase. str(exc) is deliberately not
    used: an HTTPError can carry a response body, and a URLError whatever the
    stack put in it.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (ValueError, EOFError, gzip.BadGzipFile, zlib.error)):
        return "malformed response"
    if isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException)):
        return "connection error"
    return "unexpected error"


def _discard(exc: urllib.error.HTTPError) -> None:
    """Release an error response without reading it."""
    try:
        exc.close()
    except Exception:  # noqa: BLE001 — nothing to recover from a failed close
        pass


def _read(response) -> dict:
    raw = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw) if raw else {}


def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with open_url(request, timeout=timeout) as response:
        return _read(response)


def check_key(base_url: str, api_key: str) -> KeyCheck:
    """Ask Langflow whether `api_key` works right now. Fails closed.

    Cheaper and more honest than trusting .env: after `down -v` the file still
    holds the previous instance's key, which looks valid and is not.

    Only two things are conclusive: a 200 with a JSON body (valid) and a 401
    (rejected). Everything else — a 403 (rights or a proxy, not proof the key is
    dead) and a 200 that is not JSON (a proxy's error page) included — is
    unverifiable, because none of it says anything about the key.
    """
    if is_placeholder(api_key) or not api_key.strip():
        return KeyCheck(KEY_ABSENT, "no key set")
    if not (api_key.isascii() and api_key.isprintable()):
        # A header cannot carry it, so Langflow was never asked: neither valid nor rejected.
        return KeyCheck(KEY_UNVERIFIABLE, "the key in .env has characters that cannot be sent")

    request = urllib.request.Request(f"{base_url}/api/v1/flows/?page=1&size=1",
                                     headers={"x-api-key": api_key})
    try:
        with open_url(request, timeout=CHECK_TIMEOUT) as response:
            if response.status != 200:
                return KeyCheck(KEY_UNVERIFIABLE, f"unexpected HTTP {response.status}")
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        _discard(exc)
        if exc.code == 401:
            return KeyCheck(KEY_REJECTED, "HTTP 401")
        if exc.code == 403:
            return KeyCheck(KEY_UNVERIFIABLE, DETAIL_ACCESS_DENIED)
        if 300 <= exc.code < 400:
            # Refused on purpose (http_safe): the key must not leave this address.
            return KeyCheck(KEY_UNVERIFIABLE, f"HTTP {exc.code} redirect, not followed; "
                                              "set LANGFLOW_URL to the final address")
        return KeyCheck(KEY_UNVERIFIABLE, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 — fail closed: any surprise is "cannot tell"
        return KeyCheck(KEY_UNVERIFIABLE, describe_error(exc))

    if not isinstance(payload, (dict, list)):
        return KeyCheck(KEY_UNVERIFIABLE, "malformed response")
    return KeyCheck(KEY_VALID, "HTTP 200")


def login(base_url: str, username: str, password: str) -> str:
    """Exchange superuser credentials for a bearer token (OAuth2 password grant)."""
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    result = _request(
        f"{base_url}/api/v1/login",
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = result.get("access_token") if isinstance(result, dict) else None
    if not token:
        raise KeyServiceError("login returned no access token")
    return token


def own_keys(base_url: str, token: str) -> list[dict] | None:
    """The listed keys that carry this script's name, or None when the listing cannot be trusted.

    Read-only. None is "unknown", never "empty": a caller that took a failed listing for an empty
    one would find a "fresh" key that was already there.
    """
    try:
        keys = _request(f"{base_url}/api/v1/api_key/", headers={"Authorization": f"Bearer {token}"})["api_keys"]
    except Exception:  # noqa: BLE001 - any trouble (HTTP, JSON, a shape we do not know) is "unknown"
        return None
    if not isinstance(keys, list):
        return None
    own = [entry for entry in keys if isinstance(entry, dict) and entry.get("name") == KEY_NAME]
    return own if all(entry.get("id") for entry in own) else None   # an entry without an id cannot be told apart


def delete_keys(base_url: str, token: str, key_ids: list[str]) -> int:
    """Delete exactly `key_ids`; returns how many could not be deleted. Never fatal."""
    auth = {"Authorization": f"Bearer {token}"}
    failed = 0
    for key_id in key_ids:
        try:
            _request(f"{base_url}/api/v1/api_key/{urllib.parse.quote(str(key_id), safe='')}",
                     method="DELETE", headers=auth)
        except Exception:  # noqa: BLE001 - best effort
            failed += 1
    return failed


def create_key(base_url: str, token: str) -> tuple[str, str | None]:
    """Mint a fresh API key and delete nothing; returns (value, id or None).

    The full value is returned only here: the list endpoint masks it, so it cannot be recovered later.
    """
    result = _request(
        f"{base_url}/api/v1/api_key/",
        method="POST",
        data=json.dumps({"name": KEY_NAME}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    api_key = result.get("api_key") if isinstance(result, dict) else None
    if not api_key:
        raise KeyServiceError("key creation returned no api_key")
    key_id = result.get("id")
    return api_key, (str(key_id) if key_id else None)


_NOT_PUBLISHED, _PUBLISHED, _UNKNOWN = "not published", "published", "unknown"
_PROOF_UNCHANGED = ".env is unchanged (compared byte for byte)"
_PROOF_UNTOUCHED = "nothing was written to .env"
_NEXT_STEPS = ("Check LANGFLOW_API_KEY in .env, run scripts/create_api_key.py again (it checks the key "
               "in .env first), and run `docker compose up -d` so the containers read it.")


def _read_raw(env_path: pathlib.Path) -> bytes | None:
    """The bytes of .env as they are now; None when there is no such file. OSError when unreadable."""
    try:
        return pathlib.Path(env_path).read_bytes()
    except FileNotFoundError:
        return None


def _judge(env_path: pathlib.Path, api_key: str, before: bytes | None) -> tuple[str, str]:
    """What a fresh read of .env proves about a write that started: (state, what was verified).

    "not published" only when the bytes equal those read before the write; a file that changed
    without holding the key, or cannot be read, is unknown.
    """
    try:
        now = _read_raw(env_path)
    except OSError:
        return _UNKNOWN, "it could not be read again"
    if now is not None and api_key.encode("utf-8") in now:
        return _PUBLISHED, "it holds the key"
    if now == before:
        return _NOT_PUBLISHED, _PROOF_UNCHANGED
    return _UNKNOWN, "it changed during the write and does not hold the key"


def _not_stored_text(reason: str, proof: str, removed: bool | None, interrupted: bool) -> str:
    tail = {True: " The new key was removed from Langflow again.",
            False: " The new key could not be removed from Langflow and was left there.",
            None: ""}[removed]
    action = "Run this again." if interrupted else "Fix the file's permissions and run this again."
    return f"The key was not stored: .env could not be written ({reason}); {proof}.{tail} {action}"


def _uncertain_text(state: str, reason: str, detail: str, minted: bool) -> str:
    what = "new Langflow API" if minted else "pasted"
    if state == _PUBLISHED:
        head = f"The {what} key is stored in .env, but the write did not finish cleanly ({reason})."
    else:
        head = f"Whether .env holds the {what} key is unknown ({reason}; {detail})."
    aside = ("The new key exists in Langflow and was left there; no key was deleted."
             if minted else "Nothing in Langflow was touched.")
    return f"{head} {aside} {_NEXT_STEPS}"


def _store(env_path: pathlib.Path, api_key: str, *, discard=None) -> None:
    """Write the key to .env (atomically), read it back, and return only when .env holds it.

    A failure is judged from .env itself, not from the exception type: only a fresh read that proves the
    write did not happen calls `discard` (deletes the Langflow key; None for a pasted key). Otherwise the key
    is kept and the state reported: KeyStateUncertain, or the interrupt itself after one stderr line.
    """
    before: bytes | None = None
    entered, failure = False, None
    try:
        before = _read_raw(env_path)
        entered = True                      # from here on a publication is possible
        set_env(env_path, {"LANGFLOW_API_KEY": api_key})
        if read_env(env_path).get("LANGFLOW_API_KEY") == api_key:
            return
    except BaseException as exc:  # noqa: BLE001 - judged from the disk below, then re-raised or reported
        failure = exc
    state, detail = _judge(env_path, api_key, before) if entered else (_NOT_PUBLISHED, _PROOF_UNTOUCHED)
    interrupted = failure is not None and not isinstance(failure, Exception)
    reason = ("the readback did not match" if failure is None
              else "interrupted" if isinstance(failure, KeyboardInterrupt) else type(failure).__name__)
    if state == _NOT_PUBLISHED:
        removed = discard() if discard else None
        text = _not_stored_text(reason, detail, removed, interrupted)
        error = failure if interrupted else KeyStoreError(text)
    else:
        text = _uncertain_text(state, reason, detail, minted=discard is not None)
        error = failure if interrupted else KeyStateUncertain(text)
    if interrupted:
        print(f"  ! {text}", file=sys.stderr)
    raise error


def _discard_new(base_url: str, token: str, new_id: str | None, before: list[dict] | None) -> bool:
    """Delete the key just created and never stored; True when it is gone, else tell the operator it stayed.

    Only by the id the creation reply gave, or by the one entry that a listing shows now and the listing
    before the creation did not. A failed listing, or any other number of fresh entries, deletes nothing.
    """
    found = new_id
    if not found and before is not None:
        after = own_keys(base_url, token)
        if after is not None:
            known = {str(entry["id"]) for entry in before}
            fresh = [str(entry["id"]) for entry in after if str(entry["id"]) not in known]
            found = fresh[0] if len(fresh) == 1 else None
    if found and not delete_keys(base_url, token, [found]):
        return True
    print(f"  ! A key named {KEY_NAME} was created in this run and left in Langflow (it could not be identified "
          "or removed reliably). Remove it in Langflow, Settings, API Keys, if it is unused.", file=sys.stderr)
    return False


def _replace_key(base_url: str, env_path: pathlib.Path, username: str, password: str, replaces: str) -> str:
    """Create a new key, check it and store it. The previous key (`replaces`, "" for none) is never deleted.

    Before the write starts a failure deletes the new key again and .env was never touched; once it
    has started _store decides, from .env itself, whether the new key may be deleted.
    """
    token = login(base_url, username, password)
    before = own_keys(base_url, token)
    api_key, new_id = create_key(base_url, token)

    def discard() -> bool:
        return _discard_new(base_url, token, new_id, before)

    try:
        check = check_key(base_url, api_key)
    except BaseException:                   # Ctrl-C: the key was never stored
        discard()
        raise
    if check.status != KEY_VALID:
        removed = discard()
        raise KeyUnverifiable(
            f"A newly created Langflow API key was not accepted by {display_url(base_url)} ({check.detail}). "
            + ("It was removed again. Nothing was changed: .env and the existing keys are as they were. "
               if removed else
               "It could not be removed again (see above). .env and the existing keys were not touched. ")
            + "If this repeats, the address probably does not lead to Langflow itself (a proxy in front?).")
    _store(env_path, api_key, discard=discard)
    if replaces:
        print("  ! The previous key was left in Langflow: it cannot be identified reliably from the masked "
              "listing. Remove it in Langflow, Settings, API Keys, if it is unused.", file=sys.stderr)
    return api_key


def ensure_api_key(base_url: str, env_path: pathlib.Path, *,
                   force: bool = False, allow_prompt: bool = True) -> tuple[str, str]:
    """Guarantee .env holds a working LANGFLOW_API_KEY.

    Returns (api_key, outcome), outcome being
      "skip"      the key in .env was accepted — nothing changed,
      "created"   there was no key (or `force`) and a new one was written,
      "replaced"  Langflow rejected the key in .env (401 only) and a new one was written,
      "manual"    a pasted key was written.

    Raises KeyUnverifiable — having changed nothing — when .env holds a key that
    Langflow neither accepted nor rejected (a 403 included), or when the new key is not
    accepted. Raises KeyStateUncertain when .env may hold the new key (it was kept).
    `force` is the operator's explicit override and skips the check; no automatic
    caller may pass it. The previous key is never deleted (see _replace_key).

    `env_path` has no default on purpose: a caller says which file it means, so a
    test or an installer working on an external configuration can never write to the
    checkout's own .env by omission.
    """
    env = read_env(env_path)
    existing = env.get("LANGFLOW_API_KEY", "")
    replacing = False

    if not force:
        check = check_key(base_url, existing)
        if check.status == KEY_VALID:
            return existing, "skip"
        if check.status == KEY_UNVERIFIABLE:
            denied = (" If you know the key is dead, `scripts/create_api_key.py --force` replaces it."
                      if check.detail == DETAIL_ACCESS_DENIED else "")
            raise KeyUnverifiable(
                f"The LANGFLOW_API_KEY in .env could not be verified against "
                f"{display_url(base_url)} ({check.detail}). Nothing was changed: no key "
                "was created or removed and .env was not rewritten. Check that Langflow "
                f"answers normally, then run this again.{denied}"
            )
        if check.status == KEY_REJECTED:
            replacing = True
            print(f"  ! Langflow rejected the API key in .env ({check.detail}); "
                  "replacing it.", file=sys.stderr)

    username = env.get("LANGFLOW_SUPERUSER", "admin")
    password = env.get("LANGFLOW_SUPERUSER_PASSWORD", "")

    if not is_placeholder(password):
        try:
            api_key = _replace_key(base_url, env_path, username, password,
                                   "" if is_placeholder(existing) else existing)
        except (KeyUnverifiable, KeyStoreError):   # includes KeyStateUncertain
            raise                                  # a prompt would not help: an address that is not Langflow, or a file that cannot be written
        except KeyServiceError as exc:
            print(f"  ! automatic key creation failed: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — described by status/phrase only
            print(f"  ! automatic key creation failed: {describe_error(exc)}", file=sys.stderr)
        else:
            return api_key, "replaced" if replacing else "created"
    else:
        print("  ! LANGFLOW_SUPERUSER_PASSWORD is not set in .env", file=sys.stderr)

    # Reached only for an absent or a rejected key (or `force`) — never for an
    # unverifiable one. The fallback keeps a first deployment to one command plus
    # one paste, rather than sending the operator back to editing files.
    if not allow_prompt or not sys.stdin.isatty():
        raise RuntimeError(
            "Could not create a Langflow API key and cannot prompt for one. "
            f"Create it at {display_url(base_url)} → Settings → API Keys and put it in "
            ".env as LANGFLOW_API_KEY."
        )

    print(f"  Open {display_url(base_url)} → Settings → API Keys → Add New.")
    api_key = getpass.getpass("  Paste the key here (input is hidden): ").strip()
    if not api_key:
        raise RuntimeError("No key provided.")
    check = check_key(base_url, api_key)
    if check.status != KEY_VALID:
        raise KeyUnverifiable(
            f"The pasted key was not accepted by {display_url(base_url)} ({check.detail}). "
            "Nothing was changed: .env is as it was.")
    _store(env_path, api_key)
    return api_key, "manual"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Exit status: 0 done, 1 error (also when .env may hold a new key: the message says what is "
               "known), 3 the key could not be verified, an HTTP 403 included (nothing was changed; "
               "run it again later).")
    parser.add_argument("--url", default=None,
                        help="Langflow base URL (default: LANGFLOW_URL from .env, "
                             "else http://127.0.0.1:7860)")
    parser.add_argument("--force", action="store_true",
                        help="create a new key even if the one in .env works "
                             "(operator's choice; skips the check)")
    parser.add_argument("--no-input", action="store_true",
                        help="fail instead of prompting when automatic creation fails")
    args = parser.parse_args()

    env = read_env(ENV_PATH)
    base_url = (args.url or env.get("LANGFLOW_URL") or "http://127.0.0.1:7860").rstrip("/")

    if not ENV_PATH.is_file():
        sys.exit(f"{ENV_PATH} not found — run scripts/bootstrap.py first.")

    try:
        _, outcome = ensure_api_key(
            base_url, ENV_PATH, force=args.force, allow_prompt=not args.no_input
        )
    except KeyUnverifiable as exc:
        print(f"NOT CHANGED: {exc}", file=sys.stderr)
        sys.exit(EXIT_RETRY_LATER)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")

    message = {
        "skip": "LANGFLOW_API_KEY in .env already works — unchanged.",
        "created": "Created a new API key and wrote it to .env.",
        "replaced": "Langflow rejected the API key in .env; created a new one and wrote it to .env.",
        "manual": "Stored the pasted key in .env.",
    }[outcome]
    print(message)
    if outcome != "skip":
        print("Run `docker compose up -d` so the containers pick up the new value "
              "(env is read at container creation, not on restart).")


if __name__ == "__main__":
    main()
