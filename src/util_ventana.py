"""
Utilidades de ventana, foco y foto -- Fase 0 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Agrupa lo que no pertenece a ninguna pantalla en particular: el logger de la
interfaz, los badges de estado, el centrado de emergentes en el monitor
correcto, el forzado de foreground de Windows y la carga de miniaturas.
Estaban desparramadas en dos zonas del archivo (cerca de la línea 900 y de la
8300) solo por historia, no por diseño.

El logger (`_logger_gui`) se mudó acá junto con ellas -- no estaba en el mapeo
original de la Fase 0, pero `_cargar_foto_generico` lo usa, y dejarlo en
`gui_profesional_ctk` habría creado un import circular entre los dos archivos.
"""

import contextlib
import ctypes
import logging
import os
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageTk

from tema import (  # noqa: E402
    COLOR_OK, COLOR_OK_BG, COLOR_MAL, COLOR_MAL_BG,
    COLOR_NEUTRO, COLOR_FONDO,
)

# Misma carpeta que `gui_profesional_ctk.py`: acá vive `icono_app.ico`, que
# `_centrar_en_ventana_principal` le pone a cada ventana emergente.
PROYECTO = Path(__file__).resolve().parent


# Auditoría 2026-09-23, Fase B (M3): antes de esto, la interfaz (11,000+
# líneas, 155 bloques `except`) no dejaba NINGÚN rastro cuando algo fallaba
# -- ni siquiera un print(), porque además (ver arriba) esta app corre sin
# consola. `motor_calificacion.py` ya tiene su propio logger a
# `<lote>/motor.log`, pero importarlo acá forzaría a cargar todo el motor de
# visión (torch/transformers) en CADA arranque de la GUI, incluso para
# pasos que nunca tocan scoring -- por eso la interfaz nunca lo importa a
# nivel de módulo (mismo patrón ya existente: `import motor_calificacion`
# siempre local, dentro de la función que lo necesita). Este logger es
# liviano (solo `logging` de la librería estándar) y va a un archivo por
# día, no por lote -- muchos fallos posibles (el diálogo inicial, elegir
# proveedor) ocurren ANTES de que exista un lote activo.
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_log_gui = logging.getLogger("gui_profesional_ctk")
_log_gui.setLevel(logging.INFO)


