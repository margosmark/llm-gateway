import { Model, Plugin, Provider } from "@opencode/plugin"
import { catalogue, gatewayURL } from "./catalog.mjs"

// Install globally through opencode.jsonc. This plugin never calls an upstream
// provider: only the gateway's published, metadata-only LAN catalogue.
export default Plugin.define({
  id: "personal-llm-gateway",
  async setup(ctx) {
    const endpoint = gatewayURL(String(ctx.options.baseURL ?? ""))
    const providerID = Provider.ID.make("personal-gateway")
    const source = { models: [] as ReturnType<typeof Model.Info.default>[] }

    const refresh = async () => {
      const response = await fetch(endpoint + "/alias-catalog", {
        redirect: "error",
        signal: AbortSignal.timeout(5_000),
      })
      if (!response.ok) throw new Error(`gateway catalogue returned HTTP ${response.status}`)
      const rows = catalogue(await response.json()) // Validate everything before replacing anything.
      source.models = rows.map((row) => ({
        ...Model.Info.default(providerID, Model.ID.make(row.id)),
        name: row.name,
        capabilities: {
          ...Model.Info.default(providerID, Model.ID.make(row.id)).capabilities,
          tools: row.tools,
          input: ["text"], // Image routing is not yet enforced by the gateway.
          output: ["text"],
          reasoning: { supported: row.efforts.length > 0 },
        },
        limit: { context: row.context, input: row.input, output: row.output },
        // OpenCode's semantic reasoning settings vary across protocol drivers.
        // A variant injects the Chat Completions field our gateway normalises.
        variants: row.efforts.map((effort) => ({ id: effort, body: { reasoning_effort: effort } })),
      }))
      await ctx.provider.reload()
    }

    // A failed first read must not fabricate aliases. Later failures keep the
    // last complete catalogue; they do not erase the owner's selection.
    await ctx.provider.transform((editor) => {
      editor.add({
        info: {
          ...Provider.Info.empty(providerID),
          name: "Personal LLM Gateway (trusted LAN)",
          activation: "enabled",
          package: "@opencode/ai/providers/openai-compatible",
          settings: { baseURL: endpoint },
        },
        models: source.models,
      })
    })
    try { await refresh() } catch (error) { console.warn("gateway catalogue unavailable", error instanceof Error ? error.message : "unknown") }
    const timer = setInterval(() => void refresh().catch((error) => {
      console.warn("gateway catalogue refresh failed", error instanceof Error ? error.message : "unknown")
    }), 60_000)
    return () => clearInterval(timer)
  },
})
