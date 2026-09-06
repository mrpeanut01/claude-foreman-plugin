---
name: issue-triage
description: Classify, size, risk-score and dedupe GitHub issues into the foreman ledger, applying only labels the repo already defines. Use when triaging an issue queue, deciding whether an issue is actionable, judging duplicates, or scoring risk before work is planned.
---

# Issue Triage

Turn an issue queue into a work queue. This stage writes labels and ledger
records — never code, never comments, never closures.

`scripts/triage.py` holds the deterministic scoring; this skill holds the
judgement. Run the script, then read the issues it scored and correct what it got
wrong. It is blunt on purpose, and blunt-but-consistent beats clever-but-drifting
when the same queue is triaged every day.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py" plan --repo OWNER/NAME --limit 50
```

## Three rules that override everything

**1. Labels come only from the repo's own vocabulary.** `plan_labels` intersects
with `gh label list`. Never invent one, never suggest creating one mid-run.

**2. A false lifecycle label is worse than a missing one.** `needs-repro` costs a
reporter a round trip and reads as dismissal. Apply it only when a bug has no
evidence *and* no stated expectation.

**3. When in doubt, `actionable`.** A wrongly-queued issue gets caught at build
time. A wrongly-parked one sits there until someone notices, which may be never.

## Verdicts

| Verdict | When | Consequence |
|---------|------|-------------|
| `actionable` | Someone could start now | Enters batching |
| `needs-repro` | Bug, no evidence, no stated expectation | Parked on the reporter |
| `needs-info` | Bug that blames an environment it never names | Parked on the reporter |
| `duplicate` | Title overlap ≥ 0.6 with an **open** issue, on two or more words | Linked, not queued |

## What counts as evidence

Any of these makes a bug actionable: a traceback; `File "x.py", line N`; a file
path; a backticked command; numbered steps; an all-caps error code; an HTTP
status. So does an expectation stated under a condition — *"when the file is empty
it suggests deleting it, which should never happen"* is a perfectly good bug
report and must not be asked for steps.

`needs-info` is the other half of that rule, and the narrower half. It is for a
bug that has shown its failure but pins it on something the report does not
give: *"works on my machine"*, *"only on Windows"*, *"since upgrading"*, with no
version, OS or runtime anywhere in the text. Any gesture at a version counts.
Absence of environment detail on its own never does — most bugs do not depend on
one, and asking anyway is a round trip that reads as dismissal.

Never mark a duplicate against a closed issue. Pointing someone at a closed
thread as though it answers them is worse than saying nothing.

One shared word is never a duplicate, however high the ratio. "Bug 1" and
"Bug 2" overlap completely once the digit is discounted, and a duplicate
verdict at 1.0 parks the newer issue until someone reads both.

## Risk drives everything downstream

`high` risk means never batched and never auto-merged. It is set by a protected
path, a security-ish keyword, or a `security`/`critical`/`data-loss` label.

Rounding risk **up** is cheap: the issue still gets fixed, just in a solo PR.
Rounding it down puts an auth change into a five-issue batch that merges itself.

So it rounds up where it can, and `risk_reason` on every record says what it
saw: `protected path src/auth/session.py`, `the security label`, `"token" in
the title`.

The title is authoritative both ways. The body is read more carefully, because
that is where the collisions live: on this repo's own queue eleven of twelve
`high` scores came from a single word in the body — `tokens` about a
tokeniser, `sessions` about agent sessions, `schema` about JSON — and every one
was a collision. An unambiguous word in the body (`password`, `csrf`,
`migration`) scores high on its own. A collision-prone one (`token`, `session`,
`schema`, `permission`, bare `auth`) needs a second, different one beside it,
because an issue *about* auth keeps talking about it and a tokeniser issue
never says `session`. One on its own is a mention, and the record says so:
`"tokens" in the body only — one such word is a mention, not a subject`.
That is the line to read before overriding in either direction.

The low-risk words (`docs`, `test`, `typo`) are read from the title only.
Nothing about a body that mentions a test makes a change safe, and fifteen
issues on this repo's queue were `low` for exactly that.

See [modules/taxonomy.md](modules/taxonomy.md) for the size and risk vocabularies
and how to extend them.

## Exit criteria

Every open issue either has a ledger record, appears in `skipped`, or appears in
`failed` with the reason its labels could not be written. The plan was shown
before anything was applied. No label outside `gh label list` was used.

A non-empty `failed` list is not a partial success to move on from. Nothing in
it was recorded, so the verdicts are still to be applied; report the reason
rather than rerunning into the same wall.
