import { Eye, Loader2, Play } from "lucide-react"
import { useState } from "react"

import { dataSource } from "../../data-sources/apim/api"
import type {
  ManagedModel,
  ModelRouterDecisionPreview,
  ModelRouterDecisionTrialResult,
  ModelRouterDefinition,
  ModelRouterNode,
  ModelRouterReasoningEffort,
} from "../../data-sources/apim/types"
import { Button } from "../ui/button"
import { Input } from "../ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import { Textarea } from "../ui/textarea"

const DEFAULT_MAX_PROMPT_CHARS = 4_000

const SAMPLE_PROMPT_PRESETS = [
  {
    id: "refactor",
    label: "代码重构",
    prompt: "重构这段 TypeScript 异步任务队列，修复高并发下的竞态条件并补充边界单元测试。",
  },
  {
    id: "translate",
    label: "日常翻译",
    prompt: "把下面这段发布说明翻译成简洁自然的中文：Added multi-region failover for API gateways.",
  },
  {
    id: "math",
    label: "算法证明",
    prompt: "请严格证明 Dijkstra 算法在带负权边图上为什么会失效，并给出反例与 Bellman-Ford 复杂度对比。",
  },
] as const

const REASONING_EFFORT_LABELS: Record<string, string> = {
  default: "默认（跟随模型）",
  none: "关闭思考（none · 最快）",
  low: "低强度（low）",
  medium: "中强度（medium）",
  high: "高强度（high）",
}

