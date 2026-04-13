# Revisiones Pendientes — 4G15 NA / MS2Extra
**Generado:** 2026-04-12  
**Base de análisis:** 28 logs MSL de abril 2026 (222,637 filas) + CurrentTune.msq  
**Motor:** Lancer 1993 — 4G15 1499cc NA — MegaSquirt MS2Extra 3.4.4

---

## CRÍTICO — Atender antes de rodar agresivamente

### 1. Knock sensor desactivado (`knk_option = "Disabled"`)
- El sistema de detección de detonación está completamente apagado.
- La tabla de avance llega hasta **36°** en zonas de baja carga — valores agresivos sin ninguna red de protección.
- Si ocurre detonación, el motor no retarda y el daño puede ser inmediato (pistones, bielas).
- **Acción:** Instalar sensor de knock e integrar al MS2, o ser muy conservador con el avance en zonas de carga media-alta hasta confirmar que no hay detonación.

### 2. Lost sync durante operación (motor en movimiento)
- Se detectaron **2 eventos de sync perdido con motor corriendo:**
  - `RPM=1510 CLT=42°C` — probablemente al arrancar
  - `RPM=937 CLT=79°C` — ralentí caliente, motor en marcha → el más preocupante
- Lost Sync Count máximo observado: **6 en una sola sesión**.
- Puede causar cortes de ignición esporádicos, fallas de arranque o comportamiento errático.
- **Posibles causas:** ruido eléctrico en el cable del CAS, mal blindaje, alternador ruidoso (problema conocido), o diente del reluctor sucio/dañado.
- **Acción:** Revisar blindaje del cable del sensor CAS, asegurar masa sólida del MS2, verificar gaps del reluctor.

---

## MODERADO — Revisar pronto

### 3. IAC prácticamente cerrado en ralentí caliente
- **IAC Duty Cycle promedio en ralentí (CLT > 60°C): 0.8%** (prácticamente cero).
- Solo el 0.6% del tiempo el IAC supera el 15%.
- La configuración es `"Open-loop (warmup)"` — el IAC se cierra al superar la fast idle temp (60°C). Esto es *por diseño*, pero implica que **todo el aire de ralentí en caliente viene del tornillo/bypass mecánico** y el sistema no tiene capacidad de compensar cargas eléctricas o demandas abruptas.
- Esto explica las **169 caídas de RPM por debajo de 750** en ralentí caliente y los **127 eventos RPM < 700** con motor caliente.
- **Acción:** Evaluar cambiar `IdleCtl_alg` a closed-loop para que el IAC pueda compensar en caliente, o al menos ajustar la posición del tornillo de aire para que el ralentí base sea estable a ~850-900 RPM sin necesitar el timing para mantenerse.

### 4. Oscilación de RPM en ralentí — el timing está haciendo el trabajo del IAC
- El **15.5%** del tiempo en ralentí caliente hay swings de RPM mayores a **150 RPM**.
- `SPK: Idle Correction Advance` está activo el **13% del tiempo** (hasta 14° de corrección).
- El motor está usando la corrección de timing de ralentí como compensador porque el IAC no actúa en caliente. Esto es una solución de parche: el timing puede corregir RPM pero con latencia y con impacto en la mezcla.
- **Acción:** Si el IAC se configura en closed-loop, el timing correction debería ser solo un auxiliar, no el controlador primario.

### 5. MAT Retard inactivo a pesar de temperaturas altas de aire
- La tabla `matRetard` está configurada con retardos desde 71°C de MAT (2° a 93°C, 4° a 104°C...).
- En los logs, **el MAT promedio es 44.5°C y llega hasta 64.2°C**, con **10.6% del tiempo > 55°C**.
- Sin embargo: **0 lecturas de MAT Retard activo en todo el historial de logs**.
- Causa probable: la función de MAT retard puede requerir una opción de feature activada en el firmware que no está habilitada, o el umbral mínimo (71°C MAT) nunca se alcanza en los logs (confirmado, max 64.2°C).
- **Acción:** La función técnicamente no se activa porque el MAT no llega a 71°C. Sin embargo, con clima cálido o carga prolongada puede llegar. Revisar si vale la pena bajar el umbral a ~55°C o si el sensor MAT está midiendo correctamente (intenta tocarlo en caliente).

