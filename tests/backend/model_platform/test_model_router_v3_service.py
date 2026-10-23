from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from backend.services.model_router_service import (
    ModelRouterConflictError,
    ModelRouterNotFoundError,
    ModelRouterService,
)
from backend.services.model_router_v3_service import ModelRouterV3Service
from turnstile_core.domain.model_router import ModelRouterDefinition, ModelRouterWrite
from turnstile_core.domain.model_router_v3 import ModelRouterV3Write, RouterSpecV3
from turnstile_core.persistence.in_memory import InMemoryRepository

OWNER_ID = UUID("00000000-0000-4000-8000-000000000101")
OWNER_EMAIL = "owner@contoso.com"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("removal_status", ["active", "superseded"])
def test_router_model_reference_does_not_rebind_a_removed_id_to_a_recreated_alias(
    enabled: bool, removal_status: str,
) -> None:
    repository = InMemoryRepository()
    original = repository.models[0]
    original_id = original["id"]
    replacement_id = UUID(int=901)
    original_name = original["display_name"]
    repository.models = [item for item in repository.models if item["id"] != original_id]
    repository.models.append({
        **original, "id": replacement_id, "display_name": "Replacement", "enabled": enabled,
    })
    repository.gateway_publications.extend([{
        "id": UUID(int=902), "status": "superseded", "completed_at": datetime.now(UTC),
        "desired_spec": {"bindings": [{
            "provider_name": "Historical provider", "model": {
                "model_key": original["model_key"], "family_key": "openai",
            },
        }]},
    }, {
        "id": UUID(int=903), "base_release_id": UUID(int=902), "status": removal_status,
        "completed_at": datetime.now(UTC), "desired_spec": {"removed_models": [{
            "model_id": str(original_id), "model_key": original["model_key"],
            "display_name": original_name,
        }]},
    }, {
        "id": UUID(int=905), "base_release_id": UUID(int=902), "status": "failed",
        "completed_at": datetime.now(UTC), "desired_spec": {"removed_models": [{
            "model_id": str(original_id), "model_key": original["model_key"],
            "display_name": "Failed removal must not replace historical identity",
        }]},
    }])

    references = {item["id"]: item for item in repository.model_router_model_references(
        [original_id, replacement_id, UUID(int=904)],
    )}

    assert set(references) == {original_id, replacement_id}
    assert references[original_id]["display_name"] == original_name
    assert references[original_id]["provider_name"] == "Historical provider"
    assert references[original_id]["family_key"] == "openai"
    assert references[original_id]["status"] == "removed"
    assert references[replacement_id]["display_name"] == "Replacement"
    assert references[replacement_id]["status"] == ("available" if enabled else "disabled")
    assert original_id not in {item["id"] for item in repository.models}


def install_model_facts(
    repository: InMemoryRepository,
    model_ids: tuple[UUID, ...],
) -> None:
    models = {UUID(str(model["id"])): model for model in repository.models}
    repository.model_router_model_facts = [
        {
            "model_id": model_id,
            "facts_version": 1,
            "logical_model_key": f"logical-{model_id}",
            "enabled": True,
            "supported_operations": ["chat_completions", "responses"],
            "capabilities": list(models[model_id]["capabilities"]),
            "foundry_router_eligible": False,
            "context_window": models[model_id]["context_window"],
            "provider_id": models[model_id]["provider_id"],
            "data_zone": "us",
        }
        for model_id in model_ids
    ]


def v3_write(
    model_ids: tuple[UUID, ...],
    *,
    alias: str = "enterprise-router",
) -> ModelRouterV3Write:
    spec = RouterSpecV3.model_validate(
        {
            "identity": {
                "alias": alias,
                "description": "Typed enterprise Router.",
                "supported_operations": ["chat_completions", "responses"],
                "streaming": "disabled",
                "tools": "allowed",
            },
            "input_contract": [
                {
                    "name": "user_id",
                    "value_type": "string",
                    "source": "authenticated_identity",
                    "registry_version": 1,
                    "required": True,
                }
            ],
            "candidate_pool": {
                "candidates": [
                    {"model_id": model_id, "facts_version": 1}
                    for model_id in model_ids
                ]
            },
            "eligibility": {
                "required_capabilities": ["chat"],
                "minimum_context_window": None,
                "allowed_provider_ids": [],
                "allowed_data_zones": ["us"],
            },
            "rules": [],
            "selector": {"kind": "priority", "model_order": list(model_ids)},
            "resilience": {
                "equivalent_deployment_retry": {
                    "max_attempts_per_request": 1,
                    "retryable_events": [],
                    "respect_retry_after": False,
                },
                "cross_model_fallback": {
                    "model_order": [],
                    "retryable_events": [],
                },
                "streaming_failover": "disabled",
            },
            "stickiness": {"scope": "none", "ttl_seconds": None},
            "qos_policy": None,
        }
    )
    return ModelRouterV3Write(
        name="Enterprise Router",
        description="Compiled v3 Router.",
        spec=spec,
    )


