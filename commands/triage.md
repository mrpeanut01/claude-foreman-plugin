---
description: Classify, size, risk-score and dedupe open issues into the ledger
usage: /foreman:triage [--limit 50] [--apply] [--force]
allowed-tools: Bash(python3:*), Bash(gh:*), Read, Write
---

# Triage

Reads open issues, produces a verdict for each, and records it. Writes **labels
only** — no code, no comments, no closures. That makes it the safest stage to let
run wide before you trust anything else.

## Plan first

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py" plan \
  --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" --limit 50 \
  > /tmp/foreman-triage.json
```

Read `Skill(foreman:issue-triage)` before reviewing the output — the deterministic
scoring is in the script, but deciding whether a verdict is *right* means reading
the issue.

## Show the plan, then apply

Render the plan as a table (issue, kind, size, risk, verdict, labels, why) and
show it before writing anything. Each record also carries `risk_reason` — the
word that set the score and whether it came from the title or the body — so put
it next to the risk column whenever anything scores `high`. Then:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py" apply \
  --repo OWNER/NAME --plan /tmp/foreman-triage.json
```

Labels go through `gh_safe.sh`, and every issue's verdict is appended to the
ledger — so the next run skips anything that has not been edited since.

## The two rules that keep this trustworthy

**Never invent a label.** `plan_labels` drops anything the repo has not defined.
If a size or risk taxonomy is missing, the labels silently do not appear — that
is correct. Adding `size:small` to a repo that has no such label is noise.

**A false lifecycle label is worse than a missing one.** `needs-repro` only
applies to bugs with no evidence *and* no stated expectation. A narrative
description of expected-versus-actual counts as a repro; so does a traceback, a
file path, a command, or numbered steps. `needs-info` is tighter still: the
failure has been shown, and the reporter's own words make it environment-
specific — *"works locally, fails in production"*, *"since upgrading"* — without
naming the version or the machine. A bug that never blames an environment is
never asked for one.

## Verdicts

| Verdict | Means | Next |
|---------|-------|------|
| `actionable` | Someone can start on it | Enters the batching queue |
| `needs-repro` | Bug with nothing to go on | Waits on the reporter |
| `needs-info` | Bug that blames an environment it never names | Waits on the reporter |
| `duplicate` | Title overlaps an open issue ≥ 0.6 on two or more words | Linked, not queued |

Only `actionable` reaches `/foreman:batch`.

## When you disagree with the score

The heuristics are deliberately blunt. If an issue is scored `low` risk and you
can see it touches something dangerous, override it in the plan before applying —
and if the same mistake recurs, the fix belongs in `HIGH_RISK_HINTS`, not in a
one-off correction.

The commonest disagreement runs the other way: `risk_reason` reads
`"tokens" in the body` on an issue about a tokeniser. Risk matches the title and
the body, and no pattern can tell an issue *about* auth from one that mentions
it — a real auth bug often has a neutral title and a single mention in the body,
which is why the score rounds up. Downgrading a mention you have read is a
correct override, not a workaround; it just has to be a reading, not a guess.
Nothing about the vocabulary needs changing for it.
