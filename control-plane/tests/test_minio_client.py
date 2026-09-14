from __future__ import annotations

from datetime import datetime, timezone

from control_plane import minio_client


def test_list_files_uses_list_metadata_without_per_object_head(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakePaginator:
        def paginate(self, *, Bucket: str, Prefix: str):
            calls.append(("paginate", (Bucket, Prefix)))
            yield {
                "Contents": [
                    {
                        "Key": "docs/report.txt",
                        "Size": 12,
                        "LastModified": datetime(2026, 5, 20, tzinfo=timezone.utc),
                        "ETag": '"etag-1"',
                    }
                ]
            }

    class FakeS3:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return FakePaginator()

        def head_bucket(self, **kwargs):  # pragma: no cover - should not matter
            raise AssertionError("head_bucket should be stubbed separately")

    monkeypatch.setattr(minio_client, "ensure_bucket", lambda bucket: None)
    monkeypatch.setattr(minio_client, "_S3", FakeS3())
    monkeypatch.setattr(minio_client, "_head_object", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("head_object should not be called")))

    rows = list(minio_client.list_files("user-1-files"))

    assert calls == [("paginate", ("user-1-files", ""))]
    assert rows == [
        {
            "path": "docs/report.txt",
            "size": 12,
            "modified_at": "2026-05-20T00:00:00+00:00",
            "content_type": "text/plain",
            "etag": "etag-1",
        }
    ]


def test_list_children_returns_direct_children_only(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakePaginator:
        def paginate(self, **params):
            calls.append(("paginate", params))
            yield {
                "CommonPrefixes": [{"Prefix": "docs/"}, {"Prefix": "agents/demo/"}],
                "Contents": [
                    {
                        "Key": "readme.md",
                        "Size": 4,
                        "LastModified": datetime(2026, 5, 21, tzinfo=timezone.utc),
                        "ETag": '"etag-2"',
                    },
                    {
                        "Key": "docs/",
                        "Size": 0,
                    },
                ],
            }

    class FakeS3:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return FakePaginator()

        def head_bucket(self, **kwargs):  # pragma: no cover - should not matter
            raise AssertionError("head_bucket should be stubbed separately")

    monkeypatch.setattr(minio_client, "ensure_bucket", lambda bucket: None)
    monkeypatch.setattr(minio_client, "_S3", FakeS3())

    rows = list(minio_client.list_children("user-1-files"))

    assert calls == [("paginate", {"Bucket": "user-1-files", "Delimiter": "/"})]
    assert rows == [
        {
            "path": "docs",
            "size": 0,
            "modified_at": "",
            "content_type": "",
            "is_dir": True,
        },
        {
            "path": "agents/demo",
            "size": 0,
            "modified_at": "",
            "content_type": "",
            "is_dir": True,
        },
        {
            "path": "readme.md",
            "size": 4,
            "modified_at": "2026-05-21T00:00:00+00:00",
            "content_type": "text/markdown",
            "etag": "etag-2",
        },
    ]
