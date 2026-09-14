"""Main orchestrator agent — LangGraph deepagents wired to the a2a platform.

Public surface:

    from main_agent import build_orchestrator, OrchestratorContext, run_chat

``build_orchestrator(ctx)`` returns a compiled LangGraph that, when invoked,
drives the user's MinIO bucket via a small set of tools and can hand off
to deployed A2A agents using signed grants.

The control plane can either:
  (a) import ``build_orchestrator`` directly and stream the resulting graph, or
  (b) call this package's HTTP surface (the ``main-agent`` A2A deployment),
      delegating chat to its ``chat`` skill.
"""
from __future__ import annotations

from .hooks import PlatformHooks
from .orchestrator import (
    MAIN_AGENT_RECURSION_LIMIT,
    OrchestratorContext,
    build_orchestrator,
    main_agent_graph_config,
    run_chat,
)

__all__ = [
    "MAIN_AGENT_RECURSION_LIMIT",
    "OrchestratorContext",
    "PlatformHooks",
    "build_orchestrator",
    "main_agent_graph_config",
    "run_chat",
]
