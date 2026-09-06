---
description: Group triaged issues into batches sized by what CI actually costs
usage: /foreman:batch [--apply]
allowed-tools: Bash(python3:*), Read, Write
---

# Batch

Turns actionable issues into batches, each destined for one PR and one suite run.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" plan > /tmp/foreman-batches.json
```

Plans from the ledger: every issue triage recorded as `actionable` that no batch
yet holds — the same set `loop.py next` names when it answers `batch`. That is
the only source that survives a new session. `--triage /tmp/foreman-triage.json`
plans from a triage file instead, for reading a plan straight after `/foreman:triage`;
issues a batch already holds are left out either way, because a second batch
for work already in flight is the runaway the loop guards against.

Prints the batches plus the savings they buy. Read `Skill(foreman:work-batching)`
for the grouping rules and the arithmetic behind them.

## Apply

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" apply --plan /tmp/foreman-batches.json
```

Appends one `batch.created` event per batch. Nothing is built yet. The plan
file is the one the first command wrote — it used to print to the terminal and
this step then read a file nothing had written.

## Reading the savings block

```json
{"issues": 7, "batches": 3, "suite_runs_saved": 4, "seconds_saved": 9600}
```

`suite_runs_saved` is `issues − batches`: the runs you did not have to pay for.
`seconds_saved` is `null` when the repo has no CI profile — the saving is real,
but unquantified, and the tool says so rather than inventing a number.

## Why a batch may be smaller than you expect

Every batch in the plan carries `started_because`: the first reason its first
issue could not join the batch before it, or `null` for the first batch. Read
that field rather than guessing.

| `started_because` | Fix |
|--------|-----|
| `unknown paths: independence cannot be established` | The issues name no files. Add path hints to the issue, or accept solo batches. |
| `both touch src/x.py` | Correct behaviour — conflicts inside a batch are self-inflicted. |
| `risk high exceeds the batching ceiling` | High-risk issues always run alone. |
| Weight cap | `max_batch_weight` in config; small=1, medium=2, large=4. |

Absent path information is not evidence of independence, so the grouper refuses
to batch on a guess. On a repo where issues rarely name files, expect mostly
solo batches until issue templates improve.
