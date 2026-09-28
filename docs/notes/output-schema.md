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
3. **Recover and mirror.** Before staging a new cycle, the exporter resolves
   `current.json` and fetches every immutable object it names. If the fixed
   keys do not exactly match that complete cycle, it repairs them from the
   fetched snapshot. It then copies the new staged payloads to the fixed keys
   at the prefix root — `hourly.parquet`, `commits.parquet`,
   `bead_events.parquet`, then `meta.json` **last** — for consumers that
   predate the pointer. A process death can leave a partial mirror, but the
   next cycle repeats recovery from the pointer rather than trusting those
   keys.
4. **Commit.** One atomic PUT of `current.json`:
   `{"schema_version", "cycle_id", "generated_at", "objects": {name → key}}`,
   where keys are relative to the prefix. This PUT is the only instant at
   which the pointer-resolved dataset changes. If its response is ambiguous,
   the exporter reads the pointer: the exact pointer present in S3 wins. If
   the new pointer is absent, recovery restores the fixed keys from the old
   pointer; if the pod dies before resolution, restart performs the same
   check.
5. **Prune.** The committed cycle and the newest two others are kept
   (three cycles ≈ three poll intervals of grace for a reader that resolved
   the previous pointer); older prefixes are deleted, best-effort. "Newest"
   means the cycle ID order defined below, not S3 modification time. A
   discovery/listing failure or a cycle-prefix deletion failure emits a
   `WARNING` with the committed cycle, the affected cycle when known, and the
   exception; the old prefix is left for a later cycle. These failures never
   turn a committed publication into a failed cycle. `/health` reports
   `prune.last_outcome`, the process-lifetime `prune.failures_total`, and
   consecutive failed prune attempts in `prune.consecutive_failures`;
   operators should alert on a non-zero consecutive count and use the
   warning's cycle IDs to investigate. The immutable cycle's `meta.json` does
   not repeat this post-commit status.

### Bootstrap and invalid-pointer recovery

`current.json` is the only durable commit marker. Recovery never infers a
committed dataset from the fixed keys or from an orphaned `cycles/` prefix.
The following rules apply before each collection and publication attempt:

- **Absent on first publication:** an absent pointer is bootstrap state. The
  exporter leaves existing fixed keys and cycle prefixes untouched, stages all
  payloads under a new cycle ID, and mirrors the fixed keys only after every
  staged upload has completed. It writes `current.json` last. Because there
  was no authoritative pointer to retain or prune, retention cleanup is
  skipped for that first commit; a later cycle with a valid pointer can prune
  old prefixes normally.
- **Malformed, unsupported, or out-of-prefix:** the pointer bytes are kept for
  diagnosis, but no object key from the document is fetched. Pointer keys must
  be relative to the configured prefix and exactly equal
  `cycles/<cycle_id>/<object-name>`; absolute keys, `..` traversal, another
  prefix, and a cycle/name mismatch are unusable. No existing fixed key or
  cycle prefix is deleted during recovery, and retention cleanup is skipped.
  A later complete staged cycle may replace the unusable pointer atomically.
- **Missing or mismatched named objects:** a structurally valid pointer is
  usable only when it names the configured object set, every named immutable
  object exists, and `meta.json` repeats the pointer's `cycle_id` and
  `generated_at`. A missing object, a missing object name, or metadata from a
  different cycle makes the pointer unusable. The exporter does not use that
  snapshot to repair fixed keys or prune; it preserves the pointer and old
  cycle objects while bootstrapping a replacement.
- **Incomplete replacement:** an unusable pointer is not replaced merely
  because a new cycle started. If any staged upload fails, the old pointer
  remains byte-for-byte unchanged and the new cycle is not published. Only a
  complete staged set can be mirrored and followed by the one atomic pointer
  PUT. A handled mirror or pointer failure restores the fixed keys to their
  pre-bootstrap state when no valid pointer was available.

When a valid pointer and every object it names are present, recovery retains
the existing behavior: it fetches that immutable snapshot first, repairs the
fixed keys with `meta.json` last, and permits best-effort retention pruning
only after the replacement pointer commits.

