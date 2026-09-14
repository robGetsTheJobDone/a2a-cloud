"""Create ArgoCD Application + Repository secret for an agent's gitea repo."""
from __future__ import annotations

import base64

from kubernetes import client
from kubernetes.client.rest import ApiException

from .gitea import GITEA_PASS, GITEA_USER
from .k8s import _load_kube

ARGO_NAMESPACE = "a2a-infra"
A2A_RUNTIME_LABEL = "a2a.cloud/managed-agent-runtime"


class ArgoApplicationConflict(RuntimeError):
    """Raised when an agent runtime would overwrite a non-agent Argo app."""


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def ensure_repo_secret(secret_name: str, repo_url: str) -> None:
    _load_kube()
    core = client.CoreV1Api()
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": ARGO_NAMESPACE,
            "labels": {"argocd.argoproj.io/secret-type": "repository"},
        },
        "type": "Opaque",
        "data": {
            "type": _b64("git"),
            "url": _b64(repo_url),
            "username": _b64(GITEA_USER),
            "password": _b64(GITEA_PASS),
        },
    }
    try:
        core.patch_namespaced_secret(secret_name, ARGO_NAMESPACE, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        _load_kube()
        core = client.CoreV1Api()
        core.create_namespaced_secret(ARGO_NAMESPACE, body)


def delete_repo_secret(secret_name: str) -> None:
    _load_kube()
    core = client.CoreV1Api()
    try:
        core.delete_namespaced_secret(secret_name, ARGO_NAMESPACE)
    except ApiException as exc:
        if exc.status not in (404, 410):
            raise


def _application_body(agent_name: str, repo_url: str) -> dict:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": agent_name,
            "namespace": ARGO_NAMESPACE,
            "labels": {
                A2A_RUNTIME_LABEL: "true",
                "app.kubernetes.io/managed-by": "a2a-control-plane",
                "app.kubernetes.io/name": agent_name,
            },
            "annotations": {
                "a2a.cloud/runtime-repo-url": repo_url,
            },
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": repo_url,
                "targetRevision": "main",
                "path": "deploy",
            },
            "destination": {"server": "https://kubernetes.default.svc"},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
            },
            "ignoreDifferences": [
                {
                    "group": "serving.knative.dev",
                    "kind": "Service",
                    "jsonPointers": [
                        "/status",
                        "/spec/traffic",
                        "/metadata/annotations/serving.knative.dev~1creator",
                        "/metadata/annotations/serving.knative.dev~1lastModifier",
                    ],
                }
            ],
        },
    }


def _source_repo_url(application: dict) -> str | None:
    spec = application.get("spec") if isinstance(application, dict) else None
    source = spec.get("source") if isinstance(spec, dict) else None
    repo_url = source.get("repoURL") if isinstance(source, dict) else None
    return repo_url if isinstance(repo_url, str) and repo_url else None


def _assert_application_can_be_managed(
    application: dict,
    *,
    agent_name: str,
    repo_url: str,
) -> None:
    existing_repo_url = _source_repo_url(application)
    if existing_repo_url == repo_url:
        return
    raise ArgoApplicationConflict(
        f"refusing to overwrite ArgoCD Application {agent_name!r}: "
        f"existing source repo is {existing_repo_url!r}, expected generated "
        f"runtime repo {repo_url!r}"
    )


def ensure_application(agent_name: str, repo_url: str) -> None:
    _load_kube()
    custom = client.CustomObjectsApi()
    body = _application_body(agent_name, repo_url)
    args = ("argoproj.io", "v1alpha1", ARGO_NAMESPACE, "applications")
    try:
        existing = custom.get_namespaced_custom_object(*args, agent_name)
    except ApiException as exc:
        if exc.status != 404:
            raise
        _load_kube()
        custom = client.CustomObjectsApi()
        custom.create_namespaced_custom_object(*args, body)
        return
    _assert_application_can_be_managed(
        existing if isinstance(existing, dict) else {},
        agent_name=agent_name,
        repo_url=repo_url,
    )
    custom.patch_namespaced_custom_object(*args, agent_name, body)


def delete_application(agent_name: str) -> None:
    _load_kube()
    custom = client.CustomObjectsApi()
    args = ("argoproj.io", "v1alpha1", ARGO_NAMESPACE, "applications")
    try:
        custom.delete_namespaced_custom_object(*args, agent_name)
    except ApiException as exc:
        if exc.status not in (404, 410):
            raise


def request_application_refresh(agent_name: str, *, hard: bool = True) -> dict[str, str | bool | None]:
    """Ask ArgoCD to re-read the managed runtime repo for an agent."""
    _load_kube()
    custom = client.CustomObjectsApi()
    args = ("argoproj.io", "v1alpha1", ARGO_NAMESPACE, "applications")
    refresh_value = "hard" if hard else "normal"
    body = {
        "metadata": {
            "annotations": {
                "argocd.argoproj.io/refresh": refresh_value,
            },
        },
    }
    custom.patch_namespaced_custom_object(*args, agent_name, body)
    return {"requested": True, "mode": refresh_value}


def application_summary(agent_name: str) -> dict[str, str | bool | None]:
    """Small, UI-safe ArgoCD status summary for a managed agent app."""
    _load_kube()
    custom = client.CustomObjectsApi()
    args = ("argoproj.io", "v1alpha1", ARGO_NAMESPACE, "applications")
    try:
        app = custom.get_namespaced_custom_object(*args, agent_name)
    except ApiException as exc:
        if exc.status == 404:
            return {"exists": False, "sync": None, "health": None, "revision": None}
        raise
    status = app.get("status") if isinstance(app, dict) else {}
    sync = status.get("sync") if isinstance(status, dict) else {}
    health = status.get("health") if isinstance(status, dict) else {}
    return {
        "exists": True,
        "sync": sync.get("status") if isinstance(sync, dict) else None,
        "health": health.get("status") if isinstance(health, dict) else None,
        "revision": sync.get("revision") if isinstance(sync, dict) else None,
    }
