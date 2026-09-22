"""
Extrae las fotos incrustadas en catálogos/preventas en PDF de proveedor,
identificando a qué código de producto pertenece cada una — para después
pasarlas por el mismo pipeline de limpieza (quitar fondo, completar suela).

Detección de código POR ANCLAS DE TEXTO, sin asumir "un producto por página"
(reemplaza el detector anterior que tomaba la primera línea de texto de la
página como único código, y por eso perdía todo menos el primero en cualquier
PDF con más de un producto por página).

Estrategia, sin OCR:
  1. Se lee el texto NATIVO del PDF con posición (`page.get_text("words")`)
     -- no se renderiza a imagen ni se corre OCR sobre píxeles. Esto evita
     agregarle pytesseract a este venv (Zawa/.venv no lo tiene, y el puente a
     GestionTUC depende de que este proyecto no gane dependencias nuevas; ver
     ingestar_catalogo_tuc.py, que sí usa OCR pero corre en el venv .venv_ml
     de otro proyecto). Si el PDF es un escaneo sin texto seleccionable, este
     método no encuentra nada y la página cae al modo de una sola foto con
     el nombre de archivo como respaldo -- igual que antes.
  2. Las palabras se agrupan en líneas por su propio (block,line) de PyMuPDF,
     y cada línea que calza con el PATRON de código de producto es un ancla
     -- un ancla = un producto. El número de anclas en la página dice cuántos
     productos hay, no se asume de antemano (1, 2, 6, lo que sea).
  3. Cada imagen de la página (filtrando íconos/logos por tamaño mínimo, como
     antes) se asigna al ancla más cercana por distancia de centros -- no se
     arma una grilla fija, así que sirve con layouts irregulares.
  4. Si una página no tiene NINGUNA ancla (sin texto, o ningún patrón de
     código matcheó), se degrada al comportamiento anterior: la imagen más
     grande de la página, código = primera línea de texto no vacía.

Sigue el mismo principio que `extraer_excel.py`: no se asume layout fijo por
proveedor, y una página problemática no frena el resto del PDF.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

import metadatos
import control_extraccion

# Antes se usaba un área mínima en puntos para descartar íconos/logos chicos.
# Se retiró (auditoría 2026-09-03, BASH.pdf): el logo del proveedor se mostraba
# MÁS GRANDE (126x63pt) que las fotos de producto reales (87x78pt) -- un
# umbral de tamaño no puede separar los dos casos si el orden está invertido.
# Criterio nuevo: una imagen que se repite en más de una página del mismo PDF
# es cromo de plantilla (logo, marco, marca de agua) -- una foto de producto
# aparece una sola vez. Es una propiedad estructural del documento, no de
# tamaño, así que no depende de la escala de cada catálogo. Validado contra
# BASH.pdf (88 imágenes, exactamente 1 repetida -- el logo) y PREVENTA
# SS2026.pdf (una imagen grande por página, ninguna repetida).
#
# Se mantiene además un piso de resolución NATIVA muy permisivo (independiente
# de cómo se muestre en la página) solo para descartar íconos realmente
# diminutos (sellos de talla, checks) que por algún motivo no se repitieran.
RESOLUCION_MINIMA_PX = 80

# Mismo patrón que `ingestar_catalogo_tuc.py` (GestionTUC) -- duplicado a
# propósito en vez de importado: ese módulo depende de pytesseract/psycopg2,
# ausentes en este venv por diseño (ver docstring de arriba). Si la regla
# cambia allá, hay que replicar el cambio acá.
PATRON_CODIGO = re.compile(
    r"\b(?:[A-Z]{1,4}-?\d{3,6}[A-Z]?(?:-[A-Z]+)?|\d{2,4}-\d{3,6}[A-Z]?)\b"
)
PATRON_RANGO_TALLA = re.compile(r"(\d{1,2})\s*(?:-|A|/|AL)\s*(\d{1,2})", re.IGNORECASE)
PATRON_EMPAQUE = re.compile(r"(\d{1,3})\s*(DOC|PAR|PARES|UND|UNIDADES|PZA|CAJA)\b",
                            re.IGNORECASE)


def _normalizar(txt: object) -> str:
    if txt is None:
        return ""
    txt = str(txt).strip().upper()
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
    return txt


def _sanitizar_nombre(txt: str) -> str:
    txt = _normalizar(txt)
    txt = re.sub(r"[^A-Z0-9._-]+", "_", txt).strip("_")
    return txt[:80] if txt else ""


def _primera_linea_no_vacia(pagina) -> str:
    """Comportamiento de respaldo cuando la página no tiene ninguna ancla de
    código: la primera línea de texto, igual que el detector anterior."""
    texto = pagina.get_text() or ""
    for linea in texto.splitlines():
        limpio = _sanitizar_nombre(linea)
        if limpio:
            return limpio
    return ""


@dataclass
class _Linea:
    texto: str
    cx: float
    cy: float
    y0: float
    y1: float


def _lineas_de_pagina(pagina) -> list[_Linea]:
    """Agrupa las palabras nativas del PDF en líneas usando el propio
    agrupamiento (block,line) de PyMuPDF -- sin OCR, sin asumir posición."""
    palabras = pagina.get_text("words")  # (x0,y0,x1,y1,texto,block,line,word_no)
    if not palabras:
        return []
    grupos: dict[tuple, list] = {}
    for x0, y0, x1, y1, txt, block, line, _wno in palabras:
        grupos.setdefault((block, line), []).append((x0, y0, x1, y1, txt))

    lineas = []
    for partes in grupos.values():
        texto = " ".join(p[4] for p in partes)
        x0 = min(p[0] for p in partes)
        y0 = min(p[1] for p in partes)
        x1 = max(p[2] for p in partes)
        y1 = max(p[3] for p in partes)
        lineas.append(_Linea(texto=texto, cx=(x0 + x1) / 2, cy=(y0 + y1) / 2, y0=y0, y1=y1))
    return lineas


def _anclas_de_pagina(lineas: list[_Linea]) -> list[_Linea]:
    """Una línea es ancla de producto si su texto (normalizado) matchea el
    patrón de código -- el número de anclas encontradas ES el número de
    productos de la página, no se asume de antemano."""
    anclas = []
    for ln in lineas:
        if PATRON_CODIGO.search(_normalizar(ln.texto).replace(" ", "")):
            anclas.append(ln)
    return anclas


def _texto_por_ancla(lineas: list[_Linea], anclas: list[_Linea]) -> dict[int, str]:
    """Reparte TODAS las líneas de la página entre las anclas por cercanía en
    2D (no solo en Y): un catálogo a dos columnas tiene productos distintos a
    la misma altura, y filtrar solo por banda vertical (bug real: primera
    versión de esta función) mezclaba la talla/empaque de un producto con la
    del vecino de al lado. Devuelve {id(ancla): texto_de_sus_lineas}."""
    por_ancla: dict[int, list[str]] = {id(a): [] for a in anclas}
    for ln in lineas:
        mas_cercana = min(anclas, key=lambda a: (a.cx - ln.cx) ** 2 + (a.cy - ln.cy) ** 2)
        por_ancla[id(mas_cercana)].append(ln.texto)
    return {k: " | ".join(v) for k, v in por_ancla.items()}


def _parsear_talla(texto: str) -> tuple[int | None, int | None]:
    m = PATRON_RANGO_TALLA.search(texto)
    if not m:
        return None, None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if a <= b else (b, a)


def _parsear_empaque(texto: str) -> tuple[int | None, str | None]:
    m = PATRON_EMPAQUE.search(texto)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2).upper()


@dataclass
class ImagenExtraida:
    codigo: str
    ruta: Path
    origen_archivo: str
    pagina: int


def _paginas_por_xref(doc) -> dict[int, int]:
    """Cuenta en cuántas páginas DISTINTAS aparece cada xref de imagen en todo
    el documento -- una imagen que aparece en varias páginas es cromo de
    plantilla (logo/marco), no una foto de producto individual."""
    conteo: dict[int, int] = {}
    for pagina in doc:
        for xref in {img[0] for img in pagina.get_images(full=True)}:
            conteo[xref] = conteo.get(xref, 0) + 1
    return conteo


def _imagenes_calificadas(pagina, doc, paginas_por_xref: dict[int, int]) -> list[tuple[int, float, float, float]]:
    """(xref, area, cx, cy) de cada imagen de la página que parece foto de
    producto: no se repite en otra página del documento, y no es un ícono de
    resolución nativa diminuta."""
    resultado = []
    for img in pagina.get_images(full=True):
        xref = img[0]
        if paginas_por_xref.get(xref, 1) > 1:
            continue  # se repite en otra pagina -> cromo de plantilla
        try:
            base = doc.extract_image(xref)
            if min(base.get("width", 0), base.get("height", 0)) < RESOLUCION_MINIMA_PX:
                continue
        except Exception:
            pass
        rects = pagina.get_image_rects(xref)
        if not rects:
            continue
        rect = max(rects, key=lambda r: r.width * r.height)
        area = rect.width * rect.height
        resultado.append((xref, area, (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2))
    return resultado


def extraer_archivo(path_pdf: Path, carpeta_salida: Path,
                     vistos: dict[str, str] | None = None,
                     productos_meta: dict[str, dict] | None = None,
                     control=None) -> list[ImagenExtraida]:
    """`vistos`: hash de contenido -> código ya guardado con ese contenido,
    para no reprocesar la misma foto dos veces si aparece repetida (mismo
    producto en varias páginas, o repetido entre varios PDF del lote).

    `productos_meta`: si se pasa, se rellena con talla/empaque leído del
    texto cercano a cada ancla de código.

    `control`: `ControlExtraccion` opcional (pausa/cancelación cooperativa).
    Con `None` -- el default -- el comportamiento es idéntico al de siempre.
    Se consulta al empezar cada página y antes de escribir cada imagen, así
    que cancelar devuelve las imágenes ya escritas, no una excepción."""
    vistos = vistos if vistos is not None else {}
    productos_meta = productos_meta if productos_meta is not None else {}
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    extraidas: list[ImagenExtraida] = []

    doc = fitz.open(path_pdf)
    contador_por_codigo: dict[str, int] = {}
    cortado = False
    try:
        # `_paginas_por_xref` recorre TODO el documento antes de extraer nada
        # (es el filtro de cromo de plantilla): en un PDF grande, una pausa
        # pedida durante ese barrido inicial no surte efecto hasta que
        # termina. Se deja así a propósito — cortarlo a la mitad daría un
        # conteo de repeticiones incompleto y colaría logos como productos.
        paginas_por_xref = _paginas_por_xref(doc)
        for n_pagina in range(len(doc)):
            if cortado or not control_extraccion.seguir(control):
                break
            pagina = doc[n_pagina]
            calificadas = _imagenes_calificadas(pagina, doc, paginas_por_xref)
            if not calificadas:
                continue

            lineas = _lineas_de_pagina(pagina)
            anclas = _anclas_de_pagina(lineas)

            if not anclas:
                # Respaldo: comportamiento anterior, 1 producto = la foto más
                # grande de la página, código = primera línea no vacía.
                xref, _area, _cx, _cy = max(calificadas, key=lambda t: t[1])
                codigo_txt = _primera_linea_no_vacia(pagina) or f"PAGINA{n_pagina + 1}"
                asignaciones = [(xref, codigo_txt, "")]
            else:
                # Cada imagen calificada va con el ancla más cercana (distancia
                # de centros) -- generaliza a layouts irregulares sin asumir
                # una grilla NxM. El texto de talla/empaque se reparte con el
                # mismo criterio (ver _texto_por_ancla) para no mezclar
                # columnas vecinas a la misma altura.
                texto_por_ancla = _texto_por_ancla(lineas, anclas)
                asignaciones = []
                for xref, _area, cx, cy in calificadas:
                    ancla = min(anclas, key=lambda a: (a.cx - cx) ** 2 + (a.cy - cy) ** 2)
                    asignaciones.append((xref, ancla.texto, texto_por_ancla[id(ancla)]))

            for xref, codigo_txt, texto_cercano in asignaciones:
                if not control_extraccion.seguir(control):
                    cortado = True
                    break
                codigo = _sanitizar_nombre(codigo_txt) or f"PAGINA{n_pagina + 1}"

                base = doc.extract_image(xref)
                datos = base["image"]
                huella = hashlib.sha1(datos).hexdigest()[:16]
                if huella in vistos:
                    continue
                vistos[huella] = codigo

                contador_por_codigo[codigo] = contador_por_codigo.get(codigo, 0) + 1
                n = contador_por_codigo[codigo]
                sufijo = "" if n == 1 else f"_{n}"
                ext = base.get("ext", "jpg")
                destino = carpeta_salida / f"{codigo}{sufijo}.{ext}"
                while destino.exists():
                    n += 1
                    destino = carpeta_salida / f"{codigo}_{n}.{ext}"

                destino.write_bytes(datos)
                extraidas.append(ImagenExtraida(codigo, destino, path_pdf.name, n_pagina + 1))

                if texto_cercano:
                    talla_min, talla_max = _parsear_talla(texto_cercano)
                    emp_cant, emp_unid = _parsear_empaque(texto_cercano)
                    meta = {"talla_min": talla_min, "talla_max": talla_max,
                           "empaque_cantidad": emp_cant, "empaque_unidad": emp_unid,
                           "costo": None}
                    if metadatos.hay_algun_dato(meta):
                        productos_meta[codigo] = meta
    finally:
        doc.close()

    return extraidas


def extraer_lista(archivos: list[Path], carpeta_salida: Path, control=None) -> dict:
    """Procesa una lista puntual de .pdf (uno o varios archivos sueltos).
    Devuelve un resumen con la misma forma que `extraer_excel.extraer_lista`
    para que la interfaz pueda mostrar ambos con el mismo código.

    `control` opcional (`ControlExtraccion`): cancelar a la mitad devuelve un
    resumen parcial válido, con `cancelado`/`archivos_pendientes`."""
    carpeta_salida = Path(carpeta_salida)
    archivos = sorted(Path(a) for a in archivos)
    control_extraccion.anunciar(control, total=len(archivos))

    vistos: dict[str, str] = {}
    productos_meta: dict[str, dict] = {}
    resumen = {"archivos": {}, "total_imagenes": 0, "total_sin_codigo": 0, "rutas": []}

    for i, archivo in enumerate(archivos):
        if not control_extraccion.seguir(control):
            break
        control_extraccion.anunciar(control, archivo=archivo.name, indice=i + 1,
                                    imagenes=resumen["total_imagenes"])
        try:
            extraidas = extraer_archivo(archivo, carpeta_salida, vistos, productos_meta,
                                        control=control)
        except Exception as exc:  # noqa: BLE001 - un archivo malo no frena el lote
            resumen["archivos"][archivo.name] = {"error": str(exc)}
            continue
        sin_codigo = sum(1 for e in extraidas if e.codigo.startswith("PAGINA"))
        resumen["archivos"][archivo.name] = {
            "imagenes": len(extraidas),
            "sin_codigo": sin_codigo,
        }
        resumen["total_imagenes"] += len(extraidas)
        resumen["total_sin_codigo"] += sin_codigo
        resumen["rutas"].extend(str(e.ruta) for e in extraidas)

    if control is not None and control.cancelado:
        resumen["cancelado"] = True
        resumen["conservar_parcial"] = control.conservar_parcial
        resumen["archivos_pendientes"] = len(archivos) - len(resumen["archivos"])

    # Ver extraer_excel.extraer_lista: con "detener y cancelar" no se deja
    # metadato huérfano de fotos que la interfaz va a borrar.
    if productos_meta and not control_extraccion.descartar_parcial(control):
        ruta_meta = metadatos.escribir(carpeta_salida, productos_meta)
        resumen["metadatos"] = str(ruta_meta)
        resumen["total_con_metadatos"] = len(productos_meta)

    return resumen


def extraer_carpeta(carpeta_pdf: Path, carpeta_salida: Path) -> dict:
    """Procesa todos los .pdf de una carpeta."""
    carpeta_pdf = Path(carpeta_pdf)
    return extraer_lista(sorted(carpeta_pdf.glob("*.pdf")), carpeta_salida)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("carpeta_pdf", type=Path)
    ap.add_argument("carpeta_salida", type=Path)
    args = ap.parse_args()

    resumen = extraer_carpeta(args.carpeta_pdf, args.carpeta_salida)
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
