from __future__ import annotations

from sandbox_runtime.minio_backend import MinIOBackend


def _backend(
    *,
    mode: str = "read_write_overlay",
    allow_patterns: tuple[str, ...] = ("**",),
    deny_patterns: tuple[str, ...] = (),
    outputs_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
) -> MinIOBackend:
    backend = MinIOBackend.__new__(MinIOBackend)
    backend._mode = mode
    backend._allow_patterns = allow_patterns
    backend._deny_patterns = deny_patterns
    backend._outputs_prefix = outputs_prefix.strip("/") if outputs_prefix else None
    backend._write_prefixes = tuple(prefix.strip("/") for prefix in write_prefixes)
    return backend


def test_write_prefixes_are_enforced_over_broad_allow_patterns() -> None:
    backend = _backend(
        allow_patterns=("**",),
        outputs_prefix="outputs/",
        write_prefixes=("outputs/", "reports/"),
    )

    assert backend._writable("outputs/run.log")
    assert backend._writable("reports/final.md")
    assert not backend._writable("data/input.csv")


def test_legacy_outputs_prefix_still_restricts_writes() -> None:
    backend = _backend(allow_patterns=("**",), outputs_prefix="outputs/")

    assert backend._writable("outputs/run.log")
    assert not backend._writable("reports/final.md")


def test_no_write_prefix_falls_back_to_allow_and_deny_patterns() -> None:
    backend = _backend(
        allow_patterns=("data/**",),
        deny_patterns=("data/private/**",),
    )

    assert backend._writable("data/public.csv")
    assert not backend._writable("data/private/secret.csv")
    assert not backend._writable("reports/final.md")


def test_read_only_mode_blocks_write_prefixes() -> None:
    backend = _backend(mode="read_only", write_prefixes=("outputs/",))

    assert not backend._writable("outputs/report.md")
