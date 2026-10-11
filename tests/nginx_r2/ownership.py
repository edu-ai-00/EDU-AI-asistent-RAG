#!/usr/bin/env python3
"""Ownership of R2 lab resources and the cleanup that is allowed to rely on it. Standard library only.

A lab resource is OURS only when its LABELS say so: purpose == b04r2-nginx-lab, edu_ai.run == the run id, and, for what Compose
created, the Compose project label equals the project recorded for this run (edu_ai.project) and has the exact form of this run's
project name. A name, or a part of a name, never decides: `operator_b04r2_shared` is not ours, and neither is another run's network.
Whatever cannot be verified (inspection failed, no labels, an unexpected answer) is KEPT and reported, never removed.

Nothing here selects resources by name for removal. Every function takes a `runner(cmd, check=False)` so the offline tests can
replace Docker with a fake and prove the commands that would have been issued.
"""
from __future__ import annotations

import json
import re
from typing import Callable, NamedTuple

PURPOSE = "b04r2-nginx-lab"
LABEL_PURPOSE, LABEL_RUN, LABEL_PROJECT = "edu_ai.purpose", "edu_ai.run", "edu_ai.project"
COMPOSE_PROJECT = "com.docker.compose.project"
RUN_ID_RE = re.compile(r"^\d{8}[0-9a-f]{4}$")          # MMDDhhmm + 4 hex digits, see R2Lab.__init__


class OwnershipError(Exception):
    pass


class InspectError(OwnershipError):
    """The resource could not be inspected: its owner is unknown, so it is not touched."""


class Refused(OwnershipError):
    """The resource exists but is not owned by this run: it is not touched."""


class CleanupResult(NamedTuple):
    removed: list          # (kind, id)
    kept: list             # (kind, id, reason)
    errors: list           # text
    lookalikes: list       # names that merely resemble ours (listed, never removed)

    def ok(self):
        return not self.kept and not self.errors


Runner = Callable[..., object]

_KINDS = {
    "container": {"ls": ["docker", "ps", "-aq"], "inspect": lambda i: ["docker", "inspect", "--type", "container", "--format", "{{json .Config.Labels}}", i],
                  "rm": lambda i: ["docker", "rm", "-f", i]},
    "network": {"ls": ["docker", "network", "ls", "-q"], "inspect": lambda i: ["docker", "network", "inspect", "--format", "{{json .Labels}}", i],
                "rm": lambda i: ["docker", "network", "rm", i]},
    "volume": {"ls": ["docker", "volume", "ls", "-q"], "inspect": lambda i: ["docker", "volume", "inspect", "--format", "{{json .Labels}}", i],
               "rm": lambda i: ["docker", "volume", "rm", "-f", i]},
}
ORDER = ("container", "network", "volume")             # containers first, so the networks they hold can go


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.fullmatch(run_id or ""))


def project_re(run_id: str):
    return re.compile(rf"^b04r2_{run_id}_p_[a-z0-9_]+$")


def owned(labels, run_id: str):
    """(True, '') or (False, reason). `labels` is what Docker printed for the resource (None when it has none)."""
    if not valid_run_id(run_id):
        return False, f"{run_id!r} is not a run id"
    if not isinstance(labels, dict):
        return False, "no labels"
    if labels.get(LABEL_PURPOSE) != PURPOSE:
        return False, "purpose label missing or not the lab's"
    if labels.get(LABEL_RUN) != run_id:
        return False, f"belongs to another run ({labels.get(LABEL_RUN)!r})"
    proj = labels.get(COMPOSE_PROJECT)
    if proj is not None:
        if labels.get(LABEL_PROJECT) != proj:
            return False, "Compose project is not the project recorded for this run"
        if not project_re(run_id).fullmatch(proj):
            return False, f"Compose project {proj!r} is not a project of this run"
    return True, ""


def get_labels(runner: Runner, kind: str, ident: str):
    """The labels of one resource. An inspection that fails, prints no JSON or finds nothing raises InspectError."""
    p = runner(_KINDS[kind]["inspect"](ident), check=False)
    if p.returncode != 0:
        text = (p.stderr or p.stdout or "").strip()[:200]
        if "no such" in text.lower():
            raise FileNotFoundError(ident)
        raise InspectError(f"inspect {kind} {ident} failed (rc={p.returncode}): {text}")
    try:
        return json.loads((p.stdout or "").strip() or "null")
    except json.JSONDecodeError as e:
        raise InspectError(f"inspect {kind} {ident} printed invalid JSON: {e}")


