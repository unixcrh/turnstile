from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from turnstile_core.domain.model_router import (
    ModelRouter,
    ModelRouterAssignedList,
    ModelRouterAssignment,
    ModelRouterAssignmentWrite,
    ModelRouterDecisionCandidatePreview,
    ModelRouterDecisionPreview,
    ModelRouterDecisionPreviewWrite,
    ModelRouterDecisionProbability,
    ModelRouterDecisionProtocol,
    ModelRouterDecisionTrialResult,
    ModelRouterDecisionTrialWrite,
    ModelRouterDefinition,
    ModelRouterDuplicateWrite,
    ModelRouterListResponse,
    ModelRouterModelCatalog,
    ModelRouterModelReference,
    ModelRouterNode,
    ModelRouterReasoningEffort,
    ModelRouterStrategyModel,
    ModelRouterValidationIssue,
    ModelRouterValidationResult,
    ModelRouterWrite,
)
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    ChoiceQuestion,
    DecisionInvocationRequest,
    DecisionInvocationResponse,
    InvocationMetadata,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from turnstile_core.persistence.repository import QueryRepository

from .runtime_service import ModelRuntimeService

_USER_REQUEST_RE = re.compile(r"<userRequest>\s*(.*?)\s*</userRequest>", re.DOTALL)
_CATALOG_PLACEHOLDER = "{catalog}"
_DECISION_PROMPT_MAX_CHARS = 4_000
_DECISION_TIMEOUT_MS = 5_000
_DECISION_MAX_OUTPUT_TOKENS = 200
_DECISION_REASONING_MAX_OUTPUT_TOKENS = 1_024
_DEFAULT_DECISION_PROMPT = (
    "You are Turnstile's model routing classifier. Select exactly one model "
    "from the candidate catalog for the user's request. Respect capabilities, "
    "context window, cost, and configured priority.\n\n"
    "Candidate catalog:\n{catalog}\n\n"
    "Return only a JSON object with this shape: "
    '{"model_id":"<candidate UUID>","rationale":"<one short sentence>"}. '
    "Never return a model outside the catalog."
)
_DEFAULT_SYSTEM_ONE_INSTRUCTIONS = (
    "Select the single best candidate model for the user's request based on "
    "each candidate's description, capabilities, context window, and cost/priority trade-offs."
)


@dataclass(frozen=True)
class _RouterExecutionPlan:
    candidates: list[ModelRouterStrategyModel]
    fallback_events: set[str]
    strategy_type: str
    routing_model_id: UUID | None
    attempt_timeout_ms: int | None
    decision_timeout_ms: int
    decision_prompt: str | None
    max_prompt_chars: int
    condition_evaluations: list[dict[str, Any]]
    decision_reasoning_effort: ModelRouterReasoningEffort | None = None


class ModelRouterConflictError(ValueError):
    pass


class ModelRouterNotFoundError(ValueError):
    pass


