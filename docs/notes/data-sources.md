# Data sources and their limits

## Git — full window, best-effort coverage

`git log --numstat` against a bare shallow mirror. Backfills the entire
window on first run. A healthy cycle covers every non-empty repo the token can
read; failure and stale coverage are explicit in `meta.json` as described
below.

Merge commits are excluded: git reports no numstat for them, so counting them
would add commit rows that can never carry lines.

Binary files report `-`/`-` in numstat. They count as a touched file and
contribute zero lines.

## Failure semantics

Specified here because every one of these used to be an accident of
implementation. `GIT_TIMEOUT_SECONDS` (default 600) bounds each git
invocation individually — clone, fetch, `log`, `show` — not a cycle.

**A timed-out fetch keeps the mirror and serves it stale.** The repo still
appears in the cycle, scanned from the previous cycle's copy, and is listed
in `repos_stale` in `meta.json`. On the first timeout that copy is normally at
most one `POLL_INTERVAL_SECONDS` old; repeated timeouts can make it older, so
the repo remains in `repos_stale` on every published cycle until a fetch
succeeds. Dropping the repo instead would subtract its whole window from the
published cycle; the earlier behavior was strictly worse — a fetch timeout
escalated into deleting the mirror and re-cloning, a longer network operation
that usually timed out too, costing the repo both its fresh data and its
mirror. Stale repos do not count toward the publish guard: their data is
present, merely not newest.

**Any other fetch failure is treated as corruption and re-clones.** A killed
clone can leave a partial pack that would silently serve wrong numbers, and
git cannot be asked to distinguish; re-cloning is the cheap side of that
trade.

**A timed-out or failed clone excludes the repo from the cycle.** It is
listed in `repos_failed` with the reason in `repo_errors`, and the partial
`<name>.git.tmp` pack is removed from `CLONE_ROOT` rather than left to
accumulate. Clone, fetch, `log` and `show` share the one bound; there is no
separate cold-clone timeout.

**A timed-out or failed `log`/`show` also excludes the repo.** A timeout while
extracting commits or reading the forensic log cannot establish complete
coverage, so the repo is not partially published. Its existing mirror is
kept, and the repo is listed in `repos_failed`/`repo_errors`; a later cycle
tries again. A missing `.beads/checkpoint/forensic.jsonl` is not a failure —
that file is optional and produces an empty bead-event result.

**A repo failing does not fail the cycle.** Each failing repo is skipped, the
cycle continues, and the publish guard decides: above `MAX_FAILURE_RATE`
(default 0.2) of repos failed, the cycle is withheld entirely and the
previous cycle's objects stay live; at or below it, the cycle publishes with
the gaps recorded. The fraction exists because of a real incident: a rotated
Forgejo token failed 97 of 112 repos while the 15 public ones kept cloning,
and an every-repo guard stayed quiet while a 6,588-cell dataset was replaced
by a 1,580-cell one.

**Persistent failure has no in-cycle retry or circuit breaker.** The next poll
is the retry, and there is no memory of past cycles across an exporter restart,
deliberately: per-cycle truth is what `meta.json` can honestly state. A repo
whose clone or `log`/`show` keeps failing appears in
`repos_failed`/`repo_errors` of every cycle that publishes; a repo whose fetch
keeps timing out appears in `repos_stale` instead. Comparing successive
`meta.json` objects exposes either form of persistence without silently
discarding the last usable mirror.

**A Forgejo enumeration failure fails the cycle before repo processing.** No
mirrors are pruned and no objects are published; the previous published
objects remain live and the next poll retries enumeration. A successful but
empty enumeration is not treated as proof that the PVC is empty: pruning is
skipped in that case, and the empty result is otherwise handled as the
enumerated fleet.

