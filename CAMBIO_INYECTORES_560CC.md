# Cambio de Inyectores: 180cc → 560cc INP-020

**Fecha documentado:** 2026-05-06  
**Motor:** 4G15 — Lancer 1993 (N/A → turbo planificado ~7 psi)  
**ECU:** MegaSquirt MS2 Extra 3.4.4  
**Tune base:** CurrentTune.msq  
**Inyectores:** INP-020 / MDL560 — OEM EVO 5–9 — 560 cc/min @ 43.5 psi  
**Hardware:** MS2 con drivers saturados + resistencias ballast 50W 7Ω en serie

---

## Contexto: ¿Por qué 560cc?

Planificado para turbo street ~7 psi (~150 hp). Para ese nivel son sobredimensionados pero funcionales en boost. Compromiso: idle rough inherente al tamaño del inyector en un 1.5L.

| HP objetivo | CC requerido (80% DC) | 560cc |
|------------|----------------------|-------|
| 150 hp | ~316 cc | ✓ suficiente |
| 200 hp | ~421 cc | ✓ suficiente |
| 266 hp | ~560 cc | límite 80% DC |

---

## Lección del Intento Anterior (Mar 21 – Abr 7, 2026)

Ya se probaron inyectores de 550cc. Se abandonó el experimento y se volvió a los 180cc el 8 de abril. **El problema fue el `injOpen` — demasiado bajo para el setup de ballast.**

### Progresión del tune anterior con 550cc

| Fecha | reqFuel | injOpen | battFac | Estado |
|-------|---------|---------|---------|--------|
| Mar 21 | 2.300 | 0.950 | 0.0850 | inicio |
| Mar 21 | 2.300 | 1.000 | 0.0750 | |
| Mar 21 | 2.300 | 0.950 | 0.0700 | |
| Mar 27 | 2.300 | 0.950 | 0.0500 | |
| Abr 4 | 2.300 | 0.928 | 0.0420 | |
| Abr 7 | 2.300 | 0.910 | 0.0500 | último |
| **Abr 8** | **6.850** | **1.000** | **0.2400** | **→ volvió a 180cc** |

### Por qué falló

Los valores de `injOpen` (0.91–1.0ms) son los de la **tabla oficial del EVO con drivers peak-and-hold**. Pero el MS2 usa **drivers saturados + ballast 7Ω**, lo que produce un dead time real mucho mayor (~2.0ms).

```
Setup EVO OEM (P&H):   corriente pico = ~4A  →  aguja abre rápido  →  dead time ~0.93ms
Setup MS2 + 7Ω ballast: corriente máx = 14V/11Ω = 1.27A  →  aguja lenta  →  dead time ~2.0ms
```

Con `injOpen=0.91ms` y dead time real ~2.0ms en idle:

```
PW total comandado  = PW_eff (0.32ms) + injOpen_config (0.91ms) = 1.23ms
Dead time real      = ~2.0ms
Inyección efectiva  = 1.23 - 2.0 = NEGATIVO → injector no abre / sin combustible
```

El motor corría lean severo en idle. El `reqFuel=2.30ms` era correcto — solo falló el `injOpen`.

---

## Circuito y Hardware

| Componente | Valor |
|-----------|-------|
| INP-020 resistencia | ~4 Ω (low impedance) |
| Ballast resistor | 7 Ω, 50W |
| Resistencia total | ~11 Ω |
| Corriente máxima a 14V | 14/11 = **1.27 A** |
| Driver MS2 | Saturado (no peak-and-hold) |
| PWM current limiting | `injPwmT2=1.04ms`, `injPwmP=75%`, `injPwmPd=66%` |

El MS2 tiene configurado PWM current limiting, pero la corriente efectiva (~0.95A durante fase pico) sigue siendo muy menor a los ~4A de un driver P&H nativo.

---

## Parámetros a Modificar

### Valores definitivos

| Parámetro | Actual (180cc) | Nuevo (560cc) | Notas |
|-----------|---------------|---------------|-------|
| `reqFuel` | 6.85 ms | **2.20 ms** | Calculado: 6.85 × (180/560) |
| `injOpen` | 1.0 ms | **ver protocolo** | No usar tabla P&H — medir empírico |
| `battFac` | 0.21 ms/V | **ver protocolo** | Ajustar empírico |
| `primePWTable` | ver tabla | **ver tabla** | Escalar × 0.3214 |
| `staged_pri_size` | 184 | **560** | Solo display TS |

### Prime Pulse (`primePWTable`) — factor 180/560 = 0.3214

| Bin temp | Actual (ms) | Nuevo (ms) |
|----------|------------|-----------|
| 1 | 7.0 | 2.25 |
| 2 | 6.2 | 1.99 |
| 3 | 5.4 | 1.74 |
| 4 | 4.7 | 1.51 |
| 5 | 3.9 | 1.25 |
| 6 | 3.1 | 1.00 |
| 7 | 2.3 | 0.74 |
| 8 | 1.6 | 0.51 |
| 9 | 0.8 | 0.26 |
| 10 | 0.0 | 0.00 |

