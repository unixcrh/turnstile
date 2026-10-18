from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api import app, runtime_service
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.services.model_router_service import (
    ModelRouterExecutionService,
    ModelRouterService,
)
from turnstile_core.domain.assistant_models import (
    AssistantReply,
    AssistantRouterProposal,
    AssistantRouterProposalModel,
)
from turnstile_core.domain.model_router import ModelRouterCondition, ModelRouterDefinition
from turnstile_core.domain.runtime_models import (
    DecisionInvocationRequest,
    DecisionInvocationResponse,
    InvocationUsage,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from turnstile_core.persistence.in_memory import InMemoryRepository

client = TestClient(app)
OWNER_ID = UUID("00000000-0000-4000-8000-000000000101")
MEMBER_ID = UUID("00000000-0000-4000-8000-000000000102")


def identity(id_: UUID, email: str, role: str = "owner") -> SessionIdentity:
    return SessionIdentity(
        id=str(id_),
        email=email,
        name=email.split("@")[0],
        role=role,  # type: ignore[arg-type]
        method="password",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def router_repository() -> Iterator[tuple[InMemoryRepository, dict[str, SessionIdentity]]]:
    repository = InMemoryRepository()
    current = {"identity": identity(OWNER_ID, "owner@contoso.com")}
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[require_authenticated_session] = lambda: current["identity"]
    yield repository, current
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def test_router_list_keeps_missing_model_display_metadata_without_changing_candidates(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    model = next(item for item in repository.models if item["id"] == model_id)
    created = client.post("/api/v1/model-routers", json={
        "name": "Retained model display", "description": "",
        "definition": valid_definition(model["id"]),
    })
    assert created.status_code == 201
    definition = created.json()["definition"]
    reference = {
        "id": str(model["id"]), "display_name": model["display_name"],
        "model_key": model["model_key"], "family_key": "openai",
        "provider_name": "Microsoft Foundry", "status": "removed",
    }
    resolve = Mock(return_value=[reference])
    monkeypatch.setattr(repository, "model_router_model_references", resolve, raising=False)
    repository.models = [item for item in repository.models if item["id"] != model["id"]]

    response = client.get("/api/v1/model-routers")

    assert response.status_code == 200
    assert response.json()["model_references"] == [reference]
    assert response.json()["items"][0]["definition"] == definition
    resolve.assert_called_once_with([model["id"]])
    catalog = client.get("/api/v1/model-routers/catalog")
    assert catalog.status_code == 200
    assert str(model["id"]) not in catalog.json()["model_ids"]


def valid_definition(model_id: UUID) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "default_model_id": str(model_id),
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "candidates",
                "type": "candidate_models",
                "label": "Candidate Models",
                "position": {"x": 300, "y": 160},
                "candidates": [
                    {
                        "model_id": str(model_id),
                        "priority": 1,
                        "weight": 100,
                        "enabled": True,
                    }
                ],
            },
            {
                "id": "strategy",
                "type": "strategy",
                "label": "Strategy",
                "position": {"x": 500, "y": 160},
                "strategy_type": "priority",
            },
            {
                "id": "output",
                "type": "output",
                "label": "Output",
                "position": {"x": 700, "y": 160},
            },
        ],
        "edges": [
            {"id": "start-candidates", "source": "start", "target": "candidates"},
            {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
            {"id": "strategy-output", "source": "strategy", "target": "output"},
        ],
    }


def fallback_definition(first_model_id: UUID, second_model_id: UUID) -> dict[str, Any]:
    definition = valid_definition(first_model_id)
    definition["nodes"][1]["candidates"] = [
        {
            "model_id": str(first_model_id),
            "priority": 1,
            "weight": 100,
            "enabled": True,
        },
        {
            "model_id": str(second_model_id),
            "priority": 2,
            "weight": 100,
            "enabled": True,
        },
    ]
    definition["nodes"].insert(
        3,
        {
            "id": "fallback",
            "type": "fallback",
            "label": "Fallback",
            "position": {"x": 500, "y": 320},
            "fallback_on": ["rate_limit"],
        },
    )
    definition["edges"] = [
        {"id": "start-candidates", "source": "start", "target": "candidates"},
        {
            "id": "strategy-output",
            "source": "strategy",
            "target": "output",
            "label": "正常输出",
        },
        {
            "id": "strategy-fallback",
            "source": "strategy",
            "target": "fallback",
            "label": "异常策略",
        },
        {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
    ]
    return definition


def model_based_definition(
    routing_model_id: UUID,
    first_model_id: UUID,
    second_model_id: UUID,
) -> dict[str, Any]:
    definition = fallback_definition(first_model_id, second_model_id)
    strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    strategy["strategy_type"] = "model_based"
    strategy["routing_model_id"] = str(routing_model_id)
    return definition


def described_model_based_definition(
    routing_model_id: UUID,
    first_model_id: UUID,
    second_model_id: UUID,
) -> dict[str, Any]:
    definition = model_based_definition(
        routing_model_id,
        first_model_id,
        second_model_id,
    )
    strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    strategy["decision_prompt"] = (
        "Choose one authorized candidate and return JSON only.\n{catalog}"
    )
    strategy["max_prompt_chars"] = 64
    candidates = next(
        node for node in definition["nodes"] if node["type"] == "candidate_models"
    )["candidates"]
    candidates[0]["description"] = "Fast, economical default."
    candidates[1]["description"] = ""
    return definition


def legacy_fallback_definition(
    first_model_id: UUID, second_model_id: UUID
) -> dict[str, Any]:
    definition = fallback_definition(first_model_id, second_model_id)
    definition["nodes"] = [
        node for node in definition["nodes"] if node["type"] != "strategy"
    ]
    definition["nodes"].insert(
        2,
        {
            "id": "priority",
            "type": "priority",
            "label": "Priority",
            "position": {"x": 450, "y": 160},
        },
    )
    definition["edges"] = [
        {"id": "start-candidates", "source": "start", "target": "candidates"},
        {"id": "candidates-priority", "source": "candidates", "target": "priority"},
        {"id": "priority-fallback", "source": "priority", "target": "fallback"},
        {"id": "fallback-output", "source": "fallback", "target": "output"},
    ]
    return definition


def invocation_payload(router_id: str, user_id: str = "owner@contoso.com") -> dict[str, Any]:
    return {
        "router_id": router_id,
        "metadata": {
            "organization_id": "org-contoso-global",
            "organization": "Contoso Global",
            "department_id": "department-platform",
            "department": "AI Platform",
            "project_id": "project-finops",
            "project": "Model FinOps",
            "agent_id": "agent-console",
            "agent": "Invocation Console",
            "user_id": user_id,
            "user": user_id.split("@")[0],
            "workflow": "router-test",
            "model_id": "router",
            "model": "router",
            "runtime": "router",
            "request_source": "router-test",
            "run_id": "router-test",
            "turn_index": 1,
        },
        "messages": [{"role": "user", "content": "route this request"}],
    }


def enabled_model_ids(repository: InMemoryRepository) -> list[UUID]:
    return [UUID(str(model["id"])) for model in repository.models if model["enabled"]]


def persist_router_proposal(
    repository: InMemoryRepository,
    *,
    conversation_id: UUID,
    model_id: UUID,
    owner_id: str = "owner@contoso.com",
) -> AssistantRouterProposal:
    definition = ModelRouterDefinition.model_validate(valid_definition(model_id))
    proposal = AssistantRouterProposal(
        id=UUID("00000000-0000-4000-8000-000000000931"),
        name="Assistant Confirmed Router",
        description="Created from a persisted assistant proposal.",
        strategy_type="priority",
        models=[
            AssistantRouterProposalModel(
                model_id=model_id,
                display_name="Proposal model",
                provider_name="Test provider",
                capabilities=["chat"],
                branch="primary",
                priority=1,
                weight=100,
            )
        ],
        definition=definition,
    )
    reply = AssistantReply(
        conversation_id=str(conversation_id),
        message="Review this Router proposal.",
        charts=[],
        router_proposals=[proposal],
        steps=[],
        latency_ms=1,
        model="test-model",
        total_tokens=1,
    )
    repository.append_conversation_turn(
        conversation_id=conversation_id,
        owner_id=owner_id,
        title="Router proposal",
        question="Create a Router",
        reply=reply.model_dump(mode="json"),
    )
    return proposal


def test_execution_plan_scopes_fallback_to_the_reached_candidate_pool() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000911")
    second_model = UUID("00000000-0000-4000-8000-000000000912")
    current = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    )
    legacy = ModelRouterDefinition.model_validate(
        legacy_fallback_definition(first_model, second_model)
    )
    unrelated = current.model_copy(
        update={
            "edges": [edge for edge in current.edges if edge.target != "fallback"]
        }
    )

    current_candidates, current_events, current_strategy = (
        ModelRouterExecutionService._execution_plan(current, {})
    )
    legacy_candidates, legacy_events, legacy_strategy = (
        ModelRouterExecutionService._execution_plan(legacy, {})
    )
    _, unrelated_events, _ = ModelRouterExecutionService._execution_plan(unrelated, {})

    assert [candidate.model_id for candidate in current_candidates] == [
        first_model,
        second_model,
    ]
    assert current_events == {"rate_limit"}
    assert current_strategy == "priority"
    assert [candidate.model_id for candidate in legacy_candidates] == [
        first_model,
        second_model,
    ]
    assert legacy_events == {"rate_limit"}
    assert legacy_strategy == "priority"
    assert unrelated_events == set()


def test_execution_plan_never_bypasses_the_graph_with_default_model() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000913")
    definition = valid_definition(model_id)
    definition["edges"] = [
        {"id": "start-output", "source": "start", "target": "output"}
    ]

    with pytest.raises(
        ValueError, match="selected Router path must reach exactly one Strategy"
    ):
        ModelRouterExecutionService._execution_plan(
            ModelRouterDefinition.model_validate(definition), {}
        )


def test_v2_fallback_timeout_migrates_to_owning_strategy() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000914")
    second_model = UUID("00000000-0000-4000-8000-000000000915")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = None
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)
    migrated_strategy = next(node for node in migrated.nodes if node.type == "strategy")
    migrated_fallback = next(node for node in migrated.nodes if node.type == "fallback")

    assert migrated.schema_version == 2
    assert migrated_strategy.timeout_ms == 1_750
    assert migrated_fallback.timeout_ms is None


