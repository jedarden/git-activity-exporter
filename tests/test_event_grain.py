"""The event-grain bead output, against a forensic fixture.

The fixture is the real on-disk shape -- `record_type`/`event` nesting, with
`origin_store_uuid` and `detail` at the event level -- copied from a fleet
workspace rather than invented, because a schema guess that drifts from what
bead-rs writes would break the factory ledger join silently, in the data
stack, months later.
"""
import io
import json
import os
import subprocess
from datetime import datetime, timezone

import pyarrow.parquet as pq

from src import aggregate, beads, parquet_io

WORKSPACE_UUID = "3c213df4-b69d-fca5-1263-5950727cf636"
WORKER = "claude-code-glm-5.3-flash-glm-vibe"
EARLY = "2020-01-01T00:00:00Z"


def _event(seq, issue, kind, actor, time, detail):
    return {
        "record_type": "event",
        "event": {
            "$schema": "urn:bead-rs:schema:event:native-v1",
            "origin_store_uuid": WORKSPACE_UUID,
            "origin_event_sequence": seq,
            "issue_id": issue,
            "kind": kind,
            "actor": actor,
            "time": time,
            "detail": detail,
        },
    }


# One bead's full lifecycle (created -> claimed -> closed) plus a
# released/reopened pair on a second bead: the five events the acceptance
# fixture asks for, and exactly the transitions the ledger join reads.
FORENSIC_FIXTURE = "\n".join([
    # State, not an event -- must never become a row.
    json.dumps({
        "record_type": "issue",
        "issue": {
            "id": "gitact-65c81e6f", "base_status": "in_progress",
            "issue_type": "task", "priority": 2, "revision": 2,
            "created_at": "2026-09-02T03:08:51.754039063Z",
            "updated_at": "2026-09-06T04:33:47.839853123Z",
        },
    }),
    json.dumps(_event(1, "gitact-65c81e6f", "created", "system",
                      "2026-09-02T03:08:51.754039063Z",
                      {"actor": "system", "issue_id": "gitact-65c81e6f",
                       "issue_type": "task", "priority": 2, "title": "t"})),
    json.dumps(_event(2, "gitact-65c81e6f", "claimed", WORKER,
                      "2026-09-06T04:33:47.881245113Z",
                      {"policy": "standard", "resulting_base_status": "in_progress",
                       "with_lease": True, "lease_ttl_seconds": 3600})),
    json.dumps(_event(3, "gitact-65c81e6f", "closed", "system",
                      "2026-09-06T05:12:09.401772631Z",
                      {"prior_base_status": "in_progress", "reason": "Completed"})),
    json.dumps(_event(4, "gitact-4797240e", "released", "system",
                      "2026-09-06T06:02:33.510924001Z",
                      {"prior_assignee": WORKER, "resulting_base_status": "open"})),
    json.dumps(_event(5, "gitact-4797240e", "reopened", "system",
                      "2026-09-06T06:45:10.207334889Z",
                      {"prior_base_status": "closed", "resulting_base_status": "open",
                       "prior_assignee": WORKER})),
    # A truncated record: skipped and counted, never fatal.
    '{"record_type": "event", "event": {"origin_store_uuid"',
]) + "\n"


def _parse(cutoff="2020-01-01T00:00:00+00:00"):
    return beads.parse_events(
        FORENSIC_FIXTURE, "fixture-repo", datetime.fromisoformat(cutoff))


def _mirror_with_forensic(tmp_path, text=FORENSIC_FIXTURE):
    """A checkout that carries a forensic log the way a bare mirror does, so
    read_events' `git show HEAD:` path is exercised for real."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / ".beads" / "checkpoint"
    path.mkdir(parents=True)
    (path / "forensic.jsonl").write_text(text)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "add", ".beads/checkpoint/forensic.jsonl"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "commit", "-q", "--no-verify", "-m", "forensic"], check=True, env=env)
    return str(tmp_path)


def test_five_fixture_events_round_trip_with_every_column(tmp_path):
    # The acceptance case: the fixture goes forensic log -> events -> rows ->
    # Parquet -> read back, and every documented column survives.
    mirror = _mirror_with_forensic(tmp_path)
    events = beads.read_events(mirror, "fixture-repo", window_days=3650, timeout=60)
    assert len(events) == 5, "the issue snapshot and the truncated line are not events"

    table = parquet_io.table_to_parquet_bytes(
        aggregate.bead_event_rows(events, {}), parquet_io.BEAD_EVENTS_SCHEMA)
    rows = pq.read_table(io.BytesIO(table)).to_pylist()

    assert rows, "nothing was written"
    assert [r["kind"] for r in rows] == [
        "created", "claimed", "closed", "released", "reopened"]

    closed = rows[2]
    assert closed["ts_utc"] == "2026-09-06T05:12:09Z"
    assert closed["hour_utc"] == "2026-09-06T05:00:00Z"
    assert closed["repo"] == "fixture-repo"
    assert closed["workspace_uuid"] == WORKSPACE_UUID
    assert closed["issue_id"] == "gitact-65c81e6f"
    assert closed["actor"] == "system"
    assert closed["resulting_status"] == "closed"
    assert closed["is_bulk_import"] is False


def test_workspace_uuid_is_the_forensic_logs_origin_store_uuid():
    # A bead id is only unique inside one workspace, so this column is what
    # makes issue_id joinable across the fleet.
    assert all(e["workspace_uuid"] == WORKSPACE_UUID for e in _parse())


def test_every_forensic_kind_is_published_not_just_the_counted_ones():
    # The ledger needs the transitions the hourly rollup ignores; `created`
    # in particular is the only record of when a bead came into existence.
    assert {e["kind"] for e in _parse()} == {
        "created", "claimed", "closed", "released", "reopened"}


def test_resulting_status_comes_from_the_log_then_from_the_kind():
    by_kind = {e["kind"]: e["resulting_status"] for e in _parse()}

    assert by_kind["claimed"] == "in_progress"
    assert by_kind["released"] == "open"
    assert by_kind["reopened"] == "open"
    # A close's detail records the *prior* status and the reason; the result
    # is the kind itself.
    assert by_kind["closed"] == "closed"
    # Not a status transition -- left null rather than guessed.
    assert by_kind["created"] is None


def test_actor_is_preserved_verbatim():
    by_kind = {e["kind"]: e["actor"] for e in _parse()}
    assert by_kind["claimed"] == WORKER
    assert by_kind["closed"] == "system"


def test_events_before_the_window_are_dropped():
    # The window filter runs on the event time, so a stale fixture cannot
    # leak into a live cycle's publish.
    out = _parse(cutoff="2026-09-06T05:00:00+00:00")
    assert [e["kind"] for e in out] == ["closed", "released", "reopened"]


def test_state_records_and_malformed_lines_are_skipped_not_fatal():
    assert len(_parse()) == 5


def test_an_event_without_a_time_is_counted_as_malformed(caplog):
    caplog.set_level("WARNING")
    timeless = json.dumps(_event(9, "gitact-0000000", "updated", "system", None, {}))
    good = json.dumps(_event(1, "gitact-65c81e6f", "created", "system", EARLY, {}))

    out = beads.parse_events(
        timeless + "\n" + good, "fixture-repo",
        datetime(1999, 1, 1, tzinfo=timezone.utc),
    )

    assert [e["kind"] for e in out] == ["created"]
    assert any("malformed" in r.getMessage() for r in caplog.records)
