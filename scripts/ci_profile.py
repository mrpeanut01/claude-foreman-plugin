#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Learn what this repo's CI actually costs, so the loop can stop overspending it.

Produces .foreman/ci-profile.json: the job graph, observed durations, which
jobs are required to merge, which ones flake, and how a diff maps to tests.
Every number here is measured from real runs, not declared in a config file.

CLI:
    ci_profile.py probe --repo OWNER/NAME [--runs 50] [--out PATH]
    ci_profile.py impact --changed FILE [FILE ...]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

# A job whose p95 sits under this runs in the cheap tier: it is worth paying on
# every push. Anything slower waits behind the cheap tier and the review gate.
DEFAULT_TIER_THRESHOLD_S = 300


class ProfileError(Exception):
    pass


DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_DIRS = {"docs", "doc", "documentation"}
# Test sources only. The `.*` glob otherwise matches compiled bytecode, and a
# .pyc handed to a test runner is at best an error.
TEST_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".rb", ".go", ".rs", ".java", ".kt"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache"}
FINISHED = {"success", "failure"}


# --- workflow parsing ---------------------------------------------------------


def _on_block(doc: dict) -> dict:
    # YAML 1.1 reads a bare `on:` key as the boolean True. PyYAML obliges.
    raw = doc.get("on", doc.get(True, {}))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {k: {} for k in raw}
    return raw if isinstance(raw, dict) else {}


def _path_filters(on: dict, events: set[str] | None = None) -> list[str]:
    """Path filters declared by the given events (all of them when None).

    Filters belong to the event that declared them. A `paths:` on `push` says
    nothing about whether a job runs on a pull request, and treating the union as
    if it did marks unconditional PR jobs conditional.
    """
    paths: list[str] = []
    for event, cfg in on.items():
        if events is not None and str(event) not in events:
            continue
        if isinstance(cfg, dict):
            paths.extend(cfg.get("paths", []) or [])
    return sorted(dict.fromkeys(paths))


