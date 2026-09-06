#!/usr/bin/env python3
"""Batching: group issues so one slow suite run covers several fixes.

A 40-minute suite costs 40 minutes whether the PR fixes one issue or five, so
grouping compatible issues is the largest single saving available on a repo with
slow CI. The cost of grouping is that a failure is harder to attribute — which is
why every issue gets its own commit and `split()` exists.

CLI:
    batch.py plan --triage triage.json [--ledger .foreman] [--config .foreman/config.json]
        [--profile .foreman/ci-profile.json]
    batch.py apply --plan batches.json [--ledger .foreman]
    batch.py paths --batch b-001 --base main [--head HEAD] [--ledger .foreman]
        [--repo-dir .] [--apply]

`plan` reads the ledger too, to allocate ids that continue past the ones already
issued, so its --ledger must name the same directory `apply` will write to.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

WEIGHT = {"small": 1, "medium": 2, "large": 4}
RISK_ORDER = ["low", "medium", "high"]
DEFAULT_MAX_ISSUES = 5
DEFAULT_MAX_WEIGHT = 6


class CannotSplit(Exception):
    pass


class PathsUnavailable(Exception):
    pass


def _rank(risk: str) -> int:
    return RISK_ORDER.index(risk) if risk in RISK_ORDER else len(RISK_ORDER)


def ceiling_problem(config: dict) -> str | None:
    """Why the configured risk ceiling cannot be applied, or None when it can.

    Fails closed. `_rank` puts an unknown value above `high`, which is right
    for an issue's risk (unknown rounds up) and exactly wrong for the ceiling:
    a misspelling — `"Medium"`, `"strict"`, a trailing space — ranked above
    every real risk, so nothing ever exceeded it and two high-risk issues
    shared a PR without a word said.
    """
    ceiling = config.get("risk_ceiling", "medium")
    if ceiling in RISK_ORDER:
        return None
    return (
        f"risk_ceiling {ceiling!r} is not one of {RISK_ORDER}; nothing shares a batch until it is"
    )


def can_group(a: dict, b: dict, config: dict) -> tuple[bool, str]:
    """Whether two issues may share a PR, and if not, why not."""
    problem = ceiling_problem(config)
    if problem:
        return False, problem
    ceiling = config.get("risk_ceiling", "medium")
    for record in (a, b):
        if _rank(record.get("risk", "high")) > _rank(ceiling):
            return False, (
                f"#{record['issue']} risk {record.get('risk')} exceeds "
                f"the batching ceiling {ceiling}"
            )

    pa = set(a.get("paths") or [])
    pb = set(b.get("paths") or [])
    if not pa or not pb:
        # Absence of path information is not evidence of independence. Batching
        # on a guess produces conflicts inside a batch, which is self-inflicted.
        return False, "unknown paths: independence cannot be established"

    overlap = pa & pb
    if overlap:
        return False, f"both touch {sorted(overlap)[0]}"
    return True, ""


def _id_number(batch_id: str) -> int:
    """The numeric part of a batch id, so ids continue past it. 0 when unreadable."""
    match = re.match(r"b-(\d+)", str(batch_id))
    return int(match.group(1)) if match else 0


def group_issues(records: list[dict], config: dict, taken: set[str] | None = None) -> list[dict]:
    """Greedily pack actionable issues into batches. Deterministic by issue number."""
    limits = config.get("limits", {})
    max_issues = limits.get("max_batch_issues", DEFAULT_MAX_ISSUES)
    max_weight = limits.get("max_batch_weight", DEFAULT_MAX_WEIGHT)

    queue = sorted(
        (r for r in records if r.get("verdict") == "actionable"), key=lambda r: r["issue"]
    )

    packed: list[list[dict]] = []
    for record in queue:
        weight = WEIGHT.get(record.get("size"), 2)
        for group in packed:
            if len(group) >= max_issues:
                continue
            if sum(WEIGHT.get(g.get("size"), 2) for g in group) + weight > max_weight:
                continue
            if all(can_group(existing, record, config)[0] for existing in group):
                group.append(record)
                break
        else:
            packed.append([record])  # incompatible with every open batch, so it starts one

    # Ids must continue past everything the ledger already holds. Numbering from
    # 1 each run reuses the id of an earlier batch, and the fold keys batches by
    # id, so a merged batch's history is what gets overwritten.
    taken = set(taken or ())
    # Continue past the highest id ever issued rather than filling gaps: a gap
    # may be an id whose events were pruned or rotated away, and reusing it
    # would merge two unrelated batches under one key.
    index = max((_id_number(t) for t in taken), default=0)

    def _next_id() -> str:
        nonlocal index
        while True:
            index += 1
            candidate = f"b-{index:03d}"
            if candidate not in taken:
                taken.add(candidate)
                return candidate

    batches = []
    for group in packed:
        paths = sorted({p for r in group for p in (r.get("paths") or [])})
        batches.append(
            {
                "id": _next_id(),
                "issues": [r["issue"] for r in group],
                "paths": paths,
                "risk": max((r.get("risk", "medium") for r in group), key=_rank),
                "weight": sum(WEIGHT.get(r.get("size"), 2) for r in group),
                "titles": {r["issue"]: r.get("title") for r in group},
            }
        )
    return batches


def estimate_savings(batches: list[dict], profile: dict) -> dict:
    """CI runs not taken. The whole argument for batching, in one number."""
    issues = sum(len(b["issues"]) for b in batches)
    runs_saved = issues - len(batches)

    suite = None
    if profile.get("cheap_tier_s") is not None and profile.get("expensive_tier_s") is not None:
        suite = profile["cheap_tier_s"] + profile["expensive_tier_s"]

    return {
        "issues": issues,
        "batches": len(batches),
        "suite_runs_saved": runs_saved,
        "suite_seconds": suite,
        "seconds_saved": None if suite is None else runs_saved * suite,
        "batched_s": None if suite is None else len(batches) * suite,
        "unbatched_s": None if suite is None else issues * suite,
    }


def split(batch: dict, failing_issue: int) -> tuple[dict, dict]:
    """Peel a failing issue off so the rest of the batch keeps moving.

    This is what keeps batching worth it: a bad commit costs one extra suite
    run, not a redo of everything that was already fine.
    """
    issues = list(batch.get("issues") or [])
    if len(issues) < 2:
        raise CannotSplit(f"{batch.get('id')} holds one issue; there is nothing to split off")
    if failing_issue not in issues:
        raise CannotSplit(f"#{failing_issue} is not in {batch.get('id')} ({issues})")

    base = batch["id"]
    common = {
        "paths": batch.get("paths", []),
        "risk": batch.get("risk", "medium"),
        "split_from": base,
    }
    failing = {**common, "id": f"{base}a", "issues": [failing_issue]}
    rest = {**common, "id": f"{base}b", "issues": [i for i in issues if i != failing_issue]}
    return failing, rest


# --- what a batch actually changed --------------------------------------------
# A batch's paths start as the union of the file paths its issues' prose happens
# to name. That is a statement of intent, and prose is a poor source: it invents
# files that do not exist and stays silent about files the fix had to touch. The
# protected-path merge gate reads those paths, so guarding intent instead of the
# change is a hole in it. These read the branch's real diff instead.


def diff_paths(base: str, head: str = "HEAD", repo: Path | str | None = None) -> list[str]:
    """The files a branch changes, from git.

    Three dots, not two: `base...head` diffs head against the merge base, so
    everything that landed on trunk after the branch started is excluded. With
    two dots a busy trunk makes every batch look like it touched half the repo.

    Every way the diff can fail to arrive raises `PathsUnavailable`, including
    git not being runnable at all — the same refusal `gate._git` makes, for the
    same reason. Converting only a non-zero exit left `FileNotFoundError` to
    reach the caller as a traceback instead of the one-line reason `main` is
    written to print.
    """
    try:
        done = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PathsUnavailable(f"git diff {base}...{head} could not be run: {exc}") from exc
    if done.returncode != 0:
        # Never return [] here. An empty path list clears the protected-path
        # gate, so a failed git call would read as "this batch is safe".
        raise PathsUnavailable(
            f"git diff {base}...{head} failed: {done.stderr.strip() or 'no output'}"
        )
    return sorted({line.strip() for line in done.stdout.splitlines() if line.strip()})


def head_commit(head: str = "HEAD", repo: Path | str | None = None) -> str:
    """The full SHA `head` names, so an observation can say which commit it is of.

    A path list is a statement about one commit, exactly as a gate verdict is.
    `land.merge_blockers` compares this against the commit actually being
    merged, so a list confirmed before a later push cannot clear the gate for
    code it never saw (issue #76).
    """
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", f"{head}^{{commit}}"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PathsUnavailable(f"git rev-parse {head} could not be run: {exc}") from exc
    if done.returncode != 0 or not done.stdout.strip():
        raise PathsUnavailable(f"git rev-parse {head} failed: {done.stderr.strip() or 'no output'}")
    return done.stdout.strip()


def observed_paths(
    batch: dict, base: str, head: str = "HEAD", repo: Path | str | None = None
) -> dict:
    """A batch's declared paths against the ones it really changed.

    `paths` is the answer — the value that should replace the declared list. The
    other two keys are the drift, and both are worth seeing: `undeclared` is the
    protected file no issue mentioned, `untouched` is the file the prose invented.
    `head_sha` is the commit the answer is about.

    An empty diff is not an answer, so it raises rather than returning one. git
    exiting 0 with no output means "this branch changes nothing", and a batch
    that changes nothing has produced no observation to replace intent with. The
    two ways to arrive here are indistinguishable from inside: the batch has
    committed nothing yet, and `repo` names a checkout that does not hold the
    branch — the documented invocation run from the main worktree while the work
    sits in a linked one. Both used to answer `paths: []`, which `--apply` wrote
    over the declared list, and land.merge_blockers reads that list: the
    protected-path gate went quiet for exactly the batch nobody had looked at.
    """
    declared = sorted(set(batch.get("paths") or []))
    observed = diff_paths(base, head, repo)
    if not observed:
        raise PathsUnavailable(
            f"git diff {base}...{head} names no changed file, so there is nothing to "
            f"replace {batch.get('id')}'s declared paths with. Either the batch has "
            f"committed nothing yet, or --repo-dir names a checkout without its branch. "
            f"Its declared paths are left alone, and the merge gate keeps reading them."
        )
    return {
        "batch": batch.get("id"),
        "declared": declared,
        "paths": observed,
        "head_sha": head_commit(head, repo),
        "undeclared": [p for p in observed if p not in set(declared)],
        "untouched": [p for p in declared if p not in set(observed)],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--triage", required=True, help="output of `triage.py plan`")
    p.add_argument("--ledger", default=".foreman")
    p.add_argument(
        "--config", help="foreman config (default .foreman/config.json in the repository root)"
    )
    p.add_argument(
        "--profile", help="CI profile (default .foreman/ci-profile.json in the repository root)"
    )
    p = sub.add_parser("apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--ledger", default=".foreman")
    p = sub.add_parser("paths", help="recompute a batch's paths from its real diff")
    p.add_argument("--batch", required=True)
    p.add_argument("--base", required=True, help="the branch the PR would merge into")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--ledger", default=".foreman")
    p.add_argument("--repo-dir", default=".", help="the working tree holding the branch")
    p.add_argument("--apply", action="store_true", help="record the recomputed paths in the ledger")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "plan":
        triage_out = json.loads(Path(args.triage).read_text())
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ledger as ledger_mod

        # Both anchored to the repository, like the ledger (issue #74). Read
        # against the caller, a plan cut from a build worktree saw no config —
        # no limits, no risk ceiling — and no profile, so it could not say what
        # its batching saved.
        config = ledger_mod.load_config(args.config)
        profile = ledger_mod.load_profile(args.profile)

        ledger_root = Path(args.ledger)
        if not ledger_mod.resolve_root(ledger_root).exists():
            # Without the ledger the taken set is empty and ids restart at b-001,
            # colliding with batches the ledger already holds — and the fold keeps
            # the first record of an id, so apply's collision is silently dropped
            # while still being printed under created. Running from a directory
            # other than the repo root is the usual cause, and it is invisible
            # unless it is said out loud.
            print(
                f"warning: no ledger at {ledger_root}; batch ids will restart at b-001",
                file=sys.stderr,
            )
        problem = ceiling_problem(config)
        if problem:
            # Every issue comes out solo below, which is safe and useless;
            # without this line it is also silent.
            print(f"warning: {problem}", file=sys.stderr)
        taken = set(ledger_mod.load(ledger_root).batches)
        batches = group_issues(triage_out.get("triaged", []), config, taken=taken)
        print(
            json.dumps(
                {"batches": batches, "savings": estimate_savings(batches, profile)}, indent=2
            )
        )
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ledger as ledger_mod

    root = Path(args.ledger)

    if args.cmd == "paths":
        record = ledger_mod.load(root).batches.get(args.batch)
        if record is None:
            # The fold drops a batch.meta event for a batch it has never seen, so
            # --apply would report success and change nothing.
            print(f"no batch {args.batch!r} in {root}", file=sys.stderr)
            return 1
        try:
            seen = observed_paths(record, args.base, args.head, args.repo_dir)
        except PathsUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.apply:
            # batch.meta is the fold's channel for correcting a batch record, so
            # the recomputed paths become what land.merge_blockers reads — and
            # `paths_head` is how it knows they are about the commit it is
            # merging rather than an earlier one.
            ledger_mod.append(
                root,
                "batch.meta",
                batch=args.batch,
                paths=seen["paths"],
                paths_head=seen["head_sha"],
            )
        print(json.dumps(seen, indent=2))
        return 0

    plan = json.loads(Path(args.plan).read_text())
    for item in plan.get("batches", []):
        ledger_mod.append(
            root,
            "batch.created",
            batch=item["id"],
            issues=item["issues"],
            paths=item.get("paths", []),
            risk=item.get("risk"),
        )
    print(json.dumps({"created": [b["id"] for b in plan.get("batches", [])]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
