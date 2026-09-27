import pytest

from src import retry


def test_transient_failure_uses_three_attempts_and_exponential_backoff(monkeypatch):
    calls = []
    sleeps = []

    def operation():
        calls.append(len(calls) + 1)
        if len(calls) < retry.MAX_ATTEMPTS:
            raise RuntimeError("temporary")
        return "ok"

    monkeypatch.setattr(retry.time, "sleep", sleeps.append)

    result = retry.call(operation, is_retryable=lambda error: True, label="fixture")

    assert result == "ok"
    assert calls == [1, 2, 3]
    assert sleeps == [1, 2]


def test_non_transient_failure_is_fail_fast():
    calls = []

    def operation():
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        retry.call(operation, is_retryable=lambda error: False, label="fixture")

    assert len(calls) == 1
