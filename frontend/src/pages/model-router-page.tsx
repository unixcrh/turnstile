import {
  type CSSProperties,
  useEffect,
  useMemo,
  useState,
} from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertCircle,
  Blocks,
  CheckCircle2,
  Copy,
  FileSliders,
  GitBranch,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  Trash2,
  Users,
  Workflow,
  X,
} from "lucide-react"

import { RouterCanvas } from "../components/model-router/router-canvas"
import { RouterConfigForm } from "../components/model-router/router-config-form"
import { FoundryLogo } from "../components/brand-logos"
import {
  cloneDefinition,
  createStarterDefinition,
  normalizeRouterDefinition,
  resolveRouterDisplayModels,
} from "../components/model-router/router-definition"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../components/ui/alert-dialog"
import { Button } from "../components/ui/button"
import { ButtonGroup } from "../components/ui/button-group"
import { Checkbox } from "../components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu"
import { Input } from "../components/ui/input"
import { Textarea } from "../components/ui/textarea"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { dataSource } from "../data-sources/apim/api"
import { finopsKeys, finopsQueries } from "../data-sources/apim/queries"
import type {
  ManagedModel,
  ModelRouter,
  ModelRouterList,
  ModelRouterModelReference,
  ModelRouterStatus,
  ModelRouterValidation,
  ModelRouterWrite,
} from "../data-sources/apim/types"
import { getIntlLocale } from "../locales/index"
import { useAuth } from "../providers/auth-provider"


type RouterMode = "canvas" | "config"
type RouterFilter = "all" | ModelRouterStatus

const ROUTER_CONFIG_MODE_ENABLED = false
const ROUTER_PANE_WIDTH_KEY = "turnstile_model_router_pane_width"
const ROUTER_PANE_WIDTH_DEFAULT = 304
const ROUTER_PANE_WIDTH_MIN = 240
const ROUTER_PANE_WIDTH_MAX = 420

const STATUS_LABELS: Record<ModelRouterStatus, string> = {
  draft: "草稿",
  published: "已发布",
  disabled: "已停用",
}

function routerIdFromUrl() {
  return new URL(window.location.href).searchParams.get("router")
}

function writeRouterUrl(routerId: string | null, push = false) {
  const url = new URL(window.location.href)
  if (routerId) url.searchParams.set("router", routerId)
  else url.searchParams.delete("router")
  window.history[push ? "pushState" : "replaceState"](null, "", `${url.pathname}${url.search}${url.hash}`)
}

function writeFromRouter(router: ModelRouter): ModelRouterWrite {
  return {
    name: router.name,
    description: router.description,
    definition: normalizeRouterDefinition(router.definition),
  }
}

function serializedWrite(value: ModelRouterWrite) {
  return JSON.stringify(value)
}

function relativeUpdatedAt(value: string) {
  const date = new Date(value)
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60_000))
  const locale = getIntlLocale()
  if (elapsedMinutes < 1) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(0, "second")
  if (elapsedMinutes < 60) return new Intl.RelativeTimeFormat(locale, { numeric: "always" }).format(-elapsedMinutes, "minute")
  const hours = Math.floor(elapsedMinutes / 60)
  if (hours < 24) return new Intl.RelativeTimeFormat(locale, { numeric: "always" }).format(-hours, "hour")
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(date)
}

