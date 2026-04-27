#!/usr/bin/env bash
# Selran Canvas — installer.
#
# What this does:
#   1. pip-installs selran-canvas (the package providing the 7 canvas tools +
#      the HTTP/WebSocket server + the bundled CSL styles).
#   2. Wires the canvas into selran-mcp if it's available, so the tools
#      auto-appear in Claude desktop alongside writer/design/sada.
#   3. Falls back gracefully — if selran-mcp isn't installed, the canvas
#      still works standalone via `python -m selran_canvas`.
#
# Idempotent — safe to re-run.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SADA_PATH="${HOME}/NeutronDev/Selran datacore skill platform"

echo "→ Installing selran-canvas..."
pip install -e "$SELF_DIR" >/dev/null
echo "✓ selran-canvas installed (Python package + bundled 78 CSL styles)"

if [ -d "$SADA_PATH/mcp_server" ]; then
    echo
    echo "→ Wiring into selran-mcp..."
    pip install -e "$SADA_PATH/mcp_server" >/dev/null
    selran-mcp install
    echo
    echo "✓ Canvas registered with selran-mcp."
    echo "  Restart Claude desktop (⌘Q + reopen) to pick up the new tools."
    echo
    echo "  Verify with:"
    echo "    selran-mcp status         # should list 'canvas' alongside writer/design/sada"
    echo "    selran-mcp scan \"$SELF_DIR\""
else
    echo
    echo "⚠  selran-mcp source not found at $SADA_PATH/mcp_server"
    echo "   Canvas will work standalone:"
    echo "     python -m selran_canvas --demo"
    echo "   To wire into Claude desktop's unified MCP later, install selran-mcp"
    echo "   then re-run this script."
fi

echo
echo "Done."
