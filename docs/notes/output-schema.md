# Output schema

Four data objects under `DEST_S3_PREFIX` each cycle, plus the publication
pointer that makes reading them atomic. `hourly.parquet` is what the panel
loads; `commits.parquet` and `bead_events.parquet` are the event-grain
detail behind it and the two join sources of the factory attempt ledger
(NEEDLE plan section 4.4; the sink and the joins themselves are owned by
`declarative-config`).

| Object | Grain | Purpose |
|---|---|---|
| `current.json` | — | the pointer: names the committed cycle; the atomic commit of every publication |
| `cycles/<cycle_id>/hourly.parquet` | `(repo, hour, worker)` | repository aggregates plus optional worker partitions |
| `cycles/<cycle_id>/commits.parquet` | commit | drill-down detail; the commit side of the ledger's run join |
| `cycles/<cycle_id>/bead_events.parquet` | forensic event | bead lifecycle detail; the ledger's bead-side join source |
| `cycles/<cycle_id>/meta.json` | — | freshness, coverage, and the caveats a consumer must display |
| `hourly.parquet`, `commits.parquet`, `bead_events.parquet`, `meta.json` (prefix root) | — | legacy fixed keys, kept in sync for consumers that have not moved to the pointer |

## Publication protocol

S3 has exactly one atomic primitive — a single-object PUT — and no
multi-object transaction. The protocol turns that into a whole-cycle
commit:

1. **Generate.** All four payloads are built in memory before any S3 write.
   A Parquet or `meta.json` generation failure raises with nothing
   uploaded; the previous cycle stays live everywhere.
2. **Stage.** The payloads are uploaded to `cycles/<cycle_id>/`, a prefix no
   consumer reads until it is pointed at. Before the first upload the
   publisher checks that this prefix is empty; an existing prefix is a
   `cycle_id` collision and the publication fails without changing the
   committed or fixed-key dataset. A staged cycle's objects are **immutable**:
   they are written once, before the commit, and never rewritten. An upload
   failure here aborts with the previous cycle still live; the orphaned
   prefix is swept by a later cycle's prune.
3. **Mirror.** The staged payloads are copied to the fixed keys at the
   prefix root — `hourly.parquet`, `commits.parquet`, `bead_events.parquet`,
   then `meta.json` **last** — for consumers that predate the pointer. A
   mirror failure rolls the fixed keys back to their previous state before
   the cycle fails, so a failed publication never leaves a half-mirrored
   set behind.
4. **Commit.** One atomic PUT of `current.json`:
   `{"schema_version", "cycle_id", "generated_at", "objects": {name → key}}`,
   where keys are relative to the prefix. This PUT is the only instant at
   which the published dataset changes. A failure rolls the mirror back too
   and the cycle fails with the previous cycle still committed.
5. **Prune.** The committed cycle and the newest two others are kept
   (three cycles ≈ three poll intervals of grace for a reader that resolved
   the previous pointer); older prefixes are deleted, best-effort. "Newest"
   means the cycle ID order defined below, not S3 modification time.

**Consumers should read pointer-first:** GET `current.json`, then the
objects its `objects` mapping names, resolving keys against the prefix the
pointer came from. Because a cycle's staged objects are immutable and the
pointer swap is one atomic PUT, such a read always assembles exactly one
whole cycle — whichever cycle it saw — even while a publication is in
flight. Do not cache pointer-resolved URLs across polls: pruning will
eventually delete the cycle they name.

The fixed keys remain for consumers that have not moved to the pointer
(the static panel reads `hourly.parquet` + `meta.json` directly). They
carry the pre-protocol guarantee plus the rollback rule; a reader landing
between the mirror's PUTs can still interleave, exactly as it always
could. `meta.json`'s `cycle_id` vs `current.json`'s tells such a reader it
crossed a publication boundary. The pointer is the migration target.

Publication is single-writer by deployment: one exporter replica owns the
prefix.

### Cycle identity and retention ordering

`generated_at` is captured once at the start of a cycle and is the cycle's
only clock value. It is a real UTC calendar timestamp in the exact form
`YYYY-MM-DDTHH:MM:SSZ`, with one-second precision and an explicit `Z`. A
cycle ID has the exact grammar:

