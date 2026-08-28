# last change in code by Jan Drozd and Martin Safranek (2026-08-13).
"""Shared helpers for the resilience test harness.

Black-box, host-run: hits the backends on loopback (bypasses the nginx auth
edge — clean), asserts DB state via `docker exec psql`, and injects faults by
stopping/recreating containers. No pytest dependency — stdlib + requests.
"""

import subprocess
import time

import requests

API = "http://127.0.0.1:8080"      # edu_ai_api (loopback, no nginx auth)
SEARCH = "http://127.0.0.1:8090"   # edu_ai_search (loopback)
PG = "edu_ai_postgres"
COMPOSE_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent


# ── HTTP ──────────────────────────────────────────────────────
def search(question, timeout=60, **body):
    body["question"] = question
    return requests.post(f"{SEARCH}/v1/ask", json=body, timeout=timeout)


def ingest_file(path, filename, timeout=300, **form):
    form.setdefault("user_name", "resilience-test")
    with open(path, "rb") as fh:
        return requests.post(f"{API}/v1/ingest", files={"file": (filename, fh)},
                             data=form, timeout=timeout)


# ── DB (via docker exec psql) ─────────────────────────────────
def db(sql):
    """Run SQL, return stdout (unaligned, tuples-only)."""
    r = subprocess.run(
        ["docker", "exec", "-i", PG, "psql", "-U", "edu_ai", "-d", "edu_ai", "-tA", "-c", sql],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return r.stdout.strip()


def db_count(sql_expr, where=""):
    return int(db(f"SELECT count(*) FROM {sql_expr} {where}"))


# ── container / compose control ───────────────────────────────
def dcompose(*args, extra_files=()):
    # ALWAYS include the dev override — once any -f is given, docker compose stops
    # auto-loading docker-compose.override.yml, which is what binds the loopback
    # host ports (127.0.0.1:8090 …). Dropping it leaves recreated containers with
    # no host port → the harness can't reach them.
    files = ["-f", "docker-compose.yml", "-f", "docker-compose.override.yml"]
    files += [x for f in extra_files for x in ("-f", f)]
    return subprocess.run(["docker", "compose", *files, *args],
                          cwd=COMPOSE_DIR, capture_output=True, text=True)


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def wait_http(url, timeout=60, interval=2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(url, timeout=5).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(interval)
    return False


def wait_search_healthy(timeout=60):
    return wait_http(f"{SEARCH}/health", timeout)


def wait_db_ready(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if docker("exec", PG, "pg_isready", "-U", "edu_ai").returncode == 0:
            return True
        time.sleep(2)
    return False
