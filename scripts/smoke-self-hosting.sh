#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PROFILE_DIR="$ROOT/examples/self-hosting"
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
IMAGE=${IMAGE:-git-activity-exporter-self-hosting:"$VERSION"}
PROJECT=${COMPOSE_PROJECT_NAME:-git-activity-exporter-self-hosting-$$}
SMOKE_TMP=$(mktemp -d)
FIXTURE_ROOT="$SMOKE_TMP/fixture"

python "$ROOT/scripts/check-release-drift.py"

cleanup() {
    EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
        docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
        --profile self-hosting down --volumes --remove-orphans >/dev/null 2>&1 || true
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

EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
    docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
    --profile self-hosting up --detach

HOST_PORT=$(EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
    docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
    port exporter 8080 | sed -n '1s/.*://p')
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
    running=$(EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
        docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
        ps --status running --services)
    if ! grep -qx exporter <<< "$running"; then
        EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
            docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
            logs exporter >&2 || true
        exit 1
    fi
    sleep 1
done

[[ ${health_status:-} == 200 ]]
[[ ${ready_status:-} == 200 ]]

S3_PORT=$(EXPORTER_IMAGE="$IMAGE" FIXTURE_ROOT="$FIXTURE_ROOT" \
    docker compose --project-name "$PROJECT" --file "$PROFILE_DIR/compose.yaml" \
    port s3-fixture 9000 | sed -n '1s/.*://p')
BROWSER_HOST=dashboard.example.test
BROWSER_BASE_URL="http://127.0.0.1:$S3_PORT/git-activity/data"
# The Host header selects the fixture's website handler. The path is the same
# path the authenticated dashboard browser uses; the fixture maps it to the
# objects written through the S3 API above. No S3 credentials are sent here.
curl --fail --silent --show-error \
    --header "Host: $BROWSER_HOST" \
    "$BROWSER_BASE_URL/current.json" > "$SMOKE_TMP/current.json"

python - "$BROWSER_BASE_URL" "$BROWSER_HOST" "$SMOKE_TMP/current.json" <<'PY'
import io
import json
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pyarrow.parquet as pq

browser_base, browser_host, pointer_path = sys.argv[1:]


def browser_open(url, method="GET"):
    return urlopen(Request(url, method=method, headers={"Host": browser_host}), timeout=10)


pointer = json.loads(open(pointer_path).read())
assert set(pointer["objects"]) == {
    "hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"
}
assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", pointer["cycle_id"])
assert pointer["cycle_id"].startswith(pointer["generated_at"].replace("-", "").replace(":", ""))

pointer_url = f"{browser_base}/current.json"
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
assert events == []
PY

echo "self-hosting smoke passed: owner=reuser family=reuser-projects bucket=reuser-git-activity prefix=exports/reuser image=$IMAGE pvc=self-hosting-mirrors"
