import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { runInNewContext } from "node:vm"

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url))
const { rolldown } = require("rolldown")
const bundle = await rolldown({ input: fileURLToPath(new URL("../../../frontend/src/lib/model-inventory-graph.ts", import.meta.url)), platform: "node" })
const code = (await bundle.generate({ format: "cjs" })).output[0].code
await bundle.close()
const exports = {}
runInNewContext(code, { exports, require })

function layout(count, measure) {
  return exports.layoutModelInventoryGraph({ gateways: [], connections: [{
    id: "connection", gatewayId: null, tone: "foundry",
    models: Array.from({ length: count }, (_, index) => ({ id: `model-${index}`, display_name: `Model ${index}`, family_key: "gpt" })),
  }] }, 1440, 900, measure).nodes
}

test("double rings keep labels compact without overlapping models or the provider", () => {
  for (const count of [13, 14, 19, 24, 32]) {
    for (const measure of [() => 250, name => Number(name.split(" ")[1]) % 2 ? 250 : 40]) {
      const [connection, ...models] = layout(count, measure)
      assert.ok(connection.data.innerRadius > 0)
      for (const [index, model] of models.entries()) {
        const width = Number(model.style.width)
        assert.ok(width <= 180)
        const centerX = model.position.x + width / 2
        const centerY = model.position.y + 19
        assert.ok(Math.abs(centerX - connection.data.radius) >= width / 2 + 42
          || Math.abs(centerY - connection.data.radius) >= 61)
        for (const other of models.slice(index + 1)) {
          const otherWidth = Number(other.style.width)
          assert.ok(Math.abs(centerX - other.position.x - otherWidth / 2) >= (width + otherWidth) / 2 + 9.99
            || Math.abs(centerY - other.position.y - 19) >= 47.99, `${count} models: ${model.id} overlaps ${other.id}`)
        }
      }
    }
  }
  assert.ok(layout(14, () => 250)[0].data.radius <= 370)
})

test("single-ring label widths and radii stay unchanged", () => {
  const [connection, ...models] = layout(12, () => 250)
  assert.equal(connection.data.innerRadius, 0)
  assert.equal(connection.data.radius, 360)
  assert.ok(models.every(model => model.style.width === 240))
})