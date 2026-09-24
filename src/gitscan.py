"""Mirror clones and commit extraction.

Clones are bare mirrors bounded by --shallow-since. Measured 2026-08-17
against git.ardenone.com: NEEDLE at a 60-day bound cost 148.6s and 133 MB to
clone cold, but only 1.21s to fetch incrementally. The cold pass is the
expensive one, which is why mirrors live on a PVC and a cycle publishes
whatever it has rather than blocking on a complete set.
"""
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from .window import ReportingWindow, as_utc

log = logging.getLogger(__name__)

# `fix(needle-318c33ba): ...` -- the conventional-commit scope, when it looks
# like a bead id. Coverage is too uneven to use as a *measure* (sampled
# 2026-08-17: vista 60%, commitgraph 48%, NEEDLE 42%, declarative-config 5.5%,
# aide-de-camp 0.4%), so it is carried only as a drill-down attribute. The
# bead measures come from the forensic log instead -- see beads.py.
_SCOPE_RE = re.compile(r"^\w+(?:\([^)]*\))?!?:")
_BEAD_LIKE_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9a-z]{5,8}$")
_SCOPE_CAPTURE_RE = re.compile(r"^\w+\(([^)]+)\)!?:")

_FIELD_SEP = "\x1f"
_PRETTY = f"C{_FIELD_SEP}%H{_FIELD_SEP}%at{_FIELD_SEP}%aE{_FIELD_SEP}%(trailers:key=Bead-Id,valueonly,separator=){_FIELD_SEP}%s"
_DEEPEN_MIN_COMMITS = 100
_DEEPEN_MAX_ATTEMPTS = 8


class GitError(Exception):
    pass


class GitTimeout(GitError):
    """The invocation exceeded GIT_TIMEOUT_SECONDS and git was killed.

    Distinguished from other GitError so ensure_mirror can preserve an
    existing mirror when only its fetch timed out. Timeouts during a cold
    clone, commit scan, or forensic-log read still exclude that repo because
    those operations cannot provide a complete result. See ensure_mirror and
    docs/notes/data-sources.md "Failure semantics".
    """


def _run(args, timeout, cwd=None, env=None):
    """Run git, and NEVER let a credential reach an exception string.

    subprocess.TimeoutExpired stringifies the entire argv. When the token was
    embedded in the clone URL that put it verbatim into the log on every
    timeout -- which is exactly what happened on the first live cold pass.
    The token now travels in the environment (see _credential_env) so argv is
    clean, and every failure path is additionally scrubbed here so a future
    change cannot silently reintroduce the leak.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env
        )
    except subprocess.TimeoutExpired:
        raise GitTimeout(f"{_safe(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise GitError(f"{_safe(args)} failed rc={proc.returncode}: {_scrub(proc.stderr.strip()[:300])}")
    return proc.stdout


# Anything that looks like credentials in a URL, whatever the scheme.
_CRED_RE = re.compile(r"(https?://)[^/@\s]+@")


def _scrub(text: str) -> str:
    return _CRED_RE.sub(r"\1<redacted>@", text or "")


def _safe(args) -> str:
    """A loggable rendering of a git command with any credential removed."""
    return _scrub(" ".join(str(a) for a in args))


def _credential_env(token: str) -> dict:
    """Supply the credential through git's config-via-environment mechanism.

    The token never appears in argv, in the stored remote, or in .git/config
    on the PVC -- so it cannot be captured by a process listing, an exception
    string, or anything that echoes a command back.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["FORGE_TOKEN"] = token
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = (
        '!f() { test "$1" = get && echo "username=x-access-token" '
        '&& echo "password=$FORGE_TOKEN"; }; f'
    )
    return env


def mirror_path(clone_root: str, repo_name: str) -> str:
    return os.path.join(clone_root, f"{repo_name}.git")


