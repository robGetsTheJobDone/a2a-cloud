from __future__ import annotations

import hashlib
import io
import json
import keyword
import re
import tarfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import yaml

from .safe_http import SafeHTTPError, safe_fetch_url

DEFAULT_PETSTORE_OPENAPI_URL = "https://petstore3.swagger.io/api/v3/openapi.json"
MAX_OPENAPI_RESPONSE_BYTES = 10 * 1024 * 1024

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
DESTRUCTIVE_METHODS = {"post", "put", "patch", "delete"}
MAX_GENERATED_OPERATIONS = 500
MAX_DIRECT_OPERATION_SKILLS = 80
OPENAI_TOOL_NAME_MAX_LENGTH = 64
TOOL_NAME_HASH_LENGTH = 10
PATH_PARAMETER_RE = re.compile(r"{([^}/]+)}")
VERSION_SEGMENT_RE = re.compile(r"v\d+(?:\.\d+)?", re.IGNORECASE)
SECURITY_FIELD_HINT_RE = re.compile(r"(?:\$|`)([A-Z][A-Z0-9_]{2,})(?:`)?")


class OpenAPIGenerationError(ValueError):
    """Raised when an OpenAPI document cannot be converted to source."""


@dataclass(frozen=True)
class GeneratedOpenAPIAgent:
    name: str
    class_name: str
    description: str
    version: str
    files: dict[str, str]
    preview: dict[str, Any]
    server_url: str
    source_openapi_urls: list[str] | None = None
    server_urls: list[str] | None = None


@dataclass(frozen=True)
class _OpenAPISource:
    spec: Mapping[str, Any]
    url: str | None
    server_url: str
    base_url_field: str
    index: int
    title: str
    label: str
    auth_prefix: str


async def fetch_openapi_spec(url: str) -> dict[str, Any]:
    clean = _normalize_url(url)
    try:
        response = await safe_fetch_url(
            clean,
            headers={"accept": "application/json, application/yaml, text/yaml, */*"},
            max_response_bytes=MAX_OPENAPI_RESPONSE_BYTES,
            timeout_seconds=15.0,
        )
    except SafeHTTPError as exc:
        raise OpenAPIGenerationError(f"could not fetch OpenAPI URL: {exc}") from exc
    if response.status_code >= 400:
        raise OpenAPIGenerationError(
            f"OpenAPI URL returned HTTP {response.status_code}"
        )
    return parse_openapi_document(response.text)


