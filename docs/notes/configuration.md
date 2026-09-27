# Configuration

All configuration is environment variables. Only `FORGE_TOKEN` and the four
destination values `DEST_S3_ENDPOINT`, `DEST_S3_BUCKET`,
`DEST_S3_ACCESS_KEY_ID` and `DEST_S3_SECRET_ACCESS_KEY` are required;
everything else has a default.

| Variable | Default | Notes |
|---|---|---|
| `FORGE_BASE_URL` | `https://git.ardenone.com` | Forgejo instance |
| `FORGE_OWNER` | `jedarden` | whose repos to enumerate |
| `FORGE_TOKEN` | *(required)* | read scope is sufficient |
| `REPO_DENYLIST` | *(empty)* | comma-separated repo names to skip |
| `CLONE_ROOT` | `/data/mirrors` | must be a persistent volume; mirrors orphaned by a deleted/renamed/denylisted/empty repo are pruned from it each cycle |
| `WINDOW_DAYS` | `90` | positive reporting-window length in elapsed 24-hour UTC days; [boundary contract](data-sources.md#reporting-window-boundary-contract) |
| `SHALLOW_SINCE_DAYS` | `WINDOW_DAYS + 10` | date-based history bound; existing mirrors deepen on fetch when a wider window is requested — [shallow-mirror behavior](data-sources.md#git--bounded-window-explicit-coverage) |
| `TRIM_MAX_LINES` | `5000` | strict upper bound on a commit's filtered `lines_added + lines_deleted`; above it the commit is flagged bulk — [commit bulk contract](output-schema.md#commit-bulk-and-filtered-loc-contract) |
| `TRIM_MAX_FILES` | `200` | strict upper bound on a commit's filtered `files_changed`; above it the commit is flagged bulk — [commit bulk contract](output-schema.md#commit-bulk-and-filtered-loc-contract) |
| `EXCLUDED_PATH_PATTERNS` | [the four defaults](#default-excluded-path-patterns) | regexes, comma-separated; replaces the default list wholesale |
| `BEAD_BULK_CLOSE_THRESHOLD` | `150` | closures per `(repo, hour)` above which the cell is flagged |
| `BEAD_BULK_HOUR_SHARE` | `0.5` | share of an hour's fleet-wide closures already flagged before the whole hour is treated as bulk |
| `MAX_FAILURE_RATE` | `0.2` | fraction of repos that may fail before the cycle is withheld instead of published |
| `FAMILIES_FILE` | `families.yaml` | repo → family map; a relative path resolves against the process working directory — [The families file](#the-families-file) |
| `VERSION_FILE` | `VERSION` | stamped into `meta.json` |
| `POLL_INTERVAL_SECONDS` | `3600` | sleep from one cycle attempt's end to the next cycle's start — [Poll-cycle lifecycle](#poll-cycle-lifecycle) |
| `GIT_TIMEOUT_SECONDS` | `600` | per Git attempt — clone, fetch, `log`, `ls-tree`, `show`; remote clone/fetch attempts use the [bounded retry policy](data-sources.md#transient-failure-retries) |
| `HTTP_TIMEOUT_SECONDS` | `30` | per Forgejo API attempt (repo enumeration only); 5xx/transport failures use the [bounded retry policy](data-sources.md#transient-failure-retries) |
| `HEALTH_PORT` | `8080` | port for the [health endpoints](#health-endpoints) |
| `LOG_LEVEL` | `INFO` | |
| `DEST_S3_ENDPOINT` | *(required)* | S3-compatible endpoint URL |
| `DEST_S3_BUCKET` | *(required)* | destination bucket |
| `DEST_S3_ACCESS_KEY_ID` | *(required)* | destination access key |
| `DEST_S3_SECRET_ACCESS_KEY` | *(required)* | destination secret key |
| `DEST_S3_REGION` | `us-east-1` | botocore region; most S3-compatible stores ignore it |
| `DEST_S3_ADDRESSING_STYLE` | `virtual` | `path` wherever the store has no per-bucket virtual-host DNS — see [Destination credentials](#destination-credentials-dest_s3_) |
| `DEST_S3_PREFIX` | `git-activity/data` | key prefix under the bucket; trailing slash stripped |

## Commit bulk flag and rollup contract

`TRIM_MAX_LINES` and `TRIM_MAX_FILES` are independent, strict upper bounds
evaluated per commit after `EXCLUDED_PATH_PATTERNS` has been applied. A commit
is flagged when either condition is true:

```text
is_bulk = (lines_added + lines_deleted) > TRIM_MAX_LINES
          OR files_changed > TRIM_MAX_FILES
```

Equality is not bulk: the default boundaries are 5,000 filtered changed lines
and 200 filtered files, so the first triggering values are 5,001 and 201.
The line test uses the combined additions-plus-deletions total; additions and
deletions do not trigger independently. The file test can flag a commit whose
filtered line total is small, and vice versa.

The flag is an annotation, not a deletion. The commit remains one row in
`commits.parquet` and still counts in hourly `commits`; hourly
`bulk_commits` records how many such rows the cell contains. Bulk commits are
excluded from hourly `lines_added`, `lines_deleted`, and `files_changed`, but
their complete `lines_added_raw` / `lines_deleted_raw` values remain in the
hourly audit totals. Those raw line fields never trigger `is_bulk`; neither do
excluded-path-only changes, once the filtered values have been removed.

The per-commit filtered values remain visible in `commits.parquet` alongside
`is_bulk`, so a consumer can inspect the row that was excluded from the
hourly filtered rollup. There is no separate bulk hourly row or split table;
use `bulk_commits` together with `commits` when excluding bulk commit counts.

## Health endpoints

The health server binds `0.0.0.0:HEALTH_PORT` after configuration, the family
map, and the S3 client have loaded, and before the first collection cycle. A
probe made before that point receives a connection failure rather than an HTTP
response. Once the server is bound, these are the complete contracts:

| Request | Status | Payload | Meaning |
|---|---:|---|---|
| `GET /health` | `200` | JSON health snapshot | The process is alive. This does not depend on collection or publication success. |
| `GET /metrics` | `200` | Prometheus text exposition | Numeric freshness, cycle-outcome, prune, and publication-failure signals for the monitoring stack. |
| `GET /ready`, before the first successful cycle | `503` | empty, zero-byte body | The process has not yet completed a publication cycle in this process lifetime. |
| `GET /ready`, after the first successful cycle | `200` | empty, zero-byte body | A cycle has completed in this process lifetime. |
| `GET` any other path | `404` | empty, zero-byte body | Not an exporter endpoint. |

`/health` returns `Content-Type: application/json` with exactly these fields:

```json
{"last_successful_cycle_at":"2026-09-27T12:00:00Z","last_cycle_outcome":"published","prune":{"last_outcome":"succeeded","failures_total":0,"consecutive_failures":0,"last_failure_cycle_id":null}}
```

Before the first cycle attempt, both values are `null`. `last_successful_cycle_at`
is the `generated_at` timestamp from the newest cycle whose complete publication
committed. It remains unchanged when a later cycle is `withheld` or `failed`.
`last_cycle_outcome` is the most recent attempt: `published`, `withheld` (the
`MAX_FAILURE_RATE` guard rejected it), or `failed` (another cycle exception).
The `prune` object is process-local because pruning runs after the pointer
commit and therefore cannot be added to the immutable cycle's `meta.json`.
`last_outcome` is `null` before the first committed cycle and then is either
`succeeded` or `failed`; a failed outcome means cycle discovery or at least one
cycle-prefix deletion failed, not that publication failed. `failures_total`
counts failed prune attempts since process start, while
`consecutive_failures` resets to zero after a fully successful prune. The
`last_failure_cycle_id` remains the most recent committed cycle whose cleanup
failed, including after cleanup recovers. A non-zero consecutive count is an
operator alert signal; the application log contains the affected prefix and
exception for each failed attempt. A prune failure leaves `/health` at `200`,
keeps `last_cycle_outcome` at `published`, and does not clear `/ready`.
The `/ready` response remains empty and has no `Content-Type` header. These are
exact GET paths; a trailing slash or query string is a different path and
therefore returns `404`.

### Staleness alert

Poll `/health` at least once per `POLL_INTERVAL_SECONDS`. Alert when
`last_successful_cycle_at` is null beyond a startup grace of two poll intervals
or when its age exceeds `2 * POLL_INTERVAL_SECONDS`. The two-interval allowance
covers one missed cycle; a cycle that routinely takes as long as the interval
is already degraded and should page sooner according to the operator's normal
cycle-duration budget. The reference Prometheus rule in
[`examples/self-hosting/monitoring.yaml`](../../examples/self-hosting/monitoring.yaml)
uses a 10-minute evaluation hold after the threshold is crossed. Use
`last_cycle_outcome` to distinguish a `withheld` publication from an
unexpected `failed` cycle when diagnosing the alert.

The `/metrics` endpoint makes the remaining signals alertable without parsing
JSON or log text:

- `git_activity_exporter_cycle_attempts_total{outcome="withheld"}` alerts after
  two withheld cycles in a rolling two-hour window. This is the default
  one-hour-poll deployment threshold; adjust the rule window if the poll
  interval changes materially.
- `git_activity_exporter_prune_consecutive_failures >= 1` alerts after 15
  minutes. This detects an S3 cleanup permission or availability problem even
  while new cycles continue publishing.
- `git_activity_exporter_publication_failures_consecutive >= 2` alerts after
  10 minutes. A successful publication resets the streak; the total counter is
  retained for the process lifetime.

The application logs remain the diagnosis surface. A withheld cycle logs
`cycle withheld`, a failed publication logs `publication failed`, and failed
cleanup logs include the committed and affected cycle IDs. These messages are
safe to ship to the cluster log collector; they do not contain credentials.

### Readiness transitions

Readiness is a process-lifetime latch, not a report on the newest cycle:

1. **Startup:** after the health server binds and before any successful cycle,
   `/health` is `200` and `/ready` is `503`. This deliberately allows a cold
   fleet-wide clone to finish without making liveness fail.
2. **Failed cycle before first success:** enumeration failures, payload
   generation failures, publication failures, and other exceptions leave
   `/ready` at `503`. The next poll retries; `/health` remains `200`.
3. **Withheld publication before first success:** a cycle rejected by the
   `MAX_FAILURE_RATE` guard raises before publication and also leaves
   `/ready` at `503`. A cycle at or below the threshold is a successful
   publication even when it contains the permitted partial coverage.
4. **First successful cycle:** only a cycle that returns without an exception
   flips `/ready` from `503` to `200`.
5. **Later failure or withheld publication:** after readiness has been
   achieved, a later failed or withheld cycle does not clear it. `/ready`
   stays `200` and `/health` stays `200`; the previous complete publication
   remains live. Use `/health`'s `last_successful_cycle_at` and
   `last_cycle_outcome` for probe-level freshness and outcome; use `meta.json`'s
   `generated_at`, `repos_failed`, `repos_stale`, and `repos_partial_history`
   for publication coverage details.
6. **Recovery:** a successful cycle after pre-readiness failures transitions
   `/ready` from `503` to `200`. Recovery after readiness has already been
   achieved has no observable endpoint transition; it remains `200`.
7. **Restart:** every process starts unready again, even if S3 already contains
   a valid publication. Readiness is not restored from remote state; this
   process must complete another cycle.

The distinction is intentional: `/ready` prevents a cold start from receiving
traffic before its first local publication, while `/health` prevents a long or
repeatedly failing collection from causing a restart loop. A sticky readiness
latch does not claim that later cycles are fresh; consumers diagnose that from
the published metadata.

## Poll-cycle lifecycle

The exporter is one sequential loop: run a complete collection-and-publication
cycle, sleep `POLL_INTERVAL_SECONDS`, repeat. Every timing property below
follows from that shape; each is pinned behaviorally against the real loop in
`tests/test_poll_lifecycle.py`.

**The first poll starts immediately.** Startup loads configuration, the family
map, and the S3 client, binds the [health server](#health-endpoints), and then
begins the first cycle attempt with no initial delay. That attempt first runs
the S3 destination permission preflight: collection does not start until the
configured prefix can be listed, a temporary probe can be written, its
metadata and body can be read, and it can be deleted. The first interval
sleep happens only after that cycle attempt has finished. A fresh deployment
therefore lands its first publication one cycle-duration after start, not
`POLL_INTERVAL_SECONDS` plus one cycle-duration, and `/ready` stays `503` until
that publication commits.

**`POLL_INTERVAL_SECONDS` is a sleep, not a schedule.** It is measured from
the end of one cycle attempt — success, failure, or withheld publication — to
the start of the next. It is never measured cycle-start to cycle-start and
never against wall-clock boundaries, so the effective period is always
`cycle_seconds + POLL_INTERVAL_SECONDS`. That is why `meta.json` records
`cycle_seconds` at all: a cycle approaching the interval in duration is
degrading even when every repo in it succeeded, because the fleet's actual
refresh rate has fallen to roughly half what the interval alone suggests.
Three consequences are deliberate:

- **Slow cycles delay later cycles, and nothing catches up.** Time spent
  collecting is not debited against the following interval, and an overrun is
  never compensated by a shortened one. Cadence drifts forward monotonically;
  a run of overruns cannot bunch into back-to-back cycles.
- **A failed cycle waits the full interval before retrying.** Individual
  transient remote operations may already have used their bounded retries, but
  there is no cycle-level backoff or immediate whole-cycle retry. As in
  [failure semantics](data-sources.md#failure-semantics), the next poll is the
  retry after an operation is exhausted.
- **The sleep is interruptible.** It is an event wait, not a busy `time.sleep`,
  so a shutdown signal arriving mid-sleep exits immediately rather than after
  the remaining interval.

**Cycles never overlap.** The loop is single-threaded and sequential: the next
cycle cannot begin until the previous attempt has returned — published,
withheld, or failed — and the full interval has elapsed. Overlap prevention is
structural, not enforced with locks or a scheduler: a cycle that overruns its
interval is merely late, never concurrent with its successor.

**Shutdown does not interrupt a cycle.** `SIGTERM` and `SIGINT` set the stop
event and nothing else. A cycle in progress runs to completion — it publishes
fully or fails per the [publication protocol](output-schema.md#publication-protocol)
— and the process then exits without waiting out the interval and without
starting another cycle. There is deliberately no mid-cycle abort: a graceful
shutdown can never publish a partial cycle, and the hard-death case (SIGKILL,
OOM, node loss) is covered by the protocol instead — the pointer keeps naming
the previous complete cycle, and any staging it orphaned is inert until a
later cycle's retention prune sweeps it.

**A restart re-polls immediately and starts from scratch.** No schedule state
persists across processes. The new process begins its first cycle right away,
starts unready even when S3 already holds a valid publication, and owes its
`/ready` transition to its own first success. Missed intervals are not
replayed: a restart costs the outage duration plus one cycle of freshness, and
because mirrors persist on `CLONE_ROOT`, that first post-restart cycle is a
fetch pass over warm mirrors, not a fleet-wide cold clone.

## Why `SHALLOW_SINCE_DAYS` must cover `WINDOW_DAYS`

`SHALLOW_SINCE_DAYS` is a date/history duration applied before the reporting
window's start, not a commit count. It defaults to ten days beyond the window
so a normal small bump still has margin. `config.load()` rejects a bound
shorter than the reporting window because a clone shallower than the window
truncates the oldest hours of every chart with no error.

When a mirror already exists, widening `WINDOW_DAYS` recomputes its cutoff on
the next fetch. Git deepens the mirror in place with the date bound; the
collector records any repository whose shallow boundary still truncates the
requested bound in `meta.json`'s `repos_partial_history`. See
[data-sources.md](data-sources.md#failure-semantics) for the exact coverage and
stale-mirror semantics.

## Chosen thresholds are measurements, not guesses

`TRIM_MAX_LINES` / `TRIM_MAX_FILES`: the largest genuine commits in a 30-day
sample sat near a 104-line median, while bulk artifact commits reached
9,072,022 lines and 25,639 files. Anything in between is comfortably
separated.

`BEAD_BULK_CLOSE_THRESHOLD`: the bead-rs migration produced `(repo, hour)`
cells up to 1,966 closures. The busiest genuine hour any repo has recorded is
69. 150 sits in the gap with room on both sides.

## Default excluded-path patterns

When `EXCLUDED_PATH_PATTERNS` is unset, these four regexes apply. Each is a
`re.search` against the repo-relative path, so a pattern fires at any depth;
a setting replaces all four, it does not append.

```
(^|/)\.beads/
(^|/)(vendor|node_modules|third_party|\.venv)/
(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum|uv\.lock|composer\.lock)$
\.(min\.js|min\.css|map)$
```

Line by line: bead checkpoint bookkeeping; vendored dependency trees;
lockfiles (machine-written, never hand-edited); minified bundles and source
maps. These are the 68.1% `.beads/` plus 5.2% vendored volume from the
2026-08-17 measurement, plus the lockfile and minified-code classes — the
filter is why `lines_*` tracks work rather than checkpoint churn, and why
`lines_*_raw` exists alongside it for auditing.

The list lives in `config.DEFAULT_EXCLUDED_PATHS`; `tests/test_docs.py`
fails if this block and the code drift apart.

## The families file

`FAMILIES_FILE` (default `families.yaml`) maps repositories to the middle
scope tier. The mapping is editorial — no rule derived from repo metadata
says that `commitgraph` and `commitgraph-deprecated` are one programme — so
it is a checked-in document rather than something inferred at runtime, and a
reuser is expected to replace it wholesale:

```yaml
families:
  agent-fleet:
    - NEEDLE
    - bead-rs
  infra:
    - declarative-config
```

One top-level `families` key; under it, family name → list of repo names.
Any other top-level key is ignored. A family whose list is empty or null is
legal and contributes nothing. The loader does not type-check beyond that —
a family value must be a list of name strings (a bare scalar would be
iterated character by character into nonsense mappings), and a family name
repeated in the YAML itself is collapsed by the YAML parser, last one
winning, before the loader sees it.

**Matching is exact and case-sensitive.** Names are looked up as whole
strings against the Forgejo repo name exactly as enumeration reports it —
no globs, no patterns, no substrings, no case folding: `needle-pod` does
not match `NEEDLE-POD`. A spelling or case mismatch is not an error; the
repo silently lands in `unassigned`, which is precisely what `meta.json`'s
`unassigned_repos` exists to surface.

Unmapped repos fall through to `unassigned` rather than failing, so a new
repo appears in the ecosystem and repo tiers on its first commit without
anyone touching config first. Two listing errors are distinguished:

- A repo listed twice under the **same** family is tolerated.
- A repo listed under **two different** families is a hard error
  (`ValueError`), not last-write-wins — duplicates would make family totals
  depend on dict ordering.

The same asymmetry governs a failed load:

| State of the file | Startup behavior |
|---|---|
| readable, valid | mapping loaded; startup proceeds |
| missing | warning logged, empty mapping, every repo reports `unassigned`; startup proceeds |
| empty file, or `families:` absent/null | empty mapping with no warning; every repo reports `unassigned` |
| present but unparseable YAML | process exits non-zero before the health server binds |
| a repo under two families | process exits non-zero before the health server binds |

An absent file is a legible degraded state — the whole fleet reports
`unassigned` and the panel still works — while a corrupt or ambiguous one is
a deployment fault: the process crashloops until the file is fixed, because
publishing a silently wrong middle tier would be worse than publishing none.
Missing is a config decision; broken is a bug.

**The file is read once per process**, at startup, before the health server
binds and before the first cycle. Nothing re-reads it mid-process, so
editing it changes nothing until the exporter restarts. What that restart
does to already-published cycles — the attribution-over-time contract — is
pinned in [output-schema.md](output-schema.md#family-attribution-over-time).

For the Kubernetes self-hosting profile, the `families.yaml` ConfigMap is
watched by Stakater Reloader through the Deployment annotation
`configmap.reloader.stakater.com/reload: git-activity-exporter-families`.
When GitOps reconciles a mapping-only ConfigMap commit, Reloader changes the
pod template and Kubernetes performs the restart automatically. The
replacement process loads the new map before its next cycle; no live
`kubectl rollout restart` or other manual cluster mutation is part of the
change. Install Reloader, or provide an equivalent ConfigMap checksum/rollout
controller, before using this profile.

## Destination credentials (`DEST_S3_*`)

`config.load()` reads the four required values straight from the process
environment and nothing else — it never opens a Secret, a file, or a store.
So the deployment's only job is to land those values in the pod env by some
secrets-by-reference means. The values must exist in as few places as
possible and never in git, a ConfigMap, or a log.

This environment injection is the supported provisioning path: a secret
manager (or a Kubernetes `Secret`/`ExternalSecret` reconciled from one) sets
`DEST_S3_ENDPOINT`, `DEST_S3_ACCESS_KEY_ID`, and
`DEST_S3_SECRET_ACCESS_KEY` in the exporter process environment. The exporter
does not accept S3 credentials as command-line flags, does not read credential
files, and never passes them to a subprocess. For Docker or local smoke runs,
use an ephemeral `--env-file` or inherited environment instead of
`--env NAME=value`, so the values are absent from the child process argument
list as well.

Credential values are intentionally absent from client-construction errors,
publication errors, retry logs, and the `/health` response. Provider error
messages are reduced to a safe type/code/status summary before they are logged;
the health contract exposes only publication state. The smoke scripts use
temporary env files and their output contains no credential values.

The author's deployment (manifests in the `declarative-config` repo,
`k8s/ardenone-cluster/git-activity-exporter/`) wires them like this, as the
reference for reusers:

| Variable | Provisioned from |
|---|---|
| `DEST_S3_ENDPOINT` | `secretKeyRef` → Secret `dashboard-s3-credentials`, key `S3_ENDPOINT` |
| `DEST_S3_ACCESS_KEY_ID` | `secretKeyRef` → Secret `dashboard-s3-credentials`, key `ACCESS_KEY_ID` |
| `DEST_S3_SECRET_ACCESS_KEY` | `secretKeyRef` → Secret `dashboard-s3-credentials`, key `SECRET_ACCESS_KEY` |
| `DEST_S3_BUCKET` | literal `dashboard-site` in the Deployment — not a credential |
| `DEST_S3_PREFIX` | literal `git-activity/data` in the Deployment — not a credential |
| `DEST_S3_ADDRESSING_STYLE` | literal `path` in the Deployment — see below |

`dashboard-s3-credentials` is generated in-cluster by the garage-operator:
the `GarageKey` custom resource `dashboard-write-key`
(`k8s/ardenone-cluster/garage-operator/keys.yml`) mints the key inside the
cluster, and the operator writes the resulting Secret; Reflector then mirrors
it from the `garage-operator` namespace into `git-activity-exporter`. No
value ever passes through a manifest. The key is scoped read+write to the
`dashboard-site` bucket and nothing else. `FORGE_TOKEN` is the one value that
does come from OpenBao: ExternalSecret `git-activity-exporter-forge`
(ClusterSecretStore `openbao-v2`, `refreshInterval: 1h`) mirrors KV v2 path
`ardenone-cluster/git-activity-exporter/forge`, field `forgejo-token`.

Two details a reuser will otherwise hit:

- `DEST_S3_ADDRESSING_STYLE` defaults to `virtual` (botocore's default), but
  the author's deployment sets `path` because Garage — like most
  self-hosted S3 stores — has no per-bucket virtual-host DNS. A bare run
  against Garage with only the four required values set will fail signature
  or DNS resolution until this is set to `path`.
- The reflected Secret is shared: every `dashboard-site` writer
  (`b2-usage-exporter`, `cluster-status`, `argo-workflows-exporter`, …) uses
  the same key. A reuser with a different destination needs only their own
  bucket's credentials; any mechanism that puts the four values in the env —
  a plain Secret, or an ExternalSecret against their own store — works.

## Rotation

**S3 access key.** Rotate at the source, never by hand-editing the reflected
Secret: update or re-mint the `dashboard-write-key` GarageKey, and the
operator rewrites `dashboard-s3-credentials`, Reflector re-mirrors it, and
Reloader (`reloader.stakater.com/auto: "true"` on the Deployment) restarts
the pod — no manifest change needed. Because the key is shared by every
`dashboard-site` writer, this rotation restarts all of them at once; if that
blast radius is unwanted, mint a dedicated GarageKey scoped to `dashboard-site`
and reflect it into this namespace alone. Verify by property, not by value:
the pod restarts and the next cycle publishes, or it doesn't.

**`FORGE_TOKEN`.** Write a new KV v2 version at
`ardenone-cluster/git-activity-exporter/forge` (field `forgejo-token`) — an
update, never a delete; version history is the rollback. The ExternalSecret
picks the new version up within its 1h `refreshInterval` and rewrites the
K8s Secret, Reloader restarts the pod, and the next cycle proves it. To
verify without reading the value:
`kubectl get externalsecret git-activity-exporter-forge -n git-activity-exporter`
shows `SecretSynced`, and `meta.json`'s freshness advances.
