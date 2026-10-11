#!/usr/bin/env python3
"""R2 proposal, derived from the ACTIVE files at a git ref. Standard library only, no Docker.

The proposal is never a hand-edited copy: every file is produced from the committed baseline by exact, counted text
replacements (a replacement that does not match exactly once is an error), so the lab cannot drift from what production runs
and the diff is the whole change.

The trusted network's CIDR is NOT a constant of any file. It is the installation's own setting EDU_AI_TUNNEL_SUBNET: Compose
takes the subnet of the `tunnel` network and the trust range of nginx from that ONE variable (the range reaches nginx as an
inline Compose `config` mounted at /etc/edu_ai/tunnel/trust.conf, which nginx.conf includes). With the variable unset Compose
refuses to render the model; with the file absent nginx trusts nobody.

    python tests/nginx_r2/candidate.py render --out DIR [--ref REF]
    python tests/nginx_r2/candidate.py check [--ref REF]
    python tests/nginx_r2/candidate.py apply --step 1|2|3 [--ref REF] [--to DIR]      write the files of one rollout step
    python tests/nginx_r2/candidate.py verify [--ref REF] --repo PATH --step1 SHA --step2 SHA --step3 SHA

REF is the DEPLOYED S2 commit (default S2_BASE), not HEAD: the implementation commits sit on top of it, so the derivation and the
committed files are one definition. `verify` proves that the three step commits hold byte for byte what is derived here.
Nothing here touches a running stack; `apply` writes only into --to (default: this checkout's working tree).
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
S2_BASE = "af5f4910396ab6d16a42419ce4d306329acf1d65"      # the S2 commit that runs on production; the files below are derived from it

SUBNET_VAR = "EDU_AI_TUNNEL_SUBNET"
CONFIG_TARGET = "/etc/edu_ai/tunnel/trust.conf"
INCLUDE_LINE = "include /etc/edu_ai/tunnel/*.conf;"
OLD_TRUST_LINE = "set_real_ip_from 172.16.0.0/12;"
XFF_OLD = "proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;"
XFF_NEW = "proxy_set_header X-Forwarded-For   $remote_addr;"
# The wrong constants of the two mutants that keep a second, independent copy of the range (the checks must notice them).
MUTANT_LITERAL = "10.250.250.0/29"

FILES = ("docker-compose.yml", "nginx/conf/nginx.conf", "nginx/conf/search_api.conf", "nginx/conf/mime.types")


def git_show(ref: str, path: str) -> bytes:
    p = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{path}"], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"git show {ref}:{path} failed: {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout


def baseline(ref: str = S2_BASE) -> dict[str, bytes]:
    """The committed files, byte for byte (the blobs are LF; the working tree may be CRLF)."""
    return {f: git_show(ref, f) for f in FILES}


def replace_once(text: str, old: str, new: str, what: str) -> str:
    n = text.count(old)
    if n != 1:
        raise ValueError(f"{what}: expected exactly one match, found {n}")
    return text.replace(old, new)


def replace_n(text: str, old: str, new: str, count: int, what: str) -> str:
    n = text.count(old)
    if n != count:
        raise ValueError(f"{what}: expected {count} matches, found {n}")
    return text.replace(old, new)


def insert_before_line(text: str, prefix: str, new: str, what: str) -> str:
    lines = text.splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    if len(hits) != 1:
        raise ValueError(f"{what}: expected exactly one line starting with the anchor, found {len(hits)}")
    lines.insert(hits[0], new)
    return "".join(lines)


# ── nginx.conf ──────────────────────────────────────────────────────────
OLD_TRUST_BLOCK = (
    "        # Trusting the header is only safe where the peer cannot be anyone: port\n"
    "        # 81 is NOT published to the host, so the only thing that can connect is\n"
    "        # a container on the docker networks — hence the private range here, and\n"
    "        # deliberately NOT on port 80, which has no trustworthy proxy in front.\n"
    "        set_real_ip_from 172.16.0.0/12;\n"
)

TRUST_COMMENT = (
    "        # Trusting the header is only safe where the peer cannot be anyone. A private\n"
    "        # range, a service name or the header itself prove nothing: every container on\n"
    "        # `edge` (the API that fetches user URLs, Langflow, ...) can reach this port.\n"
    "        # The trusted peer is therefore defined by NETWORK MEMBERSHIP: `tunnel` is a\n"
    "        # dedicated small subnet that only nginx and cloudflared are attached to. Its\n"
    "        # CIDR is the installation's own setting (EDU_AI_TUNNEL_SUBNET); Compose gives\n"
    "        # the SAME value to the network and, as an inline config mounted below, to the\n"
    "        # trust range, so it is written nowhere in this file. No such file, no trusted\n"
    "        # peer. A peer on any other network, a proxy left on `edge` included, is NOT\n"
    "        # trusted: its header is ignored and the rate limit counts the real peer, never\n"
    "        # a forged address. Deliberately NOT on port 80, which has no trustworthy proxy.\n"
)


def trust_block(mode: str) -> str:
    """mode: include | legacy+include | none | literal:<cidr>"""
    if mode == "include":
        body = f"        {INCLUDE_LINE}\n"
    elif mode == "legacy+include":
        body = f"        {OLD_TRUST_LINE}      # rollout step 1 and 2 only: removed in step 3\n        {INCLUDE_LINE}\n"
    elif mode == "none":
        body = ""
    elif mode.startswith("literal:"):
        body = f"        set_real_ip_from {mode.split(':', 1)[1]};\n"
    else:
        raise ValueError(mode)
    return TRUST_COMMENT + body


XFF_NOTE = (
    "    # ── Forwarded client address ──────────────────────\n"
    "    # Every proxy_set_header X-Forwarded-For below sends $remote_addr ALONE: nginx's own\n"
    "    # view of the client (the CF-Connecting-IP value for a trusted tunnel peer, otherwise\n"
    "    # the TCP peer). A chain supplied by the caller is dropped, not appended to\n"
    "    # ($proxy_add_x_forwarded_for), so no leftmost entry is client-controlled.\n"
    "\n"
)
LOG_ANCHOR_PREFIX = "    # ── Name resolution "


def derive_nginx_conf(text: str, *, trust="include", xff_overwrite=True, note=True) -> str:
    text = replace_once(text, OLD_TRUST_BLOCK, trust_block(trust), "trust block")
    if xff_overwrite:
        text = replace_n(text, XFF_OLD, XFF_NEW, 6, "X-Forwarded-For in nginx.conf")
        if note:
            text = insert_before_line(text, LOG_ANCHOR_PREFIX, XFF_NOTE, "Forwarded address note anchor")
    return text


def derive_search_api(text: str, *, xff_overwrite=True) -> str:
    if xff_overwrite:
        text = replace_once(text, XFF_OLD, XFF_NEW, "X-Forwarded-For in search_api.conf")
    return text


# ── docker-compose.yml ──────────────────────────────────────────────────
NGINX_NET_OLD = "      - edge            # edge ONLY — cannot reach the data tier\n"
NGINX_NET_NEW = (
    "      edge: {}          # the upstreams; cannot reach the data tier\n"
    "      tunnel:           # the tunnel hop: shared ONLY with cloudflared (R2)\n"
    "        aliases:\n"
    "          # The tunnel's public hostname targets http://edu_ai_nginx:81. The alias keeps that name valid here\n"
    "          # whatever container_name says (cloudflared is no longer on `edge`).\n"
    "          - edu_ai_nginx\n"
    "    configs:\n"
    f"      # R2: the range nginx trusts for CF-Connecting-IP. Same variable as the subnet of `tunnel`.\n"
    "      - source: tunnel_trust\n"
    f"        target: {CONFIG_TARGET}\n"
)
CF_NET_OLD = (
    "      - app\n"
    "      # `app` is internal:true and has no route out, so without `edge` cloudflared\n"
    "      # cannot reach Cloudflare at all and the tunnel never registers.\n"
    "      - edge\n"
)
CF_NET_NEW = (
    "      - app\n"
    "      # The ONLY network it shares with nginx. nginx trusts CF-Connecting-IP from this\n"
    "      # network and from nothing else (R2). The tunnel's public hostname keeps pointing at\n"
    "      # http://edu_ai_nginx:81: the name resolves here to nginx's `tunnel` address only,\n"
    "      # because cloudflared is no longer on `edge`.\n"
    "      - tunnel\n"
    "      # `app` and `tunnel` are internal:true and have no route out, so cloudflared needs a\n"
    "      # network of its own to reach Cloudflare. Nothing else is on it.\n"
    "      - tunnel_egress\n"
)
NETS_OLD = (
    "  bifrost_egress:\n"
    "    driver: bridge                   # gateway → LLM providers; `app` alone is\n"
    "                                     # internal, and Bifrost dies at startup if\n"
    "                                     # it cannot fetch its pricing data.\n"
)


def nets_new(content_literal: str | None) -> str:
    trust_value = content_literal or f"${{{SUBNET_VAR}}}"
    return NETS_OLD + (
        "  tunnel:\n"
        "    driver: bridge\n"
        "    internal: true                   # nginx <-> cloudflared only; the trust boundary of R2\n"
        "    ipam:\n"
        "      config:\n"
        f"        # One setting per installation, never shared and never a constant here. It is allocated and\n"
        f"        # checked by the subnet preflight (no overlap with host routes, Docker networks or pools).\n"
        f"        - subnet: ${{{SUBNET_VAR}:?{SUBNET_VAR} is not set; run the subnet preflight}}\n"
        "  tunnel_egress:\n"
        "    driver: bridge                   # cloudflared -> Cloudflare; no other member\n"
        "\n"
        "# The trust range of nginx for CF-Connecting-IP: the SAME variable as the subnet of `tunnel`.\n"
        "configs:\n"
        "  tunnel_trust:\n"
        "    content: |\n"
        f"      set_real_ip_from {trust_value};\n"
    )


HEADER_NETS_OLD = (
    "#    bifrost_egress – LLM gateway → providers. Its own network, NOT edge:\n"
    "#                     nginx lives on edge and has no business reaching Bifrost.\n"
)
HEADER_NETS_NEW = HEADER_NETS_OLD + (
    "#    tunnel         – nginx <-> cloudflared ONLY (internal): the trust boundary of\n"
    "#                     CF-Connecting-IP; its subnet is the setting EDU_AI_TUNNEL_SUBNET (R2).\n"
    "#    tunnel_egress  – cloudflared -> Cloudflare ONLY.\n"
)
HEADER_NGINX_OLD = "#  nginx is on `edge` ONLY → it cannot reach Postgres directly.\n"
HEADER_NGINX_NEW = "#  nginx is on `edge` and `tunnel` only → it cannot reach Postgres directly.\n"


def derive_compose(text: str, *, content_literal: str | None = None) -> str:
    text = replace_once(text, HEADER_NETS_OLD, HEADER_NETS_NEW, "header: networks")
    text = replace_once(text, HEADER_NGINX_OLD, HEADER_NGINX_NEW, "header: nginx networks")
    text = replace_once(text, NGINX_NET_OLD, NGINX_NET_NEW, "nginx networks")
    text = replace_once(text, CF_NET_OLD, CF_NET_NEW, "cloudflared networks")
    text = replace_once(text, NETS_OLD, nets_new(content_literal), "networks section")
    return text


# ── the variants the lab runs ───────────────────────────────────────────
VARIANT_NAMES = ("baseline", "candidate", "transition", "transition2", "mut_broad", "mut_notrust", "mut_xff",
                 "mut_literal", "mut_cidr")


def variants(ref: str = S2_BASE) -> dict[str, dict[str, bytes]]:
    """name -> {path: bytes}. All derived from the baseline at `ref`.

    baseline     the files production runs
    candidate    the proposal: tunnel network, trust only from the configured subnet, X-Forwarded-For = $remote_addr
    transition   rollout step 1: the include is in place (nothing mounted yet), the old range still trusted, compose still old
    transition2  rollout step 2: compose moved (network + config), the old range still trusted besides the new one
    mut_*        deliberately wrong proposals; the checks must notice each one (sensitivity):
                 mut_broad    the old broad range is trusted again
                 mut_notrust  no trust at all
                 mut_xff      the caller's X-Forwarded-For chain is appended to again
                 mut_literal  nginx.conf keeps its OWN copy of the range (a second, independent constant)
                 mut_cidr     the Compose config carries a different literal than the network's variable
    """
    base = baseline(ref)
    txt = {k: v.decode("utf-8") for k, v in base.items() if k != "nginx/conf/mime.types"}
    mime = base["nginx/conf/mime.types"]

    def mk(*, trust, xff=True, compose=True, content_literal=None) -> dict[str, bytes]:
        conf = derive_nginx_conf(txt["nginx/conf/nginx.conf"], trust=trust, xff_overwrite=xff)
        sapi = derive_search_api(txt["nginx/conf/search_api.conf"], xff_overwrite=xff)
        comp = derive_compose(txt["docker-compose.yml"], content_literal=content_literal) if compose else txt["docker-compose.yml"]
        return {"docker-compose.yml": comp.encode(), "nginx/conf/nginx.conf": conf.encode(),
                "nginx/conf/search_api.conf": sapi.encode(), "nginx/conf/mime.types": mime}

    out = {"baseline": dict(base)}
    out["candidate"] = mk(trust="include")
    out["transition"] = mk(trust="legacy+include", compose=False)
    out["transition2"] = mk(trust="legacy+include")
    out["mut_broad"] = mk(trust="legacy+include")
    out["mut_notrust"] = mk(trust="none")
    out["mut_xff"] = mk(trust="include", xff=False)
    out["mut_literal"] = mk(trust=f"literal:{MUTANT_LITERAL}")
    out["mut_cidr"] = mk(trust="include", content_literal=MUTANT_LITERAL)
    return out


def uses_subnet_variable(files: dict[str, bytes]) -> bool:
    return SUBNET_VAR.encode() in files["docker-compose.yml"]


def unified(old: bytes, new: bytes, path: str) -> str:
    a = old.decode("utf-8").splitlines(keepends=True)
    b = new.decode("utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))


def render_diffs(ref: str = S2_BASE) -> dict[str, str]:
    base, cand = baseline(ref), variants(ref)["candidate"]
    return {"r2-compose.diff": unified(base["docker-compose.yml"], cand["docker-compose.yml"], "docker-compose.yml"),
            "r2-nginx.diff": unified(base["nginx/conf/nginx.conf"], cand["nginx/conf/nginx.conf"], "nginx/conf/nginx.conf")
            + unified(base["nginx/conf/search_api.conf"], cand["nginx/conf/search_api.conf"], "nginx/conf/search_api.conf")}


def cmd_render(args) -> int:
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in render_diffs(args.ref).items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    for path, data in variants(args.ref)["candidate"].items():
        dest = out / "candidate" / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    print(f"rendered the proposal derived from {args.ref} into {out}")
    return 0


def directives(text: str) -> str:
    """The text without comment lines (comments may mention a directive)."""
    return "".join(l for l in text.splitlines(keepends=True) if not l.lstrip().startswith("#"))


def cidr_literals(text: str) -> list[str]:
    return re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b", text)


def cmd_check(args) -> int:
    """Offline checks of the derivation itself: exact counts, one source of the CIDR, diffs apply to the baseline with git."""
    problems = []
    vs = variants(args.ref)
    base = vs["baseline"]
    if base["nginx/conf/nginx.conf"].decode().count(OLD_TRUST_LINE) != 1:
        problems.append("baseline lost its single trust line")
    cand = vs["candidate"]
    cconf, csapi, ccomp = (directives(cand[p].decode()) for p in ("nginx/conf/nginx.conf", "nginx/conf/search_api.conf", "docker-compose.yml"))
    # ONE source: no CIDR literal in any non-comment line of the candidate, the variable in exactly two places
    for name, text in (("nginx.conf", cconf), ("search_api.conf", csapi), ("docker-compose.yml", ccomp)):
        lit = cidr_literals(text)
        if lit:
            problems.append(f"candidate {name} has a CIDR literal in a non-comment line: {lit}")
    if "set_real_ip_from" in cconf:
        problems.append("candidate nginx.conf must not carry its own set_real_ip_from (the range comes from the Compose config)")
    if cconf.count(INCLUDE_LINE) != 1:
        problems.append("candidate nginx.conf must include the Compose-provided trust file exactly once")
    if ccomp.count("${" + SUBNET_VAR) != 2:
        problems.append(f"the compose file must use ${{{SUBNET_VAR}}} exactly twice (network subnet, trust config), found {ccomp.count('${' + SUBNET_VAR)}")
    if ":?" not in ccomp:
        problems.append("the network subnet must be required (no default value)")
    # the transition and mutant variants
    for name, want_lit, want_inc in (("transition", 1, 1), ("transition2", 1, 1), ("mut_broad", 1, 1), ("mut_notrust", 0, 0),
                                     ("mut_xff", 0, 1), ("mut_literal", 1, 0), ("mut_cidr", 0, 1)):
        d = directives(vs[name]["nginx/conf/nginx.conf"].decode())
        got_lit, got_inc = d.count("set_real_ip_from"), d.count(INCLUDE_LINE)
        if (got_lit, got_inc) != (want_lit, want_inc):
            problems.append(f"{name}: set_real_ip_from lines/includes = {(got_lit, got_inc)}, expected {(want_lit, want_inc)}")
    for name in VARIANT_NAMES[1:]:
        if vs[name]["nginx/conf/mime.types"] != base["nginx/conf/mime.types"]:
            problems.append(f"{name}: mime.types changed")
    if "$proxy_add_x_forwarded_for" in cconf or "$proxy_add_x_forwarded_for" in csapi:
        problems.append("candidate still appends to a caller-supplied X-Forwarded-For")
    if "$proxy_add_x_forwarded_for" not in directives(vs["mut_xff"]["nginx/conf/nginx.conf"].decode()):
        problems.append("mut_xff does not keep the append form")
    if MUTANT_LITERAL not in vs["mut_cidr"]["docker-compose.yml"].decode():
        problems.append("mut_cidr lost its literal")
    # the diffs must apply cleanly to the committed baseline
    with tempfile.TemporaryDirectory(prefix="r2chk_") as d:
        d = pathlib.Path(d)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        for path, data in base.items():
            (d / path).parent.mkdir(parents=True, exist_ok=True)
            (d / path).write_bytes(data)
        for name, text in render_diffs(args.ref).items():
            (d / name).write_text(text, encoding="utf-8", newline="\n")
            p = subprocess.run(["git", "-C", str(d), "-c", "core.autocrlf=false", "apply", "--check", name], capture_output=True, text=True)
            if p.returncode != 0:
                problems.append(f"{name} does not apply to the baseline: {p.stderr.strip()[:200]}")
    if problems:
        print("candidate derivation: FAIL\n  - " + "\n  - ".join(problems))
        return 1
    print("candidate derivation: OK (exact replacements, the CIDR has ONE source, diffs apply to the baseline, variants as designed)")
    return 0


# -- the rollout steps (section 6 of the proposal) are exactly these variants, and the committed files are exactly their content --
STEP_VARIANT = {1: "transition", 2: "transition2", 3: "candidate"}
STEP_FILES = {1: ("nginx/conf/nginx.conf", "nginx/conf/search_api.conf"), 2: ("docker-compose.yml",), 3: ("nginx/conf/nginx.conf",)}


def step_content(step: int, ref: str = S2_BASE) -> dict[str, bytes]:
    """The files a step commit changes, with their full content (a later step changes only what differs from the previous one)."""
    vs = variants(ref)
    return {path: vs[STEP_VARIANT[step]][path] for path in STEP_FILES[step]}


def cmd_apply(args) -> int:
    dest = pathlib.Path(args.to) if args.to else REPO
    for path, data in step_content(args.step, args.ref).items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)                      # LF, byte for byte what the commit must hold
        print(f"wrote {path}")
    return 0


def cmd_verify(args) -> int:
    """Each step commit changes exactly its files, and every file of every step equals the derived variant byte for byte."""
    def show(sha, path):
        p = subprocess.run(["git", "-C", args.repo, "show", f"{sha}:{path}"], capture_output=True)
        return p.stdout if p.returncode == 0 else None

    def names(a, b):
        p = subprocess.run(["git", "-C", args.repo, "diff", "--name-only", a, b, "--", "docker-compose.yml", "nginx"], capture_output=True, text=True)
        return sorted(p.stdout.split())

    problems, vs = [], variants(args.ref)
    prev = args.ref
    for step, sha in ((1, args.step1), (2, args.step2), (3, args.step3)):
        if names(f"{sha}^", sha) != sorted(STEP_FILES[step]):
            problems.append(f"step {step} ({sha[:10]}) changes {names(f'{sha}^', sha)}, expected {sorted(STEP_FILES[step])}")
        if names(prev, f"{sha}^"):
            problems.append(f"between the previous step and step {step} the deployed files changed: {names(prev, f'{sha}^')}")
        for path in FILES:
            if show(sha, path) != vs[STEP_VARIANT[step]][path]:
                problems.append(f"step {step}: {path} is not the derived variant {STEP_VARIANT[step]}")
        prev = sha
    if problems:
        print("implementation vs derivation: FAIL\n  - " + "\n  - ".join(problems))
        return 1
    print("implementation vs derivation: OK (the three step commits hold byte for byte the derived transition / transition2 / candidate files)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", required=True)
    r.add_argument("--ref", default=S2_BASE)
    r.set_defaults(fn=cmd_render)
    c = sub.add_parser("check")
    c.add_argument("--ref", default=S2_BASE)
    c.set_defaults(fn=cmd_check)
    a = sub.add_parser("apply")
    a.add_argument("--step", type=int, choices=(1, 2, 3), required=True)
    a.add_argument("--ref", default=S2_BASE)
    a.add_argument("--to", default=None, help="directory to write into (default: this checkout)")
    a.set_defaults(fn=cmd_apply)
    v = sub.add_parser("verify")
    v.add_argument("--ref", default=S2_BASE)
    v.add_argument("--repo", required=True)
    for k in ("step1", "step2", "step3"):
        v.add_argument(f"--{k}", required=True)
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
