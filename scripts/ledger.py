#!/usr/bin/env python3
"""Foreman ledger: an append-only event log, folded into current state.

Why an event log and not a state file: the loop is long-running, crashes,
runs out of context, and may have two sessions touching one repo. Appending
a line is atomic enough to survive all three; a rewritten state file is not.
Current state is always a fold over the log, so it is never stale and the
whole history stays auditable.

CLI:
    ledger.py init [--root DIR]
    ledger.py append --type TYPE --json '{"batch": "b-001"}'
    ledger.py transition BATCH STATE
    ledger.py gate BATCH {ci|review} VALUE
    ledger.py state [--batch ID]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LEDGER_DIR = ".foreman"
EVENTS = "events.jsonl"

# --- the batch lifecycle ------------------------------------------------------
# A batch is one group of issues heading for one PR. Gates (CI and review) run
# concurrently and are tracked separately from this coarse lifecycle.

TRANSITIONS: dict[str, set[str]] = {
    "planned":   {"building", "escalated", "abandoned"},
    "building":  {"built", "escalated", "abandoned"},
    "built":     {"open", "escalated", "abandoned"},
    "open":      {"blocked", "ready", "escalated", "abandoned"},
    "blocked":   {"open", "escalated", "abandoned"},
    "ready":     {"merging", "blocked", "escalated"},
    "merging":   {"merged", "blocked", "escalated"},
    "escalated": {"planned", "abandoned"},
    "merged":    set(),
    "abandoned": set(),
}
TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}

CI_GATE_VALUES = {"pending", "cheap_green", "full_green", "failed"}
REVIEW_GATE_VALUES = {"pending", "clean", "changes_requested"}
GATES = {"ci": CI_GATE_VALUES, "review": REVIEW_GATE_VALUES}

# Merging needs the full suite green and a clean independent review. Nothing
# else clears the gate — "cheap CI passed" is explicitly not enough.
CLEAR = {"ci": "full_green", "review": "clean"}

COUNTER_ORDER = ("pushes", "review_rounds", "reruns")


class LedgerError(Exception):
    pass


class IllegalTransition(LedgerError):
    pass


class GateNotClear(LedgerError):
    pass


class UnknownBatch(LedgerError):
    pass


@dataclass
class State:
    issues: dict[int, dict] = field(default_factory=dict)
    batches: dict[str, dict] = field(default_factory=dict)
    escalations: list[dict] = field(default_factory=list)
    flakes: dict[str, int] = field(default_factory=dict)
    reviews: list[dict] = field(default_factory=list)
    reverts: list[dict] = field(default_factory=list)
    skipped_lines: int = 0


# --- storage ------------------------------------------------------------------

def init(repo_root: Path | str) -> Path:
    root = Path(repo_root) / LEDGER_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / EVENTS).touch()
    return root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append(root: Path, event_type: str, **fields) -> dict:
    event = {"ts": _now(), "type": event_type, **fields}
    with (Path(root) / EVENTS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=False) + "\n")
    return event


def read_events(root: Path) -> list[dict]:
    path = Path(root) / EVENTS
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn write must not take the whole ledger down
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


# --- folding ------------------------------------------------------------------

def _new_batch(event: dict) -> dict:
    return {
        "id": event.get("batch"),
        "issues": event.get("issues", []),
        "branch": event.get("branch"),
        "pr": event.get("pr"),
        "state": "planned",
        "ci_gate": "pending",
        "review_gate": "pending",
        "attempts": {k: 0 for k in COUNTER_ORDER},
        "created": event.get("ts"),
        "updated": event.get("ts"),
    }


def fold(events: list[dict]) -> State:
    state = State()
    for event in events:
        kind = event.get("type")
        bid = event.get("batch")
        batch = state.batches.get(bid) if bid else None

        if kind == "issue.triaged":
            state.issues[event["issue"]] = {k: v for k, v in event.items() if k != "type"}

        elif kind == "batch.created":
            state.batches[bid] = _new_batch(event)

        elif kind == "batch.state" and batch:
            batch["state"] = event["to"]

        elif kind == "batch.pushed" and batch:
            # A new commit invalidates every gate verdict about the old one.
            batch["ci_gate"] = "pending"
            batch["review_gate"] = "pending"
            batch["attempts"]["pushes"] += 1
            batch["head_sha"] = event.get("sha")

        elif kind == "gate.set" and batch:
            which, value = event["gate"], event["value"]
            batch[f"{which}_gate"] = value
            if which == "review" and value == "changes_requested":
                batch["attempts"]["review_rounds"] += 1
            # A gate going red under a batch already declared ready un-declares it.
            if batch["state"] == "ready" and value != CLEAR.get(which):
                batch["state"] = "blocked"

        elif kind == "ci.rerun" and batch:
            batch["attempts"]["reruns"] += 1

        elif kind == "batch.meta" and batch:
            batch.update({k: v for k, v in event.items()
                          if k not in {"type", "ts", "batch"}})

        elif kind == "escalation":
            state.escalations.append(event)

        elif kind == "flake.observed":
            key = f"{event.get('job')}::{event.get('test')}"
            state.flakes[key] = state.flakes.get(key, 0) + 1

        elif kind == "review.verdict":
            state.reviews.append(event)

        elif kind == "merge.reverted":
            state.reverts.append(event)

        if batch is not None:
            batch["updated"] = event.get("ts", batch.get("updated"))
    return state


def load(root: Path) -> State:
    return fold(read_events(root))


# --- rules --------------------------------------------------------------------

def blocking_gates(batch: dict) -> list[str]:
    """Gates not yet clear enough to merge, in a stable order."""
    return [name for name in ("ci", "review")
            if batch.get(f"{name}_gate") != CLEAR[name]]


def may_run_expensive_tier(batch: dict) -> bool:
    """Spend the slow suite only once the cheap signals agree it is worth it."""
    return (batch.get("review_gate") == "clean"
            and batch.get("ci_gate") in {"cheap_green", "full_green"})


def cap_breached(batch: dict, caps: dict[str, int]) -> str | None:
    """Name the first attempt counter that has hit its cap, else None."""
    attempts = batch.get("attempts", {})
    for name in COUNTER_ORDER:
        if name in caps and attempts.get(name, 0) >= caps[name]:
            return name
    return None


def transition(root: Path, batch_id: str, to_state: str) -> dict:
    state = load(root)
    batch = state.batches.get(batch_id)
    if batch is None:
        raise UnknownBatch(f"no batch {batch_id!r} in the ledger")

    current = batch["state"]
    if to_state not in TRANSITIONS.get(current, set()):
        detail = "terminal state" if current in TERMINAL else "not a legal move"
        raise IllegalTransition(
            f"{batch_id}: {current} -> {to_state} is {detail}; "
            f"legal from {current}: {sorted(TRANSITIONS.get(current, set())) or 'nothing'}"
        )
    if to_state == "ready":
        blocking = blocking_gates(batch)
        if blocking:
            raise GateNotClear(
                f"{batch_id}: cannot be ready while these gates are not clear: "
                + ", ".join(f"{g} ({batch[f'{g}_gate']})" for g in blocking)
            )
    return append(root, "batch.state", batch=batch_id, **{"from": current, "to": to_state})


def gate(root: Path, batch_id: str, which: str, value: str) -> dict:
    if which not in GATES:
        raise LedgerError(f"unknown gate {which!r}; expected one of {sorted(GATES)}")
    if value not in GATES[which]:
        raise LedgerError(f"{which} gate cannot be {value!r}; expected one of {sorted(GATES[which])}")
    return append(root, "gate.set", batch=batch_id, gate=which, value=value)


# --- CLI ----------------------------------------------------------------------

def _resolve(root_arg: str | None) -> Path:
    return Path(root_arg) if root_arg else Path.cwd() / LEDGER_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", help=f"ledger directory (default ./{LEDGER_DIR})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("append"); p.add_argument("--type", required=True); p.add_argument("--json", default="{}")
    p = sub.add_parser("transition"); p.add_argument("batch"); p.add_argument("state")
    p = sub.add_parser("gate"); p.add_argument("batch"); p.add_argument("which", choices=sorted(GATES)); p.add_argument("value")
    p = sub.add_parser("state"); p.add_argument("--batch")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            print(init(Path(args.root) if args.root else Path.cwd()))
            return 0
        root = _resolve(args.root)
        if args.cmd == "append":
            print(json.dumps(append(root, args.type, **json.loads(args.json))))
        elif args.cmd == "transition":
            print(json.dumps(transition(root, args.batch, args.state)))
        elif args.cmd == "gate":
            print(json.dumps(gate(root, args.batch, args.which, args.value)))
        elif args.cmd == "state":
            st = load(root)
            payload = st.batches.get(args.batch) if args.batch else {
                "issues": st.issues, "batches": st.batches,
                "escalations": st.escalations, "flakes": st.flakes,
            }
            print(json.dumps(payload, indent=2))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
