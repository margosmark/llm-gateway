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
    const context = row.context.known ? row.context.limit : UNKNOWN_CONTEXT_BUDGET
    const efforts = Array.isArray(row.primary_reasoning_efforts)
      ? row.primary_reasoning_efforts.filter((x) => ["low", "medium", "high", "xhigh", "max"].includes(x))
      : []
    return {
      id: row.id,
      name: row.context.known ? row.id : `${row.id} (200k client budget; upstream unknown)`,
      context,
      input: context - OUTPUT_RESERVE,
      output: OUTPUT_RESERVE,
      tools: row.supports_function_tools === true,
      efforts: [...new Set(efforts)],
      publishedContext: row.context.known,
    }
  })
}
