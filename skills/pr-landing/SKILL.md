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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" blockers --batch <id> --pr <n> --repo OWNER/NAME
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr merge <n> --auto --squash
```

`blockers` reports everything at once — gates, labels, protected paths, caps,
`auto_merge` — so you fix or escalate in one pass rather than discovering
obstacles one at a time.

## After the merge

Record the verdict for the scoreboard:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type review.verdict \
  --json '{"batch":"<id>","verdict":"clean"}'
```

If the merge is later reverted, append `merge.reverted`. That pairing is the only
honest measure of whether the review gate works, and `/foreman:status` reports it.

## Exit criteria

The batch is `merged`, or escalated with a reason a human can act on. Both gate
values are in the ledger. No merge happened with a non-empty blocker list.
