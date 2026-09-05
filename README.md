# foreman

A Claude Code plugin that runs the development loop end to end: triage issues,
group them into batches sized by what CI actually costs, build them, review each
PR with an **independent** agent, and auto-merge on green.

Built for repos where the test suite is slow enough that how you spend CI is the
main constraint on throughput.

> **Status: phase 1 of 5.** The ledger, the CI profiler, and `/foreman:status`
> are implemented and tested. Triage, batching, build, and land are designed but
> not yet built — see [Roadmap](#roadmap).

## The idea

The master agent is **a loop over a durable ledger, not a long conversation.**
All state lives in an append-only event log, so the loop survives crashes,
context limits, and a laptop closing mid-batch. Any session re-reads the ledger
and continues from exactly where the last one stopped.

```
                    ┌──────────── .foreman/events.jsonl ──────────┐
                    │  issues · batches · gates · flakes · caps    │
                    └─────────────────────────────────────────────┘
                          ▲        ▲        ▲        ▲
   issues ──▶ TRIAGE ──▶ BATCH ──▶ BUILD ──▶ LAND ──▶ merged
              classify   group by  worktree  PR, CI
              dedupe     CI cost   TDD       review gate
              size       + risk    local     auto-merge
                                   gate        │
                                               └─▶ ESCALATE (never blocks the loop)
```

## Why CI economics gets its own skill

Most of what an autonomous dev loop wastes is CI, and it wastes it one way:
**paying a slow remote suite to discover something a laptop already knew.**

| Tactic | Effect |
|--------|--------|
| **Local gate** | Map the diff to its covering tests, run those locally, push only when green |
| **Batching** | A 40-minute suite costs 40 minutes whether the PR fixes one issue or five — so group compatible issues, one commit each |
| **Tier ladder** | Cheap CI and the agent review run concurrently; the expensive suite waits for both |
| **Flake budget** | A flake is *one commit where a job both failed and passed* — rerun those, fix everything else, capped at 2 |
| **Merge queue** | The full suite runs against real trunk exactly once, at the end |

## The review gate

There is no human merge gate. Instead an independent agent reviews the PR, and
a clean verdict is what unlocks the merge. The design problem is that an agent
reviewing an agent's work tends to approve it, so "clean" is made expensive to say:

- **Context isolation** — the reviewer sees the diff, the issue, and repo
  conventions. Not the builder's transcript; the PR body is passed as the
  author's *claim*, not as fact.
- **Revert-test proof** — revert the source change, keep the new test, run it. If
  it still passes, the test guards nothing. A fact, not a judgement.
- **Evidence-typed verdict** — a `clean` verdict must name the covering tests and
  carry `revert_check: failed_as_expected`, or the schema parser rejects it.
- **No fix ability** — the reviewer has no `Edit`/`Write`. It must articulate the
  defect rather than quietly patch and approve.
- **Two lenses on risky diffs** — correctness and blast-radius reviewers must both
  come back clean.
- **Post-merge measurement** — the ledger tracks clean reviews that were later
  reverted. That ratio is the rubber-stamp signal, and `/foreman:status` shows it.

That last point is the honest one: you cannot fully verify a reviewer up front,
so the system measures it after the fact and tells you when it is drifting.

## Install

```bash
claude plugin marketplace add mrpeanut01/claude-foreman-plugin
claude plugin install foreman@claude-foreman-plugin
```

## Commands

| Command | Does |
|---------|------|
| `/foreman:status` | What is in flight, what is stuck, what needs you |
| `/foreman:ci-profile` | Measure the repo's real CI costs into `.foreman/ci-profile.json` |

Planned: `/foreman:triage`, `/foreman:batch`, `/foreman:build`, `/foreman:land`,
`/foreman:run`.

## Skills

| Skill | Owns |
|-------|------|
| `orchestration` | The loop, ledger schema, batch state machine, caps, escalation |
| `ci-economics` | CI profiling, test impact analysis, the tier ladder, flake budget |

## Scripts

All stdlib Python plus PyYAML; no service, no database.

| Script | Purpose |
|--------|---------|
| `scripts/ledger.py` | Append-only event log, folded state, validated transitions |
| `scripts/ci_profile.py` | Job graph, measured durations, flake rates, diff→test mapping |
| `scripts/status.py` | The digest |
| `scripts/gh_safe.sh` | Allowlisted `gh` wrapper — no deletes, no `--admin`, no protection edits, everything audited |

## Two rules worth stating plainly

**A push resets both gates.** A gate verdict describes one commit. After a new
push, a green CI result and a clean review both describe code that no longer
exists. Carrying them forward is the easiest way to merge unreviewed code.

**An incomplete impact map means run everything.** If any changed file cannot be
mapped to tests, the full suite runs. An unmapped file is exactly where an
unguarded regression hides, so the mapper never guesses and the loop never
overrides it.

## Tests

```bash
python3 -m pytest tests/ -q
```

## Roadmap

| Phase | Ships | State |
|-------|-------|-------|
| 1 | Ledger, CI profiler, `/foreman:status` | ✅ done |
| 2 | `issue-triage` + `/foreman:triage` | planned |
| 3 | `work-batching` + `/foreman:batch` | planned |
| 4 | `build` + `land` + review gate + auto-merge | planned |
| 5 | `/foreman:run` — the unattended loop, budgets, escalation routing | planned |

## Prior art

Patterns borrowed, with thanks:

- [getsentry/skills](https://github.com/getsentry/skills) — scripts that emit JSON,
  actionable-vs-human-gate check classification, explicit exit conditions
- [athola/claude-night-market](https://github.com/athola/claude-night-market) —
  thin command → fat skill → `modules/` progressive disclosure; diff-derived
  validation with revert-tests
- [anthropics/claude-code](https://github.com/anthropics/claude-code) `triage-issue` —
  constrained wrapper scripts, closed label vocabulary, conservative bias
- [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action) —
  structured-output flake detection with a confidence threshold
- [chhoumann/claude-github-triage](https://github.com/chhoumann/claude-github-triage) —
  persistent triage store, skip-already-triaged, apply gate

## License

MIT
