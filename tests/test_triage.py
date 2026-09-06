"""Triage: classify, size, risk-score and dedupe issues into the ledger."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import triage  # noqa: E402


def issue(**kw):
    base = {
        "number": 1,
        "title": "Something is broken",
        "body": "It broke.",
        "labels": [],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
        "comments": [],
    }
    return {**base, **kw}


# --- sizing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("Fix typo in README", "s/teh/the/", "small"),
        ("Bump requests to 2.32", "Routine dependency bump.", "small"),
        ("Add retry to the upload client", "Uploads fail on 503. " * 12, "medium"),
        ("Redesign the auth subsystem", "We should rewrite how sessions work. " * 20, "large"),
        ("Migrate storage to the new schema", "Multi-step migration.", "large"),
    ],
)
def test_size_from_title_and_body(title, body, expected):
    assert triage.classify_size(issue(title=title, body=body)) == expected


def test_a_long_checklist_reads_as_large():
    body = "\n".join(f"- [ ] step {i}" for i in range(9))
    assert triage.classify_size(issue(title="Improve onboarding", body=body)) == "large"


def test_missing_body_does_not_crash_sizing():
    assert triage.classify_size(issue(body=None)) in {"small", "medium", "large"}


# --- risk ---------------------------------------------------------------------

PROTECTED = ["**/auth/**", "**/migrations/**", "**/payments/**"]


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("Fix typo in the docs", "Just wording.", "low"),
        ("Add a test for the parser", "Coverage gap.", "low"),
        ("Handle empty upload payload", "Returns 500 on empty body.", "medium"),
        ("Session token never expires", "Anyone with an old token stays logged in.", "high"),
        ("Add column to users table", "Needs a migration.", "high"),
    ],
)
def test_risk_level(title, body, expected):
    assert triage.risk_level(issue(title=title, body=body), PROTECTED) == expected


def test_a_security_label_forces_high_risk():
    got = triage.risk_level(issue(title="Tidy up logging", body="", labels=["security"]), PROTECTED)
    assert got == "high"


def test_a_path_under_protection_forces_high_risk():
    got = triage.risk_level(issue(title="Tweak helper", body="in src/auth/session.py"), PROTECTED)
    assert got == "high"


# --- actionability ------------------------------------------------------------


def test_a_bug_with_no_repro_signal_needs_repro():
    verdict = triage.actionability(
        issue(title="App crashes", body="It crashes sometimes.", labels=["bug"])
    )
    assert verdict["actionable"] is False
    assert verdict["lifecycle"] == "needs-repro"


@pytest.mark.parametrize(
    "body",
    [
        'Traceback (most recent call last):\n  File "app.py", line 3',
        "Steps: 1. run `foo bar` 2. see the error",
        "It fails in src/parser/lexer.py when the input is empty",
        "Run `npm test` and the third case fails with ECONNREFUSED",
    ],
)
def test_evidence_in_the_body_means_no_lifecycle_label(body):
    """Anthropic's rule: false positives are worse than missing labels."""
    verdict = triage.actionability(issue(title="Parser fails", body=body, labels=["bug"]))
    assert verdict["lifecycle"] is None
    assert verdict["actionable"] is True


def test_lifecycle_labels_never_apply_to_questions_or_enhancements():
    for kind in ("question", "enhancement"):
        verdict = triage.actionability(issue(title="Could we?", body="thoughts?", labels=[kind]))
        assert verdict["lifecycle"] is None


def test_a_model_behaviour_report_does_not_need_traditional_repro():
    verdict = triage.actionability(
        issue(
            title="Wrong suggestion on empty file",
            body="When the file is empty it suggests deleting it, which should never happen.",
            labels=["bug"],
        )
    )
    assert verdict["lifecycle"] is None


# --- dedupe -------------------------------------------------------------------


def test_near_identical_titles_are_flagged_as_duplicates():
    new = issue(number=10, title="Upload fails with 503 on large files")
    others = [
        issue(number=3, title="Upload fails with 503 for large files"),
        issue(number=4, title="Dark mode for the settings page"),
    ]
    hits = triage.dedupe(new, others)
    assert hits and hits[0]["number"] == 3
    assert hits[0]["score"] >= 0.6


