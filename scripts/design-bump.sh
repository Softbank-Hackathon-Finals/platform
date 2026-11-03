#!/usr/bin/env bash
# Prepare local design history; publication follows the reviewed PR flow.
set -euo pipefail
exec python3 "$(dirname "$0")/design_bump.py" "$@"
