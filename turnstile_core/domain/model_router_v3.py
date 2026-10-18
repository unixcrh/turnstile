from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from .models import StrictModel

RouterOperation = Literal["chat_completions", "responses"]
RouterContextValueType = Literal["string", "integer", "number", "boolean"]
RouterContextSource = Literal[
    "authenticated_identity",
    "server_derived",
    "client_claim",
]
RouterPredicateOperator = Literal[
    "equals",
    "not_equals",
    "contains",
    "in",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "exists",
]
RouterRetryableEvent = Literal[
    "transport_error",
    "timeout",
    "rate_limit",
    "server_error",
    "deployment_unavailable",
]
RouterScalar = str | int | float | bool


class RouterV3Model(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RouterIdentityV3(RouterV3Model):
    alias: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    description: str = Field(max_length=500)
    supported_operations: tuple[RouterOperation, ...] = Field(
        min_length=1,
        max_length=2,
    )
    streaming: Literal["disabled", "allowed", "required"]
    tools: Literal["disabled", "allowed", "required"]


class RouterInputFieldV3(RouterV3Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: RouterContextValueType
    source: RouterContextSource
    registry_version: int = Field(ge=1)
    required: bool


class RouterModelRefV3(RouterV3Model):
    model_id: UUID
    facts_version: int = Field(ge=1)


class RouterCandidatePoolV3(RouterV3Model):
    candidates: tuple[RouterModelRefV3, ...] = Field(min_length=1, max_length=50)


class RouterEligibilityV3(RouterV3Model):
    required_capabilities: tuple[str, ...] = Field(max_length=50)
    minimum_context_window: int | None = Field(ge=1)
    allowed_provider_ids: tuple[UUID, ...] = Field(max_length=50)
    allowed_data_zones: tuple[str, ...] = Field(max_length=50)


class RouterPredicateV3(RouterV3Model):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    operator: RouterPredicateOperator
    value: RouterScalar | tuple[RouterScalar, ...] | None


class RouterSelectModelActionV3(RouterV3Model):
    type: Literal["select_model"]
    model_id: UUID


class RouterExcludeModelsActionV3(RouterV3Model):
    type: Literal["exclude_models"]
    model_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)


class RouterProfileRefV3(RouterV3Model):
    id: UUID
    version: int = Field(ge=1)


class RouterQosWeightsV1(RouterV3Model):
    quality: float = Field(ge=0, le=1)
    cost: float = Field(ge=0, le=1)
    latency: float = Field(ge=0, le=1)
    reliability: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_total(self) -> RouterQosWeightsV1:
        total = self.quality + self.cost + self.latency + self.reliability
        if abs(total - 1.0) > 1e-9:
            raise ValueError("QoS objective weights must total exactly 1")
        return self


class RouterQosProfileConfigV1(RouterV3Model):
    weights: RouterQosWeightsV1
    latency_slo_ms: int = Field(ge=1, le=300_000)
    cost_reference_per_million: float = Field(gt=0)
    quality_floor: float | None = Field(default=None, ge=0, le=1)
    minimum_success_rate: float = Field(ge=0, le=1)


class RouterUseProfileActionV3(RouterV3Model):
    type: Literal["use_profile"]
    profile: RouterProfileRefV3


RouterRuleActionV3 = Annotated[
    RouterSelectModelActionV3
    | RouterExcludeModelsActionV3
    | RouterUseProfileActionV3,
    Field(discriminator="type"),
]


class RouterRuleV3(RouterV3Model):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    predicate: RouterPredicateV3
    action: RouterRuleActionV3


class RouterPrioritySelectorV3(RouterV3Model):
    kind: Literal["priority"]
    model_order: tuple[UUID, ...] = Field(min_length=1, max_length=50)


class RouterWeightedAllocationV3(RouterV3Model):
    model_id: UUID
    basis_points: int = Field(ge=1, le=10_000)


class RouterWeightedSelectorV3(RouterV3Model):
    kind: Literal["weighted_split"]
    allocations: tuple[RouterWeightedAllocationV3, ...] = Field(
        min_length=1,
        max_length=50,
    )


class RouterQosSelectorV3(RouterV3Model):
    kind: Literal["qos"]
    profile: RouterProfileRefV3


class RouterFoundrySemanticSelectorV3(RouterV3Model):
    kind: Literal["foundry_semantic"]
    routing_model: RouterModelRefV3
    mode: Literal["balanced", "cost", "quality"]
    model_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)


class RouterEvaluatedCustomSelectorV3(RouterV3Model):
    kind: Literal["evaluated_custom"]
    routing_model: RouterModelRefV3
    evaluation_id: UUID
    model_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)


RouterSelectorV3 = Annotated[
    RouterPrioritySelectorV3
    | RouterWeightedSelectorV3
    | RouterQosSelectorV3
    | RouterFoundrySemanticSelectorV3
    | RouterEvaluatedCustomSelectorV3,
    Field(discriminator="kind"),
]


