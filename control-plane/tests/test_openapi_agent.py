from __future__ import annotations

import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.openapi_agent import (
    GeneratedOpenAPIAgent,
    _collect_operations,
    build_openapi_agent_source,
    fetch_openapi_spec,
    source_tarball_bytes,
)
from control_plane.models import Agent, User
from control_plane.routes import agents
from control_plane.schemas import AgentOpenAPIGenerateIn


PETSTORE_MINIMAL = {
    "openapi": "3.0.4",
    "info": {
        "title": "Swagger Petstore",
        "version": "1.0.0",
        "description": "Petstore fixture.",
    },
    "servers": [{"url": "https://petstore3.swagger.io/api/v3"}],
    "components": {
        "securitySchemes": {
            "api_key": {
                "type": "apiKey",
                "name": "api_key",
                "in": "header",
            },
            "petstore_auth": {
                "type": "oauth2",
                "flows": {
                    "implicit": {
                        "authorizationUrl": "https://petstore3.swagger.io/oauth/authorize",
                        "scopes": {"write:pets": "modify pets"},
                    }
                },
            },
        },
        "parameters": {
            "PetId": {
                "name": "petId",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        },
    },
    "security": [{"api_key": []}],
    "paths": {
        "/pet/findByStatus": {
            "get": {
                "operationId": "findPetsByStatus",
                "summary": "Finds Pets by status",
                "tags": ["pet"],
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
        "/pet": {
            "post": {
                "operationId": "addPet",
                "summary": "Add a new pet",
                "tags": ["pet"],
                "security": [{"petstore_auth": ["write:pets"]}],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                        }
                    }
                },
            }
        },
        "/pet/{petId}": {
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete a pet",
                "tags": ["pet"],
                "parameters": [{"$ref": "#/components/parameters/PetId"}],
                "security": [{"api_key": []}],
            }
        },
    },
}


SWAGGER2_MINIMAL = {
    "swagger": "2.0",
    "info": {
        "title": "Swagger 2 Fixture",
        "version": "2024-01-01",
        "description": "Swagger 2 fixture.",
    },
    "host": "api.example.test",
    "basePath": "/v2",
    "schemes": ["https"],
    "securityDefinitions": {
        "api_key": {
            "type": "apiKey",
            "name": "X-API-Key",
            "in": "header",
        },
    },
    "security": [{"api_key": []}],
    "paths": {
        "/widgets/{widgetId}": {
            "put": {
                "operationId": "updateWidget",
                "summary": "Update a widget",
                "parameters": [
                    {
                        "name": "widgetId",
                        "in": "path",
                        "required": True,
                        "type": "integer",
                    },
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        },
                    },
                ],
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


GODADDY_DOMAINS_MINIMAL = {
    "swagger": "2.0",
    "info": {
        "title": "Domains API",
        "description": "The Domains API is for domain-related actions.",
    },
    "host": "api.ote-godaddy.com",
    "schemes": ["https"],
    "paths": {
        "/v1/domains/available": {
            "get": {
                "operationId": "available",
                "summary": "Determine whether or not the specified domain is available",
                "parameters": [
                    {
                        "name": "domain",
                        "in": "query",
                        "required": True,
                        "type": "string",
                    }
                ],
                "responses": {
                    "200": {"description": "ok"},
                    "401": {"description": "Authentication info not sent or invalid"},
                },
            }
        }
    },
}


BLOG_AUTH_ALTERNATIVES_MINIMAL = {
    "openapi": "3.1.0",
    "info": {"title": "a2a cloud blog API", "version": "0.1.0"},
    "servers": [{"url": "https://blog.a2acloud.io"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Use `Authorization: Bearer $BLOG_API_KEY`.",
            },
            "apiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "x-api-key",
                "description": "Use the configured `BLOG_API_KEY` value.",
            },
        }
    },
    "paths": {
        "/api/posts": {
            "get": {
                "operationId": "listBlogPosts",
                "summary": "List posts",
                "security": [{}, {"bearerAuth": []}, {"apiKeyAuth": []}],
            },
            "post": {
                "operationId": "createBlogPost",
                "summary": "Create a blog post",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "requestBody": {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                },
            },
        }
    },
}


