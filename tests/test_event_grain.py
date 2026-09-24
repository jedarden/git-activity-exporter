"""The event-grain bead output, against a forensic fixture.

The fixture is the real on-disk shape -- `record_type`/`event` nesting, with
`origin_store_uuid` and `detail` at the event level -- copied from a fleet
workspace rather than invented, because a schema guess that drifts from what
bead-rs writes would break the factory ledger join silently, in the data
stack, months later.
"""
import hashlib
import io
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src import aggregate, beads, gitscan, parquet_io

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
]) + "\n"

TRUNCATED_FIXTURE = FORENSIC_FIXTURE + '{"record_type":"event","event":{'


def _manifest(text, total_records=None):
    if total_records is None:
        total_records = sum(1 for line in text.split("\n") if line.strip())
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return json.dumps({
        "active_root": {
            "path": f"objects/{digest}.jsonl",
            "sha256": digest,
        },
        "total_record_count": total_records,
    })


def _parse(cutoff="2020-01-01T00:00:00+00:00"):
    return beads.parse_events(
        FORENSIC_FIXTURE, "fixture-repo", datetime.fromisoformat(cutoff))


def _mirror_with_forensic(tmp_path, text=FORENSIC_FIXTURE, manifest=None):
    """A checkout that carries a forensic log the way a bare mirror does, so
    read_events' `git show HEAD:` path is exercised for real."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / ".beads" / "checkpoint"
    path.mkdir(parents=True)
    (path / "forensic.jsonl").write_text(text)
    paths = [".beads/checkpoint/forensic.jsonl"]
    if manifest is not None:
        (path / "current.json").write_text(manifest)
        paths.append(".beads/checkpoint/current.json")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "add", *paths], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "commit", "-q", "--no-verify", "-m", "forensic"], check=True, env=env)
    return str(tmp_path)


def test_a_repo_without_a_forensic_log_reads_as_empty_not_an_error(tmp_path):
    # Only 64 of 97 repos that committed in the last 30 days carry a forensic
    # log (measured 2026-08-17): absence is the normal case for a third of
    # the fleet and must read as "no bead events", never as a failed repo.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("a repo that never used beads\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "add", "README.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
                    "commit", "-q", "--no-verify", "-m", "no beads"], check=True, env=env)

    assert beads.read_events(str(tmp_path), "quiet-repo", window_days=90, timeout=60) == []


def test_an_empty_present_forensic_log_reads_as_empty(tmp_path):
    mirror = _mirror_with_forensic(tmp_path, "")

    assert beads.read_events(
        mirror, "empty-forensic-repo", window_days=90, timeout=60
    ) == []


def test_a_manifest_matching_the_forensic_file_is_accepted(tmp_path):
    mirror = _mirror_with_forensic(
        tmp_path, FORENSIC_FIXTURE, _manifest(FORENSIC_FIXTURE)
    )

    assert len(beads.read_events(
        mirror, "fixture-repo", window_days=3650, timeout=60
    )) == 5


def test_manifest_detects_truncation_on_a_record_boundary(tmp_path):
    first = json.dumps(
        _event(1, "gitact-0000000", "created", "system", EARLY, {})
    )
    second = json.dumps(
        _event(2, "gitact-0000001", "closed", "system", EARLY, {})
    )
    complete = first + "\n" + second + "\n"
    mirror = _mirror_with_forensic(
        tmp_path, first + "\n", _manifest(complete)
    )

    with pytest.raises(beads.ForensicParseError, match="record count"):
        beads.read_events(mirror, "truncated-repo", window_days=3650, timeout=60)


def test_manifest_detects_forensic_content_changes_at_the_same_record_count(tmp_path):
    manifest = json.loads(_manifest(FORENSIC_FIXTURE))
    manifest["active_root"]["sha256"] = "0" * 64
    mirror = _mirror_with_forensic(
        tmp_path, FORENSIC_FIXTURE, json.dumps(manifest)
    )

    with pytest.raises(beads.ForensicParseError, match="checksum"):
        beads.read_events(mirror, "changed-repo", window_days=3650, timeout=60)


def test_malformed_checkpoint_manifest_fails_the_repository(tmp_path):
    mirror = _mirror_with_forensic(tmp_path, FORENSIC_FIXTURE, "{")

    with pytest.raises(beads.ForensicParseError, match="malformed forensic checkpoint manifest"):
        beads.read_events(mirror, "manifest-repo", window_days=3650, timeout=60)


def test_a_present_but_unreadable_forensic_log_is_an_error(tmp_path):
    mirror = _mirror_with_forensic(tmp_path)
    blob = subprocess.run(
        ["git", "-C", mirror, "rev-parse", f"HEAD:{beads.FORENSIC_PATH}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    object_path = Path(mirror) / ".git" / "objects" / blob[:2] / blob[2:]
    unreadable_path = object_path.with_suffix(".unreadable")
    object_path.rename(unreadable_path)

    try:
        with pytest.raises(gitscan.GitError, match=r"git .* show .* failed"):
            beads.read_events(mirror, "broken-repo", window_days=90, timeout=60)
    finally:
        unreadable_path.rename(object_path)


def test_a_git_tree_at_the_forensic_path_is_not_treated_as_missing(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    forensic_path = tmp_path / ".beads" / "checkpoint" / "forensic.jsonl"
    forensic_path.mkdir(parents=True)
    (forensic_path / "child").write_text("not a forensic file\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
         "add", ".beads/checkpoint/forensic.jsonl/child"],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false",
         "commit", "-q", "--no-verify", "-m", "tree"],
        check=True, env=env,
    )

    with pytest.raises(beads.ForensicParseError, match="not a regular Git blob"):
        beads.read_events(
            str(tmp_path), "tree-repo", window_days=90, timeout=60
        )


def test_unicode_line_separators_inside_json_strings_are_data():
    text = json.dumps(
        _event(1, "gitact-0000000", "created", "system", EARLY,
               {"title": "before\u2028after"}),
        ensure_ascii=False,
    )

    assert len(beads.parse_events(
        text, "fixture-repo", datetime(1999, 1, 1, tzinfo=timezone.utc)
    )) == 1


def test_malformed_json_fails_the_whole_repository():
    with pytest.raises(beads.ForensicParseError, match="malformed forensic JSON at line 7"):
        beads.parse_events(
            FORENSIC_FIXTURE + "not-json\n", "fixture-repo",
            datetime.fromisoformat(EARLY),
        )


def test_a_truncated_final_record_fails_the_whole_repository():
    with pytest.raises(beads.ForensicParseError, match="malformed forensic JSON at line 7"):
        beads.parse_events(
            TRUNCATED_FIXTURE, "fixture-repo", datetime.fromisoformat(EARLY)
        )


@pytest.mark.parametrize(
    "record",
    [
        "NaN",
        "[]",
        "null",
        "{}",
        '{"record_type":"issue"}',
        '{"record_type":"evnet","event":{}}',
        '{"record_type":"event","event":[]}',
    ],
)
def test_a_malformed_record_fails_the_repository(record):
    with pytest.raises(beads.ForensicParseError, match="line 1"):
        beads.parse_events(
            record, "fixture-repo", datetime.fromisoformat(EARLY)
        )


@pytest.mark.parametrize(
    "record_type",
    [
        "issue",
        "attempt_outcome",
        "provenance_receipt",
        "redaction_finding",
        "redaction_acknowledgment",
        "redaction_receipt",
        "redaction_epoch",
        "redaction_tombstone",
    ],
)
def test_recognized_non_event_records_are_validated_and_ignored(record_type):
    text = json.dumps({
        "record_type": record_type,
        record_type: {"id": "fixture"},
    })

    assert beads.parse_events(
        text, "fixture-repo", datetime.fromisoformat(EARLY)
    ) == []


@pytest.mark.parametrize("detail", [None, [], "metadata", 1, True])
def test_event_detail_accepts_any_json_value(detail):
    text = json.dumps(
        _event(1, "gitact-0000000", "updated", "system", EARLY, detail)
    )

    assert len(beads.parse_events(
        text, "fixture-repo", datetime(1999, 1, 1, tzinfo=timezone.utc)
    )) == 1


def test_decimal_unix_second_timestamp_is_interpreted_as_utc():
    timestamp = int(datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc).timestamp())
    text = json.dumps(
        _event(1, "gitact-0000000", "created", "system", str(timestamp), {})
    )

    events = beads.parse_events(
        text, "fixture-repo", datetime.fromisoformat(EARLY)
    )

    assert events[0]["ts"] == timestamp


def test_wrong_typed_stated_resulting_status_is_malformed():
    text = json.dumps(
        _event(1, "gitact-0000000", "claimed", "system", EARLY,
               {"resulting_base_status": 0})
    )

    with pytest.raises(beads.ForensicParseError, match="invalid resulting_base_status"):
        beads.parse_events(
            text, "fixture-repo", datetime(1999, 1, 1, tzinfo=timezone.utc)
        )


def test_duplicate_event_identity_fails_even_outside_the_window():
    duplicate = json.dumps(
        _event(1, "gitact-65c81e6f", "created", "system", EARLY, {})
    )
    with pytest.raises(beads.ForensicParseError, match="duplicate forensic event identity"):
        beads.parse_events(
            duplicate + "\n" + duplicate, "fixture-repo",
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )


def test_five_fixture_events_round_trip_with_every_column(tmp_path):
    # The acceptance case: the fixture goes forensic log -> events -> rows ->
    # Parquet -> read back, and every documented column survives.
    mirror = _mirror_with_forensic(tmp_path)
    events = beads.read_events(mirror, "fixture-repo", window_days=3650, timeout=60)
    assert len(events) == 5, "the issue snapshot is not an event"

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


def test_state_records_are_ignored_but_event_records_are_validated():
    assert len(_parse()) == 5


def test_an_event_without_a_time_is_malformed():
    timeless = json.dumps(_event(9, "gitact-0000000", "updated", "system", None, {}))

    with pytest.raises(beads.ForensicParseError, match="invalid time"):
        beads.parse_events(
            timeless, "fixture-repo",
            datetime(1999, 1, 1, tzinfo=timezone.utc),
        )
