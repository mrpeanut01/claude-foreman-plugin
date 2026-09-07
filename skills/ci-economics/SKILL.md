---
name: ci-economics
description: Spend CI deliberately on repos with slow test suites — measure job costs, map a diff to the tests that cover it, batch issues to amortize a monolithic suite, run an escalating tier ladder, launch benchmarks and simulations only on a diff that can move them, and tell flakes from real failures. Use when deciding what to run locally, how large a batch should be, whether a benchmark is worth running on this change, whether to rerun a failed job, or why CI feels expensive.
---

# CI Economics

Most of what an autonomous dev loop wastes is CI, and it wastes it in one way:
**paying a slow remote suite to discover something a laptop already knew.**
Everything here follows from that.

Read the profile first — `.foreman/ci-profile.json`, built by `/foreman:ci-profile`.
Without it every rule below degrades to guessing.

## The six tactics, in order of money saved

### 1. Local gate — never spend CI to learn what a laptop knows

Before any push: map the diff to its tests, run those plus lint and typecheck
locally, and push only when green.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed $(git diff --name-only origin/main...HEAD)
```

If it returns `"complete": false`, **run the full suite locally**. See
[impact-analysis.md](modules/impact-analysis.md) — the incomplete case is the one
that matters and the one everyone gets wrong.

### 2. Batch to amortize a monolithic suite

A 40-minute suite costs 40 minutes whether the PR fixes one issue or five. So
group compatible issues into one PR.

```
separate PRs:  k × T
one batch:     T          →  saves (k−1) × T when it passes
on failure:    T + T      →  split the failing commit out, rerun both
```

Break-even is roughly `p_fail < (k−1)/k`. At k=3 a batch pays for itself unless
more than two thirds of batches fail — which is why **one commit per issue**
matters: it makes the split cheap and keeps the arithmetic favourable.

Group only when all hold:

| Rule | Reason |
|------|--------|
| No two issues edit the same file | Conflicts inside a batch are self-inflicted |
| All triaged `low` or `medium` risk | One `high` issue contaminates the whole batch's blast radius |
| Same test area where possible | Keeps the local gate narrow |
| No `protected_paths` touched | Those never auto-merge, so batching them buys nothing |
| ≤ `max_batch_issues` (default 5) | Bisecting a failure gets worse faster than the savings grow |

### 3. The tier ladder — earn the expensive suite

```
push ─▶ local gate ─┬─▶ cheap CI (lint, typecheck, unit)  ─┐
                    │                                       ├─▶ both clean ─▶ expensive tier ─▶ merge queue
                    └─▶ independent review (agent)         ─┘
```

Review and cheap CI run **concurrently**; the expensive tier waits for both.
Benchmarks sit outside this ladder entirely — they are measurements, not gates,
and tactic 4 decides whether they run at all.
A review costs minutes of tokens; the suite costs 40 minutes of compute. Putting
the cheap judge first is the whole trick. `ledger.may_run_expensive_tier(batch)`
is the gate.

### 4. Launch a benchmark only on a diff that can move it

A test returns a verdict; a benchmark returns a number. A number cannot say the
diff is wrong, so waiting for one holds a correctness fix behind an answer to a
different question — and a number only means anything when the diff could have
moved it.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" benchmark-plan \
  --changed $(git diff --name-only origin/main...HEAD) --batch <id>
```

`run` lists only jobs something *said* to run: a benchmark is launched when a
`paths:` filter or a `benchmark_paths` entry selects the diff, never because
nothing said it should not. Anything undeclared comes back `unknown` and is
launched on nothing — reported as a gap to declare, because both guesses are
wrong invisibly. See [benchmark-runs.md](modules/benchmark-runs.md); the rule
that a name-matched guess may never loosen a merge gate lives there.

A warranted run goes in **its own PR**, not this batch's. The batch is a
correctness fix; serialising a 40-minute measurement in front of it costs the
whole saving the ladder exists to make, and the measurement cannot veto the fix.

### 5. Tell flakes from real failures before reacting

A flake is **one commit where the same job both failed and passed**. Failing every
time is a real failure. Classify with a confidence score and act on the number,
not the vibe — see [flake-budget.md](modules/flake-budget.md).

### 6. Merge queue, once, at the end

The full suite runs against real trunk exactly once per batch, in the queue, with
`gh pr merge --auto --squash`. Cap concurrent open PRs (default 3): every trunk
move re-runs CI on all of them, so in-flight PRs cost superlinearly.

## Budget

`.foreman/config.json` holds `max_ci_minutes_per_day`. Track spend by summing p95
of the jobs launched. When the budget is gone, the loop stops launching CI and
escalates rather than queueing work it cannot pay for.

## Modules

| Read when | Module |
|-----------|--------|
| Building or refreshing the profile | [profiling.md](modules/profiling.md) |
| Deciding what to run locally | [impact-analysis.md](modules/impact-analysis.md) |
| A job failed and you must rerun or fix | [flake-budget.md](modules/flake-budget.md) |
| A repo has benchmarks, simulations or soaks | [benchmark-runs.md](modules/benchmark-runs.md) |
