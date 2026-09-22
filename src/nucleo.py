"""
Núcleo compartido de LimpiezaImagenes.

Usa el pipeline de Zawa como librería — no modifica su código, así que si llega
una versión actualizada se reemplaza la carpeta y esto sigue funcionando.

Tres cosas que arregla en esta capa, porque a escala importan:

1. **Reanudación por contenido, no por ruta.** Zawa recuerda lo hecho guardando
   el texto de la ruta, así que la misma foto vista como ruta absoluta y como
   relativa cuenta dos veces. Acá la identidad de una lámina es su huella SHA-1.

2. **Deduplicado que sobrevive entre corridas.** El proveedor reenvía la misma
   lámina en tandas distintas; comparar solo dentro del lote actual no lo
   detecta. Acá se compara contra todo lo ya procesado.

3. **fp32 garantizado en CPU.** Los pesos de BiRefNet vienen en fp16, que en
   GPU es más rápido pero en CPU se emula: 21 min por recorte en vez de 12 s
   (medido, i5-1155G7). Se fuerza fp32 acá también, para que el arreglo no se
   pierda si Zawa se reemplaza.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import almacen

PROYECTO = Path(__file__).resolve().parent
CONFIG_PATH = PROYECTO / "config.json"

# Subir este número cada vez que cambie algo en `limpiar_fondo.py`, `reparar.py`
# o `reparar_sombra.py` que altere el resultado sobre la MISMA foto de origen.
# La reanudación por huella SHA-1 identifica la foto, no la receta: sin esto,
# una lámina ya procesada con lógica vieja queda así para siempre, aunque el
# pipeline mejore — pasó de verdad el 2026-08-19 (curva robusta, contorno
# sólido, corrección de sombra por grupo). Con esto, `huellas_procesadas` solo
# cuenta como "ya hecha" una lámina si además se procesó con esta versión.
VERSION_PIPELINE = 2

EXTENSIONES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


# ── Configuración ────────────────────────────────────────────────────────────


def cargar_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Falta {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def ruta_zawa() -> Path:
    d = Path(cargar_config()["zawa_dir"])
    if not (d / "pipeline" / "run.py").exists():
        raise SystemExit(f"No encuentro el pipeline de Zawa en {d}")
    return d


def python_zawa() -> Path:
    p = ruta_zawa() / ".venv" / "Scripts" / "python.exe"
    if not p.exists():
        raise SystemExit(f"No encuentro el intérprete de Zawa en {p}")
    return p


# ── Huellas y descubrimiento ─────────────────────────────────────────────────


def sha1_archivo(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()[:16]


def descubrir(entrada: Path | list[Path]) -> list[Path]:
    """Imágenes de una carpeta (recursivo), de una lista de archivos sueltos,
    o de un solo archivo.

    Aceptar archivos sueltos sirve para reprocesar 2-3 láminas puntuales sin
    tener que armarles una carpeta aparte.
    """
    if isinstance(entrada, (list, tuple)):
        vistas: set[Path] = set()
        for item in entrada:
            vistas.update(descubrir(Path(item)))
        return sorted(vistas)
    entrada = Path(entrada)
    if entrada.is_file():
        return [entrada] if entrada.suffix.lower() in EXTENSIONES else []
    return sorted(p for p in entrada.rglob("*") if p.suffix.lower() in EXTENSIONES)


def huellas_procesadas(salida: Path) -> set[str]:
    """Huellas ya registradas con la versión ACTUAL del pipeline.

    Una lámina procesada con una `version_pipeline` vieja (o sin ese campo,
    de antes de que existiera) no cuenta como "ya hecha": el contenido de la
    foto no cambió, pero la receta sí, y el resultado guardado en disco quedó
    desactualizado. Se excluye de `hechas` para que `planificar` la vuelva a
    poner en la cola, sin necesitar `rehacer=True` sobre todo el lote.
    """
    return almacen.huellas(salida, VERSION_PIPELINE)


@dataclass
class Plan:
    pendientes: list[tuple[Path, str]]
    ya_hechas: int
    duplicadas: list[tuple[Path, Path]]
    total_origen: int


def _clave_canonica(path: Path):
    """Ordena copias del mismo archivo dejando adelante el nombre más limpio.

    Importa porque el nombre del recorte de salida hereda el del origen: si de
    `foto.png` y `foto (1).png` se elige la copia, el catálogo termina con
    archivos llamados `foto (1)_01.png`. Se prefiere el que no parece copia,
    luego el nombre más corto.
    """
    nombre = path.name
    parece_copia = 1 if re.search(r"\(\s*\d+\s*\)|copia|copy", nombre, re.IGNORECASE) else 0
    return (parece_copia, len(nombre), nombre)


def planificar(entrada: Path | list[Path], salida: Path, limite: int | None = None,
               rehacer: bool = False) -> Plan:
    """Decide qué láminas hay que procesar, sin repetir trabajo ni contenido.

    `rehacer=True` ignora lo ya procesado en esa carpeta y vuelve a hacerlo todo.
    Hace falta para volver a correr la misma foto con otros ajustes: sin eso, la
    reanudación por huella la reconoce y no procesa nada.
    """
    todas = descubrir(entrada)
    hechas = set() if rehacer else huellas_procesadas(salida)

    # Agrupar por contenido antes de elegir, para poder quedarse con el mejor
    # nombre de cada grupo y no con el primero del orden alfabético.
    por_huella: dict[str, list[Path]] = {}
    for path in todas:
        por_huella.setdefault(sha1_archivo(path), []).append(path)

    pendientes: list[tuple[Path, str]] = []
    duplicadas: list[tuple[Path, Path]] = []
    ya = 0

    for huella, grupo in por_huella.items():
        grupo = sorted(grupo, key=_clave_canonica)
        canonica, copias = grupo[0], grupo[1:]
        duplicadas.extend((copia, canonica) for copia in copias)
        if huella in hechas:
            ya += 1
            continue
        pendientes.append((canonica, huella))

    pendientes.sort(key=lambda par: str(par[0]))
    if limite:
        pendientes = pendientes[:limite]

    return Plan(pendientes, ya, duplicadas, len(todas))


def escribir_planes(plan: Plan, salida: Path, n_procesos: int) -> list[int]:
    """Reparte las pendientes entre N procesos y devuelve los ids de proceso.

    Antes esto escribía N archivos `plan_i.jsonl` y devolvía sus rutas; ahora la
    cola vive en la tabla `plan` del `lote.sqlite` y cada worker lee la suya por
    su `--id` (que ya recibía de todos modos). Un archivo menos que se puede
    quedar a medio escribir, y un argumento menos que se puede desincronizar
    del `--id`.
    """
    salida.mkdir(parents=True, exist_ok=True)
    return almacen.escribir_plan(salida, plan.pendientes, n_procesos)


def leer_plan(salida: Path, worker_id: int) -> list[tuple[Path, str]]:
    return almacen.leer_plan(salida, worker_id)


# ── Pipeline de Zawa ─────────────────────────────────────────────────────────


def importar_zawa():
    d = ruta_zawa()
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    from pipeline import compose, layout, qa
    from pipeline import run as zrun
    from pipeline.config import Config
    from pipeline.detect import ShoeDetector
    from pipeline.device import pick_device
    from pipeline.instance import Sam2Instances
    from pipeline.matte import BiRefNetMatter

    return {
        "run": zrun,
        "compose": compose,
        "layout": layout,
        "qa": qa,
        "Config": Config,
        "ShoeDetector": ShoeDetector,
        "pick_device": pick_device,
        "Sam2Instances": Sam2Instances,
        "BiRefNetMatter": BiRefNetMatter,
    }


def construir_pipeline(z: dict, cfg, device, hilos: int | None = None):
    """Detector + matting + separador, con fp32 asegurado en CPU."""
    import torch

    if hilos:
        # Con varios procesos en paralelo conviene un hilo por proceso: si cada
        # uno abre 4, se pelean por los mismos núcleos y el total empeora.
        torch.set_num_threads(hilos)

    detector = z["ShoeDetector"](cfg, device)
    matter = z["BiRefNetMatter"](cfg, device)

    if device.type == "cpu" and getattr(matter, "dtype", None) != torch.float32:
        matter.model = matter.model.float()
        matter.dtype = torch.float32

    instancer = z["Sam2Instances"](cfg, device) if cfg.use_instances else None
    return detector, matter, instancer


def preparar_dirs(salida: Path, con_debug: bool = False) -> dict[str, Path]:
    dirs = {
        "transparente": salida / "transparente",
        "blanco": salida / "blanco",
        "revision": salida / "revision",
    }
    if con_debug:
        dirs["debug"] = salida / "debug"
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs
