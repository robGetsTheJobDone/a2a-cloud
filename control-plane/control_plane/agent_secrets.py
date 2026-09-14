from __future__ import annotations

import base64
import json
import ssl
import time
from typing import Any
import urllib.error
import urllib.request

from kubernetes import client
from kubernetes.client.rest import ApiException

from .agent_secret_names import agent_runtime_secret_name
from .config import settings
from .k8s import (
    KNATIVE_API_GROUP,
    KNATIVE_API_VERSION,
    KNATIVE_SERVICES_PLURAL,
    SERVICE_ACCOUNT_TOKEN_PATH,
    _load_kube,
)

SERVICE_ACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def upsert_agent_secret_value(
    *,
    agent_name: str,
    key: str,
    value: str,
    owner_id: int | None,
) -> None:
    if settings.in_cluster:
        _raw_upsert_agent_secret_value(
            agent_name=agent_name,
            key=key,
            value=value,
            owner_id=owner_id,
        )
        return

    _load_kube()
    core = client.CoreV1Api()
    name = agent_runtime_secret_name(agent_name)
    ns = settings.agents_namespace
    data = {key: _b64(value)}
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"a2a/managed-by": "control-plane"},
            "annotations": {
                "a2a/agent-name": agent_name,
                **({"a2a/owner-id": str(owner_id)} if owner_id is not None else {}),
            },
        },
        "type": "Opaque",
        "data": data,
    }
    try:
        existing = core.read_namespaced_secret(name, ns)
        merged = dict(existing.data or {})
        merged[key] = data[key]
        body["data"] = merged
        core.replace_namespaced_secret(name, ns, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespaced_secret(ns, body)


def delete_agent_secret_value(*, agent_name: str, key: str) -> None:
    if settings.in_cluster:
        _raw_delete_agent_secret_value(agent_name=agent_name, key=key)
        return

    _load_kube()
    core = client.CoreV1Api()
    name = agent_runtime_secret_name(agent_name)
    ns = settings.agents_namespace
    try:
        existing = core.read_namespaced_secret(name, ns)
    except ApiException as exc:
        if exc.status in (404, 410):
            return
        raise

    data = dict(existing.data or {})
    data.pop(key, None)
    if data:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": {"a2a/managed-by": "control-plane"},
                "annotations": {"a2a/agent-name": agent_name},
            },
            "type": "Opaque",
            "data": data,
        }
        core.replace_namespaced_secret(name, ns, body)
        return

    delete_agent_runtime_secret(agent_name)


def read_agent_secret_value(*, agent_name: str, key: str) -> str | None:
    if settings.in_cluster:
        existing = _raw_read_secret_optional(
            settings.agents_namespace,
            agent_runtime_secret_name(agent_name),
        )
        encoded = ((existing or {}).get("data") or {}).get(key)
        if encoded is None:
            return None
        return base64.b64decode(encoded).decode("utf-8")

    _load_kube()
    core = client.CoreV1Api()
    name = agent_runtime_secret_name(agent_name)
    ns = settings.agents_namespace
    try:
        existing = core.read_namespaced_secret(name, ns)
    except ApiException as exc:
        if exc.status in (404, 410):
            return None
        raise

    encoded = (existing.data or {}).get(key)
    if encoded is None:
        return None
    return base64.b64decode(encoded).decode("utf-8")


def delete_agent_runtime_secret(agent_name: str) -> None:
    _load_kube()
    core = client.CoreV1Api()
    try:
        if settings.in_cluster:
            _raw_delete_secret(settings.agents_namespace, agent_runtime_secret_name(agent_name))
        else:
            core.delete_namespaced_secret(
                agent_runtime_secret_name(agent_name),
                settings.agents_namespace,
            )
    except ApiException as exc:
        if exc.status not in (404, 410):
            raise


def ensure_agent_secret_projection(agent_name: str) -> dict[str, Any]:
    """Project an agent's secret into the running runtime and roll pods.

    Argo-managed agents stamped with the current scaffold already carry this
    envFrom block. This helper covers existing runtimes and direct-register
    agents, while keeping the secret optional so agents can start before users
    add secrets.
    """
    if settings.in_cluster:
        return _raw_ensure_agent_secret_projection(agent_name)

    _load_kube()
    apps = client.AppsV1Api()
    ns = settings.agents_namespace
    try:
        dep = apps.read_namespaced_deployment(agent_name, ns)
    except ApiException as exc:
        if exc.status == 404:
            knative = _ensure_knative_secret_projection(agent_name)
            if knative is not None:
                return knative
            return {"ok": False, "reason": "deployment_not_found"}
        raise

    containers = dep.spec.template.spec.containers or []
    target = next((c for c in containers if c.name == "agent"), containers[0] if containers else None)
    if target is None:
        return {"ok": False, "reason": "agent_container_not_found"}

    secret_name = agent_runtime_secret_name(agent_name)
    env_from = _env_from_to_wire(target.env_from or [])
    if not any(_secret_ref_name(item) == secret_name for item in env_from):
        env_from.append({"secretRef": {"name": secret_name, "optional": True}})

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "a2a/secrets-revision": str(int(time.time())),
                    },
                },
                "spec": {
                    "containers": [
                        {
                            "name": target.name,
                            "envFrom": env_from,
                        }
                    ]
                },
            }
        }
    }
    apps.patch_namespaced_deployment(agent_name, ns, patch)
    return {"ok": True, "secret_name": secret_name}


def _ensure_knative_secret_projection(agent_name: str) -> dict[str, Any] | None:
    custom = client.CustomObjectsApi()
    ns = settings.agents_namespace
    args = (
        KNATIVE_API_GROUP,
        KNATIVE_API_VERSION,
        ns,
        KNATIVE_SERVICES_PLURAL,
    )
    try:
        service = custom.get_namespaced_custom_object(*args, agent_name)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise

    spec = service.get("spec") if isinstance(service, dict) else {}
    template = spec.get("template") if isinstance(spec, dict) else {}
    template_spec = template.get("spec") if isinstance(template, dict) else {}
    containers = template_spec.get("containers") if isinstance(template_spec, dict) else []
    if not isinstance(containers, list) or not containers:
        return {"ok": False, "reason": "agent_container_not_found"}
    target = next(
        (
            c
            for c in containers
            if isinstance(c, dict) and c.get("name") == "agent"
        ),
        containers[0] if isinstance(containers[0], dict) else None,
    )
    if not isinstance(target, dict):
        return {"ok": False, "reason": "agent_container_not_found"}

    secret_name = agent_runtime_secret_name(agent_name)
    env_from = [item for item in target.get("envFrom") or [] if isinstance(item, dict)]
    if not any(_secret_ref_name(item) == secret_name for item in env_from):
        env_from.append({"secretRef": {"name": secret_name, "optional": True}})

    updated_container = {**target, "envFrom": env_from}
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "a2a/secrets-revision": str(int(time.time())),
                    },
                },
                "spec": {
                    "containers": [updated_container]
                },
            }
        }
    }
    custom.patch_namespaced_custom_object(*args, agent_name, patch)
    return {"ok": True, "secret_name": secret_name, "runtime": "knative_service"}


def _raw_upsert_agent_secret_value(
    *,
    agent_name: str,
    key: str,
    value: str,
    owner_id: int | None,
) -> None:
    name = agent_runtime_secret_name(agent_name)
    ns = settings.agents_namespace
    data = {key: _b64(value)}
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"a2a/managed-by": "control-plane"},
            "annotations": {
                "a2a/agent-name": agent_name,
                **({"a2a/owner-id": str(owner_id)} if owner_id is not None else {}),
            },
        },
        "type": "Opaque",
        "data": data,
    }
    existing = _raw_read_secret_optional(ns, name)
    if existing is None:
        _raw_create_secret(ns, body)
        return

    merged = dict((existing.get("data") if isinstance(existing, dict) else {}) or {})
    merged[key] = data[key]
    body["data"] = merged
    _raw_patch_secret(ns, name, body)


def _raw_delete_agent_secret_value(*, agent_name: str, key: str) -> None:
    name = agent_runtime_secret_name(agent_name)
    ns = settings.agents_namespace
    existing = _raw_read_secret_optional(ns, name)
    if existing is None:
        return

    data = dict((existing.get("data") if isinstance(existing, dict) else {}) or {})
    data.pop(key, None)
    if data:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": {"a2a/managed-by": "control-plane"},
                "annotations": {"a2a/agent-name": agent_name},
            },
            "type": "Opaque",
            "data": data,
        }
        _raw_patch_secret(ns, name, body)
        return

    _raw_delete_secret(ns, name)


