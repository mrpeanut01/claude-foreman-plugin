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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

# A job whose p95 sits under this runs in the cheap tier: it is worth paying on
# every push. Anything slower waits behind the cheap tier and the review gate.
DEFAULT_TIER_THRESHOLD_S = 300

DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_DIRS = {"docs", "doc", "documentation"}
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


def _path_filters(on: dict) -> list[str]:
    paths: list[str] = []
    for cfg in on.values():
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
        for name, spec in (doc.get("jobs") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            needs = spec.get("needs", [])
            jobs.append({
                "name": name,
                "workflow": doc.get("name", path.stem),
                "workflow_file": path.name,
                "needs": [needs] if isinstance(needs, str) else list(needs or []),
                "triggers": triggers,
                "path_filters": filters,
            })
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


def classify_tiers(stats: dict[str, dict], threshold_s: int = DEFAULT_TIER_THRESHOLD_S) -> dict[str, str]:
    return {name: ("cheap" if s.get("p95", 0) <= threshold_s else "expensive")
            for name, s in stats.items()}


def flake_rates(job_runs: list[dict]) -> dict[str, float]:
    """A flake is one commit where the same job both failed and passed.

    Failing every time on a commit is a real failure, not a flake. That
    distinction is the whole point: it decides rerun versus fix.
    """
    by_job: dict[str, dict[str, set[str]]] = {}
    for job in job_runs:
        if job.get("conclusion") not in FINISHED:
            continue
        by_job.setdefault(job["name"], {}).setdefault(job.get("head_sha", ""), set()).add(job["conclusion"])
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

def _is_test(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and parts[0] in {"test", "tests"} and Path(rel).name.startswith("test")


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
            found = [str(p.relative_to(root))
                     for p in sorted(root.glob(f"tests/**/test_{stem}.*"))]
            if found:
                hits.update(found)
            else:
                complete = False
    return sorted(hits), complete


# --- assembly -----------------------------------------------------------------

def build_profile(workflow_dir: Path, job_runs: list[dict], protection: dict | None,
                  threshold_s: int = DEFAULT_TIER_THRESHOLD_S) -> dict:
    jobs, problems = parse_workflows(workflow_dir, report_problems=True)
    stats = duration_stats(job_runs)
    tiers = classify_tiers(stats, threshold_s)
    flakes = flake_rates(job_runs)
    required = set(required_checks(protection))

    merged, unmeasured = {}, []
    for job in jobs:
        name = job["name"]
        stat = stats.get(name)
        if stat is None:
            unmeasured.append(name)
        merged[name] = {
            **{k: job[k] for k in ("workflow", "workflow_file", "needs", "triggers", "path_filters")},
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
        "cheap_tier_s": tier_cost("cheap"),
        "expensive_tier_s": tier_cost("expensive"),
        "unmeasured_jobs": sorted(unmeasured),
        "problems": problems,
    }


# --- live probe ---------------------------------------------------------------

def _gh_json(args: list[str]) -> object:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def probe(repo: str, runs: int = 50, branch: str | None = None) -> dict:
    """Pull real run history off GitHub. Read-only: list runs, jobs, protection."""
    listing = _gh_json(["run", "list", "--repo", repo, "--limit", str(runs),
                        *(["--branch", branch] if branch else []),
                        "--json", "databaseId,headSha,conclusion"]) or []
    job_runs = []
    for run in listing:
        detail = _gh_json(["api", f"repos/{repo}/actions/runs/{run['databaseId']}/jobs"]) or {}
        for job in detail.get("jobs", []):
            job_runs.append({
                "run_id": run["databaseId"], "head_sha": run.get("headSha"),
                "name": job.get("name"), "conclusion": job.get("conclusion"),
                "started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
            })
    default_branch = (_gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {})
    branch_name = (default_branch.get("defaultBranchRef") or {}).get("name", "main")
    protection = _gh_json(["api", f"repos/{repo}/branches/{branch_name}/protection"])
    return build_profile(Path(".github/workflows"), job_runs, protection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--repo", required=True)
    p.add_argument("--runs", type=int, default=50)
    p.add_argument("--out", default=".foreman/ci-profile.json")
    p = sub.add_parser("impact")
    p.add_argument("--changed", nargs="+", required=True)
    p.add_argument("--root", default=".")

    args = parser.parse_args(argv)
    if args.cmd == "probe":
        profile = probe(args.repo, args.runs)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(json.dumps({"written": str(out), "jobs": len(profile["jobs"]),
                          "cheap_tier_s": profile["cheap_tier_s"],
                          "expensive_tier_s": profile["expensive_tier_s"],
                          "unmeasured": profile["unmeasured_jobs"]}, indent=2))
    else:
        tests, complete = impacted_tests(args.changed, Path(args.root))
        print(json.dumps({"tests": tests, "complete": complete,
                          "recommendation": "run listed tests" if complete else "run full suite"},
                         indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
