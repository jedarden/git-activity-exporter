"""Contract tests for the versioned Argo release path.

The manifests are owned by the sibling ``declarative-config`` repository.
When that checkout is available, these tests inspect its manifests directly;
the small committed fixtures keep the test suite reproducible from an
application-only archive as well.
"""

from pathlib import Path

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


def _templates(workflow: dict) -> dict[str, dict]:
    return {template["name"]: template for template in workflow["spec"]["templates"]}


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
