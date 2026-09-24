import os
import re
import subprocess
from datetime import datetime, timezone

import pytest

from src import beads, gitscan
from src.config import DEFAULT_EXCLUDED_PATHS
from src.window import ReportingWindow


def test_bead_id_prefers_trailer_over_scope():
    assert gitscan._bead_id_from("fix(needle-aaaaaaaa): x", "needle-bbbbbbbb") == "needle-bbbbbbbb"


def test_bead_id_from_conventional_scope():
    assert gitscan._bead_id_from("fix(needle-318c33ba): thing", "") == "needle-318c33ba"


def test_bead_id_ignores_non_bead_scopes():
    # A scope that is a component name, not a bead id, must not be recorded as
    # one -- `fix(span):` is real and appears in NEEDLE's history.
    assert gitscan._bead_id_from("fix(span): resolve type mismatch", "") is None
    assert gitscan._bead_id_from("docs: no scope at all", "") is None


def test_default_exclusions_catch_the_dominant_contaminator():
    # .beads/ churn was 68.1% of all line volume across 105 repos over 30
    # days; if this stops matching, "lines of code" silently becomes
    # "checkpoint bookkeeping".
    pats = [re.compile(p) for p in DEFAULT_EXCLUDED_PATHS]

    def excluded(path):
        return any(p.search(path) for p in pats)

    assert excluded(".beads/checkpoint/forensic.jsonl")
    assert excluded("sub/.beads/checkpoint/objects/gen-abc.jsonl")
    assert excluded("web/node_modules/left-pad/index.js")
    assert excluded("Cargo.lock")
    assert excluded("crates/core/Cargo.lock")
    assert excluded("public/vendor/hyparquet/index.js")
    assert excluded("app/bundle.min.js")
    assert not excluded("src/main.rs")
    assert not excluded("docs/plan/plan.md")
    # A path merely containing the word must not be swept up.
    assert not excluded("src/beads_client.rs")


def test_mark_bulk_flags_but_does_not_drop():
    commits = [
        {"lines_added": 10, "lines_deleted": 5, "files_changed": 3},
        {"lines_added": 9_000_000, "lines_deleted": 0, "files_changed": 133},
        {"lines_added": 12, "lines_deleted": 0, "files_changed": 25_639},
    ]
    out = gitscan.mark_bulk(commits, 5000, 200)
    assert [c["is_bulk"] for c in out] == [False, True, True]
    assert len(out) == 3, "bulk commits must survive as commits"


def test_credentials_never_survive_into_a_log_line():
    # The first live cold pass leaked the Forgejo token into the pod log:
    # subprocess.TimeoutExpired stringifies the whole argv, and the token was
    # embedded in the clone URL. Both halves of the fix are asserted here --
    # argv no longer carries it, and any that reappears is scrubbed anyway.
    leaky = "https://x-access-token:DEADBEEFCAFE1234@git.ardenone.com/jedarden/x.git"
    assert "DEADBEEFCAFE1234" not in gitscan._scrub(leaky)
    assert gitscan._scrub(leaky) == "https://<redacted>@git.ardenone.com/jedarden/x.git"

    rendered = gitscan._safe(["git", "clone", "--mirror", leaky, "/data/mirrors/x.git.tmp"])
    assert "DEADBEEF" not in rendered
    assert "<redacted>" in rendered

    # stderr from git is scrubbed on the same path
    assert "DEADBEEF" not in gitscan._scrub(f"fatal: could not read from {leaky}")


def test_token_travels_in_env_not_argv():
    env = gitscan._credential_env("SUPERSECRET")
    # Present for git to consume...
    assert env["FORGE_TOKEN"] == "SUPERSECRET"
    # ...but only ever dereferenced by name, so it cannot appear in a command
    # line, a process listing, or an exception string.
    assert "SUPERSECRET" not in env["GIT_CONFIG_VALUE_0"]
    assert "$FORGE_TOKEN" in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# --- Failure semantics (docs/notes/data-sources.md) -------------------------
# These tests exercise ensure_mirror's decision tree with the git subprocess
# faked out, the same way the rest of this suite avoids needing a mirror.