DISTINCT_AUTH_ALTERNATIVES_MINIMAL = {
    "openapi": "3.1.0",
    "info": {"title": "Alternative Auth API", "version": "0.1.0"},
    "servers": [{"url": "https://api.example.test"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "x-api-key"},
        }
    },
    "paths": {
        "/items": {
            "post": {
                "operationId": "createItem",
                "summary": "Create an item",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
            }
        }
    },
}


OPENPANEL_ADMIN_MINIMAL = {
    "openapi": "3.1.0",
    "info": {"title": "OpenPanel Admin", "version": "1.0.0"},
    "servers": [{"url": "https://analytics.a2acloud.io/api"}],
    "paths": {
        "/manage/projects": {
            "get": {
                "operationId": "listProjects",
                "summary": "List projects",
            }
        }
    },
}


OPENPANEL_PROJECT_MINIMAL = {
    "openapi": "3.1.0",
    "info": {"title": "OpenPanel Project", "version": "1.0.0"},
    "servers": [{"url": "https://analytics.a2acloud.io/api"}],
    "paths": {
        "/events": {
            "post": {
                "operationId": "createEvent",
                "summary": "Create event",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                        }
                    }
                },
            }
        }
    },
}


APIFY_OPENAPI_URL = "https://docs.apify.com/api/openapi.json"


def _many_route_operations_spec(count: int = 81) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Large Routes API", "version": "1.0.0"},
        "servers": [{"url": "https://large.example.test"}],
        "paths": {
            f"/v1/widgets/{index}": {
                "get": {
                    "operationId": f"getWidget{index}",
                    "summary": f"Get widget {index}",
                    "tags": ["widgets"],
                }
            }
            for index in range(count)
        },
    }


def test_build_openapi_agent_source_generates_editable_a2apack_project(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(
        PETSTORE_MINIMAL,
        name="petstore-auto",
        spec_url="https://petstore3.swagger.io/api/v3/openapi.json",
    )

    assert generated.name == "petstore-auto"
    assert set(generated.files) == {
        "README.md",
        "agent.py",
        "a2a.yaml",
        "requirements.txt",
        "openapi.json",
    }
    assert generated.preview["operation_count"] == 3
    assert generated.preview["source_openapi_url"] == "https://petstore3.swagger.io/api/v3/openapi.json"
    assert generated.preview["regenerable"] is True
    assert "auto" in generated.preview["skills"]
    assert "findPetsByStatus" not in generated.files["agent.py"]
    assert "find_pets_by_status" in generated.files["agent.py"]
    assert "ConsumerSetupField.secret" in generated.files["agent.py"]
    assert "OPENAPI_BASE_URL" in generated.files["agent.py"]
    assert "llm_provisioning = LLMProvisioning.PLATFORM" in generated.files["agent.py"]
    assert "llm_credentials_missing" in generated.files["agent.py"]
    assert "source_openapi_url" in generated.files["agent.py"]
    assert "regenerable" in generated.files["agent.py"]
    assert "https://petstore3.swagger.io/api/v3/openapi.json" in generated.files["agent.py"]
    assert "may need to be refreshed from the latest OpenAPI spec" in generated.files["agent.py"]
    assert "prefer explicit health/status/ping GET operations" in generated.files["agent.py"]
    assert "_select_smoke_operation" in generated.files["agent.py"]
    assert "stale_openapi_hint" in generated.files["agent.py"]
    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)
    assert namespace["agent"].name == "petstore-auto"
    auto_skill = next(
        skill
        for skill in namespace["agent"].card().model_dump(mode="json")["skills"]
        if skill["name"] == "auto"
    )
    assert auto_skill["policy"]["timeout_seconds"] == 900
    delete_params = namespace["OPERATIONS"]["delete_pet"]["parameters"]
    assert delete_params == [
        {
            "name": "petId",
            "in": "path",
            "required": True,
            "description": "",
            "schema": {"type": "string"},
        }
    ]
    setup_fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    # Disjoint per-operation schemes: neither credential is needed by every
    # secured operation, so neither may gate the whole agent (required =
    # intersection, not union). Each optional field names its skills instead.
    assert setup_fields["API_KEY"]["required"] is False
    assert "Used by:" in setup_fields["API_KEY"]["description"]
    assert setup_fields["PETSTORE_AUTH"]["required"] is False
    assert "Used by:" in setup_fields["PETSTORE_AUTH"]["description"]
    assert 'path = path.replace("{" + name + "}", str(value))' in generated.files["agent.py"]
    assert generated.preview["routing_mode"] == "direct_operation_skills"
    assert generated.preview["route_groups"] == []


