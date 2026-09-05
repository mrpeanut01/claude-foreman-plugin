---
description: Show what the foreman loop is doing, what is stuck, and what needs a human
usage: /foreman:status [--json]
allowed-tools: Bash(python3:*), Read
---

# Foreman Status

Render the ledger digest for this repo.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/status.py" --root .foreman
```

Add `--json` to get raw state instead of the digest — use that when you need to
act on a batch rather than read about it.

## Reading the output

| Section | Means |
|---------|-------|
| **IN FLIGHT** | Batches between `planned` and `merging`, with both gates shown. `cheap gates clear; full suite may run` marks a batch that has earned the expensive tier. |
| **NEEDS YOU** | Escalations and cap breaches. The loop has stopped working these and will not retry. Everything else keeps moving. |
| **FLAKES** | Tests seen failing then passing on one commit, worst first. A test near the top of this list should be quarantined, not rerun. |
| **REVIEW QUALITY** | Clean reviews whose merges were later reverted. Above 10% means the review gate is approving too easily — treat it as a defect in the reviewer, not noise. |

## When there is no ledger yet

`.foreman/` is created on first use:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" init
```

Add `.foreman/` to `.gitignore` — it is per-clone working state, not source.
