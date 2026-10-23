from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService,
    StubTokenProvider,
    bedrock_publication,
    publisher_settings,
)
from turnstile_core.domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
    ApiFormat,
    AuthStrategy,
    GatewayBackendPoolConfig,
    GatewayBackendPoolMember,
    GatewayPublication,
    GatewayRateLimitCircuitBreaker,
    GatewayRateLimitResilience,
    PublicationKind,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    BackendPoolMemberResource,
    BackendPoolResource,
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.persistence.in_memory import InMemoryRepository


@pytest.mark.parametrize(
    "difference", [None, "modelName", "modelVersion", "modelPublisher", "missing"],
)
def test_foundry_pool_equivalence_uses_catalog_model_identity(difference: str | None) -> None:
    from turnstile_core.integrations.gateway_connection_probe import (
        foundry_deployment_metadata,
        foundry_model_identity,
    )

    primary = {"name": "deployment-a", "modelName": "gpt-5.6",
               "modelVersion": "2026-10-01", "modelPublisher": "OpenAI"}
    secondary = {**primary, "name": "deployment-b"}
    if difference == "missing":
        secondary.pop("modelVersion")
    elif difference is not None:
        secondary[difference] = "another-model-identity"
    requests: list[httpx.Request] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/projects/main/deployments"
        assert request.url.params["api-version"] == "v1"
        return httpx.Response(200, json={"value": [primary, secondary]})

    project = "https://pool-project.services.ai.azure.com/api/projects/main"
    with httpx.Client(transport=httpx.MockTransport(metadata)) as client:
        first = foundry_deployment_metadata(client, project, project, "deployment-a", lambda: {})
        second = foundry_deployment_metadata(client, project, project, "deployment-b", lambda: {})
    assert len(requests) == 2
    if difference == "missing":
        with pytest.raises(PolicyCompilationError, match="model identity is unavailable"):
            foundry_model_identity(second)
    elif difference is None:
        assert foundry_model_identity(first) == foundry_model_identity(second)
    else:
        assert foundry_model_identity(first) != foundry_model_identity(second)


