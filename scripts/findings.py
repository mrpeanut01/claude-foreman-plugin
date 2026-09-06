#!/usr/bin/env python3
"""Turn review findings into GitHub issues, so the loop can pick them up again.

Without this the loop leaks. A reviewer's findings live in a verdict JSON and a
PR comment: the blocking ones get fixed in that PR, and the rest evaporate. An
issue is the only artefact triage reads, so a finding that never becomes one can
never be worked.

CLI:
    findings.py plan --verdict verdict.json --batch b-001 --pr 7 --repo OWNER/NAME
    findings.py file --plan plan.json --repo OWNER/NAME [--ledger .foreman]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from triage import _tokens  # the same title-overlap test triage uses for duplicates

TITLE_LIMIT = 100
# Overlap coefficient rather than Jaccard: one title being a terser phrasing of
# another should not be penalised for the longer one's extra words. MIN_SHARED
# stops two short titles matching on a single common word.
DUPLICATE_THRESHOLD = 0.6
MIN_SHARED_TOKENS = 3
SEVERITY_ORDER = ["critical", "blocker", "high", "medium", "low"]

# Severity chooses the label; a finding about a doc file overrides it.
SEVERITY_LABEL = {
    "critical": "bug",
    "blocker": "bug",
    "high": "bug",
    "medium": "bug",
    "low": "enhancement",
}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}


class UnusableFinding(Exception):
    pass


def _rank(finding: dict) -> int:
    severity = str(finding.get("severity", "")).lower()
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _title(summary: str) -> str:
    summary = " ".join(summary.split())
    if len(summary) <= TITLE_LIMIT:
        return summary
    return summary[: TITLE_LIMIT - 1].rstrip() + "…"


def _label(finding: dict, available: list[str]) -> list[str]:
    severity = str(finding.get("severity", "")).lower()
    wanted = SEVERITY_LABEL.get(severity, "bug")
    if Path(str(finding.get("file", ""))).suffix.lower() in DOC_SUFFIXES:
        wanted = "documentation"
    return [wanted] if wanted in (available or []) else []


def to_issue(finding: dict, context: dict, available_labels: list[str]) -> dict:
    """One finding, one issue. Provenance in the body so it can be traced back."""
    summary = " ".join(str(finding.get("summary") or "").split())
    if not _tokens(summary):
        # Punctuation is not a summary, and an issue titled "...!?" can never be
        # matched against anything, so it would be refiled every round.
        raise UnusableFinding(f"finding has no usable summary: {finding!r}")

    where = finding.get("file") or "unknown file"
    if finding.get("line"):
        where = f"{where}:{finding['line']}"

    # --pr is optional: a review can be raised on a batch before there is a PR,
    # or on a repo rather than a change. The provenance line is the only trace
    # back to the review that raised the finding, so it is exactly the line that
    # must not render a null — the batch and round carry it when the PR cannot.
    raised_on = f" of PR #{context['pr']}" if context.get("pr") else ""
    body = [
        f"Raised by the independent review{raised_on} "
        f"(batch `{context.get('batch')}`, round {context.get('round')}).",
        "",
        f"**Severity:** {finding.get('severity', 'unknown')}  ",
        f"**Location:** `{where}`",
        "",
        "## What",
        "",
        summary,
    ]
    if finding.get("failure_scenario"):
        body += ["", "## How it fails", "", str(finding["failure_scenario"])]

    return {
        "title": _title(summary),
        "body": "\n".join(body),
        "labels": _label(finding, available_labels),
        "severity": str(finding.get("severity", "")).lower(),
    }


def _floor(mine: set[str], theirs: set[str]) -> int:
    """How many shared tokens this pair of titles needs before it can be a duplicate.

    Two-word titles can never reach a flat floor of 3, which would make "Race
    condition" permanently undedupable, so the floor scales down for them. It may
    only scale down when *both* titles are that short. The score divides by the
    shorter title's length, so a two-token tracker stub is contained in a long
    summary at a perfect 1.0 — "Flaky tests" would swallow "Flaky tests in the
    upload suite mask a real regression in land.py". Brevity on one side is not
    evidence; it is only an excuse for the floor when the other side is brief too.
    """
    if max(len(mine), len(theirs)) >= MIN_SHARED_TOKENS:
        return MIN_SHARED_TOKENS
    return min(MIN_SHARED_TOKENS, len(mine), len(theirs))


def _duplicate_of(title: str, open_issues: list[dict]) -> dict | None:
    """The open issue this title duplicates, as a record rather than a number.

    The record, because an entry queued earlier in this same run has no issue
    number yet and the caller has to be able to tell the two cases apart.
    """
    mine = _tokens(title)
    if not mine:
        return None
    best, best_score = None, 0.0
    for issue in open_issues or []:
        if str(issue.get("state", "open")).lower() != "open":
            continue  # a regression of a closed issue is news, not a duplicate
        theirs = _tokens(issue.get("title"))
        if not theirs:
            continue
        shared = mine & theirs
        if len(shared) < _floor(mine, theirs):
            continue
        score = len(shared) / min(len(mine), len(theirs))
        if score >= DUPLICATE_THRESHOLD and score > best_score:
            best, best_score = issue, score
    return best


def plan(
    finding_list: list[dict], context: dict, available_labels: list[str], open_issues: list[dict]
) -> dict:
    """What to file, what is already tracked, and what could not be read."""
    to_file, skipped, unusable = [], [], []
    for finding in sorted(finding_list or [], key=_rank):
        try:
            issue = to_issue(finding, context, available_labels)
        except UnusableFinding as exc:
            unusable.append({"finding": finding, "reason": str(exc)})
            continue
        # Compare against the tracker AND against what this run already queued,
        # or one defect reported at two call sites files two identical issues.
        # The queued entries carry a null number on purpose: they are not filed
        # yet, so there is no number, and inventing one puts a string where every
        # consumer of duplicate_of expects an issue to link to.
        seen = list(open_issues or []) + [
            {"number": None, "title": i["title"], "state": "open"} for i in to_file
        ]
        duplicate = _duplicate_of(issue["title"], seen)
        if duplicate is not None:
            entry = {**issue, "duplicate_of": duplicate.get("number")}
            if entry["duplicate_of"] is None:
                # Suppressed by a sibling finding in this very run. The title is
                # the only handle on it, and it is worth reporting: two call
                # sites reporting one defect is itself information.
                entry["duplicate_of_title"] = duplicate.get("title")
            skipped.append(entry)
        else:
            to_file.append(issue)
    return {"file": to_file, "skipped": skipped, "unusable": unusable}


def from_verdict(
    verdict: dict, context: dict, available_labels: list[str], open_issues: list[dict]
) -> dict:
    return plan(verdict.get("findings") or [], context, available_labels, open_issues)


# --- gh plumbing --------------------------------------------------------------


def _gh_json(args: list[str]):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def fetch_open_issues(repo: str, limit: int = 100) -> list[dict]:
    """Only open issues are used for duplicate detection, so only fetch those.

    Asking for `--state all` spends the window on closed rows that are discarded,
    and on a repo whose newest issues are mostly closed the open ones needed here
    fall outside it entirely.
    """
    args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,state",
    ]
    raw = _gh_json(args) or []
    for item in raw:
        item["state"] = str(item.get("state", "open")).lower()
    return raw


def fetch_labels(repo: str) -> list[str]:
    raw = _gh_json(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"]) or []
    return [item["name"] for item in raw]


def create_issue(repo: str, issue: dict, wrapper: Path) -> str | None:
    args = [
        str(wrapper),
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        issue["title"],
        "--body",
        issue["body"],
    ]
    for label in issue.get("labels", []):
        args += ["--label", label]
    done = subprocess.run(args, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--verdict", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--batch", required=True)
    p.add_argument("--pr", type=int)
    p.add_argument("--round", type=int, default=1)
    p = sub.add_parser("file")
    p.add_argument("--plan", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--ledger", default=".foreman")

    args = parser.parse_args(argv)
    here = Path(__file__).resolve().parent

    if args.cmd == "plan":
        verdict = json.loads(Path(args.verdict).read_text())
        context = {"batch": args.batch, "pr": args.pr, "round": args.round, "repo": args.repo}
        result = from_verdict(
            verdict, context, fetch_labels(args.repo), fetch_open_issues(args.repo)
        )
        print(json.dumps({**result, "context": context}, indent=2))
        return 0

    import ledger as ledger_mod

    payload = json.loads(Path(args.plan).read_text())
    context = payload.get("context", {})
    # An issue, once created, is not something the wrapper can undo, so the
    # plan's own repository has to agree with --repo before the first one.
    planned_for = context.get("repo")
    if planned_for and str(planned_for).lower() != args.repo.lower():
        print(
            f"error: this plan was built for {planned_for}, not {args.repo}; "
            f"rebuild it with --repo {args.repo} rather than filing it here",
            file=sys.stderr,
        )
        return 1
    root = Path(args.ledger)
    filed, failed = [], []
    for issue in payload.get("file", []):
        url = create_issue(args.repo, issue, here / "gh_safe.sh")
        if url:
            filed.append(url)
            ledger_mod.append(
                root,
                "finding.filed",
                batch=context.get("batch"),
                url=url,
                severity=issue.get("severity"),
                title=issue["title"],
            )
        else:
            failed.append(issue["title"])
    skipped = payload.get("skipped", [])
    print(
        json.dumps(
            {
                "filed": filed,
                "failed": failed,
                # Issue numbers only, so a caller can follow them. A finding
                # suppressed by another finding in the same run has no number;
                # it is reported by title under its own key rather than as a
                # fake one nobody can look up.
                "already_tracked": [
                    s["duplicate_of"] for s in skipped if s.get("duplicate_of") is not None
                ],
                "duplicate_within_run": [
                    s["title"] for s in skipped if s.get("duplicate_of") is None
                ],
            },
            indent=2,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
