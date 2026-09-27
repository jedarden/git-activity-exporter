import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "examples" / "self-hosting"


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
    assert services["exporter"]["image"].endswith(":0.1.28}")
    assert ":latest" not in services["exporter"]["image"]
    assert services["exporter"]["environment"] == {
        "FORGE_BASE_URL": "http://forgejo-fixture:8081",
        "FORGE_OWNER": "reuser",
        "FORGE_TOKEN": "self-hosting-forge-token",
        "GIT_CONFIG_SYSTEM": "/etc/git-activity-exporter/gitconfig",
        "FAMILIES_FILE": "/etc/git-activity-exporter/families.yaml",
        "CLONE_ROOT": "/data/mirrors",
        "DEST_S3_ENDPOINT": "http://s3-fixture:9000",
        "DEST_S3_BUCKET": "reuser-git-activity",
        "DEST_S3_PREFIX": "exports/reuser",
        "DEST_S3_ACCESS_KEY_ID": "self-hosting-access-key",
        "DEST_S3_SECRET_ACCESS_KEY": "self-hosting-secret-key",
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
    container = deployment["spec"]["template"]["spec"]["containers"][0]
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
