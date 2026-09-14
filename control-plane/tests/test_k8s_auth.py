from __future__ import annotations

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from control_plane import k8s


def test_load_kube_mirrors_incluster_authorization_to_bearer_token(monkeypatch) -> None:
    def fake_load_incluster_config() -> None:
        cfg = client.Configuration()
        cfg.host = "https://kubernetes.default.svc"
        cfg.api_key["authorization"] = "bearer token-123"
        client.Configuration.set_default(cfg)

    monkeypatch.setattr(k8s.settings, "in_cluster", True)
    monkeypatch.setattr(k8s.config, "load_incluster_config", fake_load_incluster_config)

    k8s._load_kube()

    cfg = client.Configuration.get_default_copy()
    assert cfg.api_key["BearerToken"] == "Bearer token-123"
    assert "BearerToken" not in cfg.api_key_prefix
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer token-123"


def test_load_kube_keeps_bearer_token_in_sync_after_refresh(monkeypatch) -> None:
    def fake_load_incluster_config() -> None:
        cfg = client.Configuration()
        cfg.host = "https://kubernetes.default.svc"
        cfg.api_key["authorization"] = "bearer old-token"

        def refresh(configuration: client.Configuration) -> None:
            configuration.api_key["authorization"] = "bearer fresh-token"

        cfg.refresh_api_key_hook = refresh
        client.Configuration.set_default(cfg)

    monkeypatch.setattr(k8s.settings, "in_cluster", True)
    monkeypatch.setattr(k8s.config, "load_incluster_config", fake_load_incluster_config)

    k8s._load_kube()

    cfg = client.Configuration.get_default_copy()
    assert cfg.api_key["BearerToken"] == "Bearer old-token"
    assert cfg.refresh_api_key_hook is not None
    cfg.refresh_api_key_hook(cfg)
    assert cfg.api_key["BearerToken"] == "Bearer fresh-token"
    assert "BearerToken" not in cfg.api_key_prefix
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer fresh-token"


def test_load_kube_does_not_double_prefix_prefixed_bearer_token_refresh(
    monkeypatch,
) -> None:
    def fake_load_incluster_config() -> None:
        cfg = client.Configuration()
        cfg.host = "https://kubernetes.default.svc"
        cfg.api_key["authorization"] = "bearer old-token"

        def refresh(configuration: client.Configuration) -> None:
            configuration.api_key["authorization"] = "Bearer fresh-token"
            configuration.api_key["BearerToken"] = "Bearer fresh-token"
            configuration.api_key_prefix["BearerToken"] = "Bearer"

        cfg.refresh_api_key_hook = refresh
        client.Configuration.set_default(cfg)

    monkeypatch.setattr(k8s.settings, "in_cluster", True)
    monkeypatch.setattr(k8s.config, "load_incluster_config", fake_load_incluster_config)

    k8s._load_kube()

    cfg = client.Configuration.get_default_copy()
    assert cfg.refresh_api_key_hook is not None
    cfg.refresh_api_key_hook(cfg)
    assert cfg.api_key["BearerToken"] == "Bearer fresh-token"
    assert "BearerToken" not in cfg.api_key_prefix
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer fresh-token"


def test_delete_agent_retries_unauthorized_resource_after_reloading_kube(
    monkeypatch,
) -> None:
    load_calls = 0
    ingress_deletes: list[str] = []
    custom_deletes: list[tuple[str, str]] = []

    def fake_load_kube() -> None:
        nonlocal load_calls
        load_calls += 1

    class FakeAppsV1Api:
        def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
            assert name == "demo-agent"
            assert namespace == "agents"

    class FakeCoreV1Api:
        def delete_namespaced_service(self, name: str, namespace: str) -> None:
            assert name == "demo-agent"
            assert namespace == "agents"

    class FakeNetworkingV1Api:
        def delete_namespaced_ingress(self, name: str, namespace: str) -> None:
            assert namespace == "agents"
            ingress_deletes.append(name)
            if name == "demo-agent-custom-domains" and ingress_deletes.count(name) == 1:
                raise ApiException(status=401, reason="Unauthorized")

    class FakeCustomObjectsApi:
        def delete_namespaced_custom_object(
            self,
            group: str,
            version: str,
            namespace: str,
            plural: str,
            name: str,
        ) -> None:
            assert namespace == "agents"
            custom_deletes.append((plural, name))

        def list_namespaced_custom_object(
            self,
            group: str,
            version: str,
            namespace: str,
            plural: str,
            label_selector: str,
        ) -> dict:
            assert namespace == "agents"
            if group == "serving.knative.dev":
                assert plural == "domainmappings"
                assert label_selector == "app=demo-agent,a2a/custom-domain=true"
            else:
                assert group == "traefik.io"
                assert plural == "middlewares"
                assert label_selector == "app=demo-agent,a2a/custom-domain-redirect=true"
            return {"items": []}

    monkeypatch.setattr(k8s, "_load_kube", fake_load_kube)
    monkeypatch.setattr(k8s.client, "AppsV1Api", FakeAppsV1Api)
    monkeypatch.setattr(k8s.client, "CoreV1Api", FakeCoreV1Api)
    monkeypatch.setattr(k8s.client, "NetworkingV1Api", FakeNetworkingV1Api)
    monkeypatch.setattr(k8s.client, "CustomObjectsApi", FakeCustomObjectsApi)
    monkeypatch.setattr(k8s.settings, "agents_namespace", "agents")

    k8s.delete_agent("demo-agent")

    assert load_calls == 12
    assert custom_deletes == [
        ("services", "demo-agent"),
        ("domainmappings", "demo-agent.example.com"),
        ("middlewares", "demo-agent-custom-domains-https"),
        ("middlewares", "demo-agent-custom-domains-host"),
    ]
    assert ingress_deletes == [
        "demo-agent",
        "demo-agent-custom-domains",
        "demo-agent-custom-domains",
        "demo-agent-custom-domains-http",
    ]


