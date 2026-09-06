# Profiling CI

`/foreman:ci-profile` writes `.foreman/ci-profile.json` from measured history.

## Why measured, not declared

Workflow files say what CI *does*, never what it *costs*. A job named `unit` may
take 8 minutes because it builds a Docker image first. Only run history knows.
The profiler reads `gh run list`, the jobs API, and branch protection — all
read-only.

## The fields that drive decisions

**`tier`** — `cheap` when p95 ≤ `tier_threshold_s` (default 300s). Cheap jobs run
on every push; expensive jobs wait behind the cheap tier *and* the review gate.

**`required`** — from branch protection. Only required checks can block a merge.
Waiting on an advisory check is pure wall clock; the loop ignores them for
gating and reads them only for information.

**`protection_known`** — whether branch protection could actually be read.

> This one is load-bearing. When protection is absent or unreadable, every job
> lands in the profile with `required: false`. Read literally, that says nothing
> can block a merge, and a fully red CI becomes a green gate. So `false` here
> means **unknown**, and `land.py` treats every check as required until proven
> otherwise. Never "simplify" that back.

**`samples`** — how many finished runs the numbers come from. `samples: 0` yields
`tier: "unmeasured"`, listed in `unmeasured_jobs`.

> **Treat `unmeasured` as expensive.** A job with no history is most often a new
> or rarely-triggered one — nightly soaks, release jobs — and those are the slow
> ones. Assuming cheap is the assumption that costs you.

**`p95`, not `p50`** — tier assignment uses p95 because the loop's risk is the
bad case. A job that is usually 90 seconds and occasionally 20 minutes is an
expensive job.

**`flake_rate`** — fraction of commits where the job both failed and passed.
Above ~0.1 the job's failures are barely evidence; fix or quarantine it before
trusting anything it says.

**`events`** — the filters each trigger declared, one entry per event. This is
what decides whether a job can produce a check on a pull request at all.

> One job name declared in several workflows is one check name, so those
> declarations collapse into a single config per event. A check appears if *any*
> of them fires — but one filter dict cannot say "`branches: [main]` **or**
> `paths: [src/**]`", and merging key by key says "no filters at all", which
> marks a job requirable that GitHub may never run. So the union is taken only
> where it is exact, and otherwise the declaration that can still report while
> the PR is open is the one kept. Under-requiring costs a wait; over-requiring
> hangs the gate until the staleness timer escalates it.

## Cancelled and skipped runs are excluded

They report the moment someone hit cancel, not the cost of the work. Including
them drags p95 down and mislabels expensive jobs as cheap.

## Refreshing

Re-probe after any workflow change, after adding a job, or every ~2 weeks of
active development. A stale profile is worse than none: the loop trusts it, and
acts on numbers that no longer describe the repo.

## When the profile is thin

New repo, few runs? Do not block. Run with `unmeasured` everywhere — the loop
degrades to "run everything locally, and treat all CI as expensive," which is
correct-but-slow rather than fast-but-wrong.
