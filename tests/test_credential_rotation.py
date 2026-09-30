"""Contract tests for credential rotation and secret injection boundaries."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
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
EXTERNALSECRET_RELATIVE = Path(
    "k8s/ardenone-cluster/git-activity-exporter/externalsecret.yml"
)
GARAGE_KEYS_RELATIVE = Path("k8s/ardenone-cluster/garage-operator/keys.yml")
RUNTIME_MANIFEST_FIXTURE_DIR = ROOT / "tests/fixtures/git-activity-exporter-runtime"
GARAGE_KEY_FIXTURE = RUNTIME_MANIFEST_FIXTURE_DIR / "garage-key.yml"

RUNTIME_CONFIG_DATA = {
    "FORGE_BASE_URL": "https://git.ardenone.com",
    "FORGE_OWNER": "jedarden",
    "WINDOW_DAYS": "90",
    "SHALLOW_SINCE_DAYS": "100",
    "CLONE_ROOT": "/data/mirrors",
    "POLL_INTERVAL_SECONDS": "3600",
    "GIT_TIMEOUT_SECONDS": "1800",
    "TRIM_MAX_LINES": "5000",
    "TRIM_MAX_FILES": "200",
    "BEAD_BULK_CLOSE_THRESHOLD": "150",
    "LOG_LEVEL": "INFO",
}
FORGE_EXTERNAL_SECRET_SPEC = {
    "refreshInterval": "1h",
    "secretStoreRef": {"name": "openbao-v2", "kind": "ClusterSecretStore"},
    "target": {"name": "git-activity-exporter-forge", "creationPolicy": "Owner"},
    "data": [
        {
            "secretKey": "FORGE_TOKEN",
            "remoteRef": {
                "key": "ardenone-cluster/git-activity-exporter/forge",
                "property": "forgejo-token",
            },
        }
    ],
}
EXPECTED_RUNTIME_SECRET_REFS = {
    "FORGE_TOKEN": {
        "name": "git-activity-exporter-forge",
        "key": "FORGE_TOKEN",
    },
    "DEST_S3_ENDPOINT": {
        "name": "dashboard-s3-credentials",
        "key": "S3_ENDPOINT",
    },
    "DEST_S3_ACCESS_KEY_ID": {
        "name": "dashboard-s3-credentials",
        "key": "ACCESS_KEY_ID",
    },
    "DEST_S3_SECRET_ACCESS_KEY": {
        "name": "dashboard-s3-credentials",
        "key": "SECRET_ACCESS_KEY",
    },
}
EXPECTED_RUNTIME_LITERALS = {
    "DEST_S3_BUCKET": "dashboard-site",
    "DEST_S3_PREFIX": "git-activity/data",
    "DEST_S3_ADDRESSING_STYLE": "path",
}
REQUIRED_APPLICATION_ENV = {
    "FORGE_TOKEN",
    "DEST_S3_ENDPOINT",
    "DEST_S3_BUCKET",
    "DEST_S3_ACCESS_KEY_ID",
    "DEST_S3_SECRET_ACCESS_KEY",
}

RUNTIME_SECRET_NAMES = {
    "FORGE_TOKEN",
    "DEST_S3_ENDPOINT",
    "DEST_S3_ACCESS_KEY_ID",
    "DEST_S3_SECRET_ACCESS_KEY",
}


def _workflow() -> dict:
    sibling = ROOT.parent / "declarative-config" / WORKFLOW_RELATIVE
    path = (
        sibling
        if sibling.is_file()
        else ROOT / "tests/fixtures/git-activity-exporter-workflow.yml"
    )
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
    live_paths = [
        sibling_dir / DEPLOYMENT_RELATIVE.name,
        sibling_dir / CONFIGMAP_RELATIVE.name,
        sibling_dir / EXTERNALSECRET_RELATIVE.name,
    ]
    paths = live_paths if all(path.is_file() for path in live_paths) else [
        RUNTIME_MANIFEST_FIXTURE_DIR / "deployment.yml",
        RUNTIME_MANIFEST_FIXTURE_DIR / "configmap.yml",
    ]
    resources = [
        resource
        for path in paths
        for resource in yaml.safe_load_all(path.read_text())
        if resource
    ]
    if len(paths) != len(live_paths):
        # The ExternalSecret mapping is value-free and kept as a contract
        # alongside the YAML snapshots. The live source is checked below
        # whenever the declarative-config checkout is available.
        resources.append(
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ExternalSecret",
                "metadata": {
                    "name": "git-activity-exporter-forge",
                    "namespace": "git-activity-exporter",
                    "annotations": {"reloader.stakater.com/auto": "true"},
                },
                "spec": FORGE_EXTERNAL_SECRET_SPEC,
            }
        )
    return resources


def _deployment_and_configmap() -> tuple[dict, dict]:
    resources = _runtime_resources()
    deployment = next(resource for resource in resources if resource["kind"] == "Deployment")
    configmap = next(resource for resource in resources if resource["kind"] == "ConfigMap")
    return deployment, configmap


def _external_secret() -> dict:
    return next(
        resource
        for resource in _runtime_resources()
        if resource["kind"] == "ExternalSecret"
        and resource["metadata"]["name"] == "git-activity-exporter-forge"
    )


def _garage_key() -> dict:
    return yaml.safe_load(GARAGE_KEY_FIXTURE.read_text())


def _exporter_container(deployment: dict | None = None) -> dict:
    if deployment is None:
        deployment, _ = _deployment_and_configmap()
    return next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "exporter"
    )


def _materialize_external_secret(
    external_secret: dict, provider_values: dict[tuple[str, str], str]
) -> dict:
    """Model External Secrets writing its selected provider properties."""

    secret_data = {}
    for item in external_secret["spec"]["data"]:
        remote_ref = item["remoteRef"]
        remote_key = (remote_ref["key"], remote_ref["property"])
        try:
            secret_data[item["secretKey"]] = provider_values[remote_key]
        except KeyError as exc:
            raise RuntimeError("ExternalSecret provider property is unavailable") from exc
    return {
        "name": external_secret["spec"]["target"]["name"],
        "data": secret_data,
    }


def _resolve_pod_environment(
    deployment: dict, configmap: dict, secrets: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Resolve the exporter's envFrom, literals, and required SecretKeyRefs."""

    container = _exporter_container(deployment)
    environment = {}
    for source in container.get("envFrom", []):
        config_map_ref = source.get("configMapRef")
        if not config_map_ref:
            raise AssertionError("unexpected envFrom source in exporter container")
        if config_map_ref["name"] != configmap["metadata"]["name"]:
            raise AssertionError("exporter envFrom references an unexpected ConfigMap")
        environment.update(configmap.get("data", {}))

    for entry in container.get("env", []):
        if "value" in entry:
            environment[entry["name"]] = entry["value"]
            continue
        secret_ref = entry.get("valueFrom", {}).get("secretKeyRef")
        if not secret_ref or secret_ref.get("optional") is True:
            raise AssertionError("runtime credential references must be required")
        try:
            environment[entry["name"]] = secrets[secret_ref["name"]][secret_ref["key"]]
        except KeyError as exc:
            raise RuntimeError("container cannot resolve a required SecretKeyRef") from exc
    return environment


