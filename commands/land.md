---
description: Open the PR, run the review gate and CI concurrently, and merge on green
usage: /foreman:land <batch-id>
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Task
---

# Land

Takes a built batch to merged. Read `Skill(foreman:pr-landing)` first.

## The ladder

```
push ─▶ cheap CI tier ────────┐
     └─▶ independent review ──┴─▶ both clean ─▶ expensive tier ─▶ merge queue ─▶ merged
```

Review and cheap CI start together. The expensive tier waits for both, because a
`changes_requested` verdict costs minutes of tokens and saves a 40-minute suite run.

## Steps

**1. Open the PR** and record it.

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr create --title "..." --body "..."
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type batch.meta \
  --json '{"batch":"<id>","pr":<n>}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> open
```

**2. Start the review** — dispatch the `reviewer` agent with the diff, the issue
text, and repo conventions. Nothing else. See `modules/review-gate.md`.

**3. Watch CI.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" checks --pr <n> --repo OWNER/NAME
```

Act on `gate`, never on the raw check list. Wait only on `actionable_pending`;
`human_gate_pending` and `advisory_pending` are not the loop's business.

**4. Record both verdicts.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" verdict --file /tmp/verdict.json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type review.verdict \
  --json '{"batch":"<id>","verdict":"clean","round":<r>,"findings":[...]}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" gate <batch> review clean
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" gate <batch> ci full_green
```

`verdict` validates and prints; it appends nothing. The middle line is what puts
the round on the record, and it goes in **every** round — clean or
`changes_requested` — with `findings` copied verbatim from the verdict file and
`round` the number `findings.py plan` gets. A gate value is one word about the
current commit; these two rules read the findings across rounds instead:

| Reads `review.verdict` | Inert while the ledger holds none |
|------------------------|-----------------------------------|
| `land.review_stalled` | A reviewer and builder trading one objection run to the ceiling of 5 instead of escalating the round it repeats |
| `/foreman:status` REVIEW QUALITY | Clean reviews later reverted reads `0/0` forever — the only measure of whether the gate rubber-stamps |

A verdict that fails validation is **not** a clean review. Send it back to the
reviewer with the errors; do not record it and do not argue with the validator.

**5. Merge.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" blockers --batch <id> --pr <n> --repo OWNER/NAME
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr merge <n> --auto --squash
```

`blockers` exits non-zero with a list when anything stands in the way. Fix or
escalate; never merge past it.

## On a red gate

CI failed → classify flake vs bug (`modules/ci-watch.md`), then rerun, fix, or
escalate as `flake_decision` says. Review requested changes → address the
findings and push; the gates reset and both run again. Rounds continue while the
reviewer keeps finding *different* things; the batch escalates when a finding
survives a round, or at the runaway ceiling of 5.
