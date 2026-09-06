#!/usr/bin/env python3
"""Landing: read CI honestly, judge the reviewer, and refuse to merge on doubt.

Three deterministic decisions live here, because each one is a place where a
plausible-sounding judgement call would quietly cost you something:

  * which checks are worth waiting for (waiting on a human gate stalls the loop);
  * whether a `clean` review carries the evidence a clean review requires;
  * whether a failure is a flake or a bug (rerunning a bug hides it).

CLI:
    land.py checks   --pr N --repo OWNER/NAME [--sha SHA] [--profile .foreman/ci-profile.json]
    land.py verdict  --file verdict.json
    land.py blockers --batch b-001 [--ledger .foreman] [--config .foreman/config.json]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger as ledger_mod  # noqa: E402
from ci_profile import attribute  # noqa: E402
from globs import compile_glob, matches_any  # noqa: E402
from ledger import PROGRESS_COUNTERS  # noqa: E402

PASSED = {"SUCCESS", "PASS", "NEUTRAL", "SKIPPED"}
# A cancelled check is not a pass. Leaving it in neither set parks it in
# actionable_pending forever, which is a hang.
FAILED = {
    "FAILURE",
    "FAIL",
    "ERROR",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "CANCELLED",
    "CANCELED",
    # Terminal: a stale result is never recomputed, so it must not sit in
    # pending waiting for a resolution that cannot come.
    "STALE",
}

# A pending entry matching any of these needs a person, so the loop must not
# sit waiting on it.
HUMAN_GATE = re.compile(
    r"(review\s+required|required\s+review|requires\s+review|approval\s+required"
    r"|waiting\s+for\s+approval|manual\s+approval|awaiting\s+approval)",
    re.I,
)

BLOCKING_LABELS = {"needs-human", "do-not-merge", "wip", "blocked", "hold"}

VERDICTS = {"clean", "changes_requested"}
SERIOUS = {"high", "medium", "critical", "blocker"}
FLAKE_CONFIDENCE = 0.7

CLEAR = {"ci": "full_green", "review": "clean"}

# Events that can produce a check on a pull request. A job wired to anything else
# will never report here, and requiring it would hang the gate forever.
# merge_group checks report against the merge queue ref, not the pull request,
# so a merge_group-only job never reports here.
PR_EVENTS = {"pull_request", "pull_request_target", "push"}

# Two reviewers disagreeing forever is deadlock; two reviewers finding different
# things each round is progress. Only the first is worth escalating on.
REVIEW_MATCH_THRESHOLD = 0.5
DEFAULT_REVIEW_CEILING = 5
# Consecutive rounds carrying a blocking finding in the same file before that
# counts as deadlock on its own. Two is ordinary iteration — a reviewer reading
# one file twice. Three says the fixes are not converging on a correct model of
# that code, and it has to fire below the hard ceiling to say something more
# useful than "you ran out of rounds".
LOCUS_REPEAT_ROUNDS = 3
_WORD = re.compile(r"[a-z0-9]+")
# Words a reviewer exchanges for one another without meaning anything new by
# it. This is the whole list, and it is kept to pairs that are synonyms in
# every sentence a finding is written in. What is NOT here matters more than
# what is: "null" and "type", "read" and "write", "before" and "after" name
# different defects when swapped in place, and the substitution rule below
# depends on them staying different (issues #29, #79).
_SYNONYMS = {
    "unlimited": "unbounded",
    "uncapped": "unbounded",
    "unbound": "unbounded",
    "infinite": "unbounded",
    "endless": "unbounded",
    "absent": "missing",
    "omitted": "missing",
    "lacking": "missing",
    "lack": "missing",
    "unhandled": "uncaught",
    "incorrect": "wrong",
    "quietly": "silently",
    "quiet": "silent",
}
_NOISE = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "no",
    "not",
    "at",
    "in",
    "on",
    "of",
    "to",
    "and",
    "or",
    "it",
    "its",
    "this",
    "that",
    "still",
    "all",
    "has",
    "have",
}


# --- reading the check list ---------------------------------------------------


PR_TRIGGERS = ("pull_request", "pull_request_target")
FILTER_KEYS = ("paths", "paths_ignore", "branches", "tags")
# Activity types that fire on EVERY pull request, which is what requirable means.
# GitHub's default when `types:` is omitted is [opened, synchronize, reopened],
# and `opened` is the member of it that every pull request necessarily raises.
#
# The near misses matter more than the obvious ones. `closed` and `labeled` never
# fire on an open PR at all, but `ready_for_review` and `edited` do — just not on
# every PR. `on: pull_request: types: [ready_for_review]` is the standard way to
# hold an expensive E2E suite until a PR leaves draft; foreman opens non-draft
# PRs and edits nothing, so no check is ever created and requiring one hangs the
# gate until `stale_after_s` escalates it. `synchronize` has the same shape: it
# fires on the pushes after creation, which a PR that is right first time never
# gets.
PR_UNCONDITIONAL_TYPES = {"opened"}


def _branch_allows(branch: str, patterns: list[str]) -> bool:
    """GitHub branch-filter semantics: `!` excludes, and `*` does not cross `/`.

    fnmatch does neither — it reads `!main` as a literal that never matches, and
    since inclusion is an any() test, an ignored exclusion reads as permitted.
    """
    positives = [p for p in patterns if not p.startswith("!")]
    negatives = [p[1:] for p in patterns if p.startswith("!")]
    if positives and not matches_any(branch, positives):
        return False
    return not any(compile_glob(p).match(branch) for p in negatives)


def _unconditional(
    cfg: dict, base_branch: str | None = None, resolve_branches: bool = False
) -> bool:
    """Whether this trigger fires on every pull request into `base_branch`.

    `branches:` on a pull_request trigger matches the PR's BASE, so a matching
    base means it is no restriction at all. On a `push` trigger it matches the
    HEAD — the PR's feature branch — so it stays a restriction whatever the base
    is, and resolving it against the base marks jobs requirable that can never
    report. Callers say which meaning applies via `resolve_branches`.
    """
    if any(cfg.get(key) for key in ("paths", "paths_ignore", "tags", "tags_ignore")):
        return False

    branches = list(cfg.get("branches") or [])
    branches += [f"!{p}" for p in (cfg.get("branches_ignore") or [])]
    if branches:
        if not resolve_branches or base_branch is None:
            return False
        if not _branch_allows(base_branch, branches):
            return False

    types = cfg.get("types")
    if types and not (set(types) & PR_UNCONDITIONAL_TYPES):
        # e.g. types: [closed], which never fires while the PR is open, or
        # types: [ready_for_review], which fires for some PRs but not this one.
        return False
    return True


def _legacy_can_report(spec: dict) -> bool:
    """Profiles written before per-event data. Filters are known only in union."""
    if not (PR_EVENTS & set(spec.get("triggers") or [])):
        return False
    conditional = spec.get("pr_path_filters")
    if conditional is None:
        conditional = spec.get("path_filters")
    return not conditional


def can_report_on_pr(spec: dict, base_branch: str | None = None) -> bool:
    """Whether this job will produce a check on every pull request.

    Requirable means *unconditional*: some trigger fires on a PR with no filter
    attached. Anything narrower — paths, paths-ignore, branches, tags — is
    conditional and counts only once it actually reports.

    Being conservative here is safe in both directions, which is the point.
    Excluding a job that would have reported cannot cause an early merge,
    because a job that reports lands in `actionable_pending` while it runs and
    in `failed` if it fails. Including a job that can never report is what hangs
    the gate forever.
    """
    if "${{" in str(spec.get("display") or ""):
        return False  # a templated name cannot be attributed back to this job
    events = spec.get("events")
    if events is None:
        return _legacy_can_report(spec)
    for name in PR_TRIGGERS:
        cfg = events.get(name)
        if isinstance(cfg, dict) and _unconditional(cfg, base_branch, resolve_branches=True):
            return True
    push = events.get("push")
    if isinstance(push, dict) and _unconditional(push):
        return True
    return False


# Retained for callers that predate the rename.
_can_report = can_report_on_pr


# A check result names the commit it ran against. Anything shorter than this is
# not enough of a SHA to identify one.
MIN_SHA_PREFIX = 7


def describes_commit(entry: dict, expected_sha: str) -> bool:
    """Whether this check result provably ran against `expected_sha`.

    A result that names no commit is not evidence about this one. `gh pr checks`
    reports whatever the pull request last had, so between a push and the new
    workflow run registering it returns the PREVIOUS commit's results — which is
    the whole hazard this predicate exists to close.
    """
    reported = str(entry.get("head_sha") or entry.get("headSha") or "")
    if len(reported) < MIN_SHA_PREFIX or len(expected_sha or "") < MIN_SHA_PREFIX:
        return False
    # Either side may be abbreviated: `git rev-parse --short HEAD` on one side,
    # a full API SHA on the other.
    return reported.startswith(expected_sha) or expected_sha.startswith(reported)


def checks_for_sha(checks: list[dict], expected_sha: str | None) -> list[dict]:
    """The checks that describe `expected_sha`; all of them when no SHA is given."""
    if not expected_sha:
        return list(checks or [])
    return [c for c in (checks or []) if describes_commit(c, expected_sha)]


def _shapes(profile: dict) -> list[dict]:
    """The job list `ci_profile.attribute` wants: job key plus declared display name."""
    return [
        {"name": name, "display": (spec or {}).get("display")}
        for name, spec in (profile.get("jobs") or {}).items()
    ]


def _job_for(name: str, profile: dict, shapes: list[dict] | None = None) -> dict:
    """The profile entry for a reported check name, or {} when nothing matches.

    GitHub reports matrix cells (`test (3.11)`) and reusable-workflow legs
    (`caller / called`), while the profile is keyed by the workflow's job key
    (`test`). Indexing `jobs` with the reported name therefore misses on exactly
    the repos this system is for. `ci_profile.attribute` already reverses those
    display forms, and returns None rather than guessing.
    """
    jobs = profile.get("jobs") or {}
    if name in jobs:
        return jobs[name] or {}
    key = attribute(name, _shapes(profile) if shapes is None else shapes)
    return (jobs.get(key) or {}) if key else {}


def _is_advisory(name: str, profile: dict, shapes: list[dict] | None = None) -> bool:
    """Unknown checks count as required: an unknown gate may block the queue.

    When branch protection could not be read, nothing is known to be advisory.
    Treating an unread protection API as "nothing is required" turns a fully red
    CI into a green gate, which is the one outcome this whole system exists to
    prevent.
    """
    if not profile.get("protection_known", False):
        return False
    if name in set(profile.get("required_checks") or []):
        # Protection names this exact context. The job's own `required` flag is
        # computed by matching the job KEY against those contexts, so a matrix
        # job whose cells are required still reads required=False. Believing the
        # flag over the context list would file a failing required cell as
        # advisory, and a red gate would read green.
        return False
    job = _job_for(name, profile, shapes)
    return bool(job) and job.get("required") is False


def classify_checks(checks: list[dict], profile: dict, expected_sha: str | None = None) -> dict:
    shapes = _shapes(profile)
    summary = {
        "passed": [],
        "failed": [],
        "advisory_failed": [],
        "actionable_pending": [],
        "advisory_pending": [],
        "human_gate_pending": [],
        "pending": [],
        "stale": [],
    }
    for entry in checks or []:
        name = entry.get("name", "")
        if expected_sha and not describes_commit(entry, expected_sha):
            # Belongs to another commit, or names none at all. Counting it in
            # either direction lets CI that never saw this code decide its gate.
            summary["stale"].append(name)
            continue
        state = (entry.get("state") or entry.get("bucket") or "").upper()
        advisory = _is_advisory(name, profile, shapes)

        if state in PASSED:
            summary["passed"].append(name)
        elif state in FAILED:
            summary["advisory_failed" if advisory else "failed"].append(name)
        else:
            summary["pending"].append(name)
            haystack = f"{name} {entry.get('description', '')} {entry.get('workflow', '')}"
            if HUMAN_GATE.search(haystack):
                summary["human_gate_pending"].append(name)
            elif advisory:
                summary["advisory_pending"].append(name)
            else:
                summary["actionable_pending"].append(name)
    return summary


def ci_gate(
    checks: list[dict],
    profile: dict,
    base_branch: str | None = None,
    expected_sha: str | None = None,
) -> str:
    """Translate a check list into the ledger's ci_gate value.

    `expected_sha` is the commit this verdict is about. A gate verdict is a
    statement about one commit — that is why the ledger resets both gates on
    `batch.pushed` — so results that cannot be shown to describe that commit are
    dropped before anything is judged. What is left may be nothing, and nothing
    is `pending`: honest ignorance, never green.
    """
    scoped = checks_for_sha(checks, expected_sha)
    # A result naming another commit is not evidence about this one — but that it
    # exists at all is evidence that this pull request runs CI, and that this
    # commit's own results are still to come.
    dropped = len(scoped) != len(checks or [])
    checks = scoped
    summary = classify_checks(checks, profile)
    if summary["failed"]:
        return "failed"

    required = list(profile.get("required_checks") or [])
    if not required:
        # Both branches below need the same two sets: what this repo declares,
        # and which of those declarations have reported on this commit. Whether
        # protection could be read changes what is *required*; it changes
        # nothing about what counts as CI having spoken.
        declared = profile.get("jobs") or {}
        shapes = _shapes(profile)
        reported = {attribute(c.get("name", ""), shapes) for c in (checks or [])}

        if not profile.get("protection_known", False):
            # Nothing is known to be optional, so decide from what the workflows
            # declare plus what has actually reported.
            requirable = {n: s for n, s in declared.items() if can_report_on_pr(s, base_branch)}
            covered = {attribute(name, shapes) for name in summary["passed"]}

            if requirable:
                if not all(n in covered for n in requirable):
                    return "pending"  # a job that always runs has not reported yet
            elif not (reported & set(declared)):
                # No unconditional job to wait for, and nothing this repo declares
                # has reported. A DCO bot or a preview deploy going green says
                # nothing about whether CI ran. That is ignorance, not success.
                return "pending"

            # Anything that did report counts, requirable or not.
            return "full_green" if not summary["actionable_pending"] else "pending"

        # Protection says nothing is required, so nothing can block a merge.
        # "Nothing blocks" is not "CI has already spoken", though. Until this
        # commit's own results arrive — every result so far belonged to another
        # commit, or none has arrived in the window after a push — a repo that
        # runs CI at all has simply not reported yet. Reading that as green is
        # how a batch merges past a suite that never ran, and it is strictly
        # worse than the `failed` the dropped stale red used to produce.
        #
        # What ends the wait is a result from a job this repo declares, the same
        # test the branch above applies. Counting any check at all is what a
        # DCO status or a preview deploy satisfies within a second of the push,
        # while every declared job is still missing. With nothing declared there
        # is no such list to check against, so any result for this commit is the
        # only evidence there can be. Nothing declared and nothing dropped is
        # the one case with nothing to wait for: a repo with no CI, which
        # protection agrees requires nothing.
        spoken = bool(reported & set(declared)) if declared else bool(checks)
        if (declared or dropped) and not spoken:
            return "pending"
        return "full_green" if not summary["actionable_pending"] else "pending"

    done = set(summary["passed"])
    # Required checks are protection contexts — the names GitHub reports — so
    # their tier lives under the job that declared them, not under the context.
    # Reading the tier off the context directly finds nothing for every matrix
    # cell, the expensive job then counts as cheap, and cheap_green collapses
    # into full_green: the expensive tier gets launched on every push, which is
    # the whole saving the ladder exists to make.
    shapes = _shapes(profile)
    expensive = [n for n in required if _job_for(n, profile, shapes).get("tier") == "expensive"]
    cheap = [n for n in required if n not in expensive]

    if all(n in done for n in required):
        return "full_green"
    if cheap and all(n in done for n in cheap):
        return "cheap_green"
    return "pending"


# --- judging the reviewer -----------------------------------------------------


def validate_review(verdict: dict) -> tuple[bool, list[str]]:
    """A clean verdict must carry its evidence, or it is not a clean verdict.

    An agent reviewing an agent's work drifts toward approval. The counter is to
    make `clean` mechanically expensive: name the covering tests, and show that
    reverting the fix makes one of them fail. Both are facts a reviewer cannot
    produce by being agreeable.
    """
    errors: list[str] = []
    name = verdict.get("verdict")
    if name not in VERDICTS:
        return False, [f"verdict must be one of {sorted(VERDICTS)}, got {name!r}"]

    findings = verdict.get("findings") or []
    serious = [f for f in findings if str(f.get("severity", "")).lower() in SERIOUS]

    if name == "changes_requested":
        if not findings:
            errors.append("changes_requested must carry at least one finding saying what to change")
        return not errors, errors

    if serious:
        errors.append(
            f"a clean verdict cannot carry {len(serious)} finding(s) of "
            f"severity {sorted({str(f.get('severity')).lower() for f in serious})}"
        )

    if verdict.get("behaviour_change", True):
        if not verdict.get("tests_covering"):
            errors.append("clean requires tests_covering: name the tests that exercise this change")
        if verdict.get("revert_check") != "failed_as_expected":
            errors.append(
                f"clean requires revert_check=failed_as_expected (got "
                f"{verdict.get('revert_check')!r}): revert the fix, keep the test, "
                "and show it fails"
            )
    elif verdict.get("revert_check") == "still_passed":
        errors.append("revert_check=still_passed contradicts behaviour_change=false")

    return not errors, errors


# --- review convergence -------------------------------------------------------


def _stem(word: str) -> str:
    """Plural to singular, and nothing subtler.

    `loops` and `loop` are one word; so are `retries` and `retry`. A fuller
    stemmer would fold `missing` into `miss` and `parsing` into `parse`, which
    starts merging words that mean different things, and the substitution rule
    needs different words to stay different.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _canon(word: str) -> str:
    """One spelling per meaning, as far as a table and a plural rule can get.

    The substitution rule reads an in-place swap as a new defect, which is
    right for `null` -> `type` and wrong for `unbounded` -> `unlimited`: the
    second preserves term count and swaps one word in place, so it is
    structurally identical to a genuine swap, and only the words themselves
    tell the two apart (issue #79). Normalising here lets that rule stay
    exactly as strict as it is while no longer counting a synonym or a plural
    as a swap.
    """
    stemmed = _stem(word)
    return _SYNONYMS.get(stemmed, _SYNONYMS.get(word, stemmed))


