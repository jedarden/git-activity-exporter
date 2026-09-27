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


def _copy_release_files(root: Path) -> None:
    for relative_path in (*VERSIONED_FILES, *PINNED_IMAGE_FILES):
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
    assert "release image references match VERSION" in result.stdout


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
            "FROM python:3.12-slim",
            "FROM python",
            "Dockerfile",
        ),
        (
            Path("tests/fixtures/git-activity-exporter-workflow.yml"),
            "image: gcr.io/kaniko-project/executor:v1.23.2",
            "image: gcr.io/kaniko-project/executor",
            "tests/fixtures/git-activity-exporter-workflow.yml",
        ),
        (
            Path("examples/self-hosting/kubernetes.yaml"),
            "image: ronaldraygun/git-activity-exporter:0.1.41",
            "image: ronaldraygun/git-activity-exporter",
            "examples/self-hosting/kubernetes.yaml",
        ),
        (
            Path("examples/self-hosting/compose.yaml"),
            "git-activity-exporter-self-hosting:0.1.41}",
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
    path.write_text(path.read_text().replace(old, new))

    result = _run(tmp_path)

    assert result.returncode == 1
    assert message in result.stderr
    assert "has no tag" in result.stderr or "no concrete tag" in result.stderr


def test_release_drift_check_rejects_latest_in_workflow_image(tmp_path):
    _copy_release_files(tmp_path)
    workflow = tmp_path / "tests" / "fixtures" / "git-activity-exporter-workflow.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "image: gcr.io/kaniko-project/executor:v1.23.2",
            "image: gcr.io/kaniko-project/executor:latest",
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
            "FROM python:3.12-slim",
            "FROM python@sha256:0123456789abcdef",
        )
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
