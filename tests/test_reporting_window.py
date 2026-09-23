import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import aggregate, beads, gitscan, main
from src.window import ReportingWindow

UTC = timezone.utc


def _event(issue, timestamp):
    return json.dumps({
        "record_type": "event",
        "event": {
            "origin_store_uuid": "workspace",
            "issue_id": issue,
            "kind": "closed",
            "time": timestamp,
        },
    })


def _commit_line(sha, timestamp):
    return gitscan._FIELD_SEP.join(("C", sha, str(int(timestamp.timestamp())), "a@example.com", "", "s"))


def test_generated_at_is_the_utc_cycle_anchor():
    window = main._reporting_window("2026-09-06T05:00:00Z", 1)

    assert window.start == datetime(2026, 9, 5, 5, 0, tzinfo=UTC)
    assert window.end == datetime(2026, 9, 6, 5, 0, tzinfo=UTC)


def test_window_start_is_inclusive_and_end_is_exclusive():
    anchor = datetime(2026, 9, 6, 5, 17, tzinfo=UTC)
    window = ReportingWindow.from_anchor(anchor, 2)

    assert window.start == anchor - timedelta(days=2)
    assert window.end == anchor
    assert window.contains(window.start)
    assert not window.contains(window.end)


def test_offset_timestamps_are_compared_as_utc_instants():
    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 1
    )
    text = "\n".join([
        _event("before", "2026-09-05T06:59:59+02:00"),
        _event("start", "2026-09-05T05:00:00Z"),
        _event("end", "2026-09-06T07:00:00+02:00"),
    ])

    events = beads.parse_events(text, "repo", window)

    assert [event["issue_id"] for event in events] == ["start"]


def test_dst_window_is_elapsed_utc_not_local_calendar_time():
    eastern = ZoneInfo("America/New_York")
    anchor = datetime(2026, 3, 8, 3, 30, tzinfo=eastern)
    window = ReportingWindow.from_anchor(anchor, 1)

    assert window.start == datetime(2026, 3, 7, 7, 30, tzinfo=UTC)
    assert window.end == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    text = "\n".join([
        _event("start", "2026-03-07T02:30:00-05:00"),
        _event("end", "2026-03-08T03:30:00-04:00"),
    ])

    events = beads.parse_events(text, "repo", window)

    assert [event["issue_id"] for event in events] == ["start"]


def test_current_partial_hour_is_included_but_events_at_cycle_end_are_not():
    anchor = datetime(2026, 9, 6, 5, 17, 42, 500000, tzinfo=UTC)
    window = ReportingWindow.from_anchor(anchor, 1)
    text = "\n".join([
        _event("partial-hour", "2026-09-06T05:00:00Z"),
        _event("before-end", "2026-09-06T05:17:42.499999Z"),
        _event("at-end", "2026-09-06T05:17:42.500000Z"),
    ])

    events = beads.parse_events(text, "repo", window)
    rows = aggregate.build_hourly([], events, {})

    assert [event["issue_id"] for event in events] == ["partial-hour", "before-end"]
    assert rows[0]["hour_utc"] == "2026-09-06T05:00:00Z"
    assert rows[0]["commits"] == 0


def test_commit_scan_uses_the_same_inclusive_start_and_exclusive_end(monkeypatch):
    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 1
    )
    output = "\n".join([
        _commit_line("before", window.start - timedelta(microseconds=1)),
        _commit_line("start", window.start),
        _commit_line("last", window.end - timedelta(seconds=1)),
        _commit_line("end", window.end),
    ])
    calls = []

    def fake_run(args, timeout):
        calls.append((args, timeout))
        return output

    monkeypatch.setattr(gitscan, "_run", fake_run)
    commits = gitscan.scan_commits(
        "/mirror", "repo", 1, [], 60, reporting_window=window
    )

    assert [commit["sha"] for commit in commits] == ["start", "last"]
    assert calls[0][0][0:2] == ["git", "-C"]


def test_git_filters_author_time_when_committer_time_differs(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in {"user.name": "Test", "user.email": "test@example.com"}.items():
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)

    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 1
    )
    commits = [
        ("before", window.start - timedelta(seconds=1), window.start - timedelta(seconds=1)),
        ("start", window.start, window.start),
        ("end", window.end, window.end - timedelta(seconds=1)),
        ("mismatch", window.end - timedelta(seconds=1), window.start - timedelta(days=2)),
    ]
    for name, author, committer in commits:
        (path / name).write_text(name)
        subprocess.run(["git", "-C", str(path), "add", name], check=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": author.isoformat(),
            "GIT_COMMITTER_DATE": committer.isoformat(),
        }
        subprocess.run(
            ["git", "-C", str(path), "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", name],
            check=True,
            env=env,
        )

    scanned = gitscan.scan_commits(str(path), "repo", 1, [], 60, window)

    assert {commit["subject"] for commit in scanned} == {"start", "mismatch"}


def test_naive_event_timestamps_are_malformed_rather_than_ambiguous():
    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 1
    )
    events = beads.parse_events(_event("naive", "2026-09-05T05:00:00"), "repo", window)

    assert events == []


def test_cycle_window_is_shared_by_git_and_bead_reads(monkeypatch, tmp_path):
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        forge_base_url="https://forge",
        forge_token="token",
        forge_owner="owner",
        http_timeout_seconds=30,
        repo_denylist=[],
        clone_root=str(tmp_path),
        shallow_since_days=2,
        window_days=1,
        excluded_path_patterns=[],
        git_timeout_seconds=60,
    )
    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 1
    )
    seen = []
    mirror_starts = []
    monkeypatch.setattr(main.forge, "list_repos", lambda *args: [
        {"name": "repo", "clone_url": "https://forge/repo.git"}
    ])
    monkeypatch.setattr(main.gitscan, "prune_orphans", lambda *args: [])
    monkeypatch.setattr(
        main.gitscan,
        "ensure_mirror",
        lambda *args: mirror_starts.append(args[-1]) or ("/mirror", True),
    )
    monkeypatch.setattr(
        main.gitscan,
        "scan_commits",
        lambda *args: seen.append(("git", args[-1])) or [],
    )
    monkeypatch.setattr(
        main.beads,
        "read_events",
        lambda *args: seen.append(("beads", args[-1])) or [],
    )

    main._collect(cfg, {}, window)

    assert mirror_starts == [window.start]
    assert seen == [("git", window), ("beads", window)]


def test_reporting_window_rejects_naive_anchors_and_non_positive_days():
    with pytest.raises(ValueError, match="timezone offset"):
        ReportingWindow.from_anchor(datetime(2026, 9, 6, 5, 0), 1)
    with pytest.raises(ValueError, match="positive integer"):
        ReportingWindow.from_anchor(
            datetime(2026, 9, 6, 5, 0, tzinfo=UTC), 0
        )
