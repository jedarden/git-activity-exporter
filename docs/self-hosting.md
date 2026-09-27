# Self-hosting guide

This guide replaces the reference deployment's Forgejo owner, family map,
S3 destination, credentials, and workload settings with your own. The
exporter is a single sequential worker: it enumerates repositories from
Forgejo, keeps bounded mirrors on persistent storage, and publishes one
complete cycle to an S3-compatible bucket.

The fastest way to understand the wiring is the repository-owned smoke
profile. It uses fake Forgejo and S3 endpoints and does not exercise your
credentials or production infrastructure:

```bash
scripts/smoke-self-hosting.sh
```

For an actual deployment, use the Kubernetes profile described below. The
profile in [`examples/self-hosting/kubernetes.yaml`](../examples/self-hosting/kubernetes.yaml)
is a starting point, not the author's deployment to apply unchanged.

## 1. Choose your values

Decide these values before editing the workload:

| Value | Example | Where it is used |
| --- | --- | --- |
| Forgejo URL | `https://forgejo.example.com` | `FORGE_BASE_URL` |
| Forgejo owner | `analytics` | `FORGE_OWNER` |
| S3 bucket | `analytics-git-activity` | `DEST_S3_BUCKET` |
| S3 prefix | `exports/git-activity` | `DEST_S3_PREFIX` |
| Image | `registry.example/analytics/git-activity-exporter:0.1.43` | Deployment `image` |
| Mirror volume | a writable 20 GiB or larger RWO PVC | `CLONE_ROOT` and the volume mount |

Use a bucket/prefix dedicated to this exporter. Only one exporter replica may
write a prefix; two writers can race while publishing the fixed compatibility
keys. Keep `replicas: 1` and `strategy.type: Recreate`.

## 2. Create the Forgejo credential

Create a token for the Forgejo user represented by `FORGE_OWNER`. It needs to
be able to:

- call the repository search endpoint for that owner;
- read every public or private repository that should be included; and
- clone and fetch those repositories over Git HTTPS.

The exporter uses one `FORGE_TOKEN` for both API requests and Git operations.
Git receives it through a process environment credential helper; it is not
placed in a clone URL, command argument, mirror config, or log. Do not put the
token in a ConfigMap or commit it to the repository.

Set the Forgejo inputs in the workload's non-secret configuration:

```yaml
FORGE_BASE_URL: https://forgejo.example.com
FORGE_OWNER: analytics
```

`FORGE_BASE_URL` is the Forgejo origin, not the `/api/v1` URL. The API path is
added by the exporter. `FORGE_OWNER` is case-sensitive and must be the owner
whose repositories Forgejo returns.

## 3. Replace `families.yaml`

Families are an editorial repository-to-family mapping. Replace the example
mapping with your own repository names; matching is exact and case-sensitive:

```yaml
families:
  platform:
    - api
    - web
  research:
    - experiments
```