@pytest.mark.parametrize("fault", [None, "old-version", "wrong-model", "redirect", "bad-json"])
def test_deployment_pool_requires_a_working_observer_rewriter(fault: str | None) -> None:
    from turnstile_core.integrations.gateway_connection_probe import verify_pool_deployment_observer

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/_turnstile/pool-deployment-probe"
        assert request.headers["x-turnstile-upstream-host"] == "127.0.0.1:1"
        assert request.headers["x-turnstile-pool-deployment"] == "turnstile-deployment-probe"
        assert json.loads(request.content) == {"model": "turnstile-original-probe"}
        if fault == "bad-json":
            return httpx.Response(200, content=b"invalid")
        return httpx.Response(
            302 if fault == "redirect" else 200,
            headers={} if fault == "old-version" else {
                "x-turnstile-pool-deployment-version": "1",
            },
            json={"model": "wrong" if fault == "wrong-model" else "turnstile-deployment-probe"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if fault:
            with pytest.raises(PolicyCompilationError, match="rewrite upgrade"):
                verify_pool_deployment_observer(client, "https://observer.example.com", "unit-key")
        else:
            verify_pool_deployment_observer(client, "https://observer.example.com", "unit-key")
    assert len(requests) == 1


@pytest.mark.parametrize("mismatch", [False, True])
def test_new_pool_members_are_rechecked_even_when_deployment_names_match(
    mismatch: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.domain.runtime_models import BrandKey
    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    publication = _pooled_publication()
    binding = publication.desired_spec.bindings[0]
    pool = binding.backend_pool
    assert pool is not None
    members = [member.model_copy(update={
        "model_id": UUID(int=index + 900),
        "upstream_model_id": binding.model.upstream_model_id,
    }) for index, member in enumerate(pool.members)]
    updated = binding.model_copy(update={"provider_brand_key": BrandKey.MICROSOFT_FOUNDRY,
                                        "runtime_config": {
        **binding.runtime_config,
        "apim_backend_pool": pool.model_copy(update={"members": members}).model_dump(mode="json"),
    }})
    publication = publication.model_copy(update={"desired_spec":
        publication.desired_spec.model_copy(update={"bindings": [updated]}),
    })
    factory = FixedRoutePublisherFactory(
        InMemoryRepository(), {}, active_fence=lambda _gateway: {},
        availability_projector=lambda _model: None,
    )
    checked = []

    def metadata(*args: Any, binding: Any) -> dict[str, str]:
        checked.append(binding.runtime_id)
        return {"modelPublisher": "OpenAI", "modelName": "gpt-5.6",
                "modelVersion": "other-version" if mismatch and len(checked) == 2 else "v1"}

    monkeypatch.setattr(factory, "_validate_deployment", metadata)
    if mismatch:
        with pytest.raises(PolicyCompilationError, match="same model and version"):
            factory._validate_pool_deployments(publication, None, None, None)
    else:
        factory._validate_pool_deployments(publication, None, None, None)
    assert checked == [member.runtime_id for member in members]


@pytest.mark.parametrize("add_member", [False, True])
def test_legacy_foundry_pool_members_keep_their_own_project(add_member: bool) -> None:
    from turnstile_core.domain.runtime_models import BrandKey
    from turnstile_core.services.route_publication import pool_member_bindings

    binding = _pooled_publication().desired_spec.bindings[0]
    pool = binding.backend_pool
    assert pool is not None
    primary_project = "https://primary.services.ai.azure.com/api/projects/main"
    secondary_project = "https://secondary.services.ai.azure.com/api/projects/backup"
    members = [member.model_copy(update={"backend_url": HttpUrl(project)})
               for member, project in zip(
                   pool.members, (primary_project, secondary_project), strict=True,
               )]
    if add_member:
        members.append(members[0].model_copy(update={
            "model_id": UUID(int=903), "upstream_model_id": "another-deployment",
            "project_endpoint": HttpUrl(primary_project),
        }))
    raw_pool = pool.model_copy(update={"members": members}).model_dump(mode="json")
    pooled = binding.model_copy(update={
        "provider_brand_key": BrandKey.MICROSOFT_FOUNDRY,
        "backend_url": HttpUrl(primary_project),
        "runtime_config": {
            **binding.runtime_config, "project_endpoint": primary_project,
            "apim_backend_pool": raw_pool,
        },
    })

    expanded = pool_member_bindings(pooled)

    assert [source.runtime_config["project_endpoint"] for source in expanded] == [
        primary_project, secondary_project,
    ] + ([primary_project] if add_member else [])
    assert "project_endpoint" not in raw_pool["members"][1]
    assert pooled.runtime_config["apim_backend_pool"] == raw_pool


def _pooled_publication(affinity: bool = False) -> GatewayPublication:
    publication = GatewayControlPlaneService(InMemoryRepository()).publish(
        bedrock_publication("affinity-model"), "owner@example.com"
    )
    binding = publication.desired_spec.bindings[-1]
    primary_id = UUID("00000000-0000-4000-8000-000000000001")
    pool = GatewayBackendPoolConfig(
        members=[
            GatewayBackendPoolMember(
                runtime_id=primary_id,
                runtime_name="Primary connection",
                backend_url=HttpUrl(str(binding.backend_url)),
                auth_strategy=binding.auth_strategy,
                named_value_name=binding.named_value_name,
                priority=0,
                weight=1,
            ),
            GatewayBackendPoolMember(
                runtime_id=UUID("00000000-0000-4000-8000-000000000002"),
                runtime_name="Secondary connection",
                backend_url=HttpUrl("https://secondary.example.test"),
                auth_strategy=AuthStrategy.NAMED_VALUE_BEARER,
                named_value_name="test-secondary-key",
                priority=0,
                weight=1,
            ),
        ],
        rate_limit=GatewayRateLimitResilience(
            max_attempts_per_request=2,
            retry_interval_seconds=1,
            first_fast_retry=True,
            circuit_breaker=GatewayRateLimitCircuitBreaker(
                failure_count=1,
                interval_seconds=60,
                trip_duration_seconds=60,
                accept_retry_after=True,
            ),
        ),
    )
    config = {
        **binding.runtime_config,
        "apim_backend_pool": pool.model_dump(mode="json", exclude={"session_affinity"}),
    }
    if affinity:
        config["apim_backend_pool_session_affinity"] = True
    updated_binding = binding.model_copy(
        update={"runtime_id": primary_id, "runtime_config": config}
    )
    return GatewayPublication.model_validate(
        publication.model_copy(
            update={
                "desired_spec": publication.desired_spec.model_copy(
                    update={"bindings": [updated_binding]}
                ),
            }
        ).model_dump(mode="python")
    )


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_affinity_sidecar_preserves_legacy_pool_records(affinity: bool) -> None:
    publication = _pooled_publication(affinity)
    binding = publication.desired_spec.bindings[-1]
    raw_pool = binding.runtime_config["apim_backend_pool"]
    assert isinstance(raw_pool, dict)
    assert "session_affinity" not in raw_pool
    pool = binding.backend_pool
    assert pool is not None and pool.session_affinity is affinity
    assert GatewayBackendPoolConfig.model_validate(raw_pool).session_affinity is False
    assert (
        publication.model_dump(mode="json")["desired_spec"]["bindings"][-1]["runtime_config"][
            "apim_backend_pool"
        ]
        == raw_pool
    )


@pytest.mark.parametrize("value", ("true", 1, [], {}))
def test_pool_affinity_sidecar_rejects_nonboolean_values(value: object) -> None:
    publication = _pooled_publication()
    payload = publication.model_dump(mode="python")
    payload["desired_spec"]["bindings"][-1]["runtime_config"][
        "apim_backend_pool_session_affinity"
    ] = value
    with pytest.raises(ValueError, match="must be a boolean"):
        GatewayPublication.model_validate(payload)


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_resource_identity_is_stable_only_when_affinity_is_enabled(affinity: bool) -> None:
    publication = _pooled_publication(affinity)
    compiler = ApimPolicyCompiler(
        usage_observer_url="https://observer.example.test",
        usage_observer_key_named_value="test-observer-key",
    )
    first = compiler.compile(publication)
    next_release = compiler.compile(publication.model_copy(update={"id": uuid4(), "generation": 2}))
    first_pool = next(item for item in first.backends if isinstance(item, BackendPoolResource))
    next_pool = next(
        item for item in next_release.backends if isinstance(item, BackendPoolResource)
    )
    assert [member.priority for member in first_pool.members] == [0, 0]
    assert [member.weight for member in first_pool.members] == [1, 1]
    assert (first_pool.id == next_pool.id) is affinity
    assert (
        [item.id for item in first.backends] == [item.id for item in next_release.backends]
    ) is affinity
    if affinity:
        assert first_pool.session_cookie_name is not None
        assert first_pool.session_cookie_name.startswith("TurnstileAffinity-")
        assert first_pool.session_cookie_name == next_pool.session_cookie_name
    else:
        assert first_pool.session_cookie_name is None
        assert first_pool.id.startswith(f"turnstile-pool-{publication.id.hex[:12]}-")
    binding = publication.desired_spec.bindings[-1]
    pool = binding.backend_pool
    assert pool is not None
    assert {
        compiler.pool_member_backend_id(publication, binding, member) for member in pool.members
    } == {member.backend_id for member in first_pool.members}


def test_pool_affinity_toggle_is_visible_as_a_release_change() -> None:
    old = _pooled_publication(False)
    binding = old.desired_spec.bindings[-1]
    new_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool_session_affinity": True,
            }
        }
    )
    changed = old.model_copy(
        update={"desired_spec": old.desired_spec.model_copy(update={"bindings": [new_binding]})}
    )
    changes = GatewayControlPlaneService._release_changes(changed, old)
    assert changes.changed_models == [binding.model.model_key]
    assert changes.changed_backend_pools == []
    assert changes.added_backend_pools == []
    assert changes.removed_backend_pools == []


