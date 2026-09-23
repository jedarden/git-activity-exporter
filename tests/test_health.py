import http.client
import logging
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src import main


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
    yield
    main._published.clear()


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


def test_endpoint_status_and_empty_payload_contract(health_server):
    for path, expected_status in (
        ("/health", 200),
        ("/ready", 503),
        ("/missing", 404),
        ("/ready/", 404),
        ("/ready?probe=1", 404),
    ):
        status, headers, body = _get(health_server, path)
        assert status == expected_status
        assert body == b""
        assert "Content-Type" not in headers

    main._published.set()
    status, headers, body = _get(health_server, "/ready")
    assert status == 200
    assert body == b""
    assert "Content-Type" not in headers


def test_readiness_transitions_across_failures_withholding_and_recovery(
    monkeypatch, health_server
):
    outcomes = iter(
        [
            RuntimeError("forge enumeration failed"),
            RuntimeError("withheld: repository failure rate exceeded limit"),
            None,
            RuntimeError("publication failed after readiness"),
            RuntimeError("withheld: repository failure rate exceeded limit"),
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
        dest=object(),
        health_port=8080,
        poll_interval_seconds=3600,
    )
    monkeypatch.setattr(main.config, "load", lambda: cfg)
    monkeypatch.setattr(main.families, "load", lambda _path: {})
    monkeypatch.setattr(main.s3io, "client", lambda _dest: object())
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
