"""Contract tests for the versioned Argo release path.

The manifests are owned by the sibling ``declarative-config`` repository.
When that checkout is available, these tests inspect its manifests directly;
the small committed fixtures keep the test suite reproducible from an
application-only archive as well.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
WORKFLOW_RELATIVE = Path(
    "k8s/iad-ci/argo-workflows/git-activity-exporter-build-workflowtemplate.yml"
)
SENSOR_RELATIVE = Path(
    "k8s/iad-ci/argo-events/git-activity-exporter-sensor.yml"
)


def _manifest(relative_path: Path, fixture_name: str) -> dict:
    sibling = ROOT.parent / "declarative-config" / relative_path
    path = sibling if sibling.is_file() else FIXTURES / fixture_name
    return yaml.safe_load(path.read_text())


def _workflow() -> dict:
    return _manifest(WORKFLOW_RELATIVE, "git-activity-exporter-workflow.yml")


def _sensor() -> dict:
    return _manifest(SENSOR_RELATIVE, "git-activity-exporter-sensor.yml")


def _deployment_resources() -> list[dict]:
    sibling = ROOT.parent / "declarative-config" / (
        "k8s/ardenone-cluster/git-activity-exporter/deployment.yml"
    )
    path = (
        sibling
        if sibling.is_file()
        else ROOT / "examples" / "self-hosting" / "kubernetes.yaml"
    )
    return [resource for resource in yaml.safe_load_all(path.read_text()) if resource]


def _image_fields(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "image" and isinstance(nested, str):
                yield nested
            yield from _image_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _image_fields(nested)


def _assert_explicit_image_pin(image: str) -> None:
    image_without_digest, separator, digest = image.partition("@")
    if separator:
        assert digest.startswith("sha256:"), image
        assert digest.removeprefix("sha256:"), image
        return
    last_component = image_without_digest.rsplit("/", 1)[-1]
    assert ":" in last_component, f"image has no tag: {image}"
    assert last_component.rsplit(":", 1)[1].lower() != "latest", image


def _templates(workflow: dict) -> dict[str, dict]:
    return {template["name"]: template for template in workflow["spec"]["templates"]}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _new_bare_repository(root: Path, name: str) -> tuple[Path, Path]:
    origin = root / f"{name}.git"
    seed = root / f"{name}-seed"
    _git(root, "init", "--bare", "--initial-branch=main", str(origin))
    _git(root, "init", "--initial-branch=main", str(seed))
    _git(seed, "config", "user.name", "Release test")
    _git(seed, "config", "user.email", "release-test@example.invalid")
    _git(seed, "remote", "add", "origin", str(origin))
    return origin, seed


def _application_origin(root: Path, *, explicit_version_change: bool) -> Path:
    origin, seed = _new_bare_repository(root, "application")
    (seed / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (seed / "release-input.txt").write_text("initial\n", encoding="utf-8")
    _git(seed, "add", "VERSION", "release-input.txt")
    _git(seed, "commit", "-m", "initial release state")

    if explicit_version_change:
        (seed / "VERSION").write_text("1.2.9\n", encoding="utf-8")
    else:
        (seed / "release-input.txt").write_text("application change\n", encoding="utf-8")
    _git(seed, "add", "VERSION", "release-input.txt")
    _git(seed, "commit", "-m", "trigger release")
    _git(seed, "push", "--set-upstream", "origin", "main")
    return origin


def _run_resolve_version(
    workflow: dict, root: Path, origin: Path, expected_version_path: Path
) -> str:
    """Run the WorkflowTemplate's resolver against a local bare Git repo."""
    script = _templates(workflow)["resolve-version"]["script"]["source"]
    checkout = root / "resolve-checkout"
    clone_pattern = re.compile(
        r'git clone --branch \{\{workflow\.parameters\.branch\}\} \\\n'
        r'\s+"https://git\.ardenone\.com/\{\{workflow\.parameters\.git-repo\}\}\.git" \\\n'
        r'\s+/tmp/repo'
    )
    script, replacements = clone_pattern.subn(
        "git clone --branch main "
        f"{shlex.quote(str(origin))} {shlex.quote(str(checkout))}",
        script,
        count=1,
    )
    assert replacements == 1, script
    script = script.replace("/tmp/repo", str(checkout))
    script = script.replace("/tmp/version", str(expected_version_path))
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    return expected_version_path.read_text(encoding="utf-8").strip()


