#!/usr/bin/env bash
# Constrained `gh` wrapper for the foreman loop.
#
# An unattended loop with an unrestricted `gh` can delete an issue, rewrite
# branch protection, or admin-merge past its own gates. This wrapper allows only
# the operations the loop actually needs, refuses the rest, and audits both.
#
#   FOREMAN_DRY_RUN=1   validate and audit without calling gh
#   FOREMAN_LEDGER=DIR  audit location (default .foreman, anchored to the repo)
set -euo pipefail

# --- where the audit log goes -------------------------------------------------
# `commands/build.md` has a build work inside `../foreman-<batch>`, a linked
# worktree that `git worktree remove` deletes once the batch lands, so a
# cwd-relative audit log named a directory with a shorter life than the record
# it held: half of what an unattended loop did to GitHub was written where
# nobody would look for it, and was then thrown away (issue #71). #64 anchored
# the ledger to the repository in `scripts/ledger.py`; this wrapper never runs
# that code, so it anchors the same way, by the same rule -- and an absolute
# FOREMAN_LEDGER is still obeyed verbatim, which is how a caller says "this
# ledger, not the one you would have picked".

repo_root() {
  # `--git-common-dir` rather than `--show-toplevel` on purpose: inside a linked
  # worktree the toplevel is the worktree, which is exactly the wrong answer.
  # The common dir is the one thing every worktree of a repo agrees on.
  local common top
  common=$(git rev-parse --git-common-dir 2>/dev/null) || common=""
  case "$common" in
    "") ;;
    /*) ;;
    *) common="$PWD/$common" ;;
  esac
  # A `.git` directory sits in its working tree; anything else (a bare repo, or
  # --separate-git-dir) does not, so ask where the tree is.
  if [ "${common##*/}" = ".git" ] && [ -d "${common%/*}" ]; then
    printf '%s\n' "${common%/*}"
    return
  fi
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  # No repository here at all: a directory is still a fine place for a log.
  printf '%s\n' "${top:-$PWD}"
}

LEDGER="${FOREMAN_LEDGER:-.foreman}"
case "$LEDGER" in
  /*) ;;
  *) LEDGER="$(repo_root)/$LEDGER" ;;
esac
mkdir -p "$LEDGER"
AUDIT="$LEDGER/gh-audit.log"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# verb:subcommand pairs the loop is permitted to use.
ALLOWED="
issue:view issue:list issue:comment issue:edit issue:create issue:close
pr:view pr:list pr:create pr:comment pr:merge pr:checks pr:diff pr:edit pr:ready pr:review
run:view run:list run:rerun run:watch
repo:view label:list search:issues search:prs cache:list
"
# Whole verbs the loop has no business touching.
DENIED_VERBS="auth release secret variable gist ssh-key gpg-key config alias extension codespace org"

# --- the audit record --------------------------------------------------------
# This log is the only record of what an unattended loop did with write access,
# so a record has to survive whatever ends up in argv. It did not: the old
# tab-separated line reserved the newline as its record separator, and
# `gh issue create --body` and `gh pr create --body` take multi-line markdown as
# a matter of course. One comment sprawled over twenty lines, no record could be
# parsed back, and a body containing the word REFUSED was indistinguishable from
# a genuine refusal -- the log could be forged by the very text it was logging.
#
# So: one JSON object per line, the same JSONL the ledger writes, with argv as
# an array. The array is the point. Joining arguments with spaces cannot
# distinguish `--body "one two"` from `--body one two`, and after an incident
# the difference between those two calls is exactly what is being asked.

json_string() {
  # Encode "$1" as a JSON string literal. Backslash and quote have to go first,
  # then the three control characters with short escapes; the rest of the C0
  # range takes the \uXXXX form (ESC is not hypothetical -- captured terminal
  # output carries colour codes). NUL cannot appear: execve() arguments are
  # NUL-terminated, so the kernel could not have delivered one.
  local s=$1 code ch rep
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  for code in 1 2 3 4 5 6 7 8 11 12 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31; do
    printf -v ch "\\$(printf '%03o' "$code")"
    printf -v rep '\\u%04x' "$code"
    s=${s//"$ch"/"$rep"}
  done
  printf '"%s"' "$s"
}

# audit DECISION REASON [ARG...] -- REASON is "" for an allowed call.
audit() {
  local decision=$1 reason=$2 record argv="" arg
  shift 2
  for arg in "$@"; do
    argv="${argv:+$argv,}$(json_string "$arg")"
  done
  record="{\"ts\":$(json_string "$STAMP")"
  record="$record,\"decision\":$(json_string "$decision")"
  record="$record,\"argv\":[$argv]"
  if [ -n "$reason" ]; then
    record="$record,\"reason\":$(json_string "$reason")"
  fi
  printf '%s}\n' "$record" >>"$AUDIT"
}

refuse() {
  audit REFUSED "$REASON" "$@"
  echo "refused: $REASON" >&2
  echo "  attempted: gh $*" >&2
  exit 2
}

[ $# -ge 1 ] || { REASON="no subcommand given"; refuse "$@"; }

VERB="$1"
SUB="${2:-}"

for denied in $DENIED_VERBS; do
  if [ "$VERB" = "$denied" ]; then
    REASON="gh $VERB is never available to the loop (credentials, releases, or org state)"
    refuse "$@"
  fi
done

if [ "$SUB" = "delete" ]; then
  REASON="deletion is unrecoverable; a human does this or nobody does"
  refuse "$@"
fi

for arg in "$@"; do
  case "$arg" in
    --admin)
      REASON="--admin merges past the very gates the loop exists to enforce"
      refuse "$@" ;;
    --force|--force-with-lease)
      REASON="force operations are not available to the loop"
      refuse "$@" ;;
  esac
done

# `gh api` is read-only here. Anything that mutates has a named subcommand
# above, which is easier to audit than an arbitrary endpoint plus a verb.
if [ "$VERB" = "api" ]; then
  prev=""
  for arg in "$@"; do
    case "$prev" in
      -X|--method)
        if [ "$arg" != "GET" ]; then
          REASON="gh api is read-only for the loop; refusing method $arg"
          refuse "$@"
        fi ;;
    esac
    case "$arg" in
      --input|-f|--field|--raw-field)
        REASON="gh api request bodies are not available to the loop"
        refuse "$@" ;;
    esac
    prev="$arg"
  done
else
  case " $(echo $ALLOWED) " in
    *" ${VERB}:${SUB} "*) ;;
    *)
      REASON="gh ${VERB} ${SUB} is not on the foreman allowlist"
      refuse "$@" ;;
  esac
fi

audit ALLOWED "" "$@"
[ "${FOREMAN_DRY_RUN:-0}" = "1" ] && exit 0
exec gh "$@"