@pytest.mark.parametrize("affinity", (False, True))
def test_pool_affinity_arm_configuration_and_readback(affinity: bool) -> None:
    requests: list[httpx.Request] = []
    stored: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        expected_version = "2025-09-01-preview" if affinity else "2024-05-01"
        assert request.url.params["api-version"] == expected_version
        if request.method == "PUT":
            stored.update(json.loads(request.content)["properties"])
            return httpx.Response(201, json={"properties": stored})
        return httpx.Response(200, json={"properties": stored}) if stored else httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    backend = BackendPoolResource(
        id="test-pool",
        title="Test Pool",
        url="",
        members=(BackendPoolMemberResource("one", 0, 1), BackendPoolMemberResource("two", 0, 1)),
        session_cookie_name="TurnstileAffinity-test" if affinity else None,
    )
    client.ensure_backend(backend)
    assert [member["priority"] for member in stored["pool"]["services"]] == [1, 1]
    if affinity:
        assert requests[-1].method == "GET"
        assert stored["pool"]["sessionAffinity"] == {
            "sessionId": {"source": "cookie", "name": "TurnstileAffinity-test"}
        }
        stored["pool"]["sessionAffinity"]["sessionId"]["name"] = "unexpected-cookie"
        with pytest.raises(PolicyCompilationError, match="different settings"):
            client.ensure_backend(backend)
    else:
        assert "sessionAffinity" not in stored["pool"]
        client.ensure_backend(backend)
    assert sum(request.method == "PUT" for request in requests) == 1


