#!/usr/bin/env python3
"""Offline tests of the R2 lab's own safety logic. No Docker, no network, standard library only.

    python tests/nginx_r2/offline_tests.py          run all, exit 0 only when every test passes
    (r2lab.py selfcheck runs the same tests and records them)

What they prove, with a FAKE Docker that records every command it receives:
  * cleanup removes exactly the resources a run owns (labels, exact Compose project), by id, containers before networks;
  * a foreign network with a similar name (the review's `operator_b04r2_shared`), another run's resources, a resource whose
    inspection fails and a resource with a mismatching Compose project are KEPT, and no command ever names them;
  * the subnet preflight refuses overlaps with Docker networks, address pools, routes, interface addresses, the well-known list
    and the administrator's reserved ranges; refuses an invalid value and an unverifiable host; never refuses its own network;
  * the allocator is deterministic, spreads projects, skips what is taken and always returns a canonical, allowed block;
  * the host read (R2-R3): a failed route read OR address read is unverifiable (exit 3, nothing allocated) whatever the other read
    printed, a collision that exists only in a non-main routing table is found (exit 2), and a complete read passes. The helper
    container's script runs in a REAL POSIX shell with a fake `ip`, so the exit-status semantics are the shell's, not a model of them;
  * the CIDR has ONE source: a second constant or a mismatching config is detected;
  * the switch script and the S2 guard (switch_offline.py): the exact change of a step, the scope of the commit, the inputs of a return, the S2 guard.
"""
from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ownership as OW  # noqa: E402
import tunnel_subnet as TS  # noqa: E402

RUN = "1004123456ab"
OTHER = "10049900cd01"


