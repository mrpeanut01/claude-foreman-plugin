# Size and Risk Taxonomy

## Size — how much work, not how urgent

| Size | Weight | Signals |
|------|--------|---------|
| `small` | 1 | typo, bump, rename, readme, changelog, whitespace, lint, spelling; or a body under 120 characters; or exactly one file named, no checkboxes, and a body under 900 |
| `medium` | 2 | Everything else |
| `large` | 4 | redesign, rewrite, refactor, migrate, overhaul, architecture, epic; or 6+ checkboxes; or a body over 2000 characters; or three or more files named in a body over 600 |

Title hints are words almost nobody writes in an issue title, so on their own
they size nothing: every open issue on this repo came out `medium`, and constant
size is constant weight — `max_batch_weight` stops measuring CI cost and just
caps the issue count. The file-count and body-length signals are there because
ordinary reports do carry them.

Files are counted from prose only. A file named inside a fenced block is quoted
evidence — a traceback names five files and none of them is being changed. Two
named files stay `medium`: it could be a move, or a caller and its callee, and
sizing rounds up for the same reason risk does.

Weight feeds `max_batch_weight`. A batch of two mediums (4) and a small (5) fills
the default budget of 5; a single large (4) leaves room for one small.

Size is a rough proxy for how much of a batch's blast radius one issue occupies.
It is not an estimate, and nothing schedules on it.

## Risk — how much damage if it is wrong

| Risk | Batched? | Auto-merged? | Signals |
|------|----------|--------------|---------|
| `low` | yes | yes | docs, tests, comments, formatting, renames |
| `medium` | yes | yes | anything not otherwise classified |
| `high` | **never** | **never** | protected paths; auth (any inflection, en-GB included), token, password, session, credential, permission, migration, schema, payment, billing, secret, csrf, xss, injection, encryption, privilege; or a `security`/`critical`/`data-loss`/`p0` label |

`medium` is the default because most issues are ordinary, and defaulting to
`high` would mean nothing ever batches.

## Extending the vocabularies

The lists live at the top of `scripts/triage.py`. **They are regex fragments, not
plain words** — `_hint_matcher` joins them into `\b(?:…)\b` without escaping.

Add to them when a repo has its own dangerous words — `tenant`, `quota`,
`pricing`, `pii` — rather than correcting the same issue by hand every run. A
correction you make twice belongs in the list.

> **Escape anything that is not a plain word.** A `.` becomes a wildcard, so
> `.env` matches `aXb`. An unbalanced parenthesis raises `re.PatternError` and
> crashes every triage run. Use `re.escape("...")` for literal strings.

Match inflections deliberately, because a risk gate that stops recognising a word
is worse than the substring bug plain matching would cause:

| Want | Write | Not |
|------|-------|-----|
| authentication, unauthorised, Reauthentication | `r"\w*authentic\w*"`, `r"\w*authoris\w*"` | `r"auth\w*"` — matches Author |
| token, tokens | `r"tokens?"` | `r"token"` — misses the plural |
| doc, docs — but never Dockerfile | `r"docs?"` | `r"doc"` with substring matching |

Both spellings, always: this project writes en-GB, so `authorise` and
`authorisation` are what its reporters type.

If the repo defines `risk:low` / `risk:medium` / `risk:high` or `size:*` labels,
they will be applied. If it does not, the scores still land in the ledger and the
labels are silently skipped — the taxonomy is not worth a PR adding ten labels to
someone's repo.

## What the scoring cannot see

It reads the issue text only. It does not know that `src/util/retry.py` is
load-bearing for billing, or that one module has no test coverage at all. That
knowledge belongs in `protected_paths` in config, where it applies mechanically
rather than depending on a model noticing.
