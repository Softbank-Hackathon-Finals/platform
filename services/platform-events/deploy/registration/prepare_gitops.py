"""Prepare the registration build context from a clean, committed source tree."""

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def prepare(output):
    if output.exists() or output.resolve().is_relative_to(ROOT):
        raise ValueError("new_external_context_required")
    if subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"]):
        raise ValueError("clean_committed_checkout_required")
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    spec = importlib.util.spec_from_file_location("registration_base", HERE.parent / "product-read/build.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    output.mkdir(parents=True)
    base.prepare_context(output, revision)
    for source, target in (("gitops.Dockerfile", "Dockerfile"), ("checkout.py", "registration-checkout.py")):
        relative = str((HERE / source).relative_to(ROOT))
        (output / target).write_bytes(base.git("show", f"{revision}:{relative}"))
    files = [{"path": str(p.relative_to(output)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(output.rglob("*")) if p.is_file()]
    receipt = {"source_revision": revision, "files": files,
               "context_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise ValueError("new_receipt_required")
    receipt = prepare(args.output)
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: receipt[key] for key in ("source_revision", "context_sha256")}))


if __name__ == "__main__":
    main()
