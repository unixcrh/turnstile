import {
  AlertCircle,
  Boxes,
  CheckCircle2,
  GitBranch,
  Settings2,
} from "lucide-react"
import { useState } from "react"

import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import { Input } from "../ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import { Textarea } from "../ui/textarea"
import type {
  ManagedModel,
  ModelRouterConditionOperator,
  ModelRouterDefinition,
  ModelRouterFallbackEvent,
  ModelRouterModelReference,
  ModelRouterNode,
  ModelRouterStrategyType,
  ModelRouterValidation,
  ModelRouterWrite,
} from "../../data-sources/apim/types"
import {
  cloneDefinition,
  enabledCandidateModelIds,
  routerModelStatusLabel,
  syncStrategyModelConfigs,
} from "./router-definition"
import { StrategyModelEditor } from "./strategy-model-editor"
import { DecisionPromptEditor } from "./decision-prompt-editor"


function numberOrNull(value: string): number | null {
  if (!value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function updateNode(
  definition: ModelRouterDefinition,
  node: ModelRouterNode,
): ModelRouterDefinition {
  return {
    ...cloneDefinition(definition),
    nodes: definition.nodes.map((item) => item.id === node.id ? node : item),
  }
}

type RouterBranch = {
  candidate: ModelRouterNode
  condition: ModelRouterNode | null
  routeLabel: string
  strategy: ModelRouterNode | null
  fallback: ModelRouterNode | null
}

function routerBranches(definition: ModelRouterDefinition): RouterBranch[] {
  const nodesById = new Map(definition.nodes.map((node) => [node.id, node]))
  return definition.nodes
    .filter((node) => node.type === "candidate_models")
    .map((candidate) => {
      const incoming = definition.edges.find((edge) =>
        edge.target === candidate.id && nodesById.get(edge.source)?.type === "condition",
      )
      const condition = incoming ? nodesById.get(incoming.source) ?? null : null
      const strategyEdge = definition.edges.find((edge) =>
        edge.source === candidate.id
        && ["strategy", "priority"].includes(nodesById.get(edge.target)?.type ?? ""),
      )
      const strategy = strategyEdge ? nodesById.get(strategyEdge.target) ?? null : null
      const fallbackEdge = strategy
        ? definition.edges.find((edge) =>
            edge.source === strategy.id && nodesById.get(edge.target)?.type === "fallback",
          )
        : null
      return {
        candidate,
        condition,
        routeLabel: incoming?.label.trim() || candidate.label,
        strategy,
        fallback: fallbackEdge ? nodesById.get(fallbackEdge.target) ?? null : null,
      }
    })
}

function StrategyConfiguration({
  definition,
  node,
  models,
  displayModels,
  onChange,
  onOpenCanvas,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode | null
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  onChange: (definition: ModelRouterDefinition) => void
  onOpenCanvas: () => void
}) {
  if (!node) {
    return <div className="router-config-missing"><span>该分支尚未连接 Strategy 节点。</span><Button type="button" variant="outline" onClick={onOpenCanvas}>前往画布配置</Button></div>
  }
  const setStrategy = (strategyType: ModelRouterStrategyType) => {
    const candidateIds = enabledCandidateModelIds(definition)
    onChange(updateNode(definition, {
      ...node,
      strategy_type: strategyType,
      routing_model_id: strategyType === "model_based"
        ? node.routing_model_id ?? models.find((model) => model.capabilities.includes("decision"))?.id ?? candidateIds[0] ?? null
        : null,
    }))
  }
  const updateStrategyNode = (nextNode: ModelRouterNode) => {
    onChange(updateNode(definition, nextNode))
  }
  const routingModel = displayModels.find((model) => model.id === node.routing_model_id)
  return <div className="router-strategy-config">
    <label className="router-field"><span>选择策略</span>
      <Select value={node.strategy_type ?? "priority"} onValueChange={(value) => setStrategy((value ?? "priority") as ModelRouterStrategyType)}>
        <SelectTrigger aria-label="常规配置选择策略"><SelectValue>{{ priority: "优先级顺序", weighted: "加权选择", model_based: "模型判路由" }[node.strategy_type ?? "priority"]}</SelectValue></SelectTrigger>
        <SelectContent>
          <SelectItem value="priority">优先级顺序</SelectItem>
          <SelectItem value="weighted">加权选择</SelectItem>
          <SelectItem value="model_based">模型判路由</SelectItem>
        </SelectContent>
      </Select>
    </label>
    {node.strategy_type === "model_based" && <label className="router-field"><span>Routing Model</span>
      <Select value={node.routing_model_id ?? ""} onValueChange={(value) => updateStrategyNode({ ...node, routing_model_id: value ?? null })}>
        <SelectTrigger aria-label="常规配置选择 Routing Model" title={node.routing_model_id ?? undefined}>
          <SelectValue>{node.routing_model_id ? <>
            <span>{routingModel?.display_name ?? "已不可用的模型"}</span>
            {routingModel && routingModel.status !== "available" && <small>{routerModelStatusLabel(routingModel.status)}</small>}
          </> : "选择判路由模型"}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {models.map((model) => <SelectItem key={model.id} value={model.id}>{model.display_name}</SelectItem>)}
        </SelectContent>
      </Select>
    </label>}
    {node.strategy_type === "model_based" && <DecisionPromptEditor definition={definition} node={node} models={models} onChange={updateStrategyNode} />}
    <label className="router-field"><span>候选调用超时 ms</span><Input type="number" min={1} value={node.timeout_ms ?? ""} onChange={(event) => updateStrategyNode({ ...node, timeout_ms: numberOrNull(event.target.value) })} /></label>
    <div className="router-config-inline-empty">{node.strategy_type === "weighted" ? "根据权重稳定选择首选候选；Fallback 时继续尝试其余候选。" : node.strategy_type === "model_based" ? "Routing Model 从当前候选池选择最合适的模型；判路由失败时按优先级继续。" : "在下方排列候选模型，列表顺序就是执行优先级。"}</div>
    <StrategyModelEditor definition={definition} node={node} models={displayModels} onChange={updateStrategyNode} />
  </div>
}

function CandidateConfiguration({
  definition,
  node,
  models,
  onChange,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode
  models: ManagedModel[]
  onChange: (definition: ModelRouterDefinition) => void
}) {
  const candidatePoolModels = models.filter((model) => !model.capabilities.includes("decision"))
  const configured = new Set(node.candidates.map((candidate) => candidate.model_id))
  const toggleModel = (modelId: string, checked: boolean) => {
    const selected = configured.has(modelId)
    const nextNode = structuredClone(node)
    if (checked && !selected) {
      nextNode.candidates.push({ model_id: modelId })
    } else if (!checked && selected) {
      nextNode.candidates = nextNode.candidates.filter((candidate) => candidate.model_id !== modelId)
    } else {
      return
    }
    const next = syncStrategyModelConfigs(updateNode(definition, nextNode))
    const remaining = enabledCandidateModelIds(next)
    if (!next.default_model_id || !remaining.includes(next.default_model_id)) {
      next.default_model_id = remaining[0] ?? null
    }
    onChange(next)
  }
  return <div className="router-candidate-pool" role="group" aria-label="候选模型池">
      {candidatePoolModels.map((model) => {
        const selected = configured.has(model.id)
        return <label className="router-candidate-pool-item" data-selected={selected} key={model.id}><Checkbox checked={selected} onCheckedChange={(checked) => toggleModel(model.id, checked === true)} /><span className="router-model-logo" /><span><b>{model.display_name}</b><small>{model.provider_name} · {model.capabilities.join(" / ")}</small></span></label>
      })}
      {!candidatePoolModels.length && <div className="router-config-empty">当前用户没有可用于模型路由的模型。</div>}
  </div>
}

function FallbackConfiguration({
  definition,
  node,
  onChange,
  onOpenCanvas,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode | null
  onChange: (definition: ModelRouterDefinition) => void
  onOpenCanvas: () => void
}) {
  if (!node) {
    return <div className="router-config-missing"><span>未连接 Fallback 节点；调用失败时不会继续尝试其他候选模型。</span><Button type="button" variant="outline" onClick={onOpenCanvas}>前往画布配置</Button></div>
  }
  const setEvent = (event: ModelRouterFallbackEvent, checked: boolean) => {
    onChange(updateNode(definition, {
      ...node,
      fallback_on: checked
        ? [...node.fallback_on, event]
        : node.fallback_on.filter((value) => value !== event),
    }))
  }
  return <div className="router-fallback-config">
    <div className="router-fallback-events">
      {(["error", "timeout", "rate_limit"] as const).map((event) => <label key={event}><Checkbox checked={node.fallback_on.includes(event)} onCheckedChange={(checked) => setEvent(event, checked === true)} /><span>{{ error: "调用失败", timeout: "超时", rate_limit: "限流" }[event]}</span></label>)}
      {node.fallback_on.includes("quality") && <label><Checkbox checked onCheckedChange={() => setEvent("quality", false)} /><span>质量不足（未支持，请移除）</span></label>}
    </div>
  </div>
}

function ConditionConfiguration({
  definition,
  node,
  onChange,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode
  onChange: (definition: ModelRouterDefinition) => void
}) {
  const update = (nextNode: ModelRouterNode) => onChange(updateNode(definition, nextNode))
  const routes = definition.edges
    .filter((edge) => edge.source === node.id)
    .map((edge) => edge.label.trim())
    .filter(Boolean)
  return <div className="router-condition-config">
    <div className="router-condition-heading"><b>{node.label}</b><small>{routes.join(" / ") || "未连接分支"}</small></div>
    <div className="router-condition-grid">
      <label className="router-field"><span>判断字段</span>
        <Select value={node.condition?.field ?? "task_type"} onValueChange={(value) => update({ ...node, condition: { field: value ?? "task_type", operator: node.condition?.operator ?? "equals", value: node.condition?.value ?? "" } })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="task_type">任务类型</SelectItem>
            <SelectItem value="request_parameter">请求参数</SelectItem>
            <SelectItem value="prompt_length">问题长度</SelectItem>
            <SelectItem value="user_id">用户标识</SelectItem>
            <SelectItem value="required_capability">所需能力</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="router-field"><span>操作符</span>
        <Select value={node.condition?.operator ?? "equals"} onValueChange={(value) => update({ ...node, condition: { field: node.condition?.field ?? "task_type", operator: (value ?? "equals") as ModelRouterConditionOperator, value: node.condition?.value ?? "" } })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="equals">等于</SelectItem>
            <SelectItem value="not_equals">不等于</SelectItem>
            <SelectItem value="contains">包含</SelectItem>
            <SelectItem value="matches_any">包含任一关键词</SelectItem>
            <SelectItem value="greater_than">大于</SelectItem>
            <SelectItem value="greater_than_or_equal">大于或等于</SelectItem>
            <SelectItem value="less_than">小于</SelectItem>
            <SelectItem value="less_than_or_equal">小于或等于</SelectItem>
            <SelectItem value="exists">存在</SelectItem>
          </SelectContent>
        </Select>
      </label>
      {node.condition?.operator !== "exists" && <label className="router-field"><span>比较值</span><Input placeholder={node.condition?.operator === "matches_any" ? "多个关键词用逗号分隔，如：证明, 算法, theorem" : undefined} value={String(node.condition?.value ?? "")} onChange={(event) => update({ ...node, condition: { field: node.condition?.field ?? "task_type", operator: node.condition?.operator ?? "equals", value: event.target.value } })} /></label>}
    </div>
  </div>
}

export function RouterConfigForm({
  value,
  models,
  displayModels,
  validation,
  onChange,
  onIssueSelect,
}: {
  value: ModelRouterWrite
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  validation: ModelRouterValidation | null
  onChange: (value: ModelRouterWrite) => void
  onIssueSelect: (nodeId: string) => void
}) {
  const conditions = value.definition.nodes.filter((node) => node.type === "condition")
  const branches = routerBranches(value.definition)
  const [activeBranchId, setActiveBranchId] = useState(
    () => branches[0]?.candidate.id ?? "",
  )
  const activeBranch = branches.find(
    (branch) => branch.candidate.id === activeBranchId,
  ) ?? branches[0] ?? null
  return <div className="router-config-scroll">
    <section className="router-config-section router-global-section">
      <header><div><Settings2 size={16} /><span><b>基础信息</b><small>路由器标题与说明</small></span></div></header>
      <div className="router-config-grid">
        <label className="router-field"><span>标题</span><Input value={value.name} maxLength={120} onChange={(event) => onChange({ ...value, name: event.target.value })} /></label>
        <label className="router-field span-2"><span>描述</span><Textarea value={value.description} maxLength={500} rows={3} onChange={(event) => onChange({ ...value, description: event.target.value })} /></label>
      </div>
    </section>

    {conditions.length > 0 && <section className="router-config-section router-condition-section">
      <header><div><GitBranch size={16} /><span><b>路由条件</b><small>编辑判断语义；分支连接关系仍在画布中维护</small></span></div><em>{`${conditions.length} 个条件`}</em></header>
      <div className="router-condition-list">{conditions.map((node) => <ConditionConfiguration key={node.id} definition={value.definition} node={node} onChange={(definition) => onChange({ ...value, definition })} />)}</div>
    </section>}

    <section className="router-config-section router-branches-section">
      <header><div><Boxes size={16} /><span><b>路由分支</b><small>每条分支独立配置候选模型、选择策略和失败处理</small></span></div><em>{`${branches.length} 条分支`}</em></header>
      {branches.length > 0 ? <>
        <div className="router-branch-tabs" role="tablist" aria-label="路由分支">
          {branches.map((branch) => <button type="button" role="tab" aria-selected={activeBranch?.candidate.id === branch.candidate.id} className={activeBranch?.candidate.id === branch.candidate.id ? "active" : ""} key={branch.candidate.id} onClick={() => setActiveBranchId(branch.candidate.id)}><span>{branch.routeLabel}</span><small>{`${branch.candidate.candidates.length} 个模型`}</small></button>)}
        </div>
        {activeBranch && <div className="router-branch-editor" role="tabpanel">
          <div className="router-branch-context"><div><b>{activeBranch.candidate.label}</b><small>{activeBranch.condition ? `${activeBranch.condition.label} · ${activeBranch.routeLabel}` : "直接路由"}</small></div></div>
          <div className="router-branch-editor-grid">
            <div className="router-branch-block"><header><b>候选模型池</b><small>{`${activeBranch.candidate.candidates.length} 个模型`}</small></header><CandidateConfiguration definition={value.definition} node={activeBranch.candidate} models={models} onChange={(definition) => onChange({ ...value, definition })} /></div>
            <div className="router-branch-policy">
              <div className="router-branch-block"><header><b>选择策略</b></header><StrategyConfiguration definition={value.definition} node={activeBranch.strategy} models={models} displayModels={displayModels} onChange={(definition) => onChange({ ...value, definition })} onOpenCanvas={() => onIssueSelect(activeBranch.candidate.id)} /></div>
              <div className="router-branch-block"><header><b>Fallback 策略</b></header><FallbackConfiguration definition={value.definition} node={activeBranch.fallback} onChange={(definition) => onChange({ ...value, definition })} onOpenCanvas={() => onIssueSelect(activeBranch.strategy?.id ?? activeBranch.candidate.id)} /></div>
            </div>
          </div>
        </div>}
      </> : <div className="router-config-missing"><span>尚未配置 Candidate Models 分支。</span><Button type="button" variant="outline" onClick={() => onIssueSelect(value.definition.nodes[0]?.id ?? "")}>前往画布配置</Button></div>}
    </section>

    <section className="router-config-section router-validation-section">
      <header><div>{validation?.valid ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}<span><b>发布前校验</b><small>{validation ? validation.valid ? "配置完整，可以发布" : `${validation.issues.length} 个问题待处理` : "尚未运行校验"}</small></span></div></header>
      {validation && !validation.valid && <div className="router-validation-list">{validation.issues.map((issue, index) => <button type="button" key={`${issue.code}-${issue.node_id ?? index}`} onClick={() => issue.node_id && onIssueSelect(issue.node_id)} disabled={!issue.node_id}><AlertCircle size={14} /><span>{issue.message}</span>{issue.node_id && <small>定位节点</small>}</button>)}</div>}
      {validation?.valid && <div className="router-validation-ok"><CheckCircle2 size={16} /><span>Start、Output、候选模型、Strategy 和流程连通性均已通过。</span></div>}
    </section>
  </div>
}
