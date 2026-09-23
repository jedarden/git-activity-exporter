"""Calculation semantics of the commit scan and the rollup.

The sibling suites each pin one thing: test_gitscan the failure paths,
test_output_schema the shape of the published objects, test_beads the
bulk-hour heuristic in isolation. This file pins the arithmetic the panel
actually draws -- numstat parsing against a real git transcript, the
raw/filtered split inside one commit, the bulk thresholds, which bead-event
kinds the rollup counts, and how a bulk-import hour splits closures.
Zero-data behaviour is test_output_schema's job and is not repeated here.
"""
import os
import subprocess
from datetime import datetime, timezone

import pytest

from src import aggregate, beads, gitscan
from src.config import DEFAULT_EXCLUDED_PATHS
from src.window import ReportingWindow

UTC = timezone.utc

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(path, *args, date=None):
    env = {**os.environ, **_GIT_ENV}
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, env=env).stdout.strip()


def _write(path, rel, content):
    p = path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())


def _commit(path, date, msg):
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--no-verify", "-m", msg, date=date)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    """scan_commits over a real repo whose every numstat shape is known:
    plain text, a binary file, excluded paths, a merge, and commits sitting
    exactly on each reporting-window boundary. Built with real git rather
    than a hand-written transcript because the point is the parser against
    git's actual output.

    Author times, oldest first (the scan window is [09-19, 09-21)):

      old    2026-09-18T12:00Z  before the window       (dropped)
      start  2026-09-19T00:00Z  exactly at window start  (kept)
      mixed  2026-09-20T10:00Z  src + .beads + binary + node_modules
      side   2026-09-20T11:00Z  on a side branch
      main   2026-09-20T12:00Z  on main
      merge  2026-09-20T13:00Z  --no-ff merge of side   (never a row)
      anchor 2026-09-21T00:00Z  exactly at window end    (next cycle's)

    git log emits newest first, so `old`'s numstat lines are the last thing
    in the transcript, directly after `start`'s entry.
    """
    path = tmp_path_factory.mktemp("semantics")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)],
                   check=True, capture_output=True)

    _write(path, "old.txt", "a\n" * 5)
    _commit(path, "2026-09-18T12:00:00+00:00", "old: before the window")
    _write(path, "start.txt", "s\n" * 3)
    _commit(path, "2026-09-19T00:00:00+00:00", "start: exactly at window start")

    _write(path, "src.txt", "x\ny\n")
    _write(path, ".beads/checkpoint/forensic.jsonl", '{"a":1}\n')
    _write(path, "logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    _write(path, "web/node_modules/left-pad/index.js", "l\n" * 4)
    _commit(path, "2026-09-20T10:00:00+00:00", "feat(gitact-0a1b2c3): mixed commit")

    _git(path, "checkout", "-q", "-b", "side")
    _write(path, "side.txt", "z\n")
    _commit(path, "2026-09-20T11:00:00+00:00", "chore: side work")
    _git(path, "checkout", "-q", "main")
    _write(path, "other.txt", "o\n")
    _commit(path, "2026-09-20T12:00:00+00:00", "chore: main work")
    merge_sha = _git(path, "merge", "--no-ff", "-q", "--no-verify",
                     "side", "-m", "merge: bring side back", date="2026-09-20T13:00:00+00:00")

    _write(path, "anchor.txt", "n\n" * 2)
    _commit(path, "2026-09-21T00:00:00+00:00", "anchor: exactly at window end")

    window = ReportingWindow(datetime(2026, 9, 19, tzinfo=UTC),
                             datetime(2026, 9, 21, tzinfo=UTC))
    commits = gitscan.scan_commits(
        str(path), "fixture", 2, DEFAULT_EXCLUDED_PATHS, 60,
        reporting_window=window)
    return commits, merge_sha


def _by_subject(commits, subject):
    matches = [c for c in commits if c["subject"] == subject]
    assert len(matches) == 1, f"expected exactly one commit titled {subject!r}"
    return matches[0]


# --- numstat parsing, against the real transcript ---------------------------


def test_a_merge_commit_never_becomes_a_row(scanned):
    # git reports no numstat for a merge, so a counted merge would be a
    # commit row that can never carry lines (docs/notes/data-sources.md).
    commits, merge_sha = scanned
    assert merge_sha not in {c["sha"] for c in commits}
    assert {c["subject"] for c in commits} == {
        "chore: main work",
        "chore: side work",
        "feat(gitact-0a1b2c3): mixed commit",
        "start: exactly at window start",
    }


def test_a_binary_file_is_a_touched_file_with_zero_lines(scanned):
    # Binary files report -/- in numstat: a real change, counted as a file in
    # BOTH totals, contributing zero lines to either (data-sources.md).
    commits, _ = scanned
    mixed = _by_subject(commits, "feat(gitact-0a1b2c3): mixed commit")
    assert mixed["files_changed_raw"] == 4
    assert mixed["files_changed"] == 2  # src.txt + the binary; only the two pattern-excluded paths drop
    assert mixed["lines_added_raw"] == 7  # 2 src + 1 .beads + 4 node_modules + 0 binary
    assert mixed["lines_added"] == 2  # the binary added nothing to filter either


def test_excluded_paths_leave_raw_a_true_total_and_trim_the_filtered_one(scanned):
    # The 68.1% measured volume exclusion: .beads/ bookkeeping and vendored
    # trees stay in lines_*_raw so the totals reconcile, but never reach the
    # LOC the chart draws.
    commits, _ = scanned
    mixed = _by_subject(commits, "feat(gitact-0a1b2c3): mixed commit")
    assert mixed["lines_added"] == 2  # src.txt alone
    assert mixed["lines_deleted"] == 0
    # and the gap is exactly the excluded contribution, nothing more
    assert mixed["lines_added_raw"] - mixed["lines_added"] == 5  # 1 .beads + 4 vendored
    assert mixed["files_changed_raw"] - mixed["files_changed"] == 2  # .beads + node_modules


def test_the_window_is_half_open_on_the_author_epoch(scanned):
    commits, _ = scanned
    subjects = {c["subject"] for c in commits}
    assert "start: exactly at window start" in subjects  # inclusive lower bound
    # the upper bound belongs to the NEXT cycle; admitting it would double-count
    assert "anchor: exactly at window end" not in subjects
    assert "old: before the window" not in subjects


def test_numstat_of_a_dropped_commit_cannot_leak_into_the_last_kept_one(scanned):
    # git log emits newest first, so old.txt's numstat lines arrive directly
    # after the window-start commit's header. If the parser kept the previous
    # commit open across an out-of-window header, `start` would silently
    # absorb old.txt's 5 lines.
    commits, _ = scanned
    start = _by_subject(commits, "start: exactly at window start")
    assert start["lines_added"] == start["lines_added_raw"] == 3
    assert start["files_changed"] == start["files_changed_raw"] == 1


# --- bulk-commit thresholds --------------------------------------------------


def _lines(added, deleted, files=1, raw_added=None, raw_files=None):
    return {
        "lines_added": added, "lines_deleted": deleted, "files_changed": files,
        "lines_added_raw": raw_added if raw_added is not None else added,
        "lines_deleted_raw": deleted,
        "files_changed_raw": raw_files if raw_files is not None else files,
    }


def test_bulk_line_threshold_is_strict_and_judges_the_combined_total():
    at_the_bar = _lines(3000, 2000)  # added + deleted == 5000 exactly
    one_over = _lines(3000, 2001)
    split = _lines(3000, 2501)  # neither side alone crosses the bar
    out = gitscan.mark_bulk([at_the_bar, one_over, split], 5000, 200)
    assert [c["is_bulk"] for c in out] == [False, True, True]


def test_bulk_file_threshold_is_strict_and_independent_of_lines():
    out = gitscan.mark_bulk(
        [_lines(0, 0, files=200), _lines(0, 0, files=201)], 5000, 200)
    assert [c["is_bulk"] for c in out] == [False, True]


def test_bulk_reads_the_filtered_counts_not_the_raw_ones():
    # A commit whose entire volume is checkpoint churn is already excluded
    # from lines_*; flagging it bulk too would be double exclusion. Bulk
    # trims what the LOC filter leaves, and this commit leaves nothing.
    checkpoint_only = _lines(0, 0, files=2, raw_added=9_000_000, raw_files=2)
    out = gitscan.mark_bulk([checkpoint_only], 5000, 200)
    assert out[0]["is_bulk"] is False


# --- rollup: raw versus filtered, and which event kinds count ----------------


def _rollup_commit(repo, ts, added, deleted, files, bulk=False, raw_added=None):
    return {
        "sha": f"{repo}-{ts}-{added}-{files}", "repo": repo, "ts": ts,
        "author_email": "e@x", "subject": "s", "bead_id": None,
        "lines_added": added, "lines_deleted": deleted, "files_changed": files,
        "lines_added_raw": raw_added if raw_added is not None else added,
        "lines_deleted_raw": deleted, "files_changed_raw": files,
        "is_bulk": bulk,
    }


def test_a_bulk_commit_gives_up_filtered_lines_and_files_but_not_raw():
    # One (repo, hour) cell holding both kinds of commit: the trimmed
    # measures must carry exactly the small commit, while raw stays the true
    # total -- and bulk contributes no files either (output-schema.md:
    # "excluded paths and bulk commits contribute 0").
    hour = 3600 * 100
    commits = [
        _rollup_commit("NEEDLE", hour, added=40, deleted=10, files=3),
        _rollup_commit("NEEDLE", hour, added=9_000_000, deleted=1, files=400,
                       bulk=True, raw_added=9_000_000),
    ]
    rows = aggregate.build_hourly(commits, [], {})
    assert len(rows) == 1
    r = rows[0]
    assert r["commits"] == 2
    assert r["bulk_commits"] == 1
    assert (r["lines_added"], r["lines_deleted"], r["files_changed"]) == (40, 10, 3)
    assert (r["lines_added_raw"], r["lines_deleted_raw"]) == (9_000_040, 11)


def _ev(repo, hour, kind, actor="system", issue="x"):
    return {"repo": repo, "ts": hour * 3600, "issue_id": issue,
            "kind": kind, "actor": actor}


def test_every_counted_kind_lands_in_its_own_measure():
    events = [_ev("NEEDLE", 100, kind, actor="w1") for kind in
              ("closed", "claimed", "released", "reopened")]
    rows = aggregate.build_hourly([], events, {})
    assert len(rows) == 2
    r = next(row for row in rows if row["worker"] is None)
    assert (r["beads_closed"], r["beads_claimed"],
            r["beads_released"], r["beads_reopened"]) == (1, 1, 1, 1)
    assert r["beads_closed_bulk"] == 0


# --- bulk-hour closure splitting ---------------------------------------------


def test_bulk_hour_threshold_is_strict():
    at_the_bar = [_ev("a", 100, "closed", issue=f"b{i}") for i in range(150)]
    one_over = [_ev("a", 101, "closed", issue=f"c{i}") for i in range(151)]
    _, cells = beads.mark_bulk_hours(at_the_bar + one_over, 150)
    assert cells == {("a", 101)}


def test_hour_contamination_breaks_exactly_at_the_configured_share():
    # 151 flagged closures against 151 unflagged ones is exactly the default
    # 0.5 share: the whole hour was one bulk import, so everything in it
    # splits out.
    events = (
        [_ev("bulky", 100, "closed", issue=f"b{i}") for i in range(151)]
        + [_ev("a", 100, "closed", issue=f"a{i}") for i in range(75)]
        + [_ev("b", 100, "closed", issue=f"c{i}") for i in range(76)]
    )
    _, cells = beads.mark_bulk_hours(events, 150)
    assert ("a", 100) in cells
    assert ("b", 100) in cells

    # One more genuine closure tips the share to 151/303 < 0.5 and the hour
    # keeps only what the per-repo bar flagged on its own.
    events += [_ev("genuine", 100, "closed", issue="g1")]
    _, cells = beads.mark_bulk_hours(events, 150)
    assert cells == {("bulky", 100)}


def test_closure_split_survives_into_the_rollup_reconcilably():
    # The invariant the panel relies on when it toggles bulk hours off:
    # beads_closed + beads_closed_bulk must always equal the closures the
    # event grain carries, whatever the density heuristic decided.
    events = (
        [_ev("NEEDLE", 100, "closed", issue=f"b{i}") for i in range(200)]  # bulk hour
        + [_ev("NEEDLE", 101, "closed", issue=f"g{i}") for i in range(3)]  # quiet hour
    )
    events, _cells = beads.mark_bulk_hours(events, 150)
    rows = aggregate.build_hourly([], events, {})
    by_hour = {
        r["hour_epoch"]: r for r in rows if r["worker"] is None
    }
    assert by_hour[100]["beads_closed_bulk"] == 200
    assert by_hour[100]["beads_closed"] == 0
    assert by_hour[101]["beads_closed"] == 3
    assert by_hour[101]["beads_closed_bulk"] == 0
    assert sum(
        r["beads_closed"] + r["beads_closed_bulk"]
        for r in rows if r["worker"] is None
    ) == 203


def test_claims_inside_a_bulk_hour_are_never_flagged_but_still_count():
    # Contagion marks the hour's CLOSURES. A claim in the same hour is real
    # work that happened then: it keeps its own accounting and its actors.
    events = (
        [_ev("NEEDLE", 100, "closed", issue=f"b{i}") for i in range(200)]
        + [_ev("NEEDLE", 100, "claimed", actor="w1", issue="k1")]
        + [_ev("NEEDLE", 100, "claimed", actor="w2", issue="k2")]
    )
    events, cells = beads.mark_bulk_hours(events, 150)
    assert ("NEEDLE", 100) in cells
    claims = [e for e in events if e["kind"] == "claimed"]
    assert all(e["is_bulk_import"] is False for e in claims)

    rows = aggregate.build_hourly([], events, {})
    r = rows[0]
    assert r["beads_claimed"] == 2
    assert r["workers_active"] == 2
    assert r["beads_closed_bulk"] == 200
    assert r["beads_closed"] == 0
