#!/usr/bin/env python3
"""A fake `docker` for the offline tests of the R2 scripts. Standard library only; no daemon.

`docker compose config` is the REAL Compose client (it renders files and needs no daemon; results are cached per input); every other
command is answered from a small JSON "world" (FAKE_DOCKER_WORLD) or refused. Every call is appended to FAKE_DOCKER_CALLS as one JSON
line, marked `mutating` when it would change something (up, rm, tag, network create, nginx reload ...): a refusal test asserts there is none.
An unknown command exits 99, so nothing the scripts start to issue goes unnoticed.

Failure injection (keys of the world): "no_docker" (exit 127 for everything), "daemon_down" (every non-compose command fails like an unreachable daemon), "inspect_fail" {container:
{"rc", "stdout", "stderr"}} (that `docker inspect` answers so), "ps_fail" {"rc", "stdout", "stderr"} (`docker ps` answers so). A container missing
from "containers" is absent: inspect fails with "No such object" and `docker ps -a` does not list it.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

DEFAULT_SMOKE = {"80|/health": 200, "80|/api/health": 200, "80|/search/health": 200, "80|/chat/health": 200, "80|/qa-api/v1/health": 200,
                 "80|/search/v1/query-log": 404, "81|/search/health": 200, "81|/qa/": 404, "81|/api/": 404, "8443|/pgadmin/": 200, "8444|/health": 200}
MUTATING_COMPOSE = {"up", "down", "restart", "rm", "create", "start", "stop", "kill", "pause", "unpause", "run"}


def world():
    return json.loads(pathlib.Path(os.environ["FAKE_DOCKER_WORLD"]).read_text(encoding="utf-8"))


def note(argv, mutating):
    with open(os.environ["FAKE_DOCKER_CALLS"], "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"argv": argv, "mutating": mutating}) + "\n")


def compose(argv):
    sub = next((a for a in argv[1:] if a in MUTATING_COMPOSE | {"config", "ps", "ls", "version"}), None)
    if sub in MUTATING_COMPOSE:
        note(argv, True)
        print("fake docker: compose " + sub + " refused", file=sys.stderr)
        return 1
    note(argv, False)
    if sub == "ps":
        return 0
    if sub != "config":
        print("fake docker: unexpected compose command", file=sys.stderr)
        return 99
    stdin = sys.stdin.buffer.read() if "-" in argv else b""
    key = hashlib.sha256(json.dumps([argv, os.getcwd(), os.environ.get("EDU_AI_TUNNEL_SUBNET", ""), os.environ.get("COMPOSE_FILE", "")]).encode() + stdin).hexdigest()
    cache = pathlib.Path(os.environ["FAKE_DOCKER_CACHE"]) / key
    if cache.exists():
        rc, out, err = json.loads(cache.read_text(encoding="utf-8"))
    else:
        p = subprocess.run([os.environ["REAL_DOCKER"], *argv], input=stdin, capture_output=True)
        rc, out, err = p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([rc, out, err]), encoding="utf-8")
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def fmt_of(argv):
    return argv[argv.index("--format") + 1] if "--format" in argv else ""


def injected(spec):
    sys.stdout.write(spec.get("stdout", ""))
    sys.stderr.write(spec.get("stderr", ""))
    return int(spec.get("rc", 1))


def inspect(argv):
    w, name, fmt = world(), argv[-1], fmt_of(argv)
    if name in w.get("inspect_fail", {}):
        return injected(w["inspect_fail"][name])
    c = w["containers"].get(name)
    if c is None:
        print(f"Error: No such object: {name}", file=sys.stderr)
        return 1
    pre = w["project"] + "_"
    if ".NetworkSettings.Networks" in fmt and "{{range .Mounts}}" in fmt and "|" in fmt:          # the guard's one inspection: networks|mounts
        print(" ".join(pre + n for n in c["networks"]) + (" " if c["networks"] else "") + "|" + " ".join(c.get("mounts", [])) + (" " if c.get("mounts") else ""))
    elif fmt.startswith("{{.State.Status}}|"):
        print(f"{c['status']}|{c.get('health', 'none')}")
    elif fmt == "{{.Image}}":
        print(c["image"])
    elif ".NetworkSettings.Networks" in fmt:
        sep = "\n" if '{{"\\n"}}' in fmt else " "
        sys.stdout.write("".join(pre + n + sep for n in c["networks"]))
        print() if sep == " " else None
    elif "{{range .Mounts}}" in fmt:
        print(" ".join(c.get("mounts", [])) + " ")
    elif fmt == "{{.Id}} {{.State.StartedAt}}":
        print(f"{name}-id 2026-10-04T00:00:00Z")
    elif fmt == "{{.RestartCount}}":
        print(0)
    else:
        print(f"fake docker: inspect format not modelled: {fmt}", file=sys.stderr)
        return 99
    return 0


def image_inspect(argv):
    w, ref, fmt = world(), argv[-1], fmt_of(argv)
    ids = set(w["images"].values())
    ident = w["images"].get(ref, ref if ref in ids else None)
    if ident is None:
        print(f"Error: No such image: {ref}", file=sys.stderr)
        return 1
    if fmt == "{{.Id}}":
        print(ident)
    elif fmt == "{{json .Config.Cmd}}":
        print('["nginx","-g","daemon off;"]')
    elif fmt:
        print(f"fake docker: image inspect format not modelled: {fmt}", file=sys.stderr)
        return 99
    return 0


def main(argv):
    note(argv, False) if argv[:1] != ["compose"] else None
    if not argv:
        return 99
    cmd = argv[0]
    if world().get("no_docker"):
        print("docker: command not found", file=sys.stderr)
        return 127
    if cmd != "compose" and world().get("daemon_down"):
        print("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", file=sys.stderr)
        return 1
    if cmd == "ps":
        w = world()
        if "ps_fail" in w:
            return injected(w["ps_fail"])
        print("\n".join(w["containers"]))
        return 0
    if cmd == "compose":
        return compose(argv)
    if cmd == "inspect":
        return inspect(argv)
    if cmd == "image" and argv[1:2] == ["inspect"]:
        return image_inspect(argv)
    if cmd == "logs":
        sys.stdout.write(world().get("cf_log", ""))
        return 0
    if cmd == "exec":
        rest = argv[2:]
        if rest[:1] == ["wget"]:
            url = rest[-1].split("//", 1)[1]
            port, path = url.split("/", 1)[0].split(":")[1], "/" + url.split("/", 1)[1]
            status = {**DEFAULT_SMOKE, **world().get("smoke", {})}.get(f"{port}|{path}")
            if status is None:
                print("wget: server returned error: not modelled", file=sys.stderr)
                return 1
            print(f"  HTTP/1.1 {status} X", file=sys.stderr)
            return 0 if status < 400 else 8
        if rest[:1] == ["cat"]:
            text = world()["containers"][argv[1]].get("trust_conf")
            if text is None:
                return 1
            print(text)
            return 0
        if rest[:2] == ["nginx", "-s"]:
            note(argv, True)
            return 1
        print("fake docker: exec not modelled", file=sys.stderr)
        return 1
    if cmd == "run" and "nginx" in argv and "-t" in argv and "--rm" in argv:
        print("nginx: configuration file /etc/nginx/nginx.conf test is successful")
        return 0
    if cmd in ("tag", "rm", "rmi", "network", "volume", "start", "stop", "kill"):
        if cmd != "network" or argv[1] in ("create", "rm", "connect", "disconnect"):
            note(argv, True)
        return 1
    print(f"fake docker: command not modelled: {' '.join(argv[:3])}", file=sys.stderr)
    return 99


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
