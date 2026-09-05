---
description: Run the whole loop unattended — triage, batch, build, land, merge
usage: /foreman:run [--max-actions N] [--dry-run]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Task
---

# Run

The master loop. Read `Skill(foreman:orchestration)` for the rules it obeys.

## How it works

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop.py" next
```

Returns exactly one action. Take it, then ask again — never plan several steps
ahead, because the ledger may have moved (CI finished, a reviewer replied, another
session merged something).

| `do` | Then |
|------|------|
| `triage` | `/foreman:triage` |
| `batch` | `/foreman:batch` |
| `build` | `/foreman:build <batch>` |
| `open_pr` / `watch` / `advance` / `merge` | `/foreman:land <batch>` |
| `unblock` | Fix the red gate, push; gates reset |
| `escalate` | Record it, comment on the PR, label `needs-human`, **move on** |
| `idle` | Report and stop |

## Priorities, and why

Finish before starting. An in-flight batch holds a PR open and re-runs CI on
every trunk move, so draining one is worth more than beginning another. Caps are
checked before anything else — a batch past its cap must not be picked up again
by a loop that has forgotten why it failed.

## Stopping

The loop stops on `idle`, on `--max-actions`, or when the CI budget for the day
is spent. It does **not** stop on an escalation: that batch is set aside and the
next one starts. Stalling every unrelated piece of work on one bad issue is the
failure mode this design exists to avoid.

## Unattended overnight

Pair with `/loop`:

```
/loop 15m /foreman:run --max-actions 3
```

Before you do, check three things in `.foreman/config.json`:

| Setting | Why it matters overnight |
|---------|--------------------------|
| `auto_merge` | `false` means it will build and review but never merge. Start here. |
| `limits.max_ci_minutes_per_day` | The only thing standing between a stuck loop and your CI bill. |
| `protected_paths` | Anything listed here escalates instead of merging, however green. |

Then read `/foreman:status` in the morning. `NEEDS YOU` is the whole report.