class RouterDeploymentRetryV3(RouterV3Model):
    max_attempts_per_request: int = Field(ge=1, le=30)
    retryable_events: tuple[RouterRetryableEvent, ...] = Field(max_length=5)
    respect_retry_after: bool


class RouterCrossModelFallbackV3(RouterV3Model):
    model_order: tuple[UUID, ...] = Field(max_length=50)
    retryable_events: tuple[RouterRetryableEvent, ...] = Field(max_length=5)


class RouterResilienceV3(RouterV3Model):
    equivalent_deployment_retry: RouterDeploymentRetryV3
    cross_model_fallback: RouterCrossModelFallbackV3
    streaming_failover: Literal["disabled", "before_first_byte_only"]


class RouterStickinessV3(RouterV3Model):
    scope: Literal["none", "interaction", "conversation"]
    ttl_seconds: int | None = Field(ge=1, le=31_536_000)

    @model_validator(mode="after")
    def validate_ttl(self) -> RouterStickinessV3:
        if (self.scope == "none") != (self.ttl_seconds is None):
            raise ValueError("ttl_seconds is required exactly when stickiness is enabled")
        return self


class RouterQosPolicyV3(RouterV3Model):
    profile: RouterProfileRefV3
    freshness_seconds: int = Field(ge=1, le=604_800)
    minimum_samples: int = Field(ge=1)
    hysteresis_margin: float = Field(ge=0, le=1)
    missing_data_behavior: Literal["static_priority", "fail_closed"]
    fallback_model_order: tuple[UUID, ...] = Field(max_length=50)


class RouterSpecV3(RouterV3Model):
    schema_version: Literal[3] = 3
    identity: RouterIdentityV3
    input_contract: tuple[RouterInputFieldV3, ...]
    candidate_pool: RouterCandidatePoolV3
    eligibility: RouterEligibilityV3
    rules: tuple[RouterRuleV3, ...]
    selector: RouterSelectorV3
    resilience: RouterResilienceV3
    stickiness: RouterStickinessV3
    qos_policy: RouterQosPolicyV3 | None


class RouterContextFieldRegistrationV1(RouterV3Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: RouterContextValueType
    source: RouterContextSource
    version: int = Field(ge=1)
    enabled: bool


class RouterCompiledContextFieldV1(RouterV3Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: RouterContextValueType
    source: RouterContextSource
    required: bool
    registry_version: int = Field(ge=1)


class RouterModelFactsV1(RouterV3Model):
    model_id: UUID
    facts_version: int = Field(ge=1)
    logical_model_key: str = Field(min_length=1, max_length=200)
    enabled: bool
    supported_operations: tuple[RouterOperation, ...] = Field(max_length=2)
    capabilities: tuple[str, ...] = Field(max_length=50)
    foundry_router_eligible: bool
    context_window: int | None = Field(ge=1)
    provider_id: UUID
    data_zone: str = Field(min_length=1, max_length=100)


class RouterProfileRegistrationV1(RouterV3Model):
    id: UUID
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["qos"]
    config: RouterQosProfileConfigV1
    enabled: bool

class RouterEvaluationRegistrationV1(RouterV3Model):
    id: UUID
    routing_model_id: UUID
    routing_model_facts_version: int = Field(ge=1)
    model_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)
    passed: bool
    enabled: bool


class RouterCompilationIssue(RouterV3Model):
    code: str
    path: str
    message: str


class RouterCompiledPlanBodyV1(RouterV3Model):
    plan_schema_version: Literal[1] = 1
    source_schema_version: Literal[3] = 3
    compiler_version: Literal["1.0.0"] = "1.0.0"
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: RouterIdentityV3
    context_contract: tuple[RouterCompiledContextFieldV1, ...]
    candidates: tuple[RouterModelFactsV1, ...]
    selector_model: RouterModelFactsV1 | None
    profiles: tuple[RouterProfileRegistrationV1, ...]
    eligibility: RouterEligibilityV3
    rules: tuple[RouterRuleV3, ...]
    selector: RouterSelectorV3
    resilience: RouterResilienceV3
    stickiness: RouterStickinessV3
    qos_policy: RouterQosPolicyV3 | None


class RouterCompiledPlanV1(RouterCompiledPlanBodyV1):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelRouterV3Write(RouterV3Model):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=500)
    spec: RouterSpecV3


class ModelRouterV3(RouterV3Model):
    id: UUID
    name: str
    description: str
    status: Literal["draft", "published", "disabled"]
    version: int = Field(ge=0)
    spec: RouterSpecV3
    compiled_plan: RouterCompiledPlanV1 | None
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    created_by: str
    updated_by: str


class ModelRouterV3List(RouterV3Model):
    items: tuple[ModelRouterV3, ...]


class ModelRouterV3ValidationResult(RouterV3Model):
    valid: bool
    issues: tuple[RouterCompilationIssue, ...]
    compiled_plan: RouterCompiledPlanV1 | None