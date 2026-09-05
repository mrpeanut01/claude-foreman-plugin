"""Batching: group issues so one slow suite run covers several fixes."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import batch  # noqa: E402

CONFIG = {"limits": {"max_batch_issues": 3, "max_batch_weight": 5}, "risk_ceiling": "medium"}


def rec(n, risk="low", size="small", paths=()):
    return {
        "issue": n,
        "verdict": "actionable",
        "risk": risk,
        "size": size,
        "paths": list(paths),
        "title": f"issue {n}",
    }


# --- pairwise compatibility ---------------------------------------------------


def test_two_independent_low_risk_issues_can_share_a_batch():
    assert batch.can_group(rec(1, paths=["src/a.py"]), rec(2, paths=["src/b.py"]), CONFIG)[0]


def test_issues_touching_the_same_file_cannot():
    ok, why = batch.can_group(rec(1, paths=["src/a.py"]), rec(2, paths=["src/a.py"]), CONFIG)
    assert not ok and "src/a.py" in why


def test_a_high_risk_issue_never_shares_a_batch():
    ok, why = batch.can_group(rec(1), rec(2, risk="high"), CONFIG)
    assert not ok and "risk" in why


def test_unknown_paths_are_treated_as_possibly_overlapping():
    """No path information is not evidence of independence."""
    ok, why = batch.can_group(rec(1, paths=[]), rec(2, paths=[]), CONFIG)
    assert not ok and "unknown" in why.lower()


# --- grouping -----------------------------------------------------------------


def test_compatible_issues_are_packed_together():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, paths=["b.py"]), rec(3, paths=["c.py"])], CONFIG
    )
    assert len(groups) == 1
    assert groups[0]["issues"] == [1, 2, 3]


def test_the_issue_cap_closes_a_batch():
    records = [rec(n, paths=[f"f{n}.py"]) for n in range(1, 6)]
    groups = batch.group_issues(records, CONFIG)
    assert [len(g["issues"]) for g in groups] == [3, 2]


def test_the_weight_cap_closes_a_batch_before_the_issue_cap():
    records = [
        rec(1, size="medium", paths=["a.py"]),
        rec(2, size="medium", paths=["b.py"]),
        rec(3, size="medium", paths=["c.py"]),
    ]
    groups = batch.group_issues(records, CONFIG)  # medium=2, cap 5 -> two then one
    assert [len(g["issues"]) for g in groups] == [2, 1]


def test_a_high_risk_issue_gets_a_batch_of_its_own_rather_than_being_dropped():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, risk="high", paths=["b.py"])], CONFIG
    )
    solo = [g for g in groups if g["issues"] == [2]]
    assert solo and solo[0]["risk"] == "high"
    assert sum(len(g["issues"]) for g in groups) == 2, "no issue may be silently dropped"


def test_only_actionable_records_are_grouped():
    records = [rec(1, paths=["a.py"]), {**rec(2, paths=["b.py"]), "verdict": "needs-repro"}]
    groups = batch.group_issues(records, CONFIG)
    assert [i for g in groups for i in g["issues"]] == [1]


def test_batch_ids_are_stable_and_ordered():
    groups = batch.group_issues([rec(n, paths=[f"f{n}.py"]) for n in range(1, 5)], CONFIG)
    assert [g["id"] for g in groups] == ["b-001", "b-002"]


def test_grouping_is_deterministic_regardless_of_input_order():
    records = [rec(n, paths=[f"f{n}.py"]) for n in (3, 1, 2)]
    a = batch.group_issues(records, CONFIG)
    b = batch.group_issues(list(reversed(records)), CONFIG)
    assert [g["issues"] for g in a] == [g["issues"] for g in b]


def test_a_batch_carries_the_union_of_its_paths_and_its_worst_risk():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, risk="medium", paths=["b.py"])], CONFIG
    )
    assert set(groups[0]["paths"]) == {"a.py", "b.py"}
    assert groups[0]["risk"] == "medium"


# --- the economics ------------------------------------------------------------


def test_savings_are_the_suite_runs_not_taken():
    profile = {"cheap_tier_s": 120, "expensive_tier_s": 2280}
    groups = [{"issues": [1, 2, 3]}, {"issues": [4]}]
    saved = batch.estimate_savings(groups, profile)
    assert saved["suite_runs_saved"] == 2  # 4 issues, 2 batches
    assert saved["seconds_saved"] == 2 * 2400
    assert saved["batched_s"] == 2 * 2400 and saved["unbatched_s"] == 4 * 2400


def test_no_savings_claimed_when_every_batch_holds_one_issue():
    saved = batch.estimate_savings(
        [{"issues": [1]}, {"issues": [2]}], {"cheap_tier_s": 60, "expensive_tier_s": 60}
    )
    assert saved["suite_runs_saved"] == 0 and saved["seconds_saved"] == 0


def test_an_unprofiled_repo_reports_savings_it_cannot_quantify():
    saved = batch.estimate_savings([{"issues": [1, 2]}], {})
    assert saved["suite_runs_saved"] == 1
    assert saved["seconds_saved"] is None


# --- splitting a failed batch -------------------------------------------------


def test_a_failing_issue_peels_off_and_the_rest_proceed():
    failing, rest = batch.split(
        {"id": "b-001", "issues": [1, 2, 3], "risk": "low", "paths": ["a.py"]}, failing_issue=2
    )
    assert failing["issues"] == [2] and rest["issues"] == [1, 3]
    assert failing["id"] == "b-001a" and rest["id"] == "b-001b"
    assert failing["split_from"] == "b-001"


def test_splitting_a_single_issue_batch_is_refused():
    with pytest.raises(batch.CannotSplit):
        batch.split({"id": "b-001", "issues": [1]}, failing_issue=1)


def test_splitting_on_an_issue_outside_the_batch_is_refused():
    with pytest.raises(batch.CannotSplit):
        batch.split({"id": "b-001", "issues": [1, 2]}, failing_issue=9)
