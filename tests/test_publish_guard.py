"""The guard that decides whether a cycle is fit to publish.

Extracted as a pure predicate so the threshold behaviour is testable without
standing up a cycle. main._run_cycle applies exactly this rule.
"""
import json
from types import SimpleNamespace

import pytest

from src import main, s3io
from tests.fake_s3 import FakeS3


def should_publish(total_repos: int, failed: int, max_failure_rate: float) -> bool:
    if not total_repos:
        return True
    return (failed / total_repos) <= max_failure_rate


def test_healthy_cycle_publishes():
    assert should_publish(112, 0, 0.2)
    assert should_publish(112, 3, 0.2), "a few slow clones must not withhold a cycle"


def test_revoked_credential_is_withheld():
    # The real incident: rotating the Forgejo token failed 97 of 112 repos,
    # but the 15 public ones still cloned anonymously. A total-failure guard
    # stayed quiet and let a 6,588-cell dataset be replaced by a 1,580-cell
    # one. This is the case the fraction exists for.
    assert not should_publish(112, 97, 0.2)


def test_total_failure_is_withheld():
    assert not should_publish(112, 112, 0.2)


def test_boundary_is_inclusive():
    # Exactly at the limit still publishes; one more repo does not.
    assert should_publish(100, 20, 0.2)
    assert not should_publish(100, 21, 0.2)


@pytest.mark.parametrize("failed", [1, 5, 22])
def test_partial_failures_scale_with_the_fleet(failed):
    # The rule is a fraction, not a count, so it keeps meaning as the number
    # of repos grows.
    assert should_publish(1000, failed, 0.2)


BUCKET = "dashboard-site"
PREFIX = "git-activity/safety"


def _cycle_cfg(max_failure_rate):
    return SimpleNamespace(
        version="test",
        window_days=90,
        git_timeout_seconds=600,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=[],
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        max_failure_rate=max_failure_rate,
        dest=SimpleNamespace(bucket=BUCKET),
        dest_prefix=PREFIX,
    )


def _cycle_result(total, failed):
    repos = [
        {
            "name": f"repo-{index}",
            "full_name": f"owner/repo-{index}",
            "clone_url": f"https://forge/owner/repo-{index}.git",
        }
        for index in range(total)
    ]
    failed_names = [repo["name"] for repo in repos[-failed:]] if failed else []
    return repos, [], [], {
        "repos_total": total,
        "repos_scanned": total - failed,
        "repos_failed": failed_names,
        "repo_errors": {name: "authentication failed" for name in failed_names},
        "repos_stale": [],
        "mirrors_pruned": [],
        "repos_with_bead_data": 0,
    }


def _meta_for_pointer(s3, pointer):
    key = pointer["objects"]["meta.json"]
    return json.loads(s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{key}"))


@pytest.mark.parametrize(
    ("failed", "published"),
    [(1, True), (2, True), (3, False)],
    ids=["below", "exactly-at", "above"],
)
def test_run_cycle_failure_rate_boundaries(monkeypatch, failed, published):
    cfg = _cycle_cfg(max_failure_rate=0.4)
    s3 = FakeS3()
    results = iter((_cycle_result(5, 0), _cycle_result(5, failed)))
    nows = iter(("2026-09-23T12:00:00Z", "2026-09-23T12:01:00Z"))
    monkeypatch.setattr(main, "_collect", lambda *_: next(results))
    monkeypatch.setattr(main, "_now", lambda: next(nows))

    main._run_cycle(cfg, s3, {})
    previous_pointer = s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json")
    previous_objects = dict(s3.objects)
    previous_puts = list(s3.puts)

    if published:
        main._run_cycle(cfg, s3, {})
        pointer_bytes = s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json")
        pointer = json.loads(pointer_bytes)
        assert pointer_bytes != previous_pointer
        assert _meta_for_pointer(s3, pointer)["repos_failed"] == [
            f"repo-{index}" for index in range(5 - failed, 5)
        ]
    else:
        with pytest.raises(RuntimeError, match=rf"{failed}/5 repo"):
            main._run_cycle(cfg, s3, {})
        assert s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json") == previous_pointer
        assert s3.objects == previous_objects
        assert s3.puts == previous_puts
