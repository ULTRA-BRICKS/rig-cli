#!/bin/sh
# Compatibility entrypoint; all smoke checks live in the Python implementation.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$ROOT/tests/smoke.py" "$@"
