"""The four published objects at zero data, against docs/notes/output-schema.md.

Most cycles are quiet: roughly a third of the fleet carries no forensic log
at all (measured 2026-08-17), whole repos see no window activity, and an
empty enumeration publishes rather than failing (data-sources.md "Failure
semantics"). Quiet cycles are also where a schema is easiest to lose -- an
empty table still ships every column, and nothing downstream notices a
missing one until a join comes up empty months later.

These tests run zero-data cycles through the real _run_cycle path, read the
landed objects back, and hold each against the documented columns, types,
UTC formatting, nullability and join keys -- the executable counterpart of
the schema doc's Parquet tables, the way tests/test_meta_schema.py holds
meta.json to its field table. The no-forensic-log read itself is pinned next
door, where the forensic fixtures live: tests/test_event_grain.py.
"""
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import main, parquet_io, s3io
from src.parquet_io import BEAD_EVENTS_SCHEMA, COMMITS_SCHEMA, HOURLY_SCHEMA
from tests.fake_s3 import FakeS3
from tests.test_meta_schema import RFC3339_Z, validate_meta

OUTPUT_SCHEMA_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "output-schema.md"
)

BUCKET = "dashboard-site"
PREFIX = "git-activity/data"

#: The three Parquet objects, under the names they are published with.
PARQUET_OBJECTS = [
    ("hourly.parquet", HOURLY_SCHEMA),
    ("commits.parquet", COMMITS_SCHEMA),
    ("bead_events.parquet", BEAD_EVENTS_SCHEMA),
]

#: output-schema.md states Parquet types in this vocabulary.
DOC_ARROW_TYPES = {"string": pa.string(), "int64": pa.int64(), "bool": pa.bool_()}


def _documented_columns(heading):
    """The (column, type) pairs of the schema table under `heading`, in
    documented order. A combined row -- `lines_added` / `lines_deleted` --
    is split into one entry per named column."""
    text = OUTPUT_SCHEMA_MD.read_text()
    _, _, section = text.partition(heading)
    assert section, f"output-schema.md lost the {heading} section"
    section = section.split("\n## ", 1)[0]
    columns = []
    for name_cell, type_cell in re.findall(r"^\|([^|]+)\|([^|]+)\|", section, re.MULTILINE):
        if type_cell.strip() not in DOC_ARROW_TYPES:
            continue  # the header and separator rows
        for name in name_cell.split("/"):
            columns.append((name.strip().strip("`"), type_cell.strip()))
    assert columns, f"{heading} must document its columns in a table"
    return columns


def _cfg():
    return SimpleNamespace(
        version="test",
        window_days=90,
        git_timeout_seconds=600,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=[],
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        max_failure_rate=0.2,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
    )


def _stats(total, scanned, with_beads):
    return {
        "repos_total": total, "repos_scanned": scanned,
        "repos_failed": [], "repo_errors": {},
        "repos_stale": [], "mirrors_pruned": [],
        "repos_with_bead_data": with_beads,
    }


def _publish(monkeypatch, repos, commits, events, stats, family_map=None):
    """Run one cycle against the fake S3 and return the four landed objects.

    ``staged`` is what the pointer names (cycles/<cycle_id>/...), ``fixed``
    the legacy prefix-root keys a static-panel consumer reads."""
    s3 = FakeS3()
    monkeypatch.setattr(main, "_collect", lambda *a: (repos, commits, events, stats))
    main._run_cycle(_cfg(), s3, family_map or {})

    pointer = json.loads(s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json"))
    staged = {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{key}")
        for name, key in pointer["objects"].items()
    }
    fixed = {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}") for name in staged
    }
    return {"pointer": pointer, "staged": staged, "fixed": fixed}


def _read_parquet(data):
    return pq.read_table(io.BytesIO(data))


def _commit(repo, ts, n, bead_id=None):
    return {
        "sha": f"{n:040x}", "repo": repo, "ts": ts,
        "author_email": "dev@example.com", "subject": "do a thing", "bead_id": bead_id,
        "lines_added": 10, "lines_deleted": 4, "files_changed": 2,
        "lines_added_raw": 12, "lines_deleted_raw": 4, "files_changed_raw": 2,
    }