### 6. AFR rico en ralentí caliente
- **9.1% del tiempo en ralentí caliente (CLT > 70°C, TPS < 3%) el AFR está por debajo de 13.0**.
- El AFR target en ralentí es 14.7. Hay una desviación consistente hacia mezcla rica en algunos rangos.
- Combinado con el IAC casi cerrado, no hay manera de que el sistema compense sin cerrar el loop de EGO.
- **Acción:** Revisar las celdas de VE en las zonas de ralentí (MAP 20–45 kPa, RPM 800–1000). Con el script ya tienes las herramientas — corre un análisis específico en esas celdas.

### 7. Timing error hasta 12.7% en operación normal
- 365 lecturas con `Timing Err%` mayor a ±5%, con máximo de **12.7%**.
- Los eventos ocurren en condiciones normales:
  - RPM=818 MAP=42kPa CLT=60°C → Error = -10.1%
  - RPM=1007 MAP=45kPa CLT=59°C → Error = +5.3%
  - RPM=1044 MAP=76kPa CLT=71°C → Error = -8.0%
- Timing error en MS2 indica que el tiempo entre dientes del reluctor no es el esperado — señal de ruido eléctrico, CAS degradándose, o reluctor con irregularidades.
- Está correlacionado con los lost sync events (misma causa raíz probable).
- **Acción:** Misma revisión que punto 2 — cable CAS, blindaje, masa. Considerar activar `NoiseFilterOpts` en el MSQ.

---

## MENOR — Monitorear / optimizar

### 8. Cold start rico (25.6% del tiempo con CLT < 50°C, AFR < 13.0)
- Con motor frío, el AFR cae por debajo de 13.0 en casi 1 de cada 4 lecturas.
- WUE máximo de 130% activo en arranque frío.
- Cold Advance activo (máximo 4.7°, promedio 3.8°) — correcto y esperado.
- El 3.8% del tiempo en frío está lean (> 14.7 AFR), lo que puede causar dificultad de arranque.
- **Acción:** Si el arranque en frío todavía falla a veces, revisar WUE y/o ASE (After Start Enrichment). Si arranca bien, ignorar por ahora.

### 9. RPM máximo registrado: 4,457 RPM
- En todos los logs de abril, **nunca se superaron los 4,500 RPM**.
- Las tablas de VE e ignición tienen zonas configuradas hasta 8,000 RPM que nunca se ejercitan.
- **Acción:** No es un problema activo, pero significa que no tienes datos para validar el comportamiento en RPM altas. Las correcciones de VE hechas hasta ahora son solo para rango urbano/ralentí. Cuando puedas hacer una rodada con aceleración fuerte, correla esas zonas.

### 10. IAC RPM target vs realidad
- La tabla IAC target va de 1,500 RPM (21°C) a 850 RPM (60°C).
- El RPM promedio en ralentí caliente es **968 RPM** — ligeramente alto vs los 850 esperados a temperatura plena.
- Como el IAC no actúa en caliente (open-loop warmup), el RPM real queda determinado por el bypass mecánico + timing correction.
- **Acción:** El tornillo de bypass mecánico podría ajustarse ligeramente hacia menos apertura para bajar el ralentí caliente a ~900 RPM.

### 11. AE máximo de 318% — revisar eventos extremos
- El AE llega hasta 318% en algunos eventos (151 filas con AE > 200%).
- El AE promedio cuando activo es 130%, lo cual es razonable.
- Los eventos extremos pueden ser transitorias bruscas o ruido de sensor TPS.
- **Acción:** Revisar en los logs si esos eventos coinciden con aceleraciones reales o con ruido de señal.

### 12. Voltaje con caídas puntuales a 10V (241 lecturas < 12V)
- El voltaje promedio es 13.92V, pero hay 241 lecturas por debajo de 12V.
- La lectura mínima de 10V es con RPM=0 (motor apagado o justo encendiendo).
- El problema del alternador es conocido y pendiente. Mientras no esté resuelto, el `battFac` (compensación de dwell por voltaje) es crítico para que las chispas sean consistentes.
- El `battFac` actual es **0.24 ms/V** — confirmar que esté calibrado para las bobinas COP instaladas.

