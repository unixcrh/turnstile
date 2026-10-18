from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from turnstile_core.domain.model_router_v3 import (
    RouterCompilationIssue,
    RouterCompiledContextFieldV1,
    RouterCompiledPlanBodyV1,
    RouterCompiledPlanV1,
    RouterContextFieldRegistrationV1,
    RouterEligibilityV3,
    RouterEvaluatedCustomSelectorV3,
    RouterEvaluationRegistrationV1,
    RouterExcludeModelsActionV3,
    RouterFoundrySemanticSelectorV3,
    RouterModelFactsV1,
    RouterPrioritySelectorV3,
    RouterProfileRegistrationV1,
    RouterQosSelectorV3,
    RouterResilienceV3,
    RouterRuleV3,
    RouterSelectModelActionV3,
    RouterSpecV3,
    RouterUseProfileActionV3,
    RouterWeightedSelectorV3,
)


class ModelRouterCompilationError(ValueError):
    def __init__(self, issues: Sequence[RouterCompilationIssue]) -> None:
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = tuple(issues)


class ModelRouterV3Compiler:
    @classmethod
    def compile(
        cls,
        spec: RouterSpecV3,
        *,
        model_catalog: Mapping[tuple[UUID, int], RouterModelFactsV1],
        context_registry: Mapping[
            tuple[str, int], RouterContextFieldRegistrationV1
        ],
        profile_registry: Mapping[tuple[UUID, int], RouterProfileRegistrationV1],
        evaluation_registry: Mapping[UUID, RouterEvaluationRegistrationV1],
    ) -> RouterCompiledPlanV1:
        issues: list[RouterCompilationIssue] = []
        candidate_refs = spec.candidate_pool.candidates
        candidate_ids = tuple(candidate.model_id for candidate in candidate_refs)
        candidate_set = set(candidate_ids)
        profile_references = {
            (rule.action.profile.id, rule.action.profile.version)
            for rule in spec.rules
            if isinstance(rule.action, RouterUseProfileActionV3)
        }
        if isinstance(spec.selector, RouterQosSelectorV3):
            profile_references.add(
                (spec.selector.profile.id, spec.selector.profile.version)
            )
        if spec.qos_policy is not None:
            profile_references.add(
                (spec.qos_policy.profile.id, spec.qos_policy.profile.version)
            )
        cls._require_unique(
            candidate_ids,
            "candidate_duplicate",
            "candidate_pool.candidates",
            issues,
        )
        cls._require_unique(
            spec.identity.supported_operations,
            "operation_duplicate",
            "identity.supported_operations",
            issues,
        )
        cls._validate_context(spec, context_registry, issues)
        cls._validate_candidates(spec, model_catalog, issues)
        cls._validate_rules(
            spec.rules,
            candidate_set,
            {field.name: field.registry_version for field in spec.input_contract},
            context_registry,
            profile_registry,
            issues,
        )
        cls._validate_selector(
            spec,
            candidate_set,
            model_catalog,
            profile_registry,
            evaluation_registry,
            issues,
        )
        cls._validate_resilience(spec, candidate_set, issues)
        cls._validate_qos_policy(spec, candidate_set, profile_registry, issues)
        if issues:
            raise ModelRouterCompilationError(issues)

        normalized_identity = spec.identity.model_copy(
            update={
                "supported_operations": tuple(sorted(spec.identity.supported_operations)),
            }
        )
        normalized_eligibility = RouterEligibilityV3(
            required_capabilities=tuple(sorted(cls._effective_capabilities(spec))),
            minimum_context_window=spec.eligibility.minimum_context_window,
            allowed_provider_ids=tuple(
                sorted(set(spec.eligibility.allowed_provider_ids), key=str)
            ),
            allowed_data_zones=tuple(sorted(set(spec.eligibility.allowed_data_zones))),
        )
        normalized_resilience = RouterResilienceV3(
            equivalent_deployment_retry=(
                spec.resilience.equivalent_deployment_retry.model_copy(
                    update={
                        "retryable_events": tuple(
                            sorted(
                                set(
                                    spec.resilience.equivalent_deployment_retry.retryable_events
                                )
                            )
                        )
                    }
                )
            ),
            cross_model_fallback=(
                spec.resilience.cross_model_fallback.model_copy(
                    update={
                        "retryable_events": tuple(
                            sorted(
                                set(spec.resilience.cross_model_fallback.retryable_events)
                            )
                        )
                    }
                )
            ),
            streaming_failover=spec.resilience.streaming_failover,
        )
        spec_sha256 = cls._sha256(spec.model_dump(mode="json"))
        body = RouterCompiledPlanBodyV1(
            spec_sha256=spec_sha256,
            identity=normalized_identity,
            context_contract=tuple(
                sorted(
                    (
                        RouterCompiledContextFieldV1(
                            name=field.name,
                            value_type=field.value_type,
                            source=field.source,
                            required=field.required,
                            registry_version=field.registry_version,
                        )
                        for field in spec.input_contract
                    ),
                    key=lambda field: field.name,
                )
            ),
            candidates=tuple(
                model_catalog[(candidate.model_id, candidate.facts_version)]
                for candidate in sorted(candidate_refs, key=lambda item: str(item.model_id))
            ),
            selector_model=cls._selector_model(spec, model_catalog),
            profiles=tuple(
                profile_registry[reference]
                for reference in sorted(
                    profile_references,
                    key=lambda item: (str(item[0]), item[1]),
                )
            ),
            eligibility=normalized_eligibility,
            rules=spec.rules,
            selector=cls._normalized_selector(spec.selector),
            resilience=normalized_resilience,
            stickiness=spec.stickiness,
            qos_policy=spec.qos_policy,
        )
        plan_sha256 = cls._sha256(body.model_dump(mode="json"))
        return RouterCompiledPlanV1(
            **body.model_dump(mode="python"),
            plan_sha256=plan_sha256,
        )

    @classmethod
    def _validate_context(
        cls,
        spec: RouterSpecV3,
        registry: Mapping[tuple[str, int], RouterContextFieldRegistrationV1],
        issues: list[RouterCompilationIssue],
    ) -> None:
        fields = spec.input_contract
        cls._require_unique(
            (field.name for field in fields),
            "context_field_duplicate",
            "input_contract",
            issues,
        )
        for index, field in enumerate(fields):
            registration = registry.get((field.name, field.registry_version))
            path = f"input_contract.{index}"
            if registration is None or not registration.enabled:
                cls._issue(
                    issues,
                    "context_field_unregistered",
                    path,
                    f"Context field {field.name} is not enabled in the registry",
                )
                continue
            if (
                registration.name != field.name
                or registration.version != field.registry_version
                or field.value_type != registration.value_type
                or field.source != registration.source
            ):
                cls._issue(
                    issues,
                    "context_field_contract_mismatch",
                    path,
                    f"Context field {field.name} does not match its registered type and source",
                )

    @classmethod
    def _validate_candidates(
        cls,
        spec: RouterSpecV3,
        catalog: Mapping[tuple[UUID, int], RouterModelFactsV1],
        issues: list[RouterCompilationIssue],
    ) -> None:
        required_operations = set(spec.identity.supported_operations)
        required_capabilities = set(spec.eligibility.required_capabilities)
        allowed_providers = set(spec.eligibility.allowed_provider_ids)
        allowed_zones = set(spec.eligibility.allowed_data_zones)
        cls._require_unique(
            spec.eligibility.required_capabilities,
            "eligibility_capability_duplicate",
            "eligibility.required_capabilities",
            issues,
        )
        cls._require_unique(
            spec.eligibility.allowed_provider_ids,
            "eligibility_provider_duplicate",
            "eligibility.allowed_provider_ids",
            issues,
        )
        cls._require_unique(
            spec.eligibility.allowed_data_zones,
            "eligibility_data_zone_duplicate",
            "eligibility.allowed_data_zones",
            issues,
        )
        logical_models: list[str] = []
        for index, candidate in enumerate(spec.candidate_pool.candidates):
            model_id = candidate.model_id
            path = f"candidate_pool.candidates.{index}"
            facts = catalog.get((model_id, candidate.facts_version))
            if facts is None or not facts.enabled:
                cls._issue(
                    issues,
                    "candidate_unavailable",
                    path,
                    f"Candidate {model_id} is absent or disabled",
                )
                continue
            if (
                facts.model_id != model_id
                or facts.facts_version != candidate.facts_version
            ):
                cls._issue(
                    issues,
                    "candidate_catalog_identity_mismatch",
                    path,
                    f"Candidate catalog entry {model_id} carries another model identity",
                )
                continue
            cls._require_unique(
                facts.supported_operations,
                "candidate_operation_duplicate",
                f"{path}.supported_operations",
                issues,
            )
            cls._require_unique(
                facts.capabilities,
                "candidate_capability_duplicate",
                f"{path}.capabilities",
                issues,
            )
            logical_models.append(facts.logical_model_key)
            if not required_operations.issubset(facts.supported_operations):
                cls._issue(
                    issues,
                    "candidate_operation_unsupported",
                    path,
                    f"Candidate {model_id} does not support every Router operation",
                )
            if not required_capabilities.issubset(facts.capabilities):
                cls._issue(
                    issues,
                    "candidate_capability_missing",
                    path,
                    f"Candidate {model_id} lacks a required capability",
                )
            if (
                spec.eligibility.minimum_context_window is not None
                and (
                    facts.context_window is None
                    or facts.context_window < spec.eligibility.minimum_context_window
                )
            ):
                cls._issue(
                    issues,
                    "candidate_context_too_small",
                    path,
                    f"Candidate {model_id} does not meet the context-window floor",
                )
            if allowed_providers and facts.provider_id not in allowed_providers:
                cls._issue(
                    issues,
                    "candidate_provider_disallowed",
                    path,
                    f"Candidate {model_id} is outside the allowed provider set",
                )
            if allowed_zones and facts.data_zone not in allowed_zones:
                cls._issue(
                    issues,
                    "candidate_data_zone_disallowed",
                    path,
                    f"Candidate {model_id} is outside the allowed data zones",
                )
        if any(count > 1 for count in Counter(logical_models).values()):
            cls._issue(
                issues,
                "candidate_logical_model_duplicate",
                "candidate_pool.candidates",
                "Equivalent deployments of one logical model belong in an APIM "
                "pool, not the Router candidate pool",
            )

    @classmethod
    def _validate_rules(
        cls,
        rules: Sequence[RouterRuleV3],
        candidate_ids: set[UUID],
        declared_context_fields: Mapping[str, int],
        context_registry: Mapping[
            tuple[str, int], RouterContextFieldRegistrationV1
        ],
        profile_registry: Mapping[tuple[UUID, int], RouterProfileRegistrationV1],
        issues: list[RouterCompilationIssue],
    ) -> None:
        cls._require_unique(
            (rule.id for rule in rules),
            "rule_id_duplicate",
            "rules",
            issues,
        )
        for index, rule in enumerate(rules):
            path = f"rules.{index}"
            registry_version = declared_context_fields.get(rule.predicate.field)
            if registry_version is None:
                cls._issue(
                    issues,
                    "rule_context_undeclared",
                    f"{path}.predicate.field",
                    f"Rule {rule.id} references a field outside the input contract",
                )
                field = None
            else:
                field = context_registry.get(
                    (rule.predicate.field, registry_version)
                )
            if field is None or not field.enabled:
                if registry_version is not None:
                    cls._issue(
                        issues,
                        "rule_context_unregistered",
                        f"{path}.predicate.field",
                        f"Rule {rule.id} references an unregistered context field",
                    )
            else:
                cls._validate_predicate(rule, field, path, issues)
            action = rule.action
            if isinstance(action, RouterSelectModelActionV3):
                cls._require_candidate(action.model_id, candidate_ids, path, issues)
            elif isinstance(action, RouterExcludeModelsActionV3):
                cls._require_unique(
                    action.model_ids,
                    "rule_action_model_duplicate",
                    f"{path}.action.model_ids",
                    issues,
                )
                for model_id in action.model_ids:
                    cls._require_candidate(model_id, candidate_ids, path, issues)
            elif isinstance(action, RouterUseProfileActionV3):
                cls._require_profile(
                    action.profile.id,
                    action.profile.version,
                    "qos",
                    profile_registry,
                    f"{path}.action.profile",
                    issues,
                )

    @classmethod
    def _validate_predicate(
        cls,
        rule: RouterRuleV3,
        field: RouterContextFieldRegistrationV1,
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        operator = rule.predicate.operator
        value = rule.predicate.value
        if operator == "exists":
            valid = value is None
        elif operator == "in":
            valid = (
                isinstance(value, tuple)
                and bool(value)
                and all(cls._value_matches(field.value_type, item) for item in value)
            )
        elif operator == "contains":
            valid = field.value_type == "string" and isinstance(value, str)
        elif operator in {
            "greater_than",
            "greater_than_or_equal",
            "less_than",
            "less_than_or_equal",
        }:
            valid = (
                field.value_type in {"integer", "number"}
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )
        else:
            valid = (
                not isinstance(value, tuple)
                and value is not None
                and cls._value_matches(field.value_type, value)
            )
        if not valid:
            cls._issue(
                issues,
                "rule_predicate_type_mismatch",
                f"{path}.predicate.value",
                f"Rule {rule.id} has a value incompatible with its field and operator",
            )

    @classmethod
    def _validate_selector(
        cls,
        spec: RouterSpecV3,
        candidate_ids: set[UUID],
        model_catalog: Mapping[tuple[UUID, int], RouterModelFactsV1],
        profile_registry: Mapping[tuple[UUID, int], RouterProfileRegistrationV1],
        evaluation_registry: Mapping[UUID, RouterEvaluationRegistrationV1],
        issues: list[RouterCompilationIssue],
    ) -> None:
        selector = spec.selector
        if isinstance(selector, RouterPrioritySelectorV3):
            cls._require_exact_candidates(
                selector.model_order,
                candidate_ids,
                "selector.model_order",
                issues,
            )
        elif isinstance(selector, RouterWeightedSelectorV3):
            allocation_ids = tuple(item.model_id for item in selector.allocations)
            cls._require_exact_candidates(
                allocation_ids,
                candidate_ids,
                "selector.allocations",
                issues,
            )
            if sum(item.basis_points for item in selector.allocations) != 10_000:
                cls._issue(
                    issues,
                    "selector_weight_total_invalid",
                    "selector.allocations",
                    "Weighted split allocations must total exactly 10000 basis points",
                )
        elif isinstance(selector, RouterQosSelectorV3):
            cls._require_profile(
                selector.profile.id,
                selector.profile.version,
                "qos",
                profile_registry,
                "selector.profile",
                issues,
            )
        elif isinstance(selector, RouterFoundrySemanticSelectorV3):
            cls._require_subset_candidates(
                selector.model_ids,
                candidate_ids,
                "selector.model_ids",
                issues,
            )
            routing_model = model_catalog.get(
                (
                    selector.routing_model.model_id,
                    selector.routing_model.facts_version,
                )
            )
            if routing_model is None or not routing_model.enabled:
                cls._issue(
                    issues,
                    "selector_routing_model_unavailable",
                    "selector.routing_model",
                    "Foundry semantic Routing Model is absent or disabled",
                )
            elif (
                routing_model.model_id != selector.routing_model.model_id
                or routing_model.facts_version
                != selector.routing_model.facts_version
            ):
                cls._issue(
                    issues,
                    "selector_routing_model_identity_mismatch",
                    "selector.routing_model",
                    "Foundry semantic Routing Model facts carry another identity",
                )
            elif not set(spec.identity.supported_operations).issubset(
                routing_model.supported_operations
            ):
                cls._issue(
                    issues,
                    "selector_routing_model_operation_unsupported",
                    "selector.routing_model",
                    "Foundry semantic Routing Model does not support every Router operation",
                )
            elif "semantic_routing" not in routing_model.capabilities:
                cls._issue(
                    issues,
                    "selector_routing_model_capability_missing",
                    "selector.routing_model",
                    "Foundry semantic Routing Model lacks semantic_routing capability",
                )
            candidate_facts = {
                candidate.model_id: model_catalog.get(
                    (candidate.model_id, candidate.facts_version)
                )
                for candidate in spec.candidate_pool.candidates
            }
            for model_id in selector.model_ids:
                candidate = candidate_facts.get(model_id)
                if candidate is not None and not candidate.foundry_router_eligible:
                    cls._issue(
                        issues,
                        "selector_candidate_not_foundry_eligible",
                        "selector.model_ids",
                        f"Candidate {model_id} is not eligible for Foundry model routing",
                    )
        elif isinstance(selector, RouterEvaluatedCustomSelectorV3):
            cls._require_subset_candidates(
                selector.model_ids,
                candidate_ids,
                "selector.model_ids",
                issues,
            )
            evaluation = evaluation_registry.get(selector.evaluation_id)
            routing_model = model_catalog.get(
                (
                    selector.routing_model.model_id,
                    selector.routing_model.facts_version,
                )
            )
            if routing_model is None or not routing_model.enabled:
                cls._issue(
                    issues,
                    "selector_routing_model_unavailable",
                    "selector.routing_model",
                    "Custom selector Routing Model is absent or disabled",
                )
            elif (
                routing_model.model_id != selector.routing_model.model_id
                or routing_model.facts_version
                != selector.routing_model.facts_version
            ):
                cls._issue(
                    issues,
                    "selector_routing_model_identity_mismatch",
                    "selector.routing_model",
                    "Custom selector Routing Model facts carry another identity",
                )
            elif not set(spec.identity.supported_operations).issubset(
                routing_model.supported_operations
            ):
                cls._issue(
                    issues,
                    "selector_routing_model_operation_unsupported",
                    "selector.routing_model",
                    "Custom selector Routing Model does not support every Router operation",
                )
            elif "semantic_routing" not in routing_model.capabilities:
                cls._issue(
                    issues,
                    "selector_routing_model_capability_missing",
                    "selector.routing_model",
                    "Custom selector Routing Model lacks semantic_routing capability",
                )
            if (
                evaluation is None
                or evaluation.id != selector.evaluation_id
                or evaluation.routing_model_id != selector.routing_model.model_id
                or evaluation.routing_model_facts_version
                != selector.routing_model.facts_version
                or not evaluation.passed
                or not evaluation.enabled
                or not set(selector.model_ids).issubset(evaluation.model_ids)
                or len(evaluation.model_ids) != len(set(evaluation.model_ids))
            ):
                cls._issue(
                    issues,
                    "selector_evaluation_gate_failed",
                    "selector.evaluation_id",
                    "Custom selector requires a matching passed and enabled evaluation",
                )

    @classmethod
    def _validate_resilience(
        cls,
        spec: RouterSpecV3,
        candidate_ids: set[UUID],
        issues: list[RouterCompilationIssue],
    ) -> None:
        retry = spec.resilience.equivalent_deployment_retry
        fallback = spec.resilience.cross_model_fallback
        cls._require_unique(
            retry.retryable_events,
            "deployment_retry_event_duplicate",
            "resilience.equivalent_deployment_retry.retryable_events",
            issues,
        )
        cls._require_unique(
            fallback.retryable_events,
            "cross_model_event_duplicate",
            "resilience.cross_model_fallback.retryable_events",
            issues,
        )
        cls._require_subset_candidates(
            fallback.model_order,
            candidate_ids,
            "resilience.cross_model_fallback.model_order",
            issues,
        )
        if bool(fallback.model_order) != bool(fallback.retryable_events):
            cls._issue(
                issues,
                "cross_model_fallback_incomplete",
                "resilience.cross_model_fallback",
                "Cross-model fallback requires both an ordered allowlist and retryable events",
            )
        if retry.max_attempts_per_request == 1 and retry.retryable_events:
            cls._issue(
                issues,
                "deployment_retry_events_inert",
                "resilience.equivalent_deployment_retry.retryable_events",
                "Retryable deployment events are inert when max attempts is one",
            )
        if retry.max_attempts_per_request > 1 and not retry.retryable_events:
            cls._issue(
                issues,
                "deployment_retry_events_missing",
                "resilience.equivalent_deployment_retry.retryable_events",
                "Multiple deployment attempts require retryable events",
            )
        if retry.respect_retry_after and "rate_limit" not in retry.retryable_events:
            cls._issue(
                issues,
                "retry_after_inert",
                "resilience.equivalent_deployment_retry.respect_retry_after",
                "Retry-After handling requires rate_limit to be retryable",
            )
        failover_enabled = (
            retry.max_attempts_per_request > 1 and bool(retry.retryable_events)
        ) or bool(fallback.retryable_events)
        if (
            spec.identity.streaming != "disabled"
            and failover_enabled
            and spec.resilience.streaming_failover != "before_first_byte_only"
        ):
            cls._issue(
                issues,
                "streaming_fallback_unsafe",
                "resilience.streaming_failover",
                "Streaming cross-model fallback must be limited to before the first byte",
            )
        if (
            spec.identity.streaming == "disabled"
            and spec.resilience.streaming_failover != "disabled"
        ):
            cls._issue(
                issues,
                "streaming_failover_inert",
                "resilience.streaming_failover",
                "Streaming failover must be disabled when streaming is disabled",
            )

    @classmethod
    def _validate_qos_policy(
        cls,
        spec: RouterSpecV3,
        candidate_ids: set[UUID],
        profile_registry: Mapping[tuple[UUID, int], RouterProfileRegistrationV1],
        issues: list[RouterCompilationIssue],
    ) -> None:
        selector = spec.selector
        policy = spec.qos_policy
        if isinstance(selector, RouterQosSelectorV3):
            if policy is None:
                cls._issue(
                    issues,
                    "qos_policy_missing",
                    "qos_policy",
                    "QoS selection requires an explicit QoS policy",
                )
                return
            if policy.profile != selector.profile:
                cls._issue(
                    issues,
                    "qos_profile_mismatch",
                    "qos_policy.profile",
                    "QoS selector and policy must reference the same profile version",
                )
            cls._require_profile(
                policy.profile.id,
                policy.profile.version,
                "qos",
                profile_registry,
                "qos_policy.profile",
                issues,
            )
            if policy.missing_data_behavior == "static_priority":
                cls._require_exact_candidates(
                    policy.fallback_model_order,
                    candidate_ids,
                    "qos_policy.fallback_model_order",
                    issues,
                )
            elif policy.fallback_model_order:
                cls._issue(
                    issues,
                    "qos_fail_closed_has_fallback",
                    "qos_policy.fallback_model_order",
                    "Fail-closed QoS policy cannot carry a fallback order",
                )
        elif policy is not None:
            cls._issue(
                issues,
                "qos_policy_inert",
                "qos_policy",
                "QoS policy is only valid with the QoS selector",
            )

    @staticmethod
    def _normalized_selector(selector: Any) -> Any:
        if isinstance(selector, RouterWeightedSelectorV3):
            return selector.model_copy(
                update={
                    "allocations": tuple(
                        sorted(selector.allocations, key=lambda item: str(item.model_id))
                    )
                }
            )
        if isinstance(
            selector,
            (RouterFoundrySemanticSelectorV3, RouterEvaluatedCustomSelectorV3),
        ):
            return selector.model_copy(
                update={"model_ids": tuple(sorted(selector.model_ids, key=str))}
            )
        return selector

    @staticmethod
    def _selector_model(
        spec: RouterSpecV3,
        model_catalog: Mapping[tuple[UUID, int], RouterModelFactsV1],
    ) -> RouterModelFactsV1 | None:
        selector = spec.selector
        if isinstance(
            selector,
            (RouterFoundrySemanticSelectorV3, RouterEvaluatedCustomSelectorV3),
        ):
            return model_catalog.get(
                (
                    selector.routing_model.model_id,
                    selector.routing_model.facts_version,
                )
            )
        return None

    @classmethod
    def _require_exact_candidates(
        cls,
        values: Sequence[UUID],
        candidates: set[UUID],
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        cls._require_unique(values, "selector_model_duplicate", path, issues)
        if set(values) != candidates:
            cls._issue(
                issues,
                "selector_candidates_mismatch",
                path,
                "Selector must configure every candidate exactly once",
            )

    @classmethod
    def _require_subset_candidates(
        cls,
        values: Sequence[UUID],
        candidates: set[UUID],
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        cls._require_unique(values, "selector_model_duplicate", path, issues)
        if not set(values).issubset(candidates):
            cls._issue(
                issues,
                "selector_model_outside_pool",
                path,
                "Selector or fallback references a model outside the candidate pool",
            )

    @classmethod
    def _require_candidate(
        cls,
        model_id: UUID,
        candidates: set[UUID],
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        if model_id not in candidates:
            cls._issue(
                issues,
                "rule_action_model_outside_pool",
                f"{path}.action",
                "Rule action references a model outside the candidate pool",
            )

    @classmethod
    def _require_profile(
        cls,
        profile_id: UUID,
        version: int,
        expected_kind: str,
        registry: Mapping[tuple[UUID, int], RouterProfileRegistrationV1],
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        profile = registry.get((profile_id, version))
        if (
            profile is None
            or profile.id != profile_id
            or profile.version != version
            or not profile.enabled
            or profile.kind != expected_kind
        ):
            cls._issue(
                issues,
                "profile_unavailable",
                path,
                f"Enabled {expected_kind} profile {profile_id} version {version} is required",
            )

    @staticmethod
    def _require_unique(
        values: Iterable[Hashable],
        code: str,
        path: str,
        issues: list[RouterCompilationIssue],
    ) -> None:
        materialized = tuple(values)
        if any(count > 1 for count in Counter(materialized).values()):
            ModelRouterV3Compiler._issue(
                issues,
                code,
                path,
                f"{path} cannot contain duplicates",
            )

    @staticmethod
    def _issue(
        issues: list[RouterCompilationIssue],
        code: str,
        path: str,
        message: str,
    ) -> None:
        issues.append(RouterCompilationIssue(code=code, path=path, message=message))

    @staticmethod
    def _sha256(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _effective_capabilities(spec: RouterSpecV3) -> set[str]:
        capabilities = set(spec.eligibility.required_capabilities)
        if spec.identity.tools == "required":
            capabilities.add("tools")
        if spec.identity.streaming == "required":
            capabilities.add("streaming")
        return capabilities

    @staticmethod
    def _value_matches(value_type: str, value: Any) -> bool:
        if value_type == "string":
            return isinstance(value, str)
        if value_type == "boolean":
            return isinstance(value, bool)
        if value_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if value_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return False