def _event(repo, ts, issue, kind, actor="system", resulting=None):
    """The dict shape beads.parse_events produces, minus bulk flagging -- the
    same keys a real scan yields to _run_cycle."""
    return {
        "repo": repo, "ts": ts,
        "workspace_uuid": "3c213df4-b69d-fca5-1263-5950727cf636",
        "issue_id": issue, "kind": kind, "actor": actor,
        "resulting_status": resulting,
    }


def _ts(hour, minute=0):
    return int(datetime(2026, 9, 15, hour, minute, tzinfo=timezone.utc).timestamp())


HOUR_18 = "2026-09-15T18:00:00Z"
HOUR_19 = "2026-09-15T19:00:00Z"


def _epoch_of(hour_utc):
    """The numeric form output-schema.md promises: hour_epoch is the hour of
    hour_utc."""
    t = datetime.strptime(hour_utc, "%Y-%m-%dT%H:00:00Z")
    return int(t.replace(tzinfo=timezone.utc).timestamp()) // 3600


# --- the documented tables vs the Arrow schemas ------------------------------


@pytest.mark.parametrize(
    "heading,schema",
    [(f"## `{name}`", schema) for name, schema in PARQUET_OBJECTS],
    ids=[name for name, _ in PARQUET_OBJECTS],
)
def test_documented_columns_and_types_match_the_arrow_schema(heading, schema):
    # output-schema.md is what the panel and the attempt ledger were built
    # against; the Parquet schemas are what actually ships. Neither side may
    # add, drop, reorder or retype a column without failing here.
    documented = _documented_columns(heading)
    assert [name for name, _ in documented] == list(schema.names)
    for name, doc_type in documented:
        assert schema.field(name).type == DOC_ARROW_TYPES[doc_type], (
            f"{name}: documented as {doc_type}, written as {schema.field(name).type}"
        )


@pytest.mark.parametrize(
    "schema", [schema for _, schema in PARQUET_OBJECTS],
    ids=[name for name, _ in PARQUET_OBJECTS],
)
def test_zero_row_payloads_keep_the_full_documented_schema(schema):
    # The empty fleet is the case that ships every schema: zero rows, every
    # column. A serialization change that dropped or retyped columns on empty
    # input would hand consumers a file that renders as silently blank.
    table = _read_parquet(parquet_io.table_to_parquet_bytes([], schema))
    assert table.num_rows == 0
    assert table.schema.equals(schema)


# --- the empty fleet ---------------------------------------------------------


def test_an_empty_fleet_still_publishes_all_four_typed_objects(monkeypatch):
    # An empty enumeration publishes (data-sources.md "Failure semantics"):
    # the pointer commits a cycle whose Parquet files are zero-row but
    # column-complete, and meta.json describes the emptiness rather than
    # going missing.
    landed = _publish(monkeypatch, [], [], [], _stats(0, 0, 0))

    assert set(landed["pointer"]["objects"]) == {
        "hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"}

    for name, schema in PARQUET_OBJECTS:
        assert landed["staged"][name] == landed["fixed"][name], \
            f"the {name} mirror must serve the staged bytes"
        table = _read_parquet(landed["staged"][name])
        assert table.num_rows == 0
        assert table.schema.equals(schema), f"{name} lost its documented schema"

    meta = json.loads(landed["staged"]["meta.json"])
    assert landed["staged"]["meta.json"] == landed["fixed"]["meta.json"], \
        "the completion marker must mirror the staged bytes"
    assert validate_meta(meta) == []
    assert meta["repos_total"] == 0
    assert meta["repos_scanned"] == 0
    assert meta["repos_with_bead_data"] == 0
    assert meta["bead_epoch_utc"] is None, "no bead rows means a null epoch, not a guess"
    assert meta["unassigned_repos"] == []


# --- repos with no activity, and repos without forensic logs -----------------


