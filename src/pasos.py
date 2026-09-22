"""
Procesamiento por etapas, para que la interfaz pueda mostrar el avance real.

Zawa resuelve una lámina completa dentro de `process_image()` y devuelve el
resultado al final: sirve para un lote, pero no deja ver nada intermedio. Acá se
llama a **las mismas funciones de Zawa, en el mismo orden**, avisando después de
cada etapa para que la interfaz muestre la foto cargada, las cajas detectadas y
cada recorte a medida que sale.

Es orquestación duplicada, no lógica duplicada: la detección, la separación por
instancia, el matting y la normalización siguen siendo las de Zawa. Si Zawa
cambia el orden de sus etapas, esto hay que actualizarlo — el precio de poder
mostrar el proceso.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

import limpiar_fondo

CARPETA_PASOS = "_pasos"


def _alfa_del_origen(path: Path) -> np.ndarray | None:
    """Canal alfa del archivo, con la misma orientación que usa el pipeline.

    Zawa aplica `exif_transpose` al cargar; hay que aplicarlo igual o el alfa
    queda rotado respecto del RGB.
    """
    from PIL import ImageOps

    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGBA", "LA") and "transparency" not in im.info:
                return None
            return np.array(im.convert("RGBA"))[..., 3].astype(np.float32) / 255.0
    except Exception:  # noqa: BLE001
        return None


def procesar_lamina(
    path: Path,
    detector,
    matter,
    instancer,
    cfg,
    dirs: dict[str, Path],
    z: dict,
    avisar: Callable[..., None],
    limpiar_fondo_activo: bool = True,
    erosion_fondo: int | None = None,
) -> dict:
    """Procesa una lámina avisando etapa por etapa.

    `avisar(**campos)` recibe cada evento. `z` es el paquete de módulos de Zawa
    que devuelve nucleo.importar_zawa().
    """
    zrun = z["run"]
    compose = z["compose"]
    layout = z["layout"]
    qa = z["qa"]

    t_inicio = time.perf_counter()
    salida_raiz = dirs["transparente"].parent
    dir_pasos = salida_raiz / CARPETA_PASOS
    dir_pasos.mkdir(parents=True, exist_ok=True)

    # ── 1. Cargar ────────────────────────────────────────────────────────────
    imagen = zrun.load_rgb(path)
    rgb_full = np.array(imagen)
    # Si la foto ya viene recortada por otro programa, su canal alfa ES la silueta
    # correcta. Medido contra una sandalia ya recortada: volver a segmentarla con
    # BiRefNet pierde el 7.1 % de la silueta real, una franja de 2-4 px en todo el
    # contorno. Usar el alfa que ya está en el archivo es exacto y además salta el
    # matting, que es el 80 % del tiempo.
    origen_limpio = limpiar_fondo.ya_sin_fondo(path)
    alfa_origen = _alfa_del_origen(path) if origen_limpio else None
    if alfa_origen is not None and alfa_origen.shape != rgb_full.shape[:2]:
        alfa_origen = None  # desalineado: mejor no usarlo que usarlo mal
    avisar(paso="cargada", archivo=path.name, ancho=imagen.width, alto=imagen.height,
           ruta=str(path), ya_sin_fondo=int(origen_limpio),
           usa_alfa_origen=int(alfa_origen is not None))

    # ── 2. Detectar ──────────────────────────────────────────────────────────
    t = time.perf_counter()
    detecciones = detector(imagen)
    cajas = np.array([d.box for d in detecciones], dtype=np.float32)
    orden = layout.order_by_reading_position(cajas) if len(detecciones) else []
    ms_det = int((time.perf_counter() - t) * 1000)

    overlay_path = ""
    if detecciones:
        overlay = zrun.draw_overview(imagen, detecciones, orden)
        overlay_path = str(dir_pasos / f"{path.stem}_cajas.jpg")
        overlay.save(overlay_path, quality=88)
    avisar(paso="detectada", cajas=len(detecciones), ms=ms_det, overlay=overlay_path)

    if not detecciones:
        registro = {
            "origen": str(path), "tamano": [imagen.width, imagen.height],
            "detecciones": 0, "matting": matter.name,
            "revisar": qa.check_image(0, cfg), "recortes": [],
        }
        registro["revision_manual"] = True
        registro["ms"] = int((time.perf_counter() - t_inicio) * 1000)
        avisar(paso="lista", recortes=0, revisar=1)
        return registro

    # ── 3. Separar instancias ────────────────────────────────────────────────
    t = time.perf_counter()
    if instancer:
        instancias = instancer(imagen, [d.box for d in detecciones])
    else:
        instancias = np.zeros((len(detecciones), imagen.height, imagen.width), dtype=bool)
    avisar(paso="separadas", piezas=len(instancias),
           ms=int((time.perf_counter() - t) * 1000))

    registro = {
        "origen": str(path),
        "tamano": [imagen.width, imagen.height],
        "detecciones": len(detecciones),
        "matting": "alfa_del_origen" if alfa_origen is not None else matter.name,
        "revisar": qa.check_image(len(detecciones), cfg),
        "recortes": [],
    }

    # ── 4. Limpiar recorte por recorte ───────────────────────────────────────
    for posicion, indice in enumerate(orden, start=1):
        t = time.perf_counter()
        avisar(paso="limpiando", i=posicion, de=len(orden))

        deteccion = detecciones[indice]
        x1, y1, x2, y2 = compose.expand_box(
            deteccion.box, imagen.width, imagen.height, cfg.box_margin)
        crop_rgb = rgb_full[y1:y2, x1:x2]
        if crop_rgb.size == 0:
            continue

        if alfa_origen is not None:
            # La silueta ya venía en el archivo: no se re-segmenta.
            alpha = alfa_origen[y1:y2, x1:x2].copy()
        else:
            alpha = matter(Image.fromarray(crop_rgb))

        if len(instancias) > 1:
            propio = compose.ownership_mask(instancias, indice, cfg.neighbor_feather)
            alpha = alpha * propio[y1:y2, x1:x2]

        # Territorio REAL de los demás calzados detectados en esta misma
        # lámina, recortado igual que `alpha` — para saber después, sin
        # adivinar por la forma del contorno, qué parte de la suela faltante
        # es porque otro par estaba encima (mordida real) y no por sombra, un
        # taco segmentado o una plataforma que sube (ver conversación del
        # 2026-08-24: la curva de un solo arco confunde estos 3 casos).
        vecinos_full = None
        if len(instancias) > 1:
            vecinos_full = np.zeros(instancias.shape[1:], dtype=bool)
            for j in range(len(instancias)):
                if j != indice:
                    vecinos_full |= instancias[j]

        caja_en_recorte = (int(deteccion.box[0]) - x1, int(deteccion.box[1]) - y1,
                           int(deteccion.box[2]) - x1, int(deteccion.box[3]) - y1)
        alpha, vecino = compose.isolate_target(alpha, caja_en_recorte, cfg.isolate_feather)

        # Descartar la franja de contorno donde el fondo se mezcló con el
        # producto. Va ANTES de medir o recortar: si no, el recorte queda ajustado
        # a la sombra y todo lo que se mida después arrastra el error.
        quitado_fondo = 0
        if limpiar_fondo_activo and not origen_limpio:
            alpha, quitado_fondo = limpiar_fondo.limpiar_alfa(
                crop_rgb, alpha,
                erosion=limpiar_fondo.EROSION_PX if erosion_fondo is None
                else erosion_fondo)

        # Afinado del filo: va siempre, incluso con alfa del origen, porque los
        # desenfoques que aplica Zawa al separar instancias también lo ablandan.
        alpha, motas = limpiar_fondo.afinar_borde(alpha)
        quitado_fondo += motas

        cobertura = float((alpha >= cfg.alpha_threshold).mean())

        bbox = compose.tight_bbox(alpha, cfg.alpha_threshold)
        if bbox is None:
            registro["recortes"].append({
                "indice": posicion, "revisar": ["matting vacío"],
                "score": deteccion.score})
            avisar(paso="recorte", i=posicion, de=len(orden), archivo="",
                   revisar=1, ms=int((time.perf_counter() - t) * 1000), vacio=1)
            continue

        tx1, ty1, tx2, ty2 = bbox
        crop_rgb = crop_rgb[ty1:ty2, tx1:tx2]
        crop_alpha = alpha[ty1:ty2, tx1:tx2]

        # El despill quita el color del fondo que se coló en el borde. Si la foto
        # ya venía recortada no hay tal contaminación, y aplicarlo solo agregaría
        # artefactos sobre un borde que ya estaba bien.
        if cfg.despill_radius and alfa_origen is None:
            crop_rgb = compose.decontaminate(crop_rgb, crop_alpha, cfg.despill_radius)

        rgba = compose.to_canvas(crop_rgb, crop_alpha, cfg)
        stem = f"{path.stem}_{posicion:02d}"

        # Guardar dónde está el vecino real, en las mismas coordenadas del PNG
        # final — con esto `reparar.py` puede confirmar una mordida real en
        # vez de adivinarla por la forma de la curva (que se confunde con
        # tacos, sombra o plataformas que suben). Vacío si no hay más de un
        # calzado en la lámina: no hay con qué haber tapado nada.
        ruta_vecino = ""
        if vecinos_full is not None:
            vecino_crop = vecinos_full[y1:y2, x1:x2][ty1:ty2, tx1:tx2]
            if vecino_crop.any():
                relleno = np.dstack([vecino_crop.astype(np.uint8) * 255] * 3)
                vecino_canvas = compose.to_canvas(relleno, vecino_crop.astype(np.float32), cfg)
                dir_vecinos = salida_raiz / "_vecinos"
                dir_vecinos.mkdir(parents=True, exist_ok=True)
                ruta_vecino = str(dir_vecinos / f"{stem}.png")
                vecino_canvas.split()[3].save(ruta_vecino)  # solo el canal alfa, en gris
        entrada_vecino_mascara = ruta_vecino

        entrada = {
            "indice": posicion,
            "score": round(deteccion.score, 3),
            "etiqueta": deteccion.label,
            "caja": [x1, y1, x2, y2],
            "cobertura_alfa": round(cobertura, 3),
            "vecino_quitado": vecino,
            "vecino_mascara": entrada_vecino_mascara,
            "fondo_quitado_px": quitado_fondo,
            "colores": compose.dominant_colors(crop_rgb, crop_alpha),
            "revisar": qa.check_crop(deteccion, cobertura, (tx2 - tx1, ty2 - ty1),
                                     (imagen.width, imagen.height), vecino, cfg),
        }

        vista = ""
        if cfg.background in ("transparent", "both"):
            destino = dirs["transparente"] / f"{stem}.png"
            rgba.save(destino, compress_level=cfg.png_compress_level)
            entrada["png"] = str(destino)
            vista = str(destino)
        if cfg.background in ("white", "both"):
            destino = dirs["blanco"] / f"{stem}.jpg"
            compose.flatten_on_white(rgba).save(
                destino, quality=cfg.jpeg_quality, subsampling=0, progressive=True)
            entrada["jpg"] = str(destino)
            vista = str(destino)

        registro["recortes"].append(entrada)
        avisar(paso="recorte", i=posicion, de=len(orden), archivo=vista,
               revisar=int(bool(entrada["revisar"])), fondo=quitado_fondo,
               ms=int((time.perf_counter() - t) * 1000))

    # ── 5. Cerrar ────────────────────────────────────────────────────────────
    necesita = bool(registro["revisar"]) or any(c["revisar"] for c in registro["recortes"])
    if necesita and overlay_path:
        # El overlay ya existe en _pasos; se copia a revision/ para que la
        # pestaña de Revisión lo encuentre donde lo busca.
        try:
            Image.open(overlay_path).save(
                dirs["revision"] / f"{path.stem}_cajas.jpg", quality=88)
        except OSError:
            pass

    registro["revision_manual"] = necesita
    registro["ms"] = int((time.perf_counter() - t_inicio) * 1000)
    avisar(paso="lista", recortes=len(registro["recortes"]), revisar=int(necesita))
    return registro
