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
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

LEDGER_DIR = ".foreman"
EVENTS = "events.jsonl"
CONFIG_FILE = "config.json"

# --- the batch lifecycle ------------------------------------------------------
# A batch is one group of issues heading for one PR. Gates (CI and review) run
# concurrently and are tracked separately from this coarse lifecycle.

TRANSITIONS: dict[str, set[str]] = {
    "planned": {"building", "escalated", "abandoned"},
    # `building -> building` is the resume: a build interrupted by a crash or a
    # context limit is picked up again by re-entering the state it never left,
    # and the recipe in commands/build.md opens with exactly that transition.
    # The self-loop makes resuming idempotent instead of an error to reason
    # around; the fold treats it as no movement, so it cannot launder staleness.
    "building": {"building", "built", "escalated", "abandoned"},
    "built": {"open", "escalated", "abandoned"},
    "open": {"blocked", "ready", "escalated", "abandoned"},
    "blocked": {"open", "escalated", "abandoned"},
    "ready": {"merging", "blocked", "escalated"},
    "merging": {"merged", "blocked", "escalated"},
    "escalated": {"planned", "abandoned"},
    "merged": set(),
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

# Counters that measure whether a batch is converging rather than how much has
# happened to it. `cap_breached` deliberately ignores these: a runaway ceiling
# and a wrong diagnosis are different verdicts read off different numbers. Each
# has its own rule below, and each of those rules carries a default ceiling —
# unlike `caps`, which is opt-in, these hold on a repo with no config at all.
PROGRESS_COUNTERS = ("futile_pushes", "build_resumes")

# Three pushes into the same red CI is where "the next attempt will get it"
# stops being defensible. Loose enough that fixing one failing test and
# uncovering a different one is not mistaken for a failed diagnosis.
DEFAULT_FUTILE_PUSH_CEILING = 3

# A build that has been picked up three times and still has not reached `built`
# is not having bad luck. Same shape of number as the push ceiling, and loose
# enough to absorb a closed laptop, a context limit, and one crash.
DEFAULT_BUILD_RESUME_CEILING = 3


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
    ci_spend: list[dict] = field(default_factory=list)
    last_triage_at: str | None = None
    # When triage last found each issue on the tracker's *open* list. Separate
    # from `issues` because the most useful sighting is usually of an issue
    # triage deliberately did not re-record — see `triage.should_skip`.
    open_seen_at: dict[int, str] = field(default_factory=dict)
    skipped_lines: int = 0


# --- where the ledger lives ---------------------------------------------------
# Every script defaults `--ledger` to a relative `.foreman`, and a build runs
# with its cwd inside `../foreman-<batch>`, so a relative path used to name a
# different directory depending on who was calling. `init` then created it on
# demand, and the write went into a second ledger nobody reads: pushes stayed
# uncounted, gates were never reset, and the loop merged commits whose review
# had been recorded somewhere else entirely (issue #64). A relative path is
# therefore anchored to the repository, never to the caller.


@cache
def _git_dir(start: str) -> str | None:
    """The shared `.git` directory for `start`, or None outside a repository.

    `--git-common-dir` rather than `--show-toplevel` on purpose: inside a linked
    worktree the toplevel is the worktree, which is exactly the wrong answer
    here. The common dir is the one thing every worktree of a repo agrees on.

    Cached per directory because `append` is called in loops and a directory
    does not change which repository it belongs to while a process runs.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=start,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None  # no git on PATH: a directory is still a fine place for a ledger
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.strip()


@cache
def _toplevel(start: str) -> str | None:
    """The working tree root, for the layouts `--git-common-dir` cannot place."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True
        )
    except OSError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def repo_root(start: Path | str | None = None) -> Path:
    """The checkout that owns the ledger, or `start` when there is no repository.

    From a linked worktree this is the main checkout, so a build writing from
    `../foreman-b-002` and the loop reading from the repo see one ledger.
    """
    here = Path(start) if start else Path.cwd()
    common = _git_dir(str(here))
    if common:
        path = Path(common)
        if not path.is_absolute():
            path = here / path
        # A `.git` directory sits in its working tree; anything else (a bare
        # repo, or --separate-git-dir) does not, so ask where the tree is.
        if path.name == ".git" and path.parent.is_dir():
            return path.parent
    top = _toplevel(str(here))
    return Path(top) if top else here


