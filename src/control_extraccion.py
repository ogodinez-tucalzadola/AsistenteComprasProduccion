"""
Control cooperativo de pausa/cancelación para la ETAPA DE EXTRACCIÓN
(Excel/PDF/correo → fotos crudas), pedido por el usuario el 2026-09-08:
"estoy en el paso 1 de 4… me gustaría un botón de pausar y de continuar con
las imágenes ya cargadas, y de detener y cancelar".

Por qué acá y no reusando lo de la limpieza: `nucleo.py`/`worker.py` pausan
con `psutil` porque la limpieza corre como SUBPROCESO del sistema operativo y
se le puede mandar suspend/resume. La extracción, en cambio, corre como HILO
Python dentro del mismo proceso de la interfaz — un hilo no se puede suspender
desde afuera sin arriesgar dejar a medias un archivo abierto (openpyxl,
PyMuPDF, extract_msg). La única forma segura es COOPERATIVA: el bucle de
extracción llama `punto_de_control()` entre unidad y unidad de trabajo
(archivo, página, imagen, adjunto) y ahí — y solo ahí — se pausa o se corta.

Consecuencia honesta de ese diseño: pausar y cancelar NO son instantáneos.
Surten efecto en el próximo punto de control, o sea al terminar la imagen /
página / adjunto que se estaba escribiendo. En un PDF de cientos de páginas o
un Excel con cientos de fotos eso es imperceptible; lo que NO se puede cortar
es la apertura inicial de un archivo muy grande (`openpyxl.load_workbook`,
`fitz.open`), que es una sola llamada de librería sin puntos intermedios.

Los tres extractores aceptan `control=None` por defecto: sin control, el
comportamiento es EXACTAMENTE el de antes (ningún llamador viejo se rompe,
incluidos los `__main__` de línea de comandos).
"""

from __future__ import annotations

import threading


class ControlExtraccion:
    """Semáforo de tres estados (corriendo / pausado / cancelado) compartido
    entre el hilo de extracción y la interfaz.

    La interfaz llama `pausar()`, `reanudar()`, `cancelar(conservar=…)`.
    El extractor llama `punto_de_control()` y `anunciar(...)`.
    """

    def __init__(self, total_archivos: int = 0):
        # `_reanudar` seteado = seguir. Se usa un Event en vez de un bool
        # porque el hilo tiene que poder DORMIR mientras está pausado, no
        # quemar CPU en un `while pausado: pass`.
        self._reanudar = threading.Event()
        self._reanudar.set()
        self._cancelar = threading.Event()
        self._conservar_parcial = True

        self._lock = threading.Lock()
        self.total_archivos = total_archivos
        self.indice_archivo = 0
        self.archivo_actual = ""
        self.imagenes_hasta_ahora = 0

    # ── lo que llama la interfaz ─────────────────────────────────────────

    def pausar(self) -> None:
        self._reanudar.clear()

    def reanudar(self) -> None:
        self._reanudar.set()

    @property
    def pausado(self) -> bool:
        return not self._reanudar.is_set() and not self._cancelar.is_set()

    def cancelar(self, conservar: bool = True) -> None:
        """`conservar=True` → "continuar con lo cargado": cortar acá pero
        devolver el resumen parcial válido para seguir el flujo normal.
        `conservar=False` → "detener y cancelar": cortar y no seguir con nada.

        Se setea `_reanudar` también: si el hilo estaba dormido esperando
        reanudación, tiene que despertar para poder ver la cancelación (si no,
        cancelar estando en pausa colgaría el hilo para siempre)."""
        self._conservar_parcial = conservar
        self._cancelar.set()
        self._reanudar.set()

    @property
    def cancelado(self) -> bool:
        return self._cancelar.is_set()

    @property
    def conservar_parcial(self) -> bool:
        return self._conservar_parcial

    def estado_texto(self) -> tuple[str, int, int]:
        """(nombre del archivo en curso, índice 1-based, total) para la
        interfaz, tomado bajo lock porque lo escribe el otro hilo."""
        with self._lock:
            return self.archivo_actual, self.indice_archivo, self.total_archivos

    # ── lo que llama el extractor ────────────────────────────────────────

    def punto_de_control(self) -> bool:
        """Bloquea mientras esté pausado. Devuelve False si hay que cortar el
        bucle (y quedarse con lo hecho hasta acá), True si hay que seguir."""
        self._reanudar.wait()
        return not self._cancelar.is_set()

    def anunciar(self, archivo: str = "", indice: int | None = None,
                 total: int | None = None, imagenes: int | None = None) -> None:
        with self._lock:
            if archivo:
                self.archivo_actual = archivo
            if indice is not None:
                self.indice_archivo = indice
            if total is not None:
                self.total_archivos = total
            if imagenes is not None:
                self.imagenes_hasta_ahora = imagenes


def seguir(control: "ControlExtraccion | None") -> bool:
    """Punto de control tolerante a `control=None` — deja el código de los
    extractores en una sola línea (`if not seguir(control): break`) y garantiza
    que sin control no cambie nada del comportamiento anterior."""
    return True if control is None else control.punto_de_control()


def anunciar(control: "ControlExtraccion | None", **kw) -> None:
    if control is not None:
        control.anunciar(**kw)


def descartar_parcial(control: "ControlExtraccion | None") -> bool:
    """True solo cuando se canceló pidiendo NO conservar lo extraído."""
    return control is not None and control.cancelado and not control.conservar_parcial
