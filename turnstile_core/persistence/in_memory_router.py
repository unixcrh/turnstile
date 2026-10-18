from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.models import TokenUsageRecord
from .repository_support import (
    model_router_v3_view,
    model_router_view,
    published_model_router_view,
)


class InMemoryRouterRepositoryMixin:
    models: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    gateway_publications: list[dict[str, Any]]
    usage_records: list[TokenUsageRecord]
    model_routers: list[dict[str, Any]]
    model_router_revisions: list[dict[str, Any]]
    model_router_specs: list[dict[str, Any]]
    model_router_spec_revisions: list[dict[str, Any]]
    model_router_context_fields: list[dict[str, Any]]
    model_router_model_facts: list[dict[str, Any]]
    model_router_profiles: list[dict[str, Any]]
    model_router_evaluations: list[dict[str, Any]]
    user_model_router_policies: dict[str, dict[str, Any]]
    user_model_router_access: dict[str, set[UUID]]
    user_model_router_access_audit: list[dict[str, Any]]
    model_router_run_bindings: dict[tuple[UUID, int, str, str], dict[str, Any]]
    model_router_invocations: dict[str, dict[str, Any]]

    def model_router_model_references(
        self, model_ids: Sequence[UUID],
    ) -> list[dict[str, Any]]:
        references = []
        publications = sorted(self.gateway_publications, key=lambda item: (
            str(item.get("completed_at") or ""), str(item["id"]),
        ), reverse=True)
        for model_id in sorted(set(model_ids), key=str):
            model = next((item for item in self.models if item["id"] == model_id), None)
            if model is not None:
                provider = next(
                    item for item in self.providers if item["id"] == model["provider_id"]
                )
                references.append({
                    "id": model_id, "model_key": model["model_key"],
                    "display_name": model["display_name"], "provider_name": provider["name"],
                    "family_key": model.get("family_key", "generic"),
                    "status": "available" if model["enabled"] else "disabled",
                })
                continue
            removed = next((
                (publication, item)
                for publication in publications if publication["status"] in {"active", "superseded"}
                for item in publication["desired_spec"].get("removed_models", [])
                if str(item["model_id"]) == str(model_id)
            ), None)
            if removed is not None:
                publication, target = removed
                previous = next((item for item in publications
                                 if item["id"] == publication.get("base_release_id")), None)
                bindings = previous["desired_spec"].get("bindings", []) if previous else []
                binding: dict[str, Any] = next(
                    (item for item in bindings if item["model"]["model_key"].casefold()
                     == target["model_key"].casefold()), {},
                )
                references.append({
                    "id": model_id, "model_key": target["model_key"],
                    "display_name": target["display_name"],
                    "provider_name": binding.get("provider_name", ""),
                    "family_key": binding.get("model", {}).get("family_key", "generic"),
                    "status": "removed",
                })
                continue
            usage = max(
                (item for item in self.usage_records
                 if item.model_id == str(model_id) and item.usage_domain == "apim"
                 and item.model.strip() and item.model not in {"unattributed", str(model_id)}),
                key=lambda item: (item.ts, item.id), default=None,
            )
            if usage is not None:
                references.append({
                    "id": model_id, "model_key": usage.model, "display_name": usage.model,
                    "provider_name": usage.provider if usage.provider != "unattributed" else "",
                    "family_key": "generic", "status": "removed",
                })
        return references

    def list_model_routers(self, owner_id: UUID) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.model_routers
            if row["owner_id"] == owner_id and row["deleted_at"] is None
        ]
        return [
            model_router_view(row)
            for row in sorted(rows, key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        ]

    def get_model_router(
        self, router_id: UUID, owner_id: UUID
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        return model_router_view(row) if row is not None else None

    def create_model_router(
        self, owner_id: UUID, owner_email: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if any(
            row["owner_id"] == owner_id
            and row["deleted_at"] is None
            and str(row["name"]).casefold() == str(values["name"]).casefold()
            for row in self.model_routers
        ):
            return None
        now = datetime.now(UTC)
        row = {
            "id": uuid4(),
            "owner_id": owner_id,
            "name": values["name"],
            "description": values["description"],
            "status": "draft",
            "version": 0,
            "draft_config": deepcopy(values["definition"]),
            "published_config": None,
            "created_by": owner_email,
            "updated_by": values["updated_by"],
            "created_at": now,
            "updated_at": now,
            "published_at": None,
            "deleted_at": None,
        }
        self.model_routers.append(row)
        return model_router_view(row)

    def update_model_router(
        self, router_id: UUID, owner_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return None
        if any(
            item["owner_id"] == owner_id
            and item["deleted_at"] is None
            and item["id"] != router_id
            and str(item["name"]).casefold() == str(values["name"]).casefold()
            for item in self.model_routers
        ):
            return None
        row.update(
            name=values["name"],
            description=values["description"],
            draft_config=deepcopy(values["definition"]),
            updated_by=values["updated_by"],
            updated_at=datetime.now(UTC),
        )
        return model_router_view(row)

    def publish_model_router(
        self, router_id: UUID, owner_id: UUID, published_by: str
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return None
        now = datetime.now(UTC)
        version = int(row["version"]) + 1
        published_config = deepcopy(row["draft_config"])
        self.model_router_revisions.append(
            {
                "router_id": router_id,
                "version": version,
                "config": published_config,
                "published_by": published_by,
                "published_at": now,
            }
        )
        row.update(
            status="published",
            version=version,
            published_config=published_config,
            published_at=now,
            updated_at=now,
            updated_by=published_by,
        )
        return model_router_view(row)

    def disable_model_router(
        self, router_id: UUID, owner_id: UUID, changed_by: str
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
                and item["status"] == "published"
                and item["published_config"] is not None
            ),
            None,
        )
        if row is None:
            return None
        row.update(status="disabled", updated_at=datetime.now(UTC), updated_by=changed_by)
        return model_router_view(row)

    def delete_model_router(self, router_id: UUID, owner_id: UUID) -> bool:
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return False
        now = datetime.now(UTC)
        row.update(deleted_at=now, updated_at=now)
        return True

    def list_assigned_model_routers(self, user_id: str) -> list[dict[str, Any]]:
        assigned = self.user_model_router_access.get(user_id, set())
        return [
            published_model_router_view(row)
            for row in sorted(self.model_routers, key=lambda item: str(item["name"]))
            if row["id"] in assigned
            and row["status"] == "published"
            and row["deleted_at"] is None
            and row["published_config"] is not None
        ]

    def get_callable_model_router(
        self, router_id: UUID, user_id: str
    ) -> dict[str, Any] | None:
        if router_id not in self.user_model_router_access.get(user_id, set()):
            return None
        row = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["status"] == "published"
                and item["deleted_at"] is None
                and item["published_config"] is not None
            ),
            None,
        )
        if row is None:
            return None
        return published_model_router_view(row)

    def list_model_routers_v3(self, owner_id: UUID) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.model_router_specs
            if row["owner_id"] == owner_id
            and row["deleted_at"] is None
        ]
        return [
            model_router_v3_view(row)
            for row in sorted(
                rows,
                key=lambda item: (item["updated_at"], item["id"]),
                reverse=True,
            )
        ]

    def get_model_router_v3(
        self, router_id: UUID, owner_id: UUID
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_router_specs
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        return model_router_v3_view(row) if row is not None else None

    def create_model_router_v3(
        self, owner_id: UUID, owner_email: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        alias = str(values["spec"]["identity"]["alias"]).casefold()
        if any(
            row["owner_id"] == owner_id
            and row["deleted_at"] is None
            and (
                str(row["name"]).casefold() == str(values["name"]).casefold()
                or str(row["alias"]).casefold() == alias
            )
            for row in self.model_router_specs
        ):
            return None
        now = datetime.now(UTC)
        row = {
            "id": uuid4(),
            "owner_id": owner_id,
            "name": values["name"],
            "description": values["description"],
            "alias": alias,
            "status": "draft",
            "version": 0,
            "draft_spec": deepcopy(values["spec"]),
            "published_spec": None,
            "published_plan": None,
            "created_by": owner_email,
            "updated_by": values["updated_by"],
            "created_at": now,
            "updated_at": now,
            "published_at": None,
            "deleted_at": None,
        }
        self.model_router_specs.append(row)
        return model_router_v3_view(row)

    def update_model_router_v3(
        self, router_id: UUID, owner_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_router_specs
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return None
        alias = str(values["spec"]["identity"]["alias"]).casefold()
        published_alias = (
            str(row["published_spec"]["identity"]["alias"]).casefold()
            if row["published_spec"] is not None
            else None
        )
        if published_alias is not None and alias != published_alias:
            return None
        if any(
            item["owner_id"] == owner_id
            and item["deleted_at"] is None
            and item["id"] != router_id
            and (
                str(item["name"]).casefold() == str(values["name"]).casefold()
                or str(item["alias"]).casefold() == alias
            )
            for item in self.model_router_specs
        ):
            return None
        row.update(
            name=values["name"],
            description=values["description"],
            alias=alias,
            draft_spec=deepcopy(values["spec"]),
            updated_by=values["updated_by"],
            updated_at=datetime.now(UTC),
        )
        return model_router_v3_view(row)

    def publish_model_router_v3(
        self,
        router_id: UUID,
        owner_id: UUID,
        published_by: str,
        compiled_plan: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_router_specs
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return None
        now = datetime.now(UTC)
        version = int(row["version"]) + 1
        published_spec = deepcopy(row["draft_spec"])
        published_plan = deepcopy(compiled_plan)
        self.model_router_spec_revisions.append(
            {
                "router_id": router_id,
                "version": version,
            "spec": published_spec,
                "compiled_plan": published_plan,
                "published_by": published_by,
                "published_at": now,
            }
        )
        row.update(
            status="published",
            version=version,
            published_spec=published_spec,
            published_plan=published_plan,
            published_at=now,
            updated_at=now,
            updated_by=published_by,
        )
        return model_router_v3_view(row)

    def disable_model_router_v3(
        self, router_id: UUID, owner_id: UUID, changed_by: str
    ) -> dict[str, Any] | None:
        row = next(
            (
                item
                for item in self.model_router_specs
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
                and item["status"] == "published"
                and item["published_spec"] is not None
                and item["published_plan"] is not None
            ),
            None,
        )
        if row is None:
            return None
        row.update(
            status="disabled",
            updated_at=datetime.now(UTC),
            updated_by=changed_by,
        )
        return model_router_v3_view(row)

    def delete_model_router_v3(self, router_id: UUID, owner_id: UUID) -> bool:
        row = next(
            (
                item
                for item in self.model_router_specs
                if item["id"] == router_id
                and item["owner_id"] == owner_id
                and item["deleted_at"] is None
            ),
            None,
        )
        if row is None:
            return False
        now = datetime.now(UTC)
        row.update(deleted_at=now, updated_at=now)
        return True

    def list_model_router_context_fields(
        self, references: Sequence[tuple[str, int]]
    ) -> list[dict[str, Any]]:
        selected = set(references)
        return [
            deepcopy(row)
            for row in self.model_router_context_fields
            if (str(row["name"]), int(row["version"])) in selected
        ]

    def list_model_router_model_facts(
        self, references: Sequence[tuple[UUID, int]]
    ) -> list[dict[str, Any]]:
        selected = set(references)
        return [
            deepcopy(row)
            for row in self.model_router_model_facts
            if (
                UUID(str(row["model_id"])),
                int(row["facts_version"]),
            ) in selected
            and row["enabled"]
        ]

    def list_model_router_profiles(
        self, references: Sequence[tuple[UUID, int]]
    ) -> list[dict[str, Any]]:
        selected = set(references)
        return [
            deepcopy(row)
            for row in self.model_router_profiles
            if (UUID(str(row["id"])), int(row["version"])) in selected
            and row["enabled"]
        ]

    def list_model_router_evaluations(
        self, evaluation_ids: Sequence[UUID]
    ) -> list[dict[str, Any]]:
        selected = set(evaluation_ids)
        return [
            deepcopy(row)
            for row in self.model_router_evaluations
            if UUID(str(row["id"])) in selected and row["enabled"]
        ]

    def set_model_router_assignment(
        self, router_id: UUID, user_id: str, assigned: bool, changed_by: str
    ) -> bool:
        router = next(
            (
                item
                for item in self.model_routers
                if item["id"] == router_id
                and item["status"] == "published"
                and item["deleted_at"] is None
                and item["published_config"] is not None
            ),
            None,
        )
        if router is None:
            return False
        previous = sorted(self.user_model_router_access.get(user_id, set()), key=str)
        selected = self.user_model_router_access.setdefault(user_id, set())
        if assigned:
            selected.add(router_id)
        else:
            selected.discard(router_id)
        now = datetime.now(UTC)
        self.user_model_router_policies[user_id] = {
            "user_id": user_id,
            "updated_at": now,
            "updated_by": changed_by,
        }
        next_ids = sorted(selected, key=str)
        if previous != next_ids:
            self.user_model_router_access_audit.append(
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "previous_router_ids": previous,
                    "new_router_ids": next_ids,
                    "changed_at": now,
                    "changed_by": changed_by,
                }
            )
        return True

    def list_model_router_assignees(self, router_id: UUID) -> list[str]:
        return sorted(
            user_id
            for user_id, router_ids in self.user_model_router_access.items()
            if router_id in router_ids
        )

    def get_model_router_run_binding(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_id: str,
    ) -> UUID | None:
        row = self.model_router_run_bindings.get(
            (router_id, router_version, user_id, run_id)
        )
        return UUID(str(row["selected_model_id"])) if row is not None else None

    def save_model_router_run_binding(
        self,
        router_id: UUID,
        router_version: int,
        user_id: str,
        run_id: str,
        selected_model_id: UUID,
        selected_model_name: str,
    ) -> None:
        key = (router_id, router_version, user_id, run_id)
        now = datetime.now(UTC)
        created_at = self.model_router_run_bindings.get(key, {}).get(
            "created_at", now
        )
        self.model_router_run_bindings[key] = {
            "router_id": router_id,
            "router_version": router_version,
            "user_id": user_id,
            "run_id": run_id,
            "selected_model_id": selected_model_id,
            "selected_model_name": selected_model_name,
            "created_at": created_at,
            "updated_at": now,
        }

    def save_model_router_invocation(
        self,
        request_id: str,
        router_id: UUID,
        router_name: str,
        router_version: int,
        selected_model_id: UUID,
        selected_model_name: str,
        attempts: Sequence[Mapping[str, Any]],
    ) -> None:
        self.model_router_invocations.setdefault(
            request_id,
            {
                "router_id": router_id,
                "router_name": router_name,
                "router_version": router_version,
                "selected_model_id": selected_model_id,
                "selected_model_name": selected_model_name,
                "router_attempts": [dict(item) for item in attempts],
            },
        )

