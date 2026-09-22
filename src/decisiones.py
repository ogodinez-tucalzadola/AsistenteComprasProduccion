"""
Registro de aprobado / descartado por recorte.

Sigue siendo una bitácora que solo crece: cada cambio de opinión agrega una
fila y vale la última. Así queda la historia de quién decidió qué y cuándo, en
vez de sobreescribir el dato.

Lo que cambió (2026-09-08) es DÓNDE se guarda: antes era un `decisiones.jsonl`
suelto en la carpeta de salida, ahora es la tabla `decision` del `lote.sqlite`
de esa misma carpeta (ver `almacen.py`). La lógica de negocio de este módulo
—qué significa aprobado/descartado/eliminado, qué se exporta, qué destinos se
rechazan— no cambió: solo el almacenamiento por debajo.

El grano es el recorte, no la lámina: de una lámina de 4 colores se puede
aprobar 3 y descartar 1.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import almacen

APROBADO = "aprobado"
DESCARTADO = "descartado"
PENDIENTE = "pendiente"

# "Descartado" es reversible: solo dice "esta foto no va al catálogo", el
# archivo sigue en disco y mañana se puede aprobar. "Eliminado" es el caso
# distinto: la foto era basura y su archivo se borró del disco a propósito.
# Se registra aparte justamente para poder distinguir las dos cosas en la
# bitácora — un "descartado" sin archivo detrás sería indistinguible de un
# archivo perdido por accidente.
ELIMINADO = "eliminado"


def cargar(salida: Path) -> dict[str, dict]:
    """Última decisión de cada recorte. Clave = nombre del recorte."""
    return almacen.decisiones_vigentes(salida)


def registrar(salida: Path, recorte: str, decision: str,
              motivos_qa: list[str] | None = None, nota: str = "") -> dict:
    return almacen.registrar_decision(salida, recorte, decision, motivos_qa, nota)


def eliminados(salida: Path) -> set[str]:
    """Recortes cuyo archivo se borró del disco a propósito.

    La interfaz la usa para NO volver a listarlos al recargar el lote: el
    registro de la lámina es por LÁMINA (sha-1 de la foto de entrada) y una
    lámina trae varios recortes, así que borrar ese registro perdería también
    los hermanos buenos del mismo calzado. Se deja intacto y se filtra por esta
    bitácora, que es el grano correcto (el recorte).
    """
    return {n for n, reg in cargar(salida).items()
            if reg.get("decision") == ELIMINADO}


def purgar_stem(salida: Path, stem: str) -> int:
    """Borra las decisiones de una lámina que se va a reprocesar.

    Si `RP9300_02` ya estaba aprobado y la lámina se corre de nuevo, el píxel que
    aprobaste ya no es el que queda en disco. Mantener la decisión vieja la haría
    pasar por revisada sin que nadie haya visto el resultado nuevo.
    """
    return almacen.purgar_decisiones_stem(salida, stem)


def resumen(salida: Path, total_recortes: int) -> dict[str, int]:
    """Conteo por decisión.

    `total_recortes` es el total que la pantalla tiene cargado AHORA, y los
    eliminados ya no están en esa lista (su archivo se borró del disco). Por
    eso "eliminado" se cuenta aparte y NO se resta de `total_recortes`: si se
    restara, cada borrado definitivo dejaría el conteo de pendientes en
    negativo-corregido-a-cero y "aprobado + descartado + pendiente" dejaría de
    dar el total de la pantalla.
    """
    d = cargar(salida)
    conteo = {APROBADO: 0, DESCARTADO: 0, ELIMINADO: 0}
    for reg in d.values():
        if reg["decision"] in conteo:
            conteo[reg["decision"]] += 1
    conteo[PENDIENTE] = max(0, total_recortes - conteo[APROBADO] - conteo[DESCARTADO])
    return conteo


class ErrorExportacion(Exception):
    """Exportación rechazada antes de copiar nada (destino inseguro, etc.)."""


# subcarpeta interna del pipeline -> subcarpeta del destino, extensión
FORMATOS = {
    "blanco": ("fondo_blanco", ".jpg"),
    "transparente": ("fondo_transparente", ".png"),
}


def _es_dentro(hijo: Path, padre: Path) -> bool:
    try:
        hijo.relative_to(padre)
        return True
    except ValueError:
        return False


def validar_destino(salida: Path, destino: Path,
                    entrada: Path | str | None = None) -> None:
    """Rechaza destinos que se confundan con la carpeta de trabajo.

    La carpeta de trabajo se vacía con "Vaciar carpeta de trabajo": exportar
    dentro de ella (o exportar la carpeta que la contiene) es exactamente el
    error que ya destruyó fotos finales una vez. Se bloquea antes de copiar.
    """
    salida = Path(salida).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()

    if destino == salida:
        raise ErrorExportacion(
            "La carpeta de destino es la MISMA carpeta de trabajo:\n"
            f"{destino}\n\n"
            "Esa carpeta se borra cuando usas «Vaciar carpeta de trabajo», así "
            "que las fotos exportadas ahí se perderían.\n\n"
            "Elige una carpeta aparte, fuera de la carpeta de trabajo.")

    if _es_dentro(destino, salida):
        raise ErrorExportacion(
            "La carpeta de destino está DENTRO de la carpeta de trabajo:\n"
            f"destino:  {destino}\n"
            f"trabajo:  {salida}\n\n"
            "Esa carpeta se borra cuando usas «Vaciar carpeta de trabajo», así "
            "que las fotos exportadas ahí se perderían.\n\n"
            "Elige una carpeta aparte, fuera de la carpeta de trabajo.")

    if _es_dentro(salida, destino):
        raise ErrorExportacion(
            "La carpeta de destino CONTIENE a la carpeta de trabajo:\n"
            f"destino:  {destino}\n"
            f"trabajo:  {salida}\n\n"
            "Exportar ahí mezcla las fotos finales con los archivos temporales "
            "del programa y es fácil borrarlas por error.\n\n"
            "Elige una carpeta aparte, que no contenga la carpeta de trabajo.")

    if entrada:
        ent = Path(entrada).expanduser()
        if ent.is_file():
            ent = ent.parent
        if ent.exists():
            ent = ent.resolve()
            if destino == ent or _es_dentro(destino, ent):
                raise ErrorExportacion(
                    "La carpeta de destino es (o está dentro de) la carpeta de "
                    "los catálogos originales PDF/Excel:\n"
                    f"destino:   {destino}\n"
                    f"catálogos: {ent}\n\n"
                    "Mejor deja las fotos exportadas en una carpeta propia, "
                    "para no mezclarlas con los archivos de origen.")


def exportar_aprobados(salida: Path, destino: Path,
                       nombres,
                       formatos=("blanco", "transparente"),
                       entrada: Path | str | None = None) -> dict:
    """Copia a `destino` los recortes APROBADOS del lote actual.

    `nombres` es el conjunto de recortes del manifiesto cargado ahora mismo en
    la interfaz. Es obligatorio: la bitácora de decisiones guarda TODA la
    historia de la carpeta, y si la carpeta se reutilizó para otro catálogo,
    exportar la bitácora completa mete fotos de lotes viejos en la entrega de
    hoy.

    Cada formato va a su propia subcarpeta (`fondo_blanco/`, `fondo_transparente/`)
    y se escribe un `exportacion_<fecha>.json` dentro del destino con el
    detalle de lo exportado.

    Devuelve un dict con conteos, faltantes, errores y ruta del manifiesto.
    Lanza `ErrorExportacion` si el destino es inseguro (no copia nada).
    """
    salida, destino = Path(salida), Path(destino)
    validar_destino(salida, destino, entrada)

    del_lote = {str(n) for n in (nombres or [])}
    if not del_lote:
        raise ErrorExportacion(
            "No hay un lote cargado en la pantalla, así que no se sabe qué "
            "recortes exportar. Carga la carpeta de trabajo y vuelve a intentar.")

    formatos = [f for f in formatos if f in FORMATOS]
    if not formatos:
        raise ErrorExportacion("No se indicó ningún formato de exportación válido.")

    historia = cargar(salida)
    aprobados = sorted(n for n in del_lote
                       if historia.get(n, {}).get("decision") == APROBADO)
    fuera_de_lote = sum(1 for n, reg in historia.items()
                        if reg.get("decision") == APROBADO and n not in del_lote)

    destino.mkdir(parents=True, exist_ok=True)

    copiados: dict[str, list[str]] = {}
    faltantes: list[str] = []
    errores: list[str] = []

    for fmt in formatos:
        sub, ext = FORMATOS[fmt]
        carpeta = destino / sub
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errores.append(f"No se pudo crear la carpeta {carpeta}: {exc}")
            continue
        hechos: list[str] = []
        for recorte in aprobados:
            origen = salida / fmt / f"{Path(recorte).stem}{ext}"
            if not origen.exists():
                faltantes.append(f"{sub}/{origen.name}")
                continue
            try:
                shutil.copy2(origen, carpeta / origen.name)
            except OSError as exc:
                errores.append(f"No se pudo copiar {origen.name} a {sub}: {exc}")
                continue
            hechos.append(origen.name)
        copiados[sub] = hechos

    total = sum(len(v) for v in copiados.values())
    manifiesto = {
        "cuando": datetime.now().isoformat(timespec="seconds"),
        "carpeta_trabajo": str(salida.resolve()),
        "destino": str(destino.resolve()),
        "recortes_del_lote": len(del_lote),
        "aprobados_del_lote": len(aprobados),
        "aprobados_de_otros_lotes_omitidos": fuera_de_lote,
        "archivos_copiados": total,
        "carpetas": {sub: {"formato": fmt, "archivos": copiados.get(sub, [])}
                     for fmt, (sub, _e) in FORMATOS.items() if fmt in formatos},
        "archivos_faltantes": faltantes,
        "errores": errores,
    }
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_manifiesto = destino / f"exportacion_{sello}.json"
    try:
        ruta_manifiesto.write_text(
            json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        errores.append(f"No se pudo escribir el registro de exportación: {exc}")
        ruta_manifiesto = None

    return {
        "copiados": total,
        "por_carpeta": {s: len(v) for s, v in copiados.items()},
        "faltantes": faltantes,
        "errores": errores,
        "aprobados": len(aprobados),
        "omitidos_otros_lotes": fuera_de_lote,
        "manifiesto": str(ruta_manifiesto) if ruta_manifiesto else "",
        "destino": str(destino.resolve()),
    }
