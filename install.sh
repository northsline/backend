#!/bin/sh
# known — install the Known CLI tool
# usage: curl -sSL .../install.sh | sh
#   or:  sh install.sh

set -e

PKG_DIR="known-cli"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  \033[1mKnown\033[0m"
echo "  \033[2minstalling cli…\033[0m"
echo ""

# install the package
pip install --quiet "$SCRIPT_DIR/$PKG_DIR" 2>/dev/null || pip3 install --quiet "$SCRIPT_DIR/$PKG_DIR"

# verify
if command -v known >/dev/null 2>&1; then
    echo "  \033[38;5;108m●\033[0m  installed — run \033[1mknown\033[0m"
else
    echo "  \033[38;5;167m●\033[0m  installed but not on PATH"
    echo "  \033[2madd $(python3 -m site --user-base)/bin to your PATH\033[0m"
fi
echo ""