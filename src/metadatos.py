"""
Escritura/lectura de `metadatos.json`, el archivo que cada extractor
(`extraer_excel.py`, `extraer_pdf.py`, `extraer_msg.py`) deja junto a la
carpeta de fotos con lo que pudo leer de talla, empaque y costo por código.

Por que un archivo aparte y no meterlo en el nombre de la foto: el nombre de
archivo ya es la unica fuente de verdad del CODIGO (para que el puente hacia
GestionTUC lo lea sin volver a hacer OCR); talla/empaque/costo no caben ahi
sin arriesgar que un extractor futuro los confunda con el codigo. Ademas
distintos proveedores no siempre dan los tres datos -- el archivo permite
"lo que se pudo leer, con lo demas en null", en vez de forzar un formato.

Formato en disco: `{"productos": {"CODIGO": {"talla_min":, "talla_max":,
"empaque_cantidad":, "empaque_unidad":, "costo":, "tipo_declarado":,
"tipo_norm":, "color_declarado":, "color_familia":, "temporada_declarada":,
"temporada_norm":}, ...}}` -- mismo contrato que ya espera
`GestionTUC/pipeline/ingestar_limpios_tuc.py::cargar_metadatos`.

Los últimos 6 campos (plan 2026-09-04, paso 6) son el atributo declarado por
el proveedor en su propio Excel, normalizado con `atributos_proveedor.py` --
ver `extraer_excel.py`. Quedan en None si el Excel del proveedor no trae esa
columna, igual que talla/empaque/costo ya hacían.

Multiples extractores pueden escribir en la MISMA carpeta de salida (ej. un
Excel y un PDF del mismo proveedor, en el mismo lote) -- por eso `escribir()`
FUSIONA con lo que ya haya en el archivo en vez de sobreescribirlo, con la
misma regla de "no pisar un dato ya presente con uno vacio" que usa
`indice_fuente.py` para el resto del pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

NOMBRE_ARCHIVO = "metadatos.json"

CAMPOS = ("talla_min", "talla_max", "empaque_cantidad", "empaque_unidad", "costo",
         "moneda_costo", "tipo_declarado", "tipo_norm", "color_declarado", "color_familia",
         "temporada_declarada", "temporada_norm", "marca_declarada")
# `moneda_costo` (plan 2026-09-07, paso 8): "USD"/"CRC"/None según lo que se
# pudo inferir del encabezado de la columna de costo (ver
# extraer_excel.py::_detectar_moneda_costo). None = sin pista de moneda; NO
# asumir CRC por defecto -- servidor_pty.py se abstiene de estimar precio de
# venta cuando la moneda es desconocida, en vez de adivinar.


def _vacio(v) -> bool:
    return v is None or v == ""


def leer(carpeta_salida: Path) -> dict[str, dict]:
    ruta = Path(carpeta_salida) / NOMBRE_ARCHIVO
    if not ruta.exists():
        return {}
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return crudo.get("productos", {}) if isinstance(crudo, dict) else {}


def escribir(carpeta_salida: Path, productos_nuevos: dict[str, dict]) -> Path:
    """Fusiona `productos_nuevos` con lo que ya exista en metadatos.json de esa
    carpeta. Por producto, un campo nuevo no vacio siempre gana; un campo
    nuevo vacio NUNCA borra uno ya presente (ej. el PDF no trae costo pero el
    Excel del mismo lote si -- no se pierde)."""
    carpeta_salida = Path(carpeta_salida)
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    ruta = carpeta_salida / NOMBRE_ARCHIVO

    existente = leer(carpeta_salida)
    for codigo, datos in productos_nuevos.items():
        actual = existente.setdefault(codigo, {c: None for c in CAMPOS})
        for campo in CAMPOS:
            nuevo = datos.get(campo)
            if not _vacio(nuevo):
                actual[campo] = nuevo

    ruta.write_text(
        json.dumps({"productos": existente}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ruta


def hay_algun_dato(datos: dict) -> bool:
    return any(not _vacio(datos.get(c)) for c in CAMPOS)
