import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const root = new URL("../src/bravo_console/static/", import.meta.url);
const html = readFileSync(new URL("index.html", root), "utf8");
for (const name of [
  "app.js",
  "model.js",
  "transport.js",
  "registration.js",
  "recovery.js",
  "style.css",
  "workspace.js",
  "panels.js",
  "operations.js",
  "network-workspace.js",
  "network-workspace.css",
])
  assert.ok(readFileSync(new URL(name, root)).length);
assert.ok(!/<script(?![^>]*src=)/.test(html));
assert.ok(!/\son\w+=/.test(html));
assert.ok(!/https?:\/\//.test(html));
console.log("Console assets: local-only scripts/styles, no inline handlers.");
const network = new URL("network/", root);
const atlasHTML = readFileSync(new URL("index.html", network), "utf8");
assert.ok(!/<script(?![^>]*src=)/.test(atlasHTML));
assert.ok(!/\son\w+=/.test(atlasHTML));
assert.ok(
  !/fonts\.googleapis\.com/.test(
    readFileSync(new URL("style.css", network), "utf8"),
  ),
);
for (const name of [
  "app.js",
  "live.js",
  "bridge.js",
  "i18n.js",
  "architecture.js",
  "sequence.js",
  "diagnostics.js",
  "map-controls.js",
  "vendor/panzoom-4.6.2.es.js",
  "vendor/PANZOOM-LICENSE.txt",
  "architecture-original.jpg",
  "migration-original.jpg",
])
  assert.ok(readFileSync(new URL(name, network)).length);
for (const name of ["app.js", "live.js", "bridge.js", "i18n.js"])
  assert.ok(
    !/fetch\(['"](?:https?:|\/api\/|\/cdn-cgi\/)/.test(
      readFileSync(new URL(name, network), "utf8"),
    ),
  );
console.log(
  "Network atlas: pinned local assets, existing license, fixed same-origin read transport.",
);
