#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec bash apps/desktop/build.sh
