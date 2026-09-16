from types import SimpleNamespace

from src import gitscan, main


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