def test_v2_shared_fallback_timeout_migrates_to_every_owning_strategy() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000907")
    second_model = UUID("00000000-0000-4000-8000-000000000908")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    strategy["timeout_ms"] = None
    definition["nodes"].append({**strategy, "id": "strategy-2", "label": "Strategy 2"})
    definition["edges"].append(
        {
            "id": "strategy-2-fallback",
            "source": "strategy-2",
            "target": "fallback",
        }
    )
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)

    assert {
        node.timeout_ms for node in migrated.nodes if node.type == "strategy"
    } == {1_750}
    assert next(node for node in migrated.nodes if node.type == "fallback").timeout_ms is None


def test_v2_unowned_fallback_timeout_remains_readable() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000905")
    second_model = UUID("00000000-0000-4000-8000-000000000906")
    definition = fallback_definition(first_model, second_model)
    definition["edges"] = [
        edge for edge in definition["edges"] if edge["target"] != "fallback"
    ]
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)

    assert next(node for node in migrated.nodes if node.type == "fallback").timeout_ms == 1_750


def test_validation_rejects_quality_fallback() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000916")
    second_model = UUID("00000000-0000-4000-8000-000000000917")
    definition = fallback_definition(first_model, second_model)
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["fallback_on"] = ["quality"]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "fallback_quality_unsupported" in {
        issue.code for issue in validation.issues
    }


