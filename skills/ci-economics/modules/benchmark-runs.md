# Benchmarks and Simulations

A test returns a **verdict**: this code is correct, or it is not. A benchmark or
a simulation returns a **measurement**: this took 4.2s, this converged in 31
iterations.

That difference decides everything below, and it is the one the tier ladder does
not capture. `tier` says what a job costs. It says nothing about what its result
means, and for measurements the meaning is what makes waiting for them pointless:

- **A number cannot say the diff is wrong.** So a merge that waits for one is
  holding a correctness fix behind an answer to a different question.
- **A number only means something when the diff could have moved it.** Run a
  benchmark on a README change and you have paid the full wall clock to
  reproduce noise, then compared that noise against yesterday's.

So `kind` is a second axis in the profile, orthogonal to `tier`.

| | `kind: test` | `kind: benchmark` |
|---|---|---|
| Runs in the local gate | Yes, if cheap | **Never** — a laptop's numbers cannot join CI's series |
| Blocks the merge | Yes, if required | **Only** if branch protection requires it |
| Launched on | Every push | Only a diff that can move it |
| A red result means | Fix it | File it. It is information, not a refusal |

## How a job is recognised

```json
"benchmark_jobs": ["bench", "*simulation*", "load-test"]
```

Two sources, and the profile records which answered, because they carry very
different weight:

| `kind_source` | Means | What acts on it |
|---------------|-------|-----------------|
| `config` | A human wrote `benchmark_jobs`. A fact about this repo. | Everything, including the merge gate |
| `name` | The job calls itself a benchmark. A guess. | Only what is harmless to get wrong |

**A guess may never loosen a gate.** A job named `perf-regression-test` that
really does gate the merge would be waved through on nothing but its name, so
`land.py` acts only on `config`. `gate.py` acts on both: the worst a wrong guess
does there is leave a correctness job to CI, which costs money, not correctness.

**Branch protection outranks the config.** A repo that made its benchmark a
required check has said the merge waits for it. `required_checks` is read first.

## Deciding whether this diff warrants a run

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" benchmark-plan \
  --changed $(git diff --name-only origin/main...HEAD) --batch <id>
```

The ladder, in order of who is more likely to be right:

1. **The job's own `paths:` filter.** GitHub already enforces it on every push.
   Deciding differently here puts the loop at odds with what CI actually does.
   Read with GitHub's rules, `!` exclusions and last-match-wins included.
2. **`benchmark_paths` in config** — a map of job to globs, or one flat list
   covering every benchmark. Written to look like a `paths:` filter because that
   is what it stands in for.
3. **A documentation-only diff.** The one thing sayable with no declaration at
   all: prose has no runtime.
4. **Otherwise `unknown`.**

## `unknown` is the answer that matters

Three answers, not two, for the same reason `impacted_tests` returns `complete`:
both guesses are wrong in a way that never surfaces.

| | If guessed | Cost |
|---|---|---|
| `true` | Run it | The every-PR spend this whole decision exists to stop |
| `false` | Skip it | A benchmark quietly stops measuring, and nobody notices for months |

So neither is guessed. `run` lists only jobs something said to run — **a job is
launched when a declaration says it should be, never because nothing said it
should not.** That asymmetry is the saving.

The gap becomes an issue instead: one line of `benchmark_paths` permanently
improves every later decision, and nothing else in the loop will ever ask for it.
All undeclared jobs share **one** issue, because it is one edit to one file —
and because one issue per job produces titles a word apart that `findings.py`
correctly reads as duplicates, leaving the survivor naming a single job.

## Why a warranted run is its own PR

A benchmark this diff *can* move still does not belong in this batch's PR.

The batch is a correctness fix. Putting a 40-minute measurement in front of it
serialises the two, which is the exact cost the tier ladder spends its whole
design avoiding — and the measurement cannot veto the fix anyway, so the wait
buys nothing. The run becomes an issue, triage picks it up, and it lands in a
pull request of its own, running beside the fix rather than in front of it.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" benchmark-plan \
  --changed <paths> --batch <id> > bench.json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/findings.py" plan --verdict bench.json \
  --batch <id> --repo OWNER/NAME --source "the benchmark plan" > bench-plan.json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/findings.py" file --plan bench-plan.json --repo OWNER/NAME
```

`benchmark-plan` emits findings in the shape `findings.py` already files, so
dedupe, labelling and the `finding.filed` record come for free. `--source` is
what stops the issue claiming the independent review raised it, which would send
the next reader to a verdict file that never mentions it.

## What this does not do

It does not launch a workflow. `gh_safe.sh` allows no `workflow` verb, and
deliberately: the loop's answer to "this should be measured" is to file the work,
not to spend CI on its own initiative. Nor does it read the numbers a benchmark
produces — a regression is a finding for a person or a reviewer, and inventing a
threshold here would be one more confident guess in a file that exists to refuse
them.
