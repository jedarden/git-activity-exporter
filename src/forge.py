"""Forgejo repo enumeration.

Only used to discover *which* repos exist. Commit data deliberately does not
come from the API: measured 2026-08-17 against git.ardenone.com (Forgejo
10.0.0+gitea-1.22.0), a page of 50 commits costs 0.95s without stats and
13.0s with `stat=true` -- ~14x, because the server diffs every commit. A
30-day backfill of ~14k commits would be about an hour of pure diffing. The
same numbers come free from `git log --numstat` against a local mirror.
"""
import logging
import re

import requests

from . import retry

log = logging.getLogger(__name__)

PAGE_SIZE = 50


def _is_transient(error: Exception) -> bool:
    """Whether a Forgejo page request may succeed on another attempt."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return 500 <= status < 600
        # Keep mocked/adapted response implementations useful without making
        # production behavior depend on parsing an exception string.
        match = re.search(r"\b([5-9][0-9]{2})\b", str(error))
        return bool(match and 500 <= int(match.group(1)) < 600)
    return False


def list_repos(base_url: str, token: str, owner: str, timeout: int, denylist=()):
    """Every non-empty repo owned by `owner`, newest API page first."""
    session = requests.Session()
    session.headers.update({"Authorization": f"token {token}"})

    repos, page = [], 1
    while True:
        def get_page():
            resp = session.get(
                f"{base_url}/api/v1/repos/search",
                params={"limit": PAGE_SIZE, "page": page, "owner": owner},
                timeout=timeout,
            )
            # The status check belongs inside the retry operation: a 5xx is
            # the response from this attempt, not a successful GET followed
            # by a separate non-retryable validation step.
            resp.raise_for_status()
            return resp

        resp = retry.call(
            get_page,
            is_retryable=_is_transient,
            label=f"Forgejo repository enumeration page {page}",
        )
        batch = resp.json().get("data") or []
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1

    out = []
    for r in repos:
        name = r.get("name")
        # An empty repo has no HEAD; cloning one succeeds but every later git
        # call against it fails, so drop it here rather than per-cycle.
        if r.get("empty"):
            log.info("skipping empty repo %s", name)
            continue
        if name in denylist:
            log.info("skipping denylisted repo %s", name)
            continue
        out.append({"name": name, "full_name": r.get("full_name"), "clone_url": r.get("clone_url")})

    log.info("discovered %d repo(s) from %s", len(out), base_url)
    return out