def _terms(text: str) -> list[str]:
    """The content words of a summary, in order, first occurrence only."""
    seen: dict[str, None] = {}
    for word in _WORD.findall((text or "").lower()):
        if word not in _NOISE and len(word) > 1:
            seen.setdefault(_canon(word), None)
    return list(seen)


def _words(text: str) -> set[str]:
    return set(_terms(text))


def _substitution(a: list[str], b: list[str]) -> bool:
    """Whether one summary is the other with terms swapped in place.

    A swap keeps the sentence and exchanges words inside it, so the sequences
    align position for position with nothing inserted and nothing dropped. A
    rewording does not keep the sentence: it elaborates, compresses, reorders,
    swaps filler for filler, and its alignment always carries an insertion or a
    deletion somewhere.
    """
    if len(a) != len(b):
        return False
    # SequenceMatcher does not promise the same opcodes both ways round, and a
    # rule that depends on which round was passed first is not a rule.
    left, right = sorted((a, b))
    ops = difflib.SequenceMatcher(a=left, b=right, autojunk=False).get_opcodes()
    return all(tag in ("equal", "replace") for tag, *_ in ops)


def same_finding(a: dict, b: dict, threshold: float = REVIEW_MATCH_THRESHOLD) -> bool:
    """Whether two findings are the same complaint, allowing for rewording."""
    if a.get("file") != b.get("file"):
        return False
    # Compare bands, not exact labels: a complaint drifting high <-> medium is the
    # same complaint, and both block a clean verdict.
    if (str(a.get("severity", "")).lower() in SERIOUS) != (
        str(b.get("severity", "")).lower() in SERIOUS
    ):
        return False
    ta, tb = _terms(a.get("summary")), _terms(b.get("summary"))
    wa, wb = set(ta), set(tb)
    if not wa or not wb:
        return False
    if len(wa & wb) / len(wa | wb) < threshold:
        return False
    if not (wa - wb) or not (wb - wa):
        return True  # one summary's words contain the other's: an elaboration

    # Overlap alone cannot finish the job, and no threshold can. Two summaries
    # about one file share their locus and their verb for free, so "missing null
    # check in parse_config" and "missing type check in parse_config" name
    # unrelated defects while overlapping MORE (0.67) than a genuine rewording
    # does (0.50). Ranking by similarity puts them in the wrong order.
    #
    # Shape separates them where size cannot. Those two are one sentence with a
    # term swapped in place, and the swapped term is the entire content of the
    # complaint. Requiring containment instead — the first cut at this — rejected
    # the swap but took every reworded repeat with it, because a reviewer
    # restating an objection both adds and drops words: "unbounded retry loop in
    # the fetch helper" and "the retry loop in fetch has no ceiling" are one
    # objection twice, and read as two rounds of progress.
    #
    # The bound worth knowing: a new defect named by swapping a term AND adding
    # a locator ("...in parse_config on line 12") is structurally a rewording and
    # reads as a repeat. That escalates a converging review to a human early,
    # which is the affordable direction — the other one merges on a defect
    # nobody looked at twice.
    return not _substitution(ta, tb)


