"""Contract tests for credential rotation and secret injection boundaries."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_DOC = ROOT / "docs" / "notes" / "deployment.md"
CONFIGURATION_DOC = ROOT / "docs" / "notes" / "configuration.md"
WORKFLOW_RELATIVE = Path(
    "k8s/iad-ci/argo-workflows/git-activity-exporter-build-workflowtemplate.yml"
)
DEPLOYMENT_RELATIVE = Path(
    "k8s/ardenone-cluster/git-activity-exporter/deployment.yml"
)
CONFIGMAP_RELATIVE = Path(
    "k8s/ardenone-cluster/git-activity-exporter/configmap.yml"
)

RUNTIME_SECRET_NAMES = {
    "FORGE_TOKEN",
    "DEST_S3_ENDPOINT",
    "DEST_S3_ACCESS_KEY_ID",
    "DEST_S3_SECRET_ACCESS_KEY",
}


def _workflow() -> dict:
    sibling = ROOT.parent / "declarative-config" / WORKFLOW_RELATIVE
    path = sibling if sibling.is_file() else ROOT / "tests/fixtures/git-activity-exporter-workflow.yml"
    return yaml.safe_load(path.read_text())


def _templates(workflow: dict) -> dict[str, dict]:
    return {template["name"]: template for template in workflow["spec"]["templates"]}


def _env_by_name(template: dict) -> dict[str, dict]:
    env = template.get("env")
    if env is None and isinstance(template.get("script"), dict):
        env = template["script"].get("env", [])
    if env is None and isinstance(template.get("container"), dict):
        env = template["container"].get("env", [])
    return {entry["name"]: entry for entry in env or []}


def _runtime_resources() -> list[dict]:
    sibling_dir = ROOT.parent / "declarative-config" / DEPLOYMENT_RELATIVE.parent
    if sibling_dir.is_dir():
        paths = [sibling_dir / DEPLOYMENT_RELATIVE.name, sibling_dir / CONFIGMAP_RELATIVE.name]
    else:
        paths = [ROOT / "examples/self-hosting/kubernetes.yaml"]

    resources = []
    for path in paths:
        resources.extend(
            resource
            for resource in yaml.safe_load_all(path.read_text())
            if resource
        )
    return resources


def _deployment_and_configmap() -> tuple[dict, dict]:
    resources = _runtime_resources()
    deployment = next(resource for resource in resources if resource["kind"] == "Deployment")
    configmap = next(resource for resource in resources if resource["kind"] == "ConfigMap")
    return deployment, configmap


def test_rotation_runbook_covers_provision_validation_revocation_and_failure():
    text = DEPLOYMENT_DOC.read_text()
    _, _, section = text.partition("## Credential rotation runbook")
    assert section, "deployment.md lost the credential rotation runbook"
    section = section.split("\n### Updating reproducibility pins", 1)[0]
    compact = " ".join(section.split())

    required = (
        "Runtime Forgejo (`FORGE_TOKEN`)",
        "Runtime S3 (`DEST_S3_*`)",
        "Release Forgejo (`FORGEJO_TOKEN`/`GIT_PASSWORD`)",
        "Registry (Docker Hub)",
        "Inventory and provision",
        "Write the managed source",
        "Propagate without reading values",
        "Validate the consumer",
        "Revoke only after validation",
        "Failure and rollback behavior",
        "leave the old credential active",
        "SecretSynced=True",
        "previous S3 pointer remains authoritative",
        "same resolved version",
        "must not appear in provider errors",
    )
    for phrase in required:
        assert phrase in compact, f"credential rotation contract missing: {phrase}"

    assert compact.index("Inventory and provision") < compact.index("Write the managed source")
    assert compact.index("Write the managed source") < compact.index("Propagate without reading values")
    assert compact.index("Propagate without reading values") < compact.index("Validate the consumer")
    assert compact.index("Validate the consumer") < compact.index("Revoke only after validation")

    configuration = " ".join(CONFIGURATION_DOC.read_text().split())
    assert "deployment.md#credential-rotation-runbook" in configuration
    assert "application configuration does not change during rotation" in configuration


def test_runtime_credentials_are_secret_refs_not_application_configuration():
    deployment, configmap = _deployment_and_configmap()
    container = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "exporter"
    )
    env = {entry["name"]: entry for entry in container["env"]}

    assert RUNTIME_SECRET_NAMES.isdisjoint(configmap.get("data", {}))
    for name in RUNTIME_SECRET_NAMES:
        entry = env[name]
        assert "value" not in entry
        assert set(entry) == {"name", "valueFrom"}
        assert set(entry["valueFrom"]) == {"secretKeyRef"}
        assert entry["valueFrom"]["secretKeyRef"].get("optional") is not True


def test_release_credentials_are_references_and_registry_is_not_a_workflow_input():
    workflow = _workflow()
    templates = _templates(workflow)

    for template_name in ("test", "resolve-version", "promote"):
        env = _env_by_name(templates[template_name])
        forgejo = env["FORGEJO_TOKEN"]
        assert "value" not in forgejo
        assert forgejo["valueFrom"]["secretKeyRef"] == {
            "name": "forgejo-webhook-token",
            "key": "token",
        }

    docker = templates["docker-build"]["container"]
    password = _env_by_name(docker)["GIT_PASSWORD"]
    assert "value" not in password
    assert password["valueFrom"]["secretKeyRef"] == {
        "name": "forgejo-webhook-token",
        "key": "token",
    }

    registry_volume = next(
        volume
        for volume in workflow["spec"]["volumes"]
        if volume["name"] == "docker-config"
    )
    assert registry_volume["secret"] == {
        "secretName": "docker-hub-registry",
        "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
    }
    assert all(
        parameter["name"] not in {"FORGEJO_TOKEN", "GIT_PASSWORD", "PAT"}
        for parameter in workflow["spec"].get("arguments", {}).get("parameters", [])
    )
    assert all(
        parameter["name"] not in {"FORGEJO_TOKEN", "GIT_PASSWORD", "PAT"}
        for template in templates.values()
        for parameter in template.get("inputs", {}).get("parameters", [])
    )