def test_quiet_repos_publish_empty_tables_and_stay_out_of_unassigned(monkeypatch):
    # Both repos scan fine; one has no window commits and no forensic log,
    # the other no activity at all. Nothing about either is a failure, so
    # coverage still counts them as scanned -- and neither may appear in
    # unassigned_repos, which names unmapped repos *with window activity*.
    repos = [{"name": "no-forensic-log"}, {"name": "no-activity"}]
    landed = _publish(monkeypatch, repos, [], [], _stats(2, 2, 0))

    for name, schema in PARQUET_OBJECTS:
        table = _read_parquet(landed["staged"][name])
        assert table.num_rows == 0
        assert table.schema.equals(schema), f"{name} lost its documented schema"

    meta = json.loads(landed["staged"]["meta.json"])
    assert validate_meta(meta) == []
    assert meta["repos_total"] == 2
    assert meta["repos_scanned"] == 2
    assert meta["repos_failed"] == []
    assert meta["repos_with_bead_data"] == 0
    assert meta["bead_epoch_utc"] is None
    assert meta["unassigned_repos"] == []


# --- join keys, nullability and UTC on a sparse cycle ------------------------


def test_worker_partitions_and_epoch_round_trip(monkeypatch):
    events = [
        _event("worker-repo", _ts(17, 1), "gitact-old", "closed", actor="system"),
        _event("worker-repo", _ts(17, 2), "gitact-old", "claimed", actor="old-worker"),
        _event("worker-repo", _ts(18, 1), "gitact-new", "closed", actor="new-worker"),
        _event("worker-repo", _ts(18, 2), "gitact-new", "closed", actor="system"),
    ]
    landed = _publish(
        monkeypatch,
        [{"name": "worker-repo"}],
        [],
        events,
        _stats(1, 1, 1),
    )

    hourly = _read_parquet(landed["staged"]["hourly.parquet"]).to_pylist()
    meta = json.loads(landed["staged"]["meta.json"])
    base = [row for row in hourly if row["worker"] is None]
    named = [row for row in hourly if row["worker"] == "new-worker"]
    inferential = [row for row in hourly if row["worker"] == "inferential"]

    assert len(base) == 2
    assert len(named) == 1
    assert named[0]["beads_closed"] == 1
    assert {row["hour_utc"] for row in inferential} == {
        "2026-09-15T17:00:00Z", "2026-09-15T18:00:00Z"}
    assert not any(row["worker"] == "old-worker" for row in hourly)
    assert meta["attribution_epoch"] == {
        "worker-repo": "2026-09-15T18:01:00Z",
    }
    assert validate_meta(meta) == []