def _materialize_garage_key_secret(garage_key: dict, generation: str) -> dict:
    """Model GarageKey rotating its credentials into its configured Secret."""

    template = garage_key["spec"]["secretTemplate"]
    data = {
        template["accessKeyIdKey"]: f"access-key:{generation}",
        template["secretAccessKeyKey"]: f"secret-key:{generation}",
    }
    if template.get("includeEndpoint"):
        data[template["endpointKey"]] = f"https://s3:{generation}.example.invalid"
    return {
        "name": template["name"],
        "namespace": garage_key["metadata"]["namespace"],
        "annotations": template.get("annotations", {}),
        "data": data,
    }


def _reflect_garage_key_secret(source_secret: dict, target_namespace: str) -> dict:
    """Model the configured reflector copy into the runtime namespace."""

    annotations = source_secret["annotations"]
    allowed_namespaces = set(
        annotations.get(
            "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces", ""
        ).split(",")
    )
    automatic_namespaces = set(
        annotations.get(
            "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces", ""
        ).split(",")
    )
    allowed_namespaces = {namespace.strip() for namespace in allowed_namespaces}
    automatic_namespaces = {namespace.strip() for namespace in automatic_namespaces}
    if (
        annotations.get("reflector.v1.k8s.emberstack.com/reflection-allowed") != "true"
        or annotations.get("reflector.v1.k8s.emberstack.com/reflection-auto-enabled")
        != "true"
        or target_namespace not in allowed_namespaces
        or target_namespace not in automatic_namespaces
    ):
        raise RuntimeError("GarageKey Secret is not configured for runtime reflection")
    return {"name": source_secret["name"], "data": source_secret["data"]}


