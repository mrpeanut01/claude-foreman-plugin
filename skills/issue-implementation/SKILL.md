---
name: issue-implementation
description: Implement a batch of issues in an isolated worktree, test-first, one commit per issue, behind a single local gate command that runs the impacted tests and the whole cheap CI tier before any CI is spent. Use when building a foreman batch or deciding what to verify locally before pushing.
---

# Issue Implementation

Build one batch. Prove it locally. Only then spend CI.

## Setup

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> building
git worktree add ../foreman-<batch> -b foreman/<batch>
```

A worktree, not a branch switch: the loop may be watching CI for another batch,
and stepping on a shared working directory mid-run is how two batches end up in
one PR.

## Per issue, in issue order

**Red.** Write a test that fails for the reason the issue describes. Run it.
Watch it fail, and read the failure — if it fails for a different reason than the
issue reports, you have not reproduced the bug yet.

**Green.** Make the smallest change that turns it green. Root cause, not symptom:
if the fix is a conditional guarding the exact line from the traceback, that is
usually the symptom.

**Commit.** One commit per issue, referencing its number.

One commit per issue is load-bearing. `batch.split()` peels a failing commit off
so the rest of the batch proceeds — and the entire economic case for batching
assumes that split is cheap.

## The local gate

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" run
```

One command. It maps the diff to its tests and runs those, then runs every
cheap-tier CI step — lint, format, types — using CI's own commands.

| Exit | Means |
|------|-------|
| 0 | every check ran here and passed; push |
| 1 | a check failed, and the report names it |
| 2 | the gate could not finish, and the report names what it could not run |

This used to be a table of steps to work through by hand, and the later steps got
skipped: running a checklist is an act of remembering, and remembering fails
silently. Do not reconstruct the checklist — run the command and read the exit
code. See [modules/local-gate.md](modules/local-gate.md).

Never work around exit 2 by re-running the parts that pass. An unrunnable check
is an unknown, and a green assembled out of unknowns is the exact result this
gate exists to prevent.

## Finishing

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> built
```

Stop. Opening the PR is `/foreman:land`.

## When an issue resists

Drop it from the batch rather than forcing it: append `batch.meta` with the
reduced issue list, escalate the dropped issue with a reason, keep building the
rest. Half-solving an issue to keep a batch intact produces a PR that looks
finished and is not — the most expensive outcome available here.

## Exit criteria

Every issue in the batch has a failing-then-passing test and its own commit. The
local gate exited zero — green, not waived. The batch is in `built`. Nothing
was pushed.