def test_validation_rejects_extra_condition_branch() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000918")
    definition = valid_definition(model_id)
    definition["nodes"].insert(
        1,
        {
            "id": "condition",
            "type": "condition",
            "label": "Condition",
            "position": {"x": 180, "y": 160},
            "condition": {"field": "task_type", "operator": "equals", "value": "chat"},
        },
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {"id": "condition-match", "source": "condition", "target": "candidates", "label": "Match"},
        {"id": "condition-else", "source": "condition", "target": "candidates", "label": "Else"},
        {"id": "condition-extra", "source": "condition", "target": "candidates", "label": "Other"},
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "condition_branches_ambiguous" in {
        issue.code for issue in validation.issues
    }


def test_validation_checks_else_branch_of_exists_condition_without_sampling() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000910")
    definition = valid_definition(model_id)
    definition["nodes"].insert(
        1,
        {
            "id": "condition",
            "type": "condition",
            "label": "Condition",
            "position": {"x": 180, "y": 160},
            "condition": {
                "field": "request_parameter",
                "operator": "exists",
                "value": None,
            },
        },
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-match",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        {
            "id": "condition-else",
            "source": "condition",
            "target": "output",
            "label": "Else",
        },
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "strategy_path_ambiguous" in {
        issue.code for issue in validation.issues
    }


def test_validation_rejects_multiple_strategies_on_one_path() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000919")
    second_model = UUID("00000000-0000-4000-8000-000000000920")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    definition["nodes"].append({**strategy, "id": "strategy-2", "label": "Strategy 2"})
    definition["edges"].extend(
        [
            {
                "id": "candidates-strategy-2",
                "source": "candidates",
                "target": "strategy-2",
            },
            {
                "id": "strategy-2-output",
                "source": "strategy-2",
                "target": "output",
            },
        ]
    )
    parsed = ModelRouterDefinition.model_validate(definition)

    with pytest.raises(
        ValueError, match="selected Router path must reach exactly one Strategy"
    ):
        ModelRouterExecutionService._execution_plan(parsed, {})
    assert "strategy_path_ambiguous" in {
        issue.code for issue in ModelRouterService.validate_definition(parsed).issues
    }


def test_validation_allows_one_strategy_on_each_exclusive_condition_branch() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000903")
    second_model = UUID("00000000-0000-4000-8000-000000000904")
    definition = ModelRouterDefinition.model_validate(
        valid_definition(first_model)
    ).model_dump(mode="json")
    first_candidates = next(
        node for node in definition["nodes"] if node["type"] == "candidate_models"
    )
    first_strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    definition["nodes"].extend(
        [
            {
                "id": "condition",
                "type": "condition",
                "label": "Condition",
                "position": {"x": 180, "y": 160},
                "condition": {
                    "field": "task_type",
                    "operator": "equals",
                    "value": "chat",
                },
            },
            {
                **first_candidates,
                "id": "candidates-2",
                "candidates": [{"model_id": str(second_model)}],
            },
            {
                **first_strategy,
                "id": "strategy-2",
                "model_configs": [
                    {
                        "model_id": str(second_model),
                        "priority": 1,
                        "weight": 100,
                        "max_latency_ms": None,
                        "max_cost_per_million": None,
                        "description": "",
                    }
                ],
            },
        ]
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-match",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        {
            "id": "condition-else",
            "source": "condition",
            "target": "candidates-2",
            "label": "Else",
        },
        {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
        {"id": "strategy-output", "source": "strategy", "target": "output"},
        {
            "id": "candidates-2-strategy-2",
            "source": "candidates-2",
            "target": "strategy-2",
        },
        {"id": "strategy-2-output", "source": "strategy-2", "target": "output"},
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert validation.valid is True
    assert validation.issues == []


def test_strategy_changes_candidate_order_and_blocks_unavailable_model_routing() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000921")
    second_model = UUID("00000000-0000-4000-8000-000000000922")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    )
    candidate_node = next(
        node for node in definition.nodes if node.type == "candidate_models"
    )
    strategy = next(node for node in definition.nodes if node.type == "strategy")

    assert definition.schema_version == 2
    assert candidate_node.candidates[0].model_dump() == {"model_id": first_model}
    assert [config.priority for config in strategy.model_configs] == [1, 2]
    assert [config.weight for config in strategy.model_configs] == [100, 100]
    strategy.model_configs[0].priority = 1
    strategy.model_configs[0].weight = 10
    strategy.model_configs[1].priority = 2
    strategy.model_configs[1].weight = 90

    priority_candidates, _, priority_type = ModelRouterExecutionService._execution_plan(
        definition, {}
    )
    strategy.strategy_type = "weighted"
    weighted_candidates, _, weighted_type = ModelRouterExecutionService._execution_plan(
        definition, {}
    )
    strategy.strategy_type = "model_based"
    validation = ModelRouterService.validate_definition(definition)
    bypass_definition = fallback_definition(first_model, second_model)
    bypass_definition["edges"].append(
        {
            "id": "candidate-output-bypass",
            "source": "candidates",
            "target": "output",
        }
    )
    bypass_validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(bypass_definition)
    )

    assert priority_type == "priority"
    assert [candidate.model_id for candidate in priority_candidates] == [
        first_model,
        second_model,
    ]
    assert weighted_type == "weighted"
    assert [candidate.model_id for candidate in weighted_candidates] == [
        second_model,
        first_model,
    ]
    assert "strategy_routing_model_missing" in {
        issue.code for issue in validation.issues
    }
    strategy.routing_model_id = first_model
    configured_validation = ModelRouterService.validate_definition(definition)
    assert "strategy_model_based_unavailable" not in {
        issue.code for issue in configured_validation.issues
    }
    assert "strategy_routing_model_missing" not in {
        issue.code for issue in configured_validation.issues
    }
    assert "candidate_strategy_bypass" in {
        issue.code for issue in bypass_validation.issues
    }