class _ModelRouterLifecycleService(ABC):
    def __init__(self, repository: QueryRepository) -> None:
        self._repository = repository

    @staticmethod
    @abstractmethod
    def validate_definition(
        definition: ModelRouterDefinition,
        *,
        available_model_ids: set[UUID] | None = None,
    ) -> ModelRouterValidationResult: ...

    @abstractmethod
    def _assert_models_allowed(
        self, definition: ModelRouterDefinition, user_email: str, role: str
    ) -> None: ...

    @abstractmethod
    def _available_model_ids(self, user_email: str, role: str) -> set[UUID]: ...

    @staticmethod
    @abstractmethod
    def _candidate_model_ids(definition: ModelRouterDefinition) -> set[UUID]: ...

    @staticmethod
    @abstractmethod
    def _values(write: ModelRouterWrite, changed_by: str) -> Mapping[str, Any]: ...

    def list(self, owner_id: UUID) -> ModelRouterListResponse:
        items = [
            ModelRouter.model_validate(row)
            for row in self._repository.list_model_routers(owner_id)
        ]
        model_ids = sorted({
            model_id
            for router in items
            for node in router.definition.nodes
            for model_id in (
                *(candidate.model_id for candidate in node.candidates),
                *(config.model_id for config in node.model_configs),
                node.routing_model_id,
            )
            if model_id is not None
        }, key=str)
        return ModelRouterListResponse(
            items=items,
            model_references=[
                ModelRouterModelReference.model_validate(row)
                for row in self._repository.model_router_model_references(model_ids)
            ] if model_ids else [],
        )

    def catalog(self, user_email: str, role: str) -> ModelRouterModelCatalog:
        return ModelRouterModelCatalog(
            model_ids=sorted(
                self._available_model_ids(user_email, role),
                key=str,
            )
        )

    def preview_decision(
        self,
        write: ModelRouterDecisionPreviewWrite,
        user_email: str,
        role: str,
    ) -> ModelRouterDecisionPreview:
        self._assert_models_allowed(write.definition, user_email, role)
        available = self._available_model_ids(user_email, role)
        models = {
            UUID(str(model["id"])): model
            for model in self._repository.registry()["models"]
            if UUID(str(model["id"])) in available
        }
        return ModelRouterExecutionService.decision_preview(write, models)

    def get(self, router_id: UUID, owner_id: UUID) -> ModelRouter:
        row = self._repository.get_model_router(router_id, owner_id)
        if row is None:
            raise ModelRouterNotFoundError("Model router not found")
        return ModelRouter.model_validate(row)

    def create(
        self,
        write: ModelRouterWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        values = self._values(write, owner_email)
        self._assert_models_allowed(write.definition, owner_email, role)
        row = self._repository.create_model_router(owner_id, owner_email, values)
        if row is None:
            raise ModelRouterConflictError("A model router with this name already exists")
        return ModelRouter.model_validate(row)

    def update(
        self,
        router_id: UUID,
        write: ModelRouterWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        values = self._values(write, owner_email)
        self._assert_models_allowed(write.definition, owner_email, role)
        row = self._repository.update_model_router(router_id, owner_id, values)
        if row is None:
            if self._repository.get_model_router(router_id, owner_id) is None:
                raise ModelRouterNotFoundError("Model router not found")
            raise ModelRouterConflictError("A model router with this name already exists")
        return ModelRouter.model_validate(row)

    def duplicate(
        self,
        router_id: UUID,
        write: ModelRouterDuplicateWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        source = self.get(router_id, owner_id)
        duplicate = ModelRouterWrite(
            name=write.name,
            description=source.description,
            definition=source.definition,
        )
        return self.create(duplicate, owner_id, owner_email, role)

    def validate(
        self, router_id: UUID, owner_id: UUID, user_email: str, role: str
    ) -> ModelRouterValidationResult:
        router = self.get(router_id, owner_id)
        return self.validate_definition(
            router.definition,
            available_model_ids=self._available_model_ids(user_email, role),
        )

    def publish(
        self, router_id: UUID, owner_id: UUID, user_email: str, role: str
    ) -> ModelRouter:
        result = self.validate(router_id, owner_id, user_email, role)
        if not result.valid:
            raise ModelRouterConflictError(
                "Model router validation failed: "
                + "; ".join(issue.message for issue in result.issues)
            )
        router = self.get(router_id, owner_id)
        for assignee in self._repository.list_model_router_assignees(router_id):
            self._assert_assignee_models(router.definition, assignee)
        row = self._repository.publish_model_router(router_id, owner_id, user_email)
        if row is None:
            raise ModelRouterNotFoundError("Model router not found")
        return ModelRouter.model_validate(row)

    def disable(
        self, router_id: UUID, owner_id: UUID, user_email: str
    ) -> ModelRouter:
        row = self._repository.disable_model_router(router_id, owner_id, user_email)
        if row is None:
            router = self._repository.get_model_router(router_id, owner_id)
            if router is None:
                raise ModelRouterNotFoundError("Model router not found")
            raise ModelRouterConflictError("Only a published model router can be disabled")
        return ModelRouter.model_validate(row)

    def remove(self, router_id: UUID, owner_id: UUID) -> None:
        if not self._repository.delete_model_router(router_id, owner_id):
            raise ModelRouterNotFoundError("Model router not found")

    def assigned(self, user_id: str) -> ModelRouterAssignedList:
        return ModelRouterAssignedList(
            items=[
                ModelRouter.model_validate(row)
                for row in self._repository.list_assigned_model_routers(user_id)
            ]
        )

    def assignees(self, router_id: UUID, owner_id: UUID) -> Sequence[str]:
        self.get(router_id, owner_id)
        return list(self._repository.list_model_router_assignees(router_id))

    def assign(
        self,
        router_id: UUID,
        write: ModelRouterAssignmentWrite,
        owner_id: UUID,
        changed_by: str,
    ) -> ModelRouterAssignment:
        router = self.get(router_id, owner_id)
        if router.status != "published":
            raise ModelRouterConflictError("Only a published model router can be assigned")
        if write.assigned:
            self._assert_assignee_models(router.definition, write.user_id)
        if not self._repository.set_model_router_assignment(
            router_id, write.user_id, write.assigned, changed_by
        ):
            raise ModelRouterNotFoundError("Published model router not found")
        return ModelRouterAssignment(
            router_id=router_id,
            user_id=write.user_id,
            assigned=write.assigned,
        )

    def _assert_assignee_models(
        self, definition: ModelRouterDefinition, user_id: str
    ) -> None:
        requested = self._candidate_model_ids(definition)
        policy = next(
            (
                row
                for row in self._repository.list_user_model_policies([user_id])
                if str(row["user_id"]) == user_id
            ),
            None,
        )
        assigned = (
            {UUID(str(value)) for value in policy.get("model_ids", [])}
            if policy
            else set()
        )
        registry_models = {
            UUID(str(model["id"])): model for model in self._repository.registry()["models"]
        }
        unavailable = {
            model_id
            for model_id in requested
            if model_id not in registry_models
            or not registry_models[model_id].get("enabled")
            or "image_generation" in (registry_models[model_id].get("capabilities") or [])
            or (
                "decision" not in (registry_models[model_id].get("capabilities") or [])
                and (
                    (policy is not None and model_id not in assigned)
                    or (policy is None and registry_models[model_id].get("assignment_required"))
                )
            )
        }
        if unavailable:
            raise ModelRouterConflictError(
                "The assignee must have access to every assignment-required candidate model"
            )


class ModelRouterExecutionService:
    def __init__(
        self,
        repository: QueryRepository,
        runtime_service: ModelRuntimeService,
    ) -> None:
        self._repository = repository
        self._runtime_service = runtime_service

    def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        if request.router_id is None:
            raise HTTPException(status_code=422, detail="router_id is required")
        router_row = self._repository.get_callable_model_router(
            request.router_id, request.metadata.user_id
        )
        if router_row is None:
            raise HTTPException(
                status_code=403,
                detail="Model router is not published or assigned to this user",
            )
        router = ModelRouter.model_validate(router_row)
        registry = self._repository.registry()
        models = self._callable_models(registry, request.metadata.user_id)
        selection_key = ":".join(
            (
                str(router.id),
                str(router.version),
                request.metadata.user_id,
                request.metadata.run_id,
            )
        )
        try:
            plan = self._build_execution_plan(
                router.definition,
                self._routing_context(request),
                selection_key=selection_key,
            )
        except ModelRouterConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        candidates = list(plan.candidates)
        fallback_events = plan.fallback_events
        strategy_type = plan.strategy_type
        attempts: list[dict[str, Any]] = []
        preferred_basis: str | None = None
        derived_run_id = self._derived_interaction_run_id(request)
        bound_model_id = self._repository.get_model_router_run_binding(
            router.id,
            router.version,
            request.metadata.user_id,
            request.metadata.run_id,
        )
        if (
            bound_model_id is None
            and derived_run_id is not None
            and self._is_agent_continuation(request)
        ):
            bound_model_id = self._repository.get_model_router_run_binding(
                router.id,
                router.version,
                request.metadata.user_id,
                derived_run_id,
            )
        if bound_model_id is not None and bound_model_id not in models:
            disabled = any(
                model["id"] == bound_model_id and model["enabled"] is False
                for model in registry["models"]
            )
            raise HTTPException(status_code=403, detail={
                "code": "model_disabled" if disabled else "model_unavailable",
                "message": "The conversation's bound model is unavailable; its binding is retained",
            })
        if bound_model_id is not None and any(
            candidate.model_id == bound_model_id for candidate in candidates
        ) and bound_model_id in models:
            candidates = [
                *(
                    candidate
                    for candidate in candidates
                    if candidate.model_id == bound_model_id
                ),
                *(
                    candidate
                    for candidate in candidates
                    if candidate.model_id != bound_model_id
                ),
            ]
            preferred_basis = "interaction_sticky"
        elif strategy_type == "model_based":
            candidates, decision_attempt, selected = self._model_based_order(
                request,
                plan,
                models,
            )
            attempts.append(decision_attempt)
            preferred_basis = (
                "model_based_selection" if selected else "model_based_fallback"
            )
        last_error: HTTPException | None = None
        for index, candidate in enumerate(candidates):
            model = models.get(candidate.model_id)
            if model is None or self._is_system_one_model(model):
                attempts.append(
                    self._attempt(
                        len(attempts) + 1,
                        candidate.model_id,
                        "unavailable",
                        priority=candidate.priority,
                        weight=candidate.weight,
                        max_latency_ms=candidate.max_latency_ms,
                        max_cost_per_million=candidate.max_cost_per_million,
                        strategy_type=strategy_type,
                        decision_basis="candidate_unavailable",
                    )
                )
                continue
            if self._over_cost_limit(model, candidate.max_cost_per_million):
                attempts.append(
                    self._attempt(
                        len(attempts) + 1,
                        candidate.model_id,
                        "cost_limit_exceeded",
                        model_name=str(model["display_name"]),
                        priority=candidate.priority,
                        weight=candidate.weight,
                        max_latency_ms=candidate.max_latency_ms,
                        max_cost_per_million=candidate.max_cost_per_million,
                        strategy_type=strategy_type,
                        decision_basis="cost_limit_exceeded",
                    )
                )
                continue
            attempt_request_id = str(uuid4())
            concrete = request.model_copy(
                update={
                    "router_id": None,
                    "runtime_id": UUID(str(model["runtime_id"])),
                    "model_id": candidate.model_id,
                    "metadata": request.metadata.model_copy(
                        update={
                            "model_id": str(candidate.model_id),
                            "model": str(model["model_key"]),
                            "runtime": str(model["runtime_name"]),
                        }
                    ),
                }
            )
            try:
                effective_timeout_ms = candidate.max_latency_ms
                if plan.attempt_timeout_ms is not None:
                    effective_timeout_ms = (
                        min(effective_timeout_ms, plan.attempt_timeout_ms)
                        if effective_timeout_ms is not None
                        else plan.attempt_timeout_ms
                    )
                if effective_timeout_ms is None:
                    response = self._runtime_service.invoke(
                        concrete, request_id=attempt_request_id
                    )
                else:
                    response = self._runtime_service.invoke(
                        concrete,
                        request_id=attempt_request_id,
                        timeout_ms=effective_timeout_ms,
                    )
            except HTTPException as error:
                event = self._failure_event(error.status_code)
                actual_request_id = (
                    error.headers.get("x-request-id", attempt_request_id)
                    if error.headers
                    else attempt_request_id
                )
                attempt = self._attempt(
                    len(attempts) + 1,
                    candidate.model_id,
                    "failed",
                    model_name=str(model["display_name"]),
                    request_id=actual_request_id,
                    status_code=error.status_code,
                    reason=str(error.detail),
                    priority=candidate.priority,
                    weight=candidate.weight,
                    max_latency_ms=candidate.max_latency_ms,
                    max_cost_per_million=candidate.max_cost_per_million,
                    strategy_type=strategy_type,
                    fallback_trigger=event,
                    decision_basis="provider_error",
                )
                attempts.append(attempt)
                self._attach_condition_trace(attempts, plan)
                self._save_attempt_trace(
                    actual_request_id, router, candidate.model_id, model, attempts
                )
                last_error = error
                if bound_model_id is not None and error.status_code in {403, 404, 409}:
                    raise
                if event in fallback_events and index < len(candidates) - 1:
                    continue
                raise
            attempts.append(
                self._attempt(
                    len(attempts) + 1,
                    candidate.model_id,
                    "succeeded",
                    model_name=str(model["display_name"]),
                    request_id=response.request_id,
                    status_code=200,
                    latency_ms=response.latency_ms,
                    priority=candidate.priority,
                    weight=candidate.weight,
                    max_latency_ms=candidate.max_latency_ms,
                    max_cost_per_million=candidate.max_cost_per_million,
                    strategy_type=strategy_type,
                    decision_basis=(
                        "fallback_success"
                        if any(
                            attempt.get("attempt_role") == "candidate"
                            for attempt in attempts
                        )
                        else preferred_basis
                        if preferred_basis is not None
                        else "weighted_selection_success"
                        if strategy_type == "weighted"
                        else "highest_priority_success"
                    ),
                    **self._response_usage(response),
                )
            )
            self._attach_condition_trace(attempts, plan)
            self._save_attempt_trace(
                response.request_id, router, candidate.model_id, model, attempts
            )
            self._repository.save_model_router_run_binding(
                router.id,
                router.version,
                request.metadata.user_id,
                request.metadata.run_id,
                candidate.model_id,
                str(model["display_name"]),
            )
            if derived_run_id is not None and derived_run_id != request.metadata.run_id:
                self._repository.save_model_router_run_binding(
                    router.id,
                    router.version,
                    request.metadata.user_id,
                    derived_run_id,
                    candidate.model_id,
                    str(model["display_name"]),
                )
            return response.model_copy(
                update={
                    "router_id": router.id,
                    "router_name": router.name,
                    "router_version": router.version,
                    "selected_model_id": candidate.model_id,
                    "selected_model_name": str(model["display_name"]),
                    "router_attempts": attempts,
                }
            )
        if last_error is not None:
            raise last_error
        raise HTTPException(status_code=409, detail="Model router has no callable candidates")

    @classmethod
    def decision_preview(
        cls,
        write: ModelRouterDecisionPreviewWrite,
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> ModelRouterDecisionPreview:
        strategy = next(
            (
                node
                for node in write.definition.nodes
                if node.id == write.strategy_node_id and node.type == "strategy"
            ),
            None,
        )
        if strategy is None or strategy.strategy_type != "model_based":
            raise ModelRouterConflictError(
                "Decision preview requires a Model-based Strategy node"
            )
        if strategy.routing_model_id is None:
            raise ModelRouterConflictError("The Strategy has no Routing Model")
        routing_model = models.get(strategy.routing_model_id)
        if routing_model is None:
            raise ModelRouterConflictError("The Routing Model is unavailable")
        nodes = {node.id: node for node in write.definition.nodes}
        candidate_ids = {
            candidate.model_id
            for edge in write.definition.edges
            if edge.target == strategy.id
            for candidate_node in [nodes.get(edge.source)]
            if candidate_node is not None
            and candidate_node.type == "candidate_models"
            for candidate in candidate_node.candidates
        }
        candidates = sorted(
            (
                config
                for config in strategy.model_configs
                if config.model_id in candidate_ids
                and config.model_id in models
                and not cls._is_system_one_model(models[config.model_id])
            ),
            key=lambda config: (config.priority, str(config.model_id)),
        )
        if not candidates:
            raise ModelRouterConflictError(
                "The Strategy has no callable candidate models"
            )
        catalog = cls._decision_catalog(candidates, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if cls._is_system_one_model(routing_model)
            else "chat_json"
        )
        if protocol == "system_one":
            _, system, is_default, has_placeholder = cls._render_system_one_question(
                strategy.decision_prompt,
                catalog,
            )
        else:
            system, is_default, has_placeholder = cls._render_decision_prompt(
                strategy.decision_prompt,
                catalog,
            )
        user, truncated = cls._truncate_decision_prompt(
            write.sample_prompt,
            strategy.max_prompt_chars,
        )
        return ModelRouterDecisionPreview(
            system=system,
            user=user,
            sample_truncated=truncated,
            candidates=[
                ModelRouterDecisionCandidatePreview.model_validate(item)
                for item in catalog
            ],
            routing_model_id=strategy.routing_model_id,
            routing_model_name=str(routing_model["display_name"]),
            decision_protocol=protocol,
            decision_reasoning_effort=strategy.decision_reasoning_effort,
            is_default_prompt=is_default,
            has_placeholder=has_placeholder,
            models_without_description=[
                candidate.model_id
                for candidate in candidates
                if not candidate.description.strip()
            ],
            chars=len(system),
        )

    def trial_decision(
        self,
        write: ModelRouterDecisionTrialWrite,
        user_email: str,
        *,
        available_model_ids: set[UUID],
    ) -> ModelRouterDecisionTrialResult:
        models = {
            UUID(str(model["id"])): model
            for model in self._repository.registry()["models"]
            if UUID(str(model["id"])) in available_model_ids
        }
        strategy = next(
            (
                node
                for node in write.definition.nodes
                if node.id == write.strategy_node_id and node.type == "strategy"
            ),
            None,
        )
        if strategy is None or strategy.strategy_type != "model_based":
            raise ModelRouterConflictError(
                "Decision trial requires a Model-based Strategy node"
            )
        if strategy.routing_model_id is None:
            raise ModelRouterConflictError("The Strategy has no Routing Model")
        routing_model = models.get(strategy.routing_model_id)
        if routing_model is None:
            raise ModelRouterConflictError("The Routing Model is unavailable")
        nodes = {node.id: node for node in write.definition.nodes}
        candidate_ids = {
            candidate.model_id
            for edge in write.definition.edges
            if edge.target == strategy.id
            for candidate_node in [nodes.get(edge.source)]
            if candidate_node is not None
            and candidate_node.type == "candidate_models"
            for candidate in candidate_node.candidates
        }
        eligible = sorted(
            (
                config
                for config in strategy.model_configs
                if config.model_id in candidate_ids
                and config.model_id in models
                and not self._is_system_one_model(models[config.model_id])
                and not self._over_cost_limit(
                    models[config.model_id], config.max_cost_per_million
                )
            ),
            key=lambda config: (config.priority, str(config.model_id)),
        )
        if not eligible:
            raise ModelRouterConflictError(
                "The Strategy has no callable candidate models"
            )
        prompt = self._extract_user_prompt(
            [ChatMessage(role="user", content=write.sample_prompt)]
        )
        decision_input, prompt_truncated = self._truncate_decision_prompt(
            prompt,
            strategy.max_prompt_chars,
        )
        catalog = self._decision_catalog(eligible, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if self._is_system_one_model(routing_model)
            else "chat_json"
        )
        trial_request_id = str(uuid4())
        metadata = InvocationMetadata(
            organization_id="unattributed",
            organization="unattributed",
            department_id="unattributed",
            department="unattributed",
            project_id="unattributed",
            project="unattributed",
            agent_id="model-router-editor",
            agent="Model Router Editor",
            user_id=user_email,
            user=user_email.split("@")[0] or user_email,
            workflow="model-router-decision-trial",
            model_id=str(strategy.routing_model_id),
            model=str(routing_model["model_key"]),
            runtime=str(routing_model["runtime_name"]),
            request_source="model-router-trial",
            run_id=f"trial-{trial_request_id}",
            turn_index=1,
        )
        fallback_used = False
        probabilities_map: dict[str, float] = {}
        confidence: float | None = None
        rationale: str | None = None
        if protocol == "system_one":
            question, _, _, _ = self._render_system_one_question(
                strategy.decision_prompt,
                catalog,
            )
            decision_req = DecisionInvocationRequest(
                metadata=metadata,
                model_id=strategy.routing_model_id,
                runtime_id=UUID(str(routing_model["runtime_id"])),
                state=decision_input,
                questions={"route": question},
            )
            decision_resp = self._invoke_decision_service(
                decision_req,
                request_id=trial_request_id,
                timeout_ms=_DECISION_TIMEOUT_MS,
            )
            usage_info = self._response_usage(decision_resp)
            latency_ms = decision_resp.latency_ms
            try:
                (
                    selected_model_id,
                    rationale,
                    probabilities_map,
                    confidence,
                ) = self._parse_system_one_decision(
                    decision_resp.answers,
                    eligible,
                    models,
                )
            except (TypeError, ValueError) as error:
                selected_model_id = eligible[0].model_id
                fallback_used = True
                rationale = f"Fallback to P1 ({error})"
        else:
            system_prompt, _, _ = self._render_decision_prompt(
                strategy.decision_prompt,
                catalog,
            )
            max_tokens = (
                _DECISION_REASONING_MAX_OUTPUT_TOKENS
                if strategy.decision_reasoning_effort not in {None, "none"}
                else _DECISION_MAX_OUTPUT_TOKENS
            )
            chat_req = ModelInvocationRequest(
                metadata=metadata,
                runtime_id=UUID(str(routing_model["runtime_id"])),
                model_id=strategy.routing_model_id,
                messages=[
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=decision_input),
                ],
                max_output_tokens=max_tokens,
                reasoning_effort=strategy.decision_reasoning_effort,
                response_format="json_object",
                stream=False,
            )
            chat_resp = self._runtime_service.invoke(
                chat_req,
                request_id=trial_request_id,
                timeout_ms=_DECISION_TIMEOUT_MS,
            )
            usage_info = self._response_usage(chat_resp)
            latency_ms = chat_resp.latency_ms
            try:
                (
                    selected_model_id,
                    rationale,
                    probabilities_map,
                    confidence,
                ) = self._parse_model_decision_details(
                    chat_resp.content,
                    eligible,
                    models,
                )
            except (TypeError, ValueError) as error:
                selected_model_id = eligible[0].model_id
                fallback_used = True
                rationale = f"Fallback to P1 ({error})"
        probability_items = [
            ModelRouterDecisionProbability(
                model_id=candidate.model_id,
                display_name=str(models[candidate.model_id]["display_name"]),
                probability=probabilities_map[str(candidate.model_id)],
            )
            for candidate in eligible
            if str(candidate.model_id) in probabilities_map
        ]
        probability_items.sort(key=lambda item: (-item.probability, str(item.model_id)))
        return ModelRouterDecisionTrialResult(
            routing_model_id=strategy.routing_model_id,
            routing_model_name=str(routing_model["display_name"]),
            decision_protocol=protocol,
            selected_model_id=selected_model_id,
            selected_model_name=str(models[selected_model_id]["display_name"]),
            fallback_used=fallback_used,
            rationale=rationale or None,
            confidence=confidence,
            probabilities=probability_items,
            latency_ms=latency_ms,
            input_tokens=usage_info["input_tokens"],
            output_tokens=usage_info["output_tokens"],
            total_tokens=usage_info["total_tokens"],
            estimated_cost=usage_info["estimated_cost"],
            sample_truncated=prompt_truncated,
            decision_input_chars=len(decision_input),
        )

    @staticmethod
    def _decision_catalog(
        candidates: Sequence[ModelRouterStrategyModel],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "model_id": candidate.model_id,
                "display_name": str(models[candidate.model_id]["display_name"]),
                "provider_name": str(models[candidate.model_id]["provider_name"]),
                "description": candidate.description.strip(),
                "capabilities": list(
                    models[candidate.model_id].get("capabilities") or []
                ),
                "context_window": models[candidate.model_id].get("context_window"),
                "input_cost_per_million": models[candidate.model_id].get(
                    "input_cost_per_million"
                ),
                "output_cost_per_million": models[candidate.model_id].get(
                    "output_cost_per_million"
                ),
                "priority": candidate.priority,
                "weight": candidate.weight,
                "max_latency_ms": candidate.max_latency_ms,
                "max_cost_per_million": candidate.max_cost_per_million,
            }
            for candidate in candidates
        ]

    @staticmethod
    def _is_system_one_model(model: Mapping[str, Any] | None) -> bool:
        return bool(model and "decision" in (model.get("capabilities") or []))

    @staticmethod
    def _render_decision_prompt(
        template: str | None,
        catalog: Sequence[Mapping[str, Any]],
    ) -> tuple[str, bool, bool]:
        custom = (template or "").strip()
        effective = custom or _DEFAULT_DECISION_PROMPT
        catalog_text = json.dumps(catalog, ensure_ascii=False, indent=2, default=str)
        has_placeholder = _CATALOG_PLACEHOLDER in effective
        system = (
            effective.replace(_CATALOG_PLACEHOLDER, catalog_text)
            if has_placeholder
            else f"{effective}\n\nCandidate catalog:\n{catalog_text}"
        )
        return system, not custom, has_placeholder

    @staticmethod
    def _render_system_one_question(
        template: str | None,
        catalog: Sequence[Mapping[str, Any]],
    ) -> tuple[ChoiceQuestion, str, bool, bool]:
        custom = (template or "").strip()
        has_placeholder = _CATALOG_PLACEHOLDER in custom
        if has_placeholder:
            custom = custom.replace(_CATALOG_PLACEHOLDER, "").strip()
        instructions = custom or _DEFAULT_SYSTEM_ONE_INSTRUCTIONS
        criteria: dict[str, str] = {}
        for item in catalog:
            model_id = str(item["model_id"])
            display_name = str(item.get("display_name") or model_id)
            provider_name = str(item.get("provider_name") or "")
            description = str(item.get("description") or "").strip()
            capabilities = ", ".join(item.get("capabilities") or ["chat"])
            cost_parts: list[str] = []
            if item.get("input_cost_per_million") is not None:
                cost_parts.append(f"in ${item['input_cost_per_million']}/1M")
            if item.get("output_cost_per_million") is not None:
                cost_parts.append(f"out ${item['output_cost_per_million']}/1M")
            meta = f"{display_name} ({provider_name}, capabilities: {capabilities}"
            if cost_parts:
                meta += f", {', '.join(cost_parts)}"
            meta += f", priority P{item.get('priority', 1)})"
            criteria[model_id] = f"{meta} — {description}" if description else meta
        if len(criteria) == 1:
            criteria["__none__"] = (
                "Placeholder option; never select this option when a candidate model is present."
            )
        question = ChoiceQuestion(
            type="choice",
            instructions=instructions,
            criteria=criteria,
        )
        rendered = json.dumps(
            {"questions": {"route": question.model_dump(mode="json")}},
            ensure_ascii=False,
            indent=2,
        )
        return question, rendered, not custom, has_placeholder

    def _invoke_decision_service(
        self,
        request: DecisionInvocationRequest,
        *,
        request_id: str,
        timeout_ms: int,
    ) -> DecisionInvocationResponse:
        invoke_fn = self._runtime_service.invoke_decision
        try:
            return invoke_fn(
                request,
                request_id=request_id,
                timeout_ms=timeout_ms,
            )
        except TypeError:
            return invoke_fn(request, request_id=request_id)

    def _model_based_order(
        self,
        request: ModelInvocationRequest,
        plan: _RouterExecutionPlan,
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[list[ModelRouterStrategyModel], dict[str, Any], bool]:
        eligible = [
            candidate
            for candidate in plan.candidates
            if candidate.model_id in models
            and not self._is_system_one_model(models[candidate.model_id])
            and not self._over_cost_limit(
                models[candidate.model_id], candidate.max_cost_per_million
            )
        ]
        candidate_ids = [str(candidate.model_id) for candidate in eligible]
        routing_model_id = plan.routing_model_id
        routing_model = models.get(routing_model_id) if routing_model_id else None
        prompt = self._extract_user_prompt(request.messages)
        decision_input, prompt_truncated = self._truncate_decision_prompt(
            prompt,
            plan.max_prompt_chars,
        )
        catalog = self._decision_catalog(eligible, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if self._is_system_one_model(routing_model)
            else "chat_json"
        )
        question: ChoiceQuestion | None = None
        if protocol == "system_one" and eligible:
            question, system_prompt, _, _ = self._render_system_one_question(
                plan.decision_prompt,
                catalog,
            )
        else:
            system_prompt, _, _ = self._render_decision_prompt(
                plan.decision_prompt,
                catalog,
            )
        decision_evidence: dict[str, Any] = {
            "decision_protocol": protocol,
            "decision_system": system_prompt,
            "decision_system_sha256": hashlib.sha256(
                system_prompt.encode()
            ).hexdigest(),
            "decision_input_chars": len(decision_input),
            "decision_input_sha256": hashlib.sha256(
                decision_input.encode()
            ).hexdigest(),
        }
        if routing_model_id is None or routing_model is None or not eligible:
            reason = (
                "Routing model is unavailable"
                if routing_model is None
                else "No callable candidates are available"
            )
            return list(plan.candidates), self._attempt(
                1,
                routing_model_id or UUID(int=0),
                "failed",
                model_name=(
                    str(routing_model["display_name"])
                    if routing_model is not None
                    else "Routing Model"
                ),
                reason=reason,
                strategy_type=plan.strategy_type,
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
            ), False

        decision_request_id = str(uuid4())
        response: ModelInvocationResponse | DecisionInvocationResponse | None = None
        response_text = ""
        probabilities: dict[str, float] = {}
        confidence: float | None = None
        try:
            if protocol == "system_one":
                assert question is not None
                decision_req = DecisionInvocationRequest(
                    metadata=request.metadata.model_copy(
                        update={
                            "model_id": str(routing_model_id),
                            "model": str(routing_model["model_key"]),
                            "runtime": str(routing_model["runtime_name"]),
                            "workflow": "model-router-decision",
                        }
                    ),
                    model_id=routing_model_id,
                    runtime_id=UUID(str(routing_model["runtime_id"])),
                    state=decision_input,
                    questions={"route": question},
                )
                decision_resp = self._invoke_decision_service(
                    decision_req,
                    request_id=decision_request_id,
                    timeout_ms=plan.decision_timeout_ms,
                )
                response = decision_resp
                response_text = json.dumps(
                    decision_resp.answers,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                (
                    selected_model_id,
                    _rationale,
                    probabilities,
                    confidence,
                ) = self._parse_system_one_decision(
                    decision_resp.answers,
                    eligible,
                    models,
                )
            else:
                max_tokens = (
                    _DECISION_REASONING_MAX_OUTPUT_TOKENS
                    if plan.decision_reasoning_effort not in {None, "none"}
                    else _DECISION_MAX_OUTPUT_TOKENS
                )
                decision_request = request.model_copy(
                    update={
                        "router_id": None,
                        "runtime_id": UUID(str(routing_model["runtime_id"])),
                        "model_id": routing_model_id,
                        "router_context": {},
                        "messages": [
                            ChatMessage(role="system", content=system_prompt),
                            ChatMessage(role="user", content=decision_input),
                        ],
                        "tools": None,
                        "temperature": None,
                        "max_output_tokens": max_tokens,
                        "reasoning_effort": plan.decision_reasoning_effort,
                        "response_format": "json_object",
                        "stream": False,
                        "metadata": request.metadata.model_copy(
                            update={
                                "model_id": str(routing_model_id),
                                "model": str(routing_model["model_key"]),
                                "runtime": str(routing_model["runtime_name"]),
                                "workflow": "model-router-decision",
                            }
                        ),
                    }
                )
                chat_resp = self._runtime_service.invoke(
                    decision_request,
                    request_id=decision_request_id,
                    timeout_ms=plan.decision_timeout_ms,
                )
                response = chat_resp
                response_text = chat_resp.content
                (
                    selected_model_id,
                    _rationale,
                    probabilities,
                    confidence,
                ) = self._parse_model_decision_details(
                    chat_resp.content,
                    eligible,
                    models,
                )
        except HTTPException as error:
            actual_request_id = (
                error.headers.get("x-request-id", decision_request_id)
                if error.headers
                else decision_request_id
            )
            attempt = self._attempt(
                1,
                routing_model_id,
                "failed",
                model_name=str(routing_model["display_name"]),
                request_id=actual_request_id,
                status_code=error.status_code,
                reason="Routing model request failed",
                strategy_type=plan.strategy_type,
                fallback_trigger=self._failure_event(error.status_code),
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
            )
            return list(plan.candidates), attempt, False
        except (TypeError, ValueError) as error:
            response_details: dict[str, Any] = {}
            if response is not None:
                response_details = {
                    "latency_ms": response.latency_ms,
                    "decision_response_chars": len(response_text),
                    "decision_response_sha256": hashlib.sha256(
                        response_text.encode()
                    ).hexdigest(),
                    **self._response_usage(response),
                }
            attempt = self._attempt(
                1,
                routing_model_id,
                "failed",
                model_name=str(routing_model["display_name"]),
                request_id=decision_request_id,
                status_code=200,
                reason=str(error),
                strategy_type=plan.strategy_type,
                fallback_trigger="error",
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
                **response_details,
            )
            return list(plan.candidates), attempt, False

        selected = next(
            candidate
            for candidate in eligible
            if candidate.model_id == selected_model_id
        )
        ordered = [
            selected,
            *[
                candidate
                for candidate in plan.candidates
                if candidate.model_id != selected_model_id
            ],
        ]
        attempt = self._attempt(
            1,
            routing_model_id,
            "succeeded",
            model_name=str(routing_model["display_name"]),
            request_id=response.request_id,
            status_code=200,
            latency_ms=response.latency_ms,
            reason="Routing model selected an authorized candidate",
            strategy_type=plan.strategy_type,
            decision_basis="model_based_selection",
            attempt_role="routing_decision",
            decision_candidates=candidate_ids,
            selected_candidate_model_id=selected_model_id,
            prompt_truncated=prompt_truncated,
            decision_response_chars=len(response_text),
            decision_response_sha256=hashlib.sha256(
                response_text.encode()
            ).hexdigest(),
            decision_confidence=confidence,
            decision_probabilities=probabilities or None,
            **decision_evidence,
            **self._response_usage(response),
        )
        return ordered, attempt, True

    def _callable_models(
        self,
        registry: Mapping[str, Sequence[Mapping[str, Any]]],
        user_id: str,
    ) -> dict[UUID, Mapping[str, Any]]:
        providers = {UUID(str(row["id"])): row for row in registry["providers"]}
        runtimes = {UUID(str(row["id"])): row for row in registry["runtimes"]}
        gateways = {UUID(str(row["id"])): row for row in registry["gateways"]}
        policy = next(
            (
                row
                for row in self._repository.list_user_model_policies([user_id])
                if str(row["user_id"]) == user_id
            ),
            None,
        )
        assigned = (
            {UUID(str(value)) for value in policy.get("model_ids", [])}
            if policy is not None
            else None
        )
        callable_models: dict[UUID, Mapping[str, Any]] = {}
        for model in registry["models"]:
            model_id = UUID(str(model["id"]))
            capabilities = model.get("capabilities") or []
            if "image_generation" in capabilities:
                continue
            runtime = runtimes.get(UUID(str(model["runtime_id"])))
            provider = providers.get(UUID(str(model["provider_id"])))
            gateway = None
            if runtime is not None and runtime.get("gateway_profile_id") is not None:
                gateway = gateways.get(UUID(str(runtime["gateway_profile_id"])))
            if not model.get("enabled") or runtime is None or not runtime.get("enabled"):
                continue
            if provider is None or not provider.get("enabled"):
                continue
            if gateway is not None and not gateway.get("enabled"):
                continue
            if "decision" not in capabilities:
                if assigned is not None and model_id not in assigned:
                    continue
                if assigned is None and model.get("assignment_required"):
                    continue
            callable_models[model_id] = model
        return callable_models

    @staticmethod
    def _extract_user_prompt(messages: Sequence[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role != "user":
                continue
            content = message.content or ""
            matches = _USER_REQUEST_RE.findall(content)
            return matches[-1] if matches else content
        return ""

    @staticmethod
    def _extract_first_user_prompt(messages: Sequence[ChatMessage]) -> str:
        for message in messages:
            if message.role != "user":
                continue
            content = message.content or ""
            matches = _USER_REQUEST_RE.findall(content)
            return matches[0] if matches else content
        return ""

    @classmethod
    def _derived_interaction_run_id(
        cls, request: ModelInvocationRequest
    ) -> str | None:
        first_prompt = cls._extract_first_user_prompt(request.messages).strip()
        if not first_prompt:
            return None
        digest = hashlib.sha256(
            f"{request.metadata.user_id}:{first_prompt}".encode()
        ).hexdigest()[:16]
        return f"derived-{digest}"

    @staticmethod
    def _is_agent_continuation(request: ModelInvocationRequest) -> bool:
        if any(message.role in {"assistant", "tool"} for message in request.messages):
            return True
        initiator = str(
            request.router_context.get("x_initiator")
            or request.router_context.get("initiator")
            or ""
        ).strip().casefold()
        return initiator == "agent" and request.metadata.turn_index > 1

    @classmethod
    def _routing_context(
        cls, request: ModelInvocationRequest
    ) -> dict[str, str | float | bool]:
        prompt = cls._extract_user_prompt(request.messages)
        context = dict(request.router_context)
        context["user_id"] = request.metadata.user_id
        context["request_parameter"] = prompt
        context["prompt_length"] = len(prompt)
        context["required_capability"] = "tools" if request.tools else "chat"
        return context

    @staticmethod
    def _truncate_decision_prompt(
        prompt: str,
        max_chars: int = _DECISION_PROMPT_MAX_CHARS,
    ) -> tuple[str, bool]:
        if len(prompt) <= max_chars:
            return prompt, False
        marker = "\n...[middle omitted]...\n"
        remaining = max_chars - len(marker)
        head = remaining // 2
        tail = remaining - head
        return f"{prompt[:head]}{marker}{prompt[-tail:]}", True

    @staticmethod
    def _resolve_candidate_id(
        selected_value: Any,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> UUID | None:
        candidate_ids = {candidate.model_id for candidate in candidates}
        try:
            candidate_uuid = UUID(str(selected_value))
            if candidate_uuid in candidate_ids:
                return candidate_uuid
        except (TypeError, ValueError, AttributeError):
            pass
        alias = str(selected_value or "").strip().casefold()
        if not alias:
            return None
        matches = [
            model_id
            for model_id in candidate_ids
            if alias
            in {
                str(models[model_id].get("display_name") or "").casefold(),
                str(models[model_id].get("model_key") or "").casefold(),
            }
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _extract_candidate_probabilities(
        cls,
        raw_probabilities: Any,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> dict[str, float]:
        if not isinstance(raw_probabilities, Mapping):
            return {}
        probabilities: dict[str, float] = {}
        for key, value in raw_probabilities.items():
            if str(key) == "__none__":
                continue
            resolved = cls._resolve_candidate_id(key, candidates, models)
            if resolved is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric < 0:
                continue
            probabilities[str(resolved)] = round(min(1.0, numeric), 4)
        return probabilities

    @classmethod
    def _parse_model_decision_details(
        cls,
        content: str,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str, dict[str, float], float | None]:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Routing model response must be a JSON object")
        selected_value = payload.get("model_id", payload.get("model"))
        selected_model_id = cls._resolve_candidate_id(
            selected_value,
            candidates,
            models,
        )
        if selected_model_id is None:
            raise ValueError("Routing model returned a model outside the candidate catalog")
        rationale = str(payload.get("rationale") or "").strip()[:500]
        probabilities = cls._extract_candidate_probabilities(
            payload.get("probabilities"),
            candidates,
            models,
        )
        confidence: float | None = None
        raw_confidence = payload.get("confidence")
        if raw_confidence is not None:
            try:
                confidence = round(min(1.0, max(0.0, float(raw_confidence))), 4)
            except (TypeError, ValueError):
                confidence = None
        if confidence is None and str(selected_model_id) in probabilities:
            confidence = probabilities[str(selected_model_id)]
        return selected_model_id, rationale, probabilities, confidence

    @classmethod
    def _parse_model_decision(
        cls,
        content: str,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str]:
        selected_model_id, rationale, _, _ = cls._parse_model_decision_details(
            content,
            candidates,
            models,
        )
        return selected_model_id, rationale

    @classmethod
    def _parse_system_one_decision(
        cls,
        answers: Mapping[str, Any],
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str, dict[str, float], float | None]:
        if not isinstance(answers, Mapping) or not answers:
            raise ValueError("System One decision response contained no answers")
        route_answer = answers.get("route")
        if route_answer is None and len(answers) == 1:
            route_answer = next(iter(answers.values()))
        if route_answer is None:
            raise ValueError("System One decision response missed the route question")
        probabilities: dict[str, float] = {}
        confidence: float | None = None
        rationale = ""
        selected_value: Any = None
        if isinstance(route_answer, str):
            selected_value = route_answer
        elif isinstance(route_answer, Mapping):
            for key in (
                "choice",
                "value",
                "result",
                "answer",
                "selected",
                "model_id",
                "model",
            ):
                if route_answer.get(key) is not None:
                    selected_value = route_answer.get(key)
                    break
            raw_probs = (
                route_answer.get("probabilities")
                or route_answer.get("distribution")
                or route_answer.get("scores")
                or route_answer
            )
            probabilities = cls._extract_candidate_probabilities(
                raw_probs,
                candidates,
                models,
            )
            raw_conf = route_answer.get("confidence", route_answer.get("probability"))
            if raw_conf is not None:
                try:
                    confidence = round(min(1.0, max(0.0, float(raw_conf))), 4)
                except (TypeError, ValueError):
                    confidence = None
            rationale = str(
                route_answer.get("rationale") or route_answer.get("reason") or ""
            ).strip()[:500]
        else:
            raise ValueError("System One route answer must be an object or string")
        selected_model_id = cls._resolve_candidate_id(
            selected_value,
            candidates,
            models,
        )
        if selected_model_id is None and probabilities:
            best_id_str = max(
                probabilities.items(),
                key=lambda item: item[1],
            )[0]
            selected_model_id = cls._resolve_candidate_id(
                best_id_str,
                candidates,
                models,
            )
        if selected_model_id is None:
            raise ValueError("Routing model returned a model outside the candidate catalog")
        if confidence is None and str(selected_model_id) in probabilities:
            confidence = probabilities[str(selected_model_id)]
        if not rationale and confidence is not None:
            rationale = f"System One choice confidence {confidence * 100:.1f}%"
        return selected_model_id, rationale, probabilities, confidence

    @staticmethod
    def _attach_condition_trace(
        attempts: list[dict[str, Any]], plan: _RouterExecutionPlan
    ) -> None:
        if attempts and plan.condition_evaluations:
            attempts[0].setdefault(
                "condition_evaluations",
                list(plan.condition_evaluations),
            )

    def _save_attempt_trace(
        self,
        request_id: str,
        router: ModelRouter,
        model_id: UUID,
        model: Mapping[str, Any],
        attempts: list[dict[str, Any]],
    ) -> None:
        self._repository.save_model_router_invocation(
            request_id,
            router.id,
            router.name,
            router.version,
            model_id,
            str(model["display_name"]),
            attempts,
        )

    @staticmethod
    def _attempt(
        sequence: int,
        model_id: UUID,
        outcome: str,
        *,
        model_name: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
        latency_ms: int | None = None,
        reason: str | None = None,
        priority: int | None = None,
        weight: int | None = None,
        max_latency_ms: int | None = None,
        max_cost_per_million: float | None = None,
        strategy_type: str,
        fallback_trigger: str | None = None,
        decision_basis: str | None = None,
        input_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost: float | None = None,
        attempt_role: str = "candidate",
        decision_protocol: str | None = None,
        decision_candidates: Sequence[str] | None = None,
        selected_candidate_model_id: UUID | None = None,
        prompt_truncated: bool | None = None,
        decision_system: str | None = None,
        decision_system_sha256: str | None = None,
        decision_input_chars: int | None = None,
        decision_input_sha256: str | None = None,
        decision_response_chars: int | None = None,
        decision_response_sha256: str | None = None,
        decision_confidence: float | None = None,
        decision_probabilities: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "attempt_role": attempt_role,
            "model_id": str(model_id),
            "model_name": model_name or str(model_id),
            "request_id": request_id,
            "outcome": outcome,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "reason": reason,
            "priority": priority,
            "weight": weight,
            "max_latency_ms": max_latency_ms,
            "max_cost_per_million": max_cost_per_million,
            "strategy_type": strategy_type,
            "fallback_trigger": fallback_trigger,
            "decision_basis": decision_basis,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost": estimated_cost,
            "decision_protocol": decision_protocol,
            "decision_candidates": list(decision_candidates or []),
            "selected_candidate_model_id": (
                str(selected_candidate_model_id)
                if selected_candidate_model_id is not None
                else None
            ),
            "prompt_truncated": prompt_truncated,
            "decision_system": decision_system,
            "decision_system_sha256": decision_system_sha256,
            "decision_input_chars": decision_input_chars,
            "decision_input_sha256": decision_input_sha256,
            "decision_response_chars": decision_response_chars,
            "decision_response_sha256": decision_response_sha256,
            "decision_confidence": decision_confidence,
            "decision_probabilities": (
                dict(decision_probabilities)
                if decision_probabilities is not None
                else None
            ),
        }

    @staticmethod
    def _response_usage(
        response: ModelInvocationResponse | DecisionInvocationResponse,
    ) -> dict[str, Any]:
        usage = response.usage
        if usage is None:
            return {
                "input_tokens": None,
                "cached_tokens": None,
                "cache_write_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "estimated_cost": response.estimated_cost,
            }
        return {
            "input_tokens": usage.input_tokens,
            "cached_tokens": usage.cached_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.cached_tokens + usage.output_tokens,
            "estimated_cost": response.estimated_cost,
        }

    @classmethod
    def _execution_plan(
        cls,
        definition: ModelRouterDefinition,
        context: Mapping[str, str | float | bool],
        *,
        selection_key: str = "model-router-validation",
    ) -> tuple[list[ModelRouterStrategyModel], set[str], str]:
        plan = cls._build_execution_plan(
            definition,
            context,
            selection_key=selection_key,
        )
        return plan.candidates, plan.fallback_events, plan.strategy_type

    @classmethod
    def _build_execution_plan(
        cls,
        definition: ModelRouterDefinition,
        context: Mapping[str, str | float | bool],
        *,
        selection_key: str,
    ) -> _RouterExecutionPlan:
        nodes = {node.id: node for node in definition.nodes}
        outgoing: dict[str, list[Any]] = {}
        for edge in definition.edges:
            outgoing.setdefault(edge.source, []).append(edge)
        starts = [node for node in definition.nodes if node.type == "start"]
        pending = [starts[0].id] if starts else []
        visited: set[str] = set()
        candidates: list[Any] = []
        candidate_node_ids: set[str] = set()
        reached_strategy_ids: set[str] = set()
        condition_evaluations: list[dict[str, Any]] = []
        while pending:
            node_id = pending.pop(0)
            if node_id in visited or node_id not in nodes:
                continue
            visited.add(node_id)
            node = nodes[node_id]
            if node.type == "candidate_models":
                candidate_node_ids.add(node.id)
                candidates.extend(node.candidates)
            if node.type in {"strategy", "priority"}:
                reached_strategy_ids.add(node.id)
            edges = outgoing.get(node_id, [])
            if (
                node.type == "condition"
                and node.condition is not None
                and len(edges) > 1
            ):
                matched = cls._condition_matches(node.condition, context)
                condition_evaluations.append(
                    {
                        "node_id": node.id,
                        "field": node.condition.field,
                        "operator": node.condition.operator,
                        "matched": matched,
                    }
                )
                edges = [
                    edge
                    for edge in edges
                    if cls._condition_edge_matches(edge.label, matched)
                ]
            pending.extend(edge.target for edge in edges)
        unique_model_ids: list[UUID] = []
        for candidate in candidates:
            if candidate.model_id not in unique_model_ids:
                unique_model_ids.append(candidate.model_id)
        fallback_node_ids: set[str] = set()
        pending_policy_nodes = list(reached_strategy_ids or candidate_node_ids)
        visited_policy_nodes: set[str] = set()
        while pending_policy_nodes:
            source_id = pending_policy_nodes.pop()
            if source_id in visited_policy_nodes:
                continue
            visited_policy_nodes.add(source_id)
            for edge in outgoing.get(source_id, []):
                target = nodes.get(edge.target)
                if target is None:
                    continue
                if target.type == "fallback":
                    fallback_node_ids.add(target.id)
                elif target.type in {"strategy", "priority"}:
                    pending_policy_nodes.append(target.id)
        fallback_events: set[str] = {
            str(event)
            for node_id in fallback_node_ids
            for event in nodes[node_id].fallback_on
            if event in {"error", "timeout", "rate_limit"}
        }
        strategy_type = "priority"
        strategy = None
        if len(reached_strategy_ids) != 1:
            raise ModelRouterConflictError(
                "The selected Router path must reach exactly one Strategy"
            )
        strategy = nodes[next(iter(reached_strategy_ids))]
        strategy_type = strategy.strategy_type or "priority"
        configs_by_model = {
            config.model_id: config
            for config in (strategy.model_configs if strategy is not None else [])
        }
        available = [
            configs_by_model[model_id]
            for model_id in unique_model_ids
            if model_id in configs_by_model
        ]
        ordered = (
            cls._weighted_order(available, selection_key)
            if strategy_type == "weighted"
            else sorted(
                available,
                key=lambda item: (item.priority, -item.weight, str(item.model_id)),
            )
        )
        return _RouterExecutionPlan(
            candidates=ordered,
            fallback_events=fallback_events,
            strategy_type=strategy_type,
            routing_model_id=(strategy.routing_model_id if strategy is not None else None),
            attempt_timeout_ms=strategy.timeout_ms,
            decision_timeout_ms=_DECISION_TIMEOUT_MS,
            decision_prompt=(strategy.decision_prompt if strategy is not None else None),
            max_prompt_chars=(
                strategy.max_prompt_chars
                if strategy is not None
                else _DECISION_PROMPT_MAX_CHARS
            ),
            condition_evaluations=condition_evaluations,
            decision_reasoning_effort=(
                strategy.decision_reasoning_effort if strategy is not None else None
            ),
        )

    @staticmethod
    def _weighted_order(
        candidates: list[ModelRouterStrategyModel], selection_key: str
    ) -> list[ModelRouterStrategyModel]:
        remaining = sorted(candidates, key=lambda item: str(item.model_id))
        ordered: list[ModelRouterStrategyModel] = []
        round_index = 0
        while remaining:
            total_weight = sum(candidate.weight for candidate in remaining)
            digest = hashlib.sha256(
                f"{selection_key}:{round_index}".encode()
            ).digest()
            draw = int.from_bytes(digest[:8], "big") % total_weight
            cumulative = 0
            selected_index = 0
            for index, candidate in enumerate(remaining):
                cumulative += candidate.weight
                if draw < cumulative:
                    selected_index = index
                    break
            ordered.append(remaining.pop(selected_index))
            round_index += 1
        return ordered

    @staticmethod
    def _condition_matches(condition: Any, context: Mapping[str, Any]) -> bool:
        actual = context.get(condition.field)
        expected = condition.value
        if condition.operator == "exists":
            return actual is not None
        if condition.operator == "equals":
            return bool(actual == expected)
        if condition.operator == "not_equals":
            return bool(actual != expected)
        if condition.operator == "contains":
            return expected is not None and str(expected).casefold() in str(
                actual or ""
            ).casefold()
        if condition.operator == "matches_any":
            if expected is None:
                return False
            haystack = str(actual or "").casefold()
            raw_items = (
                expected
                if isinstance(expected, (list, tuple, set))
                else re.split(r"[,;\n|，；]+", str(expected))
            )
            keywords = [
                str(item).strip().casefold()
                for item in raw_items
                if str(item).strip()
            ]
            return bool(keywords) and any(
                keyword in haystack for keyword in keywords
            )
        try:
            if condition.operator == "greater_than":
                return float(str(actual)) > float(str(expected))
            if condition.operator == "greater_than_or_equal":
                return float(str(actual)) >= float(str(expected))
            if condition.operator == "less_than":
                return float(str(actual)) < float(str(expected))
            if condition.operator == "less_than_or_equal":
                return float(str(actual)) <= float(str(expected))
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _condition_edge_matches(label: str, matched: bool) -> bool:
        normalized = label.strip().casefold()
        if not normalized:
            return True
        true_labels = {"match", "matched", "true", "yes", "匹配"}
        false_labels = {"else", "default", "false", "no", "未匹配"}
        if normalized in true_labels:
            return matched
        if normalized in false_labels:
            return not matched
        return matched

    @staticmethod
    def _failure_event(status_code: int) -> str:
        if status_code == 429:
            return "rate_limit"
        if status_code in {408, 504}:
            return "timeout"
        return "error"

    @staticmethod
    def _over_cost_limit(model: Mapping[str, Any], maximum: float | None) -> bool:
        if maximum is None:
            return False
        prices = [
            float(value)
            for value in (
                model.get("input_cost_per_million"),
                model.get("output_cost_per_million"),
            )
            if value is not None
        ]
        return bool(prices) and max(prices) > maximum


class ModelRouterService(_ModelRouterLifecycleService):

    @staticmethod
    def validate_definition(
        definition: ModelRouterDefinition,
        *,
        available_model_ids: set[UUID] | None = None,
    ) -> ModelRouterValidationResult:
        issues: list[ModelRouterValidationIssue] = []
        node_counts = Counter(node.id for node in definition.nodes)
        edge_counts = Counter(edge.id for edge in definition.edges)
        node_ids = set(node_counts)
        duplicate_nodes = sorted(node_id for node_id, count in node_counts.items() if count > 1)
        duplicate_edges = sorted(edge_id for edge_id, count in edge_counts.items() if count > 1)
        for node_id in duplicate_nodes:
            issues.append(ModelRouterValidationIssue(
                code="duplicate_node_id",
                message="节点 ID 必须唯一。",
                node_id=node_id,
            ))
        for edge_id in duplicate_edges:
            issues.append(ModelRouterValidationIssue(
                code="duplicate_edge_id",
                message=f"连线 ID {edge_id} 重复。",
            ))

        starts = [node for node in definition.nodes if node.type == "start"]
        outputs = [node for node in definition.nodes if node.type == "output"]
        if len(starts) != 1:
            issues.append(ModelRouterValidationIssue(
                code="start_count",
                message="路由必须且只能包含一个 Start 节点。",
            ))
        if len(outputs) != 1:
            issues.append(ModelRouterValidationIssue(
                code="output_count",
                message="路由必须且只能包含一个 Output 节点。",
            ))

        selected_model_ids: set[UUID] = set()
        for node in definition.nodes:
            if node.type == "condition" and node.condition is None:
                issues.append(ModelRouterValidationIssue(
                    code="condition_missing",
                    message="Condition 节点缺少判断条件。",
                    node_id=node.id,
                ))
            if node.type == "candidate_models":
                if not node.candidates:
                    issues.append(ModelRouterValidationIssue(
                        code="candidate_empty",
                        message="Candidate Models 节点至少需要一个模型。",
                        node_id=node.id,
                    ))
                selected_model_ids.update(candidate.model_id for candidate in node.candidates)
                candidate_counts = Counter(candidate.model_id for candidate in node.candidates)
                if any(count > 1 for count in candidate_counts.values()):
                    issues.append(ModelRouterValidationIssue(
                        code="candidate_duplicate",
                        message="同一候选池不能重复添加模型。",
                        node_id=node.id,
                    ))
            if node.type == "strategy":
                if node.strategy_type is None:
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_type_missing",
                        message="Strategy 节点必须选择一种策略。",
                        node_id=node.id,
                    ))
                elif (
                    node.strategy_type == "model_based"
                    and node.routing_model_id is None
                ):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_routing_model_missing",
                        message="模型判路由策略必须指定 Routing Model。",
                        node_id=node.id,
                    ))
                elif node.strategy_type == "model_based":
                    assert node.routing_model_id is not None
                    selected_model_ids.add(node.routing_model_id)
                config_counts = Counter(
                    config.model_id for config in node.model_configs
                )
                if any(count > 1 for count in config_counts.values()):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_model_duplicate",
                        message="Strategy 不能重复配置同一个候选模型。",
                        node_id=node.id,
                    ))
                priorities = [
                    config.priority for config in node.model_configs
                ]
                if node.strategy_type == "priority" and len(priorities) != len(
                    set(priorities)
                ):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_priority_duplicate",
                        message="Priority 策略中每个候选模型必须有唯一优先级。",
                        node_id=node.id,
                    ))
            if node.type == "fallback" and not node.fallback_on:
                issues.append(ModelRouterValidationIssue(
                    code="fallback_event_missing",
                    message="Fallback 节点至少需要一个触发条件。",
                    node_id=node.id,
                ))
            if node.type == "fallback" and "quality" in node.fallback_on:
                issues.append(ModelRouterValidationIssue(
                    code="fallback_quality_unsupported",
                    message="质量不足尚无在线评估信号，不能作为 Fallback 条件。",
                    node_id=node.id,
                ))

        if available_model_ids is not None:
            unavailable = selected_model_ids - available_model_ids
            if unavailable:
                issues.append(ModelRouterValidationIssue(
                    code="candidate_unavailable",
                    message="候选池包含已停用或当前用户无权访问的模型。",
                ))

        outgoing: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        incoming: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        for edge in definition.edges:
            if edge.source == edge.target:
                issues.append(ModelRouterValidationIssue(
                    code="self_edge",
                    message="节点不能连接到自身。",
                    node_id=edge.source,
                ))
            if edge.source not in node_ids or edge.target not in node_ids:
                issues.append(ModelRouterValidationIssue(
                    code="edge_endpoint_missing",
                    message="连线引用了不存在的节点。",
                ))
                continue
            outgoing[edge.source].add(edge.target)
            incoming[edge.target].add(edge.source)

        nodes_by_id = {node.id: node for node in definition.nodes}
        for condition_node in (
            node for node in definition.nodes if node.type == "condition"
        ):
            condition_edges = [
                edge
                for edge in definition.edges
                if edge.source == condition_node.id
            ]
            true_labels = {"match", "matched", "true", "yes", "匹配"}
            false_labels = {"else", "default", "false", "no", "未匹配"}
            match_edges = [
                edge
                for edge in condition_edges
                if edge.label.strip().casefold() in true_labels
            ]
            else_edges = [
                edge
                for edge in condition_edges
                if edge.label.strip().casefold() in false_labels
            ]
            if len(match_edges) != 1 or len(else_edges) != 1:
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_incomplete",
                    message="Condition 必须且只能连接一条 Match 和一条 Else 分支。",
                    node_id=condition_node.id,
                ))
            if len(condition_edges) != 2 or len(match_edges) + len(else_edges) != 2:
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_ambiguous",
                    message="Condition 不能包含未命名或额外分支。",
                    node_id=condition_node.id,
                ))
            if (
                len(match_edges) == 1
                and len(else_edges) == 1
                and match_edges[0].target == else_edges[0].target
            ):
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_same_target",
                    message="Condition 的 Match 和 Else 必须指向不同分支。",
                    node_id=condition_node.id,
                ))
        for candidate_node in (
            node for node in definition.nodes if node.type == "candidate_models"
        ):
            strategy_targets = {
                target_id
                for target_id in outgoing[candidate_node.id]
                if nodes_by_id[target_id].type in {"strategy", "priority"}
            }
            if not strategy_targets:
                issues.append(ModelRouterValidationIssue(
                    code="candidate_strategy_missing",
                    message="Candidate Models 必须连接到 Strategy 节点。",
                    node_id=candidate_node.id,
                ))
            elif outgoing[candidate_node.id] - strategy_targets:
                issues.append(ModelRouterValidationIssue(
                    code="candidate_strategy_bypass",
                    message="Candidate Models 不能绕过 Strategy 连接其他节点。",
                    node_id=candidate_node.id,
                ))
            for strategy_id in strategy_targets:
                strategy_model_ids = {
                    config.model_id
                    for config in nodes_by_id[strategy_id].model_configs
                }
                candidate_model_ids = {
                    candidate.model_id for candidate in candidate_node.candidates
                }
                if strategy_model_ids != candidate_model_ids:
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_models_mismatch",
                        message="Strategy 必须配置候选池中的每一个模型，且不能包含池外模型。",
                        node_id=strategy_id,
                    ))

        has_cycle = ModelRouterService._has_cycle(node_ids, outgoing)
        if len(starts) == 1:
            if incoming[starts[0].id]:
                issues.append(ModelRouterValidationIssue(
                    code="start_has_input",
                    message="Start 节点不能有入线。",
                    node_id=starts[0].id,
                ))
            reachable = ModelRouterService._reachable(starts[0].id, outgoing)
            disconnected = node_ids - reachable
            for node_id in sorted(disconnected):
                issues.append(ModelRouterValidationIssue(
                    code="node_disconnected",
                    message="节点未连接到路由入口。",
                    node_id=node_id,
                ))
            if len(outputs) == 1 and outputs[0].id not in reachable:
                issues.append(ModelRouterValidationIssue(
                    code="output_unreachable",
                    message="Start 节点无法到达 Output 节点。",
                    node_id=outputs[0].id,
                ))
            elif len(outputs) == 1:
                can_reach_output = ModelRouterService._reachable(outputs[0].id, incoming)
                terminal_policy_ids = {
                    node.id for node in definition.nodes if node.type == "fallback"
                }
                for node_id in sorted(reachable - can_reach_output - terminal_policy_ids):
                    issues.append(ModelRouterValidationIssue(
                        code="dead_end",
                        message="该节点无法到达 Output 节点。",
                        node_id=node_id,
                    ))
            if not has_cycle:
                outcomes = ModelRouterService._strategy_outcomes(
                    starts[0].id,
                    nodes_by_id,
                    outgoing,
                )
                if any(len(outcome) != 1 for outcome in outcomes):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_path_ambiguous",
                        message="每条可执行路径必须且只能到达一个 Strategy。",
                    ))
        if len(outputs) == 1 and outgoing[outputs[0].id]:
            issues.append(ModelRouterValidationIssue(
                code="output_has_exit",
                message="Output 节点不能有出线。",
                node_id=outputs[0].id,
            ))
        if has_cycle:
            issues.append(ModelRouterValidationIssue(
                code="cycle",
                message="路由流程存在循环依赖。",
            ))
        return ModelRouterValidationResult(valid=not issues, issues=issues)

    @staticmethod
    def _strategy_outcomes(
        start_id: str,
        nodes: Mapping[str, ModelRouterNode],
        outgoing: Mapping[str, set[str]],
    ) -> set[tuple[str, ...]]:
        memo: dict[str, set[tuple[str, ...]]] = {}

        def merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
            if len(left) > 1 or len(right) > 1:
                return ("", "")
            if not left:
                return right
            if not right or left == right:
                return left
            return ("", "")

        def visit(node_id: str) -> set[tuple[str, ...]]:
            if node_id in memo:
                return memo[node_id]
            node = nodes[node_id]
            own = (node.id,) if node.type in {"strategy", "priority"} else ()
            child_outcomes = [
                visit(target_id)
                for target_id in outgoing.get(node_id, set())
                if target_id in nodes
            ]
            if node.type == "condition":
                outcomes = {
                    merge(own, outcome)
                    for alternatives in child_outcomes
                    for outcome in alternatives
                }
                if not child_outcomes:
                    outcomes.add(own)
            else:
                outcomes = {own}
                for alternatives in child_outcomes:
                    outcomes = {
                        merge(current, outcome)
                        for current in outcomes
                        for outcome in alternatives
                    }
            memo[node_id] = outcomes
            return outcomes

        return visit(start_id)

    @staticmethod
    def _reachable(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(adjacency.get(node_id, ()))
        return seen

    @staticmethod
    def _has_cycle(node_ids: Iterable[str], adjacency: Mapping[str, set[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            if any(visit(target) for target in adjacency.get(node_id, ())):
                return True
            visiting.remove(node_id)
            visited.add(node_id)
            return False

        return any(visit(node_id) for node_id in node_ids if node_id not in visited)

    def _assert_models_allowed(
        self, definition: ModelRouterDefinition, user_email: str, role: str
    ) -> None:
        requested = self._candidate_model_ids(definition)
        if requested - self._available_model_ids(user_email, role):
            raise ModelRouterConflictError(
                "One or more candidate models are disabled or not assigned to this user"
            )

    def _available_model_ids(self, user_email: str, role: str) -> set[UUID]:
        registry = self._repository.registry()
        providers = {row["id"]: row for row in registry["providers"]}
        runtimes = {row["id"]: row for row in registry["runtimes"]}
        gateways = {row["id"]: row for row in registry["gateways"]}
        policy = next(
            (
                row
                for row in self._repository.list_user_model_policies([user_email])
                if str(row["user_id"]) == user_email
            ),
            None,
        )
        assigned = {UUID(str(value)) for value in policy.get("model_ids", [])} if policy else None
        available: set[UUID] = set()
        for model in registry["models"]:
            if "image_generation" in (model.get("capabilities") or []):
                continue
            runtime = runtimes.get(model["runtime_id"])
            provider = providers.get(model["provider_id"])
            gateway = gateways.get(runtime.get("gateway_profile_id")) if runtime else None
            if not model.get("enabled") or not runtime or not runtime.get("enabled"):
                continue
            if not provider or not provider.get("enabled"):
                continue
            if gateway is not None and not gateway.get("enabled"):
                continue
            if role not in (model.get("allowed_roles") or []):
                continue
            if role not in (runtime.get("allowed_roles") or []):
                continue
            model_id = UUID(str(model["id"]))
            if "decision" not in (model.get("capabilities") or []):
                if assigned is not None and model_id not in assigned:
                    continue
                if assigned is None and model.get("assignment_required"):
                    continue
            available.add(model_id)
        return available

    @staticmethod
    def _candidate_model_ids(definition: ModelRouterDefinition) -> set[UUID]:
        candidate_ids = {
            candidate.model_id
            for node in definition.nodes
            for candidate in node.candidates
        }
        return candidate_ids | {
            node.routing_model_id
            for node in definition.nodes
            if node.routing_model_id is not None
        }

    @staticmethod
    def _values(write: ModelRouterWrite, changed_by: str) -> Mapping[str, Any]:
        name = write.name.strip()
        if not name:
            raise ModelRouterConflictError("Model router name cannot be blank")
        return {
            "name": name,
            "description": write.description.strip(),
            "definition": write.definition.model_dump(mode="json"),
            "updated_by": changed_by,
        }