def parse_workflows(workflow_dir: Path, report_problems: bool = False):
    """Read .github/workflows into a flat list of jobs with their edges."""
    jobs: list[dict] = []
    problems: list[str] = []
    for path in sorted(Path(workflow_dir).glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: unparseable ({type(exc).__name__})")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: not a workflow mapping")
            continue
        on = _on_block(doc)
        triggers = sorted(str(k) for k in on)
        filters = _path_filters(on)
        pr_filters = _path_filters(on, {"pull_request", "pull_request_target"})
        events = {
            str(event): {
                "paths": list((cfg or {}).get("paths") or []),
                "paths_ignore": list((cfg or {}).get("paths-ignore") or []),
                "branches": list((cfg or {}).get("branches") or []),
                "tags": list((cfg or {}).get("tags") or []),
                "types": list((cfg or {}).get("types") or []),
                "branches_ignore": list((cfg or {}).get("branches-ignore") or []),
                "tags_ignore": list((cfg or {}).get("tags-ignore") or []),
            }
            for event, cfg in on.items()
            if isinstance(cfg, dict) or cfg is None
        }
        for name, spec in (doc.get("jobs") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            needs = spec.get("needs", [])
            jobs.append(
                {
                    "name": name,
                    "display": spec.get("name"),
                    "workflow": doc.get("name", path.stem),
                    "workflow_file": path.name,
                    "needs": [needs] if isinstance(needs, str) else list(needs or []),
                    "triggers": triggers,
                    "path_filters": filters,
                    "pr_path_filters": pr_filters,
                    "events": events,
                }
            )
    return (jobs, problems) if report_problems else jobs


# --- observed cost ------------------------------------------------------------


def _seconds(job: dict) -> float | None:
    try:
        start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None
    return (end - start).total_seconds()


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank. Predictable on the small samples CI history actually gives."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


_MATRIX_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def attribute(reported: str, jobs: list[dict]) -> str | None:
    """Resolve a name GitHub reported back to the workflow job that declared it.

    Returns None rather than guessing. A wrong attribution silently mixes two
    jobs' durations, which is worse than an honest gap in the profile.
    """
    if not reported:
        return None
    candidates = {}
    for job in jobs:
        candidates[job["name"]] = job["name"]
        display = job.get("display")
        # A display name built from a matrix expression cannot be reversed.
        if display and not _EXPRESSION.search(display):
            candidates[display] = job["name"]
    # A reusable workflow reports as "caller / called-job"; the caller is the job
    # the workflow file declares.
    probes = [reported, _MATRIX_SUFFIX.sub("", reported)]
    if " / " in reported:
        head = reported.split(" / ")[0].strip()
        probes += [head, _MATRIX_SUFFIX.sub("", head)]
    for probe_name in probes:
        if probe_name in candidates:
            return candidates[probe_name]
    return None


def attribute_runs(job_runs: list[dict], jobs: list[dict]) -> tuple[list[dict], list[str]]:
    """Relabel observed runs with their declared job key; report what did not match."""
    mapped, orphans = [], set()
    for run in job_runs:
        key = attribute(run.get("name", ""), jobs)
        if key is None:
            orphans.add(run.get("name", ""))
        else:
            mapped.append({**run, "name": key})
    return mapped, sorted(n for n in orphans if n)


def duration_stats(job_runs: list[dict]) -> dict[str, dict]:
    buckets: dict[str, list[float]] = {}
    for job in job_runs:
        if job.get("conclusion") not in FINISHED:
            continue  # cancelled/skipped runs say nothing about what a job costs
        secs = _seconds(job)
        if secs is not None:
            buckets.setdefault(job["name"], []).append(secs)
    return {
        name: {"n": len(vals), "p50": _percentile(vals, 50), "p95": _percentile(vals, 95)}
        for name, vals in buckets.items()
    }


def classify_tiers(
    stats: dict[str, dict], threshold_s: int = DEFAULT_TIER_THRESHOLD_S
) -> dict[str, str]:
    return {
        name: ("cheap" if s.get("p95", 0) <= threshold_s else "expensive")
        for name, s in stats.items()
    }


def flake_rates(job_runs: list[dict]) -> dict[str, float]:
    """A flake is one commit where the same job both failed and passed.

    Failing every time on a commit is a real failure, not a flake. That
    distinction is the whole point: it decides rerun versus fix.
    """
    by_job: dict[str, dict[str, set[str]]] = {}
    for job in job_runs:
        if job.get("conclusion") not in FINISHED:
            continue
        by_job.setdefault(job["name"], {}).setdefault(job.get("head_sha", ""), set()).add(
            job["conclusion"]
        )
    rates = {}
    for name, shas in by_job.items():
        flaky = sum(1 for outcomes in shas.values() if outcomes >= {"success", "failure"})
        rates[name] = flaky / len(shas) if shas else 0.0
    return rates


def required_checks(protection: dict | None) -> list[str]:
    """Only required checks can block a merge; everything else is advisory."""
    block = (protection or {}).get("required_status_checks") or {}
    names = list(block.get("contexts") or [])
    names += [c.get("context") for c in (block.get("checks") or []) if c.get("context")]
    return sorted(dict.fromkeys(names))


# --- test impact --------------------------------------------------------------


def _is_test_source(path: Path) -> bool:
    """A real test file, not a build artefact that happens to sit beside one."""
    return path.is_file() and path.suffix in TEST_SUFFIXES and not SKIP_DIRS & set(path.parts)


def _is_test(rel: str) -> bool:
    path = Path(rel)
    parts = path.parts
    return (
        bool(parts)
        and parts[0] in {"test", "tests"}
        and path.name.startswith("test")
        and path.suffix in TEST_SUFFIXES
        and not SKIP_DIRS & set(parts)
    )


def _is_doc(rel: str) -> bool:
    path = Path(rel)
    return path.suffix.lower() in DOC_SUFFIXES or bool(set(path.parts) & DOC_DIRS)


def impacted_tests(changed: list[str], repo_root: Path) -> tuple[list[str], bool]:
    """Map a diff to the tests that cover it.

    Returns (tests, complete). `complete` is False when any changed file could
    not be mapped — the caller must then run the full suite. Narrowing on a
    partial map is how you ship a regression, so this never guesses.
    """
    root = Path(repo_root)
    hits: set[str] = set()
    complete = True
    for rel in changed:
        if _is_test(rel):
            hits.add(rel)
        elif _is_doc(rel):
            continue  # documentation genuinely maps to no tests
        else:
            stem = Path(rel).stem
            found = [
                str(p.relative_to(root))
                for p in sorted(root.glob(f"tests/**/test_{stem}.*"))
                if _is_test_source(p)
            ]
            if found:
                hits.update(found)
            else:
                complete = False
    return sorted(hits), complete


# --- assembly -----------------------------------------------------------------


# Allow-lists narrow a trigger to what they list; ignore-lists only ever remove
# runs from it. The two directions compare oppositely, which is the whole reason
# a filter *count* cannot stand in for how permissive a trigger is.
_ALLOW_FILTERS = ("paths", "branches", "tags", "types")
_IGNORE_FILTERS = ("paths_ignore", "branches_ignore", "tags_ignore")
_FILTERS = _ALLOW_FILTERS + _IGNORE_FILTERS

# Activity types that occur while a pull request is still open. A trigger
# restricted to any other type — `types: [closed]` — cannot put a check on a live
# PR at all. This is deliberately broader than land.PR_UNCONDITIONAL_TYPES, which
# asks the stricter question of whether a job reports on *every* PR and so may be
# required; conflating the two was issue #49. Containment between them is asserted
# in tests/test_ci_profile.py.
OPEN_PR_TYPES = frozenset({"opened", "synchronize", "reopened", "ready_for_review", "edited"})


def _reports_while_open(cfg: dict) -> bool:
    types = set(cfg.get("types") or [])
    return not types or bool(types & OPEN_PR_TYPES)


def _fires_wherever(a: dict, b: dict) -> bool:
    """Whether everything that makes `b` fire also makes `a` fire.

    Per axis, because the filters are a conjunction: an absent allow-list is no
    restriction at all, a present one has to cover b's, and an ignore list is
    weaker the shorter it is. `types` is the one approximation — an absent
    `types` means the default activity types rather than every type — and it errs
    towards keeping the declaration that reports on an open pull request, which
    is the only situation the gate ever asks about.
    """
    for key in _ALLOW_FILTERS:
        mine, theirs = set(a.get(key) or []), set(b.get(key) or [])
        if mine and not (theirs and mine >= theirs):
            return False
    for key in _IGNORE_FILTERS:
        mine, theirs = set(a.get(key) or []), set(b.get(key) or [])
        if mine and not mine <= theirs:
            return False
    return True


def _canonical(cfg: dict) -> str:
    return json.dumps({key: sorted(set(cfg.get(key) or [])) for key in _FILTERS}, sort_keys=True)


def _informativeness(cfg: dict) -> tuple:
    """Ranks two declarations that neither cover each other nor combine, best first.

    Every choice here is sound: one real declaration always fires on a subset of
    what the job as a whole fires on, and under-requiring costs nothing — a job
    that does report lands in `actionable_pending` while it runs. So this ranks
    by how much of the job's behaviour survives the compression. A declaration
    that cannot report while the PR is open says nothing whatsoever; after that,
    a `branches` filter is still resolved against the PR's base at query time,
    while a path or tag filter can never be. The serialised config breaks the
    remaining ties so the answer never depends on which file sorted first.
    """
    unresolvable = any(cfg.get(key) for key in ("paths", "paths_ignore", "tags", "tags_ignore"))
    return (not _reports_while_open(cfg), unresolvable, _canonical(cfg))


def _merge_triggers(declarations: list[dict]) -> dict:
    """Fold every declaration of one job name on one event into a single config.

    A check appears if ANY declaration fires, so the merged config should stand
    for the union of their firing sets. One filter dict cannot express a
    disjunction that crosses two axes — `branches: [main]` OR `paths: [src/**]`
    is emphatically not "no filters", and merging key by key would produce
    exactly that, marking a job requirable that GitHub may never run. So the
    union is taken only where it is exact, and otherwise one declaration is kept.
    """
    merged, *rest = sorted(declarations, key=_canonical)  # input order cannot decide
    for cfg in rest:
        if _fires_wherever(merged, cfg):
            continue
        if _fires_wherever(cfg, merged):
            merged = cfg
            continue
        differing = [k for k in _FILTERS if set(merged.get(k) or []) != set(cfg.get(k) or [])]
        if len(differing) == 1 and differing[0] in _ALLOW_FILTERS:
            # Every other axis is identical, so the disjunction is precisely the
            # union of the two lists on this one.
            key = differing[0]
            merged = {**merged, key: sorted(set(merged.get(key) or []) | set(cfg.get(key) or []))}
            continue
        merged = min((merged, cfg), key=_informativeness)
    return merged


def build_profile(
    workflow_dir: Path,
    job_runs: list[dict],
    protection: dict | None,
    threshold_s: int = DEFAULT_TIER_THRESHOLD_S,
) -> dict:
    jobs, problems = parse_workflows(workflow_dir, report_problems=True)
    attributed, unattributed = attribute_runs(job_runs, jobs)
    stats = duration_stats(attributed)
    tiers = classify_tiers(stats, threshold_s)
    flakes = flake_rates(attributed)
    required = set(required_checks(protection))

    # A job name reused across workflows is one check name, so merge rather than
    # overwrite: a release-only `test` job must not erase the PR `test` job.
    by_name: dict[str, dict] = {}
    declared: dict[str, dict[str, list[dict]]] = {}
    for job in jobs:
        for event, cfg in (job.get("events") or {}).items():
            declared.setdefault(job["name"], {}).setdefault(event, []).append(cfg)
        seen = by_name.get(job["name"])
        if seen is None:
            by_name[job["name"]] = dict(job)
            continue
        seen["triggers"] = sorted(set(seen["triggers"]) | set(job["triggers"]))
        seen["path_filters"] = sorted(set(seen["path_filters"]) | set(job["path_filters"]))
        seen["pr_path_filters"] = sorted(set(seen["pr_path_filters"]) | set(job["pr_path_filters"]))
    # Every declaration of an event is collected before any of them is merged, so
    # the result depends on what the workflows say and not on the order the files
    # happened to be read in.
    for name, events in declared.items():
        by_name[name]["events"] = {event: _merge_triggers(cfgs) for event, cfgs in events.items()}
    jobs = list(by_name.values())

    merged, unmeasured = {}, []
    for job in jobs:
        name = job["name"]
        stat = stats.get(name)
        if stat is None:
            unmeasured.append(name)
        merged[name] = {
            **{
                k: job[k]
                for k in (
                    "workflow",
                    "workflow_file",
                    "needs",
                    "triggers",
                    "path_filters",
                    "pr_path_filters",
                    "events",
                )
            },
            "display": job.get("display"),
            "p50": stat["p50"] if stat else None,
            "p95": stat["p95"] if stat else None,
            "samples": stat["n"] if stat else 0,
            "tier": tiers.get(name, "unmeasured"),
            "required": name in required,
            "flake_rate": round(flakes.get(name, 0.0), 3),
        }

    def tier_cost(tier: str) -> float:
        return sum(j["p95"] or 0 for j in merged.values() if j["tier"] == tier)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tier_threshold_s": threshold_s,
        "jobs": merged,
        "required_checks": sorted(required),
        # Absent protection means we do not know what is required. Recording
        # that as a fact stops land.py reading it as "nothing is".
        "protection_known": bool(protection),
        "cheap_tier_s": tier_cost("cheap"),
        "expensive_tier_s": tier_cost("expensive"),
        "unmeasured_jobs": sorted(unmeasured),
        "unattributed_runs": unattributed,
        "problems": problems,
    }


