import logging
import re
import uuid

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)

from .config import S3Endpoint
from . import retry

log = logging.getLogger(__name__)

_TRANSIENT_ERROR_CODES = {
    "InternalError",
    "RequestTimeout",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
}


class ImmutableObjectError(RuntimeError):
    """An immutable S3 object exists with bytes or metadata that differ."""


class S3PermissionError(RuntimeError):
    """The destination failed the startup permission preflight."""


_PERMISSION_PROBE_CONTENT = b"git-activity-exporter S3 permission probe\n"
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _safe_error_summary(error: Exception) -> str:
    """Summarize a remote error without copying its provider message.

    Provider messages can echo request data, endpoint details, or credentials.
    Error codes and HTTP statuses are sufficient to identify a missing scope
    while keeping the startup log safe to expose to operators.
    """
    if isinstance(error, ClientError):
        response = error.response or {}
        error_info = response.get("Error", {})
        code = error_info.get("Code")
        if not isinstance(code, str) or not _SAFE_ERROR_CODE.fullmatch(code):
            code = "provider-error"
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return f"{code} (HTTP {status})"
        return code
    return type(error).__name__


def _permission_probe_key(prefix: str) -> str:
    prefix = prefix.strip("/")
    suffix = f".git-activity-exporter-permission-check/{uuid.uuid4().hex}"
    return f"{prefix}/{suffix}" if prefix else suffix


def check_permissions(s3, bucket: str, prefix: str):
    """Verify every S3 operation required by collection and publication.

    The check uses a unique temporary object so a missing object cannot be
    mistaken for a successful GetObject permission. It always attempts to
    delete a probe after PutObject has been attempted, including when a later
    check fails. Only a sanitized operation summary is exposed to callers.
    """
    key = _permission_probe_key(prefix)
    content_type = "application/octet-stream"
    operation = "ListBucket"
    probe_attempted = False
    failure = None
    cleanup_failure = None

    try:
        s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)

        operation = "PutObject"
        # The request may reach S3 even when the client loses its response, so
        # cleanup is attempted whenever the PUT has been started.
        probe_attempted = True
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=_PERMISSION_PROBE_CONTENT,
            ContentType=content_type,
        )

        operation = "HeadObject (metadata)"
        metadata = s3.head_object(Bucket=bucket, Key=key)
        if metadata.get("ContentLength") != len(_PERMISSION_PROBE_CONTENT):
            raise ValueError("unexpected probe content length")
        if metadata.get("ContentType") != content_type:
            raise ValueError("unexpected probe content type")

        operation = "GetObject"
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            if body.read() != _PERMISSION_PROBE_CONTENT:
                raise ValueError("unexpected probe body")
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
    except Exception as error:
        failure = (operation, _safe_error_summary(error))
    finally:
        if probe_attempted:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception as error:
                cleanup_failure = ("DeleteObject", _safe_error_summary(error))

    if failure is None and cleanup_failure is not None:
        failure = cleanup_failure
    if failure is not None:
        operation, summary = failure
        message = f"S3 destination permission preflight failed for {operation}: {summary}"
        if cleanup_failure is not None and failure != cleanup_failure:
            cleanup_operation, cleanup_summary = cleanup_failure
            message += f"; cleanup {cleanup_operation} failed: {cleanup_summary}"
        raise S3PermissionError(message) from None


def _is_transient(error: Exception) -> bool:
    if isinstance(error, ClientError):
        response = error.response or {}
        error_info = response.get("Error", {})
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return (
            error_info.get("Code") in _TRANSIENT_ERROR_CODES
            or (status is not None and (status >= 500 or status == 429))
        )
    return isinstance(error, (
        ConnectTimeoutError,
        ConnectionClosedError,
        EndpointConnectionError,
        ProxyConnectionError,
        ReadTimeoutError,
        SSLError,
    ))


def _call(operation, label):
    return retry.call(operation, is_retryable=_is_transient, label=label)


