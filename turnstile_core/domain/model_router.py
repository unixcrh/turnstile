from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import StrictModel
from .runtime_models import ModelFamilyKey

ModelRouterStatus = Literal["draft", "published", "disabled"]
ModelRouterNodeType = Literal[
    "start",
    "condition",
    "candidate_models",
    "strategy",
    "priority",
    "fallback",
    "output",
]
ModelRouterStrategyType = Literal["priority", "weighted", "model_based"]
ModelRouterDecisionProtocol = Literal["chat_json", "system_one"]
ModelRouterReasoningEffort = Literal["none", "low", "medium", "high"]
ModelRouterConditionOperator = Literal[
    "equals",
    "not_equals",
    "contains",
    "matches_any",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "exists",
]
ModelRouterFallbackEvent = Literal["error", "timeout", "rate_limit", "quality"]


class ModelRouterPosition(StrictModel):
    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)


class ModelRouterCondition(StrictModel):
    field: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Condition lookup key. Identity, prompt, prompt length, and required "
            "capability are derived by the server during execution."
        ),
    )
    operator: ModelRouterConditionOperator
    value: str | float | bool | None = None


class ModelRouterCandidate(StrictModel):
    model_id: UUID


class ModelRouterStrategyModel(StrictModel):
    model_id: UUID
    priority: int = Field(default=1, ge=1, le=100)
    weight: int = Field(default=100, ge=1, le=100)
    max_latency_ms: int | None = Field(
        default=None,
        ge=1,
        le=300_000,
        description=(
            "Legacy field name for the candidate invocation timeout. It may only "
            "tighten the Strategy timeout and is not evaluated after success."
        ),
    )
    max_cost_per_million: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Static ceiling for the larger input/output list rate per million, "
            "not an estimated request-cost budget."
        ),
    )
    description: str = Field(default="", max_length=1_000)


