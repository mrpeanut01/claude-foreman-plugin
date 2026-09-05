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
    triage.py plan --repo OWNER/NAME [--limit 50] [--json]
    triage.py apply --repo OWNER/NAME --plan plan.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CATEGORIES = ("bug", "enhancement", "question", "duplicate")
DUPLICATE_THRESHOLD = 0.6

LARGE_HINTS = ("redesign", "rewrite", "refactor", "migrate", "migration", "overhaul",
               "architecture", "epic", "re-architect", "port to")
SMALL_HINTS = ("typo", "bump", "rename", "readme", "changelog", "whitespace",
               "lint", "formatting", "spelling", "docstring")
LOW_RISK_HINTS = ("typo", "doc", "docs", "documentation", "readme", "comment",
                  "test", "tests", "coverage", "lint", "format", "rename", "changelog")
HIGH_RISK_HINTS = ("auth", "token", "password", "session", "credential", "permission",
                   "migration", "migrate", "schema", "payment", "billing", "secret",
                   "csrf", "xss", "injection", "encryption", "privilege", "logged in")
HIGH_RISK_LABELS = ("security", "critical", "data-loss", "p0")

# Evidence that a reporter has already given someone enough to work with.
EVIDENCE = (
    re.compile(r"traceback|stack ?trace|exception in|panic:", re.I),
    re.compile(r'File "[^"]+", line \d+'),
    re.compile(r"\b[A-Z]{3,}[A-Z_]*\b"),                 # ECONNREFUSED, ENOENT
    re.compile(r"\b[45]\d\d\b"),                          # HTTP status codes
    re.compile(r"[\w./-]+\.(py|js|ts|tsx|rs|go|rb|java|kt|c|cpp|sh)\b"),
    re.compile(r"`[^`]+`"),                               # a command or symbol
    re.compile(r"^\s*\d+[.)]\s+\S", re.M),                # numbered steps
    re.compile(r"\bsteps?\s+to\s+reproduce\b", re.I),
)
# A described expectation is a repro for behaviour bugs, where "run this and
# watch" does not apply.
EXPECTATION = re.compile(r"\b(should|expected|instead of|rather than|ought to)\b", re.I)
CONDITION = re.compile(r"\b(when|if|after|whenever|once)\b", re.I)

STOPWORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "on", "in", "at", "to",
             "for", "of", "with", "and", "or", "but", "it", "its", "this", "that", "when",
             "from", "by", "as", "if", "not", "no", "does", "do", "doesn't", "can", "cannot"}


from globs import compile_glob as _glob_to_re  # noqa: E402


def _paths_in(text: str) -> list[str]:
    return re.findall(r"[\w][\w./-]*/[\w./-]+\.\w+", text or "")


def _text(issue: dict) -> str:
    return f"{issue.get('title') or ''}\n{issue.get('body') or ''}"


def _has(text: str, needles) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


# --- sizing -------------------------------------------------------------------

def classify_size(issue: dict) -> str:
    """How much work this looks like. Feeds batch grouping, not scheduling."""
    text = _text(issue)
    body = issue.get("body") or ""
    checkboxes = len(re.findall(r"^\s*[-*]\s*\[[ xX]\]", body, re.M))
    if _has(text, LARGE_HINTS) or checkboxes >= 6 or len(body) > 2000:
        return "large"
    if _has(text, SMALL_HINTS) or len(body) < 120:
        return "small"
    return "medium"


# --- risk ---------------------------------------------------------------------

def risk_level(issue: dict, protected: list[str]) -> str:
    """Risk gates batching and auto-merge. When unsure, this rounds upward."""
    if set(issue.get("labels") or []) & set(HIGH_RISK_LABELS):
        return "high"
    text = _text(issue)
    matchers = [_glob_to_re(p) for p in protected or []]
    for path in _paths_in(text):
        if any(m.match(path) for m in matchers):
            return "high"
    if _has(text, HIGH_RISK_HINTS):
        return "high"
    if _has(text, LOW_RISK_HINTS):
        return "low"
    return "medium"


# --- actionability ------------------------------------------------------------

