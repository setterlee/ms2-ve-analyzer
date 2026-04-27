# VE Analyzer — MegaSquirt MS2 / TunerStudio

Herramienta de calibración VE (Volumetric Efficiency) para motores con ECU MegaSquirt MS2 y firmware MS2 Extra. Analiza logs de TunerStudio, detecta zonas lean/rich, genera tablas corregidas listas para importar, y produce un diagnóstico de salud del motor.

Soporta logs en formato texto (`.msl`) y binario (`.mlg`).

---

## Descarga

Descarga el ejecutable para tu sistema operativo desde la sección **Releases** de este repositorio. No requiere instalar nada.

| Sistema | Archivo |
|---------|---------|
| macOS | `VE-Analyzer-macOS.zip` → descomprimir → doble clic en `.app` |
| Windows | `VE-Analyzer.exe` → doble clic |

Coloca el ejecutable en la misma carpeta donde está tu `CurrentTune.msq` y la carpeta `DataLogs/`. La aplicación los detecta automáticamente al abrirse.

### Aviso de SmartScreen en Windows

Al ejecutar por primera vez, Windows puede mostrar el aviso **"Windows protegió tu PC"**. Esto ocurre porque el ejecutable no tiene firma digital de una empresa registrada — es normal en herramientas open source gratuitas.

Para ejecutarlo:

1. Clic en **"Más información"**
2. Clic en **"Ejecutar de todas formas"**

El código fuente de este proyecto es público y auditable en este mismo repositorio.

### Aviso de Gatekeeper en macOS

En macOS puede aparecer un aviso similar al abrir el `.app` por primera vez:

1. Clic derecho en `VE-Analyzer.app` → **Abrir**
2. Clic en **Abrir** en el diálogo de confirmación

Solo es necesario la primera vez.

---

## Uso — Interfaz gráfica (GUI)

Al abrir la aplicación, el panel izquierdo muestra los controles y el derecho los resultados.

### Análisis VE

1. **Archivos:** selecciona tu `CurrentTune.msq` y agrega logs (`.msl` o `.mlg`). El botón **Auto** busca los más recientes en `DataLogs/` automáticamente.
2. **Opciones:** elige la tabla VE (1 o 3), mínimo de muestras por celda, e incluir ralentí si quieres corregir esas celdas.
3. Clic en **Analizar VE**.
4. Los resultados aparecen en las pestañas **Resumen**, **Pobres** y **Ricas**.
5. Clic en **Generar tabla corregida** para guardar el `.table` listo para importar a TunerStudio.

### Diagnóstico de salud

Clic en **Diagnóstico de salud** para obtener un reporte completo del motor: voltaje, temperaturas, sincronización, inyectores, ralentí, AE, y más.

### Calibración AE

Clic en **Calibrar AE** para analizar eventos de aceleración y obtener sugerencias de ajuste para los valores `taeBins` (Added ms) de la curva TAE en TunerStudio.

### Suavizado

Después de varias sesiones de corrección, clic en **Suavizar tabla VE** para generar una tabla suavizada que pondera el historial de correcciones y limita gradientes bruscos entre celdas vecinas.

---

## Uso — Línea de comandos (CLI)

Para usuarios avanzados, el script se puede ejecutar directamente desde la terminal.

```bash
# Análisis básico (modo interactivo — elige logs desde una lista)
python3 ve_analyzer.py

# Usar los 3 logs más recientes
python3 ve_analyzer.py --latest 3

# Especificar logs manualmente
python3 ve_analyzer.py --logs DataLogs/session1.msl DataLogs/session2.mlg

# Análisis con configuración completa
python3 ve_analyzer.py --table-num 1 --latest 5 --min-samples 20

# Solo diagnóstico de salud
python3 ve_analyzer.py --health-only --latest 3

# Guardar reporte de salud como archivo .md
python3 ve_analyzer.py --save-report --latest 3

# Suavizar tabla VE (usa el historial de _corrected.table)
python3 ve_analyzer.py --smooth --table-num 1

# Calibración AE
python3 ve_analyzer.py --ae-cal --latest 5
```

### Parámetros disponibles

| Parámetro | Descripción |
|-----------|-------------|
| `--table-num 1` | Tabla VE del MSQ a usar como base (1 ó 3, default: 1) |
| `--latest N` | Usar los N logs más recientes de `DataLogs/` |
| `--logs f1 f2` | Especificar archivos de log manualmente |
| `--min-samples N` | Mínimo de muestras por celda para aplicar corrección (default: 20) |
| `--include-idle` | Incluir celdas de ralentí (TPS < 3%) en las correcciones |
| `--smooth` | Generar tabla suavizada desde el historial de correcciones |
| `--health-only` | Solo mostrar diagnóstico de salud, sin análisis VE |
| `--no-health` | Omitir el diagnóstico de salud |
| `--save-report` | Guardar diagnóstico de salud como `.md` |
| `--ae-cal` | Análisis de calibración AE (Acceleration Enrichment) |
| `--msq FILE` | Ruta al archivo MSQ (default: `CurrentTune.msq`) |
| `--log-dir DIR` | Directorio de logs (default: `DataLogs/`) |