def _fake_mirror(tmp_path, name="x"):
    path = tmp_path / f"{name}.git"
    path.mkdir()
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _repo(name="x"):
    return {"name": name, "clone_url": f"https://git.ardenone.com/jedarden/{name}.git"}


def _fake_clone(target):
    """Stand in for `git clone` by materializing the target it points at."""
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")


def _git(path, *args, date=None):
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
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit_fixture(path, name, content, date, subject):
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--no-verify", "-m", subject, date=date)


def test_existing_shallow_mirror_is_deepened_for_wider_window(tmp_path):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    _git(source, "config", "user.name", "fixture")
    _git(source, "config", "user.email", "fixture@example.com")
    _commit_fixture(source, "old.txt", "old\n", "2020-01-01T00:00:00+00:00", "old")
    _commit_fixture(
        source, "middle.txt", "middle\n", "2026-08-30T00:00:00+00:00", "middle"
    )
    _commit_fixture(source, "new.txt", "new\n", "2026-09-20T00:00:00+00:00", "new")
    middle_sha = _git(source, "rev-parse", "HEAD~1")

    mirror = tmp_path / "history.git"
    subprocess.run(
        [
            "git", "clone", "-q", "--mirror", "--shallow-since=2026-09-01",
            source.as_uri(), str(mirror),
        ],
        check=True,
    )
    window_start = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    assert not gitscan.mirror_history_complete(str(mirror), window_start, 100, 60)
    assert subprocess.run(
        ["git", "-C", str(mirror), "cat-file", "-e", f"{middle_sha}^{{commit}}"],
        capture_output=True,
    ).returncode != 0

    inode = os.stat(mirror).st_ino
    path, refreshed = gitscan.ensure_mirror(
        {"name": "history", "clone_url": source.as_uri()},
        str(tmp_path), "token", 100, 60, window_start,
    )

    assert path == str(mirror)
    assert refreshed is True
    assert os.stat(mirror).st_ino == inode
    assert gitscan.mirror_history_complete(str(mirror), window_start, 100, 60)
    assert subprocess.run(
        ["git", "-C", str(mirror), "cat-file", "-e", f"{middle_sha}^{{commit}}"],
        capture_output=True,
    ).returncode == 0
    window = ReportingWindow.from_anchor(
        datetime(2026, 9, 23, 12, tzinfo=timezone.utc), 30
    )
    commits = gitscan.scan_commits(str(mirror), "history", 30, [], 60, window)
    assert {commit["subject"] for commit in commits} == {"middle", "new"}


