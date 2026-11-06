# Sova network atlas — provenance

UI source: [Softbank-Hackathon-Finals/b-cluster](https://github.com/Softbank-Hackathon-Finals/b-cluster),
commit `dd02ed055606c0be3e756de7a61ffb80e6e913a6`.

The original repository remains independent and unmodified. `upstream.json` records
the original and adapted SHA-256 for each imported asset. Original diagram JPEG bytes
and authorship are preserved. They describe dated team designs, not Sova execution results.
Panzoom 4.6.2 and its MIT license are retained in `vendor/`.

The product renders these capabilities through Sova-owned `network-workspace.js`
and `network-workspace.css` inside the existing console shell. The original site's
HTML, global styles, navigation and application bootstrap are not mounted in the
product UI. Shared reference data, sequence/diagnostic helpers and Panzoom are reused.
The old atlas page URL redirects to the native Sova network view; diagram asset URLs
remain unchanged. Local language controls affect network material only.

Reproduce the UI import from the console directory:

```sh
python scripts/import-network-atlas.py --source /path/to/b-cluster
```

Sova adaptations: branding; same-origin, plan-authorized read-only HTTP/WebSocket;
explicit shared-environment scope; revocation clearing; local font fallback;
browser/preferred locale instead of geo-location; console return link; relative
original image paths; correct WS/WSS label for the active browser transport.
`bridge.js` is Sova-owned integration code, not an upstream file.

The collector, LaunchAgent, publisher/ingest, DNS management Worker, domain workflow,
runtime snapshots, credentials and Sites project configuration are **not** installed
or imported into this application. They remain in the cloned upstream repository.
The original site continues to own its existing collector and publication process.

The protocol explorer, permission matrix, pending list and original diagrams retain
their upstream dates/reference meaning. Resource Ready, peer connectivity and traffic
animation do not establish application health, security approval or data integrity.
In particular, the explanatory path animation is not packet capture.
