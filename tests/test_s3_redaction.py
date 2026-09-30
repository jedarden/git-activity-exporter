import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, main, publish, s3io
from tests.fake_s3 import FakeS3


ACCESS_KEY = "s3-access-key-redaction-test-only"
SECRET_KEY = "s3-secret-key-redaction-test-only"


@pytest.fixture(autouse=True)
def reset_main_state():
    main._published.clear()
    main._reset_cycle_state()
    yield
    main._published.clear()
    main._reset_cycle_state()


def _endpoint():
    return config.S3Endpoint(
        endpoint_url="https://s3.example.test",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        bucket="activity-bucket",
        addressing_style="path",
        region="us-east-1",
    )


def _payloads():
    return [
        ("hourly.parquet", b"hourly", "application/octet-stream"),
        ("commits.parquet", b"commits", "application/octet-stream"),
        ("bead_events.parquet", b"events", "application/octet-stream"),
        ("meta.json", b"{}", "application/json"),
    ]


def test_client_creation_failure_redacts_both_s3_credentials(monkeypatch, caplog):
    def fail_client(*_args, **_kwargs):
        raise RuntimeError(f"provider echoed {ACCESS_KEY} and {SECRET_KEY}")

    monkeypatch.setattr(s3io.boto3, "client", fail_client)

    with caplog.at_level("ERROR"):
        with pytest.raises(s3io.S3ClientError) as raised:
            s3io.client(_endpoint())

    assert ACCESS_KEY not in str(raised.value)
    assert SECRET_KEY not in str(raised.value)
    assert ACCESS_KEY not in caplog.text
    assert SECRET_KEY not in caplog.text


def test_main_client_creation_failure_redacts_startup_stderr(monkeypatch, capsys):
    cfg = SimpleNamespace(
        log_level="ERROR",
        families_file="families.yaml",
        dest=_endpoint(),
    )

    monkeypatch.setattr(main.config, "load", lambda: cfg)
    monkeypatch.setattr(main.families, "load", lambda _path: {})
    monkeypatch.setattr(
        main.s3io,
        "client",
        lambda _dest: (_ for _ in ()).throw(
            RuntimeError(f"client failed with {ACCESS_KEY} {SECRET_KEY}")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        main.main()

    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert "S3 client creation failed" in stderr
    assert ACCESS_KEY not in stderr
    assert SECRET_KEY not in stderr


def test_main_publication_failure_redacts_log_and_health_state(monkeypatch, caplog):
    cfg = SimpleNamespace(
        log_level="ERROR",
        families_file="families.yaml",
        dest=_endpoint(),
        dest_prefix="exports/activity",
        health_port=8080,
        poll_interval_seconds=3600,
    )

    class StopAfterOneWait:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    monkeypatch.setattr(main.config, "load", lambda: cfg)
    monkeypatch.setattr(main.families, "load", lambda _path: {})
    monkeypatch.setattr(main.s3io, "client", lambda _dest: object())
    monkeypatch.setattr(main.s3io, "check_permissions", lambda *_args: None)
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main, "_serve_health", lambda _port: None)
    monkeypatch.setattr(main.threading, "Event", StopAfterOneWait)
    monkeypatch.setattr(
        main,
        "_run_cycle",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError(f"publication failed with {ACCESS_KEY} {SECRET_KEY}")
        ),
    )

    with caplog.at_level("ERROR"):
        main.main()

    assert "cycle failed" in caplog.text
    assert ACCESS_KEY not in caplog.text
    assert SECRET_KEY not in caplog.text
    assert main._health_snapshot()["last_cycle_outcome"] == "failed"


def test_publication_failure_redacts_provider_exception_and_safe_cause(monkeypatch):
    s3 = FakeS3()
    s3.fail_when(
        lambda op, _key: RuntimeError(f"S3 request contained {ACCESS_KEY} {SECRET_KEY}")
        if op == "put" else None
    )
    monkeypatch.setattr(s3io.retry.time, "sleep", lambda _seconds: None)

    with pytest.raises(publish.PublicationError) as raised:
        publish.publish_cycle(
            s3,
            "activity-bucket",
            "exports/activity",
            _payloads(),
            cycle_id="20260927T120000Z-01234567",
            generated_at="2026-09-27T12:00:00Z",
        )

    texts = [str(raised.value), str(raised.value.__cause__)]
    assert all(ACCESS_KEY not in text and SECRET_KEY not in text for text in texts)
    assert "staging" in str(raised.value)
    assert "S3OperationError" in str(raised.value)


def test_health_response_contains_no_s3_error_or_credential_text():
    main._record_cycle_outcome("failed")
    snapshot = json.dumps(main._health_snapshot())

    assert ACCESS_KEY not in snapshot
    assert SECRET_KEY not in snapshot
    assert set(json.loads(snapshot)) == {
        "last_successful_cycle_at", "last_cycle_outcome",
        "last_successful_repos_partial_history", "prune",
    }


def test_smoke_scripts_keep_s3_values_out_of_subprocess_arguments():
    root = Path(__file__).resolve().parent.parent
    container = (root / "scripts" / "smoke-container.sh").read_text()
    self_hosting = (root / "scripts" / "smoke-self-hosting.sh").read_text()
    compose = (root / "examples" / "self-hosting" / "compose.yaml").read_text()

    assert "--env-file \"$SMOKE_ENV\"" in container
    assert "--env DEST_S3_ACCESS_KEY_ID=" not in container
    assert "--env DEST_S3_SECRET_ACCESS_KEY=" not in container
    assert "--env-file \"$SMOKE_ENV\"" in self_hosting
    assert "DEST_S3_ACCESS_KEY_ID: ${SMOKE_S3_ACCESS_KEY_ID}" in compose
    assert "DEST_S3_SECRET_ACCESS_KEY: ${SMOKE_S3_SECRET_ACCESS_KEY}" in compose
    assert "DEST_S3_ACCESS_KEY_ID: self-hosting-access-key" not in compose
    assert "DEST_S3_SECRET_ACCESS_KEY: self-hosting-secret-key" not in compose
