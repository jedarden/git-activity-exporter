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
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from src import forge, gitscan, main, s3io
from tests.fake_s3 import FakeS3

BASE = "https://forge"
TOKEN = "test-token"
OWNER = "test-owner"
TIMEOUT = 30
FORGEJO_FIXTURES = Path(__file__).parent / "fixtures" / "forgejo"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    # Retry timing is tested directly in tests/test_retry.py. Keep API tests
    # quick while still asserting every attempt.
    monkeypatch.setattr(forge.retry.time, "sleep", lambda _seconds: None)


class FakeResponse:
    def __init__(self, payload, status=200, error_message=None):
        self.payload = payload
        self.status_code = status
        self.error_message = error_message

    def raise_for_status(self):
        if self.status_code >= 400:
            message = self.error_message or f"{self.status_code} error for repos/search"
            error = requests.HTTPError(message)
            error.response = self
            raise error

    def json(self):
        return self.payload


class MalformedJSONResponse(FakeResponse):
    def json(self):
        raise json.JSONDecodeError("malformed", "{", 1)


class ResponseSequence:
    """Responses for one page, consumed once per attempt."""

    def __init__(self, *responses):
        self.responses = list(responses)

    def next(self):
        return self.responses.pop(0)


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
                if isinstance(item, ResponseSequence):
                    item = item.next()
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, int):
                    return FakeResponse({"data": []}, status=item)
                if isinstance(item, FakeResponse):
                    return item
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


def forgejo_fixture(name):
    return json.loads((FORGEJO_FIXTURES / name).read_text())


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


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "JSON object"),
        ({}, "data field"),
        ({"data": {}}, "data field"),
        ({"data": ["repo"]}, "entry 0"),
        ({"data": [{"name": "repo-1"}]}, "full_name"),
        ({"data": [repo(1) | {"clone_url": None}]}, "clone_url"),
        ({"data": [repo(1) | {"empty": "false"}]}, "empty"),
        ({"data": [repo(1)], "ok": False}, "unsuccessful"),
    ],
)
def test_successful_response_schema_errors_fail_without_retry(monkeypatch, payload, message):
    f = FakeForge([payload]).install(monkeypatch)

    with pytest.raises(forge.EnumerationError, match=message):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert len(f.calls) == 1


@pytest.mark.parametrize(
    "clone_url",
    [
        "file:///tmp/secret-repo",
        "ssh://git@forge/test-owner/repo-1.git",
        "http://forge/test-owner/repo-1.git",
        "https://unexpected.example/test-owner/repo-1.git",
        "https://x-access-token:secret@forge/test-owner/repo-1.git",
    ],
)
def test_clone_url_must_use_the_configured_forgejo_origin(monkeypatch, clone_url):
    f = FakeForge([{"data": [repo(1) | {"clone_url": clone_url}]}]).install(monkeypatch)

    with pytest.raises(forge.EnumerationError, match="unsafe clone_url"):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert len(f.calls) == 1


def test_malformed_json_fails_without_retry(monkeypatch):
    f = FakeForge([MalformedJSONResponse(None)]).install(monkeypatch)

    with pytest.raises(forge.EnumerationError, match="malformed JSON"):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert len(f.calls) == 1


@pytest.mark.parametrize("duplicate_field", ["name", "full_name"])
def test_duplicate_repositories_fail_the_complete_walk(monkeypatch, duplicate_field):
    first = repo(1)
    second = repo(2)
    second[duplicate_field] = first[duplicate_field]
    f = FakeForge([{"data": [first, second]}]).install(monkeypatch)

    with pytest.raises(forge.EnumerationError, match="duplicate"):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert len(f.calls) == 1


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


def test_denylist_matching_is_exact_and_case_sensitive(monkeypatch):
    records = [
        repo(1),
        repo(1) | {
            "name": "repo-1-extra",
            "full_name": f"{OWNER}/repo-1-extra",
            "clone_url": f"{BASE}/{OWNER}/repo-1-extra.git",
        },
        repo(1) | {
            "name": "Repo-1",
            "full_name": f"{OWNER}/Repo-1",
            "clone_url": f"{BASE}/{OWNER}/Repo-1.git",
        },
    ]

    out, _ = list_repos_with_pages(
        monkeypatch,
        [{"ok": True, "data": records}],
        denylist=["repo-1"],
    )

    assert [record["name"] for record in out] == ["repo-1-extra", "Repo-1"]


