import { ArrowDown, ArrowUp, SlidersHorizontal } from "lucide-react"
import { useState } from "react"

import { ModelBrandLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Input } from "../ui/input"
import { Textarea } from "../ui/textarea"
import type {
  ModelRouterDefinition,
  ModelRouterModelReference,
  ModelRouterNode,
  ModelRouterStrategyModel,
} from "../../data-sources/apim/types"
import { routerModelStatusLabel } from "./router-definition"

function numberOrNull(value: string): number | null {
  if (!value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function candidateModelIds(
  definition: ModelRouterDefinition,
  strategyId: string,
) {
  const nodesById = new Map(definition.nodes.map((node) => [node.id, node]))
  return [...new Set(definition.edges
    .filter((edge) => edge.target === strategyId)
    .map((edge) => nodesById.get(edge.source))
    .filter((node): node is ModelRouterNode => node?.type === "candidate_models")
    .flatMap((node) => node.candidates.map((candidate) => candidate.model_id)))]
}

export function StrategyModelEditor({
  definition,
  node,
  models,
  onChange,
  compact = false,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode
  models: ModelRouterModelReference[]
  onChange: (node: ModelRouterNode) => void
  compact?: boolean
}) {
  const [expandedModelId, setExpandedModelId] = useState<string | null>(null)
  const modelIds = candidateModelIds(definition, node.id)
  const modelsById = new Map(models.map((model) => [model.id, model]))
  const configsById = new Map(node.model_configs.map((config) => [config.model_id, config]))
  const configs = modelIds
    .map((modelId, index) => configsById.get(modelId) ?? {
      model_id: modelId,
      priority: index + 1,
      weight: 100,
      max_latency_ms: null,
      max_cost_per_million: null,
      description: "",
    })
    .sort((left, right) => left.priority - right.priority)

  const setConfigs = (nextConfigs: ModelRouterStrategyModel[]) => {
    onChange({
      ...node,
      model_configs: nextConfigs.map((config, index) => ({
        ...config,
        priority: index + 1,
      })),
    })
  }
  const updateConfig = (modelId: string, patch: Partial<ModelRouterStrategyModel>) => {
    setConfigs(configs.map((config) =>
      config.model_id === modelId ? { ...config, ...patch } : config,
    ))
  }
  const moveConfig = (index: number, offset: -1 | 1) => {
    const target = index + offset
    if (target < 0 || target >= configs.length) return
    const reordered = [...configs]
    ;[reordered[index], reordered[target]] = [reordered[target], reordered[index]]
    setConfigs(reordered)
  }

  if (!configs.length) {
    return <div className="router-properties-empty">先在 Candidate Models 中选择候选模型。</div>
  }

  const weightedMode = node.strategy_type === "weighted"
  const orderedMode = !weightedMode
  return <div className={`router-strategy-model-editor ${compact ? "compact" : ""}`}>
    <div className="router-strategy-model-heading">
      <span>{weightedMode ? "候选模型权重" : node.strategy_type === "model_based" ? "安全回退顺序" : "优先级顺序"}</span>
      <small>{configs.length} 个模型</small>
    </div>
    <div className="router-strategy-model-list">
      {configs.map((config, index) => {
        const model = modelsById.get(config.model_id)
        const constraintsExpanded = expandedModelId === config.model_id
        return <div className="router-strategy-model-row" data-mode={orderedMode ? "priority" : "weighted"} key={config.model_id} data-model-id={config.model_id} data-model-status={model?.status ?? "unavailable"} title={config.model_id}>
          <div className="router-strategy-model-main">
            {orderedMode && <strong>P{index + 1}</strong>}
            <span className="router-model-logo"><ModelBrandLogo familyKey={model?.family_key} identity={`${model?.display_name ?? ""} ${model?.model_key ?? ""}`} providerName={model?.provider_name ?? ""} size={14} /></span>
            <span className="router-strategy-model-name"><b>{model?.display_name ?? "已不可用的模型"}</b><small>{model?.provider_name}{model?.provider_name && model.status !== "available" ? " · " : null}{model ? routerModelStatusLabel(model.status) : "候选模型"}</small></span>
            <span className="router-strategy-row-actions">
              {orderedMode && <span className="router-strategy-order-actions">
                <Button type="button" variant="ghost" size="icon-sm" disabled={index === 0} onClick={() => moveConfig(index, -1)} title="提高优先级" aria-label="提高优先级"><ArrowUp size={13} /></Button>
                <Button type="button" variant="ghost" size="icon-sm" disabled={index === configs.length - 1} onClick={() => moveConfig(index, 1)} title="降低优先级" aria-label="降低优先级"><ArrowDown size={13} /></Button>
              </span>}
              <Button
                className="router-strategy-constraints-toggle"
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-expanded={constraintsExpanded}
                aria-pressed={constraintsExpanded}
                data-state={constraintsExpanded ? "open" : "closed"}
                aria-label={constraintsExpanded ? "收起运行约束" : "配置运行约束"}
                title={constraintsExpanded ? "收起运行约束" : "配置运行约束"}
                onClick={() => setExpandedModelId(constraintsExpanded ? null : config.model_id)}
              ><SlidersHorizontal size={13} /></Button>
            </span>
          </div>
          {weightedMode && <label className="router-field router-strategy-weight"><span>权重</span><Input aria-label={`${model?.display_name ?? config.model_id} 权重`} type="number" min={1} max={100} value={config.weight} onChange={(event) => updateConfig(config.model_id, { weight: Math.min(100, Math.max(1, Number(event.target.value) || 1)) })} /></label>}
          {constraintsExpanded && <div className="router-strategy-model-constraints">
            <label className="router-field span-2"><span>路由用途说明</span><Textarea rows={3} maxLength={1000} placeholder="说明该模型适合处理的任务，供 Routing Model 判路由时参考" value={config.description} onChange={(event) => updateConfig(config.model_id, { description: event.target.value })} /></label>
            <label className="router-field"><span>候选超时 ms</span><Input type="number" min={1} placeholder="使用 Strategy 默认值" value={config.max_latency_ms ?? ""} onChange={(event) => updateConfig(config.model_id, { max_latency_ms: numberOrNull(event.target.value) })} /></label>
            <label className="router-field"><span>最高单价 / M</span><Input type="number" min={0} step="0.01" placeholder="不限" value={config.max_cost_per_million ?? ""} onChange={(event) => updateConfig(config.model_id, { max_cost_per_million: numberOrNull(event.target.value) })} /></label>
          </div>}
        </div>
      })}
    </div>
  </div>
}
