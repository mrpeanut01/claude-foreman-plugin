# Escalation

Escalation is how the loop stays autonomous. Handing a problem to a human and
**continuing with everything else** is what separates a loop that runs overnight
from one that stalls on its first surprise.

## Escalate immediately

| Trigger | Why not retry |
|---------|---------------|
| `cap_breached` returns a counter | Three failed pushes means the diagnosis is wrong, not the attempt count. |
| Same test fails identically twice | A second identical failure is evidence, not noise. |
| Diff touches `protected_paths` | Auth, migrations, payments, and CI config are never auto-merged, however green. |
| Review requests changes twice | Builder and reviewer are not converging. |
| Issue is ambiguous after triage | Guessing intent produces a plausible PR solving the wrong problem — the most expensive failure mode here. |
| A merge conflict needs judgment | Mechanical rebase is fine; semantic conflicts are not. |
| `gh` returns auth/permission errors | Infrastructure, not code. |

## Never escalate

- A first CI failure with a clear cause — fix it.
- A flake above the confidence threshold — rerun it, within the rerun cap.
- Lint or format failures — fix them.

## How

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition b-001 escalated
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type escalation \
  --json '{"batch":"b-001","reason":"test_auth_expiry fails identically on 3 pushes; the fix in session.py:88 does not address the clock-skew path"}'
```

Then **move to the next batch**. Do not wait, do not ask, do not stop.

## Writing the reason

The reason is read by a person with no context, possibly tomorrow. State what was
tried and what the evidence points at.

> ✅ `test_auth_expiry fails identically on 3 pushes; the fix in session.py:88 does not address the clock-skew path`
> ❌ `CI keeps failing`

Post the same text as a PR comment so it is visible where the work is, and label
the PR `needs-human`. That label is a hard merge blocker regardless of gate state.

## Requeueing

A human resolves and moves it back: `escalated → planned`. Attempt counters are
**not** reset by that transition — if the caps should be forgiven, that is a
deliberate `batch.meta` event, so the forgiveness is itself on the record.