def test_denylisting_every_repo_does_not_make_prune_delete_mirrors(monkeypatch, tmp_path):
    """An all-denylisted result is not permission to treat the fleet as gone."""
    FakeForge([{"ok": True, "data": [repo(1)]}]).install(monkeypatch)
    _mirror(tmp_path, "repo-1")
    _mirror(tmp_path, "survivor")

    cfg = _cfg(tmp_path)
    cfg.repo_denylist = ["repo-1"]

    repos, commits, events, stats = main._collect(cfg, {})

    assert repos == []
    assert commits == []
    assert events == []
    assert stats["repos_total"] == 0
    assert stats["mirrors_pruned"] == []
    assert (tmp_path / "repo-1.git").exists()
    assert (tmp_path / "survivor.git").exists()


# --- visibility and empty-repository behavior ------------------------------


def test_private_repos_are_enumerated_like_any_other(monkeypatch):
    # Visibility is decided server-side by what FORGE_TOKEN may see; the
    # exporter applies no client-side visibility filter.
    out, _ = list_repos_with_pages(
        monkeypatch, [{"ok": True, "data": [repo(1, private=True), repo(2)]}]
    )
    assert [r["name"] for r in out] == ["repo-1", "repo-2"]


def test_mixed_visibility_fixture_retains_every_owner_repository(monkeypatch):
    # Forgejo applies the token's visibility rules before returning these
    # pages. The exporter must retain both public and private repositories
    # rather than applying a second visibility filter of its own.
    pages = forgejo_fixture("mixed_visibility.json")
    listed = [repository for page_ in pages for repository in page_["data"]]
    expected = [
        {
            field: repository[field]
            for field in ("name", "full_name", "clone_url")
        }
        for repository in listed
    ]

    assert {repository["private"] for repository in listed} == {False, True}
    assert all(
        repository["full_name"].startswith(f"{OWNER}/")
        for repository in listed
    )

    out, fake = list_repos_with_pages(monkeypatch, pages)

    assert out == expected
    assert [call["params"]["page"] for call in fake.calls] == [1]


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
    assert [c["params"]["page"] for c in f.calls] == [1, 2, 2, 2]


def test_http_5xx_succeeds_on_a_later_attempt(monkeypatch):
    f = FakeForge([ResponseSequence(503, 503, page(1, count=1))]).install(monkeypatch)

    out = forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert [c["params"]["page"] for c in f.calls] == [1, 1, 1]
    assert {c["timeout"] for c in f.calls} == {TIMEOUT}
    assert [r["name"] for r in out] == ["repo-1"]


def test_client_error_is_not_retried(monkeypatch):
    f = FakeForge([401]).install(monkeypatch)

    with pytest.raises(requests.HTTPError):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert len(f.calls) == 1


def test_authentication_error_redacts_token_from_exception(monkeypatch, caplog):
    response = FakeResponse(None, status=401, error_message=f"credential rejected: {TOKEN}")
    f = FakeForge([response]).install(monkeypatch)

    with pytest.raises(requests.HTTPError) as raised:
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert TOKEN not in str(raised.value)
    assert raised.value.response.status_code == 401
    assert raised.value.response.request is None
    assert TOKEN not in caplog.text
    assert len(f.calls) == 1


def test_retry_log_and_final_exception_redact_token(monkeypatch, caplog):
    response = requests.Response()
    response.status_code = 503
    error = requests.HTTPError(f"temporary Forgejo failure echoed {TOKEN}", response=response)
    f = FakeForge([error]).install(monkeypatch)
    caplog.set_level("WARNING", logger="src.retry")

    with pytest.raises(requests.HTTPError) as raised:
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)

    assert TOKEN not in str(raised.value)
    assert TOKEN not in caplog.text
    assert "<redacted>" in caplog.text
    assert len(f.calls) == forge.retry.MAX_ATTEMPTS


