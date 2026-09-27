import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check-release-drift.py"
VERSIONED_FILES = (
    Path("VERSION"),
    Path("examples/self-hosting/compose.yaml"),
    Path("examples/self-hosting/kubernetes.yaml"),
    Path("docs/self-hosting.md"),
    Path("docs/notes/deployment.md"),
)


def _copy_release_files(root: Path) -> None:
    for relative_path in VERSIONED_FILES:
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative_path, destination)


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
    assert "match VERSION" in result.stdout


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
