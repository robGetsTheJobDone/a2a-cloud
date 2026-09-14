from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel

from .agent import A2AAgent
from .auth import APIKeyAuth, JWTAuth, NoAuth, PlatformUserAuth
from .dsl import AgentDsl


def agent_openapi_spec(
    agent: A2AAgent,
    *,
    base_url: str | None = None,
    require_bearer_auth: bool | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI 3.1 document for direct skill invocation.

    The generated paths mirror the runtime ``POST /invoke/{skill}`` endpoint,
    but expose one concrete operation per skill so client generators can produce
    typed methods with each skill's exact argument and result schemas.
    """

    agent_cls = type(agent)
    return _openapi_spec(
        name=agent_cls.name,
        description=agent_cls.description,
        version=agent_cls.version,
        skills=tuple(agent.skills.values()),
        auth=_auth_extension(agent_cls, require_bearer_auth=require_bearer_auth),
        base_url=base_url,
    )


def agent_dsl_openapi_spec(
    dsl: AgentDsl,
    *,
    base_url: str | None = None,
    require_bearer_auth: bool | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI 3.1 document from a compiled Agent DSL."""

    return _openapi_spec(
        name=dsl.name,
        description=dsl.description,
        version=dsl.version,
        skills=tuple(dsl.skills),
        auth=_dsl_auth_extension(dsl, require_bearer_auth=require_bearer_auth),
        base_url=base_url,
    )


def _openapi_spec(
    *,
    name: str,
    description: str,
    version: str,
    skills: tuple[Any, ...],
    auth: dict[str, Any],
    base_url: str | None,
) -> dict[str, Any]:
    security = _security_requirement(auth)
    components: dict[str, Any] = {
        "schemas": {
            "AgentEvent": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string"},
                    "payload": {"type": "object", "additionalProperties": True},
                },
                "required": ["kind", "payload"],
            },
            "AgentArtifact": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "size_bytes": {"type": "integer", "minimum": 0},
                },
                "required": ["name", "size_bytes"],
            },
            "InvokeError": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "detail": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object", "additionalProperties": True},
                        ]
                    }
                },
            },
            "LLMCreds": _llm_creds_schema(),
            "CompositionBudget": {"type": "object", "additionalProperties": True},
        }
    }
    if security:
        components["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": _bearer_format(auth["strategy"]),
                "description": _bearer_description(auth["strategy"]),
            }
    }

    paths: dict[str, Any] = {}
    for skill in skills:
        operation_id = f"invoke{_pascal_case(skill.name)}"
        paths[f"/invoke/{skill.name}"] = {
            "post": {
                "operationId": operation_id,
                "summary": skill.description or f"Invoke {skill.name}",
                "description": _skill_description(skill),
                "tags": list(skill.tags) or ["skills"],
                "security": security,
                "x-a2a-skill": skill.name,
                "x-a2a-scopes": list(skill.scopes),
                "x-a2a-stream": skill.stream,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": _invoke_request_schema(skill),
                        }
                    },
                },
                "responses": _invoke_responses(skill),
            }
        }
        if skill.stream:
            paths[f"/invoke/{skill.name}"]["post"]["responses"]["200"]["content"][
                "text/event-stream"
            ] = {
                "schema": {
                    "type": "string",
                    "description": "Server-sent events. Send Accept: text/event-stream.",
                }
            }

    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": f"{name} Skills API",
            "description": description,
            "version": version,
        },
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "paths": paths,
        "components": components,
        "x-a2a-agent": {
            "name": name,
            "version": version,
            "description": description,
        },
        "x-a2a-auth": auth,
    }
    if base_url:
        spec["servers"] = [{"url": base_url.rstrip("/")}]
    return spec


def _invoke_request_schema(skill: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "arguments": _inline_local_defs(skill.input_schema),
            "grant": {
                "type": ["string", "null"],
                "description": "Optional A2A workspace/sandbox grant token.",
            },
            "llm_creds": {
                "anyOf": [{"$ref": "#/components/schemas/LLMCreds"}, {"type": "null"}],
                "description": "Optional caller-provided LLM credentials.",
            },
            "composition": {
                "anyOf": [
                    {"$ref": "#/components/schemas/CompositionBudget"},
                    {"type": "null"},
                ]
            },
            "consumer_config": {
                "type": ["object", "null"],
                "additionalProperties": True,
            },
            "consumer_secrets": {
                "type": ["object", "null"],
                "additionalProperties": {"type": "string"},
            },
            "cp_jwt": {
                "type": ["string", "null"],
                "description": "Optional control-plane JWT forwarded by trusted orchestrators.",
            },
            "cp_url": {
                "type": ["string", "null"],
                "description": "Optional control-plane base URL.",
            },
        },
        "required": ["arguments"],
    }


