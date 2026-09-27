import logging

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

    The publish protocol snapshots the fixed keys before overwriting them,
    and putting a snapshot back must reproduce the content type too, not
    just the bytes.
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


def upload_bytes(s3, bucket: str, key: str, data: bytes, content_type: str):
    _call(
        lambda: s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type),
        f"S3 PUT s3://{bucket}/{key}",
    )
    log.info("uploaded %d bytes to s3://%s/%s", len(data), bucket, key)
