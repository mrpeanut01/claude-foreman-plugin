---
name: orchestration
description: The foreman dev loop — a durable, resumable pipeline that triages issues, batches them, builds, reviews independently, and auto-merges. Use when running or resuming unattended issue-to-merge work, reading foreman state, or deciding whether something should escalate to a human.
---

# Foreman Orchestration

The master agent is **a loop over a durable ledger, not a long conversation.**
Everything it knows lives in `.foreman/events.jsonl`. That is what lets the loop
survive a crash, a context limit, or a laptop closing mid-batch: any session can
re-read the ledger and pick up exactly where the last one stopped.

## The loop

```
triage ──▶ batch ──▶ build ──▶ land ──▶ merged
  │          │         │        │
  └──────────┴─────────┴────────┴──▶ escalate  (records and moves on)
```

Each pass: fold the ledger, pick the highest-value legal action, take exactly one
step, append the resulting event. Never hold state in your head between steps —
if it matters, it is an event.

## Iron rules

1. **Escalation never blocks the loop.** An escalated batch is set aside with a
   recorded reason; the loop immediately continues with other batches. A blocking
   prompt would stall every unrelated piece of work.
2. **The ledger is the truth.** If an action succeeded but the event was not
   appended, treat it as not having happened and reconcile against GitHub.
3. **One commit per issue.** A batch that fails can then be split without redoing
   the work that was fine.
4. **Caps are hard.** When `cap_breached` names a counter, stop working that batch.
   Retrying past a cap is how an autonomous loop burns a CI budget overnight.
5. **Mutations go through the wrapper.** `scripts/gh_safe.sh` only; never raw
   `gh api -X DELETE`, never `--force` to a protected branch.

## State

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" state            # everything
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" state --batch b-001
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition b-001 building
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" gate b-001 review clean
```

A refused transition exits non-zero and writes nothing. That is the design: an
illegal move should be impossible to record, not merely discouraged.

## Modules

| Read when | Module |
|-----------|--------|
| Writing or reading events | [ledger-schema.md](modules/ledger-schema.md) |
| Moving a batch, or a move was refused | [state-machine.md](modules/state-machine.md) |
| Deciding whether to stop or continue | [escalation.md](modules/escalation.md) |

## Config — `.foreman/config.json`

```json
{
  "caps": { "pushes": 3, "review_rounds": 2, "reruns": 2 },
  "limits": { "max_open_prs": 3, "max_batch_issues": 5, "max_ci_minutes_per_day": 400 },
  "auto_merge": false,
  "protected_paths": ["**/auth/**", "**/migrations/**", ".github/workflows/**", "**/payments/**"],
  "merge_method": "squash"
}
```

`auto_merge` stays `false` until you have watched the review gate work on real
PRs. Turning it on is the only irreversible decision in the system.
