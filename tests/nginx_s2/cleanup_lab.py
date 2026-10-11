"""Remove what an INTERRUPTED lab run left behind: only resources of ONE run id, found by the run's name prefix
`b04s2_<run>_` (Compose projects of the lab carry no labels) or by the label edu_ai.run=<run>.

    python tests/nginx_s2/cleanup_lab.py                 # lists the run ids that have resources, removes nothing
    python tests/nginx_s2/cleanup_lab.py <run id>        # lists and removes that run's containers, networks, volumes

A normal run cleans up after itself and reports anything it could not remove.
"""
import re
import subprocess
import sys


def docker(*args, check=False):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        sys.exit(f"docker {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.split()


def found(run):
    out = {}
    for kind, ls in (("containers", ["ps", "-a", "--format", "{{.Names}}"]), ("networks", ["network", "ls", "--format", "{{.Name}}"]),
                     ("volumes", ["volume", "ls", "--format", "{{.Name}}"]),
                     ("images", ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])):
        out[kind] = sorted(n for n in docker(*ls) if n.startswith(f"b04s2_{run}_"))
    return out


def main():
    if len(sys.argv) == 1:
        names = (docker("ps", "-a", "--format", "{{.Names}}") + docker("network", "ls", "--format", "{{.Name}}") + docker("volume", "ls", "--format", "{{.Name}}")
                 + docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}"))
        runs = sorted({m.group(1) for n in names if (m := re.match(r"b04s2_([0-9a-f]+)_", n))})
        print("run ids with resources:", runs or "none")
        return
    run = sys.argv[1]
    if not re.fullmatch(r"[0-9a-f]{6,16}", run):
        sys.exit("run id looks wrong")
    res = found(run)
    for kind, names in res.items():
        print(kind, names)
    if res["containers"]:
        docker("rm", "-f", *res["containers"])
    if res["networks"]:
        docker("network", "rm", *res["networks"])
    if res["volumes"]:
        docker("volume", "rm", *res["volumes"])
    if res["images"]:
        docker("rmi", *res["images"])               # tags only
    left = {k: v for k, v in found(run).items() if v}
    print("left:", left or "nothing")
    sys.exit(1 if left else 0)


if __name__ == "__main__":
    main()
