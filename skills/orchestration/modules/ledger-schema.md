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
| `issue.triaged` | `issue`, `verdict` | Upserts `issues[n]`. Re-triage overwrites; the log keeps both. Its `ts` is also a sighting (see below). |
| `triage.completed` | `open_issues` | Sets `last_triage_at`, and stamps every issue in `open_issues` into `open_seen_at`. Written once per `triage.py apply`, **even when it labelled nothing** — it marks that a pass happened, and `loop.triage_due` reads it to decide when to look for new issues. Without it the loop asks for triage on every tick. |
| `batch.created` | `batch`, `issues` | Creates the batch in `planned`, both gates `pending`. Optional `branch`, `pr`. **Ignored when the id already exists** — ids are unique, so a repeat is a numbering bug, and replacing the record would discard a merge. |
| `batch.state` | `batch`, `from`, `to` | Moves the batch. Only ever written by `ledger.transition`. `building -> building` is the resume: it records no progress, so it increments `attempts.build_resumes` instead — the only number that grows while a batch is parked mid-build. |
| `batch.pushed` | `batch`, `sha` | **Resets both gates to `pending`** and increments `attempts.pushes`. Remembers the CI verdict it was pushed into, so the next CI result can score the push: red again the same way increments `attempts.futile_pushes`, any green resets that run to 0. |
| `gate.set` | `batch`, `gate`, `value` | Sets `ci_gate`/`review_gate`. A red gate under a `ready` batch drops it to `blocked`. |
| `ci.rerun` | `batch`, `job` | Increments `attempts.reruns`. |
| `review.verdict` | `batch`, `verdict`, `findings`, `round` | Appends to the review scoreboard. **`findings` is not optional** — `land.review_stalled` reads it to decide whether findings are repeating, and without it the convergence rule is inert. |
| `merge.reverted` | `batch` | The rubber-stamp signal. Pairs against clean verdicts in `/foreman:status`. |
| `flake.observed` | `job`, `test` | Increments the `job::test` counter. |
| `escalation` | `batch`, `reason` | Adds to NEEDS YOU. `reason` is read by a human — write a sentence, not a code. An escalation about an **issue** rather than a batch carries `issues` (plus `merged_batch` for context) *instead of* `batch` — see below. |
| `batch.meta` | `batch`, plus fields | Merges arbitrary fields (e.g. `pr`, `branch`) onto the batch. |

`ts` and `type` are added by `ledger.append`. Every event carrying `batch` also
refreshes that batch's `updated`.

## Sightings: how the loop learns an issue is still open

`triage.fetch_issues` asks GitHub for **open** issues only, so every issue that
reaches a triage plan was open when the plan was built. `open_seen_at[n]` records
when each was last seen that way, and `loop.merged_leaving_open` reads it: a
sighting newer than a merged batch's `progress_at` is the loop's only evidence
that the merge did not close the issue.

`open_issues` on `triage.completed` carries **skipped** issues as well as
recorded ones, and that is the whole point. `triage.should_skip` refuses to
re-triage an issue whose `updatedAt` has not changed, and a PR that merges
without a closing keyword changes nothing about the issue — so no `issue.triaged`
newer than the merge is ever written for exactly the issues this rule exists to
catch. Reading the records alone left the rule with no producer (issue #58).

A ledger written before this field folds fine; those passes simply record no
sightings.

### What a sighting produces: an escalation, not a re-batch

The sighting says the issue is open. It does **not** say why, and the two
possibilities want opposite actions: the PR merged without a closing keyword and
the fix is on trunk (close the issue), or the fix did not fix it (do the work
again). So the loop hands it to a person rather than guessing.

The escalation is keyed on `issues`, not on `batch`, and that is deliberate
twice over. The batch is `merged` — terminal, with no transition to make and
nothing to retry — so it is not what needs attention; and `status._needs_human`
decides whether an escalation still matters by reading its batch's *current*
state, so filing this under a merged batch would hide it in exactly the bucket
the morning digest is right to consider finished.

`loop.merged_leaving_open` drops any issue a recorded escalation already names,
which is what stops it repeating every tick. Handing the issue back to batching
instead was unreachable and unbounded in both directions — see the note in
`loop._grouped_issues`.

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

The fold makes the same trade for a line that parses but cannot be read: a
`triage.completed` whose `open_issues` is a number, a `batch.state` with no
`to`, a `gate.set` with no `gate`. Each is one `ledger.py append --json` away,
the line cannot be taken back out, and `fold` is the single reader every script
goes through — so before this, one such line crashed `loop.py`, `status.py` and
`land.py` from then on. Now it is skipped **whole** (every branch reads what it
needs before it writes anything, so nothing is half-applied) and counted in
`State.skipped_lines` alongside the torn lines. `/foreman:status` prints a
`LEDGER` warning whenever that count is non-zero, because a state that has
quietly dropped an event is a state that has stopped matching the repository.