**Consumers should read pointer-first:** GET `current.json`, then the
objects its `objects` mapping names, resolving keys against the prefix the
pointer came from. Because a cycle's staged objects are immutable and the
pointer swap is one atomic PUT, such a read always assembles exactly one
whole cycle — whichever cycle it saw — even while a publication is in
flight. Do not cache pointer-resolved URLs across polls: pruning will
eventually delete the cycle they name.

### Legacy fixed-key read consistency

The fixed keys remain for consumers that have not moved to the pointer (the
static panel reads `hourly.parquet` + `meta.json` directly), but they are not
an atomic read surface. A legacy consumer must treat a fixed-key read as a
candidate snapshot and validate it against the pointer before returning any
of its bytes. The required read attempt is:

1. GET and validate `current.json`; retain its `cycle_id`, `generated_at`, and
   `objects` mapping as the attempt's expected identity. A missing, malformed,
   unsupported, or incomplete pointer rejects the attempt; fixed keys cannot
   recover an absent authority.
2. GET all four root fixed keys without mixing values from another attempt.
   Missing keys or a read error rejects the attempt.
3. Parse root `meta.json` and require both `cycle_id` and `generated_at` to
   equal the pointer. A difference is a publication-boundary mismatch, not a
   signal to choose the older or newer fixed keys.
4. GET the four immutable objects named by the pointer and require each root
   fixed-key body to be byte-for-byte equal to its named immutable body. This
   catches the data-before-`meta.json` window where the root marker can still
   name the old cycle even though one or more data keys already contain the
   new cycle.
5. GET `current.json` again and require the validated pointer identity to be
   unchanged. If the pointer changed while the fixed keys were read, discard
   the entire candidate and start again.

On any mismatch, missing object, transient read error, or pointer change, the
consumer discards every value from that attempt and retries the complete
sequence at most three times, with a short increasing delay (100 ms, then
250 ms). It must never merge keys from attempts or fall back to an unvalidated
fixed-key snapshot. If the third attempt fails, the read is rejected with no
data returned (for example, `legacy_snapshot_inconsistent`), and the consumer
emits a diagnostic containing the prefix, attempt count, pointer cycle before
and after the read, root `meta.json` cycle, and the missing/mismatched key
names. The next poll may retry; persistent rejection is an operational
incident, not a reason to guess a cycle.

This validation makes a mixed fixed-key snapshot unservable as one cycle even
though a reader can land between the mirror PUTs. The publisher, not the
consumer, repairs the root keys: on the next publication or restart it
reconciles every fixed key from the immutable cycle named by `current.json`,
writing `meta.json` last. The pointer-resolved path remains the migration
target and the simpler read path.

## Output schema-version compatibility

`current.json` is the versioned output envelope. Its `schema_version` is an
integer describing the pointer and the four objects it names; it is not the
exporter's release `version` in `meta.json`. The supported values are:

| `schema_version` | Status | Compatibility contract |
|---|---|---|
| `1` | supported | The v1 pointer fields and the v1 `meta.json`/Parquet payload schemas documented below. |

There are no other supported values today. A breaking change gets a new
integer and a new versioned consumer fixture (for example, `v2`); v1 readers
must not guess how to read it. `meta.json` and the Parquet files do not carry
an independent `schema_version`: they inherit the version of the pointer
that names them.

### Required and optional fields

The v1 `current.json` envelope requires all four fields below. Their types and
relationships are part of the compatibility contract; a field set to `null`
is not a valid substitute for a missing or correctly typed value.

| Field | Required in v1 | Compatibility rule |
|---|---|---|
| `schema_version` | yes | Integer `1`; JSON booleans are not integers for this purpose. |
| `cycle_id` | yes | A valid cycle ID whose timestamp is exactly `generated_at`. |
| `generated_at` | yes | A valid UTC timestamp with an explicit `Z`. |
| `objects` | yes | A non-empty object of simple names to exact relative `cycles/<cycle_id>/<name>` keys. |

