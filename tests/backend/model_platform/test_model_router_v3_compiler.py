from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.services.model_router_compiler import (
    ModelRouterCompilationError,
    ModelRouterV3Compiler,
)
from turnstile_core.domain.model_router_v3 import (
    RouterContextFieldRegistrationV1,
    RouterEvaluatedCustomSelectorV3,
    RouterEvaluationRegistrationV1,
    RouterFoundrySemanticSelectorV3,
    RouterInputFieldV3,
    RouterModelFactsV1,
    RouterModelRefV3,
    RouterPredicateV3,
    RouterProfileRefV3,
    RouterProfileRegistrationV1,
    RouterQosPolicyV3,
    RouterQosProfileConfigV1,
    RouterQosSelectorV3,
    RouterQosWeightsV1,
    RouterSpecV3,
    RouterWeightedAllocationV3,
    RouterWeightedSelectorV3,
)

MODEL_A = UUID("10000000-0000-4000-8000-000000000001")
MODEL_B = UUID("10000000-0000-4000-8000-000000000002")
PROVIDER = UUID("20000000-0000-4000-8000-000000000001")
QOS_PROFILE = UUID("30000000-0000-4000-8000-000000000001")
ROUTING_MODEL = UUID("10000000-0000-4000-8000-000000000003")
CUSTOM_EVALUATION = UUID("50000000-0000-4000-8000-000000000001")


def context_registry() -> dict[tuple[str, int], RouterContextFieldRegistrationV1]:
    return {
        ("task_type", 3): RouterContextFieldRegistrationV1(
            name="task_type",
            value_type="string",
            source="client_claim",
            version=3,
            enabled=True,
        ),
        ("user_id", 7): RouterContextFieldRegistrationV1(
            name="user_id",
            value_type="string",
            source="authenticated_identity",
            version=7,
            enabled=True,
        ),
        ("prompt_length", 2): RouterContextFieldRegistrationV1(
            name="prompt_length",
            value_type="integer",
            source="server_derived",
            version=2,
            enabled=True,
        ),
    }


def model_facts(
    model_id: UUID,
    logical_model_key: str,
) -> RouterModelFactsV1:
    return RouterModelFactsV1(
        model_id=model_id,
        facts_version=5,
        logical_model_key=logical_model_key,
        enabled=True,
        supported_operations=("chat_completions", "responses"),
        capabilities=("chat", "streaming", "tools"),
        foundry_router_eligible=False,
        context_window=200_000,
        provider_id=PROVIDER,
        data_zone="us",
    )


def catalog() -> dict[tuple[UUID, int], RouterModelFactsV1]:
    return {
        (MODEL_A, 5): model_facts(MODEL_A, "logical-model-a"),
        (MODEL_B, 5): model_facts(MODEL_B, "logical-model-b"),
    }


def profiles() -> dict[tuple[UUID, int], RouterProfileRegistrationV1]:
    return {
        (QOS_PROFILE, 4): RouterProfileRegistrationV1(
            id=QOS_PROFILE,
            version=4,
            name="Balanced enterprise QoS",
            kind="qos",
            config=RouterQosProfileConfigV1(
                weights=RouterQosWeightsV1(
                    quality=0.4,
                    cost=0.2,
                    latency=0.2,
                    reliability=0.2,
                ),
                latency_slo_ms=2_000,
                cost_reference_per_million=10.0,
                quality_floor=0.7,
                minimum_success_rate=0.99,
            ),
            enabled=True,
        )
    }


