"""The guard that decides whether a cycle is fit to publish.

Extracted as a pure predicate so the threshold behaviour is testable without
standing up a cycle. main._run_cycle applies exactly this rule.
"""
import pytest


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