class Fake:
    """An in-memory Docker. Unknown commands fail the test: nothing the code issues may go unnoticed."""

    def __init__(self):
        self.objs = {"container": {}, "network": {}, "volume": {}}      # id -> {"name": ..., "labels": dict|None, "fail": bool}
        self.calls = []

    def add(self, kind, ident, name, labels, fail=False):
        self.objs[kind][ident] = {"name": name, "labels": labels, "fail": fail}

    def __call__(self, cmd, check=False, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        out = lambda s="", rc=0, err="": types.SimpleNamespace(returncode=rc, stdout=s, stderr=err)   # noqa: E731
        kind = {"ps": "container", "network": "network", "volume": "volume", "rm": "container", "inspect": "container"}.get(cmd[1])
        if cmd[1] == "network" and cmd[2] in ("ls", "rm", "inspect"):
            sub, rest = cmd[2], cmd[3:]
        elif cmd[1] == "volume" and cmd[2] in ("ls", "rm", "inspect"):
            sub, rest = cmd[2], cmd[3:]
        elif cmd[1] in ("ps", "rm", "inspect"):
            sub, rest = {"ps": "ls", "rm": "rm", "inspect": "inspect"}[cmd[1]], cmd[2:]
        else:
            raise AssertionError(f"unexpected docker command: {cmd}")
        table = self.objs[kind]
        filt = [rest[i + 1] for i, a in enumerate(rest) if a == "--filter"]
        if sub == "ls":
            ids = []
            for ident, o in table.items():
                ok = True
                for f in filt:
                    if f.startswith("label="):
                        k, _, v = f[len("label="):].partition("=")
                        ok = ok and isinstance(o["labels"], dict) and o["labels"].get(k) == v
                    elif f.startswith("name="):
                        ok = ok and f[len("name="):] in o["name"]
                    else:
                        raise AssertionError(f"unexpected filter {f}")
                if ok:
                    ids.append(ident)
            if "--format" in rest:
                return out("\n".join(f"{i} {table[i]['name']}" for i in ids))
            return out("\n".join(ids))
        ref = rest[-1]
        ident = ref if ref in table else next((i for i, o in table.items() if o["name"] == ref), ref)    # Docker accepts an id or a name
        if sub == "inspect":
            o = table.get(ident)
            if o is None:
                return out("", 1, f"Error: No such {kind}: {ident}")
            if o["fail"]:
                return out("", 1, "Error response from daemon: boom")
            return out(json.dumps(o["labels"]))
        if sub == "rm":
            if ident not in table:
                return out("", 1, f"Error: No such {kind}: {ref}")
            del table[ident]
            return out(ident)
        raise AssertionError(cmd)

    def removals(self):
        res = []
        for c in self.calls:
            if (c[1] == "rm") or (c[1] in ("network", "volume") and c[2] == "rm"):
                res.append((c[1] if c[1] != "rm" else "container", c[-1]))
        return res


def labels(run=RUN, project=None, recorded=None, purpose=OW.PURPOSE):
    d = {OW.LABEL_PURPOSE: purpose, OW.LABEL_RUN: run}
    if project:
        d[OW.COMPOSE_PROJECT] = project
    if recorded:
        d[OW.LABEL_PROJECT] = recorded
    return d


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── ownership and cleanup ────────────────────────────────────────────────
def t_own_removed_by_id_in_order():
    f = Fake()
    f.add("container", "c111", f"b04r2_{RUN}_nginx_candidate", labels(project=f"b04r2_{RUN}_p_candidate", recorded=f"b04r2_{RUN}_p_candidate"))
    f.add("container", "c222", f"b04r2_{RUN}_candidate_cloudflared", labels())
    f.add("network", "n111", f"b04r2_{RUN}_p_candidate_edge", labels(project=f"b04r2_{RUN}_p_candidate", recorded=f"b04r2_{RUN}_p_candidate"))
    f.add("network", "n222", "plain_own_network", labels())
    f.add("volume", "v111", "own_volume", labels())
    r = OW.cleanup_run(f, RUN)
    must(r.ok(), f"cleanup of an all-owned run is not clean: kept={r.kept} errors={r.errors}")
    must(not any(f.objs.values()), f"owned resources left: {f.objs}")
    rem = f.removals()
    must(rem == [("container", "c111"), ("container", "c222"), ("network", "n111"), ("network", "n222"), ("volume", "v111")],
         f"removal order or targets differ: {rem}")
    names = {o["name"] for kind in ("container", "network", "volume") for o in []}
    must(all(re.fullmatch(r"[cnv]\d{3}", ident) for _, ident in rem), "a removal command named something other than an id")
    return "5 owned resources removed by id, containers before networks before volumes"


def t_foreign_similar_name_kept():
    """The review's reproduction: a network `operator_b04r2_shared` without labels, found only by name."""
    f = Fake()
    f.add("network", "nFOREIGN", "operator_b04r2_shared", None)
    f.add("network", "nOWN", f"b04r2_{RUN}_p_x_edge", labels(project=f"b04r2_{RUN}_p_x", recorded=f"b04r2_{RUN}_p_x"))
    f.add("container", "cLOOK", f"b04r2_{RUN}_lookalike", None)           # carries this run's id in its NAME, but no labels
    r = OW.cleanup_run(f, RUN)
    must("nFOREIGN" in f.objs["network"] and "cLOOK" in f.objs["container"], "a foreign / unlabelled resource was removed")
    must("nOWN" not in f.objs["network"], "the owned network was not removed")
    must(all(c[-1] not in ("operator_b04r2_shared", f"b04r2_{RUN}_lookalike", "nFOREIGN", "cLOOK") for c in f.calls if "rm" in c),
         "a removal command named the foreign resources")
    must(("container", "cLOOK", f"b04r2_{RUN}_lookalike") in r.lookalikes, f"the lookalike was not reported: {r.lookalikes}")
    return "foreign network and unlabelled same-prefix container kept; only the owned network removed; the lookalike is reported"


def t_original_bug_is_gone():
    """The old cmd_cleanup asked Docker for `name=b04r2_` and removed every hit. Prove that no name filter selects removals."""
    f = Fake()
    f.add("network", "nFOREIGN", "operator_b04r2_shared", None)
    OW.cleanup_run(f, RUN)
    must(f.removals() == [], f"something was removed: {f.removals()}")
    for c in f.calls:
        must(not any(a.startswith("name=b04r2_") and not a.startswith(f"name=b04r2_{RUN}") for a in c), f"a prefix name filter was issued: {c}")
    return "no removal and no bare `name=b04r2_` filter"


def old_cleanup(runner):
    """The FIRST version of cmd_cleanup, kept here only so that these tests can show they catch the review's finding (R2-R2)."""
    for kind, cmd in (("containers", ["docker", "ps", "-aq"]), ("networks", ["docker", "network", "ls", "-q"]), ("volumes", ["docker", "volume", "ls", "-q"])):
        for i in runner(cmd + ["--filter", f"label=edu_ai.purpose={OW.PURPOSE}"], check=False).stdout.split():
            runner({"containers": ["docker", "rm", "-f", i], "networks": ["docker", "network", "rm", i], "volumes": ["docker", "volume", "rm", "-f", i]}[kind], check=False)
    for net in runner(["docker", "network", "ls", "--format", "{{.Name}}", "--filter", "name=b04r2_"], check=False).stdout.split():
        runner(["docker", "network", "rm", net], check=False)


def t_old_version_reproduces_the_finding():
    f_old, f_new = Fake(), Fake()
    for f in (f_old, f_new):
        f.add("network", "nFOREIGN", "operator_b04r2_shared", None)
    old_cleanup(f_old)
    must("nFOREIGN" not in f_old.objs["network"] and ("network", "operator_b04r2_shared") in f_old.removals(),
         "the reproduction of the first version did not remove the foreign network: these tests would not catch the finding")
    OW.cleanup_run(f_new, RUN)
    must("nFOREIGN" in f_new.objs["network"], "the fixed cleanup removed the foreign network")
    return "first version: `docker network rm operator_b04r2_shared` (the finding, reproduced); fixed version: kept"


def t_other_run_kept():
    f = Fake()
    f.add("container", "cOTHER", f"b04r2_{OTHER}_nginx_candidate", labels(OTHER))
    f.add("network", "nOTHER", f"b04r2_{OTHER}_p_candidate_edge", labels(OTHER, f"b04r2_{OTHER}_p_candidate", f"b04r2_{OTHER}_p_candidate"))
    f.add("container", "cMINE", f"b04r2_{RUN}_x", labels())
    f.add("network", "nWRONGPURPOSE", "x", labels(purpose="something-else"))
    r = OW.cleanup_run(f, RUN)
    must("cOTHER" in f.objs["container"] and "nOTHER" in f.objs["network"], "another run's resources were removed")
    must("cMINE" not in f.objs["container"], "this run's container was kept")
    must(f.removals() == [("container", "cMINE")], f"unexpected removals: {f.removals()}")
    return "another active run's container and network untouched; only this run's container removed"


def t_wrong_purpose_with_our_run_label_kept():
    f = Fake()
    f.add("network", "nX", "x", {OW.LABEL_PURPOSE: "other", OW.LABEL_RUN: RUN})
    r = OW.cleanup_run(f, RUN)
    must(r.kept and r.kept[0][0:2] == ("network", "nX") and "nX" in f.objs["network"], f"a resource with the wrong purpose was handled: {r.kept}")
    return f"kept: {r.kept[0][2]}"


def t_inspect_failure_kept():
    f = Fake()
    f.add("container", "cBAD", "x", labels(), fail=True)
    f.add("container", "cGOOD", "y", labels())
    r = OW.cleanup_run(f, RUN)
    must("cBAD" in f.objs["container"], "a container whose inspection failed was removed")
    must("cGOOD" not in f.objs["container"], "the verifiable owned container was not removed")
    must(any(k[1] == "cBAD" and "cannot verify" in k[2] for k in r.kept) and not r.ok(), f"the failure is not reported: {r.kept}")
    return "inspection failure -> kept, reported, result not ok; the verifiable one was removed"


def t_compose_project_must_match_this_run():
    f = Fake()
    p_own, p_other = f"b04r2_{RUN}_p_candidate", f"b04r2_{OTHER}_p_candidate"
    f.add("network", "nOK", "a", labels(project=p_own, recorded=p_own))
    f.add("network", "nOTHERPROJ", "b", labels(project=p_other, recorded=p_other))                       # run label ours, project another run's
    f.add("network", "nUNRECORDED", "c", labels(project=p_own))                                          # project label without the recorded one
    f.add("network", "nMISMATCH", "d", labels(project=p_own, recorded=f"b04r2_{RUN}_p_other"))           # recorded differs from the real project
    f.add("network", "nEVIL", "e", labels(project="operator_b04r2_shared", recorded="operator_b04r2_shared"))
    r = OW.cleanup_run(f, RUN)
    must(set(f.objs["network"]) == {"nOTHERPROJ", "nUNRECORDED", "nMISMATCH", "nEVIL"}, f"networks left: {sorted(f.objs['network'])}")
    return "only the network of this run's exact project removed; four project mismatches kept"


def t_run_id_required_and_validated():
    f = Fake()
    for bad in ("", "b04r2", "*", "1004", "100412AB", "100412ab; rm", "0004124b0"):
        try:
            OW.cleanup_run(f, bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a run id")
    must(f.calls == [], "docker was called for an invalid run id")
    return "empty, partial, wildcard and malformed run ids are refused before any docker call"


def t_safe_remove_refuses_foreign_name_and_handles_absent():
    f = Fake()
    f.add("container", "cFOR", f"b04r2_{RUN}_candidate_stubs_main", None)             # same planned name, foreign holder
    try:
        OW.safe_remove(f, "container", f"b04r2_{RUN}_candidate_stubs_main", RUN)
        raise AssertionError("a foreign holder of a planned name was removed")
    except OW.Refused:
        pass
    must(OW.safe_remove(f, "container", "nothing-here", RUN) == "absent", "an absent resource is not 'absent'")
    f.add("container", "cERR", "x", labels(), fail=True)
    try:
        OW.safe_remove(f, "container", "cERR", RUN)
        raise AssertionError("an uninspectable resource was removed")
    except OW.InspectError:
        pass
    must("cFOR" in f.objs["container"] and "cERR" in f.objs["container"], "a refused resource disappeared")
    return "foreign holder of a planned name refused; absent resource tolerated; inspection failure refuses"


def t_dry_run_removes_nothing():
    f = Fake()
    f.add("container", "c1", "x", labels())
    f.add("network", "n1", "y", labels())
    r = OW.cleanup_run(f, RUN, dry_run=True)
    must(f.removals() == [] and f.objs["container"] and f.objs["network"], "a dry run removed something")
    must(sorted(r.removed) == [("container", "c1"), ("network", "n1")], f"the plan is wrong: {r.removed}")
    return "dry run lists the plan and issues no removal"


def t_list_runs_only_labelled():
    f = Fake()
    f.add("container", "c1", "x", labels(RUN))
    f.add("network", "n1", "y", labels(OTHER))
    f.add("network", "n2", "operator_b04r2_shared", None)
    f.add("volume", "v1", "z", {OW.LABEL_PURPOSE: OW.PURPOSE, OW.LABEL_RUN: "not-a-run-id"})
    must(OW.list_runs(f) == sorted([RUN, OTHER]), f"runs: {OW.list_runs(f)}")
    return f"found {sorted([RUN, OTHER])}"


# ── subnet preflight and allocation ──────────────────────────────────────
def net(s):
    return ipaddress.ip_network(s)


def state(networks=(), routes=(), addrs=(), pools=("172.16.0.0/12", "192.168.0.0/16")):
    return TS.State(tuple(TS.NetInfo(n, "id-" + n, tuple(net(s) for s in subs)) for n, subs in networks),
                    tuple(net(p) for p in pools), tuple((net(r.split()[0]), r) for r in routes),
                    tuple((ipaddress.ip_interface(a).network, a) for a in addrs))


def kinds(found):
    return sorted({c.kind for c in found})


def t_candidate_value_validation():
    ok = TS.parse_candidate("10.77.3.8/29")
    must(str(ok) == "10.77.3.8/29", "a valid value was altered")
    for bad, why in (("10.77.3.5/29", "INVALID"), ("10.77.3.0/24", "INVALID"), ("10.77.3.0/30", "INVALID"), ("8.8.8.0/29", "NOT_PRIVATE"),
                     ("fd00::/64", "INVALID"), ("abc", "INVALID"), ("", "INVALID"), ("10.77.3.8", "INVALID")):
        try:
            TS.parse_candidate(bad)
        except ValueError as e:
            must(str(e).startswith(why), f"{bad!r}: {e}")
            continue
        raise AssertionError(f"{bad!r} was accepted")
    return "canonical /28../29 inside RFC 1918 accepted; host bits, other prefixes, public, IPv6 and junk refused"


def t_conflict_kinds():
    st = state(networks=[("other_project_tunnel", ["10.77.3.8/29"]), ("br", ["10.99.0.0/16"])], routes=["10.150.0.0/20 dev wg0", "10.160.1.4 dev x"],
               addrs=["10.170.0.1/24"], pools=("172.16.0.0/12", "192.168.0.0/16", "10.180.0.0/16"))
    cases = {
        "DOCKER_NETWORK": "10.77.3.8/29", "DOCKER_NETWORK ": "10.99.4.0/29", "HOST_ROUTE": "10.150.8.0/29", "HOST_ROUTE ": "10.160.1.0/29",
        "HOST_ADDRESS": "10.170.0.8/29", "DOCKER_POOL": "10.180.1.0/29", "DENYLIST": "10.66.0.8/29", "DENYLIST ": "172.20.0.0/29",
        "DENYLIST  ": "192.168.65.0/29", "DENYLIST   ": "10.130.0.0/29",
    }
    for kind, cidr in cases.items():
        must(kind.strip() in kinds(TS.conflicts(net(cidr), st)), f"{cidr}: expected {kind.strip()}, got {kinds(TS.conflicts(net(cidr), st))}")
    must("RESERVED" in kinds(TS.conflicts(net("10.200.1.0/29"), st, reserved=["10.200.0.0/16"])), "an administrator-reserved range was not refused")
    must(TS.conflicts(net("10.55.5.0/29"), st) == [], f"a free block was refused: {TS.conflicts(net('10.55.5.0/29'), st)}")
    return "Docker network, address pool, route (net and host), interface address, well-known list and reserved range each refuse; a free block passes"


def t_own_network_is_not_a_conflict_but_a_mismatch_is():
    cand = net("10.77.3.8/29")
    st_same = state(networks=[("inst_tunnel", ["10.77.3.8/29"])])
    st_diff = state(networks=[("inst_tunnel", ["10.88.1.0/29"])])
    must(TS.conflicts(cand, st_same, project="inst") == [], "the installation's own network, same subnet, is refused")
    must(kinds(TS.conflicts(cand, st_diff, project="inst")) == ["NETWORK_MISMATCH"], f"mismatch not found: {TS.conflicts(cand, st_diff, project='inst')}")
    must("DOCKER_NETWORK" in kinds(TS.conflicts(cand, st_same, project="another")), "another installation's identical subnet was not refused")
    return "own network with the same subnet passes; with another subnet it is NETWORK_MISMATCH; the same subnet seen by another project is refused"


def t_own_bridge_is_not_a_collision():
    """A running installation's own network puts a route and an address (br-<id>) into the host; the re-check must not refuse itself."""
    cand = net("10.14.118.48/29")
    own_id = "62dd04c0db44aabbccddeeff00112233445566778899aabbccddeeff00112233"
    st = TS.State((TS.NetInfo("inst_tunnel", own_id, (cand,)),), (), ((cand, "10.14.118.48/29 dev br-62dd04c0db44 scope link  src 10.14.118.49"),),
                  ((ipaddress.ip_interface("10.14.118.49/29").network, "354: br-62dd04c0db44    inet 10.14.118.49/29 brd 10.14.118.55 scope global br-62dd04c0db44"),))
    must(TS.conflicts(cand, st, project="inst") == [], f"the installation refused itself: {TS.conflicts(cand, st, project='inst')}")
    must({"HOST_ROUTE", "HOST_ADDRESS", "DOCKER_NETWORK"} <= set(kinds(TS.conflicts(cand, st, project="another"))), "another installation was not refused")
    other_if = TS.State(st.networks, (), ((cand, "10.14.118.48/29 dev eth9 scope link"),), ())
    must("HOST_ROUTE" in kinds(TS.conflicts(cand, other_if, project="inst")), "a route on ANOTHER interface was exempted")
    must(TS.allocate("inst", st) == cand, "allocate did not keep the installation's existing subnet")
    return "own bridge route/address exempt; another project, or a route on another interface, still refused; allocate keeps the existing subnet"


def t_route_and_address_parsing():
    routes = ("default via 172.17.0.1 dev eth0\n10.66.0.0/24 dev wg0 proto kernel scope link src 10.66.0.1\n172.17.0.0/16 dev docker0 scope link\n"
              "blackhole 10.250.0.0/16\n10.9.9.9 via 10.66.0.2 dev wg0\nnonsense line\n")
    r = [str(d) for d, _ in TS.parse_routes(routes)]
    must(r == ["10.66.0.0/24", "172.17.0.0/16", "10.250.0.0/16", "10.9.9.9/32"], f"routes parsed as {r}")
    a = TS.parse_addrs("2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\n3: wg0    inet 10.66.0.1/24 scope global wg0\n4: lo    inet 127.0.0.1/8 scope host lo\n")
    must([str(d) for d, _ in a] == ["192.168.1.0/24", "10.66.0.0/24", "127.0.0.0/8"], f"addresses parsed as {[str(d) for d, _ in a]}")
    return "ip route / ip addr output parsed (default skipped, host route /32, type word, junk ignored)"


def t_allocation():
    st = state()
    a = TS.allocate("proj_a", st)
    must(a == TS.allocate("proj_a", st), "allocation is not deterministic")
    must(a != TS.allocate("proj_b", st), "different projects got the same block")
    for p in (f"p{i}" for i in range(300)):
        n = TS.allocate(p, st)
        must(n.prefixlen == 29 and n.subnet_of(net("10.0.0.0/8")) and not TS.conflicts(n, st), f"{p}: bad allocation {n}")
        must(not any(n.overlaps(net(b)) for b, _ in TS.DENY), f"{p}: {n} is in the well-known list")
    taken = state(networks=[("x", [str(a)])])
    b = TS.allocate("proj_a", taken)
    must(b != a and not b.overlaps(a), f"a taken block was handed out again: {b}")
    try:
        TS.allocate("p", st, pool="10.66.0.0/29", prefix=29)
        raise AssertionError("the single block of a one-block pool was handed out although it is in the well-known list")
    except TS.NoFreeSubnet:
        pass
    small = state(networks=[("x", ["10.20.0.0/29"])])
    try:
        TS.allocate("p", small, pool="10.20.0.0/29", prefix=29)
        raise AssertionError("an exhausted pool still allocated")
    except TS.NoFreeSubnet:
        pass
    return f"deterministic ({a}), distinct per project, 300 allocations canonical /29 in 10/8 outside the well-known list, taken blocks skipped, exhaustion refused"


def t_unverifiable_host_is_refused():
    def bad_runner(rc, out="", err="x"):
        return lambda cmd, check=False, **kw: types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    for what, runner in (("network ls fails", bad_runner(1)), ("invalid JSON", bad_runner(0, out="{")),):
        try:
            TS.gather(runner, routes="host")
            raise AssertionError(f"{what}: the host state was accepted")
        except TS.Unverifiable:
            pass
    ok_net = lambda cmd, check=False, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr="")   # noqa: E731
    try:
        TS.gather(ok_net, routes="host")
        raise AssertionError("an empty route and address table was accepted")
    except TS.Unverifiable:
        pass
    try:
        TS.gather(ok_net, routes="docker", helper_image=None)
        raise AssertionError("--routes docker without a helper image was accepted")
    except TS.Unverifiable:
        pass
    return "failing, malformed, empty or unreadable host state raises Unverifiable (refuse, never assume free)"


def t_cli_exit_codes():
    import contextlib
    import io
    real_gather = TS.gather
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            TS.gather = lambda *a, **k: state(networks=[("o", ["10.77.3.8/29"])], routes=["10.150.0.0/20 dev wg0"])
            codes = [TS.main(["check", "10.55.5.0/29"]), TS.main(["check", "10.77.3.8/29"]), TS.main(["check", "10.77.3.9/29"]),
                     TS.main(["allocate", "--project", "p"])]

            def fail(*a, **k):
                raise TS.Unverifiable("simulated")
            TS.gather = fail
            codes += [TS.main(["check", "10.55.5.0/29"]), TS.main(["allocate", "--project", "p"])]
    finally:
        TS.gather = real_gather
    must(codes == [0, 2, 2, 0, 3, 3], f"exit codes {codes}")
    must("EDU_AI_TUNNEL_SUBNET=10." in sink.getvalue() and sink.getvalue().count("EDU_AI_TUNNEL_SUBNET=") == 1, "allocation printed no setting, or printed one while unverifiable")
    return "exit 0 free / 2 refused (overlap, invalid) / 3 refused (cannot verify); no allocation when the host cannot be inspected"


# ── the host read: every routing table, every read checked (R2-R3) ───────────────
CAND = "10.55.5.0/29"
MAIN_ROUTES = "default via 172.17.0.1 dev eth0 \n172.17.0.0/16 dev docker0 scope link  src 172.17.0.1 \n"
LOCAL_ROUTES = ("local 127.0.0.0/8 dev lo table local scope host  src 127.0.0.1 \n"
                "local 172.17.0.1 dev docker0 table local scope host  src 172.17.0.1 \n"
                "broadcast 172.17.255.255 dev docker0 table local scope link  src 172.17.0.1 \n")
ADDRS = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
         "2: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever\n")
