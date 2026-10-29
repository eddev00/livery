#!/usr/bin/env bash
# Builds dist/Livery-<version>-x86_64.AppImage.
#   1. engine + tools in an Ubuntu 22.04 container (build-engine.sh) -> build/
#   2. the Electron app with electron-builder, which packs build/ as resources
# Requires docker (or podman with a docker alias) and Node.js >= 20.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$ROOT/build"
docker run --rm -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$ROOT/engine:/src/engine:ro" -v "$ROOT/packaging:/src/packaging:ro" -v "$ROOT/build:/out" \
  ubuntu:22.04 bash /src/packaging/build-engine.sh
cd "$ROOT/app"
npm ci --no-audit --no-fund
npm run build
npx electron-builder --linux AppImage --publish never
ls -lh "$ROOT/dist/"*.AppImage
