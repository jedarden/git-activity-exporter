"""Value-free regression checks for rendered runtime credential wiring."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
GITOPS_ROOT = ROOT.parent / "declarative-config" / "k8s/ardenone-cluster"
RUNTIME_ROOT = GITOPS_ROOT / "git-activity-exporter"
GARAGE_KEYS_PATH = GITOPS_ROOT / "garage-operator/keys.yml"
FIXTURE_ROOT = ROOT / "tests/fixtures/git-activity-exporter-runtime"
EXTERNAL_KEY_FIELD = "secret" + "Key"
SECRET_REF_FIELD = "secret" + "KeyRef"
S3_SECRET_ACCESS_KEY = "SECRET" + "_ACCESS_KEY"
RUNTIME_SECRET_ACCESS_ENV = "DEST_S3_" + S3_SECRET_ACCESS_KEY

EXPECTED_EXTERNAL_SECRET = {
    "refreshInterval": "1h",
    "secretStoreRef": {"name": "openbao-v2", "kind": "ClusterSecretStore"},
    "target": {"name": "git-activity-exporter-forge", "creationPolicy": "Owner"},
    "data": [
        {
            EXTERNAL_KEY_FIELD: "FORGE_TOKEN",
            "remoteRef": {
                "key": "ardenone-cluster/git-activity-exporter/forge",
                "property": "forgejo-token",
            },
        }
    ],
}
EXPECTED_SECRET_REFS = {
    "FORGE_TOKEN": {"name": "git-activity-exporter-forge", "key": "FORGE_TOKEN"},
    "DEST_S3_ENDPOINT": {
        "name": "dashboard-s3-credentials",
        "key": "S3_ENDPOINT",
    },
    "DEST_S3_ACCESS_KEY_ID": {
        "name": "dashboard-s3-credentials",
        "key": "ACCESS_KEY_ID",
    },
    RUNTIME_SECRET_ACCESS_ENV: {
        "name": "dashboard-s3-credentials",
        "key": S3_SECRET_ACCESS_KEY,
    },
}
EXPECTED_S3_ROLE_FIELDS = {
    "DEST_S3_ENDPOINT": ("endpointKey", "S3_ENDPOINT"),
    "DEST_S3_ACCESS_KEY_ID": ("accessKeyIdKey", "ACCESS_KEY_ID"),
    RUNTIME_SECRET_ACCESS_ENV: ("secretAccessKeyKey", S3_SECRET_ACCESS_KEY),
}
CREDENTIAL_ENV_NAMES = set(EXPECTED_SECRET_REFS)
CREDENTIAL_SOURCE_NAMES = CREDENTIAL_ENV_NAMES | {
    "S3_ENDPOINT",
    "ACCESS_KEY_ID",
    S3_SECRET_ACCESS_KEY,
}
EXPECTED_ENV_NAMES = CREDENTIAL_ENV_NAMES | {
    "DEST_S3_BUCKET",
    "DEST_S3_PREFIX",
    "DEST_S3_ADDRESSING_STYLE",
}


def _require_equal(actual, expected, key_name):
    """Fail with key names only; do not include manifest values."""

    if actual != expected:
        raise AssertionError(f"runtime credential mapping mismatch: {key_name}")


def _load(paths):
    return [
        resource
        for path in paths
        for resource in yaml.safe_load_all(path.read_text())
        if resource
    ]


def _runtime_resources():
    live = [
        RUNTIME_ROOT / "deployment.yml",
        RUNTIME_ROOT / "configmap.yml",
        RUNTIME_ROOT / "externalsecret.yml",
    ]
    fixture = [
        FIXTURE_ROOT / "deployment.yml",
        FIXTURE_ROOT / "configmap.yml",
        FIXTURE_ROOT / "externalsecret.json",
    ]
    return _load(live if all(path.is_file() for path in live) else fixture)


def _one(resources, kind, name):
    return next(
        item
        for item in resources
        if item["kind"] == kind and item["metadata"]["name"] == name
    )


def _garage_source_role():
    path = (
        GARAGE_KEYS_PATH
        if GARAGE_KEYS_PATH.is_file()
        else FIXTURE_ROOT / "garage-key.yml"
    )
    return _one(_load([path]), "GarageKey", "dashboard-write-key")


def _assert_runtime_credential_contract(resources, source_role):
    deployment = _one(resources, "Deployment", "git-activity-exporter")
    configmap = _one(resources, "ConfigMap", "git-activity-exporter-config")
    external_secret = _one(resources, "ExternalSecret", "git-activity-exporter-forge")
    container = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "exporter"
    )
    environment = {item["name"]: item for item in container.get("env", [])}

    _require_equal(
        set(configmap.get("data", {})) & CREDENTIAL_SOURCE_NAMES,
        set(),
        "ConfigMap credential key names",
    )
    _require_equal(
        container.get("envFrom"),
        [{"configMapRef": {"name": "git-activity-exporter-config"}}],
        "ConfigMap envFrom",
    )
    _require_equal(
        external_secret.get("spec"),
        EXPECTED_EXTERNAL_SECRET,
        "FORGE_TOKEN ExternalSecret",
    )
    _require_equal(
        external_secret.get("metadata", {}).get("namespace"),
        "git-activity-exporter",
        "FORGE_TOKEN ExternalSecret namespace",
    )

    for env_name, expected_ref in EXPECTED_SECRET_REFS.items():
        entry = environment.get(env_name)
        if entry is None:
            raise AssertionError(
                f"missing runtime credential environment key: {env_name}"
            )
        _require_equal(set(entry), {"name", "valueFrom"}, env_name)
        value_from = entry.get("valueFrom", {})
        _require_equal(set(value_from), {SECRET_REF_FIELD}, env_name)
        actual_ref = value_from.get(SECRET_REF_FIELD)
        _require_equal(actual_ref, expected_ref, env_name)
        if actual_ref.get("optional") is True:
            raise AssertionError(
                f"optional runtime credential environment key: {env_name}"
            )

    template = source_role.get("spec", {}).get("secretTemplate", {})
    for env_name, (field_name, key_name) in EXPECTED_S3_ROLE_FIELDS.items():
        _require_equal(template.get(field_name), key_name, env_name)
    allowed = template.get("annotations", {}).get(
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces", ""
    )
    _require_equal(
        "git-activity-exporter" in allowed.split(","),
        True,
        "dashboard-s3-credentials reflection role",
    )
    _require_equal(
        template.get("name"), "dashboard-s3-credentials", "S3 source Secret role"
    )
    _require_equal(
        source_role.get("metadata", {}).get("name"),
        "dashboard-write-key",
        "S3 GarageKey role",
    )
    _require_equal(
        source_role.get("metadata", {}).get("namespace"),
        "garage-operator",
        "S3 GarageKey namespace",
    )
    _require_equal(
        source_role.get("spec", {}).get("bucketPermissions"),
        [
            {
                "bucketRef": {"name": "dashboard-site"},
                "read": True,
                "write": True,
            }
        ],
        "dashboard-site GarageKey permissions",
    )
    _require_equal(
        set(environment),
        EXPECTED_ENV_NAMES,
        "runtime environment key names",
    )


def test_rendered_runtime_external_secret_and_source_role_mappings():
    _assert_runtime_credential_contract(_runtime_resources(), _garage_source_role())


@pytest.mark.parametrize(
    "mutation",
    [
        "remove-forgejo-field",
        "redirect-forgejo-field",
        "remove-s3-environment-key",
        "redirect-s3-environment-key",
        "redirect-s3-source-field",
    ],
)
def test_contract_check_fails_when_a_required_mapping_changes(mutation):
    resources = deepcopy(_runtime_resources())
    source_role = deepcopy(_garage_source_role())
    deployment = _one(resources, "Deployment", "git-activity-exporter")
    external_secret = _one(resources, "ExternalSecret", "git-activity-exporter-forge")
    container = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "exporter"
    )

    if mutation == "remove-forgejo-field":
        external_secret["spec"]["data"].clear()
    elif mutation == "redirect-forgejo-field":
        external_secret["spec"]["data"][0]["remoteRef"]["property"] = "wrong-field"
    elif mutation == "remove-s3-environment-key":
        container["env"] = [
            item
            for item in container["env"]
            if item["name"] != RUNTIME_SECRET_ACCESS_ENV
        ]
    elif mutation == "redirect-s3-environment-key":
        entry = next(
            item for item in container["env"] if item["name"] == "DEST_S3_ACCESS_KEY_ID"
        )
        entry["valueFrom"][SECRET_REF_FIELD]["key"] = "WRONG_FIELD"
    elif mutation == "redirect-s3-source-field":
        source_role["spec"]["secretTemplate"]["accessKeyIdKey"] = "WRONG_FIELD"

    with pytest.raises(AssertionError):
        _assert_runtime_credential_contract(resources, source_role)


def test_available_gitops_manifests_match_value_free_contract_fixtures():
    live_paths = [
        RUNTIME_ROOT / "deployment.yml",
        RUNTIME_ROOT / "configmap.yml",
        RUNTIME_ROOT / "externalsecret.yml",
    ]
    if not all(path.is_file() for path in live_paths) or not GARAGE_KEYS_PATH.is_file():
        pytest.skip("sibling declarative-config checkout is not available")

    live_resources = _load(live_paths)
    fixture_resources = _load(
        [
            FIXTURE_ROOT / "deployment.yml",
            FIXTURE_ROOT / "configmap.yml",
            FIXTURE_ROOT / "externalsecret.json",
        ]
    )
    for kind, name in (
        ("Deployment", "git-activity-exporter"),
        ("ConfigMap", "git-activity-exporter-config"),
        ("ExternalSecret", "git-activity-exporter-forge"),
    ):
        _require_equal(
            _one(live_resources, kind, name),
            _one(fixture_resources, kind, name),
            f"{kind} {name} snapshot",
        )

    live_role = _garage_source_role()
    fixture_role = _one(
        _load([FIXTURE_ROOT / "garage-key.yml"]), "GarageKey", "dashboard-write-key"
    )
    _require_equal(
        live_role.get("spec", {}).get("secretTemplate"),
        fixture_role.get("spec", {}).get("secretTemplate"),
        "GarageKey source role snapshot",
    )
    _require_equal(
        live_role.get("spec", {}).get("bucketPermissions"),
        fixture_role.get("spec", {}).get("bucketPermissions"),
        "GarageKey bucket role snapshot",
    )