# a fake `ip` for the real shell: a plain route read shows `main` only, `table all` every table, like the real command
SHELL_IP = r'''ip() {
  case "$*" in
    *route*)
      [ "$FAKE_ROUTE_RC" = 0 ] || { echo 'RTNETLINK answers: simulated route read failure' >&2; return 1; }
      case "$*" in
        *'table all'*) [ "$FAKE_ALL" = 1 ] || { echo "ip: invalid argument 'all' to 'table'" >&2; return 1; }
                       printf '%s' "$FAKE_MAIN$FAKE_OTHER";;
        *table*) echo "ip: invalid argument to 'table'" >&2; return 1;;
        *) printf '%s' "$FAKE_MAIN";;
      esac;;
    *addr*) [ "$FAKE_ADDR_RC" = 0 ] || { echo 'simulated address read failure' >&2; return 1; }
            printf '%s' "$FAKE_ADDRS";;
    *) return 2;;
  esac
}
'''
OLD_HELPER_SCRIPT = "ip -4 route show; echo @@@; ip -4 -o addr show"          # the first version (R2-R3), kept to show the tests catch it


def posix_shell():
    cands = [r"C:\Program Files\Git\usr\bin\sh.exe", r"C:\Program Files\Git\bin\sh.exe"] if os.name == "nt" else []
    cands += [shutil.which("sh")]
    for c in cands:
        if c and pathlib.Path(c).exists():
            return c
    raise RuntimeError("no POSIX shell: the helper script's exit-status semantics cannot be tested (ERROR, never PASS)")


