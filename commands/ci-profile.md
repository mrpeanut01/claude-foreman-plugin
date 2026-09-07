---
description: Measure what this repo's CI actually costs, so the loop can stop overspending it
usage: /foreman:ci-profile [--repo OWNER/NAME] [--runs 50] [--refresh]
allowed-tools: Bash(python3:*), Bash(gh:*), Read, Write
---

# CI Profile

Build `.foreman/ci-profile.json` from **measured** run history, not from what the
workflow files claim. Every later decision — what to run locally, how big a batch
should be, when to spend the slow suite — reads this file.

## Run it

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" probe --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" --runs 50
```

Read-only against GitHub: the run list, the jobs API, branch protection, and
branch rules. Protection is read from **both** places GitHub keeps it — classic
`branches/{b}/protection` and `rules/branches/{b}` for rulesets — and unioned,
since GitHub enforces both and a check required by either blocks the merge.

## What it records

| Field | Why the loop needs it |
|-------|----------------------|
| `jobs[].needs` / `triggers` / `path_filters` | The job graph — what can run without what, and which paths even wake a job. |
| `jobs[].p50` / `p95` / `samples` | Real cost. `samples: 0` means unmeasured; treat that job as expensive until proven otherwise. |
| `jobs[].tier` | `cheap` (p95 ≤ threshold, run on every push) or `expensive` (waits behind the cheap tier *and* the review gate). |
| `jobs[].kind` / `kind_source` | `benchmark` when the job returns a measurement rather than a verdict, and whether that came from `benchmark_jobs` in config (`config`, acted on everywhere) or from the job's own name (`name`, a guess that may never loosen a merge gate). |
| `jobs[].required` | Only required checks can block a merge. Waiting on an advisory check is wasted wall clock. |
| `jobs[].flake_rate` | Fraction of commits where this job both failed and passed. Drives rerun-vs-fix. |
| `cheap_tier_s` / `expensive_tier_s` | The two numbers that make batching arithmetic possible. |
| `protection_known` / `protection_sources` | Whether the merge gate could be read, and what each of GitHub's two mechanisms said. Classic branch protection and rulesets are both read and unioned; `false` means **unknown**, never "nothing is required". |
| `required_approvals` | Approving reviews the merge needs. **Non-zero means the loop cannot merge unattended** — the review gate is a ledger fact, not a GitHub approval, and GitHub refuses an approval from the PR's own author. |
| `benchmark_jobs` / `benchmark_s` | Which jobs measure rather than judge, and what a full sweep of them costs. Counted beside the tiers rather than carved out of them: a benchmark is usually expensive, but a 40-second smoke simulation is cheap and still not a verdict. |

## Mapping a diff to tests

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed src/foo.py src/bar.py
```

Returns `{"tests": [...], "complete": true|false}`.

**`complete: false` means run the full suite.** A partial map is not a licence to
narrow — an unmapped file is exactly where an unguarded regression hides. The
loop treats this as non-negotiable.

## Deciding whether a benchmark is worth running

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" benchmark-plan \
  --changed src/foo.py --batch b-001
```

Returns `{"run": [...], "skip": [...], "unknown": [...], "seconds_not_spent": N,
"findings": [...]}`.

**`run` is the only field that spends anything**, and it lists only jobs a
`paths:` filter or a `benchmark_paths` entry actually selected. A benchmark
nothing declares comes back in `unknown` and is launched on no diff at all —
reported as a gap to declare, never guessed at in either direction. The
`findings` are shaped for `findings.py`, which is how a warranted run becomes a
pull request of its own instead of sitting in the current batch's merge path.
`seconds_not_spent` is the p95 wall clock this decision did not launch — the
saving, in the same units the tier totals use.

Declare both keys in `.foreman/config.json`; see `benchmark-runs.md` in the
`ci-economics` skill.

## When to refresh

Re-probe after workflow changes, after adding a job, or every ~2 weeks of active
development. A profile older than its evidence is worse than none, because the
loop trusts it.

`--refresh` is how you say so. Without it, an existing `.foreman/ci-profile.json`
younger than two weeks and newer than every file in `.github/workflows/` is left
alone, and the command reports its age instead of spending fifty API calls to
learn what it already knows. With it, the probe runs regardless.
