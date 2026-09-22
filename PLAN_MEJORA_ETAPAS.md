# Plan de mejora por etapas — Asistente de Compras (TU Calzado)

Consolidado con Opus el 2026-09-22 a partir de dos auditorías previas, con verificación adicional de cifras que discrepaban entre ellas. No incluye calibración humana, etiquetado manual ni validación contra historial de compras (descartado explícitamente por el dueño).

## 0. Corrección a las auditorías previas

Antes de armar el plan se verificaron contra el código real las cifras que discrepaban entre los dos reportes anteriores:

- **`f_rotacion` no es una sola fórmula — son dos, independientes:** `motor_calificacion.py:3863` (canal marca, contra `REFERENCIA_ROT30_MARCA`) y `:4295` (canal estándar, contra `REFERENCIA_ROTACION`). Cualquier cambio a esa lógica tiene que tocar los dos sitios o quedan divergentes.
- **`_limpiar_scores_variante` (el borrado destructivo) tiene 2 llamadores, no 1:** `:3688` y `:4028`. La mitigación transaccional (Etapa 2) debe cubrir ambas rutas.
- **Total de líneas confirmado:** `motor_calificacion.py` = 5,585 (no 5,576 como dice `PLAN.md`, que quedó desactualizado). `servidor_pty.py` = 3,888.
- Confirmado también: 17 archivos con la contraseña hardcodeada, `CORTES_CLASIFICACION` en `:2777`, el `print()` de fallo de vectorización en `:2218-2219`, cero `import logging` en todo el archivo, y la colisión real de `version_formula` entre `servidor_pty.py:2718` y `motor_calificacion.py:4376` (ambos escriben `"2026-09-21-v3"`, pero calculan distinto).

---

## Etapa 1 — Contención de riesgo inmediato
**Objetivo:** detener el daño que se agrava solo con que pase el tiempo.

**CERRADA (2026-09-22):**
- ✅ `AsistenteComprasLotes/` agregado al `.gitignore` (commit `c24f5c9`) — 108 MB de datos reales ya no rastreables.
- ✅ Evaluación de compromiso: el dueño confirmó que el repo fue **siempre privado** y **solo él tuvo acceso** — no se considera comprometido.
- ❌ **Rotación de contraseña / migración a variables de entorno: descartada explícitamente por el dueño.** No se toca la credencial actual. Si en el futuro cambia el acceso al repo (nuevo colaborador, se vuelve público), reevaluar.

---

## Etapa 2 — Integridad de datos
**Objetivo:** que ningún fallo a mitad de camino deje la base peor que antes de empezar.

- Envolver `puntuar_candidatos()` en una transacción explícita que cubra `_limpiar_scores_variante` + recálculo, en **ambos** llamadores (`:3688` y `:4028`)
- Crear el lanzador que falta para `calcular_tuc_metricas.py`/`calcular_tcm_metricas.py` (hoy no hay `.bat` ni tarea programada — 0 confirmado con `schtasks`)
- Agregar verificación de frescura: avisar cuando el período de ventas se aleje demasiado del recálculo de métricas (hoy las métricas son del 21-sep pero las ventas llegan solo a julio)

**Permiso:** la transacción se puede hacer sin consulta. Programar una tarea recurrente y fijar el umbral de desfase requieren confirmación del dueño.

---

## Etapa 3 — Trazabilidad y consistencia
**Objetivo:** saber, mirando una fila de la base, qué motor la produjo y qué pasó cuando algo falló.

- Cambiar la etiqueta del gemelo (`servidor_pty.py:2718`) a un valor distinguible (ej. `"2026-09-21-pty-v3"`) para que deje de colisionar con la del motor real
- Decidir qué hacer con las filas ya escritas bajo la etiqueta ambigua (recalcular o marcar como origen desconocido)
- Introducir `logging` en `motor_calificacion.py`, reemplazando el `print()` de `:2218-2219` que hoy nadie ve en la GUI de escritorio
- Dirigir el log a archivo, para que los fallos queden registrados después de cerrar la app

**Permiso:** el cambio de etiqueta y el logging se pueden hacer sin consulta. El destino de las filas históricas ambiguas requiere decisión del dueño.

---