def test_delete_legacy_knative_agent_deletes_owned_core_service(monkeypatch) -> None:
    custom_deletes: list[tuple[str, str]] = []
    service_deletes: list[str] = []

    class FakeCustomObjectsApi:
        def delete_namespaced_custom_object(
            self,
            group: str,
            version: str,
            namespace: str,
            plural: str,
            name: str,
        ) -> None:
            assert namespace == "agents"
            custom_deletes.append((plural, name))

        def list_namespaced_custom_object(
            self,
            group: str,
            version: str,
            namespace: str,
            plural: str,
            label_selector: str,
        ) -> dict:
            assert namespace == "agents"
            if group == "serving.knative.dev":
                assert plural == "domainmappings"
                assert label_selector == "app=demo-agent,a2a/custom-domain=true"
            else:
                assert group == "traefik.io"
                assert plural == "middlewares"
                assert label_selector == "app=demo-agent,a2a/custom-domain-redirect=true"
            return {"items": []}

    class FakeCoreV1Api:
        def read_namespaced_service(self, name: str, namespace: str):
            assert name == "demo-agent"
            assert namespace == "agents"
            return client.V1Service(
                metadata=client.V1ObjectMeta(
                    owner_references=[
                        client.V1OwnerReference(
                            api_version="serving.knative.dev/v1",
                            kind="Route",
                            name="demo-agent",
                            uid="uid",
                        )
                    ]
                ),
                spec=client.V1ServiceSpec(type="ExternalName"),
            )

        def delete_namespaced_service(self, name: str, namespace: str) -> None:
            assert namespace == "agents"
            service_deletes.append(name)

    monkeypatch.setattr(k8s, "_load_kube", lambda: None)
    monkeypatch.setattr(k8s.client, "CustomObjectsApi", FakeCustomObjectsApi)
    monkeypatch.setattr(k8s.client, "CoreV1Api", FakeCoreV1Api)
    monkeypatch.setattr(k8s.settings, "agents_namespace", "agents")

    k8s.delete_legacy_knative_agent("demo-agent")

    assert custom_deletes == [
        ("services", "demo-agent"),
        ("domainmappings", "demo-agent.example.com"),
    ]
    assert service_deletes == ["demo-agent"]


def test_delete_legacy_deployment_agent_drops_core_resources_not_knative_service(
    monkeypatch,
) -> None:
    deployment_deletes: list[str] = []
    ingress_deletes: list[str] = []
    service_deletes: list[str] = []

    class FakeAppsV1Api:
        def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
            assert namespace == "agents"
            deployment_deletes.append(name)

    class FakeNetworkingV1Api:
        def delete_namespaced_ingress(self, name: str, namespace: str) -> None:
            assert namespace == "agents"
            ingress_deletes.append(name)

    class FakeCoreV1Api:
        def read_namespaced_service(self, name: str, namespace: str):
            assert name == "demo-agent"
            # A Deployment-era core Service: plain ClusterIP, no Knative owner.
            return client.V1Service(
                metadata=client.V1ObjectMeta(name=name),
                spec=client.V1ServiceSpec(type="ClusterIP"),
            )

        def delete_namespaced_service(self, name: str, namespace: str) -> None:
            assert namespace == "agents"
            service_deletes.append(name)

    monkeypatch.setattr(k8s, "_load_kube", lambda: None)
    monkeypatch.setattr(k8s.client, "AppsV1Api", FakeAppsV1Api)
    monkeypatch.setattr(k8s.client, "NetworkingV1Api", FakeNetworkingV1Api)
    monkeypatch.setattr(k8s.client, "CoreV1Api", FakeCoreV1Api)
    monkeypatch.setattr(k8s.settings, "agents_namespace", "agents")

    k8s.delete_legacy_deployment_agent("demo-agent")

    assert deployment_deletes == ["demo-agent"]
    assert ingress_deletes == ["demo-agent"]
    # Only the non-Knative leftover Service is removed.
    assert service_deletes == ["demo-agent"]


