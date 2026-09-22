"""
Extrae las fotos adjuntas en correos de proveedor guardados como .msg
(Outlook), identificando el código de producto de cada una — para después
pasarlas por el mismo pipeline de limpieza.

El patrón visto en estos correos: cada foto llega adjunta con el propio
código de producto como nombre de archivo (ej. "LM111302.jpg"), a diferencia
de los Excel/PDF donde el código hay que leerlo de una celda o de texto de
página. Por eso acá el código sale directo del nombre del adjunto.

Los correos también traen, casi siempre, una imagen de firma/banner inserta
en el cuerpo (Outlook la adjunta como "image001.png", "image002.png", ...) —
esa NO es una foto de producto y se descarta por nombre y por su forma
alargada de banner, no por tamaño (algunas fotos de producto vienen chicas
también, según la cámara del proveedor)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import extract_msg
from PIL import Image

import control_extraccion

EXTENSIONES_IMAGEN = {"jpg", "jpeg", "png", "bmp", "gif", "tiff", "webp"}

# Outlook nombra así sus imágenes de firma/inserción de cuerpo, nunca un
# proveedor nombra así una foto de producto real.
PATRON_IMAGEN_OUTLOOK = re.compile(r"^image\d+$", re.IGNORECASE)

# Una foto de producto es razonablemente cuadrada/vertical; un banner de
# firma es mucho más ancho que alto.
RELACION_ASPECTO_MAXIMA = 2.2


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


def _es_banner_no_producto(stem: str, datos: bytes) -> bool:
    if PATRON_IMAGEN_OUTLOOK.match(stem):
        return True
    try:
        ancho, alto = Image.open(BytesIO(datos)).size
    except Exception:  # noqa: BLE001 - si no se puede abrir, que la rechace el pipeline después
        return False
    if alto == 0:
        return True
    return (ancho / alto) > RELACION_ASPECTO_MAXIMA


@dataclass
class ImagenExtraida:
    codigo: str
    ruta: Path
    origen_archivo: str
    adjunto_original: str


def extraer_archivo(path_msg: Path, carpeta_salida: Path,
                     vistos: dict[str, str] | None = None,
                     control=None) -> list[ImagenExtraida]:
    """`vistos`: hash de contenido -> código ya guardado con ese contenido,
    para no reprocesar la misma foto dos veces si el mismo correo (o dos
    correos del lote) traen la misma imagen repetida.

    `control`: `ControlExtraccion` opcional (pausa/cancelación cooperativa).
    Con `None` -- el default -- el comportamiento es idéntico al de siempre.
    Se consulta antes de cada adjunto; cortar devuelve lo ya escrito."""
    vistos = vistos if vistos is not None else {}
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    extraidas: list[ImagenExtraida] = []

    msg = extract_msg.Message(path_msg)
    try:
        contador_por_codigo: dict[str, int] = {}
        for adjunto in msg.attachments:
            if not control_extraccion.seguir(control):
                break
            nombre = adjunto.longFilename or adjunto.shortFilename or ""
            ext = Path(nombre).suffix.lstrip(".").lower()
            if ext not in EXTENSIONES_IMAGEN:
                continue

            datos = adjunto.data
            if not datos:
                continue

            stem = Path(nombre).stem
            if _es_banner_no_producto(stem, datos):
                continue

            codigo = _sanitizar_nombre(stem) or "SINCODIGO"

            huella = hashlib.sha1(datos).hexdigest()[:16]
            if huella in vistos:
                continue
            vistos[huella] = codigo

            contador_por_codigo[codigo] = contador_por_codigo.get(codigo, 0) + 1
            n = contador_por_codigo[codigo]
            sufijo = "" if n == 1 else f"_{n}"
            destino = carpeta_salida / f"{codigo}{sufijo}.{ext}"
            while destino.exists():
                n += 1
                destino = carpeta_salida / f"{codigo}_{n}.{ext}"

            destino.write_bytes(datos)
            extraidas.append(ImagenExtraida(codigo, destino, path_msg.name, nombre))
    finally:
        msg.close()

    return extraidas


def extraer_lista(archivos: list[Path], carpeta_salida: Path, control=None) -> dict:
    """Procesa una lista puntual de .msg (uno o varios correos sueltos).

    `control` opcional (`ControlExtraccion`): cancelar a la mitad devuelve un
    resumen parcial válido, con `cancelado`/`archivos_pendientes`."""
    carpeta_salida = Path(carpeta_salida)
    archivos = sorted(Path(a) for a in archivos)
    control_extraccion.anunciar(control, total=len(archivos))

    vistos: dict[str, str] = {}
    resumen = {"archivos": {}, "total_imagenes": 0, "total_sin_codigo": 0, "rutas": []}

    for i, archivo in enumerate(archivos):
        if not control_extraccion.seguir(control):
            break
        control_extraccion.anunciar(control, archivo=archivo.name, indice=i + 1,
                                    imagenes=resumen["total_imagenes"])
        try:
            extraidas = extraer_archivo(archivo, carpeta_salida, vistos, control=control)
        except Exception as exc:  # noqa: BLE001 - un correo malo no frena el lote
            resumen["archivos"][archivo.name] = {"error": str(exc)}
            continue
        sin_codigo = sum(1 for e in extraidas if e.codigo == "SINCODIGO")
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

    return resumen


def extraer_carpeta(carpeta_msg: Path, carpeta_salida: Path) -> dict:
    """Procesa todos los .msg de una carpeta."""
    carpeta_msg = Path(carpeta_msg)
    return extraer_lista(sorted(carpeta_msg.glob("*.msg")), carpeta_salida)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("carpeta_msg", type=Path)
    ap.add_argument("carpeta_salida", type=Path)
    args = ap.parse_args()

    resumen = extraer_carpeta(args.carpeta_msg, args.carpeta_salida)
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