def _logger_gui() -> logging.Logger:
    if not _log_gui.handlers:
        try:
            _LOG_DIR.mkdir(exist_ok=True)
            archivo = _LOG_DIR / f"gui_{datetime.now():%Y-%m-%d}.log"
            handler = logging.FileHandler(archivo, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            _log_gui.addHandler(handler)
        except OSError:
            # No poder loguear no puede tumbar la app -- se queda sin
            # handler (los .exception()/.error() no revientan sin handler,
            # simplemente no escriben a ningún lado).
            pass
    return _log_gui


def _badge(texto: str, ok: bool | None) -> tuple[str, str, str]:
    """Devuelve (texto, color_fg, color_bg) para un badge de estado.
    ok=True -> verde, ok=False -> rojo/ámbar según severidad, ok=None -> neutro."""
    if ok is True:
        return texto, COLOR_OK, COLOR_OK_BG
    if ok is False:
        return texto, COLOR_MAL, COLOR_MAL_BG
    return texto, COLOR_NEUTRO, COLOR_FONDO


def _centrar_en_ventana_principal(padre, ventana, ancho: int, alto: int) -> None:
    """Centra una ventana emergente (CTkToplevel) sobre `padre`, en el MISMO
    monitor donde esté -- no siempre el monitor primario.

    Bug real (2026-09-22): la mayoría de los CTkToplevel del archivo se
    creaban con `ventana.geometry("WxH")`, sin posición -- Tk/Windows los
    coloca por defecto en el monitor primario, así que si el usuario trabaja
    con la app en un segundo monitor, cada ventana emergente "salta" al
    primero. Los 2 lugares que sí calculaban una posición relativa a `padre`
    (`self.winfo_x()`/`winfo_y()`) además hacían `max(x,0)`/`max(y,0)`, que
    fuerza la ventana de vuelta al monitor primario cuando el segundo monitor
    está a la izquierda (coordenadas negativas) -- mismo bug por otro camino.

    `winfo_rootx()`/`winfo_rooty()` (no `winfo_x()`/`winfo_y()`) funcionan
    tanto si `padre` es la ventana raíz como si es un Frame embebido
    (`VentanaCandidatosCTk`/`PanelComparablesCTk`), porque dan la posición en
    pantalla, no relativa a un contenedor. El clamp usa el escritorio VIRTUAL
    completo (`winfo_vrootx/y/width/height`, que en Windows con multi-monitor
    incluye monitores con coordenadas negativas), no 0,0 -- así una ventana
    nunca queda completamente fuera de pantalla, pero tampoco se fuerza al
    monitor primario."""
    padre.update_idletasks()
    x = padre.winfo_rootx() + (padre.winfo_width() - ancho) // 2
    y = padre.winfo_rooty() + (padre.winfo_height() - alto) // 2
    vx, vy = padre.winfo_vrootx(), padre.winfo_vrooty()
    vw, vh = padre.winfo_vrootwidth(), padre.winfo_vrootheight()
    x = min(max(x, vx), vx + vw - ancho)
    y = min(max(y, vy), vy + vh - alto)
    ventana.geometry(f"{ancho}x{alto}+{x}+{y}")

    # Pedido del usuario (2026-09-23): las ventanas emergentes se veían con
    # el ícono genérico de Tk/Python en vez de la pluma de la app -- ningún
    # CTkToplevel llamaba a `iconbitmap` por su cuenta (solo la ventana
    # principal lo hacía, en `HerramientaUnica.__init__`). Como TODOS los
    # CTkToplevel del archivo pasan por esta función para centrarse, es el
    # único lugar que hace falta tocar para cubrirlos a todos de una vez.
    try:
        ventana.iconbitmap(str(PROYECTO / "icono_app.ico"))
    except tk.TclError:
        pass  # sin ícono no rompe la ventana, solo se ve la genérica de Tk


def _nombre_archivo_seguro(texto: str, largo: int = 60) -> str:
    """Convierte un nombre de proyecto/proveedor en algo que Windows acepte
    como parte de un nombre de archivo (el nombre del proyecto lo escribe el
    comprador y puede traer `/`, `:`, comillas...)."""
    limpio = re.sub(r'[<>:"/\\|?*]+', "-", str(texto or "").strip())
    limpio = re.sub(r"\s+", "_", limpio).strip("._-")
    return (limpio or "sin_nombre")[:largo]


@contextlib.contextmanager
def reloj(widget):
    """Cursor de reloj de arena mientras dura una operación, flecha normal al
    terminar -- pase lo que pase (`finally`), incluso si la operación revienta
    o abre un `messagebox` en el medio.

    Generaliza (2026-09-09) lo que ya se hacía a mano en los cambios de paso y
    en los botones de reparación: cualquier acción que procese algo (tocar la
    base, reparar una foto, exportar un Excel, decidir en bloque) tiene que dar
    señal de que el programa está trabajando. `update_idletasks` es obligatorio:
    sin él, el cursor recién se repintaría DESPUÉS de terminar, o sea nunca.

    Tolera `TclError` a propósito: si el widget se destruyó durante la
    operación (pantalla reconstruida), la operación igual terminó bien y no
    tiene sentido que el reloj de arena sea lo que rompa la app."""
    try:
        widget.configure(cursor="watch")
        widget.update_idletasks()
    except tk.TclError:
        # widget destruido antes de poner el reloj de arena: no es un fallo.
        pass
    try:
        yield
    finally:
        try:
            widget.configure(cursor="")
        except tk.TclError:
            # widget destruido antes de quitar el reloj de arena: no es un fallo.
            pass


def _hwnd_real_windows(ventana) -> int:
    """HWND de nivel Windows de `ventana` (el que maneja la barra de tareas).

    Este es el bug concreto que hacía fallar los intentos anteriores, medido
    el 2026-09-23: `winfo_id()` sobre la ventana principal NO devuelve ese
    HWND, devuelve el de una ventana HIJA sin título que Tk/customtkinter
    crean por dentro. En la medición, `winfo_id()` daba 13566620 (título
    vacío) mientras el HWND real era 3145838 (título 'Asistente de
    Compras'), y `SetForegroundWindow()` con el primero devolvió 0 (o sea,
    falló) las ~300 veces que se lo llamó, sin lanzar ninguna excepción --
    por eso el intento anterior parecía correcto y no hacía nada.
    `GetAncestor(..., GA_ROOT)` es lo que sube del hijo al verdadero."""
    hwnd = ventana.winfo_id()
    try:
        raiz = ctypes.windll.user32.GetAncestor(hwnd, 2)  # 2 = GA_ROOT
    except Exception:  # noqa: BLE001 -- sin la API se sigue con el id crudo
        return hwnd
    return raiz or hwnd


def _foreground_es_de_esta_app() -> bool:
    """¿La ventana activa de Windows ya pertenece a ESTE proceso?

    Es la condición de corte de los reintentos de `_forzar_foreground_windows`:
    sin ella los reintentos le seguirían robando el foco al usuario si él se
    cambió a propósito a otro programa mientras la app terminaba de abrir."""
    try:
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), ctypes.byref(pid)
        )
        return pid.value == os.getpid()
    except Exception:  # noqa: BLE001
        return False


