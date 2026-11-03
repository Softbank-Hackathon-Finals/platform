"""Test-only local T10 fixture. Never mount this module in a deployed service."""

import tempfile
from pathlib import Path

import uvicorn
from test_app import setup
from test_reports import wire

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="t21-browser-") as root:
        app, *_ = setup(
            Path(root) / "state.db", origin="http://127.0.0.1:18121", workload_id="demo-chat"
        )
        app.controls = wire(app.api)
        uvicorn.run(app, host="127.0.0.1", port=18121, log_level="warning")
