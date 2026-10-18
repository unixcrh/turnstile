from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from turnstile_core.domain.model_router_v3 import (
    ModelRouterV3,
    ModelRouterV3List,
    ModelRouterV3ValidationResult,
    ModelRouterV3Write,
    RouterCompiledPlanV1,
    RouterContextFieldRegistrationV1,
    RouterEvaluatedCustomSelectorV3,
    RouterEvaluationRegistrationV1,
    RouterFoundrySemanticSelectorV3,
    RouterModelFactsV1,
    RouterProfileRegistrationV1,
    RouterQosSelectorV3,
    RouterSpecV3,
    RouterUseProfileActionV3,
)
from turnstile_core.persistence.repository import QueryRepository

from .model_router_compiler import ModelRouterCompilationError, ModelRouterV3Compiler
from .model_router_service import (
    ModelRouterConflictError,
    ModelRouterNotFoundError,
    ModelRouterService,
)


class ModelRouterV3Service:
    def __init__(self, repository: QueryRepository) -> None:
        self._repository = repository

    def list(self, owner_id: UUID) -> ModelRouterV3List:
        return ModelRouterV3List(
            items=tuple(
                ModelRouterV3.model_validate(row)
                for row in self._repository.list_model_routers_v3(owner_id)
            )
        )

    def get(self, router_id: UUID, owner_id: UUID) -> ModelRouterV3:
        row = self._repository.get_model_router_v3(router_id, owner_id)
        if row is None:
            raise ModelRouterNotFoundError("Model router v3 not found")
        return ModelRouterV3.model_validate(row)

    def create(
        self,
        write: ModelRouterV3Write,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouterV3:
        self._assert_models_allowed(write.spec, owner_email, role)
        row = self._repository.create_model_router_v3(
            owner_id,
            owner_email,
            self._values(write, owner_email),
        )
        if row is None:
            raise ModelRouterConflictError(
                "A model router with this name or alias already exists"
            )
        return ModelRouterV3.model_validate(row)

    def update(
        self,
        router_id: UUID,
        write: ModelRouterV3Write,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouterV3:
        self._assert_models_allowed(write.spec, owner_email, role)
        row = self._repository.update_model_router_v3(
            router_id,
            owner_id,
            self._values(write, owner_email),
        )
        if row is None:
            if self._repository.get_model_router_v3(router_id, owner_id) is None:
                raise ModelRouterNotFoundError("Model router v3 not found")
            raise ModelRouterConflictError(
                "Published aliases are immutable and draft names and aliases must be unique"
            )
        return ModelRouterV3.model_validate(row)

    def validate(
        self,
        router_id: UUID,
        owner_id: UUID,
        user_email: str,
        role: str,
    ) -> ModelRouterV3ValidationResult:
        router = self.get(router_id, owner_id)
        self._assert_models_allowed(router.spec, user_email, role)
        try:
            plan = self._compile(router.spec)
        except ModelRouterCompilationError as error:
            return ModelRouterV3ValidationResult(
                valid=False,
                issues=error.issues,
                compiled_plan=None,
            )
        return ModelRouterV3ValidationResult(
            valid=True,
            issues=(),
            compiled_plan=plan,
        )

    def publish(
        self,
        router_id: UUID,
        owner_id: UUID,
        published_by: str,
        role: str,
    ) -> ModelRouterV3:
        validation = self.validate(router_id, owner_id, published_by, role)
        if not validation.valid or validation.compiled_plan is None:
            raise ModelRouterConflictError(
                "Model router v3 compilation failed: "
                + "; ".join(issue.message for issue in validation.issues)
            )
        row = self._repository.publish_model_router_v3(
            router_id,
            owner_id,
            published_by,
            validation.compiled_plan.model_dump(mode="json"),
        )
        if row is None:
            raise ModelRouterNotFoundError("Model router v3 not found")
        return ModelRouterV3.model_validate(row)

    def disable(
        self,
        router_id: UUID,
        owner_id: UUID,
        changed_by: str,
    ) -> ModelRouterV3:
        row = self._repository.disable_model_router_v3(
            router_id,
            owner_id,
            changed_by,
        )
        if row is None:
            if self._repository.get_model_router_v3(router_id, owner_id) is None:
                raise ModelRouterNotFoundError("Model router v3 not found")
            raise ModelRouterConflictError(
                "Only a published model router v3 can be disabled"
            )
        return ModelRouterV3.model_validate(row)

    def remove(self, router_id: UUID, owner_id: UUID) -> None:
        if not self._repository.delete_model_router_v3(router_id, owner_id):
            raise ModelRouterNotFoundError("Model router v3 not found")

    def _compile(self, spec: RouterSpecV3) -> RouterCompiledPlanV1:
        context_references = tuple(
            (field.name, field.registry_version) for field in spec.input_contract
        )
        model_references = {
            (candidate.model_id, candidate.facts_version)
            for candidate in spec.candidate_pool.candidates
        }
        if isinstance(
            spec.selector,
            (RouterFoundrySemanticSelectorV3, RouterEvaluatedCustomSelectorV3),
        ):
            model_references.add(
                (
                    spec.selector.routing_model.model_id,
                    spec.selector.routing_model.facts_version,
                )
            )
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
        evaluation_ids = (
            (spec.selector.evaluation_id,)
            if isinstance(spec.selector, RouterEvaluatedCustomSelectorV3)
            else ()
        )
        context_rows = self._repository.list_model_router_context_fields(
            context_references
        )
        model_rows = self._repository.list_model_router_model_facts(
            tuple(sorted(model_references, key=lambda item: (str(item[0]), item[1])))
        )
        profile_rows = self._repository.list_model_router_profiles(
            tuple(sorted(profile_references, key=lambda item: (str(item[0]), item[1])))
        )
        evaluation_rows = self._repository.list_model_router_evaluations(
            evaluation_ids
        )
        contexts = {
            (field.name, field.version): field
            for row in context_rows
            for field in [RouterContextFieldRegistrationV1.model_validate(row)]
        }
        models = {
            (facts.model_id, facts.facts_version): facts
            for row in model_rows
            for facts in [RouterModelFactsV1.model_validate(row)]
        }
        profiles = {
            (profile.id, profile.version): profile
            for row in profile_rows
            for profile in [RouterProfileRegistrationV1.model_validate(row)]
        }
        evaluations = {
            evaluation.id: evaluation
            for row in evaluation_rows
            for evaluation in [RouterEvaluationRegistrationV1.model_validate(row)]
        }
        return ModelRouterV3Compiler.compile(
            spec,
            model_catalog=models,
            context_registry=contexts,
            profile_registry=profiles,
            evaluation_registry=evaluations,
        )

    def _assert_models_allowed(
        self,
        spec: RouterSpecV3,
        user_email: str,
        role: str,
    ) -> None:
        requested = {
            candidate.model_id for candidate in spec.candidate_pool.candidates
        }
        if isinstance(
            spec.selector,
            (RouterFoundrySemanticSelectorV3, RouterEvaluatedCustomSelectorV3),
        ):
            requested.add(spec.selector.routing_model.model_id)
        available = set(
            ModelRouterService(self._repository).catalog(user_email, role).model_ids
        )
        if requested - available:
            raise ModelRouterConflictError(
                "One or more v3 Router models are disabled or not assigned to this user"
            )

    @staticmethod
    def _values(write: ModelRouterV3Write, changed_by: str) -> Mapping[str, Any]:
        name = write.name.strip()
        if not name:
            raise ModelRouterConflictError("Model router v3 name cannot be blank")
        return {
            "name": name,
            "description": write.description.strip(),
            "spec": write.spec.model_dump(mode="json"),
            "updated_by": changed_by,
        }