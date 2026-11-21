#!/usr/bin/env bash
set -euo pipefail

# Package clean source archive (~1 MB) excluding macOS .venv, runtime data, and caches.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
OUT_ZIP="${1:-$PARENT_DIR/book-cleaner-win.zip}"

rm -f "$OUT_ZIP"
(
  cd "$PARENT_DIR"
  zip -r "$OUT_ZIP" "$(basename "$PROJECT_DIR")" \
    -x "*/.venv/*" \
       "*/data/*" \
       "*/__pycache__/*" \
       "*/.pytest_cache/*" \
       "*.DS_Store" \
       "*/dist/*" \
       "*/build/*"
)
ls -lh "$OUT_ZIP"
echo "Done: $OUT_ZIP"
