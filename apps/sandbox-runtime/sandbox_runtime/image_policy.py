"""Operator-owned OCI image policy for sandbox VM creation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable

DEFAULT_SANDBOX_IMAGE = "python:3.11-slim"
DEFAULT_IMAGE_ENV = "A2A_SANDBOX_DEFAULT_IMAGE"
ALLOWED_IMAGES_ENV = "A2A_SANDBOX_ALLOWED_IMAGES"
IMAGE_ALIASES_ENV = "A2A_SANDBOX_IMAGE_ALIASES"
REQUIRE_DIGEST_ENV = "A2A_SANDBOX_REQUIRE_IMAGE_DIGEST"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SandboxImageRejected(ValueError):
    """Raised when a caller requests an image outside the operator allowlist."""


def _validate_configured_image(image: str) -> str:
    if not image or image != image.strip():
        raise RuntimeError("sandbox image references must be non-empty and trimmed")
    if any(char.isspace() or ord(char) < 32 for char in image):
        raise RuntimeError("sandbox image references must not contain whitespace")
    if "://" in image or "\\" in image or "?" in image or "#" in image:
        raise RuntimeError(f"invalid sandbox image reference: {image!r}")
    if image.startswith(("/", ".", "-")) or "//" in image:
        raise RuntimeError(f"invalid sandbox image reference: {image!r}")
    if any(part in {"", ".", ".."} for part in image.split("/")):
        raise RuntimeError(f"invalid sandbox image reference: {image!r}")
    if "@" in image:
        name, separator, digest = image.rpartition("@")
        if not separator or not name or not _DIGEST_RE.fullmatch(digest):
            raise RuntimeError("sandbox image digests must be full sha256 digests")
    return image


def _configured_images(raw: str) -> tuple[str, ...]:
    images = tuple(
        part.strip()
        for line in raw.splitlines()
        for part in line.split(",")
        if part.strip()
    )
    if not images:
        raise RuntimeError(f"{ALLOWED_IMAGES_ENV} must contain at least one image")
    return images


def _require_digest() -> bool:
    raw = os.environ.get(REQUIRE_DIGEST_ENV, "").strip().lower()
    if raw in {"", "0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    raise RuntimeError(f"{REQUIRE_DIGEST_ENV} must be true or false")


@dataclass(frozen=True, slots=True)
class SandboxImagePolicy:
    default_image: str
    allowed_images: frozenset[str]
    image_aliases: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_env(
        cls,
        *,
        default_image: str | None = None,
        allowed_images: Iterable[str] | None = None,
        image_aliases: dict[str, str] | None = None,
    ) -> "SandboxImagePolicy":
        default = default_image or os.environ.get(DEFAULT_IMAGE_ENV, DEFAULT_SANDBOX_IMAGE)
        if allowed_images is None:
            configured = os.environ.get(ALLOWED_IMAGES_ENV)
            allowed = (default,) if configured is None else _configured_images(configured)
        else:
            allowed = tuple(allowed_images)
            if not allowed:
                raise RuntimeError("sandbox image allowlist must not be empty")

        if image_aliases is None:
            raw_aliases = os.environ.get(IMAGE_ALIASES_ENV, "{}").strip() or "{}"
            try:
                parsed_aliases = json.loads(raw_aliases)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{IMAGE_ALIASES_ENV} must be a JSON object") from exc
            if not isinstance(parsed_aliases, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in parsed_aliases.items()
            ):
                raise RuntimeError(f"{IMAGE_ALIASES_ENV} must map image strings to images")
        else:
            parsed_aliases = image_aliases

        validated_aliases = {
            _validate_configured_image(alias): _validate_configured_image(target)
            for alias, target in parsed_aliases.items()
        }
        validated_default = _validate_configured_image(default)
        validated_allowed = frozenset(_validate_configured_image(item) for item in allowed)
        resolved_default = validated_aliases.get(validated_default, validated_default)
        if resolved_default not in validated_allowed:
            raise RuntimeError("default sandbox image is not in the operator allowlist")
        if any(target not in validated_allowed for target in validated_aliases.values()):
            raise RuntimeError("sandbox image aliases must resolve to allowlisted images")
        if _require_digest() and any("@sha256:" not in image for image in validated_allowed):
            raise RuntimeError("production sandbox images must use full sha256 digests")
        return cls(
            default_image=resolved_default,
            allowed_images=validated_allowed,
            image_aliases=tuple(sorted(validated_aliases.items())),
        )

    def resolve(self, requested: str | None) -> str:
        image = self.default_image if requested is None else requested
        if not isinstance(image, str):
            raise SandboxImageRejected("sandbox image is not in the operator allowlist")
        resolved = dict(self.image_aliases).get(image, image)
        if resolved not in self.allowed_images:
            raise SandboxImageRejected("sandbox image is not in the operator allowlist")
        return resolved