class FakeHostNet:
    """An in-memory host: Docker networks, routing tables and interface addresses. `docker run ... sh -c SCRIPT` executes the script
    text the code under test really sent, in a real POSIX shell, against this host's `ip`; a read the host is told to fail exits 1."""

    def __init__(self, main=MAIN_ROUTES, other=LOCAL_ROUTES, addrs=ADDRS, networks=(), route_rc=0, addr_rc=0, all_tables=True):
        self.main, self.other, self.addrs, self.networks = main, other, addrs, tuple(networks)
        self.route_rc, self.addr_rc, self.all_tables = route_rc, addr_rc, all_tables
        self.calls, self.helper_results = [], []

    def ip(self, args):
        if "route" in args:
            if self.route_rc:
                return 1, "", "RTNETLINK answers: simulated route read failure"
            if "table" in args:
                if args[args.index("table") + 1] == "all" and self.all_tables:
                    return 0, self.main + self.other, ""
                return 1, "", "ip: invalid argument to 'table'"
            return 0, self.main, ""
        if "addr" in args:
            return (1, "", "simulated address read failure") if self.addr_rc else (0, self.addrs, "")
        return 2, "", "unknown ip command"

    def shell(self, script):
        env = dict(os.environ, FAKE_ROUTE_RC=str(self.route_rc), FAKE_ADDR_RC=str(self.addr_rc), FAKE_ALL="1" if self.all_tables else "0",
                   FAKE_MAIN=self.main, FAKE_OTHER=self.other, FAKE_ADDRS=self.addrs)
        p = subprocess.run([posix_shell()], input=(SHELL_IP + "\n" + script + "\n").encode(), capture_output=True, env=env, timeout=30)
        res = types.SimpleNamespace(returncode=p.returncode, stdout=p.stdout.decode(), stderr=p.stderr.decode())
        self.helper_results.append(res)
        return res

    def __call__(self, cmd, check=False, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        out = lambda s="", rc=0, err="": types.SimpleNamespace(returncode=rc, stdout=s, stderr=err)   # noqa: E731
        if cmd[:3] == ["docker", "network", "ls"]:
            return out("\n".join(f"id-{n}" for n, _, _ in self.networks))
        if cmd[:3] == ["docker", "network", "inspect"]:
            return out(json.dumps([{"Name": n, "Id": i, "IPAM": {"Config": [{"Subnet": s} for s in subs]}} for n, i, subs in self.networks]))
        if cmd[:2] == ["docker", "info"]:
            return out("[]")
        if cmd[0] == "ip":
            rc, o, e = self.ip(cmd[1:])
            return out(o, rc, e)
        if cmd[:2] == ["docker", "run"]:
            must(cmd[cmd.index("--entrypoint") + 1] == "sh" and cmd[cmd.index("-c") - 1] == "fake-helper", f"unexpected helper command {cmd}")
            return self.shell(cmd[cmd.index("-c") + 1])
        raise AssertionError(f"unexpected command: {cmd}")

    def route_reads(self):
        return [c for c in self.calls if c[:1] == ["ip"] and "route" in c]


def flags(mode):
    return ["--routes", mode] + (["--helper-image", "fake-helper"] if mode == "docker" else [])


def cli(host, argv, mode):
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        rc = TS.main(list(argv) + flags(mode), runner=host)
    return rc, sink.getvalue()


def must_be_unverifiable(host, mode, *words):
    try:
        TS.gather(host, routes=mode, helper_image="fake-helper" if mode == "docker" else None)
    except TS.Unverifiable as e:
        for w in words:
            must(w in str(e), f"{mode}: the refusal does not say {w!r}: {e}")
        return str(e)
    raise AssertionError(f"{mode}: an unreadable host state was accepted")


def refused_unverifiable(host, mode):
    """Both commands refuse with 3 and `allocate` prints no subnet."""
    for argv in (["check", CAND], ["check", CAND, "--project", "inst"], ["allocate", "--project", "inst"]):
        rc, out = cli(host, argv, mode)
        must(rc == 3 and "REFUSED (cannot verify)" in out and "EDU_AI_TUNNEL_SUBNET=" not in out, f"{mode} {argv[0]}: rc={rc} {out.strip()[:200]}")


def t_route_read_failure_is_unverifiable():
    notes = []
    for mode in ("host", "docker"):
        host = FakeHostNet(route_rc=1)                                  # the address read succeeds and has a valid answer
        msg = must_be_unverifiable(host, mode, "route")
        refused_unverifiable(host, mode)
        if mode == "docker":
            must(host.helper_results[0].returncode == 11 and host.helper_results[0].stdout.count(TS.SEPARATOR) == 0 and "simulated route" in msg,
                 f"the helper hid the failed route read: rc={host.helper_results[0].returncode} {host.helper_results[0].stdout!r}")
        notes.append(f"{mode}: {msg[:90]}")
    return "route read fails, address read succeeds: gather refuses, check and allocate exit 3, no subnet printed (" + " | ".join(notes) + ")"


def t_address_read_failure_is_unverifiable():
    notes = []
    for mode in ("host", "docker"):
        host = FakeHostNet(addr_rc=1)                                   # the route read succeeds
        msg = must_be_unverifiable(host, mode, "addr")
        refused_unverifiable(host, mode)
        if mode == "docker":
            must(host.helper_results[0].returncode == 12, f"the helper did not report the failed address read: rc={host.helper_results[0].returncode}")
        notes.append(f"{mode}: {msg[:90]}")
    return "address read fails, route read succeeds: gather refuses, check and allocate exit 3, no subnet printed (" + " | ".join(notes) + ")"


def t_collision_only_in_another_table():
    policy = f"{CAND} dev wg0 table 51820 scope link \n"
    notes = []
    for mode in ("host", "docker"):
        host = FakeHostNet(other=LOCAL_ROUTES + policy)
        must(CAND not in host.ip(["-4", "route", "show"])[1], "the fixture shows the policy route in main")
        check_rc, out = cli(host, ["check", CAND], mode)
        must(check_rc == 2 and "HOST_ROUTE" in out and "table 51820" in out and "EDU_AI_TUNNEL_SUBNET=" not in out, f"{mode} check: rc={check_rc} {out.strip()[:200]}")
        clean = TS.gather(FakeHostNet(), routes=mode, helper_image="fake-helper" if mode == "docker" else None)
        first = TS.allocate("inst", clean)                              # where the allocator would start without the policy route
        taken = FakeHostNet(other=LOCAL_ROUTES + f"{first} dev wg0 table 51820 scope link \n")
        rc, out = cli(taken, ["allocate", "--project", "inst"], mode)
        got = out.strip().split("=", 1)[1] if rc == 0 and "=" in out else None
        must(got is not None and got != str(first) and not ipaddress.ip_network(got).overlaps(first), f"{mode} allocate handed out {got} (policy route {first}): rc={rc}")
        must(TS.conflicts(TS.parse_candidate(got), TS.gather(taken, routes=mode, helper_image="fake-helper" if mode == "docker" else None)) == [], "the allocation is not free")
        notes.append(f"{mode}: check exit {check_rc} HOST_ROUTE (table 51820), allocate skipped {first}")
    return "; ".join(notes)


def t_no_silent_fall_back_to_main():
    for mode in ("host", "docker"):
        host = FakeHostNet(all_tables=False, other=LOCAL_ROUTES + f"{CAND} dev wg0 table 51820 scope link \n")   # an `ip` that rejects `table all`
        must_be_unverifiable(host, mode, "route")
        refused_unverifiable(host, mode)
        must(not [c for c in host.route_reads() if c[-2:] != ["table", "all"]], f"{mode}: a main-only route read was issued: {host.route_reads()}")
    must("table all" in TS.HELPER_SCRIPT and TS.HELPER_SCRIPT.count("route") == 1, f"the helper script reads routes in some other way: {TS.HELPER_SCRIPT}")
    return "an `ip` that rejects `table all` is refused (exit 3) in both modes; no route read without `table all` is ever issued"


def t_complete_valid_read_passes():
    states = {}
    for mode in ("host", "docker"):
        host = FakeHostNet()
        rc, out = cli(host, ["check", CAND], mode)
        must(rc == 0 and out.startswith(f"OK: {CAND}"), f"{mode} check: rc={rc} {out.strip()[:200]}")
        rc, out = cli(host, ["allocate", "--project", "inst"], mode)
        must(rc == 0 and out.count("EDU_AI_TUNNEL_SUBNET=10.") == 1, f"{mode} allocate: rc={rc} {out.strip()[:200]}")
        states[mode] = TS.gather(host, routes=mode, helper_image="fake-helper" if mode == "docker" else None)
        must([str(d) for d, _ in states[mode].routes][-3:] == ["127.0.0.0/8", "172.17.0.1/32", "172.17.255.255/32"], f"{mode}: local-table routes missing: {states[mode].routes}")
    must(states["host"] == states["docker"], "the two modes read different inventories")
    return "complete read (main + local tables, addresses): check 0 / OK, allocate 0 with one subnet; host and docker read the identical inventory"


def t_own_installation_with_all_tables():
    own, own_id = TS.parse_candidate("10.14.118.48/29"), "62dd04c0db44aabbccddeeff00112233445566778899aabbccddeeff00112233"
    main = MAIN_ROUTES + "10.14.118.48/29 dev br-62dd04c0db44 scope link  src 10.14.118.49 \n"
    other = LOCAL_ROUTES + ("local 10.14.118.49 dev br-62dd04c0db44 table local scope host  src 10.14.118.49 \n"
                            "broadcast 10.14.118.48 dev br-62dd04c0db44 table local scope link  src 10.14.118.49 \n"
                            "broadcast 10.14.118.55 dev br-62dd04c0db44 table local scope link  src 10.14.118.49 \n")
    addrs = ADDRS + "354: br-62dd04c0db44    inet 10.14.118.49/29 brd 10.14.118.55 scope global br-62dd04c0db44\\       valid_lft forever preferred_lft forever\n"
    for mode in ("host", "docker"):
        host = FakeHostNet(main=main, other=other, addrs=addrs, networks=[("inst_tunnel", own_id, [str(own)])])
        for i in range(3):                                                  # the preflight runs on every create/update
            rc, out = cli(host, ["check", str(own), "--project", "inst"], mode)
            must(rc == 0, f"{mode}: the running installation refused itself on re-check {i + 1}: rc={rc} {out.strip()[:300]}")
        rc, out = cli(host, ["allocate", "--project", "inst"], mode)
        must(rc == 0 and out.strip() == f"EDU_AI_TUNNEL_SUBNET={own}", f"{mode}: allocate did not keep the existing subnet: rc={rc} {out.strip()}")
        rc, out = cli(host, ["check", str(own), "--project", "another"], mode)
        must(rc == 2 and all(k in out for k in ("HOST_ROUTE", "HOST_ADDRESS", "DOCKER_NETWORK")), f"{mode}: another installation was not refused: rc={rc} {out.strip()[:300]}")
        foreign = FakeHostNet(main=main, other=other + f"{own} dev eth9 table 100 scope link \n", addrs=addrs, networks=[("inst_tunnel", own_id, [str(own)])])
        rc, out = cli(foreign, ["check", str(own), "--project", "inst"], mode)
        must(rc == 2 and "HOST_ROUTE" in out and "eth9" in out, f"{mode}: a policy route on ANOTHER interface was exempted: rc={rc} {out.strip()[:300]}")
    return (f"{own}: the running installation passes three repeated preflights and allocate keeps its subnet with its connected, local and broadcast routes "
            "in the inventory (host, docker); another project is refused; a policy route on another interface is still refused")


def t_empty_is_not_failed():
    for mode in ("host", "docker"):
        host = FakeHostNet(main="", other="")                               # reads succeeded and printed nothing: a result, not a failure
        rc, out = cli(host, ["check", CAND], mode)
        must(rc == 0, f"{mode}: an empty route table with a valid address read was refused: rc={rc} {out.strip()[:200]}")
        must(TS.gather(host, routes=mode, helper_image="fake-helper" if mode == "docker" else None).routes == (), f"{mode}: routes are not empty")
        only_default = FakeHostNet(main="default via 172.17.0.1 dev eth0 \n", other="")
        must(cli(only_default, ["check", CAND], mode)[0] == 0, f"{mode}: a table with only a default route was refused")
        must_be_unverifiable(FakeHostNet(main="", other="", addrs=""), mode, "neither")           # nothing at all, not even a loopback
        must_be_unverifiable(FakeHostNet(main="Segmentation fault\n", other=""), mode, "parse")  # a successful read that prints junk
        must_be_unverifiable(FakeHostNet(addrs="garbage\n"), mode, "parse")
    for exc in (FileNotFoundError("ip: no such file"), subprocess.TimeoutExpired("ip", 60)):
        def broken(cmd, check=False, **kw):
            raise exc
        for mode in ("host", "docker"):
            must_be_unverifiable(broken, mode, "could not run")
            rc, out = cli(broken, ["check", CAND], mode)
            must(rc == 3, f"{mode}: {type(exc).__name__} gave exit {rc}, not 3")
    return ("empty output of a successful read is a result (exit 0), a failed read is not (exit 3); no output at all, unparseable output, a missing binary "
            "and a timeout are all refused (exit 3)")


def t_first_version_reproduces_the_finding():
    """The first helper script and the main-only read, run against the same fake host: the test must see them hide the problem."""
    host = FakeHostNet(route_rc=1)
    old = host.shell(OLD_HELPER_SCRIPT)
    new = host.shell(TS.HELPER_SCRIPT)
    must(old.returncode == 0 and old.stdout.startswith(TS.SEPARATOR) and "inet 172.17.0.1/16" in old.stdout,
         f"the first version no longer reproduces the hidden failure: rc={old.returncode} {old.stdout!r}")
    r_txt, a_txt = old.stdout.split(TS.SEPARATOR, 1)
    hidden = TS.State((), (), tuple(TS.parse_routes(r_txt)), tuple(TS.parse_addrs(a_txt)))
    must(TS.conflicts(TS.parse_candidate(CAND), hidden) == [] and old.stdout.strip().startswith(TS.SEPARATOR),
         "the first version's read does not look like an empty, valid host")
    must(new.returncode == 11 and TS.SEPARATOR not in new.stdout, f"the fixed script hides the failure: rc={new.returncode}")
    policy = FakeHostNet(other=LOCAL_ROUTES + f"{CAND} dev wg0 table 51820 scope link \n")
    main_only = TS.State((), (), tuple(TS.parse_routes(policy.ip(["-4", "route", "show"])[1])), ())
    full = TS.State((), (), tuple(TS.parse_routes(policy.ip(["-4", "route", "show", "table", "all"])[1])), ())
    must(TS.conflicts(TS.parse_candidate(CAND), main_only) == [], "the main-only read reports the policy route")
    must(any(c.kind == "HOST_ROUTE" for c in TS.conflicts(TS.parse_candidate(CAND), full)), "the all-tables read misses the policy route")
    return (f"first version: failed route read + good address read -> exit {old.returncode}, an empty route table and a free subnet; fixed script: exit {new.returncode}. "
            "main-only read: policy-table route invisible (free); all-tables read: HOST_ROUTE")


# ── the operational tool (scripts/tunnel_subnet.py): a caller continues ONLY on 0 ───────────────
def t_unreadable_pool_and_unexpected_errors_are_exit_3():
    base = FakeHostNet()

    def with_info(text):
        def runner(cmd, check=False, **kw):
            if [str(c) for c in cmd][:2] == ["docker", "info"]:
                return types.SimpleNamespace(returncode=0, stdout=text, stderr="")
            return base(cmd)
        return runner
    for what, text in (("an unreadable pool", '[{"Base":"bogus","Size":24}]'), ("a pool that is not a network", '[{"Base":"10.0.0.0/33"}]')):
        for argv in (["check", CAND], ["allocate", "--project", "inst"]):
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                rc = TS.main(argv + ["--routes", "host"], runner=with_info(text))
            must(rc == 3 and "unreadable address pool" in sink.getvalue() and "EDU_AI_TUNNEL_SUBNET=" not in sink.getvalue(), f"{what} {argv[0]}: rc={rc} {sink.getvalue().strip()[:200]}")

    def broken_inspect(cmd, check=False, **kw):                 # a daemon answer of an unexpected shape: not a crash, not a verdict
        c = [str(x) for x in cmd]
        if c[:3] == ["docker", "network", "ls"]:
            return types.SimpleNamespace(returncode=0, stdout="id1\n", stderr="")
        if c[:3] == ["docker", "network", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="[1, 2]", stderr="")
        return base(cmd)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        rc = TS.main(["check", CAND, "--routes", "host"], runner=broken_inspect)
    must(rc == 3 and "unexpected" in sink.getvalue(), f"an unexpected error while reading: rc={rc} {sink.getvalue().strip()[:200]}")
    return "an unreadable Docker pool and an answer of an unexpected shape: exit 3 for check and allocate, no subnet printed (formerly a traceback, exit 1)"


def t_model_checks():
    import tempfile
    sub = "10.77.3.8/29"
    m = model(sub)
    m["services"]["nginx"]["image"] = "nginx:1.27-alpine"

    def cli(argv, mdl):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            rc = TS.main(argv, stdin=io.StringIO(json.dumps(mdl)))
        return rc, sink.getvalue()
    must(TS.wiring_problems(m) == [], f"the designed wiring has problems: {TS.wiring_problems(m)}")
    wrong = {"cloudflared still on edge": lambda x: x["services"]["cloudflared"]["networks"].update({"edge": None}),
             "cloudflared without tunnel_egress": lambda x: x["services"]["cloudflared"]["networks"].pop("tunnel_egress"),
             "nginx not on edge": lambda x: x["services"]["nginx"]["networks"].pop("edge"),
             "another member of tunnel_egress": lambda x: x["services"]["search"]["networks"].update({"tunnel_egress": None}),
             "tunnel_egress internal": lambda x: x["networks"].update({"tunnel_egress": {"internal": True}})}
    for what, mutate in wrong.items():
        mm = json.loads(json.dumps(m))
        mutate(mm)
        must(TS.wiring_problems(mm), f"{what}: not detected")
    rc, out = cli(["model-subnet"], m)
    must(rc == 0 and out.strip() == sub, f"model-subnet: rc={rc} {out!r}")
    rc, out = cli(["render-trust"], m)
    must(rc == 0 and out == f"set_real_ip_from {sub};\n", f"render-trust: rc={rc} {out!r}")
    rc, out = cli(["wiring", "cloudflared"], m)
    must(rc == 0 and out.split() == ["app", "tunnel", "tunnel_egress"], f"wiring: {out!r}")
    rc, out = cli(["image", "nginx"], m)
    must(rc == 0 and out.strip() == "nginx:1.27-alpine", f"image: {out!r}")
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "nginx.conf").write_text(GOOD_CONF)
        (d / "search_api.conf").write_text("# nothing\n")
        rc, out = cli(["model-check", "--conf-dir", str(d)], m)
        must(rc == 0 and out.startswith("OK"), f"a correct model was refused: rc={rc} {out!r}")
        (d / "nginx.conf").write_text(GOOD_CONF + "  set_real_ip_from 172.16.0.0/12;\n")
        rc, out = cli(["model-check", "--conf-dir", str(d)], m)
        must(rc == 2 and "own copy" in out, f"the legacy range was accepted in the final state: rc={rc} {out!r}")
        rc, out = cli(["model-check", "--conf-dir", str(d), "--allow-legacy"], m)
        must(rc == 0, f"the legacy range was refused during step 2: rc={rc} {out!r}")
        rc, out = cli(["model-check", "--conf-dir", str(d / "missing")], m)
        must(rc == 2, f"a missing conf directory: rc={rc}")
    for what, bad in (("no tunnel network", {"services": {}}), ("two subnets", {"networks": {"tunnel": {"ipam": {"config": [{"subnet": sub}, {"subnet": "10.1.0.0/29"}]}}}})):
        rc, out = cli(["model-subnet"], bad)
        must(rc == 2 and "REFUSED" in out, f"{what}: rc={rc} {out!r}")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        rc = TS.main(["model-subnet"], stdin=io.StringIO("not json"))
    must(rc == 2 and "unreadable" in sink.getvalue(), f"garbage on stdin: rc={rc}")
    secret = json.loads(json.dumps(m))
    secret["services"]["cloudflared"]["environment"] = {"TUNNEL_TOKEN": "s3cr3t-token-value"}
    for argv in (["model-subnet"], ["render-trust"], ["wiring", "cloudflared"], ["image", "cloudflared"]):
        rc, out = cli(argv, secret)
        must("s3cr3t" not in out, f"{argv[0]} printed a secret of the model")
    return "wiring of nginx / cloudflared / tunnel_egress (5 deviations detected), subnet, trust file, image and single-source checks of the effective model; garbage and a missing directory refused; no secret of the model is ever printed"


