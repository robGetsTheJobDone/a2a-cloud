from __future__ import annotations

from typing import Any

from kubernetes.client.rest import ApiException

from control_plane import argo


def test_ensure_repo_secret_patches_without_read(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    class _CoreV1Api:
        def read_namespaced_secret(self, *args, **kwargs) -> None:
            raise AssertionError("read_namespaced_secret should not be called")

        def patch_namespaced_secret(self, name: str, namespace: str, body: dict[str, Any]) -> None:
            calls.append(("patch", name, namespace, body))

        def create_namespaced_secret(self, namespace: str, body: dict[str, Any]) -> None:
            calls.append(("create", namespace, body))

    monkeypatch.setattr(argo, "_load_kube", lambda: None)
    monkeypatch.setattr(argo.client, "CoreV1Api", _CoreV1Api)
    monkeypatch.setattr(argo, "GITEA_USER", "user")
    monkeypatch.setattr(argo, "GITEA_PASS", "pass")

    argo.ensure_repo_secret("repo-secret", "https://gitea.example/repo")

    assert calls and calls[0][0] == "patch"
    assert calls[0][1] == "repo-secret"
    assert calls[0][2] == argo.ARGO_NAMESPACE
    assert calls[0][3]["metadata"]["name"] == "repo-secret"
    assert len(calls) == 1


def test_ensure_repo_secret_reloads_before_create_after_missing_patch(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    class _CoreV1Api:
        def __init__(self) -> None:
            calls.append(("init", len([c for c in calls if c[0] == "init"])))

        def patch_namespaced_secret(self, name: str, namespace: str, body: dict[str, Any]) -> None:
            calls.append(("patch", name, namespace, body))
            raise ApiException(status=404)

        def create_namespaced_secret(self, namespace: str, body: dict[str, Any]) -> None:
            calls.append(("create", namespace, body))

    monkeypatch.setattr(argo, "_load_kube", lambda: calls.append(("load",)))
    monkeypatch.setattr(argo.client, "CoreV1Api", _CoreV1Api)
    monkeypatch.setattr(argo, "GITEA_USER", "user")
    monkeypatch.setattr(argo, "GITEA_PASS", "pass")

    argo.ensure_repo_secret("repo-secret", "https://gitea.example/repo")

    assert [call[0] for call in calls] == ["load", "init", "patch", "load", "init", "create"]


def test_ensure_application_patches_existing_matching_runtime_app(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []
    repo_url = "https://gitea.example/demo-agent-runtime"

    class _CustomObjectsApi:
        def get_namespaced_custom_object(self, *args, **kwargs) -> dict[str, Any]:
            calls.append(("get", args, kwargs))
            return {"spec": {"source": {"repoURL": repo_url}}}

        def patch_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("patch", args, kwargs))

        def create_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("create", args, kwargs))

    monkeypatch.setattr(argo, "_load_kube", lambda: None)
    monkeypatch.setattr(argo.client, "CustomObjectsApi", _CustomObjectsApi)

    argo.ensure_application("demo-agent", repo_url)

    assert [call[0] for call in calls] == ["get", "patch"]
    assert calls[0][1][0:4] == ("argoproj.io", "v1alpha1", argo.ARGO_NAMESPACE, "applications")
    assert calls[1][1][0:4] == ("argoproj.io", "v1alpha1", argo.ARGO_NAMESPACE, "applications")
    body = calls[1][1][-1]
    assert body["metadata"]["labels"][argo.A2A_RUNTIME_LABEL] == "true"
    assert body["metadata"]["annotations"]["a2a.cloud/runtime-repo-url"] == repo_url
    assert body["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true",
        "ServerSideApply=true",
    ]
    assert body["spec"]["ignoreDifferences"] == [
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
    ]


def test_ensure_application_reloads_before_create_after_missing_get(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    class _CustomObjectsApi:
        def __init__(self) -> None:
            calls.append(("init", len([c for c in calls if c[0] == "init"])))

        def get_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("get", args, kwargs))
            raise ApiException(status=404)

        def create_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("create", args, kwargs))

    monkeypatch.setattr(argo, "_load_kube", lambda: calls.append(("load",)))
    monkeypatch.setattr(argo.client, "CustomObjectsApi", _CustomObjectsApi)

    argo.ensure_application("demo-agent", "https://gitea.example/demo-agent")

    assert [call[0] for call in calls] == ["load", "init", "get", "load", "init", "create"]


def test_ensure_application_refuses_to_overwrite_non_runtime_app(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    class _CustomObjectsApi:
        def __init__(self) -> None:
            calls.append(("init", len([c for c in calls if c[0] == "init"])))

        def get_namespaced_custom_object(self, *args, **kwargs) -> dict[str, Any]:
            calls.append(("get", args, kwargs))
            return {
                "spec": {
                    "source": {
                        "repoURL": "http://gitea-http.gitea.svc.cluster.local:3000/gitea_admin/a2a-cloud.git",
                    },
                },
            }

        def patch_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("patch", args, kwargs))

        def create_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("create", args, kwargs))

    monkeypatch.setattr(argo, "_load_kube", lambda: calls.append(("load",)))
    monkeypatch.setattr(argo.client, "CustomObjectsApi", _CustomObjectsApi)

    try:
        argo.ensure_application(
            "sample-app",
            "http://gitea-http.gitea.svc.cluster.local:3000/gitea_admin/sample-app-runtime.git",
        )
    except argo.ArgoApplicationConflict as exc:
        assert "refusing to overwrite ArgoCD Application 'sample-app'" in str(exc)
    else:
        raise AssertionError("expected ArgoApplicationConflict")

    assert [call[0] for call in calls] == ["load", "init", "get"]


def test_request_application_refresh_patches_refresh_annotation(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    class _CustomObjectsApi:
        def patch_namespaced_custom_object(self, *args, **kwargs) -> None:
            calls.append(("patch", args, kwargs))

    monkeypatch.setattr(argo, "_load_kube", lambda: calls.append(("load",)))
    monkeypatch.setattr(argo.client, "CustomObjectsApi", _CustomObjectsApi)

    result = argo.request_application_refresh("demo-agent")

    assert result == {"requested": True, "mode": "hard"}
    assert [call[0] for call in calls] == ["load", "patch"]
    patch_args = calls[1][1]
    assert patch_args[0:4] == (
        "argoproj.io",
        "v1alpha1",
        argo.ARGO_NAMESPACE,
        "applications",
    )
    assert patch_args[4] == "demo-agent"
    assert patch_args[5]["metadata"]["annotations"] == {
        "argocd.argoproj.io/refresh": "hard",
    }
