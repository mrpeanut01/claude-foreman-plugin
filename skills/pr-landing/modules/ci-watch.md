# Watching CI

## Wait only on what can move

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" checks --pr <n> --repo OWNER/NAME
```

| Bucket | Wait? |
|--------|-------|
| `actionable_pending` | Yes — required and still running |
| `human_gate_pending` | **No.** Report it and move to another batch |
| `advisory_pending` | No |
| `advisory_failed` | No — informational |
| `failed` | No — act now |
| `stale` | Not this commit's results at all — see below |

## Which commit are these results about?

A gate verdict is a statement about **one commit**, which is why a push resets
both gates. The read is therefore scoped to a SHA: `checks` resolves the PR's
current head and asks the SHA-addressed endpoints for it, so a result that
arrives provably ran against that commit. `--sha <commit>` overrides it — pass
the batch's `head_sha` from the ledger when you want the gate judged against the
commit you pushed rather than whatever the PR points at now.

`gh pr checks` cannot do this: its output carries no head SHA, so in the window
between a push and the new run registering it reports the *previous* commit's
results. On a repo whose suite takes minutes, that window is minutes long, and a
`full_green` read inside it merges code CI has never run.

Anything landing in `stale` describes another commit and is ignored. An empty
check list after that is `pending` — CI has not started on this commit yet —
never green.

Polling a human approval gate is an unattended loop waiting for someone who is
asleep. Report `CHECKS_BLOCKED_BY_REVIEW_GATE`, move on, come back later.

## Flake or bug

```json
{"is_flaky": true, "confidence": 0.82, "summary": "connection reset to test Redis during setup"}
```

Flaky signals: timeouts, connection resets, port collisions, "address already in
use", clock or ordering dependence, a job whose profiled `flake_rate` is already
high.

Real signals: an assertion on a value the diff touches, an import or type error,
a failure in a file the diff changed, deterministic local reproduction.

Then:

```python
land.flake_decision(classification, batch, config)  # rerun | fix | escalate
```

Below 0.7 confidence it returns `fix`. Rerunning a real failure hides it and
costs a suite run; fixing a flake costs a small wrong change that review will
usually catch. The asymmetry favours fixing.

## Rerunning

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/gh_safe.sh" run rerun <run-id> --failed
```

`--failed` only. Rerunning a whole run to fix one flaky job pays for every job
again. Record it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type ci.rerun --json '{"batch":"<id>","job":"integration"}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type flake.observed --json '{"job":"integration","test":"test_login"}'
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type ci.launched --json '{"batch":"<id>","tier":"cheap","seconds":120}'
```

The `ci.launched` event is what the daily budget counts. Skip it and the loop
believes CI is free.

## Fixing a real failure

Read the whole log — `gh run view <id> --log-failed` — not the last line. State
the cause before editing: *"fails because X, which the change at Y did not
account for."* If you cannot say that sentence, you are about to guess.

Then fix, verify locally, push. The push resets both gates and the ladder starts
again from the cheap tier.

## Caps

`pushes: 8`, `review_rounds: 5`, `reruns: 2` — all **runaway ceilings**, not
judgement. At any of them the batch escalates.

`reruns` is the one that means what it says: a third rerun is no longer a
hypothesis about flakiness, it is a hope. The other two are loose because they
count events elapsed rather than progress, and a PR that survives several genuine
review rounds trips them without anything being wrong. Review deadlock is decided
by `land.review_stalled` instead — see [review-gate](review-gate.md).
