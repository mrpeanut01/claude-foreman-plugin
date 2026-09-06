---
name: issue-triage
description: Classify, size, risk-score and dedupe GitHub issues into the foreman ledger, applying only labels the repo already defines. Use when triaging an issue queue, deciding whether an issue is actionable, judging duplicates, or scoring risk before work is planned.
---

# Issue Triage

Turn an issue queue into a work queue. This stage writes labels and ledger
records — never code, never comments, never closures.

`scripts/triage.py` holds the deterministic scoring; this skill holds the
judgement. Run the script, then read the issues it scored and correct what it got
wrong. It is blunt on purpose, and blunt-but-consistent beats clever-but-drifting
when the same queue is triaged every day.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py" plan --repo OWNER/NAME --limit 50
```

## Three rules that override everything

**1. Labels come only from the repo's own vocabulary.** `plan_labels` intersects
with `gh label list`. Never invent one, never suggest creating one mid-run.

**2. A false lifecycle label is worse than a missing one.** `needs-repro` costs a
reporter a round trip and reads as dismissal. Apply it only when a bug has no
evidence *and* no stated expectation.

**3. When in doubt, `actionable`.** A wrongly-queued issue gets caught at build
time. A wrongly-parked one sits there until someone notices, which may be never.

## Verdicts

| Verdict | When | Consequence |
|---------|------|-------------|
| `actionable` | Someone could start now | Enters batching |
| `needs-repro` | Bug, no evidence, no stated expectation | Parked on the reporter |
| `needs-info` | Missing environment or version detail | Parked on the reporter |
| `duplicate` | Title overlap ≥ 0.6 with an **open** issue, on two or more words | Linked, not queued |

## What counts as evidence

Any of these makes a bug actionable: a traceback; `File "x.py", line N`; a file
path; a backticked command; numbered steps; an all-caps error code; an HTTP
status. So does an expectation stated under a condition — *"when the file is empty
it suggests deleting it, which should never happen"* is a perfectly good bug
report and must not be asked for steps.

Never mark a duplicate against a closed issue. Pointing someone at a closed
thread as though it answers them is worse than saying nothing.

One shared word is never a duplicate, however high the ratio. "Bug 1" and
"Bug 2" overlap completely once the digit is discounted, and a duplicate
verdict at 1.0 parks the newer issue until someone reads both.

## Risk drives everything downstream

`high` risk means never batched and never auto-merged. It is set by a protected
path, a security-ish keyword, or a `security`/`critical`/`data-loss` label.

Rounding risk **up** is cheap: the issue still gets fixed, just in a solo PR.
Rounding it down puts an auth change into a five-issue batch that merges itself.

See [modules/taxonomy.md](modules/taxonomy.md) for the size and risk vocabularies
and how to extend them.

## Exit criteria

Every open issue either has a ledger record or appears in `skipped`. The plan was
shown before anything was applied. No label outside `gh label list` was used.
