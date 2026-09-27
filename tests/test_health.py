import http.client
import json
import logging
import re
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src import main


_METRIC_CATALOG = {
    "git_activity_exporter_up": {
        "help": "Process health; this endpoint is live.",
        "type": "gauge",
        "labels": [frozenset()],
    },
    "git_activity_exporter_process_start_time_seconds": {
        "help": "Unix start time.",
        "type": "gauge",
        "labels": [frozenset()],
    },
    "git_activity_exporter_poll_interval_seconds": {
        "help": "Configured post-cycle sleep.",
        "type": "gauge",
        "labels": [frozenset()],
    },
    "git_activity_exporter_last_successful_publication_timestamp_seconds": {
        "help": "Unix timestamp of the latest committed publication, or 0 before the first one.",
        "type": "gauge",
        "labels": [frozenset()],
    },
    "git_activity_exporter_cycle_attempts_total": {
        "help": "Cycle attempts by terminal outcome.",
        "type": "counter",
        "labels": [
            frozenset({("outcome", outcome)})
            for outcome in ("published", "withheld", "failed")
        ],
    },
    "git_activity_exporter_last_cycle_outcome": {
        "help": "Current terminal outcome, one for the current outcome.",
        "type": "gauge",
        "labels": [
            frozenset({("outcome", outcome)})
            for outcome in ("published", "withheld", "failed")
        ],
    },
    "git_activity_exporter_publication_failures_total": {
        "help": "Failed publication attempts since process start.",
        "type": "counter",
        "labels": [frozenset()],
    },
    "git_activity_exporter_publication_failures_consecutive": {
        "help": "Consecutive failed publication attempts.",
        "type": "gauge",
        "labels": [frozenset()],
    },
    "git_activity_exporter_prune_consecutive_failures": {
        "help": "Consecutive failed post-publication prune attempts.",
        "type": "gauge",
        "labels": [frozenset()],
    },
}


def _parse_prometheus_text(text):
    """Parse the subset of the Prometheus text format emitted by /metrics."""
    families = {}
    samples = {}
    label_pattern = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')
    sample_pattern = re.compile(
        r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
        r"(?:\{(?P<labels>[^}]*)\})?\s+"
        r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]Inf)$"
    )

    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            name, separator, help_text = line[7:].partition(" ")
            assert separator, f"malformed HELP line: {line!r}"
            assert name not in families or "help" not in families[name]
            families.setdefault(name, {})["help"] = help_text
            continue
        if line.startswith("# TYPE "):
            name, separator, metric_type = line[7:].partition(" ")
            assert separator, f"malformed TYPE line: {line!r}"
            assert name not in families or "type" not in families[name]
            families.setdefault(name, {})["type"] = metric_type
            continue

        match = sample_pattern.fullmatch(line)
        assert match, f"malformed sample line: {line!r}"
        labels = {}
        serialized_labels = match.group("labels")
        if serialized_labels:
            pieces = serialized_labels.split(",")
            for piece in pieces:
                label_match = label_pattern.fullmatch(piece)
                assert label_match, f"malformed label set: {serialized_labels!r}"
                label_name, label_value = label_match.groups()
                assert label_name not in labels
                labels[label_name] = label_value
        key = (match.group("name"), frozenset(labels.items()))
        assert key not in samples, f"duplicate sample: {key!r}"
        samples[key] = float(match.group("value"))

    return families, samples


def _metrics_snapshot(server):
    status, headers, body = _get(server, "/metrics")
    assert status == 200
    assert headers["Content-Type"] == "text/plain; version=0.0.4"
    return _parse_prometheus_text(body.decode())


def _metric_value(snapshot, name, labels=None):
    labels = frozenset((labels or {}).items())
    return snapshot[1][(name, labels)]


def _get(server, path):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def _probe(server):
    health_status, _, _ = _get(server, "/health")
    ready_status, _, _ = _get(server, "/ready")
    return health_status, ready_status


@pytest.fixture(autouse=True)
def reset_published():
    main._published.clear()
    main._reset_cycle_state()
    yield
    main._published.clear()
    main._reset_cycle_state()


@pytest.fixture
def health_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), main._HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_endpoint_status_and_payload_contract(health_server):
    for path, expected_status in (
        ("/ready", 503),
        ("/missing", 404),
        ("/ready/", 404),
        ("/ready?probe=1", 404),
    ):
        status, headers, body = _get(health_server, path)
        assert status == expected_status
        assert body == b""
        assert "Content-Type" not in headers

    status, headers, body = _get(health_server, "/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "last_successful_cycle_at": None,
        "last_cycle_outcome": None,
        "prune": {
            "last_outcome": None,
            "failures_total": 0,
            "consecutive_failures": 0,
            "last_failure_cycle_id": None,
        },
    }

    main._published.set()
    status, headers, body = _get(health_server, "/ready")
    assert status == 200
    assert body == b""
    assert "Content-Type" not in headers