## Etapa 4 — Red de seguridad
**Objetivo:** tener cómo demostrar que un cambio no alteró los puntajes.

- Crear `requirements.txt` en ambos proyectos (hoy no existe en ninguno)
- Primeros tests (hoy hay 0): un test de caracterización que fije el `score_final` de un lote conocido, y tests de las dos `f_rotacion` por separado (usan constantes distintas)
- Un test que confirme que un fallo a mitad de `puntuar_candidatos()` deja los puntajes previos intactos (valida la Etapa 2)

**Permiso:** sin consulta.

---

## Etapa 5 — Rendimiento
**Objetivo:** bajar los 21 s y 640 MB de pico medidos por carga del índice de comparables.

**CERRADA (2026-09-22):**
- ✅ Confirmado con dos benchmarks reales contra la base (`tracemalloc`, consulta real de `silver.fct_embedding_imagen`/`gold.agg_tuc_metricas`, 9,795 filas): un cursor normal con `fetchall()` duplica en memoria del cliente todo el resultado; un cursor servidor (nombrado, `itersize`) + conversión a numpy por lote lo evita — 617 MB → 258 MB de pico (-58%).
- ✅ Escrito el helper reusable `_consumir_por_lotes()` en `motor_calificacion.py` y aplicado a los 4 sitios reales que cargan el índice de comparables: `_puntuar_candidatos_impl` (ramas ti/estándar, canal TUC) e `_indice_marca_para_score` (ramas ti/estándar, canal TC Marcas).
- ✅ Medido en producción contra el lote real `Prueba2` (canal marca, 18 candidatos): **654.9 MB → 281.6 MB de pico (-57%)**, tiempo sin cambio significativo (~21 s — el cuello de botella de tiempo es la vectorización/consulta en sí, no la materialización en memoria).
- ✅ Los 17 tests de la Etapa 4 pasan, incluidas las dos pruebas de caracterización — los `score_final` quedan byte-idénticos a antes del cambio.

**Permiso:** sin consulta, **pero condicionado a que la Etapa 4 esté hecha primero** — sin tests no hay forma de distinguir "más rápido" de "más rápido y distinto".

---

## Etapa 6 — Documentación y limpieza
**Objetivo:** que el comprador pueda operar el sistema sin preguntar, y que los documentos no mientan.

**CERRADA (2026-09-22):**
- ✅ Escrita [`GUIA_OPERATIVA.md`](GUIA_OPERATIVA.md) — flujo de los 7 pasos del asistente, canal de venta, qué es reversible y qué no, verificado contra `src/gui_profesional_ctk.py` línea por línea (no existía ninguna guía de este tipo antes).
- ✅ Corregido el total de líneas del motor en `PLAN.md`: decía 5,576 (ya desactualizado desde antes de esta sesión); el real hoy es **5,747** (creció por las Etapas 2/3/5). Se agregó nota de vigencia advirtiendo que las referencias de línea puntuales del documento son del 2026-09-21 y pueden haberse desplazado.
- ✅ Documentado `CORTES_CLASIFICACION` (`motor_calificacion.py:2808`) con nota de transparencia in situ: es un umbral de criterio de negocio, no calibrado contra ventas ni historial de compras.
- ✅ Identificadas con evidencia real (grep de llamadores, cruce contra el puente `motor_candidatos.py`) las funciones de `motor_calificacion.py` sin ningún llamador del lado de escritorio pero activas en `servidor_pty.py`:
  - `modelo_activo_del_catalogo` (`:1215`) — la GUI usa su propio equivalente `motor_candidatos.modelo_activo_configurado()`.
  - `api_variantes` (`:5079`) — sin uso en la GUI; `servidor_pty.py` la expone en `/api/variantes`.
  - `api_margen_global` (`:5234`) — sin uso en la GUI; `servidor_pty.py` la expone en `/api/candidatos`.
  - Nota honesta: el mismo criterio técnico (cero llamadores en escritorio + activa en `servidor_pty.py`) también aplica a `procesar_catalogo` (`:907`), `api_pendientes` (`:1133`) y `api_confirmar_pendiente` (`:1155`) — pero esas tres son parte de un subsistema completo (ingesta OCR de catálogo de proveedor + cola "pendientes"/`staging_tuc`) que **no existe en absoluto** del lado de escritorio, no funciones equivalentes-pero-huérfanas. No se borra ninguna de las 6 — todas siguen en uso real en `servidor_pty.py`.

