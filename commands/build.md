---
description: Implement one batch in its own worktree, test-first, behind a local gate
usage: /foreman:build <batch-id>
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Build

Implements every issue in one batch, then proves it locally before spending any CI.

Read `Skill(foreman:issue-implementation)` — it carries the worktree setup, the
Red→Green loop, and the local gate.

## Shape of the work

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> building
git worktree add ../foreman-<batch> -b foreman/<batch>
```

Then, **per issue, in issue order**:

1. Write a failing test that reproduces the issue. Run it. Watch it fail.
2. Make the minimal change that turns it green.
3. Commit — **one commit per issue**, message referencing the issue number.

One commit per issue is not tidiness. It is what makes `batch.split()` cheap when
one issue in a batch turns out to be wrong, and the whole economic case for
batching rests on that split being cheap.

## The local gate

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact \
  --changed $(git diff --name-only main...HEAD)
```

`"complete": true` → run the listed tests. `"complete": false` → **run the full
suite locally.** Then lint and typecheck. Push only when all of it is green.

Every failure caught here costs seconds. The same failure caught in CI costs a
suite run and a round trip, and on a repo with a 40-minute suite that is the
difference between four batches a day and one.

## Finishing

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> built
```

Stop there. Opening the PR is `/foreman:land`.

## When an issue turns out to be wrong

Do not force it. Drop that issue from the batch, append a `batch.meta` event with
the reduced issue list, and escalate the dropped one with a reason. A batch that
half-solves something is worse than a batch that solves less.
