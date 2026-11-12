#!/bin/sh
# One-time development setup: the locked environment (wheels only).
set -eu
cd "$(dirname "$0")/.."
uv sync --locked --no-build
echo "ready: uv run tally --help"
