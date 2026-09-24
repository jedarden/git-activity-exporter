# Data sources and their limits

## Forgejo repo enumeration

One endpoint answers "which repos exist": `GET
{FORGE_BASE_URL}/api/v1/repos/search`, called with `Authorization: token
<FORGE_TOKEN>` and query parameters `owner` (pinned to `FORGE_OWNER`),
`limit` (fixed at 50) and `page` (1-based), each request bounded by
`HTTP_TIMEOUT_SECONDS`. It is discovery only — commit data deliberately
does not come from the API, where a page of 50 commits costs ~1s without
stats and ~13s with and the same numbers come free from `git log` against
the local mirror.

**Pagination walks until a short page, and completeness is the point.**
The walker fetches page 1, 2, 3, … and stops after the first page holding
fewer than 50 repos, or an empty one. A full page cannot be known to be
last, so a fleet numbering an exact multiple of 50 always pays one extra
request that comes back empty — the walk errs toward asking again rather
than missing a page. What reaches the cycle is the concatenation of every
page, in page order: a repo on page 7 is exactly as enumerated as one on
page 1. That is load-bearing, not incidental — the mirror prune deletes
anything its live set omits, so a walk that stopped at page 1 would make
every later page look deleted upstream. `REPO_DENYLIST` is applied to the
completed listing, never used to cut the walk short.

**Visibility is what the token can see; there is no client-side filter.**
The search endpoint applies Forgejo's own access rules, so the enumerated
fleet is "repos owned by `FORGE_OWNER` that `FORGE_TOKEN` may read" —
private and public alike, processed identically. Listing visibility and
mirror reachability are different token capabilities, though: in the
token-rotation incident the listing still returned all 112 repos while
clone authentication failed for 97 of them. The failure-rate guard below
is what catches that case; nothing about a shrinking listing is itself an
alarm.

**Two different things are called "empty", handled in two different
places.** A repo whose API payload carries `"empty": true` — created but
never pushed, so it has no HEAD — is dropped during enumeration, before
any clone is attempted. It is equally absent from the prune's live set, so
a mirror left over from before the repo was emptied is deleted the same
cycle, and the repo re-enters the fleet as a cold clone the day it gains a
commit. A *successful enumeration returning zero repos* is the other kind:
it is never accepted as proof the fleet shrank to zero, because a listing
fault (wrong `FORGE_OWNER`, an API change) is indistinguishable from it.
Pruning is skipped for that cycle and the empty result is otherwise
handled as the enumerated fleet — the misconfiguration case heals one
cycle after a fix, while the wrong-deletion case would cost every mirror a
full cold re-clone.

An enumeration that *errors* — an HTTP failure or a timeout — fails the
cycle before any repo is scanned, prunes nothing and publishes nothing;
that rule sits with the other cycle-fatal faults in Failure semantics
below.

## Git — bounded window, explicit coverage

`git log --numstat` against a bare shallow mirror. A healthy cycle covers
every non-empty repo the token can read; failure, stale, and incomplete-history
coverage are explicit in `meta.json` as described below.

`SHALLOW_SINCE_DAYS` is a date bound, not a commit count. The cutoff for a
cycle is the UTC date `generated_at - WINDOW_DAYS - SHALLOW_SINCE_DAYS`. A
new mirror is cloned with that bound. On every later cycle the collector fetches
existing mirrors with the recomputed `--shallow-since` date. When a window
grows and that date moves older—or, equivalently, the computed `SHALLOW_SINCE_DAYS`
cutoff is older than the mirror's existing boundary—the collector then issues
bounded `--deepen` fetches while the existing shallow boundary is still newer
than the requested cutoff.
It does not use `--unshallow` and does not re-clone merely because the bound
grew. This preserves the storage bound while making the widened window's
history available.

After the fetch, the collector checks the mirror's actual shallow boundary. A
full mirror, or a shallow boundary at or before the computed cutoff, is
complete for the requested bound. If the boundary remains newer than that
cutoff, the repository is still scanned and published with the available data,
but its name is recorded in `repos_partial_history` in `meta.json`. The
consumer must treat the lower edge of that repository's history as incomplete;
the marker is separate from `repos_stale` and may overlap it when a timed-out
fetch left the old boundary in place. A partial-history repository is not
silently counted as a fresh, complete mirror and does not become a fetch
failure merely because the bounded deepen could not reach the cutoff.

