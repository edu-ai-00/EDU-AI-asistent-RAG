"""Isolated, synthetic PostgreSQL for the migration-runner tests (B03e).

    python tests/migrate_test_env.py up   b03e_pg_a      # own container + volume + internal network
    python tests/migrate_test_env.py down b03e_pg_a      # remove exactly that container and its volume

Everything is named `b03e_*` and labelled; `down` refuses anything else. The
container has no published ports and no .env, and is only attached to the
internal network `b03e_net`. The clean install (init_clean 00-03, line endings
folded to LF) runs on first start; `edu_ai_tpl` is a pristine copy that tests
use as a template.
"""
from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "b03e_postgres:cs"
NETWORK = "b03e_net"
LABEL = "edu_ai.purpose=b03e-migrate-test"
USER = DB = "edu_ai"
TEMPLATE = "edu_ai_tpl"


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
    if not name.startswith("b03e_"):
        sys.exit(f"refusing: '{name}' does not start with b03e_")


def up(name: str) -> None:
    check_name(name)
    volume = f"{name}_data"
    if exists("container", name) or exists("volume", volume):
        sys.exit(f"{name} or {volume} already exists; run 'down' first")
    if not exists("network", NETWORK):
        docker("network", "create", "--internal", "--label", LABEL, NETWORK)
    if not exists("image", IMAGE):
        with tempfile.TemporaryDirectory() as ctx:
            docker("build", "-f", str(ROOT / "Dockerfile.postgres"), "-t", IMAGE, ctx)
    docker("volume", "create", "--label", LABEL, volume, quiet=True)
    docker("create", "--name", name, "--label", LABEL, "--network", NETWORK,
           "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={secrets.token_hex(12)}",
           "-e", f"POSTGRES_DB={DB}", "-v", f"{volume}:/var/lib/postgresql/data",
           IMAGE, "postgres", "-c", "shared_preload_libraries=pg_search", quiet=True)
    with tempfile.TemporaryDirectory() as tmp:
        for f in sorted((ROOT / "postgres" / "init_clean").glob("*.sql")):
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
    labels = docker("inspect", "--format", "{{json .Config.Labels}}", name, check=False, quiet=True).stdout
    if "b03e-migrate-test" not in labels:
        sys.exit(f"refusing: {name} is not a labelled b03e test container")
    docker("rm", "-f", name, quiet=True)
    docker("volume", "rm", f"{name}_data", quiet=True)
    print(f"removed {name} and {name}_data")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["up", "down"])
    ap.add_argument("name")
    a = ap.parse_args()
    (up if a.action == "up" else down)(a.name)