def _blocking(round_findings: list[dict]) -> list[dict]:
    """Only findings that stood between the batch and a merge."""
    return [f for f in round_findings or [] if str(f.get("severity", "")).lower() in SERIOUS]


def _repeated_locus(rounds: list[list[dict]], span: int) -> str | None:
    """A file that carried a blocking finding in each of the last `span` rounds.

    Repeated wording is one deadlock signal; repeated *locus* is a stronger one.
    A builder and reviewer can name a genuinely different defect in the same
    function every round, in alternating directions, and never once repeat
    themselves textually — while the thing being described is a model of that
    code that neither of them has right. A human watching that arc stops the
    patching and asks for the spec; this is the rule that does the same.
    """
    if span < 1 or len(rounds) < span:
        return None
    recent = [{f.get("file") for f in _blocking(r) if f.get("file")} for r in rounds[-span:]]
    if not all(recent):
        return None  # a round with no blocking finding at all breaks the run
    shared = set.intersection(*recent)
    return sorted(shared)[0] if shared else None


def review_stalled(
    rounds: list[list[dict]],
    hard_ceiling: int = DEFAULT_REVIEW_CEILING,
    locus_span: int = LOCUS_REPEAT_ROUNDS,
) -> str | None:
    """Reason to stop reviewing, or None to allow another round.

    Rounds elapsed is the wrong measure. A builder and reviewer who surface a
    different real defect each round are converging, however many rounds it
    takes; two who trade the same finding are not, and two who keep being wrong
    about the same code are not either. Only blocking severities count — a
    repeated `low` never stood between the batch and a merge.
    """
    if len(rounds) >= hard_ceiling:
        return f"review reached the hard ceiling of {hard_ceiling} rounds"
    if len(rounds) < 2:
        return None

    previous, current = _blocking(rounds[-2]), _blocking(rounds[-1])
    for now in current:
        if any(same_finding(before, now) for before in previous):
            return (
                f"the same finding survived a round (repeat): "
                f"{now.get('file')} - {str(now.get('summary', ''))[:80]}"
            )

    locus = _repeated_locus(rounds, locus_span)
    if locus:
        return (
            f"{locus_span} rounds running have found a blocking defect in the same "
            f"place (locus): {locus}. The fixes are not converging on a correct "
            "model of that code - stop patching and write the model down."
        )
    return None