def client(endpoint: S3Endpoint):
    return boto3.client(
        "s3",
        endpoint_url=endpoint.endpoint_url,
        aws_access_key_id=endpoint.access_key_id,
        aws_secret_access_key=endpoint.secret_access_key,
        region_name=endpoint.region,
        # boto3's own adaptive/standard retries would otherwise compose with
        # the application policy and make the attempt bound unknowable.
        config=BotoConfig(
            s3={"addressing_style": endpoint.addressing_style},
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )


def download_bytes(s3, bucket: str, key: str):
    """Returns the object body, or None if it doesn't exist yet (first run)."""
    try:
        return _call(
            lambda: s3.get_object(Bucket=bucket, Key=key)["Body"].read(),
            f"S3 GET s3://{bucket}/{key}",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def fetch_object(s3, bucket: str, key: str):
    """(body, content_type), or None if the object doesn't exist.

    Publication recovery compares both bytes and content type so a repaired
    fixed key has the same object metadata as its immutable source.
    """
    try:
        def get():
            resp = s3.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read(), resp.get("ContentType")

        return _call(get, f"S3 GET s3://{bucket}/{key}")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def delete_key(s3, bucket: str, key: str):
    _call(
        lambda: s3.delete_object(Bucket=bucket, Key=key),
        f"S3 DELETE s3://{bucket}/{key}",
    )


def prefix_exists(s3, bucket: str, prefix: str) -> bool:
    """Whether any object exists below prefix."""
    resp = _call(
        lambda: s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1),
        f"S3 LIST s3://{bucket}/{prefix}",
    )
    return bool(resp.get("Contents"))


def list_prefixes(s3, bucket: str, prefix: str) -> list:
    """The immediate child "directories" of prefix, as full prefixes.

    Paginated by continuation token rather than get_paginator so the
    protocol's fake S3 in tests only has to model raw list_objects_v2.
    """
    out = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _call(
            lambda: s3.list_objects_v2(**kwargs),
            f"S3 LIST s3://{bucket}/{prefix}",
        )
        out.extend(cp["Prefix"] for cp in resp.get("CommonPrefixes", []))
        token = resp.get("NextContinuationToken")
        if not token:
            return out


def list_keys(s3, bucket: str, prefix: str) -> list:
    """Every object key under prefix, paginated."""
    out = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _call(
            lambda: s3.list_objects_v2(**kwargs),
            f"S3 LIST s3://{bucket}/{prefix}",
        )
        out.extend(obj["Key"] for obj in resp.get("Contents", []))
        token = resp.get("NextContinuationToken")
        if not token:
            return out


def upload_bytes(
    s3,
    bucket: str,
    key: str,
    data: bytes,
    content_type: str,
    cache_control: str = None,
):
    kwargs = {
        "Bucket": bucket,
        "Key": key,
        "Body": data,
        "ContentType": content_type,
    }
    if cache_control is not None:
        kwargs["CacheControl"] = cache_control
    _call(
        lambda: s3.put_object(**kwargs),
        f"S3 PUT s3://{bucket}/{key}",
    )
    log.info("uploaded %d bytes to s3://%s/%s", len(data), bucket, key)


def upload_immutable_bytes(
    s3,
    bucket: str,
    key: str,
    data: bytes,
    content_type: str,
    cache_control: str = None,
):
    """Create an object without ever overwriting a different existing value.

    A PUT can succeed at S3 and still lose its response to the caller. A
    normal retry would then overwrite the staged object, which is harmless for
    fixed keys but violates the cycle immutability guarantee. Check before the
    PUT and, after an exception, accept the operation only when a GET finds
    the exact requested bytes and content type. A retry is safe only when the
    object is still absent.
    """
    data = bytes(data)

    def operation():
        existing = fetch_object(s3, bucket, key)
        if existing is not None:
            _assert_immutable_match(key, existing, data, content_type)
            return

        try:
            kwargs = {
                "Bucket": bucket,
                "Key": key,
                "Body": data,
                "ContentType": content_type,
            }
            if cache_control is not None:
                kwargs["CacheControl"] = cache_control
            s3.put_object(**kwargs)
        except Exception as error:
            # Resolve an ambiguous outcome before retrying. S3 is strongly
            # read-after-write consistent, so an exact object means the PUT
            # landed even if its response did not.
            observed = fetch_object(s3, bucket, key)
            if observed is not None:
                try:
                    _assert_immutable_match(key, observed, data, content_type)
                except ImmutableObjectError as mismatch:
                    raise mismatch from error
                return
            raise

    _call(operation, f"S3 immutable PUT s3://{bucket}/{key}")
    log.info("uploaded immutable %d bytes to s3://%s/%s", len(data), bucket, key)


def _assert_immutable_match(key: str, observed, data: bytes, content_type: str):
    observed_data, observed_content_type = observed
    if observed_data != data or observed_content_type != content_type:
        raise ImmutableObjectError(
            f"immutable object s3://{key} already contains different data"
        )