def test_pool_affinity_readback_mismatch_never_reports_success() -> None:
    stored: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            stored.update(json.loads(request.content)["properties"])
            stored["pool"].pop("sessionAffinity")
            return httpx.Response(200)
        return httpx.Response(200, json={"properties": stored}) if stored else httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryablePublicationError, match="readback differs"):
        client.ensure_backend(
            BackendPoolResource(
                id="test-pool",
                title="Test Pool",
                url="",
                members=(BackendPoolMemberResource("one", 0, 1),),
                session_cookie_name="TurnstileAffinity-test",
            )
        )


def test_pool_integrity_classifies_members_by_resource_type() -> None:
    publication = _pooled_publication(True)
    compiled = ApimPolicyCompiler().compile(publication)
    expected_pool = next(
        item for item in compiled.backends if isinstance(item, BackendPoolResource)
    )
    publication = publication.model_copy(
        update={
            "resource_manifest": {
                "backends": [item.id for item in compiled.backends],
                "named_values": [],
            }
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/" + expected_pool.id):
            assert request.url.params["api-version"] == "2025-09-01-preview"
        return httpx.Response(404)

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    dependencies = client.inspect_revision_dependencies(publication)
    assert dependencies.backend_pools == [expected_pool.id]
    assert dependencies.backends == sorted(
        item.id for item in compiled.backends if not isinstance(item, BackendPoolResource)
    )


def test_pool_baseline_selectors_are_read_from_raw_revision_policy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert ";rev=previous" in request.url.path
        if request.url.path.endswith("/operations"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "actual-chat-operation",
                            "properties": {"method": "POST", "urlTemplate": "/chat/completions"},
                        }
                    ]
                },
            )
        assert "/actual-chat-operation/policies/policy" in request.url.path
        assert request.url.params["format"] == "rawxml"
        return httpx.Response(
            200,
            headers={"content-type": "application/xml"},
            text=(
                '<policies><inbound><choose><when condition="@(true &amp;&amp; true)">'
                '<set-backend-service backend-id="old-member" />'
                "</when></choose></inbound></policies>"
            ),
        )

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client._pool_baseline_backend_ids("previous", ApiFormat.OPENAI_CHAT) == {"old-member"}
    assert client._pool_baseline_backend_ids("previous", ApiFormat.ANTHROPIC_MESSAGES) == set()