def actionability(issue: dict) -> dict:
    """Can someone start on this, or does it need something from the reporter?

    Lifecycle labels apply to bugs only. Anything already carrying evidence, or
    describing an expectation that was violated, is actionable.
    """
    labels = set(issue.get("labels") or [])
    if not labels & {"bug"} and (labels & {"question", "enhancement", "feature"}):
        return {"actionable": True, "lifecycle": None, "reason": "not a bug; no lifecycle label"}

    body = issue.get("body") or ""
    text = _text(issue)
    if any(rx.search(body) for rx in EVIDENCE):
        return {"actionable": True, "lifecycle": None, "reason": "body carries concrete evidence"}
    if EXPECTATION.search(text) and CONDITION.search(text):
        return {"actionable": True, "lifecycle": None,
                "reason": "describes expected vs actual behaviour under a stated condition"}
    if "bug" in labels:
        return {"actionable": False, "lifecycle": "needs-repro",
                "reason": "bug report with no evidence, steps, or stated expectation"}
    return {"actionable": True, "lifecycle": None, "reason": "uncategorised; no lifecycle label"}


# --- dedupe -------------------------------------------------------------------

def _tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower())
            if t not in STOPWORDS and len(t) > 1}


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
        score = len(mine & theirs) / len(mine | theirs)
        if score >= threshold:
            hits.append({"number": other["number"], "score": round(score, 3),
                         "title": other.get("title")})
    return sorted(hits, key=lambda h: -h["score"])


# --- labels -------------------------------------------------------------------

def plan_labels(record: dict, available: list[str]) -> list[str]:
    """Only labels the repo already defines. Never invent one."""
    have = set(available or [])
    wanted = [record.get("kind"), f"size:{record.get('size')}",
              record.get("lifecycle"), f"risk:{record.get('risk')}"]
    return [l for l in wanted if l and l in have]


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


def triage_issue(issue: dict, others: list[dict], protected: list[str],
                 available_labels: list[str]) -> dict:
    duplicates = dedupe(issue, others)
    act = actionability(issue)
    record = {
        "issue": issue.get("number"),
        "title": issue.get("title"),
        "kind": "duplicate" if duplicates else _kind(issue),
        "size": classify_size(issue),
        "risk": risk_level(issue, protected),
        "lifecycle": None if duplicates else act["lifecycle"],
        "issue_updated_at": issue.get("updatedAt"),
        "rationale": act["reason"],
    }
    if duplicates:
        record["verdict"] = "duplicate"
        record["duplicate_of"] = duplicates[0]["number"]
        record["duplicate_score"] = duplicates[0]["score"]
        record["rationale"] = f"title overlaps #{duplicates[0]['number']} at {duplicates[0]['score']}"
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
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def fetch_issues(repo: str, limit: int = 50) -> list[dict]:
    raw = _gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit),
                    "--json", "number,title,body,labels,state,updatedAt"]) or []
    for item in raw:
        item["labels"] = [l["name"] if isinstance(l, dict) else l for l in item.get("labels", [])]
    return raw


def fetch_labels(repo: str) -> list[str]:
    raw = _gh_json(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"]) or []
    return [l["name"] for l in raw]


def apply_labels(repo: str, number: int, labels: list[str], wrapper: Path) -> bool:
    if not labels:
        return True
    args = [str(wrapper), "issue", "edit", str(number), "--repo", repo]
    for label in labels:
        args += ["--add-label", label]
    return subprocess.run(args, capture_output=True, text=True).returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--config", default=".foreman/config.json")
    p.add_argument("--ledger", default=".foreman")
    p = sub.add_parser("apply")
    p.add_argument("--repo", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--ledger", default=".foreman")

    args = parser.parse_args(argv)
    here = Path(__file__).resolve().parent

    if args.cmd == "plan":
        config = {}
        if Path(args.config).exists():
            config = json.loads(Path(args.config).read_text())
        sys.path.insert(0, str(here))
        import ledger as ledger_mod
        prior = ledger_mod.load(Path(args.ledger)).issues if Path(args.ledger).exists() else {}
        issues = fetch_issues(args.repo, args.limit)
        available = fetch_labels(args.repo)
        records, skipped = [], []
        for item in issues:
            if should_skip(item, prior):
                skipped.append(item["number"])
                continue
            records.append(triage_issue(item, issues, config.get("protected_paths", []), available))
        print(json.dumps({"repo": args.repo, "triaged": records, "skipped": skipped,
                          "queueable": [r["issue"] for r in queueable(records)]}, indent=2))
        return 0

    plan = json.loads(Path(args.plan).read_text())
    sys.path.insert(0, str(here))
    import ledger as ledger_mod
    root = Path(args.ledger)
    ledger_mod.init(root.parent if root.name == ledger_mod.LEDGER_DIR else root)
    failed = []
    for record in plan.get("triaged", []):
        ok = apply_labels(args.repo, record["issue"], record.get("labels", []), here / "gh_safe.sh")
        if not ok:
            failed.append(record["issue"])
        ledger_mod.append(root, "issue.triaged", **record)
    print(json.dumps({"applied": len(plan.get("triaged", [])) - len(failed), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