**Orphaned mirrors are pruned every cycle.** After a successful enumeration,
mirror directories on `CLONE_ROOT` whose repo no longer qualifies — deleted
or renamed on the forge, denylisted, or turned empty — are deleted, along
with `<name>.git.tmp` litter from killed clones. Without this the PVC grew
without bound: every rename and dead repo left a full mirror behind forever.
Pruned repo names are recorded in `mirrors_pruned` so a deletion is
auditable. The prune refuses to run against an empty enumeration — a listing
fault (wrong owner, API change) must not be able to delete the whole mirror
farm — and re-including a pruned repo simply costs one cold clone.

## Publication semantics

Specified alongside the failure semantics above because they are the same
discipline applied to the S3 side: a failed or degraded cycle must never be
able to destroy or blur the last good dataset. The full protocol lives in
[output-schema.md](output-schema.md#publication-protocol); this is the
failure behavior each step defines:

**A Parquet or `meta.json` generation failure uploads nothing.** All four
payloads are built in memory before the first S3 write, so a serialization
failure — a schema drift, an unrepresentable value — fails the cycle with
the previous cycle live everywhere. The pre-protocol loop serialized each
table inside its upload, so the same failure had already overwritten
`hourly.parquet` by the time it fired.

**A staging upload failure leaves the previous cycle committed.** The
payloads land under `cycles/<cycle_id>/` before anything consumer-visible
changes; the first failure aborts there. The orphaned prefix is inert —
nothing points at it — and is swept by the retention prune of a later
cycle.

**A fixed-key mirror or pointer failure rolls the fixed keys back.** The
fixed keys are snapshotted in memory before they are overwritten; any
failure from the first mirror PUT through the commit PUT restores that
snapshot before the cycle fails. After any failed publication, the pointer
and the fixed keys both name the previous complete cycle — never a mixture
of two cycles, never a half-written set. Rollback is best-effort: if the
rollback itself fails (the same outage that broke the publish usually
breaks the restore), the failure is logged and the original cause is what
the cycle reports.

**The commit is one PUT.** `current.json` is written with a single
`put_object`, which is the only atomic operation S3 offers; the cycle
becomes published at exactly that instant and not before. The staged
objects the pointer names are immutable — written once before the commit,
never rewritten — so a consumer reading pointer-then-objects always
assembles one whole cycle regardless of where a concurrent publication
has gotten to.

**Pruning is retention-bounded and never fatal.** Three cycle prefixes are
kept (the committed one plus the two newest), so a consumer that resolved
the previous pointer has roughly three poll intervals to finish reading.
Older prefixes are deleted after the commit; a deletion failure is logged
and left for the next cycle. The committed cycle is protected explicitly,
so no prune can ever delete the cycle the pointer names.

## Beads — epoch-bounded, two-thirds coverage

`.beads/checkpoint/forensic.jsonl`, read straight out of the bare mirror with
`git show HEAD:<path>`. It is git-tracked, so it needs no second data source
and no access to any host's live SQLite store.

Three limits the panel must respect:

1. **Epoch, not history.** Every forensic log begins 2026-08-14, the bead-rs
   migration; prior bf ids were discarded by the replay. Git backfills 90
   days on day one; beads cannot be backfilled at all.
2. **Partial coverage.** 64 of the 97 repos that committed in a 30-day sample
   carry a forensic log. Absence is normal for a third of the fleet, not an
   error.
3. **Attribution is claim-only.** `claimed` events carry a real worker
   identity (measured: 2,683 of 2,683 attributable). `closed`, `released`,
   `updated` and `reopened` are all actor `system` — 0% attributable.
   Inferring who *closed* a bead means joining claim→close on `issue_id`,
   which is wrong whenever a bead is released and re-claimed by another
   worker. `workers_active` therefore counts distinct claimers, and no
   closure is ever attributed to a worker.

## Why the three measures all ship

They agree without being redundant. Over an 82-hour overlap window with the
migration spike excluded: commits↔LOC r=+0.75, commits↔claims r=+0.70,
commits↔beads r=+0.63. Close enough to corroborate each other, far enough
apart that none is a proxy for the others.
