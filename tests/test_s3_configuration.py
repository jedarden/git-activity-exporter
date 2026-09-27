import json

import pytest

from src import config, main, s3io
from tests.fake_s3 import FakeS3


DESTINATION_ENV = {
    "FORGE_TOKEN": "forge-token",
    "DEST_S3_ENDPOINT": "https://s3.test",
    "DEST_S3_BUCKET": "activity-bucket",
    "DEST_S3_ACCESS_KEY_ID": "access-key",
    "DEST_S3_SECRET_ACCESS_KEY": "secret-key",
}


def _set_destination_env(monkeypatch, style):
    for name, value in DESTINATION_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEST_S3_REGION", "eu-west-2")
    monkeypatch.setenv("DEST_S3_ADDRESSING_STYLE", style)
    # More than one trailing slash catches both the config boundary and any
    # accidental second slash in the generated object keys.
    monkeypatch.setenv("DEST_S3_PREFIX", "reports/activity///")


@pytest.mark.parametrize("style", ["path", "virtual"])
def test_destination_environment_wires_boto_client_configuration(
    monkeypatch, tmp_path, style
):
    _set_destination_env(monkeypatch, style)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("test\n")

    cfg = config.load()
    assert cfg.dest.region == "eu-west-2"
    assert cfg.dest.addressing_style == style
    assert cfg.dest_prefix == "reports/activity"

    calls = []
    sentinel = object()

    def fake_client(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(s3io.boto3, "client", fake_client)

    assert s3io.client(cfg.dest) is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "https://s3.test"
    assert kwargs["aws_access_key_id"] == "access-key"
    assert kwargs["aws_secret_access_key"] == "secret-key"
    assert kwargs["region_name"] == "eu-west-2"
    assert kwargs["config"].s3 == {"addressing_style": style}
    assert kwargs["config"].retries == {
        "mode": "standard",
        "total_max_attempts": 1,
    }


class RecordingS3(FakeS3):
    def __init__(self):
        super().__init__()
        self.put_calls = []

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl=None):
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "ContentType": ContentType,
                "CacheControl": CacheControl,
            }
        )
        return super().put_object(Bucket, Key, Body, ContentType, CacheControl)


@pytest.mark.parametrize("style", ["path", "virtual"])
def test_configured_prefix_generates_documented_object_keys(
    monkeypatch, tmp_path, style
):
    _set_destination_env(monkeypatch, style)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("test\n")
    cfg = config.load()
    s3 = RecordingS3()

    monkeypatch.setattr(
        main,
        "_collect",
        lambda *args: (
            [],
            [],
            [],
            {
                "repos_total": 0,
                "repos_scanned": 0,
                "repos_failed": [],
                "repo_errors": {},
                "repos_stale": [],
                "repos_partial_history": [],
                "mirrors_pruned": [],
                "repos_with_bead_data": 0,
            },
        ),
    )

    main._run_cycle(cfg, s3, {})

    prefix = cfg.dest_prefix
    pointer_key = f"{prefix}/current.json"
    pointer = json.loads(s3.objects[pointer_key][0])
    names = ["hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"]
    expected_keys = (
        [
            f"{prefix}/cycles/{pointer['cycle_id']}/{name}"
            for name in names
        ]
        + [f"{prefix}/{name}" for name in names]
        + [pointer_key]
    )

    assert [call["Bucket"] for call in s3.put_calls] == ["activity-bucket"] * 9
    assert [call["Key"] for call in s3.put_calls] == expected_keys
    assert [call["ContentType"] for call in s3.put_calls[:4]] == [
        "application/octet-stream",
        "application/octet-stream",
        "application/octet-stream",
        "application/json",
    ]
    assert [call["ContentType"] for call in s3.put_calls[4:8]] == [
        "application/octet-stream",
        "application/octet-stream",
        "application/octet-stream",
        "application/json",
    ]
    assert s3.put_calls[8]["ContentType"] == "application/json"
    assert set(s3.objects) == set(expected_keys)
    assert all(key.startswith(f"{prefix}/") for key in s3.objects)
    assert all("//" not in key for key in s3.objects)
    assert pointer["objects"] == {
        name: f"cycles/{pointer['cycle_id']}/{name}" for name in names
    }
    assert [f"{prefix}/{key}" for key in pointer["objects"].values()] == expected_keys[:4]
    assert [call["CacheControl"] for call in s3.put_calls[:4]] == [
        "public, max-age=31536000, immutable"
    ] * 4
    assert [call["CacheControl"] for call in s3.put_calls[4:]] == [
        "no-cache, max-age=0, must-revalidate"
    ] * 5
    assert all("//" not in call["Key"] for call in s3.put_calls)
