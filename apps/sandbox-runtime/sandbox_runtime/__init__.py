"""Host-side microsandbox runtime: FUSE-mounted MinIO ↔ microsandbox VM."""
from .microsandbox_fuse import (  # noqa: F401
    S3FuseAdapter,
    S3SandboxBackend,
    aclose_session,
    microsandbox_session,
    microsandbox_session_sync,
    mount_backend,
)
from .resource_caps import CapsTracker, ResourceCaps  # noqa: F401

# MinIO backend + a2a SandboxClient bridge are optional imports because they
# pull boto3 / a2a-pack respectively. Keep the core import light.
try:
    from .minio_backend import FileResponse, MinIOBackend  # noqa: F401
except ImportError:  # boto3 not installed
    pass
try:
    from .sandbox_client import LocalMicrosandboxClient  # noqa: F401
except ImportError:  # a2a-pack not installed in this env
    pass

__all__ = [
    "CapsTracker",
    "FileResponse",
    "LocalMicrosandboxClient",
    "MinIOBackend",
    "ResourceCaps",
    "S3FuseAdapter",
    "S3SandboxBackend",
    "aclose_session",
    "microsandbox_session",
    "microsandbox_session_sync",
    "mount_backend",
]