def test_build_openapi_agent_source_uses_route_subagents_for_large_specs(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(
        _many_route_operations_spec(81),
        name="large-routes",
    )

    assert generated.preview["operation_count"] == 81
    assert generated.preview["routing_mode"] == "deepagents_subagents"
    assert generated.preview["direct_operation_skill_limit"] == 80
    assert generated.preview["skills"] == ["auto"]
    assert generated.preview["deepagent_skills"] == ["widgets-1-routes", "widgets-2-routes"]
    assert [group["operation_count"] for group in generated.preview["route_groups"]] == [80, 1]
    assert "skills/widgets-1/SKILL.md" in generated.files
    assert "skills/widgets-2/SKILL.md" in generated.files
    assert "get_widget0" in generated.files["skills/widgets-1/SKILL.md"]
    assert "get_widget80" in generated.files["skills/widgets-2/SKILL.md"]
    assert "async def get_widget0" not in generated.files["agent.py"]
    assert "subagents=self._operation_subagents(ctx, skills_root)" in generated.files["agent.py"]
    assert "prefer explicit health/status/ping route groups" in generated.files["agent.py"]
    assert "smoke_result = await self._maybe_smoke_test" in generated.files["agent.py"]

    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    card = namespace["agent"].card().model_dump(mode="json")
    assert [skill["name"] for skill in card["skills"]] == ["auto"]
    assert len(namespace["OPERATIONS"]) == 81
    covered = {
        operation_id
        for group in namespace["OPERATION_GROUPS"]
        for operation_id in group["operation_ids"]
    }
    assert covered == set(namespace["OPERATIONS"])


def test_build_openapi_agent_source_caps_provider_tool_names(monkeypatch) -> None:
    long_prefix = "readAssignmentTaskSubmissionsForDeeplyNestedCampusCourse"
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Long Tool API", "version": "1.0.0"},
        "servers": [{"url": "https://long.example.test"}],
        "paths": {
            "/v1/assignments/{assignment_uuid}/tasks/{task_uuid}/submissions/me": {
                "get": {
                    "operationId": f"{long_prefix}CurrentUserWithVerboseCompatibilitySuffix",
                    "summary": "Read my submission",
                }
            },
            "/v1/assignments/{assignment_uuid}/tasks/{task_uuid}/submissions/user/{user_id}": {
                "get": {
                    "operationId": f"{long_prefix}SpecificUserWithVerboseCompatibilitySuffix",
                    "summary": "Read user submission",
                }
            },
        },
    }

    generated = build_openapi_agent_source(spec, name="long-tool-api")
    operations = generated.preview["operations"]
    tool_names = [operation["skill_name"] for operation in operations]

    assert all(len(name) <= 64 for name in tool_names)
    assert len(set(tool_names)) == len(tool_names)
    assert generated.preview["skills"] == ["auto", *tool_names]
    assert "Operation ID:" in generated.files["agent.py"]

    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    runtime_tool_names = [
        tool.name for tool in namespace["agent"]._operation_tools(object())
    ]
    assert runtime_tool_names == tool_names
    assert all(len(name) <= 64 for name in runtime_tool_names)


