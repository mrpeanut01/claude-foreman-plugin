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
   `review_rounds` is a runaway ceiling only — review deadlock is decided by
   `land.review_stalled`, which asks whether findings are *repeating*.
5. **Mutations go through the wrapper.** `scripts/gh_safe.sh` only; never raw
   `gh api -X DELETE`, never `--force` to a protected branch. The plugin's
   `PreToolUse` hook (`scripts/gh_guard.py`) denies a bare `gh` the wrapper
   would refuse, in any checkout holding `.foreman/`, so this is a rule the
   harness keeps rather than one you remember.

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
  "caps": { "pushes": 8, "review_rounds": 5, "reruns": 2 },
  "limits": {
    "max_open_prs": 3,
    "max_batch_issues": 5,
    "max_ci_minutes_per_day": 400,
    "stale_after_s": 5400,
    "triage_every_s": 3600
  },
  "auto_merge": false,
  "protected_paths": ["**/auth/**", "**/migrations/**", ".github/workflows/**", "**/payments/**"],
  "merge_method": "squash"
}
```

`stale_after_s` is not optional: without it a gate that never resolves pins a
batch on `watch` forever, and watching increments no counter, so nothing else
would ever surface it.

`triage_every_s` is the earliest the loop will look for new issues again, not the
latest. The triage branch in `loop.next_action` sits below every live batch and
below any actionable issue not yet in one, so a batch parked on `watch` holds
triage off for as long as it stays live: with `triage_every_s` 3600 and
`stale_after_s` 5400, nothing is triaged until that batch goes stale and
escalates an hour and a half later. Triage spends no CI, so it is checked ahead
of the budget stop. Setting it to `0` switches triage off, which means the loop
only ever works on what the ledger already holds.

`caps.pushes` is deliberately loose at 8. It counts every push, including the
ones that resolved review findings, so a PR that survives several genuine review
rounds hits it without anything being wrong (see issue #17). Until that counting
rule is fixed, a tight value escalates healthy work.

`auto_merge` stays `false` until you have watched the review gate work on real
PRs. Turning it on is the only irreversible decision in the system.
