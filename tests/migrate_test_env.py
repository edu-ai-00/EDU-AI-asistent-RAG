"""Isolated, synthetic PostgreSQL for the migration-runner tests (B03e).

    python tests/migrate_test_env.py up   b03e_pg_a      # own container + volume + internal network
    python tests/migrate_test_env.py down b03e_pg_a      # remove exactly that container and its volume

Everything is named `b03e_*` and labelled; `down` refuses anything else. The
container has no published ports and no .env, and is only attached to the
internal network `b03e_net`. The current C2 clean install (SQL and its .psql
includes, line endings folded to LF) runs on first start; `edu_ai_tpl` is a
pristine copy that tests use as a template. Secrets are generated for this
synthetic fixture only. Existing networks and every object removed by `down`
must have the expected labels; fixtures made without the owner label are refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Tagged by the Dockerfile content, so a base-image bump builds a fresh image
# instead of reusing a stale one that no longer matches the baseline manifest.
_DOCKERFILE = (ROOT / "Dockerfile.postgres").read_bytes().replace(b"\r\n", b"\n")
IMAGE = "b03e_postgres:cs-" + hashlib.sha256(_DOCKERFILE).hexdigest()[:12]
NETWORK = "b03e_net"
LABEL = "edu_ai.purpose=b03e-migrate-test"
PURPOSE_KEY, PURPOSE = LABEL.split("=", 1)
OWNER_KEY = "edu_ai.test-owner"
USER, DB = "edu_ai_admin", "edu_ai"
TEMPLATE = "edu_ai_tpl"
DATA_TARGET = "/var/lib/postgresql/data"
IMAGE_VOLUME = "/var/lib/postgresql"
PASSWORD_ENV = ("POSTGRES_PASSWORD", "POSTGRES_INGEST_PASSWORD", "POSTGRES_SEARCH_PASSWORD",
                "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD")


def docker(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        sys.exit(f"docker {' '.join(args[:3])} failed: {p.stderr.strip()}")
    if not quiet and p.stdout.strip():
        print(p.stdout.strip())
    return p


def exists(kind: str, name: str) -> bool:
    return docker(kind, "inspect", name, check=False, quiet=True).returncode == 0


def psql(name: str, sql: str, db: str = DB) -> str:
    p = subprocess.run(["docker", "exec", "-i", name, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                        "-U", USER, "-d", db], input=sql, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def check_name(name: str) -> None:
    if not re.fullmatch(r"b03e_[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        sys.exit("refusing: needs a valid Docker name starting with b03e_")


def inspect_resource(kind: str, name: str) -> dict:
    result = docker(kind, "inspect", "--format", "{{json .}}", name, check=False, quiet=True)
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
    sys.exit(f"refusing: cannot verify {kind} {name}")


def owns(labels, name: str | None = None) -> bool:
    return (isinstance(labels, dict) and labels.get(PURPOSE_KEY) == PURPOSE
            and (name is None or labels.get(OWNER_KEY) == name))


def check_network() -> None:
    value = inspect_resource("network", NETWORK)
    if value.get("Name") != NETWORK or value.get("Internal") is not True or not owns(value.get("Labels")):
        sys.exit(f"refusing: {NETWORK} is not the labelled internal test network")


def check_volume(volume: str, name: str) -> None:
    value = inspect_resource("volume", volume)
    if value.get("Name") != volume or not owns(value.get("Labels"), name):
        sys.exit(f"refusing: {volume} is not this fixture's labelled volume")


def fixture_passwords() -> dict[str, str]:
    values = {key: secrets.token_hex(24) for key in PASSWORD_ENV}
    if len(set(values.values())) != len(PASSWORD_ENV) or any(len(value) < 16 for value in values.values()):
        sys.exit("refusing: synthetic fixture password generation failed")
    return values


def up(name: str) -> None:
    check_name(name)
    volume = f"{name}_data"
    if exists("container", name) or exists("volume", volume):
        sys.exit(f"{name} or {volume} already exists; run 'down' first")
    passwords = fixture_passwords()
    if not exists("network", NETWORK):
        docker("network", "create", "--internal", "--label", LABEL, NETWORK)
    check_network()
    if not exists("image", IMAGE):
        with tempfile.TemporaryDirectory() as ctx:
            docker("build", "-f", str(ROOT / "Dockerfile.postgres"), "-t", IMAGE, ctx)
    owner_label = f"{OWNER_KEY}={name}"
    docker("volume", "create", "--label", LABEL, "--label", owner_label, volume, quiet=True)
    check_volume(volume, name)
    password_args = [arg for key, value in passwords.items() for arg in ("-e", f"{key}={value}")]
    docker("create", "--name", name, "--label", LABEL, "--label", owner_label, "--network", NETWORK,
           "-e", f"POSTGRES_USER={USER}", *password_args,
           "-e", f"POSTGRES_DB={DB}", "-v", f"{volume}:{DATA_TARGET}",
           IMAGE, "postgres", "-c", "shared_preload_libraries=pg_search", quiet=True)
    with tempfile.TemporaryDirectory() as tmp:
        files = (ROOT / "postgres" / "init_clean").iterdir()
        for f in sorted(f for f in files if f.suffix in (".sql", ".psql")):
            (Path(tmp) / f.name).write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))
        docker("cp", f"{tmp}/.", f"{name}:/docker-entrypoint-initdb.d/", quiet=True)
    docker("start", name, quiet=True)
    for _ in range(120):  # the init scripts run on a socket-only server first
        if docker("exec", name, "pg_isready", "-h", "127.0.0.1", check=False, quiet=True).returncode == 0:
            try:
                if int(psql(name, "SELECT count(*) FROM edu_ai.taxonomy_rules;")) > 0:
                    break
            except (RuntimeError, ValueError):
                pass
        time.sleep(2)
    else:
        sys.exit(f"{name} did not become ready")
    psql(name, f"CREATE DATABASE {TEMPLATE} TEMPLATE {DB};", db="postgres")
    print(f"{name} ready (template database {TEMPLATE})")


def down(name: str) -> None:
    check_name(name)
    volume = f"{name}_data"
    value = inspect_resource("container", name)
    config, networks, host = value.get("Config"), value.get("NetworkSettings"), value.get("HostConfig")
    mounts = value.get("Mounts")
    cid = value.get("Id")
    # PostgreSQL 18 images declare VOLUME /var/lib/postgresql: an empty anonymous volume beside ours.
    if isinstance(mounts, list):
        mounts = [m for m in mounts if not (isinstance(m, dict) and m.get("Destination") == IMAGE_VOLUME
                                            and re.fullmatch(r"[0-9a-f]{64}", str(m.get("Name"))))]
    if (value.get("Name") != f"/{name}" or not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid)
            or not isinstance(config, dict) or not owns(config.get("Labels"), name)
            or not isinstance(networks, dict) or not isinstance(networks.get("Networks"), dict)
            or set(networks["Networks"]) != {NETWORK} or not isinstance(host, dict)
            or "PortBindings" not in host or host["PortBindings"] not in (None, {})
            or not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict)
            or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != volume
            or mounts[0].get("Destination") != DATA_TARGET or mounts[0].get("RW") is not True):
        sys.exit(f"refusing: {name} is not this isolated fixture with its own data mount")
    check_network()
    check_volume(volume, name)
    users = docker("ps", "-a", "--filter", f"volume={volume}", "--format", "{{.Names}}", quiet=True)
    if set(users.stdout.splitlines()) != {name}:
        sys.exit(f"refusing: {volume} has unexpected users")
    docker("rm", "-f", "-v", cid, quiet=True)          # -v: the anonymous image volume only, never a named one
    docker("volume", "rm", volume, quiet=True)
    print(f"removed {name} and {volume}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["up", "down"])
    ap.add_argument("name")
    a = ap.parse_args()
    (up if a.action == "up" else down)(a.name)