def test_join_keys_nullability_and_utc_hold_on_a_sparse_cycle(monkeypatch):
    # One repo with commits, one with bead events, neither at full density:
    # the properties the attempt ledger and the panel rely on must hold on
    # exactly the sparse shapes a quiet night produces.
    family_map = {"ledger-repo": "ship-fleet"}
    commits = [
        _commit("ledger-repo", _ts(18, 4), 1, bead_id="gitact-70240395"),
        _commit("ledger-repo", _ts(18, 59), 2),
    ]
    events = [
        _event("bead-repo", _ts(18, 10), "gitact-1a2b3c4d", "claimed",
               actor="worker-1", resulting="in_progress"),
        # resulting_status is what beads.parse_events derives before rows are
        # built: stated by the claim's detail, taken from the kind on a close,
        # null when no status moves (tests/test_event_grain.py pins that
        # derivation; this pins that it survives serialization).
        _event("bead-repo", _ts(18, 40), "gitact-1a2b3c4d", "closed", resulting="closed"),
        # Hour 19 has only kinds the rollup never counted: it must appear in
        # bead_events.parquet while minting no hourly.parquet cell.
        _event("bead-repo", _ts(19, 5), "gitact-1a2b3c4d", "updated"),
        # A workspace-level event names no bead: issue_id is null, exactly as
        # documented, and nothing else may be.
        _event("bead-repo", _ts(19, 15), None, "workspace_created"),
    ]
    repos = [{"name": "ledger-repo"}, {"name": "bead-repo"}]
    landed = _publish(monkeypatch, repos, commits, events, _stats(2, 2, 1), family_map)

    hourly = _read_parquet(landed["staged"]["hourly.parquet"]).to_pylist()
    commit_rows = _read_parquet(landed["staged"]["commits.parquet"]).to_pylist()
    event_rows = _read_parquet(landed["staged"]["bead_events.parquet"]).to_pylist()
    meta = json.loads(landed["staged"]["meta.json"])

    # UTC formatting: every timestamp carries an explicit Z, every hour
    # bucket is hour-grained, and hour_epoch is the numeric form of hour_utc.
    cells = {(row["repo"], row["hour_utc"]): row for row in hourly if row["worker"] is None}
    for row in hourly:
        assert RFC3339_Z.match(row["hour_utc"])
        assert row["hour_utc"].endswith(":00:00Z")
        assert row["hour_epoch"] == _epoch_of(row["hour_utc"])
    for row in commit_rows:
        assert RFC3339_Z.match(row["ts_utc"])
        assert RFC3339_Z.match(row["hour_utc"]) and row["hour_utc"].endswith(":00:00Z")
    for row in event_rows:
        assert RFC3339_Z.match(row["ts_utc"])
        assert RFC3339_Z.match(row["hour_utc"]) and row["hour_utc"].endswith(":00:00Z")

    # The joins: commits always mint their cell, counted events always land
    # in one, and hour 19 exists only at event grain -- the kinds the rollup
    # never counted publish rows without minting a cell.
    assert set(cells) == {("ledger-repo", HOUR_18), ("bead-repo", HOUR_18)}
    assert cells[("ledger-repo", HOUR_18)]["commits"] == len(commit_rows)
    closed_rows = [r for r in event_rows if r["kind"] == "closed"]
    assert sum(r["beads_closed"] for r in hourly if r["worker"] is None) \
        + sum(r["beads_closed_bulk"] for r in hourly if r["worker"] is None) == len(closed_rows)
    assert ("bead-repo", HOUR_19) not in cells
    assert {r["kind"] for r in event_rows if r["hour_utc"] == HOUR_19} \
        == {"updated", "workspace_created"}

    # Family is a property of the repo, identical in every file that carries
    # the repo, so a family or ecosystem sum cannot depend on which file it
    # came from.
    by_repo_hourly = {r["repo"]: r["family"] for r in hourly}
    assert by_repo_hourly["ledger-repo"] == "ship-fleet"
    assert by_repo_hourly["bead-repo"] == "unassigned"
    for row in commit_rows:
        assert row["family"] == by_repo_hourly[row["repo"]]
    for row in event_rows:
        assert row["family"] == by_repo_hourly[row["repo"]]

    # Commit nullability: only bead_id may be null, and only when the commit
    # references no bead. sha stays a full 40-char SHA -- the CI join key.
    assert {r["bead_id"] for r in commit_rows} == {"gitact-70240395", None}
    for row in commit_rows:
        assert re.fullmatch(r"[0-9a-f]{40}", row["sha"])
        for column, value in row.items():
            if column != "bead_id":
                assert value is not None, f"commits.{column} must never be null"

    # Event nullability: workspace_uuid is always present (the join scope),
    # issue_id exactly on the workspace-level event, resulting_status only
    # where the event moves a status.
    assert all(r["workspace_uuid"] for r in event_rows)
    by_kind = {r["kind"]: r for r in event_rows}
    assert by_kind["workspace_created"]["issue_id"] is None
    assert all(r["issue_id"] for r in event_rows if r["kind"] != "workspace_created")
    assert by_kind["claimed"]["resulting_status"] == "in_progress"
    assert by_kind["closed"]["resulting_status"] == "closed"
    assert by_kind["updated"]["resulting_status"] is None
    assert by_kind["workspace_created"]["resulting_status"] is None
    assert all(r["is_bulk_import"] is False for r in event_rows)

    # meta.json agrees with what landed: the epoch is this cycle's earliest
    # event, and only the repo with activity can be unassigned.
    assert validate_meta(meta) == []
    assert meta["bead_epoch_utc"] == "2026-09-15T18:10:00Z"
    assert meta["attribution_epoch"] == {}
    assert meta["repos_with_bead_data"] == 1
    assert meta["unassigned_repos"] == ["bead-repo"]
