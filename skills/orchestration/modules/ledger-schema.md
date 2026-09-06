# Ledger Schema

`.foreman/events.jsonl` — one JSON object per line, append-only. Current state is
a fold over the file (`ledger.fold`). Never rewrite a line; correct by appending.

## Event types

| `type` | Required fields | Effect on folded state |
|--------|-----------------|------------------------|
| `issue.triaged` | `issue`, `verdict` | Upserts `issues[n]`. Re-triage overwrites; the log keeps both. |
| `triage.completed` | — | Sets `last_triage_at`. Written once per `triage.py apply`, **even when it labelled nothing** — it marks that a pass happened, and `loop.triage_due` reads it to decide when to look for new issues. Without it the loop asks for triage on every tick. |
| `batch.created` | `batch`, `issues` | Creates the batch in `planned`, both gates `pending`. Optional `branch`, `pr`. **Ignored when the id already exists** — ids are unique, so a repeat is a numbering bug, and replacing the record would discard a merge. |
| `batch.state` | `batch`, `from`, `to` | Moves the batch. Only ever written by `ledger.transition`. |
| `batch.pushed` | `batch`, `sha` | **Resets both gates to `pending`** and increments `attempts.pushes`. |
| `gate.set` | `batch`, `gate`, `value` | Sets `ci_gate`/`review_gate`. A red gate under a `ready` batch drops it to `blocked`. |
| `ci.rerun` | `batch`, `job` | Increments `attempts.reruns`. |
| `review.verdict` | `batch`, `verdict`, `findings`, `round` | Appends to the review scoreboard. **`findings` is not optional** — `land.review_stalled` reads it to decide whether findings are repeating, and without it the convergence rule is inert. |
| `merge.reverted` | `batch` | The rubber-stamp signal. Pairs against clean verdicts in `/foreman:status`. |
| `flake.observed` | `job`, `test` | Increments the `job::test` counter. |
| `escalation` | `batch`, `reason` | Adds to NEEDS YOU. `reason` is read by a human — write a sentence, not a code. |
| `batch.meta` | `batch`, plus fields | Merges arbitrary fields (e.g. `pr`, `branch`) onto the batch. |

`ts` and `type` are added by `ledger.append`. Every event carrying `batch` also
refreshes that batch's `updated`.

## Why a push resets both gates

A gate verdict is a statement about **one commit**. After a new push, a green CI
result and a clean review both describe code that no longer exists. Carrying them
forward is the single easiest way to merge unreviewed code, so the fold drops
them unconditionally.

## Reading state in your own code

```python
import ledger

state = ledger.load(Path(".foreman"))
for bid, batch in state.batches.items():
    if ledger.blocking_gates(batch):
        ...
```

## Corruption

`read_events` skips unparseable lines rather than raising. A torn write during a
crash costs one event, not the whole ledger. If a batch looks impossible,
reconcile against GitHub — `gh pr view` is the tiebreaker — and append a
`batch.meta` correction.
