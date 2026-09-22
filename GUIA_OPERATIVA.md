# Guía operativa — Asistente de Compras (uso diario)

Escrita el 2026-09-22 (plan de mejora, Etapa 6) directamente contra el código
de `src/gui_profesional_ctk.py` — cada paso descrito acá corresponde a un
botón, pantalla o validación que existe hoy en la app, no a una intención de
diseño. Donde el código no confirma algo con certeza, se dice explícitamente
en vez de inventarlo (ver "Puntos sin confirmar" al final).

No reemplaza al [README.md](README.md) (que documenta límites conocidos de
la limpieza de imagen) ni al [PLAN.md](PLAN.md) (que documenta el pedido
original del dueño) — esta guía es solo "qué botón toco y en qué orden".

## Cómo abrir la app

Doble clic en `Abrir AsistenteCompras.lnk` (raíz del proyecto).

Al abrir, si hay un lote sin terminar de una sesión anterior, aparece un
diálogo **"¿Qué querés hacer?"** con la lista de proyectos guardados. Desde
ahí:
- **"＋ Nuevo proyecto"** — empieza un lote nuevo.
- **"Retomar"** (junto a un proyecto de la lista) — continúa exactamente
  donde quedó, con todas las decisiones (aprobado/descartado, canal de
  venta, costos) ya guardadas en el `lote.sqlite` de esa carpeta.

## El flujo: 7 pasos, en orden fijo

La app **no tiene pestañas libres** — es un asistente de un solo camino, con
un botón "Atrás" y un botón principal que avanza al siguiente paso (su texto
cambia: "Siguiente →" en el paso 1, "Continuar →" en los pasos 2 a 4). No se
puede saltar pasos.

### Paso 1 — Elegir proveedor y origen

Antes de avanzar hay que completar **tres cosas**, todas obligatorias:

1. **Proveedor** del catálogo que se va a revisar.
2. **Origen de las fotos** (botón "Elegir…").
3. **Canal de venta: "TC Marcas" o "TU Calzado".**

**El canal de venta es la decisión más importante del lote entero.** No
viene preseleccionado a propósito — hay que elegirlo activamente, porque
determina contra qué catálogo se compara cada candidato y con qué datos de
rotación/precio se califica. Si se olvida, la app no deja avanzar y explica
por qué. **Se elige una sola vez por lote** y no se puede cambiar después
sin volver a empezar.

### Paso 2 — Elegir qué limpiar

Se descartan las fotos que no son calzado (portadas del catálogo, logos,
etc.) antes de gastar tiempo de cómputo limpiándolas.

### Paso 3 — Limpiar y revisar

Botón **"▶ Limpiar fotos"** dispara el recorte/limpieza automática de fondo.
No hay que esperar a que termine el lote completo — a medida que cada foto
sale, ya se puede revisar y decidir sobre ella.

Sobre cada foto/candidato, las acciones disponibles son:

| Botón | Qué hace |
|---|---|
| **✓ Aprobar** | marca el candidato como aprobado. Reversible. |
| **✗ Descartar** | marca el candidato como descartado. **Reversible** — solo lo saca de la vista, no borra nada. |
| **🗑 Desechar definitivamente** | **borra el archivo del disco. NO se puede deshacer.** No confundir con "Descartar". |
| **🔧 Reparar suela** / **✂ Recortar sombra** | correcciones automáticas puntuales sobre esa imagen. |
| **↺ Revertir correcciones** / **Deshacer limpieza automática** | vuelve una imagen a su estado anterior. |

También existen las mismas acciones en **lote** (multi-selección): aprobar,
descartar, desechar, reparar suela y recortar sombra para varias fotos a la
vez.

**Regla práctica: "Descartar" es seguro de usar sin pensarlo dos veces.
"Desechar definitivamente" no — es borrado real.**

### Paso 4 — Confirmar y enviar

Pantalla de resumen: cuántos candidatos quedaron aprobados, descartados y
pendientes. Botón **"Exportar aprobados…"** envía solo los aprobados al
siguiente paso.

### Paso 5 — Vectorizar y comparar

Se elige el método de vectorización y se presiona **"▶ Calcular y
continuar"**. Este es el botón que **dispara el cálculo real de scores**
(`puntuar_candidatos`) — el recálculo **no es automático en ningún otro
momento** del flujo normal; solo ocurre acá, una vez por lote (salvo la
excepción de la nota siguiente).

Corre en segundo plano; al terminar, la app abre sola el Paso 6.

> Nota técnica: si se restauran categorías desde un respaldo, la app
> recalcula automáticamente porque el score depende de categoría/género del
> candidato. Es la única excepción al "solo se recalcula acá".

### Paso 6 — Candidatos calificados

Lista de candidatos ya puntuados, cada uno con su **grado: S, A, B, C, D**
(verde para S/A, rojo para C/D), o **"sin_comparables"** cuando no se
encontró nada parecido en el catálogo contra el que se comparó.

- Botón **"¿por qué? ↑"** sobre la etiqueta de grado — abre el desglose del
  cálculo de ese candidato en particular.
- **"👁 Ver comparables"** — muestra contra qué productos reales se comparó.
- **"🗑 Eliminar"** — saca el candidato de la lista.
- Si el lote tiene más de un proveedor, hay una barra de filtro por
  proveedor.
- Botón **"💾 Guardar lote"** — vuelca a `lote.sqlite` cualquier corrección
  de tipo/género/costo hecha en esta pantalla.

### Paso 7 — Sugerido de compra

- **"Generar selección"** — corre el optimizador de compra sobre los
  candidatos aprobados.
- **"📊 Exportar a Excel"** — queda deshabilitado hasta que se generó una
  selección. Al exportar, la app ofrece abrir la carpeta o el Excel
  directamente.

## Cerrar un lote

La app no tiene un botón literal de "cerrar lote". El ciclo de trabajo
termina exportando: **"Exportar aprobados…"** (paso 4) y/o **"📊 Exportar a
Excel"** del sugerido de compra (paso 7). Todo lo demás queda guardado en
`lote.sqlite` para poder retomarlo después.

Si un proyecto ya no sirve, existe **"Eliminar proyecto"** en la pantalla de
"¿Qué querés hacer?" — borra la carpeta completa (fotos, decisiones, costos)
y **no se puede deshacer**.

## Resumen de qué es reversible y qué no

| Acción | ¿Se puede deshacer? |
|---|---|
| Descartar candidato | Sí |
| Aprobar candidato | Sí |
| Eliminar candidato calificado (paso 6) | Sí (solo sale de la lista) |
| Reparar suela / recortar sombra | Sí ("Revertir correcciones") |
| **Desechar definitivamente** | **No — borra el archivo** |
| **Eliminar proyecto** | **No — borra la carpeta completa** |

## Puntos sin confirmar en el código (no incluidos arriba)

Estos dos puntos no se documentan como pasos porque no se pudo confirmar con
certeza contra el código — se anota acá para no inventarlos y para que quien
los necesite sepa dónde buscar:

- El botón/mecanismo exacto para editar el color de un candidato ya
  calificado (existe una función de asignar categoría que sí recalcula el
  score, pero no se confirmó el texto de botón visible para "editar color"
  específicamente).
- Si la pantalla de candidatos (paso 6) tiene un selector de "ordenar por"
  además del orden nativo por calificación.