# ── one source of the CIDR ───────────────────────────────────────────────
def model(subnet="10.77.3.8/29", trust=None, mounted=True, members=("cloudflared", "nginx"), internal=True):
    trust = subnet if trust is None else trust
    svc = {"nginx": {"networks": {"edge": None, "tunnel": None},
                     "configs": [{"source": "tunnel_trust", "target": "/etc/edu_ai/tunnel/trust.conf"}] if mounted else []},
           "cloudflared": {"networks": {"app": None, "tunnel": None, "tunnel_egress": None}}}
    for m in ("search", "api"):
        svc[m] = {"networks": {"edge": None, "tunnel": None} if m in members else {"edge": None}}
    return {"networks": {"tunnel": {"internal": internal, "ipam": {"config": [{"subnet": subnet}]}}},
            "configs": {"tunnel_trust": {"content": f"set_real_ip_from {trust};\n"}}, "services": svc}


GOOD_CONF = "server {\n  # note set_real_ip_from 1.2.3.4/29;\n  include /etc/edu_ai/tunnel/*.conf;\n  real_ip_header CF-Connecting-IP;\n}\n"


def t_single_source():
    sub = "10.77.3.8/29"
    must(TS.single_source_problems(model(sub), {"nginx.conf": GOOD_CONF}, sub) == [], f"a correct setup has problems: {TS.single_source_problems(model(sub), {'nginx.conf': GOOD_CONF}, sub)}")
    bad = {
        "second constant in nginx.conf": (model(sub), {"nginx.conf": GOOD_CONF + "  set_real_ip_from 10.250.250.0/29;\n"}, "own copy"),
        "config differs from the network": (model(sub, trust="10.250.250.0/29"), {"nginx.conf": GOOD_CONF}, "second, independent constant"),
        "network differs from the setting": (model("10.88.1.0/29", trust="10.88.1.0/29"), {"nginx.conf": GOOD_CONF}, "not the configured"),
        "config not mounted": (model(sub, mounted=False), {"nginx.conf": GOOD_CONF}, "does not mount"),
        "no include": (model(sub), {"nginx.conf": "server {\n}\n"}, "does not include"),
        "tunnel not internal": (model(sub, internal=False), {"nginx.conf": GOOD_CONF}, "not internal"),
        "extra member on tunnel": (model(sub, members=("cloudflared", "nginx", "api")), {"nginx.conf": GOOD_CONF}, "members of `tunnel`"),
    }
    for name, (m, c, frag) in bad.items():
        got = TS.single_source_problems(m, c, sub)
        must(got and any(frag in g for g in got), f"{name}: not detected ({got})")
    legacy = GOOD_CONF + "  set_real_ip_from 172.16.0.0/12;\n"
    must(TS.single_source_problems(model(sub), {"nginx.conf": legacy}, sub) != [], "the legacy range was tolerated outside the rollout")
    must(TS.single_source_problems(model(sub), {"nginx.conf": legacy}, sub, allow_legacy=True) == [], "the legacy range was refused during the rollout")
    return "7 broken setups detected (second constant, mismatching config/network, unmounted, no include, not internal, extra member); the rollout's legacy line only tolerated when allowed"


