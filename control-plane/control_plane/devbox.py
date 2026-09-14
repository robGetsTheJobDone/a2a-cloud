"""Provisioning for `a2a ssh <agent>` — the throwaway, scale-to-zero dev box.

One Knative Service per agent (`<agent>-devbox`), running the ``a2a-devbox``
image (node + python + a2a-pack + git + sshd + the WS<->sshd bridge). It scales
to zero when idle and wakes on the next connection. SSH reaches it over the
existing HTTPS ingress as a WebSocket (`wss://<agent>-devbox.<host>/ssh`) — see
``apps/devbox`` — so no raw-TCP ingress is required.

Two independent auth layers, both provisioned here per session:
  * transport: a rotating access token in the devbox secret, checked by the
    bridge (``A2A_DEVBOX_ACCESS_TOKEN``);
  * session: the caller's ephemeral public key (``A2A_DEVBOX_AUTHORIZED_KEYS``).
"""
from __future__ import annotations

import base64
import logging
import os
import re

from kubernetes import client
from kubernetes.client.rest import ApiException

from . import gitea
from . import grants
from .config import settings
from .k8s import (
    KNATIVE_API_GROUP,
    KNATIVE_API_VERSION,
    KNATIVE_SERVICES_PLURAL,
    _load_kube,
)

log = logging.getLogger(__name__)

# Pinned to the image built from apps/devbox/Dockerfile. Override for
# staging/tests via A2A_CP_DEVBOX_IMAGE.
DEVBOX_IMAGE = os.environ.get(
    "A2A_CP_DEVBOX_IMAGE", f"{settings.image_registry}/a2a/a2a-devbox:latest"
)
_IMAGE_DIGEST_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
DEVBOX_PORT = 8000
# The agent's own dev server (uvicorn --reload) listens here inside the box;
# the bridge on DEVBOX_PORT reverse-proxies to it (see apps/devbox bridge.py).
DEVBOX_AGENT_PORT = 8001
# Long enough for a real work session. Requires the cluster
# `max-revision-timeout-seconds` in the Knative config-defaults to be >= this.
DEVBOX_TIMEOUT_SECONDS = 28800  # 8h
DEVBOX_USER = "dev"
# TTL of the transport grant. Only checked at connect time (an open session
# outlives it), and every new connection mints a fresh one, so an hour is ample.
DEVBOX_GRANT_TTL_SECONDS = 3600


def devbox_service_name(agent_name: str) -> str:
    return f"{agent_name}-devbox"


def devbox_secret_name(agent_name: str) -> str:
    return f"devbox-{agent_name}"


def devbox_host(agent_name: str) -> str:
    return settings.ingress_host_template.format(name=devbox_service_name(agent_name))


def _resolve_devbox_image() -> str:
    """Return the operator-pinned devbox image or fail closed.

    Registry tags are untrusted during containment, and anonymous registry
    probing no longer works after authentication. The independently verified
    digest must therefore be supplied through ``A2A_CP_DEVBOX_IMAGE``.
    """
    image = DEVBOX_IMAGE.strip()
    if _IMAGE_DIGEST_RE.fullmatch(image):
        return image
    raise RuntimeError(
        "A2A_CP_DEVBOX_IMAGE must be an independently verified "
        "image@sha256:<64 lowercase hex> reference"
    )


def render_devbox_service(agent_name: str, *, owner_id: int | None = None) -> dict:
    """Knative Service manifest for an agent's dev box.

    Publicly routable (the CLI must reach it from outside), scale-to-zero, one
    replica. The two auth layers — not the network boundary — protect it.
    """
    svc = devbox_service_name(agent_name)
    labels = {"app": svc, "a2a/managed-by": "control-plane", "a2a/devbox": "true"}
    env: list[dict] = [
        {"name": "A2A_DEVBOX_PORT", "value": str(DEVBOX_PORT)},
        {"name": "A2A_DEVBOX_AGENT_PORT", "value": str(DEVBOX_AGENT_PORT)},
        {"name": "A2A_DEVBOX_USER", "value": DEVBOX_USER},
        {"name": "A2A_AGENT_NAME", "value": agent_name},
        # Injected for the phase-2b bridge upgrade (Ed25519 grant verification).
        {
            "name": "A2A_GRANT_VERIFYING_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "platform-secrets", "key": "grant_verifying_key"}
            },
        },
    ]
    if owner_id is not None:
        env.append({"name": "A2A_AGENT_OWNER_ID", "value": str(owner_id)})
    return {
        "apiVersion": f"{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}",
        "kind": "Service",
        "metadata": {
            "name": svc,
            "namespace": settings.agents_namespace,
            "annotations": {"argocd.argoproj.io/sync-options": "ServerSideApply=true"},
            "labels": labels,
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {"app": svc},
                    "annotations": {
                        "autoscaling.knative.dev/min-scale": "0",
                        "autoscaling.knative.dev/max-scale": "1",
                        "autoscaling.knative.dev/target": "1",
                        # Keep the box briefly after disconnect so a quick
                        # reconnect skips a cold start.
                        "autoscaling.knative.dev/scale-to-zero-pod-retention-period": "5m",
                    },
                },
                "spec": {
                    "enableServiceLinks": False,
                    "nodeSelector": {"a2a/worker": "true"},
                    # One box, many connections (VS Code opens several) → don't
                    # let concurrency spawn a second pod; max-scale already caps.
                    "containerConcurrency": 0,
                    "timeoutSeconds": DEVBOX_TIMEOUT_SECONDS,
                    "responseStartTimeoutSeconds": DEVBOX_TIMEOUT_SECONDS,
                    "containers": [
                        {
                            "name": "devbox",
                            "image": _resolve_devbox_image(),
                            "imagePullPolicy": "Always",
                            "env": env,
                            "envFrom": [
                                {
                                    "secretRef": {
                                        "name": devbox_secret_name(agent_name),
                                        "optional": True,
                                    }
                                }
                            ],
                            "ports": [
                                {"containerPort": DEVBOX_PORT, "name": "http1", "protocol": "TCP"}
                            ],
                            "startupProbe": {
                                "httpGet": {"path": "/healthz", "port": DEVBOX_PORT},
                                "periodSeconds": 5,
                                "timeoutSeconds": 3,
                                "failureThreshold": 60,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": DEVBOX_PORT},
                                "periodSeconds": 5,
                                "timeoutSeconds": 3,
                                "failureThreshold": 6,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "4Gi"},
                            },
                        }
                    ],
                },
            }
        },
    }


