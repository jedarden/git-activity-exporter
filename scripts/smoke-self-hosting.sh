#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PROFILE_DIR="$ROOT/examples/self-hosting"
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
IMAGE=${IMAGE:-git-activity-exporter-self-hosting:"$VERSION"}
PROJECT=${COMPOSE_PROJECT_NAME:-git-activity-exporter-self-hosting-$$}
SMOKE_TMP=$(mktemp -d)
FIXTURE_ROOT="$SMOKE_TMP/fixture"
SMOKE_ENV="$SMOKE_TMP/compose.env"

cat > "$SMOKE_ENV" <<EOF
SMOKE_FORGE_TOKEN=self-hosting-forge-token
SMOKE_S3_ACCESS_KEY_ID=self-hosting-access-key
SMOKE_S3_SECRET_ACCESS_KEY=self-hosting-secret-key
EOF

compose() {
    EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
        docker compose --env-file "$SMOKE_ENV" --project-name "$PROJECT" \
        --file "$PROFILE_DIR/compose.yaml" "$@"
}

if [[ -n ${SMOKE_PYTHON_IMAGE:-} ]]; then
    docker run --rm \
        --volume "$ROOT:/repo:ro" \
        --workdir /repo \
        "$SMOKE_PYTHON_IMAGE" \
        python /repo/scripts/check-release-drift.py
else
    python "$ROOT/scripts/check-release-drift.py"
fi

