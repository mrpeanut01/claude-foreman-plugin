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
| `building` | Implementing, TDD, local gate | Local gate is green |
| `built` | Committed locally, not pushed | The PR opens |
| `open` | PR up; CI and review running **concurrently** | A gate resolves |
| `blocked` | A gate came back red | A new push resets the gates |
| `ready` | Both gates clear | Merge is requested |
| `merging` | In the merge queue | GitHub confirms |
| `merged` | Terminal | — |
| `escalated` | Set aside for a human | A human requeues or abandons it |
| `abandoned` | Terminal | — |

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
