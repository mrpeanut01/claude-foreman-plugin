"""The manifest must describe what is actually on disk."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())


@pytest.mark.parametrize("kind", ["commands", "skills", "agents"])
def test_every_declared_path_exists(kind):
    missing = [p for p in MANIFEST.get(kind, []) if not (ROOT / p).exists()]
    assert missing == [], f"{kind} declared but absent: {missing}"


@pytest.mark.parametrize(
    "kind,pattern",
    [
        ("commands", "commands/*.md"),
        ("skills", "skills/*"),
        ("agents", "agents/*.md"),
    ],
)
def test_nothing_on_disk_is_left_undeclared(kind, pattern):
    declared = {(ROOT / p).resolve() for p in MANIFEST.get(kind, [])}
    found = {p.resolve() for p in ROOT.glob(pattern)}
    assert found - declared == set(), f"{kind} on disk but not in plugin.json"


@pytest.mark.parametrize(
    "skill", sorted((ROOT / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name
)
def test_each_skill_declares_a_name_and_description(skill):
    head = skill.read_text()[:1200]
    assert head.startswith("---"), f"{skill} has no frontmatter"
    assert re.search(r"^name:\s*\S+", head, re.M), f"{skill} has no name"
    description = re.search(r"^description:\s*(.+)$", head, re.M)
    assert description, f"{skill} has no description"
    assert len(description.group(1)) > 40, (
        "a description too short to trigger on is not a description"
    )


@pytest.mark.parametrize(
    "skill", sorted((ROOT / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name
)
def test_every_module_a_skill_links_to_exists(skill):
    links = re.findall(r"\]\((modules/[^)]+)\)", skill.read_text())
    missing = [item for item in links if not (skill.parent / item).exists()]
    assert missing == [], f"{skill.parent.name} links to missing modules: {missing}"


def test_the_agent_that_reviews_cannot_edit():
    """Separation of powers: reviewing and repairing are different jobs."""
    head = (ROOT / "agents" / "reviewer.md").read_text()[:800]
    tools = re.search(r"^tools:\s*(.+)$", head, re.M)
    assert tools, "reviewer declares no tool list"
    granted = {t.strip() for t in tools.group(1).split(",")}
    assert not granted & {"Edit", "Write", "NotebookEdit"}, "the reviewer must not be able to fix"


def test_the_marketplace_and_plugin_versions_agree():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["plugins"][0]["version"] == MANIFEST["version"]
    assert market["plugins"][0]["name"] == MANIFEST["name"]


@pytest.mark.parametrize(
    "recipe,starts_in,ends_in",
    [
        ("commands/build.md", "planned", "built"),
        ("commands/land.md", "built", "merged"),
    ],
)
def test_a_recipe_walks_a_legal_path_through_the_state_machine(recipe, starts_in, ends_in):
    """`open -> merging` is not a move, and the land recipe used to make it
    after requesting the merge: GitHub was merging while the ledger refused."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import ledger

    moves = re.findall(r'ledger\.py" transition <batch> (\w+)', (ROOT / recipe).read_text())
    assert moves, f"{recipe} moves no batch at all"
    current = starts_in
    for nxt in moves:
        if nxt == current:
            continue  # the documented resume, `building -> building`
        assert nxt in ledger.TRANSITIONS[current], (
            f"{recipe} moves {current} -> {nxt}, which ledger.transition refuses"
        )
        current = nxt
    assert current == ends_in


def test_every_script_the_docs_invoke_exists():
    referenced = set()
    for doc in list(ROOT.glob("commands/*.md")) + list(ROOT.glob("skills/**/*.md")):
        referenced |= set(re.findall(r"CLAUDE_PLUGIN_ROOT\}/(scripts/[\w./-]+)", doc.read_text()))
    missing = sorted(s for s in referenced if not (ROOT / s).exists())
    assert missing == [], f"docs invoke scripts that do not exist: {missing}"
