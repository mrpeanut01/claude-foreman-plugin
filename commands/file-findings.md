---
description: Turn review findings into GitHub issues so the loop can pick them up again
usage: /foreman:file-findings <batch-id> [--verdict PATH] [--apply]
allowed-tools: Bash(python3:*), Bash(gh:*), Read, Write
---

# File Findings

Without this the loop leaks. A reviewer's findings live in a verdict JSON and a PR
comment: the blocking ones get fixed in that PR, and everything else evaporates.
**An issue is the only artefact triage reads**, so a finding that never becomes one
can never be worked.

## Plan

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/findings.py" plan \
  --verdict /tmp/verdict.json --repo OWNER/NAME --batch b-001 --pr 7 --round 2 \
  > /tmp/findings-plan.json
```

Returns three lists:

| List | Means |
|------|-------|
| `file` | New findings, ordered worst severity first |
| `skipped` | Already on the tracker, with `duplicate_of` — or, when the twin is another finding in this same run, a null `duplicate_of` and a `duplicate_of_title` |
| `unusable` | No summary to file. Reported, never silently dropped |

## Apply

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/findings.py" file \
  --plan /tmp/findings-plan.json --repo OWNER/NAME
```

Creates each issue through `gh_safe.sh` and appends a `finding.filed` event, so
the ledger records which review produced which issue.

`already_tracked` holds issue numbers and nothing else, so they can be followed.
Findings suppressed by a sibling finding in the same run were never filed and have
no number; they are listed by title under `duplicate_within_run`.

## File every severity, not just blocking ones

High and medium findings block the merge and get fixed in the current PR. **Low
findings are exactly the ones that need an issue** — nothing else will ever make
them happen.

## On duplicate detection

Titles are generated from the reviewer's summary and compared against the tracker
by token overlap. A human-written issue title for the same defect is often worded
differently enough to slip past, so expect it to occasionally file something
already tracked.

That is the direction to fail in. A duplicate issue is caught by `/foreman:triage`,
which marks it `duplicate` and drops it from the queue. A finding that was never
filed is simply gone.