# --- live probe ---------------------------------------------------------------


def _gh_json(args: list[str]) -> object:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def current_repo() -> str | None:
    """The repo the cwd belongs to, or None outside a GitHub checkout."""
    info = _gh_json(["repo", "view", "--json", "nameWithOwner"]) or {}
    return info.get("nameWithOwner")


def _fetch_job_runs(repo: str, runs: int, branch: str | None) -> list[dict]:
    listing = (
        _gh_json(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--limit",
                str(runs),
                *(["--branch", branch] if branch else []),
                "--json",
                "databaseId,headSha,conclusion",
            ]
        )
        or []
    )
    job_runs = []
    for run in listing:
        detail = _gh_json(["api", f"repos/{repo}/actions/runs/{run['databaseId']}/jobs"]) or {}
        for job in detail.get("jobs", []):
            job_runs.append(
                {
                    "run_id": run["databaseId"],
                    "head_sha": run.get("headSha"),
                    "name": job.get("name"),
                    "conclusion": job.get("conclusion"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                }
            )
    return job_runs


def _fetch_protection(repo: str) -> dict | None:
    view = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {}
    branch = (view.get("defaultBranchRef") or {}).get("name", "main")
    # Absent or inaccessible protection is normal, not an error — but it means
    # UNKNOWN, not "nothing is required". build_profile records that as
    # protection_known, and land.py then treats every check as required.
    return _gh_json(["api", f"repos/{repo}/branches/{branch}/protection"])


def probe(
    repo: str,
    runs: int = 50,
    branch: str | None = None,
    workflow_dir: Path | None = None,
    threshold_s: int = DEFAULT_TIER_THRESHOLD_S,
) -> dict:
    """Pull real run history off GitHub. Read-only: run list, jobs, protection.

    Job *history* comes from `repo`; job *definitions* come from workflow files
    on disk. Reading those from different repos yields a profile describing
    neither, so probing a repo other than the current one requires saying where
    its workflows are.
    """
    if workflow_dir is None:
        here = current_repo()
        if here and here.lower() != repo.lower():
            raise ProfileError(
                f"refusing to profile {repo} from a checkout of {here}: run history would "
                f"come from {repo} but job definitions from {here}. Run this inside {repo}, "
                f"or pass --workflows pointing at its workflow directory."
            )
        workflow_dir = Path(".github/workflows")

    workflow_dir = Path(workflow_dir)
    if not workflow_dir.is_dir():
        raise ProfileError(f"no workflow directory at {workflow_dir}: nothing to profile")

    profile = build_profile(
        workflow_dir, _fetch_job_runs(repo, runs, branch), _fetch_protection(repo), threshold_s
    )
    profile["repo"] = repo
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--repo", required=True)
    p.add_argument("--runs", type=int, default=50)
    p.add_argument("--out", default=".foreman/ci-profile.json")
    p.add_argument(
        "--workflows",
        default=None,
        help="workflow directory (required when profiling another repo)",
    )
    p.add_argument("--branch", default=None)
    p.add_argument("--threshold", type=int, default=DEFAULT_TIER_THRESHOLD_S)
    p = sub.add_parser("impact")
    p.add_argument("--changed", nargs="+", required=True)
    p.add_argument("--root", default=".")

    args = parser.parse_args(argv)
    if args.cmd == "probe":
        try:
            profile = probe(
                args.repo,
                args.runs,
                args.branch,
                Path(args.workflows) if args.workflows else None,
                args.threshold,
            )
        except ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "written": str(out),
                    "jobs": len(profile["jobs"]),
                    "cheap_tier_s": profile["cheap_tier_s"],
                    "expensive_tier_s": profile["expensive_tier_s"],
                    "unmeasured": profile["unmeasured_jobs"],
                },
                indent=2,
            )
        )
    else:
        tests, complete = impacted_tests(args.changed, Path(args.root))
        print(
            json.dumps(
                {
                    "tests": tests,
                    "complete": complete,
                    "recommendation": "run listed tests" if complete else "run full suite",
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