def _raw_ensure_agent_secret_projection(agent_name: str) -> dict[str, Any]:
    ns = settings.agents_namespace
    deployment = _raw_read_optional(
        f"/apis/apps/v1/namespaces/{ns}/deployments/{agent_name}"
    )
    if deployment is not None:
        containers = (
            (
                ((deployment.get("spec") or {}).get("template") or {})
                .get("spec") or {}
            ).get("containers")
            if isinstance(deployment, dict)
            else None
        )
        target = _target_container_dict(containers)
        if target is None:
            return {"ok": False, "reason": "agent_container_not_found"}
        secret_name = agent_runtime_secret_name(agent_name)
        env_from = [
            item for item in target.get("envFrom") or [] if isinstance(item, dict)
        ]
        if not any(_secret_ref_name(item) == secret_name for item in env_from):
            env_from.append({"secretRef": {"name": secret_name, "optional": True}})
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "a2a/secrets-revision": str(int(time.time())),
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": str(target.get("name") or "agent"),
                                "envFrom": env_from,
                            }
                        ]
                    },
                }
            }
        }
        _raw_kube_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{ns}/deployments/{agent_name}",
            patch,
            content_type="application/strategic-merge-patch+json",
        )
        return {"ok": True, "secret_name": secret_name}

    service = _raw_read_optional(
        f"/apis/{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}/namespaces/{ns}"
        f"/{KNATIVE_SERVICES_PLURAL}/{agent_name}"
    )
    if service is None:
        return {"ok": False, "reason": "deployment_not_found"}

    containers = (
        (((service.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "containers"
        )
        if isinstance(service, dict)
        else None
    )
    target = _target_container_dict(containers)
    if target is None:
        return {"ok": False, "reason": "agent_container_not_found"}

    secret_name = agent_runtime_secret_name(agent_name)
    env_from = [
        item for item in target.get("envFrom") or [] if isinstance(item, dict)
    ]
    if not any(_secret_ref_name(item) == secret_name for item in env_from):
        env_from.append({"secretRef": {"name": secret_name, "optional": True}})
    updated_container = {**target, "envFrom": env_from}
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "a2a/secrets-revision": str(int(time.time())),
                    },
                },
                "spec": {
                    "containers": [updated_container]
                },
            }
        }
    }
    _raw_kube_request(
        "PATCH",
        f"/apis/{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}/namespaces/{ns}"
        f"/{KNATIVE_SERVICES_PLURAL}/{agent_name}",
        patch,
        content_type="application/merge-patch+json",
    )
    return {"ok": True, "secret_name": secret_name, "runtime": "knative_service"}


def _target_container_dict(containers: Any) -> dict[str, Any] | None:
    if not isinstance(containers, list) or not containers:
        return None
    target = next(
        (
            c
            for c in containers
            if isinstance(c, dict) and c.get("name") == "agent"
        ),
        containers[0] if isinstance(containers[0], dict) else None,
    )
    return target if isinstance(target, dict) else None


def _env_from_to_wire(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        secret_ref = getattr(item, "secret_ref", None)
        config_map_ref = getattr(item, "config_map_ref", None)
        if secret_ref is not None and getattr(secret_ref, "name", None):
            out.append(
                {
                    "secretRef": {
                        "name": secret_ref.name,
                        "optional": bool(getattr(secret_ref, "optional", False)),
                    }
                }
            )
        elif config_map_ref is not None and getattr(config_map_ref, "name", None):
            out.append(
                {
                    "configMapRef": {
                        "name": config_map_ref.name,
                        "optional": bool(getattr(config_map_ref, "optional", False)),
                    }
                }
            )
    return out


def _secret_ref_name(item: dict[str, Any]) -> str | None:
    ref = item.get("secretRef")
    if not isinstance(ref, dict):
        return None
    name = ref.get("name")
    return name if isinstance(name, str) else None


def _raw_create_secret(namespace: str, body: dict[str, Any]) -> None:
    _raw_kube_request(
        "POST",
        f"/api/v1/namespaces/{namespace}/secrets",
        body,
        content_type="application/json",
    )


def _raw_patch_secret(namespace: str, name: str, body: dict[str, Any]) -> None:
    _raw_kube_request(
        "PATCH",
        f"/api/v1/namespaces/{namespace}/secrets/{name}",
        {
            "metadata": {
                "labels": body.get("metadata", {}).get("labels", {}),
                "annotations": body.get("metadata", {}).get("annotations", {}),
            },
            "type": body.get("type", "Opaque"),
            "data": body.get("data", {}),
        },
        content_type="application/merge-patch+json",
    )


def _raw_delete_secret(namespace: str, name: str) -> None:
    _raw_kube_request("DELETE", f"/api/v1/namespaces/{namespace}/secrets/{name}", None)


def _raw_read_secret_optional(namespace: str, name: str) -> dict[str, Any] | None:
    return _raw_read_optional(f"/api/v1/namespaces/{namespace}/secrets/{name}")


def _raw_read_optional(path: str) -> dict[str, Any] | None:
    try:
        return _raw_kube_request("GET", path, None)
    except ApiException as exc:
        if exc.status in (404, 410):
            return None
        raise


def _raw_kube_request(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    content_type: str = "application/json",
) -> dict[str, Any] | None:
    with open(SERVICE_ACCOUNT_TOKEN_PATH, encoding="utf-8") as fh:
        token = fh.read().strip()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"https://kubernetes.default.svc{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": content_type} if body is not None else {}),
        },
    )
    context = ssl.create_default_context(cafile=SERVICE_ACCOUNT_CA_PATH)
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        api_exc = ApiException(
            status=getattr(exc, "code", None),
            reason=getattr(exc, "reason", None) or getattr(exc, "msg", None),
        )
        try:
            error_payload = exc.read()
        except Exception:  # noqa: BLE001
            error_payload = b""
        if error_payload:
            api_exc.body = (
                error_payload.decode("utf-8", errors="replace")
                if isinstance(error_payload, bytes)
                else str(error_payload)
            )
        raise api_exc from exc
    if not payload:
        return None
    parsed = json.loads(payload.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else None


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")
