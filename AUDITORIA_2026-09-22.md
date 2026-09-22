# Auditoría — Asistente de Compras (TU Calzado)

Verificado el 2026-09-22 contra código real, `git` real y consultas ejecutadas contra `tcmarcas`. No incluye ninguna acción de calibración humana, etiquetado manual ni validación contra historial de compras (rechazado explícitamente por el dueño en sesiones anteriores).

---

## HALLAZGOS DE AUDITORÍA

### CRÍTICO

**C1. La contraseña de Postgres está en un repositorio de GitHub, ya empujado.**

El repo tiene remoto real: `origin https://github.com/ogodinez-tucalzadola/Proyectos-TUCalzado.git`, y todo lo local ya está subido (`0 0` de diferencia con `origin/main`). Hoy responde 404 sin autenticar (privado).

Archivos versionados con la contraseña en texto plano:
- `shared/db_conn.py:19`
- `AsistenteComprasProduccion/src/motor_calificacion.py:605`
- `GestionTUC/pipeline/servidor_pty.py:590`
- 14 archivos más solo dentro de estos dos proyectos (17 coincidencias totales de `"tcmarcas2025!"`), y 60+ en el resto del monorepo.

También hay credenciales de **superusuario** (`postgres/postgres123`) hardcodeadas en varios scripts.

Riesgo real: Postgres solo escucha en `127.0.0.1`, pero el secreto ya salió de la máquina. Cualquiera con acceso de lectura al repo (colaborador presente o futuro, token filtrado, cambio accidental a público) obtiene ambas contraseñas. Cambiar el archivo hoy no borra el historial — sigue en los 106 commits.

**C2. Todo `AsistenteComprasProduccion` y `GestionTUC` viven en un monorepo de 13 proyectos, con 900 archivos sucios.**

- `git status --short` desde `AsistenteComprasProduccion` lista 900 archivos modificados, de los cuales solo 2 son de este proyecto. Los otros 898 son de otros 6 proyectos ajenos.
- El `.git` pesa 130 MB con solo 106 commits, porque tiene una instalación completa de DBeaver versionada: 1,001 de los 1,752 archivos del repo (57%) son de ese programa de terceros.
- Hay un repo git anidado en `ComparadorPrecios/.git` (ni submódulo ni ignorado).

**C3. `AsistenteComprasLotes/` (datos de trabajo reales) no está en `.gitignore`.**

108 MB, 355 archivos, `git status` los muestra como sin trackear. Un `git add -A` descuidado los mete al historial de forma irreversible sin reescribir historia.

### IMPORTANTE

**I1. Cero pruebas automatizadas.** Verificado con `find`/`grep`: 0 archivos `test_*.py`, 0 imports de pytest/unittest, 0 `requirements.txt`. Ni una prueba para `motor_calificacion.py` (5,585 líneas), `almacen.py`, `servidor_pty.py` ni `motor_candidatos.py`.

**I2. Un fallo a mitad de `puntuar_candidatos()` borra los puntajes, no los deja viejos.** `_limpiar_scores_variante()` (línea 4028) borra los scores de todos los candidatos **antes** de recalcularlos, sin transacción ni try/except alrededor del bucle. Si Postgres se cae o el usuario cierra la app a mitad, el lote queda con candidatos sin score y sin aviso.

**I3. No hay logging.** Solo 8 `print()` y 8 `except` en 5,585 líneas. Casos concretos de fallo silencioso:
- Si vectorizar un candidato revienta, se hace `print()` y se sigue — para el comprador se ve idéntico a "sin comparables".
- Si el `lote.sqlite` de un lote de marca se corrompe, `canal_venta_lote()` cae a `"tuc"` en silencio — se puntúa contra el catálogo equivocado sin ningún aviso.

**I4. 21 segundos y 640 MB de pico por cada llamada a `puntuar_candidatos()`. Medido, no estimado** (con `tracemalloc` real). El problema no es numpy (69 MB están bien) — es que `fetchall()` materializa 9,674 filas como listas de floats de Python (570 MB) antes de convertirlas. A ~25,000 filas el pico pasa de 1.5 GB; a ~35,000 roza 2 GB (riesgo de `MemoryError`). Hoy hay margen, pero duplicar el catálogo lo acerca a la zona de riesgo.

**I5. El gemelo `servidor_pty.py` está atrasado tres funcionalidades completas — y estampa la MISMA etiqueta de versión.** No tiene canal marca, no puntúa por color, no tiene calificación ponderada por referencia (0 coincidencias de `canal_venta`, `candidato_score_variante`, `promedio_simple_variantes`). Pero `servidor_pty.py:2718` escribe `version_formula = "2026-09-21-v3"` — exactamente la misma etiqueta que usa el motor real, que SÍ calcula distinto (una vez por color vs. una vez por candidato con vector promedio). Mirando la base no hay forma de saber cuál motor produjo un score.

**I6. No existe guía operativa para el comprador.** El `README.md` es sobre límites de reparación de imagen; el `PLAN.md` es sobre el pedido original. Nada explica cómo recalcular scores, qué significan los grados, o qué hacer ante `sin_comparables`.

**I7. No hay ningún mecanismo automático para refrescar las métricas base — y ya están desfasadas.** `calcular_tuc_metricas.py`/`calcular_tcm_metricas.py` (los scripts que alimentan el score) son los únicos del pipeline sin lanzador `.bat`, y no hay ninguna tarea programada (`schtasks` = 0 tareas). Las métricas se recalcularon ayer, pero **sobre ventas que solo llegan hasta julio 2026** — agosto y septiembre no están. La app nunca avisa de este desfase.

### MENOR

