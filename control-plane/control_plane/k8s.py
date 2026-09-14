"""Apply Kubernetes Deployment/Service/Ingress for an agent.

Uses the in-cluster service account; the control plane pod is granted
RBAC to manage objects in the agents namespace only.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .agent_secret_names import agent_runtime_secret_name
from .config import settings

log = logging.getLogger(__name__)
SERVICE_ACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
KNATIVE_API_GROUP = "serving.knative.dev"
KNATIVE_API_VERSION = "v1"
KNATIVE_SERVICES_PLURAL = "services"
KNATIVE_DOMAIN_MAPPING_API_VERSION = "v1beta1"
KNATIVE_DOMAIN_MAPPINGS_PLURAL = "domainmappings"
TRAEFIK_API_GROUP = "traefik.io"
TRAEFIK_API_VERSION = "v1alpha1"
TRAEFIK_MIDDLEWARES_PLURAL = "middlewares"
CERT_MANAGER_API_GROUP = "cert-manager.io"
CERT_MANAGER_API_VERSION = "v1"
CERT_MANAGER_CERTIFICATES_PLURAL = "certificates"
AVAILABILITY_ALWAYS_ON = "always_on"
AGENT_INGRESS_GATEWAY_SERVICE = "agent-ingress-gateway"
# Every literal first path segment declared under the ``/v1/agents`` prefix.
# ``/v1/agents/{name}`` shares that prefix, so an agent named "search" or
# "mine" is created fine and then loses every one of its own API calls to the
# platform route that matches first. Reserving the names is the only fix that
# does not reorder the route table.
#
# This list is the route table, not a guess: ``tests/test_reserved_agent_names``
# walks the mounted FastAPI app and fails if the two ever drift apart.
#
# Enforcement is not yet complete, and knowing where it stops matters:
# ``deploy_agent`` below refuses a reserved name, and
# ``routes.agents._validate_agent_name`` rejects one on /import, /from-openapi
# and /compose — but ``POST /v1/agents/from-tarball`` and
# ``POST /v1/agents/from-source``, the two routes ``a2a deploy`` uses, never
# call that validator, so they still accept these names. The same test module
# drives both routes and pins that gap so it stays visible; closing it is a
# change in ``routes/agents.py``.
API_RESERVED_AGENT_NAMES = frozenset(
    {
        "compose",
        "from-openapi",
        "from-source",
        "from-tarball",
        "import",
        "mine",
        "openapi",
        "search",
        "studio",
    }
)
PLATFORM_RESERVED_AGENT_NAMES = frozenset(
    {AGENT_INGRESS_GATEWAY_SERVICE} | API_RESERVED_AGENT_NAMES
)
# Max request duration Knative permits per the cluster KnativeServing config
# (``max-revision-timeout-seconds``). Agent timeouts are clamped to this.
KNATIVE_MAX_TIMEOUT_SECONDS = 1800


def default_agent_timeout_seconds() -> int:
    try:
        configured = int(settings.agents_default_timeout_seconds)
    except (TypeError, ValueError):
        configured = KNATIVE_MAX_TIMEOUT_SECONDS
    return min(max(configured, 1), KNATIVE_MAX_TIMEOUT_SECONDS)


def always_on_agents() -> set[str]:
    """Names of platform-critical agents that must never scale to zero."""
    return {n.strip() for n in settings.always_on_agents.split(",") if n.strip()}


def agent_min_scale(name: str) -> int:
    """Resolve the Knative ``min-scale`` for an agent.

    Ordinary user agents default to ``settings.agents_min_scale`` (0 =
    scale-to-zero). Agents named in ``settings.always_on_agents`` are pinned to
    at least one replica so passive idleness never tears them down.
    """
    if name in always_on_agents():
        return max(1, settings.agents_min_scale)
    return max(0, settings.agents_min_scale)


class KubernetesDeleteError(RuntimeError):
    """Raised after best-effort deletion leaves one or more resources behind."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__("; ".join(failures))


def _load_kube() -> None:
    if settings.in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=settings.kubeconfig)
    _normalize_bearer_token_auth()