def test_v3_lifecycle_persists_compiled_plan_and_isolates_v2() -> None:
    repository = InMemoryRepository()
    model_ids = tuple(
        UUID(str(model["id"])) for model in repository.models if model["enabled"]
    )[:2]
    legacy_service = ModelRouterService(repository)
    legacy = legacy_service.create(
        ModelRouterWrite(
            name="Legacy Router",
            definition=ModelRouterDefinition(),
        ),
        OWNER_ID,
        OWNER_EMAIL,
        "owner",
    )
    assert repository.publish_model_router(legacy.id, OWNER_ID, OWNER_EMAIL) is not None
    service = ModelRouterV3Service(repository)
    created = service.create(
        v3_write(model_ids),
        OWNER_ID,
        OWNER_EMAIL,
        "owner",
    )

    assert [router.id for router in legacy_service.list(OWNER_ID).items] == [legacy.id]
    assert [router.id for router in service.list(OWNER_ID).items] == [created.id]
    assert created.version == 0
    assert created.compiled_plan is None

    invalid = service.validate(created.id, OWNER_ID, OWNER_EMAIL, "owner")
    assert invalid.valid is False
    assert invalid.compiled_plan is None
    assert {issue.code for issue in invalid.issues} == {"candidate_unavailable"}
    with pytest.raises(ModelRouterConflictError, match="compilation failed"):
        service.publish(created.id, OWNER_ID, OWNER_EMAIL, "owner")

    install_model_facts(repository, model_ids)
    valid = service.validate(created.id, OWNER_ID, OWNER_EMAIL, "owner")
    assert valid.valid is True
    assert valid.compiled_plan is not None
    published = service.publish(created.id, OWNER_ID, OWNER_EMAIL, "owner")

    assert published.status == "published"
    assert published.version == 1
    assert published.compiled_plan is not None
    assert published.compiled_plan.plan_sha256 == valid.compiled_plan.plan_sha256
    assert (
        repository.model_router_spec_revisions[-1]["compiled_plan"]["plan_sha256"]
        == published.compiled_plan.plan_sha256
    )
    assert len(repository.model_router_revisions) == 1
    assert "compiled_plan" not in repository.model_router_revisions[0]
    assert len(repository.model_router_spec_revisions) == 1


def test_v3_published_alias_is_immutable_and_lifecycle_is_owner_scoped() -> None:
    repository = InMemoryRepository()
    model_ids = tuple(
        UUID(str(model["id"])) for model in repository.models if model["enabled"]
    )[:2]
    install_model_facts(repository, model_ids)
    service = ModelRouterV3Service(repository)
    created = service.create(
        v3_write(model_ids),
        OWNER_ID,
        OWNER_EMAIL,
        "owner",
    )
    service.publish(created.id, OWNER_ID, OWNER_EMAIL, "owner")

    with pytest.raises(ModelRouterConflictError, match="aliases are immutable"):
        service.update(
            created.id,
            v3_write(model_ids, alias="changed-router"),
            OWNER_ID,
            OWNER_EMAIL,
            "owner",
        )

    other_owner = UUID("00000000-0000-4000-8000-000000000999")
    with pytest.raises(ModelRouterNotFoundError):
        service.get(created.id, other_owner)
    disabled = service.disable(created.id, OWNER_ID, OWNER_EMAIL)
    assert disabled.status == "disabled"
    service.remove(created.id, OWNER_ID)
    with pytest.raises(ModelRouterNotFoundError):
        service.get(created.id, OWNER_ID)


def test_v3_publish_rejects_concurrent_draft_modification_after_compilation() -> None:
    repository = InMemoryRepository()
    model_ids = tuple(
        UUID(str(model["id"])) for model in repository.models if model["enabled"]
    )[:2]
    install_model_facts(repository, model_ids)
    service = ModelRouterV3Service(repository)
    created = service.create(
        v3_write(model_ids),
        OWNER_ID,
        OWNER_EMAIL,
        "owner",
    )
    compiled = service.validate(created.id, OWNER_ID, OWNER_EMAIL, "owner")
    assert compiled.valid is True
    assert compiled.compiled_plan is not None
    stale_plan = compiled.compiled_plan.model_dump(mode="json")

    updated_write = v3_write((model_ids[1], model_ids[0]))
    service.update(created.id, updated_write, OWNER_ID, OWNER_EMAIL, "owner")

    assert (
        repository.publish_model_router_v3(
            created.id, OWNER_ID, OWNER_EMAIL, stale_plan
        )
        is None
    )

    original_validate = service.validate

    def validate_then_concurrent_update(
        router_id: UUID, owner_id: UUID, owner_email: str, role: str
    ):
        result = original_validate(router_id, owner_id, owner_email, role)
        service.update(router_id, v3_write(model_ids), owner_id, owner_email, role)
        return result

    service.validate = validate_then_concurrent_update  # type: ignore[method-assign]
    with pytest.raises(
        ModelRouterConflictError, match="draft changed during compilation"
    ):
        service.publish(created.id, OWNER_ID, OWNER_EMAIL, "owner")