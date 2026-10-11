"""Run the S2 suite INSIDE a Linux container, against the same Docker daemon (isolated Linux rehearsal).

    python3 tests/nginx_s2/clean_clone.py --baseline <40-hex SHA> --linux rehearsal

The suite and the deployment script then run under real Linux: GNU bash, coreutils, git, Python 3.12 (Ubuntu 24.04), the
Docker CLI and Compose plugin of the official `docker:cli` image, and real POSIX signals. Mechanism:

* executor image: the Ubuntu 24.04 image `mcr.microsoft.com/playwright/python:v1.49.0-noble` (already local; bash, git, python3, GNU tools);
* `docker` and `docker-compose` are copied out of the `docker:cli` image (pulled once, with the owner's consent);
* the Docker socket is mounted: the container drives the host daemon like the host's own CLI does (the lab creates and removes
  only resources named b04s2_<run>_*, exactly as when it runs on the host);
* bind-mount sources are resolved by the DAEMON, so everything the lab mounts lives on a named volume that is mounted into the
  executor at the volume's own Mountpoint path: the same absolute path is valid inside the container and for the daemon.
Nothing of the real checkout is mounted: the executor gets a copy of the clean clone.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import tempfile
import time
import uuid

import labkit as L

EXEC_IMAGE = "mcr.microsoft.com/playwright/python:v1.49.0-noble"
CLI_IMAGE = "docker:cli"


def sh(*cmd, check=True, **kw):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", env=L._env(), **kw)
    if check and p.returncode != 0:
        raise L.LabError(f"{' '.join(str(c) for c in cmd[:4])} ... rc={p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p


def run_in_linux(clone, baseline, commands, out_dir, *, keep=False, compose_binary=None, compose_sha256=None):
    clone, out_dir = pathlib.Path(clone), pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    lab_labels = ["--label", f"edu_ai.purpose={L.PURPOSE}", "--label", f"edu_ai.run={rid}"]
    cli_c, vol, ex = f"b04s2_{rid}_lxcli", f"b04s2_{rid}_lxdata", f"b04s2_{rid}_linux"
    host_tmp = pathlib.Path(tempfile.mkdtemp(prefix="b04s2_lxbin_"))
    ok = True
    try:
        for image in (EXEC_IMAGE, CLI_IMAGE):
            if sh("docker", "image", "inspect", image, check=False).returncode != 0:
                raise L.LabError(f"image {image} is not present locally (docker pull {image})")
        sh("docker", "create", "--name", cli_c, *lab_labels, CLI_IMAGE)
        sh("docker", "cp", f"{cli_c}:/usr/local/bin/docker", host_tmp / "docker")
        sh("docker", "cp", f"{cli_c}:/usr/local/libexec/docker/cli-plugins/docker-compose", host_tmp / "docker-compose")
        sh("docker", "rm", "-f", cli_c)
        if compose_binary:                       # a specific Compose release instead of the one in docker:cli (e.g. the production version)
            data = pathlib.Path(compose_binary).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if compose_sha256 and digest != compose_sha256.lower():
                raise L.LabError(f"{compose_binary}: sha256 {digest} is not the expected {compose_sha256}")
            (host_tmp / "docker-compose").write_bytes(data)
            print(f"using Compose binary {compose_binary} (sha256 {digest})")
        sh("docker", "volume", "create", *lab_labels, vol)
        mp = sh("docker", "volume", "inspect", "--format", "{{.Mountpoint}}", vol).stdout.strip()
        sh("docker", "run", "-d", "--name", ex, *lab_labels, "-v", f"{vol}:{mp}", "-v", "/var/run/docker.sock:/var/run/docker.sock",
           "--entrypoint", "sleep", EXEC_IMAGE, "infinity")
        sh("docker", "exec", ex, "mkdir", "-p", "/usr/local/libexec/docker/cli-plugins", f"{mp}/tmp", f"{mp}/out")
        sh("docker", "cp", host_tmp / "docker", f"{ex}:/usr/local/bin/docker")
        sh("docker", "cp", host_tmp / "docker-compose", f"{ex}:/usr/local/libexec/docker/cli-plugins/docker-compose")
        sh("docker", "exec", ex, "chmod", "+x", "/usr/local/bin/docker", "/usr/local/libexec/docker/cli-plugins/docker-compose")
        # the copy keeps no file modes worth comparing: content only. Identity is only for the throw-away commits of the lab.
        sh("docker", "exec", ex, "bash", "-c",
           "git config --global safe.directory '*' && git config --global core.fileMode false && git config --global core.autocrlf false"
           " && git config --global user.name lab && git config --global user.email lab@example.invalid")
        sh("docker", "cp", clone, f"{ex}:{mp}/clone")
        facts = sh("docker", "exec", ex, "bash", "-c",
                   "echo \"$(uname -sr); $(bash --version | head -1); $(git --version); $(python3 --version); $(docker --version); $(docker compose version)\"").stdout.strip()
        print(f"Linux executor {ex}: {facts}")
        (out_dir / "linux_executor.txt").write_text(facts + "\n", encoding="utf-8", newline="\n")
        for cmd in commands:
            print(f"== (linux) run_s2.py {cmd} --baseline {baseline[:10]}", flush=True)
            forward = [a for k in ("S2_REHEARSAL_ONLY",) if os.environ.get(k) for a in ("-e", f"{k}={os.environ[k]}")]     # debugging aid
            p = subprocess.run(["docker", "exec", "-w", f"{mp}/clone", "-e", f"TMPDIR={mp}/tmp", "-e", "PYTHONUNBUFFERED=1", *forward, ex, "python3",
                                "tests/nginx_s2/run_s2.py", cmd, "--baseline", baseline, "--out", f"{mp}/out/{cmd}.json"], env=L._env())
            ok = ok and p.returncode == 0
        sh("docker", "cp", f"{ex}:{mp}/out/.", out_dir)
        return ok
    finally:
        if keep:
            print(f"executor kept: container {ex}, volume {vol}")
        else:
            sh("docker", "rm", "-f", cli_c, ex, check=False)
            sh("docker", "volume", "rm", "-f", vol, check=False)
        L.rmtree(host_tmp)