def _forzar_foreground_windows(ventana, intentos: int = 10) -> None:
    """Fuerza que `ventana` sea la ventana ACTIVA de Windows (no solo que
    esté encima en el apilamiento) -- pedido del usuario (2026-09-23): al
    abrir la app con doble clic en el ícono del escritorio, Claude Code (la
    terminal donde se estaba trabajando) se quedaba al frente en vez de la
    app recién abierta.

    Causa real, medida (no supuesta) el 2026-09-23 con
    `GetForegroundWindow()` antes y después de abrir la app:

    1. La app SÍ nace al frente -- Windows se lo concede -- pero pierde el
       foco sola ~0.8 s después, cuando termina de armarse (customtkinter
       rehace la ventana para pintar la barra de título y la app se
       maximiza), y el foreground vuelve al programa anterior. Medición sin
       ninguna corrección activa: la app toma el frente en t+1.6 s y en
       t+2.4 s el frente ya es de nuevo la ventana anterior. O sea: no hay
       que "ganar" el foco al abrir, hay que RECUPERARLO después.
    2. El intento anterior usaba `winfo_id()` como HWND, que es el de una
       ventana hija -- ver `_hwnd_real_windows`. `SetForegroundWindow()`
       devolvía 0 (falló) siempre, en silencio.

    Por eso acá: HWND real vía `_hwnd_real_windows`, la técnica estándar de
    Win32 de "pedir prestado" el hilo de entrada de la ventana que hoy tiene
    el foco (`AttachThreadInput`) para que Windows acepte el pedido, y
    REINTENTOS cada 300 ms -- un disparo único es una carrera contra ese
    rearmado, y quién gana depende de lo rápida que esté la máquina, que es
    justamente por qué antes "a veces" parecía andar. Los reintentos cortan
    apenas el foreground ya es de este proceso, así que no le pelean el foco
    al usuario si él se cambió a otro programa a propósito. Si la API de
    Windows falla, se degrada al toggle de `-topmost` de siempre -- nunca
    revienta la app por esto."""
    if _foreground_es_de_esta_app():
        return  # ya estamos al frente: no hay nada que forzar
    try:
        hwnd = _hwnd_real_windows(ventana)
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd_actual = user32.GetForegroundWindow()
        hilo_actual = kernel32.GetCurrentThreadId()
        hilo_frente = user32.GetWindowThreadProcessId(hwnd_actual, None)
        user32.AttachThreadInput(hilo_frente, hilo_actual, True)
        try:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE -- por si estaba minimizada
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(hilo_frente, hilo_actual, False)
    except Exception:  # noqa: BLE001 -- si la API de Windows falla, se sigue
        # con el intento -topmost de siempre; no hay forma de que esto
        # tumbe la app por un problema de traerla al frente.
        pass
    _traer_al_frente(ventana)
    if intentos > 0 and not _foreground_es_de_esta_app():
        try:
            ventana.after(
                300, lambda: _forzar_foreground_windows(ventana, intentos - 1)
            )
        except tk.TclError:
            # ventana cerrada mientras se reintentaba: no es un fallo.
            pass


def _traer_al_frente(ventana) -> None:
    """Fuerza que una ventana nueva aparezca AL FRENTE, una sola vez.

    Problema real (2026-09-09, reclamo del usuario): las ventanas de
    comparables nacían DETRÁS de la principal y había que ir a buscarlas a la
    barra de tareas. `lift()` solo no alcanza en Windows cuando la ventana que
    tiene el foco es otra del mismo proceso.

    El patrón es `-topmost` ON y OFF poco después: durante ese instante el
    gestor de ventanas la pone encima de todo, y al apagarlo la ventana queda
    en su lugar normal de apilamiento — así NO se queda pegada siempre encima,
    que es lo que molestaría al querer volver a la lista de candidatos."""
    def _paso1() -> None:
        try:
            ventana.attributes("-topmost", True)
            ventana.lift()
            ventana.focus_force()
        except tk.TclError:
            # ventana cerrada antes de traerla al frente: no hay nada que hacer.
            return
        ventana.after(200, _paso2)

    def _paso2() -> None:
        try:
            ventana.attributes("-topmost", False)
        except tk.TclError:
            # ventana cerrada antes de soltar el "siempre encima": no es un fallo.
            pass

    try:
        ventana.after(80, _paso1)
    except tk.TclError:
        # ventana cerrada antes de agendar el traer-al-frente: no es un fallo.
        pass


def _cargar_foto_generico(ruta: Path | None, tam=(90, 90)) -> ImageTk.PhotoImage | None:
    if ruta is None or not ruta.exists():
        return None
    try:
        im = Image.open(ruta).convert("RGBA")
        im.thumbnail(tam)
        return ImageTk.PhotoImage(im)
    except Exception:  # noqa: BLE001
        _logger_gui().exception("no se pudo cargar la foto %s", ruta)
        return None
