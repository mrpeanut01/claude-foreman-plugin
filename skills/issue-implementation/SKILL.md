---
name: issue-implementation
description: Implement a batch of issues in an isolated worktree, test-first, one commit per issue, behind a local gate that runs the impacted tests before any CI is spent. Use when building a foreman batch or deciding what to verify locally before pushing.
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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact \
  --changed $(git diff --name-only main...HEAD)
```

| Result | Do |
|--------|----|
| `"complete": true` | Run the listed tests |
| `"complete": false` | **Run the full suite locally** |

Then lint and typecheck. Push only when every part is green. See
[modules/local-gate.md](modules/local-gate.md).

Never override a `"complete": false`. An unmapped file is exactly where an
unguarded regression hides, and narrowing on a partial map is how a green local
gate ships a break.

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
local gate ran and was green. The batch is in `built`. Nothing was pushed.
