#!/bin/bash
# build.sh — Compila VE Analyzer para macOS
# Resultado: dist/VE Analyzer.app  (doble clic para abrir)
#
# La primera vez instala las dependencias automáticamente.

set -e
cd "$(dirname "$0")"

# Tkinter para Python 3.12 de Homebrew (solo la primera vez)
if ! /opt/homebrew/bin/python3.12 -c "import tkinter" 2>/dev/null; then
    echo "==> Instalando python-tk@3.12…"
    brew install python-tk@3.12
fi

echo "==> Instalando PyInstaller…"
/opt/homebrew/bin/python3.12 -m pip install pyinstaller --quiet

echo "==> Compilando…"
/opt/homebrew/bin/python3.12 -m pyinstaller \
    --name "VE Analyzer" \
    --windowed \
    --onefile \
    --clean \
    ve_analyzer_gui.py

echo ""
echo "✓ Listo."
echo "  Mac:  dist/VE Analyzer.app  (arrastra a Aplicaciones o doble clic)"
