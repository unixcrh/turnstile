import type { GatewayBackendPoolMember, GatewayBackendPoolWrite, ManagedModel } from "../../data-sources/apim/types"

export type DeploymentMode = "failover" | "balanced" | "weighted" | "custom"

export function poolMemberIdentity(member: Pick<GatewayBackendPoolMember, "runtime_id" | "model_id">) {
  return member.model_id ?? member.runtime_id
}

export function poolDeploymentModelId(
  member: Pick<GatewayBackendPoolMember, "runtime_id" | "model_id" | "upstream_model_id">,
  primary: ManagedModel,
  models: ManagedModel[],
) {
  if (member.model_id) return member.model_id
  const upstream = (member.upstream_model_id ?? primary.upstream_model_id ?? primary.model_key).trim().toLocaleLowerCase()
  if (member.runtime_id === primary.runtime_id
    && upstream === (primary.upstream_model_id ?? primary.model_key).trim().toLocaleLowerCase()) return primary.id
  return models.find(model => model.runtime_id === member.runtime_id
    && (model.upstream_model_id ?? model.model_key).trim().toLocaleLowerCase() === upstream)?.id
    ?? member.runtime_id
}

export function deploymentMode(
  members: GatewayBackendPoolWrite["members"],
  primaryMemberId: string,
): DeploymentMode {
  const primary = members.find(member => poolMemberIdentity(member) === primaryMemberId)
  const replicas = members.filter(member => poolMemberIdentity(member) !== primaryMemberId)
  if (!primary || !replicas.length) return "failover"
  const equalWeights = new Set(members.map(member => member.weight)).size === 1
  if (new Set(members.map(member => member.priority)).size === 1) {
    return equalWeights ? "balanced" : "weighted"
  }
  if (equalWeights
    && replicas.every(member => member.priority > primary.priority)
    && new Set(replicas.map(member => member.priority)).size === 1) return "failover"
  return "custom"
}

export function membersForMode(
  members: GatewayBackendPoolWrite["members"],
  mode: Exclude<DeploymentMode, "custom">,
  primaryMemberId: string,
) {
  const hasCustomWeights = new Set(members.map(member => member.weight)).size > 1
  return members.map(member => ({
    ...member,
    priority: mode === "failover" && poolMemberIdentity(member) !== primaryMemberId ? 1 : 0,
    weight: mode === "weighted"
      ? hasCustomWeights ? Math.max(1, member.weight) : poolMemberIdentity(member) === primaryMemberId ? 2 : 1
      : 1,
  }))
}

export function poolPublicationPayload(
  draft: GatewayBackendPoolWrite,
  affinitySupported: boolean,
): GatewayBackendPoolWrite {
  if (affinitySupported) return { ...draft, session_affinity: draft.session_affinity === true }
  if (draft.session_affinity) throw new Error("当前后端未发布会话黏着能力。")
  const { session_affinity: _affinity, ...legacy } = draft
  return legacy
}