The v1 envelope defines no named optional fields. Unknown/additive envelope
fields are treated as optional extensions and ignored by a v1 reader after the
required fields validate. Producers must keep the required fields stable;
consumers must not invent defaults for them. A future optional payload field
must be read only when present, with its documented default, and must not
silently replace a required field.

All fields in the v1 `meta.json` table and all columns in the v1 Parquet
tables are required for that payload schema. `bead_epoch_utc` is nullable, but
it is not optional. The `version` field in `meta.json` identifies the writer
release, not the output format. A missing required payload field, missing
pointer-named object, or pointer object-name mismatch makes the cycle
incomplete rather than a partial dataset to be guessed around.

### Reader behavior

A consumer must validate `current.json` before fetching any named object:

- An unknown, missing, non-integer, or otherwise invalid `schema_version` is
  unsupported. The consumer retains the pointer bytes for diagnosis, reads no
  object named by it, does not repair fixed keys from it, and does not prune
  cycle prefixes. It waits for or falls back to the last independently valid
  cycle according to its application policy.
- A missing or invalid required field has the same fail-closed behavior. The
  consumer must not fall back to the fixed-key mirror as though it were an
  atomic replacement, because those keys can be mixed during publication.
- An unknown/additive field is ignored. A missing optional field is handled by
  its documented default; v1 has no named optional fields, so no default is
  implied for any of its four envelope fields.
- A pointer that passes envelope validation but omits one of the configured
  four object names, names an object that is absent, or contains `meta.json`
  from another cycle is unusable. The complete previous cycle remains
  authoritative until a complete pointer is available.

The exporter implements this policy in `publish.validate_pointer_document`
and `publish._read_pointer_state`. The reader tests cover supported v1,
unknown versions, missing required fields, additive fields, and the guarantee
that an unusable pointer is not dereferenced or used for destructive recovery.

### Crash recovery

Pod termination is treated as a loss of the client response, not as evidence
that the S3 operation did not happen. S3 is queried after restart, and the
durable pointer—not the fixed-key mirror and not pod memory—decides what is
authoritative.

| Interruption or ambiguous PUT | State left in S3 | Recovery rule |
|---|---|---|
| Staging `cycles/<cycle_id>/...` | The previous pointer remains authoritative; the new prefix may be partial or complete | Staged objects are write-once. Never retry by overwriting an existing object or reuse that cycle ID. The orphan prefix is inert and later pruning can delete it. |
| Fixed-key mirror | The previous pointer remains authoritative; root fixed keys may be mixed | Before the next cycle, fetch every object named by the pointer and repair all fixed keys from that immutable set, with `meta.json` last. A restart never uses the mixed fixed keys as its source. |
| Pointer commit | Either the old pointer or the new pointer may be present | If the PUT response is uncertain, read `current.json`. The exact pointer found there wins. On restart, fixed keys are reconciled from that pointer before another cycle is staged. |
| Absent or unusable `current.json` | No trusted pointer exists; fixed keys and cycle prefixes may contain legacy, partial, or diagnostic objects | Do not dereference or prune them. Bootstrap only after a complete new staged cycle exists; retain the old pointer bytes if present and replace them only at the final atomic PUT. A handled failed bootstrap restores the fixed keys to their pre-attempt bytes. |

The recovery read happens before the next expensive repository scan and is
idempotent. A failure while repairing fixed keys leaves the pointer-first read
path safe; the next poll retries the same reconciliation. Thus a killed pod
cannot make a pointer-resolved reader assemble objects from two cycles, and a
complete orphaned stage cannot be mistaken for a commit merely because its
PUTs reached S3.

## Single-writer deployment rule

Publication is intentionally single-writer by deployment: exactly one
exporter replica owns `DEST_S3_PREFIX`. Within that exporter process,
publication entry points also take a process-local reentrant lock covering
recovery, staging, fixed-key mirroring, the `current.json` commit, and prune.
That lock prevents concurrent threads from interleaving the protocol, but it
is not an S3 lease and cannot serialize separate processes or pods. The
deployment shape therefore remains part of the correctness boundary, not
merely a capacity choice.

