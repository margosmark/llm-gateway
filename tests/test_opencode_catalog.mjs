import assert from "node:assert/strict"
import { catalogue, gatewayURL } from "../integrations/opencode/catalog.mjs"

assert.equal(gatewayURL("http://10.10.11.109:8765/v1"), "http://10.10.11.109:8765/v1")
for (const value of ["https://example.com/v1", "http://example.com/v1", "http://10.10.11.109:8765/owner/api/state", "http://127.0.0.1:8765/v1?key=x"]) {
  assert.throws(() => gatewayURL(value))
}
const row = (id, limit, known = limit !== null, efforts = ["low", "high", "ultra"], image = false) => ({
  id, context: { limit, known }, supports_function_tools: true,
  supports_image_input: image,
  primary_reasoning_efforts: efforts,
})
const [known, unknown] = catalogue({ object: "list", data: [row("chatgpt", 258400), row("pm", null)] })
// OpenCode displays the working input budget as context; the gateway's
// provider window is still carried in the input budget calculation.
assert.equal(known.context, 250208)
assert.equal(known.input, 250208)
assert.equal(unknown.context, 191808)
assert.equal(unknown.input, 191808)
assert.match(unknown.name, /upstream unknown/)
// Every published level is forwarded: the gateway's data, not a curated list.
assert.deepEqual(unknown.efforts, ["low", "high", "ultra"])
assert.deepEqual(unknown.variants.map((v) => v.id), ["low", "high", "ultra"])
// Image capability is forwarded as an input modality only when the gateway
// says every route that can serve the alias accepts images.
assert.equal(known.image, false)
const [vision] = catalogue({ object: "list", data: [row("vision", 258400, true, [], true)] })
assert.equal(vision.image, true)
assert.deepEqual(vision.efforts, [])
// Version skew: an older gateway that omits the field keeps aliases text-only
// instead of failing the whole refresh.
const [legacy] = catalogue({ object: "list", data: [{ id: "legacy", context: { limit: 258400, known: true },
  supports_function_tools: true, primary_reasoning_efforts: [] }] })
assert.equal(legacy.image, false)
assert.throws(() => catalogue({ object: "list", data: [row("pm", null, false, ["High"]) ] }))
assert.throws(() => catalogue({ object: "list", data: [row("pm", null, false, [42]) ] }))
assert.throws(() => catalogue({ object: "list", data: [row("pm", null), row("pm", null)] }))
assert.throws(() => catalogue({ object: "list", data: [row("unsafe/name", null)] }))
assert.throws(() => catalogue({ object: "list", data: [row("bad", 100, true)] }))
assert.throws(() => catalogue({ object: "list", data: [{
  id: "badimg", context: { limit: 258400, known: true }, supports_function_tools: true,
  supports_image_input: "yes", primary_reasoning_efforts: [],
}] }))
console.log("OpenCode catalogue checks passed")
