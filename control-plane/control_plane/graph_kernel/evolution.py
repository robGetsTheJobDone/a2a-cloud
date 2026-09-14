"""Simulation-only evolution experiments for graph-kernel templates.

The runner mutates bounded simulation specs, runs each variant through the
existing pure kernel simulator, scores the results, and emits a disabled draft
proposal for the winning variant. It does not persist, apply, or authorize live
runtime changes.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .simulation import (
    CustomSimulationError,
    CustomSimulationResult,
    run_custom_kernel_simulation,
)
from .templates import render_custom_kernel_template
from .primitives import (
    FitnessVector,
    Lineage,
    Mutation,
    PromotionGate,
    RollbackPlan,
    SafetyMarkers,
)
from .safety import (
    FORBIDDEN_ACTIVE_APPLY_FLAGS,
    FORBIDDEN_WRITE_INTENT_KEYS,
    SAFETY_MARKERS,
)


MAX_EVOLUTION_VARIANTS = 8
MAX_SCORE_DELTA = 25.0
MIN_EVOLUTION_PARTICIPANTS = 2
MAX_EVOLUTION_PARTICIPANTS = 12
MULTI_AGENT_TOPOLOGIES = ("star", "ring", "market", "mentor", "adversarial_review")
JsonObject = dict[str, Any]


class KernelEvolutionError(ValueError):
    pass


@dataclass(frozen=True)
class KernelVariantResult:
    variant_id: str
    parent_ref: str
    mutation: JsonObject
    spec: JsonObject
    passed: bool
    score: float
    trace: JsonObject
    trace_summary: JsonObject
    state_summary: JsonObject
    replay_digest: str
    proposal_ref: str | None = None
    error: str | None = None

    def to_payload(self) -> JsonObject:
        return {
            "variant_id": self.variant_id,
            "parent_ref": self.parent_ref,
            "mutation": deepcopy(self.mutation),
            "spec": deepcopy(self.spec),
            "passed": self.passed,
            "score": self.score,
            "trace": deepcopy(self.trace),
            "trace_summary": deepcopy(self.trace_summary),
            "state_summary": deepcopy(self.state_summary),
            "replay_digest": self.replay_digest,
            "proposal_ref": self.proposal_ref,
            "error": self.error,
            **SAFETY_MARKERS,
        }


@dataclass(frozen=True)
class KernelEvolutionResult:
    experiment_id: str
    title: str
    base_ref: str
    seed: str
    variants: tuple[KernelVariantResult, ...]
    winner_variant_id: str | None
    proposal: JsonObject | None

    @property
    def passed(self) -> bool:
        return self.winner_variant_id is not None

    def to_payload(self) -> JsonObject:
        return {
            "experiment_id": self.experiment_id,
            "title": self.title,
            "base_ref": self.base_ref,
            "seed": self.seed,
            "variant_count": len(self.variants),
            "passed": self.passed,
            "winner_variant_id": self.winner_variant_id,
            "variants": [variant.to_payload() for variant in self.variants],
            "proposal": deepcopy(self.proposal),
            **SAFETY_MARKERS,
        }


def run_kernel_evolution_experiment(experiment: JsonObject) -> KernelEvolutionResult:
    if not isinstance(experiment, dict):
        raise KernelEvolutionError("evolution experiment must be an object")
    _reject_active_evolution_request(experiment)
    title = _text(experiment.get("title") or "Kernel evolution experiment", field="title")
    variant_count = _int(experiment.get("variant_count"), default=3, minimum=1, maximum=MAX_EVOLUTION_VARIANTS)
    max_score_delta = _float(
        experiment.get("max_score_delta"),
        default=float(max(0, variant_count - 1)),
        minimum=0.0,
        maximum=MAX_SCORE_DELTA,
    )
    base_ref, base_spec = _base_spec(experiment)
    seed = _seed(experiment, base_ref=base_ref, base_spec=base_spec)
    experiment_id = _slug(experiment.get("experiment_id") or f"evolution-{seed[:12]}")
    strategy = _strategy(experiment, base_spec=base_spec)

    variants: list[KernelVariantResult] = []
    for index in range(variant_count):
        mutation = _mutation(
            index=index,
            variant_count=variant_count,
            max_score_delta=max_score_delta,
            strategy=strategy,
            base_spec=base_spec,
        )
        spec = _mutated_spec(base_spec, mutation=mutation, index=index, seed=seed)
        variant_id = f"{experiment_id}-v{index + 1}"
        proposal_ref = f"proposal:{experiment_id}:{variant_id}"
        variants.append(
            _run_variant(
                variant_id=variant_id,
                parent_ref=base_ref,
                mutation=mutation,
                spec=spec,
                proposal_ref=proposal_ref,
            )
        )

    winner = _winner(variants)
    proposal = _proposal(
        experiment_id=experiment_id,
        title=title,
        base_ref=base_ref,
        seed=seed,
        winner=winner,
    ) if winner is not None else None
    return KernelEvolutionResult(
        experiment_id=experiment_id,
        title=title,
        base_ref=base_ref,
        seed=seed,
        variants=tuple(variants),
        winner_variant_id=winner.variant_id if winner is not None else None,
        proposal=proposal,
    )


def _base_spec(experiment: JsonObject) -> tuple[str, JsonObject]:
    has_template = bool(experiment.get("base_template_id"))
    has_spec = isinstance(experiment.get("base_spec"), dict)
    population = experiment.get("participants")
    if population is None:
        population = experiment.get("agents")
    has_population = population is not None
    if sum(1 for item in (has_template, has_spec, has_population) if item) != 1:
        raise KernelEvolutionError("provide exactly one of base_template_id, base_spec, or participants")
    if has_template:
        template_id = _text(experiment.get("base_template_id"), field="base_template_id")
        try:
            return template_id, render_custom_kernel_template(template_id)
        except CustomSimulationError as exc:
            raise KernelEvolutionError(str(exc)) from exc
    if has_population:
        participants = _participant_specs(population)
        title = _text(experiment.get("title") or "Multi-agent evolution experiment", field="title")
        return "multi_agent_population", {
            "title": title,
            "scenario_id": _slug(f"{title}-population"),
            "metadata": {
                "evolution_population": participants,
                "simulation_type": _optional_text(experiment.get("simulation_type"), default="multi_agent_evolution"),
                **SAFETY_MARKERS,
            },
        }
    return "inline_spec", deepcopy(experiment["base_spec"])


def _reject_active_evolution_request(experiment: JsonObject) -> None:
    for key in FORBIDDEN_ACTIVE_APPLY_FLAGS:
        if bool(experiment.get(key)):
            raise KernelEvolutionError("kernel evolution experiments are simulation-only and cannot request active apply")
    for key in FORBIDDEN_WRITE_INTENT_KEYS:
        value = experiment.get(key)
        if value not in (None, "", [], {}, ()):
            raise KernelEvolutionError("kernel evolution experiments cannot carry mutation grants or write intents")


def _run_variant(
    *,
    variant_id: str,
    parent_ref: str,
    mutation: JsonObject,
    spec: JsonObject,
    proposal_ref: str,
) -> KernelVariantResult:
    try:
        result = run_custom_kernel_simulation(spec)
    except CustomSimulationError as exc:
        error = str(exc)
        empty = {
            "scenario_id": spec.get("scenario_id") or variant_id,
            "passed": False,
            "events": [],
            "invariant_results": [],
            "alerts": [],
            "violations": [error],
            **SAFETY_MARKERS,
        }
        return KernelVariantResult(
            variant_id=variant_id,
            parent_ref=parent_ref,
            mutation=mutation,
            spec=spec,
            passed=False,
            score=-10_000.0,
            trace=empty,
            trace_summary={
                "passed": False,
                "invariant_fail_count": 1,
                "violation_count": 1,
                "active_apply_enabled": False,
            },
            state_summary={},
            replay_digest=_digest({"variant_id": variant_id, "error": error}),
            proposal_ref=None,
            error=error,
        )
    score = _score_result(result, mutation=mutation)
    return KernelVariantResult(
        variant_id=variant_id,
        parent_ref=parent_ref,
        mutation=mutation,
        spec=spec,
        passed=result.passed,
        score=score,
        trace=result.trace,
        trace_summary=result.trace_summary,
        state_summary=result.state_summary,
        replay_digest=_digest(result.trace),
        proposal_ref=proposal_ref if result.passed else None,
    )


def _mutated_spec(base_spec: JsonObject, *, mutation: JsonObject, index: int, seed: str) -> JsonObject:
    if mutation.get("type") == "multi_agent_topology":
        return _multi_agent_variant_spec(base_spec, mutation=mutation, index=index, seed=seed)

    spec = deepcopy(base_spec)
    score_delta = float(mutation["score_delta"])
    spec["title"] = f"{_text(spec.get('title') or 'Kernel simulation', field='title')} / evolved {index + 1}"
    spec["scenario_id"] = _slug(f"{spec.get('scenario_id') or spec['title']}-{seed[:8]}-v{index + 1}")
    spec["metadata"] = {
        **(spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}),
        "evolution": {
            "variant_index": index,
            "mutation": deepcopy(mutation),
            "seed": seed,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
            **{key: value for key, value in SAFETY_MARKERS.items() if key != "active_apply_enabled"},
        },
    }
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "score_participant" and "score" in step:
            step["score"] = _round_score(float(step.get("score") or 0.0) + score_delta)
        if step.get("type") == "record_outcome":
            metrics = step.get("metrics")
            if isinstance(metrics, dict) and "score" in metrics:
                metrics["score"] = _round_score(float(metrics.get("score") or 0.0) + (score_delta / 100.0))
    _sync_participant_score_invariants(spec)
    return spec


def _sync_participant_score_invariants(spec: JsonObject) -> None:
    scores: dict[str, float] = {}
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "score_participant":
            score_id = str(step.get("score_id") or step.get("id") or "")
            if score_id:
                scores[score_id] = float(step.get("score") or 0.0)
    for invariant in spec.get("invariants") or []:
        if not isinstance(invariant, dict):
            continue
        if invariant.get("id") != "participant_score":
            continue
        score_id = str(invariant.get("score_id") or invariant.get("id") or "")
        if score_id in scores:
            invariant["score"] = _round_score(scores[score_id])


def _strategy(experiment: JsonObject, *, base_spec: JsonObject) -> str:
    metadata = base_spec.get("metadata") if isinstance(base_spec.get("metadata"), dict) else {}
    has_population = isinstance(metadata.get("evolution_population"), list)
    default = "multi_agent_topology" if has_population else "bounded_score_shift"
    strategy = _optional_text(experiment.get("strategy"), default=default)
    if strategy not in {"bounded_score_shift", "multi_agent_topology"}:
        raise KernelEvolutionError("strategy must be bounded_score_shift or multi_agent_topology")
    if has_population and strategy != "multi_agent_topology":
        raise KernelEvolutionError("participants require strategy multi_agent_topology")
    if not has_population and strategy != "bounded_score_shift":
        raise KernelEvolutionError("multi_agent_topology requires participants")
    return strategy


def _mutation(
    *,
    index: int,
    variant_count: int,
    max_score_delta: float,
    strategy: str,
    base_spec: JsonObject,
) -> JsonObject:
    if variant_count <= 1 or max_score_delta <= 0:
        score_delta = 0.0
    else:
        score_delta = max_score_delta * (index / float(variant_count - 1))
    if strategy == "multi_agent_topology":
        topology = MULTI_AGENT_TOPOLOGIES[index % len(MULTI_AGENT_TOPOLOGIES)]
        participants = _population_from_spec(base_spec)
        return {
            "type": "multi_agent_topology",
            "topology": topology,
            "score_delta": _round_score(score_delta),
            "rotation": index % len(participants),
            "participant_count": len(participants),
            "structural_complexity": _topology_complexity(topology, len(participants)),
            "bounds": {
                "max_score_delta": max_score_delta,
                "max_variants": MAX_EVOLUTION_VARIANTS,
                "min_participants": MIN_EVOLUTION_PARTICIPANTS,
                "max_participants": MAX_EVOLUTION_PARTICIPANTS,
            },
        }
    return {
        "type": "bounded_score_shift",
        "score_delta": _round_score(score_delta),
        "bounds": {
            "max_score_delta": max_score_delta,
            "max_variants": MAX_EVOLUTION_VARIANTS,
        },
    }


def _score_result(result: CustomSimulationResult, *, mutation: JsonObject) -> float:
    trace = result.trace
    summary = result.trace_summary
    if not result.passed:
        return -1_000.0 - float(summary.get("invariant_fail_count") or 0) * 100.0
    total_score = 0.0
    for event in trace.get("events") or []:
        if not isinstance(event, dict) or event.get("event_type") != "score.assigned":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        total_score += float(payload.get("score") or 0.0)
    policy_denials = _policy_denial_count(trace)
    alerts = len(trace.get("alerts") or [])
    violations = len(trace.get("violations") or [])
    return (
        1_000.0
        + total_score
        + float(mutation.get("score_delta") or 0.0)
        + float(mutation.get("structural_complexity") or 0.0)
        - policy_denials * 150.0
        - alerts * 25.0
        - violations * 250.0
    )


def _participant_specs(value: Any) -> list[JsonObject]:
    if not isinstance(value, list):
        raise KernelEvolutionError("participants must be a list")
    if len(value) < MIN_EVOLUTION_PARTICIPANTS or len(value) > MAX_EVOLUTION_PARTICIPANTS:
        raise KernelEvolutionError(
            f"participants must include between {MIN_EVOLUTION_PARTICIPANTS} and {MAX_EVOLUTION_PARTICIPANTS} agents"
        )
    participants: list[JsonObject] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            source: JsonObject = {"id": raw}
        elif isinstance(raw, dict):
            source = raw
        else:
            raise KernelEvolutionError("each participant must be text or an object")
        participant_id = _slug(source.get("id") or source.get("participant_id") or source.get("name") or f"agent-{index + 1}")[:48]
        if participant_id in seen:
            raise KernelEvolutionError(f"duplicate participant id: {participant_id}")
        seen.add(participant_id)
        skill = _slug(source.get("skill") or source.get("primary_skill") or f"skill-{index + 1}")[:48]
        fitness = _float(source.get("fitness", source.get("score")), default=60.0 + (index * 7.0), minimum=0.0, maximum=100.0)
        cost = _int(source.get("cost"), default=1 + (index % 3), minimum=0, maximum=20)
        capabilities = source.get("capabilities")
        if capabilities is None:
            capability_rows = [skill]
        elif isinstance(capabilities, list):
            capability_rows = [_slug(item)[:48] for item in capabilities[:8]]
        else:
            raise KernelEvolutionError("participant capabilities must be a list")
        participants.append(
            {
                "id": participant_id,
                "role": _optional_text(source.get("role"), default="candidate"),
                "skill": skill,
                "fitness": _round_score(fitness),
                "cost": cost,
                "capabilities": capability_rows or [skill],
            }
        )
    return participants


def _population_from_spec(base_spec: JsonObject) -> list[JsonObject]:
    metadata = base_spec.get("metadata") if isinstance(base_spec.get("metadata"), dict) else {}
    participants = metadata.get("evolution_population")
    if not isinstance(participants, list):
        raise KernelEvolutionError("multi-agent topology requires participants")
    return [deepcopy(item) for item in participants if isinstance(item, dict)]


def _multi_agent_variant_spec(base_spec: JsonObject, *, mutation: JsonObject, index: int, seed: str) -> JsonObject:
    participants = _rotate(_population_from_spec(base_spec), int(mutation.get("rotation") or 0))
    topology = _text(mutation.get("topology") or "star", field="topology")
    process_id = f"evo-process-{index + 1}"
    arena_id = f"evo-arena-{index + 1}"
    coordinator = "kernel-orchestrator"
    score_delta = float(mutation.get("score_delta") or 0.0)
    excluded = _excluded_participant(participants, topology)
    edges = _topology_edges(participants, topology, coordinator)
    winner_id = _expected_winner(
        participants,
        topology=topology,
        excluded_id=excluded["id"] if excluded else None,
        score_delta=score_delta,
    )
    resources = ["skill:*", "agent:*"] + [
        f"{participant['id']}:invoke:{participant['skill']}" for participant in participants
    ] + [
        f"{participant['id']}:peer:evolve" for participant in participants
    ]
    steps: list[JsonObject] = [
        {
            "type": "start_process",
            "process_id": process_id,
            "owner": coordinator,
            "capability_id": "cap-evolution-coordinator",
            "ttl": 20,
            "budget": max(10, len(edges) + len(participants) + 2),
            "max_calls": len(edges) + 2,
        }
    ]
    for edge in edges:
        steps.extend(
            [
                {
                    "type": "propose_edge",
                    "edge_id": edge["edge_id"],
                    "from": {"node_id": edge["from"], "port_id": edge["from_port"]},
                    "to": {"node_id": edge["to"], "port_id": edge["to_port"]},
                    "edge_type": edge["edge_type"],
                    "capability_id": "cap-evolution-coordinator",
                    "process_id": process_id,
                    "provenance_ref": f"kernel_evolution:{topology}:{seed[:12]}",
                },
                {
                    "type": "activate_edge",
                    "edge_id": edge["edge_id"],
                    "decision_id": f"pd-{edge['edge_id']}",
                },
                {"type": "use_edge", "edge_id": edge["edge_id"], "cost": edge["cost"]},
            ]
        )
    for rank, participant in enumerate(participants):
        score = _participant_score(participant, topology=topology, rank=rank, score_delta=score_delta)
        steps.extend(
            [
                {
                    "type": "emit_signal",
                    "node_id": participant["id"],
                    "signal_type": "evolution.observation",
                    "payload": {
                        "topology": topology,
                        "role": participant["role"],
                        "skill": participant["skill"],
                        "fitness": participant["fitness"],
                        "score": score,
                        "simulation_only": True,
                    },
                },
                {
                    "type": "record_outcome",
                    "outcome_id": f"outcome-{participant['id']}",
                    "participant_id": participant["id"],
                    "process_id": process_id,
                    "metrics": {
                        "score": _round_score(score / 100.0),
                        "cost": participant["cost"],
                        "topology": topology,
                    },
                    "evidence_refs": [f"evolution:{seed[:12]}:{topology}:{participant['id']}"],
                },
                {
                    "type": "score_participant",
                    "score_id": f"score-{participant['id']}",
                    "participant_id": participant["id"],
                    "outcome_id": f"outcome-{participant['id']}",
                    "score": score,
                    "explanation_refs": [f"mutation:{topology}:{index + 1}"],
                },
            ]
        )
    if excluded is not None:
        steps.append(
            {
                "type": "freeze_node",
                "node_id": excluded["id"],
                "reason": "unsafe arena participant",
            }
        )
    steps.extend(
        [
            {
                "type": "select_winner",
                "arena_id": arena_id,
                "candidates": {
                    participant["id"]: {
                        "score_id": f"score-{participant['id']}",
                        "outcome_id": f"outcome-{participant['id']}",
                        "process_id": process_id,
                    }
                    for participant in participants
                },
            },
            {
                "type": "check_policy",
                "decision_id": f"pd-evolution-{index + 1}",
                "action": "call",
                "resource": "skill:evolve",
            },
            {
                "type": "stop_process",
                "process_id": process_id,
                "reason": "simulation_completed",
            },
        ]
    )
    invariants: list[Any] = [
        "replay_deterministic",
        "no_active_apply",
        "no_violations",
        {"id": "winner_selected", "arena_id": arena_id, "winner_id": winner_id},
        {"id": "process_absent", "process_id": process_id},
        {"id": "expected_decision", "step": len(steps) - 1, "decision": "allow"},
    ]
    if excluded is not None:
        invariants.append(
            {
                "id": "candidate_excluded",
                "arena_id": arena_id,
                "participant_id": excluded["id"],
                "reason": "participant_frozen",
            }
        )
    for participant in participants:
        invariants.append(
            {
                "id": "outcome_recorded",
                "outcome_id": f"outcome-{participant['id']}",
                "participant_id": participant["id"],
            }
        )
    base_metadata = base_spec.get("metadata") if isinstance(base_spec.get("metadata"), dict) else {}
    return {
        "title": f"{_text(base_spec.get('title') or 'Multi-agent evolution', field='title')} / {topology} {index + 1}",
        "scenario_id": _slug(f"{base_spec.get('scenario_id') or 'multi-agent-evolution'}-{seed[:8]}-{topology}-{index + 1}"),
        "template_kind": "multi_agent_evolution",
        "risk_class": "simulation",
        "actors": [{"id": coordinator}] + [{"id": participant["id"]} for participant in participants],
        "ports": _multi_agent_ports(participants, coordinator=coordinator),
        "capabilities": [
            {
                "id": "cap-evolution-coordinator",
                "owner": coordinator,
                "actions": ["call", "review", "score", "collaborate", "mentor", "compare"],
                "resources": resources,
                "budget": max(20, len(edges) + len(participants) * 2),
                "delegation_depth": 1,
            }
        ],
        "policies": [
            {
                "id": "owner-allow-evolution",
                "level": "owner",
                "effect": "allow",
                "actions": ["call", "review", "collaborate", "mentor", "compare"],
                "resources": ["skill:*", "*:invoke:*", "*:peer:evolve"],
            }
        ],
        "steps": steps,
        "invariants": invariants[:25],
        "metadata": {
            **base_metadata,
            "evolution": {
                "variant_index": index,
                "mutation": deepcopy(mutation),
                "seed": seed,
                "simulation_type": base_metadata.get("simulation_type") or "multi_agent_evolution",
                "participants": deepcopy(participants),
                "topology": topology,
                "winner_id": winner_id,
                "excluded_participant_id": excluded["id"] if excluded else None,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
        },
    }


def _multi_agent_ports(participants: list[JsonObject], *, coordinator: str) -> list[JsonObject]:
    ports = [
        {"node_id": coordinator, "id": "route:evolve", "direction": "output", "schema_ref": "kernel.evolution.v1"},
        {"node_id": coordinator, "id": "review:evolve", "direction": "input", "schema_ref": "kernel.review.v1"},
    ]
    for participant in participants:
        ports.append(
            {
                "node_id": participant["id"],
                "id": f"invoke:{participant['skill']}",
                "direction": "input",
                "schema_ref": "agent.skill.v1",
            }
        )
        ports.append(
            {
                "node_id": participant["id"],
                "id": "peer:evolve",
                "direction": "bidirectional",
                "schema_ref": "agent.peer.v1",
            }
        )
    return ports


def _topology_edges(participants: list[JsonObject], topology: str, coordinator: str) -> list[JsonObject]:
    edges: list[JsonObject] = []
    if topology in {"star", "market", "adversarial_review"}:
        for participant in participants:
            edges.append(
                {
                    "edge_id": f"edge-{coordinator}-{participant['id']}"[:80],
                    "from": coordinator,
                    "from_port": "route:evolve",
                    "to": participant["id"],
                    "to_port": f"invoke:{participant['skill']}",
                    "edge_type": "call" if topology != "adversarial_review" else "review",
                    "cost": participant["cost"],
                }
            )
    if topology in {"ring", "mentor"}:
        limit = len(participants) if topology == "ring" else max(1, len(participants) - 1)
        for index in range(limit):
            current = participants[index]
            peer = participants[(index + 1) % len(participants)]
            if topology == "mentor" and index == len(participants) - 1:
                continue
            edges.append(
                {
                    "edge_id": f"edge-{current['id']}-{peer['id']}"[:80],
                    "from": current["id"],
                    "from_port": "peer:evolve",
                    "to": peer["id"],
                    "to_port": "peer:evolve",
                    "edge_type": "collaborate" if topology == "ring" else "mentor",
                    "cost": max(1, int(peer["cost"])),
                }
            )
    if topology == "market" and len(participants) >= 3:
        leader = participants[0]
        for participant in participants[1:]:
            edges.append(
                {
                    "edge_id": f"edge-market-{leader['id']}-{participant['id']}"[:80],
                    "from": leader["id"],
                    "from_port": "peer:evolve",
                    "to": participant["id"],
                    "to_port": "peer:evolve",
                    "edge_type": "compare",
                    "cost": 1,
                }
            )
    return edges


def _participant_score(participant: JsonObject, *, topology: str, rank: int, score_delta: float) -> float:
    role_bonus = {
        "star": 0.0,
        "ring": 2.0,
        "market": 4.0,
        "mentor": 3.0,
        "adversarial_review": -1.0,
    }.get(topology, 0.0)
    rank_bonus = max(0.0, 3.0 - float(rank))
    raw = float(participant["fitness"]) + role_bonus + rank_bonus + score_delta
    return _round_score(min(100.0, raw))


def _expected_winner(
    participants: list[JsonObject],
    *,
    topology: str,
    excluded_id: str | None,
    score_delta: float,
) -> str:
    scored = [
        (
            _participant_score(participant, topology=topology, rank=index, score_delta=score_delta),
            participant["id"],
        )
        for index, participant in enumerate(participants)
        if participant["id"] != excluded_id
    ]
    return sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[0][1]


def _excluded_participant(participants: list[JsonObject], topology: str) -> JsonObject | None:
    if topology != "adversarial_review":
        return None
    return sorted(participants, key=lambda item: (float(item["fitness"]), str(item["id"])))[0]


def _topology_complexity(topology: str, participant_count: int) -> float:
    multiplier = {
        "star": 1.0,
        "ring": 1.5,
        "market": 2.0,
        "mentor": 1.75,
        "adversarial_review": 2.25,
    }.get(topology, 1.0)
    return _round_score(multiplier * participant_count)


def _rotate(values: list[JsonObject], offset: int) -> list[JsonObject]:
    if not values:
        return []
    pivot = offset % len(values)
    return values[pivot:] + values[:pivot]


def _policy_denial_count(trace: JsonObject) -> int:
    count = 0
    for decision in trace.get("policy_decisions") or []:
        if isinstance(decision, dict) and decision.get("decision") in {"deny", "blocked"}:
            count += 1
    return count


def _winner(variants: list[KernelVariantResult]) -> KernelVariantResult | None:
    passed = [variant for variant in variants if variant.passed]
    if not passed:
        return None
    return sorted(passed, key=lambda variant: (variant.score, variant.variant_id), reverse=True)[0]


def _proposal(
    *,
    experiment_id: str,
    title: str,
    base_ref: str,
    seed: str,
    winner: KernelVariantResult,
) -> JsonObject:
    kernel_lineage = Lineage(
        parent_refs=[winner.parent_ref],
        variant_id=winner.variant_id,
        mutations=[Mutation(type=str(winner.mutation.get("type") or "mutation"), parameters=deepcopy(winner.mutation))],
        replay_digest=winner.replay_digest,
        safety=SafetyMarkers(),
    )
    fitness = FitnessVector(
        aggregate_score=winner.score,
        dimensions={
            "score": winner.score,
            "passed": winner.passed,
            "policy_denials": _policy_denial_count(winner.trace),
            "alerts": len(winner.trace.get("alerts") or []),
            "violations": len(winner.trace.get("violations") or []),
        },
    )
    rollback_plan = RollbackPlan(
        strategy="discard_disabled_draft",
        target_refs=[f"proposal:{experiment_id}:{winner.variant_id}"],
        restore_refs=[base_ref],
        steps=["leave current kernel state unchanged", "discard disabled proposal draft"],
        verified=True,
    )
    proposal = {
        "proposal_id": f"{experiment_id}:{winner.variant_id}",
        "proposal_type": "kernel_template_variant",
        "title": f"{title} proposal",
        "base_ref": base_ref,
        "winner_variant_id": winner.variant_id,
        "lineage": {
            "parent_ref": winner.parent_ref,
            "variant_id": winner.variant_id,
            "mutation": deepcopy(winner.mutation),
            "seed": seed,
            "replay_digest": winner.replay_digest,
        },
        "kernel_lineage": kernel_lineage.to_payload(),
        "draft": {
            "status": "disabled",
            "spec": deepcopy(winner.spec),
        },
        "score": winner.score,
        "fitness": fitness.to_payload(),
        "rollback_plan": rollback_plan.to_payload(),
        "promotion_gates": [
            PromotionGate(gate_type="owner_approval", status="missing").to_payload(),
            PromotionGate(gate_type="review", status="missing").to_payload(),
            PromotionGate(gate_type="proof", status="missing").to_payload(),
            PromotionGate(gate_type="canary", status="missing").to_payload(),
            PromotionGate(gate_type="rollback", status="passed", evidence_refs=[winner.replay_digest]).to_payload(),
        ],
        **SAFETY_MARKERS,
    }
    proposal["proposal_digest"] = _digest(proposal)
    return proposal


def _seed(experiment: JsonObject, *, base_ref: str, base_spec: JsonObject) -> str:
    provided = experiment.get("seed")
    if provided is not None:
        return _slug(provided)
    return _digest({"base_ref": base_ref, "base_spec": base_spec, "variant_count": experiment.get("variant_count")})


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _round_score(value: float) -> float:
    rounded = round(value, 6)
    return int(rounded) if rounded.is_integer() else rounded


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, (str, int, float)):
        raise KernelEvolutionError(f"{field} must be text")
    text = str(value).strip()
    if not text:
        raise KernelEvolutionError(f"{field} is required")
    if len(text) > 256:
        raise KernelEvolutionError(f"{field} exceeds 256 characters")
    return text


def _optional_text(value: Any, *, default: str) -> str:
    if value is None:
        return default
    return _text(value, field="text")


def _int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise KernelEvolutionError("expected an integer") from exc
    if number < minimum or number > maximum:
        raise KernelEvolutionError(f"integer must be between {minimum} and {maximum}")
    return number


def _float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if value is None:
        value = default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise KernelEvolutionError("expected a number") from exc
    if number < minimum or number > maximum:
        raise KernelEvolutionError(f"number must be between {minimum:g} and {maximum:g}")
    return number


def _slug(value: Any) -> str:
    text = _text(value, field="id").lower()
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-")
    return out[:96] or "evolution"