def safe_remove(runner: Runner, kind: str, ident: str, run_id: str) -> str:
    """Remove ONE resource this run owns. Returns 'removed' or 'absent'; raises Refused (not ours) or InspectError (unknown)."""
    try:
        labels = get_labels(runner, kind, ident)
    except FileNotFoundError:
        return "absent"
    ok, why = owned(labels, run_id)
    if not ok:
        raise Refused(f"refusing to remove {kind} {ident}: {why}")
    p = runner(_KINDS[kind]["rm"](ident), check=False)
    if p.returncode != 0:
        raise OwnershipError(f"could not remove {kind} {ident}: {(p.stderr or p.stdout or '').strip()[:200]}")
    return "removed"


def list_ids(runner: Runner, kind: str, run_id: str):
    """Candidates: ONLY by the run label. A name filter is never used to choose what to remove."""
    p = runner(_KINDS[kind]["ls"] + ["--filter", f"label={LABEL_RUN}={run_id}"], check=False)
    if p.returncode != 0:
        raise InspectError(f"listing {kind}s of run {run_id} failed (rc={p.returncode}): {(p.stderr or p.stdout or '').strip()[:200]}")
    return [x for x in (p.stdout or "").split() if x]


def lookalikes(runner: Runner, run_id: str, own_ids):
    """Names that contain this run's prefix but are not among the owned resources: reported, never removed."""
    out = []
    for kind, cmd in (("container", ["docker", "ps", "-a", "--format", "{{.ID}} {{.Names}}"]),
                      ("network", ["docker", "network", "ls", "--format", "{{.ID}} {{.Name}}"])):
        p = runner(cmd + ["--filter", f"name=b04r2_{run_id}"], check=False)
        if p.returncode != 0:
            continue
        for line in (p.stdout or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] not in own_ids.get(kind, ()):
                out.append((kind, parts[0], parts[1]))
    return out


def cleanup_run(runner: Runner, run_id: str, dry_run: bool = False) -> CleanupResult:
    """Remove what THIS run owns and nothing else. Anything unverifiable or foreign is kept and listed."""
    if not valid_run_id(run_id):
        raise ValueError(f"{run_id!r} is not a run id of the form MMDDhhmm + 4 hex digits")
    removed, kept, errors = [], [], []
    plan = {}
    for kind in ORDER:
        try:
            ids = list_ids(runner, kind, run_id)
        except InspectError as e:
            errors.append(str(e))
            continue
        for ident in ids:
            try:
                labels = get_labels(runner, kind, ident)
            except FileNotFoundError:
                continue                                     # gone meanwhile
            except InspectError as e:
                kept.append((kind, ident, f"cannot verify ownership: {e}"))
                continue
            ok, why = owned(labels, run_id)
            if not ok:
                kept.append((kind, ident, why))
                continue
            plan.setdefault(kind, []).append(ident)
    for kind in ORDER:
        for ident in plan.get(kind, []):
            if dry_run:
                removed.append((kind, ident))
                continue
            try:
                if safe_remove(runner, kind, ident, run_id) == "removed":
                    removed.append((kind, ident))
            except OwnershipError as e:
                kept.append((kind, ident, str(e)))
    look = lookalikes(runner, run_id, {k: set(v) for k, v in plan.items()})
    return CleanupResult(removed, kept, errors, look)


def list_runs(runner: Runner):
    """Run ids found on resources that carry the lab's purpose label (read-only)."""
    runs = set()
    for kind in ORDER:
        p = runner(_KINDS[kind]["ls"] + ["--filter", f"label={LABEL_PURPOSE}={PURPOSE}"], check=False)
        if p.returncode != 0:
            raise InspectError(f"listing {kind}s failed: {(p.stderr or p.stdout or '').strip()[:200]}")
        for ident in (p.stdout or "").split():
            try:
                labels = get_labels(runner, kind, ident)
            except (FileNotFoundError, InspectError):
                continue
            if isinstance(labels, dict) and labels.get(LABEL_PURPOSE) == PURPOSE and valid_run_id(labels.get(LABEL_RUN, "")):
                runs.add(labels[LABEL_RUN])
    return sorted(runs)