@pytest.mark.parametrize("previous_affinity", (False, True))
@pytest.mark.parametrize("next_affinity", (False, True))
@pytest.mark.parametrize("known_member", (False, True))
def test_pool_probe_baseline_uses_only_verified_previous_member_ids(
    previous_affinity: bool,
    next_affinity: bool,
    known_member: bool,
) -> None:
    previous = _pooled_publication(previous_affinity)
    binding = previous.desired_spec.bindings[-1]
    compiler = ApimPolicyCompiler()
    pool = binding.backend_pool
    assert pool is not None
    old_member_ids = {
        compiler.pool_member_backend_id(previous, binding, member) for member in pool.members
    }
    next_binding = binding.model_copy(
        update={
            "runtime_config": {
                **binding.runtime_config,
                "apim_backend_pool_session_affinity": next_affinity,
            }
        }
    )
    candidate = previous.model_copy(
        update={
            "id": uuid4(),
            "base_release_id": previous.id,
            "publication_kind": PublicationKind.ROUTE_RECONCILE,
            "desired_spec": previous.desired_spec.model_copy(update={"bindings": [next_binding]}),
            "resource_manifest": {"base_apim_revision": "previous"},
        }
    )
    next_member_ids = {
        compiler.pool_member_backend_id(candidate, next_binding, member) for member in pool.members
    }
    baseline_requests: list[httpx.Request] = []
    candidate_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/v1/models"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": model.id} for model in candidate.desired_spec.discovery_models]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/operations"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "actual-message-operation",
                            "properties": {"method": "POST", "urlTemplate": "/v1/messages"},
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/policies/policy"):
            ids = sorted(old_member_ids) if known_member else ["unrelated-member"]
            selection = compiler._backend_selection("unit-old-pool", tuple(ids))
            policy = f"<policies><inbound>{selection}</inbound></policies>"
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=policy)
        body = json.loads(request.content)
        if request.url.path.endswith("/count_tokens"):
            rejection = COUNT_TOKENS_REJECTIONS[
                "unavailable" if body["model"] == binding.model.model_key else "unpublished"
            ]
            return httpx.Response(rejection.status_code, json=rejection.payload())
        if body.get("model") != binding.model.model_key:
            rejection = COUNT_TOKENS_REJECTIONS["unpublished"]
            return httpx.Response(rejection.status_code, json=rejection.payload())
        if "x-turnstile-pool-member" not in request.headers:
            return httpx.Response(200, json={"content": []})
        if ";rev=previous" in request.url.path:
            baseline_requests.append(request)
            assert request.headers["x-turnstile-pool-member"] in old_member_ids
        else:
            candidate_requests.append(request)
            assert request.headers["x-turnstile-pool-member"] in next_member_ids
        return httpx.Response(403, json={"error": "preexisting-provider-failure"})

    settings = publisher_settings().model_copy(
        update={
            "apim_probe_subscription_key": SecretStr("unit-probe-key"),
            "apim_regression_model_key": binding.model.model_key,
        }
    )
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    if known_member:
        client.probe_revision("candidate", candidate, baseline_publication=previous)
        assert len(baseline_requests) == 2
        assert len(candidate_requests) == 2
    else:
        with pytest.raises(httpx.HTTPStatusError):
            client.probe_revision("candidate", candidate, baseline_publication=previous)
        assert baseline_requests == []


