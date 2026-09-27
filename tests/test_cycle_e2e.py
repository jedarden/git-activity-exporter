import io
import json
import os
import subprocess
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from src import forge, main, s3io
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
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    clone_root = tmp_path / "mirrors"
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


def _lifecycle_repo(name, source, empty=False):
    return {
        "name": name,
        "full_name": f"fixture/{name}",
        "clone_url": source.as_uri(),
        "empty": empty,
    }


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
    monkeypatch.setattr(main, "_now", lambda: GENERATED_AT)

    clone_root = tmp_path / "mirrors"
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
