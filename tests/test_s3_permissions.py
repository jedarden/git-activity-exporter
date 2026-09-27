import pytest
from botocore.exceptions import ClientError

from src import s3io
from tests.fake_s3 import FakeS3


def _access_denied(operation):
    return ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "credentials=should-never-be-logged",
            },
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        operation,
    )


def test_permission_preflight_checks_and_cleans_up_every_operation():
    s3 = FakeS3()

    s3io.check_permissions(s3, "activity-bucket", "exports/activity")

    assert [operation for operation, _key in s3.calls] == [
        "list", "put", "head", "get", "delete",
    ]
    assert s3.objects == {}


def test_permission_preflight_reports_only_safe_operation_details(monkeypatch):
    s3 = FakeS3()
    s3.fail_when(lambda op, _key: _access_denied("PutObject") if op == "put" else None)

    with pytest.raises(s3io.S3PermissionError) as raised:
        s3io.check_permissions(s3, "activity-bucket", "exports/activity")

    message = str(raised.value)
    assert "PutObject" in message
    assert "AccessDenied" in message
    assert "HTTP 403" in message
    assert "should-never-be-logged" not in message
    assert "credentials" not in message
    assert s3.objects == {}


def test_permission_preflight_reports_cleanup_failure_without_provider_message():
    s3 = FakeS3()

    def fail_delete(op, _key):
        if op == "delete":
            return _access_denied("DeleteObject")
        return None

    s3.fail_when(fail_delete)

    with pytest.raises(s3io.S3PermissionError, match="DeleteObject.*AccessDenied"):
        s3io.check_permissions(s3, "activity-bucket", "exports/activity")
