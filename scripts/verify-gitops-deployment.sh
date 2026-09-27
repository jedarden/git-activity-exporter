#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/verify-gitops-deployment.sh \
  --app APP --revision GITOPS_COMMIT --image REGISTRY/IMAGE:SEMVER

Read-only post-reconcile verification for the reference GitOps deployment.
Optional flags override the reference resource names:
  --namespace NAMESPACE  Kubernetes namespace (default: git-activity-exporter)
  --deployment NAME      Deployment name (default: git-activity-exporter)
  --container NAME       container name (default: exporter)
  --selector SELECTOR    pod label selector (default: app=git-activity-exporter)
  --timeout SECONDS      Argo/pod wait timeout (default: 600)
  --local-port PORT      loopback port for the temporary port-forward (default: 18080)
EOF
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

APP=""
REVISION=""
EXPECTED_IMAGE=""
NAMESPACE="git-activity-exporter"
DEPLOYMENT="git-activity-exporter"
CONTAINER="exporter"
SELECTOR="app=git-activity-exporter"
TIMEOUT=600
LOCAL_PORT=18080

while (($#)); do
    case "$1" in
        --app)
            (($# >= 2)) || fail "--app requires a value"
            APP=$2
            shift 2
            ;;
        --revision)
            (($# >= 2)) || fail "--revision requires a value"
            REVISION=$2
            shift 2
            ;;
        --image)
            (($# >= 2)) || fail "--image requires a value"
            EXPECTED_IMAGE=$2
            shift 2
            ;;
        --namespace)
            (($# >= 2)) || fail "--namespace requires a value"
            NAMESPACE=$2
            shift 2
            ;;
        --deployment)
            (($# >= 2)) || fail "--deployment requires a value"
            DEPLOYMENT=$2
            shift 2
            ;;
        --container)
            (($# >= 2)) || fail "--container requires a value"
            CONTAINER=$2
            shift 2
            ;;
        --selector)
            (($# >= 2)) || fail "--selector requires a value"
            SELECTOR=$2
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || fail "--timeout requires a value"
            TIMEOUT=$2
            shift 2
            ;;
        --local-port)
            (($# >= 2)) || fail "--local-port requires a value"
            LOCAL_PORT=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[[ -n "$APP" ]] || fail "--app is required"
[[ -n "$REVISION" ]] || fail "--revision is required"
[[ -n "$EXPECTED_IMAGE" ]] || fail "--image is required"
[[ "$REVISION" =~ ^[0-9a-fA-F]{7,64}$ ]] ||
    fail "--revision must be a commit SHA (7-64 hexadecimal characters)"
[[ "$EXPECTED_IMAGE" =~ ^[^[:space:]@]+:[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    fail "--image must end in an immutable semver tag such as :0.1.31"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "--timeout must be a positive integer"
[[ "$LOCAL_PORT" =~ ^[1-9][0-9]*$ ]] || fail "--local-port must be a positive integer"

need_command argocd
need_command kubectl
need_command curl
need_command python3

TMP_DIR=$(mktemp -d)
PORT_FORWARD_PID=""

cleanup() {
    if [[ -n "$PORT_FORWARD_PID" ]]; then
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
        wait "$PORT_FORWARD_PID" 2>/dev/null || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

echo "Waiting for ArgoCD application $APP at revision $REVISION"
check_application() {
    python3 - "$TMP_DIR/application.json" "$REVISION" <<'PY'
import json
import sys

path, expected_revision = sys.argv[1:]
app = json.loads(open(path, encoding="utf-8").read())
status = app.get("status", {})
sync = status.get("sync", {})
health = status.get("health", {})

if sync.get("status") != "Synced":
    raise SystemExit(f"FAIL: ArgoCD sync status is {sync.get('status')!r}")
if health.get("status") != "Healthy":
    raise SystemExit(f"FAIL: ArgoCD health status is {health.get('status')!r}")
if sync.get("revision") != expected_revision:
    raise SystemExit(
        "FAIL: ArgoCD revision is "
        f"{sync.get('revision')!r}, expected {expected_revision!r}"
    )
PY
}

application_verified=0
for ((attempt = 1; attempt <= TIMEOUT; attempt++)); do
    if argocd app get "$APP" --output json \
        >"$TMP_DIR/application.json" 2>"$TMP_DIR/argocd-get.log" &&
        check_application > /dev/null 2>&1; then
        application_verified=1
        break
    fi
    sleep 1
done
if [[ "$application_verified" != 1 ]]; then
    cat "$TMP_DIR/argocd-get.log" >&2
    fail "ArgoCD application did not reach the requested synced/healthy revision within ${TIMEOUT}s"
fi

argocd app wait "$APP" --sync --health --timeout "$TIMEOUT"
argocd app get "$APP" --output json >"$TMP_DIR/application.json"
check_application

kubectl --namespace "$NAMESPACE" get deployment "$DEPLOYMENT" --output json \
    >"$TMP_DIR/deployment.json"
python3 - "$TMP_DIR/deployment.json" "$CONTAINER" "$EXPECTED_IMAGE" <<'PY'
import json
import sys

path, expected_container, expected_image = sys.argv[1:]
deployment = json.loads(open(path, encoding="utf-8").read())
spec = deployment.get("spec", {})
template = spec.get("template", {})
pod_spec = template.get("spec", {})
containers = [
    item for item in pod_spec.get("containers", [])
    if item.get("name") == expected_container
]
if len(containers) != 1:
    raise SystemExit(
        f"FAIL: expected exactly one container named {expected_container!r}"
    )
container = containers[0]
if container.get("image") != expected_image:
    raise SystemExit(
        f"FAIL: Deployment image is {container.get('image')!r}, "
        f"expected {expected_image!r}"
    )

ports = {item.get("name"): item.get("containerPort") for item in container.get("ports", [])}
if ports.get("health") != 8080:
    raise SystemExit("FAIL: Deployment health port is not named health on 8080")

if container.get("livenessProbe", {}).get("httpGet") != {
    "path": "/health",
    "port": "health",
}:
    raise SystemExit("FAIL: liveness probe is not GET /health on the health port")
if container.get("readinessProbe", {}).get("httpGet") != {
    "path": "/ready",
    "port": "health",
}:
    raise SystemExit("FAIL: readiness probe is not GET /ready on the health port")
PY

kubectl --namespace "$NAMESPACE" get pods --selector "$SELECTOR" --output json \
    >"$TMP_DIR/pods.json"
python3 - "$TMP_DIR/pods.json" "$CONTAINER" "$EXPECTED_IMAGE" <<'PY'
import json
import sys

path, expected_container, expected_image = sys.argv[1:]
pods = json.loads(open(path, encoding="utf-8").read()).get("items", [])
if not pods:
    raise SystemExit("FAIL: the Deployment selector returned no pods")

for pod in pods:
    name = pod.get("metadata", {}).get("name", "<unnamed>")
    if pod.get("status", {}).get("phase") != "Running":
        raise SystemExit(f"FAIL: pod {name} is not Running")
    ready = next(
        (
            condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
            if condition.get("type") == "Ready"
        ),
        False,
    )
    if not ready:
        raise SystemExit(f"FAIL: pod {name} is not Ready")
    containers = [
        item
        for item in pod.get("status", {}).get("containerStatuses", [])
        if item.get("name") == expected_container
    ]
    if len(containers) != 1:
        raise SystemExit(
            f"FAIL: pod {name} has no unique {expected_container!r} status"
        )
    container = containers[0]
    if container.get("image") != expected_image:
        raise SystemExit(
            f"FAIL: pod {name} runs {container.get('image')!r}, "
            f"expected {expected_image!r}"
        )
    if not container.get("ready"):
        raise SystemExit(f"FAIL: container {expected_container!r} in pod {name} is not ready")
PY

echo "ArgoCD and Kubernetes state match $EXPECTED_IMAGE; checking live probes"
kubectl --namespace "$NAMESPACE" port-forward --address 127.0.0.1 \
    "deployment/$DEPLOYMENT" "$LOCAL_PORT:8080" \
    >"$TMP_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!

health_status=000
ready_status=000
for ((attempt = 1; attempt <= TIMEOUT; attempt++)); do
    health_status=$(curl --silent --show-error --output "$TMP_DIR/health.json" \
        --write-out '%{http_code}' --max-time 5 \
        "http://127.0.0.1:$LOCAL_PORT/health" || true)
    ready_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --max-time 5 "http://127.0.0.1:$LOCAL_PORT/ready" || true)
    if [[ "$health_status" == 200 && "$ready_status" == 200 ]]; then
        break
    fi
    if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        cat "$TMP_DIR/port-forward.log" >&2
        fail "kubectl port-forward exited before the probes passed"
    fi
    sleep 1
done

[[ "$health_status" == 200 ]] || fail "/health returned HTTP $health_status"
[[ "$ready_status" == 200 ]] || fail "/ready returned HTTP $ready_status"

python3 - "$TMP_DIR/health.json" <<'PY'
import json
import sys

health = json.loads(open(sys.argv[1], encoding="utf-8").read())
if health.get("last_cycle_outcome") != "published":
    raise SystemExit(
        "FAIL: /health does not report a successful publication: "
        f"{health.get('last_cycle_outcome')!r}"
    )
if not health.get("last_successful_cycle_at"):
    raise SystemExit("FAIL: /health has no last_successful_cycle_at")
PY

echo "Post-reconcile verification passed: app=$APP revision=$REVISION image=$EXPECTED_IMAGE health=200 ready=200 publication=published"
