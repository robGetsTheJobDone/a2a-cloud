"""LangChain tools that the orchestrator hands to deepagents.

Each builder takes an :class:`OrchestratorContext` (user_id, bucket, settings,
auth) and returns a list of ``@tool``-decorated callables closed over that
context. The orchestrator composes them into a single tool list at build
time.
"""
from __future__ import annotations

from .dag import build_dag_tools
from .discovery import build_discovery_tools
from .files import build_file_tools
from .handoff import build_handoff_tools
from .sandbox import build_sandbox_tools

__all__ = [
    "build_dag_tools",
    "build_discovery_tools",
    "build_file_tools",
    "build_handoff_tools",
    "build_sandbox_tools",
]