def test_sync_custom_domain_ingress_retries_unauthorized_after_reloading_kube(
    monkeypatch,
) -> None:
    load_calls = 0
    namespace_reads = 0
    ingress_replaces: list[str] = []
    middleware_patches: list[str] = []
    middleware_deletes: list[str] = []

    def fake_load_kube() -> None:
        nonlocal load_calls
        load_calls += 1

    class FakeCoreV1Api:
        def read_namespace(self, namespace: str) -> None:
            nonlocal namespace_reads
            assert namespace == "agents"
            namespace_reads += 1
            if namespace_reads == 1:
                raise ApiException(status=401, reason="Unauthorized")

    class FakeNetworkingV1Api:
        def replace_namespaced_ingress(self, name: str, namespace: str, body: dict) -> None:
            assert namespace == "agents"
            assert body["metadata"]["name"] == name
            ingress_replaces.append(name)

    class FakeCustomObjectsApi:
        def list_namespaced_custom_object(self, *args: object, **kwargs: object) -> dict:
            return {"items": []}

        def patch_namespaced_custom_object(self, *args: object) -> None:
            middleware_patches.append(str(args[4]))

        def create_namespaced_custom_object(self, *args: object) -> None:
            return None

        def delete_namespaced_custom_object(self, *args: object) -> None:
            middleware_deletes.append(str(args[4]))

    monkeypatch.setattr(k8s, "_load_kube", fake_load_kube)
    monkeypatch.setattr(k8s.client, "CoreV1Api", FakeCoreV1Api)
    monkeypatch.setattr(k8s.client, "NetworkingV1Api", FakeNetworkingV1Api)
    monkeypatch.setattr(k8s.client, "CustomObjectsApi", FakeCustomObjectsApi)
    monkeypatch.setattr(k8s.settings, "agents_namespace", "agents")

    k8s.sync_custom_domain_ingress(
        "demo-agent",
        [{"hostname": "demo.example.com", "canonical_hostname": "demo.example.com"}],
    )

    assert load_calls == 2
    assert namespace_reads == 2
    assert ingress_replaces == [
        "demo-agent-custom-domains",
        "demo-agent-custom-domains-http",
    ]
    assert middleware_patches == ["demo-agent-custom-domains-https"]
    assert middleware_deletes == ["demo-agent-custom-domains-host"]


def test_gateway_alias_cannot_be_deployed_or_deleted_as_an_agent(monkeypatch) -> None:
    load_calls = 0

    def fake_load_kube() -> None:
        nonlocal load_calls
        load_calls += 1

    class ExplodingCoreV1Api:
        def delete_namespaced_service(self, name: str, namespace: str) -> None:
            raise AssertionError(f"must not delete {namespace}/{name}")

    monkeypatch.setattr(k8s, "_load_kube", fake_load_kube)
    monkeypatch.setattr(k8s.client, "CoreV1Api", ExplodingCoreV1Api)

    with pytest.raises(ValueError, match="reserved for platform infrastructure"):
        k8s.deploy_agent("agent-ingress-gateway", "image", True, {})
    assert load_calls == 0

    k8s._delete_agent_resource("agent-ingress-gateway", "service")
    assert load_calls == 1


def test_delete_legacy_deployment_agent_keeps_knative_owned_core_service(
    monkeypatch,
) -> None:
    service_deletes: list[str] = []

    class FakeAppsV1Api:
        def delete_namespaced_deployment(self, name: str, namespace: str) -> None:
            raise ApiException(status=404, reason="Not Found")

    class FakeNetworkingV1Api:
        def delete_namespaced_ingress(self, name: str, namespace: str) -> None:
            raise ApiException(status=404, reason="Not Found")

    class FakeCoreV1Api:
        def read_namespaced_service(self, name: str, namespace: str):
            # Knative-owned route Service must survive the migration cleanup.
            return client.V1Service(
                metadata=client.V1ObjectMeta(
                    name=name,
                    owner_references=[
                        client.V1OwnerReference(
                            api_version="serving.knative.dev/v1",
                            kind="Route",
                            name=name,
                            uid="uid",
                        )
                    ],
                ),
                spec=client.V1ServiceSpec(type="ClusterIP"),
            )

        def delete_namespaced_service(self, name: str, namespace: str) -> None:
            service_deletes.append(name)

    monkeypatch.setattr(k8s, "_load_kube", lambda: None)
    monkeypatch.setattr(k8s.client, "AppsV1Api", FakeAppsV1Api)
    monkeypatch.setattr(k8s.client, "NetworkingV1Api", FakeNetworkingV1Api)
    monkeypatch.setattr(k8s.client, "CoreV1Api", FakeCoreV1Api)
    monkeypatch.setattr(k8s.settings, "agents_namespace", "agents")

    # Missing Deployment/Ingress (404) plus a Knative-owned Service is a no-op.
    k8s.delete_legacy_deployment_agent("demo-agent")

    assert service_deletes == []
