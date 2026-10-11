"""Layout, baseline/release manifests and migration-file discovery (no database access)."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import fingerprint

TOOL = "scripts/migrate.py v1"
FIRST_MIGRATION = 217
MIGRATION_FILE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
BASELINE_ID = "B216"


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def init_clean(self) -> Path:
        return self.root / "postgres" / "init_clean"

    @property
    def baseline_manifest(self) -> Path:
        return self.root / "postgres" / "baseline" / f"{BASELINE_ID}.json"

    @property
    def releases_dir(self) -> Path:
        return self.root / "postgres" / "releases"

    @property
    def migrations_dir(self) -> Path:
        return self.root / "postgres" / "migrations"

    @property
    def dockerfile(self) -> Path:
        return self.root / "Dockerfile.postgres"


def dump_json(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_json(obj).encode("utf-8"))  # LF on every platform


def load_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"{what} not found: {path}") from None
    except ValueError as e:
        raise ManifestError(f"{what} is not valid JSON: {path}: {e}") from None
    if data.get("format") != 1:
        raise ManifestError(f"{what}: unsupported format {data.get('format')!r}")
    return data


def load_baseline(layout: Layout) -> dict:
    data = load_json(layout.baseline_manifest, "baseline manifest")
    fp = data.get("fingerprint", {})
    if data.get("id") != BASELINE_ID or fp.get("algorithm") != fingerprint.ALGORITHM:
        raise ManifestError(f"baseline manifest is not {BASELINE_ID} / {fingerprint.ALGORITHM}")
    if fp.get("schema_dump_args") != fingerprint.SCHEMA_DUMP_ARGS \
            or fp.get("config_dump_args") != fingerprint.CONFIG_DUMP_ARGS \
            or fp.get("config_masked_columns") != list(fingerprint.MASK_COLUMNS):
        raise ManifestError("baseline manifest was generated with different fingerprint settings; regenerate it")
    return data


def dockerfile_base_image(layout: Layout) -> str:
    for line in layout.dockerfile.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*FROM\s+(\S+)\s*$", line)
        if m:
            image = m.group(1)
            if not re.search(r"@sha256:[0-9a-f]{64}$", image):
                raise ManifestError(f"{layout.dockerfile.name}: base image {image} is not pinned by digest")
            return image
    raise ManifestError(f"{layout.dockerfile.name}: no FROM line")


def init_clean_hashes(layout: Layout) -> dict[str, str]:
    return {f"postgres/init_clean/{p.name}": fingerprint.file_sha256(p.read_bytes())
            for p in sorted(layout.init_clean.glob("*.sql"))}


def verify_init_clean(layout: Layout, baseline: dict) -> list[str]:
    """Problems between the installation files on disk and the baseline manifest."""
    want = {f["path"]: f["sha256"] for f in baseline["files"]}
    have = init_clean_hashes(layout)
    out = [f"{p}: file is missing" for p in want if p not in have]
    out += [f"{p}: file is not in the baseline manifest" for p in have if p not in want]
    out += [f"{p}: sha256 differs from the baseline manifest (file was changed)"
            for p in want if p in have and have[p] != want[p]]
    return out


def git(layout: Layout, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(layout.root), *args], capture_output=True)
    if p.returncode != 0:
        raise ManifestError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode()


def git_blob(layout: Layout, rev: str, rel: str) -> bytes:
    p = subprocess.run(["git", "-C", str(layout.root), "show", f"{rev}:{rel}"], capture_output=True)
    if p.returncode != 0:
        raise ManifestError(f"git show {rev}:{rel} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


@dataclass(frozen=True)
class MigFile:
    id: str
    number: int
    path: Path
    sha256: str
    text: str


def list_migrations(directory: Path) -> list[MigFile]:
    """Migration files in numeric order; naming, numbering and uniqueness are enforced."""
    out: list[MigFile] = []
    if not directory.is_dir():
        return out
    for p in directory.iterdir():
        if p.suffix != ".sql":
            continue
        m = MIGRATION_FILE.match(p.name)
        if not m:
            raise ManifestError(f"{p.name}: migration file names look like NNN_name.sql (lowercase)")
        n = int(m.group(1))
        if n < FIRST_MIGRATION:
            raise ManifestError(f"{p.name}: migrations before {FIRST_MIGRATION} belong to baseline {BASELINE_ID}")
        data = p.read_bytes()
        out.append(MigFile(p.stem, n, p, fingerprint.file_sha256(data), data.decode("utf-8")))
    out.sort(key=lambda f: f.number)
    for a, b in zip(out, out[1:]):
        if a.number == b.number:
            raise ManifestError(f"{a.path.name} and {b.path.name}: duplicate number {a.number}")
    return out


def release_files(layout: Layout) -> list[Path]:
    return sorted(layout.releases_dir.glob("*.json")) if layout.releases_dir.is_dir() else []


def load_release(layout: Layout, name: str | None) -> dict | None:
    """The release manifest named by --release, else the only one that exists."""
    if name:
        data = load_json(layout.releases_dir / f"{name}.json", f"release manifest {name}")
    else:
        found = release_files(layout)
        if not found:
            return None
        if len(found) > 1:
            raise ManifestError("several release manifests exist; choose one with --release")
        data = load_json(found[0], "release manifest")
    return data


def validate_release(release: dict, baseline: dict) -> list[str]:
    out = []
    if release.get("baseline") != baseline["id"]:
        out.append(f"release manifest is for baseline {release.get('baseline')!r}, not {baseline['id']}")
    if release.get("baseline_schema_fingerprint") != baseline["fingerprint"]["schema"]:
        out.append("release manifest was generated from a different baseline fingerprint")
    if release.get("fingerprint_algorithm") != fingerprint.ALGORITHM:
        out.append("release manifest uses a different fingerprint algorithm")
    ids = [m["id"] for m in release.get("migrations", [])]
    if len(set(ids)) != len(ids):
        out.append("release manifest lists a migration twice")
    nums = [int(i.split("_", 1)[0]) for i in ids]
    if nums != sorted(nums):
        out.append("release manifest migrations are not in ascending order")
    return out
