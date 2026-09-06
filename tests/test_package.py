"""Packaging: the plugin tree at a ref, and nothing else, in an archive an installer can read."""

import hashlib
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One build of HEAD for the whole module; git archive is not free."""
    out = tmp_path_factory.mktemp("dist")
    return package.build(REPO, "HEAD", out)


def _tar_names(path):
    with tarfile.open(path, "r:*") as tf:
        return {m.name: m for m in tf.getmembers()}


def test_both_archives_and_the_checksums_are_written(built):
    tar, zip_ = (Path(a) for a in built["archives"])
    assert tar.name == f"foreman-{built['version']}.tar.gz" and tar.exists()
    assert zip_.name == f"foreman-{built['version']}.zip" and zip_.exists()
    sums = Path(built["sums"]).read_text().splitlines()
    assert [line.split()[1] for line in sums] == [tar.name, zip_.name]
    for line, archive in zip(sums, (tar, zip_), strict=True):
        assert line.split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_everything_sits_under_one_versioned_directory(built):
    tar, _ = built["archives"]
    prefix = f"foreman-{built['version']}/"
    # The directory entry itself is stored without the trailing slash.
    assert all(name == prefix[:-1] or name.startswith(prefix) for name in _tar_names(tar))


def test_the_archive_holds_what_an_install_needs(built):
    tar, _ = built["archives"]
    names = set(_tar_names(tar))
    prefix = f"foreman-{built['version']}/"
    for rel in (
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "hooks/hooks.json",
        "scripts/gh_safe.sh",
        "scripts/gh_guard.py",
        "scripts/ledger.py",
        "agents/reviewer.md",
        "skills/orchestration/SKILL.md",
        "commands/run.md",
        "README.md",
        "config.example.json",
    ):
        assert prefix + rel in names, rel


def test_tests_ci_and_local_state_do_not_ship(built):
    for archive in built["archives"]:
        names = (
            set(_tar_names(archive))
            if archive.endswith(".tar.gz")
            else set(zipfile.ZipFile(archive).namelist())
        )
        stripped = {n.split("/", 1)[1] for n in names if "/" in n}
        assert not any(n.startswith(("tests/", ".github/", ".foreman/")) for n in stripped)
        assert "pyproject.toml" not in stripped and ".gitignore" not in stripped


def test_the_wrapper_and_the_hook_stay_executable(built):
    tar, zip_ = built["archives"]
    prefix = f"foreman-{built['version']}/"
    members = _tar_names(tar)
    for rel in package.EXECUTABLE:
        assert members[prefix + rel].mode & 0o111, f"{rel} lost its mode in the tarball"
    with zipfile.ZipFile(zip_) as zf:
        for rel in package.EXECUTABLE:
            assert (zf.getinfo(prefix + rel).external_attr >> 16) & 0o111, f"{rel} in the zip"


def test_a_built_archive_verifies_clean(built):
    for archive in built["archives"]:
        assert package.verify(Path(archive)) == []
        assert package.verify(Path(archive), expect_version=built["version"]) == []


def test_the_version_comes_from_the_manifest_at_the_ref(built):
    """At the ref, not in the working tree: a bump not yet committed is not
    yet the version anything can be released at."""
    import subprocess

    at_head = subprocess.run(
        ["git", "-C", str(REPO), "show", "HEAD:.claude-plugin/plugin.json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert built["version"] == json.loads(at_head)["version"]
    assert built["version"] == package.version_at(REPO, "HEAD")


def test_verify_catches_an_archive_that_is_missing_what_the_manifest_declares(tmp_path):
    """A hand-made archive with a manifest naming a command that is not there."""
    bad = tmp_path / "foreman-9.9.9.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        manifest = json.dumps(
            {"version": "9.9.9", "commands": ["./commands/run.md"], "hooks": "./hooks/hooks.json"}
        ).encode()
        info = tarfile.TarInfo("foreman-9.9.9/.claude-plugin/plugin.json")
        info.size = len(manifest)
        import io

        tf.addfile(info, io.BytesIO(manifest))
        stray = tarfile.TarInfo("foreman-9.9.9/tests/test_x.py")
        tf.addfile(stray, io.BytesIO(b""))
    problems = package.verify(bad, expect_version="1.0.0")
    assert any("commands/run.md" in p for p in problems)
    assert any("hooks" in p for p in problems)
    assert any("must not ship" in p for p in problems)
    assert any("expected 1.0.0" in p for p in problems)


def test_the_cli_refuses_a_version_it_did_not_expect(tmp_path, capsys):
    rc = package.main(["--ref", "HEAD", "--out", str(tmp_path), "--expect-version", "0.0.0"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert all("expected 0.0.0" in " ".join(v) for v in out["problems"].values())


def test_a_ref_that_is_not_a_plugin_tree_is_refused(tmp_path, capsys):
    rc = package.main(["--ref", "HEAD~999", "--out", str(tmp_path)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
