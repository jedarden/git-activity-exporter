# Deployment and release workflow

The exporter is released as a pinned Docker image and run as one Kubernetes
Deployment. The application repository is the source for the code and image;
the `declarative-config` repository is the source for the workload that runs
it. Building an image does not deploy it: the Deployment image tag must also be
updated in GitOps.

The reference manifests are in `declarative-config`:

- `k8s/iad-ci/argo-workflows/git-activity-exporter-build-workflowtemplate.yml`
  builds the image.
- `k8s/iad-ci/argo-events/git-activity-exporter-sensor.yml` starts that
  WorkflowTemplate for pushes to `main`.
- `k8s/ardenone-cluster/git-activity-exporter/` contains the namespace,
  ConfigMap, Deployment, ExternalSecret, and PVC.

## Image build and versioning

The `git-activity-exporter-build` WorkflowTemplate runs these steps in order:

1. Clone `jedarden/git-activity-exporter` from Forgejo and run
   `pip install -r requirements-dev.txt` followed by `python -m pytest tests/ -q`.
   Tests run before version resolution so a failed change cannot leave an
   auto-bump commit behind.
2. Read `VERSION` from `main`. If the triggering commit changed `VERSION`,
   that value is used. Otherwise, increment the patch component, commit the
   new `VERSION` as `ci: auto-bump version to ...`, and push that commit back
   to Forgejo. The sensor excludes commits authored by `Argo Workflows CI`, so
   this CI write-back does not recursively start another build.
3. Kaniko builds `Dockerfile` from the Forgejo `main` context and pushes
   `ronaldraygun/git-activity-exporter:<version>` to Docker Hub. The workflow
   uses a pinned Kaniko image and a cache repository; it never publishes or
   references `:latest`.

The Dockerfile packages Python 3.12 slim, `git`, `curl`, the pinned Python
requirements, `src/`, `VERSION`, and `families.yaml`. It runs as non-root
`appuser` (UID/GID 1000), exposes port 8080, and starts
`python -m src.main`. The image keeps `/app` as its working directory, so the
default relative paths resolve to `/app/VERSION` and `/app/families.yaml`.
The image's Docker healthcheck calls `/health`; the Kubernetes Deployment
defines the authoritative cluster probes described below.

The workflow passes the resolved version as a Kaniko build argument, but the
image's embedded version comes from the `VERSION` file copied from the build
context. Because an automatic bump is pushed before the Kaniko step, the
source version and image tag agree. The workflow serializes releases with the
`git-activity-exporter-release` mutex so version write-back and promotion do
not race one another. The resolved version is a workflow output passed to every
later stage; retries do not recalculate it.

## Self-hosting smoke profile

The committed [`examples/self-hosting/`](../../examples/self-hosting/) profile
is an opt-in reuser example. It is intentionally separate from the author's
default `families.yaml` and deployment: no `jedarden` owner, author bucket,
author prefix, or packaged family mapping is needed. The Kubernetes example
contains the workload shape, while the Compose profile is the runnable smoke
harness used to exercise one complete cycle locally.

The profile makes every reuser-specific input visible:

- `FORGE_OWNER=reuser` and a non-default Forgejo endpoint;
- a mounted `FAMILIES_FILE` mapping `reuser-project` to `reuser-projects`;
- `DEST_S3_ENDPOINT`, bucket `reuser-git-activity`, prefix `exports/reuser`,
  path-style addressing, and credentials supplied through environment variables
  in the fixture only;
- a single-writer mirror volume (`self-hosting-mirrors` in Compose and a
  `ReadWriteOnce` 20Gi PVC in Kubernetes); and
- a semver-pinned exporter image (`ronaldraygun/git-activity-exporter:0.1.43`).

The repository's release-drift check compares these committed image
references with `VERSION`, rejects mutable `:latest` or untagged references,
and covers the Dockerfile, release WorkflowTemplate fixture, Kubernetes
manifests, and examples. It runs as part of the test gate and can also update
the versioned references after an intentional version bump:

```bash
python scripts/check-release-drift.py --write
```

Run the smoke profile from the repository root:

```bash
scripts/smoke-self-hosting.sh
```

