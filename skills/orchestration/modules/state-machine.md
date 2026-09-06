# Batch State Machine

A batch is one group of issues heading for one PR.

```
planned ─▶ building ─▶ built ─▶ open ─▶ ready ─▶ merging ─▶ merged
   │           │          │       │  ▲            │
   │           │          │       ▼  │            │
   │           │          │    blocked ◀──────────┘
   └───────────┴──────────┴──────┴──▶ escalated ─▶ planned | abandoned
```

| State | Meaning | Leaves when |
|-------|---------|-------------|
| `planned` | Grouped, not started | A worktree is created |
| `building` | Implementing, TDD, local gate | Local gate is green. Counts as work in flight, and re-entering it resumes an interrupted build |
| `built` | Committed locally, not pushed | The PR opens |
| `open` | PR up; CI and review running **concurrently** | A gate resolves |
| `blocked` | A gate came back red | A new push resets the gates |
| `ready` | Both gates clear | Merge is requested |
| `merging` | In the merge queue | GitHub confirms. The loop `watch`es it and escalates it on `stale_after_s`, because `--auto` never reports back |
| `merged` | Terminal | — |
| `escalated` | Set aside for a human | A human requeues or abandons it |
| `abandoned` | Terminal | — |

## In flight starts at `building`

`building`, `built`, `open`, `blocked`, `ready` and `merging` all hold a
worktree, a branch or a PR, so all six count against `max_open_prs` in
`loop.in_flight_count`. `building` is easy to forget because it owns no PR yet,
but it owns a worktree and a session, and leaving it out lets the loop start
another batch while a build is already running.

Counting a state as in flight and giving it no action in `next_action` is the
other half of the same mistake, and `merging` had exactly that shape: it held a
slot against `max_open_prs` and no branch ever picked it up, so a merge the
queue refused parked the batch there for good with nothing to escalate it. Every
state in the list above answers to some branch of `next_action`.

## Resuming an interrupted build

A build ends when the laptop closes, the context runs out, or the process dies —
and the batch is left in `building` with a worktree on disk. `loop.next_action`
returns `build` for it, ahead of any `planned` batch: resuming a worktree that
already exists beats cutting a new one, and abandoning work in place is the
failure the durable ledger exists to prevent.

`building -> building` is therefore legal. Re-entering the state is how a resume
starts, so `commands/build.md` can open with the same transition either way. The
fold records no movement for it, so a resume cannot reset the staleness clock.

That last property is also why resuming has to be counted. With `progress_at`
standing still and no push to score, a batch parked in `building` is invisible to
every other governor, and `build` would be the answer forever. Each re-entry
increments `attempts.build_resumes`, and `ledger.stalled_build` escalates at
three (`caps.build_resumes` to change it) — see `escalation.md`.

## Gates are not states

CI and review run **at the same time** and are tracked as separate fields:

- `ci_gate`: `pending` → `cheap_green` → `full_green`, or `failed`
- `review_gate`: `pending` → `clean`, or `changes_requested`

`ready` requires `ci_gate == "full_green"` **and** `review_gate == "clean"`.
`cheap_green` is deliberately not enough — attempting it raises `GateNotClear`
naming the offending gate.

`may_run_expensive_tier(batch)` is the other rule: the slow suite runs only once
the cheap tier is green **and** the review is clean. A `changes_requested` verdict
kills the expensive run before it is paid for.

## Recovering a refused move

`IllegalTransition` lists the legal moves from where you are. Two causes, in order
of likelihood:

1. **The ledger is behind reality** — an action succeeded but its event was never
   appended. Reconcile with `gh pr view` and append `batch.meta`.
2. **You skipped a step** — e.g. `built → ready`. Walk the path; do not invent an
   event to jump the gap.

Never append a raw `batch.state` event by hand to force a move. The validation in
`transition` is the only thing standing between the loop and merging something
that was never reviewed.
