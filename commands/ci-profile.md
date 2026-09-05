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

Read-only against GitHub: `gh run list`, the jobs API, and branch protection.

## What it records

| Field | Why the loop needs it |
|-------|----------------------|
| `jobs[].needs` / `triggers` / `path_filters` | The job graph — what can run without what, and which paths even wake a job. |
| `jobs[].p50` / `p95` / `samples` | Real cost. `samples: 0` means unmeasured; treat that job as expensive until proven otherwise. |
| `jobs[].tier` | `cheap` (p95 ≤ threshold, run on every push) or `expensive` (waits behind the cheap tier *and* the review gate). |
| `jobs[].required` | Only required checks can block a merge. Waiting on an advisory check is wasted wall clock. |
| `jobs[].flake_rate` | Fraction of commits where this job both failed and passed. Drives rerun-vs-fix. |
| `cheap_tier_s` / `expensive_tier_s` | The two numbers that make batching arithmetic possible. |

## Mapping a diff to tests

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed src/foo.py src/bar.py
```

Returns `{"tests": [...], "complete": true|false}`.

**`complete: false` means run the full suite.** A partial map is not a licence to
narrow — an unmapped file is exactly where an unguarded regression hides. The
loop treats this as non-negotiable.

## When to refresh

Re-probe after workflow changes, after adding a job, or every ~2 weeks of active
development. A profile older than its evidence is worse than none, because the
loop trusts it.