def _inline_local_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic-local ``$defs`` refs for OpenAPI client generators.

    Pydantic may emit schemas shaped like ``{"$defs": {...}, "items":
    {"$ref": "#/$defs/Model"}}`` for nested models. That is valid as a
    standalone JSON Schema fragment, but once embedded under an OpenAPI request
    body the root ``#/$defs`` target no longer exists. Inline those local refs
    before exposing the schema to OpenAPI tooling.
    """

    return _inline_local_defs_value(copy.deepcopy(schema), ())


def _inline_local_defs_value(
    value: Any,
    defs_stack: tuple[dict[str, Any], ...],
) -> Any:
    if isinstance(value, list):
        return [_inline_local_defs_value(item, defs_stack) for item in value]
    if not isinstance(value, dict):
        return value

    local_defs = value.get("$defs")
    if isinstance(local_defs, dict):
        defs_stack = (local_defs, *defs_stack)

    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = _json_pointer_token(ref.removeprefix("#/$defs/"))
        for defs in defs_stack:
            if name in defs:
                resolved = _inline_local_defs_value(copy.deepcopy(defs[name]), defs_stack)
                siblings = {
                    key: item
                    for key, item in value.items()
                    if key not in {"$ref", "$defs"}
                }
                if not siblings:
                    return resolved
                if isinstance(resolved, dict):
                    merged = dict(resolved)
                    merged.update(
                        {
                            key: _inline_local_defs_value(item, defs_stack)
                            for key, item in siblings.items()
                        }
                    )
                    return merged
                return _inline_local_defs_value(siblings, defs_stack)

    return {
        key: _inline_local_defs_value(item, defs_stack)
        for key, item in value.items()
        if key != "$defs"
    }


def _json_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _invoke_responses(skill: Any) -> dict[str, Any]:
    return {
        "200": {
            "description": "Skill invocation completed.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "result": _inline_local_defs(skill.output_schema),
                            "events": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/AgentEvent"},
                            },
                            "artifacts": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/AgentArtifact"},
                            },
                            "grant_id": {"type": ["string", "null"]},
                        },
                        "required": ["result", "events", "artifacts", "grant_id"],
                    }
                }
            },
        },
        "400": {
            "description": "Invalid skill arguments.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeError"}}},
        },
        "401": {
            "description": "Missing or invalid authentication.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeError"}}},
        },
        "403": {
            "description": "Authentication succeeded but required scope or grant is missing.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeError"}}},
        },
        "404": {
            "description": "Unknown skill.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeError"}}},
        },
        "500": {
            "description": "Skill handler raised.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeError"}}},
        },
    }


def _auth_extension(
    agent_cls: type[A2AAgent],
    *,
    require_bearer_auth: bool | None,
) -> dict[str, Any]:
    auth_model: type[BaseModel] = agent_cls.auth_model
    resolver = getattr(agent_cls, "auth_resolver", None)
    if auth_model is NoAuth:
        strategy = "public"
        required = False
    elif auth_model is APIKeyAuth:
        strategy = "api_key"
        required = True
    elif auth_model is PlatformUserAuth:
        strategy = "platform_user"
        required = True
    elif auth_model is JWTAuth:
        strategy = "jwt"
        required = True
    else:
        strategy = "custom"
        required = True
    effective_required = required if require_bearer_auth is None else require_bearer_auth
    return {
        "required": effective_required,
        "principal_required": required,
        "transport_bearer_required": effective_required,
        "strategy": strategy,
        "model": f"{auth_model.__module__}.{auth_model.__qualname__}",
        "principal_schema": auth_model.model_json_schema(),
        "resolver": _resolver_ref(resolver),
    }


def _dsl_auth_extension(
    dsl: AgentDsl,
    *,
    require_bearer_auth: bool | None,
) -> dict[str, Any]:
    required = bool(dsl.auth.required)
    effective_required = required if require_bearer_auth is None else require_bearer_auth
    return {
        "required": effective_required,
        "principal_required": required,
        "transport_bearer_required": effective_required,
        "strategy": dsl.auth.strategy,
        "model": dsl.auth.model,
        "principal_schema": dsl.auth.principal_schema,
        "resolver": dsl.auth.resolver,
    }


def _security_requirement(auth: dict[str, Any]) -> list[dict[str, list[str]]]:
    return [{"bearerAuth": []}] if auth["required"] else []


def _resolver_ref(resolver: Any) -> str | None:
    if resolver is None:
        return None
    cls = type(resolver)
    return f"{cls.__module__}.{cls.__qualname__}"


def _bearer_format(strategy: str) -> str:
    if strategy == "api_key":
        return "A2A API key"
    if strategy == "platform_user":
        return "A2A platform session token"
    if strategy == "jwt":
        return "JWT"
    return "Bearer token"


def _bearer_description(strategy: str) -> str:
    if strategy == "api_key":
        return "Pass the configured A2A API key as `Authorization: Bearer <token>`."
    if strategy == "platform_user":
        return "Pass an A2A platform session token as `Authorization: Bearer <token>`."
    if strategy == "jwt":
        return "Pass a JWT as `Authorization: Bearer <token>`."
    return "Pass a bearer token accepted by the agent auth resolver."


def _skill_description(skill: Any) -> str:
    lines = [skill.description or f"Invoke `{skill.name}`."]
    if skill.scopes:
        lines.append("")
        lines.append("Required scopes: " + ", ".join(f"`{scope}`" for scope in skill.scopes))
    if skill.stream:
        lines.append("")
        lines.append("This skill can stream progress with `Accept: text/event-stream`.")
    return "\n".join(lines)


def _llm_creds_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "base_url": {"type": "string"},
            "api_key": {"type": "string"},
            "model": {"type": "string"},
            "temperature_mode": {"type": "string", "default": "default"},
            "temperature": {"type": ["number", "null"]},
            "extra_body": {"type": ["object", "null"], "additionalProperties": True},
            "metadata": {"type": ["object", "null"], "additionalProperties": True},
        },
        "required": ["base_url", "api_key", "model"],
    }


def _pascal_case(value: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", value)
    clean = "".join(part[:1].upper() + part[1:] for part in parts if part)
    return clean or "Skill"
