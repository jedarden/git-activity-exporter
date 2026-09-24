import logging

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import S3Endpoint

log = logging.getLogger(__name__)


def client(endpoint: S3Endpoint):
    return boto3.client(
        "s3",
        endpoint_url=endpoint.endpoint_url,
        aws_access_key_id=endpoint.access_key_id,
        aws_secret_access_key=endpoint.secret_access_key,
        region_name=endpoint.region,
        config=BotoConfig(s3={"addressing_style": endpoint.addressing_style}),
    )


def download_bytes(s3, bucket: str, key: str):
    """Returns the object body, or None if it doesn't exist yet (first run)."""
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
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
        resp = s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read(), resp.get("ContentType")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def delete_key(s3, bucket: str, key: str):
    s3.delete_object(Bucket=bucket, Key=key)


def prefix_exists(s3, bucket: str, prefix: str) -> bool:
    """Whether any object exists below prefix."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
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
        resp = s3.list_objects_v2(**kwargs)
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
        resp = s3.list_objects_v2(**kwargs)
        out.extend(obj["Key"] for obj in resp.get("Contents", []))
        token = resp.get("NextContinuationToken")
        if not token:
            return out


def upload_bytes(s3, bucket: str, key: str, data: bytes, content_type: str):
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    log.info("uploaded %d bytes to s3://%s/%s", len(data), bucket, key)
