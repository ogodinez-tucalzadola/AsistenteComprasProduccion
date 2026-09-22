"""
Extrae las fotos incrustadas en archivos Excel de catálogo/oferta de proveedor,
identificando a qué código de producto pertenece cada una — para después
pasarlas por el mismo pipeline de limpieza (quitar fondo, completar suela).

El problema real: cada proveedor arma su Excel distinto. Cambia cuántas
columnas tiene, en cuál fila empieza el encabezado (algunos meten 5-8 filas de
logo/dirección/condiciones ANTES del encabezado real), cómo se llama la
columna del código ("CODIGO MODELO", "REFERENCIA", "Style No.", "SKU", "STYLE"),
y si el código va repetido en cada fila o solo en la primera fila de cada grupo
de color/talla (dejando las demás en blanco, heredando el de arriba).

Por eso NO se asume ninguna posición fija: se busca el encabezado por
coincidencia de palabras clave en las primeras filas, se elige la columna de
código por prioridad de nombre, y se "arrastra hacia abajo" el último código
visto para las filas que lo dejan en blanco (patrón común de estos Excel).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import openpyxl

import metadatos
import atributos_proveedor
import control_extraccion

EXTENSIONES_IMAGEN = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "bmp": "bmp",
                      "gif": "gif", "tiff": "tiff", "webp": "webp"}

# Prioridad de columna de código: la primera PALABRA CLAVE que aparezca
# CONTENIDA en el texto del encabezado (no exacta) — cada proveedor arma el
# encabezado a su manera ("REFERENCIA", "L4K REF#", "STYLE/REFERENCIA",
# "Article Number", "CODIGO MODELO"...), y todas comparten una de estas
# palabras en alguna parte del texto. De mayor a menor especificidad: "SKU"
# ya suele venir con color incluido (ej. "150263BBK"), por eso va primero.
CANDIDATOS_CODIGO = [
    ["SKU"],
    ["REF"],  # cubre REFERENCIA, REF#, L4K REF#, STYLE/REFERENCIA, etc.
    ["CODIGO"],
    ["ARTICLE"],  # Article Number, Article#
    ["STYLE"],
    ["MODELO"],
    ["PRODUCTO"],
]

# Talla/empaque/costo: mismo patron de "primera palabra clave contenida en el
# encabezado, no exacta" que CANDIDATOS_CODIGO -- cada proveedor arma su Excel
# distinto ("TALLA RANGO", "SIZE RUN", "EMPAQUE", "CTNS/PRS", "COSTO FOB",
# "UNIT COST"...). A diferencia del codigo, estos campos NO se arrastran hacia
# abajo: se leen tal cual estan en la fila de la imagen, y si esa fila los
# trae vacios se deja en null -- inventar un valor heredado de otra fila es
# peor que no tener el dato.
CANDIDATOS_TALLA = [["TALLA"], ["SIZE"]]
CANDIDATOS_EMPAQUE = [["EMPAQUE"], ["PACK"], ["CTNS"], ["CAJA"]]
CANDIDATOS_COSTO = [["COSTO"], ["COST"], ["PRECIO COMPRA"], ["FOB"]]
# Atributos declarados por el proveedor en texto libre (plan 2026-09-04, paso
# 6) -- "TIPO"/"CATEGORIA" antes que "DESCRIPCION" porque una columna de tipo
# dedicada es más confiable que extraer de una descripción larga; "COLOR" es
# casi siempre su propia columna en estos Excel.
CANDIDATOS_TIPO = [["TIPO"], ["CATEGORIA"], ["DESCRIPCION"], ["DESCRIPTION"]]
CANDIDATOS_COLOR_TXT = [["COLOR"]]
CANDIDATOS_TEMPORADA = [["TEMPORADA"], ["SEASON"]]
# Marca del proveedor (plan 2026-09-04, paso 7 -- señal de importación por
# marca): "BRAND" antes que "MARCA" solo importaría si un encabezado trajera
# ambas, caso no visto; el orden real no afecta nada porque un Excel real
# trae como mucho una de las dos.
CANDIDATOS_MARCA = [["MARCA"], ["BRAND"]]

PALABRAS_ENCABEZADO = {
    "FOTO", "PICTURE", "IMAGE", "IMAGEN", "LOGO", "SKU", "REF", "REFERENCIA",
    "CODIGO", "ARTICLE", "STYLE", "MODELO", "PRODUCTO",
    "COLOR", "TALLA", "SIZE", "DESCRIPCION", "DESCRIPTION", "CTNS", "PRS",
    "PRECIO", "MARCA", "BRAND", "GENDER", "GENERO", "EMPAQUE", "PACK", "COSTO", "COST", "FOB",
    "TIPO", "CATEGORIA", "TEMPORADA", "SEASON",
}

# "21-26", "21 A 26", "21/26" -> (21, 26). Un numero suelto ("26") se toma
# como min=max=26 (talla unica), no como rango incompleto.
PATRON_RANGO_TALLA = re.compile(r"(\d{1,2})\s*(?:-|A|/|AL)\s*(\d{1,2})", re.IGNORECASE)
PATRON_TALLA_UNICA = re.compile(r"^\s*(\d{1,2})\s*$")
# "12 PARES", "1 DOC", "6 PZA" -> (12, "PARES"). Mismo espiritu que
# PATRON_EMPAQUE de ingestar_catalogo_tuc.py (GestionTUC), duplicado aca a
# proposito: ese modulo importa pytesseract/psycopg2, ausentes en este venv
# (Zawa/.venv) por diseno -- ver CLAUDE.md del puente.
PATRON_EMPAQUE_CELDA = re.compile(r"(\d{1,3})\s*(DOC|PAR|PARES|UND|UNIDADES|PZA|CAJA)\b",
                                  re.IGNORECASE)
PATRON_NUMERO = re.compile(r"[\d.,]+")


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


@dataclass
class ImagenExtraida:
    codigo: str
    ruta: Path
    origen_archivo: str
    origen_hoja: str
    fila_excel: int


def _detectar_fila_encabezado(ws, max_filas: int = 25) -> int:
    """Busca la fila con más coincidencias de palabras clave de encabezado,
    en vez de asumir que es la fila 1 — varios Excel meten filas de logo,
    dirección o condiciones comerciales antes del encabezado real."""
    mejor_fila, mejor_puntaje = 1, -1
    limite = min(max_filas, ws.max_row)
    for fila in ws.iter_rows(min_row=1, max_row=limite):
        valores = [_normalizar(c.value) for c in fila]
        puntaje = sum(1 for v in valores
                      if v and any(palabra in v for palabra in PALABRAS_ENCABEZADO))
        if puntaje > mejor_puntaje:
            mejor_puntaje, mejor_fila = puntaje, fila[0].row
    return mejor_fila


def _elegir_columna_codigo(encabezados: dict[int, str]) -> int | None:
    for candidatos in CANDIDATOS_CODIGO:
        for col, texto in encabezados.items():
            if any(palabra in texto for palabra in candidatos):
                return col
    return None


def _elegir_columna(encabezados: dict[int, str], candidatos_por_prioridad: list[list[str]]) -> int | None:
    for candidatos in candidatos_por_prioridad:
        for col, texto in encabezados.items():
            if any(palabra in texto for palabra in candidatos):
                return col
    return None


def _valor_celda(ws, fila: int, col: int | None) -> str:
    if col is None:
        return ""
    celda = ws.cell(row=fila, column=col)
    return _normalizar(celda.value)


def _parsear_talla(texto: str) -> tuple[int | None, int | None]:
    if not texto:
        return None, None
    m = PATRON_RANGO_TALLA.search(texto)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b) if a <= b else (b, a)
    m = PATRON_TALLA_UNICA.match(texto)
    if m:
        n = int(m.group(1))
        return n, n
    return None, None


def _parsear_empaque(texto: str) -> tuple[int | None, str | None]:
    if not texto:
        return None, None
    m = PATRON_EMPAQUE_CELDA.search(texto)
    if m:
        return int(m.group(1)), m.group(2).upper()
    # Columna dedicada solo al numero (la unidad viene en el encabezado, ej.
    # "EMPAQUE (pares)"), sin patron "N UNIDAD" dentro de la celda.
    m = PATRON_NUMERO.search(texto)
    if m:
        try:
            return int(float(m.group(0).replace(",", ""))), None
        except ValueError:
            return None, None
    return None, None


def _parsear_costo(texto: str) -> float | None:
    if not texto:
        return None
    m = PATRON_NUMERO.search(texto)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# Moneda del costo (plan 2026-09-07): el usuario confirmó que depende del
# proveedor -- un catálogo de importación FOB casi siempre cotiza en USD,
# uno local en colones. No hay columna dedicada de moneda en estos Excel;
# se infiere del propio encabezado de costo elegido ("COSTO FOB USD",
# "UNIT COST $", "PRECIO COMPRA CRC"...). Si el encabezado no trae ninguna
# pista, queda en None -- adivinar la moneda de un monto es peor que no
# tener el dato (mismo criterio que talla/empaque/costo vacíos).
_MONEDA_USD = ("USD", "US$", "$", "DOLAR", "DOLLAR", "FOB")
_MONEDA_CRC = ("CRC", "COLON", "COLONES", "₡")


def _detectar_moneda_costo(encabezado_costo: str | None) -> str | None:
    if not encabezado_costo:
        return None
    k = _normalizar(encabezado_costo)
    if any(m in k for m in _MONEDA_CRC):
        return "CRC"
    if any(m in k for m in _MONEDA_USD):
        return "USD"
    return None


def _mapa_codigos_por_fila(ws, fila_encabezado: int, col_codigo: int) -> dict[int, str]:
    """Código por fila, arrastrando hacia abajo el último valor no vacío —
    el patrón de "código solo en la primera fila del grupo de color/talla,
    las demás en blanco" es la norma en estos Excel, no la excepción."""
    mapa: dict[int, str] = {}
    ultimo = ""
    for fila in ws.iter_rows(min_row=fila_encabezado + 1, max_row=ws.max_row):
        celda = fila[col_codigo - 1] if col_codigo - 1 < len(fila) else None
        valor = _normalizar(celda.value) if celda is not None else ""
        if valor:
            ultimo = valor
        mapa[fila[0].row] = ultimo
    return mapa


