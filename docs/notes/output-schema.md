# Output schema

Four objects under `DEST_S3_PREFIX` each cycle. `hourly.parquet` is what the
panel loads; `commits.parquet` and `bead_events.parquet` are the event-grain
detail behind it and the two join sources of the factory attempt ledger
(NEEDLE plan section 4.4; the sink and the joins themselves are owned by
`declarative-config`).

| Object | Grain | Purpose |
|---|---|---|
| `hourly.parquet` | `(repo, hour)` | every scope tier derives from this one table |
| `commits.parquet` | commit | drill-down detail; the commit side of the ledger's run join |
| `bead_events.parquet` | forensic event | bead lifecycle detail; the ledger's bead-side join source |
| `meta.json` | — | freshness, coverage, and the caveats a consumer must display |

Column types are the Parquet types as written. "Null when" describes the
values actually seen; nothing in this file is a `NaN` or a sentinel string.

All three Parquet files hold only the reporting window (`WINDOW_DAYS`, 90 by
default), and every timestamp is UTC with an explicit `Z` — a naive isoformat
gets read as browser-local time and shifts every chart.

## `hourly.parquet`

One row per `(repo, hour)` that saw counted activity. Ecosystem, family and
repo tiers are sums over these rows computed client-side; there are no
per-tier files, so tiers cannot drift apart.

| Column | Type | Null when | Notes |
|---|---|---|---|
| `hour_utc` | string | never | `YYYY-MM-DDTHH:00:00Z`, the hour bucket |
| `hour_epoch` | int64 | never | hours since the epoch; the numeric form of `hour_utc` |
| `repo` | string | never | Forgejo repo name |
| `family` | string | never | from `families.yaml`; `unassigned` when unmapped |
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
| `workers_active` | int64 | never | distinct `claimed` actors in the cell |

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
| `hour_utc` | string | never | joins `hourly.parquet` |
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
row is one `beads_closed`/`beads_closed_bulk` unit, so
`sum(hourly.beads_closed) + sum(hourly.beads_closed_bulk)` always equals the
count of `kind = 'closed'` rows. The total row count matches no hourly
column, because `updated` and label/dependency events are published here and
counted nowhere.

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
  "generated_at": "2026-09-06T05:00:00Z",
  "window_days": 90,
  "repos_total": 112,
  "repos_scanned": 110,
  "repos_failed": ["one-repo"],
  "repos_with_bead_data": 64,
  "bead_epoch_utc": "2026-08-14T16:42:03Z",
  "bulk_bead_cells": 12,
  "unassigned_repos": [],
  "trim_max_lines": 5000,
  "trim_max_files": 200,
  "excluded_path_patterns": ["\\.beads/", "..."]
}
```

`generated_at` is the collection heartbeat: a cycle that fails its
publish guard leaves the previous objects and their `generated_at` in place,
so a stalled exporter is visible as an aging timestamp rather than as a
quiet fleet. `repos_failed` lists the repos missing from this cycle; a cycle
over `MAX_FAILURE_RATE` is withheld entirely instead of published partial.

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

Until bead-rs BR-T12 (actor on every mutating command) and NEEDLE N-T17
(worker identity passed through) ship and are installed fleet-wide, only
`claimed` events carry a real worker. Measured 2026-08-17 and re-measured
2026-09-06 across the fleet: 100% of `claimed` events are attributable,
0% of `closed`, `released` and `reopened` — those read `system`, because the
mutation was performed by the CLI on the worker's behalf.

Consequences for a join:

- Any join condition requiring a non-`system` actor on a close returns
  nothing before the epoch. That is the data being honest, not a gap in the
  join.
- **The epoch is per repo**, and the natural definition is the earliest
  event in a repo whose `actor` is not `system`. This file does not flag it;
  Phase 4 of the plan adds a per-repo `attribution_epoch` to `meta.json`.
  Until that ships, derive it (`min(ts_utc)` over that repo's non-`system`
  rows) or treat every actor on a non-claim kind as unknown.
- Nothing can be attributed retroactively. A join that back-fills pre-epoch
  closes from the claim→close inference must label the result, and the plan
  deliberately leans against mixing an inference into the same column.

## The two filters that are visible, not silent

**Lines of code excludes machine-generated paths.** Measured 2026-08-17 over
30 days and 105 repos, `.beads/` bookkeeping alone was 68.1% of all line
volume. `lines_*` excludes the configured patterns; `lines_*_raw` never
does; a bulk commit counts as a commit and contributes only to the raw
totals. Every column needed to recompute one from the other is present.

**Bead closures carry a migration artifact.** Every forensic log in the
fleet begins 2026-08-14 — the bead-rs migration — and three hours that day
hold 87% of all closure events. Those hours are flagged `is_bulk_import`
(density heuristic, not a hard-coded date) and stay in the file, so a
consumer can exclude them and still reconcile against the total.
`meta.json`'s `bead_epoch_utc` bounds how far back bead data can reach at
all: git backfills the window, beads cannot.
