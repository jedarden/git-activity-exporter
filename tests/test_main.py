import json
from types import SimpleNamespace

import pytest

from src import gitscan, main, parquet_io, s3io
from tests.fake_s3 import FakeS3


def test_collect_keeps_going_and_records_failed_and_stale_repos(monkeypatch, tmp_path):
    repos = [
        {"name": "timeout", "clone_url": "https://forge/timeout.git"},
        {"name": "slow", "clone_url": "https://forge/slow.git"},
    ]
    cfg = SimpleNamespace(
        forge_base_url="https://forge",
        forge_token="token",
        forge_owner="owner",
        http_timeout_seconds=30,
        repo_denylist=[],
        clone_root=str(tmp_path),
        shallow_since_days=100,
        window_days=90,
        excluded_path_patterns=[],
        git_timeout_seconds=17,
    )

    monkeypatch.setattr(main.forge, "list_repos", lambda *args: repos)
    monkeypatch.setattr(main.gitscan, "prune_orphans", lambda *args: ["old-name"])

    def fake_ensure(repo, *args):
        if repo["name"] == "timeout":
            raise gitscan.GitTimeout("git clone timed out after 17s")
        return "/data/mirrors/slow.git", False

    monkeypatch.setattr(main.gitscan, "ensure_mirror", fake_ensure)
    monkeypatch.setattr(main.gitscan, "scan_commits", lambda *args: [])
    monkeypatch.setattr(main.beads, "read_events", lambda *args: [])

    found, commits, events, stats = main._collect(cfg, {})

    assert found == repos
    assert commits == []
    assert events == []
    assert stats == {
        "repos_total": 2,
        "repos_scanned": 1,
        "repos_failed": ["timeout"],
        "repo_errors": {"timeout": "git clone timed out after 17s"},
        "repos_stale": ["slow"],
        "mirrors_pruned": ["old-name"],
        "repos_with_bead_data": 0,
    }


def _cycle_cfg(dest_prefix="git-activity/data"):
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
        dest=SimpleNamespace(bucket="dashboard-site"),
        dest_prefix=dest_prefix,
    )


def _cycle_stats():
    return {
        "repos_total": 1,
        "repos_scanned": 1,
        "repos_failed": [],
        "repo_errors": {},
        "repos_stale": [],
        "mirrors_pruned": [],
        "repos_with_bead_data": 0,
        "bulk_bead_cells": 0,
    }


class RecordingS3(FakeS3):
    """FakeS3 is enough for _run_cycle; the subclass name keeps the tests'
    intent readable (record puts, read the pointer back)."""


def _stub_collect(monkeypatch, commits=None, events=None):
    monkeypatch.setattr(
        main, "_collect",
        lambda cfg, family_map: ([], commits or [], events or [], _cycle_stats()),
    )


def test_generation_failure_uploads_nothing(monkeypatch):
    """A Parquet generation failure must leave S3 exactly as it was -- the
    pre-protocol loop had already overwritten hourly.parquet by the time the
    second table's serialization could fail."""
    cfg = _cycle_cfg()
    s3 = RecordingS3()
    _stub_collect(monkeypatch)

    calls = {"n": 0}
    real_serialize = parquet_io.table_to_parquet_bytes

    def boom_on_second_table(rows, schema):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("parquet serialization failed")
        return real_serialize(rows, schema)

    monkeypatch.setattr(main.parquet_io, "table_to_parquet_bytes", boom_on_second_table)

    with pytest.raises(RuntimeError, match="serialization"):
        main._run_cycle(cfg, s3, {})

    assert s3.puts == [], "a generation failure must not upload anything"
    assert s3.objects == {}


def test_run_cycle_publishes_one_cycle_through_the_pointer(monkeypatch):
    cfg = _cycle_cfg()
    s3 = RecordingS3()
    _stub_collect(monkeypatch)

    main._run_cycle(cfg, s3, {})

    assert len(s3.puts) == 9, "4 staged + 4 fixed-key mirrors + 1 pointer"
    assert s3.puts[-1] == "git-activity/data/current.json", "the commit is the last write"

    ptr = json.loads(s3io.download_bytes(s3, "dashboard-site", "git-activity/data/current.json"))
    meta_key = ptr["objects"]["meta.json"]
    meta = json.loads(s3io.download_bytes(s3, "dashboard-site", f"git-activity/data/{meta_key}"))
    assert meta["cycle_id"] == ptr["cycle_id"]
    staged = {name: key for name, key in ptr["objects"].items()}
    assert set(staged) == {"hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"}


def test_publish_failure_marks_the_cycle_failed(monkeypatch):
    """A publication failure must surface as a cycle failure so main()'s
    retry loop treats the cycle as not-published, not as success."""
    cfg = _cycle_cfg()
    s3 = RecordingS3()
    _stub_collect(monkeypatch)
    monkeypatch.setattr(
        main.publish, "publish_cycle",
        lambda *a, **kw: (_ for _ in ()).throw(main.publish.PublicationError("injected")),
    )

    with pytest.raises(main.publish.PublicationError):
        main._run_cycle(cfg, s3, {})
