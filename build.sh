#!/bin/bash
#
# build.sh - Clone the lbbrhzn/ocpp repo and extract
#            only the files we need (no Home Assistant dependencies).
#
# This script is called during Docker build and can also be run
# locally to refresh the extracted files.
#
# Usage: ./build.sh [--local]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# In Docker, COPY src/ flattens contents into SCRIPT_DIR (no src/ subdir).
# Locally (when run from repo root), files live in a src/ subdirectory.
# Detect which layout we're in.
if [ -d "$SCRIPT_DIR/src" ]; then
    SRC_DIR="$SCRIPT_DIR/src"
else
    SRC_DIR="$SCRIPT_DIR"
fi

TEMP_DIR="$SRC_DIR/_ocpp_temp"
OUT_DIR="$SRC_DIR/ocpp"
REPO_URL="https://github.com/lbbrhzn/ocpp.git"

echo "=== OCPP library extraction ==="

# Clean any previous temp checkout
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

echo "Cloning $REPO_URL ..."
git clone --depth 1 "$REPO_URL" "$TEMP_DIR"

LIB_SRC="$TEMP_DIR/custom_components/ocpp"

# Files that come directly from upstream (NO HA deps)
# These are safe to overwrite each build
UPSTREAM_FILES=(
    "api.py"
    "charge_point.py"
    "const.py"
    "enums.py"
    "central_system.py"
)

mkdir -p "$OUT_DIR"

for file in "${UPSTREAM_FILES[@]}"; do
    if [ -f "$LIB_SRC/$file" ]; then
        cp "$LIB_SRC/$file" "$OUT_DIR/$file"
        echo "  Extracted (upstream): $file"
    else
        echo "  WARNING: $file not found in repo"
    fi
done

# Copy any additional Python modules from the upstream repo
# that don't have Home Assistant imports
EXTRA_FILES=(
    "exceptions.py"
    "ha_entity.py"
)

for file in "${EXTRA_FILES[@]}"; do
    if [ -f "$LIB_SRC/$file" ]; then
        cp "$LIB_SRC/$file" "$OUT_DIR/$file"
        echo "  Extracted (extra): $file"
    fi
done

# If we have a local ocpp_bridge.py that wraps/extend the library,
# copy it into the output directory
if [ -f "$SRC_DIR/ocpp_bridge.py" ]; then
    echo "  Using LOCAL ocpp_bridge.py (custom bridge logic)"
    cp "$SRC_DIR/ocpp_bridge.py" "$OUT_DIR/ocpp_bridge.py"
fi

# Clean up temp directory
rm -rf "$TEMP_DIR"
echo "=== Extraction complete ==="