def t_candidate_derivation_offline():
    import candidate as C
    ns = types.SimpleNamespace(ref=C.S2_BASE)          # the deployed S2 commit, not HEAD: the implementation sits on top of it
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = C.cmd_check(ns)
    must(rc == 0, "candidate.py check failed: " + buf.getvalue().strip()[:300])
    return buf.getvalue().strip()[:150]


TESTS = [
    ("cleanup.own_removed", "an owned run is removed by id, containers before networks", t_own_removed_by_id_in_order),
    ("cleanup.foreign_similar_name", "a foreign network / unlabelled container with a similar name is kept", t_foreign_similar_name_kept),
    ("cleanup.original_bug_gone", "the name-based removal of the first version cannot happen", t_original_bug_is_gone),
    ("cleanup.old_version", "the first version of the cleanup is reproduced and caught (R2-R2)", t_old_version_reproduces_the_finding),
    ("cleanup.other_run", "another active run's resources are kept", t_other_run_kept),
    ("cleanup.wrong_purpose", "a resource with our run label but not our purpose is kept", t_wrong_purpose_with_our_run_label_kept),
    ("cleanup.inspect_failure", "a resource whose inspection fails is kept and reported", t_inspect_failure_kept),
    ("cleanup.compose_project", "a Compose resource must belong to this run's exact project", t_compose_project_must_match_this_run),
    ("cleanup.run_id_required", "a concrete, well-formed run id is required", t_run_id_required_and_validated),
    ("cleanup.safe_remove", "removal by a planned name verifies ownership first", t_safe_remove_refuses_foreign_name_and_handles_absent),
    ("cleanup.dry_run", "a dry run removes nothing", t_dry_run_removes_nothing),
    ("cleanup.list_runs", "run ids are listed only from labelled resources", t_list_runs_only_labelled),
    ("subnet.validation", "the administrator's value must be canonical, small, private", t_candidate_value_validation),
    ("subnet.conflicts", "every kind of overlap is refused, a free block passes", t_conflict_kinds),
    ("subnet.own_network", "an installation's own network is not a collision, a different subnet is a mismatch", t_own_network_is_not_a_conflict_but_a_mismatch_is),
    ("subnet.own_bridge", "an installation's own bridge route/address is not a collision for itself", t_own_bridge_is_not_a_collision),
    ("subnet.parsing", "route and address tables are parsed", t_route_and_address_parsing),
    ("subnet.allocation", "allocation is deterministic, spread, free and canonical", t_allocation),
    ("subnet.unverifiable", "an uninspectable host is refused", t_unverifiable_host_is_refused),
    ("subnet.cli_exit_codes", "the command line refuses with 2 or 3 and never allocates when unverifiable", t_cli_exit_codes),
    ("subnet.pool_unreadable", "an unreadable Docker pool or an unexpected answer is exit 3, never a crash (operational tool)", t_unreadable_pool_and_unexpected_errors_are_exit_3),
    ("subnet.model_checks", "the checks of the effective Compose model: wiring, subnet, trust file, image; no secret is printed", t_model_checks),
    ("subnet.read.route_failure", "a failed route read is unverifiable (exit 3) although the address read succeeds (R2-R3)", t_route_read_failure_is_unverifiable),
    ("subnet.read.address_failure", "a failed address read is unverifiable (exit 3) although the route read succeeds (R2-R3)", t_address_read_failure_is_unverifiable),
    ("subnet.read.other_table", "a collision only in a non-main routing table is found (exit 2) and never allocated (R2-R3)", t_collision_only_in_another_table),
    ("subnet.read.no_main_fallback", "an `ip` that rejects `table all` is refused, never read as main only (R2-R3)", t_no_silent_fall_back_to_main),
    ("subnet.read.complete", "a complete valid read without a collision passes in both modes with the same inventory", t_complete_valid_read_passes),
    ("subnet.read.own_installation", "a running installation passes repeated preflights with local and bridge routes included", t_own_installation_with_all_tables),
    ("subnet.read.empty_vs_failed", "empty output of a successful read is a result, a failed or unreadable one is not", t_empty_is_not_failed),
    ("subnet.read.first_version", "the first helper script and the main-only read are reproduced and caught (R2-R3)", t_first_version_reproduces_the_finding),
    ("subnet.single_source", "a second constant or a mismatching config is detected", t_single_source),
    ("candidate.offline", "the proposal derives from git with ONE source of the CIDR", t_candidate_derivation_offline),
]


import switch_offline as SW  # noqa: E402  (the judgements of the switch script and the S2 guard: R2-R4, R2-R5; needs bash, git and a docker client, no daemon)
TESTS += SW.TESTS


def run_all(quiet=False):
    results = []
    for tid, desc, fn in TESTS:
        try:
            detail, status = fn(), "PASS"
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except Exception as e:  # noqa: BLE001
            detail, status = f"{type(e).__name__}: {e}", "ERROR"
        results.append((tid, desc, status, detail))
        if not quiet:
            print(f"  {status:5} {tid}  {desc}\n        {detail}")
    return results


if __name__ == "__main__":
    res = run_all()
    bad = [r for r in res if r[2] != "PASS"]
    print(f"\n{len(res) - len(bad)}/{len(res)} PASS")
    sys.exit(1 if bad else 0)
