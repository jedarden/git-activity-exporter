#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
IMAGE=${IMAGE:-git-activity-exporter-smoke:"$VERSION"}
NAME=${NAME:-git-activity-exporter-smoke-$$}
SMOKE_TMP=$(mktemp -d)

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT INT TERM

if [[ ${SKIP_BUILD:-0} != 1 ]]; then
    docker build --tag "$IMAGE" "$ROOT"
fi

docker run --detach --name "$NAME" --publish 127.0.0.1::8080 \
    --env FORGE_BASE_URL=http://127.0.0.1:9 \
    --env FORGE_OWNER=smoke \
    --env FORGE_TOKEN=smoke-token \
    --env DEST_S3_ENDPOINT=http://127.0.0.1:9 \
    --env DEST_S3_ACCESS_KEY_ID=smoke-access-key \
    --env DEST_S3_SECRET_ACCESS_KEY=smoke-secret-key \
    --env DEST_S3_BUCKET=smoke \
    --env DEST_S3_ADDRESSING_STYLE=path \
    --env CLONE_ROOT=/tmp/mirrors \
    --env POLL_INTERVAL_SECONDS=3600 \
    "$IMAGE" >/dev/null

HOST_PORT=$(docker port "$NAME" 8080/tcp | sed -n '1s/.*://p')
if [[ -z $HOST_PORT ]]; then
    echo "container did not publish port 8080" >&2
    exit 1
fi

for _ in $(seq 1 60); do
    status=$(curl --silent --show-error --output "$SMOKE_TMP/health" \
        --write-out '%{http_code}' "http://127.0.0.1:$HOST_PORT/health" || true)
    if [[ $status == 200 ]]; then
        break
    fi
    if [[ $(docker inspect --format '{{.State.Running}}' "$NAME") != true ]]; then
        docker logs "$NAME" >&2
        exit 1
    fi
    sleep 1
done

[[ ${status:-} == 200 ]]
ready_status=$(curl --silent --show-error --output "$SMOKE_TMP/ready" \
    --write-out '%{http_code}' "http://127.0.0.1:$HOST_PORT/ready")
[[ $ready_status == 503 ]]

[[ $(docker exec "$NAME" id -u) == 1000 ]]
[[ $(docker exec "$NAME" id -un) == appuser ]]
docker exec "$NAME" test -f /app/families.yaml
docker exec "$NAME" test -f /app/VERSION
[[ $(docker exec "$NAME" cat /app/VERSION | tr -d '[:space:]') == "$VERSION" ]]

[[ $(docker image inspect --format '{{.Config.User}}' "$IMAGE") == appuser ]]
docker image inspect --format '{{json .Config.ExposedPorts}}' "$IMAGE" \
    | grep -q '"8080/tcp"'

echo "container smoke passed: image=$IMAGE uid=1000 port=8080 health=200 ready=503"
