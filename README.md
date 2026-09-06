# foreman

A Claude Code plugin that runs the development loop end to end: triage issues,
group them into batches sized by what CI actually costs, build them, review each
PR with an **independent** agent, and auto-merge on green.

Built for repos where the test suite is slow enough that how you spend CI is the
main constraint on throughput.

> **Status: all five phases implemented, 764 tests.** Dogfooded against this
> repository's own issue queue, which is where most of those tests came from.
> Not yet run unattended against a production repo — `auto_merge` ships
> `false` for that reason.

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
- **No edit tools** — the reviewer has no `Edit`/`Write`. It keeps `Bash`,
  which the revert check needs, and runs that check in a scratch worktree;
  the instruction not to change the branch is a rule it is given, not one the
  harness enforces, and the README says so rather than pretending otherwise.
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
| `/foreman:run` | The whole loop — one action at a time, until idle or out of budget |
| `/foreman:triage` | Classify, size, risk-score and dedupe open issues. Writes labels only |
| `/foreman:batch` | Group actionable issues into batches sized by CI cost |
| `/foreman:build` | Implement one batch in a worktree, test-first, behind a local gate |
| `/foreman:land` | Open the PR, run review and CI concurrently, merge on green |
| `/foreman:file-findings` | Turn a review's findings into issues, deduplicated against the tracker |
| `/foreman:status` | What is in flight, what is stuck, what needs you |
| `/foreman:ci-profile` | Measure the repo's real CI costs into `.foreman/ci-profile.json` |

## Skills

| Skill | Owns |
|-------|------|
| `orchestration` | The loop, ledger schema, batch state machine, caps, escalation |
| `issue-triage` | Verdicts, closed label vocabulary, size and risk taxonomy |
| `work-batching` | Grouping rules, the amortisation arithmetic, splitting |
| `issue-implementation` | Worktrees, Red→Green, one commit per issue, the local gate |
| `pr-landing` | The ladder, check classification, the review gate, merging |
| `ci-economics` | CI profiling, test impact analysis, the tier ladder, flake budget |

Plus one agent: `reviewer` — the independent pre-merge review, with no `Edit` or
`Write`.

Plus one hook: a `PreToolUse` guard on `Bash` that holds any bare `gh` to the
same rules as `gh_safe.sh`, in any checkout with a `.foreman/` directory.
Without it the wrapper was advice — every command runs with an unscoped
`Bash`, and nothing stopped `gh api -X DELETE` typed directly. The hook is
inert in repositories foreman is not in use on.

## Scripts

All stdlib Python plus PyYAML; no service, no database.

| Script | Purpose |
|--------|---------|
| `scripts/ledger.py` | Append-only event log, folded state, validated transitions |
| `scripts/ci_profile.py` | Job graph, measured durations, flake rates, diff→test mapping |
| `scripts/status.py` | The digest |
| `scripts/triage.py` | Sizing, risk, actionability, dedupe, label planning |
| `scripts/batch.py` | Grouping, savings arithmetic, splitting a failed batch |
| `scripts/land.py` | Check classification, review-verdict validation, merge blockers |
| `scripts/gate.py` | The local gate: the diff's covering tests, then every cheap-tier CI step, as one command |
| `scripts/findings.py` | Turn review findings into issues, deduplicated against the tracker |
| `scripts/loop.py` | The scheduler: one next action, WIP limits, the daily CI budget |
| `scripts/gh_safe.sh` | Allowlisted `gh` wrapper — no deletes, no `--admin`, no protection edits, everything audited |
| `scripts/gh_guard.py` | The `PreToolUse` hook: a bare `gh` in a Bash command is denied if the wrapper would refuse it |

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

## Releasing

A release is the plugin tree at a tag, packaged so it installs without cloning.

```bash
claude plugin validate --strict .
claude plugin tag --dry-run     # foreman--v<version>; checks plugin.json and marketplace.json agree
claude plugin tag --push
python3 scripts/package.py --ref foreman--v<version> --expect-version <version>
gh release create foreman--v<version> dist/foreman-<version>.tar.gz dist/foreman-<version>.zip dist/SHA256SUMS
```

The version lives in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`,
and a test keeps the two equal; bump both on the change that ships the release.
`package.py` builds with `git archive`, so only tracked files ship — no tests, no
CI config, no `.foreman/` — and then verifies each archive the way an installer
reads it: every path the manifest declares is present, the hook's script is there
and executable, and the version is the one expected.

Installing a release:

| How | Command |
|-----|---------|
| From the tag, through the marketplace | `claude plugin marketplace add https://github.com/mrpeanut01/claude-foreman-plugin.git#foreman--v<version>` then `claude plugin install foreman@claude-foreman-plugin` |
| From the archive, for one session | `claude --plugin-dir ./foreman-<version>.zip` |
| From the archive, in a marketplace of your own | a plugin entry whose source is `{"source": "archive", "url": "<release asset URL>", "sha256": "<from SHA256SUMS>"}` |

## Roadmap

| Phase | Ships | State |
|-------|-------|-------|
| 1 | Ledger, CI profiler, `/foreman:status` | ✅ |
| 2 | `issue-triage` + `/foreman:triage` | ✅ |
| 3 | `work-batching` + `/foreman:batch` | ✅ |
| 4 | `build` + `land` + review gate + auto-merge | ✅ |
| 5 | `/foreman:run` — the unattended loop, budgets, escalation routing | ✅ |

**Not yet done:** run unattended against a real repo for a week and see what
breaks. Everything above is tested; none of it has been trusted overnight.

## Turning it on

Copy `config.example.json` to `.foreman/config.json`. Three settings decide how
much rope the loop has:

| Setting | Start at | Why |
|---------|----------|-----|
| `auto_merge` | `false` | It will build, review, and go green, but stop at the merge. Watch a few, then flip it. |
| `limits.max_ci_minutes_per_day` | Your real budget | The only thing between a stuck loop and your CI bill. |
| `protected_paths` | Generously | Anything listed escalates instead of merging, however green. |

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
