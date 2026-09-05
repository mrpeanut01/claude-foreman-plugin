---
description: Group triaged issues into batches sized by what CI actually costs
usage: /foreman:batch [--apply]
allowed-tools: Bash(python3:*), Read, Write
---

# Batch

Turns actionable issues into batches, each destined for one PR and one suite run.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" plan --triage /tmp/foreman-triage.json
```

Prints the batches plus the savings they buy. Read `Skill(foreman:work-batching)`
for the grouping rules and the arithmetic behind them.

## Apply

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" apply --plan /tmp/foreman-batches.json
```

Appends one `batch.created` event per batch. Nothing is built yet.

## Reading the savings block

```json
{"issues": 7, "batches": 3, "suite_runs_saved": 4, "seconds_saved": 9600}
```

`suite_runs_saved` is `issues − batches`: the runs you did not have to pay for.
`seconds_saved` is `null` when the repo has no CI profile — the saving is real,
but unquantified, and the tool says so rather than inventing a number.

## Why a batch may be smaller than you expect

| Reason | Fix |
|--------|-----|
| `unknown paths: independence cannot be established` | The issues name no files. Add path hints to the issue, or accept solo batches. |
| `both touch src/x.py` | Correct behaviour — conflicts inside a batch are self-inflicted. |
| `risk high exceeds the batching ceiling` | High-risk issues always run alone. |
| Weight cap | `max_batch_weight` in config; small=1, medium=2, large=4. |

Absent path information is not evidence of independence, so the grouper refuses
to batch on a guess. On a repo where issues rarely name files, expect mostly
solo batches until issue templates improve.
