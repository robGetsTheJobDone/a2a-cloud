from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from a2a_pack import (
    A2AAgent,
    AgentComposition,
    AgentGoal,
    AgentMemory,
    CompositionSubAgent,
    MetaAgentManifest,
    NoAuth,
    RunContext,
    TemplateLineage,
    skill,
)
from a2a_pack.cli.local import load_local_project
from a2a_pack.dsl import compile_agent_to_dsl
from a2a_pack.runtime import apply_project_manifest


def test_meta_agent_manifest_parses_yaml_blocks_with_aliases() -> None:
    manifest = MetaAgentManifest.from_mapping(
        {
            "composition": {
                "agents": [
                    {
                        "name": "researcher",
                        "version": "1.2.3",
                        "skills": ["search", "summarize", "search"],
                        "default_args": {"private_note": "not-for-card"},
                    },
                    {"tag": "charting", "skills": "render_chart", "required": False},
                ],
                "planning": "llm_dag",
                "max_nodes": 6,
            },
            "goal": {
                "objective": "Build a daily market brief.",
                "success_criteria": "brief includes sources",
            },
            "memory": {"tiers": ["files", "kv"], "namespace": "market-brief"},
        }
    )

    assert manifest.enabled is True
    assert manifest.composition is not None
    assert [item.name or item.tag for item in manifest.composition.sub_agents] == [
        "researcher",
        "charting",
    ]
    assert manifest.composition.sub_agents[0].skills == ("search", "summarize")
    assert manifest.goal is not None
    assert manifest.goal.success_criteria == ("brief includes sources",)
    assert manifest.memory is not None
    assert manifest.memory.tiers == ("files", "kv")


def test_composition_sub_agent_requires_name_or_tag() -> None:
    with pytest.raises(ValidationError, match="requires name or tag"):
        CompositionSubAgent.model_validate({"skills": ["run"]})


def test_meta_agent_manifest_accepts_concise_goal_and_memory_forms() -> None:
    manifest = MetaAgentManifest.from_mapping(
        {
            "composition": [{"name": "helper", "skills": "echo"}],
            "goal": "Keep a helper loop alive.",
            "memory": ["files", "kv"],
        }
    )

    assert manifest.composition is not None
    assert manifest.composition.sub_agents[0].name == "helper"
    assert manifest.goal is not None
    assert manifest.goal.objective == "Keep a helper loop alive."
    assert manifest.memory is not None
    assert manifest.memory.tiers == ("files", "kv")