**Permiso:** sin consulta.

---

## Etapa 7 — Decisiones estructurales de largo plazo
**Objetivo:** resolver las condiciones de fondo que generan estos problemas de forma repetida.

Tres decisiones tomadas por el dueño el 2026-09-22:

### 7.1 — Rotación de contraseña de Postgres: **descartada, sigue en pie**
Confirmado de nuevo tras saber que son 156 archivos en el historial (no 17
como se creía en la Etapa 1) — no cambia la decisión. Sin rotación, la purga
de historial de git no tiene sentido (la contraseña filtrada seguiría siendo
válida), así que **la purga de historial queda descartada también**, no solo
pospuesta.

### 7.2 — Separar el monorepo: **CERRADO para este proyecto (2026-09-22)**
- ✅ DBeaver vendorizado (1,001 de 1,752 archivos versionados, 159 MB) sacado
  del control de versiones con `git rm -r --cached DBeaver` + entrada en
  `.gitignore` (commit `b67b9a8`). Los binarios siguen en disco intactos —
  solo dejan de rastrearse hacia adelante. No purga el historial (ver 7.1:
  descartado).
- ✅ **`AsistenteComprasProduccion` ya tiene su propio repo git**, separado
  del monorepo de 13 proyectos. `git rm -r --cached AsistenteComprasProduccion`
  + entrada en `.gitignore` del monorepo raíz (commit `547aa65`), y `git init`
  dentro de esta misma carpeta con un commit inicial que arranca del estado
  actual (33 archivos, sin conservar el historial del monorepo — decisión
  explícita del dueño, evita arrastrar commits viejos con la contraseña de
  Postgres expuesta). La carpeta **no se movió de lugar**, a propósito:
  `motor_calificacion.py`/`motor_candidatos.py` importan módulos de
  `GestionTUC/pipeline` por ruta absoluta (OCR de catálogos de proveedor,
  ver sus propios docstrings) — moverla habría roto esa dependencia real.
  `.gitignore` propio creado (antes heredaba del raíz). Verificado: los 17
  tests siguen pasando desde el repo nuevo.
- ✅ **Respaldo remoto resuelto (2026-09-22):** repo privado creado por el
  dueño en `github.com/ogodinez-tucalzadola/AsistenteComprasProduccion`,
  `git push -u origin main` exitoso. El "riesgo crítico" de la auditoría
  original (11,134 líneas sin ningún respaldo versionado) queda cerrado.
- ⏸ Separar los otros 12 proyectos del monorepo (GestionTUC,
  AsistenteComprasTEC, etc.) en repos independientes y limpiar los ~900
  archivos sucios ajenos: **fuera de alcance** — el dueño acotó esta tarea
  explícitamente a solo `AsistenteComprasProduccion` (el resto del monorepo,
  incluido el módulo PTY de GestionTUC, no es el proyecto activo). No se
  retoma por iniciativa propia.

### 7.3 — Gemelo `servidor_pty.py`: reescritura completa, **plan de migración documentado, NO ejecutado**

**Decisión del dueño:** converger de verdad, migrando `servidor_pty.py` al
mismo modelo de datos y scoring que `motor_calificacion.py` — no solo
sincronizar fórmulas. Es un proyecto grande que se planifica acá pero se
ejecuta en una sesión aparte, con el servidor fuera de producción durante la
migración (hoy expone `/api/*` en 127.0.0.1:8900, en uso activo).

**Hallazgo previo, importante para dimensionar el trabajo:** las fórmulas de
scoring del canal TUC **ya son idénticas** entre ambos archivos — mismos
coeficientes, mismos clamps, mismas constantes de referencia (verificado
línea por línea, `servidor_pty.py:2584-2670` vs
`motor_calificacion.py:4411-4498`). El trabajo real NO es sincronizar
aritmética — es cerrar tres brechas de infraestructura:

