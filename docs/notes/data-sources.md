# Data sources and their limits

## Git — full window, complete coverage

`git log --numstat` against a bare shallow mirror. Backfills the entire
window on first run. Covers every repo the token can read.

Merge commits are excluded: git reports no numstat for them, so counting them
would add commit rows that can never carry lines.

Binary files report `-`/`-` in numstat. They count as a touched file and
contribute zero lines.

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
