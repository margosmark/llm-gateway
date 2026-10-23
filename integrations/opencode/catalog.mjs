// Pure catalogue validation and mapping, shared by the V2 plugin and tests.
export const UNKNOWN_CONTEXT_BUDGET = 200_000 // Owner-chosen, NOT a Go provider guarantee.
export const OUTPUT_RESERVE = 8_192 // OpenCode-only working reserve, NOT a published output limit.

export function gatewayURL(value) {
  const url = new URL(value)
  const host = url.hostname
  const octets = host.split(".").map(Number)
  const privateIPv4 = octets.length === 4 && octets.every((n, i) =>
    /^\d{1,3}$/.test(host.split(".")[i]) && n >= 0 && n <= 255) &&
    (octets[0] === 10 || octets[0] === 127 ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168))
  if (url.protocol !== "http:" || !privateIPv4 || url.username || url.password ||
      url.search || url.hash || (url.pathname !== "/v1" && url.pathname !== "/v1/")) {
    throw new Error("gateway URL must be http://<private-ip>:<port>/v1 without credentials or query")
  }
  return url.origin + "/v1"
}

export function catalogue(data) {
  if (data?.object !== "list" || !Array.isArray(data.data)) throw new Error("invalid gateway catalogue")
  const ids = new Set()
  return data.data.map((row) => {
    if (!row || typeof row.id !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/.test(row.id) || ids.has(row.id) ||
        typeof row.context !== "object" || row.context === null || typeof row.context.known !== "boolean" ||
        (row.context.known && (!Number.isSafeInteger(row.context.limit) || row.context.limit <= OUTPUT_RESERVE))) {
      throw new Error("invalid alias metadata")
    }
    ids.add(row.id)
    // OpenCode exposes one working input budget to the agent. The gateway
    // keeps the provider's full window in `context.limit`; this client-side
    // reserve turns that into the number an agent can safely use. The
    // reserve is OpenCode's own working allocation, not a claim that the
    // provider publishes an output limit.
    const context = row.context.known ? row.context.limit - OUTPUT_RESERVE : UNKNOWN_CONTEXT_BUDGET - OUTPUT_RESERVE
    // Every effort is the provider's own published data, so it is forwarded
    // rather than curated: a level the gateway says the primary route
    // publishes is a level the owner may select. The grammar check is a
    // safety rail on the way into an OpenCode variant id, not a vocabulary.
    const efforts = Array.isArray(row.primary_reasoning_efforts)
      ? [...new Set(row.primary_reasoning_efforts)]
      : []
    for (const effort of efforts) {
      if (typeof effort !== "string" || !/^[a-z][a-z0-9-]{0,31}$/.test(effort)) {
        throw new Error(`invalid reasoning level: ${JSON.stringify(effort)}`)
      }
    }
    return {
      id: row.id,
      name: row.context.known ? row.id : `${row.id} (200k client budget; upstream unknown)`,
      context,
      input: context,
      output: OUTPUT_RESERVE,
      tools: row.supports_function_tools === true,
      efforts,
      // The OpenCode-side variants. The body uses the Chat Completions
      // spelling, which the gateway normalises on both paths.
      variants: efforts.map((effort) => ({ id: effort, body: { reasoning_effort: effort } })),
      publishedContext: row.context.known,
    }
  })
}
