import io
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from src import forge, gitscan, main, s3io
from src.config import DEFAULT_EXCLUDED_PATHS
from tests.fake_s3 import FakeS3


GENERATED_AT = "2026-09-23T12:00:00Z"
BUCKET = "fixture-bucket"
PREFIX = "fixture/git-activity"


def _git_env(date=None):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    })
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return env


def _git(path, *args, date=None):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(date),
    ).stdout.strip()


def _init_repo(path):
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )


def _write(path, relative, content):
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _commit(path, date, subject):
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--no-verify", "-m", subject, date=date)


def _mark_clone_root(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / gitscan.CLONE_ROOT_MARKER).write_text(
        gitscan.CLONE_ROOT_MARKER_CONTENT
    )


def _event(sequence, issue_id, kind, timestamp, actor, detail):
    return json.dumps({
        "record_type": "event",
        "event": {
            "origin_store_uuid": "fixture-workspace",
            "origin_event_sequence": sequence,
            "issue_id": issue_id,
            "kind": kind,
            "actor": actor,
            "time": timestamp,
            "detail": detail,
        },
    })


def _make_source(path, date, subject, files):
    _init_repo(path)
    for relative, content in files.items():
        _write(path, relative, content)
    _commit(path, date, subject)
    return path


def _make_cycle_fixture(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    sources = {
        "bead-repo": _make_source(
            source_root / "bead-repo",
            "2026-09-23T09:00:00+00:00",
            "feat(gitact-0abc123): add bead fixture",
            {
                "src/app.py": "print('bead')\n",
                ".beads/checkpoint/forensic.jsonl": "\n".join([
                    _event(
                        1, "gitact-0abc123", "claimed", "2026-09-23T09:05:00Z",
                        "fixture-worker", {"resulting_base_status": "in_progress"},
                    ),
                    _event(
                        2, "gitact-0abc123", "closed", "2026-09-23T09:10:00Z",
                        "system", {"prior_base_status": "in_progress", "reason": "done"},
                    ),
                ]) + "\n",
            },
        ),
        "family-repo": _make_source(
            source_root / "family-repo",
            "2026-09-23T10:00:00+00:00",
            "chore: add family fixture",
            {"README.md": "family fixture\n"},
        ),
        "quiet-repo": _make_source(
            source_root / "quiet-repo",
            "2026-09-23T11:00:00+00:00",
            "chore: add quiet fixture",
            {"README.md": "quiet fixture\n"},
        ),
    }
    missing = tmp_path / "missing-repo.git"
    records = [
        {
            "name": name,
            "full_name": f"fixture/{name}",
            "clone_url": path.as_uri(),
            "empty": False,
        }
        for name, path in sources.items()
    ]
    records.append({
        "name": "broken-repo",
        "full_name": "fixture/broken-repo",
        "clone_url": missing.as_uri(),
        "empty": False,
    })
    forge_calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            params = dict(params or {})
            forge_calls.append({
                "url": url,
                "params": params,
                "timeout": timeout,
                "authorization": self.headers.get("Authorization"),
            })
            if params.get("page") == 1:
                return Response({"data": records})
            return Response({"data": []})

    monkeypatch.setattr(forge.requests, "Session", Session)
    # These fixtures clone local file:// repositories. Production Forgejo
    # enumeration and the clone boundary reject local URLs by policy; the
    # policy has dedicated tests, while this fixture focuses on cycle I/O.
    monkeypatch.setattr(forge, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main.gitscan, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    clone_root = tmp_path / "mirrors"
    _mark_clone_root(clone_root)
    cfg = SimpleNamespace(
        forge_base_url="https://forge.fixture",
        forge_token="fixture-token",
        forge_owner="fixture-owner",
        repo_denylist=[],
        clone_root=str(clone_root),
        window_days=30,
        shallow_since_days=30,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=list(DEFAULT_EXCLUDED_PATHS),
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        families_file="families.yaml",
        max_failure_rate=0.25,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
        version="fixture",
        git_timeout_seconds=60,
        http_timeout_seconds=30,
    )

    real_ensure = main.gitscan.ensure_mirror
    mirror_calls = []

    def tracked_ensure(repo, *args):
        try:
            path, refreshed = real_ensure(repo, *args)
        except Exception:
            mirror_calls.append((repo["name"], None, None))
            raise
        mirror_calls.append((repo["name"], os.stat(path).st_ino, refreshed))
        return path, refreshed

    monkeypatch.setattr(main.gitscan, "ensure_mirror", tracked_ensure)

    return SimpleNamespace(
        cfg=cfg,
        sources=sources,
        clone_root=clone_root,
        forge_calls=forge_calls,
        mirror_calls=mirror_calls,
        family_map={
            "bead-repo": "family-a",
            "family-repo": "family-b",
        },
    )


@pytest.fixture
def local_cycle(tmp_path, monkeypatch):
    return _make_cycle_fixture(tmp_path, monkeypatch)


@pytest.fixture
def clean_cycle_health():
    main._reset_cycle_state()
    yield
    main._reset_cycle_state()


def _read_cycle(s3):
    pointer_bytes = s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json")
    assert pointer_bytes is not None
    pointer = json.loads(pointer_bytes)
    staged = {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{key}")
        for name, key in pointer["objects"].items()
    }
    fixed = {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}")
        for name in pointer["objects"]
    }
    assert all(data is not None for data in staged.values())
    assert all(data is not None for data in fixed.values())
    return pointer, staged, fixed


def _rows(data):
    return pq.read_table(io.BytesIO(data)).to_pylist()


def test_local_fixture_cycle_reuses_mirrors_and_publishes_coverage(local_cycle):
    s3 = FakeS3()
    main._run_cycle(local_cycle.cfg, s3, local_cycle.family_map)

    assert len(local_cycle.forge_calls) == 1
    assert local_cycle.forge_calls[0] == {
        "url": "https://forge.fixture/api/v1/repos/search",
        "params": {"limit": 50, "page": 1, "owner": "fixture-owner"},
        "timeout": 30,
        "authorization": "token fixture-token",
    }

    first_pointer, first_staged, first_fixed = _read_cycle(s3)
    assert first_pointer["generated_at"] == GENERATED_AT
    assert set(first_pointer["objects"]) == {
        "hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"
    }
    assert first_staged == first_fixed
    assert s3.puts[-1] == f"{PREFIX}/current.json"
    assert len(s3.puts) == 9
    assert s3.puts[-2] == f"{PREFIX}/meta.json"

    first_inodes = {
        name: inode
        for name, inode, refreshed in local_cycle.mirror_calls
        if inode is not None
    }
    assert set(first_inodes) == {"bead-repo", "family-repo", "quiet-repo"}
    assert all(refreshed for _, _, refreshed in local_cycle.mirror_calls if refreshed is not None)
    assert (local_cycle.clone_root / "bead-repo.git").is_dir()
    assert (local_cycle.clone_root / "family-repo.git").is_dir()
    assert (local_cycle.clone_root / "quiet-repo.git").is_dir()
    assert not (local_cycle.clone_root / "broken-repo.git").exists()
    assert not (local_cycle.clone_root / "broken-repo.git.tmp").exists()

    meta = json.loads(first_staged["meta.json"])
    assert meta["generated_at"] == GENERATED_AT
    assert meta["repos_total"] == 4
    assert meta["repos_scanned"] == 3
    assert meta["repos_failed"] == ["broken-repo"]
    assert set(meta["repo_errors"]) == {"broken-repo"}
    assert meta["repo_errors"]["broken-repo"]
    assert meta["repos_partial_history"] == []
    assert meta["repos_with_bead_data"] == 1
    assert meta["unassigned_repos"] == ["quiet-repo"]
    assert meta["bead_epoch_utc"] == "2026-09-23T09:05:00Z"
    assert meta["attribution_epoch"] == {}
    assert meta["bulk_bead_cells"] == 0

    commits = _rows(first_staged["commits.parquet"])
    hourly = _rows(first_staged["hourly.parquet"])
    events = _rows(first_staged["bead_events.parquet"])
    assert {row["repo"] for row in commits} == {
        "bead-repo", "family-repo", "quiet-repo"
    }
    assert {row["repo"] for row in hourly} == {
        "bead-repo", "family-repo", "quiet-repo"
    }
    assert len(events) == 2
    assert {row["repo"] for row in events} == {"bead-repo"}
    assert {(row["repo"], row["family"]) for row in commits} == {
        ("bead-repo", "family-a"),
        ("family-repo", "family-b"),
        ("quiet-repo", "unassigned"),
    }
    assert {row["kind"] for row in events} == {"claimed", "closed"}
    closed = next(row for row in events if row["kind"] == "closed")
    assert closed["resulting_status"] == "closed"
    assert closed["workspace_uuid"] == "fixture-workspace"
    bead_hour = next(row for row in hourly if row["repo"] == "bead-repo")
    assert bead_hour["beads_claimed"] == 1
    assert bead_hour["beads_closed"] == 1
    assert bead_hour["workers_active"] == 1
    assert bead_hour["beads_closed_bulk"] == 0

    bead_source = local_cycle.sources["bead-repo"]
    _write(bead_source, "src/second.py", "print('second')\n")
    _commit(
        bead_source,
        "2026-09-23T11:30:00+00:00",
        "fix(gitact-0abc124): add second fixture",
    )
    calls_before_second_cycle = len(local_cycle.mirror_calls)
    main._run_cycle(local_cycle.cfg, s3, local_cycle.family_map)

    second_pointer, second_staged, second_fixed = _read_cycle(s3)
    assert second_pointer["cycle_id"] != first_pointer["cycle_id"]
    assert second_staged == second_fixed
    second_inodes = {
        name: inode
        for name, inode, refreshed in local_cycle.mirror_calls[calls_before_second_cycle:]
        if inode is not None
    }
    assert second_inodes == first_inodes
    second_commits = _rows(second_staged["commits.parquet"])
    assert sum(row["repo"] == "bead-repo" for row in second_commits) == 2
    assert len(second_commits) == 4

    previous_pointer = s3.objects[f"{PREFIX}/current.json"][0]
    previous_puts = list(s3.puts)
    local_cycle.cfg.max_failure_rate = 0.2
    with pytest.raises(RuntimeError, match=r"1/4 repo\(s\) failed"):
        main._run_cycle(local_cycle.cfg, s3, local_cycle.family_map)

    assert s3.objects[f"{PREFIX}/current.json"][0] == previous_pointer
    assert s3.puts == previous_puts


@pytest.mark.parametrize(
    ("deepening_recovers", "expected_partial_history", "expected_subjects"),
    [
        pytest.param(
            False,
            ["shallow-repo"],
            {"shallow boundary", "newest commit"},
            id="unrecoverable-boundary-is-reported",
        ),
        pytest.param(
            True,
            [],
            {"in-window parent", "shallow boundary", "newest commit"},
            id="recovered-boundary-is-complete",
        ),
    ],
)
def test_shallow_boundary_coverage_is_published_in_metadata_and_health(
    tmp_path, monkeypatch, clean_cycle_health, deepening_recovers,
    expected_partial_history, expected_subjects,
):
    # The reporting window is [2026-08-24T12Z, 2026-09-23T12Z). Its
    # 30-day bounded-history cutoff is 2026-07-25, so the Aug 26 shallow
    # root hides an Aug 25 parent that should appear in this report.
    source = tmp_path / "source"
    _init_repo(source)
    _write(source, "history.txt", "before shallow boundary\n")
    _commit(source, "2026-08-25T09:00:00+00:00", "in-window parent")
    _write(source, "history.txt", "at shallow boundary\n")
    _commit(source, "2026-08-26T10:00:00+00:00", "shallow boundary")
    _write(source, "history.txt", "newest history\n")
    _commit(source, "2026-09-20T10:00:00+00:00", "newest commit")

    clone_root = tmp_path / "mirrors"
    _mark_clone_root(clone_root)
    mirror = clone_root / "shallow-repo.git"
    subprocess.run(
        [
            "git", "clone", "-q", "--mirror", "--shallow-since=2026-08-26",
            source.as_uri(), str(mirror),
        ],
        check=True,
        capture_output=True,
    )
    shallow_boundaries = (mirror / "shallow").read_text().splitlines()
    assert len(shallow_boundaries) == 1
    boundary_sha = shallow_boundaries[0]
    assert _git(mirror, "show", "-s", "--format=%s", boundary_sha) == "shallow boundary"

    repo = {
        "name": "shallow-repo",
        "full_name": "fixture/shallow-repo",
        "clone_url": source.as_uri(),
        "empty": False,
    }
    monkeypatch.setattr(main.forge, "list_repos", lambda *_args: [repo])
    monkeypatch.setattr(main.gitscan, "validate_clone_url", lambda *_args: None)
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    remote_calls = []

    def history_cannot_be_deepened(args, _timeout, cwd=None, env=None, before_attempt=None):
        remote_calls.append(args)

    if not deepening_recovers:
        monkeypatch.setattr(main.gitscan, "_run_remote", history_cannot_be_deepened)
    cfg = SimpleNamespace(
        forge_base_url="https://forge.fixture",
        forge_token="fixture-token",
        forge_owner="fixture-owner",
        http_timeout_seconds=30,
        repo_denylist=[],
        clone_root=str(clone_root),
        window_days=30,
        shallow_since_days=30,
        excluded_path_patterns=[],
        git_timeout_seconds=60,
        trim_max_lines=5000,
        trim_max_files=200,
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        max_failure_rate=0.2,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
        version="fixture",
    )
    s3 = FakeS3()

    generated_at, partial_history = main._run_cycle(cfg, s3, {"shallow-repo": "fixture"})
    main._record_cycle_outcome("published", generated_at, partial_history)

    deepens = [call for call in remote_calls if any(arg.startswith("--deepen=") for arg in call)]
    if not deepening_recovers:
        assert len(deepens) == gitscan._DEEPEN_MAX_ATTEMPTS
    assert main.gitscan.mirror_history_complete(
        str(mirror), main._reporting_window(GENERATED_AT, cfg.window_days).start,
        cfg.shallow_since_days, cfg.git_timeout_seconds,
    ) is deepening_recovers

    pointer, staged, _fixed = _read_cycle(s3)
    assert pointer["generated_at"] == GENERATED_AT
    meta = json.loads(staged["meta.json"])
    assert meta["repos_total"] == 1
    assert meta["repos_scanned"] == 1
    assert meta["repos_failed"] == []
    assert meta["repos_partial_history"] == expected_partial_history
    assert {row["subject"] for row in _rows(staged["commits.parquet"])} == expected_subjects
    assert generated_at == GENERATED_AT
    assert partial_history == expected_partial_history
    assert main._health_snapshot()["last_successful_repos_partial_history"] == expected_partial_history


def _lifecycle_repo(name, source, empty=False):
    return {
        "name": name,
        "full_name": f"fixture/{name}",
        "clone_url": source.as_uri(),
        "empty": empty,
    }


def _make_recovery_fixture(tmp_path, monkeypatch):
    source_root = tmp_path / "recovery-sources"
    source_root.mkdir()
    sources = {
        name: _make_source(
            source_root / name,
            "2026-09-23T09:00:00+00:00",
            f"feat: add {name}",
            {"README.md": f"{name} fixture\n"},
        )
        for name in ["stable-repo", "recover-repo"]
    }
    records = [_lifecycle_repo(name, source) for name, source in sources.items()]
    forge_calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            params = dict(params or {})
            forge_calls.append({
                "url": url,
                "params": params,
                "timeout": timeout,
                "authorization": self.headers.get("Authorization"),
            })
            return Response({"data": records})

    monkeypatch.setattr(forge.requests, "Session", Session)
    # These fixtures clone local file:// repositories. Production Forgejo
    # enumeration and the clone boundary reject local URLs by policy; the
    # policy has dedicated tests, while this fixture focuses on recovery I/O.
    monkeypatch.setattr(forge, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main.gitscan, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    clone_root = tmp_path / "recovery-mirrors"
    _mark_clone_root(clone_root)
    cfg = SimpleNamespace(
        forge_base_url="https://forge.fixture",
        forge_token="fixture-token",
        forge_owner="fixture-owner",
        repo_denylist=[],
        clone_root=str(clone_root),
        window_days=30,
        shallow_since_days=30,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=list(DEFAULT_EXCLUDED_PATHS),
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        families_file="families.yaml",
        # One failed cold clone out of this two-repo fixture must withhold.
        max_failure_rate=0.2,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
        version="fixture",
        git_timeout_seconds=60,
        http_timeout_seconds=30,
    )
    return SimpleNamespace(
        cfg=cfg,
        clone_root=clone_root,
        sources=sources,
        forge_calls=forge_calls,
        family_map={
            "stable-repo": "family-a",
            "recover-repo": "family-b",
        },
    )


def _full_page_with(*records):
    filler_source = records[0]["clone_url"]
    filler = [
        {
            **records[0],
            "name": f"empty-filler-{index}",
            "full_name": f"fixture/empty-filler-{index}",
            "clone_url": filler_source,
            "empty": True,
        }
        for index in range(forge.PAGE_SIZE - len(records))
    ]
    return {"data": [record for record in records] + filler}


def test_mirror_lifecycle_prunes_orphans_and_reclones_an_emptied_repo(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    sources = {
        name: _make_source(
            source_root / name,
            "2026-09-23T09:00:00+00:00",
            f"feat: add {name}",
            {"README.md": f"{name} fixture\n"},
        )
        for name in [
            "stable-repo",
            "deleted-repo",
            "renamed-repo",
            "denylisted-repo",
            "emptied-repo",
            "late-page-repo",
        ]
    }
    # The renamed repo keeps its source URL, but its Forgejo name changes.
    sources["renamed-as-repo"] = sources["renamed-repo"]
    records = {
        name: _lifecycle_repo(name, source)
        for name, source in sources.items()
    }

    # Every first page is full, so page 2 is required to discover the live
    # repo. The filler records are empty and therefore never cloned.
    first_pages = [
        _full_page_with(
            records["stable-repo"],
            records["deleted-repo"],
            records["renamed-repo"],
            records["denylisted-repo"],
            records["emptied-repo"],
        ),
        {"data": [records["late-page-repo"]]},
    ]
    second_pages = [
        _full_page_with(
            records["stable-repo"],
            _lifecycle_repo(
                "denylisted-repo", sources["denylisted-repo"], empty=False
            ),
            _lifecycle_repo("emptied-repo", sources["emptied-repo"], empty=True),
        ),
        {
            "data": [
                records["late-page-repo"],
                records["renamed-as-repo"],
            ]
        },
    ]
    third_pages = [
        _full_page_with(records["stable-repo"]),
        {
            "data": [
                records["late-page-repo"],
                records["renamed-as-repo"],
                records["emptied-repo"],
            ]
        },
    ]
    page_sets = [first_pages, second_pages, third_pages]
    forge_calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        sessions_created = 0

        def __init__(self):
            self.headers = {}
            self.pages = page_sets[Session.sessions_created]
            Session.sessions_created += 1

        def get(self, url, params=None, timeout=None):
            params = dict(params or {})
            forge_calls.append({
                "url": url,
                "params": params,
                "timeout": timeout,
                "authorization": self.headers.get("Authorization"),
            })
            page = params["page"]
            payload = self.pages[page - 1] if page <= len(self.pages) else {"data": []}
            return Response(payload)

    monkeypatch.setattr(forge.requests, "Session", Session)
    # See the equivalent local-fixture note above: this lifecycle fixture
    # intentionally uses file:// clone sources.
    monkeypatch.setattr(forge, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main.gitscan, "validate_clone_url", lambda *args: None)
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    clone_root = tmp_path / "mirrors"
    _mark_clone_root(clone_root)
    cfg = SimpleNamespace(
        forge_base_url="https://forge.fixture",
        forge_token="fixture-token",
        forge_owner="fixture-owner",
        repo_denylist=[],
        clone_root=str(clone_root),
        window_days=30,
        shallow_since_days=30,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=list(DEFAULT_EXCLUDED_PATHS),
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        families_file="families.yaml",
        max_failure_rate=0.25,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
        version="fixture",
        git_timeout_seconds=60,
        http_timeout_seconds=30,
    )

    real_ensure = main.gitscan.ensure_mirror
    mirror_calls = []

    def tracked_ensure(repo, *args):
        path = main.gitscan.mirror_path(args[0], repo["name"])
        existed_before = os.path.exists(path)
        path, refreshed = real_ensure(repo, *args)
        mirror_calls.append((repo["name"], existed_before, refreshed))
        return path, refreshed

    monkeypatch.setattr(main.gitscan, "ensure_mirror", tracked_ensure)

    s3 = FakeS3()
    main._run_cycle(cfg, s3, {})

    first_pointer, first_staged, _ = _read_cycle(s3)
    first_meta = json.loads(first_staged["meta.json"])
    assert first_meta["repos_total"] == 6
    assert first_meta["repos_scanned"] == 6
    assert {row["repo"] for row in _rows(first_staged["commits.parquet"])} == {
        "stable-repo",
        "deleted-repo",
        "renamed-repo",
        "denylisted-repo",
        "emptied-repo",
        "late-page-repo",
    }
    assert {path.name for path in clone_root.iterdir()} == {
        gitscan.CLONE_ROOT_MARKER,
        "stable-repo.git",
        "deleted-repo.git",
        "renamed-repo.git",
        "denylisted-repo.git",
        "emptied-repo.git",
        "late-page-repo.git",
    }

    cfg.repo_denylist = ["denylisted-repo"]
    main._run_cycle(cfg, s3, {})

    second_pointer, second_staged, _ = _read_cycle(s3)
    second_meta = json.loads(second_staged["meta.json"])
    assert second_pointer["cycle_id"] != first_pointer["cycle_id"]
    assert second_meta["repos_total"] == 3
    assert second_meta["repos_scanned"] == 3
    assert second_meta["mirrors_pruned"] == [
        "deleted-repo",
        "denylisted-repo",
        "emptied-repo",
        "renamed-repo",
    ]
    assert {path.name for path in clone_root.iterdir()} == {
        gitscan.CLONE_ROOT_MARKER,
        "stable-repo.git",
        "late-page-repo.git",
        "renamed-as-repo.git",
    }
    assert {row["repo"] for row in _rows(second_staged["commits.parquet"])} == {
        "stable-repo",
        "late-page-repo",
        "renamed-as-repo",
    }
    assert [call["params"]["page"] for call in forge_calls] == [1, 2, 1, 2]
    second_names = [name for name, _, _ in mirror_calls[6:]]
    assert second_names == ["stable-repo", "late-page-repo", "renamed-as-repo"]
    assert mirror_calls[-1] == ("renamed-as-repo", False, True)

    _write(sources["emptied-repo"], "new.py", "print('reopened')\n")
    _commit(
        sources["emptied-repo"],
        "2026-09-23T11:30:00+00:00",
        "feat: reopen emptied repo",
    )
    main._run_cycle(cfg, s3, {})

    _, third_staged, _ = _read_cycle(s3)
    third_meta = json.loads(third_staged["meta.json"])
    assert third_meta["repos_total"] == 4
    assert third_meta["repos_scanned"] == 4
    assert third_meta["mirrors_pruned"] == []
    assert (clone_root / "emptied-repo.git").is_dir()
    assert mirror_calls[-1] == ("emptied-repo", False, True)
    third_commits = _rows(third_staged["commits.parquet"])
    assert sum(row["repo"] == "emptied-repo" for row in third_commits) == 2
    assert {row["repo"] for row in third_commits} == {
        "stable-repo",
        "late-page-repo",
        "renamed-as-repo",
        "emptied-repo",
    }


def test_mirror_volume_recovery_rebuilds_or_withholds_without_replacing_s3(
    tmp_path, monkeypatch
):
    """An empty/damaged cache is rebuildable, but a failed rebuild is fail-closed."""
    recovery = _make_recovery_fixture(tmp_path, monkeypatch)
    s3 = FakeS3()

    main._run_cycle(recovery.cfg, s3, recovery.family_map)
    first_pointer, first_staged, first_fixed = _read_cycle(s3)
    assert first_staged == first_fixed

    # Losing the PVC contents leaves only its ownership marker. The next
    # cycle must cold-clone both repositories and publish a complete dataset.
    for path in recovery.clone_root.iterdir():
        if path.name != gitscan.CLONE_ROOT_MARKER:
            shutil.rmtree(path)
    assert list(recovery.clone_root.iterdir()) == [
        recovery.clone_root / gitscan.CLONE_ROOT_MARKER
    ]

    main._run_cycle(recovery.cfg, s3, recovery.family_map)
    empty_recovery_pointer, empty_recovery_staged, empty_recovery_fixed = _read_cycle(s3)
    empty_recovery_meta = json.loads(empty_recovery_staged["meta.json"])
    assert empty_recovery_pointer["cycle_id"] != first_pointer["cycle_id"]
    assert empty_recovery_staged == empty_recovery_fixed
    assert empty_recovery_meta["repos_total"] == 2
    assert empty_recovery_meta["repos_scanned"] == 2
    assert empty_recovery_meta["repos_failed"] == []
    assert {row["repo"] for row in _rows(empty_recovery_staged["commits.parquet"])} == {
        "stable-repo", "recover-repo"
    }
    for name, key in first_pointer["objects"].items():
        assert s3.objects[f"{PREFIX}/{key}"][0] == first_staged[name]

    # A damaged mirror is also rebuilt from Forgejo. Removing HEAD forces the
    # cold-clone path while leaving the rest of the PVC usable.
    damaged_mirror = recovery.clone_root / "recover-repo.git"
    (damaged_mirror / "HEAD").unlink()
    main._run_cycle(recovery.cfg, s3, recovery.family_map)
    damaged_recovery_pointer, damaged_recovery_staged, damaged_recovery_fixed = _read_cycle(s3)
    damaged_recovery_meta = json.loads(damaged_recovery_staged["meta.json"])
    assert damaged_recovery_pointer["cycle_id"] != empty_recovery_pointer["cycle_id"]
    assert damaged_recovery_staged == damaged_recovery_fixed
    assert damaged_recovery_meta["repos_scanned"] == 2
    assert damaged_recovery_meta["repos_failed"] == []
    assert (damaged_mirror / "HEAD").is_file()
    assert not (recovery.clone_root / "recover-repo.git.tmp").exists()

    # If Forgejo cannot recreate a damaged mirror, the failure-rate guard
    # withholds before publication. The last complete S3 cycle remains the
    # authoritative pointer and fixed-key snapshot byte-for-byte.
    before_withhold_objects = dict(s3.objects)
    before_withhold_puts = list(s3.puts)
    shutil.rmtree(damaged_mirror)
    shutil.rmtree(recovery.sources["recover-repo"])
    with pytest.raises(main.CycleWithheld, match=r"1/2 repo\(s\) failed"):
        main._run_cycle(recovery.cfg, s3, recovery.family_map)

    assert s3.objects == before_withhold_objects
    assert s3.puts == before_withhold_puts
    current_pointer, current_staged, current_fixed = _read_cycle(s3)
    assert current_pointer == damaged_recovery_pointer
    assert current_staged == damaged_recovery_staged
    assert current_fixed == damaged_recovery_fixed
    assert (recovery.clone_root / "stable-repo.git").is_dir()
    assert not damaged_mirror.exists()
