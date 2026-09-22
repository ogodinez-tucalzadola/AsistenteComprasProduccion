# Asistente de Compras — app de escritorio (producción)

## Qué hace

Toma las láminas de catálogo de un proveedor —donde vienen varios calzados en
una sola foto— y entrega una imagen limpia por calzado (fondo blanco o
transparente, recortada, reparada donde hace falta), lista para comparar
contra el catálogo de TU Calzado por similitud vectorial y calificar como
candidato de compra.

## Cómo se ejecuta

Doble clic en `Abrir AsistenteCompras.lnk` (raíz de esta carpeta) — abre
`src\gui_profesional_ctk.py` con el intérprete de `Zawa\.venv`. Cada corrida
trabaja sobre una carpeta de lote (`lote.sqlite` + imágenes), normalmente bajo
`AsistenteComprasLotes\<nombre del lote>\`.

`Etiquetar candidatos (calibracion).bat` abre `src\etiquetado_calibracion.py`
para revisar candidatos ya calificados de un lote puntual.

## Limitaciones conocidas de reparación de imagen

Esta sección documenta límites **aceptados** del pipeline de limpieza de
imagen (`limpiar_fondo.py`, `reparar.py`, `reparar_sombra.py`,
`comparar_colores.py`). No son bugs pendientes de arreglar — son casos donde
ya se probaron las alternativas razonables y ninguna dio un resultado
confiable. El objetivo de esta lista es que nadie vuelva a intentar
"solucionarlos" sin releer primero por qué no se resolvieron antes.

### 1. Sombra o follaje fusionado como píxel opaco — automático solo si hay 2+ colores de la misma lámina

**Qué se ve:** el matting (BiRefNet) a veces pega sombra o follaje del fondo
al calzado con alfa sólido (1.0), como si fuera parte del producto. Sobre una
lámina real (RP9300) llegó a incluir hasta 26 px de follaje/sombra a lo largo
del 69% del borde inferior.

**Qué SÍ existe hoy:** `reparar_sombra.py` corrige esto automáticamente,
pero solo cuando hay **2 o más colores del mismo modelo en la misma lámina**.
La señal es comparar el contorno de cada color contra los demás
(`comparar_colores.comparar_color_borde`): si un color se despega con
claridad del resto (outlier, `RAZON_MINIMA = 1.8`), se repara con material
propio del mismo recorte. Sin un grupo de 2+ colores para comparar, no hay
con qué confirmar que es sombra y no diseño — no se toca nada.

**Qué se intentó y se descartó** para el caso de un solo color/foto (sin
grupo de comparación): clasificar por reglas de color propias, LaMa, y otras
variantes — todas fallan porque el borde es una **mezcla** de producto y
fondo, y ningún umbral separa una mezcla de forma confiable (ver encabezado
de `comparar_colores.py` y la bitácora
`LimpiezaImagenes\sombra_rp9300_bitacora.html`).

**Qué hacer cuando aparece** (producto de un solo color, sin grupo): edición
manual, o descartar esa foto/color si hay otra variante utilizable.

**Dónde vive:** `src\reparar_sombra.py`, `src\comparar_colores.py`.

### 2. Suela faltante arriba del 15% no se repara

**Qué se ve:** cuando un calzado tapa a otro en la lámina y el hueco de suela
faltante supera ~15% del contorno, el sistema no intenta rellenarlo.

**Por qué:** no es una "mordida" reparable — es medio calzado tapado.
Rellenarlo sería inventar producto que no está en ninguna foto.

**Qué hacer cuando aparece:** es un límite deliberado, no un defecto. Si el
producto es importante, pedir al proveedor una foto sin solape.

**Dónde vive:** `src\reparar.py`.

### 3. La zona de suela reparada queda lisa

**Qué se ve:** al rellenar el hueco de suela tapada (ver punto 2, casos
≤15%), el relleno no reproduce el dibujo/textura del piso de la suela — solo
difunde color de los bordes sanos.

**Por qué:** el inpainting usado difunde color, no genera textura. Es visible
al hacer zoom, no a tamaño de ficha de catálogo.

**Qué se intentó:** clonado por gradientes de Poisson (lavaba el sombreado a
blanco 251,251,251) y corrimiento de media de color (pintaba la suela de
salmón, arrastrando color del empeine). Ambos descartados — el inpainting
del propio calzado (difusión simple desde los bordes sanos) da el resultado
menos malo.

**Dónde vive:** `src\reparar.py`.

### 4. El color dominante elige el más extenso, no el más saturado

**Qué se ve:** al nombrar el colorway de un producto por su color dominante,
puede elegir mal — ejemplo real: 4 tenis de la misma lámina salieron los
4 "gris" porque comparten un gris de base extenso, cuando a ojo son celeste,
rosado, gris y camel (el color distintivo era de acento, minoritario en
área).

**Importante — esto afecta *nombrar* el colorway, no *comparar* candidatos.**
Para la comparación/consolidación de candidatos (`motor_calificacion.py`,
`_firma_color_instancia`/`_mismo_color_por_firma`), este problema ya está
corregido desde el 2026-07-24: en vez de comparar solo el centroide de color
dominante, se compara la firma completa de **todas** las regiones de color
relevantes (no solo la mayoritaria) — así dos botas negro+acento-amarillo y
negro+acento-gris no se confunden aunque compartan el mismo negro dominante.
La limitación de "elige el más extenso" sigue viva solo para la etiqueta de
texto del colorway, no para el scoring.

**Dónde vive:** el nombrado del colorway vive en el proyecto de extracción de
color (`color_imagen`, `nombrar_color()`); la corrección de comparación vive
en `src\motor_calificacion.py` (~línea 420-465).

### 5. La erosión de contorno puede descartar producto real en casos límite

**Qué se ve:** para limpiar contorno contaminado con fondo, se erosiona el
borde del calzado. En casos límite puede comerse producto real.

**Cifra vigente:** `EROSION_PX = 2` (no 4), y **no es plana** desde que se
corrigió: es proporcional al grosor local de cada estructura, medido con
distancia continua al fondo (no un kernel cuadrado), y protege zonas
delgadas (correas, pétalos) que con una erosión uniforme perderían forma.
Caso real que sigue sin resolver del todo: una sandalia con hebilla
metálica, donde un reflejo bajó la confianza de BiRefNet en un hueco interno
pequeño (perforación de diseño) y la erosión lo trató igual que el borde
exterior, agrandándolo. Se corrigió separando "borde contra fondo" de
"hueco interno", pero el caso de reflejos fuertes en superficies metálicas
sigue siendo el más propenso a error.

**Qué hacer cuando aparece:** revisar candidatos con hebillas/adornos
metálicos o brillantes en la pantalla de revisión antes de aprobarlos.

**Dónde vive:** `src\limpiar_fondo.py`.

## Qué NO intenta resolver este sistema

- **Alinear ángulos de sesiones de fotos distintas.** `comparar_colores.py`
  solo compara recortes de la **misma lámina** (misma sesión de foto, misma
  pose). Comparar el mismo producto fotografiado en sesiones distintas es un
  problema no resuelto y deliberadamente fuera de alcance.
- **Calibrar los umbrales en píxeles de `reparar.py` para todas las
  resoluciones.** Esos umbrales se midieron sobre recortes de ~352 px de
  ancho; el pipeline real genera recortes de hasta ~1400 px en lienzo
  1600×1600 — desajuste documentado el 2026-08-20 en el propio código. Antes
  de tocar esos umbrales, confirmar contra qué resolución se está probando.

## Trazabilidad

Toda imagen retocada por `reparar.py`/`reparar_sombra.py` queda registrada en
la tabla `reparacion` del `lote.sqlite` de la carpeta de trabajo
(`almacen.py`), con el original respaldado en `_originales/`: ningún píxel
retocado queda sin rastro ni sin vuelta atrás.