```text
<YYYYMMDDTHHMMSSZ>-<8 lowercase hexadecimal characters>
```

The first component is the `generated_at` value with `-` and `:` removed, not
an S3 timestamp and not the publication landing time. For example,
`2026-09-06T05:00:00Z` produces a prefix of `20260906T050000Z`. The second
component is the first eight hexadecimal characters of a UUID4 value: 32
random bits. IDs with different `generated_at` values therefore cannot
collide; IDs in the same second are distinct probabilistically, not by an
absolute guarantee.

The publisher treats any existing object below `cycles/<cycle_id>/` as a
collision. It raises a publication error before the first payload, fixed-key,
or pointer PUT; it never overwrites or reuses that prefix. The failed attempt
leaves the previous pointer and fixed keys intact, and the next poll mints a
new ID. The single-writer deployment rule makes the existence check and the
subsequent writes a serialized operation; another writer is not supported.

Retention lists immediate children below `cycles/`, accepts only IDs matching
the grammar above (and a real calendar timestamp), and sorts the valid IDs
lexicographically in descending order. Because the timestamp component is
fixed-width, that is newest `generated_at` first. Within one second the
suffix is a deterministic lexical tie-break, not evidence of collection order.
Malformed or foreign children are ignored and left untouched. The committed
ID is removed from the candidate list and retained explicitly; the remaining
`RETAINED_CYCLES - 1` highest valid IDs are the grace set. Thus "newest" is
defined by embedded `generated_at` order, while the pointer's cycle is always
protected even if the clock moves backward or retention is reduced.

Column types are the Parquet types as written. "Null when" describes the
values actually seen; nothing in this file is a `NaN` or a sentinel string.

