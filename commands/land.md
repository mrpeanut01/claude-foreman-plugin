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

**1. Push, open the PR, and record both.**

```bash
git -C ../foreman-<id> push -u origin foreman/<id>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type batch.pushed \
  --json '{"batch":"<id>","sha":"<full sha of the pushed head>"}'
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr create --title "..." --body "..."
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type batch.meta \
  --json '{"batch":"<id>","pr":<n>}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> open
```

`batch.pushed` goes in on **every** push, this one included. It is what resets
both gates, counts the push, and records the head the merge gate will later
check the batch's paths against. A push the ledger never heard about leaves a
stale green in place.

**2. Start the review** — dispatch the `reviewer` agent with the diff, the issue
text, and repo conventions. Nothing else. See `modules/review-gate.md`.

**3. Watch CI.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" checks --pr <n> --repo OWNER/NAME [--sha <commit>]
```

Act on `gate`, never on the raw check list. Wait only on `actionable_pending`;
`human_gate_pending` and `advisory_pending` are not the loop's business.

| Bucket | Means |
|--------|-------|
| `passed` | Green for this commit |
| `pending` | Every check not yet resolved, whatever kind; the three rows below split it |
| `actionable_pending` | Required, still running. The only thing worth waiting for |
| `human_gate_pending` | Waiting on a person. Report it and move to another batch |
| `advisory_pending` / `advisory_failed` | Not required. Informational |
| `failed` | Required and red. Fix or adjudicate |
| `stale` | Reported against **another commit**. Ignored — it proves nothing about this one |

The verdict is about one commit. `checks` resolves the PR's current head and
reads the SHA-addressed endpoints for it, so `head_sha` in the output names the
commit the `gate` describes; anything CI posted about a different commit lands
in `stale`. That is what stops the *previous* push's green being read as this
one's in the minutes before the new run registers. Pass `--sha` with the
batch's `head_sha` from the ledger to gate the commit you pushed rather than
whatever the PR points at now.

`head_sha: null` with `gate: pending` and a `reason` means the commit could not
be resolved at all — `gh pr view` failed, or is too old to know `headRefOid`.
It is `pending`, not green, and the check list is deliberately not read
unscoped to fill the gap. See `modules/ci-watch.md` in `Skill(foreman:pr-landing)`
for the full reasoning.

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

**5. Confirm what the batch touches, then merge.**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" paths --batch <id> --base <trunk> \
  --repo-dir ../foreman-<id> --apply
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" blockers --batch <id> --pr <n> --repo OWNER/NAME
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> ready
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr merge <n> --auto --<merge_method>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> merging
```

`<merge_method>` is the value `blockers` printed under `merge_method`: `squash`
unless `.foreman/config.json` says `merge` or `rebase`. The key used to be
written by the example config and read by nothing.

`ready` is the state both gates clear into, and it is the only state `merging`
can be reached from: `open -> merging` is not a move, and `transition` refuses
it. The `ready` line therefore comes **before** the merge is requested, so a
refusal costs nothing — the recipe used to request the merge first and move
the batch second, and when the move was refused GitHub was already merging a
batch the ledger still called `open`. `loop.py` answers `advance` for an open
batch whose gates are clear and `merge` for one already in `ready`; both land
here. If `ledger.py state --batch <id>` already says `ready` (an earlier pass
got that far and stopped), skip the `ready` line and carry on.

The first line replaces the batch's `paths` — until now the file names its
issues' prose happened to mention — with what the branch really changes, and
records the commit that diff was of. `blockers` **refuses** a batch whose paths
were never confirmed this way, or were confirmed against a commit other than
the PR's current head: an issue that names no file yields `paths: []`, and an
empty list used to clear the protected-path gate for exactly the batch nobody
had looked at. Point `--repo-dir` at the worktree holding the branch; from a
checkout without it the diff is empty, the command exits 1, and the declared
paths are left alone.

`blockers` exits non-zero with a list when anything stands in the way. Fix or
escalate; never merge past it.

**6. Confirm the merge, then close the issues.**

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" pr view <n> --repo OWNER/NAME --json state
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" transition <batch> merged
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" state --batch <id>    # its issue list
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" issue close <issue> --repo OWNER/NAME \
  --comment "Fixed by #<n> (batch <id>)."
```

`--auto` queues the merge behind the remaining checks, so the PR reads `MERGED`
only later. Close nothing before it does.

This step is the whole of `watch` on a batch in `merging`: ask GitHub, and
either record the merge or take the batch back to `blocked`. `--auto` never
reports back — a merge queue that refuses it, or never fires, leaves the batch
here holding a slot against `max_open_prs`, which is why `stale_after_s`
escalates a merge that has not completed.

Nothing else closes them. The PR body cites issues as `Refs #n`, which GitHub
does not treat as a closing keyword, and no script in the pipeline touches issue
state. The cost is not an untidy tracker: `loop._grouped_issues` counts an issue
as taken once it appears in any batch, whatever state that batch is in, so an
issue left open after its batch merged is never batched again while still sitting
in the queue. Observed twice while foreman ran on its own repo — both merged
batches left every one of their issues open.

Skipping this step is no longer silent, but it is not repaired either.
`loop.merged_leaving_open` escalates once, the next time a triage pass finds the
issue still open, and a person then closes it by hand — the loop will not batch
it again, because a second PR for work already on trunk is worse than an untidy
tracker. Closing it here is the cheap version.

## On a red gate

CI failed → classify flake vs bug (`modules/ci-watch.md`), then rerun, fix, or
escalate as `flake_decision` says. Review requested changes → address the
findings, push, and record the push (`batch.pushed`, as in step 1); the gates
reset and both run again, and step 5 will confirm the paths afresh for the new
head. Rounds continue while the
reviewer keeps finding *different* things; the batch escalates when a finding
survives a round, or at the runaway ceiling of 5.