The script builds that pinned local tag unless `SKIP_BUILD=1` is set, creates a
temporary Git repository, starts the opt-in `self-hosting` Compose profile, and
waits for `/ready` to become `200`. It then resolves `current.json` from the
fixture S3 endpoint, reads every pointer-named object, validates all three
Parquet payloads, and checks that the published metadata and family rows name
the custom owner/mapping. The generated fixture credentials and repository are
ephemeral; production credentials belong in Secret references as shown in
`examples/self-hosting/kubernetes.yaml`.

## Container smoke verification

The repository-owned [`scripts/smoke-container.sh`](../../scripts/smoke-container.sh)
builds the image locally unless `SKIP_BUILD=1` is set, then runs it with
isolated dummy Forgejo and S3 endpoints. It verifies the built image's
`families.yaml` and `VERSION` files, the `appuser`/UID 1000 runtime identity,
the 8080 exposed port, `/health` returning 200, and `/ready` returning 503
before a successful cycle. The Argo WorkflowTemplate runs the same runtime
checks against the just-built versioned image after the Kaniko step.

The GitOps repository's
`scripts/test-git-activity-exporter-deployment.sh` performs the static gate
for the WorkflowTemplate and Deployment: every referenced image is tagged,
the Deployment image is semver-pinned, and the named `health` port and
`/health` liveness plus `/ready` readiness paths remain aligned.

## Promoting an image to the Deployment

The final workflow steps perform the handoff automatically, in this order:

1. `test` must pass before version resolution can write an automatic `VERSION`
   commit or build an image.
2. `smoke` must pass against the exact semver image that was pushed.
3. `promote` clones `declarative-config`, changes only the image pin in
   `k8s/ardenone-cluster/git-activity-exporter/deployment.yml`, commits it on
   `main`, and pushes it to Forgejo. It emits that GitOps commit SHA as the
   release record.
4. `verify-rollout` waits for the generated ArgoCD application to report the
   pushed revision as `Synced`/`Healthy`, then checks the Deployment's exact
   image and ready/available replica status through the read-only Kubernetes
   proxy.

The desired-state commit triggers ArgoCD's automated reconciliation. No
workflow step applies, patches, restarts, or rolls back a live resource, and
the image remains pinned to `ronaldraygun/git-activity-exporter:<version>`.
The repository-owned verification helper below remains the detailed release
check for the live probes and successful publication.

## Recovery after a partial release

The release has two durable recovery anchors: the `VERSION` commit in the
application repository and the semver image tag in the registry. The resolved
version is captured once by `resolve-version` and is passed unchanged to the
image build, smoke check, promotion, and rollout verification steps.

If `VERSION` was pushed but the image build or registry push failed:

1. Retry the failed `docker-build` step (or retry the workflow while it is
   still holding the release mutex). Do not edit or increment `VERSION`.
2. The resolver recognizes the existing `ci: auto-bump version to <version>`
   commit when it is re-entered, and reuses that version instead of creating a
   second auto-bump commit. An explicit `VERSION` commit is likewise reused.
3. The retry builds and pushes the same `:<version>` tag. Promotion remains
   unreachable until the image smoke check succeeds.

If the image was pushed and smoke-verified but GitOps promotion failed:

1. Retry `promote`; do not start another version bump or build a different
   image. Promotion receives both the resolved version and the smoke step's
   exact verified image reference.
2. Promotion fails closed unless those references are exactly
   `ronaldraygun/git-activity-exporter:<version>`. A registry tag existing by
   itself is not verification.
3. If the earlier push actually reached `declarative-config` but the workflow
   lost the result, the retry sees that the manifest already names the version,
   reuses the existing GitOps `HEAD`, and does not create a duplicate commit.

An image build or push failure never gets promoted, and a smoke failure never
gets promoted. A successful promotion is still not a completed release until
`verify-rollout` confirms the exact GitOps revision and image are reconciled by
ArgoCD. Use the workflow's retry/resume operation so these outputs and gates
remain attached to the same release attempt; do not manually promote an image
that skipped smoke verification.

## Runtime packaging and inputs

The container receives environment variables from three places:

| Source | Inputs | Purpose |
| --- | --- | --- |
| ConfigMap `git-activity-exporter-config` via `envFrom` | `FORGE_BASE_URL`, `FORGE_OWNER`, `WINDOW_DAYS`, `SHALLOW_SINCE_DAYS`, `CLONE_ROOT`, `POLL_INTERVAL_SECONDS`, `GIT_TIMEOUT_SECONDS`, `TRIM_MAX_LINES`, `TRIM_MAX_FILES`, `BEAD_BULK_CLOSE_THRESHOLD`, `LOG_LEVEL` | Non-secret collection and reporting settings. |
| Secret `git-activity-exporter-forge` via `secretKeyRef` | `FORGE_TOKEN` | Read-scope Forgejo API/Git credential used to enumerate and fetch repositories. |
| Secret `dashboard-s3-credentials` via `secretKeyRef` | `DEST_S3_ENDPOINT` (`S3_ENDPOINT`), `DEST_S3_ACCESS_KEY_ID` (`ACCESS_KEY_ID`), `DEST_S3_SECRET_ACCESS_KEY` (`SECRET_ACCESS_KEY`) | S3-compatible publication credentials. |

The Deployment supplies these non-secret destination values literally:

```text
DEST_S3_BUCKET=dashboard-site
DEST_S3_PREFIX=git-activity/data
DEST_S3_ADDRESSING_STYLE=path
```

`path` addressing is required for the reference Garage endpoint. Values not
listed in the ConfigMap use the application defaults documented in
[`configuration.md`](configuration.md). The reference Deployment does not
override `workingDir` or mount either runtime file, so the packaged
`/app/families.yaml` and `/app/VERSION` are used. A workload that changes
`workingDir` must set absolute `FAMILIES_FILE` and `VERSION_FILE` paths or
provide both files at the new working directory; a mounted replacement should
be read-only and the process must be restarted to load it.

Neither credential Secret is populated in Git. The Forgejo token is mirrored
by External Secrets from OpenBao path
`ardenone-cluster/git-activity-exporter/forge`, property `forgejo-token`, into
`git-activity-exporter-forge` with a one-hour refresh interval. The S3 Secret
is generated by the Garage operator and reflected into this namespace. Rotate
those credentials at their source; do not edit a generated or reflected
Secret by hand. The Deployment and ExternalSecret carry the Stakater Reloader
annotation so a source Secret or ConfigMap change restarts the pod.

## Browser-facing dashboard data path

The dashboard reads this export through the Garage website endpoint, not the
S3 API endpoint. The browser-facing URL is:

```text
https://dashboard.ardenone.com/git-activity/data/current.json
```

The `dashboard-site` Garage bucket is exposed by the `dashboard-site`
IngressRoute on Garage's website port `3902`; its `globalAlias` is
`dashboard.ardenone.com`, and the route is protected by the dashboard's
Authentik forward-auth middleware. A browser with a valid dashboard session
therefore fetches the page and data from one origin. The S3 API at
`https://s3.ardenone.com` (Garage port `3900`) is for the exporter and other
writers; it is not a browser data endpoint and its access key must never reach
page JavaScript.

The exporter writes through `dashboard-write-key`, whose bucket permission is
read/write because publication recovery reads old objects and publication
prunes old cycle prefixes. The browser does not use that key: website reads
are authorized by the dashboard host's Authentik session and then served from
the bucket's website view. This separates the writer's S3 permissions from
the browser's read-only surface.

### Pointer-first browser read

The consumer must fetch the pointer from the same website path and resolve
each `objects` value relative to that pointer URL. The values are relative to
`DEST_S3_PREFIX`, so the following is the complete shape (the dashboard page
uses the equivalent `window.location.origin` URL):