def test_health_reports_last_success_and_current_outcome(health_server):
    main._record_cycle_outcome("published", "2026-09-27T12:00:00Z")
    status, _, body = _get(health_server, "/health")
    assert status == 200
    assert json.loads(body) == {
        "last_successful_cycle_at": "2026-09-27T12:00:00Z",
        "last_cycle_outcome": "published",
        "prune": {
            "last_outcome": None,
            "failures_total": 0,
            "consecutive_failures": 0,
            "last_failure_cycle_id": None,
        },
    }

    main._record_cycle_outcome("withheld")
    status, _, body = _get(health_server, "/health")
    assert status == 200
    assert json.loads(body) == {
        "last_successful_cycle_at": "2026-09-27T12:00:00Z",
        "last_cycle_outcome": "withheld",
        "prune": {
            "last_outcome": None,
            "failures_total": 0,
            "consecutive_failures": 0,
            "last_failure_cycle_id": None,
        },
    }

    main._record_cycle_outcome("failed")
    status, _, body = _get(health_server, "/health")
    assert status == 200
    assert json.loads(body) == {
        "last_successful_cycle_at": "2026-09-27T12:00:00Z",
        "last_cycle_outcome": "failed",
        "prune": {
            "last_outcome": None,
            "failures_total": 0,
            "consecutive_failures": 0,
            "last_failure_cycle_id": None,
        },
    }


def test_metrics_expose_freshness_outcomes_and_failure_streaks(health_server):
    main._set_poll_interval_seconds(120)
    main._record_cycle_outcome("withheld")
    main._record_cycle_outcome("failed")
    main._record_publication_failure()
    main._record_publication_failure()

    status, headers, body = _get(health_server, "/metrics")

    assert status == 200
    assert headers["Content-Type"] == "text/plain; version=0.0.4"
    text = body.decode()
    assert "git_activity_exporter_last_successful_publication_timestamp_seconds 0.000" in text
    assert "git_activity_exporter_poll_interval_seconds 120" in text
    assert 'git_activity_exporter_cycle_attempts_total{outcome="withheld"} 1' in text
    assert 'git_activity_exporter_cycle_attempts_total{outcome="failed"} 1' in text
    assert 'git_activity_exporter_last_cycle_outcome{outcome="failed"} 1' in text
    assert "git_activity_exporter_publication_failures_total 2" in text
    assert "git_activity_exporter_publication_failures_consecutive 2" in text
    assert "git_activity_exporter_prune_consecutive_failures 0" in text


