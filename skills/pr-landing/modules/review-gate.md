# The Review Gate

The merge gate is an agent review. The design problem is that **an agent
reviewing an agent's work drifts toward approval** — approving is agreeable,
cheap, and produces no argument. Everything here exists to make `clean` expensive.

## Dispatching

Send the `reviewer` agent exactly three things:

1. The diff (`gh pr diff <n>`).
2. The text of the issues the batch claims to fix.
3. Repo conventions — `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`.

**Not** the builder's transcript or reasoning. If you include the PR description,
mark it as the author's claim. A reviewer that has read the builder's
justification is reviewing the justification, not the code.

## Validating what comes back

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/land.py" verdict --file /tmp/verdict.json
```

`clean` requires all of:

| Requirement | Rejected when missing because |
|-------------|-------------------------------|
| `tests_covering` naming real test IDs | "The suite passes" is not evidence this change is covered |
| `revert_check: failed_as_expected` | A test that passes with the fix reverted guards nothing |
| No `high`/`medium` finding | A clean verdict carrying a serious finding contradicts itself |

Only `behaviour_change: false` — documentation, comments — may skip the first two.

The validator is mechanical on purpose. When it rejects a verdict, return the
error list to the reviewer verbatim and ask for the missing evidence. Recording
it anyway defeats the only gate standing between the loop and trunk.

## The revert check

```bash
git stash push -- <source files, not test files>
pytest <the covering test>     # must FAIL
git stash pop
```

The most valuable thing the reviewer does, because it is a fact rather than a
judgement. It catches the most common failure of autonomous building: a test that
asserts what the code does rather than what the issue asked for.

## Two lenses for risky changes

When batch risk is `high`, or the diff touches `protected_paths`, run two
reviewers — one for correctness, one for blast radius and security — and require
both clean. Two reviews cost less than one bad merge into an auth path.

## Rounds

**Escalate when findings repeat, not when rounds elapse.**

`land.review_stalled(rounds, hard_ceiling)` compares the blocking findings of the
last two rounds. A finding that survives a round — same file, same severity
band, the same complaint reworded — means the builder and reviewer are trading
the same objection, and another round will trade it again. That is deadlock, and it
escalates.

*The same complaint reworded* means one summary reads as an elaboration of the
other. Two summaries that each name something the other does not are two defects
in one file, not one defect repeated: "missing null check in parse_config" and
"missing type check in parse_config" share most of their words and describe
unrelated bugs, and escalating that pair would call a converging PR a deadlock.

Different findings each round is the opposite: the reviewer is working through
layers, and quality is rising. Let it run.

> This rule replaced a flat cap of 2, which was justified as *"a third round of an
> agent negotiating with an agent is not going to converge."* Dogfooding showed
> that rounds elapsed does not measure that at all. Two rounds on this repo's own
> first PR found entirely different real defects and the cap fired anyway, on a
> case its own rationale did not describe.

`caps.review_rounds` (default 5) remains as a **hard ceiling** — a runaway bound
for an unattended loop, not the convergence test. A repeated `low` finding never
stalls anything, since a low finding never blocked a clean verdict to begin with.

Every push resets both gates, so a re-review is a full re-review — a stale clean
verdict never carries over to new code.

## Measuring the gate

The reviewer cannot be fully verified up front. What can be measured is the
outcome: clean reviews whose merges were later reverted.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" append --type merge.reverted --json '{"batch":"b-001"}'
```

`/foreman:status` reports the ratio and warns above 10%. A rising number means the
gate is rubber-stamping — treat that as a defect in the reviewer prompt, not as
noise.
