"""
Corrige sombra fusionada por el matting dentro de píxeles sólidos, usando los
demás colores del mismo modelo (misma lámina) como referencia para CONFIRMAR
que hay un problema — nunca para copiar su color. El relleno usa siempre
material propio del mismo recorte, igual que `reparar.py` con la suela.

Flujo:
1. `comparar_colores.comparar_color_borde` mide, por recorte, cuánto se aparta
   cada tramo del borde de su propio color habitual.
2. Solo se actúa si, dentro del grupo (2+ colores de la misma lámina), un
   recorte se despega con claridad del resto (extensión total marcada muy por
   encima del siguiente más alto). Sin grupo, o sin un outlier claro, no se
   toca nada — no hay con qué confirmar que es sombra y no diseño.
3. Sobre el recorte marcado, se construye una máscara de contaminación
   columna por columna: desde el borde inferior hacia arriba, mientras el
   color siga apartado de una referencia sana tomada más arriba en la MISMA
   columna del MISMO recorte.
4. Esa máscara se repara con inpainting usando solo píxeles propios (nunca de
   otro color), con el mismo patrón ya validado en `reparar.py`: dilatar para
   borrar el filo viejo, y plumeado para que el empalme no se note.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import comparar_colores as cc

# Extensión mínima (fracción del contorno) para que un outlier valga la pena
# tocar; por debajo de esto es ruido normal de diseño, no sombra.
EXTENSION_MINIMA = 0.15

# Cuántas veces más marcado debe estar el peor recorte que el segundo del
# grupo para considerarlo un outlier real y no una variación de contraste
# entre colores (como el panel de dos tonos que sí se repite en todos).
RAZON_MINIMA = 1.8

REF_ALTO_PX = 20     # cuanto mas arriba, en la misma columna, se toma la referencia sana
REF_SEPARACION_PX = 15
DELTAE_CONTAMINACION = 25.0

# La varianza sola no distingue bien: una suela blanca curva tiene brillo
# natural (variación real, sin ser textura) y con un umbral de varianza se
# terminaba bloqueando sombra real junto con el relieve. La señal que sí
# separa ambos casos es la FORMA del perfil: una sombra es un solo cambio
# sostenido en una dirección (se oscurece progresivamente hacia el borde); el
# relieve de un taco sube y baja varias veces (raya clara, oscura, clara...).
# Se cuenta cuántas veces cambia de signo la derivada del canal L dentro de la
# ventana de referencia; por encima de esto se considera un patrón periódico
# (textura de diseño) y la columna se descarta entera, sin importar su
# varianza.
MAX_CAMBIOS_DE_SIGNO = 4

DILATAR_MASCARA = 3
PLUMA_TRANSICION = 4


@dataclass
class Correccion:
    recorte: str
    aplicada: bool
    motivo: str
    px_corregidos: int = 0


def _mascara_contaminacion(rgb_rec: np.ndarray, solido_rec: np.ndarray,
                            columnas: range) -> np.ndarray:
    alto, ancho = solido_rec.shape
    lab = cv2.cvtColor(rgb_rec.astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    mascara = np.zeros((alto, ancho), np.uint8)

    for x in columnas:
        if x < 0 or x >= ancho:
            continue
        idx = np.where(solido_rec[:, x])[0]
        if len(idx) == 0:
            continue
        arriba, fin = idx.min(), idx.max()

        # Patrón periódico (relieve, rayas) vs. cambio sostenido (sombra): se
        # cuenta cuántas veces cambia de dirección el canal L, pero SOLO en
        # una ventana pegada al borde (no en toda la columna). Analizar la
        # columna completa mezclaba materiales reales del calzado (el cuerpo
        # arriba, la suela blanca abajo) con la sombra en la punta — esa
        # transición material-a-material ya cuenta como un cambio de signo
        # legítimo antes de llegar a la sombra, y hacía descartar columnas con
        # sombra real solo por pasar antes por un material distinto. Cerca
        # del borde ya se está dentro de un solo material (la suela): un taco
        # con relieve alterna varias veces ahí mismo, una sombra no.
        ventana_textura = fin - arriba
        ventana_textura = min(ventana_textura, 40)
        columna_L = lab[fin - ventana_textura:fin + 1, x, 0]
        if len(columna_L) >= 6:
            suave = cv2.GaussianBlur(columna_L.reshape(-1, 1), (1, 5), 0).ravel()
            deriv = np.diff(suave)
            deriv[np.abs(deriv) < 3.0] = 0.0
            signos = deriv[deriv != 0]
            signos = np.sign(signos)
            cambios = int((np.diff(signos) != 0).sum()) if len(signos) > 1 else 0
            if cambios > MAX_CAMBIOS_DE_SIGNO:
                continue  # patrón periódico: no tocar esta columna

        ref_fin = fin - REF_SEPARACION_PX
        ref_ini = ref_fin - REF_ALTO_PX
        ref_ini, ref_fin = max(arriba, ref_ini), max(arriba, ref_fin)
        if ref_fin <= ref_ini:
            continue
        referencia = lab[ref_ini:ref_fin, x].mean(axis=0)

        y = fin
        while y >= arriba:
            d = float(np.sqrt(((lab[y, x] - referencia) ** 2).sum()))
            if d <= DELTAE_CONTAMINACION:
                break
            mascara[y, x] = 1
            y -= 1
    return mascara


def _reparar_region(rgb_rec: np.ndarray, mascara: np.ndarray) -> np.ndarray:
    if mascara.sum() == 0:
        return rgb_rec
    ancha = cv2.dilate(mascara, np.ones((DILATAR_MASCARA * 2 + 1,) * 2, np.uint8))
    reparado = cv2.inpaint(rgb_rec.astype(np.uint8), ancha * 255, 5, cv2.INPAINT_TELEA)

    dist = cv2.distanceTransform(1 - ancha, cv2.DIST_L2, 5)
    peso = np.clip(dist / PLUMA_TRANSICION, 0.0, 1.0)[..., None]
    return (reparado.astype(np.float32) * (1 - peso) + rgb_rec.astype(np.float32) * peso).astype(np.uint8)


def corregir_grupo(pngs: list[Path]) -> dict[str, Correccion]:
    """Revisa un grupo de colores de la misma lámina y corrige, en el disco,
    solo el/los recortes que se confirmen como outliers claros."""
    resultado: dict[str, Correccion] = {}

    if len(pngs) < 2:
        for p in pngs:
            resultado[p.name] = Correccion(p.name, False, "sin grupo para comparar")
        return resultado

    tramos_por_recorte = cc.comparar_color_borde(pngs)
    extension = {
        p.name: sum(t.x1_frac - t.x0_frac for t in tramos_por_recorte.get(p.name, []))
        for p in pngs
    }
    ordenado = sorted(extension.items(), key=lambda kv: kv[1], reverse=True)
    peor_nombre, peor_ext = ordenado[0]
    segundo_ext = ordenado[1][1] if len(ordenado) > 1 else 0.0

    es_outlier = (peor_ext >= EXTENSION_MINIMA
                  and (segundo_ext == 0 or peor_ext / segundo_ext >= RAZON_MINIMA))

    for p in pngs:
        if p.name != peor_nombre or not es_outlier:
            motivo = "no es outlier del grupo" if p.name == peor_nombre else "no es el outlier del grupo"
            resultado[p.name] = Correccion(p.name, False, motivo)
            continue

        im = np.array(Image.open(p).convert("RGBA"))
        alfa_full = im[..., 3]
        solido_full = alfa_full >= 230
        ys, xs = np.where(solido_full)
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        rgb_rec = im[y0:y1 + 1, x0:x1 + 1, :3].copy()
        solido_rec = solido_full[y0:y1 + 1, x0:x1 + 1]
        ancho_rec = solido_rec.shape[1]

        # Los tramos de `comparar_color_borde` ya sirvieron para CONFIRMAR que
        # este recorte es el outlier del grupo; para definir el área exacta a
        # reparar se recorre TODO su contorno con el mismo criterio columna
        # por columna (más preciso que los tramos, que vienen remuestreados a
        # una resolución fija y pueden partir una zona contaminada continua en
        # pedazos con huecos entre sí).
        mascara = _mascara_contaminacion(rgb_rec, solido_rec, range(ancho_rec))
        px = int(mascara.sum())
        if px == 0:
            resultado[p.name] = Correccion(p.name, False, "outlier detectado pero sin píxeles a corregir")
            continue

        rgb_reparado = _reparar_region(rgb_rec, mascara)
        im[y0:y1 + 1, x0:x1 + 1, :3] = rgb_reparado
        Image.fromarray(im, "RGBA").save(p)
        resultado[p.name] = Correccion(p.name, True, "outlier confirmado contra el grupo", px)

    return resultado


if __name__ == "__main__":
    import sys
    pngs = [Path(a) for a in sys.argv[1:]]
    for nombre, c in corregir_grupo(pngs).items():
        estado = f"CORREGIDO ({c.px_corregidos}px)" if c.aplicada else "sin cambios"
        print(f"{nombre}: {estado} — {c.motivo}")
