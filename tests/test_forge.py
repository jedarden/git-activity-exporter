"""Mocked Forgejo enumeration tests.

`list_repos` was previously only ever exercised with the function mocked
out entirely (tests/test_main.py), so its pagination, visibility and error
behavior was unspecified in the executable sense. These tests pin the API
surface -- documented in docs/notes/data-sources.md "Forgejo repo
enumeration" -- and the ordering property the mirror prune depends on: the
prune consumes the *complete* enumeration (every page, in order) and never
runs at all when enumeration fails or returns nothing.
"""
import json
from types import SimpleNamespace

import pytest
import requests

from src import forge, main, s3io
from tests.fake_s3 import FakeS3

BASE = "https://forge"
TOKEN = "test-token"
OWNER = "test-owner"
TIMEOUT = 30


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error for repos/search")

    def json(self):
        return self.payload


class FakeForge:
    """Stands in for requests.Session with canned pages.

    ``pages[0]`` is page 1. An int is an HTTP status for that page, an
    Exception instance is raised at request time, anything else is the JSON
    payload. Every request is recorded so tests can assert the walk.
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def install(self, monkeypatch):
        outer = self

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def get(self, url, params=None, timeout=None):
                params = dict(params or {})
                outer.calls.append({
                    "url": url,
                    "params": params,
                    "timeout": timeout,
                    "authorization": self.headers.get("Authorization"),
                })
                item = outer.pages[params.get("page", 1) - 1]
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, int):
                    return FakeResponse({"data": []}, status=item)
                return FakeResponse(item)

        monkeypatch.setattr(forge.requests, "Session", FakeSession)
        return self


def repo(n, empty=False, private=False):
    return {
        "name": f"repo-{n}",
        "full_name": f"{OWNER}/repo-{n}",
        "clone_url": f"{BASE}/{OWNER}/repo-{n}.git",
        "empty": empty,
        "private": private,
    }


def page(first, count=None):
    """One search page. count defaults to a full PAGE_SIZE page."""
    count = forge.PAGE_SIZE if count is None else count
    return {"ok": True, "data": [repo(n) for n in range(first, first + count)]}


def list_repos_with_pages(monkeypatch, pages, denylist=()):
    f = FakeForge(pages).install(monkeypatch)
    return forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT, denylist), f


# --- the enumeration walk -------------------------------------------------


def test_walks_every_page_before_returning(monkeypatch):
    # Pages 1 and 2 full, page 3 short: the fleet is the union of all three.
    # A walker that stopped at the first full page would hand the cycle --
    # and the mirror prune, which deletes anything unlisted -- one third of
    # the fleet and treat the rest as deleted upstream.
    out, f = list_repos_with_pages(monkeypatch, [
        page(1),
        page(forge.PAGE_SIZE + 1),
        page(2 * forge.PAGE_SIZE + 1, count=3),
    ])
    expected = [f"repo-{n}" for n in range(1, 2 * forge.PAGE_SIZE + 4)]
    assert [r["name"] for r in out] == expected, "page order is preserved"
    assert [c["params"]["page"] for c in f.calls] == [1, 2, 3]


def test_request_shape_is_pinned(monkeypatch):
    out, f = list_repos_with_pages(monkeypatch, [page(1, count=1)])
    assert len(out) == 1
    call = f.calls[0]
    assert call["url"] == f"{BASE}/api/v1/repos/search"
    assert call["params"]["limit"] == forge.PAGE_SIZE
    assert call["params"]["owner"] == OWNER
    assert call["authorization"] == f"token {TOKEN}"
    assert call["timeout"] == TIMEOUT


def test_short_page_is_the_last_page(monkeypatch):
    out, f = list_repos_with_pages(monkeypatch, [
        page(1),
        page(forge.PAGE_SIZE + 1, count=2),
    ])
    assert len(out) == forge.PAGE_SIZE + 2
    assert [c["params"]["page"] for c in f.calls] == [1, 2]


def test_exactly_full_page_costs_one_more_empty_request(monkeypatch):
    # A page of exactly PAGE_SIZE cannot be known to be last, so the walker
    # must ask once more and stop on the empty page. A fleet that is an
    # exact multiple of 50 pays one extra request, not a missing page.
    out, f = list_repos_with_pages(monkeypatch, [page(1), {"ok": True, "data": []}])
    assert len(out) == forge.PAGE_SIZE
    assert [c["params"]["page"] for c in f.calls] == [1, 2]


def test_empty_result_is_a_single_request_returning_no_repos(monkeypatch):
    out, f = list_repos_with_pages(monkeypatch, [{"ok": True, "data": []}])
    assert out == []
    assert [c["params"]["page"] for c in f.calls] == [1]


def test_denylist_filters_the_completed_walk(monkeypatch):
    # The denylist is applied to the already-complete enumeration, not used
    # to cut it short: the walk still visits both pages even though the only
    # repo on page 2 is denylisted.
    out, f = list_repos_with_pages(
        monkeypatch,
        [page(1), page(forge.PAGE_SIZE + 1, count=1)],
        denylist=[f"repo-{forge.PAGE_SIZE + 1}"],
    )
    assert [c["params"]["page"] for c in f.calls] == [1, 2]
    assert [r["name"] for r in out] == [f"repo-{n}" for n in range(1, forge.PAGE_SIZE + 1)]


# --- visibility and empty-repository behavior ------------------------------


def test_private_repos_are_enumerated_like_any_other(monkeypatch):
    # Visibility is decided server-side by what FORGE_TOKEN may see; the
    # exporter applies no client-side visibility filter.
    out, _ = list_repos_with_pages(
        monkeypatch, [{"ok": True, "data": [repo(1, private=True), repo(2)]}]
    )
    assert [r["name"] for r in out] == ["repo-1", "repo-2"]


def test_empty_repos_are_dropped_and_the_rest_kept_whole(monkeypatch):
    # An empty repo has no HEAD: cloning succeeds but every later git call
    # against it fails, so it is dropped here rather than per-cycle -- and
    # being absent from the result, its mirror is pruned the same cycle.
    out, _ = list_repos_with_pages(
        monkeypatch,
        [{"ok": True, "data": [repo(1), repo(2, empty=True), repo(3)]}],
    )
    assert [r["name"] for r in out] == ["repo-1", "repo-3"]
    assert out[0] == {
        "name": "repo-1",
        "full_name": f"{OWNER}/repo-1",
        "clone_url": f"{BASE}/{OWNER}/repo-1.git",
    }


# --- error responses -------------------------------------------------------


def test_http_error_response_fails_the_walk(monkeypatch):
    f = FakeForge([page(1), 500]).install(monkeypatch)
    with pytest.raises(requests.HTTPError):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)
    assert [c["params"]["page"] for c in f.calls] == [1, 2], "no retry, no partial result"


def test_transport_error_fails_the_walk(monkeypatch):
    f = FakeForge([requests.ConnectionError("connection refused")]).install(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)
    assert len(f.calls) == 1


# --- enumeration completeness vs. pruning ----------------------------------


def _cfg(tmp_path):
    # Superset covering both _collect and _run_cycle.
    return SimpleNamespace(
        forge_base_url=BASE,
        forge_token=TOKEN,
        forge_owner=OWNER,
        http_timeout_seconds=TIMEOUT,
        repo_denylist=[],
        clone_root=str(tmp_path),
        shallow_since_days=100,
        window_days=90,
        excluded_path_patterns=[],
        git_timeout_seconds=17,
        version="test",
        trim_max_lines=5000,
        trim_max_files=200,
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        max_failure_rate=0.2,
        dest=SimpleNamespace(bucket="dashboard-site"),
        dest_prefix="git-activity/data",
    )


def _mirror(tmp_path, name):
    # The minimum that looks like one of our bare mirrors to prune_orphans.
    d = tmp_path / f"{name}.git"
    d.mkdir()
    (d / "HEAD").write_text("ref: refs/heads/main\n")
    return d


def test_collect_prunes_only_against_the_complete_multi_page_enumeration(monkeypatch, tmp_path):
    """The ordering property the prune depends on: it consumes the whole
    multi-page listing. repo-60 (page 2) and repo-101 (page 3) must survive
    the prune -- an enumeration that had stopped at page 1 would have
    deleted them as orphans."""
    f = FakeForge([
        page(1),                                  # repo-1 .. repo-50
        page(forge.PAGE_SIZE + 1),                # repo-51 .. repo-100
        page(2 * forge.PAGE_SIZE + 1, count=1),   # repo-101
    ]).install(monkeypatch)

    for name in ("repo-1", "repo-60", "repo-101"):
        _mirror(tmp_path, name)
    _mirror(tmp_path, "orphan")

    scanned = []

    def fake_ensure(repo_, *args):
        scanned.append(repo_["name"])
        return f"/mirrors/{repo_['name']}.git", True

    monkeypatch.setattr(main.gitscan, "ensure_mirror", fake_ensure)
    monkeypatch.setattr(main.gitscan, "scan_commits", lambda *args: [])
    monkeypatch.setattr(main.beads, "read_events", lambda *args: [])

    repos, commits, events, stats = main._collect(_cfg(tmp_path), {})

    names = [r["name"] for r in repos]
    assert [c["params"]["page"] for c in f.calls] == [1, 2, 3]
    assert len(names) == 2 * forge.PAGE_SIZE + 1
    assert stats["repos_total"] == len(names)
    # Every enumerated repo on every page was processed.
    assert scanned == names
    assert stats["repos_scanned"] == len(names)
    assert stats["repos_failed"] == []
    # The prune saw the full listing: only the true orphan died, and every
    # later-page mirror is still on the PVC.
    assert stats["mirrors_pruned"] == ["orphan"]
    assert (tmp_path / "repo-60.git").exists()
    assert (tmp_path / "repo-101.git").exists()
    assert not (tmp_path / "orphan.git").exists()


def test_collect_successful_empty_enumeration_prunes_nothing(monkeypatch, tmp_path):
    """A successful enumeration returning zero repos is not accepted as
    proof the fleet shrank to zero: the cycle proceeds with an empty fleet
    and the prune refuses to run, so a listing fault cannot wipe the
    mirrors."""
    FakeForge([{"ok": True, "data": []}]).install(monkeypatch)
    _mirror(tmp_path, "survivor")

    repos, commits, events, stats = main._collect(_cfg(tmp_path), {})

    assert repos == []
    assert stats["repos_total"] == 0
    assert stats["mirrors_pruned"] == []
    assert (tmp_path / "survivor.git").exists()


def test_enumeration_error_fails_the_cycle_before_any_prune_or_publish(monkeypatch, tmp_path):
    """data-sources.md, Failure semantics: an enumeration failure fails the
    cycle before repo processing -- no mirrors are pruned and no objects are
    published. Proven end to end: the page-1 error propagates out of
    _run_cycle with the mirror untouched and S3 unwritten; the next poll
    retries enumeration."""
    FakeForge([503]).install(monkeypatch)
    _mirror(tmp_path, "survivor")
    s3 = FakeS3()

    with pytest.raises(requests.HTTPError):
        main._run_cycle(_cfg(tmp_path), s3, {})

    assert s3.puts == []
    assert s3.objects == {}
    assert (tmp_path / "survivor.git").exists()


def test_empty_enumeration_publishes_without_pruning_and_recovers_after_fix(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    bucket = cfg.dest.bucket
    prefix = cfg.dest_prefix
    cfg.forge_owner = "wrong-owner"
    _mirror(tmp_path, "repo-1")
    _mirror(tmp_path, "orphan")
    first_forge = FakeForge([{"ok": True, "data": []}]).install(monkeypatch)
    monkeypatch.setattr(
        main.gitscan,
        "ensure_mirror",
        lambda repo, *args: (str(tmp_path / f"{repo['name']}.git"), True),
    )
    monkeypatch.setattr(main.gitscan, "scan_commits", lambda *args: [])
    monkeypatch.setattr(main.beads, "read_events", lambda *args: [])
    nows = iter(("2026-09-23T12:00:00Z", "2026-09-23T12:01:00Z"))
    monkeypatch.setattr(main, "_now", lambda: next(nows))
    s3 = FakeS3()

    main._run_cycle(cfg, s3, {})

    first_pointer = json.loads(
        s3io.download_bytes(s3, bucket, f"{prefix}/current.json")
    )
    first_meta = json.loads(
        s3io.download_bytes(
            s3, bucket, f"{prefix}/{first_pointer['objects']['meta.json']}"
        )
    )
    assert len(s3.puts) == 9
    assert first_meta["repos_total"] == 0
    assert first_meta["repos_scanned"] == 0
    assert first_meta["mirrors_pruned"] == []
    assert first_forge.calls[0]["params"]["owner"] == "wrong-owner"
    assert (tmp_path / "repo-1.git").exists()
    assert (tmp_path / "orphan.git").exists()

    cfg.forge_owner = "fixed-owner"
    second_forge = FakeForge([
        {"ok": True, "data": [repo(1)]}
    ]).install(monkeypatch)

    main._run_cycle(cfg, s3, {})

    second_pointer = json.loads(
        s3io.download_bytes(s3, bucket, f"{prefix}/current.json")
    )
    second_meta = json.loads(
        s3io.download_bytes(
            s3, bucket, f"{prefix}/{second_pointer['objects']['meta.json']}"
        )
    )
    assert second_pointer["cycle_id"] != first_pointer["cycle_id"]
    assert second_forge.calls[0]["params"]["owner"] == "fixed-owner"
    assert second_meta["repos_total"] == 1
    assert second_meta["repos_scanned"] == 1
    assert second_meta["repos_failed"] == []
    assert second_meta["mirrors_pruned"] == ["orphan"]
    assert (tmp_path / "repo-1.git").exists()
    assert not (tmp_path / "orphan.git").exists()