def test_transport_error_fails_the_walk(monkeypatch):
    f = FakeForge([requests.ConnectionError("connection refused")]).install(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        forge.list_repos(BASE, TOKEN, OWNER, TIMEOUT)
    assert len(f.calls) == forge.retry.MAX_ATTEMPTS


# --- enumeration completeness vs. pruning ----------------------------------


def _cfg(tmp_path):
    # Superset covering both _collect and _run_cycle.
    (tmp_path / gitscan.CLONE_ROOT_MARKER).write_text(
        gitscan.CLONE_ROOT_MARKER_CONTENT
    )
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


def _seed_previous_publication(s3, prefix):
    generated_at = "2026-09-29T12:00:00Z"
    cycle_id = "20260929T120000Z-01234567"
    names = main.publish.DEFAULT_FIXED_NAMES
    for name in names:
        if name == "meta.json":
            data = json.dumps({
                "cycle_id": cycle_id,
                "generated_at": generated_at,
            }).encode()
            content_type = "application/json"
        else:
            data = f"previous:{name}".encode()
            content_type = "application/octet-stream"
        s3.objects[f"{prefix}/cycles/{cycle_id}/{name}"] = (data, content_type)
        s3.objects[f"{prefix}/{name}"] = (data, content_type)
    s3.objects[f"{prefix}/current.json"] = (
        main.publish.pointer_bytes(cycle_id, generated_at, names),
        "application/json",
    )


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


def test_authentication_failure_preserves_mirrors_publication_and_readiness(
    monkeypatch, tmp_path, caplog
):
    """A revoked discovery credential fails before mirror pruning or publish."""
    cfg = _cfg(tmp_path)
    existing = _mirror(tmp_path, "repo-1")
    orphan = _mirror(tmp_path, "orphan")
    response = FakeResponse(
        None, status=401, error_message=f"authentication failed: {TOKEN}"
    )
    f = FakeForge([response]).install(monkeypatch)
    s3 = FakeS3()
    _seed_previous_publication(s3, cfg.dest_prefix)
    publication_before = dict(s3.objects)
    health_before = main._health_snapshot()
    published_before = main._published.is_set()
    main._published.set()

    try:
        with pytest.raises(requests.HTTPError) as raised:
            main._run_cycle(cfg, s3, {})

        assert TOKEN not in str(raised.value)
        assert TOKEN not in caplog.text
        assert len(f.calls) == 1
        assert existing.is_dir()
        assert orphan.is_dir()
        assert s3.objects == publication_before
        assert s3.puts == []
        assert main._published.is_set()
        assert main._health_snapshot() == health_before
    finally:
        if not published_before:
            main._published.clear()


@pytest.mark.parametrize(
    "response",
    [
        MalformedJSONResponse(None),
        [],
        {"data": [{"name": "repo-1"}]},
        {"data": [repo(1), repo(1)]},
        {"data": [repo(1) | {"clone_url": "file:///tmp/unsafe.git"}]},
        {"data": [repo(1) | {"empty": "false"}]},
        {"data": [repo(1)], "ok": False},
    ],
)
def test_invalid_enumeration_response_cannot_mutate_cycle_state(
    monkeypatch, tmp_path, response
):
    """Validation must finish before any destructive or publishing work.

    Keep an actual orphan mirror in place instead of mocking the prune: if an
    invalid response ever reached orphan hygiene, this test would delete it.
    The subprocess spy covers clone, fetch, and scan Git invocations, while
    FakeS3 records all publication writes.
    """
    FakeForge([response]).install(monkeypatch)
    cfg = _cfg(tmp_path)
    _mirror(tmp_path, "survivor")
    _mirror(tmp_path, "orphan")
    s3 = FakeS3()

    mirror_calls = []

    def unexpected_mirror(*args, **kwargs):
        mirror_calls.append(args[0])
        raise AssertionError("invalid enumeration reached mirror refresh")

    monkeypatch.setattr(main.gitscan, "ensure_mirror", unexpected_mirror)

    git_calls = []

    def unexpected_git(*args, **kwargs):
        git_calls.append(args[0])
        raise AssertionError("invalid enumeration reached a Git operation")

    monkeypatch.setattr(
        main.gitscan,
        "_run",
        unexpected_git,
    )

    scan_calls = []

    def unexpected_scan(*args, **kwargs):
        scan_calls.append(args[0])
        raise AssertionError("invalid enumeration reached mirror scanning")

    monkeypatch.setattr(main.gitscan, "scan_commits", unexpected_scan)

    prune_calls = []
    real_prune = main.gitscan.prune_orphans

    def record_prune(*args, **kwargs):
        prune_calls.append(args)
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(main.gitscan, "prune_orphans", record_prune)

    deleted = []
    real_rmtree = main.gitscan.shutil.rmtree

    def record_deletion(path, *args, **kwargs):
        deleted.append(path)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(main.gitscan.shutil, "rmtree", record_deletion)

    with pytest.raises(forge.EnumerationError):
        main._run_cycle(cfg, s3, {})

    assert mirror_calls == []
    assert git_calls == []
    assert scan_calls == []
    assert prune_calls == []
    assert deleted == []
    assert s3.puts == []
    assert s3.objects == {}
    assert (tmp_path / "survivor.git").exists()
    assert (tmp_path / "orphan.git").exists()


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
