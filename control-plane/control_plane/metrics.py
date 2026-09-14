"""Prometheus HTTP metrics for the control plane API."""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)


REQUEST_COUNT = Counter(
    "a2a_control_plane_http_requests_total",
    "Total HTTP requests handled by the control plane.",
    ("method", "route", "status_code"),
)
REQUEST_DURATION = Histogram(
    "a2a_control_plane_http_request_duration_seconds",
    "HTTP request duration for the control plane.",
    ("method", "route", "status_code"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
MAIN_AGENT_RUNS = Counter(
    "a2a_control_plane_main_agent_runs_total",
    "Total main control-plane orchestrator agent runs.",
    ("surface", "status"),
)
MAIN_AGENT_RUN_DURATION = Histogram(
    "a2a_control_plane_main_agent_run_duration_seconds",
    "Main control-plane orchestrator agent run duration.",
    ("surface", "status"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 900, 1800),
)
MAIN_AGENT_ACTIONS = Counter(
    "a2a_control_plane_main_agent_actions_total",
    "Total main control-plane orchestrator agent actions.",
    ("surface", "kind", "name", "status"),
)
MAIN_AGENT_ACTION_DURATION = Histogram(
    "a2a_control_plane_main_agent_action_duration_seconds",
    "Main control-plane orchestrator agent action duration.",
    ("surface", "kind", "name", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
RATE_LIMIT_DECISIONS = Counter(
    "a2a_control_plane_rate_limit_decisions_total",
    "Rate limit decisions, by policy rule and outcome.",
    ("rule", "outcome"),
)
RATE_LIMIT_CLIENT_SOURCES = Counter(
    "a2a_control_plane_rate_limit_client_source_total",
    "Where the rate limiter's client address came from.",
    ("source",),
)


def observe_main_agent_run(surface: str, status: str, duration_seconds: float) -> None:
    labels = (_metric_label(surface), _metric_label(status))
    duration = max(0.0, float(duration_seconds or 0.0))
    MAIN_AGENT_RUNS.labels(*labels).inc()
    MAIN_AGENT_RUN_DURATION.labels(*labels).observe(duration)


def observe_main_agent_action(
    surface: str,
    kind: str,
    name: str,
    status: str,
    duration_seconds: float,
) -> None:
    labels = (
        _metric_label(surface),
        _metric_label(kind),
        _metric_label(name),
        _metric_label(status),
    )
    duration = max(0.0, float(duration_seconds or 0.0))
    MAIN_AGENT_ACTIONS.labels(*labels).inc()
    MAIN_AGENT_ACTION_DURATION.labels(*labels).observe(duration)


def observe_rate_limit_decision(rule: str, outcome: str) -> None:
    """``outcome`` is ``allowed``, ``throttled`` or ``backend_unavailable``.

    ``backend_unavailable`` is the one to alert on: it means the limiter is
    failing open and the ceilings are not actually being applied.
    """
    RATE_LIMIT_DECISIONS.labels(_metric_label(rule), _metric_label(outcome)).inc()


def observe_rate_limit_client_source(source: str) -> None:
    """``source`` is ``forwarded``, ``forwarded_ambiguous``, ``peer`` or ``first_party``.

    ``forwarded_ambiguous`` is the diagnostic one: it means the forwarded chain
    held more than one address that is not ours, which is either a caller
    prepending a hop (an attempted spoof, expected to be a small minority) or a
    proxy of ours appending a globally-routable address of its own. If it is
    the *steady state* rather than a minority, the IP key is being derived from
    a hop that is really ours and that hop's CIDR belongs in
    ``A2A_CP_RATE_LIMIT_TRUSTED_PROXY_CIDRS`` - until it is, every caller
    behind it shares one bucket.
    """
    RATE_LIMIT_CLIENT_SOURCES.labels(_metric_label(source)).inc()


def install_http_metrics(app: FastAPI) -> None:
    """Install route-level request metrics and expose /metrics."""
    if getattr(app.state, "_a2a_http_metrics_installed", False):
        return
    app.state._a2a_http_metrics_installed = True

    @app.middleware("http")
    async def http_metrics_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = _route_template(request)
            labels = (request.method.upper(), route, str(status_code))
            REQUEST_COUNT.labels(*labels).inc()
            REQUEST_DURATION.labels(*labels).observe(perf_counter() - started)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=generate_latest(_collector_registry()),
            media_type=CONTENT_TYPE_LATEST,
        )


def _collector_registry() -> CollectorRegistry:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    if request.url.path == "/healthz":
        return "/healthz"
    return "unmatched"


def _metric_label(value: str) -> str:
    text = str(value or "unknown").strip() or "unknown"
    out = []
    for ch in text[:80]:
        out.append(ch if ch.isalnum() or ch in {"_", "-", ".", ":", "/"} else "_")
    return "".join(out) or "unknown"
