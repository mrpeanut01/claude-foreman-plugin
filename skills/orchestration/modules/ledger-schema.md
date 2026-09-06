# Ledger Schema

`.foreman/events.jsonl` — one JSON object per line, append-only. Current state is
a fold over the file (`ledger.fold`). Never rewrite a line; correct by appending.

## Where it lives

One ledger per repository, in the checkout — **not** in whatever directory you
happen to be standing in. A build runs from `../foreman-<batch>`, so `ledger.py`
anchors a relative `--ledger` to the repository root (the main checkout, even
from a linked worktree) before it reads or writes. An absolute path is obeyed
as given. Two ledgers is the worst outcome available here: the push that resets
`review_gate` lands in one file while the merge decision reads the other.

## Event types

| `type` | Required fields | Effect on folded state |
|--------|-----------------|------------------------|
| `issue.triaged` | `issue`, `verdict` | Upserts `issues[n]`. Re-triage overwrites; the log keeps both. Its `ts` matters: triage asks GitHub for **open** issues only, so a record newer than a merged batch is the loop's only evidence that the merge did not close the issue, and `loop._grouped_issues` hands it back to batching. |
| `triage.completed` | — | Sets `last_triage_at`. Written once per `triage.py apply`, **even when it labelled nothing** — it marks that a pass happened, and `loop.triage_due` reads it to decide when to look for new issues. Without it the loop asks for triage on every tick. |
| `batch.created` | `batch`, `issues` | Creates the batch in `planned`, both gates `pending`. Optional `branch`, `pr`. **Ignored when the id already exists** — ids are unique, so a repeat is a numbering bug, and replacing the record would discard a merge. |
| `batch.state` | `batch`, `from`, `to` | Moves the batch. Only ever written by `ledger.transition`. `building -> building` is the resume: it records no progress, so it increments `attempts.build_resumes` instead — the only number that grows while a batch is parked mid-build. |
| `batch.pushed` | `batch`, `sha` | **Resets both gates to `pending`** and increments `attempts.pushes`. Remembers the CI verdict it was pushed into, so the next CI result can score the push: red again the same way increments `attempts.futile_pushes`, any green resets that run to 0. |
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
