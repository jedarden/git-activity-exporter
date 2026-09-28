"""The checked-in v1 output fixture is the downstream handoff contract.

This deliberately reads the fixture as a consumer would: current.json is the
only authority for the staged object paths, and the fixed-key copies are
checked only as the legacy mirror. The assertions are independent of the
cycle builder tests so a producer refactor cannot make this fixture test pass
by exercising the same in-memory objects that produced it.
"""
import json
import io
import re
import shutil
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src import parquet_io, publish
from tests.test_meta_schema import validate_meta


FIXTURE = Path(__file__).parent / "fixtures" / "output-contract" / "v1"
EXPECTED_FILES = ("hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json")
TYPE_MAP = {"string": pa.string(), "int64": pa.int64(), "bool": pa.bool_()}


def _json(name):
    return json.loads((FIXTURE / name).read_text())


def _parquet(path):
    return pq.read_table(io.BytesIO(path.read_bytes()))


def test_manifest_is_versioned_and_matches_pointer_and_meta():
    manifest = _json("manifest.json")
    pointer = _json("current.json")
    meta = _json("meta.json")

    assert manifest["contract"] == "git-activity-exporter.output"
    assert manifest["fixture_version"] == 1
    assert manifest["pointer"] == "current.json"
    assert manifest["fixed_objects"] == list(EXPECTED_FILES)
    assert manifest["cycle_id"] == pointer["cycle_id"] == meta["cycle_id"]
    assert manifest["generated_at"] == pointer["generated_at"] == meta["generated_at"]
    assert validate_meta(meta) == []
    publish.validate_cycle_id(pointer["cycle_id"], pointer["generated_at"])


def test_pointer_resolves_one_complete_cycle_and_legacy_mirror_matches():
    pointer = _json("current.json")
    assert pointer["schema_version"] == publish.POINTER_SCHEMA_VERSION
    assert set(pointer["objects"]) == set(EXPECTED_FILES)
    assert all(key.startswith(f"cycles/{pointer['cycle_id']}/") for key in pointer["objects"].values())

    resolved = {}
    for name, relative_key in pointer["objects"].items():
        staged = FIXTURE / relative_key
        fixed = FIXTURE / name
        assert staged.is_file(), f"pointer names missing object: {relative_key}"
        assert fixed.is_file(), f"legacy mirror missing object: {name}"
        assert staged.read_bytes() == fixed.read_bytes(), f"fixed mirror drifted for {name}"
        resolved[name] = staged.read_bytes()

    assert set(resolved) == set(EXPECTED_FILES)
    assert json.loads(resolved["meta.json"]) == _json("meta.json")


def test_manifest_and_parquet_bytes_hold_all_three_schemas():
    manifest = _json("manifest.json")
    expected_schemas = {
        "hourly.parquet": parquet_io.HOURLY_SCHEMA,
        "commits.parquet": parquet_io.COMMITS_SCHEMA,
        "bead_events.parquet": parquet_io.BEAD_EVENTS_SCHEMA,
    }

    for name, schema in expected_schemas.items():
        documented = manifest["parquet_schemas"][name]
        assert [(field, TYPE_MAP[type_name]) for field, type_name in documented] == [
            (field.name, field.type) for field in schema
        ]
        table = _parquet(FIXTURE / f"cycles/{manifest['cycle_id']}/{name}")
        assert table.schema.equals(schema)
        assert table.num_rows > 0


def test_ledger_fixture_pins_documented_join_keys_and_redispatch():
    joins = _json("ledger/joins.json")
    pointer = _json("current.json")
    cycle = FIXTURE / pointer["objects"]["bead_events.parquet"]
    commit_cycle = FIXTURE / pointer["objects"]["commits.parquet"]
    events = _parquet(cycle).to_pylist()
    commits = _parquet(commit_cycle).to_pylist()

    assert joins["schema"] == "git-activity-exporter.ledger-fixture.v1"
    assert joins["rules"]["bead_event_identity"] == [
        "workspace_uuid", "issue_id", "actor"
    ]
    assert joins["rules"]["commit_identity"] == ["repo", "sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", joins["attempts"][0]["expected_commit"]["sha"])

    for attempt in joins["attempts"]:
        start = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(attempt["finished_at"].replace("Z", "+00:00"))
        candidates = [
            row for row in events
            if row["workspace_uuid"] == attempt["workspace_uuid"]
            and row["issue_id"] == attempt["issue_id"]
            and start <= datetime.fromisoformat(row["ts_utc"].replace("Z", "+00:00")) <= end
        ]
        claims = [row for row in candidates if row["kind"] == "claimed"]
        claim = [row for row in claims if row["ts_utc"] == attempt["expected_claim"]]
        close = [row for row in candidates if row["kind"] == "closed"]
        assert len(claim) == 1
        assert len(close) == 1
        assert len(claims) >= 1
        assert claim[0]["actor"] == attempt["expected_worker"]
        assert close[0]["ts_utc"] == attempt["expected_close"]

        expected_commit = attempt["expected_commit"]
        assert any(
            row["repo"] == expected_commit["repo"]
            and row["sha"] == expected_commit["sha"]
            for row in commits
        )

    redispatch = next(item for item in joins["attempts"] if item["attempt_id"] == "attempt-redispatch")
    redispatch_close = next(
        row for row in events
        if row["issue_id"] == redispatch["issue_id"] and row["kind"] == "closed"
    )
    assert redispatch_close["actor"] == "system"
    assert redispatch["expected_worker"] != redispatch_close["actor"]


def _attempt(joins, attempt_id):
    return next(attempt for attempt in joins["attempts"] if attempt["attempt_id"] == attempt_id)