def _extension_de(img) -> str:
    fmt = getattr(img, "format", None)
    if fmt:
        fmt = fmt.lower()
        if fmt in EXTENSIONES_IMAGEN:
            return EXTENSIONES_IMAGEN[fmt]
    ruta = getattr(img, "path", "") or getattr(img, "_path", "") or ""
    ext = Path(str(ruta)).suffix.lstrip(".").lower()
    return EXTENSIONES_IMAGEN.get(ext, "jpg")


def extraer_archivo(path_excel: Path, carpeta_salida: Path,
                     vistos: dict[str, str] | None = None,
                     productos_meta: dict[str, dict] | None = None,
                     control=None) -> list[ImagenExtraida]:
    """`vistos`: hash de contenido -> código ya guardado con ese contenido,
    para no reprocesar la misma foto dos veces si aparece repetida (mismo
    producto listado en varias filas, o repetido entre archivos del lote).

    `productos_meta`: si se pasa, se rellena con talla/empaque/costo por
    código leído de la fila de cada imagen (no se arrastra hacia abajo como el
    código: si la fila de la imagen no trae el dato, queda en None en vez de
    heredar el de otra fila -- ver metadatos.py).

    `control`: `ControlExtraccion` opcional (pausa/cancelación cooperativa).
    Con `None` -- el default -- el comportamiento es idéntico al de siempre.
    Se consulta ANTES de cada imagen: cancelar a la mitad devuelve las
    imágenes ya escritas, no una excepción ni una lista vacía."""
    vistos = vistos if vistos is not None else {}
    productos_meta = productos_meta if productos_meta is not None else {}
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    extraidas: list[ImagenExtraida] = []

    # `load_workbook` es una sola llamada de librería sin puntos intermedios:
    # una pausa/cancelación pedida mientras se abre un Excel muy grande no
    # surte efecto hasta que termina de abrirse (limitación documentada en
    # control_extraccion.py).
    wb = openpyxl.load_workbook(path_excel)
    cortado = False
    for ws in wb.worksheets:
        if cortado:
            break
        imagenes = list(getattr(ws, "_images", []))
        if not imagenes:
            continue

        fila_encabezado = _detectar_fila_encabezado(ws)
        encabezados = {c.column: _normalizar(c.value)
                       for c in next(ws.iter_rows(min_row=fila_encabezado, max_row=fila_encabezado))
                       if c.value}
        col_codigo = _elegir_columna_codigo(encabezados)
        mapa_codigos = (_mapa_codigos_por_fila(ws, fila_encabezado, col_codigo)
                        if col_codigo else {})
        col_talla = _elegir_columna(encabezados, CANDIDATOS_TALLA)
        col_empaque = _elegir_columna(encabezados, CANDIDATOS_EMPAQUE)
        col_costo = _elegir_columna(encabezados, CANDIDATOS_COSTO)
        moneda_costo = _detectar_moneda_costo(encabezados.get(col_costo)) if col_costo else None
        col_tipo = _elegir_columna(encabezados, CANDIDATOS_TIPO)
        col_color = _elegir_columna(encabezados, CANDIDATOS_COLOR_TXT)
        col_temporada = _elegir_columna(encabezados, CANDIDATOS_TEMPORADA)
        col_marca = _elegir_columna(encabezados, CANDIDATOS_MARCA)

        contador_por_codigo: dict[str, int] = {}
        for img in imagenes:
            if not control_extraccion.seguir(control):
                cortado = True
                break
            anchor = img.anchor
            fila_0idx = getattr(getattr(anchor, "_from", None), "row", None)
            fila_excel = (fila_0idx + 1) if fila_0idx is not None else None

            codigo = mapa_codigos.get(fila_excel, "") if fila_excel else ""
            codigo = _sanitizar_nombre(codigo) or (f"FILA{fila_excel}" if fila_excel else "SINCODIGO")

            datos = img._data()
            huella = hashlib.sha1(datos).hexdigest()[:16]
            if huella in vistos:
                continue  # misma foto ya guardada (posiblemente con otro código repetido)
            vistos[huella] = codigo

            contador_por_codigo[codigo] = contador_por_codigo.get(codigo, 0) + 1
            n = contador_por_codigo[codigo]
            sufijo = "" if n == 1 else f"_{n}"
            ext = _extension_de(img)
            destino = carpeta_salida / f"{codigo}{sufijo}.{ext}"
            while destino.exists():
                n += 1
                destino = carpeta_salida / f"{codigo}_{n}.{ext}"

            destino.write_bytes(datos)
            extraidas.append(ImagenExtraida(codigo, destino, path_excel.name, ws.title,
                                             fila_excel or 0))

            if fila_excel:
                talla_min, talla_max = _parsear_talla(_valor_celda(ws, fila_excel, col_talla))
                emp_cant, emp_unid = _parsear_empaque(_valor_celda(ws, fila_excel, col_empaque))
                costo = _parsear_costo(_valor_celda(ws, fila_excel, col_costo))
                tipo_txt = _valor_celda(ws, fila_excel, col_tipo) or None
                color_txt = _valor_celda(ws, fila_excel, col_color) or None
                temporada_txt = _valor_celda(ws, fila_excel, col_temporada) or None
                marca_txt = _valor_celda(ws, fila_excel, col_marca) or None
                meta = {
                    "talla_min": talla_min, "talla_max": talla_max,
                    "empaque_cantidad": emp_cant, "empaque_unidad": emp_unid,
                    "costo": costo,
                    "moneda_costo": moneda_costo if costo is not None else None,
                    "tipo_declarado": tipo_txt,
                    "tipo_norm": atributos_proveedor.norm_tipo(tipo_txt) if tipo_txt else None,
                    "color_declarado": color_txt,
                    "color_familia": atributos_proveedor.norm_color(color_txt) if color_txt else None,
                    "temporada_declarada": temporada_txt,
                    "temporada_norm": atributos_proveedor.norm_temporada(temporada_txt) if temporada_txt else None,
                    "marca_declarada": marca_txt,
                }
                if metadatos.hay_algun_dato(meta):
                    productos_meta[codigo] = meta

    return extraidas


