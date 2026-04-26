#!/bin/bash
# build.sh — Compila VE Analyzer para macOS
# Resultado: dist/VE Analyzer.app  (doble clic para abrir)
# La primera vez instala las dependencias automáticamente.

set -e
cd "$(dirname "$0")"

PY=/opt/homebrew/bin/python3.12
VENV=".venv-build"

# Tkinter para Python 3.12 de Homebrew (solo la primera vez)
if ! $PY -c "import tkinter" 2>/dev/null; then
    echo "==> Instalando python-tk@3.12…"
    brew install python-tk@3.12
fi

# Crear virtualenv de build si no existe
if [ ! -d "$VENV" ]; then
    echo "==> Creando entorno virtual de build…"
    $PY -m venv "$VENV"
fi

echo "==> Instalando PyInstaller en el venv…"
"$VENV/bin/pip" install pyinstaller --quiet

echo "==> Compilando…"
"$VENV/bin/pyinstaller" \
    --name "VE Analyzer" \
    --windowed \
    --onefile \
    --clean \
    ve_analyzer_gui.py

echo ""
echo "✓ Listo."
echo "  Mac:  dist/VE Analyzer.app  (arrastra a Aplicaciones o doble clic)"
