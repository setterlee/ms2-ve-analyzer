# Cambio de Inyectores 560cc — Resumen para Tuner

**Vehículo:** Lancer 1993 — Motor 4G15 — Turbo planificado ~7 psi  
**ECU:** MegaSquirt MS2 Extra 3.4.4  
**Inyectores nuevos:** INP-020 / MDL560 (OEM EVO 5–9) — 560 cc/min @ 43.5 psi  
**Inyectores actuales:** 180 cc  
**Hardware crítico:** Drivers saturados MS2 + resistencias ballast **50W 7Ω** en serie

---

## Contexto Importante: Intento Anterior Fallido

Ya se probaron estos inyectores (550cc) entre marzo 21 y abril 7 de 2026. Se abandonó y se volvió a los 180cc. **El problema fue `injOpen` demasiado bajo.**

El tune anterior usó `injOpen = 0.91–1.0ms` — valores de la tabla oficial del INP-020 para drivers **peak-and-hold del EVO OEM**. Con ballast en drivers saturados, el dead time real del inyector es ~2.0ms. El inyector nunca abría correctamente en idle.

---

## Hardware: Por Qué el injOpen es Distinto

| Driver | Corriente pico | Dead time aprox |
|--------|---------------|-----------------|
| EVO OEM peak-and-hold | ~4 A | 0.93 ms (tabla oficial) |
| **MS2 saturado + 7Ω ballast** | **1.27 A** | **~2.0 ms (empírico)** |

**La tabla de dead time del fabricante NO aplica a este setup.** El `injOpen` debe determinarse empíricamente.

---

## Parámetros — Valores a Cargar Inicialmente

| Parámetro | Valor actual (180cc) | Valor inicial (560cc) |
|-----------|---------------------|----------------------|
| `reqFuel` | 6.85 ms | **2.20 ms** |
| `injOpen` | 1.0 ms | **2.50 ms** ← alto intencional |
| `battFac` | 0.21 ms/V | **0.15 ms/V** |
| `staged_pri_size` | 184 | 560 (display TS) |

### Prime Pulse (`primePWTable`)

| Bin | Actual | Nuevo |
|-----|--------|-------|
| 1 | 7.0 ms | 2.25 ms |
| 2 | 6.2 ms | 1.99 ms |
| 3 | 5.4 ms | 1.74 ms |
| 4 | 4.7 ms | 1.51 ms |
| 5 | 3.9 ms | 1.25 ms |
| 6 | 3.1 ms | 1.00 ms |
| 7 | 2.3 ms | 0.74 ms |
| 8 | 1.6 ms | 0.51 ms |
| 9 | 0.8 ms | 0.26 ms |
| 10 | 0.0 ms | 0.00 ms |

**Lo que NO cambia:** tabla VE, ignición, `crankPctTable`, AE — se auto-escalan con `reqFuel`.

---

## Protocolo de Ajuste Empírico

### Fase 1 — Encontrar `injOpen`

Motor caliente (>80°C), idle estable, sin cargas eléctricas.

- Partir con `injOpen = 2.50ms` (motor correrá **rico**)
- Bajar en pasos de **0.10ms**, esperar 30s cada paso
- Parar cuando AFR llegue a ~14.0–14.7
- Si AFR cae bruscamente → subir 0.10ms → ese es el valor correcto
- **Zona esperada: 1.5–2.5ms**

> ⚠ Sensibilidad alta: 0.2ms de error = varios puntos de AFR.  
> No intentar valores bajo 1.2ms — el intento anterior lo confirma.

### Fase 2 — Afinar `battFac`

Con `injOpen` correcto:

1. Encender cargas (luces + luneta + ventilador) → voltaje baja ~12V → anotar AFR
2. Apagar cargas → voltaje sube ~14V → anotar AFR

| Resultado | Acción |
|-----------|--------|
| AFR lean con voltaje bajo | `battFac` muy bajo → subir 0.01–0.02 ms/V |
| AFR rich con voltaje bajo | `battFac` muy alto → bajar 0.01–0.02 ms/V |
| AFR estable | ✓ correcto |

### Fase 3 — Confirmar en marcha

- 2000–3000 RPM, carga parcial, MAP 50–80 kPa
- Comparar AFR con comportamiento de los 180cc
- Si desviación sistemática: ajuste fino `reqFuel` en ±0.05ms

---

## Simulación de Referencia

Con dead time real estimado ~2.0ms (ballast setup), impacto de `injOpen` en AFR idle:

| injOpen | AFR idle | |
|---------|----------|-|
| 0.91 ms | sin combustible | ← intento anterior |
| 1.80 ms | ~40 lean severo | |
| **2.00 ms** | **13.86 ← objetivo** | |
| 2.50 ms | ~5.4 rich | ← punto de partida |

---

## Advertencia de Idle

Los 560cc son sobredimensionados para un 1.5L. Incluso con ajuste correcto, el idle puede ser algo inestable — es inherente al tamaño del inyector, no error de tune. A carga normal y boost funciona correctamente (DC máximo proyectado ~15% a 7 psi).
