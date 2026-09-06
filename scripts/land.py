#!/usr/bin/env python3
"""Landing: read CI honestly, judge the reviewer, and refuse to merge on doubt.

Three deterministic decisions live here, because each one is a place where a
plausible-sounding judgement call would quietly cost you something:

  * which checks are worth waiting for (waiting on a human gate stalls the loop);
  * whether a `clean` review carries the evidence a clean review requires;
  * whether a failure is a flake or a bug (rerunning a bug hides it).

CLI:
    land.py checks   --pr N --repo OWNER/NAME [--profile .foreman/ci-profile.json]
    land.py verdict  --file verdict.json
    land.py blockers --batch b-001 [--ledger .foreman] [--config .foreman/config.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_profile import attribute  # noqa: E402
from globs import compile_glob, matches_any  # noqa: E402

PASSED = {"SUCCESS", "PASS", "NEUTRAL", "SKIPPED"}
# A cancelled check is not a pass. Leaving it in neither set parks it in
# actionable_pending forever, which is a hang.
FAILED = {
    "FAILURE",
    "FAIL",
    "ERROR",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "CANCELLED",
    "CANCELED",
    # Terminal: a stale result is never recomputed, so it must not sit in
    # pending waiting for a resolution that cannot come.
    "STALE",
}

# A pending entry matching any of these needs a person, so the loop must not
# sit waiting on it.
HUMAN_GATE = re.compile(
    r"(review\s+required|required\s+review|requires\s+review|approval\s+required"
    r"|waiting\s+for\s+approval|manual\s+approval|awaiting\s+approval)",
    re.I,
)

BLOCKING_LABELS = {"needs-human", "do-not-merge", "wip", "blocked", "hold"}

VERDICTS = {"clean", "changes_requested"}
SERIOUS = {"high", "medium", "critical", "blocker"}
FLAKE_CONFIDENCE = 0.7

CLEAR = {"ci": "full_green", "review": "clean"}

# Events that can produce a check on a pull request. A job wired to anything else
# will never report here, and requiring it would hang the gate forever.
# merge_group checks report against the merge queue ref, not the pull request,
# so a merge_group-only job never reports here.
PR_EVENTS = {"pull_request", "pull_request_target", "push"}

# Two reviewers disagreeing forever is deadlock; two reviewers finding different
# things each round is progress. Only the first is worth escalating on.
REVIEW_MATCH_THRESHOLD = 0.5
DEFAULT_REVIEW_CEILING = 5
_WORD = re.compile(r"[a-z0-9]+")
_NOISE = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "no",
    "not",
    "at",
    "in",
    "on",
    "of",
    "to",
    "and",
    "or",
    "it",
    "its",
    "this",
    "that",
    "still",
    "all",
    "has",
    "have",
}


# --- reading the check list ---------------------------------------------------


PR_TRIGGERS = ("pull_request", "pull_request_target")
FILTER_KEYS = ("paths", "paths_ignore", "branches", "tags")
# Activity types that fire while a pull request is open. A trigger restricted to
# `closed` or `labeled` never reports on an open PR.
PR_OPEN_TYPES = {"opened", "synchronize", "reopened", "ready_for_review", "edited"}


def _branch_allows(branch: str, patterns: list[str]) -> bool:
    """GitHub branch-filter semantics: `!` excludes, and `*` does not cross `/`.

    fnmatch does neither — it reads `!main` as a literal that never matches, and
    since inclusion is an any() test, an ignored exclusion reads as permitted.
    """
    positives = [p for p in patterns if not p.startswith("!")]
    negatives = [p[1:] for p in patterns if p.startswith("!")]
    if positives and not matches_any(branch, positives):
        return False
    return not any(compile_glob(p).match(branch) for p in negatives)


def _unconditional(
    cfg: dict, base_branch: str | None = None, resolve_branches: bool = False
) -> bool:
    """Whether this trigger fires on every pull request into `base_branch`.

    `branches:` on a pull_request trigger matches the PR's BASE, so a matching
    base means it is no restriction at all. On a `push` trigger it matches the
    HEAD — the PR's feature branch — so it stays a restriction whatever the base
    is, and resolving it against the base marks jobs requirable that can never
    report. Callers say which meaning applies via `resolve_branches`.
    """
    if any(cfg.get(key) for key in ("paths", "paths_ignore", "tags", "tags_ignore")):
        return False

    branches = list(cfg.get("branches") or [])
    branches += [f"!{p}" for p in (cfg.get("branches_ignore") or [])]
    if branches:
        if not resolve_branches or base_branch is None:
            return False
        if not _branch_allows(base_branch, branches):
            return False

    types = cfg.get("types")
    if types and not (set(types) & PR_OPEN_TYPES):
        return False  # e.g. types: [closed] — never fires while the PR is open
    return True


def _legacy_can_report(spec: dict) -> bool:
    """Profiles written before per-event data. Filters are known only in union."""
    if not (PR_EVENTS & set(spec.get("triggers") or [])):
        return False
    conditional = spec.get("pr_path_filters")
    if conditional is None:
        conditional = spec.get("path_filters")
    return not conditional


def can_report_on_pr(spec: dict, base_branch: str | None = None) -> bool:
    """Whether this job will produce a check on every pull request.

    Requirable means *unconditional*: some trigger fires on a PR with no filter
    attached. Anything narrower — paths, paths-ignore, branches, tags — is
    conditional and counts only once it actually reports.

    Being conservative here is safe in both directions, which is the point.
    Excluding a job that would have reported cannot cause an early merge,
    because a job that reports lands in `actionable_pending` while it runs and
    in `failed` if it fails. Including a job that can never report is what hangs
    the gate forever.
    """
    if "${{" in str(spec.get("display") or ""):
        return False  # a templated name cannot be attributed back to this job
    events = spec.get("events")
    if events is None:
        return _legacy_can_report(spec)
    for name in PR_TRIGGERS:
        cfg = events.get(name)
        if isinstance(cfg, dict) and _unconditional(cfg, base_branch, resolve_branches=True):
            return True
    push = events.get("push")
    if isinstance(push, dict) and _unconditional(push):
        return True
    return False


# Retained for callers that predate the rename.
_can_report = can_report_on_pr


# A check result names the commit it ran against. Anything shorter than this is
# not enough of a SHA to identify one.
MIN_SHA_PREFIX = 7


def describes_commit(entry: dict, expected_sha: str) -> bool:
    """Whether this check result provably ran against `expected_sha`.

    A result that names no commit is not evidence about this one. `gh pr checks`
    reports whatever the pull request last had, so between a push and the new
    workflow run registering it returns the PREVIOUS commit's results — which is
    the whole hazard this predicate exists to close.
    """
    reported = str(entry.get("head_sha") or entry.get("headSha") or "")
    if len(reported) < MIN_SHA_PREFIX or len(expected_sha or "") < MIN_SHA_PREFIX:
        return False
    # Either side may be abbreviated: `git rev-parse --short HEAD` on one side,
    # a full API SHA on the other.
    return reported.startswith(expected_sha) or expected_sha.startswith(reported)


def checks_for_sha(checks: list[dict], expected_sha: str | None) -> list[dict]:
    """The checks that describe `expected_sha`; all of them when no SHA is given."""
    if not expected_sha:
        return list(checks or [])
    return [c for c in (checks or []) if describes_commit(c, expected_sha)]


def _shapes(profile: dict) -> list[dict]:
    """The job list `ci_profile.attribute` wants: job key plus declared display name."""
    return [
        {"name": name, "display": (spec or {}).get("display")}
        for name, spec in (profile.get("jobs") or {}).items()
    ]


def _job_for(name: str, profile: dict, shapes: list[dict] | None = None) -> dict:
    """The profile entry for a reported check name, or {} when nothing matches.

    GitHub reports matrix cells (`test (3.11)`) and reusable-workflow legs
    (`caller / called`), while the profile is keyed by the workflow's job key
    (`test`). Indexing `jobs` with the reported name therefore misses on exactly
    the repos this system is for. `ci_profile.attribute` already reverses those
    display forms, and returns None rather than guessing.
    """
    jobs = profile.get("jobs") or {}
    if name in jobs:
        return jobs[name] or {}
    key = attribute(name, _shapes(profile) if shapes is None else shapes)
    return (jobs.get(key) or {}) if key else {}


def _is_advisory(name: str, profile: dict, shapes: list[dict] | None = None) -> bool:
    """Unknown checks count as required: an unknown gate may block the queue.

    When branch protection could not be read, nothing is known to be advisory.
    Treating an unread protection API as "nothing is required" turns a fully red
    CI into a green gate, which is the one outcome this whole system exists to
    prevent.
    """
    if not profile.get("protection_known", False):
        return False
    if name in set(profile.get("required_checks") or []):
        # Protection names this exact context. The job's own `required` flag is
        # computed by matching the job KEY against those contexts, so a matrix
        # job whose cells are required still reads required=False. Believing the
        # flag over the context list would file a failing required cell as
        # advisory, and a red gate would read green.
        return False
    job = _job_for(name, profile, shapes)
    return bool(job) and job.get("required") is False


def classify_checks(checks: list[dict], profile: dict, expected_sha: str | None = None) -> dict:
    shapes = _shapes(profile)
    summary = {
        "passed": [],
        "failed": [],
        "advisory_failed": [],
        "actionable_pending": [],
        "advisory_pending": [],
        "human_gate_pending": [],
        "pending": [],
        "stale": [],
    }
    for entry in checks or []:
        name = entry.get("name", "")
        if expected_sha and not describes_commit(entry, expected_sha):
            # Belongs to another commit, or names none at all. Counting it in
            # either direction lets CI that never saw this code decide its gate.
            summary["stale"].append(name)
            continue
        state = (entry.get("state") or entry.get("bucket") or "").upper()
        advisory = _is_advisory(name, profile, shapes)

        if state in PASSED:
            summary["passed"].append(name)
        elif state in FAILED:
            summary["advisory_failed" if advisory else "failed"].append(name)
        else:
            summary["pending"].append(name)
            haystack = f"{name} {entry.get('description', '')} {entry.get('workflow', '')}"
            if HUMAN_GATE.search(haystack):
                summary["human_gate_pending"].append(name)
            elif advisory:
                summary["advisory_pending"].append(name)
            else:
                summary["actionable_pending"].append(name)
    return summary


def ci_gate(
    checks: list[dict],
    profile: dict,
    base_branch: str | None = None,
    expected_sha: str | None = None,
) -> str:
    """Translate a check list into the ledger's ci_gate value.

    `expected_sha` is the commit this verdict is about. A gate verdict is a
    statement about one commit — that is why the ledger resets both gates on
    `batch.pushed` — so results that cannot be shown to describe that commit are
    dropped before anything is judged. What is left may be nothing, and nothing
    is `pending`: honest ignorance, never green.
    """
    checks = checks_for_sha(checks, expected_sha)
    summary = classify_checks(checks, profile)
    if summary["failed"]:
        return "failed"

    required = list(profile.get("required_checks") or [])
    if not required:
        if not profile.get("protection_known", False):
            # Nothing is known to be optional, so decide from what the workflows
            # declare plus what has actually reported.
            declared = profile.get("jobs") or {}
            shapes = _shapes(profile)
            requirable = {n: s for n, s in declared.items() if can_report_on_pr(s, base_branch)}
            covered = {attribute(name, shapes) for name in summary["passed"]}
            reported = {attribute(c.get("name", ""), shapes) for c in (checks or [])}

            if requirable:
                if not all(n in covered for n in requirable):
                    return "pending"  # a job that always runs has not reported yet
            elif not (reported & set(declared)):
                # No unconditional job to wait for, and nothing this repo declares
                # has reported. A DCO bot or a preview deploy going green says
                # nothing about whether CI ran. That is ignorance, not success.
                return "pending"

            # Anything that did report counts, requirable or not.
            return "full_green" if not summary["actionable_pending"] else "pending"

        # Protection says nothing is required, so nothing can block.
        return "full_green" if not summary["actionable_pending"] else "pending"

    done = set(summary["passed"])
    # Required checks are protection contexts — the names GitHub reports — so
    # their tier lives under the job that declared them, not under the context.
    # Reading the tier off the context directly finds nothing for every matrix
    # cell, the expensive job then counts as cheap, and cheap_green collapses
    # into full_green: the expensive tier gets launched on every push, which is
    # the whole saving the ladder exists to make.
    shapes = _shapes(profile)
    expensive = [n for n in required if _job_for(n, profile, shapes).get("tier") == "expensive"]
    cheap = [n for n in required if n not in expensive]

    if all(n in done for n in required):
        return "full_green"
    if cheap and all(n in done for n in cheap):
        return "cheap_green"
    return "pending"


# --- judging the reviewer -----------------------------------------------------


def validate_review(verdict: dict) -> tuple[bool, list[str]]:
    """A clean verdict must carry its evidence, or it is not a clean verdict.

    An agent reviewing an agent's work drifts toward approval. The counter is to
    make `clean` mechanically expensive: name the covering tests, and show that
    reverting the fix makes one of them fail. Both are facts a reviewer cannot
    produce by being agreeable.
    """
    errors: list[str] = []
    name = verdict.get("verdict")
    if name not in VERDICTS:
        return False, [f"verdict must be one of {sorted(VERDICTS)}, got {name!r}"]

    findings = verdict.get("findings") or []
    serious = [f for f in findings if str(f.get("severity", "")).lower() in SERIOUS]

    if name == "changes_requested":
        if not findings:
            errors.append("changes_requested must carry at least one finding saying what to change")
        return not errors, errors

    if serious:
        errors.append(
            f"a clean verdict cannot carry {len(serious)} finding(s) of "
            f"severity {sorted({str(f.get('severity')).lower() for f in serious})}"
        )

    if verdict.get("behaviour_change", True):
        if not verdict.get("tests_covering"):
            errors.append("clean requires tests_covering: name the tests that exercise this change")
        if verdict.get("revert_check") != "failed_as_expected":
            errors.append(
                f"clean requires revert_check=failed_as_expected (got "
                f"{verdict.get('revert_check')!r}): revert the fix, keep the test, "
                "and show it fails"
            )
    elif verdict.get("revert_check") == "still_passed":
        errors.append("revert_check=still_passed contradicts behaviour_change=false")

    return not errors, errors


# --- review convergence -------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE and len(w) > 1}


def same_finding(a: dict, b: dict, threshold: float = REVIEW_MATCH_THRESHOLD) -> bool:
    """Whether two findings are the same complaint, allowing for rewording."""
    if a.get("file") != b.get("file"):
        return False
    # Compare bands, not exact labels: a complaint drifting high <-> medium is the
    # same complaint, and both block a clean verdict.
    if (str(a.get("severity", "")).lower() in SERIOUS) != (
        str(b.get("severity", "")).lower() in SERIOUS
    ):
        return False
    wa, wb = _words(a.get("summary")), _words(b.get("summary"))
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def review_stalled(
    rounds: list[list[dict]], hard_ceiling: int = DEFAULT_REVIEW_CEILING
) -> str | None:
    """Reason to stop reviewing, or None to allow another round.

    Rounds elapsed is the wrong measure. A builder and reviewer who surface a
    different real defect each round are converging, however many rounds it
    takes; two who trade the same finding are not. Only blocking severities
    count — a repeated `low` never stood between the batch and a merge.
    """
    if len(rounds) >= hard_ceiling:
        return f"review reached the hard ceiling of {hard_ceiling} rounds"
    if len(rounds) < 2:
        return None

    def blocking(round_findings):
        return [f for f in round_findings if str(f.get("severity", "")).lower() in SERIOUS]

    previous, current = blocking(rounds[-2]), blocking(rounds[-1])
    for now in current:
        if any(same_finding(before, now) for before in previous):
            return (
                f"the same finding survived a round (repeat): "
                f"{now.get('file')} - {str(now.get('summary', ''))[:80]}"
            )
    return None


# --- flake or bug -------------------------------------------------------------


def flake_decision(classification: dict, batch: dict, config: dict) -> str:
    """rerun | fix | escalate. When unsure, fix — a rerun can hide a real bug."""
    if not classification.get("is_flaky"):
        return "fix"
    confidence = classification.get("confidence")
    if confidence is None or confidence < FLAKE_CONFIDENCE:
        return "fix"
    cap = (config.get("caps") or {}).get("reruns", 2)
    if (batch.get("attempts") or {}).get("reruns", 0) >= cap:
        return "escalate"
    return "rerun"


# --- the merge decision -------------------------------------------------------


def merge_blockers(batch: dict, pr: dict, config: dict) -> list[str]:
    """Everything standing between this batch and a merge, reported at once."""
    blockers: list[str] = []

    if not config.get("auto_merge"):
        blockers.append("auto_merge is disabled in config")

    for gate, wanted in CLEAR.items():
        actual = batch.get(f"{gate}_gate")
        if actual != wanted:
            blockers.append(f"{gate} gate is {actual}, needs {wanted}")

    held = sorted(BLOCKING_LABELS & {str(item).lower() for item in (pr.get("labels") or [])})
    for label in held:
        blockers.append(f"PR carries the {label} label")

    for path in batch.get("paths") or []:
        pattern = matches_any(path, config.get("protected_paths"))
        if pattern:
            blockers.append(f"{path} is protected by {pattern}; never auto-merged")

    caps = config.get("caps") or {}
    attempts = batch.get("attempts") or {}
    for counter, cap in caps.items():
        if attempts.get(counter, 0) >= cap:
            blockers.append(f"{counter} at cap ({attempts[counter]}/{cap})")

    return blockers


# --- gh plumbing --------------------------------------------------------------


def _gh_json(args: list[str]):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _from_check_run(run: dict, sha: str) -> dict:
    """One check run in the shape the classifier reads."""
    return {
        "name": run.get("name") or "",
        # A run still in flight has no conclusion yet; its status is the state.
        "state": str(run.get("conclusion") or run.get("status") or "").upper(),
        "description": (run.get("output") or {}).get("title") or "",
        "workflow": (run.get("check_suite") or {}).get("workflow_name") or "",
        "link": run.get("html_url") or "",
        "head_sha": run.get("head_sha") or sha,
    }


def _from_status(status: dict, sha: str) -> dict:
    """One legacy commit status — external CI that never moved to check runs.

    `gh pr checks` reports these alongside check runs, so reading only check runs
    would drop a required context and hang the gate.
    """
    return {
        "name": status.get("context") or "",
        "state": str(status.get("state") or "").upper(),
        "description": status.get("description") or "",
        "workflow": "",
        "link": status.get("target_url") or "",
        "head_sha": sha,
    }


def fetch_checks(repo: str, pr: int, sha: str | None = None) -> list[dict]:
    """Check results for this pull request, tagged with the commit they describe.

    Given a `sha`, both reads are SHA-addressed, so anything that comes back
    provably ran against that commit. `gh pr checks` cannot say that: its output
    carries no head SHA, and in the window between a push and the new run
    registering it returns the previous commit's results.
    """
    if sha:
        runs = _gh_json(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"]) or {}
        combined = _gh_json(["api", f"repos/{repo}/commits/{sha}/status?per_page=100"]) or {}
        return [
            *[_from_check_run(run, sha) for run in (runs.get("check_runs") or [])],
            *[_from_status(st, sha) for st in (combined.get("statuses") or [])],
        ]

    raw = _gh_json(
        [
            "pr",
            "checks",
            str(pr),
            "--repo",
            repo,
            "--json",
            "name,state,bucket,description,workflow,link",
        ]
    )
    return raw if isinstance(raw, list) else []


def fetch_pr(repo: str, pr: int) -> dict:
    raw = (
        _gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "number,labels,isDraft,mergeable,reviewDecision,url,baseRefName,headRefOid",
            ]
        )
        or {}
    )
    raw["labels"] = [
        item["name"] if isinstance(item, dict) else item for item in raw.get("labels", [])
    ]
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("checks")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--profile", default=".foreman/ci-profile.json")
    p.add_argument(
        "--sha",
        help="the commit this gate is about; defaults to the PR's current head",
    )
    p = sub.add_parser("verdict")
    p.add_argument("--file", required=True)
    p = sub.add_parser("blockers")
    p.add_argument("--batch", required=True)
    p.add_argument("--pr", type=int)
    p.add_argument("--repo")
    p.add_argument("--ledger", default=".foreman")
    p.add_argument("--config", default=".foreman/config.json")

    args = parser.parse_args(argv)

    def load_json(path, default):
        return json.loads(Path(path).read_text()) if Path(path).exists() else default

    if args.cmd == "checks":
        profile = load_json(args.profile, {})
        pr = fetch_pr(args.repo, args.pr) or {}
        # GitHub knows the new head the moment a push lands; its checks appear
        # later. Scoping the read to that head is what stops the previous
        # commit's green from being reported as this commit's.
        sha = args.sha or pr.get("headRefOid")
        checks = fetch_checks(args.repo, args.pr, sha)
        base = pr.get("baseRefName")
        summary = classify_checks(checks, profile, sha)
        print(
            json.dumps(
                {
                    **summary,
                    "base_branch": base,
                    "head_sha": sha,
                    "gate": ci_gate(checks, profile, base, sha),
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "verdict":
        ok, errors = validate_review(load_json(args.file, {}))
        print(
            json.dumps(
                {
                    "accepted": ok,
                    "errors": errors,
                    "gate_value": "clean"
                    if ok and load_json(args.file, {}).get("verdict") == "clean"
                    else "changes_requested",
                },
                indent=2,
            )
        )
        return 0 if ok else 1

    import ledger as ledger_mod

    state = ledger_mod.load(Path(args.ledger))
    batch = state.batches.get(args.batch)
    if batch is None:
        print(f"error: no batch {args.batch}", file=sys.stderr)
        return 1
    pr = fetch_pr(args.repo, args.pr) if (args.repo and args.pr) else {"labels": []}
    blockers = merge_blockers(batch, pr, load_json(args.config, {}))
    print(
        json.dumps({"batch": args.batch, "mergeable": not blockers, "blockers": blockers}, indent=2)
    )
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
