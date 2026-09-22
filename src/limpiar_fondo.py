"""
Quita del recorte la franja de fondo que el matting dejó pegada al calzado.

El problema medido sobre una lámina real (RP9300): el matting incluye **hasta
26 px de follaje y sombra a lo largo del 69 % del borde inferior**, con colores
como [35,53,45] —verde de hoja— y [11,13,10] —casi negro—. Eso llega a la imagen
final y contamina todo lo que se mida después: una cuarta parte de lo que parecía
"suela faltante" era en realidad sombra tomada como parte del producto.

El método es **erosión del contorno protegiendo las partes delgadas**, y llegó
después de descartar dos enfoques que parecían mejores.

Por qué no clasificar por color: la idea era aprender la paleta del producto y la
del fondo, y borrar los píxeles del filo que se parecieran más al fondo. Falla
porque los píxeles del borde son una **mezcla** de producto y fondo, y ningún
umbral separa una mezcla. Con umbral flojo quitaba 92 de ~3.000 px de follaje; con
umbral agresivo abría agujeros dentro de la suela y sobre fondo blanco se comía
hasta el 20 % de una sandalia negra.

Por qué la erosión no puede ser plana: sirve en un tenis de suela gruesa y
**destruye lo delicado**. En una sandalia de niña, 4 px uniformes cortan la correa
del talón y comen los pétalos de las flores. Por eso se mide el grosor local de
cada estructura y se protege lo que no puede permitirse perder contorno.

Y si la foto ya viene sin fondo —un PNG con transparencia, recortado por otro
programa— acá no hay nada que limpiar: erosionar sería pérdida pura. Esa
detección la hace el llamador.

Un cuarto problema, encontrado con una sandalia con hebilla metálica: la erosión
no distingue "borde contra el fondo" de "hueco diminuto adentro del zapato". Un
reflejo en la hebilla le bajó la confianza a BiRefNet en 14 píxeles —un degradado
suave, típico de matting, rodeado de material sólido—, y la erosión encogió ese
huequito igual que el contorno exterior, agrandándolo a 150 píxeles. El filtro de
opacidad plena, con kernel cuadrado, terminó de darle la forma de agujero
geométrico que se veía en el recorte final. Por eso ambas funciones separan el
tratamiento: solo el contorno que toca el fondo se erosiona; los huecos internos
—perforaciones de diseño, reflejos, hebillas— quedan como estaban.
"""

from __future__ import annotations

import cv2
import numpy as np

# Píxeles de contorno que se descartan donde la estructura aguanta. Con 4 px
# desaparece todo resto de follaje en fotos sucias, pero es agresivo para
# producto delicado: el valor viaja en el preset según el tipo de origen.
EROSION_PX = 2

# Grosor local mínimo (px) para que una zona pueda perder contorno. Una correa de
# talón o un pétalo tienen grosor chico en todo su recorrido y quedan intactos;
# una suela tiene grosor grande y sí se limpia.
GROSOR_MINIMO = 9

# Por encima de esto el alfa se lleva a opaco total. Los desenfoques que aplican
# `isolate_target` y `ownership_mask` de Zawa, más el de la erosión, se acumulan y
# dejan el producto en 200-254 en vez de 255: el fondo se transparenta apenas a
# través de toda la pieza. Medido en un recorte real: **cero píxeles en 255**.
UMBRAL_OPACO = 0.90