def priority_spec() -> RouterSpecV3:
    return RouterSpecV3.model_validate(
        {
            "schema_version": 3,
            "identity": {
                "alias": "enterprise-router",
                "description": "Routes approved enterprise requests.",
                "supported_operations": ["responses", "chat_completions"],
                "streaming": "allowed",
                "tools": "allowed",
            },
            "input_contract": [
                {
                    "name": "task_type",
                    "value_type": "string",
                    "source": "client_claim",
                    "registry_version": 3,
                    "required": False,
                },
                {
                    "name": "user_id",
                    "value_type": "string",
                    "source": "authenticated_identity",
                    "registry_version": 7,
                    "required": True,
                },
            ],
            "candidate_pool": {
                "candidates": [
                    {"model_id": MODEL_B, "facts_version": 5},
                    {"model_id": MODEL_A, "facts_version": 5},
                ]
            },
            "eligibility": {
                "required_capabilities": ["chat"],
                "minimum_context_window": 128_000,
                "allowed_provider_ids": [PROVIDER],
                "allowed_data_zones": ["us"],
            },
            "rules": [
                {
                    "id": "critical-task",
                    "predicate": {
                        "field": "task_type",
                        "operator": "equals",
                        "value": "critical",
                    },
                    "action": {"type": "select_model", "model_id": MODEL_B},
                }
            ],
            "selector": {
                "kind": "priority",
                "model_order": [MODEL_A, MODEL_B],
            },
            "resilience": {
                "equivalent_deployment_retry": {
                    "max_attempts_per_request": 2,
                    "retryable_events": ["server_error", "rate_limit"],
                    "respect_retry_after": True,
                },
                "cross_model_fallback": {
                    "model_order": [MODEL_B],
                    "retryable_events": ["timeout"],
                },
                "streaming_failover": "before_first_byte_only",
            },
            "stickiness": {"scope": "interaction", "ttl_seconds": 3_600},
            "qos_policy": None,
        }
    )


def issue_codes(error: ModelRouterCompilationError) -> set[str]:
    return {issue.code for issue in error.issues}


def test_v3_qos_profile_weights_must_be_normalized() -> None:
    with pytest.raises(ValidationError, match="weights must total exactly 1"):
        RouterQosWeightsV1(
            quality=0.5,
            cost=0.5,
            latency=0.5,
            reliability=0.5,
        )


def test_v3_compiler_produces_stable_immutable_registry_snapshot() -> None:
    spec = priority_spec()
    models = catalog()
    contexts = context_registry()

    first = ModelRouterV3Compiler.compile(
        spec,
        model_catalog=models,
        context_registry=contexts,
        profile_registry=profiles(),
        evaluation_registry={},
    )
    second = ModelRouterV3Compiler.compile(
        spec,
        model_catalog=dict(reversed(tuple(models.items()))),
        context_registry=dict(reversed(tuple(contexts.items()))),
        profile_registry=profiles(),
        evaluation_registry={},
    )

    assert first == second
    assert first.plan_sha256 == second.plan_sha256
    assert len(first.plan_sha256) == 64
    assert first.identity.supported_operations == (
        "chat_completions",
        "responses",
    )
    assert [field.name for field in first.context_contract] == ["task_type", "user_id"]
    user_field = next(field for field in first.context_contract if field.name == "user_id")
    assert user_field.required is True
    assert user_field.source == "authenticated_identity"
    assert user_field.registry_version == 7
    assert [candidate.model_id for candidate in first.candidates] == [MODEL_A, MODEL_B]
    assert first.profiles == ()
    with pytest.raises(ValidationError, match="frozen_instance"):
        first.identity.alias = "changed"


def test_v3_compiler_rejects_undeclared_and_mistyped_rule_context() -> None:
    spec = priority_spec()
    rule = spec.rules[0].model_copy(
        update={
            "predicate": RouterPredicateV3(
                field="prompt_length",
                operator="contains",
                value="large",
            )
        }
    )
    invalid = spec.model_copy(update={"rules": (rule,)})

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            invalid,
            model_catalog=catalog(),
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )

    assert "rule_context_undeclared" in issue_codes(caught.value)
    assert "rule_context_unregistered" not in issue_codes(caught.value)

    declared = spec.model_copy(
        update={
            "input_contract": (
                *spec.input_contract,
                RouterInputFieldV3(
                    name="prompt_length",
                    value_type="integer",
                    source="server_derived",
                    registry_version=2,
                    required=True,
                ),
            ),
            "rules": (rule,),
        }
    )
    with pytest.raises(ModelRouterCompilationError) as mistyped:
        ModelRouterV3Compiler.compile(
            declared,
            model_catalog=catalog(),
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )
    assert "rule_predicate_type_mismatch" in issue_codes(mistyped.value)


