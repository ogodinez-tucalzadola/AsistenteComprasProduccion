"""
Completa la suela de un calzado que quedó tapado por otro en la misma lámina.

El caso: el proveedor pone el mismo modelo en varios colores y el de adelante
tapa parte de la suela del de atrás. Al recortarlos, al de atrás le falta un
pedazo que **no existe en la foto**.

El método, después de ocho enfoques probados y descartados: **inpainting del
propio calzado**. Se estima dónde debería cerrar el borde de suela, y ese hueco
se rellena difundiendo el color desde los vecinos del mismo zapato.

Por qué NO se copia de las hermanas, que fue el primer instinto: están en otra
posición de la foto, o sea otra perspectiva y otra luz. Cinco variantes de ese
camino fallaron en formas distintas — filo oscuro, mancha salmón, sombreado
lavado, puntos marrones del calzado vecino, follaje metido adentro del producto.
Cualquier material que venga de otro zapato trae su color y su textura.

Tres cosas que hacen la diferencia y están codificadas acá:

1. **El fondo tiene que estar limpio antes.** El primer inpaint falló porque
   difundía desde píxeles de follaje. Con el fondo ya removido, funciona.
2. La curva de suela se ajusta **solo sobre los extremos sanos**. Ajustarla sobre
   todo el ancho la hunde dentro de la mordida y subestima el faltante.
3. **La máscara se dilata** antes de rellenar, para consumir el filo
   antialiaseado original. Si no, ese borde viejo queda adentro de la imagen y se
   lee como una línea falsa: la suela parece tener un canto que no existe.

Descartado explícitamente: el clonado por gradientes de Poisson lavaba el
sombreado a 251,251,251, y el corrimiento de media de color pintaba la suela de
salmón porque la banda de referencia arrastraba color del empeine.

Toda imagen retocada queda marcada en la bitácora de reparaciones (tabla
`reparacion` del `lote.sqlite` de la carpeta de trabajo, ver `almacen.py`) y su
original respaldado en `_originales/`: un catálogo no puede tener píxeles
retocados sin rastro ni sin vuelta atrás.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import minimize

import almacen

CARPETA_ORIGINALES = "_originales"
ALFA_SOLIDO = 128

# Todos los umbrales en píxeles de este módulo (ventanas de suavizado, márgenes
# de dilatado, "cuántos px de ruido son normales") se midieron y calibraron
# sobre recortes de ~352px de ancho — el tamaño de las pruebas hechas en chat.
# El pipeline real genera recortes de hasta ~1400px (canvas de 1600x1600): un
# "ruido normal de 3px" ahí es insignificante, y una ventana de suavizado de
# 11px desaparece en un contorno 4x más grande. Bug real medido 2026-08-20:
# la misma foto (RP9300_02) a 352px de ancho daba un corte limpio; a 1400px,
# con las mismas constantes SIN escalar, el corte quedó mal. Por eso cada
# función que usa un umbral en píxeles lo escala con `_escala(s)` primero.
REFERENCIA_ANCHO_PX = 352.0


def _escala(s: "Suela") -> float:
    return max(s.ancho, 1) / REFERENCIA_ANCHO_PX

# Cuánto se agranda la máscara antes de rellenar. Es lo que borra el filo
# antialiaseado original: sin esto queda una línea falsa donde el zapato
# terminaba, y la suela parece tener un canto inexistente. Elegido comparando
# 0/4/7 px sobre un caso real — 4 quita la línea y conserva definición del canto.
DILATAR_MASCARA = 4

# Ancho de la transición entre lo rellenado y el material original, en px.
# El peso cae con la distancia al centro del hueco, así no hay corte duro.
PLUMA_TRANSICION = 5

# Debajo del mínimo es ruido del ajuste de curva (las hermanas sanas miden hasta
# ~2.5%, así que bajar de esto empieza a marcar como reparables recortes sanos).
# Por encima del máximo no es una suela mordida sino medio calzado tapado, y
# rellenarlo sería inventar producto.
MIN_REPARABLE_PCT = 2.0
MAX_REPARABLE_PCT = 15.0


# ── Geometría de la suela ────────────────────────────────────────────────────


@dataclass
class Suela:
    alfa: np.ndarray
    rgb: np.ndarray
    x0: int
    y0: int
    ancho: int
    alto: int
    borde: np.ndarray
    valido: np.ndarray
    curva: np.ndarray
    deficit: np.ndarray
    area_faltante: float = 0.0
    area_solida: float = 0.0
    zona: tuple[float, float] = (0.0, 0.0)


COLOR_BASURA = np.array([69.0, 79.0, 69.0])  # #454F49, dado por el usuario
TOLERANCIA_BASURA = 28.0  # distancia RGB para contar como "el mismo color de basura"


def _ajuste_robusto(ejex: np.ndarray, x_valido: np.ndarray, y_valido: np.ndarray,
                     grado: int, iteraciones: int = 6) -> np.ndarray:
    """Curva esperada por regresión robusta (IRLS, peso Tukey biweight).

    No necesita que le digan dónde está la contaminación —ni por color ni por
    fracción del ancho—, la descubre sola: ajusta, mide qué tan lejos quedó
    cada punto del ajuste, le baja el peso a los que quedaron lejos, y
    reajusta. Un tramo contaminado (sombra, follaje) es sistemáticamente un
    grupo de puntos que se aparta junto del resto — eso es exactamente lo que
    esta técnica aprende a ignorar, sin necesitar saber de antemano ni su
    color ni su posición.

    Reemplaza dos intentos previos que sí necesitaban esa información externa:
    excluir por fracción de ancho (dejaba pocos puntos y desestabilizaba el
    resto de la curva) y excluir por color de basura (frágil: depende de
    medir bien un color exacto). Verificado en RP9300_02: 11.5 px de déficit
    máximo sin ninguna pista externa, contra 38.8 px del ajuste ingenuo.
    """
    x = x_valido.astype(np.float64)
    y = y_valido.astype(np.float64)
    peso = np.ones_like(x)
    coef = np.polyfit(x, y, grado)
    for _ in range(iteraciones):
        coef = np.polyfit(x, y, grado, w=peso)
        resid = y - np.polyval(coef, x)
        mad = max(np.median(np.abs(resid - np.median(resid))) * 1.4826, 1e-3)
        c = 4.685 * mad
        u = np.clip(resid / c, -1, 1)
        peso = (1 - u ** 2) ** 2
        peso[np.abs(resid) >= c] = 0.0
    return peso, coef


def _curva_concavidad_unica(ejex: np.ndarray, x_valido: np.ndarray, y_valido: np.ndarray,
                             peso: np.ndarray, grado: int) -> np.ndarray:
    """Ajusta la curva final con UNA sola concavidad, sin ondular.

    Un calzado real (visto de lado o en 3/4) es un arco simple: la suela nunca
    pasa de cóncava a convexa y de vuelta. El ajuste robusto por sí solo
    (`_ajuste_robusto`, grado alto) sigue bien la curvatura pero puede
    ondular varias veces —hasta 3 cambios de concavidad medidos en un caso
    real—, algo que ningún zapato hace. Acá se resuelve con optimización
    restringida (SLSQP): mismo ajuste ponderado por mínimos cuadrados, pero
    con la restricción de que la segunda derivada no cambie de signo en
    ningún punto del ancho. El signo se toma del ajuste sin restricción (así
    respeta si el zapato es más cóncavo o más convexo), y el resultado
    verificado numéricamente da 0 cambios de concavidad en los 4 casos de
    prueba (antes: hasta 2-3).

    IMPORTANTE: tanto X como Y se normalizan a escala ~1 antes de optimizar.
    Solo normalizar X (como estaba antes) deja Y en píxeles reales — a un
    recorte de 1600px eso da valores ~4x más grandes que a 400px, la pérdida
    que minimiza SLSQP queda ~16x más grande, y su tolerancia de convergencia
    (`ftol`, un umbral ABSOLUTO) deja de ser significativa a esa escala: el
    optimizador corta la búsqueda mucho antes de encontrar el mejor ajuste.
    Bug real medido 2026-08-20: la misma foto (RP9300_02) a 400px daba una
    curva de un solo arco correcta; a 1600px, sin normalizar Y, quedaba visto
    a ojo mucho menos cóncava — Y sin normalizar era la causa.
    """
    escala_x = max(x_valido.max(), 1.0)
    escala_y = max(np.abs(y_valido).max(), 1.0)
    x_n = x_valido.astype(np.float64) / escala_x
    y_n = y_valido.astype(np.float64) / escala_y
    ejex_n = ejex.astype(np.float64) / escala_x

    coef0 = np.polyfit(x_n, y_n, grado, w=peso)
    signo = 1.0 if np.median(np.polyval(np.polyder(coef0, 2), ejex_n)) >= 0 else -1.0

    # El polinomio puede quedar ajustado bien en el medio pero desviarse
    # decenas de píxeles justo en los dos extremos del ancho (inestabilidad
    # clásica de un polinomio en el borde de sus propios datos — medido:
    # hasta ~90px de error en la primera columna). Bajar el grado ayuda pero
    # no lo garantiza. Acá se FUERZA que la curva pase por el borde real
    # (mediana de los primeros/últimos puntos, no un solo píxel que puede
    # traer ruido) en sus dos extremos — restricción de IGUALDAD, no mezcla
    # de datos después: SLSQP sigue optimizando el resto de la curva con la
    # concavidad única intacta, solo con esos dos puntos clavados.
    n_ancla = min(5, len(x_valido))
    x_inicio_n, y_inicio_n = x_n[0], float(np.median(y_n[:n_ancla]))
    x_fin_n, y_fin_n = x_n[-1], float(np.median(y_n[-n_ancla:]))

    def perdida(coef):
        r = (np.polyval(coef, x_n) - y_n) * np.sqrt(peso)
        return float((r ** 2).sum())

    def restriccion_concavidad(coef):
        return signo * np.polyval(np.polyder(coef, 2), ejex_n)

    restricciones = [
        {"type": "ineq", "fun": restriccion_concavidad},
        {"type": "eq", "fun": lambda coef: np.polyval(coef, x_inicio_n) - y_inicio_n},
        {"type": "eq", "fun": lambda coef: np.polyval(coef, x_fin_n) - y_fin_n},
    ]
    res = minimize(perdida, coef0, constraints=restricciones,
                    method="SLSQP", options={"maxiter": 500, "ftol": 1e-10})
    coef_final = res.x if res.success else coef0
    return np.polyval(coef_final, ejex_n) * escala_y


def medir_suela(path_png: Path, grado=5,
                 color_basura: np.ndarray | None = None,
                 tolerancia_basura: float = TOLERANCIA_BASURA,
                 concavidad_unica: bool = False) -> Suela:
    """Mide el borde inferior y estima dónde cerraría si no hubiera oclusión.

    La curva esperada se ajusta con regresión robusta (`_ajuste_robusto`): no
    depende de una ventana "sana" fija ni de excluir tramos a mano, descubre
    la contaminación por sí sola. `color_basura` queda como ayuda opcional
    (adicional, no necesaria) para cuando la contaminación además desplaza el
    borde detectado, no solo el ajuste de la curva.

    `concavidad_unica=True` fuerza además que la curva no ondule (una sola
    concavidad, como un calzado real) — se ve mejor para AUDITAR/mostrar la
    línea roja, pero infla el área de "faltante" donde la curva no puede
    seguir el relieve real (medido: 231px de máscara real pasaron a 758px en
    TEST_02). Por eso queda apagado por defecto: `reparar()` necesita la
    máscara más ajustada posible, no la más "bonita". Dejarlo en `True` solo
    para generar imágenes de auditoría (líneas azul/roja), nunca para decidir
    cuánto rellenar.
    """
    im = np.array(Image.open(path_png).convert("RGBA"))
    return _medir_suela_arr(im, nombre=path_png.name, grado=grado, color_basura=color_basura,
                             tolerancia_basura=tolerancia_basura, concavidad_unica=concavidad_unica)


def medir_suela_desde_array(im: np.ndarray, grado=5,
                             color_basura: np.ndarray | None = None,
                             tolerancia_basura: float = TOLERANCIA_BASURA,
                             concavidad_unica: bool = False) -> Suela:
    """Igual que `medir_suela`, pero sobre una imagen ya en memoria (RGBA) —
    para encadenar corte + relleno sin escribir a disco entre medio."""
    return _medir_suela_arr(im, nombre="(en memoria)", grado=grado, color_basura=color_basura,
                             tolerancia_basura=tolerancia_basura, concavidad_unica=concavidad_unica)


def _medir_suela_arr(im: np.ndarray, nombre: str, grado=5,
                      color_basura: np.ndarray | None = None,
                      tolerancia_basura: float = TOLERANCIA_BASURA,
                      concavidad_unica: bool = False) -> Suela:
    alfa_full, rgb_full = im[..., 3], im[..., :3]
    solido = alfa_full >= ALFA_SOLIDO
    if not solido.any():
        raise ValueError(f"{nombre}: sin material sólido")

    ys, xs = np.where(solido)
    y0, x0 = int(ys.min()), int(xs.min())
    rec = solido[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    rec_rgb = rgb_full[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1].astype(np.float64)
    alto, ancho = rec.shape

    if color_basura is not None:
        dist_basura = np.sqrt(((rec_rgb - color_basura) ** 2).sum(axis=-1))
        es_basura = dist_basura <= tolerancia_basura
    else:
        es_basura = np.zeros((alto, ancho), dtype=bool)

    borde = np.full(ancho, np.nan)
    for x in range(ancho):
        col = np.where(rec[:, x] & ~es_basura[:, x])[0]
        if len(col):
            borde[x] = col.max()
    valido = ~np.isnan(borde)

    ejex = np.arange(ancho)
    peso, coef = _ajuste_robusto(ejex, ejex[valido], borde[valido], grado=grado)
    if concavidad_unica:
        curva = _curva_concavidad_unica(ejex, ejex[valido], borde[valido], peso, grado=grado)
    else:
        curva = np.polyval(coef, ejex.astype(np.float64))

    deficit = np.clip(curva - np.nan_to_num(borde), 0, None)
    deficit[~valido] = 0.0
    afectadas = ejex[deficit > 3]
    zona = ((afectadas.min() / ancho, afectadas.max() / ancho)
            if len(afectadas) else (0.0, 0.0))

    return Suela(alfa_full, rgb_full, x0, y0, ancho, alto, borde, valido, curva,
                 deficit, float(deficit.sum()), float(solido.sum()), zona)


def _suavizar_para_dibujo(y: np.ndarray, valido: np.ndarray, ventana: int = 5) -> np.ndarray:
    """Promedio móvil solo para visualizar: el borde real es un valor por
    columna y se ve "escalonado" si se dibuja punto a punto — no es el dato,
    es cómo se dibuja. Un calzado real tiene un contorno continuo, no una
    escalera de píxeles."""
    y = y.copy()
    if (~valido).any() and valido.any():
        y[~valido] = np.interp(np.flatnonzero(~valido), np.flatnonzero(valido), y[valido])
    k = np.ones(ventana) / ventana
    return np.convolve(y, k, mode="same")


def dibujar_contorno(path_png: Path, color_basura: np.ndarray | None = COLOR_BASURA,
                      tolerancia_basura: float = TOLERANCIA_BASURA) -> Image.Image:
    """Azul = contorno real (suavizado). Rojo = curva esperada, con
    concavidad única — pensada para AUDITAR visualmente un recorte (mostrar
    dónde hay una mordida real o sombra sin resolver), no para decidir cuánto
    rellenar: usa `concavidad_unica=True`, que a propósito prioriza una línea
    que se lee como un calzado real por sobre el ajuste más ajustado posible
    (ver nota en `medir_suela`)."""
    s = medir_suela(path_png, color_basura=color_basura,
                     tolerancia_basura=tolerancia_basura, concavidad_unica=True)
    im = Image.open(path_png).convert("RGBA")
    # Mismo gris que usa el visor para la vista SIN líneas
    # (`gui_profesional_ctk._componer_sobre_gris`, 238,241,244) -- antes acá
    # se componía sobre blanco puro, así que togglear "Ver líneas azul/roja"
    # cambiaba visiblemente el fondo de la foto además de mostrar las líneas.
    # Reclamo real del usuario (2026-09-08): "¿por qué al quitar las líneas
    # se cambia el color de fondo?".
    fondo = Image.new("RGBA", im.size, (238, 241, 244, 255))
    vista = Image.alpha_composite(fondo, im).convert("RGB")
    draw = ImageDraw.Draw(vista)

    def tramos_validos(x_ini: int = 0, x_fin: int | None = None) -> list[list[int]]:
        """Columnas válidas agrupadas en tramos CONTIGUOS. Una columna ocluida
        por el cordón o el vecino corta la validez ahí — conectar la línea a
        través de ese hueco dibuja un salto recto que no existe en el
        calzado real."""
        x_fin = s.ancho if x_fin is None else x_fin
        tramos, actual = [], []
        for x in range(x_ini, x_fin):
            if s.valido[x]:
                actual.append(x)
            elif actual:
                tramos.append(actual)
                actual = []
        if actual:
            tramos.append(actual)
        return tramos

    salto_maximo = 20.0 * _escala(s)  # px: un contorno real no salta más que esto entre columnas vecinas

    def dividir_por_salto(tramo: list[int], valores: np.ndarray) -> list[list[int]]:
        partes, actual = [], [tramo[0]]
        for x_prev, x in zip(tramo, tramo[1:]):
            if abs(valores[x] - valores[x_prev]) > salto_maximo:
                partes.append(actual)
                actual = []
            actual.append(x)
        if actual:
            partes.append(actual)
        return partes

    def dibujar(valores: np.ndarray, color: tuple[int, int, int]) -> None:
        for tramo in tramos_validos():
            for parte in dividir_por_salto(tramo, valores):
                pts = [(s.x0 + x, s.y0 + valores[x]) for x in parte]
                if len(pts) > 1:
                    draw.line(pts, fill=color, width=2, joint="curve")

    # Umbral de "esto sí hace falta corregir" -- mismo criterio ya usado en
    # `_medir_suela_arr` para `zona`/`afectadas` (`deficit > 3`), reutilizado
    # acá para decidir DÓNDE dibujar la línea roja.
    UMBRAL_DEFICIT_DIBUJO = 3.0

    def dibujar_curva_donde_hace_falta(valores: np.ndarray, color: tuple[int, int, int]) -> None:
        """La curva esperada (roja) solo tiene sentido mostrarla donde
        realmente aporta algo: (a) columnas SIN borde real detectado (ocluido,
        sombra, color confundido con fondo) -- ahí es la única referencia que
        hay, y antes se recortaba al primer tramo válido, haciendo que
        arrancara en plena mitad de la suela (reclamo real del usuario,
        2026-09-07/08); y (b) columnas donde el borde SÍ se detectó pero la
        curva encuentra un déficit real (mordida, sombra sin resolver) -- ese
        es el "cuánto falta" que `reparar()` usa para rellenar.

        Donde el borde ya es válido Y la curva no encuentra déficit (`curva`
        por ENCIMA del borde real, osea `deficit=0` tras el clip), dibujar
        rojo ahí es puro ruido: el ajuste de una sola concavidad no puede
        seguir una esquina casi vertical (el borde real del talón visto de
        lado sube casi recto), así que la curva se ve obligada a cruzar en
        diagonal esa esquina -- otro reclamo real del usuario (2026-09-08,
        foto CHUCK_70_2_01: "la línea roja en el talón empieza mitad de la
        suela" en una foto SIN ningún tramo inválido, confirmado con
        deficit=0 en toda esa zona). Ahí el azul ya muestra el borde real
        correcto; superponerle una diagonal roja que lo corta no aporta nada,
        solo confunde."""
        necesita_roja = (~s.valido) | (s.deficit > UMBRAL_DEFICIT_DIBUJO)
        tramos, actual = [], []
        for x in range(s.ancho):
            if necesita_roja[x]:
                actual.append(x)
            elif actual:
                tramos.append(actual); actual = []
        if actual:
            tramos.append(actual)
        for tramo in tramos:
            for parte in dividir_por_salto(tramo, valores):
                pts = [(s.x0 + x, s.y0 + valores[x]) for x in parte]
                if len(pts) > 1:
                    draw.line(pts, fill=color, width=2, joint="curve")

    dibujar(_suavizar_para_dibujo(s.borde, s.valido), (0, 90, 255))
    dibujar_curva_donde_hace_falta(s.curva, (230, 30, 30))

    return vista


def porcentaje_faltante(s: Suela) -> float:
    total = s.area_solida + s.area_faltante
    return 100.0 * s.area_faltante / total if total else 0.0


# Ancho del promedio móvil que suaviza la curva antes de usarla para cortar o
# rellenar. Sin esto, redondear la curva a un entero por columna deja un
# corte "escalonado" (dientes) aunque la curva de fondo sea casi recta — el
# escalón es un artefacto de dibujar columna por columna, no parte del dato.
VENTANA_SUAVIZADO_CORTE = 11


def _curva_suavizada(s: Suela, ventana: int | None = None) -> np.ndarray:
    if ventana is None:
        ventana = max(3, int(round(VENTANA_SUAVIZADO_CORTE * _escala(s))) | 1)
    kernel = np.ones(ventana) / ventana
    extendida = np.pad(s.curva, ventana // 2, mode="edge")
    return np.convolve(extendida, kernel, mode="valid")


# La SUMA total de exceso no distingue nada: hasta en un calzado sano
# (RP9300_01/03/04) el ruido normal del ajuste acumula 220-320px repartidos
# por todo el contorno. Lo que sí separa la contaminación real es que se
# SOSTIENE en una racha ancha de columnas seguidas (medido en RP9300_02: 25
# columnas seguidas con exceso, contra máximo 19 en sus hermanas sanas).
UMBRAL_RUIDO_EXCEDENTE_PX = 3.0
RACHA_MINIMA_EXCEDENTE_PX = 15


def tiene_excedente(path_png: Path, color_basura: np.ndarray | None = COLOR_BASURA,
                     racha_minima: int | None = None) -> bool:
    """¿Hay material más allá del contorno esperado que valga la pena recortar?

    Independiente de `reparar_sombra.corregir_grupo`: ese compara colores
    ENTRE las fotos de una misma lámina, y no detecta esto — la contaminación
    de RP9300_02 es follaje que el matting fusionó como si fuera parte sólida
    del zapato, no un tono de sombra distinto al de sus hermanas. Por eso el
    botón "Recortar sombra" de la GUI usa esta señal propia, no `sombra_outlier`.

    Deliberadamente permisivo: activa un BOTÓN, no aplica nada solo — el costo
    de un falso positivo es que el usuario mire el contorno y decida no
    tocarlo; el costo de un falso negativo es el bug que motivó esto (el
    botón nunca se activaba para el caso real).
    """
    try:
        s = medir_suela(path_png, color_basura=color_basura, concavidad_unica=True)
    except ValueError:
        return False
    escala = _escala(s)
    if racha_minima is None:
        racha_minima = max(3, int(round(RACHA_MINIMA_EXCEDENTE_PX * escala)))
    exceso = np.where(s.valido, s.borde - s.curva, 0.0)
    marca = exceso > (UMBRAL_RUIDO_EXCEDENTE_PX * escala)
    racha = mejor = 0
    for v in marca:
        racha = racha + 1 if v else 0
        mejor = max(mejor, racha)
    return mejor >= racha_minima


def _cortar_y_reparar_excedente(path_png: Path, color_basura: np.ndarray | None
                                 ) -> tuple[Image.Image | None, Image.Image | None, dict]:
    """Lógica pura: corta y repara en memoria, sin tocar el disco.

    Devuelve `(imagen_cortada, imagen_reparada, stats)` — `imagen_cortada` es
    el resultado justo después de cortar (para AUDITAR el paso intermedio,
    ej. en la vista de "proceso paso a paso"), `imagen_reparada` es el
    resultado final con el hueco del corte ya relleno. Ambas son `None` si no
    había nada que cortar.
    """
    s = medir_suela(path_png, color_basura=color_basura, concavidad_unica=True)
    curva = _curva_suavizada(s)

    arr = np.array(Image.open(path_png).convert("RGBA"))
    alfa = arr[..., 3].astype(np.float32)

    px_recortados = 0
    for x in range(s.ancho):
        if not s.valido[x]:
            continue
        y0 = s.y0 + max(0, int(round(curva[x])))
        y1 = s.y0 + s.alto
        if y1 <= y0:
            continue
        columna = alfa[y0:y1, s.x0 + x]
        px_recortados += int((columna >= ALFA_SOLIDO).sum())
        columna[:] = 0.0

    if not px_recortados:
        return None, None, {"px_recortados": 0, "px_rellenados": 0}

    arr[..., 3] = np.clip(alfa, 0, 255).astype(np.uint8)
    img_cortada = Image.fromarray(arr, "RGBA")

    # Medir sobre la imagen YA cortada: el borde real ahora termina donde se
    # cortó, y el hueco entre ese borde nuevo y la misma curva suavizada es lo
    # que hay que rellenar — la MISMA línea que decidió el corte, para que no
    # quede un hueco (usar una curva distinta acá fue justo el bug: dejaba un
    # espacio claro entre lo rellenado y el borde cortado).
    im_cortada_arr = np.array(img_cortada)
    s2 = medir_suela_desde_array(im_cortada_arr, color_basura=color_basura, concavidad_unica=True)
    rgb, alfa2 = s2.rgb.copy(), s2.alfa.copy()
    escala = _escala(s2)
    dilatar = max(1, int(round(DILATAR_MASCARA * escala)))
    pluma = max(1.0, PLUMA_TRANSICION * escala)

    falta = np.zeros(alfa2.shape, np.uint8)
    for x in range(s2.ancho):
        if not s2.valido[x]:
            continue
        yb, yc = int(round(s2.borde[x])), int(round(curva[x]))
        if yc > yb:
            falta[s2.y0 + yb + 1: s2.y0 + yc + 1, s2.x0 + x] = 255

    masc = cv2.dilate(falta, np.ones((dilatar * 2 + 1,) * 2, np.uint8))
    base = rgb.copy()
    base[(alfa2 < ALFA_SOLIDO) & (masc == 0)] = _color_suela(s2)
    relleno = cv2.inpaint(base, masc, max(6, int(round(6 * escala))), cv2.INPAINT_TELEA)

    dist = cv2.distanceTransform((masc > 0).astype(np.uint8), cv2.DIST_L2, 5)
    peso = np.clip(dist / pluma, 0, 1)[..., None]
    rgb_final = (relleno * peso + rgb * (1 - peso)).astype(np.uint8)

    alfa_final = np.maximum(alfa2, (falta > 0).astype(np.uint8) * 255)
    vecino = cv2.dilate((alfa2 >= ALFA_SOLIDO).astype(np.uint8), np.ones((3, 3), np.uint8))
    alfa_final = np.maximum(alfa_final, ((masc > 0) & (vecino > 0)).astype(np.uint8) * 255)

    suave_alfa = cv2.GaussianBlur(alfa_final.astype(np.float32), (0, 0), sigmaX=1.2)
    frontera = (alfa_final > 0) & (alfa2 == 0)
    alfa_final = alfa_final.astype(np.float32)
    alfa_final[frontera] = suave_alfa[frontera]
    alfa_final = alfa_final.astype(np.uint8)

    px_rellenados = int((alfa_final >= ALFA_SOLIDO).sum() - (alfa2 >= ALFA_SOLIDO).sum())
    img_reparada = Image.fromarray(np.dstack([rgb_final, alfa_final]), mode="RGBA")
    stats = {"px_recortados": px_recortados, "px_rellenados": max(px_rellenados, 0)}
    return img_cortada, img_reparada, stats


def recortar_excedente(path_png: Path, color_basura: np.ndarray | None = COLOR_BASURA) -> dict:
    """Corta lo que sobra más allá del contorno esperado, y rellena ese mismo
    corte hasta quedar sólido — sombra o follaje que el matting fusionó como
    si fuera parte del calzado, en un solo paso: recorte + reparación.

    Después de nueve enfoques que intentaron RECONSTRUIR solo la parte
    contaminada (margen fijo, detección de tramo sostenido, límites por
    columna) y dejaban restos o cortaban de más, la solución que sí funcionó
    es más simple: la curva de `medir_suela` (concavidad única, suavizada) ya
    es una estimación robusta y ESTABLE de dónde cierra la suela real —
    cualquier píxel más allá de ella, en TODO el ancho, se corta directo, sin
    excepción por columna ni margen. Y como el corte deja un hueco donde
    antes había contaminación, se rellena de inmediato con el mismo método de
    `reparar()` (inpaint del propio calzado) usando esa MISMA curva suavizada
    como límite — así el corte y el relleno coinciden exactamente y no queda
    ni escalón visible ni una tira de sombra sin tocar.
    """
    _, img_reparada, stats = _cortar_y_reparar_excedente(path_png, color_basura)
    if img_reparada is not None:
        img_reparada.save(path_png)
    return stats


def reparable(pct: float) -> bool:
    return MIN_REPARABLE_PCT <= pct <= MAX_REPARABLE_PCT


# ── Mordidas reales vs. ruido de textura ─────────────────────────────────────

# Un tramo más angosto que esto es el ancho típico de un taco/relieve: sube y
# baja el borde en pocos píxeles y no es una mordida.
ANCHO_MINIMO_MORDIDA_PX = 10

# Profundidad promedio que debe sostener un tramo para contar como mordida real.
# El ruido de textura da picos de 1-3 px que no se sostienen en el promedio de
# todo el tramo; una mordida real (otro calzado tapando la suela) mantiene un
# déficit parejo de varios píxeles a lo largo de todo el tramo tapado.
PROFUNDIDAD_MINIMA_SOSTENIDA = 5.0

# Por encima de esto ya no es una mordida sino medio calzado tapado: inventar
# ese ancho sería fabricar producto, no reparar un defecto puntual.
ANCHO_MAXIMO_MORDIDA_FRAC = 0.5

# Cualquier ajuste de curva (robusto o no) es menos confiable justo en los
# extremos del ancho: no hay datos más allá del recorte para anclarlo, y un
# polinomio de grado alto puede oscilar ahí sin que exista ningún defecto
# real. Medido en 2 tenis ya confirmados sanos (RP9255_01, RP9255_03): ambos
# daban una "mordida" falsa de 11-13px pegada al 0% o al 100% del ancho.
MARGEN_BORDE_FRAC = 0.05


@dataclass
class Mordida:
    x0_frac: float
    x1_frac: float
    ancho_px: int
    profundidad_media: float
    profundidad_max: float
    area_px: float


def detectar_mordidas(s: Suela, ancho_minimo_px: int | None = None,
                       profundidad_minima: float | None = None
                       ) -> list[Mordida]:
    """Separa mordidas reales (tramo sostenido) del ruido de textura.

    `porcentaje_faltante` mide un área total y no distingue de dónde viene: una
    mordida real concentrada en un tramo chico del borde puede dar el mismo
    número que ruido disperso de tacos en todo el ancho — un caso real
    (TEST_02, tramo 30-40% del ancho, ~15-20px de déficit sostenido) midió solo
    1.26% de área total y quedó igual de invisible para el umbral que el ruido
    de una suela sana con relieve irregular.

    La diferencia entre ambos no está en el área, está en la forma: una mordida
    real es un tramo contiguo donde el déficit se sostiene por varios píxeles
    seguidos; el ruido de textura son picos angostos que suben y bajan sin
    mantenerse. Por eso se recorre el borde buscando tramos contiguos por
    encima del umbral de ruido, y de esos tramos solo cuentan como mordida los
    que además son anchos y profundos en promedio — no solo en su pico.
    """
    escala = _escala(s)
    ancho_minimo_px = (ANCHO_MINIMO_MORDIDA_PX * escala) if ancho_minimo_px is None else ancho_minimo_px
    profundidad_minima = (PROFUNDIDAD_MINIMA_SOSTENIDA * escala) if profundidad_minima is None else profundidad_minima
    umbral_ruido = 3.0 * escala
    mordidas: list[Mordida] = []
    en_racha = False
    inicio = 0

    def cerrar(fin: int) -> None:
        tramo = s.deficit[inicio:fin]
        ancho_tramo = fin - inicio
        if (ancho_tramo >= ancho_minimo_px
                and tramo.mean() >= profundidad_minima
                and ancho_tramo / s.ancho <= ANCHO_MAXIMO_MORDIDA_FRAC):
            mordidas.append(Mordida(
                x0_frac=inicio / s.ancho, x1_frac=fin / s.ancho,
                ancho_px=ancho_tramo,
                profundidad_media=float(tramo.mean()),
                profundidad_max=float(tramo.max()),
                area_px=float(tramo.sum()),
            ))

    margen = int(s.ancho * MARGEN_BORDE_FRAC)
    for x in range(margen, s.ancho - margen):
        activo = s.deficit[x] > umbral_ruido
        if activo and not en_racha:
            en_racha, inicio = True, x
        elif not activo and en_racha:
            cerrar(x)
            en_racha = False
    if en_racha:
        cerrar(s.ancho - margen)
    return mordidas


def tiene_mordida_real(s: Suela) -> bool:
    """¿Hay al menos un tramo que es una mordida real, sin importar qué tan
    chico sea su porcentaje del área total?"""
    return len(detectar_mordidas(s)) > 0


def columnas_mordida_por_vecino(s: Suela, vecino_alfa: np.ndarray) -> np.ndarray:
    """Confirma mordida real con el territorio REAL del vecino (ver
    `pasos.py`), no con la forma de la curva.

    `detectar_mordidas` adivina comparando el borde contra una curva de un
    solo arco — y esa curva se confunde con al menos 3 formas distintas de
    calzado real (tacos segmentados, sombra pegada a una curva natural,
    plataformas que suben hacia el empeine; los tres casos medidos el
    2026-08-24). Acá no hay que adivinar: `vecino_alfa` es la máscara real de
    dónde estaba el OTRO calzado detectado en la misma lámina, en las mismas
    coordenadas del canvas — si el vecino ocupaba la franja justo debajo del
    borde actual, ahí SÍ hubo oclusión real, sin importar qué forma tenga la
    suela de este calzado en particular.
    """
    # Acotado a una franja CHICA justo debajo del borde, no hasta el fondo
    # del recorte entero: buscar en todo el bbox capturaba presencia del
    # vecino en zonas que no tienen nada que ver con ESTE borde (otro
    # calzado del grupo, lejos, en la parte de abajo de la foto), marcando
    # columnas sanas como mordida. Usar `_escala(s)` acá daba un margen de
    # ~400px en un recorte grande (bug real medido: básicamente TODO el
    # ancho quedaba "confirmado") — esa escala se calibró para suavizado de
    # línea, no para esto. Ligado a `s.alto` (el porte del calzado) en vez
    # de al ancho del recorte.
    margen_busqueda = max(15, int(round(0.06 * s.alto)))
    marcado = np.zeros(s.ancho, dtype=bool)
    vecino_bin = vecino_alfa >= ALFA_SOLIDO
    for x in range(s.ancho):
        if not s.valido[x]:
            continue
        y_borde = s.y0 + int(round(s.borde[x]))
        y_fin = min(s.y0 + s.alto, y_borde + margen_busqueda)
        if y_fin <= y_borde or y_borde < 0 or y_borde >= vecino_bin.shape[0]:
            continue
        if vecino_bin[y_borde:y_fin, s.x0 + x].any():
            marcado[x] = True
    return marcado


def tiene_mordida_confirmada(s: Suela, vecino_alfa: np.ndarray) -> bool:
    return bool(columnas_mordida_por_vecino(s, vecino_alfa).any())


# ── Relleno con el propio calzado ────────────────────────────────────────────


@dataclass
class Resultado:
    imagen: Image.Image
    faltante_pct_antes: float
    px_rellenados: int
    zona: tuple[float, float]
    notas: list[str] = field(default_factory=list)


def _zona_faltante(s: Suela, vecino_alfa: np.ndarray | None = None, forzar: bool = False,
                    curva_forzar: np.ndarray | None = None) -> np.ndarray:
    """Máscara del hueco: SOLO dentro de una mordida real confirmada.

    `s.deficit > 0` por sí solo no alcanza: con el ajuste robusto, el ruido
    normal del método deja pequeños déficits de 1-3 px dispersos en buena
    parte del contorno (medido en TEST_02: 172 de 352 columnas con algo de
    déficit, contra 25 columnas de la mordida real). Rellenar dondequiera que
    el déficit sea mayor a 0 reparaba de más en todo el contorno —incluida la
    punta del zapato, donde no hay ningún defecto— en vez de tocar solo la
    mordida.

    Con `vecino_alfa` (la máscara real del otro calzado, ver
    `columnas_mordida_por_vecino`) se usa esa confirmación en vez de adivinar
    por la forma de la curva — es más confiable y no se confunde con tacos,
    sombra o plataformas altas. Sin eso, cae al método anterior
    (`detectar_mordidas`) como respaldo.
    """
    falta = np.zeros(s.alfa.shape, np.uint8)

    if vecino_alfa is not None:
        # La curva global (ajustada sobre TODO el ancho) puede quedar más
        # arriba que el borde real justo en la zona de la mordida — pasó de
        # verdad en EL-3232_03: ahí `curva < borde`, así que rellenar hasta
        # `curva` no llenaba nada (el rango queda invertido). Con el vecino ya
        # confirmado, el límite de relleno se interpola LOCAL entre los dos
        # hombros sanos justo afuera del tramo confirmado — no depende de la
        # curva global, que en esta zona puntual no es confiable.
        marcado = columnas_mordida_por_vecino(s, vecino_alfa)
        x = 0
        while x < s.ancho:
            if not marcado[x]:
                x += 1
                continue
            x0 = x
            while x < s.ancho and marcado[x]:
                x += 1
            x1 = x - 1  # tramo confirmado: [x0, x1]

            izq = next((i for i in range(x0 - 1, -1, -1) if s.valido[i] and not marcado[i]), None)
            der = next((i for i in range(x1 + 1, s.ancho) if s.valido[i] and not marcado[i]), None)
            if izq is None and der is None:
                continue
            y_izq = s.borde[izq] if izq is not None else s.borde[der]
            y_der = s.borde[der] if der is not None else s.borde[izq]
            x_izq = izq if izq is not None else der
            x_der = der if der is not None else izq

            for xc in range(x0, x1 + 1):
                if not s.valido[xc]:
                    continue
                t = (xc - x_izq) / (x_der - x_izq) if x_der != x_izq else 0.0
                esperado = y_izq + t * (y_der - y_izq)
                yb = int(round(s.borde[xc]))
                yc = int(round(esperado))
                if yc <= yb:
                    continue
                falta[s.y0 + yb + 1: s.y0 + yc + 1, s.x0 + xc] = 255
        return falta

    if forzar:
        # El usuario ya vio las líneas y decidió a propósito que hay que
        # reparar acá, aunque `detectar_mordidas` no haya confirmado ningún
        # tramo (le pasó de verdad a EL-3232_03: mordida real en el talón,
        # invisible para ese detector). Confiar directo en el déficit medido
        # por columna, sin la exigencia de tramo sostenido.
        #
        # `curva_forzar` (la curva de concavidad única) en vez de `s.curva`
        # (la plana): la plana puede quedar POR DEBAJO del borde real justo
        # en la mordida (curva < borde ahí, deficit=0) — pasó de verdad en
        # EL-3232_03, "forzar" con la curva plana no rellenaba nada en la
        # zona visible del hueco. La de concavidad única es la misma que se
        # dibuja en rojo y sí queda por encima del hueco en toda su extensión.
        curva_usar = curva_forzar if curva_forzar is not None else s.curva
        deficit_usar = np.clip(curva_usar - np.nan_to_num(s.borde), 0, None)
        deficit_usar[~s.valido] = 0.0
        columnas_reales = set(np.where(deficit_usar > 3)[0].tolist())
    else:
        curva_usar = s.curva
        columnas_reales = set()
        for m in detectar_mordidas(s):
            columnas_reales.update(range(int(m.x0_frac * s.ancho), int(m.x1_frac * s.ancho)))
    for x in columnas_reales:
        if not s.valido[x]:
            continue
        yb, yc = int(round(s.borde[x])), int(round(curva_usar[x]))
        if yc <= yb:
            continue
        falta[s.y0 + yb + 1: s.y0 + yc + 1, s.x0 + x] = 255
    return falta


def _color_suela(s: Suela) -> np.ndarray:
    """Color medio del tercio inferior del calzado: la referencia para el exterior."""
    m = s.alfa >= ALFA_SOLIDO
    ys, _ = np.where(m)
    banda = m.copy()
    banda[: ys.min() + int((ys.max() - ys.min()) * 0.60)] = False
    return s.rgb[banda].mean(axis=0) if banda.any() else s.rgb[m].mean(axis=0)


def reparar(path_png: Path, dilatar: int | None = None,
            pluma: float | None = None,
            color_basura: np.ndarray | None = None,
            vecino_alfa: np.ndarray | None = None,
            forzar: bool = False) -> Resultado:
    """Completa la suela ausente difundiendo el color del propio calzado.

    `vecino_alfa`: máscara real de dónde estaba el otro calzado en la misma
    lámina (ver `pasos.py` y `columnas_mordida_por_vecino`) — si se pasa, se
    usa para confirmar la mordida en vez de adivinar por la forma de la
    curva. Sin esto, cae al método anterior (menos confiable, se confunde
    con tacos/sombra/plataformas altas).

    `forzar=True`: rellena todo lo que tenga déficit medido, sin exigir que
    `detectar_mordidas` lo haya confirmado como tramo sostenido — para
    cuando el usuario ya vio la línea roja con sus propios ojos y decidió a
    propósito que hay que reparar, aunque el detector automático no lo haya
    marcado (pasa de verdad: mordidas chicas cerca de una curva natural,
    como el talón, se le escapan al detector).
    """
    s = medir_suela(path_png, color_basura=color_basura)
    curva_forzar = None
    if forzar:
        # La curva plana puede subestimar el hueco justo donde más importa
        # (ver nota larga en `_zona_faltante`) — para decidir SI hay algo que
        # reparar, y para decidir DÓNDE, se usa la de concavidad única.
        s_cu = medir_suela(path_png, color_basura=color_basura, concavidad_unica=True)
        curva_forzar = s_cu.curva
        deficit_cu = np.clip(curva_forzar - np.nan_to_num(s.borde), 0, None)
        deficit_cu[~s.valido] = 0.0
        area_faltante = float(deficit_cu.sum())
    else:
        area_faltante = s.area_faltante
    pct = porcentaje_faltante(s)
    if area_faltante < 50:
        raise ValueError("no hay suela ausente que reparar")
    if pct > MAX_REPARABLE_PCT:
        raise ValueError(f"falta demasiada suela ({pct:.0f}%): conseguí otra foto, "
                         f"rellenar esto sería inventar producto")

    escala = _escala(s)
    if dilatar is None:
        dilatar = max(1, int(round(DILATAR_MASCARA * escala)))
    if pluma is None:
        pluma = max(1.0, PLUMA_TRANSICION * escala)

    notas: list[str] = []
    falta = _zona_faltante(s, vecino_alfa, forzar=forzar, curva_forzar=curva_forzar)
    rgb, alfa = s.rgb.copy(), s.alfa.copy()

    # Máscara dilatada: además del hueco, el filo viejo que crea la línea falsa.
    if dilatar:
        masc = cv2.dilate(falta, np.ones((dilatar * 2 + 1,) * 2, np.uint8))
        notas.append(f"máscara dilatada {dilatar} px para borrar el filo original")
    else:
        masc = falta.copy()

    # El exterior se pinta con el color de la suela para que el inpaint difunda
    # desde el producto y no arrastre el blanco del fondo.
    base = rgb.copy()
    base[(alfa < ALFA_SOLIDO) & (masc == 0)] = _color_suela(s)
    relleno = cv2.inpaint(base, masc, 6, cv2.INPAINT_TELEA)

    # Transición suave: peso 1 en el centro del hueco, cayendo hacia el original.
    if pluma:
        dist = cv2.distanceTransform((masc > 0).astype(np.uint8), cv2.DIST_L2, 5)
        peso = np.clip(dist / pluma, 0, 1)[..., None]
        notas.append(f"transición de {pluma} px sobre el material original")
    else:
        peso = (masc > 0).astype(np.float32)[..., None]

    rgb_final = (relleno * peso + rgb * (1 - peso)).astype(np.uint8)

    alfa_final = np.maximum(alfa, (falta > 0).astype(np.uint8) * 255)
    vecino = cv2.dilate((alfa >= ALFA_SOLIDO).astype(np.uint8), np.ones((3, 3), np.uint8))
    alfa_final = np.maximum(alfa_final, ((masc > 0) & (vecino > 0)).astype(np.uint8) * 255)

    # Antialiasing mínimo (1 px), NO el blur de 3x3 que había antes: un corte
    # binario puro en un borde diagonal deja "ranuras" en escalón (el mismo
    # escalonado que ya se corrigió en otras partes del pipeline esta sesión).
    # 1 px de antialiasing real no es "difuminar" — es lo mismo que trae de
    # forma natural cualquier borde de BiRefNet en el resto del contorno.
    suave = cv2.GaussianBlur(alfa_final.astype(np.float32), (0, 0), sigmaX=1.2)
    frontera = (alfa_final > 0) & (alfa == 0)
    alfa_final = alfa_final.astype(np.float32)
    alfa_final[frontera] = suave[frontera]
    alfa_final = alfa_final.astype(np.uint8)

    rellenados = int((alfa_final >= ALFA_SOLIDO).sum() - (alfa >= ALFA_SOLIDO).sum())
    img = Image.fromarray(np.dstack([rgb_final, alfa_final]), mode="RGBA")
    return Resultado(img, pct, max(rellenados, 0), s.zona, notas)


# ── Aplicar, revertir, bitácora ──────────────────────────────────────────────


def aplicar(salida: Path, recorte: str, res: Resultado, calidad_jpg: int = 92) -> dict:
    """Escribe la imagen reparada, respaldando antes el original sin retocar."""
    salida = Path(salida)
    png = salida / "transparente" / f"{recorte}.png"
    jpg = salida / "blanco" / f"{recorte}.jpg"
    respaldo = salida / CARPETA_ORIGINALES
    respaldo.mkdir(parents=True, exist_ok=True)

    guardados = {}
    for origen in (png, jpg):
        if origen.exists():
            destino = respaldo / origen.name
            if not destino.exists():
                destino.write_bytes(origen.read_bytes())
            guardados[origen.suffix] = str(destino)

    res.imagen.save(png, compress_level=6)
    fondo = Image.new("RGB", res.imagen.size, (255, 255, 255))
    fondo.paste(res.imagen, mask=res.imagen.split()[3])
    fondo.save(jpg, quality=calidad_jpg, subsampling=0, progressive=True)
    registrar(salida, recorte, res, guardados)
    return guardados


def aplicar_recorte_excedente(salida: Path, recorte: str,
                               color_basura: np.ndarray | None = COLOR_BASURA,
                               calidad_jpg: int = 92) -> dict:
    """Corre `recortar_excedente` sobre el PNG del catálogo, respaldando antes
    el original — mismo patrón que `aplicar()`, pero para la operación
    opuesta (recortar sombra/follaje en vez de rellenar una mordida)."""
    salida = Path(salida)
    png = salida / "transparente" / f"{recorte}.png"
    jpg = salida / "blanco" / f"{recorte}.jpg"
    if not png.exists():
        raise ValueError(f"no encuentro {png}")
    respaldo = salida / CARPETA_ORIGINALES
    respaldo.mkdir(parents=True, exist_ok=True)

    guardados = {}
    for origen in (png, jpg):
        if origen.exists():
            destino = respaldo / origen.name
            if not destino.exists():
                destino.write_bytes(origen.read_bytes())
            guardados[origen.suffix] = str(destino)

    resultado = recortar_excedente(png, color_basura=color_basura)
    if resultado["px_recortados"]:
        imagen = Image.open(png).convert("RGBA")
        fondo = Image.new("RGB", imagen.size, (255, 255, 255))
        fondo.paste(imagen, mask=imagen.split()[3])
        fondo.save(jpg, quality=calidad_jpg, subsampling=0, progressive=True)

        almacen.registrar_reparacion(
            salida, recorte, "recorte_excedente", respaldos=guardados,
            px_recortados=resultado["px_recortados"],
            px_rellenados=resultado["px_rellenados"])

    return resultado


def revertir(salida: Path, recorte: str) -> bool:
    salida = Path(salida)
    ok = False
    for sub, ext in (("transparente", ".png"), ("blanco", ".jpg")):
        orig = salida / CARPETA_ORIGINALES / f"{recorte}{ext}"
        if orig.exists():
            (salida / sub / f"{recorte}{ext}").write_bytes(orig.read_bytes())
            ok = True
    return ok


def registrar(salida: Path, recorte: str, res: Resultado,
              respaldos: dict | None = None) -> None:
    almacen.registrar_reparacion(
        salida, recorte, "inpaint_propio", respaldos=respaldos or {},
        faltante_pct=round(res.faltante_pct_antes, 2),
        px_rellenados=res.px_rellenados,
        zona_ancho=[round(v, 3) for v in res.zona],
        notas=res.notas)


def purgar_stem(salida: Path, stem: str) -> int:
    """Borra las entradas de reparación de una lámina que se va a reprocesar.

    La bitácora es de solo agregar: si `RP9300_02` ya estaba reparado y la lámina
    se corre de nuevo, el worker sobrescribe el PNG con uno fresco sin reparar,
    pero la bitácora seguía diciendo "reparado" del intento anterior. La
    interfaz mostraba "completada" sobre una imagen que en los hechos no lo
    estaba — pasó de verdad con `RP9300_02` tras reprocesar la lámina.

    También borra el respaldo en `_originales/`: corresponde a los píxeles de la
    corrida vieja, y guardarlo dejaría revertir a un archivo que no tiene relación
    con el recorte actual.
    """
    borrados, respaldos = almacen.purgar_reparaciones_stem(salida, stem)
    for ruta in respaldos:
        Path(ruta).unlink(missing_ok=True)
    return borrados


def reparados(salida: Path) -> dict[str, dict]:
    return almacen.reparaciones_vigentes(salida)
