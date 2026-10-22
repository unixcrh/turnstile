from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from turnstile_core.domain.enterprise import (
    configured_invocation_tester,
    enterprise_catalog,
    invocation_tester,
    merge_application_owners,
    merge_observed_users,
)
from turnstile_core.domain.model_router import (
    ModelRouter,
    ModelRouterAssignedList,
    ModelRouterAssignment,
    ModelRouterAssignmentWrite,
    ModelRouterBulkAssignmentResult,
    ModelRouterBulkAssignmentWrite,
    ModelRouterDecisionPreview,
    ModelRouterDecisionPreviewWrite,
    ModelRouterDecisionTrialResult,
    ModelRouterDecisionTrialWrite,
    ModelRouterDuplicateWrite,
    ModelRouterListResponse,
    ModelRouterModelCatalog,
    ModelRouterPeopleAccessResponse,
    ModelRouterValidationResult,
    ModelRouterWrite,
)
from turnstile_core.domain.model_router_v3 import (
    ModelRouterV3,
    ModelRouterV3List,
    ModelRouterV3ValidationResult,
    ModelRouterV3Write,
)

from ..services.model_router_service import ModelRouterConflictError, ModelRouterNotFoundError
from .dependencies import Repository
from .service_dependencies import (
    ModelRouterExecutionServiceDependency,
    ModelRouterServiceDependency,
    ModelRouterV3ServiceDependency,
)
from .session import (
    Config,
    CurrentSession,
    OwnerSession,
    require_allowed_write_origin,
    require_authenticated_session,
)

router = APIRouter(
    dependencies=[
        Depends(require_authenticated_session),
        Depends(require_allowed_write_origin),
    ]
)


@router.get("/api/v1/model-routers", response_model=ModelRouterListResponse)
def list_model_routers(
    service: ModelRouterServiceDependency, identity: CurrentSession
) -> ModelRouterListResponse:
    return service.list(UUID(identity.id))


@router.get(
    "/api/v1/model-routers/catalog",
    response_model=ModelRouterModelCatalog,
)
def get_model_router_catalog(
    service: ModelRouterServiceDependency, identity: CurrentSession
) -> ModelRouterModelCatalog:
    return service.catalog(identity.email, identity.role)


