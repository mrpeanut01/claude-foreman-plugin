#!/usr/bin/env python3
"""Triage: classify, size, risk-score and dedupe issues into the ledger.

Deterministic scoring lives here so it is testable and consistent across runs.
Judgement that needs to read prose stays in Skill(foreman:issue-triage).

Two rules borrowed from anthropics/claude-code's triage-issue command, because
they are what keeps automated triage trustworthy:

  * labels come only from the repo's own vocabulary — never invented;
  * a false lifecycle label is worse than a missing one, so the bar for
    applying needs-repro is evidence of absence, not absence of evidence.

CLI:
    triage.py plan --repo OWNER/NAME [--limit 50] [--config .foreman/config.json]
        [--ledger .foreman]
    triage.py apply --repo OWNER/NAME --plan plan.json [--ledger .foreman]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CATEGORIES = ("bug", "enhancement", "question", "duplicate")
DUPLICATE_THRESHOLD = 0.6
MIN_SHARED_TOKENS = 2

# Hint fragments, matched as whole words with \b on both sides. They are regex
# rather than plain words because a plain-word list silently loses every
# inflection: \bauth\b does not match "authentication", and a risk gate that
# stops recognising authentication is worse than the substring bug it replaced.
LARGE_HINTS = (
    r"redesigns?",
    r"rewrit\w*",
    r"refactor\w*",
    r"migrat\w*",
    r"overhaul\w*",
    r"architectur\w*",
    r"epics?",
    r"port to",
    r"re-architect\w*",
)
SMALL_HINTS = (
    r"typos?",
    r"bumps?",
    r"renames?",
    r"readme",
    r"changelogs?",
    r"whitespace",
    r"lint\w*",
    r"format\w*",
    r"spelling",
    r"docstrings?",
)
LOW_RISK_HINTS = (
    r"typos?",
    r"docs?",
    r"documentation",
    r"readme",
    r"comments?",
    r"tests?",
    r"coverage",
    r"lint\w*",
    r"format\w*",
    r"renames?",
    r"changelogs?",
)
# Words that mean one thing. Nobody writes `password`, `csrf` or `billing`
# about anything but the dangerous subject, so one of these anywhere in an
# issue — title or body — is the subject.
STRONG_RISK_HINTS = (
    r"oauth\d*",
    # The leading \w* catches Reauthentication and Unauthorised. en-GB spellings
    # matter because this project writes en-GB, so its reporters will too.
    r"\w*authentic\w*",
    r"\w*authoriz\w*",
    r"\w*authoris\w*",
    r"password\w*",
    r"credentials?",
    r"migrat\w*",
    r"payments?",
    r"billing",
    r"secrets?",
    r"csrf",
    r"xss",
    r"injections?",
    r"encrypt\w*",
    r"privileges?",
    r"logged in",
)
# Words that are the subject of an auth issue and, just as often, of nothing of
# the kind: a tokeniser has tokens, an agent has sessions, a JSON file has a
# schema, an API call needs permissions. In the title they are the subject. In
# the body, one on its own is a mention — on this repo's own queue eleven of
# twelve `high` scores came from exactly one such word in the body, and every
# one was a collision (issue #5). Two different ones in the body is an issue
# that keeps talking about the dangerous thing, and that is a subject again.
COLLIDING_RISK_HINTS = (
    # Bare `auth` is safe: the trailing \b already excludes Author and authoring.
    r"auth",
    r"authn",
    r"authz",
    r"tokens?",
    r"sessions?",
    r"permissions?",
    r"schemas?",
)
HIGH_RISK_HINTS = STRONG_RISK_HINTS + COLLIDING_RISK_HINTS
HIGH_RISK_LABELS = ("security", "critical", "data-loss", "p0")

# Evidence that a reporter has already given someone enough to work with.
EVIDENCE = (
    re.compile(r"traceback|stack ?trace|exception in|panic:", re.I),
    re.compile(r'File "[^"]+", line \d+'),
    re.compile(r"\b[A-Z]{3,}[A-Z_]*\b"),  # ECONNREFUSED, ENOENT
    re.compile(r"\b[45]\d\d\b"),  # HTTP status codes
    re.compile(r"[\w./-]+\.(py|js|ts|tsx|rs|go|rb|java|kt|c|cpp|sh)\b"),
    re.compile(r"`[^`]+`"),  # a command or symbol
    re.compile(r"^\s*\d+[.)]\s+\S", re.M),  # numbered steps
    re.compile(r"\bsteps?\s+to\s+reproduce\b", re.I),
)
# A described expectation is a repro for behaviour bugs, where "run this and
# watch" does not apply.
EXPECTATION = re.compile(r"\b(should|expected|instead of|rather than|ought to)\b", re.I)
CONDITION = re.compile(r"\b(when|if|after|whenever|once)\b", re.I)

# needs-info is a narrower gap than needs-repro, not a weaker one. The failure
# has been shown; what is missing is the where. Asking every reporter for a
# version is a round trip that reads as dismissal, so the trigger is the
# reporter's own claim that the failure depends on an environment — "works
# locally but not in production", "since upgrading", "only on Windows". That
# claim is what cannot be followed up without knowing which version or which
# machine.
ENV_CLAIM = re.compile(
    r"\b(?:only (?:on|in|with|under)"
    r"|works?(?:ed)? (?:on|in|fine|locally|for me|correctly)"
    r"|used to work"
    r"|(?:since|after) (?:the )?(?:upgrad|updat|bump|switch)\w*"
    r"|stopped working"
    r"|no longer works"
    r"|on my (?:machine|laptop|box)"
    r"|in (?:production|staging|prod))\b",
    re.I,
)
# Anything that names a which or a where. Deliberately generous: a report that
# gestures at a version at all does not need a round trip to start work, and
# over-matching here can only cost a label, never park an issue wrongly.
ENV_DETAIL = re.compile(
    r"\bv?\d+\.\d+(?:\.\d+)?\b"
    r"|\b(?:versions?|os"
    r"|mac ?os|osx|windows|linux|ubuntu|debian|alpine|fedora|arch|wsl"
    r"|docker|kubernetes|k8s|node|npm|python|ruby|rust|golang|java|deno|bun"
    r"|chrome|chromium|firefox|safari|edge"
    r"|arm64|aarch64|x86_64|amd64"
    r"|github actions|runners?)\b",
    re.I,
)

STOPWORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "on",
    "in",
    "at",
    "to",
    "for",
    "of",
    "with",
    "and",
    "or",
    "but",
    "it",
    "its",
    "this",
    "that",
    "when",
    "from",
    "by",
    "as",
    "if",
    "not",
    "no",
    "does",
    "do",
    "doesn't",
    "can",
    "cannot",
}


from globs import compile_glob as _glob_to_re  # noqa: E402

# A path may start with a dot (.github/, .claude/), so the leading dot has to be
# optional rather than absent — dropping it silently unprotects every dotfile
# directory listed in protected_paths. The optional dot must not be guarded by a
# lookbehind: that would refuse every path preceded by a separator, which is how
# relative paths (./infra/deploy.py), traceback paths (File "/app/src/x.py") and
# permalinks are written, and refusing those re-opens the same hole. A sentence's
# full stop is excluded already, since \.? must be followed by a word character.
_PATH_RE = re.compile(r"\.?[\w][\w./-]*/[\w./-]+\.\w+")

# Fenced blocks are quoted evidence, not prose. Sizing counts the files an
# issue asks someone to change, and a pasted traceback is neither.
_FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)


def _paths_in(text: str) -> list[str]:
    """File paths mentioned in the text, in order, without repeats."""
    return list(dict.fromkeys(_PATH_RE.findall(text or "")))


def _text(issue: dict) -> str:
    return f"{issue.get('title') or ''}\n{issue.get('body') or ''}"


@lru_cache(maxsize=32)
def _hint_matcher(needles: tuple) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(needles) + r")\b", re.I)


def _has(text: str, needles) -> bool:
    """Whole words, inflections included.

    `docs?` must not match Dockerfile, but `authentic\\w*` must match
    authentication. An empty hint list matches nothing — `\\b()\\b` would
    otherwise match any word at all.
    """
    needles = tuple(needles)
    if not needles:
        return False
    return bool(_hint_matcher(needles).search(text or ""))


# --- sizing -------------------------------------------------------------------


def classify_size(issue: dict) -> str:
    """How much work this looks like. Feeds batch grouping, not scheduling.

    Hints are read from the title only. A bug report that *mentions* the lint job
    in passing is not a lint-sized change, and bodies quote error output freely.

    Hints alone are not enough to size a real queue, though: they are words
    almost nobody writes in an issue title, so every one of this repo's own open
    issues came out `medium`. Constant size is constant weight, and
    `max_batch_weight` then stops measuring CI cost and just caps the issue
    count. So two signals that ordinary reports do carry decide the rest —
    how many files the issue names, and how much the reporter had to write.
    """
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    checkboxes = len(re.findall(r"^\s*[-*]\s*\[[ xX]\]", body, re.M))
    if _has(title, LARGE_HINTS) or checkboxes >= 6 or len(body) > 2000:
        return "large"
    if _has(title, SMALL_HINTS) or len(body) < 120:
        return "small"

    # Files named in prose are places someone has to change. Files inside a
    # fenced block are evidence: a traceback names five of them and none of
    # them is being edited.
    named = _paths_in(f"{title}\n{_FENCE_RE.sub('', body)}")
    if len(named) >= 3 and len(body) >= 600:
        return "large"
    # One file and a report short enough to be about one thing. Two files could
    # be a move or a caller and its callee, so they stay medium — sizing rounds
    # up, the same way risk does, because an under-sized issue is the one that
    # overfills a batch.
    if len(named) == 1 and checkboxes == 0 and len(body) < 900:
        return "small"
    return "medium"


# --- risk ---------------------------------------------------------------------


def _each_hit(text: str, needles: tuple) -> list[str]:
    """The first match of every hint pattern that matches at all, in list order."""
    return [
        hit.group(0)
        for hit in (_hint_matcher((needle,)).search(text or "") for needle in needles)
        if hit
    ]


def _scored(issue: dict, protected: list[str]) -> tuple[str, str]:
    """Risk, and the evidence for it.

    The title is authoritative in both directions: a security word there is the
    subject, and a `typo` or `docs` there is what the issue is. The body is
    where the collisions live. Read as one text, a body that merely *discussed*
    a dangerous word scored high, and a body that mentioned a test scored low
    (issue #5) — and on this repo's own queue that was nearly every issue, in
    both directions. So the body is read differently: an unambiguous word
    (`password`, `csrf`, `migration`) scores high on its own; a collision-prone
    one (`token`, `session`, `schema`, `permission`, bare `auth`) needs a second,
    different one beside it before the issue counts as being *about* the
    dangerous thing; and the low-risk words are not read from the body at all,
    because nothing about mentioning a test makes a change safe.

    Still rounds up where it can: two different collision-prone words is not a
    high bar, and `risk_reason` says what was seen either way, including a
    single body mention that did not score — the override in commands/triage.md
    is made on that evidence.
    """
    labelled = sorted(set(issue.get("labels") or []) & set(HIGH_RISK_LABELS))
    if labelled:
        return "high", f"the {labelled[0]} label"
    text = _text(issue)
    matchers = [_glob_to_re(p) for p in protected or []]
    for path in _paths_in(text):
        if any(m.match(path) for m in matchers):
            return "high", f"protected path {path}"

    title, body = issue.get("title") or "", issue.get("body") or ""
    hit = _hint_matcher(HIGH_RISK_HINTS).search(title)
    if hit:
        return "high", f'"{hit.group(0)}" in the title'
    hit = _hint_matcher(STRONG_RISK_HINTS).search(body)
    if hit:
        return "high", f'"{hit.group(0)}" in the body'
    colliding = _each_hit(body, COLLIDING_RISK_HINTS)
    if len(colliding) >= 2:
        return "high", f'"{colliding[0]}" and "{colliding[1]}" in the body'
    hit = _hint_matcher(LOW_RISK_HINTS).search(title)
    if hit:
        return "low", f'"{hit.group(0)}" in the title'
    if colliding:
        return "medium", (
            f'"{colliding[0]}" in the body only — one such word is a mention, not a '
            "subject; a second security word beside it, or one in the title, would "
            "score high"
        )
    return "medium", "no risk or safety signal in the text"


def risk_level(issue: dict, protected: list[str]) -> str:
    """Risk gates batching and auto-merge. When unsure, this rounds upward."""
    return _scored(issue, protected)[0]


# --- actionability ------------------------------------------------------------


def _grounded(labels: set[str], text: str, reason: str) -> dict:
    """The verdict for a bug that has shown its failure.

    Usually actionable. The exception is a report that pins the failure on an
    environment it never names: that is `needs-info`, and it is the only route
    to that verdict, so the gate stays deliberately tight.
    """
    if "bug" in labels and ENV_CLAIM.search(text) and not ENV_DETAIL.search(text):
        return {
            "actionable": False,
            "lifecycle": "needs-info",
            "reason": "failure is blamed on an environment the report never names",
        }
    return {"actionable": True, "lifecycle": None, "reason": reason}


def actionability(issue: dict) -> dict:
    """Can someone start on this, or does it need something from the reporter?

    Lifecycle labels apply to bugs only. Anything already carrying evidence, or
    describing an expectation that was violated, is actionable — unless the
    report itself says the failure depends on an environment it never gives.
    """
    labels = set(issue.get("labels") or [])
    if not labels & {"bug"} and (labels & {"question", "enhancement", "feature"}):
        return {"actionable": True, "lifecycle": None, "reason": "not a bug; no lifecycle label"}

    body = issue.get("body") or ""
    text = _text(issue)
    if any(rx.search(body) for rx in EVIDENCE):
        return _grounded(labels, text, "body carries concrete evidence")
    if EXPECTATION.search(text) and CONDITION.search(text):
        return _grounded(
            labels, text, "describes expected vs actual behaviour under a stated condition"
        )
    if "bug" in labels:
        return {
            "actionable": False,
            "lifecycle": "needs-repro",
            "reason": "bug report with no evidence, steps, or stated expectation",
        }
    return {"actionable": True, "lifecycle": None, "reason": "uncategorised; no lifecycle label"}


# --- dedupe -------------------------------------------------------------------


def _tokens(title: str) -> set[str]:
    """Comparable words in a title, stopwords removed.

    Single characters count. They read as noise, but a title's only
    distinguishing content is often one of them — "Bug 1" and "Bug 2" become
    the same title the moment the digit is dropped, and then match at 1.0.
    """
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if t not in STOPWORDS}


def dedupe(issue: dict, others: list[dict], threshold: float = DUPLICATE_THRESHOLD) -> list[dict]:
    """Title-overlap duplicate candidates, best first. Open issues only."""
    mine = _tokens(issue.get("title"))
    if not mine:
        return []
    hits = []
    mine_number = issue.get("number") or 0
    for other in others:
        # Only an older issue can be the original. Matching both ways marks the
        # original a duplicate of its own copy, and neither ever gets worked.
        if (other.get("number") or 0) >= mine_number:
            continue
        if (other.get("state") or "open").lower() != "open":
            continue  # marking a duplicate of a closed issue helps nobody
        theirs = _tokens(other.get("title"))
        if not theirs:
            continue
        shared = mine & theirs
        # A ratio cannot see how much agreement it is made of: two two-word
        # titles reach 1.0 on a single shared word, and a duplicate verdict at
        # maximum confidence takes the newer issue out of the queue until a
        # human notices. Overlap has to be about something, so one word is
        # never enough. The cost is a pair of one-word titles that never gets
        # flagged, which is the direction the skill asks for.
        if len(shared) < MIN_SHARED_TOKENS:
            continue
        score = len(shared) / len(mine | theirs)
        if score >= threshold:
            hits.append(
                {"number": other["number"], "score": round(score, 3), "title": other.get("title")}
            )
    return sorted(hits, key=lambda h: -h["score"])


# --- labels -------------------------------------------------------------------


def plan_labels(record: dict, available: list[str]) -> list[str]:
    """Only labels the repo already defines. Never invent one."""
    have = set(available or [])
    wanted = [
        record.get("kind"),
        f"size:{record.get('size')}",
        record.get("lifecycle"),
        f"risk:{record.get('risk')}",
    ]
    return [item for item in wanted if item and item in have]


# --- skipping -----------------------------------------------------------------


def should_skip(issue: dict, prior: dict) -> bool:
    """Re-triaging an untouched issue spends tokens to reach the same verdict."""
    seen = prior.get(issue.get("number"))
    if not seen:
        return False
    return seen.get("issue_updated_at") == issue.get("updatedAt")


# --- the record ---------------------------------------------------------------


def _kind(issue: dict) -> str:
    labels = set(issue.get("labels") or [])
    for category in ("bug", "enhancement", "question"):
        if category in labels:
            return category
    text = _text(issue).lower()
    if any(w in text for w in ("crash", "fails", "error", "broken", "regression", "wrong")):
        return "bug"
    if text.strip().endswith("?") or text.lstrip().startswith(("how ", "why ", "can ")):
        return "question"
    return "enhancement"


def triage_issue(
    issue: dict, others: list[dict], protected: list[str], available_labels: list[str]
) -> dict:
    duplicates = dedupe(issue, others)
    act = actionability(issue)
    risk, why_risk = _scored(issue, protected)
    record = {
        "issue": issue.get("number"),
        "title": issue.get("title"),
        "kind": "duplicate" if duplicates else _kind(issue),
        "size": classify_size(issue),
        "risk": risk,
        # A high score blocks batching entirely, so the reviewer has to be able
        # to see whether it came from the subject of the issue or from a word
        # it happened to mention.
        "risk_reason": why_risk,
        "lifecycle": None if duplicates else act["lifecycle"],
        "issue_updated_at": issue.get("updatedAt"),
        # batch.can_group needs these; without them nothing ever groups.
        "paths": _paths_in(_text(issue)),
        "rationale": act["reason"],
    }
    if duplicates:
        record["verdict"] = "duplicate"
        record["duplicate_of"] = duplicates[0]["number"]
        record["duplicate_score"] = duplicates[0]["score"]
        record["rationale"] = (
            f"title overlaps #{duplicates[0]['number']} at {duplicates[0]['score']}"
        )
    elif act["lifecycle"]:
        record["verdict"] = act["lifecycle"]
    else:
        record["verdict"] = "actionable"
    record["labels"] = plan_labels(record, available_labels)
    return record


def queueable(records: list[dict]) -> list[dict]:
    """Only actionable issues become work. Everything else waits on someone."""
    return [r for r in records if r.get("verdict") == "actionable"]


# --- gh plumbing --------------------------------------------------------------


def _gh_json(args: list[str]):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None  # no gh on PATH is one more way to have no answer


def fetch_issues(repo: str, limit: int = 50) -> list[dict]:
    raw = (
        _gh_json(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,body,labels,state,updatedAt",
            ]
        )
        or []
    )
    for item in raw:
        item["labels"] = [
            item["name"] if isinstance(item, dict) else item for item in item.get("labels", [])
        ]
    return raw


def fetch_labels(repo: str) -> list[str]:
    raw = _gh_json(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"]) or []
    return [item["name"] for item in raw]


def apply_labels(repo: str, number: int, labels: list[str], wrapper: Path) -> tuple[bool, str]:
    """Write the labels; on failure, say why.

    The reason is the point. Every write on this repo failed with "GraphQL:
    does not have the correct permissions to execute AddLabelsToLabelable" and
    the run reported `{"applied": 0, "failed": [...]}` — a list of numbers with
    nothing to act on, while the explanation sat in a discarded stderr.
    """
    if not labels:
        return True, ""
    args = [str(wrapper), "issue", "edit", str(number), "--repo", repo]
    for label in labels:
        args += ["--add-label", label]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, ""
    reason = (proc.stderr or proc.stdout or "").strip() or f"gh exited {proc.returncode}"
    return False, reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--config",
        help="foreman config (default .foreman/config.json in the repository root)",
    )
    p.add_argument("--ledger", default=".foreman")
    p = sub.add_parser("apply")
    p.add_argument("--repo", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--ledger", default=".foreman")

    args = parser.parse_args(argv)
    here = Path(__file__).resolve().parent

    if args.cmd == "plan":
        sys.path.insert(0, str(here))
        import ledger as ledger_mod

        # Anchored to the repository, and loud when there is nothing to read:
        # an empty config means no protected paths, which silently scores every
        # auth and workflow change as medium and lets it into a batch.
        config = ledger_mod.load_config(args.config)

        # No `.exists()` guard in front of this. `ledger.load` anchors a relative
        # path to the repository and reads a missing file as no events, so the
        # guard could only ever disagree with it — and from a build worktree it
        # did: `.foreman` is not there relative to the cwd, so `prior` read empty,
        # nothing was ever skipped, and every open issue was re-triaged and its
        # labels rewritten on every pass (issue #73).
        prior = ledger_mod.load(Path(args.ledger)).issues
        # The moment the tracker was asked. `apply` may run any time after this
        # — a plan is a file, and a person reads it before applying — and the
        # sightings it records are true as of now, not as of then (issue #80).
        observed_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        issues = fetch_issues(args.repo, args.limit)
        available = fetch_labels(args.repo)
        records, skipped = [], []
        for item in issues:
            if should_skip(item, prior):
                skipped.append(item["number"])
                continue
            records.append(triage_issue(item, issues, config.get("protected_paths", []), available))
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "observed_at": observed_at,
                    "triaged": records,
                    "skipped": skipped,
                    "queueable": [r["issue"] for r in queueable(records)],
                },
                indent=2,
            )
        )
        return 0

    plan = json.loads(Path(args.plan).read_text())
    # The plan names the repository it was built for. The recipe writes every
    # plan to the same /tmp path, so a plan for one repo and an --repo for
    # another is one stale file away — and it relabelled issue numbers in the
    # wrong repository without a word. Nothing is written past this line
    # until the two agree; GitHub compares repo names case-insensitively.
    planned_for = plan.get("repo")
    if planned_for and str(planned_for).lower() != args.repo.lower():
        print(
            f"error: this plan was built for {planned_for}, not {args.repo}; "
            f"rebuild it with --repo {args.repo} rather than applying it here",
            file=sys.stderr,
        )
        return 1
    sys.path.insert(0, str(here))
    import ledger as ledger_mod

    root = Path(args.ledger)
    ledger_mod.init_dir(root)
    # Every sighting this apply writes is dated by when the plan looked at the
    # tracker, not by when apply ran. `loop.merged_leaving_open` compares the
    # sighting against a merged batch's `progress_at`, and a plan built before
    # a merge and applied after it would otherwise assert the issue was open
    # after the merge closed it (issue #80). A plan without the field — one
    # written before it existed — is stamped at apply time, as before.
    stamp = {"observed_at": plan["observed_at"]} if plan.get("observed_at") else {}
    failed, applied = [], 0
    for record in plan.get("triaged", []):
        ok, error = apply_labels(
            args.repo, record["issue"], record.get("labels", []), here / "gh_safe.sh"
        )
        if not ok:
            # No ledger event for work that did not happen. The next run skips
            # any issue whose record matches its updatedAt, so a triaged event
            # written over a failed write is never revisited: the ledger claims
            # a label the issue does not carry, permanently.
            failed.append({"issue": record["issue"], "error": error})
            continue
        applied += 1
        ledger_mod.append(root, "issue.triaged", **{**record, **stamp})
    # Every issue in the plan came back from `fetch_issues`, which asks GitHub
    # for open issues only — so the plan is a list of issues that were open when
    # it was built, whether or not this run re-recorded them. The skipped ones
    # are the point: `should_skip` guarantees no `issue.triaged` is ever written
    # for an issue whose `updatedAt` has not moved, and a PR merging without a
    # closing keyword does not move it. Without this list, `loop._grouped_issues`
    # had no way to learn that a merged batch left its issue open (issue #58).
    # A failed label write is recorded here too: whether a label stuck says
    # nothing about whether the issue is open.
    listed = [r["issue"] for r in plan.get("triaged", [])] + list(plan.get("skipped", []))
    seen_open = list(dict.fromkeys(listed))
    ledger_mod.append(
        root,
        "triage.completed",
        triaged=applied,
        failed=len(failed),
        open_issues=seen_open,
        **stamp,
    )
    print(json.dumps({"applied": applied, "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
