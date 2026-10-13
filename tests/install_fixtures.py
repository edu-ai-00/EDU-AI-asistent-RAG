"""Synthetic data and helpers for the B06-1 install tests (not a test). Nothing here reads the real repository or a real .env."""
import contextlib
import hashlib
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import install  # noqa: E402

SYN_COMPOSE = """name: syn
services:
  app:
    image: syn_app:1   # comment ${COMMENTED_ONLY:?never}
    environment:
      - DB_PASS=${POSTGRES_PASSWORD}
      - SUBNET=${SYN_SUBNET:?SYN_SUBNET is not set}
      - OPT=${SYN_OPTIONAL:-}
      - NOTE="a # inside quotes ${QUOTED_HASH:-x}"
configs:
  trust:
    content: |
      set_real_ip_from ${SYN_SUBNET};
      # literal text, still interpolated: ${IN_BLOCK:?x}
"""

SYN_TEMPLATE = """# synthetic template
COMPOSE_PROJECT_NAME=syn
#COMPOSE_FILE=docker-compose.yml
POSTGRES_PASSWORD=CHANGE_ME_pg
PGADMIN_DEFAULT_PASSWORD=CHANGE_ME_pgadmin
LANGFLOW_SUPERUSER_PASSWORD=CHANGE_ME_lf_admin
LANGFLOW_SECRET_KEY=CHANGE_ME_lf_secret
POSTGRES_ADMIN_PASSWORD=CHANGE_ME_pg_admin
POSTGRES_SEARCH_PASSWORD=CHANGE_ME_pg_search
POSTGRES_LANGFLOW_PASSWORD=CHANGE_ME_pg_langflow
POSTGRES_MIGRATOR_PASSWORD=CHANGE_ME_pg_migrator
BIFROST_SETUP_TOKEN=CHANGE_ME_bifrost_setup
BIFROST_ADMIN_PASSWORD=CHANGE_ME_bifrost_admin
LANGFLOW_API_KEY=CHANGE_ME_lf_api
OPENAI_API_KEY=CHANGE_ME_openai
IN_BLOCK=filled
SYN_CONST=literal
"""

# Known-valid generated values, so output hygiene can be checked by searching for them.
STUB_VALUES = {
    "POSTGRES_PASSWORD": "Pg" + "a" * 30,
    "PGADMIN_DEFAULT_PASSWORD": "Pa" + "b" * 30,
    "LANGFLOW_SUPERUSER_PASSWORD": "Su" + "c" * 30,
    "LANGFLOW_SECRET_KEY": "dGhpcy1pcy1hLXN5bnRoZXRpYy1mZXJuZXQta2V5ISE=",
    "POSTGRES_ADMIN_PASSWORD": "Ad" + "d" * 30,
    "POSTGRES_SEARCH_PASSWORD": "Se" + "e" * 30,
    "POSTGRES_LANGFLOW_PASSWORD": "Lf" + "f" * 30,
    "POSTGRES_MIGRATOR_PASSWORD": "Mi" + "g" * 30,
    "BIFROST_SETUP_TOKEN": "Bf" + "h" * 30,
    "BIFROST_ADMIN_PASSWORD": "Ba" + "i" * 30,
}
STUB_GEN = {k: (lambda v=v: v) for k, v in STUB_VALUES.items()}


def git(repo, *args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                          capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


def make_repo(root, files=None, message="synthetic"):
    """A throw-away git repository with one commit; returns (path, full sha)."""
    repo = Path(root)
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
        git(repo, "config", "core.autocrlf", "false")
    for name, data in (files if files is not None else {"docker-compose.yml": SYN_COMPOSE, ".env.example": SYN_TEMPLATE}).items():
        (repo / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        git(repo, "add", name)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return repo, git(repo, "rev-parse", "HEAD")


def run_cli(argv):
    """(exit code, stdout lines) of install.main."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.main(argv)
    return code, out.getvalue().splitlines()


def cli(cmd, sha, target, repo):
    return run_cli([cmd, "--source-sha", sha, "--target-dir", str(target), "--repo", str(repo)])


def field(lines, label):
    for line in lines:
        if line.startswith(label + ":"):
            return line.split(":", 1)[1].strip()
    return None


def is_emptied_staging(full):
    """The leftover of a failed run: a staging directory whose entries are all zero-length regular files (our emptied copies).

    The cleanup never removes a name, so the directory stays and is announced (STAGING_LEFT_EMPTIED); a directory with no entry at all counts too.
    """
    full = Path(full)
    if ".staging-" not in full.name or full.is_symlink() or not full.is_dir():
        return False
    for name in os.listdir(full):
        info = os.lstat(full / name)
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            return False
    return True


def secret_staging(parent):
    """Names of staging directories in `parent` that may still hold something (an emptied one is not a leftover of secrets)."""
    return [n for n in os.listdir(parent) if ".staging-" in n and not is_emptied_staging(Path(parent) / n)]


def emptied_staging(parent):
    return [n for n in os.listdir(parent) if is_emptied_staging(Path(parent) / n)]


def snapshot(path):
    """{relative path: (kind, mode, sha256 of content)} of everything under path (not following symlinks); emptied staging directories are left out."""
    path = Path(path)
    seen = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if not is_emptied_staging(Path(dirpath) / d)]      # neither the directory nor its zero-length files
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            info = os.lstat(full)
            if full.is_symlink():
                seen[str(full.relative_to(path))] = ("link", os.readlink(full), "")
            elif full.is_dir():
                seen[str(full.relative_to(path))] = ("dir", oct(info.st_mode & 0o7777), "")
            else:
                seen[str(full.relative_to(path))] = ("file", oct(info.st_mode & 0o7777), hashlib.sha256(full.read_bytes()).hexdigest())
    return seen


def write_config_set(directory, key_value, *, record_key_sha=None, with_env=True, with_record=True):
    """Hand-made set (portable, no init-config): .env and a record whose fingerprint matches key_value."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if with_env:
        (directory / ".env").write_bytes(f"LANGFLOW_SECRET_KEY={key_value}\n".encode())
    if with_record:
        import json
        record = {"schema": install.RECORD_SCHEMA, "status": "CONFIG_PREPARED",
                  "langflow_secret_key_sha256": record_key_sha or hashlib.sha256(key_value.encode()).hexdigest()}
        (directory / "install-record.json").write_text(json.dumps(record), encoding="utf-8")
    return directory
