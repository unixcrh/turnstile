import type { ManagedModel, UsageRequestDetail, UsageRequestSummary } from "../data-sources/apim/types"

type PricedModel = Pick<
  ManagedModel,
  | "id"
  | "model_key"
  | "display_name"
  | "input_cost_per_million"
  | "output_cost_per_million"
  | "cached_cost_per_million"
  | "context_price_tiers"
>

export function hasModelPrices(model: PricedModel): boolean {
  return (model.context_price_tiers?.length ?? 0) >= 2
    || (model.input_cost_per_million != null && model.output_cost_per_million != null)
}

// Telemetry identifies a model by whichever space the caller used: the registry UUID from the
// dashboard BFF, the model key from a desktop client, or the canonical display name written by
// migration 013. Resolving all three keeps one model from being priced in one place and skipped
// in another.
function findPricedModel(models: PricedModel[], key: string, label: string) {
  return models.find((item) => item.id === key)
    ?? models.find((item) => item.model_key === key)
    ?? models.find((item) => item.model_key === label)
    ?? models.find((item) => item.display_name === label)
}

/**
 * Prices one aggregate bucket returned by the trends API. Buckets carry token counts without a
 * stored per-row price snapshot, so this is a registry-price estimate over the complete data set
 * rather than the settled cost the executive KPI reports.
 */
export function estimateBucketCost(
  bucket: { key: string; label: string; input: number; cached: number; output: number },
  models: PricedModel[],
): number | null {
  const model = findPricedModel(models, bucket.key, bucket.label)
  if (model?.context_price_tiers?.length) return null
  if (model?.input_cost_per_million == null || model.output_cost_per_million == null) return null
  const cachedRate = model.cached_cost_per_million ?? model.input_cost_per_million
  return (
    bucket.input * model.input_cost_per_million
    + bucket.cached * cachedRate
    + bucket.output * model.output_cost_per_million
  ) / 1_000_000
}

export function estimateRequestCost(
  request: UsageRequestSummary,
  models: PricedModel[],
): number | null {
  if (request.usage_available === false) return null
  if (request.estimated_cost > 0) return request.estimated_cost
  if (request.cost_available === true) return request.estimated_cost
  if (request.cost_available === false) return null
  const model = models.find((item) => item.id === request.model_id)
    ?? models.find((item) => item.model_key === request.model_name)
  if (model?.context_price_tiers?.length) return null
  if (model?.input_cost_per_million == null || model.output_cost_per_million == null) return null
  const cachedTokens = Math.max(0, request.total_tokens - request.prompt_tokens - request.completion_tokens)
  // Providers bill cached input far below the base rate. Without a configured rate the input rate
  // is kept as the conservative fallback rather than silently treating cached tokens as free.
  const cachedRate = model.cached_cost_per_million ?? model.input_cost_per_million
  return (
    request.prompt_tokens * model.input_cost_per_million
    + cachedTokens * cachedRate
    + request.completion_tokens * model.output_cost_per_million
  ) / 1_000_000
}

export function priceRequests<T extends UsageRequestSummary>(
  requests: T[],
  models: PricedModel[],
): T[] {
  return requests.map((request) => {
    const estimatedCost = estimateRequestCost(request, models)
    return estimatedCost == null ? request : { ...request, estimated_cost: estimatedCost }
  })
}

