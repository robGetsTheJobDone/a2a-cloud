"""Kubernetes resource-quantity parsing helpers.

``parse_cpu`` and ``parse_memory`` turn the ``Resources`` strings an agent
declares (``"500m"``, ``"1Gi"``) into plain numbers the scaffolder and
scheduler can compare.
"""
from __future__ import annotations

import re

_CPU_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m)?\s*$", re.IGNORECASE)
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]i?)?\s*$", re.IGNORECASE)
_MEM_FACTORS = {
    "": 1 / (1024 * 1024),       # bytes → MiB
    "K": 1000 / (1024 * 1024),   # KB → MiB
    "M": 1_000_000 / (1024 * 1024),
    "G": 1_000_000_000 / (1024 * 1024),
    "Ki": 1024 / (1024 * 1024),
    "Mi": 1.0,
    "Gi": 1024.0,
    "Ti": 1024.0 * 1024.0,
}


def parse_cpu(spec: str) -> float:
    """Return millicores. ``"500m" → 500``, ``"2" → 2000``, ``"" → 0``."""
    if not spec:
        return 0.0
    m = _CPU_RE.match(spec)
    if not m:
        return 0.0
    value = float(m.group(1))
    return value if m.group(2) else value * 1000


def parse_memory(spec: str) -> float:
    """Return MiB. ``"256Mi" → 256``, ``"1Gi" → 1024``, ``"" → 0``."""
    if not spec:
        return 0.0
    m = _MEM_RE.match(spec)
    if not m:
        return 0.0
    value = float(m.group(1))
    suffix = (m.group(2) or "").capitalize()
    if suffix == "K":
        suffix = "Ki"  # k8s convention: "1k" rarely seen, treat as Ki
    return value * _MEM_FACTORS.get(suffix, _MEM_FACTORS[""])
