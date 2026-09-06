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

*The same complaint reworded* is decided by shape, not by how much the two
summaries share. Half the content words of any two findings in one file are the
locus and the verb, so overlap alone ranks them backwards: "missing null check in
parse_config" and "missing type check in parse_config" name unrelated bugs and
overlap *more* (0.67) than a genuine rewording does (0.50). What marks that pair
out is that one is the other with a term swapped in place — same sentence, same
length, the swapped term carrying the whole complaint. A rewording never keeps
the sentence; it elaborates, compresses or reorders, so something is always
inserted or dropped. "unbounded retry loop in the fetch helper" and "the retry
loop in fetch has no ceiling" are one objection stated twice, and count as a
repeat.

The rule is lexical, and it has a floor. Two statements of one objection that
share too few words — "race condition in flush when the queue is empty" and
"flush races with the queue drain on an empty buffer" overlap 0.30 — read as
progress; so does a reviewer alternating between two files, which gets no locus
signal either. Both fall through to the ceiling of 5 rounds. In the other
direction, a genuinely new defect named by swapping a term *and* adding a locator
reads as a repeat and escalates to a human a round early. That is the affordable
error of the two. A reviewer that restates a surviving finding in the words it
used before is what keeps this rule sharp.

Different findings each round is the opposite: the reviewer is working through
layers, and quality is rising. Let it run — unless they keep landing in the same
place.

### The same place, round after round

Repeated wording is one deadlock signal; a repeated **locus** is a stronger one.
Three consecutive rounds each carrying a blocking finding in the same file
escalates, whatever the summaries say.

This is the arc that produced the rule, on this repo, in one function:

| Round | Finding | Direction |
|-------|---------|-----------|
| 2 | empty check list read as `full_green` | merges early |
| 3 | required every declared job, hangs on unreportable ones | never merges |
| 4 | running job invisible; workflow-level paths disabled the requirement | merges early |
| 5 | branches/tags/paths-ignore unmodelled | both |

Every round named a real and different defect, so textually it read as progress.
But the same code had been wrong four times running in alternating directions,
which is not a sequence of bugs — it is one missing model. Two rounds on a file
is ordinary iteration; three says stop patching and write the model down, which
is what a human called here. The spec table in `tests/test_gate_spec.py` is what
came out of doing that.

The locus is the file, because a file is what a finding records. If your
reviewer reports a symbol too, the same argument applies one level finer.

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
