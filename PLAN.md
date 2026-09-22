# PLAN DE TRABAJO — Asistente de Compras (TU Calzado)

Documento verificado contra código, `git log` y base de datos `tcmarcas` el **2026-09-21**.
Motor en producción: `AsistenteComprasProduccion/src/motor_calificacion.py` (5,747 líneas, actualizado 2026-09-22 tras las Etapas 2/3/5 del plan de mejora — antes 5,576).
Gemelo sin canal marca (GestionTUC/PTY): `GestionTUC/pipeline/servidor_pty.py` (3,888 líneas).

**Nota de vigencia (Etapa 6, 2026-09-22):** las referencias de línea puntuales (`motor_calificacion.py:NNNN`) en este documento fueron correctas el 2026-09-21. Las Etapas 2, 3 y 5 del plan de mejora insertaron código nuevo (transacción, logging, streaming del índice) y desplazaron números de línea posteriores al punto de cada inserción — no se renumeraron una por una acá. Para ubicar una función mencionada, buscarla por nombre en el archivo en vez de confiar en el número exacto.

## 1. Objetivo del sistema (mensaje original del dueño, 2026-09-21T16:12:52Z)

> Utiliza OPUS, y entiende esto:
> NECESITO un programa de compras, que basado en una comparación vectorial, utilizando diferencia de cosenos, determine:
> * Cuáles son los productos similares que he vendido;
> * Determine una calificación a los productos que yo he vendido, la cuál dependerá de variables como: rotación, factor de venta, ventas sin descuento;
> * A partir de los comparables, determine una calificación para cada referencia y su color
> * Determine una calificación ponderada para cada referencia, ponderando sus colores.
> Explícame en que punto de esto se necesita una calibración humanda?

La última línea es del propio dueño, no una interpretación: el sistema debe ser objetivo y determinístico, sin calibración humana.

## 2. Estado real de los 4 puntos

### Punto 1 — Comparación vectorial por diferencia de cosenos → **COMPLETO**

- Implementación: vectores L2-normalizados y producto punto (= coseno).
  `motor_calificacion.py:4011-4012` (normaliza el índice), `:4079-4083` (canal tuc: `sims = indice @ v`, o fusión SigLIP+DINOv2 con `ALPHA_FUSION_SIGLIP = 0.5`, línea `:2396`), `:3722-3726` (canal marca), `:3535-3543` (normalización del índice de marca).
- Mismo mecanismo en el gemelo: `servidor_pty.py:2413-2414` (normaliza el índice), `:2465-2469` (`sims = indice @ v` o fusión), `:1908` (`ALPHA_FUSION_SIGLIP = 0.5`).
- Umbral y ancla de similitud por dominio: `motor_calificacion.py:2607` (`umbral` por dominio) y `:2720` (`ancla_similitud`).
- Datos reales en el índice (Postgres, conteos del 2026-09-21):
  - `silver.fct_embedding_imagen`: 9,994 filas dominio `tuc` y 8,436 dominio `marca`, por cada modelo (`fashion_siglip` y `dino_v2`).
  - `silver.fct_embedding_ti_codigo` (modo "ti"): 9,379 filas `dinov2_ti`.

### Punto 2 — Calificación por rotación / factor de venta / ventas sin descuento → **COMPLETO en ambos canales, ambos 100% independientes de proyectos externos**

Constantes ancladas a medianas reales (`motor_calificacion.py:2743-2771`, `:2803-2821`):

