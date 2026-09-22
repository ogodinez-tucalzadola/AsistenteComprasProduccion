"""
Detecta sombra/artefacto pegado al borde comparando el mismo modelo entre sus
propios colores, en vez de intentar clasificarlo por reglas de color sobre una
sola foto (eso ya se probó tres veces y no separa sombra de transiciones
legítimas de color del producto — ver limpiar_fondo.py y el diagnóstico
descartado).

La idea, a partir de un caso real (RP9300): los 4 colores de una misma lámina
comparten pose porque son la misma sesión de foto. Si una zona del contorno se
extiende con una cola semitransparente en UN color y no en los otros 3, esa
cola no es parte del producto —ningún molde cambia de forma entre colores— es
un artefacto de esa foto puntual (sombra, reflejo). Si la cola aparece en los
4 colores en la misma posición relativa, es un elemento real del diseño
(ventana de aire, textura translúcida) y no se toca.

Solo compara recortes que vengan de la MISMA lámina (mismo `origen` en el
manifiesto): no intenta alinear ángulos distintos de sesiones de foto
distintas, ese es un problema no resuelto y deliberadamente fuera de esto.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

RESOLUCION = 200          # puntos para comparar perfiles de distinto ancho
MARGEN_COLA_PX = 4.0      # cuanto mas larga que el resto del grupo para ser candidata
MIN_TRAMO = 3             # puntos consecutivos (de RESOLUCION) para no ser ruido


def _perfil_cola(png: Path, resolucion: int = RESOLUCION) -> np.ndarray | None:
    """Por cada columna del contorno, cuánto se extiende alfa baja (0.05-0.6)
    más allá del material sólido (alfa>=0.9). Remuestreado a una resolución
    común para poder comparar siluetas de distinto ancho en px."""
    im = np.array(Image.open(png).convert("RGBA")).astype(np.float32)
    alfa = im[..., 3] / 255.0
    solido = alfa >= 0.9
    if not solido.any():
        return None

    ys, xs = np.where(solido)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    rec = alfa[y0:y1 + 1, x0:x1 + 1]
    alto, ancho = rec.shape

    cola = np.zeros(ancho, dtype=np.float32)
    for x in range(ancho):
        col = rec[:, x]
        idx_solido = np.where(col >= 0.9)[0]
        if len(idx_solido) == 0:
            continue
        y = idx_solido.max() + 1
        extra = 0
        while y < alto and 0.05 < col[y] < 0.6:
            extra += 1
            y += 1
        cola[x] = extra

    xs_frac = np.linspace(0, 1, ancho)
    xs_comun = np.linspace(0, 1, resolucion)
    return np.interp(xs_comun, xs_frac, cola)


@dataclass
class Sospecha:
    recorte: str
    x0_frac: float
    x1_frac: float
    exceso_px: float


def comparar_grupo(pngs: list[Path], margen: float = MARGEN_COLA_PX,
                    min_tramo: int = MIN_TRAMO) -> dict[str, list[Sospecha]]:
    """Compara los perfiles de "cola" entre hermanos de la misma lámina.

    Devuelve, por recorte, los tramos donde ese color se extiende
    consistentemente más que la mediana del grupo — candidatos a sombra/
    artefacto exclusivo de esa foto.
    """
    perfiles = {png: _perfil_cola(png) for png in pngs}
    validos = {p: v for p, v in perfiles.items() if v is not None}
    if len(validos) < 2:
        return {}

    matriz = np.stack(list(validos.values()))
    mediana = np.median(matriz, axis=0)

    resultado: dict[str, list[Sospecha]] = {}
    for png, perfil in validos.items():
        exceso = perfil - mediana
        candidato = (exceso > margen).astype(np.uint8).reshape(1, -1)
        n, etiquetas, stats, _ = cv2.connectedComponentsWithStats(candidato, 8)
        tramos = []
        for i in range(1, n):
            ancho_tramo = stats[i, cv2.CC_STAT_WIDTH]
            if ancho_tramo < min_tramo:
                continue
            x0 = stats[i, cv2.CC_STAT_LEFT]
            x1 = x0 + ancho_tramo
            tramos.append(Sospecha(
                recorte=png.name,
                x0_frac=x0 / len(mediana), x1_frac=x1 / len(mediana),
                exceso_px=float(exceso[x0:x1].mean()),
            ))
        if tramos:
            resultado[png.name] = tramos
    return resultado


# ── Color del material sólido en el borde ────────────────────────────────────
#
# El primer intento (arriba, `comparar_grupo`) mide cuánto se extiende la
# transparencia más allá del material sólido. Falló en el caso real (RP9300):
# la sombra ahí no está en la transparencia, está DENTRO de píxeles que
# BiRefNet marcó como 100% sólidos — el matting fusionó la sombra de contacto
# con el piso como si fuera parte opaca del zapato. Por eso hay que comparar el
# COLOR de los últimos píxeles sólidos antes del borde, no cuánta transparencia
# hay después de ellos.

GROSOR_MUESTRA_PX = 4     # cuántos píxeles sólidos junto al borde se promedian
MARGEN_DELTAE_BORDE = 25.0
MIN_TRAMO_BORDE = 3       # puntos consecutivos (de RESOLUCION) para no ser ruido


def _perfil_color_borde(png: Path, resolucion: int = RESOLUCION,
                         grosor: int = GROSOR_MUESTRA_PX) -> np.ndarray | None:
    """Color (Lab) de los últimos `grosor` píxeles sólidos antes del borde
    inferior, por columna, remuestreado a una resolución común."""
    im = np.array(Image.open(png).convert("RGBA")).astype(np.float32)
    alfa = im[..., 3] / 255.0
    rgb = im[..., :3]
    solido = alfa >= 0.9
    if not solido.any():
        return None

    ys, xs = np.where(solido)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    rec_solido = solido[y0:y1 + 1, x0:x1 + 1]
    rec_rgb = rgb[y0:y1 + 1, x0:x1 + 1]
    alto, ancho = rec_solido.shape

    lab_rec = cv2.cvtColor(rec_rgb.astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)

    perfil = np.full((ancho, 3), np.nan, dtype=np.float32)
    for x in range(ancho):
        idx = np.where(rec_solido[:, x])[0]
        if len(idx) == 0:
            continue
        fin = idx.max()
        ini = max(0, fin - grosor + 1)
        perfil[x] = lab_rec[ini:fin + 1, x].mean(axis=0)

    valido = ~np.isnan(perfil[:, 0])
    if valido.sum() < 2:
        return None
    xs_frac = np.linspace(0, 1, ancho)
    xs_comun = np.linspace(0, 1, resolucion)
    return np.stack([
        np.interp(xs_comun, xs_frac[valido], perfil[valido, c]) for c in range(3)
    ], axis=-1)


@dataclass
class SospechaColor:
    recorte: str
    x0_frac: float
    x1_frac: float
    deltaE_medio: float


# Por debajo de esto, aunque un zapato sea outlier del grupo, la desviación es
# tan chica frente a SU PROPIO color de borde que no vale la pena marcarla.
MIN_AUTODESVIO_PX = 20.0

# Cuánto más debe desviarse un zapato de sí mismo, comparado con el resto del
# grupo en ese mismo punto, para contar como exclusivo de esa foto.
MARGEN_RELATIVO = 15.0


def comparar_color_borde(pngs: list[Path], min_autodesvio: float = MIN_AUTODESVIO_PX,
                          margen_relativo: float = MARGEN_RELATIVO,
                          min_tramo: int = MIN_TRAMO_BORDE
                          ) -> dict[str, list[SospechaColor]]:
    """Compara, entre hermanos de la misma lámina, cuánto se aparta cada uno
    de SU PROPIO color habitual de borde — no el color absoluto entre sí.

    Comparar color absoluto entre colores distintos del mismo modelo no sirve:
    un beige y un celeste difieren en casi todo el borde porque son colores
    distintos, no porque uno tenga un defecto. Lo que sí es comparable entre
    colores es la forma del perfil: cuánto se aparta cada punto del borde del
    color típico de ESE MISMO zapato en otras partes de su propio contorno. Un
    accesorio de diseño (costura, logo, taco) hace que TODOS los colores se
    aparten de sí mismos en la misma posición relativa; una sombra fusionada
    por el matting hace que solo UN color se aparte ahí, y los demás no.
    """
    perfiles = {png: _perfil_color_borde(png) for png in pngs}
    validos = {p: v for p, v in perfiles.items() if v is not None}
    if len(validos) < 2:
        return {}

    # Autodesvío: qué tan lejos está cada punto del color mediano de SU PROPIO
    # borde (no del de los demás). Esto ya es comparable entre colores.
    autodesvios = {}
    for png, perfil in validos.items():
        propio = np.median(perfil, axis=0)
        autodesvios[png] = np.sqrt(((perfil - propio) ** 2).sum(axis=-1))

    matriz = np.stack(list(autodesvios.values()))  # (n, resolucion)

    resultado: dict[str, list[SospechaColor]] = {}
    nombres = list(autodesvios.keys())
    for i, png in enumerate(nombres):
        propio_autodesvio = matriz[i]
        otros = np.delete(matriz, i, axis=0)
        mediana_otros = np.median(otros, axis=0) if len(otros) else np.zeros_like(propio_autodesvio)

        exclusivo = ((propio_autodesvio > min_autodesvio)
                     & (propio_autodesvio - mediana_otros > margen_relativo))
        candidato = exclusivo.astype(np.uint8).reshape(1, -1)
        n, etiquetas, stats, _ = cv2.connectedComponentsWithStats(candidato, 8)
        tramos = []
        for j in range(1, n):
            ancho_tramo = stats[j, cv2.CC_STAT_WIDTH]
            if ancho_tramo < min_tramo:
                continue
            x0 = stats[j, cv2.CC_STAT_LEFT]
            x1 = x0 + ancho_tramo
            tramos.append(SospechaColor(
                recorte=png.name,
                x0_frac=x0 / matriz.shape[1], x1_frac=x1 / matriz.shape[1],
                deltaE_medio=float(propio_autodesvio[x0:x1].mean()),
            ))
        if tramos:
            resultado[png.name] = tramos
    return resultado


if __name__ == "__main__":
    import sys
    pngs = [Path(a) for a in sys.argv[1:]]

    print("== Extensión de transparencia (cola) ==")
    res = comparar_grupo(pngs)
    if not res:
        print("Sin candidatos.")
    for nombre, tramos in res.items():
        for t in tramos:
            print(f"{nombre}: tramo {t.x0_frac*100:.0f}%-{t.x1_frac*100:.0f}% del ancho, "
                  f"{t.exceso_px:.1f}px mas que la mediana del grupo")

    print("\n== Color del borde sólido ==")
    res2 = comparar_color_borde(pngs)
    if not res2:
        print("Sin candidatos.")
    for nombre, tramos in res2.items():
        for t in tramos:
            print(f"{nombre}: tramo {t.x0_frac*100:.0f}%-{t.x1_frac*100:.0f}% del ancho, "
                  f"deltaE medio={t.deltaE_medio:.1f}")
