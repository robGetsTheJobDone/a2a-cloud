"""MinIO/S3 backend for :class:`S3FuseAdapter`.

Implements the duck-typed protocol that ``microsandbox_fuse`` consumes:

  * ``ls_info(path) -> Iterable[FileInfo]`` (dicts with ``path``, ``is_dir``,
    ``size``, ``modified_at``)
  * ``download_files(paths) -> list[FileResponse]`` (objects with
    ``content``, ``path``, ``error``)
  * ``overwrite(path, data: bytes) -> None``
  * ``overwrite_stream(path, fileobj) -> None``  (multipart-friendly)
  * ``read_range(path, offset, size) -> bytes``
  * ``delete(path) -> None``
  * ``copy(src, dst) -> None``

Each backend instance is scoped to a single bucket (the agent's workspace
bucket). Paths handed to/from the FUSE adapter are POSIX-rooted (``/foo/bar``);
S3 keys never have a leading slash, so we strip on the way in and re-add on
the way out.
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

try:
    import boto3
    from botocore.config import Config as _BotoConfig
    from botocore.exceptions import ClientError
except ImportError as exc:  # pragma: no cover - optional dep
    raise ImportError(
        "boto3 is required for MinIOBackend; "
        "install via `pip install 'sandbox-runtime[minio]'`"
    ) from exc


@dataclass
class FileResponse:
    """Object returned by :meth:`MinIOBackend.download_files`."""

    path: str
    content: bytes | None = None
    error: str | None = None


def _to_key(path: str) -> str:
    """Strip leading '/' so a POSIX-rooted path becomes a valid S3 key."""
    return path.lstrip("/")


def _to_posix(key: str) -> str:
    return key if key.startswith("/") else f"/{key}"


def _parse_iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_write_prefixes(
    outputs_prefix: str | None,
    write_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    prefixes: list[str] = []
    values: list[str] = []
    if outputs_prefix:
        values.append(outputs_prefix)
    values.extend(write_prefixes)
    for value in values:
        clean = str(value).replace("\\", "/").strip("/")
        if not clean:
            continue
        if clean not in prefixes:
            prefixes.append(clean)
    return tuple(prefixes)


class MinIOBackend:
    """S3-API backend pointed at MinIO (or any S3-compatible store).

    For the cluster setup in this repo, the defaults target MinIO at
    ``a2a-infra-minio.a2a-infra.svc.cluster.local:9000``. For
    host-side dev (running the FUSE mount on a Mac) you typically port-forward
    that service and pass ``endpoint_url='http://localhost:9000'``.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        allow_patterns: tuple[str, ...] = ("**",),
        deny_patterns: tuple[str, ...] = (),
        mode: str = "read_write_overlay",
        outputs_prefix: str | None = None,
        write_prefixes: tuple[str, ...] = (),
        region: str = "us-east-1",
        addressing_style: str = "path",  # MinIO requires path-style
    ) -> None:
        self.bucket = bucket
        self._allow_patterns = tuple(allow_patterns or ("**",))
        self._deny_patterns = tuple(deny_patterns or ())
        self._mode = mode
        self._outputs_prefix = outputs_prefix.strip("/") if outputs_prefix else None
        self._write_prefixes = _normalize_write_prefixes(
            outputs_prefix,
            write_prefixes,
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url
            or os.environ.get(
                "A2A_MINIO_ENDPOINT",
                "http://a2a-infra-minio.a2a-infra.svc.cluster.local:9000",
            ),
            aws_access_key_id=access_key
            or os.environ.get("A2A_MINIO_ACCESS_KEY", ""),
            aws_secret_access_key=secret_key
            or os.environ.get("A2A_MINIO_SECRET_KEY", ""),
            region_name=region,
            config=_BotoConfig(s3={"addressing_style": addressing_style}),
        )
        # Idempotent: create the bucket on first use.
        try:
            self._client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                self._client.create_bucket(Bucket=bucket)
            elif code == "403":
                # bucket exists but we can't HEAD; assume usable
                pass
            else:
                raise

    def _allowed(self, key: str) -> bool:
        clean = key.strip("/")
        return any(fnmatch(clean, pat) for pat in self._allow_patterns) and not any(
            fnmatch(clean, pat) for pat in self._deny_patterns
        )

    def _may_contain_allowed(self, key_prefix: str) -> bool:
        prefix = key_prefix.strip("/")
        if not prefix:
            return True
        prefix = prefix.rstrip("/") + "/"
        for pattern in self._allow_patterns:
            if pattern == "**":
                return True
            static = pattern.split("*", 1)[0].strip("/")
            if not static:
                return True
            static_prefix = static.rstrip("/") + "/"
            if static_prefix.startswith(prefix) or prefix.startswith(static_prefix):
                return True
        return False

    def _writable(self, key: str) -> bool:
        if self._mode == "read_only":
            return False
        clean = key.strip("/")
        if self._write_prefixes:
            return any(
                clean == prefix or clean.startswith(prefix + "/")
                for prefix in self._write_prefixes
            )
        if self._outputs_prefix:
            return clean == self._outputs_prefix or clean.startswith(self._outputs_prefix + "/")
        return self._allowed(clean)

    def _require_read(self, key: str) -> None:
        if not self._allowed(key):
            raise PermissionError(f"workspace path denied by grant: {key}")

    def _require_write(self, key: str) -> None:
        if not self._writable(key):
            raise PermissionError(f"workspace path is not writable by grant: {key}")

    # ------------------------------------------------------------------
    # listing
    # ------------------------------------------------------------------

    def ls_info(self, path: str) -> Iterable[dict[str, Any]]:
        """Yield FileInfo dicts for direct children of ``path``.

        S3 has no real directories — we synthesize them from common prefixes
        the way ``aws s3 ls`` does, using the ``Delimiter='/'`` trick.
        """
        prefix = _to_key(path)
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"

        paginator = self._client.get_paginator("list_objects_v2")
        seen: set[str] = set()
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes") or ():
                key = cp.get("Prefix") or ""
                child = key.rstrip("/")
                if not child or child in seen:
                    continue
                if not self._may_contain_allowed(child):
                    continue
                seen.add(child)
                yield {
                    "path": _to_posix(child),
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                }
            for obj in page.get("Contents") or ():
                key = obj.get("Key") or ""
                if not key or key == prefix:
                    continue  # skip self / placeholder marker
                if key in seen:
                    continue
                if not self._allowed(key):
                    continue
                seen.add(key)
                yield {
                    "path": _to_posix(key),
                    "is_dir": False,
                    "size": int(obj.get("Size") or 0),
                    "modified_at": _parse_iso(obj.get("LastModified")),
                }

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def download_files(self, paths: list[str]) -> list[FileResponse]:
        results: list[FileResponse] = []
        for p in paths:
            key = _to_key(p)
            if not self._allowed(key):
                results.append(FileResponse(path=_to_posix(key), error="permission_denied"))
                continue
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=key)
                results.append(
                    FileResponse(
                        path=_to_posix(key), content=resp["Body"].read()
                    )
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                err = (
                    "permission_denied"
                    if code in {"AccessDenied", "403"}
                    else "not_found"
                    if code in {"NoSuchKey", "404"}
                    else code or "error"
                )
                results.append(FileResponse(path=_to_posix(key), error=err))
        return results

    def read_range(self, path: str, offset: int, size: int) -> bytes:
        key = _to_key(path)
        self._require_read(key)
        if size <= 0:
            return b""
        rng = f"bytes={offset}-{offset + size - 1}"
        try:
            resp = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=rng
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"InvalidRange", "416"}:
                return b""
            raise
        return resp["Body"].read()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def overwrite(self, path: str, data: bytes) -> None:
        self._require_write(_to_key(path))
        self._client.put_object(
            Bucket=self.bucket, Key=_to_key(path), Body=data
        )

    def overwrite_stream(self, path: str, fileobj: Any) -> None:
        self._require_write(_to_key(path))
        # Reset spooled tempfiles to start; boto3.upload_fileobj handles the
        # multipart split automatically.
        try:
            fileobj.seek(0)
        except (AttributeError, OSError):
            pass
        self._client.upload_fileobj(fileobj, self.bucket, _to_key(path))

    def delete(self, path: str) -> None:
        self._require_write(_to_key(path))
        self._client.delete_object(Bucket=self.bucket, Key=_to_key(path))

    def copy(self, src: str, dst: str) -> None:
        self._require_read(_to_key(src))
        self._require_write(_to_key(dst))
        self._client.copy_object(
            Bucket=self.bucket,
            Key=_to_key(dst),
            CopySource={"Bucket": self.bucket, "Key": _to_key(src)},
        )

    # ------------------------------------------------------------------
    # misc helpers (handy in tests)
    # ------------------------------------------------------------------

    def write_text(self, path: str, text: str) -> None:
        self.overwrite(path, text.encode("utf-8"))

    def read_text(self, path: str) -> str:
        responses = self.download_files([path])
        if not responses or responses[0].content is None:
            raise FileNotFoundError(path)
        return responses[0].content.decode("utf-8")

    def walk(self, root: str = "/") -> Iterator[dict[str, Any]]:
        """Recursive listing — useful for tests + the bridge-mode hydrator."""
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in self.ls_info(current):
                if entry.get("is_dir"):
                    stack.append(entry["path"])
                yield entry
