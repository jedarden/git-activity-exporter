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


class GitError(Exception):
    pass


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
        raise GitError(f"{_safe(args)} timed out after {timeout}s")
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


def ensure_mirror(repo, clone_root: str, token: str, shallow_since_days: int, timeout: int) -> str:
    """Clone or refresh one bare mirror. Returns its path."""
    path = mirror_path(clone_root, repo["name"])
    since = (datetime.now(timezone.utc) - timedelta(days=shallow_since_days)).strftime("%Y-%m-%d")
    url = repo["clone_url"]
    env = _credential_env(token)

    if os.path.exists(os.path.join(path, "HEAD")):
        try:
            _run(["git", "-C", path, "fetch", "--quiet", "--prune", f"--shallow-since={since}",
                  url, "+refs/heads/*:refs/heads/*"], timeout, env=env)
            return path
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
    return path


def _bead_id_from(subject: str, trailer: str):
    if trailer.strip():
        return trailer.strip()
    m = _SCOPE_CAPTURE_RE.match(subject)
    if m and _BEAD_LIKE_RE.match(m.group(1)):
        return m.group(1)
    return None


def scan_commits(path: str, repo_name: str, window_days: int, excluded, timeout: int):
    """One dict per commit in the window, with both raw and filtered line
    counts. Merges are excluded: git reports no numstat for them, so counting
    them would inflate commit counts with rows that can never carry lines."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    out = _run(
        ["git", "-C", path, "log", "--all", "--no-merges", "--numstat",
         f"--since={since}", f"--pretty=format:{_PRETTY}"],
        timeout,
    )

    patterns = [re.compile(p) for p in excluded]
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("C" + _FIELD_SEP):
            _, sha, ts, email, trailer, subject = line.split(_FIELD_SEP, 5)
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
