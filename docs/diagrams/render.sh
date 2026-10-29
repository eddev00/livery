#!/bin/sh
# Renders the README diagrams to docs/images at 2x. Needs Chromium.
set -e
cd "$(dirname "$0")"
render() {
  chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --default-background-color=00000000 --allow-file-access-from-files --virtual-time-budget=3000 \
    --window-size="$2,$3" --screenshot="../images/$1.png" "file://$PWD/$1.html" >/dev/null 2>&1
}
render privacy 1000 460
render loop 1000 310
render verify 1000 270
