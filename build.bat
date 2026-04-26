@echo off
REM build.bat — Compila VE Analyzer para Windows
REM Resultado: dist\VE Analyzer.exe  (doble clic para abrir)
REM
REM Requisitos (solo la primera vez):
REM   Instalar Python desde https://python.org  (marcar "Add to PATH")
REM   Luego ejecutar este archivo.

echo =^> Instalando PyInstaller...
pip install pyinstaller --quiet

echo =^> Compilando...
pyinstaller ^
    --name "VE Analyzer" ^
    --windowed ^
    --onefile ^
    --clean ^
    ve_analyzer_gui.py

echo.
echo Listo. Ejecutable en:  dist\VE Analyzer.exe
pause