export function requestCostCalculation(request: UsageRequestDetail) {
  if (request.usage_available === false || request.cost_available === false
    || (request.estimated && !request.reconciled_at)) return null
  if (request.router_attempts?.length) {
    const measured = request.router_attempts.filter(
      (attempt) => (attempt.total_tokens ?? 0) > 0 || (attempt.estimated_cost ?? 0) > 0,
    )
    if (!measured.length) return null
    const lowerBound = request.ingest_error === "stream_cache_usage_unavailable"
    const terms: Array<{ bucket: string; label?: string; tokens: number; rate: number }> = []
    type ContextTierItem = NonNullable<NonNullable<UsageRequestDetail["price_context_snapshot"]>["context_tiers"]>[number]
    let resolvedContextTier: ContextTierItem | null = null
    let resolvedInputTokens = request.prompt_tokens + request.cached_tokens
    for (const attempt of measured) {
      const isPrimary = attempt.request_id === request.request_id
        || attempt.model_id === (request.selected_model_id ?? request.model_id)
      const attemptAllZeroRates =
        attempt.input_price_per_million === 0
        && attempt.output_price_per_million === 0
        && (attempt.cached_price_per_million ?? 0) === 0
        && (attempt.cache_write_price_per_million ?? 0) === 0
      const preferRequestRates =
        isPrimary
        && attemptAllZeroRates
        && (
          (attempt.estimated_cost ?? 0) > 0
          || request.price_context_tier != null
          || (request.price_context_snapshot?.context_tiers?.length ?? 0) > 0
          || (request.input_price_per_million ?? 0) > 0
          || (request.output_price_per_million ?? 0) > 0
        )
      const rawInRate = preferRequestRates
        ? request.input_price_per_million
        : (attempt.input_price_per_million ?? (isPrimary ? request.input_price_per_million : null))
      const rawOutRate = preferRequestRates
        ? request.output_price_per_million
        : (attempt.output_price_per_million
          ?? (isPrimary ? request.output_price_per_million : null)
          ?? (rawInRate != null && (attempt.output_tokens ?? 0) === 0 ? 0 : null))
      if (rawInRate == null || rawOutRate == null) return null
      const inRate = Number(rawInRate)
      const outRate = Number(rawOutRate)
      const cachedRate = Number(
        (preferRequestRates ? request.cached_price_per_million : attempt.cached_price_per_million)
          ?? (isPrimary ? request.cached_price_per_million : null)
          ?? inRate,
      )
      const writeRate = Number(
        (preferRequestRates ? request.cache_write_price_per_million : attempt.cache_write_price_per_million)
          ?? (isPrimary ? request.cache_write_price_per_million : null)
          ?? cachedRate,
      )
      if ([inRate, outRate, cachedRate, writeRate].some((v) => !Number.isFinite(v) || v < 0)) {
        return null
      }
      const inTokens = Number(attempt.input_tokens ?? 0)
      const cachedTokens = Number(attempt.cached_tokens ?? 0)
      const writes = Number(attempt.cache_write_tokens ?? 0)
      const outTokens = Number(attempt.output_tokens ?? 0)
      if (
        [inTokens, cachedTokens, writes, outTokens].some((v) => !Number.isSafeInteger(v) || v < 0)
        || writes > cachedTokens
      ) {
        return null
      }
      const attemptInputTokens = inTokens + cachedTokens
      const tiers = attempt.price_context_snapshot?.context_tiers
        ?? (isPrimary ? request.price_context_snapshot?.context_tiers : undefined)
        ?? []
      const tierKey = attempt.price_context_tier
        ?? (isPrimary ? request.price_context_tier : null)
        ?? null
      if (tiers.length) {
        const matchedTier = tiers.find((tier) => tier.key === tierKey) ?? null
        if (
          !matchedTier
          || lowerBound
          || attemptInputTokens < matchedTier.min_input_tokens
          || (matchedTier.max_input_tokens != null && attemptInputTokens > matchedTier.max_input_tokens)
        ) {
          return null
        }
        if (isPrimary || resolvedContextTier === null) {
          resolvedContextTier = matchedTier
          resolvedInputTokens = attemptInputTokens
        }
      }
      const cachedRead = Math.max(0, cachedTokens - writes)
      if (measured.length === 1) {
        terms.push(
          { bucket: "input", tokens: inTokens, rate: inRate },
          ...lowerBound ? [] : [
            { bucket: "cached", tokens: cachedRead, rate: cachedRate },
            { bucket: "write", tokens: writes, rate: writeRate },
          ],
          { bucket: "output", tokens: outTokens, rate: outRate },
        )
      } else {
        const shortModel = attempt.model_name.split(" · ")[0]
        const prefix = attempt.attempt_role === "routing_decision"
          ? `判路由(${shortModel}) `
          : `推理(${shortModel}) `
        terms.push({ bucket: `s${attempt.sequence}-in`, label: `${prefix}未缓存输入`, tokens: inTokens, rate: inRate })
        if (!lowerBound && cachedRead > 0) {
          terms.push({ bucket: `s${attempt.sequence}-cached`, label: `${prefix}缓存读取`, tokens: cachedRead, rate: cachedRate })
        }
        if (!lowerBound && writes > 0) {
          terms.push({ bucket: `s${attempt.sequence}-write`, label: `${prefix}缓存写入`, tokens: writes, rate: writeRate })
        }
        terms.push({ bucket: `s${attempt.sequence}-out`, label: `${prefix}输出`, tokens: outTokens, rate: outRate })
      }
    }
    const calculatedCost = Number((terms.reduce((sum, term) => sum + term.tokens * term.rate, 0) / 1_000_000).toFixed(8))
    return {
      terms,
      contextTier: resolvedContextTier,
      inputTokens: resolvedInputTokens,
      calculatedCost,
      lowerBound,
      recordedCost: request.estimated_cost,
      matchesRecordedCost: Math.abs(calculatedCost - request.estimated_cost) < 0.00000005,
    }
  }
  const inputRate = request.input_price_per_million
  const outputRate = request.output_price_per_million
  if (inputRate == null || outputRate == null) return null
  const cachedRate = request.cached_price_per_million ?? inputRate
  const writeRate = request.cache_write_price_per_million ?? cachedRate
  const writes = request.cache_write_tokens ?? 0
  const counts = [request.prompt_tokens, request.cached_tokens, writes, request.completion_tokens]
  if (counts.some(value => !Number.isSafeInteger(value) || value < 0)
    || writes > request.cached_tokens
    || [inputRate, cachedRate, writeRate, outputRate, request.estimated_cost]
      .some(value => !Number.isFinite(value) || value < 0)) return null
  const lowerBound = request.ingest_error === "stream_cache_usage_unavailable"
  const terms: Array<{ bucket: string; label?: string; tokens: number; rate: number }> = [
    { bucket: "input", tokens: request.prompt_tokens, rate: inputRate },
    ...lowerBound ? [] : [
      { bucket: "cached", tokens: request.cached_tokens - writes, rate: cachedRate },
      { bucket: "write", tokens: writes, rate: writeRate },
    ],
    { bucket: "output", tokens: request.completion_tokens, rate: outputRate },
  ]
  const inputTokens = request.prompt_tokens + request.cached_tokens
  const tiers = request.price_context_snapshot?.context_tiers ?? []
  const contextTier = tiers.find(tier => tier.key === request.price_context_tier) ?? null
  if (tiers.length && (!contextTier || lowerBound
    || inputTokens < contextTier.min_input_tokens
    || (contextTier.max_input_tokens != null && inputTokens > contextTier.max_input_tokens))) return null
  const calculatedCost = Number((terms.reduce((sum, term) => sum + term.tokens * term.rate, 0) / 1_000_000).toFixed(8))
  return {
    terms, contextTier, inputTokens, calculatedCost, lowerBound,
    recordedCost: request.estimated_cost,
    matchesRecordedCost: Math.abs(calculatedCost - request.estimated_cost) < 0.000000005,
  }
}