export function DecisionPromptEditor({
  definition,
  node,
  models = [],
  onChange,
}: {
  definition: ModelRouterDefinition
  node: ModelRouterNode
  models?: ManagedModel[]
  onChange: (node: ModelRouterNode) => void
}) {
  const [samplePrompt, setSamplePrompt] = useState("")
  const [preview, setPreview] = useState<ModelRouterDecisionPreview | null>(null)
  const [trial, setTrial] = useState<ModelRouterDecisionTrialResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [trialLoading, setTrialLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const routingModel = models.find((model) => model.id === node.routing_model_id) ?? null
  const isSystemOne = Boolean(routingModel?.capabilities.includes("decision"))

  const runPreview = async () => {
    setLoading(true)
    setError(null)
    try {
      setPreview(await dataSource.previewModelRouterDecision(
        definition,
        node.id,
        samplePrompt,
      ))
    } catch (previewError) {
      setPreview(null)
      setError(String(previewError))
    } finally {
      setLoading(false)
    }
  }

  const runTrial = async () => {
    const effectiveSample = samplePrompt.trim() || SAMPLE_PROMPT_PRESETS[0].prompt
    if (!samplePrompt.trim()) {
      setSamplePrompt(effectiveSample)
    }
    setTrialLoading(true)
    setError(null)
    try {
      setTrial(await dataSource.trialModelRouterDecision(
        definition,
        node.id,
        effectiveSample,
      ))
    } catch (trialError) {
      setTrial(null)
      setError(String(trialError))
    } finally {
      setTrialLoading(false)
    }
  }

  return <div className="router-decision-prompt-editor">
    <label className="router-field"><span>{isSystemOne ? "判路由指令" : "判路由提示词"}</span>
      <Textarea
        rows={6}
        maxLength={20_000}
        placeholder="留空使用内置提示词；{catalog} 会替换为当前授权候选目录"
        value={node.decision_prompt ?? ""}
        onChange={(event) => {
          onChange({ ...node, decision_prompt: event.target.value || null })
          setPreview(null)
          setTrial(null)
        }}
      />
    </label>
    {!isSystemOne && <label className="router-field"><span>判路由推理强度</span>
      <Select
        value={node.decision_reasoning_effort ?? "default"}
        onValueChange={(value) => {
          const nextEffort = !value || value === "default"
            ? undefined
            : (value as ModelRouterReasoningEffort)
          const nextNode: ModelRouterNode = { ...node }
          if (nextEffort) {
            nextNode.decision_reasoning_effort = nextEffort
          } else {
            delete nextNode.decision_reasoning_effort
          }
          onChange(nextNode)
          setPreview(null)
          setTrial(null)
        }}
      >
        <SelectTrigger aria-label="选择判路由推理强度">
          <SelectValue>{REASONING_EFFORT_LABELS[node.decision_reasoning_effort ?? "default"]}</SelectValue>
        </SelectTrigger>
        <SelectContent align="end">
          <SelectItem value="default">{REASONING_EFFORT_LABELS.default}</SelectItem>
          <SelectItem value="none">{REASONING_EFFORT_LABELS.none}</SelectItem>
          <SelectItem value="low">{REASONING_EFFORT_LABELS.low}</SelectItem>
          <SelectItem value="medium">{REASONING_EFFORT_LABELS.medium}</SelectItem>
          <SelectItem value="high">{REASONING_EFFORT_LABELS.high}</SelectItem>
        </SelectContent>
      </Select>
    </label>}
    <label className="router-field"><span>最大判路由输入字符</span>
      <Input
        type="number"
        min={64}
        max={50_000}
        value={node.max_prompt_chars ?? DEFAULT_MAX_PROMPT_CHARS}
        onChange={(event) => {
          const value = Number(event.target.value)
          onChange({
            ...node,
            max_prompt_chars: Number.isFinite(value)
              ? Math.min(50_000, Math.max(64, value))
              : DEFAULT_MAX_PROMPT_CHARS,
          })
          setPreview(null)
          setTrial(null)
        }}
      />
    </label>
    <div className="router-field">
      <div className="router-sample-prompt-head">
        <span>样例请求</span>
        <div className="router-sample-presets" role="group" aria-label="样例请求预设">
          {SAMPLE_PROMPT_PRESETS.map((preset) => <button
            key={preset.id}
            type="button"
            className={samplePrompt === preset.prompt ? "active" : ""}
            onClick={() => {
              setSamplePrompt(preset.prompt)
              setTrial(null)
            }}
          >{preset.label}</button>)}
        </div>
      </div>
      <Textarea
        rows={3}
        maxLength={200_000}
        aria-label="样例请求"
        placeholder="输入一段样例请求，检查实际系统提示词或直接实测判路由结果"
        value={samplePrompt}
        onChange={(event) => setSamplePrompt(event.target.value)}
      />
    </div>
    <div className="router-decision-actions">
      <Button type="button" variant="outline" onClick={() => void runPreview()} disabled={loading || trialLoading}>
        {loading ? <Loader2 size={14} className="assistant-spin" /> : <Eye size={14} />}
        预览判路由输入
      </Button>
      <Button type="button" variant="secondary" onClick={() => void runTrial()} disabled={loading || trialLoading}>
        {trialLoading ? <Loader2 size={14} className="assistant-spin" /> : <Play size={14} />}
        实测判路由
      </Button>
    </div>
    {error && <div className="router-properties-empty"><span>预览失败</span><span data-no-localize>{error}</span></div>}
    {trial && <section className="router-decision-trial" aria-label="实测判路由结果">
      <header>
        <b>{trial.selected_model_name}</b>
        <span>{trial.decision_protocol === "system_one" ? "System One" : "LLM JSON"} · {trial.latency_ms} ms</span>
      </header>
      <dl className="router-decision-trial-metrics">
        <div><dt>判路由模型</dt><dd>{trial.routing_model_name}</dd></div>
        <div><dt>置信度</dt><dd>{trial.confidence != null ? `${(trial.confidence * 100).toFixed(1)}%` : "—"}</dd></div>
        <div><dt>Token</dt><dd>{trial.total_tokens ?? "—"}</dd></div>
        <div><dt>状态</dt><dd>{trial.fallback_used ? "安全回退" : "命中"}</dd></div>
      </dl>
      {trial.rationale && <p className="router-decision-trial-rationale" data-no-localize>{trial.rationale}</p>}
      {trial.probabilities.length > 0 && <div className="router-decision-probabilities" role="list" aria-label="候选模型概率分布">
        {trial.probabilities.map((item) => <div className="router-decision-probability-row" role="listitem" key={item.model_id}>
          <div className="router-decision-probability-label">
            <span>{item.display_name}</span>
            <strong>{(item.probability * 100).toFixed(1)}%</strong>
          </div>
          <div className="router-decision-probability-bar"><i style={{ width: `${Math.max(2, Math.min(100, item.probability * 100))}%` }} /></div>
        </div>)}
      </div>}
    </section>}
    {preview && <details className="router-decision-preview" open>
      <summary>实际判路由输入 · {preview.candidates.length} 个候选 · {preview.chars} 字符</summary>
      {preview.models_without_description.length > 0 && <p>{`有 ${preview.models_without_description.length} 个候选缺少用途说明。`}</p>}
      <strong>{preview.decision_protocol === "system_one" ? "System One Questions" : "System"}</strong>
      <pre>{preview.system}</pre>
      <strong>Sample user input{preview.sample_truncated ? " · 已截断" : ""}</strong>
      <pre>{preview.user || "（未提供样例请求）"}</pre>
    </details>}
  </div>
}