Merge commits are excluded: git reports no numstat for them, so counting them
would add commit rows that can never carry lines.

Binary files report `-`/`-` in numstat. They count as a touched file and
contribute zero lines.

## Reporting-window boundary contract

A cycle captures `generated_at` once, before collection, and that timestamp is
the sole anchor for every repository and both data sources. The reporting
window is the half-open UTC interval

```text
[generated_at - WINDOW_DAYS, generated_at)
```

The lower bound is inclusive and the upper bound is exclusive. An observation
exactly at the start is published; one exactly at the cycle anchor is deferred
to the next cycle. This makes adjacent cycles disjoint and makes the anchor,
rather than when an individual repository happens to be scanned, determine the
result.

The current UTC hour is included as a partial bucket: activity before the
anchor in the hour containing the anchor is published, but the exporter does
not extend the window to the end of that hour or admit observations at or after
the anchor. A bucket is therefore complete or partial according to where the
cycle anchor falls.

`WINDOW_DAYS` is an elapsed duration of `N × 24` UTC hours, not a local
calendar-day calculation. Offset-bearing timestamps are normalized to UTC
before comparison, so equivalent instants written with different offsets are
one observation. A daylight-saving transition therefore does not make a
reporting day 23 or 25 hours long. Bead timestamps are compared as
timezone-aware instants before publication reduces them to seconds. Producer
timestamps may be offset-bearing ISO 8601 or a decimal Unix-second string;
the latter is interpreted as UTC. An ISO 8601 timestamp without an offset is
malformed and fails its repository. Published event timestamps remain
second-grained.
Commit rows use the Git author timestamp from `%at` and are filtered in Python
against the same interval, so the published timestamp and the boundary test
use the same clock.

## Failure semantics

Specified here because every one of these used to be an accident of
implementation. `GIT_TIMEOUT_SECONDS` (default 600) bounds each git
invocation individually — clone, fetch, `log`, `ls-tree`, `show` — not a cycle.

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
present, merely not newest. A stale repo can also appear in
`repos_partial_history` when its existing shallow boundary is newer than the
recomputed cutoff; the two fields describe independent coverage dimensions.

**Any other fetch failure is treated as corruption and re-clones.** A killed
clone can leave a partial pack that would silently serve wrong numbers, and
git cannot be asked to distinguish; re-cloning is the cheap side of that
trade.

**A timed-out or failed clone excludes the repo from the cycle.** It is
listed in `repos_failed` with the reason in `repo_errors`, and the partial
`<name>.git.tmp` pack is removed from `CLONE_ROOT` rather than left to
accumulate. Clone, fetch, `log`, `ls-tree` and `show` share the one bound;
there is no separate cold-clone timeout.

**A timed-out or failed `log` also excludes the repo.** A timeout while
extracting commits cannot establish complete coverage, so the repo is not
partially published. Its existing mirror is kept, and the repo is listed in
`repos_failed`/`repo_errors`; a later cycle tries again.

**A missing forensic path is an empty success, but a present file is
all-or-nothing.** The exact `.beads/checkpoint/forensic.jsonl` path is first
looked up in the mirror's `HEAD` tree. Only a successful lookup with no entry
means the optional file is absent and produces an empty bead-event result. The
entry must be a regular Git blob; a tree, submodule, or symlink at the path is
a malformed file, not an absent one. A failed lookup, or a failure while
reading a path the lookup found, excludes the repo rather than disguising an
unreadable file as no bead data.

A present forensic file is validated as a whole before the reporting-window
filter is applied. Records are separated by LF, so Unicode line-separator
characters inside JSON strings remain data. Blank lines and recognized
non-event records (`issue`, `attempt_outcome`, `provenance_receipt`, and the
`redaction_finding`, `redaction_acknowledgment`, `redaction_receipt`,
`redaction_epoch`, and `redaction_tombstone` objects) are ignored; a known
non-event record without its object payload, or an unknown `record_type`, is
malformed. Event `detail` may be any JSON value; when it is an object, a
non-null `resulting_base_status` must be a string. Any nonblank line that is
not valid JSON, any non-object event record, any missing or invalid
workspace/sequence, kind, or timestamp, any wrong-typed issue or actor, and any
repeated `(origin_store_uuid, origin_event_sequence)` pair fails the repo.
Sequence numbers must be non-negative integers. A torn final record is
therefore a malformed file, and a valid prefix is never published. Integrity
is checked for events outside the window too, so an old duplicate cannot hide
from the current cycle.

