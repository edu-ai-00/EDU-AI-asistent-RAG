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

Usage:
    python3 scripts/create_api_key.py            # skip if .env already works
    python3 scripts/create_api_key.py --force    # always mint a new key
    python3 scripts/create_api_key.py --no-input # never fall back to a prompt
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from envfile import is_placeholder, read_env, set_env  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Marks keys this script owns, so a re-run can retire the one it made last time
# instead of leaving a trail of dead keys on the instance.
KEY_NAME = "edu_ai-bootstrap"


def _read(response) -> dict:
    raw = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw) if raw else {}


def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _read(response)


def key_works(base_url: str, api_key: str) -> bool:
    """True if `api_key` can actually call the API right now.

    Cheaper and more honest than trusting .env: after `down -v` the file still
    holds the previous instance's key, which looks valid and is not.
    """
    if is_placeholder(api_key):
        return False
    try:
        _request(f"{base_url}/api/v1/flows/?page=1&size=1", headers={"x-api-key": api_key})
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def login(base_url: str, username: str, password: str) -> str:
    """Exchange superuser credentials for a bearer token (OAuth2 password grant)."""
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    result = _request(
        f"{base_url}/api/v1/login",
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = result.get("access_token")
    if not token:
        raise RuntimeError(f"login returned no access_token (keys: {sorted(result)})")
    return token


def _delete_stale_keys(base_url: str, token: str) -> None:
    """Remove keys this script created previously. Best effort — never fatal."""
    auth = {"Authorization": f"Bearer {token}"}
    try:
        listing = _request(f"{base_url}/api/v1/api_key/", headers=auth)
        for entry in listing.get("api_keys", []):
            if entry.get("name") == KEY_NAME and entry.get("id"):
                _request(f"{base_url}/api/v1/api_key/{entry['id']}", method="DELETE", headers=auth)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError):
        pass


def create_key(base_url: str, token: str) -> str:
    """Mint a fresh API key. The full value is returned only here — the list
    endpoint masks it (`sk-m0fgI****`), so it cannot be recovered later."""
    _delete_stale_keys(base_url, token)
    result = _request(
        f"{base_url}/api/v1/api_key/",
        method="POST",
        data=json.dumps({"name": KEY_NAME}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    api_key = result.get("api_key")
    if not api_key:
        raise RuntimeError(f"api_key/ returned no api_key (keys: {sorted(result)})")
    return api_key


def ensure_api_key(base_url: str, env_path: pathlib.Path = ENV_PATH, *,
                   force: bool = False, allow_prompt: bool = True) -> tuple[str, str]:
    """Guarantee .env holds a working LANGFLOW_API_KEY.

    Returns (api_key, outcome) where outcome is "skip", "created" or "manual".
    """
    env = read_env(env_path)
    existing = env.get("LANGFLOW_API_KEY", "")

    if not force and key_works(base_url, existing):
        return existing, "skip"

    username = env.get("LANGFLOW_SUPERUSER", "admin")
    password = env.get("LANGFLOW_SUPERUSER_PASSWORD", "")

    if not is_placeholder(password):
        try:
            api_key = create_key(base_url, login(base_url, username, password))
            set_env(env_path, {"LANGFLOW_API_KEY": api_key})
            return api_key, "created"
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                RuntimeError, json.JSONDecodeError) as exc:
            detail = exc.read().decode()[:200] if isinstance(exc, urllib.error.HTTPError) else exc
            print(f"  ! automatic key creation failed: {detail}", file=sys.stderr)
    else:
        print("  ! LANGFLOW_SUPERUSER_PASSWORD is not set in .env", file=sys.stderr)

    # Fallback keeps a first deployment to one command plus one paste, rather
    # than sending the operator back to editing files.
    if not allow_prompt or not sys.stdin.isatty():
        raise RuntimeError(
            "Could not create a Langflow API key and cannot prompt for one. "
            f"Create it at {base_url} → Settings → API Keys and put it in .env "
            "as LANGFLOW_API_KEY."
        )

    print(f"  Open {base_url} → Settings → API Keys → Add New.")
    api_key = input("  Paste the key here (sk-…): ").strip()
    if not api_key:
        raise RuntimeError("No key provided.")
    set_env(env_path, {"LANGFLOW_API_KEY": api_key})
    return api_key, "manual"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None,
                        help="Langflow base URL (default: LANGFLOW_URL from .env, "
                             "else http://127.0.0.1:7860)")
    parser.add_argument("--force", action="store_true",
                        help="create a new key even if the one in .env works")
    parser.add_argument("--no-input", action="store_true",
                        help="fail instead of prompting when automatic creation fails")
    args = parser.parse_args()

    env = read_env(ENV_PATH)
    base_url = (args.url or env.get("LANGFLOW_URL") or "http://127.0.0.1:7860").rstrip("/")

    if not ENV_PATH.is_file():
        sys.exit(f"{ENV_PATH} not found — run scripts/bootstrap.py first.")

    try:
        api_key, outcome = ensure_api_key(
            base_url, force=args.force, allow_prompt=not args.no_input
        )
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")

    message = {
        "skip": "LANGFLOW_API_KEY in .env already works — unchanged.",
        "created": f"Created a new API key and wrote it to .env ({api_key[:8]}…).",
        "manual": "Stored the pasted key in .env.",
    }[outcome]
    print(message)
    if outcome != "skip":
        print("Run `docker compose up -d` so the containers pick up the new value "
              "(env is read at container creation, not on restart).")


if __name__ == "__main__":
    main()
