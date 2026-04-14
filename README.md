# 4G15 NA v2 — Calibración VE con MegaSquirt MS2

Motor: Mitsubishi 4G15 N/A  
ECU: MegaSquirt MS2 Extra  
Vehículo: Lancer 1993

---

## Flujo de calibración VE

### Ciclo de ajuste (repetir hasta convergencia)

```
1. Saca logs en condiciones controladas
2. Importa la tabla corregida a TunerStudio → Tabla 1 → Guarda MSQ
3. Corre el análisis: python3 ve_analyzer.py --table-num 1 --latest N --min-samples 20
4. Importa el _corrected.table generado → Tabla 1 → Guarda MSQ
5. Repite desde el paso 1
```

Cuando todas las celdas convergen (sin zonas lean ni rich), el script lo detecta
automáticamente, genera la tabla suavizada final y declara el ciclo completo.

### Suavizado (después de aplicar correcciones)

```
python3 ve_analyzer.py --smooth --table-num 1
```

Importa el `_smoothed.table` generado → **Tabla 3** en TunerStudio (tabla activa en ruta).

### Uso de tablas en TunerStudio

| Tabla | Contenido | Propósito |
|-------|-----------|-----------|
| Tabla 1 | `_corrected.table` (última) | Base matemática para próxima sesión / fallback de emergencia |
| Tabla 3 | `_smoothed.table` (última) | Tabla activa en ruta — armónica y suavizada |

**En ruta:** tabla 3 activa.  
**Emergencia:** cambiar a tabla 1 desde TunerStudio si algo falla en ruta.

---

## Condiciones para tomar logs válidos

Para que el análisis sea preciso, los logs deben tomarse en condiciones controladas:

- **Hora:** tarde (MAT estable, generalmente 38–55°C)
- **Motor caliente:** CLT > 70°C antes de empezar
- **Perfil de manejo:** urbano normal, incluyendo aceleraciones parciales
- **Voltaje estable:** motor en marcha con alternador funcionando (>13V)
- **Evitar:** arranques en frío, alta carga sostenida, temperatura ambiente extrema

El script filtra automáticamente muestras fuera del rango MAT 38–58°C y TPS < 3%
(excepto con `--include-idle`).

---

## Parámetros principales del script

```
--table-num 1        Tabla VE del MSQ a usar como base (1 = corrected, 3 = smoothed)
--latest N           Usar los N logs más recientes
--logs f1 f2 ...     Especificar logs manualmente
--min-samples 20     Mínimo de muestras por celda para corregir
--include-idle       Incluir celdas de ralentí (TPS<3%) en correcciones
--smooth             Generar tabla suavizada desde el último _corrected.table
--no-health          Omitir diagnóstico de salud del motor
--health-only        Solo mostrar diagnóstico, sin análisis VE
--save-report        Guardar diagnóstico como archivo .md
```

---

## Archivos generados

Todos los `.table` se guardan en `ve-calibration-process/`:

| Patrón | Descripción |
|--------|-------------|
| `veTable1Tbl_FECHA_corrected.table` | Resultado de cada sesión de análisis |
| `veTable1Tbl_FECHA_smoothed.table` | Tabla suavizada lista para cargar en tabla 3 |

El historial de `_corrected.table` consecutivos es la fuente de verdad para
el algoritmo de amortiguación de celdas oscilantes (`cell_damping`).

---

## Propiedades del algoritmo

- **Base VE:** siempre leída del MSQ (idempotente — correr el script N veces da el mismo resultado)
- **Dead band:** correcciones < 2 puntos VE se ignoran (ruido)
- **Cell damping:** celdas que han oscilado entre sesiones reciben 50% de la corrección
- **Filtro MAT:** solo muestras con MAT 38–58°C (evita sesgo por densidad del aire)
- **min-samples por zona:** zona baja MAP (≤40 kPa) requiere mínimo 20 muestras; zona alta usa el valor de `--min-samples`
- **Suavizado:** mezcla ponderada por distancia inversa; celdas con historial frecuente se anclan, celdas sin historial se suavizan hacia vecinos
