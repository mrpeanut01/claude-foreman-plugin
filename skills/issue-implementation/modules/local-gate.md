# The Local Gate

The rule: **never spend CI to learn what a laptop already knows.**

On a repo with a 40-minute suite, a failure caught locally costs seconds and a
failure caught in CI costs a suite run plus a round trip. Four batches a day
versus one is decided almost entirely here.

## It is a command, not a checklist

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" run
```

This page used to be a table — impacted tests, lint, types, format — with the
advice to take the real commands from the repo. That advice was correct and it
did not work. On round 7 the operator ran `ruff check --fix` and never ran `ruff
format --check`; CI runs both, and went red on one unformatted file. Elsewhere
the lint step was attempted as `python3 -m ruff`, which on that machine could not
import, produced no findings, and was read as nothing to report.

Both failures share a shape: **a check that was never run looks exactly like a
check that passed.** A checklist cannot close that gap, because running one is an
act of remembering. A command can, because it either exits zero or says what it
did not do.

## What it runs, and where the commands come from

1. **The tests covering the diff**, from `ci_profile.py impact`. First, because a
   linter finding nothing in broken code is a slow way to learn nothing.
2. **Every cheap-tier CI job's `run:` steps**, verbatim, in order.

Verbatim matters. The commands in the workflow are the commands CI will run, so a
locally-invented equivalent produces a gate that only agrees with itself. The one
exception is the alias `python` → `python3`, because `setup-python` puts a bare
`python` on every runner while most laptops have only `python3`; the report says
so whenever it applied.

## The four things it can tell you

| Exit | Status | Meaning |
|------|--------|---------|
| 0 | `green` | every check ran here and passed |
| 1 | `failed` | a check failed; the report carries its command, exit code and output |
| 2 | `blocked` | the gate could not finish, and will not guess |
| 0 | `waived` | you passed `--allow-unrunnable`; it lists every check that did not run |

`blocked` is the design decision worth understanding. When a tool genuinely is
not installed, the gate could pass — the bug this fixes — or hard-fail, which is
useless on a machine that will never have that tool. It does neither. It exits 2
with a status that is deliberately not `failed`, because "your machine lacks
ruff" and "your code fails ruff" call for completely different actions. If it is
a tool you should have, install it. If it is one you genuinely cannot have,
`--allow-unrunnable` exits 0 and prints every check it let through unrun: the
decision to let CI be the first thing to run that check gets made out loud, by a
person, once, instead of by silence every time.

## What it refuses to do

| Refuses to | Because |
|------------|---------|
| Run install steps (`pip install`, `npm ci`, `apt-get`) | A gate may not rewrite the machine it is grading. Skipping them cannot hide a red — they verify nothing — and any tool they would have installed surfaces as a missing tool. |
| Run jobs a pull request does not trigger | A cheap push-only job can be a deploy. Running one on a laptop is not a gate, it is an accident. |
| Run the expensive tier | That is what CI's parallelism is for. The report names what it deferred. |
| Evaluate `if:` conditions or `${{ }}` expressions | Only Actions has that context. Guessing produces a local pass that predicts nothing. |
| Report green when no test ran over changed code | An opinion is not a gate result. A documentation-only diff is the one exception, and the impact map decides that, not you. |
| Treat a diff it could not read as an empty one | The two are opposite claims and they used to be the same value. An empty diff maps to no test, no test is then required, and the gate exits 0 having run nothing — which is `blocked` in disguise. |

Every one of these is named in the report. Silence is the single result this gate
may never produce.

## When the trunk is not `main`

`--base` defaults to `main`, so on a `master` or `develop` repo — or in a shallow
clone that does not have the base ref — the first `gate.py run` exits 2 with
`blocked` and says the diff could not be read. That is the gate working. Pass the
real trunk:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" run --base master
```

`--changed <file>...` is the other way out, and the only one that skips git
entirely: use it when git cannot answer at all and you can name the files.

## Reading the impact map

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed src/upload.py
{"tests": ["tests/test_upload.py"], "complete": true, "recommendation": "run listed tests"}
```

The gate calls this for you; run it by hand only to understand a decision.
`complete: false` means at least one changed file mapped to nothing, and the gate
then widens to the whole suite instead of narrowing. This is not a heuristic to
tune — it is the one place where being wrong is silent, because a narrowed green
suite looks exactly like a real one.

Never narrow by hand past what the map allows. An unmapped file is exactly where
an unguarded regression hides.

## The other flags, and when they earn their keep

| Flag | Use |
|------|-----|
| `plan` instead of `run` | Show what would run, without running any of it. Read this before arguing with the gate. |
| `--keep-going` | Report every failure in one pass instead of stopping at the first red. |
| `--test-command` | Name the runner when the repo is not Python. The gate infers pytest and nothing else, on purpose: choosing between jest and vitest by vibe produces failures that have nothing to do with your change, and a gate that cries wolf gets waived. |
| `--base` | The branch the diff is measured against. Defaults to `main`. |

## When the impacted set is slower than CI's cheap tier

Push and let CI run it. CI has parallelism a laptop does not. The rule is about
not paying the **expensive** tier to find cheap bugs, not about refusing CI.

Compare against `cheap_tier_s` in the profile, not against your patience.

## Improving the map

If workflows carry `paths:` filters, they encode a mapping the team already
agreed on — prefer those over convention. If the repo produces coverage contexts
(`coverage.py --contexts`, `nyc`, Jest `--coverage`), a measured file→test index
beats any convention.

Do not build a static import graph. It goes stale silently, and a silently stale
impact map produces confident wrong narrowing — the exact failure this gate exists
to prevent.
