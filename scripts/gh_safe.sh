#!/usr/bin/env bash
# Constrained `gh` wrapper for the foreman loop.
#
# An unattended loop with an unrestricted `gh` can delete an issue, rewrite
# branch protection, or admin-merge past its own gates. This wrapper allows only
# the operations the loop actually needs, refuses the rest, and audits both.
#
#   FOREMAN_DRY_RUN=1   validate and audit without calling gh
#   FOREMAN_LEDGER=DIR  audit location (default .foreman)
set -euo pipefail

LEDGER="${FOREMAN_LEDGER:-.foreman}"
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

refuse() {
  printf '%s\tREFUSED\t%s\t%s\n' "$STAMP" "$*" "$REASON" >>"$AUDIT"
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

printf '%s\tALLOWED\t%s\n' "$STAMP" "$*" >>"$AUDIT"
[ "${FOREMAN_DRY_RUN:-0}" = "1" ] && exit 0
exec gh "$@"