def test_condition_requires_match_and_else_branches() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000923")
    definition = valid_definition(model_id)
    condition = {
        "id": "condition",
        "type": "condition",
        "label": "Condition",
        "position": {"x": 180, "y": 160},
        "condition": {"field": "task_type", "operator": "equals", "value": "chat"},
    }
    definition["nodes"].insert(1, condition)
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-candidates",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "condition_branches_incomplete" in {
        issue.code for issue in validation.issues
    }

    execution_candidates, _, _ = ModelRouterExecutionService._execution_plan(
        ModelRouterDefinition.model_validate(definition),
        {"task_type": "does-not-match"},
    )
    assert [candidate.model_id for candidate in execution_candidates] == [model_id]


def test_condition_matching_uses_real_prompt_context_case_insensitively() -> None:
    payload = invocation_payload("00000000-0000-4000-8000-000000000924")
    payload["router_context"] = {
        "user_id": "spoofed@contoso.com",
        "request_parameter": "client supplied noise",
        "required_capability": "spoofed",
    }
    payload["messages"] = [
        {
            "role": "user",
            "content": "<userRequest>Please DEBUG this failure</userRequest>",
        }
    ]
    request = ModelInvocationRequest.model_validate(payload)

    context = ModelRouterExecutionService._routing_context(request)
    matched = ModelRouterExecutionService._condition_matches(
        ModelRouterCondition(
            field="request_parameter",
            operator="contains",
            value="debug",
        ),
        context,
    )
    length_matched = ModelRouterExecutionService._condition_matches(
        ModelRouterCondition(
            field="prompt_length",
            operator="greater_than_or_equal",
            value=len("Please DEBUG this failure"),
        ),
        context,
    )

    assert context["user_id"] == "owner@contoso.com"
    assert context["request_parameter"] == "Please DEBUG this failure"
    assert context["prompt_length"] == len("Please DEBUG this failure")
    assert context["required_capability"] == "chat"
    assert matched is True
    assert length_matched is True


def test_owner_confirms_persisted_assistant_router_proposal(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, current = router_repository
    model_id = enabled_model_ids(repository)[0]
    conversation_id = UUID("00000000-0000-4000-8000-000000000930")
    proposal = persist_router_proposal(
        repository,
        conversation_id=conversation_id,
        model_id=model_id,
    )
    path = (
        f"/api/v1/assistant/conversations/{conversation_id}"
        f"/router-proposals/{proposal.id}/confirm"
    )

    response = client.post(path)

    assert response.status_code == 201
    created = response.json()
    assert created["name"] == proposal.name
    assert created["status"] == "draft"
    assert created["version"] == 0
    assert created["published_at"] is None
    assert created["definition"] == proposal.definition.model_dump(mode="json")
    assert len(repository.model_routers) == 1
    assert repository.model_router_revisions == []
    assert repository.user_model_router_access == {}
    assert repository.model_router_invocations == {}
    repeated = client.post(path)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created["id"]
    assert len(repository.model_routers) == 1

    repository.model_routers[0]["description"] = "Edited after Assistant creation."
    assert client.post(path).status_code == 409

    current["identity"] = identity(MEMBER_ID, "member@contoso.com", "member")
    assert client.post(path).status_code == 403


def test_router_proposal_confirmation_is_scoped_to_its_conversation_owner(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    conversation_id = UUID("00000000-0000-4000-8000-000000000932")
    proposal = persist_router_proposal(
        repository,
        conversation_id=conversation_id,
        model_id=model_id,
        owner_id="someone.else@contoso.com",
    )

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}"
        f"/router-proposals/{proposal.id}/confirm",
        json={},
    )

    assert response.status_code == 404
    assert repository.model_routers == []