cleanup() {
    docker rm -f "$PROJECT-missing-forge_token" \
        "$PROJECT-missing-dest_s3_secret_access_key" >/dev/null 2>&1 || true
    compose --profile self-hosting down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT INT TERM

mkdir -p "$FIXTURE_ROOT/source"
git -C "$FIXTURE_ROOT/source" init --quiet --initial-branch=main
git -C "$FIXTURE_ROOT/source" config user.name self-hosting-smoke
git -C "$FIXTURE_ROOT/source" config user.email self-hosting-smoke@example.invalid
cat > "$FIXTURE_ROOT/source/README.md" <<'EOF'
# Reuser fixture

This repository proves that the self-hosting profile scans a non-author owner.
EOF
git -C "$FIXTURE_ROOT/source" add README.md
git -C "$FIXTURE_ROOT/source" commit --quiet --message 'feat: publish self-hosting fixture'
git clone --quiet --bare "$FIXTURE_ROOT/source" "$FIXTURE_ROOT/reuser-project.git"

if [[ ${SKIP_BUILD:-0} != 1 ]]; then
    docker build --tag "$IMAGE" "$ROOT"
fi

compose --profile self-hosting up --detach

HOST_PORT=$(compose port exporter 8080 | sed -n '1s/.*://p')
if [[ -z $HOST_PORT ]]; then
    echo "self-hosting exporter did not publish port 8080" >&2
    exit 1
fi

for _ in $(seq 1 90); do
    health_status=$(curl --silent --show-error --output "$SMOKE_TMP/health" \
        --write-out '%{http_code}' "http://127.0.0.1:$HOST_PORT/health" || true)
    ready_status=$(curl --silent --show-error --output "$SMOKE_TMP/ready" \
        --write-out '%{http_code}' "http://127.0.0.1:$HOST_PORT/ready" || true)
    if [[ $health_status == 200 && $ready_status == 200 ]]; then
        break
    fi
    running=$(compose ps --status running --services)
    if ! grep -qx exporter <<< "$running"; then
        compose logs exporter >&2 || true
        exit 1
    fi
    sleep 1
done

[[ ${health_status:-} == 200 ]]
[[ ${ready_status:-} == 200 ]]

# Exercise the image's real startup path with only synthetic credentials. The
# healthy exporter above proves that this same harness reaches Ready when all
# required keys are present. These isolated containers omit one runtime key at
# a time and must exit before the /ready handler can answer successfully.
RUNTIME_ENV="$SMOKE_TMP/runtime.env"
cat > "$RUNTIME_ENV" <<'EOF'
FORGE_BASE_URL=http://forgejo-fixture:8081
FORGE_OWNER=reuser
FORGE_TOKEN=self-hosting-forge-token
DEST_S3_ENDPOINT=http://s3-fixture:9000
DEST_S3_BUCKET=reuser-git-activity
DEST_S3_PREFIX=exports/reuser
DEST_S3_ACCESS_KEY_ID=self-hosting-access-key
DEST_S3_SECRET_ACCESS_KEY=self-hosting-secret-key
DEST_S3_ADDRESSING_STYLE=path
EOF

assert_missing_runtime_key_fails_closed() {
    local key=$1
    local name="$PROJECT-missing-${key,,}"
    local case_env="$SMOKE_TMP/${key}.env"
    local log="$SMOKE_TMP/${key}.log"
    local host_port running ready_status

    awk -F= -v missing_key="$key" '$1 != missing_key' "$RUNTIME_ENV" > "$case_env"
    docker run --detach --name "$name" --network "${PROJECT}_default" \
        --publish 127.0.0.1::8080 --env-file "$case_env" \
        "$IMAGE" sh -c 'python -m src.main; status=$?; sleep 3; exit "$status"' >/dev/null

    host_port=$(docker port "$name" 8080/tcp | sed -n '1s/.*://p')
    if [[ -z $host_port ]]; then
        echo "missing-key container did not publish its readiness port: key=$key" >&2
        return 1
    fi

    # The wrapper keeps the container around briefly after the real app exits,
    # so the smoke can probe the same port Kubernetes would use. Config
    # validation precedes health-server start, so /ready must stay unavailable.
    sleep 1
    ready_status=$(curl --silent --output "$SMOKE_TMP/missing-ready" \
        --write-out '%{http_code}' "http://127.0.0.1:$host_port/ready" || true)
    if [[ $ready_status == 200 ]]; then
        echo "container became Ready without required key: $key" >&2
        return 1
    fi

    docker logs "$name" > "$log" 2>&1
    if ! grep -Fq "config error: missing required env var: $key" "$log"; then
        echo "container failure did not identify missing key: $key" >&2
        return 1
    fi
    for value in self-hosting-forge-token self-hosting-access-key self-hosting-secret-key; do
        if grep -Fq "$value" "$log"; then
            echo "container failure exposed a synthetic secret value for key: $key" >&2
            return 1
        fi
    done

    for _ in $(seq 1 20); do
        running=$(docker inspect --format '{{.State.Running}}' "$name")
        [[ $running == true ]] || break
        sleep 0.25
    done
    if [[ $running != false ]]; then
        echo "container did not fail startup for missing key: $key" >&2
        return 1
    fi
    exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
    if [[ $exit_code == 0 ]]; then
        echo "container exited successfully without required key: $key" >&2
        return 1
    fi

    docker rm "$name" >/dev/null
    echo "missing-key smoke passed: key=$key readiness=$ready_status state=exited"
}

assert_missing_runtime_key_fails_closed FORGE_TOKEN
assert_missing_runtime_key_fails_closed DEST_S3_SECRET_ACCESS_KEY

S3_PORT=$(compose port s3-fixture 9000 | sed -n '1s/.*://p')
BROWSER_HOST=dashboard.example.test
BROWSER_BASE_URL="http://127.0.0.1:$S3_PORT/git-activity/data"
INTERNAL_BROWSER_BASE_URL="http://s3-fixture:9000/git-activity/data"
# The Host header selects the fixture's website handler. The path is the same
# path the authenticated dashboard browser uses; the fixture maps it to the
# objects written through the S3 API above. No S3 credentials are sent here.
curl --fail --silent --show-error \
    --header "Host: $BROWSER_HOST" \
    "$BROWSER_BASE_URL/current.json" > "$SMOKE_TMP/current.json"

compose exec --no-TTY exporter python - \
    "$INTERNAL_BROWSER_BASE_URL" "$BROWSER_HOST" <<'PY'
import io
import json
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pyarrow.parquet as pq

browser_base, browser_host = sys.argv[1:]


def browser_open(url, method="GET"):
    return urlopen(Request(url, method=method, headers={"Host": browser_host}), timeout=10)


pointer_url = f"{browser_base}/current.json"
with browser_open(pointer_url) as response:
    pointer = json.loads(response.read())
assert set(pointer["objects"]) == {
    "hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"
}
assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", pointer["cycle_id"])
assert pointer["cycle_id"].startswith(pointer["generated_at"].replace("-", "").replace(":", ""))

with browser_open(pointer_url) as response:
    assert response.headers["Cache-Control"] == "no-cache, max-age=0, must-revalidate"

objects = {}
for name, relative_key in pointer["objects"].items():
    object_url = urljoin(pointer_url, relative_key)
    assert f"/cycles/{pointer['cycle_id']}/" in object_url
    with browser_open(object_url) as response:
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        objects[name] = response.read()

meta = json.loads(objects["meta.json"])
assert meta["cycle_id"] == pointer["cycle_id"]
assert meta["repos_total"] == 1
assert meta["repos_scanned"] == 1
assert meta["repos_failed"] == []
assert meta["unassigned_repos"] == []
assert meta["repo_errors"] == {}

commits = pq.read_table(
    io.BytesIO(objects["commits.parquet"]), use_threads=False
).to_pylist()
hourly = pq.read_table(
    io.BytesIO(objects["hourly.parquet"]), use_threads=False
).to_pylist()
events = pq.read_table(
    io.BytesIO(objects["bead_events.parquet"]), use_threads=False
).to_pylist()
assert commits and {row["family"] for row in commits} == {"reuser-projects"}
assert hourly and {row["family"] for row in hourly} == {"reuser-projects"}
assert {row["repo"] for row in commits} == {"reuser-project"}
assert {row["repo"] for row in hourly} == {"reuser-project"}
assert events == []
PY

echo "self-hosting smoke passed: owner=reuser family=reuser-projects bucket=reuser-git-activity prefix=exports/reuser image=$IMAGE pvc=self-hosting-mirrors"
