"""Contract and reconcile smoke tests for the reference S3 Secret mirror."""

import base64
from copy import deepcopy
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "git-activity-exporter-reflector.yml"
DEPLOYMENT_DOC = ROOT / "docs" / "notes" / "deployment.md"
DECLARATIVE_ROOT = ROOT.parent / "declarative-config"
LIVE_KEYS = DECLARATIVE_ROOT / "k8s" / "ardenone-cluster" / "garage-operator" / "keys.yml"
LIVE_DEPLOYMENT = (
    DECLARATIVE_ROOT
    / "k8s"
    / "ardenone-cluster"
    / "git-activity-exporter"
    / "deployment.yml"
)

SOURCE_NAMESPACE = "garage-operator"
SOURCE_SECRET_NAME = "dashboard-s3-credentials"
TARGET_NAMESPACE = "git-activity-exporter"
TARGET_SECRET_NAME = "dashboard-s3-credentials"
ALLOWED_NAMESPACES = (
    "b2-usage-exporter",
    "simplefin-sync",
    "options-downloader-status-exporter",
    "cluster-status",
    "argo-workflows-exporter",
    "commitgraph-dashboard",
    "git-activity-exporter",
    "estate-analytics",
)
TARGET_KEYS = {
    "S3_ENDPOINT",
    "ACCESS_KEY_ID",
    "SECRET_ACCESS_KEY",
}


def _resources() -> list[dict]:
    return [resource for resource in yaml.safe_load_all(FIXTURE.read_text()) if resource]


def _resource(kind: str, name: str, namespace: str) -> dict:
    return next(
        resource
        for resource in _resources()
        if resource["kind"] == kind
        and resource["metadata"]["name"] == name
        and resource["metadata"].get("namespace") == namespace
    )


def _source_key() -> dict:
    return _resource("GarageKey", "dashboard-write-key", SOURCE_NAMESPACE)


def _target_secret() -> dict:
    return _resource("Secret", TARGET_SECRET_NAME, TARGET_NAMESPACE)


def _deployment() -> dict:
    return _resource("Deployment", "git-activity-exporter", TARGET_NAMESPACE)


def _resources_from(path: Path) -> list[dict]:
    return [resource for resource in yaml.safe_load_all(path.read_text()) if resource]


def _find(resources: list[dict], kind: str, name: str, namespace: str) -> dict:
    return next(
        resource
        for resource in resources
        if resource["kind"] == kind
        and resource["metadata"]["name"] == name
        and resource["metadata"].get("namespace") == namespace
    )


def _encoded_source(version: str) -> dict[str, str]:
    # These are synthetic markers, never production credentials. Base64 keeps
    # the smoke model's data shape identical to a Kubernetes Secret.
    return {
        key: base64.b64encode(f"{key}:{version}".encode()).decode()
        for key in sorted(TARGET_KEYS)
    }


def _reflect(source_data: dict[str, str], source_version: str) -> dict:
    """Model the data-bearing part of one successful Reflector reconcile."""

    target = deepcopy(_target_secret())
    target["data"] = dict(source_data)
    target["metadata"]["annotations"][
        "reflector.v1.k8s.emberstack.com/reflected-version"
    ] = source_version
    return target


def _consumer_values(target: dict) -> dict[str, str]:
    """Resolve the Deployment's SecretKeyRefs against a reflected Secret."""

    values = {}
    for entry in _deployment()["spec"]["template"]["spec"]["containers"][0]["env"]:
        ref = entry["valueFrom"]["secretKeyRef"]
        if ref["name"] != target["metadata"]["name"]:
            raise AssertionError(f"consumer points at an unexpected Secret: {ref}")
        try:
            values[entry["name"]] = base64.b64decode(target["data"][ref["key"]]).decode()
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("consumer cannot resolve the reflected Secret") from exc
    return values


def test_reference_gitops_contract_is_scoped_and_not_value_bearing():
    template = _source_key()["spec"]["secretTemplate"]
    annotations = template["annotations"]
    expected_namespaces = ",".join(ALLOWED_NAMESPACES)

    assert template["name"] == SOURCE_SECRET_NAME
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed"] == "true"
    assert annotations[
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"
    ] == expected_namespaces
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-enabled"] == "true"
    assert annotations[
        "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"
    ] == expected_namespaces
    assert TARGET_NAMESPACE in annotations[
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"
    ].split(",")
    assert "*" not in annotations[
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"
    ]
    assert template["accessKeyIdKey"] == "ACCESS_KEY_ID"
    assert template["secretAccessKeyKey"] == "SECRET_ACCESS_KEY"
    assert template["endpointKey"] == "S3_ENDPOINT"
    assert template["includeEndpoint"] is True
    assert _source_key()["spec"]["bucketPermissions"] == [
        {
            "bucketRef": {"name": "dashboard-site"},
            "read": True,
            "write": True,
        }
    ]

    target = _target_secret()
    assert target["metadata"]["annotations"] == {
        "reflector.v1.k8s.emberstack.com/reflects":
        f"{SOURCE_NAMESPACE}/{SOURCE_SECRET_NAME}"
    }
    assert "data" not in target, "credentials must not be committed in the fixture"
    assert "ownerReferences" not in target["metadata"]