# --- flake or bug -------------------------------------------------------------


def flake_decision(classification: dict, batch: dict, config: dict) -> str:
    """rerun | fix | escalate. When unsure, fix — a rerun can hide a real bug."""
    if not classification.get("is_flaky"):
        return "fix"
    confidence = classification.get("confidence")
    if confidence is None or confidence < FLAKE_CONFIDENCE:
        return "fix"
    cap = (config.get("caps") or {}).get("reruns", 2)
    if (batch.get("attempts") or {}).get("reruns", 0) >= cap:
        return "escalate"
    return "rerun"


# --- the merge decision -------------------------------------------------------


def merge_blockers(batch: dict, pr: dict, config: dict) -> list[str]:
    """Everything standing between this batch and a merge, reported at once."""
    blockers: list[str] = []

    if not config.get("auto_merge"):
        blockers.append("auto_merge is disabled in config")

    for gate, wanted in CLEAR.items():
        actual = batch.get(f"{gate}_gate")
        if actual != wanted:
            blockers.append(f"{gate} gate is {actual}, needs {wanted}")

    held = sorted(BLOCKING_LABELS & {str(item).lower() for item in (pr.get("labels") or [])})
    for label in held:
        blockers.append(f"PR carries the {label} label")

    for path in batch.get("paths") or []:
        pattern = matches_any(path, config.get("protected_paths"))
        if pattern:
            blockers.append(f"{path} is protected by {pattern}; never auto-merged")

    caps = config.get("caps") or {}
    attempts = batch.get("attempts") or {}
    for counter, cap in caps.items():
        # `caps` holds two kinds of number. The runaway ceilings count events
        # elapsed and are a standing verdict on the batch. The progress counters
        # measure whether it is converging, and each already has its own rule
        # (`futile_push_run`, `stalled_build`) that decides when to escalate and
        # when the question has stopped applying — which is why `cap_breached`
        # skips them too. Reading them here asks a converging question at the
        # one moment convergence has been settled: a batch that reached
        # `full_green` and a clean review converged, whatever it cost to get
        # there. Nothing resets `build_resumes` (the record of the interruptions
        # is meant to stand), so counting it here blocked such a batch for the
        # rest of its life, and requeueing could not clear it either.
        if counter in PROGRESS_COUNTERS:
            continue
        if attempts.get(counter, 0) >= cap:
            blockers.append(f"{counter} at cap ({attempts[counter]}/{cap})")

    return blockers