def test_v3_compiler_rejects_equivalent_deployments_and_incompatible_models() -> None:
    models = catalog()
    models[(MODEL_B, 5)] = models[(MODEL_B, 5)].model_copy(
        update={
            "logical_model_key": "logical-model-a",
            "supported_operations": ("chat_completions",),
            "context_window": 64_000,
        }
    )

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            priority_spec(),
            model_catalog=models,
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )

    assert issue_codes(caught.value) >= {
        "candidate_logical_model_duplicate",
        "candidate_operation_unsupported",
        "candidate_context_too_small",
    }


def test_v3_compiler_rejects_invalid_weight_and_inert_qos_policy() -> None:
    spec = priority_spec()
    weighted = RouterWeightedSelectorV3(
        kind="weighted_split",
        allocations=(
            RouterWeightedAllocationV3(model_id=MODEL_A, basis_points=5_000),
            RouterWeightedAllocationV3(model_id=MODEL_B, basis_points=4_000),
        ),
    )
    qos_policy = RouterQosPolicyV3(
        profile=RouterProfileRefV3(id=QOS_PROFILE, version=4),
        freshness_seconds=300,
        minimum_samples=20,
        hysteresis_margin=0.05,
        missing_data_behavior="static_priority",
        fallback_model_order=(MODEL_A, MODEL_B),
    )
    invalid = spec.model_copy(
        update={"selector": weighted, "qos_policy": qos_policy}
    )

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            invalid,
            model_catalog=catalog(),
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )

    assert issue_codes(caught.value) >= {
        "selector_weight_total_invalid",
        "qos_policy_inert",
    }


def test_v3_compiler_requires_versioned_qos_profile_and_safe_missing_data() -> None:
    spec = priority_spec()
    qos_selector = RouterQosSelectorV3(
        kind="qos",
        profile=RouterProfileRefV3(id=QOS_PROFILE, version=4),
    )
    qos_policy = RouterQosPolicyV3(
        profile=RouterProfileRefV3(id=QOS_PROFILE, version=4),
        freshness_seconds=300,
        minimum_samples=20,
        hysteresis_margin=0.05,
        missing_data_behavior="static_priority",
        fallback_model_order=(MODEL_A, MODEL_B),
    )
    valid = spec.model_copy(
        update={"selector": qos_selector, "qos_policy": qos_policy}
    )

    plan = ModelRouterV3Compiler.compile(
        valid,
        model_catalog=catalog(),
        context_registry=context_registry(),
        profile_registry=profiles(),
        evaluation_registry={},
    )

    assert isinstance(plan.selector, RouterQosSelectorV3)
    assert plan.qos_policy == qos_policy
    assert [profile.version for profile in plan.profiles] == [4]

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            valid,
            model_catalog=catalog(),
            context_registry=context_registry(),
            profile_registry={},
            evaluation_registry={},
        )
    assert "profile_unavailable" in issue_codes(caught.value)


def test_v3_compiler_rejects_inert_or_unsafe_resilience_values() -> None:
    spec = priority_spec()
    retry = spec.resilience.equivalent_deployment_retry.model_copy(
        update={"max_attempts_per_request": 1, "retryable_events": ("server_error",)}
    )
    fallback = spec.resilience.cross_model_fallback.model_copy(
        update={"model_order": (), "retryable_events": ("timeout",)}
    )
    resilience = spec.resilience.model_copy(
        update={
            "equivalent_deployment_retry": retry,
            "cross_model_fallback": fallback,
            "streaming_failover": "disabled",
        }
    )
    invalid = spec.model_copy(update={"resilience": resilience})

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            invalid,
            model_catalog=catalog(),
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )

    assert issue_codes(caught.value) >= {
        "cross_model_fallback_incomplete",
        "deployment_retry_events_inert",
        "streaming_fallback_unsafe",
    }