- **M1.** 3 funciones muertas en `motor_calificacion.py` (sin ningún llamador real), residuo del fork web→escritorio.
- **M2.** Funciones de 285-457 líneas (`puntuar_candidatos`, `_puntuar_candidatos_marca`) con demasiada responsabilidad cada una.
- **M3.** Varios valores mágicos sin fuente documentada — notablemente `CORTES_CLASIFICACION` (los cortes S/A/B/C/D que el comprador realmente lee) no tiene respaldo empírico citado en el código.
- **M4.** Los números de línea de `PLAN.md` ya no coinciden con el archivo real (desfase de ~9 líneas).
- **M5.** Varios artefactos sueltos (HTML de 72 MB, scripts de depuración de julio, lanzadores sin trackear).

---

## PLAN DE MEJORA — PASO A PASO

### Prioridad 1 — Contener la fuga de credenciales (C1)
1.1 Confirmar con el dueño quién tiene acceso al repo de GitHub 🔴 *decisión del dueño*
1.3 Sacar la contraseña de los 17 archivos, usando `shared/db_conn.get_conn()` ya existente 🟡 *reversible, probar por lote*
1.4 Que `shared/db_conn.py` lea la contraseña de una variable de entorno, no del código 🟡 *reversible*
1.2 Rotar la contraseña de `tcm_etl` (y revisar `postgres123`) — **después** de 1.3/1.4 🔴 *requiere ventana de mantenimiento*

*Nota: no propongo reescribir el historial de git (`filter-repo`/BFG) — el beneficio es marginal comparado con rotar la contraseña, que invalida el secreto filtrado sin riesgo de perder historia.*

### Prioridad 2 — Proteger los datos de trabajo (C3)
2.1 Agregar `AsistenteComprasLotes/` al `.gitignore` 🟢 *seguro*
2.2 Reemplazar las excepciones puntuales de `GestionTUC` por patrones (`_check_*`, `_z*`, etc.) 🟢 *seguro*

### Prioridad 3 — Que un fallo no borre los puntajes (I2, I3)
3.1 Envolver el bucle de `puntuar_candidatos()` en una transacción explícita (el patrón ya existe en el mismo archivo, línea 2213) 🟡 *verificar contra un lote de prueba*
3.2 Que el fallo por candidato deje rastro visible, no un `print` 🟡 *reversible*
3.3 Introducir `logging` a archivo rotativo, convertir los 8 `print`/`except` 🟡 *reversible*

### Prioridad 4 — Que las métricas no envejezcan en silencio (I7)
4.1 Crear los 2 `.bat` que faltan (`_calcular_metricas_tuc.bat`, `_calcular_metricas_tcm.bat`) 🟢 *seguro*
4.2 Cargar ventas de agosto/septiembre y recalcular — mueve los grados de los 38 candidatos 🟡 *confirmar con el dueño antes*
4.3 Mostrar la edad del dato en la app (aviso si pasaron más de 45 días) 🟡 *reversible*
4.4 (Opcional) Tarea programada mensual 🔴 *decisión del dueño*

### Prioridad 5 — Cerrar la divergencia con el gemelo (I5)
5.1 Diferenciar la etiqueta de versión (`servidor_pty.py:2718` → `"2026-09-21-pty-v3"`) 🟢 *una línea, alto valor forense*
5.2 Documentar la divergencia en el propio docstring del gemelo 🟢 *seguro*
5.3 Blindar el acceso al umbral por modelo (evitar `KeyError`) 🟢 *seguro*
5.4 Decidir el destino del gemelo (congelarlo / portarle funciones / extraer módulo compartido) 🔴 *decisión del dueño, no técnica*

### Prioridad 6 — Hacer el repo manejable (C2)
6.1 Sacar `DBeaver/` del versionado (`git rm --cached`, no borra del disco) 🟡 *reversible*
6.2 Ordenar los 900 archivos sucios de otros proyectos, uno por uno con el dueño 🔴
6.3 Resolver el repo anidado `ComparadorPrecios/.git` 🟡

### Prioridad 7 — Red de seguridad mínima (I1)
7.1 Congelar dependencias (`pip freeze > requirements.txt`) 🟢 *seguro*
7.2 Pruebas sobre funciones puras (`clasificar_score`, `promedio_simple_variantes`, etc.) 🟢 *seguro, no toca el motor*
7.3 Extraer el cálculo del score a una función pura y probarla — ataca la función de 457 líneas 🟡 *refactor del camino crítico, requiere confirmación*

### Prioridad 8 — Rendimiento (I4)
8.1 Cachear el índice en memoria entre llamadas 🟡 *reversible*
8.2 Eliminar el pico de 570 MB con lectura por bloques 🟡 *reversible, verificar scores idénticos antes/después*
8.3 (Diferible) Índice vectorial `pgvector` si el catálogo se acerca a 25,000 filas — no hace falta hoy

### Prioridad 9 — Limpieza y documentación
9.1 Guía operativa para el comprador (`GUIA_OPERATIVA.md`) 🟢 *seguro*
9.2 Borrar el código muerto verificado (no tocar `servidor_pty.py`, ahí sigue en uso) 🟢 *seguro*
9.3 Documentar los valores mágicos que quedan 🟢 *seguro*
9.4 Actualizar los números de línea de `PLAN.md` 🟢 *seguro*
9.5 Archivar restos de migración/depuración 🟢 *seguro*

---

## Secuencia recomendada

1. **Hoy, sin consultar:** 2.1, 2.2, 5.1, 7.1, 9.2, 9.4
2. **Esta semana, con verificación:** 1.3, 1.4, 3.1, 3.2, 4.1, 5.2, 5.3, 7.2, 9.1
3. **Con confirmación explícita del dueño:** 1.1→1.2 (rotar contraseña), 4.2 (recálculo que mueve grados), 5.4 (destino del gemelo), 6.2, 7.3, 8.1/8.2