The protocol has no cross-process S3 lock, conditional PUT, or runtime
replica assertion. The selected cross-process guard is therefore deployment
shape: keep exactly one exporter replica for a destination prefix.

With two separate processes or pods, the failure is not just a lost update.
None of the protocol's operations serializes with another publisher:

- The prefix existence check and staged PUTs are separate operations. If two
  publishers mint the same `cycle_id`, both can observe an empty prefix and
  overwrite the same objects, defeating staged immutability.
- Fixed-key mirror PUTs and best-effort rollback can interleave. The fixed keys
  can end with payloads from one cycle and metadata from another, or one
  publisher's rollback can overwrite another publisher's successful commit.
- A `current.json` PUT is atomic for that object but is not conditional. The
  last writer wins, so a slower publisher with an older `generated_at` can
  move the pointer backward.
- Prune protects only its publisher's `keep` cycle and the grace candidates in
  its list view. It can delete another publisher's in-flight staged prefix or a
  committed cycle that a reader just resolved. `current.json` can then name
  missing or mixed objects, so the pointer-first whole-cycle guarantee no
  longer holds.

Nothing in the current protocol identifies the owning publisher or detects
this interleaving, and there is no automatic reconciliation. Treat every
multi-writer deployment, including two replicas of this same image, as
unsupported.

**Decision: rely on deployment shape for cross-process exclusion.** The
process-local lock is defense in depth, not a replacement for this rule. The
reference Deployment in the
`declarative-config` repository
(`k8s/ardenone-cluster/git-activity-exporter/deployment.yml`) is pinned to
`replicas: 1` and carries a manifest comment warning that two pods would fight
over the RWO PVC and double-write the same objects. That warning is an
operational prohibition: do not manually scale the Deployment, attach an HPA,
or run another workload with the same destination bucket and prefix. Its
`Recreate` strategy prevents overlap during ordinary rollouts but does not
prevent scale-out; the RWO PVC and the process-local poll loop likewise are not
S3 writer exclusion. Reusers must preserve the same deployment boundary.

