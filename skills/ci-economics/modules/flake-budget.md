# Flakes and the Rerun Budget

A failed job is either a real failure or a flake, and the two demand opposite
responses. Guessing wrong is expensive both ways: rerunning a real failure wastes
the suite and delays the fix; "fixing" a flake produces a confident, wrong change.

## The definition that makes this decidable

> **A flake is one commit where the same job both failed and passed.**

Not "a test that fails sometimes" — that is a feeling. The definition above is
computable from run history and is what `ci_profile.flake_rates` measures.
A job that fails every time on a commit is a real failure, however flaky it is
in general.

## Classifying a fresh failure

History alone cannot classify a failure you have not rerun yet, so read the log
and emit a structured verdict — the pattern from Anthropic's
`test-failure-analysis.yml`:

```json
{"is_flaky": true, "confidence": 0.82, "summary": "connection reset to the test Redis during setup"}
```

Signals for flaky: timeouts, connection resets, port collisions, "address already
in use", clock or ordering dependence, a job whose `flake_rate` is already high.

Signals for real: an assertion on a value your diff touches, an import or type
error, a failure in a test file the diff changed, deterministic reproduction
locally.

## Acting on the verdict

| Verdict | Action |
|---------|--------|
| `is_flaky` and `confidence ≥ 0.7` | `gh run rerun <id> --failed` — **the failed job only**, never the whole run. Append `ci.rerun` and `flake.observed`. |
| `is_flaky` and `confidence < 0.7` | Treat as real. A cheap wrong fix beats a rerun loop that hides a bug. |
| not flaky | Fix it. Root cause, not symptom. |

## The cap is the safety rail

`caps.reruns` (default 2). At the cap, `cap_breached` names it and the batch
escalates — because a third rerun is no longer a hypothesis about flakiness, it
is a hope. This cap is what stops an unattended loop rerunning a suite all night.

## Quarantine

After the same `job::test` appears in `flake.observed` N times (default 5),
open one quarantine issue naming the test, its observed rate, and the runs. Do
not silently skip it: a skipped test with no issue is indistinguishable from
coverage that never existed.

`/foreman:status` shows the leaderboard, worst first. A test at the top of it is
costing more in reruns than deleting it would cost in coverage.

## Reading the rate

| `flake_rate` | Meaning |
|--------------|---------|
| `0.0` | Failures are evidence. Trust them. |
| `0.01 – 0.05` | Normal for integration suites. Rerun within cap. |
| `> 0.1` | This job's failures are barely information. Fix or quarantine before trusting any verdict it gives. |