# --- gh plumbing --------------------------------------------------------------


def _gh_json(args: list[str]):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _from_check_run(run: dict, sha: str) -> dict:
    """One check run in the shape the classifier reads."""
    return {
        "name": run.get("name") or "",
        # A run still in flight has no conclusion yet; its status is the state.
        "state": str(run.get("conclusion") or run.get("status") or "").upper(),
        "description": (run.get("output") or {}).get("title") or "",
        "workflow": (run.get("check_suite") or {}).get("workflow_name") or "",
        "link": run.get("html_url") or "",
        "head_sha": run.get("head_sha") or sha,
    }


def _from_status(status: dict, sha: str) -> dict:
    """One legacy commit status — external CI that never moved to check runs.

    `gh pr checks` reports these alongside check runs, so reading only check runs
    would drop a required context and hang the gate.
    """
    return {
        "name": status.get("context") or "",
        "state": str(status.get("state") or "").upper(),
        "description": status.get("description") or "",
        "workflow": "",
        "link": status.get("target_url") or "",
        "head_sha": sha,
    }


def fetch_checks(repo: str, pr: int, sha: str | None = None) -> list[dict]:
    """Check results for this pull request, tagged with the commit they describe.

    Given a `sha`, both reads are SHA-addressed, so anything that comes back
    provably ran against that commit. `gh pr checks` cannot say that: its output
    carries no head SHA, and in the window between a push and the new run
    registering it returns the previous commit's results.
    """
    if sha:
        runs = _gh_json(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"]) or {}
        combined = _gh_json(["api", f"repos/{repo}/commits/{sha}/status?per_page=100"]) or {}
        return [
            *[_from_check_run(run, sha) for run in (runs.get("check_runs") or [])],
            *[_from_status(st, sha) for st in (combined.get("statuses") or [])],
        ]

    raw = _gh_json(
        [
            "pr",
            "checks",
            str(pr),
            "--repo",
            repo,
            "--json",
            "name,state,bucket,description,workflow,link",
        ]
    )
    return raw if isinstance(raw, list) else []


def fetch_pr(repo: str, pr: int) -> dict:
    raw = (
        _gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "number,labels,isDraft,mergeable,reviewDecision,url,baseRefName,headRefOid",
            ]
        )
        or {}
    )
    raw["labels"] = [
        item["name"] if isinstance(item, dict) else item for item in raw.get("labels", [])
    ]
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("checks")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo", required=True)
    p.add_argument(
        "--profile",
        help=f"CI profile (default {ledger_mod.LEDGER_DIR}/{ledger_mod.PROFILE_FILE} "
        "in the repository root)",
    )
    p.add_argument(
        "--sha",
        help="the commit this gate is about; defaults to the PR's current head",
    )
    p = sub.add_parser("verdict")
    p.add_argument("--file", required=True)
    p = sub.add_parser("blockers")
    p.add_argument("--batch", required=True)
    p.add_argument("--pr", type=int)
    p.add_argument("--repo")
    p.add_argument("--ledger", default=".foreman")
    p.add_argument(
        "--config",
        help="foreman config (default .foreman/config.json in the repository root)",
    )

    args = parser.parse_args(argv)

    def load_json(path, default):
        return json.loads(Path(path).read_text()) if Path(path).exists() else default

    if args.cmd == "checks":
        # Anchored to the repository, like the ledger and the config: read
        # against the caller, the profile was simply not there from a build
        # worktree, and the gate lost every tier and advisory flag (issue #74).
        profile = ledger_mod.load_profile(args.profile)
        pr = fetch_pr(args.repo, args.pr) or {}
        # GitHub knows the new head the moment a push lands; its checks appear
        # later. Scoping the read to that head is what stops the previous
        # commit's green from being reported as this commit's.
        sha = args.sha or pr.get("headRefOid")
        base = pr.get("baseRefName")
        if not sha:
            # `gh pr view` returns {} on any non-zero exit — a transient 5xx, a
            # rate limit, a `gh` too old to know `headRefOid` — and the check
            # list answers anyway. In the minutes after a push it answers with
            # the PREVIOUS commit's greens, which is the whole hazard the SHA
            # scoping exists to close. Falling through to the unscoped read here
            # would put that green under `gate`, and `gate` is what the caller
            # acts on. Unprovable is pending.
            reason = (
                f"cannot say which commit this gate is about: no --sha, and "
                f"reading headRefOid for {args.repo}#{args.pr} produced nothing. "
                "An unscoped check read reports the previous commit's results as "
                "this commit's, so there is nothing here to judge."
            )
            print(
                json.dumps(
                    {
                        **classify_checks([], profile),
                        "base_branch": base,
                        "head_sha": None,
                        "gate": "pending",
                        "reason": reason,
                    },
                    indent=2,
                )
            )
            return 0

        checks = fetch_checks(args.repo, args.pr, sha)
        summary = classify_checks(checks, profile, sha)
        print(
            json.dumps(
                {
                    **summary,
                    "base_branch": base,
                    "head_sha": sha,
                    "gate": ci_gate(checks, profile, base, sha),
                    "reason": None,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "verdict":
        ok, errors = validate_review(load_json(args.file, {}))
        print(
            json.dumps(
                {
                    "accepted": ok,
                    "errors": errors,
                    "gate_value": "clean"
                    if ok and load_json(args.file, {}).get("verdict") == "clean"
                    else "changes_requested",
                },
                indent=2,
            )
        )
        return 0 if ok else 1

    state = ledger_mod.load(Path(args.ledger))
    batch = state.batches.get(args.batch)
    if batch is None:
        print(f"error: no batch {args.batch}", file=sys.stderr)
        return 1
    pr = fetch_pr(args.repo, args.pr) if (args.repo and args.pr) else {"labels": []}
    # Not `load_json`: a relative config has to be anchored to the repository the
    # way the ledger already is, and a missing one has to say so rather than
    # quietly dropping every cap and every protected path.
    blockers = merge_blockers(batch, pr, ledger_mod.load_config(args.config))
    print(
        json.dumps({"batch": args.batch, "mergeable": not blockers, "blockers": blockers}, indent=2)
    )
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