def test_build_openapi_agent_source_supports_swagger2_security_and_server(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(SWAGGER2_MINIMAL, name="swagger2-auto")

    assert generated.preview["server_url"] == "https://api.example.test/v2"
    assert generated.preview["security_schemes"] == [
        {
            "name": "api_key",
            "type": "apiKey",
            "location": "header",
            "parameter_name": "X-API-Key",
            "scheme": None,
            "description": "",
        }
    ]
    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    assert namespace["DEFAULT_BASE_URL"] == "https://api.example.test/v2"
    operation = namespace["OPERATIONS"]["update_widget"]
    assert operation["parameters"] == [
        {
            "name": "widgetId",
            "in": "path",
            "required": True,
            "description": "",
            "schema": {"type": "integer"},
        }
    ]
    assert operation["request_body"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert namespace["SECURITY_FIELDS"]["api_key"] == {
        "kind": "apiKey",
        "field": "X_API_KEY",
        "location": "header",
        "name": "X-API-Key",
    }


def test_build_openapi_agent_source_infers_godaddy_sso_key_secret(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(GODADDY_DOMAINS_MINIMAL, name="go-daddy")

    assert generated.preview["server_url"] == "https://api.ote-godaddy.com"
    assert generated.preview["warnings"] == [
        "GoDaddy's Swagger document does not declare authentication; inferred "
        "required Authorization: sso-key consumer setup."
    ]
    assert generated.preview["operations"][0]["requires_auth"] is True
    assert "GODADDY_API_KEY_SECRET" in generated.files["agent.py"]
    assert 'sent_value = f"{prefix}{value}"' in generated.files["agent.py"]
    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    assert namespace["ROOT_SECURITY"] == [{"godaddy_sso_key": []}]
    assert namespace["SECURITY_FIELDS"]["godaddy_sso_key"] == {
        "kind": "apiKey",
        "field": "GODADDY_API_KEY_SECRET",
        "location": "header",
        "name": "Authorization",
        "prefix": "sso-key ",
    }
    headers, query = namespace["agent"]._auth_for_operation(
        _SecretCtx({"GODADDY_API_KEY_SECRET": "key:secret"}),
        namespace["OPERATIONS"]["available"],
    )
    assert headers == {"Authorization": "sso-key key:secret"}
    assert query == {}
    setup_fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    assert setup_fields["GODADDY_API_KEY_SECRET"]["required"] is True
    assert setup_fields["GODADDY_API_KEY_SECRET"]["label"] == "GoDaddy API key and secret"


def test_build_openapi_agent_source_reuses_shared_auth_field_from_descriptions(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(
        BLOG_AUTH_ALTERNATIVES_MINIMAL,
        name="blog-openapi-agent",
    )

    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    setup_fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    assert set(setup_fields) == {"OPENAPI_BASE_URL", "BLOG_API_KEY"}
    assert setup_fields["BLOG_API_KEY"]["required"] is True
    assert namespace["SECURITY_FIELDS"]["bearerAuth"]["field"] == "BLOG_API_KEY"
    assert namespace["SECURITY_FIELDS"]["apiKeyAuth"]["field"] == "BLOG_API_KEY"
    assert generated.preview["operations"][0]["requires_auth"] is False
    assert generated.preview["operations"][1]["requires_auth"] is True

    headers, query = namespace["agent"]._auth_for_operation(
        _SecretCtx({"BLOG_API_KEY": "blog-secret"}),
        namespace["OPERATIONS"]["create_blog_post"],
    )
    assert headers == {"authorization": "bearer blog-secret"}
    assert query == {}


def test_build_openapi_agent_source_does_not_require_distinct_or_auth_schemes(
    monkeypatch,
) -> None:
    generated = build_openapi_agent_source(
        DISTINCT_AUTH_ALTERNATIVES_MINIMAL,
        name="alternative-auth",
    )

    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    setup_fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    assert setup_fields["BEARERAUTH_TOKEN"]["required"] is False
    assert setup_fields["X_API_KEY"]["required"] is False


def test_build_openapi_agent_source_namespaces_composite_auth_by_source_url(
    monkeypatch,
) -> None:
    admin_url = "https://analytics.a2acloud.io/api/documentation/admin.json"
    project_url = "https://analytics.a2acloud.io/api/documentation/project.json"
    generated = build_openapi_agent_source(
        [OPENPANEL_ADMIN_MINIMAL, OPENPANEL_PROJECT_MINIMAL],
        name="openpanel",
        spec_urls=[admin_url, project_url],
    )

    assert generated.preview["composite"] is True
    assert generated.preview["source_openapi_url"] is None
    assert generated.preview["source_openapi_urls"] == [admin_url, project_url]
    assert generated.preview["server_urls"] == ["https://analytics.a2acloud.io/api"]
    assert generated.preview["operation_count"] == 2
    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)

    setup_fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    client_id = "OPENPANEL_CLIENT_ID"
    client_secret = "OPENPANEL_CLIENT_SECRET"
    assert setup_fields[client_id]["required"] is True
    assert setup_fields[client_secret]["required"] is True
    assert setup_fields[client_id]["label"] == "OpenPanel client ID"
    assert setup_fields[client_secret]["label"] == "OpenPanel client secret"
    for field_name in (client_id, client_secret):
        assert admin_url in setup_fields[field_name]["description"]
        assert project_url in setup_fields[field_name]["description"]

    admin_headers, admin_query = namespace["agent"]._auth_for_operation(
        _SecretCtx({client_id: "client-id", client_secret: "client-secret"}),
        namespace["OPERATIONS"]["list_projects"],
    )
    assert admin_headers == {
        "openpanel-client-id": "client-id",
        "openpanel-client-secret": "client-secret",
    }
    assert admin_query == {}

    project_headers, project_query = namespace["agent"]._auth_for_operation(
        _SecretCtx({client_id: "client-id", client_secret: "client-secret"}),
        namespace["OPERATIONS"]["create_event"],
    )
    assert project_headers == {
        "openpanel-client-id": "client-id",
        "openpanel-client-secret": "client-secret",
    }
    assert project_query == {}


@pytest.mark.asyncio
async def test_build_openapi_agent_source_supports_live_apify_spec(monkeypatch) -> None:
    try:
        spec = await fetch_openapi_spec(APIFY_OPENAPI_URL)
    except Exception as exc:  # pragma: no cover - external availability guard
        pytest.skip(f"live Apify OpenAPI spec unavailable: {exc}")

    all_operations, _ = _collect_operations(spec, max_operations=10_000)
    assert len(all_operations) > 80

    generated = build_openapi_agent_source(
        spec,
        name="apify-openapi",
        spec_url=APIFY_OPENAPI_URL,
    )

    assert generated.preview["operation_count"] == len(all_operations)
    assert generated.preview["source_openapi_url"] == APIFY_OPENAPI_URL
    assert not any(
        warning.startswith("Only the first")
        for warning in generated.preview["warnings"]
    )
    compile(generated.files["agent.py"], "agent.py", "exec")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)
    assert len(namespace["OPERATIONS"]) == len(all_operations)


class _SecretCtx:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def consumer_secret(self, name: str) -> str:
        return self._secrets[name]


def test_source_tarball_bytes_contains_generated_project(tmp_path) -> None:
    generated = build_openapi_agent_source(PETSTORE_MINIMAL, name="petstore-auto")
    bundle = source_tarball_bytes(generated.files)
    path = tmp_path / "source.tar.gz"
    path.write_bytes(bundle)

    with tarfile.open(path, "r:gz") as tar:
        assert sorted(tar.getnames()) == [
            "README.md",
            "a2a.yaml",
            "agent.py",
            "openapi.json",
            "requirements.txt",
        ]


class _RequestCtx:
    def __init__(self, *, configs: dict[str, str] | None = None, secrets: dict[str, str] | None = None) -> None:
        self._configs = configs or {}
        self._secrets = secrets or {}

    def consumer_config(self, name: str, default=None):
        return self._configs.get(name, default)

    def consumer_secret(self, name: str) -> str:
        return self._secrets[name]


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, captured: dict, *a, **kw) -> None:
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method: str, url: str, **kw):
        self._captured["method"] = method
        self._captured["url"] = url
        self._captured["kwargs"] = kw
        return _FakeResponse()