def limpiar_alfa(rgb: np.ndarray, alfa: np.ndarray,
                 erosion: int = EROSION_PX,
                 grosor_minimo: int = GROSOR_MINIMO) -> tuple[np.ndarray, int]:
    """Descarta contorno donde el fondo se mezcló, sin tocar las partes finas.

    `alfa` es HxW en float [0,1]. `rgb` se acepta para no cambiar la firma que ya
    usa el pipeline; este método no necesita el color, y eso es justamente lo que
    lo hace predecible.

    Devuelve (alfa limpio, píxeles quitados).
    """
    if erosion <= 0:
        return alfa, 0

    solido = (alfa >= 0.5).astype(np.uint8)
    if solido.sum() < 400:
        return alfa, 0

    # Silueta RELLENA: se tapan los huecos internos antes de erosionar, para que
    # la erosión solo encoja el contorno que de verdad toca el fondo. Sin este
    # paso, un hueco legítimo —el ojal de una hebilla, una perforación de
    # diseño— se trata igual que el borde exterior y crece con la erosión.
    contornos, _ = cv2.findContours(solido, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    rellena = np.zeros_like(solido)
    cv2.drawContours(rellena, contornos, -1, 1, thickness=cv2.FILLED)
    if rellena.sum() < 200:
        return alfa, 0  # recorte tan chico que erosionar lo borraría

    # Distancia CONTINUA al fondo (subpíxel, no un kernel cuadrado): erosionar
    # comparando contra esta distancia da un borde que sigue la curva real del
    # contorno. Erosionar con `cv2.erode` y un kernel cuadrado —como se hacía
    # antes— da el mismo resultado sobre un borde horizontal, pero sobre un
    # borde diagonal (una correa cruzada, por ejemplo) el kernel cuadrado
    # produce escalones: es la causa medida del "correas pixeladas".
    distancia = cv2.distanceTransform(rellena, cv2.DIST_L2, 5)

    # Grosor de la ESTRUCTURA, no del píxel: el máximo de la distancia al borde
    # en el entorno. Una correa fina tiene máximo chico en todo su recorrido;
    # una suela lo tiene grande. Se suaviza con blur (no un corte abrupto) para
    # que el punto donde una correa se une a la suela no salte de "protegido"
    # a "se erosiona" de un píxel a otro: ese salto es lo que se veía como
    # mordiscos en el borde de la suela, justo donde cambia el grosor.
    ventana = erosion * 4 + 1
    grosor = cv2.dilate(distancia, np.ones((ventana, ventana), np.uint8))
    grosor_suave = cv2.GaussianBlur(grosor, (0, 0), sigmaX=max(erosion, 1))

    # Fracción de la erosión pedida que de verdad se aplica en cada píxel: 0 en
    # zonas protegidas (grosor <= mínimo), 1 en zonas gruesas, con rampa entre
    # ambas en vez de un escalón.
    fraccion = np.clip((grosor_suave - grosor_minimo) / max(erosion, 1), 0.0, 1.0)
    erosion_efectiva = fraccion * erosion

    # Franja de 1.5 px de degradado al borde erosionado: el mismo ancho típico
    # del antialias que ya trae BiRefNet, para no reemplazar un degradado
    # natural por otro borde duro.
    transicion = 1.5
    escala = np.clip((distancia - erosion_efectiva) / transicion, 0.0, 1.0).astype(np.float32)

    limpio = (alfa * escala).astype(np.float32)

    quitados = int(((alfa >= 0.5) & (limpio < 0.5)).sum())
    return limpio, quitados


def afinar_borde(alfa: np.ndarray,
                 umbral_opaco: float = UMBRAL_OPACO) -> tuple[np.ndarray, int]:
    """Deja el filo presentable: opaco adentro, sin piezas sueltas y sin ondas.

    Tres arreglos que se ven al ampliar el borde:

    1. **Opacidad plena.** Los desenfoques encadenados dejan el interior en
       200-254 y el fondo se cuela levemente por todo el producto.
    2. **Piezas sueltas.** Fragmentos desconectados del calzado —debris del
       fondo, un vecino mal aislado— que sobreviven a todo lo anterior.
    3. **Filo ondulado.** El contorno queda con bultos que no siguen la línea del
       calzado; una apertura morfológica los alisa sin redondear la silueta.

    Devuelve (alfa afinado, píxeles descartados por no tocar el cuerpo principal).
    """
    solido = (alfa >= 0.5).astype(np.uint8)
    if solido.sum() < 200:
        return alfa, 0

    # Piezas sueltas: sobrevive solo la más grande. Un umbral relativo (por
    # ejemplo "más del 0.4 % del área principal") dejaba pasar debris real: una
    # vara de fondo desconectada del zapato midió 1.195 px —3 % del cuerpo
    # principal— y sobrevivió entera, corrompiendo además la medición de suela
    # porque su bounding box se sumó al del calzado. El detector, la separación
    # por instancia y `isolate_target` ya se ocuparon de aislar el objetivo: si
    # después de todo eso sigue quedando una pieza sin tocar el cuerpo principal,
    # no es parte del calzado.
    n, etiquetas, stats, _ = cv2.connectedComponentsWithStats(solido, 8)
    quitados = 0
    mayor_mascara = solido
    if n > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        mayor = 1 + int(np.argmax(areas))
        mayor_mascara = (etiquetas == mayor).astype(np.uint8)
        quitados = int(solido.sum() - mayor_mascara.sum())

    # El corte anterior usaba `solido` (alfa>=0.5) directo como máscara: eso
    # descarta TODO el degradado natural de BiRefNet por debajo de 0.5 —el
    # antialias real de un borde diagonal vive justo ahí, entre 0.05 y 0.5— y
    # lo que queda es un borde duro en el umbral, que además hereda el
    # escalonado de `connectedComponentsWithStats` sobre una silueta binaria.
    # Eso es lo que se veía como "correas pixeladas": no había pixelado en el
    # dato, lo estábamos generando nosotros al tirar el degradado.
    #
    # Acá solo se necesita separar debris (piezas sueltas) del calzado real;
    # no hace falta recortar el degradado para eso. Se dilata la pieza
    # principal lo suficiente para cubrir el antialias típico de BiRefNet
    # (2-4 px) y se descarta todo lo que quede FUERA de esa zona ampliada —el
    # calzado real conserva su degradado íntegro, tal como lo entregó el
    # matting; solo el debris desconectado (que está a varios px de distancia)
    # se pierde.
    alcance = cv2.dilate(mayor_mascara, np.ones((9, 9), np.uint8))
    limpio = np.where(alcance > 0, alfa, 0.0).astype(np.float32)
    # Opacidad plena adentro, conservando el degradado del filo para el antialias.
    limpio = np.where(limpio >= umbral_opaco, 1.0, limpio).astype(np.float32)
    return limpio, quitados


def ya_sin_fondo(path) -> bool:
    """¿La foto de origen ya viene recortada por otro programa?

    Un PNG con transparencia real significa que alguien ya quitó el fondo. En ese
    caso el matting no arrastra follaje y erosionar solo destruiría producto — que
    es lo que pasó con una lámina de sandalias de niña ya recortada.
    """
    from PIL import Image

    try:
        with Image.open(path) as im:
            if im.mode not in ("RGBA", "LA") and "transparency" not in im.info:
                return False
            alfa = np.array(im.convert("RGBA"))[..., 3]
    except Exception:  # noqa: BLE001
        return False
    # Transparencia real, no un canal alfa opaco de adorno.
    return bool((alfa < 250).mean() > 0.05)
