import { CircleAlert, ExternalLink, Network, X } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import type { ManagedModel } from "../../data-sources/apim/types"
import { DeploymentResilienceEditor } from "./deployment-resilience-editor"
import { Button } from "../ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"

export type NativeRouteModelState = "available" | "active"

export type NativeRouteModelOption = {
  model: ManagedModel
  state: NativeRouteModelState
}

const STATE_ORDER: Record<NativeRouteModelState, number> = {
  available: 0,
  active: 1,
}

function optionStatus(option: NativeRouteModelOption) {
  if (option.state === "active") return "已添加"
  return null
}

export function ApimNativeRouteCreateDialog({
  open,
  options,
  onClose,
  onCreated,
}: {
  open: boolean
  options: NativeRouteModelOption[]
  onClose: () => void
  onCreated: (modelId: string) => void
}) {
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const orderedOptions = useMemo(() => [...options].sort((left, right) =>
    STATE_ORDER[left.state] - STATE_ORDER[right.state]
      || left.model.model_key.localeCompare(right.model.model_key)), [options])
  const selectedOption = orderedOptions.find((option) => option.model.id === selectedModelId) ?? null
  const selectedModel = selectedOption?.state === "available"
    ? selectedOption.model : null
  const modelItems = orderedOptions.map(option => ({
    value: option.model.id,
    label: option.model.display_name,
  }))

  useEffect(() => {
    if (!open) {
      setSelectedModelId(null)
      return
    }
    if (selectedOption) return
    setSelectedModelId(orderedOptions.find(option => option.state === "available")?.model.id
      ?? orderedOptions[0]?.model.id ?? null)
  }, [open, orderedOptions, selectedOption])

  const finishCreation = useCallback(() => {
    if (selectedModelId) onCreated(selectedModelId)
  }, [onCreated, selectedModelId])

  const modelPicker = <label className="registry-field apim-native-route-model-picker">
    <span className="registry-field-label">模型</span>
    <Select value={selectedModelId} items={modelItems} disabled={busy} onValueChange={value => setSelectedModelId(value)}>
      <SelectTrigger aria-label="模型"><SelectValue placeholder="选择模型" /></SelectTrigger>
      <SelectContent align="start" alignItemWithTrigger={false} className="apim-native-route-model-options">
        {orderedOptions.map(option => <SelectItem value={option.model.id} key={option.model.id}>
          <span className="apim-native-route-model-option">
            <span><b data-no-localize>{option.model.display_name}</b><small data-no-localize>{option.model.runtime_name}</small></span>
            <small>{optionStatus(option)}</small>
          </span>
        </SelectItem>)}
      </SelectContent>
    </Select>
  </label>
  const cancel = <Button className="apim-native-route-create-cancel" type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button>

  return <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog apim-native-route-create-dialog" finalFocus={false}>
      <div className="registry-editor">
        <DialogHeader className="registry-editor-header">
          <DialogTitle>添加后端池</DialogTitle>
          <DialogDescription className="sr-only">选择模型与池成员</DialogDescription>
        </DialogHeader>
        <button type="button" className="registry-editor-close" disabled={busy} onClick={onClose} aria-label="关闭"><X size={16} /></button>
        {selectedModel ? <DeploymentResilienceEditor
          key={selectedModel.id}
          model={selectedModel}
          onActivated={finishCreation}
          onBusyChange={setBusy}
          renderFrame={(editor, controls) => <>
            <div className="registry-editor-body apim-native-route-create-body">
              {modelPicker}
              {editor}
            </div>
            <DialogFooter className="registry-editor-footer">{cancel}{controls}</DialogFooter>
          </>}
        /> : <>
          <div className="registry-editor-body apim-native-route-create-body">
            {modelPicker}
            {selectedOption ? <div className="apim-native-route-readiness">
              <div className="apim-native-route-readiness-state"><CircleAlert size={16} /><span><b>已存在后端池</b></span></div>
              <Button className="router-action-primary apim-native-route-readiness-action" type="button" onClick={() => onCreated(selectedOption.model.id)}><ExternalLink size={14} />打开已有后端池</Button>
            </div> : <div className="smh-empty"><Network size={26} /><b>没有匹配的模型</b></div>}
          </div>
          <DialogFooter className="registry-editor-footer">{cancel}</DialogFooter>
        </>}
      </div>
    </DialogContent>
  </Dialog>
}