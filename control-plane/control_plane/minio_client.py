"""MinIO client used by the user-facing file endpoints.

Each user gets their own bucket (`user-<id>-files`). Buckets are created
lazily on first write.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterable

import boto3
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError

_MISSING_CODES = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}
_GENERIC_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream"}

_S3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get(
        "A2A_CP_MINIO_ENDPOINT",
        "http://a2a-infra-minio.a2a-infra.svc.cluster.local:9000",
    ),
    aws_access_key_id=os.environ.get("A2A_CP_MINIO_ACCESS_KEY", ""),
    aws_secret_access_key=os.environ.get("A2A_CP_MINIO_SECRET_KEY", ""),
    region_name="us-east-1",
    config=_BotoConfig(s3={"addressing_style": "path"}),
)


def bucket_for_user(user_id: int) -> str:
    return f"user-{user_id}-files"


def ensure_bucket(bucket: str) -> None:
    try:
        _S3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            _S3.create_bucket(Bucket=bucket)
        elif code == "403":
            return
        else:
            raise


def list_files(bucket: str, prefix: str = "") -> Iterable[dict[str, Any]]:
    ensure_bucket(bucket)
    paginator = _S3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or ():
            key = obj["Key"]
            yield _meta_from_head(
                key,
                {},
                size=int(obj.get("Size") or 0),
                modified_at=obj.get("LastModified"),
                etag=obj.get("ETag"),
            )


def list_files_page(
    bucket: str,
    prefix: str = "",
    *,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    ensure_bucket(bucket)
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": prefix,
        "MaxKeys": max(1, min(limit, 1000)),
    }
    if cursor:
        params["ContinuationToken"] = cursor
    page = _S3.list_objects_v2(**params)
    rows = [
        _meta_from_head(
            obj["Key"],
            {},
            size=int(obj.get("Size") or 0),
            modified_at=obj.get("LastModified"),
            etag=obj.get("ETag"),
        )
        for obj in page.get("Contents") or ()
    ]
    next_cursor = page.get("NextContinuationToken")
    return rows, str(next_cursor) if next_cursor else None


def list_children(bucket: str, prefix: str = "") -> Iterable[dict[str, Any]]:
    """Yield the direct children under ``prefix`` as file and folder entries."""
    ensure_bucket(bucket)
    clean_prefix = prefix.strip("/").rstrip("/")
    paginator = _S3.get_paginator("list_objects_v2")
    params: dict[str, Any] = {"Bucket": bucket, "Delimiter": "/"}
    if clean_prefix:
        params["Prefix"] = f"{clean_prefix}/"
    for page in paginator.paginate(**params):
        for child in page.get("CommonPrefixes") or ():
            key = str(child.get("Prefix") or "").rstrip("/")
            if not key:
                continue
            yield {
                "path": key,
                "size": 0,
                "modified_at": "",
                "content_type": "",
                "is_dir": True,
            }
        for obj in page.get("Contents") or ():
            key = str(obj.get("Key") or "")
            if not key:
                continue
            if key.endswith("/"):
                continue
            if clean_prefix and key.rstrip("/") == clean_prefix:
                continue
            yield _meta_from_head(
                key,
                {},
                size=int(obj.get("Size") or 0),
                modified_at=obj.get("LastModified"),
                etag=obj.get("ETag"),
            )


def upload_file(bucket: str, path: str, data: bytes, content_type: str | None) -> dict[str, Any]:
    ensure_bucket(bucket)
    normalized_content_type = _effective_content_type(path, content_type)
    extra = {"ContentType": normalized_content_type}
    _S3.put_object(Bucket=bucket, Key=path, Body=data, **extra)
    return {
        "path": path,
        "size": len(data),
        "modified_at": _iso(datetime.now(timezone.utc)),
        "content_type": normalized_content_type,
    }


def upload_fileobj(
    bucket: str,
    path: str,
    fileobj: BinaryIO,
    *,
    size: int,
    content_type: str | None,
) -> dict[str, Any]:
    ensure_bucket(bucket)
    normalized_content_type = _effective_content_type(path, content_type)
    fileobj.seek(0)
    _S3.upload_fileobj(
        fileobj,
        bucket,
        path,
        ExtraArgs={"ContentType": normalized_content_type},
    )
    return {
        "path": path,
        "size": size,
        "modified_at": _iso(datetime.now(timezone.utc)),
        "content_type": normalized_content_type,
    }


def delete_file(bucket: str, path: str) -> None:
    ensure_bucket(bucket)
    _head_object(bucket, path)
    try:
        _S3.delete_object(Bucket=bucket, Key=path)
    except ClientError as exc:
        if _is_missing(exc):
            raise FileNotFoundError(path) from exc
        raise


def move_file(bucket: str, src: str, dst: str) -> dict[str, Any]:
    """Copy ``src`` to ``dst`` within the same bucket, then delete the source."""
    ensure_bucket(bucket)
    if src == dst:
        return {"path": dst, "moved": False}
    _head_object(bucket, src)
    try:
        _head_object(bucket, dst)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"{dst} already exists")
    try:
        _S3.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": src},
            Key=dst,
        )
    except ClientError as exc:
        if _is_missing(exc):
            raise FileNotFoundError(src) from exc
        raise
    _S3.delete_object(Bucket=bucket, Key=src)
    return {**stat_file(bucket, dst), "moved": True, "from": src}


def get_file(bucket: str, path: str) -> tuple[bytes, str]:
    ensure_bucket(bucket)
    try:
        resp = _S3.get_object(Bucket=bucket, Key=path)
    except ClientError as exc:
        if _is_missing(exc):
            raise FileNotFoundError(path) from exc
        raise
    body = resp["Body"].read()
    return body, _effective_content_type(path, resp.get("ContentType"))


def iter_file(bucket: str, path: str, *, chunk_size: int = 1024 * 1024) -> tuple[Iterable[bytes], str]:
    ensure_bucket(bucket)
    try:
        resp = _S3.get_object(Bucket=bucket, Key=path)
    except ClientError as exc:
        if _is_missing(exc):
            raise FileNotFoundError(path) from exc
        raise
    stream = resp["Body"]

    def _chunks() -> Iterable[bytes]:
        try:
            for chunk in stream.iter_chunks(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            stream.close()

    return _chunks(), _effective_content_type(path, resp.get("ContentType"))


def stat_file(bucket: str, path: str) -> dict[str, Any]:
    ensure_bucket(bucket)
    return _meta_from_head(path, _head_object(bucket, path))


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def _guess_content_type(path: str) -> str:
    import mimetypes

    if path.lower().endswith((".md", ".markdown")):
        return "text/markdown"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _head_object(bucket: str, path: str) -> dict[str, Any]:
    try:
        return _S3.head_object(Bucket=bucket, Key=path)
    except ClientError as exc:
        if _is_missing(exc):
            raise FileNotFoundError(path) from exc
        raise


def _meta_from_head(
    path: str,
    head: dict[str, Any],
    *,
    size: int | None = None,
    modified_at: datetime | None = None,
    etag: str | None = None,
) -> dict[str, Any]:
    raw_etag = head.get("ETag") or etag or ""
    content_length = head.get("ContentLength")
    return {
        "path": path,
        "size": int(content_length if content_length is not None else size or 0),
        "modified_at": _iso(head.get("LastModified") or modified_at),
        "content_type": _effective_content_type(path, head.get("ContentType")),
        "etag": str(raw_etag).strip('"'),
    }


def _effective_content_type(path: str, content_type: str | None) -> str:
    value = (content_type or "").strip()
    main_type = value.split(";", 1)[0].strip().lower()
    if value and main_type not in _GENERIC_CONTENT_TYPES:
        return value
    return _guess_content_type(path)


def _is_missing(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    code = str(err.get("Code") or "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _MISSING_CODES or status == 404
