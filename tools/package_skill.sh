#!/usr/bin/env bash
# Build a publishable skill tarball under dist/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 - <<'PY'
import tomllib
from pathlib import Path
print(tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
PY
)"
OUT_DIR="$ROOT/dist"
NAME="llmcheck-skill-${VERSION}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME"
cp -a "$ROOT/skill/." "$STAGE/$NAME/"
rm -rf "$STAGE/$NAME/__pycache__" "$STAGE/$NAME/.DS_Store" 2>/dev/null || true

mkdir -p "$OUT_DIR"
TAR="$OUT_DIR/${NAME}.tar.gz"
tar -C "$STAGE" -czf "$TAR" "$NAME"
echo "Wrote $TAR"
tar -tzf "$TAR"