def test_project_manifest_identity_overrides_loaded_agent_class() -> None:
    class LocalAgent(A2AAgent):
        name = "stale-name"
        description = "stale description"
        version = "0.1.0"

        @skill()
        async def run(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    apply_project_manifest(
        LocalAgent,
        {
            "name": "manifest-name",
            "description": "Manifest description",
            "version": "0.2.0",
        },
    )
    dsl = compile_agent_to_dsl(LocalAgent)
    card = LocalAgent().card()

    assert dsl.name == "manifest-name"
    assert dsl.description == "Manifest description"
    assert dsl.version == "0.2.0"
    assert card.name == "manifest-name"
    assert card.description == "Manifest description"
    assert card.version == "0.2.0"


def test_project_manifest_advertises_bounded_self_healing_policy() -> None:
    class LocalAgent(A2AAgent):
        name = "healing-agent"
        description = "heals"

        @skill()
        async def run(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    apply_project_manifest(
        LocalAgent,
        {
            "self_healing": {
                "enabled": True,
                "consecutive_failures": 2,
                "cooldown_seconds": 600,
                "max_repairs_per_day": 2,
            }
        },
    )

    policy = LocalAgent().card().capabilities["self_healing"]
    assert policy == {
        "enabled": True,
        "consecutive_failures": 2,
        "window_seconds": 300,
        "cooldown_seconds": 600,
        "max_repairs_per_day": 2,
        "max_turns": 30,
        "deployment_timeout_seconds": 1800,
        "require_tests": True,
    }


def test_project_manifest_accepts_concise_self_healing_flag() -> None:
    class LocalAgent(A2AAgent):
        name = "concise-healing-agent"
        description = "heals"

    apply_project_manifest(LocalAgent, {"self_healing": True})

    assert LocalAgent().card().capabilities["self_healing"]["enabled"] is True


def test_project_manifest_rejects_unbounded_self_healing_policy() -> None:
    class LocalAgent(A2AAgent):
        name = "unsafe-healing-agent"
        description = "unsafe"

    with pytest.raises(ValidationError, match="max_repairs_per_day"):
        apply_project_manifest(
            LocalAgent,
            {"self_healing": {"max_repairs_per_day": 999}},
        )


def test_template_lineage_requires_source_for_update_policy() -> None:
    with pytest.raises(ValidationError, match="requires template_ref or source_agent"):
        TemplateLineage(update_policy="propose")


def test_template_lineage_accepts_concise_template_ref() -> None:
    lineage = TemplateLineage.from_mapping("a2acloud/templates/smtp-email-agent")

    assert lineage.enabled is True
    assert lineage.template_ref == "a2acloud/templates/smtp-email-agent"
    assert lineage.update_policy == "none"


def test_card_surfaces_redacted_meta_agent_capability() -> None:
    class _Meta(A2AAgent):
        name = "brief-meta"
        description = "meta"
        meta_agent_manifest = MetaAgentManifest(
            composition=AgentComposition(
                sub_agents=(
                    CompositionSubAgent(
                        name="researcher",
                        skills=("search",),
                        default_args={"api_key": "secret", "topic": "ai"},
                    ),
                )
            ),
            goal=AgentGoal(objective="Create a sourced brief."),
            memory=AgentMemory(tiers=("files",), namespace="brief-meta"),
        )

        @skill()
        async def run(self, ctx: RunContext[NoAuth]) -> dict[str, bool]:
            return {"ok": True}

    payload = _Meta().card().capabilities["meta_agent"]
    assert payload["schema_version"] == "2026-06-02"
    assert payload["composition"]["sub_agents"] == [
        {
            "required": True,
            "name": "researcher",
            "skills": ["search"],
            "default_arg_keys": ["api_key", "topic"],
        }
    ]
    assert "secret" not in repr(payload)
    assert payload["goal"]["objective"] == "Create a sourced brief."
    assert payload["memory"]["tiers"] == ["files"]


def test_load_local_project_applies_yaml_manifest_to_agent_card(tmp_path) -> None:
    (tmp_path / "agent.py").write_text(
        textwrap.dedent(
            """
            from a2a_pack import A2AAgent, NoAuth, RunContext, skill

            class LocalMeta(A2AAgent):
                name = "local-meta"
                description = "local meta"

                @skill()
                async def run(self, ctx: RunContext[NoAuth]) -> dict:
                    return {"ok": True}
            """
        )
    )
    (tmp_path / "a2a.yaml").write_text(
        textwrap.dedent(
            """
            name: local-meta
            version: 0.1.0
            entrypoint: agent:LocalMeta
            composition:
              sub_agents:
                - name: helper
                  skills: [echo]
            goal:
              objective: Keep a helper loop alive.
            memory:
              tiers: [files]
              namespace: local-meta
            """
        )
    )

    local = load_local_project(tmp_path)
    payload = local.agent_cls().card().capabilities["meta_agent"]

    assert payload["composition"]["sub_agents"][0]["name"] == "helper"
    assert payload["goal"]["objective"] == "Keep a helper loop alive."
    assert payload["memory"]["namespace"] == "local-meta"


def test_load_local_project_applies_yaml_platform_resource_declarations(tmp_path) -> None:
    (tmp_path / "agent.py").write_text(
        textwrap.dedent(
            """
            from a2a_pack import A2AAgent, NoAuth, RunContext, skill

            class LocalResourceAgent(A2AAgent):
                name = "local-resource-agent"
                description = "local resources"

                @skill()
                async def run(self, ctx: RunContext[NoAuth]) -> dict:
                    return {"ok": True}
            """
        )
    )
    (tmp_path / "a2a.yaml").write_text(
        textwrap.dedent(
            """
            name: local-resource-agent
            version: 0.1.0
            entrypoint: agent:LocalResourceAgent
            resources:
              memory:
                tiers: [kv, vector]
                namespace: resource-memory
              databases:
                - name: app
                  provider: neon
                  engine: postgres
                  branch: preview
                  access_mode: read_only
                  env:
                    url: APP_DATABASE_URL
                  migrations:
                    path: db/migrations
            """
        )
    )

    local = load_local_project(tmp_path)
    payload = local.agent_cls().card().runtime.platform_resources.public_payload()

    assert payload["memory"]["tiers"] == ["kv", "vector"]
    assert payload["memory"]["namespace"] == "resource-memory"
    assert payload["databases"] == [
        {
            "name": "app",
            "engine": "postgres",
            "provider": "neon",
            "scope": "user",
            "branch": "preview",
            "access_mode": "read_only",
            "env": {"url": "APP_DATABASE_URL"},
            "scale_to_zero": True,
            "migrations": {"path": "db/migrations"},
        }
    ]


def test_load_local_project_applies_yaml_template_lineage_to_agent_card(tmp_path) -> None:
    (tmp_path / "agent.py").write_text(
        textwrap.dedent(
            """
            from a2a_pack import A2AAgent, NoAuth, RunContext, skill

            class LocalTemplateChild(A2AAgent):
                name = "local-template-child"
                description = "local child"

                @skill()
                async def run(self, ctx: RunContext[NoAuth]) -> dict:
                    return {"ok": True}
            """
        )
    )
    (tmp_path / "a2a.yaml").write_text(
        textwrap.dedent(
            """
            name: local-template-child
            version: 0.1.0
            entrypoint: agent:LocalTemplateChild
            template_lineage:
              template_ref: a2acloud/templates/smtp-email-agent
              template_version: 0.3.0
              source_agent: smtp-email-template
              source_revision: abc123
              instance_version: 0.1.0
              update_policy: propose
              update_channel: stable
              migration_skill: apply_template_update
            """
        )
    )

    local = load_local_project(tmp_path)
    payload = local.agent_cls().card().model_dump(mode="json")["template_lineage"]

    assert payload["template_ref"] == "a2acloud/templates/smtp-email-agent"
    assert payload["template_version"] == "0.3.0"
    assert payload["source_agent"] == "smtp-email-template"
    assert payload["source_revision"] == "abc123"
    assert payload["instance_version"] == "0.1.0"
    assert payload["update_policy"] == "propose"


def test_project_manifest_ignores_legacy_pricing_block() -> None:
    import warnings

    class LocalAgent(A2AAgent):
        name = "legacy-priced-agent"
        description = "old manifest"

        @skill()
        async def run(self, ctx: RunContext[NoAuth]) -> str:
            return "ok"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        apply_project_manifest(
            LocalAgent,
            {"runtime": {"pricing": {"price_per_call_usd": 0.5, "caller_pays_llm": True}}},
        )

    assert any("pricing" in str(w.message) for w in caught)
    assert not hasattr(LocalAgent, "pricing")
    payload = LocalAgent().card().model_dump(mode="json")
    assert "pricing" not in payload["runtime"]
