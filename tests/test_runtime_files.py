from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src import main


ROOT = Path(__file__).resolve().parent.parent


class StopAfterCycle:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, _timeout):
        return True


@pytest.fixture(autouse=True)
def reset_published():
    main._published.clear()
    yield
    main._published.clear()


@pytest.fixture
def runtime_env(monkeypatch):
    monkeypatch.delenv("FAMILIES_FILE", raising=False)
    monkeypatch.delenv("VERSION_FILE", raising=False)
    monkeypatch.setenv("FORGE_TOKEN", "forge-token")
    monkeypatch.setenv("DEST_S3_ENDPOINT", "https://s3.test")
    monkeypatch.setenv("DEST_S3_BUCKET", "activity-bucket")
    monkeypatch.setenv("DEST_S3_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("DEST_S3_SECRET_ACCESS_KEY", "secret-key")


def run_startup(monkeypatch, observed):
    stop = StopAfterCycle()

    def run_cycle(cfg, _s3, family_map):
        observed["config"] = cfg
        observed["family_map"] = family_map
        stop.set()

    monkeypatch.setattr(main.s3io, "client", lambda _dest: object())
    monkeypatch.setattr(main.s3io, "check_permissions", lambda *_args: None)
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main, "_serve_health", lambda _port: None)
    monkeypatch.setattr(main, "_run_cycle", run_cycle)
    monkeypatch.setattr(main, "threading", SimpleNamespace(Event=lambda: stop))
    main.main()


def test_relative_runtime_paths_resolve_from_process_working_directory(
    runtime_env, monkeypatch, tmp_path
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "VERSION").write_text("2.4.6\n", encoding="utf-8")
    (runtime / "families.yaml").write_text(
        "families:\n  platform:\n    - example\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAMILIES_FILE", "runtime/families.yaml")
    monkeypatch.setenv("VERSION_FILE", "runtime/VERSION")
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].version == "2.4.6"
    assert observed["family_map"] == {"example": "platform"}


def test_missing_runtime_files_start_in_documented_degraded_mode(
    runtime_env, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].version == "unknown"
    assert observed["family_map"] == {}


@pytest.mark.parametrize("contents", [b"\n", b"\xff"])
def test_invalid_version_file_starts_with_unknown_version(
    runtime_env, monkeypatch, tmp_path, contents
):
    (tmp_path / "VERSION").write_bytes(contents)
    (tmp_path / "families.yaml").write_text("families: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].version == "unknown"
    assert observed["family_map"] == {}


def test_invalid_families_file_fails_before_s3_and_health_startup(
    runtime_env, monkeypatch, tmp_path
):
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / "families.yaml").write_text("families: [unterminated", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    started = []

    monkeypatch.setattr(main.s3io, "client", lambda _dest: started.append("s3"))
    monkeypatch.setattr(main, "_serve_health", lambda _port: started.append("health"))

    with pytest.raises(yaml.YAMLError):
        main.main()

    assert started == []


def test_image_packages_default_runtime_files_at_its_workdir():
    instructions = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    workdir = next(
        line.split(maxsplit=1)[1]
        for line in instructions
        if line.startswith("WORKDIR ")
    )
    copied = set()
    for line in instructions:
        parts = line.split()
        if parts and parts[0] == "COPY":
            copied.update(Path(source).as_posix() for source in parts[1:-1])

    assert workdir == "/app"
    assert {"VERSION", "families.yaml"} <= copied
