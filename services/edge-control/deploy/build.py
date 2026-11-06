"""Export only committed controller sources and hash-pinned registry dependencies."""
import hashlib
import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/edge-control"
PACKAGES = {"edge-control": "bravo_edge_control", "platform-events": "bravo_platform_events",
            "control-gate": "bravo_gate", "observability": "bravo_observability"}


def prepare_context(output, revision):
    spec = importlib.util.spec_from_file_location("committed_source", ROOT / "services/platform-events/deploy/product-read/build.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text())
    upstream = project["tool"]["uv"]["sources"]["bravo-observability"]
    if upstream != {"git": "https://github.com/Softbank-Hackathon-Finals/platform.git",
                    "rev": "555956b56e4f22b49036e2a9ed0fcd3f7f97eb79", "subdirectory": "services/observability"}:
        raise ValueError("reviewed_observability_source_required")
    output.mkdir(parents=True)
    requirements = output / "requirements.txt"
    command = ["uv", "export", "--frozen", "--no-header", "--no-dev", "--extra", "t10-host",
               "--no-emit-project", "-o", str(requirements)]
    for name in ("bravo-platform-events", "bravo-control-gate", "bravo-observability"):
        command += ["--no-emit-package", name]
    subprocess.run(command, cwd=SERVICE, check=True, stdout=subprocess.DEVNULL)
    if any(line.strip().startswith(("-e ", "git+", "file:", "../", "/")) or " @ " in line
           for line in requirements.read_text().splitlines() if not line.startswith("#")):
        raise ValueError("registry_dependencies_required")
    for service, package in PACKAGES.items():
        source = upstream["rev"] if service == "observability" else revision
        base.copy_committed_tree(source, f"services/{service}/src/{package}/", output / "src" / package)
    (output / "Dockerfile").write_bytes(base.git("show", f"{revision}:services/edge-control/deploy/Dockerfile"))
    files = [{"path": str(p.relative_to(output)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(output.rglob("*")) if p.is_file()]
    return {"source_revision": revision, "files": files,
            "context_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
