#!/usr/bin/env python3
"""The scheduler: given the ledger, what is the single best next action?

The loop takes one step at a time and re-reads state between steps, so a crash
costs one action rather than a plan. Priority is "finish what is started before
starting more": an in-flight batch is holding a PR open and re-running CI on
every trunk move, so draining it is worth more than beginning new work.

CLI:
    loop.py next [--ledger .foreman] [--config .foreman/config.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import land  # noqa: E402
import ledger  # noqa: E402

DORMANT = {"merged", "abandoned", "escalated"}
IN_FLIGHT = {"built", "open", "blocked", "ready", "merging"}


def in_flight_count(state: ledger.State) -> int:
    """Batches holding a branch or a PR open. Each one costs CI on every trunk move."""
    return sum(1 for b in state.batches.values() if b.get("state") in IN_FLIGHT)


def spent_today(state: ledger.State) -> float:
    today = datetime.now(UTC).date().isoformat()
    return sum(
        e.get("seconds", 0) for e in state.ci_spend if str(e.get("ts", "")).startswith(today)
    )


def budget_remaining(state: ledger.State, config: dict) -> float | None:
    """Seconds of CI left today, or None when no ceiling is configured."""
    minutes = (config.get("limits") or {}).get("max_ci_minutes_per_day")
    if not minutes:
        return None
    return minutes * 60 - spent_today(state)


def _grouped_issues(state: ledger.State) -> set[int]:
    return {i for b in state.batches.values() for i in (b.get("issues") or [])}


def next_action(state: ledger.State, config: dict) -> dict:
    """One action. Never a plan — the ledger may have moved since the last step."""
    caps = config.get("caps") or {}
    limits = config.get("limits") or {}
    max_open = limits.get("max_open_prs", 3)

    remaining = budget_remaining(state, config)
    can_spend = remaining is None or remaining > 0
    budget_blocked = False

    live = [b for _, b in sorted(state.batches.items()) if b.get("state") not in DORMANT]

    # A batch past its caps is not the loop's problem any more.
    for batch in live:
        breached = ledger.cap_breached(batch, caps)
        if breached:
            attempts = batch["attempts"][breached]
            return {
                "do": "escalate",
                "batch": batch["id"],
                "reason": f"{breached} at cap ({attempts}/{caps[breached]}); "
                "the loop will not retry",
            }

    for batch in live:
        if batch["state"] != "ready":
            continue
        blockers = land.merge_blockers(batch, {"labels": []}, config)
        if blockers:
            return {"do": "escalate", "batch": batch["id"], "reason": "; ".join(blockers)}
        if can_spend:
            return {
                "do": "merge",
                "batch": batch["id"],
                "reason": "both gates clear and nothing blocks the merge",
            }
        budget_blocked = True

    for batch in live:
        if batch["state"] != "open":
            continue
        pending = ledger.blocking_gates(batch)
        if not pending:
            return {"do": "advance", "batch": batch["id"], "reason": "gates cleared; move to ready"}
        return {
            "do": "watch",
            "batch": batch["id"],
            "reason": f"waiting on {', '.join(pending)}",
            "may_run_expensive_tier": ledger.may_run_expensive_tier(batch),
        }

    for state_name, action, reason in (
        ("blocked", "unblock", "a gate came back red; fix and push again"),
        ("built", "open_pr", "committed locally; open the PR to start the gates"),
    ):
        for batch in live:
            if batch["state"] == state_name:
                if not can_spend:
                    budget_blocked = True
                    break
                return {"do": action, "batch": batch["id"], "reason": reason}

    for batch in live:
        if batch["state"] == "planned":
            if not can_spend:
                budget_blocked = True
                break
            if in_flight_count(state) >= max_open:
                break  # WIP limit: draining beats starting
            return {"do": "build", "batch": batch["id"], "reason": "capacity to start new work"}

    if not state.issues and not state.batches:
        return {"do": "triage", "reason": "nothing in the ledger yet"}

    ungrouped = [
        n
        for n, r in sorted(state.issues.items())
        if r.get("verdict") == "actionable" and n not in _grouped_issues(state)
    ]
    if ungrouped:
        return {
            "do": "batch",
            "issues": ungrouped,
            "reason": f"{len(ungrouped)} actionable issue(s) not yet in a batch",
        }

    if budget_blocked:
        return {
            "do": "idle",
            "reason": f"CI budget for today is spent ({spent_today(state) / 60:.0f} min used)",
        }

    return {
        "do": "idle",
        "reason": "nothing actionable; every batch is done, escalated, or waiting",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next")
    p.add_argument("--ledger", default=".foreman")
    p.add_argument("--config", default=".foreman/config.json")

    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text()) if Path(args.config).exists() else {}
    state = ledger.load(Path(args.ledger))
    action = next_action(state, config)
    remaining = budget_remaining(state, config)
    print(
        json.dumps(
            {**action, "in_flight": in_flight_count(state), "budget_seconds_left": remaining},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
