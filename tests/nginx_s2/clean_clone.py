"""Run the S2 suite from a CLEAN CLONE that holds only the explicit candidate files (S2-R4).

    python3 tests/nginx_s2/clean_clone.py --baseline <40-hex SHA> [--out DIR] [--keep] [command ...]

What it does, in order, and stops at the first failure to even start:
  1. `--baseline` must be a full SHA that is a commit here and equals this checkout's HEAD: the candidate is
     the working tree ON TOP of that commit, nothing else.
  2. A fresh `git clone` of this repository is checked out at that SHA (detached, clean).
  3. ONLY the candidate files are copied in: the modified tracked files plus the untracked files under
     nginx/conf, tests/nginx_s2 and scripts/nginx_s2_switch.sh. Untracked planning documents, QA exports and
     anything else in the working tree are NOT carried over, so a test that depends on them fails here.
     `git status` of the clone must list exactly those files.
  4. `git diff --check` over the candidate (staged in the CLONE only), then docs-check, then the suite
     (default command: all) run from the clone with the explicit baseline.
Nothing is committed, pushed or staged in the real repository; the clone lives in a temp directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

if sys.version_info < (3, 10):
    sys.exit("tests/nginx_s2 needs Python >= 3.10")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rehearsal as RH  # noqa: E402  (candidate_paths)
from labkit import REPO, rmtree  # noqa: E402


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {cwd}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def verify_patches(baseline, patches, files):
    """The exported patches, applied with plain `git am` to a FRESH clone at the baseline, give exactly the candidate files
    (byte for byte after LF normalisation), as two commits authored by this checkout's git identity."""
    patches = pathlib.Path(patches).resolve()
    names = sorted(x.name for x in patches.glob("*.patch"))
    assert len(names) == 2, f"expected two patches, found {names}"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="b04s2_am_"))
    try:
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(REPO), str(tmp / "c")], check=True)
        c = tmp / "c"
        git(c, "config", "core.autocrlf", "false")          # a system-level autocrlf (Git for Windows) would rewrite the files `git am` writes
        git(c, "checkout", "-q", "--detach", baseline)
        ident =["-c", "user.name=applier", "-c", "user.email=applier@example.invalid", "-c", "commit.gpgsign=false"]
        p = subprocess.run(["git", "-C", str(c), *ident, "am", *[str(patches / n) for n in names]], capture_output=True, text=True)
        assert p.returncode == 0, f"git am failed: {(p.stderr or p.stdout)[-400:]}"
        changed = sorted(git(c, "diff", "--name-only", baseline, "HEAD").stdout.split())
        assert changed == sorted(files), f"the patches change {sorted(set(changed) ^ set(files))} differently from the candidate list"
        for rel in files:        # the COMMITTED content, byte for byte (not the working copy, which a system eol setting may rewrite)
            blob = subprocess.run(["git", "-C", str(c), "show", f"HEAD:{rel}"], capture_output=True, check=True).stdout
            assert blob == (REPO / rel).read_bytes().replace(b"\r\n", b"\n"), f"{rel} differs from the working tree"
        who = [git(REPO, "config", k).stdout.strip() for k in ("user.name", "user.email")]
        log = git(c, "log", "-2", "--format=%an <%ae>%n%B", baseline + "..HEAD").stdout
        assert f"{who[0]} <{who[1]}>" in log, "the patches are not authored by this checkout's git identity"
        assert log.count("Co-Authored-By:") == 2, "both commit messages must carry the Co-Authored-By trailer"
        return f"git am of {', '.join(names)}: {len(files)} files identical to the working tree; two commits authored by {who[0]}"
    finally:
        rmtree(tmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="the full 40-hex SHA the candidate sits on")
    ap.add_argument("--out", default=None, help="directory for the results (default: tests/eval_out/s2_clean_<time>)")
    ap.add_argument("--keep", action="store_true", help="keep the clone")
    ap.add_argument("--linux", action="store_true",
                    help="run the suite inside a Linux container (real bash/coreutils/signals) instead of on this host; see linuxexec.py")
    ap.add_argument("--compose-binary", default=None, help="with --linux: use this docker-compose Linux binary instead of the one in docker:cli")
    ap.add_argument("--compose-sha256", default=None, help="with --compose-binary: the expected sha256 of that file")
    ap.add_argument("--patches", default=None, help="export the two rehearsal commits (tools, S2) as patches into this directory")
    ap.add_argument("commands", nargs="*", default=["all"])
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.baseline):
        sys.exit("--baseline must be a full 40-hex SHA, not a ref or an abbreviation")
    full = git(REPO, "rev-parse", "--verify", f"{args.baseline}^{{commit}}").stdout.strip()
    head = git(REPO, "rev-parse", "HEAD").stdout.strip()
    if full != args.baseline or head != args.baseline:
        sys.exit(f"the candidate must sit on the baseline: HEAD {head[:10]}, --baseline {args.baseline[:10]}")
    tools, s2 = RH.candidate_paths()
    files = sorted(tools + s2)
    out = pathlib.Path(args.out or REPO / "tests" / "eval_out" / time.strftime("s2_clean_%Y%m%d_%H%M%S")).resolve()   # the suite runs with cwd = the clone
    out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="b04s2_clean_"))
    clone = tmp / "clone"
    steps = []
    try:
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)], check=True)
        git(clone, "config", "core.autocrlf", "false")
        git(clone, "checkout", "-q", "--detach", args.baseline)
        assert git(clone, "status", "--porcelain").stdout == "", "the fresh clone is not clean"
        for rel in files:
            dst = clone / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((REPO / rel).read_bytes().replace(b"\r\n", b"\n"))
        status = sorted(x[3:] for x in git(clone, "status", "--porcelain", "--untracked-files=all").stdout.splitlines())
        assert status == files, f"the clone differs from the candidate list: {sorted(set(status) ^ set(files))}"
        print(f"clean clone at {args.baseline[:10]}: {len(files)} candidate files ({len(tools)} tools, {len(s2)} S2); nothing else carried over")

        def step(name, cmd, **kw):
            t0 = time.time()
            print(f"== {name}")
            p = subprocess.run(cmd, cwd=str(clone), **kw)
            steps.append({"step": name, "rc": p.returncode, "seconds": round(time.time() - t0, 1)})
            return p.returncode == 0

        git(clone, "add", "-A")
        ok = step("git diff --check (candidate staged in the CLONE)", ["git", "diff", "--cached", "--check", args.baseline])
        ok = step("docs-check (python3 docs/tools/docs_check.py)", [sys.executable, "docs/tools/docs_check.py"]) and ok
        git(clone, "reset", "-q")              # the clone is throw-away: unstage so the suite sees the candidate as a working-tree change
        env = dict(os.environ)
        if args.patches:
            who = [git(REPO, "config", k).stdout.strip() for k in ("user.name", "user.email")]
            env["S2_EXPORT_PATCHES"] = str(pathlib.Path(args.patches).resolve())
            env["S2_PATCH_AUTHOR"] = f"{who[0]} <{who[1]}>"       # the clone has no git identity of its own
        if args.linux:
            import linuxexec  # noqa: PLC0415
            t0 = time.time()
            good = linuxexec.run_in_linux(clone, args.baseline, args.commands, out / "linux", keep=args.keep,
                                           compose_binary=args.compose_binary, compose_sha256=args.compose_sha256)
            steps.append({"step": f"LINUX run_s2.py {' '.join(args.commands)} --baseline {args.baseline[:10]}", "rc": 0 if good else 1,
                          "seconds": round(time.time() - t0, 1)})
            ok = good and ok
            args.commands = []
        for cmd in args.commands:
            ok = step(f"run_s2.py {cmd} --baseline {args.baseline[:10]}",
                      [sys.executable, "tests/nginx_s2/run_s2.py", cmd, "--baseline", args.baseline, "--out", str(out / f"{cmd}.json")], env=env) and ok
        if args.patches and not args.linux:
            t0 = time.time()
            try:
                print("== patches: " + verify_patches(args.baseline, args.patches, files))
                good = True
            except AssertionError as e:
                print(f"== patches: FAILED - {e}")
                good = False
            steps.append({"step": "git am of the exported patches onto a fresh clone", "rc": 0 if good else 1, "seconds": round(time.time() - t0, 1)})
            ok = good and ok
        digests = {rel: hashlib.sha256((REPO / rel).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for rel in files}
        (out / "clean_clone.json").write_text(json.dumps({"baseline": args.baseline, "files": files, "sha256_lf": digests, "steps": steps}, indent=1),
                                              encoding="utf-8")
        print("clean clone steps:", json.dumps(steps))
        sys.exit(0 if ok else 1)
    finally:
        if args.keep:
            print(f"clone kept at {clone}")
        else:
            rmtree(tmp)             # git objects are read-only on Windows: a plain rmtree would leave the clone behind


if __name__ == "__main__":
    main()