@router.post(
    "/api/v1/model-routers/decision-preview",
    response_model=ModelRouterDecisionPreview,
)
def preview_model_router_decision(
    write: ModelRouterDecisionPreviewWrite,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouterDecisionPreview:
    try:
        return service.preview_decision(write, identity.email, identity.role)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/api/v1/model-routers/decision-trial",
    response_model=ModelRouterDecisionTrialResult,
)
def trial_model_router_decision(
    write: ModelRouterDecisionTrialWrite,
    service: ModelRouterServiceDependency,
    execution: ModelRouterExecutionServiceDependency,
    identity: CurrentSession,
) -> ModelRouterDecisionTrialResult:
    try:
        service._assert_models_allowed(write.definition, identity.email, identity.role)
        available = service._available_model_ids(identity.email, identity.role)
        return execution.trial_decision(
            write,
            identity.email,
            available_model_ids=available,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get(
    "/api/v1/model-routers/assigned",
    response_model=ModelRouterAssignedList,
)
def list_assigned_model_routers(
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
    repository: Repository,
    settings: Config,
    user_id: Annotated[str | None, Query(min_length=1, max_length=320)] = None,
) -> ModelRouterAssignedList:
    if user_id and user_id != identity.email:
        catalog = merge_application_owners(
            merge_observed_users(enterprise_catalog(), repository.observed_users()),
            repository.application_owners(),
        )
        tester = configured_invocation_tester(
            catalog, settings.delegated_invocation_tester_ids, user_id
        ) or invocation_tester(user_id)
        if identity.role != "owner" and tester is None:
            raise HTTPException(
                status_code=403,
                detail="Router assignee must be the signed-in user or a configured tester",
            )
        assignee = user_id
    else:
        assignee = identity.email
    return service.assigned(assignee)


@router.get("/api/v1/model-router-specs", response_model=ModelRouterV3List)
def list_model_routers_v3(
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3List:
    return service.list(UUID(identity.id))


@router.post(
    "/api/v1/model-router-specs",
    response_model=ModelRouterV3,
    status_code=201,
)
def create_model_router_v3(
    write: ModelRouterV3Write,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3:
    try:
        return service.create(
            write,
            UUID(identity.id),
            identity.email,
            identity.role,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get(
    "/api/v1/model-router-specs/{router_id}",
    response_model=ModelRouterV3,
)
def get_model_router_v3(
    router_id: UUID,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3:
    try:
        return service.get(router_id, UUID(identity.id))
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/api/v1/model-router-specs/{router_id}",
    response_model=ModelRouterV3,
)
def update_model_router_v3(
    router_id: UUID,
    write: ModelRouterV3Write,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3:
    try:
        return service.update(
            router_id,
            write,
            UUID(identity.id),
            identity.email,
            identity.role,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/model-router-specs/{router_id}/validate",
    response_model=ModelRouterV3ValidationResult,
)
def validate_model_router_v3(
    router_id: UUID,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3ValidationResult:
    try:
        return service.validate(
            router_id,
            UUID(identity.id),
            identity.email,
            identity.role,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/model-router-specs/{router_id}/publish",
    response_model=ModelRouterV3,
)
def publish_model_router_v3(
    router_id: UUID,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3:
    try:
        return service.publish(
            router_id,
            UUID(identity.id),
            identity.email,
            identity.role,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/model-router-specs/{router_id}/disable",
    response_model=ModelRouterV3,
)
def disable_model_router_v3(
    router_id: UUID,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> ModelRouterV3:
    try:
        return service.disable(router_id, UUID(identity.id), identity.email)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete(
    "/api/v1/model-router-specs/{router_id}",
    status_code=204,
)
def delete_model_router_v3(
    router_id: UUID,
    service: ModelRouterV3ServiceDependency,
    identity: CurrentSession,
) -> None:
    try:
        service.remove(router_id, UUID(identity.id))
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/v1/model-routers", response_model=ModelRouter, status_code=201)
def create_model_router(
    write: ModelRouterWrite,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.create(write, UUID(identity.id), identity.email, identity.role)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/v1/model-routers/{router_id}", response_model=ModelRouter)
def get_model_router(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.get(router_id, UUID(identity.id))
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/api/v1/model-routers/{router_id}", response_model=ModelRouter)
def update_model_router(
    router_id: UUID,
    write: ModelRouterWrite,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.update(router_id, write, UUID(identity.id), identity.email, identity.role)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/model-routers/{router_id}/duplicate",
    response_model=ModelRouter,
    status_code=201,
)
def duplicate_model_router(
    router_id: UUID,
    write: ModelRouterDuplicateWrite,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.duplicate(router_id, write, UUID(identity.id), identity.email, identity.role)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/model-routers/{router_id}/validate",
    response_model=ModelRouterValidationResult,
)
def validate_model_router(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouterValidationResult:
    try:
        return service.validate(router_id, UUID(identity.id), identity.email, identity.role)
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/v1/model-routers/{router_id}/publish", response_model=ModelRouter)
def publish_model_router(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.publish(router_id, UUID(identity.id), identity.email, identity.role)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/v1/model-routers/{router_id}/disable", response_model=ModelRouter)
def disable_model_router(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> ModelRouter:
    try:
        return service.disable(router_id, UUID(identity.id), identity.email)
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/v1/model-routers/{router_id}", status_code=204)
def delete_model_router(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: CurrentSession,
) -> None:
    try:
        service.remove(router_id, UUID(identity.id))
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/api/v1/model-routers/{router_id}/people",
    response_model=ModelRouterPeopleAccessResponse,
)
def list_model_router_people(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: OwnerSession,
    department_id: Annotated[str | None, Query(max_length=320)] = None,
    query: Annotated[str | None, Query(max_length=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ModelRouterPeopleAccessResponse:
    try:
        return service.people(
            router_id,
            UUID(identity.id),
            identity.email,
            identity.name,
            department_id=department_id,
            query=query,
            offset=offset,
            limit=limit,
        )
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/v1/model-routers/{router_id}/assignees", response_model=list[str])
def list_model_router_assignees(
    router_id: UUID,
    service: ModelRouterServiceDependency,
    identity: OwnerSession,
) -> Sequence[str]:
    try:
        return service.assignees(router_id, UUID(identity.id))
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/api/v1/model-routers/{router_id}/assignment",
    response_model=ModelRouterAssignment,
)
def set_model_router_assignment(
    router_id: UUID,
    write: ModelRouterAssignmentWrite,
    service: ModelRouterServiceDependency,
    identity: OwnerSession,
) -> ModelRouterAssignment:
    try:
        return service.assign(
            router_id,
            write,
            UUID(identity.id),
            identity.email,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/api/v1/model-routers/{router_id}/assignments",
    response_model=ModelRouterBulkAssignmentResult,
)
def set_model_router_assignments_bulk(
    router_id: UUID,
    write: ModelRouterBulkAssignmentWrite,
    service: ModelRouterServiceDependency,
    identity: OwnerSession,
) -> ModelRouterBulkAssignmentResult:
    try:
        return service.assign_bulk(
            router_id,
            write,
            UUID(identity.id),
            identity.email,
            identity.name,
        )
    except ModelRouterConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ModelRouterNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