def resolve_root(root: Path | str | None) -> Path:
    """A ledger directory, anchored to the repository unless it was given absolutely.

    An absolute path is always obeyed as given — that is how a caller says
    "this ledger, not the one you would have picked".
    """
    if root is None:
        return repo_root() / LEDGER_DIR
    path = Path(root)
    return path if path.is_absolute() else repo_root() / path


def resolve_config(path: Path | str | None) -> Path:
    """The config file, anchored exactly like the ledger it sits beside.

    `--config` had the same shape of bug as `--ledger` did (issue #64): a
    relative default resolved against the caller's cwd, and a build runs inside
    `../foreman-<batch>`, so the file was simply not there.
    """
    if path is None:
        return repo_root() / LEDGER_DIR / CONFIG_FILE
    given = Path(path)
    return given if given.is_absolute() else repo_root() / given


def load_config(path: Path | str | None = None) -> dict:
    """The foreman config, or `{}` — but never `{}` quietly.

    Every governor in this system is opt-in: `cap_breached` only fires for
    counters that appear in `caps`, `stale_after_s` has no default, and
    `risk_level` sees no protected paths without `protected_paths`. So an empty
    config is not a conservative fallback, it is the loop with its brakes
    removed, and the only reason that was survivable is that it stayed hidden.
    Say it on stderr instead: stdout is JSON that callers parse.
    """
    resolved = resolve_config(path)
    if not resolved.exists():
        print(
            f"warning: no foreman config at {resolved} — running with no caps, "
            "no staleness window and no protected paths. Nothing will stop a "
            f"runaway batch. Copy config.example.json to {LEDGER_DIR}/{CONFIG_FILE}.",
            file=sys.stderr,
        )
        return {}
    return json.loads(resolved.read_text(encoding="utf-8"))


# --- storage ------------------------------------------------------------------


def init(repo_root_dir: Path | str) -> Path:
    root = resolve_root(Path(repo_root_dir) / LEDGER_DIR)
    root.mkdir(parents=True, exist_ok=True)
    (root / EVENTS).touch()
    return root


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def append(root: Path, event_type: str, **fields) -> dict:
    event = {"ts": _now(), "type": event_type, **fields}
    with (resolve_root(root) / EVENTS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=False) + "\n")
    return event


def read_events(root: Path) -> list[dict]:
    path = resolve_root(root) / EVENTS
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
        # merge_blockers reads these; dropping them here would silently
        # disable the protected-path check.
        "paths": event.get("paths", []),
        "risk": event.get("risk"),
        "state": "planned",
        "ci_gate": "pending",
        "review_gate": "pending",
        "attempts": {k: 0 for k in COUNTER_ORDER + PROGRESS_COUNTERS},
        "created": event.get("ts"),
        "updated": event.get("ts"),
        # `updated` moves on any event; `progress_at` only when something
        # actually changed. Staleness must measure the second, or polling
        # a stuck batch resets the very clock meant to notice it.
        "progress_at": event.get("ts"),
    }


def _score_push(batch: dict, ci_verdict: str) -> None:
    """Judge the last push the moment CI answers it.

    The push cap exists to catch a wrong diagnosis, not a well-reviewed PR, but
    counting every push cannot tell them apart: a batch that took three honest
    review rounds hits the cap alongside one that has been guessing (issue #17).
    What separates them is whether the push moved anything, and the only thing
    the ledger can compare a push against is the verdict it was pushed into.
    CI coming back red the same way it was red before the push means the fix
    missed; any green means it did not.

    The review gate is deliberately not scored here. Rounds of *different*
    findings are convergence, not repetition, and `land.review_stalled` already
    judges those on the findings themselves rather than on how many there were.
    """
    was = batch.pop("pushed_against_ci", None)  # each push is judged exactly once
    if ci_verdict != "failed":
        batch["attempts"]["futile_pushes"] = 0
    elif was == "failed":
        batch["attempts"]["futile_pushes"] = batch["attempts"].get("futile_pushes", 0) + 1