A repository may occur more than once in the same family, but may not occur in
two different families. Repositories omitted from the file are still scanned
and published under `unassigned`, so a partial map is safe while setting up.
The file is read at process startup. The Kubernetes example therefore marks
the Deployment with
`configmap.reloader.stakater.com/reload: git-activity-exporter-families`.
With [Stakater Reloader](https://github.com/stakater/Reloader) installed, a
GitOps-only change to that ConfigMap changes the pod template and Kubernetes
rolls out a replacement process; no manual `kubectl rollout restart` is
needed. If your cluster uses another controller, configure the equivalent
ConfigMap-to-Deployment rollout before relying on mapping changes.

For Kubernetes, put this content in the `git-activity-exporter-families`
ConfigMap (the example uses the same file at
`/etc/git-activity-exporter/families.yaml`) and set:

```yaml
FAMILIES_FILE: /etc/git-activity-exporter/families.yaml
```

Do not edit the image's packaged `families.yaml` as a substitute for mounting
the deployment-specific file. Mounting it makes the active mapping explicit
and keeps image rebuilds independent of attribution changes.

## 4. Create the S3 destination

Create the bucket before the first cycle and give the exporter credentials
scoped to the selected bucket/prefix. The first cycle may read a missing
`current.json`; later cycles also need to list and delete old cycle prefixes.
At minimum, the credential needs:

- `GetObject`, `PutObject`, and `DeleteObject` for the selected prefix;
- `ListBucket` for the selected prefix; and
- `HeadObject`/object-metadata read permission for the selected prefix.

Before the first repository collection, the exporter runs a destination
permission preflight. It lists the prefix, writes a unique temporary probe,
reads its metadata with `HeadObject`, reads its body with `GetObject`, and
deletes it. The probe is removed after the check, and collection does not
begin until every operation succeeds. If the check fails, `/health` remains
live, `/ready` remains `503`, and the exporter reports only the failed
operation plus a sanitized provider code/status before retrying on the next
poll interval; credentials and raw provider messages are not logged. The
repository-owned smoke profile includes an S3 fixture with the metadata
(`HEAD`) behavior needed to exercise this check:

```bash
scripts/smoke-self-hosting.sh
```

Configure these values in the workload:

```yaml
DEST_S3_ENDPOINT: https://s3.example.com
DEST_S3_BUCKET: analytics-git-activity
DEST_S3_PREFIX: exports/git-activity
DEST_S3_ADDRESSING_STYLE: path
DEST_S3_REGION: us-east-1
```

`DEST_S3_ENDPOINT`, `DEST_S3_ACCESS_KEY_ID`, and
`DEST_S3_SECRET_ACCESS_KEY` are secrets. Keep them in your secret manager and
inject them with Secret references or an ExternalSecret. `DEST_S3_BUCKET`,
`DEST_S3_PREFIX`, and the addressing style are non-secret workload settings.
Use `path` for stores without bucket-specific virtual-host DNS (a common
self-hosted setup); use `virtual` only when your S3 service supports it.

The exporter supports environment injection only: it reads these values from
the process environment at startup. It does not read a credential file or
command-line flags, and it never puts the values in subprocess arguments,
logs, exception text, health responses, or smoke-test output. In a temporary
Docker run, provide them through an ephemeral `--env-file` or inherited
environment; do not write them as `--env NAME=value` arguments.

The exporter writes `current.json`, `meta.json`, and three Parquet objects at
the prefix. Consumers should resolve `current.json` first; it names one
complete cycle and avoids mixing fixed keys from adjacent publications.

## 5. Build and pin an image

Build the image in your own registry, or use a published version you have
chosen deliberately. The image tag must be an immutable semver tag; do not use
`:latest`.

```bash
VERSION=$(tr -d '[:space:]' < VERSION)
IMAGE=registry.example/analytics/git-activity-exporter:${VERSION}
docker build --tag "$IMAGE" .
docker push "$IMAGE"
```

The committed Compose fixture, Kubernetes example, and release references in
this guide are checked against `VERSION`. Run the check after changing the
version, or use its write mode to update those references together:

```bash
python scripts/check-release-drift.py --write
```

The check also rejects explicit `:latest` image tags.

The Dockerfile contains `git`, the Python dependencies, the exporter, and the
default `VERSION`/`families.yaml`. The deployment-specific family file and
runtime values override the defaults.

## 6. Configure the Kubernetes workload

Copy [`examples/self-hosting/kubernetes.yaml`](../examples/self-hosting/kubernetes.yaml)
into your GitOps repository (or an isolated test directory) and change all of
the example-specific values:

1. Set the Forgejo URL and owner in `git-activity-exporter-config`.
2. Replace the S3 bucket, prefix, addressing style, and optional region.
3. Replace the inline `families.yaml` content with your mapping.
4. Replace the Deployment image with the pinned image you built.
5. Set the PVC storage class and size for your cluster. The volume must be
   writable by UID/GID 1000, mounted at `/data/mirrors`, and support
   `ReadWriteOnce`.
6. Keep `CLONE_ROOT: /data/mirrors`, one replica, and the `Recreate` strategy.
7. Keep the Reloader annotation, or configure an equivalent automatic rollout
   mechanism for `git-activity-exporter-families`.
8. If the cluster runs Prometheus Operator, copy
   [`examples/self-hosting/monitoring.yaml`](../examples/self-hosting/monitoring.yaml)
   alongside the workload. Adjust its Prometheus selector labels to match your
   instance. It adds the internal metrics Service, the `/metrics`
   `ServiceMonitor`, and the cycle alerts; do not apply the file on a cluster
   without the `monitoring.coreos.com` CRDs.

The example expects two Secrets, with these keys:

```text
Secret git-activity-exporter-forge:
  FORGE_TOKEN

Secret git-activity-exporter-s3:
  DEST_S3_ENDPOINT
  DEST_S3_ACCESS_KEY_ID
  DEST_S3_SECRET_ACCESS_KEY
```

Provision those Secrets out of band with your External Secrets, Sealed
Secrets, or other credential manager. For a temporary non-production cluster,
file-backed input avoids putting values on a command line:

Ensure the `git-activity-exporter` namespace exists before creating namespaced
Secrets. A GitOps controller can reconcile the Namespace object first; for a
disposable cluster, apply the example once to create it, create the Secrets,
then apply it again after editing the remaining values.

```bash
kubectl apply --filename examples/self-hosting/kubernetes.yaml
kubectl -n git-activity-exporter create secret generic git-activity-exporter-forge \
  --from-file=FORGE_TOKEN=./secrets/forge-token
kubectl -n git-activity-exporter create secret generic git-activity-exporter-s3 \
  --from-file=DEST_S3_ENDPOINT=./secrets/s3-endpoint \
  --from-file=DEST_S3_ACCESS_KEY_ID=./secrets/s3-access-key-id \
  --from-file=DEST_S3_SECRET_ACCESS_KEY=./secrets/s3-secret-access-key
```

The files must not have trailing newlines, must be readable only by the
operator, and must never be committed. Prefer a secret manager for a durable
deployment. Do not put these values in the ConfigMaps or in the guide.

Use your normal GitOps flow to reconcile the namespace, ConfigMaps, PVC, and
Deployment. In a disposable cluster, a direct apply is sufficient after the
Secrets exist:

```bash
kubectl apply --filename examples/self-hosting/kubernetes.yaml
```

If your cluster does not provide a default storage class, add its explicit
`storageClassName` to the PVC before reconciling it. Do not add a second
Deployment or a second writer for the same S3 prefix.

## 7. Validate the first successful cycle

The first cycle can be slow because every repository is cloned into the empty
mirror volume. `/health` is intentionally live during that cold start, while
`/ready` remains `503` until a complete publication succeeds. Do not restart
the pod just because readiness is initially `503`.

First check the rollout and logs:

```bash
kubectl -n git-activity-exporter rollout status \
  deployment/git-activity-exporter --timeout=2h
kubectl -n git-activity-exporter logs deployment/git-activity-exporter --tail=200
```

Forward the health port from the running Deployment:

```bash
kubectl -n git-activity-exporter port-forward \
  deployment/git-activity-exporter 8080:8080
```

In another terminal, the minimum endpoint checks are:

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/ready
```

The first command must return HTTP 200 and JSON containing a non-null
`last_successful_cycle_at` and `last_cycle_outcome` equal to `published`. The
second command must return HTTP 200 after that first successful cycle. Before
the first success, HTTP 503 from `/ready` is expected; after a later failed or
withheld cycle, readiness stays 200, so use `/health` for freshness and
outcome.

Then inspect the destination using your S3 client. The exact command depends
on the provider, but the checks are the same:

1. `current.json` exists at `s3://<bucket>/<prefix>/current.json`.
2. Its `cycle_id` and `generated_at` are valid, and its four named objects can
   all be downloaded.
3. The downloaded `meta.json` has the same `cycle_id` as `current.json`.
4. `repos_total` is greater than zero, `repos_scanned` is greater than zero,
   and `repos_failed` is empty (or every permitted failure is understood).
5. The Parquet rows contain your expected family names; a missing mapping will
   instead appear in `unassigned_repos`.

For a first-cycle failure, inspect `meta.json` only after a publication exists;
the previous pointer remains authoritative when collection, payload creation,
or S3 publication fails. Common causes are a token that can enumerate but not
clone private repositories, a wrong case in `FORGE_OWNER` or a family entry,
an S3 endpoint requiring `path` addressing, and a PVC that is not writable by
UID 1000.

## Ongoing operation

- Keep the mirror PVC. It is a rebuildable cache, but losing it turns the next
  cycle into a full cold clone.
- Keep one replica. Scale vertically or reduce the reporting workload rather
  than adding writers.
- Keep `SHALLOW_SINCE_DAYS >= WINDOW_DAYS`; the default adds ten days of
  history margin.
- After rotating a Forgejo or S3 credential, verify the Secret refresh and
  wait for `/health` to show a new published cycle.
- After changing `families.yaml`, commit the ConfigMap change through GitOps,
  wait for the automatic Deployment rollout, and verify the next cycle's
  Parquet family rows. Do not manually mutate the live Deployment.

The complete environment-variable and publication contracts are in
[`docs/notes/configuration.md`](notes/configuration.md) and
[`docs/notes/output-schema.md`](notes/output-schema.md). The release and
GitOps model is described in [`docs/notes/deployment.md`](notes/deployment.md).
