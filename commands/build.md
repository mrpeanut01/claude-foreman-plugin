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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" run
```

One command, and it is the whole gate: the tests covering the diff first, then
every cheap-tier CI step, in CI's own words.

| Exit | Means | Do |
|------|-------|----|
| 0 | all of it ran and passed | push |
| 1 | a check failed | it names the command and its output — fix that |
| 2 | the gate could not finish | it names what it could not run — install that, or re-run with `--allow-unrunnable` and accept that CI runs the check first |

Exit 2 is why this is a command rather than a list. A check that never ran looks
exactly like a check that passed, so the gate refuses to call a missing tool
green. The last time this section was a list of steps to remember, `ruff format
--check` was the step forgotten, and CI paid for it.

Do not substitute your own commands, and do not run "the parts that matter".
Every failure caught here costs seconds; the same failure caught in CI costs a
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
