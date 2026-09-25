#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script builds the macOS desktop app." >&2
  exit 1
fi

PYTHON="${PYTHON:-.venv/bin/python}"
DIST_DIR="${DOCKET_DESKTOP_DIST_DIR:-dist}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$(pwd)/build/.pyinstaller-cache}"
"$PYTHON" -c 'import PySide6, keyring, PyInstaller, docket, docket_desktop' || {
  echo 'Install with: .venv/bin/python -m pip install -e . -e apps/desktop pyinstaller' >&2
  exit 1
}

ICON_SOURCE="$(pwd)/apps/desktop/src/docket_desktop/assets/icon.png"

"$PYTHON" -m PyInstaller \
  --noconfirm --windowed --onedir \
  --name "Docket Desktop" \
  --icon "$ICON_SOURCE" \
  --distpath "$DIST_DIR" \
  --specpath build \
  --collect-data docket \
  --collect-data docket_desktop \
  --hidden-import docket.ocr.pdftext \
  --hidden-import docket.ocr.tesseract \
  --hidden-import docket.ocr.paddle \
  --hidden-import docket.ocr.docling \
  --hidden-import docket.ocr.vlm \
  --hidden-import openpyxl \
  --collect-submodules keyring.backends \
  --osx-bundle-identifier dev.docket.desktop \
  apps/desktop/entry.py

echo "Built: $(pwd)/$DIST_DIR/Docket Desktop.app"
