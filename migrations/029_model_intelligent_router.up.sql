CREATE TABLE model_router (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'disabled')),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    draft_config JSONB NOT NULL,
    published_config JSONB,
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    CHECK (jsonb_typeof(draft_config) = 'object'),
    CHECK (published_config IS NULL OR jsonb_typeof(published_config) = 'object'),
    CHECK (
        (version = 0 AND published_config IS NULL AND published_at IS NULL AND status = 'draft')
        OR (version > 0 AND published_config IS NOT NULL AND published_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX model_router_owner_name_idx
    ON model_router (owner_id, lower(name))
    WHERE deleted_at IS NULL;
CREATE INDEX model_router_owner_updated_idx
    ON model_router (owner_id, updated_at DESC, id)
    WHERE deleted_at IS NULL;

CREATE TABLE model_router_revision (
    router_id UUID NOT NULL REFERENCES model_router(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    published_by TEXT NOT NULL CHECK (length(trim(published_by)) > 0),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_id, version)
);

COMMENT ON TABLE model_router IS
    'User-owned model routing drafts with an independently retained active publication.';
COMMENT ON TABLE model_router_revision IS
    'Immutable published model-router versions for audit and rollback-ready history.';

CREATE TABLE user_model_router_policy (
    user_id TEXT PRIMARY KEY CHECK (length(trim(user_id)) > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0)
);

CREATE TABLE user_model_router_access (
    user_id TEXT NOT NULL REFERENCES user_model_router_policy(user_id) ON DELETE CASCADE,
    router_id UUID NOT NULL REFERENCES model_router(id) ON DELETE RESTRICT,
    PRIMARY KEY (user_id, router_id)
);

CREATE TABLE user_model_router_access_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    previous_router_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    new_router_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by TEXT NOT NULL CHECK (length(trim(changed_by)) > 0)
);

CREATE INDEX user_model_router_access_router_idx
    ON user_model_router_access (router_id, user_id);
CREATE INDEX user_model_router_access_audit_user_changed_idx
    ON user_model_router_access_audit (user_id, changed_at DESC);

CREATE TABLE model_router_invocation (
    request_id TEXT PRIMARY KEY,
    router_id UUID NOT NULL REFERENCES model_router(id) ON DELETE RESTRICT,
    router_name TEXT NOT NULL CHECK (length(trim(router_name)) > 0),
    router_version INTEGER NOT NULL CHECK (router_version > 0),
    selected_model_id UUID NOT NULL,
    selected_model_name TEXT NOT NULL CHECK (length(trim(selected_model_name)) > 0),
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    attempts JSONB NOT NULL CHECK (jsonb_typeof(attempts) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX model_router_invocation_router_created_idx
    ON model_router_invocation (router_id, created_at DESC);

COMMENT ON TABLE user_model_router_access IS
    'Explicit per-person access to published Model Routers, separate from managed-model access.';
COMMENT ON TABLE model_router_invocation IS
    'Router decision sidecar keyed by the concrete token_usage request_id.';
COMMENT ON COLUMN model_router_invocation.selected_model_id IS
    'Immutable selected-model identity snapshot; intentionally not an FK so model removal cannot erase or block trace history.';

CREATE TABLE model_router_run_binding (
    router_id UUID NOT NULL REFERENCES model_router(id) ON DELETE RESTRICT,
    router_version INTEGER NOT NULL CHECK (router_version > 0),
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    run_id TEXT NOT NULL CHECK (length(trim(run_id)) > 0),
    selected_model_id UUID NOT NULL,
    selected_model_name TEXT NOT NULL CHECK (length(trim(selected_model_name)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_id, router_version, user_id, run_id)
);

CREATE INDEX model_router_run_binding_user_updated_idx
    ON model_router_run_binding (user_id, updated_at DESC);

COMMENT ON TABLE model_router_run_binding IS
    'Durable per-run Router selection, scoped to one published revision and person.';
COMMENT ON COLUMN model_router_run_binding.selected_model_id IS
    'Selected-model identity snapshot; not an FK so catalog removal cannot erase run history.';

CREATE TABLE model_router_spec (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    alias TEXT NOT NULL CHECK (alias ~ '^[a-z][a-z0-9-]{2,62}$'),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'disabled')),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    draft_spec JSONB NOT NULL CHECK (jsonb_typeof(draft_spec) = 'object'),
    published_spec JSONB CHECK (published_spec IS NULL OR jsonb_typeof(published_spec) = 'object'),
    published_plan JSONB CHECK (published_plan IS NULL OR jsonb_typeof(published_plan) = 'object'),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    CHECK (
        draft_spec ?& ARRAY['schema_version', 'identity']
        AND (draft_spec ->> 'schema_version')::INTEGER = 3
        AND draft_spec #>> '{identity,alias}' IS NOT NULL
        AND alias = draft_spec #>> '{identity,alias}'
    ),
    CHECK (
        published_spec IS NULL
        OR (
            published_spec ?& ARRAY['schema_version', 'identity']
            AND (published_spec ->> 'schema_version')::INTEGER = 3
            AND alias = published_spec #>> '{identity,alias}'
        )
    ),
    CHECK (
        published_plan IS NULL
        OR (
            published_plan ?& ARRAY['source_schema_version', 'plan_sha256', 'identity']
            AND (published_plan ->> 'source_schema_version')::INTEGER = 3
            AND published_plan ->> 'plan_sha256' ~ '^[0-9a-f]{64}$'
            AND alias = published_plan #>> '{identity,alias}'
        )
    ),
    CHECK (
        (version = 0 AND published_spec IS NULL AND published_plan IS NULL
            AND published_at IS NULL AND status = 'draft')
        OR (version > 0 AND published_spec IS NOT NULL AND published_plan IS NOT NULL
            AND published_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX model_router_spec_owner_name_idx
    ON model_router_spec (owner_id, lower(name))
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX model_router_spec_owner_alias_idx
    ON model_router_spec (owner_id, lower(alias))
    WHERE deleted_at IS NULL;
CREATE INDEX model_router_spec_owner_updated_idx
    ON model_router_spec (owner_id, updated_at DESC, id)
    WHERE deleted_at IS NULL;

CREATE TABLE model_router_spec_revision (
    router_id UUID NOT NULL REFERENCES model_router_spec(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    spec JSONB NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
    compiled_plan JSONB NOT NULL CHECK (jsonb_typeof(compiled_plan) = 'object'),
    published_by TEXT NOT NULL CHECK (length(trim(published_by)) > 0),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_id, version),
    CHECK (
        spec ?& ARRAY['schema_version', 'identity']
        AND (spec ->> 'schema_version')::INTEGER = 3
        AND spec #>> '{identity,alias}' IS NOT NULL
    ),
    CHECK (
        compiled_plan ?& ARRAY['source_schema_version', 'plan_sha256', 'identity']
        AND (compiled_plan ->> 'source_schema_version')::INTEGER = 3
        AND compiled_plan ->> 'plan_sha256' ~ '^[0-9a-f]{64}$'
        AND spec #>> '{identity,alias}' = compiled_plan #>> '{identity,alias}'
    )
);

CREATE TABLE model_router_context_field (
    name TEXT NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$'),
    version INTEGER NOT NULL CHECK (version > 0),
    value_type TEXT NOT NULL
        CHECK (value_type IN ('string', 'integer', 'number', 'boolean')),
    source TEXT NOT NULL
        CHECK (source IN ('authenticated_identity', 'server_derived', 'client_claim')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    PRIMARY KEY (name, version)
);

INSERT INTO model_router_context_field (
    name, version, value_type, source, created_by
) VALUES
    ('user_id', 1, 'string', 'authenticated_identity', 'migration-029'),
    ('task_type', 1, 'string', 'client_claim', 'migration-029'),
    ('request_parameter', 1, 'string', 'server_derived', 'migration-029'),
    ('prompt_length', 1, 'integer', 'server_derived', 'migration-029'),
    ('required_capability', 1, 'string', 'server_derived', 'migration-029'),
    ('stream', 1, 'boolean', 'server_derived', 'migration-029'),
    ('tools_present', 1, 'boolean', 'server_derived', 'migration-029');

CREATE TABLE model_router_model_facts (
    model_id UUID NOT NULL REFERENCES managed_model(id) ON DELETE RESTRICT,
    facts_version INTEGER NOT NULL CHECK (facts_version > 0),
    logical_model_key TEXT NOT NULL CHECK (length(trim(logical_model_key)) > 0),
    supported_operations TEXT[] NOT NULL
        CHECK (
            cardinality(supported_operations) > 0
            AND supported_operations <@ ARRAY['chat_completions', 'responses']::TEXT[]
        ),
    capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    foundry_router_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    context_window INTEGER CHECK (context_window > 0),
    provider_id UUID NOT NULL REFERENCES model_provider(id) ON DELETE RESTRICT,
    data_zone TEXT NOT NULL CHECK (length(trim(data_zone)) > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    PRIMARY KEY (model_id, facts_version)
);

CREATE UNIQUE INDEX model_router_model_facts_one_enabled_idx
    ON model_router_model_facts (model_id)
    WHERE enabled;

CREATE TABLE model_router_profile (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    kind TEXT NOT NULL CHECK (kind = 'qos'),
    config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    PRIMARY KEY (id, version),
    UNIQUE (name, version),
    CHECK (
        config ?& ARRAY[
            'weights', 'latency_slo_ms', 'cost_reference_per_million',
            'minimum_success_rate'
        ]
        AND jsonb_typeof(config -> 'weights') = 'object'
        AND (config -> 'weights') ?& ARRAY['quality', 'cost', 'latency', 'reliability']
        AND jsonb_typeof(config -> 'weights' -> 'quality') = 'number'
        AND jsonb_typeof(config -> 'weights' -> 'cost') = 'number'
        AND jsonb_typeof(config -> 'weights' -> 'latency') = 'number'
        AND jsonb_typeof(config -> 'weights' -> 'reliability') = 'number'
        AND (config #>> '{weights,quality}')::NUMERIC BETWEEN 0 AND 1
        AND (config #>> '{weights,cost}')::NUMERIC BETWEEN 0 AND 1
        AND (config #>> '{weights,latency}')::NUMERIC BETWEEN 0 AND 1
        AND (config #>> '{weights,reliability}')::NUMERIC BETWEEN 0 AND 1
        AND (
            (config #>> '{weights,quality}')::NUMERIC
            + (config #>> '{weights,cost}')::NUMERIC
            + (config #>> '{weights,latency}')::NUMERIC
            + (config #>> '{weights,reliability}')::NUMERIC
        ) = 1
        AND jsonb_typeof(config -> 'latency_slo_ms') = 'number'
        AND (config ->> 'latency_slo_ms')::INTEGER BETWEEN 1 AND 300000
        AND jsonb_typeof(config -> 'cost_reference_per_million') = 'number'
        AND (config ->> 'cost_reference_per_million')::NUMERIC > 0
        AND jsonb_typeof(config -> 'minimum_success_rate') = 'number'
        AND (config ->> 'minimum_success_rate')::NUMERIC BETWEEN 0 AND 1
        AND (
            NOT config ? 'quality_floor'
            OR config -> 'quality_floor' = 'null'::JSONB
            OR (
                jsonb_typeof(config -> 'quality_floor') = 'number'
                AND (config ->> 'quality_floor')::NUMERIC BETWEEN 0 AND 1
            )
        )
    )
);

CREATE TABLE model_router_evaluation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    routing_model_id UUID NOT NULL,
    routing_model_facts_version INTEGER NOT NULL CHECK (routing_model_facts_version > 0),
    model_ids UUID[] NOT NULL CHECK (cardinality(model_ids) > 0),
    passed BOOLEAN NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    FOREIGN KEY (routing_model_id, routing_model_facts_version)
        REFERENCES model_router_model_facts(model_id, facts_version) ON DELETE RESTRICT
);

CREATE FUNCTION reject_model_router_registry_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% rows are immutable; disable the version instead', TG_TABLE_NAME;
    END IF;
    IF (to_jsonb(NEW) - 'enabled') IS DISTINCT FROM (to_jsonb(OLD) - 'enabled') THEN
        RAISE EXCEPTION '% version content is immutable', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER model_router_context_field_immutable
BEFORE UPDATE OR DELETE ON model_router_context_field
FOR EACH ROW EXECUTE FUNCTION reject_model_router_registry_rewrite();

CREATE TRIGGER model_router_model_facts_immutable
BEFORE UPDATE OR DELETE ON model_router_model_facts
FOR EACH ROW EXECUTE FUNCTION reject_model_router_registry_rewrite();

CREATE TRIGGER model_router_profile_immutable
BEFORE UPDATE OR DELETE ON model_router_profile
FOR EACH ROW EXECUTE FUNCTION reject_model_router_registry_rewrite();

CREATE TRIGGER model_router_evaluation_immutable
BEFORE UPDATE OR DELETE ON model_router_evaluation
FOR EACH ROW EXECUTE FUNCTION reject_model_router_registry_rewrite();

COMMENT ON TABLE model_router_spec IS
    'Typed schema-3 Router drafts with a stable alias and independently retained active plan.';
COMMENT ON TABLE model_router_spec_revision IS
    'Immutable typed Router specifications and content-addressed compiled plans.';
COMMENT ON TABLE model_router_context_field IS
    'Versioned trusted input-field catalog for typed Router specifications.';
COMMENT ON TABLE model_router_model_facts IS
    'Explicit versioned operation, capability, provider, and data-zone facts used at Router compilation.';
COMMENT ON TABLE model_router_profile IS
    'Immutable strongly typed versioned QoS profile definitions embedded into Router plans.';
COMMENT ON TABLE model_router_evaluation IS
    'Versioned release evidence gating custom semantic Routing Models and candidate sets.';