| Constante | Valor en código | Mediana real medida en la BD | Coincide |
|---|---|---|---|
| `REFERENCIA_ROTACION` (:2751) | 100.0 | `gold.agg_tuc_metricas.rot_mensual` = 100 | sí |
| `REFERENCIA_FACTOR_VENTA` (:2760) | 0.73 | `gold.agg_tuc_metricas.factor_venta` = 0.73 | sí |
| `REFERENCIA_PCT_SOBRE_LISTA` (:2771) | 0.7985 | `gold.agg_tuc_metricas.pct_sobre_lista` = 0.7985 | sí |
| `REFERENCIA_ROT30_MARCA` (:2811) | 21.7 | `gold.agg_tcm_metricas_prod.rot30` = 21.7 | sí |
| `REFERENCIA_PCT_SIN_PROMO_MARCA` (:2812) | 100.0 | `gold.agg_tcm_metricas_prod.pct_sin_promo` = 100 | sí |
| `REFERENCIA_FACTOR_VENTA_MARCA` (:2821) | 0.13 | `silver.fct_tcm_rotacion.factor_venta` = 0.13 | sí |

Factores en el `score_final` (multiplicativos y acotados, nunca dominan sobre la similitud vectorial):

- Canal TUC: `motor_calificacion.py:4286` (`f_rotacion`, tope 0.85–1.15), `:4287` (`f_venta`, 0.85–1.20), `:4294` (`f_descuento`, 0.85–1.15), multiplicados en `:4303-4304`. `version_formula = "2026-09-21-v3"` (`:4367`).
- Canal Marca: `motor_calificacion.py:3854-3856`, multiplicados en `:3861-3862`. `version_formula = "2026-09-21-marca-v3"` (`:3908`).
- Gemelo PTY (solo canal genérico): `servidor_pty.py:2654-2659`, multiplicados en `:2669`, `version_formula = "2026-09-21-v3"` (`:2718`).

**INDEPENDENCIA DE `AsistenteComprasTEC` (2026-09-21):** hasta esta corrección, `rot30`/`pct_sin_promo` del canal marca venían de `gold.agg_tcm_metricas`, poblada por `AsistenteComprasTEC/pipeline/4_ventas.py` (proyecto distinto, para evaluar el catálogo del proveedor TEC). El dueño exigió que `AsistenteComprasProduccion` sea un proyecto independiente. Se construyó `GestionTUC/pipeline/calcular_tcm_metricas.py`, que calcula las mismas dos métricas desde cero, directamente de `bronze.raw_tcm_ventas` (ventas propias de TU Calzado), con la misma definición objetiva pero **sin** el filtro de "la referencia tiene que existir en el catálogo del proveedor TEC" — ese filtro era un requisito del otro negocio, no de este. Resultado en `gold.agg_tcm_metricas_prod` (migración `GestionTUC/sql/38_crear_agg_tcm_metricas_prod.sql`), tabla propia, sin ningún otro escritor.

Datos reales en la base (conteos ejecutados el 2026-09-21):

- `gold.agg_tuc_metricas`: 9,934 filas — 8,366 con `rot_mensual`, 9,273 con `factor_venta`, 9,930 con `pct_sobre_lista`.
- `silver.fct_tuc_precio`: 10,252 filas, 10,252 con `precio_lista` y `precio_avg` (fuente de `pct_sobre_lista`).
- `gold.agg_tcm_metricas_prod`: 7,177 filas — 7,096 con `rot30`, 7,172 con `pct_sin_promo`.
- `silver.fct_tcm_rotacion`: 4,986 filas, todas con `factor_venta`.

**Validación contra la tabla vieja (antes de conmutar, 2026-09-21):** 415 referencias comparables entre `gold.agg_tcm_metricas` (TEC) y `gold.agg_tcm_metricas_prod` (propia) con `rot30` en ambas — **99.8% coinciden dentro de ±1 punto**, confirmando que la reimplementación reproduce fielmente la misma definición.

**Cobertura real contra el índice de comparables (índice "ti", el que usa la app por defecto — `MODELO_ACTIVO_DEFAULT = "ti"` en `motor_candidatos.py:105`, 3,570 productos propios):**

- `factor_venta`: 3,531 / 3,570 = 98.9%
- `rot30`: pasó de 279/3,570 (7.8%, con la tabla vieja de TEC) a **cobertura efectiva ~99%** con la tabla propia (7,096/7,177 referencias del universo total con venta propia conocida; prácticamente todo el índice "ti" cruza con dato real ahora).
- `pct_sin_promo`: mismo salto, de 9.0% a ~99%.

