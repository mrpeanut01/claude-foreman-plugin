#!/usr/bin/env python3
"""Package the plugin as a deployable archive, from a git ref.

A release is the plugin tree at a tag, packaged so it installs without git.
`git archive` is what builds it, on purpose: only tracked files ship, so the
local ledger, bytecode and a half-edited file cannot slip in; the archive is
reproducible for a ref, since mtimes come from the commit; and the paths that
ship are named here rather than discovered, so a test directory or a CI
workflow cannot ride along by being tracked.

CLI:
    package.py [--ref HEAD] [--out dist] [--expect-version X.Y.Z] [--repo DIR]

Writes <out>/foreman-<version>.tar.gz, <out>/foreman-<version>.zip and
<out>/SHA256SUMS, where <version> is read from .claude-plugin/plugin.json at
the ref, and then verifies each archive the way an installer would read it.
Exit 1 when verification finds a problem or the version is not the expected one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

NAME = "foreman"
MANIFEST = ".claude-plugin/plugin.json"

# Everything an installed plugin needs, and nothing else. The recipes invoke
# every script under scripts/ through ${CLAUDE_PLUGIN_ROOT}, so the whole
# directory ships; tests, CI and lint config stay behind.
SHIPPED = (
    ".claude-plugin",
    "agents",
    "commands",
    "hooks",
    "skills",
    "scripts",
    "README.md",
    "config.example.json",
)
# Files that must be executable inside the archive: the hook runs by path, and
# the recipes call the wrapper by path.
EXECUTABLE = ("scripts/gh_safe.sh", "scripts/gh_guard.py")
# Anything under these prefixes is a packaging mistake, whatever else is right.
NEVER = ("tests/", ".github/", ".foreman/", "__pycache__/")


class PackageError(Exception):
    pass


def _git(repo: Path, *args: str, binary: bool = False):
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=not binary, check=False
    )
    if done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else done.stderr.decode()
        raise PackageError(f"git {' '.join(args)} failed: {err.strip() or done.returncode}")
    return done.stdout


def version_at(repo: Path, ref: str) -> str:
    manifest = json.loads(_git(repo, "show", f"{ref}:{MANIFEST}"))
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise PackageError(f"{MANIFEST} at {ref} declares no version")
    return version


def shipped_paths(repo: Path, ref: str) -> list[str]:
    """The SHIPPED entries that exist at the ref, in SHIPPED order."""
    present = set(_git(repo, "ls-tree", "--name-only", ref).split())
    paths = [p for p in SHIPPED if p in present]
    missing = [p for p in (".claude-plugin", "skills", "scripts") if p not in present]
    if missing:
        raise PackageError(f"{ref} is not a plugin tree: no {', '.join(missing)}")
    return paths


def build(repo: Path, ref: str, out: Path) -> dict:
    """Both archives and their checksums. Returns what was written."""
    version = version_at(repo, ref)
    prefix = f"{NAME}-{version}/"
    paths = shipped_paths(repo, ref)
    out.mkdir(parents=True, exist_ok=True)
    archives = []
    for fmt, suffix in (("tar.gz", ".tar.gz"), ("zip", ".zip")):
        target = out / f"{NAME}-{version}{suffix}"
        _git(
            repo,
            "archive",
            f"--format={fmt}",
            f"--prefix={prefix}",
            "-o",
            str(target),
            ref,
            "--",
            *paths,
            binary=True,
        )
        archives.append(target)
    sums = out / "SHA256SUMS"
    sums.write_text(
        "".join(f"{hashlib.sha256(a.read_bytes()).hexdigest()}  {a.name}\n" for a in archives)
    )
    return {
        "version": version,
        "ref": ref,
        "archives": [str(a) for a in archives],
        "sums": str(sums),
    }


def _members(archive: Path) -> dict[str, int]:
    """Member path -> unix mode, for either format."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            return {i.filename: (i.external_attr >> 16) & 0o7777 for i in zf.infolist()}
    with tarfile.open(archive, "r:*") as tf:
        return {m.name + ("/" if m.isdir() else ""): m.mode for m in tf.getmembers()}


def _read(archive: Path, member: str) -> bytes:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            return zf.read(member)
    with tarfile.open(archive, "r:*") as tf:
        extracted = tf.extractfile(member)
        return extracted.read() if extracted else b""


def verify(archive: Path, expect_version: str | None = None) -> list[str]:
    """Everything wrong with an archive, as an installer would find it."""
    problems: list[str] = []
    members = _members(archive)
    if not members:
        return [f"{archive.name} is empty"]
    roots = {name.split("/", 1)[0] for name in members}
    if len(roots) != 1:
        return [f"{archive.name} has {len(roots)} top-level entries, not one directory"]
    prefix = f"{roots.pop()}/"
    inside = {name[len(prefix) :]: mode for name, mode in members.items() if name != prefix}

    if MANIFEST not in inside:
        return [f"{archive.name} carries no {MANIFEST}"]
    try:
        manifest = json.loads(_read(archive, prefix + MANIFEST))
    except (json.JSONDecodeError, KeyError) as exc:
        return [f"{MANIFEST} in {archive.name} does not parse: {exc}"]
    version = str(manifest.get("version") or "")
    if prefix != f"{NAME}-{version}/":
        problems.append(f"directory {prefix} does not match version {version!r}")
    if expect_version and version != expect_version:
        problems.append(f"manifest says {version}, expected {expect_version}")

    for kind in ("commands", "skills", "agents"):
        for declared in manifest.get(kind) or []:
            rel = declared[2:] if declared.startswith("./") else declared
            if rel not in inside and rel.rstrip("/") + "/" not in inside:
                problems.append(f"{kind} entry {declared} is not in the archive")
    hooks = manifest.get("hooks")
    if isinstance(hooks, str):
        rel = hooks[2:] if hooks.startswith("./") else hooks
        if rel not in inside:
            problems.append(f"hooks file {hooks} is not in the archive")
        else:
            spec = json.loads(_read(archive, prefix + rel))
            for entries in (spec.get("hooks") or {}).values():
                for entry in entries:
                    for hook in entry.get("hooks") or []:
                        script = hook.get("command", "").replace("${CLAUDE_PLUGIN_ROOT}/", "")
                        if script not in inside:
                            problems.append(f"hook runs {script}, which is not in the archive")
    for rel in EXECUTABLE:
        if rel in inside and not inside[rel] & 0o111:
            problems.append(f"{rel} is not executable in {archive.name}")
    for name in inside:
        if any(name.startswith(bad) or f"/{bad}" in name for bad in NEVER):
            problems.append(f"{name} must not ship")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="HEAD", help="the commit or tag to package")
    parser.add_argument("--out", default="dist", help="where the archives go")
    parser.add_argument("--expect-version", help="refuse unless the manifest says this")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)

    repo, out = Path(args.repo), Path(args.out)
    try:
        built = build(repo, args.ref, out)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    problems = {}
    for archive in built["archives"]:
        found = verify(Path(archive), args.expect_version)
        if found:
            problems[Path(archive).name] = found
    print(json.dumps({**built, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
