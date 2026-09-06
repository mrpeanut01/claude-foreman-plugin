---
name: work-batching
description: Group triaged issues into batches sized by what CI actually costs, so one slow suite run covers several fixes. Use when planning which issues share a PR, deciding a batch is too large, or splitting a batch after a failure.
---

# Work Batching

On a repo with a 40-minute suite, the suite is the unit of cost. It costs the
same whether the PR fixes one issue or five, so grouping compatible issues is the
largest saving available.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" plan --ledger .foreman > /tmp/foreman-batches.json
```

`plan` reads the ledger twice over: for the actionable issues no batch yet
holds, which is what it groups, and for the ids already issued, so new ids
continue past them. Point `--ledger` at the same directory `apply` writes to, or
ids restart at `b-001` and collide with batches that already exist — from any
directory but the repo root, the default is not that directory. `--triage
/tmp/foreman-triage.json` groups a triage file's records instead; an issue a
batch already holds is left out from either source.

## The arithmetic

```
separate PRs:  k × T
one batch:     T           saves (k−1) × T
on failure:    T + T       split, then two runs
```

Break-even sits near `p_fail < (k−1)/k`. At k=3 a batch pays unless more than two
thirds of batches fail. That holds only because splitting is cheap, and splitting
is cheap only because **every issue is its own commit**. Lose that and the whole
case collapses.

## Grouping rules

| Rule | Why |
|------|-----|
| No shared file between two issues | A conflict inside a batch is self-inflicted |
| Both `low` or `medium` risk | One high-risk issue contaminates the batch's blast radius |
| Path information known for both | Absence of evidence is not evidence of independence |
| Combined weight ≤ `max_batch_weight` | Bisecting gets worse faster than savings grow |
| Count ≤ `max_batch_issues` | Same |

A high-risk issue is never dropped — it gets a batch of its own. Solo is not
exclusion.

## Paths are intent until the diff says otherwise

A batch's `paths` are the union of file paths its issues' prose happens to name.
Prose invents files that do not exist and says nothing about files the fix had to
touch. The protected-path merge gate reads `paths`, so gating on the prose alone
guards intent rather than the change.

Once the branch exists, recompute them from it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/batch.py" paths \
  --batch b-001 --base main --repo-dir . --apply
```

| Key | Means |
|-----|-------|
| `paths` | What the branch really changes. The value that should replace the declared list |
| `undeclared` | Changed but never mentioned — the protected file the gate would have missed |
| `untouched` | Mentioned but never changed — usually a file the prose invented |

`--apply` records the real paths as a `batch.meta` event, which is where
`land.py` reads a batch's paths from.

It refuses to record an empty diff, and exits 1 saying so. git exiting 0 with no
output means the branch changes nothing — which is also exactly what you see when
`--repo-dir` names a checkout that does not hold the branch, the usual mistake
being running this from the main worktree while the work sits in a linked one.
Neither is an observation of what the batch touched, and `batch.meta` *replaces*
the path list rather than adding to it, so recording `[]` would clear the list
the protected-path merge gate reads. The declared paths stay put instead. Point
`--repo-dir` at the worktree holding the branch and run it again.

`/foreman:land` step 5 runs it, immediately before asking `blockers`, and
`land.merge_blockers` **refuses** a batch whose paths were never confirmed this
way — or were confirmed against a commit other than the one being merged, which
is what `paths_head` in the `batch.meta` event is for. So the merge gate never
judges prose: an unconfirmed list blocks rather than clears, which is the
difference between "no protected file was mentioned" and "no protected file
was touched".

## The honest downside

A batch is harder to attribute on failure and has a wider blast radius on merge.
Three mitigations, and you need all three:

1. **One commit per issue**, message naming the issue.
2. **`batch.split()` on failure** — peel the failing commit into `b-001a`, let
   `b-001b` carry on. One extra suite run, not a redo.
3. **Risk ceiling**, so a batch is never more dangerous than its safest member
   would suggest.

## When batches come out small

Usually `unknown paths: independence cannot be established` — the issues name no
files. That is the grouper refusing to batch on a guess, which is correct. The
fix is upstream: issue templates that ask where the problem is. Until then,
expect mostly solo batches and a smaller saving.

## Exit criteria

Every actionable issue is in exactly one batch. No batch mixes a high-risk issue
with anything. The savings block was reported, including `seconds_saved: null`
when the repo has no CI profile — an unquantified saving is stated as such rather
than guessed.