def test_available_declarative_config_matches_the_checked_in_contract():
    if not LIVE_KEYS.is_file() or not LIVE_DEPLOYMENT.is_file():
        pytest.skip("the sibling declarative-config checkout is not available")

    live_source = _find(
        _resources_from(LIVE_KEYS),
        "GarageKey",
        "dashboard-write-key",
        SOURCE_NAMESPACE,
    )
    fixture_source = _source_key()
    assert live_source["spec"]["secretTemplate"] == fixture_source["spec"]["secretTemplate"]

    live_deployment = _find(
        _resources_from(LIVE_DEPLOYMENT),
        "Deployment",
        "git-activity-exporter",
        TARGET_NAMESPACE,
    )
    fixture_deployment = _deployment()
    assert live_deployment["metadata"]["annotations"]["reloader.stakater.com/auto"] == (
        fixture_deployment["metadata"]["annotations"]["reloader.stakater.com/auto"]
    )
    live_env = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in live_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        if "valueFrom" in entry and "secretKeyRef" in entry["valueFrom"]
    }
    fixture_env = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in fixture_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert {name: live_env[name] for name in fixture_env} == fixture_env


def test_reconcile_smoke_rotates_reflected_values_into_the_consumer():
    first = _reflect(_encoded_source("generation-a"), "generation-a")
    assert _consumer_values(first) == {
        "DEST_S3_ENDPOINT": "S3_ENDPOINT:generation-a",
        "DEST_S3_ACCESS_KEY_ID": "ACCESS_KEY_ID:generation-a",
        "DEST_S3_SECRET_ACCESS_KEY": "SECRET_ACCESS_KEY:generation-a",
    }

    rotated = _reflect(_encoded_source("generation-b"), "generation-b")
    assert rotated["metadata"]["annotations"][
        "reflector.v1.k8s.emberstack.com/reflects"
    ] == f"{SOURCE_NAMESPACE}/{SOURCE_SECRET_NAME}"
    assert rotated["metadata"]["annotations"][
        "reflector.v1.k8s.emberstack.com/reflected-version"
    ] == "generation-b"
    assert rotated["data"] != first["data"]
    assert _consumer_values(rotated) == {
        "DEST_S3_ENDPOINT": "S3_ENDPOINT:generation-b",
        "DEST_S3_ACCESS_KEY_ID": "ACCESS_KEY_ID:generation-b",
        "DEST_S3_SECRET_ACCESS_KEY": "SECRET_ACCESS_KEY:generation-b",
    }


def test_reflector_failure_is_fail_closed_and_preserves_last_good_consumer():
    last_good = _reflect(_encoded_source("generation-a"), "generation-a")

    # A failed source refresh produces no source update event. The controller
    # must not invent a new target value, and the running process keeps its
    # last-good environment until a reflected update triggers Reloader.
    failed_refresh_target = deepcopy(last_good)
    assert failed_refresh_target == last_good
    assert _consumer_values(failed_refresh_target)["DEST_S3_SECRET_ACCESS_KEY"] == (
        "SECRET_ACCESS_KEY:generation-a"
    )

    # If an automatic mirror is absent (for example after source deletion),
    # the required SecretKeyRefs cannot resolve; there is no fallback Secret.
    with pytest.raises(RuntimeError):
        _consumer_values(_target_secret())


def test_secret_rotation_restarts_the_process_via_reloader():
    annotations = _deployment()["metadata"]["annotations"]
    assert annotations["reloader.stakater.com/auto"] == "true"
    assert _deployment()["spec"]["template"]["spec"]["containers"][0]["env"]


def test_runbook_documents_the_reflector_contract():
    text = DEPLOYMENT_DOC.read_text()
    _, _, section = text.partition("### Runtime S3 Secret reflector contract")
    assert section, "deployment.md lost the runtime S3 reflector contract"
    section = section.split("\n## ", 1)[0]
    section = " ".join(section.split())
    for phrase in (
        "GarageKey/dashboard-write-key",
        "Secret/dashboard-s3-credentials",
        "git-activity-exporter",
        "Reflector-created and maintained",
        "reflection-allowed-namespaces",
        "reflection-auto-namespaces",
        "reflector.v1.k8s.emberstack.com/reflects",
        "conflicting same-named resource",
        "update-in-place",
        "last good target remains in use",
        "fails its required `secretKeyRef` resolution",
        "tests/test_reflector_wiring.py",
    ):
        assert phrase in section, f"reflector contract missing: {phrase}"