When `.beads/checkpoint/current.json` is present beside the forensic log, the
exporter also requires its `total_record_count` to equal the number of
nonblank forensic records and its `active_root.sha256` to equal the SHA-256 of
the complete forensic bytes. This detects truncation exactly between two
complete JSONL records as well as same-count replacement or reordering. A
present but malformed, non-regular, or unreadable manifest fails the repo. For
older repositories with no manifest, boundary truncation remains
indistinguishable from a shorter complete file and cannot be detected.

A forensic integrity or read failure drops both the repo's Git rows and its
bead rows. It is absent from `repos_scanned`, appears in `repos_failed` with
the reason in `repo_errors`, and does not increment
`repos_with_bead_data`; `bead_epoch_utc`, `attribution_epoch`, and
`bulk_bead_cells` are derived only from retained repos. These failures use the
normal cycle-wide publish guard below: at or below `MAX_FAILURE_RATE` a new
`meta.json` records the gap, and above it the cycle is withheld and the
previous `meta.json` remains live.

**An incomplete shallow boundary is a coverage warning, not a scan failure.**
After either a successful bounded deepen or a stale fetch, the collector checks
that the mirror reaches `generated_at - WINDOW_DAYS - SHALLOW_SINCE_DAYS`. A
repo whose boundary is still newer is scanned from the data it has and listed
in `repos_partial_history`. It remains in `repos_scanned` and may also be in
`repos_stale`; it is disjoint from `repos_failed` and does not count toward the
failure-rate guard. This preserves useful data while making the truncated
lower edge visible to consumers.

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
whose clone, `log`, forensic lookup, or forensic `show` keeps failing appears
in `repos_failed`/`repo_errors` of every cycle that publishes; a repo whose fetch
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
publisher first checks that `cycles/<cycle_id>/` is empty, so an ID collision
is rejected before any payload, fixed-key, or pointer PUT. Payloads then land
under that prefix before anything consumer-visible changes; the first upload
failure aborts there. The orphaned prefix is inert — nothing points at it —
and is swept by the retention prune of a later cycle.

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

**Pruning is retention-bounded and never fatal.** Cycle IDs have the exact
form `<compacted generated_at>-<8 lowercase hex>`, so the fixed-width timestamp
prefix makes descending ID order equal to descending `generated_at` order at
one-second precision. The publisher keeps the committed ID plus the two
highest remaining valid IDs; the committed ID is removed from the ranking
and protected explicitly, so a clock surprise or a reduced retention cannot
delete what `current.json` names. Same-second suffixes provide only a
deterministic tie-break, and malformed or foreign children under `cycles/`
are ignored rather than consuming a slot. A consumer that resolved the
previous pointer therefore has roughly three poll intervals to finish
reading. Older valid prefixes are deleted after the commit; a deletion
failure is logged and left for the next cycle.

## Beads — epoch-bounded, two-thirds coverage

`.beads/checkpoint/forensic.jsonl`, read straight out of the bare mirror with
`git show HEAD:<path>`, plus `.beads/checkpoint/current.json` when that
manifest is present. Both are git-tracked, so this needs no second data source
and no access to any host's live SQLite store.

Three limits the panel must respect:

1. **Epoch, not history.** Every forensic log begins 2026-08-14, the bead-rs
   migration; prior bf ids were discarded by the replay. Git backfills 90
   days on day one; beads cannot be backfilled at all.
2. **Partial coverage.** 64 of the 97 repos that committed in a 30-day sample
   carry a forensic log. Absence is normal for a third of the fleet, not an
   error.
3. **Attribution is epoch-bounded.** A repository's `attribution_epoch` is
   its first `closed` event with a non-`system` actor. Events at or after that
   instant can be partitioned by their recorded actor; earlier events and
   remaining `system` events are labelled `inferential` and are excluded from
   worker counts by default. The repository rollup remains complete. No
   claim-to-close inference is performed, so a release and re-claim cannot
   silently assign a closure to the wrong worker.

## Why the three measures all ship

They agree without being redundant. Over an 82-hour overlap window with the
migration spike excluded: commits↔LOC r=+0.75, commits↔claims r=+0.70,
commits↔beads r=+0.63. Close enough to corroborate each other, far enough
apart that none is a proxy for the others.