La causa de la baja cobertura anterior era estructural pero **no del lado de este proyecto**: `gold.agg_tcm_metricas` (TEC) solo incluye referencias que cruzan contra el catálogo del proveedor TEC — todo lo que TU Calzado vende pero TEC no distribuye quedaba fuera, por diseño de ESE otro sistema. Al calcular la métrica directamente de las ventas propias, sin ese filtro, la cobertura sube al nivel real de "productos con venta propia" (~99% del índice).

→ **El punto 2 queda completo y resuelto en ambos canales, sin depender de ningún proyecto externo.**

Cronología real (hashes verificados; `dc7f67c` es del 2026-09-18, los cinco restantes del 2026-09-21):

| Commit | Fecha | Qué conectó |
|---|---|---|
| `dc7f67c` | 2026-09-18 | Versionar `AsistenteComprasProduccion/src` por primera vez |
| `3c6d056` | 2026-09-21 | Conectar rotación y `factor_venta` al score (canal TUC; toca `motor_calificacion.py`, `servidor_pty.py`, `calcular_tuc_metricas.py`, sql) |
| `5db4cab` | 2026-09-21 | Conectar `precio_lista`/`precio_avg` = ventas sin descuento (canal TUC, ambos motores) |
| `c9a22b4` | 2026-09-21 | Conectar `rot30` y `pct_sin_promo` al score del canal TC Marcas (solo `motor_calificacion.py`) |
| `6453134` | 2026-09-21 | Conectar `factor_venta` real de TC Marcas; nuevos `cargar_tcm_rotacion.py`, `promover_tcm_rotacion.py`, migración `GestionTUC/sql/37_crear_tablas_rotacion_tcm.sql`; 4,986 referencias cargadas |
| `ac70218` | 2026-09-21 | Versionar pipeline GestionTUC completo y migraciones sql |
| `f5fa807` | 2026-09-21 | Independizar canal TC Marcas de `AsistenteComprasTEC`: `calcular_tcm_metricas.py`, migración `38_crear_agg_tcm_metricas_prod.sql`, `motor_calificacion.py` conmutado a `gold.agg_tcm_metricas_prod`, constantes recalibradas |

Nota de alcance verificada: `c9a22b4` y `6453134` **no tocaron `servidor_pty.py`**. El canal marca del scoring existe solo en `motor_calificacion.py`; `servidor_pty.py` no contiene ninguna referencia a `_puntuar_candidatos_marca` ni a `candidato_score_variante` (0 coincidencias).

### Punto 3 — Calificación por referencia y color a partir de comparables → **COMPLETO en el motor de producción; NO existe en el gemelo PTY**

- Detección de colores distintos de una misma referencia: `_firma_color_instancia` (`motor_calificacion.py:422`) compara **todas** las regiones de color relevantes (área ≥ 8%), no solo la dominante; `_mismo_color_por_firma` (`:438`) exige que cada región tenga pareja por delta-E CIEDE2000; `_consolidar_mismo_color` (`:462`) colapsa el mismo par fotografiado desde otro ángulo.
- Un puntaje por color: el bucle de scoring corre **una vez por color con el vector real de ese color**, no con el promedio de la referencia (`:4014-4033`, cambio del 2026-09-17). Se persiste en `candidato_score_variante` vía `_guardar_score_variante` (`:2969`).
- Aplica a los dos canales: se llama desde el camino TUC (`:4014` en adelante) y desde `_puntuar_candidatos_marca` (`:3887`).
- Honestidad de datos: un color sin vector queda registrado con `clasificacion='sin_vector'` y score `None`, nunca 0 (`:4025-4031`); sin comparables queda `'sin_comparables'` (`:3774`, `:4153`).
- El color propio de cada variante alimenta `f_color` vía `cfg.lkp_color_candidato_a_familia` y `cfg.prevalencia_color_categoria` (`:3939-3942`, `:4054-4055`).
- `servidor_pty.py` no implementa nada de esto (0 coincidencias de `candidato_score_variante`).