def _normalize_bearer_token_auth() -> None:
    """Make kubernetes-client auth compatible with generated v36 APIs.

    ``load_incluster_config`` currently populates ``api_key["authorization"]``.
    The generated clients in kubernetes==36 read ``api_key["BearerToken"]``.
    Without mirroring the key, in-cluster calls reach the API server with no
    auth header and fail as 401 even though the mounted service-account token
    and RBAC are valid.

    Store the already-prefixed value under both keys.  The v36 compatibility
    alias can rewrite ``BearerToken`` from ``authorization`` during refresh; if
    we also leave ``api_key_prefix["BearerToken"]`` set, PATCH requests can be
    sent as ``Bearer Bearer <token>`` and Kubernetes rejects them as 401.
    """
    cfg = client.Configuration.get_default_copy()
    original_refresh = cfg.refresh_api_key_hook

    def mirror_token(configuration: client.Configuration) -> None:
        auth = configuration.api_key.get("authorization") or configuration.api_key.get("BearerToken")
        token = _token_from_auth_value(auth)
        if not token and settings.in_cluster:
            try:
                with open(SERVICE_ACCOUNT_TOKEN_PATH, encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                token = ""
        if token:
            bearer = f"Bearer {token}"
            configuration.api_key["authorization"] = bearer
            configuration.api_key["BearerToken"] = bearer
            configuration.api_key_prefix.pop("BearerToken", None)

    def refresh(configuration: client.Configuration) -> None:
        if original_refresh is not None:
            original_refresh(configuration)
        mirror_token(configuration)

    mirror_token(cfg)
    cfg.refresh_api_key_hook = refresh
    client.Configuration.set_default(cfg)


def _token_from_auth_value(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.lower().startswith("bearer "):
        return value.split(None, 1)[1].strip()
    return value


def render_manifests(
    name: str,
    image: str,
    public: bool,
    card: dict[str, Any],
    *,
    owner_id: int | None = None,
) -> list[dict[str, Any]]:
    rt = card.get("runtime", {}) or {}
    res = rt.get("resources", {}) or {}
    # Declared cpu is the burst LIMIT, not a reservation; the request is a
    # small fixed value so idle (I/O-bound) agents don't reserve cores they
    # never use and starve the single node's scheduler.
    cpu_limit = res.get("cpu", "1")
    memory = res.get("memory", "256Mi")
    # Forwarded to the agent pod so a2a-pack's auth gate (mcp/http.py)
    # can identify the owner + decide whether to allow anonymous calls.
    env: list[dict[str, Any]] = [
        {"name": "A2A_AGENT_NAME", "value": name},
        {"name": "A2A_AGENT_PUBLIC", "value": "true" if public else "false"},
        {"name": "A2A_CP_URL", "value": settings.public_cp_url},
        {"name": "A2A_LOGIN_URL", "value": settings.dashboard_url},
        # The agent's own origin-bound session, minted by /auth/callback. Never
        # the dashboard's ``__Host-`` cookie: that one is host-locked to the
        # dashboard and must stay unreadable to user-controlled agent code.
        {"name": "A2A_SESSION_COOKIE_NAME", "value": settings.agent_session_cookie_name},
        {
            "name": "A2A_AGENT_SESSION_AUTHORIZE_URL",
            "value": f"{settings.dashboard_url.rstrip('/')}/v1/auth/agent-session/authorize",
        },
        # Public executions are sealed by the trusted ingress gateway.  Keep
        # receipt/replay private keys out of this user-controlled container and
        # suppress the SDK's local best-effort signer/error event.
        {"name": "A2A_EVIDENCE_SIGNING_MODE", "value": "gateway"},
        {"name": "A2A_AGENT_IMAGE", "value": image},
        {"name": "A2A_RENDER_IMAGE", "value": image},
        {
            "name": "A2A_GRANT_VERIFYING_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "platform-secrets", "key": "grant_verifying_key"}
            },
        },
    ]
    if owner_id is not None:
        env.append({"name": "A2A_AGENT_OWNER_ID", "value": str(owner_id)})

    concurrency = _positive_int(rt.get("concurrency"), default=100, maximum=100)
    timeout = _declared_runtime_timeout(card, rt, res)
    availability = str(rt.get("availability") or "").strip().lower()
    if availability == AVAILABILITY_ALWAYS_ON:
        labels = {
            "app": name,
            "a2a/managed-by": "control-plane",
            "a2a/availability": AVAILABILITY_ALWAYS_ON,
            "a2a/workload-class": "user",
        }
        docs = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": settings.agents_namespace,
                    "labels": labels,
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "imagePullSecrets": [{"name": "registry-pull-credentials"}],
                            "nodeSelector": {"a2a/worker": "true"},
                            "containers": [
                                {
                                    "name": "agent",
                                    "image": image,
                                    "imagePullPolicy": "Always",
                                    "env": env,
                                    "envFrom": [
                                        {
                                            "secretRef": {
                                                "name": agent_runtime_secret_name(name),
                                                "optional": True,
                                            }
                                        }
                                    ],
                                    "ports": [
                                        {
                                            "containerPort": 8000,
                                            "name": "http",
                                            "protocol": "TCP",
                                        }
                                    ],
                                    "readinessProbe": {
                                        "httpGet": {"path": "/healthz", "port": "http"},
                                        "periodSeconds": 5,
                                        "timeoutSeconds": 3,
                                        "failureThreshold": 6,
                                        "successThreshold": 1,
                                    },
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": memory},
                                        "limits": {"cpu": cpu_limit, "memory": "2Gi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": name,
                    "namespace": settings.agents_namespace,
                    "labels": labels,
                },
                "spec": {
                    "selector": {"app": name},
                    "ports": [
                        {
                            "name": "http",
                            "port": 80,
                            "targetPort": "http",
                            "protocol": "TCP",
                        }
                    ],
                },
            },
        ]
        if public:
            docs.append(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": {
                        "name": name,
                        "namespace": settings.agents_namespace,
                        "labels": labels,
                    },
                    "spec": {
                        "rules": [
                            {
                                "host": settings.ingress_host_template.format(name=name),
                                "http": {
                                    "paths": [
                                        {
                                            "path": "/",
                                            "pathType": "Prefix",
                                            "backend": {
                                                "service": {
                                                    "name": AGENT_INGRESS_GATEWAY_SERVICE,
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
        return docs

    # Knative scales user agents to zero by default. Public agents get the
    # auto-assigned ``{name}.<platform_domain>`` route via the cluster domain
    # template; private agents are kept cluster-local. Edge TLS/routing to
    # kourier is wired separately (rollout/backfill task).
    labels = {
        "app": name,
        "a2a/managed-by": "control-plane",
        "a2a/workload-class": "user",
    }
    if not public:
        labels["networking.knative.dev/visibility"] = "cluster-local"
    docs: list[dict[str, Any]] = [
        {
            "apiVersion": f"{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": settings.agents_namespace,
                "annotations": {
                    "argocd.argoproj.io/sync-options": "ServerSideApply=true",
                },
                "labels": labels,
            },
            "spec": {
                "template": {
                    "metadata": {
                        "labels": {"app": name, "a2a/workload-class": "user"},
                        "annotations": {
                            "autoscaling.knative.dev/min-scale": str(agent_min_scale(name)),
                            "autoscaling.knative.dev/max-scale": str(settings.agents_max_scale),
                            "autoscaling.knative.dev/target": str(concurrency),
                        },
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "imagePullSecrets": [{"name": "registry-pull-credentials"}],
                        # Hosted user agents should run on worker capacity, not
                        # the storage/control-plane node that carries state.
                        "nodeSelector": {"a2a/worker": "true"},
                        "containerConcurrency": concurrency,
                        "timeoutSeconds": timeout,
                        "responseStartTimeoutSeconds": timeout,
                        "containers": [
                            {
                                "name": "agent",
                                "image": image,
                                "imagePullPolicy": "Always",
                                "env": env,
                                "envFrom": [
                                    {
                                        "secretRef": {
                                            "name": agent_runtime_secret_name(name),
                                            "optional": True,
                                        }
                                    }
                                ],
                                "ports": [
                                    {
                                        "containerPort": 8000,
                                        "name": "http1",
                                        "protocol": "TCP",
                                    }
                                ],
                                "startupProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8000},
                                    "periodSeconds": 5,
                                    "timeoutSeconds": 3,
                                    "failureThreshold": 60,
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8000},
                                    "periodSeconds": 5,
                                    "timeoutSeconds": 3,
                                    "failureThreshold": 6,
                                    "successThreshold": 1,
                                },
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": memory},
                                    "limits": {"cpu": cpu_limit, "memory": "2Gi"},
                                },
                            }
                        ],
                    },
                },
            },
        },
    ]
    return docs


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return min(parsed, maximum)


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _declared_runtime_timeout(
    card: dict[str, Any],
    rt: dict[str, Any],
    res: dict[str, Any],
) -> int:
    candidates: list[int] = []
    for raw in (
        rt.get("max_runtime_seconds"),
        res.get("max_runtime_seconds"),
    ):
        value = _positive_int_or_none(raw)
        if value is not None:
            candidates.append(value)

    skills = card.get("skills")
    if not isinstance(skills, list):
        capabilities = card.get("capabilities")
        if isinstance(capabilities, dict):
            skills = capabilities.get("skills")
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            policy = skill.get("policy")
            if not isinstance(policy, dict):
                continue
            for key in ("timeout_seconds", "grant_run_timeout_seconds"):
                value = _positive_int_or_none(policy.get(key))
                if value is not None:
                    candidates.append(value)

    if not candidates:
        return default_agent_timeout_seconds()
    return min(max(candidates), KNATIVE_MAX_TIMEOUT_SECONDS)


def render_domain_mapping(name: str, hostname: str, *, custom: bool = False) -> dict[str, Any]:
    labels = {"app": name, "a2a/managed-by": "control-plane"}
    if custom:
        labels["a2a/custom-domain"] = "true"
    return {
        "apiVersion": f"{KNATIVE_API_GROUP}/{KNATIVE_DOMAIN_MAPPING_API_VERSION}",
        "kind": "DomainMapping",
        "metadata": {
            "name": hostname,
            "namespace": settings.agents_namespace,
            "labels": labels,
        },
        "spec": {
            "ref": {
                "apiVersion": f"{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}",
                "kind": "Service",
                "name": name,
            }
        },
    }


def custom_domain_ingress_name(name: str) -> str:
    return f"{name}-custom-domains"


def custom_domain_http_ingress_name(name: str) -> str:
    return f"{name}-custom-domains-http"


def custom_domain_tls_secret_name(name: str) -> str:
    return f"{name}-custom-domains-tls"


def custom_domain_https_redirect_middleware_name(name: str) -> str:
    return f"{name}-custom-domains-https"


def custom_domain_host_rewrite_middleware_name(name: str) -> str:
    return f"{name}-custom-domains-host"


def custom_domain_redirect_middleware_name(name: str, hostname: str) -> str:
    host_slug = re.sub(r"[^a-z0-9-]+", "-", hostname.strip().lower()).strip("-")
    host_slug = re.sub(r"-+", "-", host_slug)[:32].strip("-") or "host"
    digest = hashlib.sha1(hostname.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"{name}-redir-{host_slug}-{digest}"[:63].rstrip("-")


def _custom_domain_route_specs(routes: list[Any]) -> list[dict[str, str | bool]]:
    specs: list[dict[str, str | bool]] = []
    for route in routes:
        if isinstance(route, str):
            hostname = route.strip().lower()
            if hostname:
                specs.append(
                    {
                        "hostname": hostname,
                        "canonical_hostname": hostname,
                        "redirect_enabled": False,
                    }
                )
            continue
        if not isinstance(route, dict):
            continue
        hostname = str(route.get("hostname") or "").strip().lower()
        if not hostname:
            continue
        canonical = str(route.get("canonical_hostname") or hostname).strip().lower()
        specs.append(
            {
                "hostname": hostname,
                "canonical_hostname": canonical or hostname,
                "redirect_enabled": bool(route.get("redirect_enabled"))
                and bool(canonical)
                and canonical != hostname,
            }
        )
    return sorted(specs, key=lambda item: str(item["hostname"]))


def render_custom_domain_ingress(name: str, routes: list[Any]) -> dict[str, Any] | None:
    route_specs = _custom_domain_route_specs(routes)
    hosts = sorted({str(route["hostname"]) for route in route_specs})
    if not hosts:
        return None
    middlewares = [
        f"{settings.agents_namespace}-{custom_domain_redirect_middleware_name(name, str(route['hostname']))}@kubernetescrd"
        for route in route_specs
        if route["redirect_enabled"]
    ]
    paths = [
        {
            "path": "/",
            "pathType": "Prefix",
            "backend": {
                "service": {
                    "name": AGENT_INGRESS_GATEWAY_SERVICE,
                    "port": {"number": 80},
                }
            },
        }
    ]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": custom_domain_ingress_name(name),
            "namespace": settings.agents_namespace,
            "labels": {
                "app": name,
                "a2a/managed-by": "control-plane",
                "a2a/custom-domain": "true",
            },
            "annotations": {
                "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                "traefik.ingress.kubernetes.io/router.tls": "true",
                **(
                    {
                        "traefik.ingress.kubernetes.io/router.middlewares": ",".join(
                            middlewares
                        )
                    }
                    if middlewares
                    else {}
                ),
            },
        },
        "spec": {
            "tls": [{"hosts": hosts, "secretName": custom_domain_tls_secret_name(name)}],
            "rules": [
                {
                    "host": host,
                    "http": {"paths": paths},
                }
                for host in hosts
            ],
        },
    }


def render_custom_domain_http_redirect_ingress(
    name: str,
    routes: list[Any],
) -> dict[str, Any] | None:
    route_specs = _custom_domain_route_specs(routes)
    hosts = sorted({str(route["hostname"]) for route in route_specs})
    if not hosts:
        return None
    middlewares = [
        f"{settings.agents_namespace}-{custom_domain_redirect_middleware_name(name, str(route['hostname']))}@kubernetescrd"
        for route in route_specs
        if route["redirect_enabled"]
    ]
    middlewares.append(
        f"{settings.agents_namespace}-{custom_domain_https_redirect_middleware_name(name)}"
        "@kubernetescrd"
    )
    paths = [
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
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": custom_domain_http_ingress_name(name),
            "namespace": settings.agents_namespace,
            "labels": {
                "app": name,
                "a2a/managed-by": "control-plane",
                "a2a/custom-domain": "true",
                "a2a/custom-domain-http": "true",
            },
            "annotations": {
                "traefik.ingress.kubernetes.io/router.entrypoints": "web",
                "traefik.ingress.kubernetes.io/router.middlewares": ",".join(
                    middlewares
                ),
            },
        },
        "spec": {
            "rules": [
                {
                    "host": host,
                    "http": {"paths": paths},
                }
                for host in hosts
            ],
        },
    }


def render_custom_domain_https_redirect_middleware(name: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{TRAEFIK_API_GROUP}/{TRAEFIK_API_VERSION}",
        "kind": "Middleware",
        "metadata": {
            "name": custom_domain_https_redirect_middleware_name(name),
            "namespace": settings.agents_namespace,
            "labels": {
                "app": name,
                "a2a/managed-by": "control-plane",
                "a2a/custom-domain": "true",
                "a2a/custom-domain-https-redirect": "true",
            },
        },
        "spec": {
            "redirectScheme": {
                "scheme": "https",
                "permanent": True,
            }
        },
    }


def render_custom_domain_redirect_middlewares(
    name: str,
    routes: list[Any],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for route in _custom_domain_route_specs(routes):
        if not route["redirect_enabled"]:
            continue
        hostname = str(route["hostname"])
        canonical = str(route["canonical_hostname"])
        docs.append(
            {
                "apiVersion": f"{TRAEFIK_API_GROUP}/{TRAEFIK_API_VERSION}",
                "kind": "Middleware",
                "metadata": {
                    "name": custom_domain_redirect_middleware_name(name, hostname),
                    "namespace": settings.agents_namespace,
                    "labels": {
                        "app": name,
                        "a2a/managed-by": "control-plane",
                        "a2a/custom-domain": "true",
                        "a2a/custom-domain-redirect": "true",
                    },
                },
                "spec": {
                    "redirectRegex": {
                        "regex": f"^https?://{re.escape(hostname)}(/.*)?$",
                        "replacement": f"https://{canonical}${{1}}",
                        "permanent": True,
                    }
                },
            }
        )
    return docs


def sync_custom_domain_ingress(name: str, routes: list[Any]) -> None:
    """Create/update/delete the extra Ingress used for verified custom domains."""
    for attempt in range(2):
        _load_kube()
        try:
            _sync_custom_domain_ingress_once(name, routes)
            return
        except ApiException as exc:
            if exc.status == 401 and attempt == 0:
                log.warning(
                    "custom domain sync for %s got 401; reloading kube config and retrying",
                    name,
                )
                continue
            raise


def _sync_custom_domain_ingress_once(name: str, routes: list[Any]) -> None:
    core = client.CoreV1Api()
    net = client.NetworkingV1Api()
    ns = settings.agents_namespace
    ingress_name = custom_domain_ingress_name(name)
    http_ingress_name = custom_domain_http_ingress_name(name)

    try:
        core.read_namespace(ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))

    body = render_custom_domain_ingress(name, routes)
    http_body = render_custom_domain_http_redirect_ingress(name, routes)
    if body is None:
        try:
            net.delete_namespaced_ingress(ingress_name, ns)
        except ApiException as exc:
            if exc.status not in (404, 410):
                raise
        try:
            net.delete_namespaced_ingress(http_ingress_name, ns)
        except ApiException as exc:
            if exc.status not in (404, 410):
                raise
        _sync_custom_domain_redirect_middlewares(name, [])
        _delete_custom_domain_https_redirect_middleware(name)
        _delete_custom_domain_host_rewrite_middleware(name)
        return

    _sync_custom_domain_https_redirect_middleware(name)
    # Older routes rewrote Host to <agent>.<platform_domain> so Knative could select
    # the workload directly. The trusted gateway must receive the verified
    # custom Host so it can resolve that active domain and preserve it for the
    # application. Reconcile away any legacy rewrite before replacing ingress.
    _delete_custom_domain_host_rewrite_middleware(name)
    _sync_custom_domain_redirect_middlewares(name, routes)
    try:
        net.replace_namespaced_ingress(ingress_name, ns, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        net.create_namespaced_ingress(ns, body)
    if http_body is not None:
        try:
            net.replace_namespaced_ingress(http_ingress_name, ns, http_body)
        except ApiException as exc:
            if exc.status != 404:
                raise
            net.create_namespaced_ingress(ns, http_body)


def _delete_custom_domain_host_rewrite_middleware(name: str) -> None:
    custom = client.CustomObjectsApi()
    try:
        custom.delete_namespaced_custom_object(
            TRAEFIK_API_GROUP,
            TRAEFIK_API_VERSION,
            settings.agents_namespace,
            TRAEFIK_MIDDLEWARES_PLURAL,
            custom_domain_host_rewrite_middleware_name(name),
        )
    except ApiException as exc:
        if exc.status not in (404, 410):
            raise


def _sync_custom_domain_https_redirect_middleware(name: str) -> None:
    custom = client.CustomObjectsApi()
    ns = settings.agents_namespace
    doc = render_custom_domain_https_redirect_middleware(name)
    args = (
        TRAEFIK_API_GROUP,
        TRAEFIK_API_VERSION,
        ns,
        TRAEFIK_MIDDLEWARES_PLURAL,
    )
    middleware_name = doc["metadata"]["name"]
    try:
        custom.patch_namespaced_custom_object(*args, middleware_name, doc)
    except ApiException as exc:
        if exc.status != 404:
            raise
        custom.create_namespaced_custom_object(*args, doc)


def _delete_custom_domain_https_redirect_middleware(name: str) -> None:
    custom = client.CustomObjectsApi()
    try:
        custom.delete_namespaced_custom_object(
            TRAEFIK_API_GROUP,
            TRAEFIK_API_VERSION,
            settings.agents_namespace,
            TRAEFIK_MIDDLEWARES_PLURAL,
            custom_domain_https_redirect_middleware_name(name),
        )
    except ApiException as exc:
        if exc.status not in (404, 410):
            raise


def _sync_custom_domain_redirect_middlewares(name: str, routes: list[Any]) -> None:
    custom = client.CustomObjectsApi()
    ns = settings.agents_namespace
    desired_docs = render_custom_domain_redirect_middlewares(name, routes)
    desired_names = {doc["metadata"]["name"] for doc in desired_docs}
    args = (
        TRAEFIK_API_GROUP,
        TRAEFIK_API_VERSION,
        ns,
        TRAEFIK_MIDDLEWARES_PLURAL,
    )
    try:
        existing = custom.list_namespaced_custom_object(
            *args,
            label_selector=f"app={name},a2a/custom-domain-redirect=true",
        )
    except ApiException as exc:
        if exc.status == 404:
            existing = {"items": []}
        else:
            raise
    for item in existing.get("items", []) if isinstance(existing, dict) else []:
        metadata = item.get("metadata") if isinstance(item, dict) else {}
        middleware_name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(middleware_name, str) and middleware_name not in desired_names:
            custom.delete_namespaced_custom_object(*args, middleware_name)
    for doc in desired_docs:
        middleware_name = doc["metadata"]["name"]
        try:
            custom.patch_namespaced_custom_object(*args, middleware_name, doc)
        except ApiException as exc:
            if exc.status != 404:
                raise
            custom.create_namespaced_custom_object(*args, doc)


def custom_domain_certificate_status(name: str) -> str:
    _load_kube()
    custom = client.CustomObjectsApi()
    try:
        cert = custom.get_namespaced_custom_object(
            CERT_MANAGER_API_GROUP,
            CERT_MANAGER_API_VERSION,
            settings.agents_namespace,
            CERT_MANAGER_CERTIFICATES_PLURAL,
            custom_domain_tls_secret_name(name),
        )
    except ApiException as exc:
        if exc.status == 404:
            return "provisioning"
        raise
    status = cert.get("status") if isinstance(cert, dict) else {}
    conditions = status.get("conditions") if isinstance(status, dict) else []
    if isinstance(conditions, list):
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get("type") != "Ready":
                continue
            if condition.get("status") == "True":
                return "ready"
            if condition.get("status") == "False":
                reason = str(condition.get("reason") or "not_ready").lower()
                return f"not_ready:{reason}"
    return "provisioning"


def deploy_agent(
    name: str,
    image: str,
    public: bool,
    card: dict[str, Any],
    *,
    owner_id: int | None = None,
) -> str | None:
    """Apply manifests; return public URL if ``public`` else None."""
    if name in PLATFORM_RESERVED_AGENT_NAMES:
        raise ValueError(f"agent name {name!r} is reserved for platform infrastructure")
    _load_kube()
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    custom = client.CustomObjectsApi()
    net = client.NetworkingV1Api()
    ns = settings.agents_namespace

    try:
        core.read_namespace(ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))

    docs = render_manifests(name, image, public, card, owner_id=owner_id)
    for doc in docs:
        kind = doc["kind"]
        api_version = doc.get("apiVersion", "")
        body = doc
        if kind == "Service" and api_version == f"{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}":
            args = (
                KNATIVE_API_GROUP,
                KNATIVE_API_VERSION,
                ns,
                KNATIVE_SERVICES_PLURAL,
            )
            try:
                custom.patch_namespaced_custom_object(*args, name, body)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                custom.create_namespaced_custom_object(*args, body)
        elif (
            kind == "DomainMapping"
            and api_version == f"{KNATIVE_API_GROUP}/{KNATIVE_DOMAIN_MAPPING_API_VERSION}"
        ):
            args = (
                KNATIVE_API_GROUP,
                KNATIVE_DOMAIN_MAPPING_API_VERSION,
                ns,
                KNATIVE_DOMAIN_MAPPINGS_PLURAL,
            )
            mapping_name = body["metadata"]["name"]
            try:
                custom.patch_namespaced_custom_object(*args, mapping_name, body)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                custom.create_namespaced_custom_object(*args, body)
        elif kind == "Deployment":
            try:
                apps.replace_namespaced_deployment(name, ns, body)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                apps.create_namespaced_deployment(ns, body)
        elif kind == "Service":
            try:
                core.replace_namespaced_service(name, ns, body)
            except ApiException as exc:
                if exc.status == 404:
                    core.create_namespaced_service(ns, body)
                elif exc.status == 422:
                    # immutable fields (clusterIP) on update; patch instead
                    core.patch_namespaced_service(name, ns, body)
                else:
                    raise
        elif kind == "Ingress":
            try:
                net.replace_namespaced_ingress(name, ns, body)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                net.create_namespaced_ingress(ns, body)

    kinds = {(doc.get("apiVersion", ""), doc.get("kind", "")) for doc in docs}
    if (f"{KNATIVE_API_GROUP}/{KNATIVE_API_VERSION}", "Service") in kinds:
        # The agent now runs as a Knative Service; clear any Deployment-era core
        # resources left over from before the migration. Best-effort — never fail
        # a successful deploy because old resources lingered.
        try:
            delete_legacy_deployment_agent(name)
        except KubernetesDeleteError as exc:
            log.warning("legacy Deployment cleanup for %s incomplete: %s", name, exc)
    elif ("apps/v1", "Deployment") in kinds:
        try:
            delete_legacy_knative_agent(name)
        except KubernetesDeleteError as exc:
            log.warning("legacy Knative cleanup for %s incomplete: %s", name, exc)

    return f"https://{settings.ingress_host_template.format(name=name)}" if public else None


def delete_agent(name: str) -> None:
    failures: list[str] = []
    for resource in (
        "knative service",
        "domain mapping",
        "custom domain mappings",
        "custom domain redirects",
        "deployment",
        "service",
        "ingress",
        "custom domain ingress",
        "custom domain http ingress",
        "custom domain https redirect",
        "custom domain host rewrite",
    ):
        try:
            _delete_agent_resource(name, resource)
        except ApiException as exc:
            if exc.status in (404, 410):
                continue
            msg = _short_api_error(exc)
            log.warning("delete agent %s %s failed: %s", name, resource, msg)
            failures.append(f"{resource}: {msg}")
    if failures:
        raise KubernetesDeleteError(failures)


def delete_legacy_knative_agent(name: str) -> None:
    """Remove old Knative-owned resources before migrating to Deployment."""
    failures: list[str] = []
    for resource in (
        "knative service",
        "domain mapping",
        "custom domain mappings",
        "legacy knative core service",
    ):
        try:
            _delete_agent_resource(name, resource)
        except ApiException as exc:
            if exc.status in (404, 410):
                continue
            msg = _short_api_error(exc)
            log.warning("delete agent %s %s failed: %s", name, resource, msg)
            failures.append(f"{resource}: {msg}")
    if failures:
        raise KubernetesDeleteError(failures)


def delete_legacy_deployment_agent(name: str) -> None:
    """Drop Deployment-era core resources after migrating an agent to Knative.

    Knative Serving creates and owns its own core ``Service`` named ``{name}``;
    a leftover non-Knative ``Service`` of that name would otherwise block the
    Knative route from coming up. Only the old ``Deployment``, the hand-rolled
    ``Ingress``, and that non-Knative core ``Service`` are removed — the target
    Knative ``Service`` is never touched. Best-effort: missing resources are
    fine; remaining failures are surfaced for the caller to log.
    """
    failures: list[str] = []
    for resource in ("deployment", "ingress", "legacy deployment core service"):
        try:
            _delete_agent_resource(name, resource)
        except ApiException as exc:
            if exc.status in (404, 410):
                continue
            msg = _short_api_error(exc)
            log.warning("migrate agent %s drop %s failed: %s", name, resource, msg)
            failures.append(f"{resource}: {msg}")
    if failures:
        raise KubernetesDeleteError(failures)


def _delete_non_knative_core_service(name: str, namespace: str) -> None:
    """Delete a leftover core ``Service`` only when Knative does not own it."""
    core = client.CoreV1Api()
    try:
        svc = core.read_namespaced_service(name, namespace)
    except ApiException as exc:
        if exc.status in (404, 410):
            return
        raise
    if not _is_knative_owned_service(svc):
        core.delete_namespaced_service(name, namespace)


def _delete_agent_resource(name: str, resource: str) -> None:
    for attempt in range(2):
        _load_kube()
        ns = settings.agents_namespace
        try:
            if resource == "knative service":
                client.CustomObjectsApi().delete_namespaced_custom_object(
                    KNATIVE_API_GROUP,
                    KNATIVE_API_VERSION,
                    ns,
                    KNATIVE_SERVICES_PLURAL,
                    name,
                )
            elif resource == "domain mapping":
                client.CustomObjectsApi().delete_namespaced_custom_object(
                    KNATIVE_API_GROUP,
                    KNATIVE_DOMAIN_MAPPING_API_VERSION,
                    ns,
                    KNATIVE_DOMAIN_MAPPINGS_PLURAL,
                    settings.ingress_host_template.format(name=name),
                )
            elif resource == "custom domain mappings":
                _delete_custom_domain_mappings(name)
            elif resource == "custom domain redirects":
                _sync_custom_domain_redirect_middlewares(name, [])
            elif resource == "deployment":
                client.AppsV1Api().delete_namespaced_deployment(name, ns)
            elif resource == "service":
                if name in PLATFORM_RESERVED_AGENT_NAMES:
                    log.warning("refusing to delete platform-owned Service %s/%s", ns, name)
                    return
                client.CoreV1Api().delete_namespaced_service(name, ns)
            elif resource == "legacy knative core service":
                _delete_legacy_knative_core_service(name, ns)
            elif resource == "legacy deployment core service":
                _delete_non_knative_core_service(name, ns)
            elif resource == "ingress":
                client.NetworkingV1Api().delete_namespaced_ingress(name, ns)
            elif resource == "custom domain ingress":
                client.NetworkingV1Api().delete_namespaced_ingress(
                    custom_domain_ingress_name(name),
                    ns,
                )
            elif resource == "custom domain http ingress":
                client.NetworkingV1Api().delete_namespaced_ingress(
                    custom_domain_http_ingress_name(name),
                    ns,
                )
            elif resource == "custom domain https redirect":
                _delete_custom_domain_https_redirect_middleware(name)
            elif resource == "custom domain host rewrite":
                _delete_custom_domain_host_rewrite_middleware(name)
            else:
                raise ValueError(f"unknown agent resource {resource!r}")
            return
        except ApiException as exc:
            if exc.status == 401 and attempt == 0:
                log.warning(
                    "delete agent %s %s got 401; reloading kube config and retrying",
                    name,
                    resource,
                )
                continue
            raise


def _delete_legacy_knative_core_service(name: str, namespace: str) -> None:
    core = client.CoreV1Api()
    svc = core.read_namespaced_service(name, namespace)
    if _is_knative_owned_service(svc):
        core.delete_namespaced_service(name, namespace)


def _is_knative_owned_service(svc: Any) -> bool:
    metadata = getattr(svc, "metadata", None)
    owner_refs = getattr(metadata, "owner_references", None) or []
    for ref in owner_refs:
        api_version = str(getattr(ref, "api_version", "") or "")
        kind = str(getattr(ref, "kind", "") or "")
        if api_version.startswith(f"{KNATIVE_API_GROUP}/") or kind in {"Route", "Revision"}:
            return True
    spec = getattr(svc, "spec", None)
    if str(getattr(spec, "type", "") or "") == "ExternalName":
        external_name = str(getattr(spec, "external_name", "") or "")
        return "knative" in external_name
    return False


def _delete_custom_domain_mappings(name: str) -> None:
    custom = client.CustomObjectsApi()
    ns = settings.agents_namespace
    args = (
        KNATIVE_API_GROUP,
        KNATIVE_DOMAIN_MAPPING_API_VERSION,
        ns,
        KNATIVE_DOMAIN_MAPPINGS_PLURAL,
    )
    existing = custom.list_namespaced_custom_object(
        *args,
        label_selector=f"app={name},a2a/custom-domain=true",
    )
    for item in existing.get("items", []) if isinstance(existing, dict) else []:
        metadata = item.get("metadata") if isinstance(item, dict) else {}
        mapping_name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(mapping_name, str):
            custom.delete_namespaced_custom_object(*args, mapping_name)


def _short_api_error(exc: ApiException) -> str:
    reason = getattr(exc, "reason", None) or "Kubernetes API error"
    body = getattr(exc, "body", None)
    detail = body if isinstance(body, str) and body.strip() else str(exc)
    detail = " ".join(detail.split())
    if detail:
        return f"{exc.status} {reason}: {detail[:240]}"
    return f"{exc.status} {reason}"


def agent_pod_logs(
    name: str,
    *,
    tail_lines: int = 200,
    include_previous: bool = True,
) -> dict[str, Any]:
    """Fetch the readiness/runtime log tail for an agent's pods.

    Returns ``{"ok": bool, "pods": [...], "error": str | None}`` where each pod
    entry carries the current container log tail and, on a restart/crashloop,
    the previous container's log tail (which is where the crash cause lives).
    Never raises: log fetching must not break deployment sync.
    """
    ns = settings.agents_namespace
    try:
        _load_kube()
        core = client.CoreV1Api()
        pods = core.list_namespaced_pod(ns, label_selector=f"app={name}")
    except ApiException as exc:
        return {"ok": False, "pods": [], "error": _short_api_error(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pods": [], "error": str(exc)[:240]}

    entries: list[dict[str, Any]] = []
    for pod in pods.items:
        pod_name = pod.metadata.name
        phase = getattr(pod.status, "phase", None)
        containers = pod.spec.containers or []
        restart_counts = {
            cs.name: cs.restart_count
            for cs in (pod.status.container_statuses or [])
        }
        for container in containers:
            cname = container.name
            restarts = restart_counts.get(cname, 0)
            current = _read_pod_log(core, ns, pod_name, cname, tail_lines, previous=False)
            previous = None
            if include_previous and restarts:
                previous = _read_pod_log(
                    core, ns, pod_name, cname, tail_lines, previous=True
                )
            entries.append(
                {
                    "pod": pod_name,
                    "container": cname,
                    "phase": phase,
                    "restarts": restarts,
                    "log": current,
                    "previous_log": previous,
                }
            )
    return {"ok": True, "pods": entries, "error": None}


def _read_pod_log(
    core: Any,
    ns: str,
    pod_name: str,
    container: str,
    tail_lines: int,
    *,
    previous: bool,
) -> str | None:
    try:
        return core.read_namespaced_pod_log(
            pod_name,
            ns,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
            timestamps=False,
        )
    except ApiException as exc:
        # 400 == no previous container yet; treat as simply unavailable.
        if exc.status == 400 and previous:
            return None
        return f"[log unavailable: {_short_api_error(exc)}]"
    except Exception as exc:  # noqa: BLE001
        return f"[log unavailable: {str(exc)[:120]}]"