---

## Flujo de calibración VE recomendado

```
1. Saca logs en condiciones controladas (ver sección siguiente)
2. Abre el analizador → selecciona logs → Analizar VE
3. Revisa las celdas Pobres y Ricas
4. Genera tabla corregida (_corrected.table)
5. Importa el _corrected.table a TunerStudio → Tabla VE 1 → Guarda MSQ
6. Repite desde el paso 1 hasta que no haya zonas fuera de objetivo
7. Cuando converge → Suavizar → importa _smoothed.table → Tabla VE 3
```

La corrección es **idempotente**: el script siempre toma los valores base del MSQ, no del `.table` anterior. Correr el análisis N veces con los mismos logs produce siempre el mismo resultado.

---

## Condiciones para logs válidos

### Condiciones del motor

- **Motor caliente:** CLT > 70°C antes de empezar a loguear
- **Temperatura de aire:** MAT estable entre 38–58°C — el script filtra muestras fuera de este rango automáticamente. Esto favorece logs nocturnos o en climas templados
- **Voltaje estable:** alternador funcionando, > 13V con motor en marcha
- **Tabla activa:** anota con qué tabla VE estabas corriendo (1 ó 3) y usa el parámetro `--table-num` correspondiente al analizar

### Perfil de manejo

El script tiene tres filtros automáticos que descartan muestras no representativas. Para maximizar los datos útiles:

**Sostén las condiciones al menos 2 segundos por zona.**
El script exige que el motor permanezca en la misma celda MAP×RPM durante ≥ 2 segundos consecutivos antes de considerar esas muestras válidas. Las "pasadas rápidas" a través de una celda (aceleraciones suaves desde ralentí, cambios de marcha) se descartan automáticamente.

**Evita cambios bruscos de MAP (MAPdot > 40 kPa/s).**
Transitorios de carga donde el MAP sube rápido antes de que el RPM responda producen lecturas AFR lean artificiales. Abre el acelerador de forma progresiva y suave.

**Varía la carga de forma controlada.**
El perfil ideal incluye:
- Crucero sostenido a distintas velocidades (MAP 40–80 kPa)
- Aceleraciones parciales y plenas para cubrir zonas de alta carga
- Algo de tráfico urbano para cubrir zonas bajas (MAP 30–55 kPa)

### Qué evitar

- Arranques en frío (CLT < 70°C)
- Temperatura ambiente extrema (MAT fuera de 38–58°C)
- Aceleraciones a fondo continuas o carga sostenida al límite
- Conducción muy entrecortada (stop-and-go puro sin periodos de crucero)

### Filtros automáticos

El script descarta automáticamente:

| Condición | Motivo |
|-----------|--------|
| AE activo (`Accel PW > 0.05 ms`) | El ECU añade combustible extra — el AFR no refleja el VE de la celda |
| RPMdot > 400 RPM/s | Motor en desaceleración brusca — colector no está en estado estacionario |
| MAPdot > 40 kPa/s | Transitorio de carga — MAP subiendo antes de que el RPM responda |
| Permanencia < 2 s en la celda | Drive-through — el motor solo pasó por esa zona brevemente |
| TPS < 3% | Decel o ralentí (excepto con `--include-idle`) |
| MAT fuera de 38–58°C | Densidad de aire inestable |
| CLT < 70°C | Motor frío — correcciones WUE activas distorsionan el AFR |

---

## Estructura del proyecto

```
.
├── ve_analyzer.py        # Lógica principal (CLI y motor del análisis)
├── ve_analyzer_gui.py    # Interfaz gráfica (Tkinter, Mac/Windows)
├── build.sh              # Compilar ejecutable para macOS
├── build.bat             # Compilar ejecutable para Windows
└── .github/
    └── workflows/
        └── release.yml   # Build y release automáticos con GitHub Actions
```

El script `ve_analyzer.py` funciona de forma independiente. La GUI es un wrapper que importa sus funciones.

---

## Compilar desde el código fuente

### macOS

```bash
chmod +x build.sh
./build.sh
# Resultado: dist/VE-Analyzer.app
```

### Windows

Instala Python desde [python.org](https://python.org) (marcar "Add to PATH"), luego doble clic en `build.bat`.

```
# Resultado: dist\VE-Analyzer.exe
```

### Sin compilar (solo Python)

Si tienes Python 3.10+ instalado, puedes correr el script directamente:

```bash
pip install pyinstaller  # solo necesario si quieres compilar
python3 ve_analyzer_gui.py   # abre la GUI
python3 ve_analyzer.py       # modo CLI interactivo
```

---

## Compatibilidad

| ECU | Firmware | Logs |
|-----|----------|------|
| MegaSquirt MS2 | MS2 Extra | `.msl`, `.mlg` |

El script lee la tabla VE, los bins de RPM y MAP, y la configuración AE directamente del `CurrentTune.msq`. Funciona con cualquier configuración de bins, incluyendo setups turbo con MAP > 100 kPa.

---

## Licencia

MIT