def test_metrics_catalog_and_cycle_state_transitions(health_server):
    main._set_poll_interval_seconds(120)

    families, samples = _metrics_snapshot(health_server)
    assert set(families) == set(_METRIC_CATALOG)
    assert {name for name, _labels in samples} == set(_METRIC_CATALOG)
    for name, expected in _METRIC_CATALOG.items():
        assert families[name] == {
            "help": expected["help"],
            "type": expected["type"],
        }
        actual_labels = {
            labels for metric_name, labels in samples if metric_name == name
        }
        assert actual_labels == set(expected["labels"])

    assert _metric_value((families, samples), "git_activity_exporter_up") == 1
    assert _metric_value(
        (families, samples), "git_activity_exporter_process_start_time_seconds"
    ) > 0
    assert _metric_value(
        (families, samples), "git_activity_exporter_poll_interval_seconds"
    ) == 120
    assert _metric_value(
        (families, samples),
        "git_activity_exporter_last_successful_publication_timestamp_seconds",
    ) == 0
    for outcome in ("published", "withheld", "failed"):
        assert _metric_value(
            (families, samples),
            "git_activity_exporter_cycle_attempts_total",
            {"outcome": outcome},
        ) == 0
        assert _metric_value(
            (families, samples),
            "git_activity_exporter_last_cycle_outcome",
            {"outcome": outcome},
        ) == 0

    def assert_outcome(snapshot, expected_outcome, expected_attempts):
        for outcome, expected in expected_attempts.items():
            assert _metric_value(
                snapshot,
                "git_activity_exporter_cycle_attempts_total",
                {"outcome": outcome},
            ) == expected
            assert _metric_value(
                snapshot,
                "git_activity_exporter_last_cycle_outcome",
                {"outcome": outcome},
            ) == int(outcome == expected_outcome)

    # A successful publication establishes freshness and the published
    # one-hot outcome.
    main._record_cycle_outcome("published", "2026-09-27T12:00:00Z")
    snapshot = _metrics_snapshot(health_server)
    assert _metric_value(
        snapshot,
        "git_activity_exporter_last_successful_publication_timestamp_seconds",
    ) == 1790510400.0
    assert_outcome(
        snapshot,
        "published",
        {"published": 1, "withheld": 0, "failed": 0},
    )
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_total"
    ) == 0
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_consecutive"
    ) == 0
    assert _metric_value(
        snapshot, "git_activity_exporter_prune_consecutive_failures"
    ) == 0

    # A MAX_FAILURE_RATE-withheld cycle counts as an attempt but cannot move
    # freshness or the current publication failure streak.
    main._record_cycle_outcome("withheld")
    snapshot = _metrics_snapshot(health_server)
    assert _metric_value(
        snapshot,
        "git_activity_exporter_last_successful_publication_timestamp_seconds",
    ) == 1790510400.0
    assert_outcome(
        snapshot,
        "withheld",
        {"published": 1, "withheld": 1, "failed": 0},
    )
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_total"
    ) == 0
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_consecutive"
    ) == 0

    # The main-loop publication-failure path records the failed cycle and the
    # PublicationError separately: the attempt changes, and both failure
    # counters advance, while the last successful timestamp stays pinned.
    main._record_cycle_outcome("failed")
    main._record_publication_failure()
    snapshot = _metrics_snapshot(health_server)
    assert _metric_value(
        snapshot,
        "git_activity_exporter_last_successful_publication_timestamp_seconds",
    ) == 1790510400.0
    assert_outcome(
        snapshot,
        "failed",
        {"published": 1, "withheld": 1, "failed": 1},
    )
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_total"
    ) == 1
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_consecutive"
    ) == 1

    # Pruning runs after the pointer commit. A failed cleanup therefore
    # leaves the cycle published, resets the publication-failure streak, and
    # raises only the prune streak.
    main.publish._record_prune_health(
        "20260927T130000Z-aaaa1111",
        main.publish.PruneReport("failed", ("20260926T130000Z-bbbb2222",)),
    )
    main._record_cycle_outcome("published", "2026-09-27T13:00:00Z")
    snapshot = _metrics_snapshot(health_server)
    assert _metric_value(
        snapshot,
        "git_activity_exporter_last_successful_publication_timestamp_seconds",
    ) == 1790514000.0
    assert_outcome(
        snapshot,
        "published",
        {"published": 2, "withheld": 1, "failed": 1},
    )
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_total"
    ) == 1
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_consecutive"
    ) == 0
    assert _metric_value(
        snapshot, "git_activity_exporter_prune_consecutive_failures"
    ) == 1

    # A later successful prune clears only its consecutive gauge; the failure
    # is not retroactively removed from the publication-failure counters.
    main.publish._record_prune_health(
        "20260927T140000Z-cccc3333", main.publish.PruneReport("succeeded")
    )
    main._record_cycle_outcome("published", "2026-09-27T14:00:00Z")
    snapshot = _metrics_snapshot(health_server)
    assert _metric_value(
        snapshot, "git_activity_exporter_prune_consecutive_failures"
    ) == 0
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_total"
    ) == 1
    assert _metric_value(
        snapshot, "git_activity_exporter_publication_failures_consecutive"
    ) == 0


def test_successful_publication_resets_publication_failure_streak(health_server):
    main._record_publication_failure()
    main._record_cycle_outcome("published", "2026-09-27T12:00:00Z")

    _, _, body = _get(health_server, "/metrics")
    text = body.decode()
    assert "git_activity_exporter_publication_failures_total 1" in text
    assert "git_activity_exporter_publication_failures_consecutive 0" in text


def test_readiness_transitions_across_failures_withholding_and_recovery(
    monkeypatch, health_server
):
    outcomes = iter(
        [
            RuntimeError("forge enumeration failed"),
            main.CycleWithheld("repository failure rate exceeded limit"),
            None,
            RuntimeError("publication failed after readiness"),
            main.CycleWithheld("repository failure rate exceeded limit"),
            None,
        ]
    )
    observed = [_probe(health_server)]
    cycle_count = 6

    class LoopStop:
        def __init__(self):
            self.stopped = False
            self.waits = 0

        def is_set(self):
            return self.stopped or self.waits == cycle_count

        def set(self):
            self.stopped = True

        def wait(self, _timeout):
            observed.append(_probe(health_server))
            self.waits += 1

    def run_cycle(*_args):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    cfg = SimpleNamespace(
        log_level=logging.WARNING,
        families_file="families.yaml",
        dest=SimpleNamespace(bucket="activity-bucket"),
        dest_prefix="exports/activity",
        health_port=8080,
        poll_interval_seconds=3600,
    )
    monkeypatch.setattr(main.config, "load", lambda: cfg)
    monkeypatch.setattr(main.families, "load", lambda _path: {})
    monkeypatch.setattr(main.s3io, "client", lambda _dest: object())
    monkeypatch.setattr(main.s3io, "check_permissions", lambda *_args: None)
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main, "_serve_health", lambda _port: None)
    monkeypatch.setattr(main, "_run_cycle", run_cycle)
    monkeypatch.setattr(main, "threading", SimpleNamespace(Event=LoopStop))

    main.main()

    assert observed == [
        (200, 503),
        (200, 503),
        (200, 503),
        (200, 200),
        (200, 200),
        (200, 200),
        (200, 200),
    ]
