#!/usr/bin/env python3
"""Batching: group issues so one slow suite run covers several fixes.

A 40-minute suite costs 40 minutes whether the PR fixes one issue or five, so
grouping compatible issues is the largest single saving available on a repo with
slow CI. The cost of grouping is that a failure is harder to attribute — which is
why every issue gets its own commit and `split()` exists.

CLI:
    batch.py plan --triage triage.json [--config .foreman/config.json]
    batch.py apply --plan batches.json [--ledger .foreman]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WEIGHT = {"small": 1, "medium": 2, "large": 4}
RISK_ORDER = ["low", "medium", "high"]
DEFAULT_MAX_ISSUES = 5
DEFAULT_MAX_WEIGHT = 6


class CannotSplit(Exception):
    pass


def _rank(risk: str) -> int:
    return RISK_ORDER.index(risk) if risk in RISK_ORDER else len(RISK_ORDER)


def can_group(a: dict, b: dict, config: dict) -> tuple[bool, str]:
    """Whether two issues may share a PR, and if not, why not."""
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


def group_issues(records: list[dict], config: dict) -> list[dict]:
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

    batches = []
    for index, group in enumerate(packed, start=1):
        paths = sorted({p for r in group for p in (r.get("paths") or [])})
        batches.append(
            {
                "id": f"b-{index:03d}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--triage", required=True, help="output of `triage.py plan`")
    p.add_argument("--config", default=".foreman/config.json")
    p.add_argument("--profile", default=".foreman/ci-profile.json")
    p = sub.add_parser("apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--ledger", default=".foreman")

    args = parser.parse_args(argv)

    if args.cmd == "plan":
        triage_out = json.loads(Path(args.triage).read_text())
        config = json.loads(Path(args.config).read_text()) if Path(args.config).exists() else {}
        profile = json.loads(Path(args.profile).read_text()) if Path(args.profile).exists() else {}
        batches = group_issues(triage_out.get("triaged", []), config)
        print(
            json.dumps(
                {"batches": batches, "savings": estimate_savings(batches, profile)}, indent=2
            )
        )
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ledger as ledger_mod

    root = Path(args.ledger)
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