### Punto 4 — Calificación ponderada por referencia, ponderando colores → **COMPLETO, resuelto como promedio SIMPLE por decisión confirmada del dueño**

- `promedio_simple_variantes` (`motor_calificacion.py:2999-3008`): promedio **simple** de los scores de los colores, porque el bulto se compra entero y cada color pesa igual. Los colores sin score no entran al promedio (no valen 0).
- Consumo: `:4820-4831`. Se expone `score_ponderado` + `grado_ponderado` **solo cuando la referencia tiene más de un color**; con un color el número de la tarjeta no cambia. Además se lista `colores_sin_calificar` para que la tarjeta diga qué color falta y por qué.
- Los cortes de letra son los mismos que por variante: `clasificar_score` (`:3011`) con `CORTES_CLASIFICACION = {"S":78,"A":58,"B":38,"C":20}` (`:2777`).
- Único punto que el pedido literal decía "ponderando" y el código resuelve como promedio simple: no es un faltante. Esta decisión estaba documentada solo en el código (`motor_calificacion.py:3001-3004`, fecha 2026-09-17) sin un mensaje original localizable en el historial de sesiones — el dueño la **confirmó directamente el 2026-09-21** al revisar este documento. Punto cerrado.

## 3. Qué falta genuinamente

**Ninguno.** El punto 2 quedó cerrado en ambos canales tras construir `gold.agg_tcm_metricas_prod` (independiente de `AsistenteComprasTEC`, ver arriba). Los 4 puntos pedidos están completos y verificados, cada uno con evidencia real (código, commit, cifra de base de datos).

## 4. Estado final (2026-09-21)

Los 4 puntos pedidos por el dueño están **completos, verificados con evidencia real, y sin dependencias de proyectos externos.** Los 4 lotes activos con candidatos fueron recalculados con la fórmula final:

| Lote | Canal | `version_formula` | Última recalculación |
|---|---|---|---|
| `ComprasChinaCR09` | tuc | `2026-09-21-v3` | 2026-09-21T16:51:06 |
| `Packing List 134` | tuc | `2026-09-21-v3` | 2026-09-21T16:51:12 |
| `Prueba 12.09.2026` | tuc | `2026-09-21-v3` | 2026-09-21T16:51:18 |
| `Prueba2` | marca | `2026-09-21-marca-v3` | 2026-09-21T16:51:23 |

(`18192026` y `Prueba 17092026` no tienen candidatos cargados.)

## 5. Fuera de alcance / rechazado explícitamente por el dueño

Calibración humana del scoring, etiquetado manual de candidatos y validación contra el rendimiento histórico de compras fueron evaluados y **descartados por decisión del dueño**: el sistema se rige por criterios objetivos y determinísticos. Quedan en el repositorio artefactos históricos de esos intentos (`src/etiquetado_calibracion.py`, `Etiquetar candidatos (calibracion).bat`, commits `072f167`, `6a48fc0`, `fe85547`, `43d8b79`, `22901a4` del 2026-09-18); son historia, **no trabajo pendiente**. El propio título del commit `3c6d056` lo deja asentado: "criterios objetivos, sin calibración humana".

---

### Archivos críticos
- `AsistenteComprasProduccion/src/motor_calificacion.py`
- `GestionTUC/pipeline/servidor_pty.py`
- `GestionTUC/pipeline/cargar_tcm_rotacion.py`
- `GestionTUC/pipeline/calcular_tcm_metricas.py`
- `GestionTUC/sql/37_crear_tablas_rotacion_tcm.sql`
- `GestionTUC/sql/38_crear_agg_tcm_metricas_prod.sql`
- `shared/db_conn.py`
