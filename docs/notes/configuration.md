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
| `SHALLOW_SINCE_DAYS` | `WINDOW_DAYS + 10` | clone depth bound |
| `TRIM_MAX_LINES` | `5000` | above this a commit is flagged bulk |
| `TRIM_MAX_FILES` | `200` | above this a commit is flagged bulk |
| `EXCLUDED_PATH_PATTERNS` | [the four defaults](#default-excluded-path-patterns) | regexes, comma-separated; replaces the default list wholesale |
| `BEAD_BULK_CLOSE_THRESHOLD` | `150` | closures per `(repo, hour)` above which the cell is flagged |
| `BEAD_BULK_HOUR_SHARE` | `0.5` | share of an hour's fleet-wide closures already flagged before the whole hour is treated as bulk |
| `MAX_FAILURE_RATE` | `0.2` | fraction of repos that may fail before the cycle is withheld instead of published |
| `FAMILIES_FILE` | `families.yaml` | repo → family map |
| `VERSION_FILE` | `VERSION` | stamped into `meta.json` |
| `POLL_INTERVAL_SECONDS` | `3600` | sleep from one cycle attempt's end to the next cycle's start — [Poll-cycle lifecycle](#poll-cycle-lifecycle) |
| `GIT_TIMEOUT_SECONDS` | `600` | per git invocation — clone, fetch, `log`, `show`; what a timeout *does* is [Failure semantics](data-sources.md#failure-semantics) |
| `HTTP_TIMEOUT_SECONDS` | `30` | per Forgejo API call (repo enumeration only) |
| `HEALTH_PORT` | `8080` | port for the [health endpoints](#health-endpoints) |
| `LOG_LEVEL` | `INFO` | |
| `DEST_S3_ENDPOINT` | *(required)* | S3-compatible endpoint URL |
| `DEST_S3_BUCKET` | *(required)* | destination bucket |
| `DEST_S3_ACCESS_KEY_ID` | *(required)* | destination access key |
| `DEST_S3_SECRET_ACCESS_KEY` | *(required)* | destination secret key |
| `DEST_S3_REGION` | `us-east-1` | botocore region; most S3-compatible stores ignore it |
| `DEST_S3_ADDRESSING_STYLE` | `virtual` | `path` wherever the store has no per-bucket virtual-host DNS — see [Destination credentials](#destination-credentials-dest_s3_) |
| `DEST_S3_PREFIX` | `git-activity/data` | key prefix under the bucket; trailing slash stripped |

## Health endpoints

The health server binds `0.0.0.0:HEALTH_PORT` after configuration, the family
map, and the S3 client have loaded, and before the first collection cycle. A
probe made before that point receives a connection failure rather than an HTTP
response. Once the server is bound, these are the complete contracts:

| Request | Status | Payload | Meaning |
|---|---:|---|---|
| `GET /health` | `200` | empty, zero-byte body | The process is alive. This does not depend on collection or publication success. |
| `GET /ready`, before the first successful cycle | `503` | empty, zero-byte body | The process has not yet completed a publication cycle in this process lifetime. |
| `GET /ready`, after the first successful cycle | `200` | empty, zero-byte body | A cycle has completed in this process lifetime. |
| `GET` any other path | `404` | empty, zero-byte body | Not an exporter endpoint. |

There is no JSON payload and no `Content-Type` header on these responses. The
status code is the entire application contract. These are exact GET paths; a
trailing slash or query string is a different path and therefore returns
`404`.

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
   remains live. Use `meta.json`'s `generated_at`, `repos_failed`, and
   `repos_stale` to assess current publication freshness and coverage.
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
begins the first cycle with no initial delay — the first interval sleep happens
only after the first cycle attempt has finished. A fresh deployment therefore
lands its first publication one cycle-duration after start, not
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
- **A failed cycle waits the full interval before retrying.** No backoff, no
  in-cycle retry — as in [failure semantics](data-sources.md#failure-semantics),
  the next poll *is* the retry.
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

## Why `SHALLOW_SINCE_DAYS` must exceed `WINDOW_DAYS`

A clone shallower than the reporting window truncates the oldest hours of
every chart with no error — the data simply is not there to find. `config.load()`
rejects that combination rather than letting it publish quietly wrong numbers.

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

## Destination credentials (`DEST_S3_*`)

`config.load()` reads the four required values straight from the process
environment and nothing else — it never opens a Secret, a file, or a store.
So the deployment's only job is to land those values in the pod env by some
secrets-by-reference means. The values must exist in as few places as
possible and never in git, a ConfigMap, or a log.

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