```js
const pointerURL = new URL('/git-activity/data/current.json', window.location.origin)
const pointer = await fetch(pointerURL, { cache: 'no-store' }).then(r => {
  if (!r.ok) throw new Error(`current.json: HTTP ${r.status}`)
  return r.json()
})

const objectURL = name => new URL(pointer.objects[name], pointerURL)
const payload = await Promise.all(
  Object.entries(pointer.objects).map(async ([name, key]) => {
    const response = await fetch(objectURL(name), { cache: 'force-cache' })
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`)
    return [name, await response.arrayBuffer()]
  }),
)
```

`current.json` is written with `Cache-Control: no-cache, max-age=0,
must-revalidate`; the explicit `no-store` fetch also prevents a browser from
reusing an old pointer during a refresh. Pointer-named cycle objects have
unique immutable URLs and are written with
`Cache-Control: public, max-age=31536000, immutable`. The legacy fixed keys
are also `no-cache`; they are not part of the atomic browser read path.

No CORS configuration is required for this path: the page and its data share
the `dashboard.ardenone.com` origin. Do not switch the consumer to
`s3.ardenone.com` merely because it is the S3 endpoint; that would turn a
same-origin read into a cross-origin request and would require an explicit,
origin-restricted Garage CORS policy for `GET`/`HEAD` and the response headers
the Parquet reader needs. If a future dashboard is hosted on another origin,
add that exact origin to the bucket's CORS policy and smoke-test its preflight
before changing this URL contract; never compensate with browser S3
credentials or an unrestricted wildcard when credentials are involved.

The read is consistent at the publication level. A single atomic PUT changes
`current.json`; every cycle object it names was uploaded before that PUT and
is immutable. A browser therefore reads one complete cycle, either the old
pointer or the new pointer, even while a publication is in flight. It must
not cache the pointer URL across refreshes or mix in fixed-key objects. Three
cycle prefixes are retained, which gives a reader that has already resolved a
previous pointer time to finish; a 404 for a named cycle object is a failed
snapshot and should cause the consumer to retry the pointer/object set rather
than fall back to fixed keys.

The self-hosting smoke profile exercises this exact website-shaped path. Its
fixture maps `/git-activity/data/current.json` and every relative pointer key
to the objects written through the S3 API, checks the cache headers, and
successfully reads and parses all three Parquet objects plus `meta.json`:

```bash
scripts/smoke-self-hosting.sh
```

## PVC and single-writer requirements

The exporter stores bare shallow repository mirrors under `/data/mirrors`.
`CLONE_ROOT` must point there (the reference ConfigMap does), and
`git-activity-exporter-mirrors` mounts that path from a `20Gi` `longhorn`
PersistentVolumeClaim with `ReadWriteOnce` access.

The mirrors are a rebuildable cache, not the published dataset. Losing the
PVC costs a slow cold collection cycle but does not delete S3 data. The volume
must be writable by UID/GID 1000; the pod `fsGroup: 1000` handles a fresh
root-owned Longhorn volume. Keep one replica and the `Recreate` strategy:
two pods cannot safely share the RWO volume and would race while publishing
the same S3 keys.

## Resource envelope and cold start

The reference Deployment requests `512Mi` of memory and limits the container
to `2Gi` (`100m`/`2` CPU). The request is a scheduling reservation, not a
process cap; the cgroup limit is the `2Gi` value. There is one process and one
sequential cycle, so collection lists, parsed rows, Arrow tables, and all four
serialized payloads can overlap in the same address space before the first S3
PUT. The exporter has no per-repo byte limit or streaming serializer.

These are the current measurements that size that envelope:

| Workload | Observed input/output | Resource implication |
|---|---:|---|
| Largest checked-in forensic blob measured (NEEDLE `HEAD`, 2026-09-27) | 21,270,143 bytes / 38,018 nonblank records | `git show HEAD:.beads/checkpoint/forensic.jsonl` reads the whole blob; parsing its 90-day window retained 34,417 events and reached 162,468KiB RSS on the measurement host. |
| 30-day fleet activity sample (2026-08-17) | ~14,000 commits; 41,626,557 changed `.beads/` lines; 16,650 migration closures | Line churn is not row count: `.beads/` lines are accumulated in commit totals, while the event log is parsed into event rows. A single 9,072,022-line/25,639-file artifact commit remains one commit row and is flagged by `TRIM_MAX_LINES`/`TRIM_MAX_FILES`; those thresholds do not cap memory. |
| Representative serialization probe at that sample's row counts | 6,128 hourly rows + 14,000 commit rows + 16,650 event rows → `hourly.parquet` 30,584 bytes, `commits.parquet` 303,121 bytes, `bead_events.parquet` 122,317 bytes, `meta.json` 590 bytes; total 456,612 bytes | The probe used the real Arrow schemas and representative row shapes, not a production cycle artifact. It reached 174,864KiB RSS locally, so serialized bytes substantially understate construction memory. Treat this as a regression baseline, not a hard capacity guarantee. |
| Largest mirror inputs | `agent-transcript-archive` 3.4GiB; `unfairmarket-research` 1.5GiB; all full mirrors 14.04GiB | These are PVC/clone and Git pack-operation bounds, not payload sizes. The first live cold pass exceeded the old 600-second Git timeout on both large histories; the reference ConfigMap now uses 1,800 seconds per Git attempt. |

The 2Gi limit is therefore a vertical capacity boundary, not a graceful
per-repository quota. A forensic file or commit scan that stays below it can
finish normally even when it is large. A Git timeout, unreadable forensic
blob, malformed record, or integrity failure is handled per repository:
`repos_failed` records the reason, the repo's rows are omitted, and the cycle
publishes the remaining fleet when the failed fraction is at most
`MAX_FAILURE_RATE` (20% by default). Above that fraction the cycle is
withheld and the previous pointer remains live. `WINDOW_DAYS` does not reduce
forensic input memory because the file is validated and parsed before its
window filter is applied.

If construction or parsing crosses the container's `2Gi` limit, Linux kills
the process with `OOMKilled`; there is no opportunity to turn that allocation
failure into a single-repo `repos_failed` entry. The Deployment restarts the
pod, `/ready` returns 503 again until a cycle succeeds, and a failure during
collection or payload generation has not written a new S3 object because all
four payloads are generated before publication. The previous pointer remains
the recovery anchor; any staged prefix left before the commit by a kill during
publication is inert and is removed by later retention cleanup. This is why a growing
forensic log needs vertical memory headroom or a future streaming/parser
change, not another exporter replica.

### First-deploy timing and scaling

A cold deployment clones the fleet serially before it can publish. NEEDLE
alone measured 148.6 seconds and 133MB for a 60-day cold clone, while an
existing mirror fetched in 1.21 seconds. The two largest histories exceeded
600 seconds during the first live fleet pass, so there is no defensible
single-minute SLA for a full-fleet cold cycle; plan for an hours-long first
publication rather than the warm-cycle cadence. The readiness probe permits
`240 × 30s = 2h` of unready time, but that is probe grace, not a clone
deadline: `/health` remains live and the pod is not restarted merely because
`/ready` is still 503. The hard per-attempt timeout is 1,800 seconds. With
roughly 111 repos, one timed-out attempt for every repo is already about 55.5
hours; the three-attempt remote retry policy makes the pathological ceiling
longer. These are failure ceilings, not the expected runtime.

After the first successful publication, a restart with the PVC intact performs
a warm fetch pass, not a fleet-wide cold clone. Keep `replicas: 1` and
`Recreate`: this writer has no S3 lease or fencing protocol, so horizontal
scaling would corrupt the single destination prefix. Scale vertically (raise
the request and limit in GitOps) or reduce the reporting/fleet workload until
payload construction remains comfortably below `2Gi`; do not add replicas as
a memory workaround.

## Deployment probes and rollout behavior

The container port is named `health` and is 8080. The Deployment uses:

- Liveness: `GET /health`, initial delay 15 seconds, period 30 seconds.
- Readiness: `GET /ready`, initial delay 30 seconds, period 30 seconds,
  failure threshold 240.

`/health` returns 200 once the process has bound its server, even while the
first cold cycle is cloning the fleet. Its JSON payload reports
`last_successful_cycle_at` and `last_cycle_outcome`; alert when the timestamp
is older than two `POLL_INTERVAL_SECONDS` intervals as documented in
[`configuration.md`](configuration.md#staleness-alert). `/ready` returns 503
until the process has completed its first successful publication, then remains
200 for that process lifetime. This separation is intentional: a cold start
can exceed ordinary probe windows, and using publication readiness as liveness
would restart the pod forever before its first cycle finished. A later failed
or withheld cycle does not clear readiness; use the health payload for
freshness and outcome, and inspect the published `meta.json` for coverage.

## Operational monitoring

The exporter exposes two complementary monitoring surfaces:

- `/health` is the JSON liveness and freshness snapshot. It is suitable for a
  read-only probe and for an operator's first diagnosis.
- `/metrics` is a dependency-free Prometheus exposition endpoint. It exports
  publication age, withheld and failed cycle counters, consecutive prune
  failures, and consecutive publication failures. It does not expose tokens,
  S3 endpoints, repository names, or error text.

Apply the optional Prometheus Operator resources in
[`examples/self-hosting/monitoring.yaml`](../../examples/self-hosting/monitoring.yaml)
alongside the workload, or carry their equivalent `Service`, `ServiceMonitor`,
and `PrometheusRule` resources into the GitOps deployment. The rule selector
labels must match the target Prometheus instance. The reference thresholds are:

| Signal | Alert threshold | Severity | Why it matters |
|---|---|---|---|
| Last successful publication | No publication after two poll intervals, or published age over two poll intervals; hold 10m | critical | The pointer is no longer fresh. |
| Withheld cycles | At least two in a rolling two-hour window; hold 10m | warning | The failure-rate guard is repeatedly refusing partial data. |
| `prune.consecutive_failures` | At least one for 15m | warning | New data can publish while old cycle cleanup is stuck. |
| Publication failures | At least two consecutive attempts; hold 10m | critical | The previous complete pointer remains live while new writes fail. |
| Monitoring scrape | `/metrics` absent for 5m | critical | The alert surface itself is blind. |

For the default `POLL_INTERVAL_SECONDS=3600`, the two-hour windows represent
two attempts. A single transient withheld cycle or prune error is intentionally
visible in `/health` and logs but does not page. The startup grace applies only
to the no-publication state; once a publication exists, its age is measured
from its `generated_at` timestamp.

### Cycle alert runbook

Use read-only access while investigating. Do not delete S3 objects, edit a
Secret, restart the Deployment manually, or use `kubectl apply`, `patch`,
`rollout`, or `set image` against this ArgoCD-managed workload.

1. **Confirm the alert and classify the cycle.** Check the Prometheus alert
   expression and target status, then inspect the live pod without exposing
   credentials:

   ```bash
   kubectl -n git-activity-exporter get pods -l app=git-activity-exporter
   kubectl -n git-activity-exporter logs deployment/git-activity-exporter --since=2h
   kubectl -n git-activity-exporter port-forward deployment/git-activity-exporter 18080:8080
   curl --fail http://127.0.0.1:18080/health
   curl --fail http://127.0.0.1:18080/metrics
   ```

   Stop the port-forward when finished. `last_cycle_outcome=withheld` means
   the failure-rate guard protected the previous dataset; `failed` means the
   attempt raised outside that guard. A non-zero
   `prune.consecutive_failures` is post-commit cleanup only and does not make
   the latest publication invalid.

2. **For withheld cycles**, find the `cycle withheld` warning and the preceding
   `repo ... failed` lines. Check the affected Forgejo repositories, token
   refresh status, and the `MAX_FAILURE_RATE` value in the committed ConfigMap.
   Correct the Forgejo/network/volume/configuration cause through GitOps or
   the credential's managed source, then wait for the next poll. Do not lower
   `MAX_FAILURE_RATE` or publish the partial output just to clear an alert;
   verify the next `/health` outcome is `published` and inspect its `meta.json`
   coverage.

3. **For stale or failed cycles**, check whether the log says `publication
   failed` or only `cycle failed`. For a publication failure, read the current
   pointer and its metadata through the normal read-only S3/dashboard path and
   confirm that they still name one complete cycle. Check the S3 endpoint,
   bucket/prefix, addressing style, and the read/write permission preflight;
   fix inputs declaratively and wait for a retry. For collection or payload
   failures, inspect the sanitized error and the repository/PVC symptoms; a
   restart is not the first recovery step because `/health` is deliberately
   live while a cycle retries.

4. **For prune failures**, use the warning's committed and affected cycle IDs
   to identify the retained prefix. Verify that the writer still has the
   configured list and delete permissions and that the S3 service is healthy.
   Leave old prefixes in place while investigating; pruning is best effort and
   the next successful cleanup will retry them. The alert clears when
   `prune.consecutive_failures` returns to zero.

5. **Close the incident only after recovery is observable.** The health
   payload must show `last_cycle_outcome: "published"` with a timestamp newer
   than two poll intervals, the publication-failure streak must be zero, and
   any prune alert must have cleared. Confirm the Prometheus target is still
   scraping and record the relevant sanitized log line and GitOps/secret
   change in the incident. If the pod itself is unhealthy, correct the image,
   configuration, Secret source, PVC, or resource request in Git and let ArgoCD
   reconcile; use the safe rollback procedure below for a bad release.

## Argo and GitOps reconciliation

There are two related Argo paths:

1. **Argo Events/Workflows in `iad-ci`** receives the configured push webhook,
   filters for `push` on `refs/heads/main` (excluding CI write-back), runs the
   test/version/build workflow, and publishes the versioned image.
2. **ArgoCD in the ardenone cluster** discovers the
   `k8s/ardenone-cluster/git-activity-exporter/` namespace directory through
   `manifest-appset-ardenone-cluster`. It creates the generated application
   `git-activity-exporter-ns-ardenone-cluster` targeting
   `https://k3s-server-a.ardenone.com:6443` and reconciles the plain manifests
   with automated sync, prune, and self-heal enabled.

