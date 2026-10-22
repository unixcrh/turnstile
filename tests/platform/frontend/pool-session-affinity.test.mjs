import assert from "node:assert/strict"
import test from "node:test"

import {
  equivalentNativeRouteRuntimes,
  nativeRouteRuntimeIssue,
  previewNativeRouteDeployments,
} from "../../../frontend/src/components/model-management/apim-native-route-eligibility.ts"
import {
  deploymentMode,
  membersForMode,
  poolDeploymentModelId,
  poolMemberIdentity,
  poolPublicationPayload,
} from "../../../frontend/src/components/model-management/pool-session-affinity.ts"

const draft = {
  members: [
    { runtime_id: "one", priority: 0, weight: 1 },
    { runtime_id: "two", priority: 0, weight: 1 },
  ],
  rate_limit: { max_attempts_per_request: 2 },
}

test("two deployments in one Runtime keep separate membership and failover priority", () => {
  const members = [
    { runtime_id: "one-project", model_id: "deployment-a", priority: 0, weight: 1 },
    { runtime_id: "one-project", model_id: "deployment-b", priority: 1, weight: 1 },
  ]
  assert.deepEqual(members.map(poolMemberIdentity), ["deployment-a", "deployment-b"])
  assert.equal(deploymentMode(members, "deployment-a"), "failover")
  assert.deepEqual(membersForMode(members, "failover", "deployment-a"), members)
  const balanced = membersForMode(members, "balanced", "deployment-a")
  assert.deepEqual(balanced.map(member => member.priority), [0, 0])
  assert.equal(deploymentMode(balanced, "deployment-a"), "balanced")
  const weighted = membersForMode(balanced, "weighted", "deployment-a")
  assert.deepEqual(weighted.map(member => member.weight), [2, 1])
  assert.equal(deploymentMode(weighted, "deployment-a"), "weighted")
})

test("legacy member resolution uses the deployment name rather than only the Project", () => {
  const primary = { id: "model-a", runtime_id: "project", upstream_model_id: "deployment-a" }
  const replica = { id: "model-b", runtime_id: "project", upstream_model_id: "deployment-b" }
  const models = [primary, replica]
  assert.equal(poolDeploymentModelId({ runtime_id: "project" }, primary, models), "model-a")
  assert.equal(poolDeploymentModelId({ runtime_id: "project", upstream_model_id: "deployment-b" }, primary, models), "model-b")
  assert.equal(poolDeploymentModelId({ runtime_id: "project", model_id: "model-b" }, primary, models), "model-b")
  assert.equal(poolMemberIdentity({ runtime_id: "legacy" }), "legacy")
})

test("deployment IDs survive the publication payload without changing defaults", () => {
  const value = { ...draft, members: [
    { runtime_id: "project", model_id: "deployment-a", priority: 0, weight: 1 },
    { runtime_id: "project", model_id: "deployment-b", priority: 1, weight: 1 },
  ] }
  assert.deepEqual(poolPublicationPayload(value, false), value)
  assert.deepEqual(poolPublicationPayload(value, true).members, value.members)
})

test("legacy backend payload omits a false or missing affinity field", () => {
  assert.deepEqual(poolPublicationPayload(draft, false), draft)
  assert.deepEqual(poolPublicationPayload({ ...draft, session_affinity: false }, false), draft)
})

test("unsupported affinity cannot silently downgrade to ordinary balancing", () => {
  assert.throws(() => poolPublicationPayload({ ...draft, session_affinity: true }, false))
})

test("capable backends receive an explicit boolean without altering member weights", () => {
  for (const enabled of [false, true]) {
    assert.deepEqual(poolPublicationPayload({ ...draft, session_affinity: enabled }, true),
      { ...draft, session_affinity: enabled })
  }
  assert.deepEqual(poolPublicationPayload(draft, true), { ...draft, session_affinity: false })
})

test("payload conversion does not mutate the in-progress draft", () => {
  const value = { ...draft, session_affinity: false }
  const before = structuredClone(value)
  const result = poolPublicationPayload(value, false)
  assert.notEqual(result, value)
  assert.deepEqual(value, before)
})

function poolRegistry() {
  const primary = {
    id: "primary",
    name: "Primary connection",
    enabled: true,
    provider_id: "foundry",
    gateway_profile_id: "gateway",
    brand_key: "microsoft_foundry",
    config: {
      control_plane_managed: true,
      backend_url: "https://primary.example.com",
      backend_path: "/chat/completions",
      auth_strategy: "managed_identity",
      managed_identity_resource: "https://cognitiveservices.azure.com",
    },
  }
  const replica = { ...structuredClone(primary), id: "replica", name: "Replica connection" }
  const missing = { ...structuredClone(primary), id: "missing", name: "Unregistered deployment" }
  const model = {
    id: "model",
    enabled: true,
    provider_id: "foundry",
    runtime_id: primary.id,
    model_key: "model-public",
    upstream_model_id: "Model-A",
    capabilities: ["chat"],
  }
  return {
    model,
    primary,
    replica,
    missing,
    registry: {
      runtimes: [primary, replica, missing],
      models: [model, { ...model, id: "replica-model", runtime_id: replica.id, upstream_model_id: " model-a " }],
    },
  }
}

