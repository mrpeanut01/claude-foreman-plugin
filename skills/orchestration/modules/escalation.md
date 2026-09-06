# Escalation

Escalation is how the loop stays autonomous. Handing a problem to a human and
**continuing with everything else** is what separates a loop that runs overnight
from one that stalls on its first surprise.

## Escalate immediately

| Trigger | Why not retry |
|---------|---------------|
| `cap_breached` returns a counter | A runaway ceiling was reached. These are loose (`pushes: 8`, `review_rounds: 5`) because they count events elapsed, not progress. |
| `futile_push_run` returns a reason | Three pushes in a row left CI red the same way. This, not `caps.pushes`, is the push rule that reads progress — see below. |
| `stalled_build` returns a reason | Three resumes and the batch still has not reached `built`. Nothing else can see this one — see below. |
| `merged_leaving_open` returns a batch | A merged batch's issue is still on the tracker, and the ledger cannot say why — see below. |
| Same test fails identically twice | A second identical failure is evidence, not noise. |
| Diff touches `protected_paths` | Auth, migrations, payments, and CI config are never auto-merged, however green. |
| The same finding survives a review round | Builder and reviewer are trading one objection. Rounds elapsed is not the test — see `review-gate.md`. |
| Issue is ambiguous after triage | Guessing intent produces a plausible PR solving the wrong problem — the most expensive failure mode here. |
| A merge conflict needs judgment | Mechanical rebase is fine; semantic conflicts are not. |
| `gh` returns auth/permission errors | Infrastructure, not code. |

## Pushes: volume is not the measure

`attempts.pushes` counts every push, and every round of review findings needs
one. Three pushes that each cleared a round of findings are a PR being reviewed
properly, and escalating it punishes exactly the batch that behaved best. So the
push rule reads `attempts.futile_pushes` instead: the fold scores each push the
moment CI answers it, and only a push that left CI failing the way it was
failing *before* the push counts. Any green ends the run.

The review gate is not scored this way — different findings each round are
convergence, and `review-gate.md` judges those on the findings themselves.

## Builds: the one the clock cannot catch

`building` is the only live state whose action does not move the batch.
`next_action` answers `build`, the recipe re-enters `building`, and the fold
records `building -> building` as no progress on purpose — so the staleness
window reads the same age forever, no counter in `caps` moves, and there is no
push for `futile_push_run` to score. Resuming is right; resuming without a bound
is a work loop that never reaches anybody.

`attempts.build_resumes` counts the re-entries, and `stalled_build` escalates at
three. It reads only while the batch is still in `building`: a build picked up
four times that then reached `built` converged, and its old resumes are history.

Like `futile_pushes`, the ceiling has a **default** — a repo with no
`.foreman/config.json` is still bounded. `caps.build_resumes` overrides it.

## A merged batch that left its issue open

Triage lists **open** issues only, so a sighting of an issue after its batch
merged is proof the merge did not close it. Two things produce that, and the
ledger cannot tell them apart:

1. the PR merged without a closing keyword and the fix is already on trunk;
2. the fix did not fix it.

They want opposite actions — close the issue, or reopen the work — so the loop
reports it. Handing the issue back to batching instead is worse than doing
nothing: `batch.py plan` groups `triage_out["triaged"]`, and these are exactly
the issues triage *skips*, so the action can never be taken; and in the case
where it could, it cuts a second PR for work already merged. Repeatedly.

The record is keyed on the **issues**, not on the batch. The batch is `merged` —
terminal, nothing to transition, nothing to retry — and `status._needs_human`
reads an escalation's batch's current state to decide whether it still matters,
so filing this under the batch would hide it from the morning digest.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type escalation \
  --json '{"issues":[5],"merged_batch":"b-001","reason":"b-001 merged, but triage has since seen #5 still open — close it if PR #7 fixed it, or say what is still wrong"}'
```

No `transition` line: `merged` is terminal. The escalation itself is the bound —
`merged_leaving_open` drops every issue a recorded escalation names, so writing
it is what stops the loop raising it again on the next tick. Then comment on the
merged PR and move on.

The root cause is usually upstream: `commands/land.md` step 6 closes the issues,
and nothing else does. A repeat of this escalation across several batches means
that step is being skipped.

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

Post the same text as a PR comment so it is visible where the work is.

If the repo defines a `needs-human` (or `do-not-merge`, `blocked`, `hold`) label,
apply it — `merge_blockers` treats all of them as hard blockers regardless of gate
state. **If the repo defines none of them, do not create one.** The closed-label
vocabulary rule outranks this, and the ledger escalation plus the PR comment are
already sufficient: `loop.next_action` will not pick the batch up again.

## Requeueing

A human resolves and moves it back: `escalated → planned`. Attempt counters are
**not** reset by that transition — if the caps should be forgiven, that is a
deliberate `batch.meta` event, so the forgiveness is itself on the record.
