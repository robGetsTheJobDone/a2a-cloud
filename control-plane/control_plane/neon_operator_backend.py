"""Per-agent Neon provisioning via the neon-operator (the hybrid).

Implements the `DatabaseBackend` interface by giving each agent its OWN
operator-managed Neon tenant (real isolation, no shared-compute SPOF) plus our
own compute wrapper + wake-proxy (scale-to-zero, which upstream OSS Neon does
not provide).

Each step here was validated MANUALLY end-to-end in the `neon-v2` spike (two
isolated live agents). This module automates that exact sequence. It is NOT yet
integration-tested inside the control-plane, and is gated behind
`settings.database_backend == "neon_operator"` (default is "postgres_admin", the
existing shared-cluster backend). Do not enable in production without a
co-located integration test + a backed-up per-agent migration (H5).

Prerequisites (environment, one-time):
  * neon-operator + a `Cluster` running in `self.namespace` (operator uses
    short-name DNS, so it must be co-located with its Cluster).
  * Safekeepers registered with storcon (POST /control/v1/safekeeper/{id},
    id>=1, distinct AZs) -- the operator does not do this itself.
  * The compute `config.json` (in `wrapper_configmap`) defines an admin role
    (`admin_user`) with `admin_password`, reachable over the network so this
    backend can create per-agent roles (cloud_admin is local-trust only).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import httpx

from .config import settings
from .database_provisioner import (
    DatabaseBackend,
    ProvisionedDatabaseRole,
    _grant_role_access,
    _grant_role_schema_access,
    _quote_ident,
    _quote_literal,
)
from .models import AgentDatabaseBinding, DatabaseBranch, DatabaseProject, DatabaseRole

log = logging.getLogger(__name__)

NEON_GROUP = "neon.oltp.molnett.org"
NEON_VERSION = "v1alpha1"


class NeonOperatorBackend(DatabaseBackend):
    """Provision an agent as its own operator tenant + compute + wake-proxy."""

    def __init__(
        self,
        *,
        namespace: str,
        cluster_name: str,
        pageserver_url: str,
        storcon_url: str | None = None,
        safekeepers: str,
        compute_image: str,
        wrapper_configmap: str,
        wake_proxy_image: str,
        wake_proxy_configmap: str,
        wake_proxy_sa: str,
        admin_user: str,
        admin_password: str,
        admin_secret_name: str,
        pg_version: int = 16,
        sslmode: str | None = None,
        idle_seconds: int = 300,
        compute_min_replicas: int = 0,
        project_ready_timeout_s: float = 120.0,
        compute_ready_timeout_s: float = 180.0,
        poll_seconds: float = 3.0,
    ) -> None:
        self.namespace = namespace
        self.cluster_name = cluster_name
        self.pageserver_url = pageserver_url.rstrip("/")
        self.storcon_url = (
            storcon_url.rstrip("/")
            if storcon_url
            else f"http://{cluster_name}-storage-controller.{namespace}.svc:8080"
        )
        self.safekeepers = safekeepers
        self.compute_image = compute_image
        self.wrapper_configmap = wrapper_configmap
        self.wake_proxy_image = wake_proxy_image
        self.wake_proxy_configmap = wake_proxy_configmap
        self.wake_proxy_sa = wake_proxy_sa
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.admin_secret_name = admin_secret_name
        self.pg_version = pg_version
        self.sslmode = sslmode
        self.idle_seconds = idle_seconds
        self.compute_min_replicas = compute_min_replicas
        self.project_ready_timeout_s = project_ready_timeout_s
        self.compute_ready_timeout_s = compute_ready_timeout_s
        self.poll_seconds = poll_seconds

    # --- public interface -------------------------------------------------

    async def ensure_binding(
        self,
        *,
        project: DatabaseProject,
        branch: DatabaseBranch,
        role: DatabaseRole,
        binding: AgentDatabaseBinding,
    ) -> ProvisionedDatabaseRole:
        agent = _dns_safe(binding.agent_name)
        tenant_id = await self._ensure_project(agent)
        timeline_id = await self._ensure_timeline(agent, tenant_id)
        await self._ensure_compute(agent, tenant_id, timeline_id)
        await self._ensure_wake_proxy(agent)
        password = secrets.token_urlsafe(32)
        await self._ensure_role(agent, role, password)
        proxy_host = f"{agent}-pg-proxy.{self.namespace}.svc.cluster.local"
        connection_uri = self._connection_uri(role, password, proxy_host)
        return ProvisionedDatabaseRole(
            project_ref=tenant_id,
            branch_ref=timeline_id,
            username=role.username,
            database_name=role.database_name,
            password=password,
            connection_uri=connection_uri,
        )

    # --- k8s clients (sync kubernetes client, run off the loop) -----------

    def _clients(self) -> Any:
        # imported lazily so environments without the kubernetes client (tests)
        # can import this module.
        from kubernetes import client, config as kube_config

        if settings.in_cluster:
            kube_config.load_incluster_config()
        else:
            kube_config.load_kube_config()
        return client

    async def _apply(self, apply_fn) -> None:
        """Create-or-replace a namespaced object via a (get, create, patch) fn."""
        await asyncio.to_thread(apply_fn)

    # --- steps ------------------------------------------------------------

    async def _ensure_project(self, agent: str) -> str:
        """Create the Project CR (= one Neon tenant); wait for the tenant id.

        Deliberately does NOT create a Branch CR: the operator's Branch reconciler
        creates its OWN compute (`<branch>-compute-node`), which conflicts with our
        hybrid compute on the same timeline (two walproposers). We own timelines +
        computes; the operator owns the Cluster + tenant. The timeline is created
        via storcon directly in `_ensure_timeline`.
        """
        client = self._clients()
        co = client.CustomObjectsApi()

        def _put_project() -> None:
            body = {
                "apiVersion": f"{NEON_GROUP}/{NEON_VERSION}",
                "kind": "Project",
                "metadata": {"name": agent, "namespace": self.namespace},
                "spec": {"cluster": self.cluster_name},
            }
            from kubernetes.client.rest import ApiException

            try:
                co.create_namespaced_custom_object(
                    NEON_GROUP, NEON_VERSION, self.namespace, "projects", body
                )
            except ApiException as exc:
                if exc.status != 409:  # already exists -> leave as-is
                    raise

        await self._apply(_put_project)

        # poll for the tenant id the operator writes onto the Project spec.
        deadline = self.project_ready_timeout_s
        waited = 0.0
        while waited < deadline:
            obj = await asyncio.to_thread(
                co.get_namespaced_custom_object,
                NEON_GROUP,
                NEON_VERSION,
                self.namespace,
                "projects",
                agent,
            )
            tenant_id = (obj.get("spec") or {}).get("tenantId")
            if tenant_id:
                return str(tenant_id)
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds
        raise RuntimeError(f"operator did not assign a tenant for agent {agent}")

    async def _ensure_timeline(self, agent: str, tenant_id: str) -> str:
        """Return the tenant's timeline, creating it via storcon if absent.

        We create the timeline through storcon's API (same call the operator's
        Branch makes internally) rather than via a Branch CR, so no conflicting
        operator compute is spawned.
        """
        ps_url = f"{self.pageserver_url}/v1/tenant/{tenant_id}/timeline"
        create_url = f"{self.storcon_url}/v1/tenant/{tenant_id}/timeline"
        deadline = self.project_ready_timeout_s
        async with httpx.AsyncClient(timeout=30) as http:
            # already have a timeline?
            try:
                resp = await http.get(ps_url)
                if resp.status_code == 200 and resp.json():
                    return str(resp.json()[0]["timeline_id"])
            except httpx.HTTPError:
                pass
            # create it via storcon
            timeline_id = secrets.token_hex(16)
            waited = 0.0
            while waited < deadline:
                try:
                    r = await http.post(
                        create_url,
                        json={"new_timeline_id": timeline_id, "pg_version": self.pg_version},
                    )
                    if r.status_code in (200, 201, 202, 409):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self.poll_seconds)
                waited += self.poll_seconds
            # confirm it landed on the pageserver
            waited = 0.0
            while waited < deadline:
                try:
                    resp = await http.get(ps_url)
                    if resp.status_code == 200 and resp.json():
                        return str(resp.json()[0]["timeline_id"])
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self.poll_seconds)
                waited += self.poll_seconds
        raise RuntimeError(f"could not ensure a timeline for tenant {tenant_id}")

    async def _ensure_compute(self, agent: str, tenant_id: str, timeline_id: str) -> None:
        client = self._clients()
        apps = client.AppsV1Api()
        core = client.CoreV1Api()
        name = f"{agent}-compute"
        dep = _compute_deployment(
            name=name,
            namespace=self.namespace,
            image=self.compute_image,
            wrapper_configmap=self.wrapper_configmap,
            tenant_id=tenant_id,
            timeline_id=timeline_id,
            pg_version=self.pg_version,
            admin_secret_name=self.admin_secret_name,
        )
        svc = _service(name, self.namespace, {"app": name}, 55433)
        await self._apply(lambda: _upsert_deployment(apps, self.namespace, name, dep))
        await self._apply(lambda: _upsert_service(core, self.namespace, name, svc))
        await self._wait_deployment_ready(apps, name)

    async def _ensure_wake_proxy(self, agent: str) -> None:
        client = self._clients()
        apps = client.AppsV1Api()
        core = client.CoreV1Api()
        name = f"{agent}-wake-proxy"
        dep = _wake_proxy_deployment(
            name=name,
            namespace=self.namespace,
            image=self.wake_proxy_image,
            configmap=self.wake_proxy_configmap,
            service_account=self.wake_proxy_sa,
            compute_deployment=f"{agent}-compute",
            idle_seconds=self.idle_seconds,
            compute_min_replicas=self.compute_min_replicas,
        )
        proxy_svc = _service(f"{agent}-pg-proxy", self.namespace, {"app": name}, 5432)
        await self._apply(lambda: _upsert_deployment(apps, self.namespace, name, dep))
        await self._apply(
            lambda: _upsert_service(core, self.namespace, f"{agent}-pg-proxy", proxy_svc)
        )
        # The next step (role creation) connects through this proxy, so it must be
        # listening first — otherwise we race it and get connection-refused.
        await self._wait_deployment_ready(apps, name)

    async def _ensure_role(self, agent: str, role: DatabaseRole, password: str) -> None:
        """Create the agent's database + login role on its compute.

        Connects through the agent's wake-proxy as the configured admin role
        (which wakes the compute). cloud_admin is local-trust only, so the
        compute config must define a network-reachable admin (admin_user).
        """
        import asyncpg  # noqa: PLC0415

        proxy_host = f"{agent}-pg-proxy.{self.namespace}.svc.cluster.local"
        admin_dsn = (
            f"postgresql://{self.admin_user}:{self.admin_password}@{proxy_host}:5432/postgres"
        )
        target_dsn = (
            f"postgresql://{self.admin_user}:{self.admin_password}@{proxy_host}:5432/{role.database_name}"
        )

        async def _do_provision() -> None:
            conn = await asyncpg.connect(admin_dsn, timeout=60)
            try:
                try:
                    await conn.execute(f"CREATE DATABASE {_quote_ident(role.database_name)}")
                except asyncpg.DuplicateDatabaseError:
                    pass
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1", role.username
                )
                if exists:
                    await conn.execute(
                        f"ALTER ROLE {_quote_ident(role.username)} WITH LOGIN PASSWORD {_quote_literal(password)}"
                    )
                else:
                    await conn.execute(
                        f"CREATE ROLE {_quote_ident(role.username)} LOGIN PASSWORD {_quote_literal(password)}"
                    )
                await _grant_role_access(conn, role=role)
            finally:
                await conn.close()

            target = await asyncpg.connect(target_dsn, timeout=60)
            try:
                await _grant_role_schema_access(target, role=role)
            finally:
                await target.close()

        # The wake wakes a cold compute, which restarts Postgres while it applies
        # its config -- dropping in-flight connections. The steps are idempotent
        # (CREATE DATABASE ignores duplicate, role is upserted), so retry through
        # that startup churn.
        last: Exception | None = None
        for attempt in range(1, 9):
            try:
                await _do_provision()
                return
            except (
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.exceptions.InterfaceError,
                OSError,
                ConnectionError,
            ) as exc:
                last = exc
                await asyncio.sleep(4.0)
        if last is not None:
            raise last

    async def _wait_deployment_ready(self, apps: Any, name: str) -> None:
        waited = 0.0
        while waited < self.compute_ready_timeout_s:
            dep = await asyncio.to_thread(
                apps.read_namespaced_deployment_status, name, self.namespace
            )
            if (dep.status.ready_replicas or 0) >= 1:
                return
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds
        raise RuntimeError(f"compute deployment {name} did not become ready")

    def _connection_uri(self, role: DatabaseRole, password: str, proxy_host: str) -> str:
        from urllib.parse import quote

        query = f"sslmode={quote(self.sslmode)}" if self.sslmode else ""
        base = (
            f"postgresql://{quote(role.username)}:{quote(password)}@{proxy_host}:5432/"
            f"{quote(role.database_name)}"
        )
        return f"{base}?{query}" if query else base


# --- module helpers: k8s object bodies (mirror the validated manifests) ----


def _dns_safe(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _upsert_deployment(apps: Any, namespace: str, name: str, body: dict[str, Any]) -> None:
    from kubernetes.client.rest import ApiException

    try:
        apps.create_namespaced_deployment(namespace, body)
    except ApiException as exc:
        if exc.status == 409:
            apps.patch_namespaced_deployment(name, namespace, body)
        else:
            raise


def _upsert_service(core: Any, namespace: str, name: str, body: dict[str, Any]) -> None:
    from kubernetes.client.rest import ApiException

    try:
        core.create_namespaced_service(namespace, body)
    except ApiException as exc:
        if exc.status != 409:  # Services are immutable enough; leave existing
            raise


def _service(name: str, namespace: str, selector: dict[str, str], port: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": selector, "ports": [{"port": port, "targetPort": port}]},
    }


def _compute_deployment(
    *,
    name: str,
    namespace: str,
    image: str,
    wrapper_configmap: str,
    tenant_id: str,
    timeline_id: str,
    pg_version: int,
    admin_secret_name: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "compute",
                            "image": image,
                            "command": ["/bin/bash", "/shell/compute.sh"],
                            "env": [
                                {"name": "PG_VERSION", "value": str(pg_version)},
                                {"name": "TENANT_ID", "value": tenant_id},
                                {"name": "TIMELINE_ID", "value": timeline_id},
                                {"name": "NEON_ADMIN_USER", "value": "zenith_admin"},
                                {
                                    "name": "NEON_ADMIN_PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": admin_secret_name, "key": "password"}
                                    },
                                },
                            ],
                            "ports": [{"containerPort": 55433}],
                            "volumeMounts": [
                                {"name": "wrapper", "mountPath": "/shell/compute.sh", "subPath": "compute.sh"},
                                {"name": "cfg", "mountPath": "/config/config.json", "subPath": "config.json"},
                                {"name": "pgdata", "mountPath": "/var/db/postgres"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "wrapper", "configMap": {"name": wrapper_configmap}},
                        {"name": "cfg", "configMap": {"name": wrapper_configmap}},
                        {"name": "pgdata", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def _wake_proxy_deployment(
    *,
    name: str,
    namespace: str,
    image: str,
    configmap: str,
    service_account: str,
    compute_deployment: str,
    idle_seconds: int,
    compute_min_replicas: int,
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "serviceAccountName": service_account,
                    "containers": [
                        {
                            "name": "proxy",
                            "image": image,
                            "command": ["python", "/app/proxy.py"],
                            "env": [
                                {
                                    "name": "NAMESPACE",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                },
                                {"name": "COMPUTE_DEPLOYMENT", "value": compute_deployment},
                                {"name": "BACKEND_HOST", "value": compute_deployment},
                                {"name": "BACKEND_PORT", "value": "55433"},
                                {"name": "LISTEN_PORT", "value": "5432"},
                                {"name": "IDLE_SECONDS", "value": str(idle_seconds)},
                                # Never scale the compute below this. Default 1 =
                                # the compute never restarts, so the stale-basebackup
                                # walproposer PANIC (which only fires on restart) is
                                # impossible. Set 0 to trade that for scale-to-zero.
                                {"name": "COMPUTE_MIN_REPLICAS", "value": str(compute_min_replicas)},
                                {"name": "PGUSER", "value": "cloud_admin"},
                                {"name": "PGPASSWORD", "value": "cloud_admin"},
                                {"name": "PGDATABASE", "value": "postgres"},
                            ],
                            "ports": [{"containerPort": 5432}],
                            "volumeMounts": [
                                {"name": "code", "mountPath": "/app/proxy.py", "subPath": "proxy.py"}
                            ],
                        }
                    ],
                    "volumes": [{"name": "code", "configMap": {"name": configmap}}],
                },
            },
        },
    }