All three Parquet files hold only the reporting window (`WINDOW_DAYS`, 90 by
default), and every timestamp is UTC with an explicit `Z` — a naive isoformat
gets read as browser-local time and shifts every chart. The window is
`[generated_at - WINDOW_DAYS, generated_at)`: the start is inclusive, the cycle
anchor is exclusive, and the UTC hour containing the anchor is retained as a
partial bucket. The full boundary, timezone, and DST contract is in
[data-sources.md](data-sources.md#reporting-window-boundary-contract).

## `hourly.parquet`

One row per `(repo, hour, worker)` partition that saw counted activity. The
`worker` value is null for the repository aggregate, the actor for a
post-epoch event, or `inferential` for activity before that repository's
attribution epoch. Ecosystem, family and repo tiers use the null-worker rows;
worker views use the named rows and should exclude `inferential` by default.
There are no per-tier files, so tiers cannot drift apart.

| Column | Type | Null when | Notes |
|---|---|---|---|
| `hour_utc` | string | never | `YYYY-MM-DDTHH:00:00Z`, the hour bucket |
| `hour_epoch` | int64 | never | hours since the epoch; the numeric form of `hour_utc` |
| `repo` | string | never | Forgejo repo name |
| `family` | string | never | from `families.yaml`; `unassigned` when unmapped |
| `worker` | string | repository rows | null for the repo aggregate, the attributed actor for post-epoch rows, or `inferential` before the repo epoch |
| `commits` | int64 | never, may be 0 | |
| `bulk_commits` | int64 | never, may be 0 | commits flagged bulk (see the LOC filter below) |
| `lines_added` / `lines_deleted` | int64 | never | excluded-path-filtered totals; bulk commits contribute 0 |
| `lines_added_raw` / `lines_deleted_raw` | int64 | never | unfiltered totals; bulk commits contribute in full |
| `files_changed` | int64 | never | excluded paths and bulk commits contribute 0 |
| `beads_closed` | int64 | never | closures outside flagged bulk-import hours |
| `beads_closed_bulk` | int64 | never | closures inside flagged bulk-import hours |
| `beads_claimed` | int64 | never | |
| `beads_released` | int64 | never | |
| `beads_reopened` | int64 | never | |
| `workers_active` | int64 | never | distinct claimers in a repository row; a named worker row has 1 when it claimed in the cell, and an `inferential` row has 0 |

The repository row remains the complete `(repo, hour)` rollup, including
activity before the attribution epoch. Named and `inferential` rows are
additive event partitions: a worker view sums only its selected `worker`, while
the default worker-count view omits `inferential`. Commits and lines have no
worker identity, so they appear only on repository rows.

## `commits.parquet`

One row per commit in the window. Merge commits are absent — git reports no
numstat for them, so counting them would add rows that can never carry
lines.

| Column | Type | Null when | Notes |
|---|---|---|---|
| `sha` | string | never | full 40-char SHA; the join key for CI runs |
| `repo` | string | never | |
| `family` | string | never | |
| `ts_utc` | string | never | author time, RFC 3339 UTC |
| `hour_utc` | string | never | joins `hourly.parquet` |
| `author_email` | string | never | |
| `subject` | string | never | first line only |
| `bead_id` | string | no bead referenced | from a `Bead-Id:`-style trailer, else a `fix(<bead-id>):` scope; a non-bead scope is not guessed into one |
| `lines_added` / `lines_deleted` | int64 | never | filtered; see the LOC filter below |
| `files_changed` | int64 | never | |
| `lines_added_raw` / `lines_deleted_raw` | int64 | never | unfiltered |
| `is_bulk` | bool | never | above `TRIM_MAX_LINES` or `TRIM_MAX_FILES` |

## `bead_events.parquet`

One row per forensic event in the window — **every** kind the forensic log
records, not only the four the hourly rollup counts. This file is a join
source, not a chart source: `created`, `updated`, label and dependency edits
are noise for a chart and exactly what a per-bead timeline needs.

| Column | Type | Null when | Notes |
|---|---|---|---|
| `ts_utc` | string | never | the forensic event's `time`, second-grained; sub-second order within one second is not recoverable from this file |
| `hour_utc` | string | never | joins the repository row in `hourly.parquet`; named/inferential worker rows share the same hour |
| `repo` | string | never | the repo whose mirror carried the forensic log |
| `family` | string | never | |
| `workspace_uuid` | string | never (measured) | the forensic log's `origin_store_uuid`: the bead workspace the event happened in |
| `issue_id` | string | workspace-level events | the bead id, as bead-rs wrote it |
| `kind` | string | never | `created`, `claimed`, `closed`, `released`, `reopened`, `updated`, `label_added`, `dependency_added`, … |
| `actor` | string | never observed | identity bead-rs recorded for the mutation; see the attribution epoch below |
| `resulting_status` | string | events that move no status | the bead's `base_status` after the event |
| `is_bulk_import` | bool | never | true when a closure falls in a flagged bulk-import hour; always false for other kinds |

Two properties a consumer must not get wrong:

**The row count reconciles with `hourly.parquet` only per kind.** A closure
row is one `beads_closed`/`beads_closed_bulk` unit, so the sum over
`hourly` rows with `worker IS NULL` always equals the count of
`kind = 'closed'` rows. Named and `inferential` rows are additional
partitions, not a second copy of the repository total. The total row count
matches no hourly column, because `updated` and label/dependency events are
published here and counted nowhere.

**`resulting_status` is stated, not inferred.** `claimed`, `released` and
`reopened` record it themselves in the event's `detail.resulting_base_status`
(`in_progress`, `open`, `open`). A `closed` event's detail records only the
*prior* status and the close reason, so its result is the kind itself:
`closed`. Kinds that move no status — `created`, `updated`, label and
dependency edits — are null rather than guessed. If bead-rs starts stating
the result on closes, this column takes the stated value with no format
change.

## `meta.json`

```json
{
  "version": "0.1.5",
  "cycle_id": "20260906T050000Z-9f2c1a4b",
  "generated_at": "2026-09-06T05:00:00Z",
  "window_days": 90,
  "repos_total": 112,
  "repos_scanned": 111,
  "repos_failed": ["another-repo"],
  "repo_errors": {"another-repo": "git clone ... timed out after 600s"},
  "repos_stale": ["one-repo"],
  "mirrors_pruned": ["a-renamed-repo"],
  "repos_with_bead_data": 64,
  "git_timeout_seconds": 600,
  "cycle_seconds": 148.6,
  "bead_epoch_utc": "2026-08-14T16:42:03Z",
  "attribution_epoch": {
    "gitact-repo": "2026-09-06T05:12:09Z"
  },
  "bulk_bead_cells": 12,
  "unassigned_repos": [],
  "trim_max_lines": 5000,
  "trim_max_files": 200,
  "excluded_path_patterns": ["\\.beads/", "..."]
}
```

Every field is required and present in every published object;
`bead_epoch_utc` is the only one that can be null. There is no
`schema_version` on this object — the pointer has one. This file's stability
is enforced the other way round: the key set is drift-tested against
`main.build_meta` in `tests/test_docs.py`, and `tests/test_meta_schema.py`
validates the example, the table below and the builder against one
executable contract, fixtures included. A field cannot appear, vanish,
change type, or lose one of the relations in the table without failing CI.

| Field | Type | Null when | Notes |
|---|---|---|---|
| `version` | string | never | The exporter's own version (`VERSION_FILE`), `"unknown"` if unreadable. Identifies the writer, not the document format. |
| `cycle_id` | string | never | Exact `generated_at` compacted (`2026-09-06T05:00:00Z` → `20260906T050000Z`) plus eight lowercase hexadecimal characters from UUID4. It is identical to `current.json`'s `cycle_id`; the timestamp portion orders retention, with the suffix only breaking same-second ties. |
| `generated_at` | string | never | RFC 3339 UTC with an explicit `Z`. When collection *began* — it can trail the pointer's landing by up to `cycle_seconds`. |
| `window_days` | int | never | The window every Parquet file of the same cycle was cut to. |
| `repos_total` | int | never | Repos enumerated this cycle: denylist applied, forge-empty repos already dropped. |
| `repos_scanned` | int | never | Repos that contributed data. `repos_scanned + len(repos_failed) == repos_total` holds in every cycle. |
| `repos_failed` | list[string] | never (may be empty) | Repos absent from this cycle — clone, fetch, `log` or `show` failed — in enumeration order. Why each one failed is in `repo_errors`. |
| `repo_errors` | map string→string | never (may be empty) | Keys are exactly `repos_failed`; values are human-readable reasons, credential-scrubbed at the source and truncated to 200 chars. |
| `repos_stale` | list[string] | never (may be empty) | Scanned this cycle from a mirror whose fetch timed out — present, merely not newest. Disjoint from `repos_failed`, a subset of the scanned set, and deliberately not counted as failure. The first timeout costs at most one poll interval of freshness; repeated timeouts can cost more. |
| `mirrors_pruned` | list[string] | never (may be empty) | Mirrors deleted this cycle because their repo was deleted, renamed, denylisted or emptied on the forge; recorded so a deletion is auditable rather than silent. |
| `repos_with_bead_data` | int | never | Scanned repos whose forensic log produced at least one event in the window; never above `repos_scanned`. Absence is the normal case for roughly a third of the fleet. |
| `git_timeout_seconds` | int | never | The per-invocation bound the failure semantics are defined against, echoed so a consumer diagnosing timeouts sees what the exporter was actually given. |
| `cycle_seconds` | number | never | Wall-clock cost of the cycle at 0.1 s resolution. A value approaching `POLL_INTERVAL_SECONDS` is degradation even when every repo succeeded. |
| `bead_epoch_utc` | string | no scanned repo produced a bead event in the window | Earliest bead event of any kind in this cycle's window; see below. |
| `attribution_epoch` | map string→string | never (may be empty) | Per-repo UTC timestamp of the first `closed` event with a non-`system` actor in this cycle's window. A missing repo has no observed attribution epoch. |
| `bulk_bead_cells` | int | never | `(repo, hour)` cells flagged as bulk imports; their closures carry `is_bulk_import` on `bead_events.parquet` and are counted into `beads_closed_bulk`, so excluding them stays reconcilable. |
| `unassigned_repos` | list[string] | never (may be empty) | Sorted and unique. Repos with activity in the window whose `families.yaml` mapping is missing; a scanned repo with no window activity cannot appear. |
| `trim_max_lines` | int | never | The bulk-commit LOC bound behind `is_bulk`. |
| `trim_max_files` | int | never | The bulk-commit file-count bound behind `is_bulk`. |
| `excluded_path_patterns` | list[string] | never | The `re.search` patterns separating `lines_*` from `lines_*_raw`, as configured (defaults in [configuration.md](configuration.md#default-excluded-path-patterns)). |

### Freshness, coverage, and withheld cycles

`generated_at` is how a consumer tells a stalled exporter from a quiet
fleet. It is stamped when the cycle begins, and a cycle that fails — a
generation fault, a staging upload, the publish guard — publishes nothing,
so the previous cycle's objects and their `generated_at` stay in place and
the timestamp ages while the data does not. A `generated_at` older than a
couple of poll intervals is an alarm, not a lull.

Coverage reads straight off the fields: `repos_total` splits into
`repos_scanned` plus `repos_failed`, and `repos_scanned` further splits into
fresh and `repos_stale`. A healthy cycle has `repos_failed`, `repos_stale`
and `mirrors_pruned` all empty; anything else is stated here rather than
inferred from missing rows.

A cycle whose failure rate exceeds `MAX_FAILURE_RATE` (default 0.2) is
withheld entirely instead of published partial. **None of these fields
updates when that happens, because nothing was published** — there is no
partial `meta.json`, ever. The live object keeps describing the last
*published* cycle; comparing successive published cycles is exactly how
persistent failure is meant to become visible
([data-sources.md](data-sources.md#failure-semantics)).

### `bead_epoch_utc`

Bead data has an epoch, not a history: every forensic log in the fleet
begins at the 2026-08-14 bead-rs migration, and nothing before it can be
reconstructed. Git backfills the whole window on day one; beads cannot. The
panel needs the epoch to caption the bead charts honestly instead of
drawing an empty left half.

The field is the earliest bead event of any kind in *this cycle's window*,
minimum over every scanned repo — a measurement of this cycle's data, not a
constant. With the default `WINDOW_DAYS` of 90 it currently coincides with
the migration instant; shrink the window below the age of bead data and it
moves forward to the window's first event. `null` means this cycle's
Parquet files contain no bead rows at all: suppress the bead charts rather
than chart emptiness.

`attribution_epoch` is a separate per-repo bound: it is the first
`closed` event with a non-`system` actor in this cycle's window. A repository
absent from that map has no observed attribution epoch. The map is a property
of the events present in this cycle, so it can move when the window or repo
coverage changes.

### Consumer caveats

- **Read pointer-first.** On the fixed legacy keys, compare `cycle_id` with
  `current.json`'s: a mismatch means the read crossed a publication
  boundary, and the four fixed keys may interleave two cycles.
- **`meta.json` is the fixed keys' completion marker.** The mirror writes
  it last (publish.py `_meta_last`), so a fixed-key reader that sees a new
  `generated_at` knows the other three fixed keys were already replaced.
- **`repo_errors` is prose, not an interface.** Reasons are truncated and
  formatted for humans; match repos on `repos_failed`, never on the shape
  of an error string.
- **The example's values are illustrative.** What is contracted is the key
  set, the types, and the relations in the table — coverage arithmetic,
  failed/stale disjointness, `repo_errors` keys = `repos_failed`, and
  `cycle_id` naming `generated_at` with the exact eight-hex grammar. All of it
  is enforced on fixtures and on the builder's real output by
  `tests/test_meta_schema.py`.

## Join keys

How the factory attempt ledger joins this exporter's objects to the attempt
ledger and to CI runs. These are the keys the data stack can rely on; none of
them requires re-reading a forensic file.

### Bead events ↔ attempt ledger: (`workspace_uuid`, `issue_id`, `actor`, time window)

| Ledger field | This file | Match |
|---|---|---|
| bead id | `issue_id` | exact string |
| workspace | `workspace_uuid` | exact string |
| worker identity | `actor` | exact string |
| attempt start / end | `ts_utc` | window, not equality |

- **`workspace_uuid` belongs in the key.** A bead id is unique inside one
  bead workspace, and `repo` is not that scope: `repo` is where the mirror
  lives, `workspace_uuid` is the store the event mutated. Every repo in the
  fleet currently maps to exactly one store uuid (measured 2026-09-06 across
  all 72 forensic logs the fleet publishes), but that is a property of the
  fleet today, not of the format — a workspace re-init mints a new uuid
  under the same repo name. Joining on `repo` + `issue_id` works until the
  first re-init and then silently double-matches.
- **The time window is a window, not an equality.** Match claim and close
  events whose `ts_utc` falls between the attempt's start and end (allow a
  skew on both sides; the bead mutation and the attempt's own clock are
  different processes). `ts_utc` is second-grained, so events inside one
  second have no defined order here — the forensic log's
  `origin_event_sequence` does, and it is deliberately not published; a
  consumer needing per-second ordering should join through the ledger, not
  re-read forensic files.
- **Identify an attempt's close by (workspace, issue, window), and take the
  worker from the claim — never the reverse.** Joining a close back to a
  claim on `issue_id` alone and attributing the closure to that claim's
  actor is wrong exactly when a bead was released and re-claimed by another
  worker, which is a normal redispatch and not an edge case.

### CI runs ↔ commits: `repo` + `sha`, via `commits.parquet`

`commits.parquet` is the commit-grain bridge: `(repo, sha)` is its identity
and `sha` is a full 40-character SHA.

`runs.parquet` (argo-workflows-exporter) keys runs by `uid` and **has no
commit column today**. The SHA does reach the cluster: workflows triggered
from a push carry a `commit_sha` metadata annotation written by the repo's
own Argo Events sensor, not by Argo (verified on `iad-ci`, 2026-09-06 —
present on 5 of the 60 workflows then live, i.e. only some sensors set it).
The repo is not a field anywhere on the workflow either; it is only
recoverable from the sensor or trigger naming convention. Until
argo-workflows-exporter promotes that annotation to a `commit_sha` column
and carries a repo, the run↔commit join must be made in the sink, and
**cannot be computed from `runs.parquet` alone**.

### The attribution epoch caveat

`meta.json`'s `attribution_epoch` map is the per-repo boundary for worker
scope. For each repository it is the timestamp of the first `closed` event
whose `actor` is neither empty nor `system`. The boundary is inclusive: the
close that establishes the epoch is itself a named worker row. Events before
that timestamp, and events whose actor remains `system`, are published in the
`worker = "inferential"` partition instead. The repository row remains the
complete rollup, so filtering named rows does not remove pre-epoch activity
from the existing repo/family/ecosystem views.

The epoch is observed per cycle, not a fleet-wide constant. A repo with no
qualifying close is absent from the map and contributes no named worker rows.
No claim-to-close inference is performed: a pre-epoch close is not backfilled
from an earlier claim, because a release and re-claim can change the actor.
Consumers should exclude `inferential` from worker counts unless they
explicitly want an inferred bucket.

## The two filters that are visible, not silent

**Lines of code excludes machine-generated paths.** Measured 2026-08-17 over
30 days and 105 repos, `.beads/` bookkeeping alone was 68.1% of all line
volume. `lines_*` excludes the configured patterns; `lines_*_raw` never
does; a bulk commit counts as a commit and contributes only to the raw
totals. Every column needed to recompute one from the other is present.

**Bead closures carry a migration artifact.** Every forensic log in the
fleet begins 2026-08-14 — the bead-rs migration — and three hours that day
hold 87% of all closure events. Closures in those hours carry
`is_bulk_import` on `bead_events.parquet` (density heuristic, not a
hard-coded date) and nothing is deleted: `hourly.parquet` splits the count
into `beads_closed` / `beads_closed_bulk`, so a consumer can exclude them
and still reconcile against the total.
`meta.json`'s `bead_epoch_utc` bounds how far back bead data can reach at
all: git backfills the window, beads cannot.