def extraer_lista(archivos: list[Path], carpeta_salida: Path, control=None) -> dict:
    """Procesa una lista puntual de .xlsx (uno o varios archivos sueltos, no
    necesariamente de la misma carpeta). Devuelve un resumen: cuántas
    imágenes se sacaron de cada archivo y cuántas filas quedaron sin código
    identificado (para que el usuario pueda revisar esos casos puntuales).

    `control` opcional (`ControlExtraccion`): si se pasa y se cancela a la
    mitad, el resumen que se devuelve es VÁLIDO y parcial — trae solo los
    archivos ya procesados más `cancelado`/`archivos_pendientes`, para que la
    interfaz pueda seguir el flujo con lo que ya se extrajo."""
    carpeta_salida = Path(carpeta_salida)
    archivos = sorted(Path(a) for a in archivos)
    control_extraccion.anunciar(control, total=len(archivos))

    vistos: dict[str, str] = {}
    productos_meta: dict[str, dict] = {}
    # "rutas": SOLO lo que se extrajo en ESTA llamada — no se relee la carpeta
    # de destino con glob, porque esa carpeta puede tener fotos de una
    # extracción anterior (mismo Excel reprocesado, u otro Excel de la misma
    # carpeta) y mezclarlas es justo el bug que esto evita.
    resumen = {"archivos": {}, "total_imagenes": 0, "total_sin_codigo": 0, "rutas": []}

    for i, archivo in enumerate(archivos):
        if not control_extraccion.seguir(control):
            break
        control_extraccion.anunciar(control, archivo=archivo.name, indice=i + 1,
                                    imagenes=resumen["total_imagenes"])
        try:
            extraidas = extraer_archivo(archivo, carpeta_salida, vistos, productos_meta,
                                        control=control)
        except Exception as exc:  # noqa: BLE001 - un archivo corrupto no frena el lote
            resumen["archivos"][archivo.name] = {"error": str(exc)}
            continue
        sin_codigo = sum(1 for e in extraidas if e.codigo.startswith("FILA")
                          or e.codigo == "SINCODIGO")
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

    # Con "detener y cancelar" no se escribe el JSON de metadatos: la interfaz
    # va a borrar las fotos parciales, y dejar el metadato huérfano apuntando a
    # códigos sin foto es basura que después nadie sabe de dónde salió.
    if productos_meta and not control_extraccion.descartar_parcial(control):
        ruta_meta = metadatos.escribir(carpeta_salida, productos_meta)
        resumen["metadatos"] = str(ruta_meta)
        resumen["total_con_metadatos"] = len(productos_meta)

    return resumen


def extraer_carpeta(carpeta_excel: Path, carpeta_salida: Path) -> dict:
    """Procesa todos los .xlsx de una carpeta."""
    carpeta_excel = Path(carpeta_excel)
    return extraer_lista(sorted(carpeta_excel.glob("*.xlsx")), carpeta_salida)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("carpeta_excel", type=Path)
    ap.add_argument("carpeta_salida", type=Path)
    args = ap.parse_args()

    resumen = extraer_carpeta(args.carpeta_excel, args.carpeta_salida)
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
