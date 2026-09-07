"""Protection lives in two places, and reading only one of them worked blind.

Classic branch protection is at `branches/{b}/protection`. Rulesets — how new
repos are configured — are at `rules/branches/{b}` and are invisible there. A
ruleset-protected repo answered 404, was recorded `protection_known: false`, and
every check it required was never learned.

The failure was safe (unknown protection makes every declared job required) and
that is exactly why it could sit unnoticed. These tests pin both halves: what is
read, and what is still honestly refused.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_profile  # noqa: E402
import land  # noqa: E402

# --- reading a ruleset --------------------------------------------------------


def _status_rule(*contexts):
    return {
        "type": "required_status_checks",
        "ruleset_id": 7,
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": c, "integration_id": 15368} for c in contexts],
        },
    }


def _pr_rule(count):
    return {
        "type": "pull_request",
        "ruleset_id": 7,
        "parameters": {
            "required_approving_review_count": count,
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
        },
    }


def test_a_ruleset_s_required_checks_are_read():
    protection = ci_profile.protection_from_rules(
        [_status_rule("lint", "test (3.11)"), {"type": "non_fast_forward"}]
    )
    assert ci_profile.required_checks(protection) == ["lint", "test (3.11)"]


def test_a_ruleset_s_required_approvals_are_read():
    assert ci_profile.required_approvals(ci_profile.protection_from_rules([_pr_rule(2)])) == 2


def test_the_strictest_approval_count_wins_across_rulesets():
    """Several rulesets can each demand approvals and satisfying one does not
    satisfy another, so the answer is the maximum, not the last one read."""
    rules = [_pr_rule(1), _pr_rule(3), _pr_rule(2)]
    assert ci_profile.required_approvals(ci_profile.protection_from_rules(rules)) == 3


def test_an_empty_rules_list_is_an_answer_not_an_absence():
    """`[]` from this endpoint means "no ruleset applies to this branch", which
    is a fact. `None` means the question could not be asked. Collapsing the two
    is what turns an unreadable gate into an open one."""
    assert ci_profile.protection_from_rules([]) is not None
    assert ci_profile.required_checks(ci_profile.protection_from_rules([])) == []
    assert ci_profile.protection_from_rules(None) is None


def test_junk_in_the_rules_payload_is_skipped_rather_than_raising():
    """A shape this does not recognise must not take the whole probe down; an
    unparsed rule reads as one more thing that was not learned."""
    protection = ci_profile.protection_from_rules(
        ["nonsense", None, {"type": "required_status_checks"}, _status_rule("lint")]
    )
    assert ci_profile.required_checks(protection) == ["lint"]


def test_a_malformed_approval_count_reads_as_none_required():
    assert (
        ci_profile.required_approvals(
            ci_profile.protection_from_rules(
                [{"type": "pull_request", "parameters": {"required_approving_review_count": "two"}}]
            )
        )
        == 0
    )


# --- classic protection still reads the same ----------------------------------


def test_classic_protection_still_reads_its_own_shapes():
    classic = {
        "required_status_checks": {"contexts": ["lint"], "checks": [{"context": "test"}]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }
    assert ci_profile.required_checks(classic) == ["lint", "test"]
    assert ci_profile.required_approvals(classic) == 1


# --- both at once -------------------------------------------------------------


def test_both_mechanisms_apply_at_once_so_the_contexts_are_unioned():
    """GitHub enforces both, and a check required by either blocks the merge.
    Taking one source as authoritative drops whatever the other demanded."""
    classic = {"required_status_checks": {"contexts": ["lint"]}}
    combined = ci_profile.combine_protection(
        classic, ci_profile.protection_from_rules([_status_rule("e2e")])
    )
    assert ci_profile.required_checks(combined) == ["e2e", "lint"]


def test_the_stricter_approval_requirement_survives_the_combination():
    classic = {"required_pull_request_reviews": {"required_approving_review_count": 1}}
    combined = ci_profile.combine_protection(
        classic, ci_profile.protection_from_rules([_pr_rule(3)])
    )
    assert ci_profile.required_approvals(combined) == 3


def test_neither_readable_is_unknown():
    assert ci_profile.combine_protection(None, None) is None


def test_one_readable_source_is_still_an_answer():
    assert ci_profile.combine_protection(None, ci_profile.protection_from_rules([])) is not None


# --- what the fetch decides ---------------------------------------------------


@pytest.fixture
def endpoints(monkeypatch):
    """Stand in for both endpoints, keyed on the path each call is given."""

    def install(*, classic, classic_status, rules):
        monkeypatch.setattr(ci_profile, "_gh_api", lambda args: (classic, classic_status))

        def fake_json(args):
            if args[0] == "repo":
                return {"defaultBranchRef": {"name": "main"}}
            if "rules/branches" in args[-1]:
                return rules
            return None

        monkeypatch.setattr(ci_profile, "_gh_json", fake_json)

    return install


def test_a_ruleset_protected_repo_is_no_longer_blind(endpoints):
    """The whole point. Classic protection 404s on a repo configured with
    rulesets, which used to be the end of the enquiry."""
    endpoints(classic=None, classic_status=404, rules=[_status_rule("lint", "e2e"), _pr_rule(1)])
    protection, sources = ci_profile.fetch_protection("me/mine")
    assert ci_profile.required_checks(protection) == ["e2e", "lint"]
    assert ci_profile.required_approvals(protection) == 1
    assert sources == {"branch": "main", "classic": "absent", "rulesets": "read"}


def test_an_unprotected_repo_reads_as_known_and_empty(endpoints):
    """404 on an endpoint whose branch came from `defaultBranchRef` is the
    documented answer for "not protected", not a failure to ask."""
    endpoints(classic=None, classic_status=404, rules=[])
    protection, sources = ci_profile.fetch_protection("me/mine")
    assert protection is not None
    assert ci_profile.required_checks(protection) == []
    assert sources["classic"] == "absent" and sources["rulesets"] == "absent"


def test_a_403_on_classic_protection_stays_unknown_even_with_rules_in_hand(endpoints):
    """The load-bearing refusal. The rules endpoint needs only read access where
    the protection one needs admin, so a non-admin token learns every ruleset
    rule and cannot see whether classic protection adds more. Reporting the
    rules as the whole answer says "these and nothing else" about a question
    half of which went unanswered."""
    endpoints(classic=None, classic_status=403, rules=[_status_rule("lint")])
    protection, sources = ci_profile.fetch_protection("me/mine")
    assert protection is None, "half an answer is not an answer"
    assert sources == {"branch": "main", "classic": "unreadable", "rulesets": "read"}


def test_a_network_failure_on_both_stays_unknown(endpoints):
    endpoints(classic=None, classic_status=None, rules=None)
    protection, sources = ci_profile.fetch_protection("me/mine")
    assert protection is None
    assert sources["classic"] == "unreadable" and sources["rulesets"] == "unreadable"


def test_unreadable_rulesets_do_not_spoil_a_read_classic_protection(endpoints):
    endpoints(
        classic={"required_status_checks": {"contexts": ["lint"]}},
        classic_status=200,
        rules=None,
    )
    protection, sources = ci_profile.fetch_protection("me/mine")
    assert ci_profile.required_checks(protection) == ["lint"]
    assert sources["rulesets"] == "unreadable"


# --- the http status the decision rests on ------------------------------------


class _Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_the_api_wrapper_reports_the_status_a_failure_carried(monkeypatch):
    """`_gh_json` folds every failure into None, which is right where any
    failure means the same thing. Here a 404 is a fact and a 403 is ignorance."""
    monkeypatch.setattr(
        ci_profile.subprocess,
        "run",
        lambda *a, **k: _Done(1, stderr="the cli said: Branch not protected (HTTP 404)\n"),
    )
    assert ci_profile._gh_api(["api", "whatever"]) == (None, 404)


def test_the_api_wrapper_reports_no_status_when_the_failure_carried_none(monkeypatch):
    monkeypatch.setattr(
        ci_profile.subprocess,
        "run",
        lambda *a, **k: _Done(1, stderr="dial tcp: lookup api.github.com: no such host\n"),
    )
    assert ci_profile._gh_api(["api", "whatever"]) == (None, None)


def test_the_api_wrapper_survives_the_cli_not_being_installed(monkeypatch):
    def boom(*a, **k):
        raise OSError("not installed")

    monkeypatch.setattr(ci_profile.subprocess, "run", boom)
    assert ci_profile._gh_api(["api", "whatever"]) == (None, None)


# --- the profile carries it ---------------------------------------------------


def test_the_profile_records_the_approvals_and_where_protection_came_from(tmp_path):
    workflows = tmp_path / "wf"
    workflows.mkdir()
    profile = ci_profile.build_profile(
        workflows,
        job_runs=[],
        protection=ci_profile.protection_from_rules([_status_rule("lint"), _pr_rule(2)]),
        protection_sources={"branch": "main", "classic": "absent", "rulesets": "read"},
    )
    assert profile["protection_known"] is True
    assert profile["required_checks"] == ["lint"]
    assert profile["required_approvals"] == 2
    assert profile["protection_sources"]["rulesets"] == "read"


def test_a_profile_built_with_no_sources_still_has_the_field(tmp_path):
    workflows = tmp_path / "wf"
    workflows.mkdir()
    profile = ci_profile.build_profile(workflows, job_runs=[], protection=None)
    assert profile["protection_sources"] == {}
    assert profile["required_approvals"] == 0


# --- the deadlock this exists to stop -----------------------------------------


CONFIG = {"auto_merge": True, "merge_method": "squash"}
CLEAR_BATCH = {
    "id": "b-001",
    "ci_gate": "full_green",
    "review_gate": "clean",
    "paths": ["src/a.py"],
    "paths_head": "abc1234",
}


def _pr(**overrides):
    return {"number": 1, "labels": [], "headRefOid": "abc1234", **overrides}


def test_a_repo_requiring_approvals_blocks_rather_than_queueing_a_merge_that_cannot_fire():
    """foreman's review gate is a ledger fact, not a GitHub approval: the
    reviewer writes `review.verdict` and never asks GitHub to approve anything.
    On a repo requiring approvals the two are disconnected — the ledger reads
    `clean`, the auto-merge queues a merge that can never fire, and the batch
    sits in `merging` until `stale_after_s`. A sentence beats a silent stall."""
    blockers = land.merge_blockers(CLEAR_BATCH, _pr(reviewDecision="REVIEW_REQUIRED"), CONFIG)
    assert any("cannot produce" in b for b in blockers)
    assert any("separate identity" in b for b in blockers)


def test_changes_requested_on_github_blocks_too():
    blockers = land.merge_blockers(CLEAR_BATCH, _pr(reviewDecision="CHANGES_REQUESTED"), CONFIG)
    assert any("requested changes" in b for b in blockers)


def test_an_approved_pr_is_not_blocked_by_the_review_decision():
    assert land.merge_blockers(CLEAR_BATCH, _pr(reviewDecision="APPROVED"), CONFIG) == []


def test_a_repo_requiring_no_reviews_is_not_blocked():
    """`reviewDecision` is null when nothing requires a review, which is most
    repos and must stay the quiet path."""
    assert land.merge_blockers(CLEAR_BATCH, _pr(reviewDecision=None), CONFIG) == []
    assert land.merge_blockers(CLEAR_BATCH, _pr(), CONFIG) == []
