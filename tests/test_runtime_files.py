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


def test_absolute_runtime_paths_are_independent_of_process_working_directory(
    runtime_env, monkeypatch, tmp_path
):
    mounted = tmp_path / "mounted"
    other_workdir = tmp_path / "elsewhere"
    mounted.mkdir()
    other_workdir.mkdir()
    version_file = mounted / "VERSION"
    families_file = mounted / "families.yaml"
    version_file.write_text("9.8.7\n", encoding="utf-8")
    families_file.write_text(
        "families:\n  absolute:\n    - example\n", encoding="utf-8"
    )
    monkeypatch.chdir(other_workdir)
    monkeypatch.setenv("FAMILIES_FILE", str(families_file))
    monkeypatch.setenv("VERSION_FILE", str(version_file))
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].families_file == str(families_file)
    assert observed["config"].version == "9.8.7"
    assert observed["family_map"] == {"example": "absolute"}


def test_packaged_defaults_load_from_image_working_directory(
    runtime_env, monkeypatch, tmp_path
):
    # Model the image's /app directory separately from the source checkout.
    # This proves that the defaults are opened from the process working
    # directory rather than from the caller's module location.
    image_app = tmp_path / "app"
    image_app.mkdir()
    (image_app / "VERSION").write_bytes((ROOT / "VERSION").read_bytes())
    (image_app / "families.yaml").write_bytes((ROOT / "families.yaml").read_bytes())
    monkeypatch.chdir(image_app)
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].version == (ROOT / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    assert observed["family_map"]["commitgraph"] == "commitgraph"


def test_missing_runtime_files_start_in_documented_degraded_mode(
    runtime_env, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    observed = {}

    run_startup(monkeypatch, observed)

    assert observed["config"].version == "unknown"
    assert observed["family_map"] == {}


def test_unreadable_families_file_fails_before_s3_and_health_startup(
    runtime_env, monkeypatch, tmp_path
):
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    unreadable = tmp_path / "families-directory"
    unreadable.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAMILIES_FILE", str(unreadable))
    started = []

    monkeypatch.setattr(main.s3io, "client", lambda _dest: started.append("s3"))
    monkeypatch.setattr(main, "_serve_health", lambda _port: started.append("health"))

    with pytest.raises(IsADirectoryError, match=str(unreadable)):
        main.main()

    assert started == []


def test_unreadable_version_file_degrades_to_unknown_version(
    runtime_env, monkeypatch, tmp_path
):
    (tmp_path / "families.yaml").write_text("families: {}\n", encoding="utf-8")
    unreadable = tmp_path / "VERSION-directory"
    unreadable.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERSION_FILE", str(unreadable))
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
