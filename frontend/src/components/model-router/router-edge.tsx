import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react"

type RouterEdgeData = {
  tone?: "normal" | "exception" | "neutral"
} & Record<string, unknown>

export function RouterEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  label,
  selected,
  data,
}: EdgeProps) {
  const edgeData = (data ?? {}) as RouterEdgeData
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  })
  const tone = edgeData.tone ?? "neutral"
  return <>
    <BaseEdge
      id={id}
      path={path}
      markerEnd={markerEnd}
      className={`router-edge-path router-edge-${tone}${selected ? " selected" : ""}`}
      interactionWidth={20}
    />
    {label && <EdgeLabelRenderer>
      <span
        className={`router-edge-label router-edge-label-${tone}`}
        style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
      >{String(label)}</span>
    </EdgeLabelRenderer>}
  </>
}