def _gitops_repository(root: Path) -> Path:
    origin, seed = _new_bare_repository(root, "declarative-config")
    manifest = seed / "k8s/ardenone-cluster/git-activity-exporter/deployment.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: exporter\n"
        "          image: ronaldraygun/git-activity-exporter:1.2.3\n",
        encoding="utf-8",
    )
    _git(seed, "add", str(manifest.relative_to(seed)))
    _git(seed, "commit", "-m", "initial GitOps state")
    _git(seed, "push", "--set-upstream", "origin", "main")
    return origin


def _run_promotion(
    workflow: dict, root: Path, gitops_origin: Path, version: str
) -> tuple[str, str]:
    """Run the promotion script locally and return its revision and image."""
    script = _templates(workflow)["promote"]["script"]["source"]
    checkout = root / "gitops-checkout"
    clone_pattern = re.compile(
        r'git clone --branch main --depth 1 \\\n'
        r'\s+"https://git\.ardenone\.com/jedarden/declarative-config\.git" \\\n'
        r'\s+/tmp/declarative-config'
    )
    script, replacements = clone_pattern.subn(
        "git clone --branch main --depth 1 "
        f"{shlex.quote(str(gitops_origin))} {shlex.quote(str(checkout))}",
        script,
        count=1,
    )
    assert replacements == 1, script
    script = script.replace("/tmp/declarative-config", str(checkout))
    script = script.replace(
        'VERSION="{{inputs.parameters.version}}"', f'VERSION="{version}"'
    )
    revision_path = root / "gitops-revision"
    script = script.replace("/tmp/gitops-revision", str(revision_path))
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    revision = revision_path.read_text(encoding="utf-8").strip()
    promoted_manifest = _git(
        root,
        "--git-dir",
        str(gitops_origin),
        "show",
        f"{revision}:k8s/ardenone-cluster/git-activity-exporter/deployment.yml",
    )
    image = re.search(
        r"^\s+image:\s+(ronaldraygun/git-activity-exporter:\S+)\s*$",
        promoted_manifest,
        re.MULTILINE,
    )
    assert image, promoted_manifest
    return revision, image.group(1)


def test_failed_tests_cannot_reach_the_version_bump():
    workflow = _workflow()
    templates = _templates(workflow)
    build_steps = workflow["spec"]["templates"][0]["steps"]

    assert workflow["spec"]["entrypoint"] == "build"
    assert [group[0]["name"] for group in build_steps] == [
        "test",
        "resolve-version",
        "docker-build",
        "smoke",
        "promote",
        "verify-rollout",
    ]

    test = templates["test"]
    test_script = test["script"]["source"]
    assert "set -e" in test_script
    assert "python -m pytest tests/ -q" in test_script
    assert "continueOn" not in test
    assert test["retryStrategy"]["retryPolicy"] == "OnError"

    resolve_script = templates["resolve-version"]["script"]["source"]
    assert "git add VERSION" in resolve_script
    assert 'git config user.name "Argo Workflows CI"' in resolve_script
    assert build_steps[1][0]["template"] == "resolve-version"
    assert build_steps[4][0]["template"] == "promote"
    assert build_steps[5][0]["template"] == "verify-rollout"


def test_promotion_is_serialized_and_verifies_the_pushed_gitops_revision():
    workflow = _workflow()
    templates = _templates(workflow)
    assert workflow["spec"]["synchronization"]["mutexes"] == [
        {"name": "git-activity-exporter-release"}
    ]

    promote = templates["promote"]
    promote_source = promote["script"]["source"]
    assert "declarative-config.git" in promote_source
    assert "k8s/ardenone-cluster/git-activity-exporter/deployment.yml" in promote_source
    assert 'git add "$MANIFEST"' in promote_source
    assert "git commit -m" in promote_source
    assert "git push origin HEAD:main" in promote_source
    assert "/tmp/gitops-revision" in promote_source
    assert promote["outputs"]["parameters"][0]["name"] == "gitops-revision"

    verify = templates["verify-rollout"]["script"]["source"]
    assert "argocd-ro-ardenone-manager-ts.ardenone.com" in verify
    assert "ardenone-cluster-traefik:8001" in verify
    assert "--server=http://ardenone-cluster-traefik:8001" in verify
    assert "kubectl apply" not in verify


