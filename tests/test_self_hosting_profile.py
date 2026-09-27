import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "examples" / "self-hosting"
VERSION = (ROOT / "VERSION").read_text().strip()


def _load_families_in_new_process(path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from src import families; "
                "print(json.dumps(families.load(sys.argv[1]), sort_keys=True))"
            ),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _resources():
    return [resource for resource in yaml.safe_load_all(
        (PROFILE / "kubernetes.yaml").read_text()
    ) if resource]


def _resource(kind, name):
    return next(
        resource for resource in _resources()
        if resource["kind"] == kind and resource["metadata"]["name"] == name
    )


def test_self_hosting_profile_is_opt_in_and_non_author_specific():
    compose = yaml.safe_load((PROFILE / "compose.yaml").read_text())
    services = compose["services"]
    assert services["exporter"]["profiles"] == ["self-hosting"]
    assert services["exporter"]["image"].endswith(f":{VERSION}" + "}")
    assert ":latest" not in services["exporter"]["image"]
    assert services["exporter"]["environment"] == {
        "FORGE_BASE_URL": "http://forgejo-fixture:8081",
        "FORGE_OWNER": "reuser",
        "FORGE_TOKEN": "${SMOKE_FORGE_TOKEN}",
        "GIT_CONFIG_SYSTEM": "/etc/git-activity-exporter/gitconfig",
        "FAMILIES_FILE": "/etc/git-activity-exporter/families.yaml",
        "CLONE_ROOT": "/data/mirrors",
        "DEST_S3_ENDPOINT": "http://s3-fixture:9000",
        "DEST_S3_BUCKET": "reuser-git-activity",
        "DEST_S3_PREFIX": "exports/reuser",
        "DEST_S3_ACCESS_KEY_ID": "${SMOKE_S3_ACCESS_KEY_ID}",
        "DEST_S3_SECRET_ACCESS_KEY": "${SMOKE_S3_SECRET_ACCESS_KEY}",
        "DEST_S3_ADDRESSING_STYLE": "path",
        "WINDOW_DAYS": "30",
        "SHALLOW_SINCE_DAYS": "40",
        "POLL_INTERVAL_SECONDS": "3600",
    }
    assert any(
        volume.endswith(":/data/mirrors")
        for volume in services["exporter"]["volumes"]
    )


def test_self_hosting_kubernetes_profile_has_pinned_single_writer_and_pvc():
    deployment = _resource("Deployment", "git-activity-exporter")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["metadata"]["annotations"] == {
        "configmap.reloader.stakater.com/reload": "git-activity-exporter-families",
    }
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith(f":{VERSION}")
    assert re.fullmatch(r"[^:]+:[0-9]+\.[0-9]+\.[0-9]+", container["image"])
    assert ":latest" not in container["image"]

    config = _resource("ConfigMap", "git-activity-exporter-config")["data"]
    assert config["FORGE_OWNER"] == "reuser"
    assert config["FAMILIES_FILE"] == "/etc/git-activity-exporter/families.yaml"
    assert config["DEST_S3_BUCKET"] == "reuser-git-activity"
    assert config["DEST_S3_PREFIX"] == "exports/reuser"

    pvc = _resource("PersistentVolumeClaim", "git-activity-exporter-mirrors")
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "20Gi"
    claim_names = {
        volume["persistentVolumeClaim"]["claimName"]
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if "persistentVolumeClaim" in volume
    }
    assert claim_names == {"git-activity-exporter-mirrors"}

    env = {item["name"]: item for item in container["env"]}
    assert {
        "FORGE_TOKEN",
        "DEST_S3_ENDPOINT",
        "DEST_S3_ACCESS_KEY_ID",
        "DEST_S3_SECRET_ACCESS_KEY",
    } <= env.keys()
    assert all("secretKeyRef" in item["valueFrom"] for item in env.values())


def test_self_hosting_families_file_is_the_mounted_example():
    families = yaml.safe_load((PROFILE / "families.yaml").read_text())
    config_map = _resource("ConfigMap", "git-activity-exporter-families")
    mounted = yaml.safe_load(config_map["data"]["families.yaml"])
    assert families == mounted
    assert families == {"families": {"reuser-projects": ["reuser-project"]}}


def test_family_mapping_change_rolls_out_and_is_loaded_by_the_next_process(tmp_path):
    deployment = _resource("Deployment", "git-activity-exporter")
    config_map = _resource("ConfigMap", "git-activity-exporter-families")
    annotations = deployment["metadata"]["annotations"]
    assert annotations["configmap.reloader.stakater.com/reload"] == config_map[
        "metadata"
    ]["name"]

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {
        volume["name"]: volume["configMap"]["name"]
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if "configMap" in volume
    }["families"] == config_map["metadata"]["name"]
    family_mount = next(
        mount
        for mount in container["volumeMounts"]
        if mount["name"] == "families"
    )
    assert family_mount["mountPath"] == "/etc/git-activity-exporter/families.yaml"

    family_file = tmp_path / "families.yaml"
    initial = {"families": {"initial": ["reuser-project"]}}
    changed = {"families": {"changed": ["reuser-project"]}}
    family_file.write_text(yaml.safe_dump(initial, sort_keys=False))
    assert _load_families_in_new_process(family_file)["reuser-project"] == "initial"

    # A ConfigMap-only GitOps commit changes the mounted file, Reloader rolls
    # out the Deployment, and the replacement process reads the new mapping.
    family_file.write_text(yaml.safe_dump(changed, sort_keys=False))
    assert _load_families_in_new_process(family_file)["reuser-project"] == "changed"


def test_self_hosting_s3_fixture_supports_metadata_preflight():
    fixture = (PROFILE / "mock-s3.py").read_text()

    # The smoke exporter calls HeadObject against this fixture before it makes
    # its first Forgejo request. Keep the fixture's metadata contract pinned
    # so a future simplification does not make the smoke profile skip a
    # required destination permission.
    assert "def do_HEAD(self):" in fixture
    assert "include_body=False" in fixture