def test_unrelated_issues_are_not_flagged():
    new = issue(number=10, title="Add dark mode")
    others = [issue(number=3, title="Upload fails with 503 for large files")]
    assert triage.dedupe(new, others) == []


def test_an_issue_never_duplicates_itself():
    new = issue(number=10, title="Upload fails with 503 on large files")
    assert triage.dedupe(new, [new]) == []


def test_only_open_issues_can_be_duplicated():
    new = issue(number=10, title="Upload fails with 503 on large files")
    closed = issue(number=3, title="Upload fails with 503 on large files", state="closed")
    assert triage.dedupe(new, [closed]) == []


# --- labels: a closed vocabulary ---------------------------------------------

AVAILABLE = [
    "bug",
    "enhancement",
    "question",
    "duplicate",
    "needs-repro",
    "needs-info",
    "security",
    "size:small",
    "size:medium",
    "size:large",
]


def test_planned_labels_come_only_from_the_repo_vocabulary():
    record = {"kind": "bug", "size": "small", "lifecycle": "needs-repro", "risk": "high"}
    planned = triage.plan_labels(record, AVAILABLE)
    assert set(planned) <= set(AVAILABLE)
    assert "bug" in planned and "size:small" in planned and "needs-repro" in planned


def test_labels_the_repo_does_not_define_are_dropped_silently():
    record = {"kind": "bug", "size": "small", "lifecycle": None, "risk": "low"}
    planned = triage.plan_labels(record, ["bug"])
    assert planned == ["bug"], "never invent a label the repo has not defined"


def test_exactly_one_category_label_is_always_planned():
    for kind in ("bug", "enhancement", "question", "duplicate"):
        planned = triage.plan_labels(
            {"kind": kind, "size": "medium", "lifecycle": None, "risk": "low"}, AVAILABLE
        )
        categories = [
            item for item in planned if item in {"bug", "enhancement", "question", "duplicate"}
        ]
        assert categories == [kind]


# --- skip already-triaged -----------------------------------------------------


def test_an_unchanged_issue_is_skipped():
    prior = {42: {"issue": 42, "issue_updated_at": "2026-09-01T00:00:00Z"}}
    assert triage.should_skip(issue(number=42, updatedAt="2026-09-01T00:00:00Z"), prior) is True


def test_an_edited_issue_is_retriaged():
    prior = {42: {"issue": 42, "issue_updated_at": "2026-09-01T00:00:00Z"}}
    assert triage.should_skip(issue(number=42, updatedAt="2026-09-02T00:00:00Z"), prior) is False


def test_an_unseen_issue_is_never_skipped():
    assert triage.should_skip(issue(number=99), {}) is False


# --- the record ---------------------------------------------------------------


def test_triage_builds_a_complete_record():
    record = triage.triage_issue(
        issue(
            number=7,
            title="Upload fails with 503",
            body="Traceback in src/upload.py line 22",
            labels=["bug"],
        ),
        others=[],
        protected=PROTECTED,
        available_labels=AVAILABLE,
    )
    assert record["issue"] == 7
    assert record["kind"] == "bug"
    assert record["size"] in {"small", "medium", "large"}
    assert record["risk"] in {"low", "medium", "high"}
    assert record["verdict"] in {"actionable", "needs-info", "needs-repro", "duplicate"}
    assert record["labels"] and set(record["labels"]) <= set(AVAILABLE)
    assert record["issue_updated_at"]


def test_a_duplicate_is_recorded_as_such_and_not_queued():
    record = triage.triage_issue(
        issue(number=10, title="Upload fails with 503 on large files", labels=["bug"]),
        others=[issue(number=3, title="Upload fails with 503 for large files")],
        protected=PROTECTED,
        available_labels=AVAILABLE,
    )
    assert record["verdict"] == "duplicate"
    assert record["duplicate_of"] == 3
    assert "duplicate" in record["labels"]


def test_only_actionable_issues_reach_the_work_queue():
    records = [
        {"issue": 1, "verdict": "actionable"},
        {"issue": 2, "verdict": "needs-repro"},
        {"issue": 3, "verdict": "duplicate"},
        {"issue": 4, "verdict": "actionable"},
    ]
    assert [r["issue"] for r in triage.queueable(records)] == [1, 4]


