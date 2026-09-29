import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check-release-drift.py"
VERSIONED_FILES = (
    Path("VERSION"),
    Path("examples/self-hosting/compose.yaml"),
    Path("examples/self-hosting/kubernetes.yaml"),
    Path("docs/self-hosting.md"),
    Path("docs/notes/deployment.md"),
)
PINNED_IMAGE_FILES = (
    Path("Dockerfile"),
    Path("examples"),
    Path("tests/fixtures/git-activity-exporter-workflow.yml"),
)
REQUIREMENT_FILES = (
    Path("requirements.txt"),
    Path("requirements-dev.txt"),
)


def _copy_release_files(root: Path) -> None:
    for relative_path in (*VERSIONED_FILES, *PINNED_IMAGE_FILES, *REQUIREMENT_FILES):
        destination = root / relative_path
        source = ROOT / relative_path
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_drift_check_passes_for_committed_self_hosting_references():
    result = _run(ROOT)

    assert result.returncode == 0, result.stderr
    assert "release image references and reproducibility pins match VERSION" in result.stdout


def test_release_drift_check_rejects_stale_version_and_can_rewrite_it(tmp_path):
    _copy_release_files(tmp_path)
    (tmp_path / "VERSION").write_text("9.9.9\n")

    stale = _run(tmp_path)

    assert stale.returncode == 1
    assert "does not match VERSION '9.9.9'" in stale.stderr

    rewritten = _run(tmp_path, "--write")

    assert rewritten.returncode == 0, rewritten.stderr
    assert "updated examples/self-hosting/compose.yaml" in rewritten.stdout
    assert "match VERSION 9.9.9" in rewritten.stdout


def test_release_drift_check_rejects_latest_in_compose_image(tmp_path):
    _copy_release_files(tmp_path)
    version = (tmp_path / "VERSION").read_text().strip()
    compose = tmp_path / "examples" / "self-hosting" / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(f":{version}" + "}", ":latest}"),
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "examples/self-hosting/compose.yaml" in result.stderr
    assert "references :latest" in result.stderr


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "message"),
    (
        (
            Path("Dockerfile"),
            "FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f",
            "FROM python",
            "Dockerfile",
        ),
        (
            Path("tests/fixtures/git-activity-exporter-workflow.yml"),
            "image: moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef",
            "image: moby/buildkit",
            "tests/fixtures/git-activity-exporter-workflow.yml",
        ),
        (
            Path("examples/self-hosting/kubernetes.yaml"),
            "image: ronaldraygun/git-activity-exporter:{version}",
            "image: ronaldraygun/git-activity-exporter",
            "examples/self-hosting/kubernetes.yaml",
        ),
        (
            Path("examples/self-hosting/compose.yaml"),
            "git-activity-exporter-self-hosting:{version}}}",
            "git-activity-exporter-self-hosting}",
            "examples/self-hosting/compose.yaml",
        ),
    ),
)
def test_release_drift_check_rejects_unpinned_images(
    tmp_path, relative_path, old, new, message
):
    _copy_release_files(tmp_path)
    path = tmp_path / relative_path
    old = old.format(version=(ROOT / "VERSION").read_text().strip())
    path.write_text(path.read_text().replace(old, new))

    result = _run(tmp_path)

    assert result.returncode == 1
    assert message in result.stderr
    assert "has no tag" in result.stderr or "no concrete tag" in result.stderr


def test_release_drift_check_rejects_mutable_dockerfile_base_image(tmp_path):
    _copy_release_files(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f",
            "FROM python:3.12.11-slim",
        )
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "Dockerfile:1" in result.stderr
    assert "base image reference" in result.stderr
    assert "must use a sha256 digest" in result.stderr


@pytest.mark.parametrize(
    ("relative_path", "old", "new"),
    (
        (Path("requirements.txt"), "requests==2.32.3", "requests>=2.32.3"),
        (Path("requirements-dev.txt"), "pytest==8.3.4", "pytest"),
    ),
)
def test_release_drift_check_rejects_unpinned_python_dependencies(
    tmp_path, relative_path, old, new
):
    _copy_release_files(tmp_path)
    path = tmp_path / relative_path
    path.write_text(path.read_text().replace(old, new))

    result = _run(tmp_path)

    assert result.returncode == 1
    assert f"{relative_path}:" in result.stderr
    assert "must use an exact == version pin" in result.stderr


def test_release_drift_check_rejects_latest_in_workflow_image(tmp_path):
    _copy_release_files(tmp_path)
    workflow = tmp_path / "tests" / "fixtures" / "git-activity-exporter-workflow.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "image: moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef",
            "image: moby/buildkit:latest",
        )
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "tests/fixtures/git-activity-exporter-workflow.yml" in result.stderr
    assert "references :latest" in result.stderr


def test_release_drift_check_accepts_digest_pinned_image(tmp_path):
    _copy_release_files(tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f",
            "FROM python@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