def ensure_mirror(repo, clone_root: str, token: str, shallow_since_days: int, timeout: int,
                  window_start: Optional[datetime] = None):
    """Clone or refresh one bare mirror. Returns (path, refreshed).

    refreshed is False when the mirror is served stale -- currently only a
    fetch timeout. The caller still scans it: preserving the mirror beats
    dropping the repo's whole window from the cycle, and it is how a slow
    forge degrades (repos_stale in meta.json) instead of escalating. Failure
    semantics are specified in
    docs/notes/data-sources.md "Failure semantics".

    Existing mirrors are always fetched with the recomputed date bound. If the
    shallow boundary is still newer than that bound, bounded --deepen fetches
    extend it until the cutoff is reached or the attempt budget is exhausted;
    widening the reporting window does not require --unshallow or a re-clone.
    """
    path = mirror_path(clone_root, repo["name"])
    reference = (
        as_utc(window_start) if window_start is not None else datetime.now(timezone.utc)
    )
    since = (reference - timedelta(days=shallow_since_days)).strftime("%Y-%m-%d")
    url = repo["clone_url"]
    env = _credential_env(token)

    if os.path.exists(os.path.join(path, "HEAD")):
        try:
            refspec = "+refs/heads/*:refs/heads/*"
            _run(["git", "-C", path, "fetch", "--quiet", "--prune", f"--shallow-since={since}",
                  url, refspec], timeout, env=env)
            depth = max(_DEEPEN_MIN_COMMITS, shallow_since_days)
            for _ in range(_DEEPEN_MAX_ATTEMPTS):
                if mirror_history_complete(path, reference, shallow_since_days, timeout):
                    break
                _run(["git", "-C", path, "fetch", "--quiet", "--prune",
                      f"--deepen={depth}", url, refspec], timeout, env=env)
                depth *= 2
            return path, True
        except GitTimeout as e:
            # A timeout means the mirror is fine and the forge is slow. The
            # old behavior deleted it and re-cloned here -- a strictly longer
            # network operation that then timed out too, so one slow cycle
            # cost the repo its mirror AND its data.
            log.warning("fetch timed out for %s, keeping the stale mirror: %s", repo["name"], e)
            return path, False
        except GitError as e:
            # A mirror can be left unusable by a killed clone (partial pack,
            # missing HEAD's target). Re-cloning is cheap relative to serving
            # wrong numbers from a corrupt one.
            log.warning("fetch failed for %s, re-cloning: %s", repo["name"], e)
            shutil.rmtree(path, ignore_errors=True)

    os.makedirs(clone_root, exist_ok=True)
    tmp = path + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        try:
            _run(["git", "clone", "--quiet", "--mirror", f"--shallow-since={since}", url, tmp], timeout, env=env)
        except GitError as e:
            # "error processing shallow info" means the cutoff excludes every
            # commit on the remote -- a repo dormant longer than the window. It
            # is a legitimate repo with nothing in range, not a broken one, so
            # fall back to a minimal clone rather than dropping it from the fleet.
            if "shallow info" not in str(e):
                raise
            log.info("%s has no commits since %s; cloning at depth 1 instead", repo["name"], since)
            shutil.rmtree(tmp, ignore_errors=True)
            _run(["git", "clone", "--quiet", "--mirror", "--depth", "1", url, tmp], timeout, env=env)
        shutil.rmtree(path, ignore_errors=True)
        os.rename(tmp, path)
        return path, True
    finally:
        # A failed or timed-out clone must not leave its partial pack on the
        # PVC -- before this finally, an interrupted clone left <name>.git.tmp
        # behind until the next clone of the SAME repo, which might be never.
        shutil.rmtree(tmp, ignore_errors=True)


def mirror_history_complete(path: str, window_start: datetime,
                            shallow_since_days: int, timeout: int) -> bool:
    """Whether a mirror reaches the requested date bound for the window."""
    cutoff = (as_utc(window_start) - timedelta(days=shallow_since_days)).date()
    shallow_path = os.path.join(path, "shallow")
    if not os.path.isfile(shallow_path):
        return True

    try:
        with open(shallow_path) as shallow:
            boundaries = [line.strip() for line in shallow if line.strip()]
        if not boundaries:
            return False
        output = _run(
            ["git", "-C", path, "log", "--no-walk", "--format=%cI", *boundaries],
            timeout,
        )
        boundary_dates = [line.strip() for line in output.splitlines() if line.strip()]
        if len(boundary_dates) != len(boundaries):
            return False
        parsed = [
            datetime.fromisoformat(value.replace("Z", "+00:00")) for value in boundary_dates
        ]
        return all(as_utc(value).date() <= cutoff for value in parsed)
    except (OSError, GitError, TypeError, ValueError) as error:
        log.warning("could not establish mirror history coverage for %s: %s", path, error)
        return False