@pytest.mark.asyncio
async def test_request_coerces_json_string_body(monkeypatch) -> None:
    """Regression: openapi-auto-agent must coerce a JSON-string body into a dict
    before passing to httpx, otherwise upstream FastAPI rejects the request
    with `Input should be a valid dictionary or object to extract fields from`.
    Triggered for write operations (PUT/PATCH/POST) when the MCP/JSON-RPC
    transport serializes the body argument."""
    generated = build_openapi_agent_source(SWAGGER2_MINIMAL, name="swagger2-auto")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)
    agent = namespace["agent"]

    captured: dict = {}
    namespace["httpx"].AsyncClient = lambda *a, **kw: _FakeAsyncClient(captured, *a, **kw)

    ctx = _RequestCtx(secrets={"X_API_KEY": "k"})
    body_as_string = '{"name": "Widget Two"}'
    result = await agent._request(
        ctx,
        "update_widget",
        parameters={"widgetId": 7},
        body=body_as_string,
    )

    assert result["ok"] is True
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/widgets/7")
    assert captured["kwargs"]["json"] == {"name": "Widget Two"}


@pytest.mark.asyncio
async def test_request_preserves_dict_body(monkeypatch) -> None:
    generated = build_openapi_agent_source(SWAGGER2_MINIMAL, name="swagger2-auto")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "a2a"))
    namespace: dict[str, object] = {}
    exec(generated.files["agent.py"], namespace)
    agent = namespace["agent"]

    captured: dict = {}
    namespace["httpx"].AsyncClient = lambda *a, **kw: _FakeAsyncClient(captured, *a, **kw)

    ctx = _RequestCtx(secrets={"X_API_KEY": "k"})
    await agent._request(
        ctx,
        "update_widget",
        parameters={"widgetId": 1},
        body={"name": "asis"},
    )

    assert captured["kwargs"]["json"] == {"name": "asis"}


