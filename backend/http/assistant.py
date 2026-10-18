from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from turnstile_core.domain.assistant_models import (
    AssistantAskRequest,
    AssistantReply,
    AssistantSettings,
    AssistantSettingsWrite,
    Conversation,
    ConversationList,
    ConversationRename,
    ConversationSummary,
    ConversationTitleRequest,
    PinnedChartOrder,
    PinnedChartRename,
    PinnedChartWrite,
    PinnedReport,
    PinnedReportLayout,
    PinnedReportList,
    PinnedReportVisibilityWrite,
)
from turnstile_core.domain.model_router import ModelRouter, ModelRouterWrite

from ..services.model_router_service import ModelRouterConflictError
from .service_dependencies import AssistantServiceDependency, ModelRouterServiceDependency
from .session import (
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


title_router = APIRouter()


@router.post("/api/v1/assistant/ask", response_model=AssistantReply)
def ask_assistant(
    request: AssistantAskRequest,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> AssistantReply:
    return service.ask(
        request,
        user_id=identity.email,
        user_name=identity.name or identity.email,
        user_role=identity.role,
    )


@router.post(
    "/api/v1/assistant/conversations/{conversation_id}/router-proposals/{proposal_id}/confirm",
    response_model=ModelRouter,
    status_code=201,
)
def confirm_assistant_router_proposal(
    conversation_id: UUID,
    proposal_id: UUID,
    response: Response,
    assistant: AssistantServiceDependency,
    router_service: ModelRouterServiceDependency,
    identity: OwnerSession,
) -> ModelRouter:
    proposal = assistant.router_proposal(conversation_id, proposal_id, identity.email)
    owner_id = UUID(identity.id)
    write = ModelRouterWrite(
        name=proposal.name.strip(),
        description=proposal.description.strip(),
        definition=proposal.definition,
    )
    try:
        return router_service.create(
            write,
            owner_id,
            identity.email,
            identity.role,
        )
    except ModelRouterConflictError as error:
        validation = router_service.validate_definition(
            write.definition,
            available_model_ids=set(
                router_service.catalog(identity.email, identity.role).model_ids
            ),
        )
        existing = next(
            (
                router
                for router in router_service.list(owner_id).items
                if router.status == "draft"
                and router.name.casefold() == write.name.casefold()
                and router.description == write.description
                and router.definition == write.definition
            ),
            None,
        )
        if existing is not None and not any(
            issue.code == "candidate_unavailable" for issue in validation.issues
        ):
            response.status_code = 200
            return existing
        raise HTTPException(status_code=409, detail=str(error)) from error


# Conversations are declared before the pinned-chart routes purely for readability; they
# share no path prefix, so no ordering constraint applies here the way it does for
# `/budgets/users` vs `/budgets/{scope_type}`.
@router.get("/api/v1/assistant/conversations", response_model=ConversationList)
def list_conversations(
    service: AssistantServiceDependency, identity: CurrentSession
) -> ConversationList:
    return ConversationList(items=service.list_conversations(identity.email))


@router.get("/api/v1/assistant/conversations/{conversation_id}", response_model=Conversation)
def get_conversation(
    conversation_id: UUID, service: AssistantServiceDependency, identity: CurrentSession
) -> Conversation:
    return service.get_conversation(conversation_id, identity.email)


@router.patch("/api/v1/assistant/conversations/{conversation_id}", response_model=Conversation)
def rename_conversation(
    conversation_id: UUID,
    rename: ConversationRename,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> Conversation:
    service.rename_conversation(conversation_id, identity.email, rename.title)
    # Returns the whole conversation rather than the summary: the panel has the transcript
    # open while renaming it, and a summary would make it refetch to keep rendering.
    return service.get_conversation(conversation_id, identity.email)


@router.delete("/api/v1/assistant/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: UUID, service: AssistantServiceDependency, identity: CurrentSession
) -> None:
    service.delete_conversation(conversation_id, identity.email)


@title_router.post(
    "/api/v1/assistant/conversations/{conversation_id}/title",
    response_model=ConversationSummary,
    dependencies=[Depends(require_allowed_write_origin)],
)
def title_conversation(
    conversation_id: UUID,
    request: ConversationTitleRequest,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> ConversationSummary:
    # POST rather than PATCH: this asks the server to produce a value, it does not carry
    # one. Repeating it is safe because the service refuses to re-title.
    return service.title_conversation(conversation_id, identity.email, request.locale)


@router.get("/api/v1/assistant/settings", response_model=AssistantSettings)
def get_assistant_settings(service: AssistantServiceDependency) -> AssistantSettings:
    return service.settings()


@router.put("/api/v1/assistant/settings", response_model=AssistantSettings)
def save_assistant_settings(
    write: AssistantSettingsWrite,
    service: AssistantServiceDependency,
    identity: OwnerSession,
) -> AssistantSettings:
    # Owner role only, no management bearer, matching budget administration rather than
    # registry administration: this changes what the workspace spends, not a credential.
    return service.save_settings(write, identity.email)


@router.get("/api/v1/assistant/pinned-charts", response_model=PinnedReportList)
def list_pinned_reports(
    service: AssistantServiceDependency, identity: CurrentSession
) -> PinnedReportList:
    return PinnedReportList(items=service.list_pinned(identity.email))


@router.post("/api/v1/assistant/pinned-charts", response_model=PinnedReport, status_code=201)
def create_pinned_report(
    write: PinnedChartWrite,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    """Pin a chart, either into a new report or into an existing one.

    Both cases return the whole report, so the caller always learns the collection the
    chart now belongs to without a second read.
    """
    return service.pin(write, identity.email)


@router.get("/api/v1/assistant/pinned-charts/{report_id}", response_model=PinnedReport)
def get_pinned_report(
    report_id: UUID, service: AssistantServiceDependency, identity: CurrentSession
) -> PinnedReport:
    return service.get_pinned(report_id, identity.email)


@router.patch("/api/v1/assistant/pinned-charts/{report_id}", response_model=PinnedReport)
def rename_pinned_report(
    report_id: UUID,
    rename: PinnedChartRename,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    return service.rename_pinned(report_id, identity.email, rename.title, rename.description)


@router.put(
    "/api/v1/assistant/pinned-charts/{report_id}/visibility",
    response_model=PinnedReport,
)
def set_pinned_report_visibility(
    report_id: UUID,
    write: PinnedReportVisibilityWrite,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    return service.set_pinned_visibility(report_id, identity.email, write.visibility)


@router.put(
    "/api/v1/assistant/pinned-charts/{report_id}/layout",
    response_model=PinnedReport,
)
def set_pinned_report_layout(
    report_id: UUID,
    layout: PinnedReportLayout,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    return service.set_pinned_layout(report_id, identity.email, layout)


@router.post("/api/v1/assistant/pinned-charts/{report_id}/refresh", response_model=PinnedReport)
def refresh_pinned_report(
    report_id: UUID,
    service: AssistantServiceDependency,
    identity: CurrentSession,
    timezone: str = "UTC",
    locale: str = "en",
) -> PinnedReport:
    return service.refresh_pinned(report_id, identity.email, timezone, locale)


@router.put(
    "/api/v1/assistant/pinned-charts/{report_id}/charts/order",
    response_model=PinnedReport,
)
def reorder_pinned_charts(
    report_id: UUID,
    order: PinnedChartOrder,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    return service.reorder_charts(report_id, identity.email, order.chart_ids)


@router.delete("/api/v1/assistant/pinned-charts/{report_id}", status_code=204)
def delete_pinned_report(
    report_id: UUID, service: AssistantServiceDependency, identity: CurrentSession
) -> None:
    service.unpin(report_id, identity.email)


@router.delete(
    "/api/v1/assistant/pinned-charts/{report_id}/charts/{chart_id}",
    response_model=PinnedReport,
)
def remove_chart_from_pinned_report(
    report_id: UUID,
    chart_id: UUID,
    service: AssistantServiceDependency,
    identity: CurrentSession,
) -> PinnedReport:
    return service.remove_chart(report_id, chart_id, identity.email)