def test_resolved_version_drives_both_embedded_version_and_image_tag():
    workflow = _workflow()
    templates = _templates(workflow)
    build_steps = templates["build"]["steps"]
    resolve_output = build_steps[2][0]["arguments"]["parameters"][0]["value"]
    docker = templates["docker-build"]
    args = docker["container"]["args"]

    assert resolve_output == "{{steps.resolve-version.outputs.parameters.version}}"
    assert "--destination=ronaldraygun/git-activity-exporter:{{inputs.parameters.version}}" in args
    assert "--build-arg=VERSION={{inputs.parameters.version}}" in args
    assert any(
        arg.startswith("--context=git://git.ardenone.com/")
        and "refs/heads/{{workflow.parameters.branch}}" in arg
        for arg in args
    )
    assert "echo \"$VERSION\" > /tmp/version" in templates["resolve-version"]["script"]["source"]
    assert "COPY VERSION ." in (ROOT / "Dockerfile").read_text()


@pytest.mark.parametrize(
    "explicit_version_change, expected_version",
    [(False, "1.2.4"), (True, "1.2.9")],
    ids=["automatic-version-bump", "explicit-version-change"],
)
def test_release_paths_keep_one_version_before_gitops_promotion(
    tmp_path: Path, explicit_version_change: bool, expected_version: str
):
    """Exercise both resolver branches and the complete image handoff contract."""
    workflow = _workflow()
    application_origin = _application_origin(
        tmp_path, explicit_version_change=explicit_version_change
    )

    resolved_version = _run_resolve_version(
        workflow, tmp_path, application_origin, tmp_path / "resolved-version"
    )
    source_version = _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "show",
        "main:VERSION",
    ).strip()
    latest_source_commit = _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "log",
        "-1",
        "--format=%s",
        "main",
    )

    assert resolved_version == expected_version
    assert source_version == resolved_version
    if explicit_version_change:
        assert latest_source_commit == "trigger release"
    else:
        assert latest_source_commit == (
            f"ci: auto-bump version to {resolved_version}"
        )

    templates = _templates(workflow)
    docker_args = templates["docker-build"]["container"]["args"]
    destination = next(arg for arg in docker_args if arg.startswith("--destination="))
    build_arg = next(arg for arg in docker_args if arg.startswith("--build-arg="))
    assert destination.replace(
        "{{inputs.parameters.version}}", resolved_version
    ) == f"--destination=ronaldraygun/git-activity-exporter:{resolved_version}"
    assert build_arg.replace(
        "{{inputs.parameters.version}}", resolved_version
    ) == f"--build-arg=VERSION={resolved_version}"

    smoke = templates["smoke"]
    assert smoke["container"]["image"] == (
        "ronaldraygun/git-activity-exporter:{{inputs.parameters.version}}"
    )
    assert (
        'test "$(tr -d \'[:space:]\' < /app/VERSION)" '
        '= "{{inputs.parameters.version}}"'
        in smoke["container"]["args"][0]
    )
    # Dockerfile COPY VERSION . places the exact main-branch context value at
    # /app/VERSION; this is checked before promotion is allowed to run below.
    assert "COPY VERSION ." in (ROOT / "Dockerfile").read_text()
    embedded_version = source_version
    assert embedded_version == resolved_version

    gitops_origin = _gitops_repository(tmp_path)
    revision, promoted_image = _run_promotion(
        workflow, tmp_path, gitops_origin, resolved_version
    )
    assert revision == _git(
        tmp_path, "--git-dir", str(gitops_origin), "rev-parse", "main"
    )
    assert promoted_image == (
        f"ronaldraygun/git-activity-exporter:{resolved_version}"
    )


def test_release_workflow_and_kubernetes_manifests_use_explicit_image_pins():
    workflow_images = list(_image_fields(_workflow()))
    deployment_images = [
        image
        for resource in _deployment_resources()
        for image in _image_fields(resource)
    ]

    assert workflow_images
    assert deployment_images
    for image in (*workflow_images, *deployment_images):
        _assert_explicit_image_pin(image)


def test_ci_writeback_author_is_excluded_from_build_trigger():
    sensor = _sensor()
    dependency = sensor["spec"]["dependencies"][0]
    filters = dependency["filters"]["data"]

    assert dependency["name"] == "git-activity-exporter-push"
    assert {item["path"]: item for item in filters}["body.ref"]["value"] == [
        "refs/heads/main"
    ]

    author_filter = next(
        item for item in filters if item["path"] == "body.head_commit.author.name"
    )
    assert author_filter["comparator"] == "!="
    assert author_filter["value"] == ["Argo Workflows CI"]