def parse_openapi_document(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise OpenAPIGenerationError("OpenAPI document must be a JSON/YAML object")
    if not isinstance(data.get("paths"), dict):
        raise OpenAPIGenerationError("OpenAPI document is missing paths")
    return data


def build_openapi_agent_source(
    spec: Mapping[str, Any] | list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    name: str | None = None,
    description: str | None = None,
    spec_url: str | None = None,
    spec_urls: list[str] | tuple[str, ...] | None = None,
    base_url: str | None = None,
    base_urls: list[str | None] | tuple[str | None, ...] | None = None,
) -> GeneratedOpenAPIAgent:
    specs = _coerce_specs(spec)
    source_urls = _coerce_source_urls(
        len(specs),
        spec_url=spec_url,
        spec_urls=spec_urls,
    )
    source_base_urls = _coerce_base_urls(
        len(specs),
        base_url=base_url,
        base_urls=base_urls,
    )
    first_spec = specs[0]
    info = first_spec.get("info") if isinstance(first_spec.get("info"), Mapping) else {}
    title = str(info.get("title") or "OpenAPI Agent").strip()
    agent_name = _slugify_agent_name(name or title)
    class_name = _slug_to_class(agent_name)
    version = _clean_version(str(info.get("version") or "0.1.0"))
    agent_description = (
        description
        or str(info.get("description") or "").strip()
        or (
            f"Composite OpenAPI agent generated from {len(specs)} OpenAPI specs."
            if len(specs) > 1
            else ""
        )
        or f"Auto mode agent generated from the {title} OpenAPI spec."
    )
    raw_server_urls = [
        _default_server_url(source_spec, spec_url=source_url, base_url=source_base_url)
        for source_spec, source_url, source_base_url in zip(
            specs,
            source_urls,
            source_base_urls,
            strict=True,
        )
    ]
    base_url_setup_fields, base_url_field_by_server = _base_url_setup_fields(raw_server_urls)
    sources: list[_OpenAPISource] = []
    for index, (source_spec, source_url, server_url) in enumerate(
        zip(specs, source_urls, raw_server_urls, strict=True),
        start=1,
    ):
        source_title = _source_title(source_spec, fallback=f"OpenAPI source {index}")
        source_label = _source_label(
            title=source_title,
            url=source_url,
            server_url=server_url,
            index=index,
        )
        sources.append(
            _OpenAPISource(
                spec=source_spec,
                url=source_url,
                server_url=server_url,
                base_url_field=base_url_field_by_server[server_url],
                index=index,
                title=source_title,
                label=source_label,
                auth_prefix=_source_auth_prefix(source_label, index=index),
            )
        )
    server_url = sources[0].server_url
    server_urls = _unique_ordered(source.server_url for source in sources)
    source_url_values = [source.url for source in sources if source.url]
    source_openapi_url = source_url_values[0] if len(source_url_values) == 1 else None
    source_openapi_urls = list(source_url_values)
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    seen_skills: set[str] = {"auto"}
    security_schemes: dict[str, dict[str, Any]] = {}
    root_security: list[Any] = []
    composite = len(sources) > 1
    for source in sources:
        source_root_security = (
            source.spec.get("security") if isinstance(source.spec.get("security"), list) else []
        )
        source_security_schemes = _security_schemes(source.spec)
        source_security_schemes, source_root_security, inferred_warnings = _infer_missing_security(
            source.spec,
            security_schemes=source_security_schemes,
            root_security=source_root_security,
        )
        warnings.extend(inferred_warnings)
        scheme_name_map = _merge_security_schemes(
            security_schemes,
            source_security_schemes,
            source=source,
            namespace=composite,
        )
        source_root_security = _rewrite_security_requirements(
            source_root_security,
            scheme_name_map,
        )
        if not composite:
            root_security = source_root_security
        remaining = MAX_GENERATED_OPERATIONS - len(operations)
        if remaining <= 0:
            break
        source_operations, source_warnings = _collect_operations(
            source.spec,
            seen_ids=seen_ids,
            seen_skills=seen_skills,
            max_operations=remaining,
        )
        warnings.extend(source_warnings)
        for operation in source_operations:
            operation["source_index"] = source.index
            operation["source_title"] = source.title
            operation["source_openapi_url"] = source.url
            operation["base_url"] = source.server_url
            operation["base_url_field"] = source.base_url_field
            if operation["security"] is None:
                if composite:
                    operation["security"] = deepcopy(source_root_security)
            else:
                operation["security"] = _rewrite_security_requirements(
                    operation["security"],
                    scheme_name_map,
                )
            operations.append(operation)
        if len(operations) >= MAX_GENERATED_OPERATIONS:
            break
    if not operations:
        raise OpenAPIGenerationError("OpenAPI document has no callable operations")
    use_subagents = len(operations) > MAX_DIRECT_OPERATION_SKILLS
    route_groups = _operation_route_groups(
        operations,
        max_group_size=MAX_DIRECT_OPERATION_SKILLS,
    ) if use_subagents else []
    required_security_schemes = _required_security_scheme_names(
        operations,
        root_security=root_security,
        security_schemes=security_schemes,
    )
    setup_fields, security_field_map = _consumer_setup_fields(
        security_schemes,
        server_url,
        required_security_schemes=required_security_schemes,
        base_url_setup_fields=base_url_setup_fields,
    )
    # Optional auth fields: say exactly which skills use them, so "why would I
    # set this?" is answered in the field itself instead of a support question.
    scheme_ops = _security_scheme_operations(operations, root_security=root_security)
    field_names_by_scheme = {
        scheme: mapping.get("field") for scheme, mapping in security_field_map.items()
    }
    for field in setup_fields:
        if field.get("required"):
            continue
        schemes_for_field = [
            s for s, fname in field_names_by_scheme.items() if fname == field.get("name")
        ]
        used_by = sorted({op for s in schemes_for_field for op in scheme_ops.get(s, [])})
        if used_by and len(used_by) < len(operations):
            shown = ", ".join(used_by[:3]) + (
                f" (+{len(used_by) - 3} more)" if len(used_by) > 3 else ""
            )
            desc = str(field.get("description") or "").rstrip()
            field["description"] = (f"{desc} " if desc else "") + f"Used by: {shown}."
    allowed_hosts = _allowed_hosts_for_urls(server_urls)
    openapi_file = _openapi_source_file(
        specs=specs,
        sources=sources,
        title=title,
        version=version,
    )
    files = {
        "README.md": _readme(
            name=agent_name,
            title=title,
            spec_url=source_openapi_url,
            spec_urls=source_openapi_urls,
            operation_count=len(operations),
            route_group_count=len(route_groups),
            use_subagents=use_subagents,
        ),
        "agent.py": _agent_py(
            class_name=class_name,
            agent_name=agent_name,
            description=agent_description,
            version=version,
            spec_url=source_openapi_url,
            spec_urls=source_openapi_urls,
            operations=operations,
            root_security=root_security,
            security_schemes=security_schemes,
            security_field_map=security_field_map,
            setup_fields=setup_fields,
            server_url=server_url,
            server_urls=server_urls,
            allowed_hosts=allowed_hosts,
            use_subagents=use_subagents,
            route_groups=route_groups,
        ),
        "a2a.yaml": _a2a_yaml(
            name=agent_name,
            class_name=class_name,
            version=version,
            description=agent_description,
            allowed_hosts=allowed_hosts,
        ),
        "requirements.txt": _requirements_txt(),
        "openapi.json": json.dumps(openapi_file, indent=2, sort_keys=True) + "\n",
    }
    if use_subagents:
        files.update(_skill_files(route_groups, operations_by_id=_operation_map_by_id(operations)))
    preview = {
        "name": agent_name,
        "description": agent_description,
        "version": version,
        "server_url": server_url,
        "server_urls": server_urls,
        "operation_count": len(operations),
        "operations": [_operation_preview(op, root_security=root_security) for op in operations],
        "consumer_setup": {"fields": setup_fields},
        "security_schemes": [
            _security_preview(name, scheme)
            for name, scheme in security_schemes.items()
        ],
        "skills": ["auto"] if use_subagents else ["auto", *(op["skill_name"] for op in operations)],
        "routing_mode": "deepagents_subagents" if use_subagents else "direct_operation_skills",
        "direct_operation_skill_limit": MAX_DIRECT_OPERATION_SKILLS,
        "route_groups": [_route_group_preview(group) for group in route_groups],
        "deepagent_skills": [group["skill_name"] for group in route_groups],
        "source_files": sorted(files),
        "source_openapi_url": source_openapi_url,
        "source_openapi_urls": source_openapi_urls,
        "regenerable": bool(source_openapi_urls),
        "composite": composite,
        "warnings": warnings,
    }
    return GeneratedOpenAPIAgent(
        name=agent_name,
        class_name=class_name,
        description=agent_description,
        version=version,
        files=files,
        preview=preview,
        server_url=server_url,
        source_openapi_urls=source_openapi_urls,
        server_urls=server_urls,
    )


def source_tarball_bytes(files: Mapping[str, str]) -> bytes:
    buf = io.BytesIO()
    now = int(datetime.now(timezone.utc).timestamp())
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for relpath, content in sorted(files.items()):
            path = PurePosixPath(relpath)
            if path.is_absolute() or ".." in path.parts:
                raise OpenAPIGenerationError(f"invalid generated path: {relpath}")
            data = content.encode("utf-8")
            info = tarfile.TarInfo(str(path))
            info.size = len(data)
            info.mtime = now
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _coerce_specs(
    spec: Mapping[str, Any] | list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> list[Mapping[str, Any]]:
    if isinstance(spec, Mapping):
        return [spec]
    specs = list(spec)
    if not specs:
        raise OpenAPIGenerationError("at least one OpenAPI document is required")
    if not all(isinstance(item, Mapping) for item in specs):
        raise OpenAPIGenerationError("OpenAPI documents must be JSON/YAML objects")
    return specs


def _coerce_source_urls(
    count: int,
    *,
    spec_url: str | None,
    spec_urls: list[str] | tuple[str, ...] | None,
) -> list[str | None]:
    if spec_urls is not None:
        urls = [str(url).strip() for url in spec_urls if str(url).strip()]
        if len(urls) != count:
            raise OpenAPIGenerationError("spec_urls must contain one URL per OpenAPI document")
        return urls
    if spec_url:
        if count != 1:
            raise OpenAPIGenerationError("composite OpenAPI agents require spec_urls")
        return [spec_url.strip()]
    return [None] * count


def _coerce_base_urls(
    count: int,
    *,
    base_url: str | None,
    base_urls: list[str | None] | tuple[str | None, ...] | None,
) -> list[str | None]:
    if base_urls is not None:
        if len(base_urls) != count:
            raise OpenAPIGenerationError("base_urls must contain one value per OpenAPI document")
        return [str(url).strip() if url else None for url in base_urls]
    return [base_url.strip() if base_url else None] * count


def _source_title(spec: Mapping[str, Any], *, fallback: str) -> str:
    info = spec.get("info") if isinstance(spec.get("info"), Mapping) else {}
    title = str(info.get("title") or "").strip()
    return title or fallback


def _source_label(
    *,
    title: str,
    url: str | None,
    server_url: str,
    index: int,
) -> str:
    if url:
        return url
    if server_url:
        return f"{title} ({server_url})"
    return f"{title} source {index}"


def _source_auth_prefix(value: str, *, index: int) -> str:
    parsed = urlparse(value)
    if parsed.netloc:
        raw = "_".join(part for part in (parsed.netloc, parsed.path.strip("/")) if part)
    else:
        raw = value
    prefix = _env_name(raw, fallback=f"OPENAPI_SOURCE_{index}")
    if len(prefix) <= 64:
        return prefix
    return f"{prefix[:32]}_{prefix[-31:]}"


def _unique_ordered(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _env_name(value: str, *, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
    if not candidate or not re.match(r"^[A-Z_]", candidate):
        candidate = fallback
    return candidate


def _normalize_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpenAPIGenerationError("OpenAPI URL must start with http:// or https://")
    return value


def _slugify_agent_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "openapi-agent"
    return slug[:128].strip("-") or "openapi-agent"


def _slug_to_class(slug: str) -> str:
    parts = re.split(r"[-_\s]+", slug.strip())
    return "".join(part[:1].upper() + part[1:] for part in parts if part) or "OpenAPIAgent"


def _clean_version(value: str) -> str:
    clean = value.strip() or "0.1.0"
    clean = re.sub(r"[^A-Za-z0-9_.+-]+", "-", clean)
    return clean[:64] or "0.1.0"


def _default_server_url(
    spec: Mapping[str, Any],
    *,
    spec_url: str | None,
    base_url: str | None,
) -> str:
    if base_url:
        return base_url.rstrip("/")
    servers = spec.get("servers")
    server_url = ""
    if isinstance(servers, list):
        for server in servers:
            if isinstance(server, Mapping) and isinstance(server.get("url"), str):
                server_url = server["url"].strip()
                break
    if server_url:
        if spec_url and server_url.startswith("/"):
            parsed = urlparse(spec_url)
            return urljoin(f"{parsed.scheme}://{parsed.netloc}", server_url).rstrip("/")
        return server_url.rstrip("/")
    swagger_host = str(spec.get("host") or "").strip()
    if swagger_host:
        schemes = spec.get("schemes")
        scheme_values: list[str] = []
        if isinstance(schemes, list):
            scheme_values = [
                str(scheme).strip().lower()
                for scheme in schemes
                if str(scheme).strip()
            ]
        scheme = "https" if "https" in scheme_values else (scheme_values[0] if scheme_values else "")
        if not scheme and spec_url:
            scheme = urlparse(spec_url).scheme
        scheme = scheme or "https"
        base_path = str(spec.get("basePath") or "").strip()
        if base_path and not base_path.startswith("/"):
            base_path = f"/{base_path}"
        return f"{scheme}://{swagger_host}{base_path}".rstrip("/")
    if spec_url:
        parsed = urlparse(spec_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return "https://api.example.com"


def _collect_operations(
    spec: Mapping[str, Any],
    *,
    seen_ids: set[str] | None = None,
    seen_skills: set[str] | None = None,
    max_operations: int = MAX_GENERATED_OPERATIONS,
) -> tuple[list[dict[str, Any]], list[str]]:
    paths = spec.get("paths")
    if not isinstance(paths, Mapping):
        return [], ["OpenAPI document has no paths object."]
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids = seen_ids if seen_ids is not None else set()
    seen_skills = seen_skills if seen_skills is not None else {"auto"}
    if max_operations <= 0:
        return operations, warnings
    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        path_parameters = path_item.get("parameters") if isinstance(path_item.get("parameters"), list) else []
        for method, operation in path_item.items():
            method_lower = str(method).lower()
            if method_lower not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            raw_id = str(operation.get("operationId") or f"{method_lower}_{path}").strip()
            operation_id = _unique_name(_sanitize_identifier(raw_id), seen_ids)
            skill_name = _unique_name(
                _openai_tool_name(raw_id),
                seen_skills,
                max_length=OPENAI_TOOL_NAME_MAX_LENGTH,
            )
            operation_parameters = (
                operation.get("parameters")
                if isinstance(operation.get("parameters"), list)
                else []
            )
            parameters = _normalize_parameters(
                [*path_parameters, *operation_parameters],
                spec=spec,
                path=str(path),
            )
            request_body = _request_body_schema(operation.get("requestBody"))
            if request_body is None:
                request_body = _swagger_body_schema(
                    [*path_parameters, *operation_parameters],
                    spec=spec,
                )
            if request_body is not None:
                request_body = _inline_refs(request_body, spec)
            if len(operations) >= max_operations:
                warnings.append(
                    f"Only the first {max_operations} operations were generated."
                )
                return operations, warnings
            operations.append(
                {
                    "operation_id": operation_id,
                    "skill_name": skill_name,
                    "method": method_lower.upper(),
                    "path": str(path),
                    "summary": str(operation.get("summary") or operation.get("description") or raw_id),
                    "description": str(operation.get("description") or operation.get("summary") or raw_id),
                    "tags": [str(tag) for tag in operation.get("tags") or []],
                    "parameters": parameters,
                    "request_body": request_body,
                    "security": operation.get("security") if isinstance(operation.get("security"), list) else None,
                    "destructive": method_lower in DESTRUCTIVE_METHODS,
                }
            )
    return operations, warnings


def _normalize_parameters(
    parameters: list[Any],
    *,
    spec: Mapping[str, Any],
    path: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for parameter in parameters:
        parameter = _resolve_parameter(parameter, spec)
        if parameter is None:
            continue
        name = str(parameter.get("name") or "").strip()
        location = str(parameter.get("in") or "query").strip().lower()
        if not name or location not in {"path", "query", "header", "cookie"}:
            continue
        key = (name, location)
        if key in seen:
            continue
        seen.add(key)
        schema = _inline_refs(_parameter_schema(parameter), spec)
        out.append(
            {
                "name": name,
                "in": location,
                "required": bool(parameter.get("required") or location == "path"),
                "description": str(parameter.get("description") or ""),
                "schema": dict(schema),
            }
        )
    for name in PATH_PARAMETER_RE.findall(path):
        key = (name, "path")
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "description": f"Path parameter `{name}`.",
                "schema": {"type": "string"},
            }
        )
    return out


def _resolve_parameter(value: Any, spec: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    ref = value.get("$ref")
    if isinstance(ref, str):
        resolved = _resolve_local_ref(spec, ref)
        return resolved if isinstance(resolved, Mapping) else None
    return value


def _inline_refs(value: Any, spec: Mapping[str, Any], _depth: int = 0) -> Any:
    """Recursively replace local ``$ref`` with the referenced schema. Agent
    cards carry no ``components`` section, so an unresolved ref is a dangling
    pointer for consumers (typed CLIs, LLM tool defs). Depth-capped so cyclic
    schemas degrade to a plain object instead of recursing forever."""
    if _depth > 12:
        return {"type": "object"}
    if isinstance(value, Mapping):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            resolved = _resolve_local_ref(spec, ref)
            if not isinstance(resolved, Mapping):
                return {"type": "object"}
            out = dict(_inline_refs(resolved, spec, _depth + 1))
            out.update({k: v for k, v in value.items() if k != "$ref"})
            return out
        return {k: _inline_refs(v, spec, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_inline_refs(v, spec, _depth + 1) for v in value]
    return value


def _resolve_local_ref(spec: Mapping[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        return None
    value: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(value, Mapping):
            return None
        key = part.replace("~1", "/").replace("~0", "~")
        value = value.get(key)
    return value


def _request_body_schema(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    content = value.get("content")
    if not isinstance(content, Mapping):
        return {"type": "object", "properties": {}}
    for mime in ("application/json", "application/*+json"):
        media = content.get(mime)
        if isinstance(media, Mapping) and isinstance(media.get("schema"), Mapping):
            return dict(media["schema"])
    for media in content.values():
        if isinstance(media, Mapping) and isinstance(media.get("schema"), Mapping):
            return dict(media["schema"])
    return {"type": "object", "properties": {}}


def _swagger_body_schema(
    parameters: list[Any],
    *,
    spec: Mapping[str, Any],
) -> dict[str, Any] | None:
    for parameter in parameters:
        parameter = _resolve_parameter(parameter, spec)
        if parameter is None:
            continue
        if str(parameter.get("in") or "").strip().lower() != "body":
            continue
        schema = parameter.get("schema")
        if isinstance(schema, Mapping):
            return dict(schema)
        return {"type": "object", "properties": {}}
    form_properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in parameters:
        parameter = _resolve_parameter(parameter, spec)
        if parameter is None:
            continue
        if str(parameter.get("in") or "").strip().lower() != "formdata":
            continue
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        form_properties[name] = _parameter_schema(parameter)
        if parameter.get("required"):
            required.append(name)
    if form_properties:
        schema: dict[str, Any] = {"type": "object", "properties": form_properties}
        if required:
            schema["required"] = required
        return schema
    return None


def _parameter_schema(parameter: Mapping[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    out: dict[str, Any] = {}
    for key in (
        "type",
        "format",
        "items",
        "collectionFormat",
        "enum",
        "default",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
    ):
        value = parameter.get(key)
        if value is not None:
            out[key] = value
    return out


def _security_schemes(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    components = spec.get("components")
    schemes: Any = None
    if isinstance(components, Mapping):
        schemes = components.get("securitySchemes")
    if not isinstance(schemes, Mapping):
        schemes = spec.get("securityDefinitions")
    if not isinstance(schemes, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, scheme in schemes.items():
        if not isinstance(scheme, Mapping):
            continue
        normalized = dict(scheme)
        if str(normalized.get("type") or "").lower() == "basic":
            normalized["type"] = "http"
            normalized["scheme"] = "basic"
        out[str(name)] = normalized
    return out


def _merge_security_schemes(
    target: dict[str, dict[str, Any]],
    incoming: Mapping[str, dict[str, Any]],
    *,
    source: _OpenAPISource,
    namespace: bool,
) -> dict[str, str]:
    name_map: dict[str, str] = {}
    for name, scheme in incoming.items():
        if namespace:
            scoped_name = _source_scoped_scheme_name(source.auth_prefix, name, target)
            target[scoped_name] = _annotate_source_security_scheme(name, scheme, source)
            name_map[name] = scoped_name
            continue
        if name not in target:
            target[name] = dict(scheme)
            name_map[name] = name
            continue
        if target[name] == dict(scheme):
            name_map[name] = name
            continue
        candidate = f"{name}_{source.index}"
        i = 2
        while candidate in target:
            candidate = f"{name}_{source.index}_{i}"
            i += 1
        target[candidate] = dict(scheme)
        name_map[name] = candidate
    return name_map


def _source_scoped_scheme_name(
    source_prefix: str,
    scheme_name: str,
    used: Mapping[str, Any],
) -> str:
    base = f"{source_prefix.lower()}__{_sanitize_identifier(scheme_name)}"
    candidate = base[:120]
    i = 2
    while candidate in used:
        suffix = f"_{i}"
        candidate = f"{base[:120 - len(suffix)]}{suffix}"
        i += 1
    return candidate


def _annotate_source_security_scheme(
    original_name: str,
    scheme: Mapping[str, Any],
    source: _OpenAPISource,
) -> dict[str, Any]:
    annotated = dict(scheme)
    base_field = _security_field_base(original_name, annotated)
    explicit_field = _explicit_security_field_name(annotated) is not None
    if not explicit_field:
        annotated["x-a2a-field-name"] = f"{source.auth_prefix}_{base_field}"
    base_label = str(
        annotated.get("x-a2a-label")
        or _default_security_label(original_name, annotated)
    )
    annotated["x-a2a-label"] = (
        base_label if explicit_field else f"{base_label} for {source.label}"
    )
    base_description = str(
        annotated.get("x-a2a-description")
        or annotated.get("description")
        or _default_security_description(original_name, annotated)
    ).strip()
    annotated["x-a2a-description"] = (
        f"OpenAPI source: {source.label}. {base_description}"
    ).strip()
    annotated["x-a2a-source-url"] = source.url or source.server_url
    annotated["x-a2a-source-title"] = source.title
    return annotated


def _rewrite_security_requirements(value: Any, name_map: Mapping[str, str]) -> list[Any]:
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for requirement in value:
        if not isinstance(requirement, Mapping):
            out.append(deepcopy(requirement))
            continue
        out.append(
            {
                name_map.get(str(name), str(name)): deepcopy(scopes)
                for name, scopes in requirement.items()
            }
        )
    return out


def _infer_missing_security(
    spec: Mapping[str, Any],
    *,
    security_schemes: Mapping[str, dict[str, Any]],
    root_security: list[Any],
) -> tuple[dict[str, dict[str, Any]], list[Any], list[str]]:
    schemes = {name: dict(scheme) for name, scheme in security_schemes.items()}
    warnings: list[str] = []
    if not schemes and not root_security and _looks_like_openpanel_api(spec):
        schemes["openpanel_client_id"] = {
            "type": "apiKey",
            "in": "header",
            "name": "openpanel-client-id",
            "description": "OpenPanel API client ID.",
            "x-a2a-field-name": "OPENPANEL_CLIENT_ID",
            "x-a2a-label": "OpenPanel client ID",
            "x-a2a-description": (
                "OpenPanel API client ID. Use a root client for manage/admin "
                "operations and a project client for project API operations."
            ),
        }
        schemes["openpanel_client_secret"] = {
            "type": "apiKey",
            "in": "header",
            "name": "openpanel-client-secret",
            "description": "OpenPanel API client secret.",
            "x-a2a-field-name": "OPENPANEL_CLIENT_SECRET",
            "x-a2a-label": "OpenPanel client secret",
            "x-a2a-description": (
                "OpenPanel API client secret. Requests send it in the "
                "`openpanel-client-secret` header."
            ),
        }
        warnings.append(
            "OpenPanel's OpenAPI document does not declare authentication; "
            "inferred required openpanel-client-id and openpanel-client-secret headers."
        )
        return schemes, [{"openpanel_client_id": [], "openpanel_client_secret": []}], warnings
    if schemes or root_security or not _looks_like_godaddy_domains_api(spec):
        return schemes, root_security, warnings

    scheme_name = "godaddy_sso_key"
    schemes[scheme_name] = {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": "GoDaddy API key and secret in API_KEY:API_SECRET format.",
        "x-a2a-field-name": "GODADDY_API_KEY_SECRET",
        "x-a2a-label": "GoDaddy API key and secret",
        "x-a2a-description": (
            "Enter API_KEY:API_SECRET. Requests send it as "
            "Authorization: sso-key <value>."
        ),
        "x-a2a-prefix": "sso-key ",
    }
    warnings.append(
        "GoDaddy's Swagger document does not declare authentication; inferred "
        "required Authorization: sso-key consumer setup."
    )
    return schemes, [{scheme_name: []}], warnings


def _looks_like_openpanel_api(spec: Mapping[str, Any]) -> bool:
    info = spec.get("info") if isinstance(spec.get("info"), Mapping) else {}
    title = str(info.get("title") or "").lower()
    paths = spec.get("paths") if isinstance(spec.get("paths"), Mapping) else {}
    if "openpanel" in title:
        return True
    return any(
        str(path).startswith(("/manage", "/track", "/events"))
        for path in paths
    )


def _looks_like_godaddy_domains_api(spec: Mapping[str, Any]) -> bool:
    host = str(spec.get("host") or "").lower()
    info = spec.get("info") if isinstance(spec.get("info"), Mapping) else {}
    title = str(info.get("title") or "").lower()
    paths = spec.get("paths") if isinstance(spec.get("paths"), Mapping) else {}
    if host in {"api.godaddy.com", "api.ote-godaddy.com"}:
        return True
    if "godaddy" in host:
        return True
    return "domains api" in title and any(str(path).startswith("/v1/domains") for path in paths)


def _consumer_setup_fields(
    security_schemes: Mapping[str, dict[str, Any]],
    server_url: str,
    *,
    required_security_schemes: set[str] | None = None,
    base_url_setup_fields: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    fields: list[dict[str, Any]] = base_url_setup_fields or [
        _config_field(
            "OPENAPI_BASE_URL",
            label="API base URL",
            description=f"Override the default API server ({server_url}).",
            required=False,
            input_type="url",
        )
    ]
    field_map: dict[str, dict[str, str]] = {}
    used_names = {str(field["name"]) for field in fields}
    field_by_name = {str(field["name"]): field for field in fields}
    required_names = required_security_schemes or set()
    for scheme_name, scheme in security_schemes.items():
        scheme_type = str(scheme.get("type") or "").lower()
        field_name = _security_field_name(
            scheme_name,
            scheme,
            used_names,
            reuse_existing=_security_field_can_reuse(scheme),
        )
        if scheme_type == "apikey":
            scheme_type = "apiKey"
        if scheme_type == "apiKey":
            location = str(scheme.get("in") or "header").lower()
            param_name = str(scheme.get("name") or scheme_name)
            label = str(scheme.get("x-a2a-label") or f"{scheme_name} API key")
            description = (
                str(scheme.get("x-a2a-description") or scheme.get("description") or "").strip()
                or f"Sent as {location} parameter {param_name!r}."
            )
            _append_secret_setup_field(
                fields,
                field_by_name,
                field_name,
                label=label,
                description=description,
                required=scheme_name in required_names,
            )
            field_map[scheme_name] = {
                "kind": "apiKey",
                "field": field_name,
                "location": location,
                "name": param_name,
            }
            prefix = scheme.get("x-a2a-prefix")
            if isinstance(prefix, str) and prefix:
                field_map[scheme_name]["prefix"] = prefix
        elif scheme_type == "http":
            auth_scheme = str(scheme.get("scheme") or "Bearer")
            label = str(
                scheme.get("x-a2a-label")
                or f"{scheme_name} {auth_scheme} credential"
            )
            description = str(scheme.get("x-a2a-description") or "").strip()
            if not description:
                if auth_scheme.lower() == "basic":
                    description = "Basic auth credential in username:password form."
                else:
                    description = str(scheme.get("description") or "").strip() or "Bearer token."
            _append_secret_setup_field(
                fields,
                field_by_name,
                field_name,
                label=label,
                description=description,
                required=scheme_name in required_names,
            )
            field_map[scheme_name] = {
                "kind": "http",
                "field": field_name,
                "scheme": auth_scheme,
            }
        elif scheme_type in {"oauth2", "openidconnect", "open_id_connect"}:
            label = str(scheme.get("x-a2a-label") or f"{scheme_name} access token")
            description = (
                str(scheme.get("x-a2a-description") or scheme.get("description") or "").strip()
                or "OAuth/OIDC access token."
            )
            _append_secret_setup_field(
                fields,
                field_by_name,
                field_name,
                label=label,
                description=description,
                required=scheme_name in required_names,
            )
            field_map[scheme_name] = {
                "kind": "oauth2",
                "field": field_name,
                "scheme": "Bearer",
            }
        else:
            field_map[scheme_name] = {"kind": "unsupported", "field": field_name}
    return fields, field_map


def _default_security_label(scheme_name: str, scheme: Mapping[str, Any]) -> str:
    scheme_type = str(scheme.get("type") or "").lower()
    if scheme_type in {"apikey", "api_key"}:
        return f"{scheme_name} API key"
    if scheme_type == "http":
        auth_scheme = str(scheme.get("scheme") or "Bearer")
        return f"{scheme_name} {auth_scheme} credential"
    if scheme_type in {"oauth2", "openidconnect", "open_id_connect"}:
        return f"{scheme_name} access token"
    return f"{scheme_name} credential"


def _default_security_description(scheme_name: str, scheme: Mapping[str, Any]) -> str:
    scheme_type = str(scheme.get("type") or "").lower()
    if scheme_type in {"apikey", "api_key"}:
        location = str(scheme.get("in") or "header").lower()
        param_name = str(scheme.get("name") or scheme_name)
        return f"Sent as {location} parameter {param_name!r}."
    if scheme_type == "http" and str(scheme.get("scheme") or "").lower() == "basic":
        return "Basic auth credential in username:password form."
    if scheme_type == "http":
        return "Bearer token."
    if scheme_type in {"oauth2", "openidconnect", "open_id_connect"}:
        return "OAuth/OIDC access token."
    return "Credential for this OpenAPI security scheme."


def _base_url_setup_fields(server_urls: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    fields: list[dict[str, Any]] = []
    field_by_server: dict[str, str] = {}
    unique_urls = _unique_ordered(server_urls)
    for index, server_url in enumerate(unique_urls, start=1):
        field_name = "OPENAPI_BASE_URL" if index == 1 else f"OPENAPI_BASE_URL_{index}"
        label = "API base URL" if index == 1 else f"API base URL {index}"
        fields.append(
            _config_field(
                field_name,
                label=label,
                description=f"Override the default API server ({server_url}).",
                required=False,
                input_type="url",
            )
        )
        field_by_server[server_url] = field_name
    return fields, field_by_server


def _config_field(
    name: str,
    *,
    label: str,
    description: str,
    required: bool,
    input_type: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "config",
        "label": label,
        "description": description,
        "required": required,
        "input_type": input_type,
        "options": [],
    }


def _required_security_scheme_names(
    operations: list[dict[str, Any]],
    *,
    root_security: list[Any],
    security_schemes: Mapping[str, dict[str, Any]],
) -> set[str]:
    """Schemes unavoidable for EVERY operation (intersection, not union).

    "Required" gates the whole agent — the platform refuses all invocations
    while a required field is unset. A scheme only some operations need (e.g.
    a session cookie on two comment endpoints of an otherwise key-authenticated
    blog API) must be declared optional, or public reads get blocked by a
    credential they never send. Per-operation needs still surface where they
    apply: the field's description names the skills that use it (see
    _security_scheme_operations), and the operation fails with that field
    named only when actually invoked."""
    # Intersect on consumer FIELD bases, not scheme names: composite imports
    # namespace schemes per source (admin__X / project__X) while both feed the
    # same consumer field, and that field is still unavoidable.
    required_bases: set[str] | None = None
    for operation in operations:
        security = operation["security"] if operation["security"] is not None else root_security
        op_required = _required_security_names_for_requirements(
            security,
            security_schemes=security_schemes,
        )
        if not op_required:
            # Anonymous-allowed or unsecured (health checks, public reads):
            # such operations need nothing, but they shouldn't demote a scheme
            # every *secured* operation depends on.
            continue
        op_bases = {
            _security_field_base(name, security_schemes.get(name, {})) for name in op_required
        }
        required_bases = op_bases if required_bases is None else (required_bases & op_bases)
        if not required_bases:
            return set()
    if not required_bases:
        return set()
    return {
        name
        for name, scheme in security_schemes.items()
        if _security_field_base(name, scheme) in required_bases
    }


def _security_scheme_operations(
    operations: list[dict[str, Any]],
    *,
    root_security: list[Any],
) -> dict[str, list[str]]:
    """scheme name -> skill names whose security mentions it (any alternative)."""
    out: dict[str, list[str]] = {}
    for operation in operations:
        security = operation["security"] if operation["security"] is not None else root_security
        for requirement in security or []:
            if not isinstance(requirement, Mapping):
                continue
            for scheme in requirement:
                out.setdefault(str(scheme), []).append(str(operation["skill_name"]))
    return out


def _required_security_names_for_requirements(
    security: list[Any],
    *,
    security_schemes: Mapping[str, dict[str, Any]],
) -> set[str]:
    if not security:
        return set()
    requirements = [requirement for requirement in security if isinstance(requirement, Mapping)]
    if any(not requirement for requirement in requirements):
        return set()
    if len(requirements) == 1:
        return {str(name) for name in requirements[0]}

    # OpenAPI security entries are alternatives: [{A: []}, {B: []}] means
    # A OR B, not A AND B. A setup field is safe to require only when every
    # alternative resolves to the same underlying consumer setup field, such
    # as bearer auth and x-api-key both using BLOG_API_KEY.
    field_keys = [
        _security_requirement_field_keys(
            requirement,
            security_schemes=security_schemes,
        )
        for requirement in requirements
    ]
    if field_keys and len(set(field_keys)) == 1:
        names: set[str] = set()
        for requirement in requirements:
            names.update(str(name) for name in requirement)
        return names
    return set()


def _security_requirement_field_keys(
    requirement: Mapping[str, Any],
    *,
    security_schemes: Mapping[str, dict[str, Any]],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _security_field_base(
                str(name),
                security_schemes.get(str(name), {}),
            )
            for name in requirement
        )
    )


def _secret_field(
    name: str,
    *,
    label: str,
    description: str,
    required: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "secret",
        "label": label,
        "description": description,
        "required": required,
        "input_type": "password",
        "options": [],
    }


def _append_secret_setup_field(
    fields: list[dict[str, Any]],
    field_by_name: dict[str, dict[str, Any]],
    name: str,
    *,
    label: str,
    description: str,
    required: bool,
) -> None:
    existing = field_by_name.get(name)
    if existing is None:
        field = _secret_field(
            name,
            label=label,
            description=description,
            required=required,
        )
        fields.append(field)
        field_by_name[name] = field
        return
    existing["required"] = bool(existing.get("required")) or required
    if not existing.get("label"):
        existing["label"] = label
    existing["description"] = _merge_descriptions(
        str(existing.get("description") or ""),
        description,
    )


def _merge_descriptions(existing: str, incoming: str) -> str:
    existing_sources, existing_body = _source_description_parts(existing)
    incoming_sources, incoming_body = _source_description_parts(incoming)
    if existing_sources and incoming_sources and existing_body == incoming_body:
        sources = _unique_ordered([*existing_sources, *incoming_sources])
        prefix = "OpenAPI source" if len(sources) == 1 else "OpenAPI sources"
        return f"{prefix}: {'; '.join(sources)}. {existing_body}"

    parts: list[str] = []
    for part in (existing.strip(), incoming.strip()):
        if part and part not in parts:
            parts.append(part)
    return " ".join(parts)


def _source_description_parts(description: str) -> tuple[list[str], str]:
    text = description.strip()
    for prefix in ("OpenAPI sources: ", "OpenAPI source: "):
        if not text.startswith(prefix):
            continue
        source_text, sep, body = text[len(prefix) :].partition(". ")
        if not sep or not body:
            return [], text
        return [source.strip() for source in source_text.split(";") if source.strip()], body
    return [], text


def _security_field_name(
    scheme_name: str,
    scheme: Mapping[str, Any],
    used_names: set[str],
    *,
    reuse_existing: bool = False,
) -> str:
    base = _security_field_base(scheme_name, scheme)
    candidate = _env_name(base, fallback="OPENAPI_CREDENTIAL")
    candidate = candidate[:96]
    if candidate not in used_names:
        used_names.add(candidate)
        return candidate
    if reuse_existing:
        return candidate
    i = 2
    while f"{candidate}_{i}" in used_names:
        i += 1
    unique = f"{candidate}_{i}"
    used_names.add(unique)
    return unique


def _security_field_base(
    scheme_name: str,
    scheme: Mapping[str, Any],
) -> str:
    scheme_type = str(scheme.get("type") or "").lower()
    explicit = _explicit_security_field_name(scheme)
    if explicit:
        return explicit
    inferred = _inferred_security_field_name(scheme)
    if inferred:
        return inferred
    if scheme_type in {"apikey", "api_key"}:
        return str(scheme.get("name") or scheme_name)
    if scheme_type == "http" and str(scheme.get("scheme") or "").lower() == "bearer":
        return f"{scheme_name}_token"
    return scheme_name


def _explicit_security_field_name(scheme: Mapping[str, Any]) -> str | None:
    explicit = scheme.get("x-a2a-field-name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return None


def _security_field_can_reuse(scheme: Mapping[str, Any]) -> bool:
    return (
        _explicit_security_field_name(scheme) is not None
        or _inferred_security_field_name(scheme) is not None
    )


def _inferred_security_field_name(scheme: Mapping[str, Any]) -> str | None:
    text = " ".join(
        str(scheme.get(key) or "")
        for key in ("x-a2a-description", "description")
    )
    for match in SECURITY_FIELD_HINT_RE.finditer(text):
        candidate = match.group(1)
        if "_" not in candidate:
            continue
        return candidate
    return None


def _allowed_hosts(server_url: str) -> list[str]:
    parsed = urlparse(server_url)
    return [parsed.hostname] if parsed.hostname else []


def _allowed_hosts_for_urls(server_urls: list[str]) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for server_url in server_urls:
        for host in _allowed_hosts(server_url):
            if host in seen:
                continue
            seen.add(host)
            hosts.append(host)
    return hosts


def _operation_preview(
    operation: Mapping[str, Any],
    *,
    root_security: list[Any],
) -> dict[str, Any]:
    security = operation["security"] if operation["security"] is not None else root_security
    out = {
        "operation_id": operation["operation_id"],
        "skill_name": operation["skill_name"],
        "method": operation["method"],
        "path": operation["path"],
        "summary": operation["summary"],
        "tags": operation["tags"],
        "destructive": operation["destructive"],
        "parameter_count": len(operation["parameters"]),
        "has_request_body": operation["request_body"] is not None,
        "requires_auth": _security_requires_auth(security),
    }
    if operation.get("source_index"):
        out["source_index"] = operation["source_index"]
    if operation.get("source_title"):
        out["source_title"] = operation["source_title"]
    if operation.get("source_openapi_url"):
        out["source_openapi_url"] = operation["source_openapi_url"]
    if operation.get("base_url"):
        out["base_url"] = operation["base_url"]
    return out


def _security_requires_auth(security: list[Any]) -> bool:
    if not security:
        return False
    return not any(isinstance(requirement, Mapping) and not requirement for requirement in security)


def _security_preview(name: str, scheme: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "name": name,
        "type": scheme.get("type") or "unknown",
        "location": scheme.get("in"),
        "parameter_name": scheme.get("name"),
        "scheme": scheme.get("scheme"),
        "description": scheme.get("description") or "",
    }
    if scheme.get("x-a2a-source-url"):
        out["source_openapi_url"] = scheme.get("x-a2a-source-url")
    if scheme.get("x-a2a-source-title"):
        out["source_title"] = scheme.get("x-a2a-source-title")
    return out


def _sanitize_identifier(value: str, *, max_length: int | None = 80) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value.strip())
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    clean = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    clean = re.sub(r"_+", "_", clean).strip("_").lower()
    if not clean:
        clean = "operation"
    if clean[0].isdigit():
        clean = f"op_{clean}"
    if keyword.iskeyword(clean):
        clean = f"op_{clean}"
    return clean[:max_length] if max_length is not None else clean


def _openai_tool_name(value: str) -> str:
    clean = _sanitize_identifier(value, max_length=None)
    if len(clean) <= OPENAI_TOOL_NAME_MAX_LENGTH:
        return clean
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:TOOL_NAME_HASH_LENGTH]
    prefix_length = OPENAI_TOOL_NAME_MAX_LENGTH - len(digest) - 1
    prefix = clean[:prefix_length].rstrip("_")
    return f"{prefix or 'operation'}_{digest}"


def _unique_name(base: str, seen: set[str], *, max_length: int | None = 80) -> str:
    if max_length is not None:
        base = base[:max_length]
    candidate = base
    i = 2
    while candidate in seen:
        suffix = f"_{i}"
        if max_length is None:
            candidate = f"{base}{suffix}"
        else:
            candidate = f"{base[:max_length - len(suffix)]}{suffix}"
        i += 1
    seen.add(candidate)
    return candidate


def _operation_map_by_id(operations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(op["operation_id"]): op for op in operations}


def _operation_route_groups(
    operations: list[dict[str, Any]],
    *,
    max_group_size: int,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for operation in operations:
        label = _operation_route_label(operation)
        buckets.setdefault(label, []).append(operation)

    groups: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for label, bucket in buckets.items():
        chunks = [
            bucket[index : index + max_group_size]
            for index in range(0, len(bucket), max_group_size)
        ]
        for chunk_index, chunk in enumerate(chunks, start=1):
            display_label = label if len(chunks) == 1 else f"{label} {chunk_index}"
            group_id = _unique_name(_sanitize_identifier(display_label), used_ids)
            skill_slug = group_id.replace("_", "-")
            common_prefix = _common_route_prefix([str(op["path"]) for op in chunk])
            groups.append(
                {
                    "id": group_id,
                    "label": display_label,
                    "subagent_name": f"{skill_slug}-api",
                    "skill_name": f"{skill_slug}-routes",
                    "skill_dir": skill_slug,
                    "skill_path": f"skills/{skill_slug}/SKILL.md",
                    "description": _route_group_description(
                        display_label,
                        common_prefix=common_prefix,
                        operation_count=len(chunk),
                    ),
                    "common_prefix": common_prefix,
                    "operation_ids": [str(op["operation_id"]) for op in chunk],
                }
            )
    return groups


def _operation_route_label(operation: Mapping[str, Any]) -> str:
    path = str(operation.get("path") or "")
    segments = [
        segment
        for segment in path.strip("/").split("/")
        if segment and not (segment.startswith("{") and segment.endswith("}"))
    ]
    meaningful = [
        segment
        for segment in segments
        if segment.lower() not in {"api", "apis", "rest"}
        and not VERSION_SEGMENT_RE.fullmatch(segment)
    ]
    if meaningful:
        return _human_route_label(meaningful[0])
    tags = [str(tag).strip() for tag in operation.get("tags") or [] if str(tag).strip()]
    if tags:
        return _human_route_label(tags[0])
    source_title = str(operation.get("source_title") or "").strip()
    if source_title:
        return _human_route_label(source_title)
    return "OpenAPI"


def _human_route_label(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z]+", " ", value).strip()
    return clean or "OpenAPI"


def _common_route_prefix(paths: list[str]) -> str:
    if not paths:
        return "/"
    split_paths = [[part for part in path.strip("/").split("/") if part] for path in paths]
    prefix: list[str] = []
    for parts in zip(*split_paths):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
    return "/" + "/".join(prefix) if prefix else "/"


def _route_group_description(
    label: str,
    *,
    common_prefix: str,
    operation_count: int,
) -> str:
    route_text = f" under {common_prefix}" if common_prefix != "/" else ""
    return f"Handle {operation_count} OpenAPI operation(s){route_text} for the {label} route group."


def _route_group_preview(group: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": group["id"],
        "label": group["label"],
        "subagent_name": group["subagent_name"],
        "skill_name": group["skill_name"],
        "skill_path": group["skill_path"],
        "operation_count": len(group["operation_ids"]),
        "operation_ids": list(group["operation_ids"]),
        "common_prefix": group["common_prefix"],
    }


def _skill_files(
    route_groups: list[dict[str, Any]],
    *,
    operations_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, str]:
    files: dict[str, str] = {}
    for group in route_groups:
        operations = [
            operations_by_id[operation_id]
            for operation_id in group["operation_ids"]
        ]
        files[group["skill_path"]] = _route_group_skill_md(group, operations)
    return files


def _route_group_skill_md(
    group: Mapping[str, Any],
    operations: list[dict[str, Any]],
) -> str:
    lines = [
        "---",
        f"name: {group['skill_name']}",
        f"description: {group['description']}",
        "---",
        "",
        f"# {group['label']} API Routes",
        "",
        str(group["description"]),
        "",
        "Use the generated operation tools to make real API calls. Do not invent API responses.",
        "If a tool reports missing setup, return the exact setup field name to the caller.",
        "For write, update, or delete operations, state the intended action before calling the tool.",
        "",
        "## Operations",
        "",
    ]
    for operation in operations:
        marker = "WRITE" if operation.get("destructive") else "READ"
        lines.extend(
            [
                f"### {operation['skill_name']}",
                "",
                f"- Operation ID: `{operation['operation_id']}`",
                f"- Route: `{operation['method']} {operation['path']}`",
                f"- Mode: {marker}",
                f"- Summary: {operation['summary']}",
            ]
        )
        if operation.get("parameters"):
            lines.append("- Parameters:")
            for parameter in operation["parameters"]:
                required = " required" if parameter.get("required") else ""
                lines.append(f"  - `{parameter['name']}` in `{parameter['in']}`{required}")
        if operation.get("request_body") is not None:
            lines.append("- Body: JSON request body accepted.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _readme(
    *,
    name: str,
    title: str,
    spec_url: str | None,
    spec_urls: list[str],
    operation_count: int,
    route_group_count: int,
    use_subagents: bool,
) -> str:
    if len(spec_urls) > 1:
        source = "Source OpenAPI:\n" + "\n".join(f"  - {url}" for url in spec_urls)
    else:
        source = f"Source OpenAPI: {spec_url or 'vendored openapi.json'}"
    routing = (
        f"- Routing mode: DeepAgents subagents across {route_group_count} route groups"
        if use_subagents
        else "- Routing mode: direct operation skills"
    )
    return f"""# {name}

Generated A2APack agent for {title}.

- {source}
- Generated operations: {operation_count}
- Main skill: `auto`
{routing}

The generated code is intentionally editable. Tune prompts, operation
grouping, auth names, and safety policy before publishing serious agents.
"""


def _openapi_source_file(
    *,
    specs: list[Mapping[str, Any]],
    sources: list[_OpenAPISource],
    title: str,
    version: str,
) -> Mapping[str, Any]:
    if len(specs) == 1:
        return specs[0]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{title} Composite",
            "version": version,
            "description": "Composite OpenAPI source bundle generated for an A2APack agent.",
        },
        "servers": [{"url": url} for url in _unique_ordered(source.server_url for source in sources)],
        "paths": {},
        "x-a2a-composite-openapi": True,
        "x-a2a-sources": [
            {
                "index": source.index,
                "title": source.title,
                "url": source.url,
                "server_url": source.server_url,
                "openapi": source.spec,
            }
            for source in sources
        ],
    }


def _a2a_yaml(
    *,
    name: str,
    class_name: str,
    version: str,
    description: str,
    allowed_hosts: list[str],
) -> str:
    data = {
        "name": name,
        "version": version,
        "entrypoint": f"agent:{class_name}",
        "description": description,
        "runtime": {
            "resources": {"cpu": "200m", "memory": "512Mi"},
            "egress": {
                "allow_hosts": allowed_hosts,
                "deny_internet_by_default": True,
            },
        },
    }
    return yaml.safe_dump(data, sort_keys=False)


def _requirements_txt() -> str:
    return """# a2a-pack is installed by the platform base image.
deepagents>=0.5.0
langchain>=0.3
langchain-openai>=0.2
langchain-core>=0.3
langgraph>=0.6
httpx>=0.27
"""


def _agent_py(
    *,
    class_name: str,
    agent_name: str,
    description: str,
    version: str,
    spec_url: str | None,
    spec_urls: list[str],
    operations: list[dict[str, Any]],
    root_security: list[Any],
    security_schemes: Mapping[str, dict[str, Any]],
    security_field_map: Mapping[str, dict[str, str]],
    setup_fields: list[dict[str, Any]],
    server_url: str,
    server_urls: list[str],
    allowed_hosts: list[str],
    use_subagents: bool,
    route_groups: list[dict[str, Any]],
) -> str:
    skill_methods = "" if use_subagents else "\n\n".join(_skill_method(op) for op in operations)
    consumer_setup = _consumer_setup_code(setup_fields)
    operation_map = {str(op["operation_id"]): op for op in operations}
    operation_groups = route_groups if use_subagents else []
    graph_setup = (
        '''        backend = ctx.workspace_backend()
        skills_root = _seed_runtime_skills(backend, ctx)
        graph = create_a2a_deep_agent(
            ctx,
            creds=creds,
            backend=backend,
            tools=[],
            subagents=self._operation_subagents(ctx, skills_root),
            system_prompt=self._system_prompt(),
        )'''
        if use_subagents
        else '''        graph = create_a2a_deep_agent(
            ctx,
            creds=creds,
            tools=self._operation_tools(ctx),
            system_prompt=self._system_prompt(),
        )'''
    )
    return f'''from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
from a2a_pack.deepagents import create_a2a_deep_agent
from langchain_core.messages import BaseMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    ConsumerSetup,
    ConsumerSetupField,
    ConsumerSetupMissing,
    EgressPolicy,
    LLMProvisioning,
    Resources,
    RunContext,
)


DEFAULT_BASE_URL = {json.dumps(server_url)}
OPERATIONS = json.loads({json.dumps(json.dumps(operation_map, indent=2, sort_keys=True))})
OPERATION_GROUPS = json.loads({json.dumps(json.dumps(operation_groups, indent=2, sort_keys=True))})
ROOT_SECURITY = json.loads({json.dumps(json.dumps(root_security, indent=2, sort_keys=True))})
SECURITY_SCHEMES = json.loads({json.dumps(json.dumps(security_schemes, indent=2, sort_keys=True))})
SECURITY_FIELDS = json.loads({json.dumps(json.dumps(security_field_map, indent=2, sort_keys=True))})
PATH_PARAMETER_RE = re.compile(r"{{([^}}/]+)}}")
SOURCE_ROOT = Path(globals().get("__file__", "agent.py")).resolve().parent
SOURCE_SKILLS_DIR = SOURCE_ROOT / "skills"
RUNTIME_SKILLS_DIR = ".deepagents/openapi-skills/"


class OperationInput(BaseModel):
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Path, query, header, and cookie parameters keyed by OpenAPI parameter name.",
    )
    body: Any | None = Field(default=None, description="JSON request body, when the operation accepts one.")


class {class_name}(A2AAgent):
    name = {json.dumps(agent_name)}
    description = {json.dumps(description)}
    version = {json.dumps(version)}
    consumer_setup = {consumer_setup}
    llm_provisioning = LLMProvisioning.PLATFORM
    resources = Resources(cpu="200m", memory="512Mi")
    egress = EgressPolicy(
        allow_hosts={tuple(allowed_hosts)!r},
        deny_internet_by_default=True,
    )
    tools_used = ("openapi", "deepagents")
    capabilities = {{
        "openapi_auto_agent": {{
            "operation_count": len(OPERATIONS),
            "default_base_url": DEFAULT_BASE_URL,
            "source_openapi_url": {spec_url!r},
            "source_openapi_urls": {spec_urls!r},
            "server_urls": {server_urls!r},
            "regenerable": {bool(spec_urls)!r},
            "security_schemes": list(SECURITY_SCHEMES),
        }}
    }}

    @a2a.tool(
        name="auto",
        description="Use the OpenAPI service to complete a natural-language goal.",
        tags=("openapi", "auto"),
        timeout_seconds=900,
    )
    async def auto(self, ctx: RunContext, goal: str) -> dict[str, Any]:
        smoke_result = await self._maybe_smoke_test(ctx, goal)
        if smoke_result is not None:
            return smoke_result
        creds = ctx.llm
        if not creds.api_key:
            return {{
                "error": "llm_credentials_missing",
                "final": (
                    "LLM key required. Add an LLM credential in Settings > "
                    "LLM credentials before running this agent."
                ),
                "messages": [],
            }}
{graph_setup}
        result = await graph.ainvoke({{"messages": [{{"role": "user", "content": goal}}]}})
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final = _message_text(messages[-1]) if messages else result
        return {{
            "final": final,
            "messages": [_message_to_dict(message) for message in messages[-8:]],
        }}

{skill_methods}

    async def _maybe_smoke_test(self, ctx: RunContext, goal: str) -> dict[str, Any] | None:
        operation_id = _select_smoke_operation(goal)
        if operation_id is None:
            return None
        operation = OPERATIONS[operation_id]
        result = await self._request(ctx, operation_id, parameters={{}}, body=None)
        body = result.get("result") if result.get("ok") else result.get("error")
        final = {{
            "endpoint_called": str(operation.get("method")) + " " + str(operation.get("path")),
            "http_status": result.get("status_code"),
            "body_preview": _body_preview(body, 200),
        }}
        return {{
            "final": json.dumps(final, indent=2, ensure_ascii=False),
            "messages": [],
        }}

    def _operation_subagents(self, ctx: RunContext, skills_root: str) -> list[dict[str, Any]]:
        subagents: list[dict[str, Any]] = []
        for group in OPERATION_GROUPS:
            skill_path = (
                f"{{skills_root}}{{group['skill_dir']}}/"
                if skills_root
                else ""
            )
            subagent = {{
                "name": group["subagent_name"],
                "description": group["description"],
                "system_prompt": self._subagent_prompt(group),
                "tools": self._operation_tools(ctx, set(group["operation_ids"])),
            }}
            if skill_path:
                subagent["skills"] = [skill_path]
            subagents.append(subagent)
        return subagents

    def _operation_tools(
        self,
        ctx: RunContext,
        operation_ids: set[str] | None = None,
    ) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for operation_id, operation in OPERATIONS.items():
            if operation_ids is not None and operation_id not in operation_ids:
                continue
            async def call(
                parameters: dict[str, Any] | None = None,
                body: Any | None = None,
                *,
                _operation_id: str = operation_id,
            ) -> dict[str, Any]:
                return await self._request(
                    ctx,
                    _operation_id,
                    parameters=parameters or {{}},
                    body=body,
                )

            tools.append(
                StructuredTool.from_function(
                    coroutine=call,
                    name=operation["skill_name"],
                    description=self._tool_description(operation),
                    args_schema=OperationInput,
                )
            )
        return tools

    def _system_prompt(self) -> str:
        if OPERATION_GROUPS:
            lines = [
                "You coordinate route-specific OpenAPI subagents.",
                "Delegate API work to the matching subagent with the task tool.",
                "Do not invent API responses; ask subagents to call their tools for real results.",
                "For smoke tests, health checks, status checks, or ping-style requests, prefer explicit health/status/ping route groups over a generic root '/' route group.",
                "If a subagent reports missing consumer setup, tell the user which setup field is required.",
                "If a subagent reports that the generated OpenAPI agent may be stale, ask whether the user wants to refresh it from the latest OpenAPI spec.",
                "",
                "Available route groups:",
            ]
            for group in OPERATION_GROUPS:
                lines.append(
                    f"- {{group['subagent_name']}}: {{group['description']}}"
                )
            return "\\n".join(lines)
        lines = [
            "You operate an API through generated OpenAPI tools.",
            "Call tools to get real results. Do not invent API responses.",
            "For smoke tests, health checks, status checks, or ping-style requests, prefer explicit health/status/ping GET operations over generic root '/' operations.",
            "Use a root '/' operation only when the user asks for the service root/homepage or no more specific read-only operation fits.",
            "For write, update, or delete operations, explain the intended action before calling the tool.",
            "If an operation reports missing consumer setup, tell the user which setup field is required.",
            "If an operation returns 404, 405, 410, or a schema/validation error that suggests the live API no longer matches these tools, tell the user this generated agent may need to be refreshed from the latest OpenAPI spec and ask whether they want to refresh it.",
            "",
            "Available operations:",
        ]
        for operation in OPERATIONS.values():
            marker = "WRITE" if operation.get("destructive") else "READ"
            lines.append(
                f"- {{operation['skill_name']}}: {{marker}} {{operation['method']}} {{operation['path']}} — {{operation['summary']}}"
            )
        return "\\n".join(lines)

    def _subagent_prompt(self, group: dict[str, Any]) -> str:
        lines = [
            "You operate one route group from a generated OpenAPI API.",
            "Use the available operation tools to make real API calls. Do not invent API responses.",
            "Read the route group's SKILL.md when it applies; it lists every route you cover.",
            "For smoke tests, health checks, status checks, or ping-style requests, prefer explicit health/status/ping GET operations over generic root '/' operations.",
            "Use a root '/' operation only when the user asks for the service root/homepage or no more specific read-only operation fits.",
            "For write, update, or delete operations, explain the intended action before calling the tool.",
            "If an operation reports missing consumer setup, return the exact setup field name.",
            "If an operation returns 404, 405, 410, or a schema/validation error that suggests the live API no longer matches these tools, say the generated agent may need to be refreshed from the latest OpenAPI spec.",
            "",
            f"Route group: {{group['label']}}",
            f"Common prefix: {{group.get('common_prefix') or '/'}}",
            "Available operation tools:",
        ]
        for operation_id in group["operation_ids"]:
            operation = OPERATIONS[operation_id]
            marker = "WRITE" if operation.get("destructive") else "READ"
            lines.append(
                f"- {{operation['skill_name']}}: {{marker}} {{operation['method']}} {{operation['path']}} - {{operation['summary']}}"
            )
        return "\\n".join(lines)

    def _tool_description(self, operation: dict[str, Any]) -> str:
        parts = [
            f"{{operation['method']}} {{operation['path']}}",
            str(operation.get("description") or operation.get("summary") or ""),
        ]
        if operation.get("parameters"):
            parts.append(
                "Parameters: "
                + ", ".join(
                    f"{{p['name']}} in {{p['in']}}{{' required' if p.get('required') else ''}}"
                    for p in operation["parameters"]
                )
            )
        if operation.get("request_body") is not None:
            parts.append("Accepts a JSON request body.")
        parts.append(f"Operation ID: {{operation['operation_id']}}")
        return "\\n".join(part for part in parts if part)

    async def _request(
        self,
        ctx: RunContext,
        operation_id: str,
        *,
        parameters: dict[str, Any],
        body: Any | None,
    ) -> dict[str, Any]:
        operation = OPERATIONS[operation_id]
        default_base_url = str(operation.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        base_url_field = str(operation.get("base_url_field") or "OPENAPI_BASE_URL")
        base_url = str(ctx.consumer_config(base_url_field, default_base_url) or default_base_url).rstrip("/")
        url, query, headers = self._request_parts(ctx, operation, parameters)
        request_kwargs: dict[str, Any] = {{
            "params": query,
            "headers": headers,
        }}
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                body = body.decode("utf-8", errors="replace")
            if isinstance(body, str):
                stripped = body.strip()
                if stripped and stripped[0] in "{{[":
                    try:
                        body = json.loads(stripped)
                    except ValueError:
                        pass
            request_kwargs["json"] = body
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.request(
                operation["method"],
                f"{{base_url}}{{url}}",
                **request_kwargs,
            )
        content_type = response.headers.get("content-type", "")
        try:
            payload: Any = response.json() if "json" in content_type else response.text
        except ValueError:
            payload = response.text
        if response.status_code >= 400:
            out = {{
                "ok": False,
                "status_code": response.status_code,
                "operation_id": operation_id,
                "tool_name": operation.get("skill_name"),
                "error": payload,
            }}
            if response.status_code in {{404, 405, 410, 422}}:
                out["stale_openapi_hint"] = (
                    "This generated OpenAPI agent may be stale. Ask the user "
                    "whether they want to refresh it from the latest OpenAPI spec."
                )
            return out
        return {{
            "ok": True,
            "status_code": response.status_code,
            "operation_id": operation_id,
            "tool_name": operation.get("skill_name"),
            "result": payload,
        }}

    def _request_parts(
        self,
        ctx: RunContext,
        operation: dict[str, Any],
        parameters: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        path = operation["path"]
        query: dict[str, Any] = {{}}
        headers: dict[str, str] = {{}}
        cookies: dict[str, Any] = {{}}
        for param in operation.get("parameters") or []:
            name = param["name"]
            if name not in parameters:
                if param.get("required"):
                    raise ValueError(f"missing required parameter {{name!r}}")
                continue
            value = parameters[name]
            location = param["in"]
            if location == "path":
                path = path.replace("{{" + name + "}}", str(value))
            elif location == "query":
                query[name] = value
            elif location == "header":
                headers[name] = str(value)
            elif location == "cookie":
                cookies[name] = value
        unresolved = PATH_PARAMETER_RE.findall(path)
        if unresolved:
            missing = ", ".join(sorted(set(unresolved)))
            raise ValueError("missing required path parameter(s): " + missing)
        if cookies:
            headers["cookie"] = "; ".join(f"{{key}}={{value}}" for key, value in cookies.items())
        auth_headers, auth_query = self._auth_for_operation(ctx, operation)
        headers.update(auth_headers)
        query.update(auth_query)
        return path, query, headers

    def _auth_for_operation(self, ctx: RunContext, operation: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        requirements = operation.get("security")
        if requirements is None:
            requirements = ROOT_SECURITY
        if requirements == []:
            return {{}}, {{}}
        missing: list[str] = []
        for requirement in requirements or []:
            if not isinstance(requirement, dict) or not requirement:
                return {{}}, {{}}
            headers: dict[str, str] = {{}}
            query: dict[str, Any] = {{}}
            ok = True
            for scheme_name in requirement:
                mapping = SECURITY_FIELDS.get(scheme_name) or {{}}
                secret_name = mapping.get("field")
                value = _consumer_secret_optional(ctx, secret_name) if secret_name else ""
                if not value:
                    ok = False
                    if secret_name:
                        missing.append(secret_name)
                    continue
                kind = mapping.get("kind")
                if kind == "apiKey":
                    prefix = mapping.get("prefix") or ""
                    sent_value = f"{{prefix}}{{value}}"
                    if mapping.get("location") == "query":
                        query[mapping.get("name") or scheme_name] = sent_value
                    else:
                        headers[mapping.get("name") or scheme_name] = sent_value
                elif kind in {{"http", "oauth2"}}:
                    scheme = mapping.get("scheme") or "Bearer"
                    if scheme.lower() == "basic":
                        token = base64.b64encode(value.encode("utf-8")).decode("ascii")
                        headers["authorization"] = f"Basic {{token}}"
                    else:
                        headers["authorization"] = f"{{scheme}} {{value}}"
                else:
                    ok = False
            if ok:
                return headers, query
        if missing:
            raise ConsumerSetupMissing(
                "operation requires consumer setup secret(s): "
                + ", ".join(sorted(set(missing)))
            )
        return {{}}, {{}}


def _consumer_secret_optional(ctx: RunContext, name: str | None) -> str:
    if not name:
        return ""
    try:
        return ctx.consumer_secret(name)
    except ConsumerSetupMissing:
        return ""


def _select_smoke_operation(goal: str) -> str | None:
    text = str(goal or "").lower()
    smoke_terms = ("smoke", "health", "status", "ping", "readiness", "liveness")
    if not any(term in text for term in smoke_terms):
        return None
    preferred_terms = ("health", "status", "ping", "ready", "readiness", "live", "liveness")
    candidates: list[tuple[int, int, str]] = []
    for operation_id, operation in OPERATIONS.items():
        if str(operation.get("method") or "").upper() != "GET":
            continue
        if operation.get("destructive"):
            continue
        if any(param.get("required") for param in operation.get("parameters") or []):
            continue
        path = str(operation.get("path") or "")
        searchable = " ".join(
            str(operation.get(key) or "")
            for key in ("operation_id", "skill_name", "path", "summary", "description")
        ).lower()
        is_root = path.strip() == "/"
        matched_preferred = next(
            (index for index, term in enumerate(preferred_terms) if term in searchable),
            None,
        )
        if matched_preferred is not None:
            score = matched_preferred
        elif "smoke" in text and not is_root:
            score = 100
        elif "smoke" in text:
            score = 1000
        else:
            continue
        candidates.append((score, len(path), operation_id))
    if not candidates:
        return None
    return min(candidates)[2]


def _body_preview(body: Any, limit: int) -> str:
    if isinstance(body, str):
        text = body
    else:
        text = json.dumps(body, ensure_ascii=False)
    return text[:limit]


def _runtime_skills_root(ctx: RunContext) -> str:
    workspace = getattr(ctx, "_workspace", None)
    prefixes = tuple(getattr(workspace, "write_prefixes", ()) or ())
    if not prefixes:
        outputs_prefix = getattr(workspace, "outputs_prefix", None)
        prefixes = (outputs_prefix or "outputs/",)
    prefix = str(prefixes[0]).strip("/")
    return f"/{{prefix}}/{{RUNTIME_SKILLS_DIR}}" if prefix else f"/{{RUNTIME_SKILLS_DIR}}"


def _seed_runtime_skills(backend: Any, ctx: RunContext) -> str:
    if not SOURCE_SKILLS_DIR.exists():
        return ""
    runtime_root = _runtime_skills_root(ctx)
    uploads: list[tuple[str, bytes]] = []
    for path in SOURCE_SKILLS_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(SOURCE_SKILLS_DIR).as_posix()
        uploads.append((runtime_root + rel, path.read_bytes()))
    if not uploads:
        return ""
    backend.upload_files(uploads)
    return runtime_root


def _message_text(message: Any) -> Any:
    content = getattr(message, "content", message)
    return content


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, BaseMessage):
        return message.model_dump(mode="json")
    if hasattr(message, "model_dump"):
        return message.model_dump(mode="json")
    if isinstance(message, dict):
        return message
    return {{"content": str(message)}}


agent = {class_name}()
'''


def _operation_input_schema(operation: Mapping[str, Any]) -> dict[str, Any]:
    """The operation's real contract as JSON Schema, in the ``parameters`` /
    ``body`` envelope the generated handler accepts. Published on the agent
    card and MCP tools so consumers (typed CLIs, LLMs) see the actual field
    names and types instead of ``parameters: object, body: any``."""
    param_props: dict[str, Any] = {}
    param_required: list[str] = []
    for param in operation.get("parameters") or []:
        schema = dict(param.get("schema") or {"type": "string"})
        if param.get("description") and "description" not in schema:
            schema["description"] = param["description"]
        # Header/cookie params are still passed by name in `parameters`; the
        # request builder routes them. Names collide across locations rarely
        # and are already de-duped upstream.
        param_props[param["name"]] = schema
        if param.get("required"):
            param_required.append(param["name"])

    properties: dict[str, Any] = {}
    required: list[str] = []
    if param_props:
        properties["parameters"] = {
            "type": "object",
            "properties": param_props,
            "required": param_required,
            "additionalProperties": False,
        }
        if param_required:
            required.append("parameters")
    body_schema = operation.get("request_body")
    if body_schema is not None:
        schema = dict(body_schema)
        schema.setdefault("description", "JSON request body.")
        properties["body"] = schema
        required.append("body")
    if not properties:
        # No inputs at all — an empty, closed object beats `any`.
        return {"type": "object", "properties": {}, "additionalProperties": False}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _skill_method(operation: Mapping[str, Any]) -> str:
    description = f"{operation['method']} {operation['path']} - {operation['summary']}"
    tags = tuple(operation.get("tags") or ("openapi",))
    timeout = 120 if operation.get("destructive") else 60
    input_schema = _operation_input_schema(operation)
    return f'''    @a2a.tool(
        name={json.dumps(operation["skill_name"])},
        description={json.dumps(description)},
        tags={tags!r},
        timeout_seconds={timeout},
        input_schema={input_schema!r},
    )
    async def {operation["skill_name"]}(
        self,
        ctx: RunContext,
        parameters: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            ctx,
            {json.dumps(operation["operation_id"])},
            parameters=parameters or {{}},
            body=body,
        )'''


def _consumer_setup_code(fields: list[dict[str, Any]]) -> str:
    if not fields:
        return "ConsumerSetup.none()"
    lines: list[str] = ["ConsumerSetup.from_fields("]
    for field in fields:
        ctor = "config" if field["kind"] == "config" else "secret"
        args = [
            json.dumps(field["name"]),
            f"label={json.dumps(field.get('label'))}",
            f"description={json.dumps(field.get('description') or '')}",
            f"required={bool(field.get('required'))!r}",
        ]
        if ctor == "config":
            args.append(f"input_type={json.dumps(field.get('input_type') or 'text')}")
        lines.append(f"        ConsumerSetupField.{ctor}({', '.join(args)}),")
    lines.append("    )")
    return "\n".join(lines)
