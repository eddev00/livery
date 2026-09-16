#!/usr/bin/env bash
# Copy one verified lorem twin into the repo so it can be shared / shown to Claude.
#
# Real inputs and outputs live OUTSIDE this repo and must never enter it. The only thing
# that may cross the boundary is a lorem twin whose anonymiser recorded a clean
# verification: wordeasy.json carries "anonymized": true, which the pipeline writes ONLY
# after verify_anonymized() and the extracted-text leak scan have both passed. Everything
# the twin build places under lorem/ is derived from the anonymised copy, so copying that
# whole subtree (and nothing beside it) is safe. This script refuses anything without the
# clean marker and never touches the real siblings that sit next to lorem/.
#
# Usage: scripts/export-twin.sh <path-to-real-out>/<doc>   [dest-name]
set -euo pipefail

src="${1:?usage: export-twin.sh <out>/<doc> [dest-name]}"
twin="$src/lorem"
marker="$twin/wordeasy.json"
dest_name="${2:-$(basename "$src")}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$repo_root/cases/$dest_name"

[ -d "$twin" ]   || { echo "refuse: no twin at $twin" >&2; exit 1; }
[ -f "$marker" ] || { echo "refuse: no $marker (twin never verified)" >&2; exit 1; }

python3 - "$marker" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("anonymized") is not True:
    sys.exit("refuse: twin is not marked anonymized (verification did not pass)")
PY

rm -rf "$dest"
mkdir -p "$dest"
cp -a "$twin/." "$dest/"
echo "exported verified twin -> cases/$dest_name/"
find "$dest" -maxdepth 1 -type f | sort | sed 's#.*/cases/#  cases/#'
