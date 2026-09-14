"""Generate Kubernetes manifests for a deployed agent.

Targets a plain Kubernetes cluster: namespace ``agents``, ingress host
``<name>.<platform domain>`` (see ``a2a_pack.cli.platform``).
"""
from __future__ import annotations

from typing import Any

import yaml

from ..agent import A2AAgent
from . import platform

NAMESPACE = "agents"


def ingress_host(name: str) -> str:
    return f"{name}.{platform.platform_domain()}"


def render_manifests(
    agent_cls: type[A2AAgent],
    *,
    image: str,
    public: bool = True,
) -> str:
    """Return a multi-doc YAML string ready for ``kubectl apply -f -``."""
    rt = agent_cls.runtime()
    name = agent_cls.name
    docs: list[dict[str, Any]] = []

    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": NAMESPACE},
        }
    )

    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app": name,
                    "a2a/version": agent_cls.version,
                    "a2a/lifecycle": rt.lifecycle.value,
                },
            },
            "spec": {
                "replicas": 1,
                "revisionHistoryLimit": 1,
                "selector": {"matchLabels": {"app": name}},
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                },
                "progressDeadlineSeconds": 900,
                "template": {
                    "metadata": {
                        "labels": {"app": name},
                    },
                    "spec": {
                        "enableServiceLinks": False,
                        "terminationGracePeriodSeconds": 30,
                        "containers": [
                            {
                                "name": "agent",
                                "image": image,
                                "imagePullPolicy": "Always",
                                "env": [
                                    {"name": "A2A_AGENT_IMAGE", "value": image},
                                    {"name": "A2A_RENDER_IMAGE", "value": image},
                                ],
                                "ports": [
                                    {
                                        "containerPort": 8000,
                                        "name": "http1",
                                        "protocol": "TCP",
                                    }
                                ],
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8000},
                                    "initialDelaySeconds": 2,
                                    "periodSeconds": 5,
                                    "failureThreshold": 3,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 1,
                                },
                                "livenessProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8000},
                                    "initialDelaySeconds": 10,
                                    "periodSeconds": 15,
                                },
                                "resources": {
                                    "requests": {
                                        "cpu": rt.resources.cpu,
                                        "memory": rt.resources.memory,
                                    },
                                    "limits": {
                                        "cpu": rt.resources.cpu,
                                        "memory": rt.resources.memory,
                                    },
                                },
                            }
                        ]
                    },
                },
            },
        }
    )

    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "annotations": {
                    "argocd.argoproj.io/sync-options": "ServerSideApply=false,Replace=true",
                },
                "labels": {
                    "app": name,
                    "a2a/version": agent_cls.version,
                    "a2a/lifecycle": rt.lifecycle.value,
                },
            },
            "spec": {
                "selector": {"app": name},
                "ports": [
                    {
                        "name": "http",
                        "port": 80,
                        "targetPort": 8000,
                        "protocol": "TCP",
                    }
                ],
            },
        }
    )

    if public:
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {
                    "rules": [
                        {
                            "host": ingress_host(name),
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": name,
                                                "port": {"number": 80},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        )

    return "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