def fold(events: list[dict]) -> State:
    state = State()
    for event in events:
        kind = event.get("type")
        bid = event.get("batch")
        batch = state.batches.get(bid) if bid else None

        if kind == "triage.completed":
            state.last_triage_at = event.get("ts")
            # Every issue the pass looked at, including the ones it skipped.
            # `triage.fetch_issues` asks GitHub for open issues only, so this is
            # a sighting: proof the issue was still open at this timestamp.
            for number in event.get("open_issues") or []:
                state.open_seen_at[number] = event.get("ts")

        elif kind == "issue.triaged":
            state.issues[event["issue"]] = {k: v for k, v in event.items() if k != "type"}
            state.open_seen_at[event["issue"]] = event.get("ts")

        elif kind == "batch.created":
            # Ids are meant to be unique. A repeat means an upstream numbering
            # bug, and replacing the record would silently discard whatever the
            # first batch of that id did — including a merge. Keep the original.
            if bid not in state.batches:
                state.batches[bid] = _new_batch(event)

        elif kind == "batch.state" and batch:
            # The resume: `building -> building`. It is deliberately not
            # progress, so nothing else in the fold moves for it — which is
            # exactly why it needs counting. Without a number here a batch can
            # be picked up forever and no rule anywhere can see it happening.
            if batch["state"] == "building" and event["to"] == "building":
                attempts = batch["attempts"]
                attempts["build_resumes"] = attempts.get("build_resumes", 0) + 1
            if batch["state"] != event["to"]:
                batch["progress_at"] = event.get("ts")
            batch["state"] = event["to"]

        elif kind == "batch.pushed" and batch:
            # A new commit invalidates every gate verdict about the old one.
            # Keep the CI verdict this push was answering, though: `_score_push`
            # needs it to tell a fix that missed from one that landed.
            batch["pushed_against_ci"] = batch.get("ci_gate")
            batch["ci_gate"] = "pending"
            batch["review_gate"] = "pending"
            batch["attempts"]["pushes"] += 1
            batch["head_sha"] = event.get("sha")
            batch["progress_at"] = event.get("ts")

        elif kind == "gate.set" and batch:
            which, value = event["gate"], event["value"]
            if batch.get(f"{which}_gate") != value:
                batch["progress_at"] = event.get("ts")
            batch[f"{which}_gate"] = value
            if which == "review" and value == "changes_requested":
                batch["attempts"]["review_rounds"] += 1
            if which == "ci" and value != "pending":
                _score_push(batch, value)
            # A gate going red under a batch already declared ready un-declares it.
            if batch["state"] == "ready" and value != CLEAR.get(which):
                batch["state"] = "blocked"

        elif kind == "ci.rerun" and batch:
            batch["attempts"]["reruns"] += 1

        elif kind == "batch.meta" and batch:
            batch.update({k: v for k, v in event.items() if k not in {"type", "ts", "batch"}})

        elif kind == "escalation":
            state.escalations.append(event)

        elif kind == "flake.observed":
            key = f"{event.get('job')}::{event.get('test')}"
            state.flakes[key] = state.flakes.get(key, 0) + 1

        elif kind == "review.verdict":
            state.reviews.append(event)

        elif kind == "merge.reverted":
            state.reverts.append(event)

        elif kind == "ci.launched":
            state.ci_spend.append(event)

        if batch is not None:
            batch["updated"] = event.get("ts", batch.get("updated"))
    return state


def load(root: Path) -> State:
    return fold(read_events(root))


# --- rules --------------------------------------------------------------------


def blocking_gates(batch: dict) -> list[str]:
    """Gates not yet clear enough to merge, in a stable order."""
    return [name for name in ("ci", "review") if batch.get(f"{name}_gate") != CLEAR[name]]


