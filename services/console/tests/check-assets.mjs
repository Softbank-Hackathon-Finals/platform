import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const root = new URL("../src/bravo_console/static/", import.meta.url);
const html = readFileSync(new URL("index.html", root), "utf8");
for (const name of ["app.js", "model.js", "transport.js", "style.css"])
  assert.ok(readFileSync(new URL(name, root)).length);
assert.ok(!/<script(?![^>]*src=)/.test(html));
assert.ok(!/\son\w+=/.test(html));
assert.ok(!/https?:\/\//.test(html));
console.log("Console assets: local-only scripts/styles, no inline handlers.");
