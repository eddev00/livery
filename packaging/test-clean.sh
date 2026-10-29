#!/usr/bin/env bash
# Installs the AppImage in a fresh container (no development checkout, no Python, no
# pandoc/typst/poppler on the system) and runs the app's end-to-end smoke test there:
# import, verified renders, element selection, and the agent design loop with the
# deterministic test agent in the Bubblewrap sandbox, then export.
#   usage: packaging/test-clean.sh [image]    (default ubuntu:24.04)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${1:-ubuntu:24.04}
APPIMAGE=$(ls "$ROOT"/dist/Livery-*-x86_64.AppImage | tail -1)
OUT="$ROOT/dist/clean-test"
[ -d "$OUT" ] && docker run --rm -v "$ROOT/dist:/d" "$IMAGE" rm -rf /d/clean-test
mkdir -p "$OUT/docs"
cp "$ROOT"/engine/tests/fixtures/features.docx "$OUT/docs/"
(cd "$ROOT/engine" && uv run python -c "
import sys; sys.path.insert(0, 'tests')
from pathlib import Path
from desktop.docs import report_doc, small_doc
report_doc(Path('$OUT/docs/report.docx')); small_doc(Path('$OUT/docs/suppliers.docx'))")
cp "$APPIMAGE" "$OUT/Livery.AppImage"
chmod -R a+rwX "$OUT"
docker run --rm -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
  -v "$OUT:/test" "$IMAGE" bash -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends bubblewrap xvfb xauth libgtk-3-0t64 libnss3 libasound2t64 libgbm1 \
      libxss1 libxtst6 libdrm2 libsecret-1-0 ca-certificates file >/dev/null 2>&1 || \
    apt-get install -y -qq --no-install-recommends bubblewrap xvfb xauth libgtk-3-0 libnss3 libasound2 libgbm1 \
      libxss1 libxtst6 libdrm2 libsecret-1-0 ca-certificates file >/dev/null
    for t in python3 pandoc typst pdftotext soffice; do command -v $t >/dev/null && echo "unexpected on clean system: $t"; done
    useradd -m tester
    su tester -c "cd /tmp && cp /test/Livery.AppImage . && ./Livery.AppImage --appimage-extract >/dev/null && \
      LIVERY_ENABLE_FAKE_AGENT=1 LIVERY_SMOKE_AGENT=fake LIVERY_SMOKE=/tmp/smoke \
      LIVERY_SMOKE_DOCS=/test/docs/report.docx:/test/docs/features.docx:/test/docs/suppliers.docx \
      timeout 900 xvfb-run -a -s \"-screen 0 1480x920x24\" squashfs-root/AppRun 2>&1 | grep -E \"\\[smoke\\]\" | cut -c1-240; \
      cp -r /tmp/smoke /test/ 2>/dev/null || true"
    chown -R "$HOST_UID:$HOST_GID" /test
  '

ls "$OUT/smoke"
