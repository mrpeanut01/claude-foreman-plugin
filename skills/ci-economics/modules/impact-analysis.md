# Test Impact Analysis

Map a diff to the tests that cover it, so the local gate is fast enough to be
worth running before every push.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ci_profile.py" impact --changed src/auth/session.py
```

```json
{"tests": ["tests/test_session.py"], "complete": true, "recommendation": "run listed tests"}
```

## `complete` is the whole point

| Value | Meaning | Do |
|-------|---------|----|
| `true` | Every changed file mapped to tests, or is documentation | Run the listed tests |
| `false` | At least one file could not be mapped | **Run the full suite** |

An unmapped file is exactly where an unguarded regression hides. Narrowing on a
partial map is how a green local gate ships a break — so `impacted_tests` never
guesses, and the loop never overrides a `false`.

## The mapping rules

1. A changed test file maps to itself.
2. Documentation (`.md`, `.rst`, `.txt`, `.adoc`, or under `docs/`) maps to no
   tests and stays `complete` — docs genuinely have no test coverage to narrow.
3. A source file maps to `tests/**/test_<stem>.*` if such a file exists.
4. Anything else sets `complete: false`.

Rule 3 is convention-based and deliberately shallow: it catches the common case
and admits failure otherwise, rather than building a call graph it would have to
keep accurate.

## Improving the map

Two honest upgrades, in order of effort:

**Path filters you already have.** If workflows carry `paths:` filters, those
encode a mapping the team already agreed on. Read them from the profile
(`jobs[].path_filters`) and prefer them over convention.

**Coverage data if the repo produces it.** `coverage.py --contexts`, `nyc`, or
Jest's `--coverage` can yield a file→test index that is measured rather than
guessed. Regenerate on trunk, not per branch.

Do not build a static import graph. It goes stale silently, and a silently stale
impact map produces exactly the confident-but-wrong narrowing this module exists
to prevent.

## Cost check

If the impacted set takes longer locally than the cheap CI tier would, push and
let CI do it — parallelism is the one thing CI has that the laptop does not. The
rule is about not paying the *expensive* tier to find cheap bugs, not about
refusing CI on principle.