function RouterCreateDialog({
  open,
  models,
  busy,
  error,
  onOpenChange,
  onCreate,
}: {
  open: boolean
  models: ManagedModel[]
  busy: boolean
  error: string | null
  onOpenChange: (open: boolean) => void
  onCreate: (value: ModelRouterWrite) => void
}) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [modelIds, setModelIds] = useState<Set<string>>(() => new Set())
  const [search, setSearch] = useState("")
  useEffect(() => {
    if (!open) return
    setName("")
    setDescription("")
    setModelIds(new Set())
    setSearch("")
  }, [open])
  const candidateModels = models.filter((model) => !model.capabilities.includes("decision"))
  const visibleModels = candidateModels.filter((model) =>
    `${model.display_name} ${model.model_key} ${model.provider_name}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  )
  return <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next) }}>
    <DialogContent className="registry-editor-dialog router-create-dialog" finalFocus={false}>
      <form className="registry-editor router-create-form" onSubmit={(event) => {
        event.preventDefault()
        if (!name.trim()) return
        onCreate({ name: name.trim(), description: description.trim(), definition: createStarterDefinition([...modelIds]) })
      }}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>新建模型路由器</DialogTitle>
          <DialogDescription>定义用途，并选择首批候选模型。</DialogDescription>
        </DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body router-create-body">
          <div className="router-create-basics">
            <label className="registry-field"><span className="registry-field-label">名称</span><Input autoFocus required maxLength={120} placeholder="例如：客服问答路由" value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label className="registry-field"><span className="registry-field-label">描述</span><Textarea rows={2} maxLength={500} placeholder="说明业务场景与路由目标" value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          </div>
          <div className="router-create-models">
            <div className="router-create-models-head"><b>初始候选模型</b><small>{modelIds.size} / {candidateModels.length} 已选</small></div>
            <div className="smh-search router-create-search"><Search size={14} /><input aria-label="搜索候选模型" placeholder="搜索模型或提供方..." value={search} onChange={(event) => setSearch(event.target.value)} /></div>
            <div className="router-create-model-list">{visibleModels.map((model) => <label key={model.id}><Checkbox checked={modelIds.has(model.id)} onCheckedChange={(checked) => setModelIds((current) => { const next = new Set(current); if (checked === true) next.add(model.id); else next.delete(model.id); return next })} /><span><b>{model.display_name}</b><small>{model.provider_name} · {model.capabilities.join(" / ")}</small></span></label>)}</div>
            {!visibleModels.length && <div className="router-config-empty">没有匹配的可用模型。</div>}
          </div>
          {error && <div className="registry-error">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !name.trim()}>{busy ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}<span>创建路由器</span></Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function RouterNameDialog({
  mode,
  router,
  initialValue,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  mode: "rename" | "duplicate"
  router: ModelRouter
  initialValue: ModelRouterWrite
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (name: string, description: string) => void
}) {
  const [name, setName] = useState(mode === "duplicate" ? `${router.name} 副本` : initialValue.name)
  const [description, setDescription] = useState(initialValue.description)
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog" finalFocus={false}>
      <form className="registry-editor simple-model-form" onSubmit={(event) => { event.preventDefault(); if (name.trim()) onSubmit(name.trim(), description.trim()) }}>
        <DialogHeader className="registry-editor-header"><DialogTitle>{mode === "duplicate" ? "复制路由器" : "编辑基础信息"}</DialogTitle><DialogDescription>{router.name}</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body simple-model-body">
          <label className="router-field"><span>名称</span><Input autoFocus maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
          {mode === "rename" && <label className="router-field"><span>描述</span><Textarea rows={3} maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} /></label>}
          {error && <div className="registry-error">{error}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose><Button type="submit" disabled={busy || !name.trim()}>{mode === "duplicate" ? <Copy size={14} /> : <Save size={14} />}{mode === "duplicate" ? "创建副本" : "保存"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function RouterAccessDialog({
  router,
  users,
  busyUserId,
  error,
  onClose,
  onToggle,
}: {
  router: ModelRouter
  users: Array<{ id: string; name: string }>
  busyUserId: string | null
  error: string | null
  onClose: () => void
  onToggle: (userId: string, assigned: boolean) => void
}) {
  const [search, setSearch] = useState("")
  const assignees = useQuery({
    queryKey: ["model-platform", "model-router-assignees", router.id],
    queryFn: () => dataSource.modelRouterAssignees(router.id),
  })
  const assigned = new Set(assignees.data ?? [])
  const normalized = search.trim().toLocaleLowerCase()
  const visibleUsers = users.filter((user) =>
    `${user.name} ${user.id}`.toLocaleLowerCase().includes(normalized),
  )
  return <Dialog open onOpenChange={(open) => { if (!open && !busyUserId) onClose() }}>
    <DialogContent className="registry-editor-dialog router-access-dialog" finalFocus={false}>
      <div className="registry-editor router-access-editor">
        <DialogHeader className="registry-editor-header"><DialogTitle>人员访问</DialogTitle><DialogDescription>{router.name} · 已发布 v{router.version}</DialogDescription></DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={Boolean(busyUserId)} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body router-access-body">
          <label className="people-model-search router-access-search"><Search size={14} /><Input autoFocus value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索姓名或邮箱" aria-label="搜索路由器人员" /></label>
          <div className="router-access-summary"><span>{assigned.size} / {users.length} 已分配</span><small>候选模型访问由保存时一并校验</small></div>
          {assignees.isLoading ? <div className="router-access-state"><RefreshCw className="spin" size={15} />正在加载人员访问</div> : <div className="people-model-select-list router-access-list" role="group" aria-label="模型路由器人员访问">
            {visibleUsers.map((person) => <label className="people-model-option" key={person.id}><Checkbox checked={assigned.has(person.id)} disabled={Boolean(busyUserId)} onCheckedChange={(checked) => onToggle(person.id, checked === true)} /><span><b>{person.name}</b><small>{person.id}</small></span>{busyUserId === person.id && <RefreshCw className="spin" size={13} />}</label>)}
            {!visibleUsers.length && <div className="people-model-empty">没有匹配的人员</div>}
          </div>}
          {(error || assignees.error) && <div className="registry-error">{error ?? String(assignees.error)}</div>}
        </div>
        <DialogFooter className="registry-editor-footer"><DialogClose render={<Button type="button" variant="outline" disabled={Boolean(busyUserId)} />}>完成</DialogClose></DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
}

function RouterListPane({
  routers,
  selectedId,
  search,
  filter,
  canManage,
  busy,
  dirty,
  onSearchChange,
  onFilterChange,
  onSelect,
  onRename,
  onDuplicate,
  onAccess,
  onDisable,
  onDelete,
}: {
  routers: ModelRouter[]
  selectedId: string | null
  search: string
  filter: RouterFilter
  canManage: boolean
  busy: boolean
  dirty: boolean
  onSearchChange: (value: string) => void
  onFilterChange: (value: RouterFilter) => void
  onSelect: (routerId: string) => void
  onRename: (router: ModelRouter) => void
  onDuplicate: (router: ModelRouter) => void
  onAccess: (router: ModelRouter) => void
  onDisable: (router: ModelRouter) => void
  onDelete: (router: ModelRouter) => void
}) {
  const counts = {
    all: routers.length,
    draft: routers.filter((router) => router.status === "draft").length,
    published: routers.filter((router) => router.status === "published").length,
    disabled: routers.filter((router) => router.status === "disabled").length,
  }
  const matches = routers.filter((router) => {
    const searchMatch = `${router.name} ${router.description}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
    return searchMatch && (filter === "all" || router.status === filter)
  })
  return <aside className="router-list-pane">
    <div className="router-list-controls">
      <div className="smh-search"><Search size={15} /><input aria-label="搜索模型路由器" placeholder="搜索模型路由器..." value={search} onChange={(event) => onSearchChange(event.target.value)} /></div>
      <div className="router-status-filters">
        {(["all", "draft", "published", "disabled"] as RouterFilter[])
          .filter((value) => value === "all" || counts[value] > 0)
          .map((value) => <button type="button" className={filter === value ? "active" : ""} aria-pressed={filter === value} key={value} onClick={() => onFilterChange(value)}>{value !== "all" && <i data-status={value} />}<span>{value === "all" ? "全部" : STATUS_LABELS[value]}</span><small>{counts[value]}</small></button>)}
      </div>
    </div>
    <div className="router-list-scroll">
      <div className="router-list-section-label"><span>模型路由器</span><i /></div>
      {matches.map((router) => <div className={`router-list-row ${router.id === selectedId ? "active" : ""}`} key={router.id}>
        <button type="button" className="router-list-main" onClick={() => onSelect(router.id)}>
          <span className="router-list-icon"><GitBranch size={15} /><i data-status={router.status} /></span>
          <span className="router-list-copy"><b>{router.name}</b><small><span>{STATUS_LABELS[router.status]}</span>{router.version > 0 && <> · v{router.version}</>} · <span data-no-localize>{relativeUpdatedAt(router.updated_at)}</span></small></span>
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button type="button" variant="ghost" size="icon-sm" className="router-list-menu" aria-label={`${router.name} 操作`} title="更多操作" disabled={busy} />}><MoreHorizontal size={15} /></DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="right">
            <DropdownMenuItem onClick={() => onRename(router)}>编辑基础信息</DropdownMenuItem>
            <DropdownMenuItem disabled={dirty && router.id === selectedId} onClick={() => onDuplicate(router)}><Copy size={14} />复制</DropdownMenuItem>
            {canManage && router.status === "published" && <DropdownMenuItem onClick={() => onAccess(router)}><Users size={14} />人员访问</DropdownMenuItem>}
            {router.status === "published" && <DropdownMenuItem disabled={dirty && router.id === selectedId} onClick={() => onDisable(router)}>停用</DropdownMenuItem>}
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onClick={() => onDelete(router)}><Trash2 size={14} />删除</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>)}
      {!matches.length && <div className="router-list-empty"><Search size={22} /><span>没有匹配的路由器</span></div>}
    </div>
    <footer>{`共 ${matches.length} 个路由器`}</footer>
  </aside>
}