def build_repo_clone_url(agent_name: str, gitea_owner: str | None) -> str:
    """A credentialed internal git URL the box can clone, using a fresh,
    read-scoped Gitea token (never the admin password). Best-effort: on failure
    return "" and the box just comes up without the repo pre-cloned."""
    repo_owner = gitea_owner or gitea.GITEA_USER
    try:
        token, _name = gitea.create_repo_clone_token(repo_owner, name_hint=agent_name)
    except Exception as exc:  # noqa: BLE001 — clone is a convenience, never fatal
        log.warning("devbox: could not mint gitea clone token for %s: %s", agent_name, exc)
        return ""
    if not token:
        return ""
    host = gitea.GITEA_INTERNAL.removeprefix("http://").removeprefix("https://")
    return f"http://{repo_owner}:{token}@{host}/{repo_owner}/{agent_name}.git"


def _upsert_devbox_secret(agent_name: str, *, data: dict[str, str], owner_id: int | None) -> None:
    """Create/replace the devbox secret wholesale so per-session values (the
    access token + authorized key) rotate out each time."""
    _load_kube()
    core = client.CoreV1Api()
    name = devbox_secret_name(agent_name)
    ns = settings.agents_namespace
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"a2a/managed-by": "control-plane", "a2a/devbox": "true"},
            "annotations": {
                "a2a/agent-name": agent_name,
                **({"a2a/owner-id": str(owner_id)} if owner_id is not None else {}),
            },
        },
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode("ascii") for k, v in data.items()},
    }
    try:
        core.replace_namespaced_secret(name, ns, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespaced_secret(ns, body)


def _apply_devbox_service(agent_name: str, *, owner_id: int | None) -> None:
    _load_kube()
    core = client.CoreV1Api()
    custom = client.CustomObjectsApi()
    ns = settings.agents_namespace
    try:
        core.read_namespace(ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))

    body = render_devbox_service(agent_name, owner_id=owner_id)
    args = (KNATIVE_API_GROUP, KNATIVE_API_VERSION, ns, KNATIVE_SERVICES_PLURAL)
    try:
        custom.patch_namespaced_custom_object(*args, devbox_service_name(agent_name), body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        custom.create_namespaced_custom_object(*args, body)


def _devbox_host_key(agent_name: str) -> str:
    """Stable per-box SSH host key: reuse the one in the existing secret, else
    mint a fresh Ed25519 key. Without this every pod would generate new host
    keys at boot and clients would see a host-key-changed error after each
    image roll or scale-from-zero."""
    _load_kube()
    core = client.CoreV1Api()
    try:
        existing = core.read_namespaced_secret(
            devbox_secret_name(agent_name), settings.agents_namespace
        )
        stored = (existing.data or {}).get("A2A_DEVBOX_HOST_KEY_ED25519")
        if stored:
            return base64.b64decode(stored).decode()
    except ApiException as exc:
        if exc.status != 404:
            raise
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()


def ensure_devbox(
    *,
    agent_name: str,
    owner_id: int | None,
    gitea_owner: str | None,
    public_key: str,
    credentials_json: str | None = None,
) -> dict:
    """Provision (or refresh) the agent's dev box for one SSH session and return
    the connection info the CLI needs."""
    # Stateless transport credential: an Ed25519 grant scoped to this box. The
    # bridge verifies it against A2A_GRANT_VERIFYING_KEY — nothing shared or
    # stored — so concurrent clients (VS Code opens several) never evict each
    # other, and there's no mutable token to leak.
    access_token, _payload = grants.mint_grant_token(
        issuer="control-plane",
        audience=devbox_service_name(agent_name),
        bucket=f"devbox-{agent_name}",
        ttl_seconds=DEVBOX_GRANT_TTL_SECONDS,
    )
    secret_data = {
        "A2A_DEVBOX_AUTHORIZED_KEYS": public_key,
        "A2A_AGENT_REPO": build_repo_clone_url(agent_name, gitea_owner),
        "A2A_AGENT_NAME": agent_name,
        "A2A_DEVBOX_HOST_KEY_ED25519": _devbox_host_key(agent_name),
    }
    if credentials_json:
        # The caller's CLI login, written to ~dev/.a2a/credentials.json by the
        # box entrypoint so `a2a` inside the box is already authenticated.
        secret_data["A2A_CREDENTIALS_JSON"] = credentials_json
    _upsert_devbox_secret(agent_name, owner_id=owner_id, data=secret_data)
    _apply_devbox_service(agent_name, owner_id=owner_id)
    host = devbox_host(agent_name)
    return {
        "wss_url": f"wss://{host}/ssh",
        "access_token": access_token,
        "user": DEVBOX_USER,
        "host": host,
    }