def test_model_router_draft_publish_revision_and_duplicate_lifecycle(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    created = client.post(
        "/api/v1/model-routers",
        json={
            "name": "客服问答路由",
            "description": "低成本优先，失败后回退",
            "definition": valid_definition(model_id),
        },
    )
    assert created.status_code == 201
    router = created.json()
    assert router["status"] == "draft"
    assert router["version"] == 0
    assert router["has_unpublished_changes"] is True

    listed = client.get("/api/v1/model-routers")
    validated = client.post(f"/api/v1/model-routers/{router['id']}/validate")
    published = client.post(f"/api/v1/model-routers/{router['id']}/publish")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [router["id"]]
    assert validated.json() == {"valid": True, "issues": []}
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["version"] == 1
    assert published.json()["has_unpublished_changes"] is False

    updated_definition = valid_definition(model_id)
    updated_definition["nodes"][1]["position"]["y"] = 240
    updated = client.put(
        f"/api/v1/model-routers/{router['id']}",
        json={
            "name": "客服问答路由",
            "description": "已调整画布",
            "definition": updated_definition,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "published"
    assert updated.json()["version"] == 1
    assert updated.json()["has_unpublished_changes"] is True

    republished = client.post(f"/api/v1/model-routers/{router['id']}/publish")
    duplicated = client.post(
        f"/api/v1/model-routers/{router['id']}/duplicate",
        json={"name": "客服问答路由副本"},
    )
    disabled = client.post(f"/api/v1/model-routers/{router['id']}/disable")

    assert republished.json()["version"] == 2
    assert len(repository.model_router_revisions) == 2
    assert duplicated.status_code == 201
    assert duplicated.json()["status"] == "draft"
    assert duplicated.json()["version"] == 0
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert client.delete(
        f"/api/v1/model-routers/{duplicated.json()['id']}"
    ).status_code == 204
    assert client.delete(f"/api/v1/model-routers/{router['id']}").status_code == 204
    assert client.get(f"/api/v1/model-routers/{router['id']}").status_code == 404
    assert len(repository.model_router_revisions) == 2
    recreated = client.post(
        "/api/v1/model-routers",
        json={"name": "客服问答路由", "definition": valid_definition(model_id)},
    )
    assert recreated.status_code == 201


def test_invalid_draft_can_be_saved_but_cannot_be_published(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    definition = valid_definition(model_id)
    definition["default_model_id"] = None
    definition["edges"].append(
        {"id": "output-start", "source": "output", "target": "start"}
    )
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "待完善路由", "definition": definition},
    )
    assert created.status_code == 201

    router_id = created.json()["id"]
    validation = client.post(f"/api/v1/model-routers/{router_id}/validate")
    publish = client.post(f"/api/v1/model-routers/{router_id}/publish")

    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert {issue["code"] for issue in validation.json()["issues"]} >= {
        "start_has_input",
        "output_has_exit",
        "cycle",
    }
    assert publish.status_code == 409
    assert repository.model_router_revisions == []


def test_model_router_is_user_scoped_and_rejects_unassigned_candidates(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, current = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Owner router", "definition": valid_definition(first_model)},
    )
    assert created.status_code == 201

    current["identity"] = identity(MEMBER_ID, "member@contoso.com", "member")
    repository.user_model_policies["member@contoso.com"] = {
        "user_id": "member@contoso.com",
        "model_ids": [first_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }

    assert client.get("/api/v1/model-routers").json() == {"items": [], "model_references": []}
    assert client.get("/api/v1/model-routers/catalog").json() == {
        "model_ids": [str(first_model)]
    }
    assert client.get(
        f"/api/v1/model-routers/{created.json()['id']}"
    ).status_code == 404
    rejected = client.post(
        "/api/v1/model-routers",
        json={"name": "Unauthorized", "definition": valid_definition(second_model)},
    )
    allowed = client.post(
        "/api/v1/model-routers",
        json={"name": "Member router", "definition": valid_definition(first_model)},
    )
    assert rejected.status_code == 409
    assert allowed.status_code == 201


def test_router_names_are_unique_per_user_case_insensitively(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    payload = {"name": "Code Router", "definition": valid_definition(model_id)}
    assert client.post("/api/v1/model-routers", json=payload).status_code == 201
    payload["name"] = "code router"
    response = client.post("/api/v1/model-routers", json=payload)
    assert response.status_code == 409


def test_published_assigned_router_falls_back_and_persists_attempt_trace(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    attempts: list[tuple[UUID, str, str]] = []

    class FallbackRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            attempts.append((request.model_id, request.metadata.user_id, request_id))
            if request.model_id == first_model:
                raise HTTPException(status_code=429, detail="provider rate limited")
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="ROUTER_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=37,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = FallbackRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Fallback Router",
                "definition": fallback_definition(first_model, second_model),
            },
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assignment = {
            "user_id": "owner@contoso.com",
            "assigned": True,
        }
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment", json=assignment
        ).status_code == 409
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        ).status_code == 403

        assigned = client.put(
            f"/api/v1/model-routers/{router_id}/assignment", json=assignment
        )
        assert assigned.status_code == 200
        assert client.get("/api/v1/model-routers/assigned").json()["items"][0][
            "id"
        ] == router_id
        assert client.get(
            f"/api/v1/model-routers/{router_id}/assignees"
        ).json() == ["owner@contoso.com"]

        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "ROUTER_FALLBACK_OK"
    assert response["router_id"] == router_id
    assert response["router_version"] == 1
    assert response["selected_model_id"] == str(second_model)
    assert [attempt[0] for attempt in attempts] == [first_model, second_model]
    assert {attempt[1] for attempt in attempts} == {"owner@contoso.com"}
    assert attempts[0][2] != attempts[1][2]
    assert [item["sequence"] for item in response["router_attempts"]] == [1, 2]
    assert [item["outcome"] for item in response["router_attempts"]] == [
        "failed",
        "succeeded",
    ]
    final_trace = repository.model_router_invocations[response["request_id"]]
    assert final_trace["router_name"] == "Fallback Router"
    assert final_trace["selected_model_id"] == second_model
    assert final_trace["router_attempts"] == response["router_attempts"]


def test_router_passes_strategy_timeout_and_falls_back_on_504(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = fallback_definition(first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = 1_250
    fallback["fallback_on"] = ["timeout"]
    calls: list[tuple[UUID, int | None]] = []

    class TimeoutRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append((request.model_id, timeout_ms))
            if request.model_id == first_model:
                raise HTTPException(status_code=504, detail="provider timed out")
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="TIMEOUT_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=21,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = TimeoutRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Timeout Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "TIMEOUT_FALLBACK_OK"
    assert calls == [(first_model, 1_250), (second_model, 1_250)]
    assert response["router_attempts"][0]["status_code"] == 504
    assert response["router_attempts"][0]["fallback_trigger"] == "timeout"


def test_router_does_not_retry_after_a_slow_success(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = fallback_definition(first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = 25
    fallback["fallback_on"] = ["timeout"]
    calls: list[tuple[UUID, int | None]] = []

    class SlowSuccessRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append((request.model_id, timeout_ms))
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="SLOW_SUCCESS_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=10_000,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = SlowSuccessRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Slow Success Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "SLOW_SUCCESS_OK"
    assert response["selected_model_id"] == str(first_model)
    assert calls == [(first_model, 25)]
    assert response["router_attempts"][0]["outcome"] == "succeeded"
    assert response["router_attempts"][0]["latency_ms"] == 10_000


def test_model_based_router_uses_real_user_request_and_selected_candidate(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]
    calls: list[ModelInvocationRequest] = []

    class ModelBasedRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append(request)
            if request.model_id == routing_model:
                assert timeout_ms == 5_000
                assert request.response_format == "json_object"
                assert request.messages[-1].content == "choose the strongest coding model"
                decision_system = request.messages[0].content or ""
                assert str(first_model) in decision_system
                assert str(second_model) in decision_system
                content = json.dumps(
                    {
                        "model_id": str(second_model),
                        "rationale": "The request needs stronger coding reasoning.",
                    }
                )
            else:
                assert request.model_id == second_model
                content = "MODEL_BASED_ROUTER_OK"
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=23,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = ModelBasedRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    second_model,
                ),
            },
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        payload = invocation_payload(router_id)
        payload["messages"] = [
            {
                "role": "user",
                "content": (
                    "<context>noisy workspace state</context>"
                    "<userRequest>choose the strongest coding model</userRequest>"
                ),
            }
        ]
        invoked = client.post("/api/v1/model-gateway/invoke", json=payload)
        payload["metadata"]["turn_index"] = 2
        payload["messages"] = [
            {
                "role": "user",
                "content": "a different follow-up that must keep the first decision",
            }
        ]
        repeated = client.post("/api/v1/model-gateway/invoke", json=payload)
        bound = next(model for model in repository.models if model["id"] == second_model)
        bound["enabled"] = False
        blocked = client.post("/api/v1/model-gateway/invoke", json=payload)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "model_disabled"
        assert len(calls) == 3
        bound["enabled"] = True
        resumed = client.post("/api/v1/model-gateway/invoke", json=payload)
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    assert repeated.status_code == 200
    response = invoked.json()
    assert response["content"] == "MODEL_BASED_ROUTER_OK"
    assert response["selected_model_id"] == str(second_model)
    assert [request.model_id for request in calls] == [
        routing_model,
        second_model,
        second_model,
        second_model,
    ]
    assert [attempt["attempt_role"] for attempt in response["router_attempts"]] == [
        "routing_decision",
        "candidate",
    ]
    assert response["router_attempts"][0]["decision_basis"] == "model_based_selection"
    assert response["router_attempts"][0]["selected_candidate_model_id"] == str(
        second_model
    )
    decision = response["router_attempts"][0]
    assert decision["decision_system"].startswith(
        "You are Turnstile's model routing classifier."
    )
    assert decision["decision_input_chars"] == len(
        "choose the strongest coding model"
    )
    assert decision["decision_input_sha256"] == hashlib.sha256(
        b"choose the strongest coding model"
    ).hexdigest()
    assert decision["decision_response_sha256"] == hashlib.sha256(
        json.dumps(
            {
                "model_id": str(second_model),
                "rationale": "The request needs stronger coding reasoning.",
            }
        ).encode()
    ).hexdigest()
    assert "decision_input" not in decision
    assert "raw_response" not in decision
    assert "rationale" not in decision
    assert "stronger coding reasoning" not in str(decision)
    assert repeated.json()["selected_model_id"] == str(second_model)
    assert repeated.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"
    assert resumed.status_code == 200
    assert resumed.json()["selected_model_id"] == str(second_model)
    assert resumed.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"


def test_model_router_decision_preview_uses_unsaved_prompt_and_descriptions(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]
    definition = described_model_based_definition(
        routing_model,
        first_model,
        second_model,
    )

    response = client.post(
        "/api/v1/model-routers/decision-preview",
        json={
            "definition": definition,
            "strategy_node_id": "strategy",
            "sample_prompt": "x" * 100,
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["system"].startswith(
        "Choose one authorized candidate and return JSON only."
    )
    assert "{catalog}" not in preview["system"]
    assert "Fast, economical default." in preview["system"]
    assert str(first_model) in preview["system"]
    assert str(second_model) in preview["system"]
    assert preview["sample_truncated"] is True
    assert len(preview["user"]) == 64
    assert preview["routing_model_id"] == str(routing_model)
    assert preview["models_without_description"] == [str(second_model)]
    assert preview["is_default_prompt"] is False
    assert preview["has_placeholder"] is True


def test_model_based_router_narrows_catalog_and_falls_back_from_invalid_choice(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, excluded_model = enabled_model_ids(repository)[:3]
    calls: list[UUID] = []

    class NarrowedRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append(request.model_id)
            if request.model_id == routing_model:
                catalog = request.messages[0].content or ""
                assert str(first_model) in catalog
                assert str(excluded_model) not in catalog
                content = json.dumps(
                    {
                        "model_id": str(excluded_model),
                        "rationale": "Attempted an excluded model.",
                    }
                )
                usage = InvocationUsage(
                    input_tokens=9,
                    cached_tokens=0,
                    output_tokens=3,
                    estimated=False,
                )
            else:
                assert request.model_id == first_model
                content = "MODEL_BASED_FALLBACK_OK"
                usage = None
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=19,
                usage=usage,
            )

    app.dependency_overrides[runtime_service] = NarrowedRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Narrowed Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    excluded_model,
                ),
            },
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        repository.user_model_policies["owner@contoso.com"] = {
            "user_id": "owner@contoso.com",
            "model_ids": [routing_model, first_model],
            "updated_at": datetime.now(UTC),
            "updated_by": "owner@contoso.com",
        }
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "MODEL_BASED_FALLBACK_OK"
    assert calls == [routing_model, first_model]
    assert response["router_attempts"][0]["outcome"] == "failed"
    assert response["router_attempts"][0]["decision_basis"] == "model_based_fallback"
    assert response["router_attempts"][0]["total_tokens"] == 12
    assert response["router_attempts"][0]["latency_ms"] == 19
    assert response["router_attempts"][1]["decision_basis"] == "model_based_fallback"


def test_model_based_router_does_not_persist_provider_error_detail(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]

    class FailedDecisionRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            if request.model_id == routing_model:
                raise HTTPException(
                    status_code=429,
                    detail="provider echoed sensitive request content",
                    headers={"x-request-id": request_id or "decision-request"},
                )
            assert request.model_id == first_model
            return ModelInvocationResponse(
                request_id=request_id or "candidate-request",
                correlation_id=request_id or "candidate-request",
                content="PRIVACY_SAFE_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=17,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = FailedDecisionRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Privacy-safe Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    second_model,
                ),
            },
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    decision = response["router_attempts"][0]
    assert response["content"] == "PRIVACY_SAFE_FALLBACK_OK"
    assert decision["status_code"] == 429
    assert decision["reason"] == "Routing model request failed"
    assert "sensitive request content" not in str(response)


def test_model_based_prompt_truncation_keeps_both_ends() -> None:
    prompt = "BEGIN-" + ("x" * 8_000) + "-END"

    truncated, was_truncated = ModelRouterExecutionService._truncate_decision_prompt(
        prompt
    )

    assert was_truncated is True
    assert len(truncated) == 4_000
    assert truncated.startswith("BEGIN-")
    assert truncated.endswith("-END")
    assert "[middle omitted]" in truncated


def test_republish_rejects_candidates_unavailable_to_existing_assignees(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    user_id = "member@contoso.com"
    repository.user_model_policies[user_id] = {
        "user_id": user_id,
        "model_ids": [first_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Stable assignment", "definition": valid_definition(first_model)},
    )
    router_id = created.json()["id"]
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    assert client.put(
        f"/api/v1/model-routers/{router_id}/assignment",
        json={"user_id": user_id, "assigned": True},
    ).status_code == 200

    assert client.put(
        f"/api/v1/model-routers/{router_id}",
        json={"name": "Stable assignment", "definition": valid_definition(second_model)},
    ).status_code == 200
    rejected = client.post(f"/api/v1/model-routers/{router_id}/publish")
    assigned = client.get(
        "/api/v1/model-routers/assigned", params={"user_id": user_id}
    ).json()["items"]

    assert rejected.status_code == 409
    assert assigned[0]["version"] == 1
    assert assigned[0]["definition"]["default_model_id"] == str(first_model)


def test_system_one_decision_model_routes_via_native_choice_with_probabilities(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    decision_model_id, first_model, second_model = enabled_model_ids(repository)[:3]
    decision_row = next(
        model for model in repository.models if model["id"] == decision_model_id
    )
    decision_row["display_name"] = "Microsoft-Decision-1"
    decision_row["model_key"] = "Microsoft-Decision-1"
    decision_row["capabilities"] = ["decision"]

    definition = described_model_based_definition(
        decision_model_id,
        first_model,
        second_model,
    )
    preview_res = client.post(
        "/api/v1/model-routers/decision-preview",
        json={
            "definition": definition,
            "strategy_node_id": "strategy",
            "sample_prompt": "Prove Dijkstra fails on negative edges",
        },
    )
    assert preview_res.status_code == 200
    preview = preview_res.json()
    assert preview["decision_protocol"] == "system_one"
    system_payload = json.loads(preview["system"])
    assert system_payload["questions"]["route"]["type"] == "choice"
    assert str(first_model) in system_payload["questions"]["route"]["criteria"]
    assert str(second_model) in system_payload["questions"]["route"]["criteria"]

    decision_calls: list[DecisionInvocationRequest] = []
    chat_calls: list[ModelInvocationRequest] = []

    class SystemOneRuntimeService:
        def invoke_decision(
            self,
            request: DecisionInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> DecisionInvocationResponse:
            decision_calls.append(request)
            assert request.model_id == decision_model_id
            assert timeout_ms == 5_000
            return DecisionInvocationResponse(
                request_id=request_id or "sysone-req",
                correlation_id="sysone-corr",
                answers={
                    "route": {
                        "choice": str(second_model),
                        "probabilities": {
                            str(first_model): 0.14,
                            str(second_model): 0.86,
                        },
                    }
                },
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=310,
                usage=InvocationUsage(
                    input_tokens=180,
                    cached_tokens=0,
                    output_tokens=0,
                    estimated=False,
                ),
                estimated_cost=0.000008,
            )

        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            chat_calls.append(request)
            assert request.model_id == second_model
            return ModelInvocationResponse(
                request_id=request_id or "chat-req",
                correlation_id=request_id or "chat-req",
                content="SYSTEM_ONE_ROUTED_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=42,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = SystemOneRuntimeService
    try:
        trial_res = client.post(
            "/api/v1/model-routers/decision-trial",
            json={
                "definition": definition,
                "strategy_node_id": "strategy",
                "sample_prompt": "Prove Dijkstra fails on negative edges",
            },
        )
        assert trial_res.status_code == 200
        trial = trial_res.json()
        assert trial["decision_protocol"] == "system_one"
        assert trial["selected_model_id"] == str(second_model)
        assert trial["confidence"] == 0.86
        assert trial["latency_ms"] == 310
        assert [item["model_id"] for item in trial["probabilities"]] == [
            str(second_model),
            str(first_model),
        ]

        created = client.post(
            "/api/v1/model-routers",
            json={"name": "System One Router", "definition": definition},
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    body = invoked.json()
    assert body["content"] == "SYSTEM_ONE_ROUTED_OK"
    assert body["selected_model_id"] == str(second_model)
    assert len(decision_calls) == 2
    assert len(chat_calls) == 1
    decision_attempt = body["router_attempts"][0]
    assert decision_attempt["decision_protocol"] == "system_one"
    assert decision_attempt["decision_confidence"] == 0.86
    assert decision_attempt["decision_probabilities"] == {
        str(first_model): 0.14,
        str(second_model): 0.86,
    }
    assert "rationale" not in decision_attempt


def test_matches_any_condition_and_reasoning_effort_and_implicit_agent_stickiness(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]

    condition = ModelRouterCondition(
        field="request_parameter",
        operator="matches_any",
        value="证明, 算法, theorem, prove",
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "Please PROVE this graph property"},
        )
        is True
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "请帮我写一段快排算法"},
        )
        is True
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "Translate hello to French"},
        )
        is False
    )

    definition = model_based_definition(routing_model, first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    strategy["decision_reasoning_effort"] = "low"
    seen_requests: list[ModelInvocationRequest] = []

    class ReasoningDecisionRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            seen_requests.append(request)
            if request.model_id == routing_model:
                assert request.reasoning_effort == "low"
                assert request.max_output_tokens == 1_024
                content = json.dumps(
                    {
                        "model_id": str(second_model),
                        "rationale": "Complex agent loop",
                    }
                )
            else:
                content = "AGENT_LOOP_OK"
            return ModelInvocationResponse(
                request_id=request_id or "req",
                correlation_id=request_id or "req",
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=25,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = ReasoningDecisionRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Agent Sticky Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200

        turn1 = invocation_payload(router_id)
        turn1["metadata"]["run_id"] = "ephemeral-turn-1"
        turn1["messages"] = [{"role": "user", "content": "Fix the race condition in queue.ts"}]
        first_res = client.post("/api/v1/model-gateway/invoke", json=turn1)

        turn2 = invocation_payload(router_id)
        turn2["metadata"]["run_id"] = "ephemeral-turn-2"
        turn2["metadata"]["turn_index"] = 2
        turn2["messages"] = [
            {"role": "user", "content": "Fix the race condition in queue.ts"},
            {
                "role": "assistant",
                "content": "Let me read queue.ts first.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"queue.ts"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "export class AsyncQueue {}",
            },
        ]
        second_res = client.post("/api/v1/model-gateway/invoke", json=turn2)
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert first_res.status_code == 200
    assert second_res.status_code == 200
    assert [req.model_id for req in seen_requests] == [
        routing_model,
        second_model,
        second_model,
    ]
    assert second_res.json()["selected_model_id"] == str(second_model)
    assert second_res.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"