The workload directory deliberately has no additional `*-application.yml`:
the namespace ApplicationSet owns plain manifests there. Adding a second
Application for the same directory creates competing ArgoCD owners.

The normal release sequence is therefore:

1. Push the application change to Forgejo `main`.
2. Let the sensor start the WorkflowTemplate and wait for tests and the
   versioned image push to succeed.
3. Let the workflow pin that image tag in `declarative-config`, push the GitOps
   commit, and verify the generated ArgoCD Application and Deployment.
4. Run the post-reconcile verification below with the GitOps commit SHA and
   exact semver image tag. It confirms the generated ArgoCD Application is
   `Synced` and `Healthy`, the replacement pod is running the requested image,
   the `/health` and `/ready` probe paths are wired correctly, and the running
   process reports a successful publication.
5. Confirm the resulting `meta.json` freshness and coverage before calling
   the release complete.

## Post-reconcile verification

After pushing the image-pin commit, wait for the generated Application and
verify the live workload from a machine with read-only ArgoCD and Kubernetes
access. The repository-owned helper performs the checks as one bounded,
repeatable command:

```bash
scripts/verify-gitops-deployment.sh \
  --app git-activity-exporter-ns-ardenone-cluster \
  --revision <gitops-image-pin-commit-sha> \
  --image ronaldraygun/git-activity-exporter:<semver>
```