function RouterEditor({
  router,
  value,
  models,
  displayModels,
  mode,
  validation,
  selectedNodeId,
  dirty,
  busy,
  onModeChange,
  onSelectedNodeChange,
  onChange,
  onSave,
  onValidate,
  onPublish,
}: {
  router: ModelRouter
  value: ModelRouterWrite
  models: ManagedModel[]
  displayModels: ModelRouterModelReference[]
  mode: RouterMode
  validation: ModelRouterValidation | null
  selectedNodeId: string | null
  dirty: boolean
  busy: boolean
  onModeChange: (mode: RouterMode) => void
  onSelectedNodeChange: (nodeId: string | null) => void
  onChange: (value: ModelRouterWrite) => void
  onSave: () => void
  onValidate: () => void
  onPublish: () => void
}) {
  const effectiveMode: RouterMode = ROUTER_CONFIG_MODE_ENABLED ? mode : "canvas"
  return <section className="router-editor-pane">
    <header className="router-editor-head">
      <div className="router-editor-identity"><span><FoundryLogo size={18} /></span><div><div><h2>{value.name || "未命名路由器"}</h2><i data-status={router.status}>{STATUS_LABELS[router.status]}</i>{router.version > 0 && <em>v{router.version}</em>}{dirty && <strong>未保存</strong>}</div></div></div>
      <div className="router-editor-actions">
        <Button className="router-action-secondary" type="button" variant="outline" disabled={busy || !dirty} onClick={onSave}>{busy ? <RefreshCw className="spin" size={14} /> : <Save size={14} />}<span>保存草稿</span></Button>
        <Button className="router-action-secondary" type="button" variant="outline" disabled={busy} onClick={onValidate}><CheckCircle2 size={14} />校验</Button>
        <Button className="router-action-primary" type="button" disabled={busy} onClick={onPublish}><Send size={14} />发布</Button>
      </div>
    </header>
    {ROUTER_CONFIG_MODE_ENABLED && <div className="router-editor-view-row">
      <ButtonGroup className="usage-metric-segment router-mode-switch" aria-label="路由器编辑模式">
        <button type="button" className={mode === "canvas" ? "active" : ""} aria-pressed={mode === "canvas"} onClick={() => onModeChange("canvas")}><Blocks size={14} />画布</button>
        <button type="button" className={mode === "config" ? "active" : ""} aria-pressed={mode === "config"} onClick={() => onModeChange("config")}><FileSliders size={14} />常规配置</button>
      </ButtonGroup>
    </div>}
    {validation && <div className={`router-validation-banner ${validation.valid ? "valid" : "invalid"}`}>
      {validation.valid ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
      <span>{validation.valid ? "配置校验通过，可以发布。" : `发现 ${validation.issues.length} 个配置问题。`}</span>
      {!validation.valid && ROUTER_CONFIG_MODE_ENABLED && <button type="button" onClick={() => onModeChange("config")}>查看问题</button>}
    </div>}
    {effectiveMode === "canvas" ? <RouterCanvas definition={value.definition} models={models} displayModels={displayModels} routerName={value.name} routerDescription={value.description} selectedNodeId={selectedNodeId} onSelectedNodeChange={onSelectedNodeChange} onChange={(definition) => onChange({ ...value, definition })} onMetadataChange={(name, description) => onChange({ ...value, name, description })} /> : <RouterConfigForm value={value} models={models} displayModels={displayModels} validation={validation} onChange={onChange} onIssueSelect={(nodeId) => { onSelectedNodeChange(nodeId); onModeChange("canvas") }} />}
  </section>
}

export function ModelRouterPage() {
  const client = useQueryClient()
  const { user } = useAuth()
  const registry = useQuery(finopsQueries.registry())
  const routers = useQuery(finopsQueries.modelRouters())
  const catalog = useQuery(finopsQueries.modelRouterCatalog())
  const entities = useQuery(finopsQueries.entities())
  const [selectedId, setSelectedId] = useState<string | null>(routerIdFromUrl)
  const [draft, setDraft] = useState<ModelRouterWrite | null>(null)
  const [savedDraftSignature, setSavedDraftSignature] = useState<string | null>(null)
  const [mode, setMode] = useState<RouterMode>("canvas")
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [validation, setValidation] = useState<ModelRouterValidation | null>(null)
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState<RouterFilter>("all")
  const {
    width: paneWidth,
    minWidth: paneMinWidth,
    maxWidth: paneMaxWidth,
    startResize: startPaneResize,
    resizeWithKeyboard: resizePaneWithKeyboard,
    resetWidth: resetPaneWidth,
  } = useResizablePane({
    storageKey: ROUTER_PANE_WIDTH_KEY,
    defaultWidth: ROUTER_PANE_WIDTH_DEFAULT,
    minWidth: ROUTER_PANE_WIDTH_MIN,
    maxWidth: ROUTER_PANE_WIDTH_MAX,
  })
  const [createOpen, setCreateOpen] = useState(false)
  const [nameAction, setNameAction] = useState<{ mode: "rename" | "duplicate"; router: ModelRouter } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ModelRouter | null>(null)
  const [disableTarget, setDisableTarget] = useState<ModelRouter | null>(null)
  const [accessTarget, setAccessTarget] = useState<ModelRouter | null>(null)
  const [pendingSelection, setPendingSelection] = useState<string | null>(null)

  const routerItems = routers.data?.items ?? []
  const selectedRouter = routerItems.find((router) => router.id === selectedId) ?? null
  const allowedIds = useMemo(() => new Set(catalog.data?.model_ids ?? []), [catalog.data?.model_ids])
  const availableModels = useMemo(
    () => (registry.data?.models ?? []).filter((model) => model.enabled && (allowedIds.has(model.id) || model.capabilities.includes("decision"))),
    [allowedIds, registry.data?.models],
  )
  const displayModels = useMemo(
    () => resolveRouterDisplayModels(registry.data?.models ?? [], routers.data?.model_references),
    [registry.data?.models, routers.data?.model_references],
  )
  const assignableUsers = useMemo(() => {
    const people = entities.data?.users ?? []
    if (!user || people.some((person) => person.id === user.email)) return people
    return [
      { id: user.email, name: user.name ?? user.email, parent_id: null },
      ...people,
    ]
  }, [entities.data?.users, user])
  const dirty = Boolean(draft && savedDraftSignature && serializedWrite(draft) !== savedDraftSignature)

  useEffect(() => {
    const syncRoute = () => setSelectedId(routerIdFromUrl())
    window.addEventListener("popstate", syncRoute)
    return () => window.removeEventListener("popstate", syncRoute)
  }, [])

  useEffect(() => {
    if (!routerItems.length) {
      if (selectedId) {
        setSelectedId(null)
        writeRouterUrl(null)
      }
      return
    }
    if (selectedId && routerItems.some((router) => router.id === selectedId)) return
    setSelectedId(routerItems[0].id)
    writeRouterUrl(routerItems[0].id)
  }, [routerItems, selectedId])

  useEffect(() => {
    if (!selectedRouter) {
      setDraft(null)
      setSavedDraftSignature(null)
      return
    }
    const nextDraft = writeFromRouter(selectedRouter)
    setDraft(nextDraft)
    setSavedDraftSignature(serializedWrite(nextDraft))
    setValidation(null)
    setSelectedNodeId(null)
  }, [
    selectedRouter?.id,
    selectedRouter?.updated_at,
  ])

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  const setRouter = (router: ModelRouter) => {
    client.setQueryData<ModelRouterList>(finopsKeys.modelRouters, (current) => ({
      ...current,
      items: [router, ...(current?.items ?? []).filter((item) => item.id !== router.id)]
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    }))
    client.setQueryData(finopsKeys.modelRouter(router.id), router)
  }

  const create = useMutation({
    mutationFn: (value: ModelRouterWrite) => dataSource.saveModelRouter(value),
    onSuccess: (router) => {
      setRouter(router)
      setSelectedId(router.id)
      writeRouterUrl(router.id, true)
      setCreateOpen(false)
    },
  })
  const save = useMutation({
    mutationFn: ({ id, value }: { id: string; value: ModelRouterWrite }) => dataSource.saveModelRouter(value, id),
    onSuccess: setRouter,
  })
  const duplicate = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => dataSource.duplicateModelRouter(id, name),
    onSuccess: (router) => {
      setRouter(router)
      setSelectedId(router.id)
      writeRouterUrl(router.id, true)
      setNameAction(null)
    },
  })
  const validate = useMutation({ mutationFn: dataSource.validateModelRouter, onSuccess: setValidation })
  const refreshAssignedRouters = () => client.invalidateQueries({ queryKey: ["model-platform", "assigned-model-routers"] })
  const publish = useMutation({ mutationFn: dataSource.publishModelRouter, onSuccess: (router) => { setRouter(router); setValidation({ valid: true, issues: [] }); void refreshAssignedRouters() } })
  const disable = useMutation({ mutationFn: dataSource.disableModelRouter, onSuccess: (router) => { setRouter(router); setDisableTarget(null); void refreshAssignedRouters() } })
  const remove = useMutation({
    mutationFn: dataSource.deleteModelRouter,
    onSuccess: (_result, id) => {
      const remaining = routerItems.filter((router) => router.id !== id)
      client.setQueryData<ModelRouterList>(finopsKeys.modelRouters, current => ({ ...current, items: remaining }))
      const nextId = remaining[0]?.id ?? null
      setSelectedId(nextId)
      writeRouterUrl(nextId)
      setDeleteTarget(null)
      void refreshAssignedRouters()
    },
  })
  const assignment = useMutation({
    mutationFn: ({ routerId, userId, assigned }: { routerId: string; userId: string; assigned: boolean }) => dataSource.setModelRouterAssignment(routerId, userId, assigned),
    onSuccess: (_result, variables) => {
      client.setQueryData<string[]>(["model-platform", "model-router-assignees", variables.routerId], (current) => variables.assigned ? [...new Set([...(current ?? []), variables.userId])].sort() : (current ?? []).filter((value) => value !== variables.userId))
      void client.invalidateQueries({ queryKey: finopsKeys.assignedModelRouters(variables.userId) })
    },
  })
  const busy = create.isPending || save.isPending || duplicate.isPending || validate.isPending || publish.isPending || disable.isPending || remove.isPending
  const actionError = create.error ?? save.error ?? duplicate.error ?? validate.error ?? publish.error ?? disable.error ?? remove.error

  const chooseRouter = (routerId: string) => {
    if (routerId === selectedId) return
    if (dirty) {
      setPendingSelection(routerId)
      return
    }
    setSelectedId(routerId)
    writeRouterUrl(routerId, true)
  }
  const persistDraft = async () => {
    if (!selectedRouter || !draft) return selectedRouter
    if (!dirty) return selectedRouter
    return save.mutateAsync({ id: selectedRouter.id, value: draft })
  }
  const runValidation = async () => {
    const saved = await persistDraft()
    if (!saved) return null
    return validate.mutateAsync(saved.id)
  }
  const runPublish = async () => {
    const result = await runValidation()
    if (!result?.valid || !selectedRouter) return
    await publish.mutateAsync(selectedRouter.id)
  }

  if (registry.isLoading || routers.isLoading || catalog.isLoading) {
    return <div className="registry-loading"><RefreshCw className="spin" />加载模型路由器</div>
  }
  if (registry.error || !registry.data) {
    return <div className="registry-error">无法加载模型管理 API：{String(registry.error)}</div>
  }
  if (routers.error || catalog.error) {
    return <div className="smh-workspace model-router-workspace"><header className="smh-page-header"><div><span className="smh-header-icon"><Workflow size={17} /></span><h1>模型智能路由</h1></div></header><div className="smh-empty"><AlertCircle size={28} /><b>无法加载模型路由服务</b><span>{String(routers.error ?? catalog.error)}</span></div></div>
  }

  return <div className="smh-workspace model-router-workspace">
    <header className="smh-page-header">
      <div><span className="smh-header-icon"><Workflow size={17} /></span><h1>模型智能路由</h1><span>{routerItems.length}</span></div>
      <div className="smh-header-actions"><Button aria-label="新建路由器" onClick={() => setCreateOpen(true)}><Plus size={14} />新建路由器</Button></div>
    </header>
    {actionError && <div className="smh-notice registry-error"><AlertCircle size={15} /><div className="smh-notice-copy"><b>操作失败</b><span>{String(actionError)}</span></div></div>}
    <div className="router-split" style={{ "--router-pane-width": `${paneWidth}px` } as CSSProperties}>
      <RouterListPane routers={routerItems} selectedId={selectedId} search={search} filter={filter} canManage={user?.role === "owner"} busy={busy} dirty={dirty} onSearchChange={setSearch} onFilterChange={setFilter} onSelect={chooseRouter} onRename={(router) => setNameAction({ mode: "rename", router })} onDuplicate={(router) => setNameAction({ mode: "duplicate", router })} onAccess={(router) => { assignment.reset(); setAccessTarget(router) }} onDisable={setDisableTarget} onDelete={setDeleteTarget} />
      <button
        className="runtime-pane-handle"
        type="button"
        role="separator"
        aria-label="调整模型路由器列表宽度"
        aria-orientation="vertical"
        aria-valuemin={paneMinWidth}
        aria-valuemax={paneMaxWidth}
        aria-valuenow={paneWidth}
        title="拖动调整宽度，双击复位"
        onPointerDown={startPaneResize}
        onKeyDown={resizePaneWithKeyboard}
        onDoubleClick={resetPaneWidth}
      />
      {selectedRouter && draft ? <RouterEditor router={selectedRouter} value={draft} models={availableModels} displayModels={displayModels} mode={mode} validation={validation} selectedNodeId={selectedNodeId} dirty={dirty} busy={busy} onModeChange={setMode} onSelectedNodeChange={setSelectedNodeId} onChange={(value) => { setDraft(value); setValidation(null) }} onSave={() => void persistDraft()} onValidate={() => void runValidation()} onPublish={() => void runPublish()} /> : <section className="router-editor-pane"><div className="smh-empty router-empty-state"><GitBranch size={32} /><b>创建第一个模型路由器</b><span>从可用模型中选择候选池，并在画布上连接条件、优先级和 Fallback。</span><Button className="router-new-button" onClick={() => setCreateOpen(true)}><Plus size={14} />新建路由器</Button></div></section>}
    </div>

    <RouterCreateDialog open={createOpen} models={availableModels} busy={create.isPending} error={create.error ? String(create.error) : null} onOpenChange={setCreateOpen} onCreate={(value) => create.mutate(value)} />
    {user?.role === "owner" && accessTarget && <RouterAccessDialog router={accessTarget} users={assignableUsers} busyUserId={assignment.isPending ? assignment.variables?.userId ?? null : null} error={assignment.error ? String(assignment.error) : null} onClose={() => setAccessTarget(null)} onToggle={(userId, assigned) => assignment.mutate({ routerId: accessTarget.id, userId, assigned })} />}
    {nameAction && <RouterNameDialog mode={nameAction.mode} router={nameAction.router} initialValue={nameAction.router.id === selectedRouter?.id && draft ? draft : writeFromRouter(nameAction.router)} busy={save.isPending || duplicate.isPending} error={(nameAction.mode === "duplicate" ? duplicate.error : save.error) ? String(nameAction.mode === "duplicate" ? duplicate.error : save.error) : null} onClose={() => setNameAction(null)} onSubmit={(name, description) => {
      if (nameAction.mode === "duplicate") duplicate.mutate({ id: nameAction.router.id, name })
      else {
        const current = nameAction.router.id === selectedRouter?.id && draft ? draft : writeFromRouter(nameAction.router)
        save.mutate({ id: nameAction.router.id, value: { ...current, name, description } }, { onSuccess: () => setNameAction(null) })
      }
    }} />}
    <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open && !remove.isPending) setDeleteTarget(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="destructive"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>删除模型路由器</AlertDialogTitle><AlertDialogDescription>{deleteTarget ? `将删除“${deleteTarget.name}”及其当前草稿，并从路由器列表中移除。已发布版本记录会保留用于审计。` : ""}</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={remove.isPending}>取消</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={remove.isPending} onClick={() => deleteTarget && remove.mutate(deleteTarget.id)}>{remove.isPending ? "正在删除" : "删除路由器"}</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={Boolean(disableTarget)} onOpenChange={(open) => { if (!open && !disable.isPending) setDisableTarget(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="neutral"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>停用模型路由器</AlertDialogTitle><AlertDialogDescription>{disableTarget ? `停用“${disableTarget.name}”后，其已发布版本将不再作为可用路由。草稿与版本记录会保留。` : ""}</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel disabled={disable.isPending}>取消</AlertDialogCancel><AlertDialogAction disabled={disable.isPending} onClick={() => disableTarget && disable.mutate(disableTarget.id)}>停用</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={Boolean(pendingSelection)} onOpenChange={(open) => { if (!open) setPendingSelection(null) }}>
      <AlertDialogContent className="model-delete-dialog router-confirm-dialog" data-tone="destructive"><div className="model-delete-body"><AlertDialogHeader><AlertDialogTitle>放弃未保存的修改？</AlertDialogTitle><AlertDialogDescription>当前画布有尚未保存的更改。切换路由器将丢失这些更改。</AlertDialogDescription></AlertDialogHeader></div><AlertDialogFooter className="model-delete-footer"><AlertDialogCancel>继续编辑</AlertDialogCancel><AlertDialogAction variant="destructive" onClick={() => { if (!pendingSelection) return; setSelectedId(pendingSelection); writeRouterUrl(pendingSelection, true); setPendingSelection(null) }}>放弃并切换</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
  </div>
}