---
name: pr-landing
description: Take a built batch to merged — open the PR, run the independent review and cheap CI concurrently, gate the expensive suite on both, adjudicate flakes, and auto-merge when nothing blocks. Use when landing a foreman batch, reading PR checks, or judging a review verdict.
---

# PR Landing

There is no human merge gate. An independent agent review and CI are the two
gates, and both must clear.

## The ladder

```
push ─▶ cheap CI tier ────────┐
     └─▶ independent review ──┴─▶ both clean ─▶ expensive tier ─▶ merge queue ─▶ merged
```

Review and cheap CI start together. The expensive tier waits for both, because a
review costs minutes of tokens and the suite costs 40 minutes of compute. Putting
the cheap judge first is the whole trick.

`ledger.may_run_expensive_tier(batch)` is the gate in code.

## Reading CI

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" checks --pr <n> --repo OWNER/NAME
```

Act on `gate`, never on the raw list.

| Field | Meaning |
|-------|---------|
| `actionable_pending` | Required, still running. **The only thing worth waiting for.** |
| `human_gate_pending` | Waiting on a person. Never wait — report and move on. |
| `advisory_pending` / `advisory_failed` | Not required. Informational only. |
| `failed` | Required and red. Fix or adjudicate. |
| `stale` | Reported against a different commit. Proves nothing about this one. |
| `head_sha` | The commit the verdict is about. `null` means it could not be resolved, and the gate is then `pending` whatever CI says. |
| `reason` | Why the gate reads as it does when no check could be judged. `null` in the ordinary case. |

An unknown check counts as required. A check the profile has never seen may be a
new required gate, and optimising it away is how a batch sits in the merge queue
forever.

## The review gate

See [modules/review-gate.md](modules/review-gate.md). In short: dispatch the
`reviewer` agent with the diff, the issues, and conventions — nothing else — and
validate what comes back:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" verdict --file /tmp/verdict.json
```

A verdict that fails validation is not a clean review. Return the errors to the
reviewer. Do not record it, and do not argue with the validator — its rules are
the reason `clean` means anything.

## Failures

See [modules/ci-watch.md](modules/ci-watch.md). Classify flake versus bug, then
`flake_decision` says `rerun`, `fix`, or `escalate`. Rerun the failed job only,
never the whole run, and never past the cap.

## Merging

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" paths --batch <id> --base <trunk> \
  --repo-dir ../foreman-<id> --apply
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" blockers --batch <id> --pr <n> --repo OWNER/NAME
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> ready
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr merge <n> --auto --<merge_method>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> merging
```

`<merge_method>` is what `blockers` prints under that key — `squash`, `merge`
or `rebase`, from `merge_method` in config, squash by default.

`ready` before the merge is requested, never after: `merging` is reachable
only from `ready`, and a move refused after `pr merge --auto` has gone out
leaves GitHub merging a batch the ledger still calls `open`.

`paths --apply` first: the protected-path check reads the batch's `paths`, and
until this runs those are the files its issues' prose mentioned, not the files
the branch changed. `blockers` refuses a batch whose paths were never confirmed
against the diff, or were confirmed for a commit other than the PR's head.

`blockers` reports everything at once — gates, labels, protected paths, whether
the paths were confirmed, the runaway caps, `auto_merge` — so you fix or
escalate in one pass rather than discovering obstacles one at a time. The convergence counters (`futile_pushes`,
`build_resumes`) are not among them: they belong to the escalation rules that
own them, not to the merge gate.

## File the findings before acting on them

Every review verdict, clean or not, goes through:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/findings.py" plan \
  --verdict /tmp/verdict.json --repo OWNER/NAME --batch <id> --pr <n> --round <r>
```

High and medium findings get fixed in this PR. Low findings will not be fixed
here and must become issues, or they are gone — an issue is the only artefact
`/foreman:triage` reads. See [file-findings](../../commands/file-findings.md).

## Record every verdict, as it happens

**Every round**, not once at the end, and always with the findings:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type review.verdict \
  --json '{"batch":"<id>","verdict":"changes_requested","round":2,"findings":[...]}'
```

`land.review_stalled` compares the findings of consecutive rounds to decide
whether the review is converging. Recording only after the merge, or omitting
`findings`, leaves it reading empty lists — the rule is then inert and only the
hard ceiling stops a genuine deadlock.

If the merge is later reverted, append `merge.reverted`. That pairing is the only
honest measure of whether the review gate works, and `/foreman:status` reports it.

## Exit criteria

The batch is `merged`, or escalated with a reason a human can act on. Both gate
values are in the ledger. No merge happened with a non-empty blocker list.