def test_run_timeout_raises_git_timeout_not_plain_giterror(monkeypatch):
    # The distinction is the whole semantic: a timeout means git is healthy
    # and the forge is slow, so the mirror is kept; any other failure means
    # corruption and costs a re-clone.
    def boom(args, timeout, cwd=None, env=None, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(gitscan.subprocess, "run", boom)
    with pytest.raises(gitscan.GitTimeout) as excinfo:
        gitscan._run(["git", "clone", "https://x", "/tmp/y"], 600)
    assert isinstance(excinfo.value, gitscan.GitError)
    assert "timed out after 600s" in str(excinfo.value)


def test_fetch_timeout_keeps_mirror_and_serves_it_stale(tmp_path, monkeypatch):
    path = _fake_mirror(tmp_path)
    calls = []

    def fake_run(args, timeout, cwd=None, env=None):
        calls.append(args)
        raise gitscan.GitTimeout(f"git fetch timed out after {timeout}s")

    monkeypatch.setattr(gitscan, "_run", fake_run)
    out_path, refreshed = gitscan.ensure_mirror(_repo(), str(tmp_path), "tok", 100, 600)

    assert out_path == str(path)
    assert refreshed is False, "a fetch timeout must surface as stale, not failure"
    assert path.is_dir(), "the mirror must survive a timed-out fetch"
    assert len(calls) == 1, "no re-clone may follow a fetch timeout"
    assert "fetch" in calls[0]


def test_fetch_corruption_reclones(tmp_path, monkeypatch):
    path = _fake_mirror(tmp_path)

    def fake_run(args, timeout, cwd=None, env=None):
        if "fetch" in args:
            raise gitscan.GitError("fatal: pack has bad object")
        _fake_clone(args[-1])
        return ""

    monkeypatch.setattr(gitscan, "_run", fake_run)
    out_path, refreshed = gitscan.ensure_mirror(_repo(), str(tmp_path), "tok", 100, 600)

    assert out_path == str(path)
    assert refreshed is True
    assert path.is_dir(), "the re-clone replaces the corrupt mirror in place"


def test_clone_timeout_excludes_repo_and_cleans_its_tmp(tmp_path, monkeypatch):
    # Before the cleanup, a timed-out clone left its partial pack as
    # <name>.git.tmp on the PVC until the next clone of the SAME repo --
    # potentially forever. This was one of the two ways mirrors grew
    # unbounded.
    def fake_run(args, timeout, cwd=None, env=None):
        assert "fetch" not in args, "no mirror exists, so there is nothing to fetch"
        os.makedirs(args[-1])
        raise gitscan.GitTimeout(f"git clone timed out after {timeout}s")

    monkeypatch.setattr(gitscan, "_run", fake_run)
    with pytest.raises(gitscan.GitTimeout):
        gitscan.ensure_mirror(_repo(), str(tmp_path), "tok", 100, 600)

    assert not (tmp_path / "x.git.tmp").exists(), "partial clone pack must not sit on the PVC"
    assert not (tmp_path / "x.git").exists()


def test_dormant_repo_still_falls_back_to_depth_one(tmp_path, monkeypatch):
    # Regression guard: the timeout split must not break the pre-existing
    # "no commits since the cutoff" fallback.
    def fake_run(args, timeout, cwd=None, env=None):
        if "--shallow-since" in args:
            raise gitscan.GitError("error processing shallow info: cutoff excludes every commit")
        _fake_clone(args[-1])
        return ""

    monkeypatch.setattr(gitscan, "_run", fake_run)
    out_path, refreshed = gitscan.ensure_mirror(_repo(), str(tmp_path), "tok", 100, 600)

    assert refreshed is True
    assert (tmp_path / "x.git").is_dir()


def test_prune_removes_orphans_and_litter_keeps_live(tmp_path):
    kept = _fake_mirror(tmp_path, "kept")
    dead = _fake_mirror(tmp_path, "dead")
    litter = tmp_path / "half.git.tmp"
    litter.mkdir()
    stranger = tmp_path / "NOTES.txt"
    stranger.write_text("not ours")
    headless = tmp_path / "headless.git"
    headless.mkdir()  # a .git suffix alone is not proof it is one of ours

    pruned = gitscan.prune_orphans(str(tmp_path), ["kept"])

    assert pruned == ["dead"]
    assert kept.is_dir()
    assert not dead.exists(), "a repo gone from the forge must not hold PVC forever"
    assert not litter.exists(), "clone litter from a killed clone is swept too"
    assert stranger.exists() and headless.exists(), "only recognizably-ours mirrors are deleted"


def test_prune_counts_failed_scan_repos_as_live(tmp_path):
    # Prune runs against the ENUMERATION, not the scan results: a repo whose
    # scan failed this cycle is not orphaned and keeps its mirror.
    failed_scan = _fake_mirror(tmp_path, "flaky")
    assert gitscan.prune_orphans(str(tmp_path), ["flaky"]) == []
    assert failed_scan.is_dir()


def test_prune_refuses_empty_enumeration(tmp_path):
    dead = _fake_mirror(tmp_path, "dead")
    assert gitscan.prune_orphans(str(tmp_path), []) == []
    assert dead.exists(), "a listing fault must not be able to clear the whole farm"


def test_prune_missing_root_is_a_noop(tmp_path):
    assert gitscan.prune_orphans(str(tmp_path / "nope"), ["x"]) == []


def test_show_timeout_uses_git_timeout_semantics(monkeypatch):
    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(beads.subprocess, "run", boom)
    with pytest.raises(gitscan.GitTimeout, match=r"git show timed out after 17s"):
        beads.read_events("/data/mirrors/x.git", "x", 90, 17)