async def test_preview_openapi_agent_route_uses_generator(monkeypatch) -> None:
    async def fake_fetch(url: str):
        assert url == "https://petstore.example/openapi.json"
        return PETSTORE_MINIMAL

    monkeypatch.setattr(agents, "fetch_openapi_spec", fake_fetch)

    out = await agents.preview_openapi_agent(
        AgentOpenAPIGenerateIn(
            url="https://petstore.example/openapi.json",
            name="petstore-auto",
            public=True,
        ),
        _user=User(id=1, email="dev@example.com", password_hash="x"),
    )

    assert out.name == "petstore-auto"
    assert out.operation_count == 3
    assert out.consumer_setup["fields"][0]["name"] == "OPENAPI_BASE_URL"
    assert {
        field["name"]: field["required"]
        for field in out.consumer_setup["fields"]
        if field["kind"] == "secret"
    } == {"API_KEY": False, "PETSTORE_AUTH": False}  # disjoint schemes: intersection semantics


async def test_preview_openapi_agent_route_accepts_multiple_urls(monkeypatch) -> None:
    admin_url = "https://analytics.a2acloud.io/api/documentation/admin.json"
    project_url = "https://analytics.a2acloud.io/api/documentation/project.json"
    fetched: list[str] = []

    async def fake_fetch(url: str):
        fetched.append(url)
        if url == admin_url:
            return OPENPANEL_ADMIN_MINIMAL
        if url == project_url:
            return OPENPANEL_PROJECT_MINIMAL
        raise AssertionError(url)

    monkeypatch.setattr(agents, "fetch_openapi_spec", fake_fetch)

    out = await agents.preview_openapi_agent(
        AgentOpenAPIGenerateIn(
            urls=[admin_url, project_url],
            name="openpanel",
            public=True,
        ),
        _user=User(id=1, email="dev@example.com", password_hash="x"),
    )

    assert fetched == [admin_url, project_url]
    assert out.name == "openpanel"
    assert out.composite is True
    assert out.source_openapi_url is None
    assert out.source_openapi_urls == [admin_url, project_url]
    assert out.operation_count == 2


