#!/usr/bin/env python3
"""Render the ledger as a digest: what is moving, what is stuck, what needs you.

CLI:
    status.py [--ledger .foreman] [--config .foreman/config.json] [--json]

`--root` is accepted as an alias for `--ledger`, for command lines written before
the two names were reconciled.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import ledger

IN_FLIGHT = ("planned", "building", "built", "open", "blocked", "ready", "merging")


def _issues(batch: dict) -> str:
    return " ".join(f"#{n}" for n in batch.get("issues", [])) or "—"


def _needs_human(state: ledger.State, caps: dict) -> list[str]:
    """Two ways a batch stops being the loop's problem and becomes yours."""
    lines = []
    for event in state.escalations:
        # An escalation event is never retracted, so the batch's current state is
        # the authority. A merged or requeued batch needs nobody, and a NEEDS YOU
        # section that accumulates resolved items is one nobody reads.
        batch = state.batches.get(event.get("batch"))
        if batch is not None and batch.get("state") != "escalated":
            continue
        lines.append(
            f"  {event.get('batch', '—'):<10} escalated — "
            f"{event.get('reason', 'no reason recorded')}"
        )
    for bid, batch in sorted(state.batches.items()):
        if batch["state"] in ("merged", "abandoned", "escalated"):
            continue
        breached = ledger.cap_breached(batch, caps)
        if breached:
            # `.get`, as cap_breached reads it: a batch.meta correction may have
            # replaced `attempts` with a partial mapping, and the LEDGER warning
            # below is what tells an operator to write one.
            hit = batch["attempts"].get(breached, 0)
            lines.append(
                f"  {bid:<10} at cap — {breached} = {hit}/{caps[breached]}, loop will not retry"
            )
    return lines


def render(state: ledger.State, config: dict | None = None) -> str:
    config = config or {}
    caps = config.get("caps", {})
    out: list[str] = ["foreman status", "=" * 60, ""]

    in_flight = {b: r for b, r in sorted(state.batches.items()) if r["state"] in IN_FLIGHT}
    merged = [b for b, r in state.batches.items() if r["state"] == "merged"]

    out.append(f"IN FLIGHT ({len(in_flight)})")
    if not in_flight:
        out.append("  No batches in flight.")
    else:
        out.append(f"  {'batch':<10} {'state':<10} {'ci':<12} {'review':<18} {'pr':<6} issues")
        for bid, batch in in_flight.items():
            pr = f"#{batch['pr']}" if batch.get("pr") else "—"
            out.append(
                f"  {bid:<10} {batch['state']:<10} {batch['ci_gate']:<12} "
                f"{batch['review_gate']:<18} {pr:<6} {_issues(batch)}"
            )
            if batch["state"] == "open" and ledger.may_run_expensive_tier(batch):
                out.append(f"  {'':<10} └─ cheap gates clear; full suite may run")
    out.append("")

    attention = _needs_human(state, caps)
    out.append(f"NEEDS YOU ({len(attention)})")
    out.extend(attention or ["  Nothing blocked."])
    out.append("")

    # A line the fold could not use was dropped rather than crashing every
    # script, which is the right trade — but a state quietly missing an event
    # is a state that has stopped matching the repository. Say it here, once,
    # where a person reads.
    if state.skipped_lines:
        out.append("LEDGER")
        out.append(
            f"  ⚠ {state.skipped_lines} line(s) in events.jsonl could not be read and were "
            "skipped; the state above may be missing what they recorded. Reconcile "
            "against GitHub and append a batch.meta correction."
        )
        out.append("")

    if state.flakes:
        out.append("FLAKES (most seen first)")
        for key, count in sorted(state.flakes.items(), key=lambda kv: (-kv[1], kv[0]))[:5]:
            job, _, test = key.partition("::")
            out.append(f"  {count:>3}x  {test}   [{job}]")
        out.append("")

    # The rubber-stamp signal. A reviewer that approves everything looks fine
    # until you count how much of what it approved came back out of trunk.
    clean = [r for r in state.reviews if r.get("verdict") == "clean"]
    if clean:
        # Reverts of batches the reviewer approved, and only those. Counting
        # every revert in the ledger charged the gate for a batch it had
        # refused and a person merged anyway.
        approved = {r.get("batch") for r in clean}
        reverted = sum(1 for e in state.reverts if e.get("batch") in approved)
        pct = round(100 * reverted / len(clean))
        out.append("REVIEW QUALITY")
        out.append(f"  clean reviews later reverted: {reverted}/{len(clean)} ({pct}%)")
        if pct >= 10:
            out.append("  ⚠ above 10% — the review gate is approving too easily")
        out.append("")

    out.append(
        f"TOTALS  merged: {len(merged)}   triaged issues: {len(state.issues)}   "
        f"batches: {len(state.batches)}"
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # --ledger is the name triage.py, batch.py and loop.py use for the same
    # directory. --root is kept as an alias rather than removed: it is what the
    # /foreman:status command and every command line written before this fix
    # pass, and breaking those to tidy a flag name is not a trade worth making.
    parser.add_argument("--ledger", "--root", default=f"./{ledger.LEDGER_DIR}")
    parser.add_argument(
        "--config",
        default=None,
        help=f"foreman config (default {ledger.LEDGER_DIR}/{ledger.CONFIG_FILE} "
        "in the repository root)",
    )
    parser.add_argument("--json", action="store_true", help="emit raw state instead of the digest")
    args = parser.parse_args(argv)

    root = Path(args.ledger)
    # Anchored to the repository, and loud when it is not there — the same
    # mechanism loop.py, land.py and triage.py use (issue #70). This script
    # resolves its events file correctly from a build worktree but used to
    # resolve the config against the caller, so the digest rendered with
    # `caps={}`: every counter shown without its ceiling, NEEDS YOU empty
    # however far past its cap a batch was, and not a word about why.
    config = ledger.load_config(args.config)

    state = ledger.load(root)
    if args.json:
        print(
            json.dumps(
                {
                    "batches": state.batches,
                    "issues": state.issues,
                    "escalations": state.escalations,
                    "flakes": state.flakes,
                },
                indent=2,
            )
        )
    else:
        print(render(state, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
