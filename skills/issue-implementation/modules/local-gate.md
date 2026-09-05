# The Local Gate

The rule: **never spend CI to learn what a laptop already knows.**

On a repo with a 40-minute suite, a failure caught locally costs seconds and a
failure caught in CI costs a suite run plus a round trip. Four batches a day
versus one is decided almost entirely here.

## What runs

| Step | Command shape | Catches |
|------|---------------|---------|
| Impacted tests | `pytest <listed>` or the full suite | Logic errors, the bug not actually fixed |
| Lint | `ruff check` / `eslint` | Style failures that fail CI for nothing |
| Types | `mypy` / `tsc --noEmit` | Signature drift |
| Format | `ruff format --check` / `prettier --check` | The single most annoying way to lose a suite run |

Take the actual commands from the repo — `Makefile`, `package.json` scripts,
`.pre-commit-config.yaml`, or the cheap-tier jobs in `.foreman/ci-profile.json`.
Guessing produces a gate that passes locally and fails in CI, which is the worst
of both.

## Reading the impact map

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed src/upload.py
{"tests": ["tests/test_upload.py"], "complete": true, "recommendation": "run listed tests"}
```

`complete: false` means at least one changed file mapped to nothing. Run
everything. This is not a heuristic to tune — it is the one place where being
wrong is silent, because a narrowed green suite looks exactly like a real one.

## When the impacted set is slower than CI's cheap tier

Push and let CI run it. CI has parallelism a laptop does not. The rule is about
not paying the **expensive** tier to find cheap bugs, not about refusing CI.

Compare against `cheap_tier_s` in the profile, not against your patience.

## Improving the map

If workflows carry `paths:` filters, they encode a mapping the team already
agreed on — prefer those over convention. If the repo produces coverage contexts
(`coverage.py --contexts`, `nyc`, Jest `--coverage`), a measured file→test index
beats any convention.

Do not build a static import graph. It goes stale silently, and a silently stale
impact map produces confident wrong narrowing — the exact failure this gate exists
to prevent.
