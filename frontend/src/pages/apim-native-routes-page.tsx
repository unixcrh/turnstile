import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react"
import { useQuery } from "@tanstack/react-query"
import { Network, Plus, RefreshCw, Search, X } from "lucide-react"

import {
  ApimNativeRouteCreateDialog,
  type NativeRouteModelOption,
} from "../components/model-management/apim-native-route-create-dialog"
import { DeploymentResilienceEditor } from "../components/model-management/deployment-resilience-editor"
import { finopsQueries } from "../data-sources/apim/queries"
import { useIsMobile } from "../hooks/use-mobile"
import { Button } from "../components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog"
import { useResizablePane } from "../components/ui/use-resizable-pane"
import { useAuth } from "../providers/auth-provider"

const NATIVE_ROUTE_PANE_WIDTH_KEY = "turnstile_apim_native_route_pane_width"
const NATIVE_ROUTE_PANE_WIDTH_DEFAULT = 304
const NATIVE_ROUTE_PANE_WIDTH_MIN = 240
const NATIVE_ROUTE_PANE_WIDTH_MAX = 420

export function ApimNativeRoutesPage({
  routeDrawerOpen,
  onRouteDrawerOpenChange,
  addOpen,
  onAddOpenChange,
}: {
  routeDrawerOpen: boolean
  onRouteDrawerOpenChange: (open: boolean) => void
  addOpen: boolean
  onAddOpenChange: (open: boolean) => void
}) {
  const { user } = useAuth()
  const isMobile = useIsMobile()
  const registry = useQuery(finopsQueries.registry())
  const pools = useQuery(finopsQueries.modelBackendPools())
  const [search, setSearch] = useState("")
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  const [pendingCreatedModelId, setPendingCreatedModelId] = useState<string | null>(null)
  const {
    width: paneWidth,
    minWidth: paneMinWidth,
    maxWidth: paneMaxWidth,
    startResize: startPaneResize,
    resizeWithKeyboard: resizePaneWithKeyboard,
    resetWidth: resetPaneWidth,
  } = useResizablePane({
    storageKey: NATIVE_ROUTE_PANE_WIDTH_KEY,
    defaultWidth: NATIVE_ROUTE_PANE_WIDTH_DEFAULT,
    minWidth: NATIVE_ROUTE_PANE_WIDTH_MIN,
    maxWidth: NATIVE_ROUTE_PANE_WIDTH_MAX,
  })
  const managedModels = useMemo(() => {
    if (!registry.data) return []
    return registry.data.models.filter((model) => model.enabled
      && !model.capabilities.includes("image_generation") && !model.capabilities.includes("decision")
      && registry.data!.runtimes.some(runtime => runtime.id === model.runtime_id
        && runtime.enabled && runtime.config.control_plane_managed === true
        && registry.data!.gateways.some(gateway => gateway.id === runtime.gateway_profile_id
          && gateway.implementation === "apim")))
      .sort((left, right) => left.model_key.localeCompare(right.model_key))
  }, [registry.data])
  const poolByModelId = new Map((pools.data ?? []).map(item => [item.model_id, item.pool] as const))
  const activeRoutes = managedModels.filter(model => poolByModelId.has(model.id))
  const routeOptions: NativeRouteModelOption[] = managedModels.map(model => ({
    model,
    state: poolByModelId.has(model.id) ? "active" : "available",
  }))
  const routeStateLoading = registry.isLoading || pools.isLoading
  const routeStateError = registry.error ?? pools.error
  const routes = useMemo(() => {
    const normalized = search.trim().toLocaleLowerCase()
    return activeRoutes.filter(model => !normalized
      || `${model.display_name} ${model.model_key} ${model.runtime_name}`.toLocaleLowerCase().includes(normalized))
  }, [activeRoutes, search])

  const handleRouteCreated = useCallback((modelId: string) => {
    onAddOpenChange(false)
    setSearch("")
    setPendingCreatedModelId(modelId)
  }, [onAddOpenChange])

  useEffect(() => {
    if (!pendingCreatedModelId || !activeRoutes.some((model) => model.id === pendingCreatedModelId)) return
    setSelectedModelId(pendingCreatedModelId)
    setPendingCreatedModelId(null)
  }, [activeRoutes, pendingCreatedModelId])

  useEffect(() => {
    if (activeRoutes.some((model) => model.id === selectedModelId)) return
    setSelectedModelId(activeRoutes[0]?.id ?? null)
  }, [activeRoutes, selectedModelId])

  useEffect(() => {
    onRouteDrawerOpenChange(false)
  }, [isMobile, onRouteDrawerOpenChange])

  const selectedModel = activeRoutes.find((model) => model.id === selectedModelId) ?? null
  const selectedPool = selectedModel
    ? poolByModelId.get(selectedModel.id)
    : null
  const memberCount = selectedPool?.members.length ?? 0

  const routeListContent = (closeOnSelect: boolean) => <>
    <div className="router-list-controls apim-native-route-list-controls">
      <label className="smh-search"><Search size={14} /><input value={search} disabled={routeStateLoading} onChange={(event) => setSearch(event.target.value)} placeholder="搜索后端池..." aria-label="搜索 APIM 后端池" /></label>
    </div>
    <div className="router-list-scroll" role="list" aria-label="APIM 后端池列表">
      {routeStateLoading ? <div className="apim-native-route-drawer-state"><RefreshCw className="spin" size={18} />加载 APIM 后端池列表</div> : routeStateError ? <div className="apim-native-route-drawer-state error">无法加载 APIM 后端池</div> : <>{routes.map((model) => {
        const members = poolByModelId.get(model.id)?.members ?? []
        return <div role="listitem" className={`router-list-row ${model.id === selectedModelId ? "active" : ""}`} key={model.id}>
          <button type="button" className="router-list-main" onClick={() => {
            setSelectedModelId(model.id)
            if (closeOnSelect) onRouteDrawerOpenChange(false)
          }}>
            <span className="router-list-icon"><Network size={15} /></span>
            <span className="router-list-copy"><b data-no-localize title={model.display_name}>{model.display_name}</b><small><code data-no-localize title={model.model_key}>{model.model_key}</code><i>·</i><span>{members.length}</span><span>个部署</span></small></span>
          </button>
        </div>
      })}
      {!routes.length && <div className="smh-empty"><Network size={28} /><b>{activeRoutes.length ? "没有匹配的后端池" : "还没有后端池"}</b><span>{activeRoutes.length ? "尝试调整搜索条件。" : "从已有模型中选择并配置 APIM 后端池。"}</span></div>}
      </>}
    </div>
  </>

  return <div className="smh-workspace apim-native-routes-workspace">
    <header className="smh-page-header">
      <div><span className="smh-header-icon"><Network size={17} /></span><h1>APIM 后端池</h1><span>{routeStateLoading ? "—" : activeRoutes.length}</span></div>
      {user?.role === "owner" && <div className="smh-header-actions"><button type="button" disabled={!registry.data || routeStateLoading || Boolean(routeStateError)} onClick={() => onAddOpenChange(true)}><Plus size={14} />添加后端池</button></div>}
    </header>
    {routeStateLoading ? <div className="registry-loading"><RefreshCw className="spin" />加载 APIM 后端池列表</div> : routeStateError || !registry.data ? <div className="registry-error">无法加载 APIM 后端池：{String(routeStateError)}</div> : <div className="router-split apim-native-routes-layout" style={{ "--router-pane-width": `${paneWidth}px` } as CSSProperties}>
      {!isMobile && <aside className="router-list-pane apim-native-route-list">{routeListContent(false)}</aside>}
      <button
        className="runtime-pane-handle"
        type="button"
        role="separator"
        aria-label="调整 APIM 后端池列表宽度"
        aria-orientation="vertical"
        aria-valuemin={paneMinWidth}
        aria-valuemax={paneMaxWidth}
        aria-valuenow={paneWidth}
        title="拖动调整宽度，双击复位"
        onPointerDown={startPaneResize}
        onKeyDown={resizePaneWithKeyboard}
        onDoubleClick={resetPaneWidth}
      />
      <section className="router-editor-pane apim-native-route-detail">
        {selectedModel ? <DeploymentResilienceEditor
          key={selectedModel.id}
          model={selectedModel}
          renderFrame={(editor, controls) => <>
            <header className="apim-native-route-toolbar">
              <b>后端池</b>
              <div className="apim-native-route-toolbar-actions">{controls}</div>
            </header>
            <div className="runtime-detail-scroll apim-native-route-scroll">
              <div className="runtime-detail-grid apim-native-route-detail-grid">
                <div className="runtime-detail-main apim-native-route-main">
                  <section className="runtime-detail-hero apim-native-route-hero">
                    <dl className="runtime-detail-facts apim-native-route-facts">
                      <div className="runtime-detail-fact"><dt>数据面</dt><dd>Azure API Management</dd></div>
                      <div className="runtime-detail-fact"><dt>上游 Deployment</dt><dd data-no-localize>{selectedModel.upstream_model_id ?? selectedModel.model_key}</dd></div>
                      <div className="runtime-detail-fact"><dt>成员</dt><dd>{memberCount}</dd></div>
                      <div className="runtime-detail-fact"><dt>作用范围</dt><dd>模型级 · 直调及所有 Router</dd></div>
                    </dl>
                  </section>
                  <section className="runtime-detail-sidecard apim-native-route-config-card">{editor}</section>
                </div>
              </div>
            </div>
          </>}
        /> : <div className="smh-empty"><Network size={28} /><b>选择一个后端池</b><span>配置等价部署的负载均衡与故障转移。</span></div>}
      </section>
    </div>}
    {isMobile && registry.data && <Dialog open={routeDrawerOpen} onOpenChange={onRouteDrawerOpenChange}>
      <DialogContent className="apim-native-route-drawer" finalFocus={false}>
        <DialogHeader className="apim-native-route-drawer-header"><DialogTitle>APIM 后端池</DialogTitle><DialogDescription className="sr-only">选择一个后端池</DialogDescription><DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="apim-native-route-drawer-close" />}><X size={16} /><span className="sr-only">关闭</span></DialogClose></DialogHeader>
        <aside className="router-list-pane apim-native-route-list apim-native-route-drawer-list">{routeListContent(true)}</aside>
      </DialogContent>
    </Dialog>}
    {registry.data && <ApimNativeRouteCreateDialog
      open={addOpen}
      options={routeOptions}
      onClose={() => onAddOpenChange(false)}
      onCreated={handleRouteCreated}
    />}
  </div>
}