async def test_from_openapi_existing_agent_requires_refresh_confirmation(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            session.add(
                Agent(
                    owner_id=user.id,
                    name="petstore-auto",
                    description="Existing generated agent",
                    version="0.0.1",
                    image="registry.example/petstore-auto:old",
                    public=True,
                    status="running",
                    url="https://petstore-auto.example.test",
                    card={},
                )
            )
            await session.commit()

            async def fake_generate(
                body: AgentOpenAPIGenerateIn,
            ) -> GeneratedOpenAPIAgent:
                assert body.url == "https://petstore.example/openapi.json"
                return GeneratedOpenAPIAgent(
                    name="petstore-auto",
                    class_name="PetstoreAuto",
                    description="Fresh generated agent",
                    version="1.0.0",
                    files={"agent.py": "# generated\n"},
                    preview={
                        "name": "petstore-auto",
                        "description": "Fresh generated agent",
                        "version": "1.0.0",
                        "server_url": "https://petstore.example",
                        "operation_count": 3,
                        "operations": [],
                        "consumer_setup": {"fields": []},
                        "security_schemes": [],
                        "skills": ["auto"],
                        "source_files": ["agent.py"],
                        "warnings": [],
                    },
                    server_url="https://petstore.example",
                )

            monkeypatch.setattr(agents, "_generate_openapi_agent_source", fake_generate)

            with pytest.raises(HTTPException) as exc_info:
                await agents.from_openapi(
                    AgentOpenAPIGenerateIn(
                        url="https://petstore.example/openapi.json",
                        name="petstore-auto",
                        public=True,
                    ),
                    user=user,
                    session=session,
                )

            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["error"] == "openapi_refresh_required"
            assert exc_info.value.detail["agent"] == "petstore-auto"
            assert "Confirm refresh" in exc_info.value.detail["message"]
    finally:
        await engine.dispose()


async def test_from_openapi_truncates_stored_agent_description(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.flush()

            long_description = "Long OpenAPI description. " * 200

            async def fake_generate(
                body: AgentOpenAPIGenerateIn,
            ) -> GeneratedOpenAPIAgent:
                return GeneratedOpenAPIAgent(
                    name="long-openapi-agent",
                    class_name="LongOpenAPIAgent",
                    description=long_description,
                    version="1.0.0",
                    files={"agent.py": "# generated\n"},
                    preview={
                        "name": "long-openapi-agent",
                        "description": long_description,
                        "version": "1.0.0",
                        "server_url": "https://api.example.test",
                        "server_urls": ["https://api.example.test"],
                        "operation_count": 1,
                        "operations": [],
                        "consumer_setup": {"fields": []},
                        "security_schemes": [],
                        "skills": ["auto"],
                        "source_files": ["agent.py"],
                        "source_openapi_url": body.url,
                        "source_openapi_urls": [body.url] if body.url else [],
                        "regenerable": True,
                        "composite": False,
                        "warnings": [],
                    },
                    server_url="https://api.example.test",
                    source_openapi_urls=[body.url] if body.url else [],
                    server_urls=["https://api.example.test"],
                )

            monkeypatch.setattr(agents, "_generate_openapi_agent_source", fake_generate)
            monkeypatch.setattr(agents, "_resolve_source_repo_scope", _async_return((None, "owner-1")))
            monkeypatch.setattr(agents, "ensure_repo", lambda *a, **kw: ("push-url", "internal-url"))
            monkeypatch.setattr(agents, "_ensure_source_repo_push_webhook", lambda *a, **kw: None)
            monkeypatch.setattr(agents, "commit_and_push_source", lambda **kw: "source-sha")
            monkeypatch.setattr(agents, "_ensure_runtime_repo", lambda *a, **kw: ("runtime-push", "runtime-url"))
            monkeypatch.setattr(agents, "commit_and_push_runtime", lambda **kw: "runtime-sha")
            monkeypatch.setattr(agents, "_lookup_source_agent", _async_return(None))
            monkeypatch.setattr(agents, "_index_agents_for_search", _async_return(None))
            invalidated_cards: list[str] = []

            async def fake_invalidate_agent_card(name: str) -> None:
                invalidated_cards.append(name)

            monkeypatch.setattr(agents, "invalidate_agent_card", fake_invalidate_agent_card)
            monkeypatch.setattr(
                agents,
                "create_deployment",
                _async_return(SimpleNamespace(deploy_id="dep-1")),
            )
            monkeypatch.setattr(agents, "record_deployment_event", _async_return(None))

            out = await agents.from_openapi(
                AgentOpenAPIGenerateIn(
                    url="https://example.test/openapi.json",
                    name="long-openapi-agent",
                    public=True,
                ),
                user=user,
                session=session,
            )

            row = (
                await session.execute(
                    agents.select(Agent).where(Agent.name == "long-openapi-agent")
                )
            ).scalar_one()
            assert out.preview.description == long_description
            assert len(row.description) <= agents.AGENT_DESCRIPTION_MAX
            assert row.description.endswith("...")
            assert invalidated_cards == ["long-openapi-agent"]
    finally:
        await engine.dispose()


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def test_collect_operations_inlines_request_body_refs():
    """Cards carry no components section, so $ref in a request body must be
    inlined or consumers (typed CLIs, tool defs) see a dangling pointer."""
    from control_plane.openapi_agent import _collect_operations, _operation_input_schema

    spec = {
        "openapi": "3.0.0",
        "paths": {"/api/posts": {"post": {
            "operationId": "create_blog_post",
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/BlogPostInput"}}}},
        }}},
        "components": {"schemas": {
            "BlogPostInput": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"$ref": "#/components/schemas/Tag"}},
                },
                "required": ["title"],
            },
            "Tag": {"type": "string"},
        }},
    }
    ops, _ = _collect_operations(spec, max_operations=10)
    body = ops[0]["request_body"]
    assert "$ref" not in repr(body)
    assert body["properties"]["title"]["type"] == "string"
    assert body["properties"]["tags"]["items"]["type"] == "string"
    published = _operation_input_schema(ops[0])
    assert published["properties"]["body"]["properties"]["title"]["type"] == "string"