test("pool candidates expose missing deployment without changing equivalent membership", () => {
  const { model, primary, replica, missing, registry } = poolRegistry()
  assert.deepEqual(equivalentNativeRouteRuntimes(model, registry).map(runtime => runtime.id), ["primary", "replica"])
  assert.equal(nativeRouteRuntimeIssue(model, primary, registry), null)
  assert.equal(nativeRouteRuntimeIssue(model, replica, registry), null)
  assert.equal(nativeRouteRuntimeIssue(model, missing, registry), "缺少同名 Deployment")
})

test("visible rejection reasons preserve each existing pool compatibility rule", () => {
  const cases = [
    ["连接已停用", runtime => { runtime.enabled = false }],
    ["提供方不一致", runtime => { runtime.provider_id = "other" }],
    ["Gateway 不一致", runtime => { runtime.gateway_profile_id = "other" }],
    ["连接未受管", runtime => { runtime.config.control_plane_managed = false }],
    ["后端地址或路径缺失", runtime => { delete runtime.config.backend_url }],
    ["API 协议不一致", runtime => { runtime.config.api_format = "anthropic_messages" }],
    ["后端路径不一致", runtime => { runtime.config.backend_path = "/different" }],
    ["认证配置不兼容", runtime => { runtime.config.managed_identity_resource = "https://other.example.com" }],
    ["认证配置不兼容", runtime => { runtime.config.auth_strategy = "named_value_api_key" }],
    ["流式模式不一致", runtime => { runtime.config.streaming_mode = "buffered" }],
  ]
  for (const [expected, change] of cases) {
    const { model, replica, registry } = poolRegistry()
    change(replica)
    assert.equal(nativeRouteRuntimeIssue(model, replica, registry), expected)
    assert.deepEqual(equivalentNativeRouteRuntimes(model, registry).map(runtime => runtime.id), ["primary"])
  }
})

test("provisioned API-key replicas remain compatible with a managed-identity primary", () => {
  const { model, replica, registry } = poolRegistry()
  replica.config.auth_strategy = "named_value_api_key"
  replica.config.named_value_name = "registered-key"
  replica.config.credential_provisioned = true
  assert.equal(nativeRouteRuntimeIssue(model, replica, registry), null)
  assert.deepEqual(equivalentNativeRouteRuntimes(model, registry).map(runtime => runtime.id), ["primary", "replica"])
})

test("a model with a different deployment name is not silently treated as equivalent", () => {
  const { model, replica, registry } = poolRegistry()
  registry.models[1].upstream_model_id = "Model-B"
  assert.equal(nativeRouteRuntimeIssue(model, replica, registry), "缺少同名 Deployment")
  registry.models[1].upstream_model_id = "Model-A"
  registry.models[1].enabled = false
  assert.equal(nativeRouteRuntimeIssue(model, replica, registry), "缺少同名 Deployment")
})

test("registered deployments preview immediately without requiring a second Runtime", () => {
  const { model, registry } = poolRegistry()
  registry.models.push({ ...model, id: "numbered-replica", upstream_model_id: "Model-A-2" })
  assert.deepEqual(previewNativeRouteDeployments(model, registry).map(item => item.id),
    ["model", "replica-model", "numbered-replica"])
})

test("deployment preview retains capability, identity, protocol and duplicate filters", () => {
  for (const change of [
    candidate => { candidate.enabled = false },
    candidate => { candidate.provider_id = "other" },
    candidate => { candidate.capabilities = ["image_generation"] },
    candidate => { candidate.capabilities = ["chat", "decision"] },
    candidate => { candidate.family_key = "claude" },
    candidate => { candidate.upstream_model_id = "Model-B" },
    candidate => { candidate.upstream_model_id = "Model-A-2026-10-01" },
  ]) {
    const { model, registry } = poolRegistry()
    change(registry.models[1])
    assert.deepEqual(previewNativeRouteDeployments(model, registry).map(item => item.id), ["model"])
  }
  const { model, replica, registry } = poolRegistry()
  registry.models.push({ ...registry.models[1], id: "duplicate" })
  assert.equal(previewNativeRouteDeployments(model, registry).length, 2)
  replica.config.backend_path = "/different"
  assert.deepEqual(previewNativeRouteDeployments(model, registry).map(item => item.id), ["model"])
})