def may_run_expensive_tier(batch: dict) -> bool:
    """Spend the slow suite only once the cheap signals agree it is worth it."""
    return batch.get("review_gate") == "clean" and batch.get("ci_gate") in {
        "cheap_green",
        "full_green",
    }


def cap_breached(batch: dict, caps: dict[str, int]) -> str | None:
    """Name the first attempt counter that has hit its cap, else None."""
    attempts = batch.get("attempts", {})
    for name in COUNTER_ORDER:
        if name in caps and attempts.get(name, 0) >= caps[name]:
            return name
    return None


def futile_push_run(batch: dict, caps: dict[str, int]) -> str | None:
    """Reason to stop pushing at this batch, or None while it is still converging.

    Read this before `caps.pushes`, which is only a runaway ceiling. Pushes that
    keep leaving CI red in the same place are evidence the diagnosis is wrong;
    pushes that keep resolving things are just a PR being reviewed properly.
    """
    ceiling = caps.get("futile_pushes", DEFAULT_FUTILE_PUSH_CEILING)
    run = (batch.get("attempts") or {}).get("futile_pushes", 0)
    if ceiling and run >= ceiling:
        return (
            f"{run} consecutive pushes left CI failing the same way; "
            "the diagnosis is wrong, and another push will not find it"
        )
    return None


def stalled_build(batch: dict, caps: dict[str, int]) -> str | None:
    """Reason to stop resuming this build, or None while resuming is still worth it.

    Resuming an interrupted build is the right behaviour — a worktree that
    already exists beats cutting a new one, and abandoning work in place is the
    failure the durable ledger exists to prevent. But `building` is the one live
    state whose action re-enters the state it is already in: `next_action`
    answers `build`, the recipe re-enters `building`, and the fold records no
    progress for the self-loop by design. So none of the counting governors can
    reach it — no counter in `COUNTER_ORDER` moves, and `futile_push_run` needs
    a push. The resume count is the only quantity that actually grows, so it is
    the one that has to be bounded (issue #62).

    It was not the only live state `loop.next_action` left without an action,
    though the first draft of this said so. `merging` had no branch there at
    all — no action, no staleness check, no governor — while still holding a
    slot against `max_open_prs`. That one the clock can catch, because a batch
    in the merge queue is waiting on something outside the loop, and
    `loop._stale_reason` now reads it like any other wait. A resume is not a
    wait, which is why this counter has to exist instead.

    Read only while the batch is still in `building`: a build that was picked
    up four times and then reached `built` converged, and its old resumes are
    history rather than a verdict.
    """
    if batch.get("state") != "building":
        return None
    ceiling = caps.get("build_resumes", DEFAULT_BUILD_RESUME_CEILING)
    resumes = (batch.get("attempts") or {}).get("build_resumes", 0)
    if ceiling and resumes >= ceiling:
        return (
            f"the build has been resumed {resumes} times without ever reaching `built`; "
            "something in it needs a person, not another session"
        )
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
        raise LedgerError(
            f"{which} gate cannot be {value!r}; expected one of {sorted(GATES[which])}"
        )
    return append(root, "gate.set", batch=batch_id, gate=which, value=value)


# --- CLI ----------------------------------------------------------------------


def _resolve(root_arg: str | None) -> Path:
    return resolve_root(root_arg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", help=f"ledger directory (default {LEDGER_DIR} in the repository root)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p = sub.add_parser("append")
    p.add_argument("--type", required=True)
    p.add_argument("--json", default="{}")
    p = sub.add_parser("transition")
    p.add_argument("batch")
    p.add_argument("state")
    p = sub.add_parser("gate")
    p.add_argument("batch")
    p.add_argument("which", choices=sorted(GATES))
    p.add_argument("value")
    p = sub.add_parser("state")
    p.add_argument("--batch")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            print(init(Path(args.root) if args.root else repo_root()))
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
            payload = (
                st.batches.get(args.batch)
                if args.batch
                else {
                    "issues": st.issues,
                    "batches": st.batches,
                    "escalations": st.escalations,
                    "flakes": st.flakes,
                }
            )
            print(json.dumps(payload, indent=2))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