def _synthetic_runtime_secrets(
    external_secret: dict, garage_key: dict, generation: str
) -> dict:
    """Run synthetic provider values through the configured Secret paths."""

    provider_values = {
        (item["remoteRef"]["key"], item["remoteRef"]["property"]):
        f"forge-token:{generation}"
        for item in external_secret["spec"]["data"]
    }
    forge_secret = _materialize_external_secret(external_secret, provider_values)
    runtime_namespace = external_secret["metadata"]["namespace"]
    s3_secret = _reflect_garage_key_secret(
        _materialize_garage_key_secret(garage_key, generation), runtime_namespace
    )
    return {
        forge_secret["name"]: forge_secret["data"],
        s3_secret["name"]: s3_secret["data"],
    }


def _start_smoke_pod(environment: dict[str, str]) -> subprocess.Popen:
    """Start the real config loader with SecretKeyRefs projected into a pod."""

    source = """
from src.config import load
import os
import sys

config = load()
print("started", flush=True)
for command in sys.stdin:
    if command.startswith("probe "):
        values = (
            config.forge_token,
            config.dest.endpoint_url,
            config.dest.access_key_id,
            config.dest.secret_access_key,
        )
        expected = command.split(" ", maxsplit=1)[1].strip()
        expected_values = (
            f"forge-token:{expected}",
            f"https://s3:{expected}.example.invalid",
            f"access-key:{expected}",
            f"secret-key:{expected}",
        )
        result = "ready" if values == expected_values else "stale"
        print(result, flush=True)
    elif command.strip() == "exit":
        break
"""
    process_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        **environment,
    }
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", source],
        cwd=ROOT,
        env=process_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    startup = process.stdout.readline().strip()
    if startup != "started":
        process.stderr.read()
        process.kill()
        raise AssertionError("runtime config did not start in smoke pod")
    process._smoke_output = [startup]
    return process


def _probe_smoke_pod(process: subprocess.Popen, generation: str) -> str:
    process.stdin.write(f"probe {generation}\n")
    process.stdin.flush()
    result = process.stdout.readline().strip()
    process._smoke_output.append(result)
    return result


def _expect_smoke_pod_ready(process: subprocess.Popen, generation: str) -> None:
    if _probe_smoke_pod(process, generation) != "ready":
        raise AssertionError("runtime smoke probe did not report ready")


class _ReloaderSmokeHarness:
    """Model the configured Reloader Secret watch in a disposable process."""

    def __init__(self, deployment: dict, configmap: dict, secrets: dict):
        self.deployment = deployment
        self.configmap = configmap
        self.secrets = secrets
        self.process = _start_smoke_pod(
            _resolve_pod_environment(deployment, configmap, secrets)
        )

    def update_secrets(self, secrets: dict) -> tuple[int, str]:
        old_process = self.process
        old_pid = old_process.pid
        container = _exporter_container(self.deployment)
        referenced_secrets = {
            entry["valueFrom"]["secretKeyRef"]["name"]
            for entry in container["env"]
            if "secretKeyRef" in entry.get("valueFrom", {})
        }
        changed_secrets = {
            name
            for name in referenced_secrets
            if self.secrets.get(name) != secrets.get(name)
        }
        if not changed_secrets:
            return old_pid, "unchanged"

        # Reloader observes changes to referenced Secrets on auto-enabled
        # workloads and replaces the Pod; the replacement resolves SecretKeyRefs
        # again from the updated Secret set.
        if self.deployment["metadata"].get("annotations", {}).get(
            "reloader.stakater.com/auto"
        ) != "true":
            self.secrets = secrets
            return old_pid, "not-reloaded"

        old_output = _stop_smoke_pod(old_process)
        self.secrets = secrets
        self.process = _start_smoke_pod(
            _resolve_pod_environment(self.deployment, self.configmap, secrets)
        )
        return old_pid, old_output


def _stop_smoke_pod(process: subprocess.Popen) -> str:
    if process.poll() is None:
        process.stdin.write("exit\n")
        process.stdin.flush()
    process.stdin.close()
    process.wait(timeout=5)
    process._smoke_output.extend(process.stdout.read().splitlines())
    stderr = process.stderr.read()
    process.stdout.close()
    process.stderr.close()
    process._smoke_output.append(stderr)
    return "\n".join(process._smoke_output)


def _assert_smoke_output_redacts_credentials(
    output: str, generations: tuple[str, ...]
) -> None:
    for generation in generations:
        values = (
            f"forge-token:{generation}",
            f"https://s3:{generation}.example.invalid",
            f"access-key:{generation}",
            f"secret-key:{generation}",
        )
        if any(value in output for value in values):
            raise AssertionError("synthetic credential appeared in runtime smoke output")