The helper:

- waits for the ArgoCD Application to be `Synced` and `Healthy`, then requires
  its observed revision to equal `--revision`;
- inspects the Deployment template and every selected pod, requiring the
  exact semver-pinned `--image`, a `Running`/`Ready` pod, and a ready container;
- checks that the Deployment's named `health` port is 8080, liveness is
  `GET /health`, and readiness is `GET /ready`;
- creates only a loopback `kubectl port-forward` and checks live `/health=200`
  and `/ready=200`; and
- requires `/health` to report both a non-null
  `last_successful_cycle_at` and `last_cycle_outcome: "published"`. That
  outcome is the application-level evidence that a complete publication
  committed; inspect the published `meta.json` for its freshness and coverage.

The helper never applies, patches, deletes, restarts, or rolls back a cluster
resource. A port-forward is temporary and local; its process is cleaned up on
exit. If the command fails, inspect ArgoCD status, pod events/logs, and the
published metadata read-only, then correct the image pin or workload inputs in
Git.

### Safe rollback

Rollback is another desired-state change, not a live cluster operation. Keep
the previous image tag and the image-pin commit SHA in the release record. If
the new pod is unhealthy or its publication is bad:

1. Identify the prior pin from the parent of the bad GitOps commit, for
   example `git show <bad-commit>^:k8s/ardenone-cluster/git-activity-exporter/deployment.yml`.
2. In the `declarative-config` checkout, change only the Deployment image back
   to that prior semver tag. If the bad commit changed only that image line,
   `git revert --no-edit <bad-commit>` is equivalent; do not use a whole-file
   restore for a commit that contains unrelated changes.
3. Commit the rollback on `main` with the prior image and run `git push origin
   main` to the configured Forgejo `origin`. ArgoCD will reconcile that new
   commit; do not use `kubectl rollout undo`, `kubectl set image`, or another
   direct cluster mutation.
4. Run `scripts/verify-gitops-deployment.sh` again with the rollback commit
   SHA and prior image tag. Do not call the rollback complete until ArgoCD is
   `Synced`/`Healthy`, the pods run the prior image, both probes pass, and the
   process reports a new successful publication.

For troubleshooting, use read-only ArgoCD/Kubernetes inspection and workflow
logs. Fix image pins, configuration, secret provisioning, PVC sizing, or
probe behavior in Git; do not mutate ArgoCD-managed resources live.