1. **Modelo de persistencia incompatible.** `servidor_pty.py` vive sobre
   Postgres (`staging_tuc.dim_candidato`, `dim_candidato_variante`,
   `agg_candidato_score`, `fct_embedding_candidato`, `raw_catalogo` — un
   catálogo activo global, truncado al cargar uno nuevo). El motor real
   abandonó ese modelo en su Fase 2: puntúa contra `lote.sqlite` por
   carpeta, vía `fijar_lote()`/`_cl()`, y **falla con `RuntimeError` si no
   hay un lote fijado** (`motor_calificacion.py:795-798`). No son
   compatibles sin reescribir la capa de acceso a datos completa.
2. **Falta el canal TC Marcas entero** en `servidor_pty.py` — no es que
   calcule distinto, es que no existe (`_indice_marca_para_score`,
   `canal_venta_lote`, y las tablas `silver.dim_tcm_producto`/`fct_tcm_*`
   no se usan ahí).
3. **Granularidad distinta.** El motor real puntúa por variante/color
   (`variantes_a_calificar`, `_guardar_score_variante`,
   `promedio_simple_variantes`); `servidor_pty.py` escribe una sola fila por
   candidato. Los esquemas de salida (y el JSON que consume el HTML del
   Asistente de Compras PTY) no son intercambiables sin cambiar también el
   front-end.

**Alcance estimado:** 1,200-1,500 líneas a reescribir (~35-40% del archivo,
3,894 líneas), concentradas en `puntuar_candidatos`,
`vectorizar_y_puntuar_candidatos`, `api_candidatos`, `api_variantes`,
`api_desglose_score`, `_promover_candidato`, `_graduar_candidatos_aprobados`,
`_vector_representativo`/`_vector_variante` y sus derivadas (~40 funciones
que hoy reciben `cur` de Postgres directo y en el motor real no).

**Lo que NO hay que tocar** (confirmado, para no gastar esfuerzo de más): el
índice de comparables kNN es idéntico en ambos — misma consulta, mismas
tablas (`silver.fct_embedding_imagen` dominio='tuc', `gold.agg_tuc_metricas`),
mismo modelo de vectorización (fashion_siglip + dino_v2), mismos cortes de
clasificación. El bloque de visión/ML (~1,400 líneas, detección de pares,
color, limpieza de fondo) también es prácticamente idéntico letra por letra
y no depende de `staging_tuc` — es la parte más fácil de migrar primero.

**Pendiente de confirmar antes de empezar la migración:** si el HTML del
Asistente de Compras PTY (o algún otro cliente) depende de la forma exacta
del JSON que hoy devuelve `api_desglose_score`/`api_candidatos` de
`servidor_pty.py` — eso condiciona cuánto se puede cambiar la granularidad
de salida sin romper el front-end.

**Permiso:** ejecutar esta migración requiere sesión dedicada y ventana sin
uso activo del servidor PTY — no se hace sin avisar antes de empezar.

---

## Orden de ejecución

```
Etapa 1 (contención)       → arranca ya, sola (.gitignore primero)

Etapa 2 (integridad)       → independiente, puede ir en paralelo con la 3
Etapa 3 (trazabilidad)     → independiente, en paralelo con la 2

Etapa 4 (red de seguridad) → PREREQUISITO de la 5
Etapa 5 (rendimiento)      → NO empezar sin la 4 cerrada

Etapa 6 (documentación)    → después de 2, 3 y 5 (documenta el estado final)

Etapa 7 (estructural)      → la purga de historial DESPUÉS de la Etapa 1
                              (rotar primero, purgar después — purgar sin
                               rotar no sirve de nada)
```

**Restricciones no negociables:** la Etapa 5 no puede preceder a la 4 (optimizar sin test de caracterización es cambiar puntajes a ciegas). La purga de historial de la Etapa 7 no puede preceder a la rotación de la Etapa 1 (el valor filtrado sigue siendo válido hasta que se rote).

**Advertencia sobre la Etapa 1:** rotar la contraseña y migrar 17 archivos es un cambio que rompe todo lo que esté corriendo hasta estar completo. Conviene hacerlo en una ventana sin pipelines activos, y verificar primero que `shared/db_conn.py` sea realmente el punto central antes de tocar los 16 restantes uno por uno — algunos podrían estar conectando por su cuenta.
