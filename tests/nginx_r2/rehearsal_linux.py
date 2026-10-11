#!/usr/bin/env python3
"""Run the R2 rollout rehearsal inside an isolated Linux container. Standard library only; needs Docker.

    python tests/nginx_r2/rehearsal_linux.py --compose-binary FILE --compose-sha256 HEX --out DIR [--only a,b]        # candidate = working tree
    python tests/nginx_r2/rehearsal_linux.py --compose-binary FILE --compose-sha256 HEX --out DIR \
        --tools SHA --step1 SHA --step2 SHA --step3 SHA                                                              # candidate = real commits

The rehearsal itself (rehearsal.py) runs under Ubuntu 24.04 (GNU bash, coreutils, git, Python 3.12, real POSIX signals) with the Docker
CLI and the Compose plugin of a given release (the production one, 5.3.1, checked by sha256) against the SAME Docker daemon, through
its socket. Everything it creates is a Compose project named b04r2i_<run>_<server> or carries the labels edu_ai.purpose / edu_ai.run.

What is rehearsed is a throw-away "release" repository: the deployed S2 commit and on top of it the tools and the three step commits.
With --tools/--step* those are the REAL commits of this repository (their SHAs and trees are recorded); without them they are built
from the working tree exactly the way the real commits are (candidate.py step content + the tool files). Either way candidate.py
`verify` first proves that the step commits are byte for byte the derived transition / transition2 / candidate files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests" / "nginx_s2"))
sys.path.insert(0, str(HERE))
import candidate as C  # noqa: E402
import labkit as L  # noqa: E402
from linuxexec import CLI_IMAGE, EXEC_IMAGE, sh  # noqa: E402

TOOLS_FILES = ("scripts/nginx_r2_switch.sh", "scripts/tunnel_subnet.py", "scripts/nginx_s2_guard.sh")        # + every file of tests/nginx_r2 and the S2 helpers it uses
HARNESS_DIRS = ("tests/nginx_r2", "tests/nginx_s2")
PURPOSE = "b04r2-rehearsal"


def git(cwd, *args, check=True, env=None):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def lf(path: pathlib.Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def build_release(dest: pathlib.Path, shas: dict | None) -> dict:
    """The throw-away release repository. Branches: s2, tools, step1, step2, step3 (and nothing else)."""
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(REPO), str(dest)], check=True)
    for k, v in (("user.name", "rehearsal"), ("user.email", "rehearsal@example.invalid"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        git(dest, "config", k, v)
    out = {}
    git(dest, "checkout", "-q", "-B", "s2", C.S2_BASE)
    if shas:
        for name in ("tools", "step1", "step2", "step3"):
            git(dest, "branch", "-f", name, shas[name])
        out = {k: git(dest, "rev-parse", v).stdout.strip() for k, v in shas.items()}
        # the step commits form a chain on the deployed S2 commit; the tools commit is where the operator's copies of the scripts come from: it may be
        # the one in front of step 1 (the first candidate) or a later one (a fixed script), so it only has to descend from S2
        problems = [f"{a}: the commit is not an ancestor of {b}" for a, b in (("s2", "tools"), ("s2", "step1"), ("step1", "step2"), ("step2", "step3"))
                    if git(dest, "merge-base", "--is-ancestor", {"s2": C.S2_BASE}.get(a, a), b, check=False).returncode != 0]
        if problems:
            raise SystemExit("; ".join(problems))
    else:
        git(dest, "checkout", "-q", "-B", "tools", "s2")
        for path in TOOLS_FILES:
            (dest / path).parent.mkdir(parents=True, exist_ok=True)
            (dest / path).write_bytes(lf(REPO / path))
        for d in HARNESS_DIRS:
            for f in sorted((REPO / d).iterdir()):
                if f.is_file() and f.suffix in (".py", ".md", ".sh", ""):
                    (dest / d).mkdir(parents=True, exist_ok=True)
                    (dest / d / f.name).write_bytes(lf(f))
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", "tools: R2 rollout script, preflight tool and harness (rehearsal build)")
        prev = "tools"
        for n in (1, 2, 3):
            git(dest, "checkout", "-q", "-B", f"step{n}", prev)
            for path, data in C.step_content(n).items():
                (dest / path).parent.mkdir(parents=True, exist_ok=True)
                (dest / path).write_bytes(data)
            git(dest, "add", "-A")
            git(dest, "commit", "-q", "-m", f"R2 step {n} (rehearsal build)")
            prev = f"step{n}"
        out = {k: git(dest, "rev-parse", k).stdout.strip() for k in ("tools", "step1", "step2", "step3")}
    out["s2"] = git(dest, "rev-parse", "s2").stdout.strip()
    git(dest, "checkout", "-q", "s2")
    return out


def run_in_linux(release, args, out_dir, *, compose_binary, compose_sha256, keep=False):
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    lab = ["--label", f"edu_ai.purpose={PURPOSE}", "--label", f"edu_ai.run={rid}"]
    cli_c, vol, ex = f"b04r2x_{rid}_lxcli", f"b04r2x_{rid}_lxdata", f"b04r2x_{rid}_linux"
    host_tmp = pathlib.Path(tempfile.mkdtemp(prefix="b04r2x_lxbin_"))
    ok = False
    try:
        for image in (EXEC_IMAGE, CLI_IMAGE):
            if sh("docker", "image", "inspect", image, check=False).returncode != 0:
                raise L.LabError(f"image {image} is not present locally")
        sh("docker", "create", "--name", cli_c, *lab, CLI_IMAGE)
        sh("docker", "cp", f"{cli_c}:/usr/local/bin/docker", host_tmp / "docker")
        sh("docker", "rm", "-f", cli_c)
        data = pathlib.Path(compose_binary).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != compose_sha256.lower():
            raise L.LabError(f"{compose_binary}: sha256 {digest} is not the expected {compose_sha256}")
        (host_tmp / "docker-compose").write_bytes(data)
        print(f"using Compose binary {compose_binary} (sha256 {digest})")
        sh("docker", "volume", "create", *lab, vol)
        mp = sh("docker", "volume", "inspect", "--format", "{{.Mountpoint}}", vol).stdout.strip()
        # NET_ADMIN only inside this container's own network namespace: the host routes are never touched
        sh("docker", "run", "-d", "--name", ex, *lab, "-v", f"{vol}:{mp}", "-v", "/var/run/docker.sock:/var/run/docker.sock",
           "--entrypoint", "sleep", EXEC_IMAGE, "infinity")
        sh("docker", "exec", ex, "mkdir", "-p", "/usr/local/libexec/docker/cli-plugins", f"{mp}/tmp", f"{mp}/out", f"{mp}/h")
        sh("docker", "cp", host_tmp / "docker", f"{ex}:/usr/local/bin/docker")
        sh("docker", "cp", host_tmp / "docker-compose", f"{ex}:/usr/local/libexec/docker/cli-plugins/docker-compose")
        sh("docker", "exec", ex, "chmod", "+x", "/usr/local/bin/docker", "/usr/local/libexec/docker/cli-plugins/docker-compose")
        sh("docker", "exec", ex, "bash", "-c", "git config --global safe.directory '*' && git config --global core.fileMode false && git config --global core.autocrlf false"
           " && git config --global user.name rehearsal && git config --global user.email rehearsal@example.invalid")
        sh("docker", "cp", release, f"{ex}:{mp}/release")
        for d in HARNESS_DIRS + ("scripts",):          # the harness code that is being tested, not a checkout
            sh("docker", "exec", ex, "mkdir", "-p", f"{mp}/h/{d}")
            for f in sorted((REPO / d).iterdir()):
                if f.is_file():
                    tmp = host_tmp / f.name
                    tmp.write_bytes(lf(f))
                    sh("docker", "cp", tmp, f"{ex}:{mp}/h/{d}/{f.name}")
        facts = sh("docker", "exec", ex, "bash", "-c", "echo \"$(uname -sr); $(bash --version | head -1); $(git --version); $(python3 --version); "
                   "$(docker --version); $(docker compose version)\"").stdout.strip()
        print(f"Linux executor {ex}: {facts}")
        (out_dir / "linux_executor.txt").write_text(facts + "\n", encoding="utf-8", newline="\n")
        cmd = ["docker", "exec", "-w", f"{mp}/h", "-e", f"TMPDIR={mp}/tmp", "-e", "PYTHONUNBUFFERED=1", ex,
               "python3", "tests/nginx_r2/rehearsal.py", "run", "--release", f"{mp}/release", "--out", f"{mp}/out/rehearsal.json", *args]
        p = subprocess.run(cmd, env=L._env())
        ok = p.returncode == 0
        sh("docker", "cp", f"{ex}:{mp}/out/.", out_dir, check=False)
        return ok
    finally:
        if keep:
            print(f"executor kept: container {ex}, volume {vol}")
        else:
            sh("docker", "rm", "-f", cli_c, ex, check=False)
            sh("docker", "volume", "rm", "-f", vol, check=False)
        L.rmtree(host_tmp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compose-binary", required=True)
    ap.add_argument("--compose-sha256", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="comma separated scenario names (default: all)")
    ap.add_argument("--keep", action="store_true")
    for k in ("tools", "step1", "step2", "step3"):
        ap.add_argument(f"--{k}", default=None)
    args = ap.parse_args(argv)
    given = [getattr(args, k) for k in ("tools", "step1", "step2", "step3")]
    if any(given) and not all(given):
        sys.exit("--tools, --step1, --step2 and --step3 go together")
    shas = dict(zip(("tools", "step1", "step2", "step3"), given)) if all(given) else None
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="b04r2x_rel_"))
    try:
        release = tmp / "release"
        info = build_release(release, shas)
        print("release:", json.dumps(info))
        if shas:      # the real commits must be the derived files, byte for byte
            p = subprocess.run([sys.executable, str(HERE / "candidate.py"), "verify", "--repo", str(REPO), "--step1", info["step1"], "--step2", info["step2"], "--step3", info["step3"]])
            if p.returncode != 0:
                return 1
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "release.json").write_text(json.dumps({"mode": "real commits" if shas else "working tree", **info}, indent=1), encoding="utf-8")
        ok = run_in_linux(release, (["--only", args.only] if args.only else []), out, compose_binary=args.compose_binary,
                          compose_sha256=args.compose_sha256, keep=args.keep)
        return 0 if ok else 1
    finally:
        L.rmtree(tmp)


if __name__ == "__main__":
    sys.exit(main())