def test_rotation_runbook_covers_provision_validation_revocation_and_failure():
    text = DEPLOYMENT_DOC.read_text()
    _, _, section = text.partition("## Credential rotation runbook")
    assert section, "deployment.md lost the credential rotation runbook"
    section = section.split("\n### Updating reproducibility pins", 1)[0]
    compact = " ".join(section.split())

    required = (
        "Runtime Forgejo (`FORGE_TOKEN`)",
        "Runtime S3 (`DEST_S3_*`)",
        "Release Forgejo (`FORGEJO_TOKEN`/`GIT_AUTH_TOKEN`)",
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
    assert compact.index("Write the managed source") < compact.index(
        "Propagate without reading values"
    )
    assert compact.index("Propagate without reading values") < compact.index(
        "Validate the consumer"
    )
    assert compact.index("Validate the consumer") < compact.index("Revoke only after validation")

    configuration = " ".join(CONFIGURATION_DOC.read_text().split())
    assert "deployment.md#credential-rotation-runbook" in configuration
    assert "application configuration does not change during rotation" in configuration


def test_runtime_credentials_are_secret_refs_not_application_configuration():
    deployment, configmap = _deployment_and_configmap()
    container = _exporter_container(deployment)
    env = {entry["name"]: entry for entry in container["env"]}

    assert RUNTIME_SECRET_NAMES.isdisjoint(configmap.get("data", {}))
    for name in RUNTIME_SECRET_NAMES:
        entry = env[name]
        assert "value" not in entry
        assert set(entry) == {"name", "valueFrom"}
        assert set(entry["valueFrom"]) == {"secretKeyRef"}
        assert entry["valueFrom"]["secretKeyRef"].get("optional") is not True
    assert "FORGEJO_TOKEN" not in env
    assert "GIT_AUTH_TOKEN" not in env
    assert all(
        entry.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
        != "forgejo-webhook-token"
        for entry in env.values()
    )


def test_rendered_runtime_manifests_map_config_and_external_secret_to_required_env():
    deployment, configmap = _deployment_and_configmap()
    external_secret = _external_secret()
    container = _exporter_container(deployment)
    env = {entry["name"]: entry for entry in container["env"]}

    assert configmap["data"] == RUNTIME_CONFIG_DATA
    assert container["envFrom"] == [
        {"configMapRef": {"name": "git-activity-exporter-config"}}
    ]
    assert external_secret["spec"] == FORGE_EXTERNAL_SECRET_SPEC
    assert external_secret["metadata"]["annotations"] == {
        "reloader.stakater.com/auto": "true"
    }
    assert env["FORGE_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": external_secret["spec"]["target"]["name"],
        "key": "FORGE_TOKEN",
    }

    for name, expected_ref in EXPECTED_RUNTIME_SECRET_REFS.items():
        assert env[name]["valueFrom"]["secretKeyRef"] == expected_ref
    for name, value in EXPECTED_RUNTIME_LITERALS.items():
        assert env[name] == {"name": name, "value": value}
    assert set(env) == set(EXPECTED_RUNTIME_SECRET_REFS) | set(
        EXPECTED_RUNTIME_LITERALS
    )

    projected = _resolve_pod_environment(
        deployment,
        configmap,
        _synthetic_runtime_secrets(external_secret, _garage_key(), "generation-a"),
    )
    assert REQUIRED_APPLICATION_ENV <= projected.keys()
    assert {name: projected[name] for name in configmap["data"]} == configmap["data"]
    assert projected["DEST_S3_BUCKET"] == "dashboard-site"


def test_available_gitops_manifests_match_checked_in_rendered_fixtures():
    sibling_dir = ROOT.parent / "declarative-config" / DEPLOYMENT_RELATIVE.parent
    live_paths = [
        sibling_dir / DEPLOYMENT_RELATIVE.name,
        sibling_dir / CONFIGMAP_RELATIVE.name,
        sibling_dir / EXTERNALSECRET_RELATIVE.name,
    ]
    if not all(path.is_file() for path in live_paths):
        pytest.skip("sibling declarative-config checkout is not available")

    fixture_paths = [
        RUNTIME_MANIFEST_FIXTURE_DIR / "deployment.yml",
        RUNTIME_MANIFEST_FIXTURE_DIR / "configmap.yml",
    ]

    def load_resources(paths: list[Path]) -> list[dict]:
        return [
            resource
            for path in paths
            for resource in yaml.safe_load_all(path.read_text())
            if resource
        ]

    live_resources = load_resources(live_paths)
    fixture_resources = load_resources(fixture_paths)
    for kind, name in (
        ("Deployment", "git-activity-exporter"),
        ("ConfigMap", "git-activity-exporter-config"),
    ):
        live = next(
            resource
            for resource in live_resources
            if resource["kind"] == kind and resource["metadata"]["name"] == name
        )
        fixture = next(
            resource
            for resource in fixture_resources
            if resource["kind"] == kind and resource["metadata"]["name"] == name
        )
        assert live == fixture

    live_external_secret = next(
        resource
        for resource in live_resources
        if resource["kind"] == "ExternalSecret"
        and resource["metadata"]["name"] == "git-activity-exporter-forge"
    )
    assert live_external_secret["spec"] == FORGE_EXTERNAL_SECRET_SPEC
    assert live_external_secret["metadata"]["annotations"] == {
        "reloader.stakater.com/auto": "true"
    }

    garage_keys_path = ROOT.parent / "declarative-config" / GARAGE_KEYS_RELATIVE
    if garage_keys_path.is_file():
        live_garage_keys = [
            resource
            for resource in yaml.safe_load_all(garage_keys_path.read_text())
            if resource
        ]
        live_garage_key = next(
            resource
            for resource in live_garage_keys
            if resource["kind"] == "GarageKey"
            and resource["metadata"]["name"] == "dashboard-write-key"
        )
        assert live_garage_key == _garage_key()


@pytest.mark.parametrize(
    ("secret_name", "secret_key"),
    [
        ("git-activity-exporter-forge", "FORGE_TOKEN"),
        ("dashboard-s3-credentials", "S3_ENDPOINT"),
        ("dashboard-s3-credentials", "ACCESS_KEY_ID"),
        ("dashboard-s3-credentials", "SECRET_ACCESS_KEY"),
    ],
)
def test_missing_required_runtime_secret_key_fails_pod_environment_resolution(
    secret_name, secret_key
):
    deployment, configmap = _deployment_and_configmap()
    secrets = _synthetic_runtime_secrets(
        _external_secret(), _garage_key(), "generation-a"
    )
    del secrets[secret_name][secret_key]

    with pytest.raises(RuntimeError, match="required SecretKeyRef"):
        _resolve_pod_environment(deployment, configmap, secrets)


def test_rotated_credentials_reach_the_reloader_restarted_runtime_process():
    deployment, configmap = _deployment_and_configmap()
    external_secret = _external_secret()
    garage_key = _garage_key()
    assert (
        deployment["metadata"]["annotations"]["reloader.stakater.com/auto"]
        == "true"
    )
    assert (
        external_secret["metadata"]["annotations"]["reloader.stakater.com/auto"]
        == "true"
    )

    workload = _ReloaderSmokeHarness(
        deployment,
        configmap,
        _synthetic_runtime_secrets(external_secret, garage_key, "generation-a"),
    )
    first_process = workload.process
    smoke_output = ""
    try:
        _expect_smoke_pod_ready(first_process, "generation-a")

        # Refresh the ExternalSecret's provider property and rotate the
        # GarageKey Secret; the reflector then updates its runtime namespace
        # copy. The auto-enabled workload replaces its process on those Secret
        # changes, and the replacement resolves the new SecretKeyRefs.
        rotated_secrets = _synthetic_runtime_secrets(
            external_secret, garage_key, "generation-b"
        )
        first_pid, old_output = workload.update_secrets(rotated_secrets)
        smoke_output += old_output
        if first_process.poll() is None:
            raise AssertionError("Reloader smoke left the old runtime process running")

        replacement_process = workload.process
        if replacement_process.pid == first_pid:
            raise AssertionError("runtime process did not restart after Secret rotation")
        if _probe_smoke_pod(replacement_process, "generation-a") != "stale":
            raise AssertionError("replacement workload still accepted the old credentials")
        _expect_smoke_pod_ready(replacement_process, "generation-b")
    finally:
        if workload.process.poll() is None:
            smoke_output += _stop_smoke_pod(workload.process)
        if first_process.poll() is None:
            smoke_output += _stop_smoke_pod(first_process)
        _assert_smoke_output_redacts_credentials(
            smoke_output, ("generation-a", "generation-b")
        )


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
    git_auth = _env_by_name(docker)["GIT_AUTH_TOKEN"]
    assert "value" not in git_auth
    assert git_auth["valueFrom"]["secretKeyRef"] == {
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
        parameter["name"] not in {"FORGEJO_TOKEN", "GIT_AUTH_TOKEN", "PAT"}
        for parameter in workflow["spec"].get("arguments", {}).get("parameters", [])
    )
    assert all(
        parameter["name"] not in {"FORGEJO_TOKEN", "GIT_AUTH_TOKEN", "PAT"}
        for template in templates.values()
        for parameter in template.get("inputs", {}).get("parameters", [])
    )
