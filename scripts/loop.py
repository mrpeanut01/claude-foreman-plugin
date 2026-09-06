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

# How long the loop will go without looking for new issues. Triage costs no CI,
# so this only needs to be short enough that overnight work is not missed.
DEFAULT_TRIAGE_EVERY_S = 3600

DORMANT = {"merged", "abandoned", "escalated"}
# `building` belongs here: it holds a worktree and a branch, and a session is
# meant to be inside it. Leaving it out let the loop start work past the WIP
# limit while a build was already running, and hid the interrupted build from
# every branch of next_action at once.
IN_FLIGHT = {"building", "built", "open", "blocked", "ready", "merging"}


def in_flight_count(state: ledger.State) -> int:
    """Batches holding a worktree, a branch or a PR. Each costs CI on every trunk move."""
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


def when(stamp: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None when unreadable."""
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return seen if seen.tzinfo else seen.replace(tzinfo=UTC)


def seconds_since(stamp: str | None, now: datetime | None = None) -> float | None:
    """Seconds since an ISO timestamp, or None when it is missing or unreadable."""
    seen = when(stamp)
    if seen is None:
        return None
    return ((now or datetime.now(UTC)) - seen).total_seconds()


def age_seconds(batch: dict, now: datetime | None = None) -> float | None:
    """Seconds since this batch last actually moved, or None if unknown."""
    return seconds_since(batch.get("progress_at") or batch.get("updated"), now)


def triage_due(state: ledger.State, limits: dict) -> bool:
    """Whether it is worth looking for issues the ledger has not seen yet.

    Triage is the loop's only source of new work, so it cannot be reachable
    from an empty ledger alone: after the first batch exists that condition is
    false forever and the loop idles while issues pile up on the tracker.
    """
    every = limits.get("triage_every_s", DEFAULT_TRIAGE_EVERY_S)
    if not every:
        return False
    since = seconds_since(state.last_triage_at)
    return since is None or since > every


def review_rounds(state: ledger.State, batch_id: str) -> list[list[dict]]:
    """Findings from each review round for one batch, oldest first."""
    return [r.get("findings", []) for r in state.reviews if r.get("batch") == batch_id]


def _seen_open_since(state: ledger.State, issue: int, batch: dict) -> bool:
    """Whether triage has seen this issue since its batch stopped moving.

    `triage.py` asks GitHub for open issues only, so a triage record written
    after the batch landed is direct evidence that the landing did not close
    the issue. An older record says nothing either way, and silence is not
    evidence: without it the issue stays with the batch.
    """
    seen = when((state.issues.get(issue) or {}).get("ts"))
    landed = when(batch.get("progress_at") or batch.get("updated"))
    return seen is not None and landed is not None and seen > landed


def _grouped_issues(state: ledger.State) -> set[int]:
    """Issues some batch is already accountable for.

    Membership alone is not the test. A merged batch used to hold its issues
    forever, so an issue whose PR merged without closing it became invisible to
    the loop permanently — nothing reconciles a merged batch against whether
    the issues it named actually closed, and the loop believed the work was
    done while the issue that motivated it stayed open (issue #58). A merged
    batch therefore stops holding an issue that triage has since seen open.

    `escalated` and `abandoned` batches keep holding theirs unconditionally: a
    person is deciding what happens to those, and offering the same issues to a
    new batch would duplicate whatever that person is doing.
    """
    grouped: set[int] = set()
    for batch in state.batches.values():
        released = batch.get("state") == "merged"
        for issue in batch.get("issues") or []:
            if released and _seen_open_since(state, issue, batch):
                continue
            grouped.add(issue)
    return grouped


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

    # Push deadlock is about a failure that keeps coming back, not pushes
    # elapsed. The caps above are runaway ceilings; this reads progress.
    for batch in live:
        stuck = ledger.futile_push_run(batch, caps)
        if stuck:
            return {"do": "escalate", "batch": batch["id"], "reason": stuck}

    # A build that keeps being picked up is the same shape of problem, and the
    # only one of the three the clock cannot catch: `building -> building`
    # records no progress, so `stale_after_s` reads the same age forever.
    for batch in live:
        stalled = ledger.stalled_build(batch, caps)
        if stalled:
            return {"do": "escalate", "batch": batch["id"], "reason": stalled}

    # Review deadlock is about repeating findings, not rounds elapsed.
    for batch in live:
        if batch.get("review_gate") != "changes_requested":
            continue
        stalled = land.review_stalled(
            review_rounds(state, batch["id"]),
            caps.get("review_rounds", land.DEFAULT_REVIEW_CEILING),
        )
        if stalled:
            return {"do": "escalate", "batch": batch["id"], "reason": stalled}

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
        # A gate that has already answered is work, not something to wait on.
        answered = [
            f"{name} is {batch[f'{name}_gate']}"
            for name in pending
            if batch[f"{name}_gate"] in {"failed", "changes_requested"}
        ]
        if answered:
            return {"do": "unblock", "batch": batch["id"], "reason": "; ".join(answered)}
        # No gate may pin a batch indefinitely. Watching forever is invisible:
        # no counter increments, so no cap ever fires and nothing reaches a human.
        stale_after = limits.get("stale_after_s")
        age = age_seconds(batch)
        if stale_after and age is not None and age > stale_after:
            return {
                "do": "escalate",
                "batch": batch["id"],
                "reason": (
                    f"stale: waiting on {', '.join(pending)} for {age / 3600:.1f}h with no change"
                ),
            }
        return {
            "do": "watch",
            "batch": batch["id"],
            "reason": f"waiting on {', '.join(pending)}",
            "may_run_expensive_tier": ledger.may_run_expensive_tier(batch),
        }

    # Nearest the merge first: a batch two steps from landing drains cheaper
    # than one that has not compiled yet. `building` sits at the end of this
    # group because it is the least finished of the three — but still ahead of
    # `planned`, because resuming a worktree that already exists beats cutting
    # a new one. Resuming is not free (the local gate re-runs and the batch is
    # heading for a push), so it is budget-gated like its neighbours.
    for state_name, action, reason in (
        ("blocked", "unblock", "a gate came back red; fix and push again"),
        ("built", "open_pr", "committed locally; open the PR to start the gates"),
        (
            "building",
            "build",
            "a build was interrupted mid-flight; resume it in the existing worktree — "
            "the batch is already in `building`, so re-entering it is a no-op",
        ),
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

    if triage_due(state, limits):
        reason = (
            "nothing in the ledger yet"
            if not state.issues and not state.batches
            else "no triage within the refresh window; look for new issues"
        )
        return {"do": "triage", "reason": reason}

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
    p.add_argument(
        "--config",
        help=f"foreman config (default {ledger.LEDGER_DIR}/{ledger.CONFIG_FILE} "
        "in the repository root)",
    )

    args = parser.parse_args(argv)
    # Anchored, and loud when it is not there: an unfound config leaves this
    # function with no caps, no budget and no staleness window at all.
    config = ledger.load_config(args.config)
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