---

## Sugerencias del MSQ — Configuración

### 13. EGO (closed loop) desactivado — `egoType = "Disabled"`
- Sabes que andas open loop. Pero vale la pena tenerlo en mente: sin EGO activo, cualquier deriva del sensor de temperatura, presión barométrica o inyectores hace que la mezcla se vaya sin corrección.
- **Sugerencia a futuro:** Activar EGO con banda estrecha (o el WB que ya tienes) para tener al menos una referencia en ralentí y crucero. El script de VE puede seguir siendo la herramienta principal de ajuste.

### 14. Enhanced Acceleration Enrichment (EAE) desactivado — `EAEOption = "Off"`
- El EAE es una forma más sofisticada de calcular el enriquecimiento de aceleración considerando la película de combustible en los puertos.
- Está desactivado. El AE estándar (TPS/MAP dot) está funcionando, pero en un motor con inyección directa de puerto el EAE puede dar transitorias más suaves.
- **Sugerencia:** Probar activar EAE si los ahogos en aceleración persisten — especialmente con motor frío.

### 15. advanceTable2 toda en ceros
- La segunda tabla de avance de ignición (`advanceTable2`) contiene **todos ceros**.
- No debería estar siendo usada activamente, pero si por algún modo se selecciona (trigger de tabla 2), el motor andaría sin avance.
- **Acción:** Verificar que no hay condición que active la tabla 2 de ignición inesperadamente. Si no se usa, está bien dejada así como indicador visual de "no configurada".

### 16. Launch Control activo en pin FLEX — `launch_opt_pins = "FLEX"`
- Launch Control está configurado con límite blando a 300 RPM antes del límite de 4000 RPM, y retardo de -5°.
- El pin de activación es FLEX, que en tu setup probablemente no tiene nada conectado.
- **Acción:** Confirmar que el pin FLEX está en estado correcto (abierto/cerrado) para no activar Launch Control accidentalmente.

### 17. Baro default 94 kPa — verificar con altitud real
- `baro_default = 94 kPa` es la presión barométrica de referencia cuando el motor no ha leído MAP en estático.
- A nivel del mar son ~101.3 kPa. A 500m son ~95 kPa. A 1000m son ~90 kPa.
- Los logs muestran barometría real entre **90.7 – 95.1 kPa** (promedio ~94), por lo que el default está bien calibrado para tu zona.
- **Acción:** Sin cambios necesarios, solo confirmar que el rango `baro_upper/lower (90–105 kPa)` cubre tu altitud.

### 18. Trigger noise filter desactivado
- `NoiseFilterOpts = "Off"` (también `NoiseFilterOpts1/2/3 = "Off"`).
- Con los timing errors y lost sync detectados en los logs, activar el filtro de ruido podría mejorar la estabilidad del trigger.
- **Acción:** Probar activar `NoiseFilterOpts` básico. Monitorear si mejora el timing error % en los logs siguientes. Si el motor arranca peor, desactivar.

---

## Resumen de Prioridades

| # | Problema | Severidad | Esfuerzo |
|---|----------|-----------|----------|
| 1 | Knock sensor desactivado | 🔴 Crítico | Alto (hardware) |
| 2 | Lost sync en operación | 🔴 Crítico | Medio (cableado) |
| 7 | Timing error > 5% | 🟠 Moderado | Medio (cableado + config) |
| 3 | IAC inactivo en caliente | 🟠 Moderado | Bajo (config) |
| 4 | Oscilación RPM ralentí | 🟠 Moderado | Bajo (config IAC) |
| 18 | Noise filter apagado | 🟠 Moderado | Bajo (config) |
| 6 | AFR rico en ralentí | 🟡 Menor | Bajo (VE) |
| 12 | Alternador / voltaje | 🟡 Menor | Alto (hardware) |
| 5 | MAT Retard umbral alto | 🟡 Menor | Bajo (config) |
| 9 | Zonas RPM altas sin cubrir | 🟡 Menor | Bajo (más logs) |

---

*Análisis automatizado — verificar observaciones contra comportamiento real del vehículo antes de hacer cambios.*