def test_v3_foundry_semantic_requires_routing_capability_and_candidate_eligibility() -> None:
    spec = priority_spec()
    selector = RouterFoundrySemanticSelectorV3(
        kind="foundry_semantic",
        routing_model=RouterModelRefV3(
            model_id=ROUTING_MODEL,
            facts_version=2,
        ),
        mode="balanced",
        model_ids=(MODEL_A, MODEL_B),
    )
    semantic_spec = spec.model_copy(update={"selector": selector})
    models = catalog()
    models[(ROUTING_MODEL, 2)] = model_facts(
        ROUTING_MODEL,
        "foundry-model-router",
    ).model_copy(update={"facts_version": 2})

    with pytest.raises(ModelRouterCompilationError) as caught:
        ModelRouterV3Compiler.compile(
            semantic_spec,
            model_catalog=models,
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )

    assert issue_codes(caught.value) >= {
        "selector_routing_model_capability_missing",
        "selector_candidate_not_foundry_eligible",
    }

    models = {
        key: facts.model_copy(update={"foundry_router_eligible": True})
        for key, facts in models.items()
    }
    models[(ROUTING_MODEL, 2)] = models[(ROUTING_MODEL, 2)].model_copy(
        update={
            "capabilities": (*models[(ROUTING_MODEL, 2)].capabilities, "semantic_routing")
        }
    )
    plan = ModelRouterV3Compiler.compile(
        semantic_spec,
        model_catalog=models,
        context_registry=context_registry(),
        profile_registry=profiles(),
        evaluation_registry={},
    )
    assert isinstance(plan.selector, RouterFoundrySemanticSelectorV3)


def test_v3_custom_selector_requires_matching_passed_evaluation() -> None:
    spec = priority_spec()
    selector = RouterEvaluatedCustomSelectorV3(
        kind="evaluated_custom",
        routing_model=RouterModelRefV3(
            model_id=ROUTING_MODEL,
            facts_version=2,
        ),
        evaluation_id=CUSTOM_EVALUATION,
        model_ids=(MODEL_A, MODEL_B),
    )
    custom_spec = spec.model_copy(update={"selector": selector})
    models = catalog()
    models[(ROUTING_MODEL, 2)] = model_facts(
        ROUTING_MODEL,
        "custom-routing-model",
    ).model_copy(
        update={
            "facts_version": 2,
            "capabilities": ("chat", "streaming", "tools", "semantic_routing"),
        }
    )

    with pytest.raises(ModelRouterCompilationError) as missing:
        ModelRouterV3Compiler.compile(
            custom_spec,
            model_catalog=models,
            context_registry=context_registry(),
            profile_registry=profiles(),
            evaluation_registry={},
        )
    assert "selector_evaluation_gate_failed" in issue_codes(missing.value)

    evaluation = RouterEvaluationRegistrationV1(
        id=CUSTOM_EVALUATION,
        routing_model_id=ROUTING_MODEL,
        routing_model_facts_version=2,
        model_ids=(MODEL_A, MODEL_B),
        passed=True,
        enabled=True,
    )
    plan = ModelRouterV3Compiler.compile(
        custom_spec,
        model_catalog=models,
        context_registry=context_registry(),
        profile_registry=profiles(),
        evaluation_registry={CUSTOM_EVALUATION: evaluation},
    )
    assert isinstance(plan.selector, RouterEvaluatedCustomSelectorV3)
    assert plan.selector_model is not None
    assert plan.selector_model.model_id == ROUTING_MODEL