def test_route_reconcile_absorbs_model_add_and_resolves_legacy_member_project_endpoint() -> None:
    from unittest.mock import Mock

    from turnstile_core.domain.storage_routes import ActiveRouteHead, ModelRouteHead
    from turnstile_core.persistence.repository_support import _route_reconcile_preparation_failed
    from turnstile_core.services.storage_route_publisher import (
        FixedRoutePublisherFactory,
        FixedRoutePublisherPort,
    )

    repository = InMemoryRepository()
    publication = _pooled_publication()
    binding = publication.desired_spec.bindings[0]
    alias = binding.model.model_key
    gateway_id = publication.gateway_profile_id
    base_id = uuid4()
    addition_id = uuid4()
    reconcile_id = uuid4()
    repository.effective_gateway_releases[gateway_id] = base_id
    repository.gateway_publications.extend([
        {
            **publication.model_dump(mode="python"),
            "id": base_id,
            "generation": 1,
            "status": "active",
            "publication_kind": "route_reconcile",
            "desired_spec": publication.desired_spec.model_copy(update={"bindings": []}).model_dump(
                mode="python"
            ),
        },
        {
            **publication.model_dump(mode="python"),
            "id": addition_id,
            "generation": 2,
            "base_release_id": base_id,
            "status": "active",
            "publication_kind": "model_add",
            "resource_manifest": {"model_route_addition": {"alias": alias}},
        },
        {
            **publication.model_dump(mode="python"),
            "id": reconcile_id,
            "generation": 3,
            "base_release_id": base_id,
            "status": "active",
            "publication_kind": "route_reconcile",
        },
    ])
    assert [row["id"] for row in repository.active_model_route_publications(gateway_id)] == [
        addition_id
    ]
    repository._supersede_absorbed_model_additions(repository.gateway_publications[-1])
    repository.effective_gateway_releases[gateway_id] = reconcile_id
    assert repository.active_model_route_publications(gateway_id) == []
    assert repository.gateway_publications[1]["status"] == "superseded"

    projection = Mock()
    verifier = Mock()
    task = Mock()
    task.model_addition = False
    route = Mock()
    route.alias = alias
    task.plan.snapshot.routes = (route,)
    task.plan.program = Mock()
    task.plan.snapshot.approval = Mock()
    task.plan.snapshot.approval.budget_epoch = "epoch-1"
    head = Mock(spec=ActiveRouteHead)
    head.approval = Mock()
    model_head = Mock(spec=ModelRouteHead)
    projection.read_model_active.return_value = (model_head, 'W/"model-etag"')
    port = FixedRoutePublisherPort(
        projection,
        verifier,
        SecretStr("unit-key"),
        Mock(),
        active_fence=Mock(),
        verify_connections=Mock(),
        prepare_availability=Mock(),
        clock=lambda: 1000,
        absorbed_model_aliases=lambda: (alias,),
    )
    port.check = Mock(return_value=Mock())  # type: ignore[method-assign]
    port.confirm(task, head)
    projection.remove_model_head.assert_called_once_with(model_head, 'W/"model-etag"')

    assert _route_reconcile_preparation_failed({
        "publication_kind": "route_reconcile",
        "error_code": "PolicyCompilationError",
        "error_message": "Route credential verification was rejected",
        "resource_manifest": {
            "route_credentials": {},
            "route_dependencies": {},
            "route_configuration": {},
            "route_last_progress_attempt": 1,
        },
    }) is True
    assert _route_reconcile_preparation_failed({
        "publication_kind": "route_reconcile",
        "error_code": "PolicyCompilationError",
        "error_message": "Foundry deployment metadata target does not match connection",
        "resource_manifest": {
            "route_credentials": {},
            "route_preparation": {},
            "route_dependencies": {},
        },
    }) is True
    assert _route_reconcile_preparation_failed({
        "publication_kind": "route_reconcile",
        "error_code": "PolicyCompilationError",
        "error_message": "Route credential verification was rejected",
        "resource_manifest": {
            "route_credentials": {},
            "route_activation": {},
        },
    }) is False

    project_url = "https://secondary.services.ai.azure.com/api/projects/secondary"
    repository.runtimes.append({
        "id": binding.runtime_id,
        "provider_id": binding.provider_id,
        "gateway_profile_id": gateway_id,
        "name": binding.runtime_name,
        "runtime_kind": binding.runtime_kind.value,
        "enabled": True,
        "is_default": False,
        "brand_key": binding.runtime_brand_key.value,
        "config": {
            **binding.runtime_config,
            "project_endpoint": project_url,
        },
        "allowed_roles": ["owner"],
    })
    fallback_binding = binding.model_copy(
        update={
            "backend_url": HttpUrl("https://secondary.openai.azure.com"),
            "auth_strategy": AuthStrategy.MANAGED_IDENTITY,
            "runtime_config": {
                **binding.runtime_config,
                "project_endpoint": "https://secondary.openai.azure.com",
            },
        }
    )
    factory = FixedRoutePublisherFactory(
        repository,
        Mock(),
        active_fence=lambda _gateway: {},
        availability_projector=Mock(),
    )
    factory._tokens = Mock(token=Mock(return_value="unit-token"))
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "name": fallback_binding.model.upstream_model_id,
                        "modelName": "gpt-5.4-mini",
                        "modelVersion": "2026-03-17",
                        "modelPublisher": "OpenAI",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        metadata = factory._validate_deployment(
            publication,
            Mock(),
            http_client,
            Mock(),
            binding=fallback_binding,
        )
    assert metadata is not None
    assert captured_urls == [f"{project_url}/deployments?api-version=v1"]

    from turnstile_core.domain.control_plane import pool_member_binding

    cross_runtime_member = GatewayBackendPoolMember(
        runtime_id=uuid4(),
        runtime_name="Cross Foundry OpenAI URL",
        backend_url=HttpUrl("https://secondary.openai.azure.com"),
        auth_strategy=AuthStrategy.NAMED_VALUE_API_KEY,
        named_value_name="secondary-key",
        priority=0,
        weight=1,
    )
    expanded_member = pool_member_binding(
        binding.model_copy(
            update={
                "runtime_config": {
                    **binding.runtime_config,
                    "project_endpoint": "https://primary.services.ai.azure.com/api/projects/main",
                }
            }
        ),
        cross_runtime_member,
    )
    assert "project_endpoint" not in expanded_member.runtime_config

    from turnstile_core.domain.storage_routes import RouteScope
    from turnstile_core.integrations.apim_storage_routes import (
        FixedRouteBinding,
        fixed_operation,
    )

    pool_operation_xml = fixed_operation(
        "chat-completions",
        FixedRouteBinding(
            scope=RouteScope(
                target_id=str(gateway_id),
                api_id="finops-foundry-llm",
                gateway_id="managed",
            ),
            api_revision="fixed-abi2-60e4f760",
            table_endpoint="https://unitstore.table.core.windows.net",
            table_name="Routes",
            ledger_table_endpoint="https://unitstore.table.core.windows.net",
            ledger_table_name="TurnstileLedger",
            trust_binding_sha256="a" * 64,
            pool_enabled=True,
        ),
    )
    assert "<retry" in pool_operation_xml
    assert 'count="1"' in pool_operation_xml
    assert 'first-fast-retry="true"' in pool_operation_xml

    provider_id = repository.providers[0]["id"]
    repository.runtimes[-1]["provider_id"] = provider_id
    absorbed_model = repository.create_registry_item(
        "model",
        {
            "provider_id": provider_id,
            "runtime_id": binding.runtime_id,
            "model_key": alias,
            "display_name": binding.model.display_name,
            "family_key": binding.model.family_key.value,
            "upstream_model_id": binding.model.upstream_model_id,
            "assignment_required": False,
            "enabled": True,
            "is_default": False,
            "capabilities": ["chat"],
            "context_window": 128000,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "cached_cost_per_million": None,
            "cache_write_cost_per_million": None,
            "allowed_roles": ["owner", "admin", "member"],
            "publication_id": addition_id,
        },
    )
    repository.gateway_publications[-1]["resource_manifest"] = {
        "fixed_route_program": {"version": 1}
    }
    unpublished_model = repository.create_registry_item(
        "model",
        {
            "provider_id": provider_id,
            "runtime_id": binding.runtime_id,
            "model_key": "unpublished-extra-model",
            "display_name": "Unpublished Extra Model",
            "family_key": binding.model.family_key.value,
            "upstream_model_id": "unpublished-extra-model",
            "assignment_required": False,
            "enabled": True,
            "is_default": False,
            "capabilities": ["chat"],
            "context_window": 128000,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "cached_cost_per_million": None,
            "cache_write_cost_per_million": None,
            "allowed_roles": ["owner", "admin", "member"],
            "publication_id": None,
        },
    )
    service = GatewayControlPlaneService(repository)
    removal_pub = service.remove_model(absorbed_model["id"], "owner@example.com")
    assert removal_pub.publication_kind is PublicationKind.MODEL_REMOVE
    assert removal_pub.base_release_id == reconcile_id
    assert "model_route_removal" not in removal_pub.resource_manifest
    assert [m.model_id for m in removal_pub.desired_spec.removed_models] == [
        absorbed_model["id"]
    ]
    assert unpublished_model["model_key"] not in [
        item.id for item in removal_pub.desired_spec.discovery_models
    ]

    # Verify zero-effect preparation failure on full-release model_remove can be retried cleanly
    repository.gateway_publications[-1].update(
        status="failed",
        error_code="PolicyCompilationError",
        error_message="Route credential verification was rejected",
        resource_manifest={
            "route_credentials": {},
            "route_dependencies": {},
            "route_configuration": {},
            "route_last_progress_attempt": 1,
        },
    )
    for outbox in repository.gateway_publication_outbox:
        if outbox["publication_id"] == removal_pub.id:
            outbox["status"] = "completed"
            outbox["lease_owner"] = None
            outbox["lease_expires_at"] = None
    assert _route_reconcile_preparation_failed(repository.gateway_publications[-1]) is True
    retried_removal = service.remove_model(absorbed_model["id"], "owner@example.com")
    assert retried_removal.id != removal_pub.id
    assert retried_removal.status.value == "queued"
