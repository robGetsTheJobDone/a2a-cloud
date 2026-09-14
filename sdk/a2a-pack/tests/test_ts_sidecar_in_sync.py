"""The embedded TypeScript sidecar must be a byte-for-byte copy of typescript/.

`a2a_pack/typescript` is what the wheel ships and what `a2a init` vendors into
TypeScript/JavaScript projects; `typescript/` is the npm package source. Only
the latter is hand-edited. Run `scripts/sync-ts-sidecar.sh` after changing it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "typescript"
EMBEDDED = ROOT / "a2a_pack" / "typescript"
SYNCED_FILES = ("src/index.ts", "package.json", "tsconfig.json", "README.md")


@pytest.mark.parametrize("relative", SYNCED_FILES)
def test_embedded_sidecar_matches_canonical_source(relative: str) -> None:
    assert (EMBEDDED / relative).read_bytes() == (CANONICAL / relative).read_bytes(), (
        f"a2a_pack/typescript/{relative} drifted; run scripts/sync-ts-sidecar.sh"
    )


def test_embedded_sidecar_dist_is_present_and_current() -> None:
    dist = EMBEDDED / "dist"
    for name in ("index.js", "index.d.ts"):
        assert (dist / name).is_file(), f"missing {dist / name}; run scripts/sync-ts-sidecar.sh"
    # A type added to the source must show up in the built declarations.
    declarations = (dist / "index.d.ts").read_text(encoding="utf-8")
    for marker in ("platform_resources", "account_access", "availability"):
        assert marker in declarations, f"dist is stale (missing {marker}); run scripts/sync-ts-sidecar.sh"


def test_sidecar_version_matches_python_package() -> None:
    from a2a_pack import __version__

    version = json.loads((CANONICAL / "package.json").read_text(encoding="utf-8"))["version"]
    assert version == __version__
