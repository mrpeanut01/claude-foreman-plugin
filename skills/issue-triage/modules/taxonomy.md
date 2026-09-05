# Size and Risk Taxonomy

## Size — how much work, not how urgent

| Size | Weight | Signals |
|------|--------|---------|
| `small` | 1 | typo, bump, rename, readme, changelog, whitespace, lint, spelling; or a body under 120 characters |
| `medium` | 2 | Everything else |
| `large` | 4 | redesign, rewrite, refactor, migrate, overhaul, architecture, epic; or 6+ checkboxes; or a body over 2000 characters |

Weight feeds `max_batch_weight`. A batch of two mediums (4) and a small (5) fills
the default budget of 5; a single large (4) leaves room for one small.

Size is a rough proxy for how much of a batch's blast radius one issue occupies.
It is not an estimate, and nothing schedules on it.

## Risk — how much damage if it is wrong

| Risk | Batched? | Auto-merged? | Signals |
|------|----------|--------------|---------|
| `low` | yes | yes | docs, tests, comments, formatting, renames |
| `medium` | yes | yes | anything not otherwise classified |
| `high` | **never** | **never** | protected paths; auth, token, session, credential, permission, migration, schema, payment, billing, secret, csrf, xss, injection, encryption; or a `security`/`critical`/`data-loss`/`p0` label |

`medium` is the default because most issues are ordinary, and defaulting to
`high` would mean nothing ever batches.

## Extending the vocabularies

Both live at the top of `scripts/triage.py` as plain tuples. Add to them when a
repo has its own dangerous words — `tenant`, `quota`, `pricing`, `pii` — rather
than correcting the same issue by hand every run. A correction you make twice
belongs in the list.

If the repo defines `risk:low` / `risk:medium` / `risk:high` or `size:*` labels,
they will be applied. If it does not, the scores still land in the ledger and the
labels are silently skipped — the taxonomy is not worth a PR adding ten labels to
someone's repo.

## What the scoring cannot see

It reads the issue text only. It does not know that `src/util/retry.py` is
load-bearing for billing, or that one module has no test coverage at all. That
knowledge belongs in `protected_paths` in config, where it applies mechanically
rather than depending on a model noticing.
