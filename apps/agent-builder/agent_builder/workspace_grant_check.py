"""Validate workspace grant policy for artifact-producing Agent Card skills."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_ARTIFACT_OUTPUT_NAMES = {
    "artifact",
    "artifacts",
    "artifact_uri",
    "artifact_uris",
    "file",
    "files",
    "generated_file",
    "generated_files",
    "output_dir",
    "output_directory",
}


def _artifact_output_fields(schema: Any) -> tuple[str, ...]:
    """Return top-level result fields that imply durable workspace output."""
    if not isinstance(schema, dict):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(
        sorted(
            name
            for name in properties
            if isinstance(name, str)
            and (
                name.lower() in _ARTIFACT_OUTPUT_NAMES
                or name.lower().endswith(("_path", "_paths"))
            )
        )
    )


def _string_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _clean_prefix(value: str) -> str:
    return value.replace("\\", "/").strip().strip("/")


def _write_prefix_covers(outputs_prefix: str, write_prefix: str) -> bool:
    output = _clean_prefix(outputs_prefix)
    writable = _clean_prefix(write_prefix)
    if not output or not writable:
        return False
    if writable in {"**", "*"}:
        return True
    writable = writable.removesuffix("/**").removesuffix("/*").rstrip("/")
    return output == writable or output.startswith(writable + "/")


def validate_workspace_grants(card: dict[str, Any]) -> list[str]:
    """Return actionable errors for artifact skills with unusable grants."""
    skills = card.get("skills")
    if not isinstance(skills, list):
        return []

    workspace_access = card.get("workspace_access")
    workspace_enabled = (
        isinstance(workspace_access, dict)
        and workspace_access.get("enabled") is True
    )
    errors: list[str] = []
    for raw_skill in skills:
        if not isinstance(raw_skill, dict):
            continue
        fields = _artifact_output_fields(raw_skill.get("output_schema"))
        if not fields:
            continue
        skill_name = str(raw_skill.get("id") or raw_skill.get("name") or "<unknown>")
        marker = ", ".join(fields)
        if not workspace_enabled:
            errors.append(
                f"skill {skill_name!r} advertises durable outputs ({marker}) but "
                "workspace_access is not enabled"
            )

        policy = raw_skill.get("policy")
        policy = policy if isinstance(policy, dict) else {}
        if policy.get("grant_mode") != "read_write_overlay":
            errors.append(
                f"skill {skill_name!r} advertises durable outputs ({marker}) but "
                "grant_mode is not 'read_write_overlay'"
            )
        allows = _string_values(policy.get("grant_allow_patterns"))
        if not allows:
            errors.append(
                f"skill {skill_name!r} advertises durable outputs ({marker}) but "
                "grant_allow_patterns is empty"
            )
        outputs = _string_values(policy.get("grant_outputs_prefix"))
        writes = _string_values(policy.get("grant_write_prefixes"))
        if not outputs:
            errors.append(
                f"skill {skill_name!r} advertises durable outputs ({marker}) but "
                "grant_outputs_prefix is empty"
            )
        if not writes:
            errors.append(
                f"skill {skill_name!r} advertises durable outputs ({marker}) but "
                "grant_write_prefixes is empty"
            )
        if outputs and writes and not any(
            _write_prefix_covers(outputs[0], write_prefix)
            for write_prefix in writes
        ):
            errors.append(
                f"skill {skill_name!r} grant_outputs_prefix {outputs[0]!r} is not "
                "covered by grant_write_prefixes"
            )
        if outputs and allows and not any(
            _write_prefix_covers(outputs[0], allow_pattern)
            for allow_pattern in allows
        ):
            errors.append(
                f"skill {skill_name!r} grant_outputs_prefix {outputs[0]!r} is not "
                "covered by grant_allow_patterns"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: workspace_grant_check.py AGENT_CARD_JSON", file=sys.stderr)
        return 2
    try:
        card = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unable to read Agent Card: {exc}", file=sys.stderr)
        return 2
    if not isinstance(card, dict):
        print("Agent Card must be a JSON object", file=sys.stderr)
        return 2

    errors = validate_workspace_grants(card)
    if errors:
        print("workspace grant policy check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Add matching grant_mode, grant_allow_patterns, "
            "grant_outputs_prefix, and grant_write_prefixes to @a2a.tool.",
            file=sys.stderr,
        )
        return 1
    print("workspace grant policy ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