Supporting more than one publisher requires a real ownership protocol, not a
preflight existence check: unique ownership, conditional acquisition and safe
takeover, renewal and release, and fencing of publication mutations are needed
to stop a paused former owner from resuming after another takes over. Until
that protocol exists, increasing the replica count is forbidden rather than an
unsupported tuning option.

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
partial bucket. When a shallow mirror cannot be deepened far enough,
`repos_partial_history` identifies the repositories whose lower edge is
incomplete. The full boundary, timezone, and DST contract is in
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
| `family` | string | never | from `families.yaml`; `unassigned` when unmapped — [attribution over time](#family-attribution-over-time) |
| `worker` | string | repository rows | null for the repo aggregate, the attributed actor for post-epoch rows, or `inferential` before the repo epoch |
| `commits` | int64 | never, may be 0 | |
| `bulk_commits` | int64 | never, may be 0 | commits flagged bulk; there is no separate bulk hourly row or split |
| `lines_added` / `lines_deleted` | int64 | never | excluded-path-filtered totals from non-bulk commits; bulk commits contribute 0 |
| `lines_added_raw` / `lines_deleted_raw` | int64 | never | unfiltered totals; bulk commits contribute in full |
| `files_changed` | int64 | never | excluded-path-filtered total from non-bulk commits; bulk commits contribute 0 |
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
| `files_changed` | int64 | never | excluded-path-filtered |
| `lines_added_raw` / `lines_deleted_raw` | int64 | never | unfiltered |
| `is_bulk` | bool | never | true when the filtered combined line total is strictly above `TRIM_MAX_LINES` **or** the filtered file total is strictly above `TRIM_MAX_FILES`; equality is not bulk |

## Commit bulk and filtered LOC contract

`is_bulk` is a per-commit annotation. The exporter never drops a flagged
commit: `commits.parquet` retains its row, and hourly `commits` includes it.
`hourly.parquet` exposes the count in `bulk_commits`; it does not create a
second bulk row or a bulk/non-bulk split.

The flag is computed after `EXCLUDED_PATH_PATTERNS` filtering, using the
per-commit `lines_added`, `lines_deleted`, and `files_changed` values:

```text
is_bulk = (lines_added + lines_deleted) > trim_max_lines
          OR files_changed > trim_max_files
```

Both tests are independent, and both are strict. With the default thresholds,
5,000 filtered changed lines and 200 filtered files are still ordinary; 5,001
filtered changed lines or 201 filtered files is bulk. The line threshold is on
the combined additions-plus-deletions total, not on either side separately.

Bulk status affects only the hourly rollup's filtered measures: a flagged
commit contributes zero to hourly `lines_added`, `lines_deleted`, and
`files_changed`. Its full `lines_added_raw` and `lines_deleted_raw` values
still contribute to the hourly raw audit totals. The raw pair is audit data,
not another bulk trigger, and excluded-path-only changes therefore cannot make
a commit bulk merely because their unfiltered counts are large. The commit's
own filtered values remain in `commits.parquet` so the annotation and the
filtering decision are inspectable together.

## `bead_events.parquet`

One row per forensic event in the window — **every** kind the forensic log
records, not only the four the hourly rollup counts. This file is a join
source, not a chart source: `created`, `updated`, label and dependency edits
are noise for a chart and exactly what a per-bead timeline needs. A missing
forensic path contributes no rows. A present but malformed, unreadable, or
detectably truncated file, or one with duplicate event identities, contributes
no rows from that repo: the repo fails the cycle's coverage check instead of
publishing a valid prefix. When `current.json` is present, its record count
and active-root checksum must also match the complete forensic file, which
makes truncation on a record boundary detectable
([data-sources.md](data-sources.md#failure-semantics)).

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

## Family attribution over time

Every `family` value in all three Parquet files comes from the
[`families.yaml` mapping](configuration.md#the-families-file) **as it stood
at process startup**. The file is read once, before the first cycle, and
every cycle that process publishes resolves `family` from that one mapping
at row-build time; nothing re-reads the file mid-process.

Published cycles are immutable — [staged objects are written once and never
rewritten](#publication-protocol) — so the temporal decision is pinned as
follows: **attribution is per-publication, never retroactive.** Editing
`families.yaml` and restarting changes only the cycles published after the
restart. Concretely:

- **A remap is a step change at a cycle boundary, not a rewrite.** The first
  cycle a restarted process publishes carries the new mapping everywhere —
  `hourly.parquet`, `commits.parquet`, `bead_events.parquet`, and
  `unassigned_repos` alike. No older cycle is republished, re-derived, or
  corrected.
- **The family tier can straddle a remap for up to two cycles.** Retention
  keeps the committed cycle plus the newest two others, and the grace
  cycles keep whatever mapping they were published with. Summing a family
  across cycles — or comparing family totals between the pointer's cycle
  and its grace set — can therefore see both sides of a remap until pruning
  ages the older cycles out. Cross-cycle family comparisons must be
  cycle-scoped. This inconsistency is accepted by design rather than
  papered over, because rewriting already-published objects is exactly what
  the publication protocol forbids; consumers wanting a remapped history
  re-aggregate client-side from per-cycle snapshots.
- **The fixed legacy keys always carry the newest mapping.** They are
  overwritten each cycle, so a fixed-key reader scraping over time sees the
  same step change at the same boundary — detectable the usual way, by
  comparing `meta.json`'s `cycle_id` with `current.json`'s.
- **`unassigned` is a fallback bucket whose membership is time-dependent by
  design, not a stable family.** Adding a repo to the map removes it from
  `unassigned` and from `unassigned_repos` only in cycles published after
  the restart; the older retained cycles keep `family = "unassigned"` and
  keep listing the repo. Removing a repo from the map — or renaming it on
  the forge, or changing its case — puts it in `unassigned` from the next
  published cycle on. A consumer reconstructing "family X over time" must
  union per-cycle snapshots; nothing in the protocol backfills the new
  mapping into older ones.

The exporter does not echo the mapping into `meta.json` and does not version
it: the mapping a cycle actually used is recoverable from that cycle's own
rows together with its `unassigned_repos`, and a changed mapping is visible
by comparing published cycles. Adding mapping metadata to `meta.json` would
be a schema change, not a clarification, and is deliberately out of this
contract.

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
  "repos_partial_history": ["one-repo"],
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
| `version` | string | never | The exporter's own version (`VERSION_FILE`), `"unknown"` if missing, unreadable, empty, or not UTF-8. Identifies the writer, not the document format. |
| `cycle_id` | string | never | Exact `generated_at` compacted (`2026-09-06T05:00:00Z` → `20260906T050000Z`) plus eight lowercase hexadecimal characters from UUID4. It is identical to `current.json`'s `cycle_id`; the timestamp portion orders retention, with the suffix only breaking same-second ties. |
| `generated_at` | string | never | RFC 3339 UTC with an explicit `Z`. When collection *began* — it can trail the pointer's landing by up to `cycle_seconds`. |
| `window_days` | int | never | The window every Parquet file of the same cycle was cut to. |
| `repos_total` | int | never | Repos enumerated this cycle: denylist applied, forge-empty repos already dropped. |
| `repos_scanned` | int | never | Repos that contributed data. `repos_scanned + len(repos_failed) == repos_total` holds in every cycle. |
| `repos_failed` | list[string] | never (may be empty) | Repos absent from this cycle — clone, fetch, `log`, forensic/manifest lookup, forensic/manifest `show`, or forensic validation failed — in enumeration order. Why each one failed is in `repo_errors`. |
| `repo_errors` | map string→string | never (may be empty) | Keys are exactly `repos_failed`; values are human-readable reasons, credential-scrubbed at the source and truncated to 200 chars. |
| `repos_stale` | list[string] | never (may be empty) | Scanned this cycle from a mirror whose fetch timed out — present, merely not newest. Disjoint from `repos_failed`, a subset of the scanned set, and deliberately not counted as failure. The first timeout costs at most one poll interval of freshness; repeated timeouts can cost more. |
| `repos_partial_history` | list[string] | never (may be empty) | Scanned repos whose shallow boundary remains newer than the computed UTC-date `generated_at - WINDOW_DAYS - SHALLOW_SINCE_DAYS` cutoff. A repo may also be in `repos_stale`; the field is disjoint from `repos_failed`, is a subset of `repos_scanned`, and is not counted as failure. Consumers must treat the lower edge as incomplete. |
| `mirrors_pruned` | list[string] | never (may be empty) | Mirrors deleted this cycle because their repo was deleted, renamed, denylisted or emptied on the forge; recorded so a deletion is auditable rather than silent. |
| `repos_with_bead_data` | int | never | Scanned repos whose present forensic log passed whole-file validation and produced at least one event in the window; never above `repos_scanned`. A missing, empty, or out-of-window-only valid log does not count. Absence is the normal case for roughly a third of the fleet. |
| `git_timeout_seconds` | int | never | The per-invocation bound the failure semantics are defined against, echoed so a consumer diagnosing timeouts sees what the exporter was actually given. |
| `cycle_seconds` | number | never | Wall-clock cost of the cycle at 0.1 s resolution. A value approaching `POLL_INTERVAL_SECONDS` is degradation even when every repo succeeded. |
| `bead_epoch_utc` | string | no scanned repo produced a bead event in the window | Earliest bead event of any kind in this cycle's window; see below. |
| `attribution_epoch` | map string→string | never (may be empty) | Per-repo UTC timestamp of the first `closed` event with a non-`system` actor in this cycle's window. A missing repo has no observed attribution epoch. |
| `bulk_bead_cells` | int | never | `(repo, hour)` cells flagged as bulk imports; their closures carry `is_bulk_import` on `bead_events.parquet` and are counted into `beads_closed_bulk`, so excluding them stays reconcilable. |
| `unassigned_repos` | list[string] | never (may be empty) | Sorted and unique. Repos with activity in the window whose `families.yaml` mapping is missing; a scanned repo with no window activity cannot appear. |
| `trim_max_lines` | int | never | The bulk-commit LOC bound behind `is_bulk`. |
| `trim_max_files` | int | never | The bulk-commit file-count bound behind `is_bulk`. |
| `excluded_path_patterns` | list[string] | never | The `re.search` patterns separating `lines_*` from `lines_*_raw`, as configured (see the [excluded-path matching contract](configuration.md#excluded-path-matching-contract); defaults in [configuration.md](configuration.md#default-excluded-path-patterns)). |

### Freshness, coverage, and withheld cycles

`generated_at` is how a consumer tells a stalled exporter from a quiet
fleet. It is stamped when the cycle begins, and a cycle that fails — a
generation fault, a staging upload, the publish guard — publishes nothing,
so the previous cycle's objects and their `generated_at` stay in place and
the timestamp ages while the data does not. A `generated_at` older than a
couple of poll intervals is an alarm, not a lull.

Coverage reads straight off the fields: `repos_total` splits into
`repos_scanned` plus `repos_failed`, and `repos_scanned` further splits into
fresh and `repos_stale`. A malformed, detectably truncated, duplicated, or
unreadable present forensic log, or an invalid present checkpoint manifest, is
a repository failure, so that repo is named in `repos_failed`, has a reason in
`repo_errors`, and contributes neither Git nor bead rows.
`repos_partial_history` is a separate coverage warning within
the scanned set: it names repos whose shallow boundary is still newer than the
configured date bound, and can overlap `repos_stale`. A healthy cycle
has `repos_failed`, `repos_stale`, `repos_partial_history`, and
`mirrors_pruned` all empty; anything else is stated here rather than inferred
from missing rows.

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

- **Read pointer-first.** A fixed-key reader must run the complete
  [legacy fixed-key read consistency](#legacy-fixed-key-read-consistency)
  sequence: compare both metadata identity fields, compare every fixed body
  with the pointer-named immutable object, and re-read the pointer. Any
  mismatch is discarded and retried; exhausted retries reject the snapshot
  rather than returning a mix of cycles.
- **`meta.json` is the fixed keys' completion marker.** The mirror writes
  it last (publish.py `_meta_last`), so a fixed-key reader that sees a new
  `generated_at` knows the other three fixed keys were already replaced.
  This marker comparison is necessary but not sufficient: the immutable-body
  comparison also catches a new data key paired with the old root marker.
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

### Versioned consumer fixture

[`tests/fixtures/output-contract/v1/`](../../tests/fixtures/output-contract/v1/)
is a checked-in, pointer-resolved publication for downstream readers. It
contains `current.json`, its complete `cycles/<cycle_id>/` object set, the
legacy fixed-key mirror, all three Parquet files with non-empty rows, a
contract manifest, and ledger-join examples. The fixture version is independent
of the exporter release version: a breaking output change gets a new `vN`
directory, while v1 remains readable by older consumers.

The dashboard-site consumer should resolve `current.json` first and read only
the four objects it names. The declarative-config factory-ledger consumer
should use the same cycle-scoped objects and replay `ledger/joins.json` to
prove the `workspace_uuid` + `issue_id` + claim actor/time-window join and the
`repo` + full-SHA commit bridge. The fixture includes positive,
zero-cardinality negative, and missing-source cases so a consumer cannot turn
a wrong key or an absent object into verified evidence.
`tests/test_output_contract_fixture.py` checks the producer-side bytes, but
does not substitute for those consumers' native reader tests.

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
does; a bulk commit counts as a commit and contributes only to the raw hourly
totals. The complete bulk and threshold contract is above.

**Bead closures carry a migration artifact.** Every forensic log in the
fleet begins 2026-08-14 — the bead-rs migration — and three hours that day
hold 87% of all closure events. Closures in those hours carry
`is_bulk_import` on `bead_events.parquet` (density heuristic, not a
hard-coded date) and nothing is deleted: `hourly.parquet` splits the count
into `beads_closed` / `beads_closed_bulk`, so a consumer can exclude them
and still reconcile against the total.
`meta.json`'s `bead_epoch_utc` bounds how far back bead data can reach at
all: git backfills the window, beads cannot.