def test_only_the_newer_issue_of_a_pair_is_the_duplicate():
    """Both sides matching would mark the original a duplicate of its own copy."""
    original = issue(number=3, title="Upload fails with 503 on large files")
    copy = issue(number=10, title="Upload fails with 503 for large files")
    assert triage.dedupe(original, [original, copy]) == []
    assert triage.dedupe(copy, [original, copy])[0]["number"] == 3


def test_the_original_of_a_duplicate_pair_stays_queueable():
    pair = [
        issue(
            number=3,
            title="Upload fails with 503 on large files",
            labels=["bug"],
            body="Traceback in src/upload.py line 5",
        ),
        issue(
            number=10,
            title="Upload fails with 503 for large files",
            labels=["bug"],
            body="Traceback in src/upload.py line 5",
        ),
    ]
    records = [triage.triage_issue(i, pair, [], AVAILABLE) for i in pair]
    assert [r["verdict"] for r in records] == ["actionable", "duplicate"]


# --- issue #2: a triage record must carry the paths batching needs ------------


def test_triage_records_carry_the_paths_found_in_the_issue():
    record = triage.triage_issue(
        issue(
            number=7,
            title="Upload fails",
            body="Traceback in src/upload.py line 22",
            labels=["bug"],
        ),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["paths"] == ["src/upload.py"]


def test_triage_output_can_be_batched_without_post_processing():
    import batch as batch_mod

    issues = [
        issue(number=n, title=t, body=f"Traceback in src/mod{n}.py line 1", labels=["bug"])
        for n, t in enumerate(
            ("Upload retries forever", "Parser drops commas", "Cache never evicts"), start=1
        )
    ]
    records = [triage.triage_issue(i, issues, [], AVAILABLE) for i in issues]
    groups = batch_mod.group_issues(
        records, {"limits": {"max_batch_issues": 3, "max_batch_weight": 9}}
    )
    assert [g["issues"] for g in groups] == [[1, 2, 3]], "batching must work on raw triage output"


# --- issue #5: hints must match words, not substrings ------------------------


@pytest.mark.parametrize(
    "title,body,expected_risk",
    [
        ("Update the Dockerfile base image", "Bump to bookworm.", "medium"),
        ("Documentation for the CLI flags", "Explain each flag.", "low"),
        # From issue #5's reproduction list. "schema" is a genuine keyword
        # collision, not a substring bug: over-scoring is the safe direction.
        ("Documentation for the schema tool", "Explain the flags.", "high"),
        # "tokens" is indistinguishable from an auth token, so this scores high.
        # Over-scoring costs a solo PR; under-scoring auto-merges a security change.
        ("Tokenizer drops short tokens", "_tokens in the parser.", "high"),
        ("Tokenizer performance is poor", "Profiling the lexer.", "medium"),
        ("Session token never expires", "Auth stays valid.", "high"),
    ],
)
def test_risk_hints_match_whole_words_only(title, body, expected_risk):
    assert triage.risk_level(issue(title=title, body=body), []) == expected_risk


def test_mentioning_a_lint_job_does_not_make_an_issue_small():
    body = (
        "The lint job fails because classify_checks treats every failure as advisory "
        "when branch protection is absent. " * 3
    )
    assert triage.classify_size(issue(title="CI gate is wrong", body=body)) != "small"


def test_a_path_mentioned_twice_is_listed_once():
    record = triage.triage_issue(
        issue(
            number=8,
            title="Two mentions",
            body="See src/a.py and also src/a.py again, plus src/b.py",
            labels=["bug"],
        ),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["paths"] == ["src/a.py", "src/b.py"]


# --- the regression the review gate caught ------------------------------------
# Word boundaries fixed substring false-positives but silently disabled the
# security vocabulary: \bauth\b does not match "authentication".


@pytest.mark.parametrize(
    "title",
    [
        "Authentication bypass on the admin API",
        "Authorization header is dropped",
        "Unauthenticated users can read private repos",
        "OAuth callback leaks the code",
        "Rotate leaked API tokens",
        "Bucket permissions are world readable",
        "Store credentials in the keychain",
        "Secrets are printed to the log",
        "Payments are double charged",
        "Schemas are not migrated",
        "Passwords are logged in plaintext",
        "Encrypting the session store",
        "Privilege escalation via the share link",
    ],
)
def test_inflected_security_words_still_score_high(title):
    assert triage.risk_level(issue(title=title, body="Details."), []) == "high", title


@pytest.mark.parametrize(
    "title",
    [
        "Author of the commit is wrong",  # must not match via `auth`
        "Authoring guide needs an update",
        "Update the Dockerfile base image",  # must not match via `doc`
        "Tokenizer performance is poor",  # `tokenizer` is not `token`
        # NB "Tokenizer drops short tokens" DOES score high, and should: the word
        # "tokens" cannot be told from an auth token, and over-scoring is safe.
    ],
)
def test_lookalike_words_do_not_inflate_risk(title):
    assert triage.risk_level(issue(title=title, body="Details."), []) != "high", title


def test_an_empty_hint_list_matches_nothing():
    """`\\b()\\b` compiles to something that matches any word."""
    assert triage._has("anything at all", ()) is False


# --- issue #11: the vocabulary lost bare `auth`, en-GB spellings, and oauth2 ---


@pytest.mark.parametrize(
    "title",
    [
        "auth bypass on the admin API",
        "Fix auth middleware ordering",
        "Unauthorised access to the admin API",
        "Authorisation header is dropped",
        "authorisation bypass in the gateway",
        "oauth2 flow is broken",
        "Passwordless login never expires",
        "Reauthentication is skipped after logout",
    ],
)
def test_en_gb_spellings_and_bare_auth_still_score_high(title):
    assert triage.risk_level(issue(title=title, body="Details."), []) == "high", title


@pytest.mark.parametrize(
    "title",
    [
        "Author of the commit is wrong",
        "Authoring guide needs an update",
        "Co-authored-by trailer is malformed",
    ],
)
def test_author_is_still_not_an_auth_issue(title):
    assert triage.risk_level(issue(title=title, body="Details."), []) != "high", title


# --- issue #54: dot-prefixed paths must survive extraction --------------------


def test_a_dot_prefixed_path_keeps_its_leading_dot():
    """The extractor required a leading word character, so '.github/...' came
    back as 'github/...' and stopped matching any protected-path glob."""
    assert triage._paths_in("the release job in .github/workflows/ci.yml") == [
        ".github/workflows/ci.yml"
    ]


def test_a_dot_prefixed_protected_path_is_scored_high_risk():
    text = "the release job in .github/workflows/ci.yml must not run on forks"
    assert triage.risk_level({"title": text, "body": ""}, [".github/workflows/**"]) == "high"


def test_ordinary_paths_are_still_extracted():
    assert triage._paths_in("traceback in src/auth/token.py line 4") == ["src/auth/token.py"]


def test_a_sentence_period_is_not_read_as_part_of_a_path():
    assert triage._paths_in("that is wrong. src/upload.py is fine") == ["src/upload.py"]


def test_a_path_preceded_by_a_separator_is_still_found():
    """Guarding the optional dot with a lookbehind refused every path written
    after a separator, which is how relative paths and tracebacks are written."""
    assert triage._paths_in("the deploy script ./infra/deploy.py needs a retry") == [
        "infra/deploy.py"
    ]
    assert triage._paths_in('File "/app/src/auth/session.py", line 9') == [
        "app/src/auth/session.py"
    ]


def test_a_relative_dot_prefixed_path_keeps_the_directory_dot():
    assert triage._paths_in("./.github/workflows/ci.yml is wrong") == [".github/workflows/ci.yml"]


def test_a_separator_prefixed_protected_path_still_scores_high_risk():
    text = "the deploy script ./infra/deploy.py needs a retry"
    assert triage.risk_level({"title": text, "body": ""}, ["**/infra/**"]) == "high"


# --- issue #3: short titles must not collide at score 1.0 ---------------------


def test_a_single_distinguishing_digit_survives_tokenising():
    """Dropping one-character tokens made "Bug 1" and "Bug 2" the same title."""
    assert triage._tokens("Bug 1") == {"bug", "1"}
    assert triage._tokens("Bug 2") == {"bug", "2"}


def test_titles_that_differ_only_by_a_digit_are_not_duplicates():
    new = issue(number=10, title="Bug 2")
    others = [issue(number=3, title="Bug 1")]
    assert triage.dedupe(new, others) == []


def test_one_shared_word_is_never_enough_to_call_a_duplicate():
    """A ratio alone cannot see that the overlap is a single word: two
    one-word titles reach 1.0 on it, which is the worst false positive there
    is because a wrongly closed duplicate needs a human to notice."""
    new = issue(number=10, title="Crash")
    others = [issue(number=3, title="Crash")]
    assert triage.dedupe(new, others) == []


def test_a_real_pair_still_overlaps_on_more_than_one_word():
    new = issue(number=10, title="Upload fails with 503 on large files")
    others = [issue(number=3, title="Upload fails with 503 for large files")]
    assert triage.dedupe(new, others)[0]["number"] == 3


# --- issue #4: needs-info was documented but unreachable ---------------------


def test_a_bug_that_blames_an_environment_it_never_names_needs_info():
    """The gap needs-info is for: a failure that has been shown, but only
    makes sense with a version or a machine the reporter did not give."""
    verdict = triage.actionability(
        issue(
            title="Upload fails",
            body="Works fine locally but fails in production with a 500.",
            labels=["bug"],
        )
    )
    assert verdict["lifecycle"] == "needs-info"
    assert verdict["actionable"] is False


def test_a_bug_that_names_the_version_it_broke_on_is_actionable():
    verdict = triage.actionability(
        issue(
            title="Upload fails",
            body="Worked on 1.4.2. Since upgrading to 1.5.0 every upload returns a 500.",
            labels=["bug"],
        )
    )
    assert verdict["lifecycle"] is None
    assert verdict["actionable"] is True


def test_a_bug_that_never_blames_an_environment_is_not_asked_for_one():
    """Asking every reporter for a version is a round trip that reads as
    dismissal, so the trigger is the reporter's own claim, not its absence."""
    verdict = triage.actionability(
        issue(
            title="Parser drops trailing commas",
            body="It fails in src/parser/lexer.py when the input ends with a comma.",
            labels=["bug"],
        )
    )
    assert verdict["lifecycle"] is None


def test_needs_info_is_a_verdict_a_record_can_actually_carry():
    """`grep -c needs-info scripts/triage.py` returned 0: the verdict was in
    both verdict tables and reachable from neither."""
    record = triage.triage_issue(
        issue(
            number=12,
            title="Build fails",
            body="Works on my machine. On CI the third case fails with ECONNREFUSED.",
            labels=["bug"],
        ),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["verdict"] == "needs-info"
    assert "needs-info" in record["labels"]
    assert triage.queueable([record]) == []


# --- issue #5 (reopened): a risk score must say what set it -------------------
# The substring half is fixed. The half that remains is a keyword collision:
# risk reads title *and* body, so an issue that merely discusses a dangerous
# word scores high. That cannot be scored away — the same word in the same
# place is the only evidence a real auth bug with a neutral title leaves, and
# rounding down puts it in a batch that merges itself. What it can do is stop
# being silent, so the override the command documents can be made on evidence.


def test_a_high_risk_score_says_which_word_set_it_and_where():
    record = triage.triage_issue(
        issue(
            number=3,
            title="Short issue titles produce false duplicates",
            body="_tokens drops short tokens, so any distinguishing digit is discarded.",
            labels=["bug"],
        ),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["risk"] == "high"
    assert "token" in record["risk_reason"]
    assert "body" in record["risk_reason"], "a reviewer needs to know it was only mentioned"


def test_a_risk_score_from_the_title_says_so():
    record = triage.triage_issue(
        issue(number=4, title="Session token never expires", body="Details.", labels=["bug"]),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["risk"] == "high"
    assert "title" in record["risk_reason"]


def test_a_protected_path_names_the_path_that_forced_the_score():
    record = triage.triage_issue(
        issue(number=5, title="Tweak helper", body="in src/auth/session.py", labels=["bug"]),
        others=[],
        protected=PROTECTED,
        available_labels=AVAILABLE,
    )
    assert record["risk"] == "high"
    assert "src/auth/session.py" in record["risk_reason"]


def test_a_high_risk_label_is_named_as_the_reason():
    record = triage.triage_issue(
        issue(number=6, title="Tidy up logging", body="Details.", labels=["security"]),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["risk_reason"] == "the security label"


def test_every_record_carries_a_risk_reason_even_when_nothing_scored():
    record = triage.triage_issue(
        issue(number=7, title="Handle empty upload payload", body="Returns 500 on empty body."),
        others=[],
        protected=[],
        available_labels=AVAILABLE,
    )
    assert record["risk"] == "medium"
    assert record["risk_reason"]