### Tabla dead time INP-020 (referencia P&H — NO usar directamente)

Esta tabla es para drivers peak-and-hold. Con ballast el dead time real es mayor. Se incluye solo como referencia histórica.

| Voltaje | Dead time P&H |
|---------|--------------|
| 6V | 1.900 ms |
| 8V | 1.688 ms |
| 10V | 1.208 ms |
| 12V | 0.928 ms |
| 14V | 0.748 ms |
| 16V | 0.628 ms |

### Parámetros que NO cambian

Tabla VE, `crankPctTable`, tabla de ignición, AE en % — todos porcentuales, se auto-escalan con el nuevo `reqFuel`.

---

## Protocolo de Puesta en Marcha

### Paso 0 — Guardar restore point

Antes de cualquier cambio: **File → Save Tune As** con nombre fechado.

### Paso 1 — Cargar valores base

```
reqFuel         = 2.20 ms
injOpen         = 2.50 ms   ← punto de partida ALTO (intencional)
battFac         = 0.15 ms/V ← punto de partida
primePWTable    = valores de tabla anterior
staged_pri_size = 560
```

El `injOpen=2.50ms` es intencionalmente alto para arrancar en zona rica. Mejor rico que lean al inicio.

### Paso 2 — Arranque

Si el motor **no arranca o apaga por lean** → subir `injOpen` a 3.0ms y reintentar.  
Si arranca pero **muy rico** → continuar al Paso 3.

### Paso 3 — Encontrar `injOpen` correcto

**Condición:** motor caliente (CLT >80°C), idle estable, sin carga eléctrica, wideband activo.  
**AFR objetivo idle:** ~14.0–14.7

1. Con motor en idle, anotar AFR
2. Bajar `injOpen` en pasos de **0.1ms**
3. Esperar 30 segundos en cada paso para que se estabilice
4. Cuando AFR llegue al objetivo → anotar valor
5. Si AFR cae bruscamente (rico repentino) → subir 0.1ms — ese es el límite inferior

> **⚠ La sensibilidad es extrema.** Simulación muestra que 0.2ms de error = varios puntos de AFR.  
> Zona esperada de aterrizaje: **1.5 – 2.5ms**. No usar valores bajo 1.2ms.

### Paso 4 — Afinar `battFac`

**Con `injOpen` correcto y motor estabilizado:**

1. Encender cargas eléctricas (luces altas + luneta + ventilador AC) → voltaje baja ~12V — anotar AFR
2. Apagar todas las cargas → voltaje sube ~14V — anotar AFR

| Observación | Acción |
|-------------|--------|
| AFR lean cuando voltaje baja | `battFac` muy bajo → subir 0.01–0.02 ms/V |
| AFR rich cuando voltaje baja | `battFac` muy alto → bajar 0.01–0.02 ms/V |
| AFR estable en ambas condiciones | `battFac` correcto ✓ |

### Paso 5 — Verificar en marcha

- Rodar a 2000–3000 RPM, carga parcial (MAP 50–80 kPa)
- AFR debe seguir el comportamiento del tune con 180cc
- Si hay desviación sistemática (todo rico o todo lean) → ajuste fino `reqFuel` en ±0.05ms

---

## Simulación: Impacto de injOpen sobre AFR (dead time real estimado ~2.0ms)

| injOpen configurado | AFR idle resultante | Estado |
|--------------------|---------------------|--------|
| 0.91 ms (intento anterior) | sin combustible | ✗ lean severo |
| 1.50 ms | ~52 | ✗ lean severo |
| 1.80 ms | ~40 | ✗ lean |
| **2.00 ms** | **13.86** | **✓ correcto** |
| 2.20 ms | ~8.5 | ✗ rich |
| 2.50 ms | ~5.4 | ✗ muy rich (punto de partida seguro) |

La zona correcta es angosta. El intento anterior (injOpen ~0.91ms) estaba completamente fuera.

---

## Presión de Gasolina (FPR 1:1)

Log confirma FPR funcionando correctamente: diferencial constante ~43.2 psi en toda la sesión.  
Coincide con presión de rating del INP-020 (43.5 psi). **No cambiar presión base** — mejora marginal en idle no justifica complejidad.

---

## Referencia: Estado Actual (180cc) — Log 2026-04-27

| Parámetro | Valor |
|-----------|-------|
| `reqFuel` | 6.85 ms |
| `injOpen` | 1.0 ms |
| `battFac` | 0.21 ms/V |
| PW total idle promedio | 1.639 ms |
| PW efectivo idle | 0.989 ms |
| Dead time a 13.66V | 0.652 ms |
| DC máximo observado | 28.4% |
| Presión diferencial FPR | ~43.2 psi constante |