def test_partial_use_security_scheme_is_optional_not_required():
    """A scheme only some operations need (session cookie on comment endpoints)
    must not gate the whole agent: required = intersection over operations, and
    the optional field's description names the skills that use it."""
    from control_plane.openapi_agent import build_openapi_agent_source

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Blogish", "version": "1"},
        "servers": [{"url": "https://blogish.example"}],
        "components": {"securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "a2aSession": {"type": "apiKey", "in": "cookie", "name": "a2a_session"},
        }},
        "paths": {
            "/posts": {
                "get": {"operationId": "list_posts",
                        "security": [{}, {"bearerAuth": []}],
                        "responses": {"200": {"description": "ok"}}},
                "post": {"operationId": "create_post",
                         "security": [{"bearerAuth": []}],
                         "responses": {"200": {"description": "ok"}}},
            },
            "/comments": {
                "post": {"operationId": "create_comment",
                         "security": [{"a2aSession": []}],
                         "responses": {"200": {"description": "ok"}}},
            },
        },
    }
    generated = build_openapi_agent_source(spec, name="blogish")
    namespace: dict = {}
    exec(compile(generated.files["agent.py"], "agent.py", "exec"), namespace)  # noqa: S102
    fields = {
        field["name"]: field
        for field in namespace["agent"].card().consumer_setup.model_dump(mode="json")["fields"]
    }
    session = next(f for n, f in fields.items() if "SESSION" in n.upper() or "A2A" in n.upper())
    assert session["required"] is False
    assert "create_comment" in (session.get("description") or "")
    # bearer key is needed by every SECURED op that isn't session-only? No —
    # writes need it, comments don't: disjoint schemes -> both optional.
    bearer = next(
        f for n, f in fields.items()
        if ("BEARER" in n.upper() or "TOKEN" in n.upper()) and n not in (session["name"],)
    )
    assert bearer["required"] is False