class ModelRouterNode(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    type: ModelRouterNodeType
    label: str = Field(min_length=1, max_length=120)
    position: ModelRouterPosition
    condition: ModelRouterCondition | None = None
    candidates: list[ModelRouterCandidate] = Field(default_factory=list, max_length=50)
    strategy_type: ModelRouterStrategyType | None = None
    model_configs: list[ModelRouterStrategyModel] = Field(
        default_factory=list, max_length=50
    )
    routing_model_id: UUID | None = None
    decision_prompt: str | None = Field(default=None, max_length=20_000)
    max_prompt_chars: int = Field(default=4_000, ge=64, le=50_000)
    decision_reasoning_effort: ModelRouterReasoningEffort | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    fallback_on: list[ModelRouterFallbackEvent] = Field(
        default_factory=list,
        description=(
            "Retry event classes on Fallback nodes. Quality remains readable for "
            "legacy definitions but validation rejects its publication."
        ),
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=1,
        le=300_000,
        description=(
            "Default candidate invocation timeout on Strategy nodes. Legacy "
            "Fallback values migrate here while loading."
        ),
    )


class ModelRouterEdge(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    source: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    target: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    source_handle: str | None = Field(default=None, max_length=64)
    label: str = Field(default="", max_length=120)


class ModelRouterDefinition(StrictModel):
    schema_version: Literal[2] = 2
    presentation_version: int = Field(default=1, ge=1, le=100)
    default_model_id: UUID | None = Field(
        default=None,
        deprecated=True,
        description="Deprecated wire-compatibility field; ignored by routing and validation.",
    )
    nodes: list[ModelRouterNode] = Field(default_factory=list, max_length=100)
    edges: list[ModelRouterEdge] = Field(default_factory=list, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1_strategy_ownership(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        upgraded = deepcopy(value)
        nodes = upgraded.get("nodes") or []
        edges = upgraded.get("edges") or []
        nodes_by_id = {
            str(node.get("id")): node for node in nodes if isinstance(node, dict)
        }
        if upgraded.get("schema_version", 1) == 1:
            for node in nodes:
                if isinstance(node, dict) and node.get("type") == "priority":
                    node["type"] = "strategy"
                    node["strategy_type"] = "priority"
            for candidate_node in nodes:
                if not isinstance(candidate_node, dict):
                    continue
                if candidate_node.get("type") != "candidate_models":
                    continue
                legacy_candidates = candidate_node.get("candidates") or []
                enabled_candidates = [
                    candidate
                    for candidate in legacy_candidates
                    if isinstance(candidate, dict) and candidate.get("enabled", True)
                ]
                candidate_node["candidates"] = [
                    {"model_id": candidate.get("model_id")}
                    for candidate in enabled_candidates
                ]
                strategy = next(
                    (
                        nodes_by_id.get(str(edge.get("target")))
                        for edge in edges
                        if isinstance(edge, dict)
                        and edge.get("source") == candidate_node.get("id")
                        and isinstance(nodes_by_id.get(str(edge.get("target"))), dict)
                        and nodes_by_id[str(edge.get("target"))].get("type")
                        in {"strategy", "priority"}
                    ),
                    None,
                )
                if strategy is None:
                    strategy = next(
                        (
                            node
                            for node in nodes
                            if isinstance(node, dict)
                            and node.get("type") in {"strategy", "priority"}
                        ),
                        None,
                    )
                if strategy is None:
                    continue
                existing = {
                    str(config.get("model_id"))
                    for config in strategy.get("model_configs") or []
                    if isinstance(config, dict)
                }
                configs = list(strategy.get("model_configs") or [])
                for index, candidate in enumerate(enabled_candidates):
                    if str(candidate.get("model_id")) in existing:
                        continue
                    configs.append(
                        {
                            "model_id": candidate.get("model_id"),
                            "priority": candidate.get("priority", index + 1),
                            "weight": candidate.get("weight", 100),
                            "max_latency_ms": candidate.get("max_latency_ms"),
                            "max_cost_per_million": candidate.get(
                                "max_cost_per_million"
                            ),
                            "description": candidate.get("description", ""),
                        }
                    )
                strategy["model_configs"] = configs
        upgraded["schema_version"] = 2
        nodes_by_id = {
            str(node.get("id")): node for node in nodes if isinstance(node, dict)
        }
        for fallback in nodes:
            if (
                not isinstance(fallback, dict)
                or fallback.get("type") != "fallback"
                or fallback.get("timeout_ms") is None
            ):
                continue
            owners = [
                owner
                for edge in edges
                if isinstance(edge, dict)
                and edge.get("target") == fallback.get("id")
                for owner in [nodes_by_id.get(str(edge.get("source")))]
                if isinstance(owner, dict)
                and owner.get("type") in {"strategy", "priority"}
            ]
            if not owners:
                continue
            for strategy in owners:
                if strategy.get("timeout_ms") is None:
                    strategy["timeout_ms"] = fallback["timeout_ms"]
            fallback["timeout_ms"] = None
        return upgraded


class ModelRouterWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    definition: ModelRouterDefinition = Field(default_factory=ModelRouterDefinition)


class ModelRouterDuplicateWrite(StrictModel):
    name: str = Field(min_length=1, max_length=120)


class ModelRouter(StrictModel):
    id: UUID
    name: str
    description: str
    status: ModelRouterStatus
    version: int = Field(ge=0)
    definition: ModelRouterDefinition
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    created_by: str
    updated_by: str


class ModelRouterModelReference(StrictModel):
    id: UUID
    model_key: str
    display_name: str
    provider_name: str
    family_key: ModelFamilyKey = ModelFamilyKey.GENERIC
    status: Literal["available", "disabled", "removed"]


class ModelRouterListResponse(StrictModel):
    items: list[ModelRouter]
    model_references: list[ModelRouterModelReference] = Field(default_factory=list)


class ModelRouterModelCatalog(StrictModel):
    model_ids: list[UUID]


class ModelRouterDecisionPreviewWrite(StrictModel):
    definition: ModelRouterDefinition
    strategy_node_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )
    sample_prompt: str = Field(default="", max_length=200_000)


class ModelRouterDecisionCandidatePreview(StrictModel):
    model_id: UUID
    display_name: str
    provider_name: str
    description: str
    capabilities: list[str]
    context_window: int | None
    input_cost_per_million: float | None
    output_cost_per_million: float | None
    priority: int
    weight: int
    max_latency_ms: int | None
    max_cost_per_million: float | None


class ModelRouterDecisionPreview(StrictModel):
    system: str
    user: str
    sample_truncated: bool
    candidates: list[ModelRouterDecisionCandidatePreview]
    routing_model_id: UUID
    routing_model_name: str
    decision_protocol: ModelRouterDecisionProtocol = "chat_json"
    decision_reasoning_effort: ModelRouterReasoningEffort | None = None
    is_default_prompt: bool
    has_placeholder: bool
    models_without_description: list[UUID]
    chars: int


class ModelRouterDecisionTrialWrite(StrictModel):
    definition: ModelRouterDefinition
    strategy_node_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )
    sample_prompt: str = Field(min_length=1, max_length=200_000)


class ModelRouterDecisionProbability(StrictModel):
    model_id: UUID
    display_name: str
    probability: float = Field(ge=0, le=1)


class ModelRouterDecisionTrialResult(StrictModel):
    routing_model_id: UUID
    routing_model_name: str
    decision_protocol: ModelRouterDecisionProtocol
    selected_model_id: UUID
    selected_model_name: str
    fallback_used: bool = False
    rationale: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: list[ModelRouterDecisionProbability] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    sample_truncated: bool
    decision_input_chars: int = Field(ge=0)


class ModelRouterAssignmentWrite(StrictModel):
    user_id: str = Field(min_length=1, max_length=320)
    assigned: bool


class ModelRouterAssignment(StrictModel):
    router_id: UUID
    user_id: str
    assigned: bool


class ModelRouterDepartmentAccessSummary(StrictModel):
    id: str = Field(min_length=1, max_length=320)
    name: str = Field(min_length=1, max_length=320)
    total_users: int = Field(ge=0)
    assigned_users: int = Field(ge=0)


class ModelRouterPersonAccessItem(StrictModel):
    id: str = Field(min_length=1, max_length=320)
    name: str = Field(min_length=1, max_length=320)
    department_id: str | None = None
    department_name: str = Field(min_length=1, max_length=320)
    assigned: bool


class ModelRouterPeopleAccessResponse(StrictModel):
    router_id: UUID
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    total_users: int = Field(ge=0)
    assigned_count: int = Field(ge=0)
    matching_assigned_count: int = Field(ge=0)
    departments: list[ModelRouterDepartmentAccessSummary] = Field(default_factory=list)
    items: list[ModelRouterPersonAccessItem] = Field(default_factory=list)


class ModelRouterDepartmentAssignmentWrite(StrictModel):
    department_id: str = Field(min_length=1, max_length=320)
    assigned: bool
    query: str | None = Field(default=None, max_length=200)


class ModelRouterBulkAssignmentWrite(StrictModel):
    changes: list[ModelRouterAssignmentWrite] = Field(
        default_factory=list, max_length=1000
    )
    department_assignments: list[ModelRouterDepartmentAssignmentWrite] = Field(
        default_factory=list, max_length=200
    )
    all_matching_assigned: bool | None = None
    department_id: str | None = Field(default=None, max_length=320)
    query: str | None = Field(default=None, max_length=200)


class ModelRouterBulkAssignmentResult(StrictModel):
    router_id: UUID
    updated_count: int = Field(ge=0)
    assigned_count: int = Field(ge=0)


class ModelRouterAssignedList(StrictModel):
    items: list[ModelRouter]


class ModelRouterValidationIssue(StrictModel):
    code: str
    message: str
    node_id: str | None = None


class ModelRouterValidationResult(StrictModel):
    valid: bool
    issues: list[ModelRouterValidationIssue]