def prune_orphans(clone_root: str, live_names) -> list:
    """Delete mirrors and clone litter whose repo is no longer live.

    Runs after a successful enumeration, against exactly that enumeration: a
    repo deleted or renamed on the forge, denylisted, or turned empty stops
    costing PVC the same cycle. A repo that merely FAILED to scan is still
    live and keeps its mirror.

    Refuses to run against an empty live set. An enumeration returning
    nothing is far more likely a listing fault (wrong owner, API change) than
    a fleet that genuinely shrank to zero, and being wrong here deletes every
    mirror at once and re-pays the full cold pass.

    Returns the sorted names whose mirrors were removed; the caller records
    them in meta.json so a deletion is auditable rather than silent.
    <name>.git.tmp litter from a killed clone is swept too, but is not a repo
    and is only logged.
    """
    live = {f"{name}.git" for name in live_names}
    if not live:
        log.warning("clone_root %s not pruned: enumeration returned no repos", clone_root)
        return []

    try:
        entries = sorted(os.listdir(clone_root))
    except FileNotFoundError:
        return []

    pruned = []
    for entry in entries:
        if entry in live:
            continue
        path = os.path.join(clone_root, entry)
        if not os.path.isdir(path):
            continue
        if entry.endswith(".git.tmp"):
            shutil.rmtree(path, ignore_errors=True)
            log.info("removed clone litter %s", entry)
        elif entry.endswith(".git") and os.path.exists(os.path.join(path, "HEAD")):
            # The HEAD check: only something that looks like one of our bare
            # mirrors is assumed to be ours to delete.
            shutil.rmtree(path, ignore_errors=True)
            pruned.append(entry[: -len(".git")])
    return pruned


def _bead_id_from(subject: str, trailer: str):
    if trailer.strip():
        return trailer.strip()
    m = _SCOPE_CAPTURE_RE.match(subject)
    if m and _BEAD_LIKE_RE.match(m.group(1)):
        return m.group(1)
    return None


def scan_commits(path: str, repo_name: str, window_days: int, excluded, timeout: int,
                 reporting_window: Optional[ReportingWindow] = None):
    """One dict per commit in the window, with both raw and filtered line
    counts. Merges are excluded: git reports no numstat for them, so counting
    them would inflate commit counts with rows that can never carry lines."""
    if reporting_window is None:
        reporting_window = ReportingWindow.from_anchor(
            datetime.now(timezone.utc), window_days
        )
    out = _run(
        ["git", "-C", path, "log", "--all", "--no-merges", "--numstat",
         f"--pretty=format:{_PRETTY}"],
        timeout,
    )

    patterns = [re.compile(p) for p in excluded]
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("C" + _FIELD_SEP):
            _, sha, ts, email, trailer, subject = line.split(_FIELD_SEP, 5)
            cur = None
            if not reporting_window.contains_epoch(int(ts)):
                continue
            cur = {
                "sha": sha, "repo": repo_name, "ts": int(ts), "author_email": email,
                "subject": subject, "bead_id": _bead_id_from(subject, trailer),
                "lines_added": 0, "lines_deleted": 0, "files_changed": 0,
                "lines_added_raw": 0, "lines_deleted_raw": 0, "files_changed_raw": 0,
            }
            commits.append(cur)
        elif line and cur is not None:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path_ = parts
            # Binary files report "-\t-": a real change, but with no line
            # count. Counted as a touched file, contributing zero lines.
            a = 0 if added == "-" else int(added)
            d = 0 if deleted == "-" else int(deleted)
            cur["lines_added_raw"] += a
            cur["lines_deleted_raw"] += d
            cur["files_changed_raw"] += 1
            if not any(p.search(path_) for p in patterns):
                cur["lines_added"] += a
                cur["lines_deleted"] += d
                cur["files_changed"] += 1
    return commits


def mark_bulk(commits, trim_max_lines: int, trim_max_files: int):
    """Flag, don't drop. A bulk commit still counts as a commit -- it just
    stops contributing to line totals, and stays visible via bulk_commits so
    the exclusion is auditable rather than a silent hole in the chart."""
    for c in commits:
        c["is_bulk"] = bool(
            (c["lines_added"] + c["lines_deleted"]) > trim_max_lines
            or c["files_changed"] > trim_max_files
        )
    return commits