def _window(attempt):
    return (
        datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00")),
        datetime.fromisoformat(attempt["finished_at"].replace("Z", "+00:00")),
    )


def test_ledger_join_probes_pin_exact_keys_and_one_to_one_cardinality():
    joins = _json("ledger/joins.json")
    pointer = _json("current.json")
    commit_path = FIXTURE / pointer["objects"]["commits.parquet"]
    event_path = FIXTURE / pointer["objects"]["bead_events.parquet"]
    commits = _parquet(commit_path)
    events = _parquet(event_path)

    # The join fixture is portable across the exporter and factory-ledger
    # repositories, but it must still be checked against the bytes that this
    # repository publishes. A matching key cannot be useful if a producer
    # silently changes the field name, order, or type.
    assert commits.schema.equals(parquet_io.COMMITS_SCHEMA)
    assert events.schema.equals(parquet_io.BEAD_EVENTS_SCHEMA)
    assert commits.schema.field("repo").type == pa.string()
    assert commits.schema.field("sha").type == pa.string()
    for field in ("workspace_uuid", "issue_id", "kind", "actor", "ts_utc"):
        assert events.schema.field(field).type == pa.string()

    commit_rows = commits.to_pylist()
    event_rows = events.to_pylist()

    for probe in joins["probes"]["commits"]:
        candidates = [
            row for row in commit_rows
            if row["repo"] == probe["repo"] and row["sha"] == probe["sha"]
        ]
        assert len(candidates) == probe["expected_cardinality"], probe["case_id"]
        assert ("matched" if candidates else "unmatched") == probe["expected_status"]

    for probe in joins["probes"]["bead_events"]:
        attempt = _attempt(joins, probe["attempt_id"])
        start, end = _window(attempt)
        workspace = probe.get("workspace_uuid", attempt["workspace_uuid"])
        candidates = [
            row for row in event_rows
            if row["workspace_uuid"] == workspace
            and row["issue_id"] == attempt["issue_id"]
            and row["kind"] == probe["kind"]
            and row["actor"] == probe["actor"]
            and start <= datetime.fromisoformat(row["ts_utc"].replace("Z", "+00:00")) <= end
        ]
        assert len(candidates) == probe["expected_cardinality"], probe["case_id"]
        assert ("matched" if candidates else "unmatched") == probe["expected_status"]

    for probe in joins["probes"]["non_matching_records"]["commits"]:
        attempt = _attempt(joins, probe["attempt_id"])
        source_rows = [
            row for row in commit_rows
            if row["repo"] == probe["repo"] and row["sha"] == probe["sha"]
        ]
        expected_key = (
            attempt["expected_commit"]["repo"],
            attempt["expected_commit"]["sha"],
        )
        assert len(source_rows) == 1, probe["case_id"]
        assert (source_rows[0]["repo"], source_rows[0]["sha"]) != expected_key
        assert probe["expected_cardinality"] == 0
        assert probe["expected_status"] == "unmatched"

    for probe in joins["probes"]["non_matching_records"]["bead_events"]:
        source_rows = [
            row for row in event_rows
            if all(row[field] == probe[field] for field in (
                "workspace_uuid", "issue_id", "kind", "actor", "ts_utc"
            ))
        ]
        assert len(source_rows) == 1, probe["case_id"]
        assert source_rows[0]["kind"] not in {"claimed", "closed"}
        assert probe["expected_cardinality"] == 0
        assert probe["expected_status"] == "unmatched"

    # Assert cardinality for every named attempt as well as the individual
    # probes. This catches accidental fan-out when a second claim or close is
    # added to a fixture, which is the most dangerous kind of join drift.
    for attempt in joins["attempts"]:
        start, end = _window(attempt)
        claims = [
            row for row in event_rows
            if row["workspace_uuid"] == attempt["workspace_uuid"]
            and row["issue_id"] == attempt["issue_id"]
            and row["kind"] == "claimed"
            and row["actor"] == attempt["expected_worker"]
            and start <= datetime.fromisoformat(row["ts_utc"].replace("Z", "+00:00")) <= end
        ]
        closes = [
            row for row in event_rows
            if row["workspace_uuid"] == attempt["workspace_uuid"]
            and row["issue_id"] == attempt["issue_id"]
            and row["kind"] == "closed"
            and start <= datetime.fromisoformat(row["ts_utc"].replace("Z", "+00:00")) <= end
        ]
        commits_for_attempt = [
            row for row in commit_rows
            if row["repo"] == attempt["expected_commit"]["repo"]
            and row["sha"] == attempt["expected_commit"]["sha"]
        ]
        assert len(claims) == 1, attempt["attempt_id"]
        assert len(closes) == 1, attempt["attempt_id"]
        assert len(commits_for_attempt) == 1, attempt["attempt_id"]


def test_ledger_join_missing_source_is_distinct_from_an_empty_match(tmp_path):
    joins = _json("ledger/joins.json")
    pointer = _json("current.json")
    cycle = FIXTURE / f"cycles/{pointer['cycle_id']}"
    copied_cycle = tmp_path / pointer["cycle_id"]
    shutil.copytree(cycle, copied_cycle)

    for probe in joins["probes"]["missing_sources"]:
        missing = copied_cycle / probe["source"]
        missing.unlink()
        assert not missing.exists()
        # Consumers must preserve the reason that no source was available;
        # treating an absent object as an empty table makes a missing export
        # indistinguishable from a legitimate zero-row join.
        status = "source_missing" if not missing.is_file() else "present"
        candidates = [] if status == "source_missing" else [object()]
        assert status == probe["expected_status"]
        assert len(candidates) == probe["expected_cardinality"]
