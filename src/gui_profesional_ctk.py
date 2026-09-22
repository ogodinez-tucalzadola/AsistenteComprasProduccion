"""
Herramienta única: procesar, revisar y decidir en una sola ventana.

Reemplaza el ir y venir entre pestañas de `gui_limpieza.py` por un solo flujo
continuo: se arranca el procesamiento y, a medida que van saliendo recortes,
la lista de la izquierda se llena — se puede empezar a revisar sin esperar a
que termine el lote completo.

El estado de cada recorte (falta%, mordida real, sombra vs. grupo, decisión)
no se recalcula acá: se lee directo del manifiesto, donde `worker.py` ya lo
dejó calculado una sola vez (ver `estado.py`).

Rediseño 2026-08: se simplificó el visor (un solo modo, contorno + antes/
después), se cambiaron los 5 botones manuales de acción por un solo control
de "deshacer/rehacer limpieza automática" por recorte, se agregó una vista en
vivo del proceso calzado a calzado durante el procesamiento, y una pantalla de
resumen final con la lámina original + los recortes + acceso a la carpeta de
salida. También se aplicó un tema visual propio (paleta + tipografía) en vez
del gris nativo de ttk.

Reescritura visual 2026-08-27 (`gui_profesional_ctk.py`): MISMA aplicación que
`gui_profesional.py`, con la capa visual reconstruida sobre `customtkinter`
(esquinas redondeadas, modo claro/oscuro que sigue al de Windows, casillas y
botones con color propio). La lógica de negocio no se tocó: los cuerpos de las
funciones que hacen algo (procesar, detener, pausar, exportar, aplicar
limpieza, etc.) son los mismos, letra por letra, que en el archivo original.

Para poder reusar esos cuerpos tal cual — que llaman `.state(["disabled"])`,
`btn["text"]` y `barra.configure(value=...)`, API de `ttk`, no de CTk — se
definen abajo unas clases puente (`Boton`, `Casilla`, `Barra`, `Etiqueta`,
`Marco`) que hablan las dos APIs. Así ni una sola línea de lógica tuvo que
reescribirse para el cambio de librería.

La lista de recortes de la izquierda dejó de ser una tabla de texto: ahora es
`GrillaRecortes`, una grilla de tarjetas con la foto de cada recorte y su
estado en badges de color — el mismo patrón que la pantalla "Elegir qué
limpiar". Esa clase expone la MISMA API mínima que exponía el `ttk.Treeview`
que reemplaza, así que la lógica que la puebla y la lee (`_recargar_manifiesto`,
`_seleccionar`, `_decidir`, `_vaciar_carpeta_trabajo`) quedó intacta: lo único
que cambió es cómo se ve y cómo se elige un recorte, no qué pasa al elegirlo.
"""

from __future__ import annotations

import sys

# Bug real (2026-09-09): "NoneType object has no attribute 'write'" en el
# paso 5, al enviar a calificar. Causa raíz: esta app corre con `pythonw.exe`
# (sin consola, para que no aparezca una ventana negra), y sin consola
# `sys.stdout`/`sys.stderr` son `None`. El motor de calificación compartido
# (`servidor_pty.py`, en el proyecto GestionTUC, usado también por su propia
# herramienta web) tiene varios `print(..., flush=True)` de progreso que
# nunca esperaron correr sin consola -- cualquiera de esos `print()` revienta
# el hilo de envío. Se arregla ACÁ, sin tocar ese archivo compartido con otro
# proyecto: si no hay consola, se les da a `print()` un destino que sí sabe
# `.write()` (no hace nada con el texto, pero no explota).
if sys.stdout is None or sys.stderr is None:
    import io
    _sumidero = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = _sumidero
    if sys.stderr is None:
        sys.stderr = _sumidero

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

import psutil

import almacen
import control_extraccion
import decisiones
import indice_fuente
import nucleo
import reparar

# Carpeta central donde vive cada corrida de lote, una subcarpeta por
# proyecto (nombrada como el usuario lo bautiza en el diálogo inicial). Antes
# cada lote se guardaba donde el usuario eligiera al vuelo (ej. mezclado con
# las fotos de entrada), sin ningún lugar fijo para ver el historial completo.
CARPETA_LOTES_CENTRAL = Path(r"C:\Users\Tucalzado\Proyectos\AsistenteComprasLotes")

try:
    import io as _io

    import win32clipboard  # type: ignore
    import win32con  # type: ignore
    _CLIPBOARD_DISPONIBLE = True
except ImportError:  # pywin32 no instalado: se usa "Guardar como…" como opción principal
    _CLIPBOARD_DISPONIBLE = False

PROYECTO = Path(__file__).resolve().parent

# El modo (claro/oscuro) lo dicta Windows. Se fija ANTES de definir la paleta
# porque los widgets clásicos de tk que quedan (labels de imagen, badges,
# celdas de las grillas) no entienden tuplas claro/oscuro: necesitan un hex
# concreto, y el que corresponde se resuelve acá una sola vez.
ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")
_MODO_OSCURO = ctk.get_appearance_mode() == "Dark"

# ── paleta ────────────────────────────────────────────────────────────────
# Azul-gris oscuro de marca + acentos. Cada token es un par (claro, oscuro):
# el azul de marca `#2f5d8a` es lo único que NO se reinventa — la app tiene
# que seguir siendo "la misma" para quien la usa todos los días.
PAR_FONDO = ("#eef1f5", "#14171b")
PAR_PANEL = ("#ffffff", "#242830")
PAR_PANEL_SUAVE = ("#eff2f6", "#2c313a")
PAR_BORDE = ("#e2e6ec", "#3a4048")
PAR_TEXTO = ("#16202b", "#e8eaed")
PAR_TEXTO_SUAVE = ("#66707c", "#9aa4b0")
PAR_PRIMARIO = ("#2f5d8a", "#3d78ad")
PAR_PRIMARIO_OSCURO = ("#20456a", "#2f5d8a")
# Azul de marca al 10% — fondo de pastillas, del paso activo y de las tarjetas
# de la pantalla de inicio. Da jerarquía sin agregar una línea de borde más.
PAR_PRIMARIO_SUAVE = ("#e4edf6", "#26384a")
PAR_ACENTO = ("#0f766e", "#2aa198")
PAR_OK = ("#1e8e3e", "#3fbb61")
PAR_OK_HOVER = ("#166b2e", "#2f9c4d")
PAR_OK_BG = ("#e6f4ea", "#1e3a26")
PAR_ALERTA = ("#b7791f", "#d9a13c")
PAR_ALERTA_BG = ("#fdf3e0", "#3a2f16")
PAR_MAL = ("#c62828", "#e5534b")
PAR_MAL_HOVER = ("#8f1f1f", "#c0392f")
PAR_MAL_BG = ("#fbe9e9", "#3d1f1f")
PAR_NEUTRO = ("#5c6670", "#9aa4b0")
PAR_VISOR_BG = ("#eef1f4", "#1f232a")
PAR_SELECCION = ("#dbe7f3", "#2f4a63")


def solido(par: tuple[str, str]) -> str:
    """El hex del par (claro, oscuro) que toca según el modo actual — para los
    widgets clásicos de tk, que solo aceptan un color."""
    return par[1] if _MODO_OSCURO else par[0]


# Nombres de siempre, ahora resueltos al modo activo: los usan los widgets tk
# que quedaron (labels de imagen, badges, celdas de grilla) y el ttk.Style de
# la tabla. Se mantienen los mismos nombres para no tocar la lógica que los
# referencia.
COLOR_FONDO = solido(PAR_FONDO)
COLOR_PANEL = solido(PAR_PANEL)
COLOR_BORDE = solido(PAR_BORDE)
COLOR_TEXTO = solido(PAR_TEXTO)
COLOR_TEXTO_SUAVE = solido(PAR_TEXTO_SUAVE)
COLOR_PRIMARIO = solido(PAR_PRIMARIO)
COLOR_PRIMARIO_OSCURO = solido(PAR_PRIMARIO_OSCURO)
COLOR_ACENTO = solido(PAR_ACENTO)
COLOR_OK = solido(PAR_OK)
COLOR_OK_BG = solido(PAR_OK_BG)
COLOR_ALERTA = solido(PAR_ALERTA)
COLOR_ALERTA_BG = solido(PAR_ALERTA_BG)
COLOR_MAL = solido(PAR_MAL)
COLOR_MAL_BG = solido(PAR_MAL_BG)
COLOR_NEUTRO = solido(PAR_NEUTRO)
COLOR_VISOR_BG = solido(PAR_VISOR_BG)
# Borde con un paso más de contraste, para las líneas de 1px que tienen que
# LEERSE (separadores entre grupos de botones). El PAR_BORDE normal, pensado
# para el contorno de una tarjeta grande, en una línea de 1px desaparece.
COLOR_BORDE_FUERTE = "#c3c9d1" if not _MODO_OSCURO else "#4c545e"

# Radios de esquina: chicos en controles, más generosos en tarjetas/paneles.
# Subidos en el rediseño de asistente (2026-09-07): con el fondo más frío y
# los paneles más grandes, los radios viejos hacían ver los bloques cuadrados
# y apretados contra el borde de la ventana.
RADIO_CONTROL = 10
RADIO_PANEL = 16

FUENTE_BASE = ("Segoe UI", 10)
FUENTE_TITULO = ("Segoe UI Semibold", 12)
FUENTE_SUBTITULO = ("Segoe UI Semibold", 10)
FUENTE_CHICA = ("Segoe UI", 9)

# Los 4 niveles tipográficos de siempre, ahora como CTkFont — se crean recién
# cuando ya existe una ventana raíz (CTkFont los necesita), en `armar_fuentes`.
F: dict[str, object] = {}


def armar_fuentes() -> None:
    """Los mismos 4 niveles de tipografía que ya existían, como CTkFont."""
    if F:
        return
    F["base"] = ctk.CTkFont(family="Segoe UI", size=13)
    F["titulo"] = ctk.CTkFont(family="Segoe UI", size=16, weight="bold")
    F["subtitulo"] = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
    F["chica"] = ctk.CTkFont(family="Segoe UI", size=12)
    # Niveles nuevos del asistente por pasos: el título de la pantalla tiene
    # que ganarle claramente a los rótulos de los controles (antes todo vivía
    # entre 12 y 13px y ninguna pantalla tenía un encabezado que se leyera).
    F["titulazo"] = ctk.CTkFont(family="Segoe UI", size=21, weight="bold")
    F["guia"] = ctk.CTkFont(family="Segoe UI", size=14)
    F["nav"] = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")


# Traducción de los `style="…"` de ttk que ya estaban repartidos por el código
# a (fuente, color de texto) de CTk. Se conservan los nombres de estilo para
# que las llamadas de construcción sigan leyéndose igual.
ESTILOS_TEXTO = {
    None: ("base", PAR_TEXTO),
    "TLabel": ("base", PAR_TEXTO),
    "Panel.TLabel": ("base", PAR_TEXTO),
    "Titulo.TLabel": ("titulo", PAR_TEXTO),
    # Encabezado de pantalla del asistente y su línea de ayuda.
    "Titulazo.TLabel": ("titulazo", PAR_TEXTO),
    "Guia.TLabel": ("guia", PAR_TEXTO_SUAVE),
    "Paso.TLabel": ("subtitulo", PAR_PRIMARIO),
    "Subtitulo.TLabel": ("subtitulo", PAR_TEXTO),
    "SubtituloPanel.TLabel": ("subtitulo", PAR_TEXTO),
    "Suave.TLabel": ("chica", PAR_TEXTO_SUAVE),
    "SuavePanel.TLabel": ("chica", PAR_TEXTO_SUAVE),
    "Ok.TLabel": ("chica", PAR_OK),
    "Alerta.TLabel": ("chica", PAR_ALERTA),
    "Mal.TLabel": ("chica", PAR_MAL),
}


def _pad_de_padding(padding) -> dict:
    """`padding` de ttk -> `padx`/`pady` de pack. ttk acepta un número, (x, y)
    o (izq, arriba, der, abajo); se traducen los tres casos."""
    if padding is None:
        return {}
    if isinstance(padding, (int, float)):
        return {"padx": padding, "pady": padding}
    vals = list(padding)
    if len(vals) == 2:
        return {"padx": vals[0], "pady": vals[1]}
    if len(vals) == 4:
        return {"padx": (vals[0], vals[2]), "pady": (vals[1], vals[3])}
    return {}


class _EstadoTtk:
    """Puente `.state([...])` de ttk -> `configure(state=…)` de CTk.

    Existe para que los cuerpos de la lógica original (`btn.state(["disabled"])`,
    `chk.state(["!disabled"])`) sigan funcionando palabra por palabra sobre
    widgets de customtkinter, sin reescribir una sola línea de esa lógica.
    """

    def state(self, spec=None):
        if not spec:
            return ()
        for s in spec:
            if s == "disabled":
                self.configure(state="disabled")
            elif s == "!disabled":
                self.configure(state="normal")
        return ()

    def __getitem__(self, clave):
        # `self.btn_deshacer_auto["text"]` de la lógica original.
        return self.cget(clave)


class Boton(_EstadoTtk, ctk.CTkButton):
    """Botón CTk con los 3 niveles semánticos que ya existían, elegidos con el
    mismo `style="…"` de ttk que usaba el código original."""

    _PALETAS = {
        "Primario.TButton": (PAR_PRIMARIO, PAR_PRIMARIO_OSCURO, ("#ffffff", "#ffffff"), "subtitulo"),
        "Aprobar.TButton": (PAR_OK, PAR_OK_HOVER, ("#ffffff", "#ffffff"), "subtitulo"),
        "Descartar.TButton": (PAR_MAL, PAR_MAL_HOVER, ("#ffffff", "#ffffff"), "subtitulo"),
        # Acciones de mantenimiento / bajo uso: mismo tamaño y tipografía que
        # las de rutina (no son "menos importantes", son menos frecuentes),
        # pero sin relleno — solo contorno y texto suave. Así el ojo va
        # primero a "Procesar" sin que estas parezcan deshabilitadas.
        "Sutil.TButton": ("transparent", PAR_PANEL_SUAVE, PAR_TEXTO_SUAVE, "base"),
        # Acción destructiva e irreversible (borrar archivos del disco): rojo
        # oscuro de contorno, NO el rojo sólido de "Descartar" — se distingue
        # a simple vista de la acción reversible que está al lado.
        "Peligro.TButton": ("transparent", PAR_MAL_BG, PAR_MAL_HOVER, "subtitulo"),
        # ── navegación del asistente (2026-09-07) ──
        # "Siguiente →" es LA acción de cada pantalla: azul lleno, tipografía
        # más grande y más alto que cualquier otro botón, para que no compita
        # con nada. "Atrás" es su par callado: sin relleno ni borde.
        "Nav.TButton": (PAR_PRIMARIO, PAR_PRIMARIO_OSCURO, ("#ffffff", "#ffffff"), "nav"),
        "NavAtras.TButton": ("transparent", PAR_PANEL_SUAVE, PAR_TEXTO_SUAVE, "nav"),
        # Tarjeta-botón grande de la pantalla de inicio (elegir proveedor /
        # elegir origen): fondo azul suave, sin gritar como el primario.
        "Tarjeta.TButton": (PAR_PRIMARIO_SUAVE, PAR_BORDE, PAR_PRIMARIO, "nav"),
        None: (PAR_PANEL_SUAVE, PAR_BORDE, PAR_TEXTO, "base"),
        "TButton": (PAR_PANEL_SUAVE, PAR_BORDE, PAR_TEXTO, "base"),
    }

    _BORDES = {
        "Sutil.TButton": PAR_BORDE,
        "Peligro.TButton": PAR_MAL_HOVER,
    }

    # Botones que nacen más altos que el estándar de 32px, sin que cada punto
    # de llamada tenga que acordarse de pasar `height=`.
    _ALTOS = {
        "Nav.TButton": 44,
        "NavAtras.TButton": 44,
        "Tarjeta.TButton": 52,
    }

    def __init__(self, master, style=None, **kw):
        fg, hover, texto, fuente = self._PALETAS.get(style, self._PALETAS[None])
        kw.setdefault("fg_color", fg)
        kw.setdefault("hover_color", hover)
        kw.setdefault("text_color", texto)
        kw.setdefault("font", F.get(fuente))
        kw.setdefault("corner_radius", RADIO_CONTROL)
        kw.setdefault("height", self._ALTOS.get(style, 32))
        kw.setdefault("width", 0)
        if style in (None, "TButton"):
            kw.setdefault("border_width", 1)
            kw.setdefault("border_color", PAR_BORDE)
        elif style in self._BORDES:
            kw.setdefault("border_width", 1)
            kw.setdefault("border_color", self._BORDES[style])
        super().__init__(master, **kw)


class Casilla(_EstadoTtk, ctk.CTkCheckBox):
    """Las casillas de la app (incluidas las 3 de cada tarjeta de la grilla de
    limpieza) con color propio, en vez del gris genérico de ttk."""

    def __init__(self, master, style=None, **kw):
        kw.pop("style", None)
        kw.setdefault("font", F.get("chica"))
        kw.setdefault("fg_color", PAR_PRIMARIO)
        kw.setdefault("hover_color", PAR_PRIMARIO_OSCURO)
        kw.setdefault("text_color", PAR_TEXTO)
        kw.setdefault("border_color", PAR_BORDE)
        kw.setdefault("checkmark_color", ("#ffffff", "#ffffff"))
        kw.setdefault("corner_radius", 5)
        kw.setdefault("checkbox_width", 18)
        kw.setdefault("checkbox_height", 18)
        kw.setdefault("border_width", 2)
        kw.setdefault("height", 22)
        super().__init__(master, **kw)


class CasillaOk(Casilla):
    """Casilla "Aprobar" — verde, el mismo verde del botón de aprobar."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", PAR_OK)
        kw.setdefault("hover_color", PAR_OK_HOVER)
        super().__init__(master, **kw)


class Barra(ctk.CTkProgressBar):
    """Barra de avance CTk que entiende la API de `ttk.Progressbar` que usa la
    lógica original: `configure(value=…)`, `maximum=`, `start(ms)`, `stop()`."""

    def __init__(self, master, mode: str = "determinate", maximum: float = 100, **kw):
        self._maximo = maximum or 100
        kw.setdefault("progress_color", PAR_PRIMARIO)
        kw.setdefault("fg_color", PAR_BORDE)
        kw.setdefault("corner_radius", RADIO_CONTROL)
        kw.setdefault("height", 10)
        super().__init__(master, mode=mode, **kw)
        if mode == "determinate":
            super().set(0)

    def configure(self, require_redraw=False, **kw):
        if "maximum" in kw:
            self._maximo = kw.pop("maximum") or 100
        valor = kw.pop("value", None)
        super().configure(require_redraw=require_redraw, **kw)
        if valor is not None:
            try:
                super().set(min(max(float(valor) / self._maximo, 0.0), 1.0))
            except (TypeError, ValueError):
                pass

    def start(self, intervalo=None):  # ttk.Progressbar.start(ms)
        super().start()

    def stop(self):
        super().stop()


class Etiqueta(ctk.CTkLabel):
    """Label CTk que acepta el `style=` y el `padding=` de ttk que usa el
    código de construcción original (el `padding` interno de ttk se traduce a
    `padx`/`pady` del pack, que en estos layouts da el mismo margen)."""

    def __init__(self, master, style=None, padding=None, **kw):
        nombre_fuente, color = ESTILOS_TEXTO.get(style, ESTILOS_TEXTO[None])
        if "font" in kw and isinstance(kw["font"], tuple):
            # el original pisaba la fuente del estilo con `font=FUENTE_TITULO`
            # en un par de lugares — se respeta esa intención.
            tam = kw["font"][1] if len(kw["font"]) > 1 else 10
            kw["font"] = F.get("titulo") if tam >= 12 else F.get("subtitulo")
        kw.setdefault("font", F.get(nombre_fuente))
        kw.setdefault("text_color", color)
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        self._pad_interno = _pad_de_padding(padding)
        super().__init__(master, **kw)

    def pack(self, **kw):
        for clave, valor in self._pad_interno.items():
            kw.setdefault(clave, valor)
        super().pack(**kw)


class Marco(ctk.CTkFrame):
    """Contenedor CTk que acepta el `padding=`/`style=` de `ttk.Frame`."""

    def __init__(self, master, padding=None, style=None, **kw):
        panel = style in ("Panel.TFrame", "Tarjeta.TFrame")
        kw.setdefault("fg_color", PAR_PANEL if style == "Tarjeta.TFrame" else "transparent")
        kw.setdefault("corner_radius", RADIO_PANEL if panel else 0)
        self._pad_interno = _pad_de_padding(padding)
        super().__init__(master, **kw)

    def pack(self, **kw):
        for clave, valor in self._pad_interno.items():
            kw.setdefault(clave, valor)
        super().pack(**kw)


class Tarjeta(ctk.CTkFrame):
    """Panel blanco (o gris oscuro) con borde y esquinas redondeadas — el
    reemplazo del `tk.Frame` con `highlightthickness=1` del original."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", PAR_PANEL)
        kw.setdefault("border_color", PAR_BORDE)
        kw.setdefault("border_width", 1)
        kw.setdefault("corner_radius", RADIO_PANEL)
        super().__init__(master, **kw)


class Separador(tk.Frame):
    """Línea horizontal de 1px — el reemplazo de `ttk.Separator`.

    Es un `tk.Frame` y no un `CTkFrame`: verificado en pantalla, un CTkFrame de
    1px de alto no dibuja nada (su canvas interno se colapsa), así que TODOS
    los separadores de los paneles eran invisibles — los bloques del panel de
    la derecha se leían como un bloque continuo.
    """

    def __init__(self, master, **kw):
        kw.pop("fg_color", None)
        kw.pop("corner_radius", None)
        kw.setdefault("height", 1)
        kw.setdefault("bg", COLOR_BORDE_FUERTE)
        super().__init__(master, **kw)
        self.pack_propagate(False)


class SeparadorV(tk.Frame):
    """Línea vertical de 1px — separa GRUPOS de botones dentro de una barra.

    Con solo `padx` los grupos se leen como "botones más juntos y más
    separados", que a simple vista es ruido; con una línea real el ojo ve
    bloques con función distinta.

    Es un `tk.Frame` y no un `CTkFrame` a propósito: probado en pantalla, un
    CTkFrame de 1px de ancho no dibuja nada (su canvas interno se colapsa) —
    la primera versión de esta barra quedó sin ninguna línea visible.
    """

    def __init__(self, master, alto: int = 24, **kw):
        kw.setdefault("width", 1)
        kw.setdefault("height", alto)
        kw.setdefault("bg", COLOR_BORDE_FUERTE)
        super().__init__(master, **kw)
        self.pack_propagate(False)


class BotonMenu(Boton):
    """Reemplazo de `ttk.Menubutton`: customtkinter no tiene menubutton, así
    que es un botón CTk que despliega el MISMO `tk.Menu` de siempre, con los
    mismos comandos, justo debajo de sí mismo."""

    def __init__(self, master, menu: tk.Menu, **kw):
        self._menu = menu
        super().__init__(master, command=self._desplegar, **kw)

    def _desplegar(self) -> None:
        try:
            self._menu.tk_popup(self.winfo_rootx(),
                                self.winfo_rooty() + self.winfo_height())
        finally:
            self._menu.grab_release()


class GrillaRecortes(ctk.CTkScrollableFrame):
    """La lista de recortes: una grilla de tarjetas con la FOTO de cada
    recorte, en vez de la tabla de texto con columnas que había antes.

    Es el mismo patrón que la pantalla "Elegir qué limpiar" — miniatura con
    las líneas de referencia, nombre, y el estado en badges de color — para
    que elegir un recorte sea mirar la foto, no leer una fila de números.

    Habla la misma API mínima que usaba el `ttk.Treeview` que reemplaza
    (`insert`, `delete`, `get_children`, `exists`, `selection`,
    `selection_set`, `set`, `item`, `tag_configure`, y el evento virtual
    `<<TreeviewSelect>>`). Eso es a propósito: así la lógica que ya existe
    — `_recargar_manifiesto`, `_seleccionar`, `_decidir`,
    `_vaciar_carpeta_trabajo` — sigue funcionando palabra por palabra, y lo
    único que cambió de verdad es CÓMO se ve y se elige un recorte, no qué
    pasa después de elegirlo.

    Las miniaturas se generan en lotes con `after()` (igual que
    `_cargar_miniaturas_limpieza`): con cientos de recortes, calcular todos
    los contornos de un tirón congelaría la ventana varios segundos.
    """

    # Una sola columna: la lista vive en un panel angosto, así que cada
    # recorte es una fila ancha — miniatura a la izquierda, nombre y estado a
    # la derecha. En dos columnas las tarjetas quedaban cortadas por el borde
    # del panel (probado en pantalla).
    COLS = 1
    LADO_MINIATURA = 78

    def __init__(self, master, proveedor_png, componer, al_cambiar_lote=None, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("scrollbar_button_color", PAR_BORDE)
        kw.setdefault("scrollbar_button_hover_color", PAR_TEXTO_SUAVE)
        super().__init__(master, **kw)
        self._proveedor_png = proveedor_png   # nombre -> Path del png actual
        self._componer = componer             # imagen RGBA -> RGB sobre gris
        self._al_cambiar_lote = al_cambiar_lote  # aviso a la app: cambió el lote
        self._orden: list[str] = []
        self._tarjetas: dict[str, dict] = {}
        self._sel: str | None = None
        # Selección MÚLTIPLE (el "lote"). Convive con `_sel` en vez de
        # reemplazarla: `_sel` sigue siendo "la foto que se está mirando en el
        # panel de detalle" (una sola, la que gobierna `_nombre_actual`), y
        # `_lote` es "sobre cuáles se va a actuar en bloque". Unificarlas
        # habría obligado a decidir qué foto mostrar en grande cuando hay 12
        # marcadas, que es justo lo que no tiene respuesta.
        self._lote: set[str] = set()
        self._ancla: str | None = None        # extremo fijo del Shift+clic
        self._colores_tag: dict[str, str] = {}
        self._fotos: dict[str, ImageTk.PhotoImage] = {}
        self._pendientes: list[str] = []
        self._tarea_lote: str | None = None
        # Las tarjetas se colocan con `grid`; este aviso también, porque Tk no
        # permite mezclar `pack` y `grid` dentro del mismo contenedor.
        self._vacio = Etiqueta(self, text="Todavía no hay recortes en esta carpeta.",
                               style="SuavePanel.TLabel", wraplength=250)
        self._vacio.grid(row=0, column=0, columnspan=self.COLS, padx=6, pady=6, sticky="w")

    # ── API que usaba el Treeview ────────────────────────────────────────

    def get_children(self, item="") -> tuple:
        return tuple(self._orden)

    def exists(self, iid: str) -> bool:
        return iid in self._tarjetas

    def selection(self) -> tuple:
        return (self._sel,) if self._sel in self._tarjetas else ()

    def deseleccionar(self) -> None:
        """Deja la grilla sin foto seleccionada ni lote marcado -- lo usa
        "cerrar el visor" después de aprobar una foto ya corregida."""
        antes = set(self._lote) | ({self._sel} if self._sel else set())
        self._sel = None
        self._lote = set()
        self._ancla = None
        self._repintar(antes)
        self._avisar_lote()
        self.event_generate("<<TreeviewSelect>>")

    def selection_set(self, iid: str) -> None:
        """Clic simple: ésta pasa a ser LA foto seleccionada y el lote se
        reduce a ella sola (el clic sin modificador siempre "empieza de
        nuevo", que es lo que espera cualquiera que venga del explorador de
        archivos)."""
        if iid not in self._tarjetas:
            return
        anterior_lote = set(self._lote)
        anterior, self._sel = self._sel, iid
        self._lote = {iid}
        self._ancla = iid
        self._repintar({anterior, iid} | anterior_lote)
        self._avisar_lote()
        self.event_generate("<<TreeviewSelect>>")

    # ── selección múltiple (lote) ────────────────────────────────────────

    def lote(self) -> list[str]:
        """Los nombres marcados para acción en bloque, en el orden en que se
        ven en pantalla."""
        return [n for n in self._orden if n in self._lote]

    def limpiar_lote(self, conservar_actual: bool = True) -> None:
        antes = set(self._lote)
        self._lote = {self._sel} if (conservar_actual and self._sel in self._tarjetas) else set()
        self._repintar(antes | self._lote)
        self._avisar_lote()

    def _avisar_lote(self) -> None:
        if callable(self._al_cambiar_lote):
            self._al_cambiar_lote(self.lote())

    def _alternar_lote(self, nombre: str) -> str:
        """Ctrl+clic: agrega o quita esta tarjeta del lote SIN tocar cuál es
        la foto del panel de detalle — marcar 8 fotos para desechar no debería
        hacer saltar el visor 8 veces."""
        if nombre not in self._tarjetas:
            return "break"
        antes = set(self._lote)
        if nombre in self._lote:
            self._lote.discard(nombre)
        else:
            self._lote.add(nombre)
            self._ancla = nombre
        # Se repinta TODO el lote (antes y después) y no solo la tarjeta
        # clicada porque la marca ámbar aparece recién con 2+: al pasar de 1 a
        # 2 hay que ir a pintar también a la que ya estaba marcada.
        self._repintar(antes | self._lote | {nombre})
        self._avisar_lote()
        return "break"

    def _repintar(self, nombres) -> None:
        for n in nombres:
            if n in self._tarjetas:
                self._pintar_borde(n)

    def _rango_lote(self, nombre: str) -> str:
        """Shift+clic: agrega al lote todo lo que hay entre el ancla (la
        última tarjeta marcada o seleccionada) y ésta."""
        if nombre not in self._tarjetas:
            return "break"
        ancla = self._ancla if self._ancla in self._tarjetas else self._sel
        if ancla not in self._tarjetas:
            return self._alternar_lote(nombre)
        i, j = sorted((self._orden.index(ancla), self._orden.index(nombre)))
        antes = set(self._lote)
        self._lote |= set(self._orden[i:j + 1])
        self._repintar(antes | self._lote)
        self._avisar_lote()
        return "break"

    def tag_configure(self, tag: str, foreground: str | None = None, **_kw) -> None:
        if foreground:
            self._colores_tag[tag] = foreground

    def delete(self, *iids) -> None:
        for iid in iids:
            tarjeta = self._tarjetas.pop(iid, None)
            if tarjeta is not None:
                tarjeta["marco"].destroy()
            if iid in self._orden:
                self._orden.remove(iid)
            self._fotos.pop(iid, None)
            if iid in self._pendientes:
                self._pendientes.remove(iid)
            if self._sel == iid:
                self._sel = None
            if self._ancla == iid:
                self._ancla = None
            self._lote.discard(iid)
        self._avisar_lote()
        self._recolocar()
        if not self._orden:
            self._vacio.grid(row=0, column=0, columnspan=self.COLS,
                             padx=6, pady=6, sticky="w")

    def _recolocar(self) -> None:
        """Reacomoda las tarjetas que quedan, para que un borrado parcial no
        deje huecos en la grilla."""
        for idx, nombre in enumerate(self._orden):
            fila, col = divmod(idx, self.COLS)
            self._tarjetas[nombre]["marco"].grid_configure(row=fila, column=col)

    def insert(self, parent="", index="end", iid: str = "", text: str = "",
               values=(), tags=()) -> str:
        """Crea la tarjeta de un recorte. Firma compatible con la del
        Treeview: `values` = (falta%, mordida, sombra, decisión)."""
        self._vacio.grid_remove()
        nombre = iid or text
        if nombre in self._tarjetas:
            self.delete(nombre)
        self._orden.append(nombre)

        self.grid_columnconfigure(0, weight=1)
        fila, col = divmod(len(self._orden) - 1, self.COLS)
        marco = ctk.CTkFrame(self, fg_color=PAR_PANEL_SUAVE, corner_radius=RADIO_CONTROL,
                             border_width=2, border_color=PAR_PANEL_SUAVE,
                             height=self.LADO_MINIATURA + 14)
        marco.grid(row=fila, column=col, padx=2, pady=3, sticky="ew")
        marco.grid_propagate(False)

        hueco = ctk.CTkFrame(marco, fg_color=PAR_VISOR_BG, corner_radius=6,
                             width=self.LADO_MINIATURA, height=self.LADO_MINIATURA)
        hueco.pack(side="left", padx=(6, 8), pady=6)
        hueco.pack_propagate(False)
        # tk.Label porque es el que recibe los ImageTk.PhotoImage que ya
        # produce el pipeline de imagen de la app.
        lbl_img = tk.Label(hueco, background=solido(PAR_VISOR_BG), cursor="hand2",
                           borderwidth=0, highlightthickness=0)
        lbl_img.pack(fill="both", expand=True)

        datos = Marco(marco)
        datos.pack(side="left", fill="both", expand=True, pady=6, padx=(0, 6))
        # Se muestra el nombre SIN la extensión (`.png`): no aporta nada y
        # hacía que el nombre se cortara en dos líneas por una "g" suelta.
        # La clave interna sigue siendo el nombre completo.
        lbl_nombre = Etiqueta(datos, text=Path(nombre).stem, style="SubtituloPanel.TLabel",
                              wraplength=170, cursor="hand2")
        lbl_nombre.pack(fill="x")
        lbl_avisos = Etiqueta(datos, text="", style="SuavePanel.TLabel",
                              wraplength=170, cursor="hand2")
        lbl_avisos.pack(fill="x")
        lbl_dec = Etiqueta(datos, text="", style="SuavePanel.TLabel", cursor="hand2")
        lbl_dec.pack(fill="x")

        self._tarjetas[nombre] = {"marco": marco, "img": lbl_img, "dec": lbl_dec,
                                  "avisos": lbl_avisos, "valores": list(values),
                                  "tags": list(tags), "cargada": False}
        for widget in (marco, hueco, lbl_img, datos, lbl_nombre, lbl_avisos, lbl_dec):
            widget.bind("<Button-1>", lambda _e, n=nombre: self.selection_set(n))
            # Tk elige el binding MÁS específico, así que estos dos ganan sobre
            # <Button-1> y el clic simple sigue comportándose como siempre.
            widget.bind("<Control-Button-1>", lambda _e, n=nombre: self._alternar_lote(n))
            widget.bind("<Shift-Button-1>", lambda _e, n=nombre: self._rango_lote(n))

        self._pintar_estado(nombre)
        self._pendientes.append(nombre)
        self._programar_lote()
        return nombre

    def set(self, iid: str, column: str, value=None):
        """`tabla.set(nombre, "decision", "aprobado")` de la lógica original."""
        tarjeta = self._tarjetas.get(iid)
        if tarjeta is None:
            return ""
        indice = {"falta": 0, "mordida": 1, "sombra": 2, "decision": 3}.get(column)
        if indice is None:
            return ""
        if value is None:
            return tarjeta["valores"][indice]
        tarjeta["valores"][indice] = value
        self._pintar_estado(iid)
        return value

    def item(self, iid: str, tags=None, **_kw):
        tarjeta = self._tarjetas.get(iid)
        if tarjeta is None:
            return {}
        if tags is not None:
            tarjeta["tags"] = list(tags)
            self._pintar_estado(iid)
        return {"tags": tuple(tarjeta["tags"])}

    def heading(self, *_a, **_kw) -> None:
        """Sin encabezados de columna: ya no es una tabla."""

    def column(self, *_a, **_kw) -> None:
        """Idem `heading` — se acepta para no tocar el código que la arma."""

    # ── pintado ──────────────────────────────────────────────────────────

    def _color_decision(self, tarjeta: dict) -> tuple[str, str] | None:
        for tag in tarjeta["tags"]:
            if tag in self._colores_tag:
                return (self._colores_tag[tag], self._colores_tag[tag])
        return None

    def _pintar_borde(self, nombre: str) -> None:
        """Tres estados visuales distintos, a propósito:

        · LA seleccionada (la del panel de detalle): fondo azul claro.
        · En el lote (marcada para acción en bloque): borde ámbar grueso.
        · Ninguna de las dos: borde del color de su decisión, o neutro.

        La seleccionada puede además estar en el lote — entonces lleva el
        fondo azul Y el borde ámbar, que es exactamente lo que quiere decir:
        "es la que estoy mirando y también entra en el lote".
        """
        tarjeta = self._tarjetas[nombre]
        en_lote = len(self._lote) > 1 and nombre in self._lote
        if nombre == self._sel:
            tarjeta["marco"].configure(
                border_color=PAR_ALERTA if en_lote else PAR_PRIMARIO,
                border_width=3 if en_lote else 2,
                fg_color=PAR_SELECCION)
            return
        color = self._color_decision(tarjeta)
        if en_lote:
            tarjeta["marco"].configure(border_color=PAR_ALERTA, border_width=3,
                                       fg_color=PAR_PANEL_SUAVE)
            return
        tarjeta["marco"].configure(border_color=color or PAR_PANEL_SUAVE,
                                   border_width=2, fg_color=PAR_PANEL_SUAVE)

    def _pintar_estado(self, nombre: str) -> None:
        """Los 4 datos que antes eran columnas de texto, ahora como color:
        borde verde/rojo según la decisión, y una línea ámbar de avisos con
        falta% + mordida + sombra."""
        tarjeta = self._tarjetas[nombre]
        falta, mordida, sombra, decision = (list(tarjeta["valores"]) + ["", "", "", ""])[:4]

        avisos = [f"falta {falta}%"] if falta not in ("", None) else []
        if str(mordida).lower() in ("sí", "si", "true", "1"):
            avisos.append("mordida")
        if str(sombra).lower() in ("sí", "si", "true", "1"):
            avisos.append("sombra")
        hay_alerta = len(avisos) > 1
        tarjeta["avisos"].configure(text="  ·  ".join(avisos),
                                    text_color=PAR_ALERTA if hay_alerta else PAR_TEXTO_SUAVE)

        texto_dec = str(decision or "")
        color = self._color_decision(tarjeta)
        tarjeta["dec"].configure(
            text={"aprobado": "✓ aprobado", "descartado": "✗ descartado"}.get(texto_dec, texto_dec),
            text_color=color or PAR_TEXTO_SUAVE)
        self._pintar_borde(nombre)

    # ── miniaturas por lotes ─────────────────────────────────────────────

    def _programar_lote(self) -> None:
        if self._tarea_lote is not None:
            try:
                self.after_cancel(self._tarea_lote)
            except (ValueError, tk.TclError):
                pass
        self._tarea_lote = self.after(40, self._cargar_lote)

    def _cargar_lote(self, cuantas: int = 10) -> None:
        self._tarea_lote = None
        hechas = 0
        while self._pendientes and hechas < cuantas:
            nombre = self._pendientes.pop(0)
            tarjeta = self._tarjetas.get(nombre)
            if tarjeta is None:
                continue
            im = None
            png = self._proveedor_png(nombre)
            if png and Path(png).exists():
                try:
                    im = reparar.dibujar_contorno(Path(png))
                    if im.mode != "RGB":
                        im = self._componer(im)
                except Exception:  # noqa: BLE001
                    try:
                        im = self._componer(Image.open(png))
                    except Exception:  # noqa: BLE001
                        im = None
            if im is None:
                im = Image.new("RGB", (self.LADO_MINIATURA, self.LADO_MINIATURA),
                               solido(PAR_VISOR_BG))
            im = im.copy()
            im.thumbnail((self.LADO_MINIATURA - 6, self.LADO_MINIATURA - 6))
            foto = ImageTk.PhotoImage(im)
            self._fotos[nombre] = foto  # referencia viva — si no, Tk la descarta
            try:
                tarjeta["img"].configure(image=foto)
            except tk.TclError:
                return  # tarjeta destruida entretanto
            tarjeta["cargada"] = True
            hechas += 1
        if self._pendientes:
            self._tarea_lote = self.after(10, self._cargar_lote)


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


class HerramientaUnica(ctk.CTk):
    # ── carpeta del lote activo ──────────────────────────────────────────────
    #
    # `_salida` es una PROPIEDAD y no un atributo simple por una razón concreta
    # de la Fase 2: desde que la revisión de catálogo vive en el `lote.sqlite`
    # del lote, el motor de calificación necesita saber CUÁL lote es antes de
    # que se lo consulte, y esa carpeta es justamente `_salida`.
    #
    # Se asigna en seis lugares distintos del flujo (elegir carpeta, retomar el
    # último lote, crear proyecto nuevo, procesar, anotar la marca del lote…).
    # Poner el aviso al motor en cada uno de esos seis lugares habría dejado el
    # séptimo sin avisar la próxima vez que alguien agregue un camino -- y el
    # síntoma es un error a mitad del flujo ("No hay lote activo") en el paso 6,
    # lejos de la causa. Con la propiedad, declarar el lote es una consecuencia
    # automática de fijar la carpeta: no hay forma de fijar una sin lo otro.
    #
    # El default a nivel de CLASE existe para que leer `self._salida` antes de
    # que `__init__` la fije (customtkinter corre bastante código propio en su
    # `super().__init__()`) devuelva None en vez de romper con AttributeError.
    __salida = None

    @property
    def _salida(self):
        return self.__salida

    @_salida.setter
    def _salida(self, valor) -> None:
        self.__salida = Path(valor) if valor is not None else None
        try:
            import motor_calificacion
            motor_calificacion.fijar_lote(self.__salida)
        except Exception as exc:  # noqa: BLE001
            # Que el motor no se pueda avisar NO debe tumbar la interfaz: la
            # carpeta igual queda fijada y los pasos 1-5 (limpieza de fotos) no
            # dependen del motor de calificación en absoluto. El paso 6 sí, y
            # ahí el error se ve con su mensaje propio.
            print(f"  [aviso] no se pudo declarar el lote activo al motor: {exc}", flush=True)

    def __init__(self) -> None:
        super().__init__()
        armar_fuentes()
        self.title("Asistente de Compras")
        self._geometria_que_entre(1240, 760)
        self.minsize(900, 560)
        # Abrir MAXIMIZADA por defecto: con 3 columnas (grilla de recortes +
        # visor + panel de decisión), una ventana de 1240x760 sin maximizar le
        # deja al visor central menos de 600px de ancho real -- causa raíz de
        # que la foto grande del paso 4 se viera "muy pequeña" pese a que el
        # cálculo de escalado (`_espacio_visor`) ya usa el tamaño real: el
        # tamaño real disponible era chico de entrada. `state("zoomed")` es
        # la forma correcta en Windows (Tk no tiene un "maximizar" portable).
        # Y hay que REPETIRLO con `after`: customtkinter, dentro de su propio
        # arranque, restaura el estado de la ventana cuando aplica el color de
        # la barra de título -- medido el 2026-09-09, la ventana volvía a
        # `state() == "normal"` con 1495x774 pese a este llamado, y por eso el
        # paso 4 seguía viéndose chico (el cuerpo se quedaba con 462px de alto
        # y el visor de la foto con 199px).
        def _maximizar() -> None:
            try:
                if self.state() != "zoomed":
                    self.state("zoomed")
            except Exception:  # noqa: BLE001
                pass  # plataforma sin soporte -- se queda con la geometría fija

        _maximizar()
        self.after(200, _maximizar)
        self.after(700, _maximizar)
        self.configure(fg_color=PAR_FONDO)
        # Bug real de customtkinter (2026-09-08, reclamo del usuario -- se veía
        # el logo de Python en la barra de tareas): CTk.__init__ (llamado en
        # super().__init__() arriba) programa su PROPIO ícono por defecto con
        # un `after()` interno que corre DESPUÉS de este punto, pisando
        # cualquier iconbitmap ya puesto acá. Hay que reforzarlo con un
        # after() propio que corra más tarde -- el llamado inmediato no
        # alcanza, se ve el ícono correcto un instante y después vuelve al de
        # Python.
        ruta_icono = str(PROYECTO / "icono_app.ico")

        def _forzar_icono() -> None:
            try:
                self.iconbitmap(ruta_icono)
            except tk.TclError:
                pass  # sin ícono no rompe la app, solo se ve la genérica de Tk

        _forzar_icono()
        self.after(150, _forzar_icono)
        self.after(500, _forzar_icono)

        self._armar_estilo()

        self._q: queue.Queue = queue.Queue()
        self._procs: list[subprocess.Popen] = []
        self._procs_pendientes: int = 0  # cuántos workers de limpieza siguen vivos (H4: puede ser > 1)
        self._pausado: bool = False
        self._salida = None   # ver la propiedad `_salida` más abajo: asignarla
        # también le declara el lote activo al motor de calificación (Fase 2)
        self._entrada_archivos: list[Path] | None = None  # si se eligió "Archivos…" en vez de carpeta
        self._recortes: dict[str, dict] = {}   # nombre -> entrada de recorte (manifiesto)
        # ¿Los recortes que hay en pantalla vienen de una sesión ANTERIOR
        # (lote retomado) o se acaban de procesar acá? El paso 3 se explica
        # distinto en cada caso — ver `_refrescar_aviso_retomar`.
        self._retomando_lote: bool = False
        self._foto_actual: ImageTk.PhotoImage | None = None
        self._foto_visor_vivo: ImageTk.PhotoImage | None = None
        self._fotos_resumen: list[ImageTk.PhotoImage] = []
        self._rutas_pendientes: dict[str, Path] = {}
        self._nombre_actual: str | None = None
        self._nombre_proyecto: str | None = None  # nombre del proyecto actual
        self._ver_antes_despues = tk.BooleanVar(value=False)
        # Las líneas de referencia (azul = borde real, roja = curva esperada)
        # se dibujaban SIEMPRE y sin forma de apagarlas en esta pantalla; el
        # interruptor para verlas solo existía en el visor del resumen final.
        self._ver_lineas = tk.BooleanVar(value=True)
        self._resumen_frame: ctk.CTkFrame | None = None
        self._widgets_resumen_recorte: dict[str, tuple[tk.Label, tk.Label]] = {}
        # Fotos que el comprador reparó/recortó a mano en esta sesión: al
        # aprobarlas, su visor se cierra solo (ver `_decidir`).
        self._corregidas_a_mano: set[str] = set()
        # ¿Hay un envío a calificar corriendo en segundo plano? Lo usa el
        # avance propio del paso 5 de vectorización (`barra_compras`).
        self._enviando_compras = False
        # ── flujo por pasos (plan 2026-09-07, paso 8) ──
        # El proveedor se elige al PRINCIPIO (barra 1), no después de procesar
        # las fotos: es lo primero que el comprador sabe del catálogo que tiene
        # en la mano. `_enviar_a_asistente_compras` lo lee de acá.
        self._proveedor_id_actual: int | None = None
        self._proveedor_nombre_actual: str | None = None
        # FASE 1 multi-proveedor (plan 2026-09-16): un lote puede traer los
        # catálogos de VARIOS proveedores. La lista es el historial completo
        # de pares (proveedor, fuente de fotos) agregados a este lote; los dos
        # escalares de arriba siguen existiendo y apuntan SIEMPRE al proveedor
        # "en edición"/activo, porque decenas de lugares del código los leen y
        # el caso normal (un solo proveedor) tiene que seguir funcionando
        # exactamente igual. Con un solo proveedor la lista tiene un elemento
        # y no cambia ningún comportamiento.
        self._proveedores_del_lote: list[dict] = []
        self._indice_proveedor_actual: int = 0
        # La vista de candidatos ya NO es un Toplevel aparte: es una pantalla
        # de reemplazo más dentro de la ventana principal, igual que el
        # resumen final o la selección de limpieza.
        self._candidatos_frame: ctk.CTkFrame | None = None
        self._fotos_visor_recorte: dict[str, ImageTk.PhotoImage] = {}

        # Pantalla del paso 5 ("Vectorizar y comparar"): reemplaza al diálogo
        # emergente que preguntaba el método justo antes del envío.
        self._vector_frame: ctk.CTkFrame | None = None

        # ¿Hay workers de limpieza corriendo AHORA? Es lo que decide si el
        # monitor de avance se ve en el paso 3 fusionado ("Limpiar y revisar"):
        # mientras corre se ve arriba, y al terminar se retira solo y la
        # pantalla queda entera para decidir fotos.
        self._procesando_lote: bool = False

        # ── revisión previa de fotos extraídas de Excel ──
        self._revision_frame: tk.Frame | None = None
        self._fotos_revision: list[ImageTk.PhotoImage] = []
        self._archivos_revision: list[Path] = []
        self._exclusiones_revision: set[str] = set()
        self._marcos_revision: dict[str, tk.Frame] = {}
        self.v_estado_revision = tk.StringVar(value="")

        # Acá vivía el estado de la pantalla masiva "Elegir recortes a
        # limpiar" (`_limpieza_frame`, `_seleccion_recorte`,
        # `_seleccion_reparo`, `_seleccion_aprobar`, sus miniaturas y marcos).
        # Se eliminó el 2026-09-08: ofrecía las MISMAS dos correcciones que la
        # barra "CORREGIR ESTA FOTO" de la grilla de revisión, y el usuario
        # eligió esa experiencia ("me gusta más la pestaña revisar, queda más
        # bonito la parte de revisión manual"). La eficiencia de aplicar a
        # varias fotos de una pasada se conserva con la selección múltiple que
        # la grilla YA tenía (Ctrl/Shift+clic → barra de acciones en bloque).

        # Cuántas columnas entran AHORA en cada grilla de miniaturas, para no
        # re-acomodar en cada evento de `<Configure>` si el número no cambió
        # (Tk emite `<Configure>` a cada píxel de arrastre del borde).
        self._cols_grilla: dict[str, int] = {}

        self._armar_ui()
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        # `after_idle`, no llamada directa: acá la ventana todavía no está
        # mapeada (eso pasa recién en mainloop()), así que `winfo_width()` da
        # basura (200) y el diálogo de arranque se centraba mal — quedaba
        # pegado a la esquina superior izquierda. Confirmado con captura real.
        # Reloj de arena mientras la ventana termina de armarse y aparece el
        # primer diálogo (retomar/nuevo proyecto) -- ese primer instante en
        # blanco es justo cuando más parece que el programa no abrió.
        self.configure(cursor="watch")
        self.after(600, lambda: self.configure(cursor=""))
        self.after_idle(self._cargar_ultima_carpeta)
        self.after(150, self._bombear_cola)

    def _geometria_que_entre(self, ancho: int, alto: int) -> None:
        """Abre la ventana del tamaño pedido, pero nunca más grande que la
        pantalla — y centrada.

        Bug real (medido 2026-09-07): customtkinter multiplica la geometría
        por el escalado de Windows, así que el `geometry("1240x760")` de toda
        la vida se convertía en una ventana de 1550x950 px reales sobre un
        monitor de 1536x864. La franja de abajo quedaba fuera de la pantalla.
        Antes eso solo escondía la línea de log; con el asistente por pasos,
        lo que se perdía era el botón «Siguiente →» — o sea, la app entera.
        """
        try:
            from customtkinter.windows.widgets.scaling import ScalingTracker
            escala = ScalingTracker.get_window_scaling(self) or 1.0
        except Exception:  # noqa: BLE001
            escala = 1.0
        # Margen para la barra de tareas y el borde de la ventana.
        max_ancho = int((self.winfo_screenwidth() - 40) / escala)
        max_alto = int((self.winfo_screenheight() - 90) / escala)
        ancho, alto = min(ancho, max_ancho), min(alto, max_alto)
        x = max(0, int((self.winfo_screenwidth() - ancho * escala) / 2))
        y = max(0, int((self.winfo_screenheight() - alto * escala) / 3))
        self.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _al_cerrar(self) -> None:
        """Si hay un proceso de limpieza vivo (corriendo o pausado) al cerrar
        la ventana, hay que reanudarlo antes de matarlo — un proceso que
        `psutil` dejó suspendido queda congelado para siempre si se cierra
        así nomás: invisible, sin ventana, reteniendo memoria y bloqueando
        archivos de la carpeta de trabajo hasta que alguien lo mate a mano
        desde el Administrador de tareas."""
        vivos = [p for p in self._procs if p.poll() is None]
        if vivos:
            if self._pausado:
                for p in vivos:
                    try:
                        psutil.Process(p.pid).resume()
                    except psutil.Error:
                        pass
            for p in vivos:
                p.terminate()
        self.destroy()

    # ── tema visual ──────────────────────────────────────────────────────

    def _armar_estilo(self) -> None:
        """Toda la interfaz es customtkinter, así que ya no hay nada de ttk
        que estilizar (la tabla de texto que quedaba se reemplazó por la
        grilla de tarjetas `GrillaRecortes`). Se deja igual el tema base y los
        tokens de texto/fondo por si algún diálogo del sistema hereda de ahí
        — así ninguna ventana nace con el gris nativo de Windows."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=COLOR_PANEL, foreground=COLOR_TEXTO,
                        fieldbackground=COLOR_PANEL, font=FUENTE_BASE,
                        borderwidth=0, relief="flat")

    # ── construcción de la ventana ───────────────────────────────────────

    # ══ construcción de la ventana: asistente por pasos ═════════════════
    # Rediseño 2026-09-07. Antes la ventana era UNA pantalla con tres barras
    # de herramientas apiladas arriba (11 controles compitiendo a la vez) que
    # seguían visibles incluso en pantallas donde no servían de nada — y con
    # la fila de contexto tan llena que los textos se pisaban entre sí
    # ("0 aprobada(s) de 20" encima de "Carpeta activa: …", visto en captura).
    #
    # Ahora es un asistente: CABECERA que dice en qué paso se está, UNA
    # pantalla a la vez en el medio, y un PIE fijo con "← Atrás" y
    # "Siguiente →". Todo lo que no es el camino principal (elegir qué
    # limpiar, resumen, candidatos, ajustes, caché, revertir) vive en el menú
    # "⋮ Más opciones" del pie.
    #
    # Los widgets y las variables conservan sus nombres de siempre: cambia
    # DÓNDE viven y CUÁNDO se ven, no la lógica que los lee ni los comandos
    # que disparan.

    # "Confirmar y enviar" (2026-09-08): daba la falsa sensación de ser el último
    # paso -- reclamo real del usuario, tenía razón: esta pantalla es un
    # CHECKPOINT antes de enviar al Asistente de Compras (paso 4), no el
    # final del flujo. "Confirmar y enviar" deja claro que falta un paso más.
    # Un paso = una cosa que el comprador hace (2026-09-08, pedido repetido del
    # usuario). Antes el paso 2 ("Revisar fotos") metía TRES trabajos distintos
    # en una sola pantalla: elegir cuáles de las fotos extraídas entran, correr
    # la limpieza, y decidir foto por foto. Eran tres, y ahora se numeran como
    # tres — el mapa del proceso es el mismo siempre, aunque un origen puntual
    # no tenga que pararse en todas las paradas (ver `_ir_a_paso`).
    # Fusión de "Limpiar fotos" + "Revisar y decidir" en UN solo paso
    # (2026-09-16, pedido del dueño: "se parecen mucho, deberían ser uno").
    # Eran dos pastillas para la MISMA pantalla: `_ir_a_paso` mostraba
    # `self.cuerpo` en los dos casos, y la única diferencia era que en el 3
    # arriba se veía el monitor de avance. Encima la grilla de abajo ya se
    # llenaba EN VIVO (cada evento "foto" del worker llama a
    # `_recargar_manifiesto`, que es diferencial), así que el comprador ya
    # podía decidir fotos mientras el lote seguía corriendo — y aun así el
    # asistente lo obligaba a tocar "Continuar" para "pasar" a una pantalla
    # idéntica. Ahora es un paso: el monitor está arriba mientras corre y se
    # retira solo al terminar (ver `_actualizar_paso`), y las fotos van
    # apareciendo abajo listas para decidir.
    #
    # "Vectorizar y comparar" (paso 5) reemplaza al diálogo emergente que
    # preguntaba el método justo antes de enviar: elegir el espacio de
    # vectores, calcular los vectores de cada candidato y comparar contra el
    # catálogo es trabajo real, con minutos de espera, así que tiene su propia
    # pantalla con su progreso en vez de esconderse en un popup.
    PASOS_FLUJO = {
        1: "Elegir proveedor y origen",
        2: "Elegir qué limpiar",
        3: "Limpiar y revisar",
        4: "Confirmar y enviar",
        5: "Vectorizar y comparar",
        6: "Candidatos calificados",
        7: "Sugerido de compra",
    }

    # Rótulo corto para las pastillas de progreso. Con 7 pasos, los títulos
    # completos no entran en una hilera (en una pantalla de portátil la fila se
    # cortaba por el borde y los últimos pasos quedaban invisibles). El título
    # largo sigue leyéndose entero abajo, en el encabezado del paso actual.
    PASOS_PILL = {
        1: "Proveedor",
        2: "Qué limpiar",
        3: "Limpiar y revisar",
        4: "Confirmar",
        5: "Vectorizar",
        6: "Candidatos",
        7: "Sugerido",
    }

    GUIA_PASOS = {
        1: "Decime de quién es el catálogo y dónde están las fotos. "
           "Después tocá «Siguiente».",
        2: "Estas son las fotos que salieron del catálogo. Hacé clic en las que "
           "no son calzado (logos, portadas) para dejarlas afuera. "
           "Después, «Continuar».",
        3: "Tocá «▶ Limpiar fotos»: recorta el fondo y repara lo que puede. "
           "Cada foto que termina aparece abajo y ya se puede decidir "
           "(aprobar o descartar), sin esperar al resto del lote.",
        4: "Este es el resultado del lote. Desde acá se exporta y se manda al "
           "Asistente de Compras.",
        5: "Elegí con qué método se vectoriza y tocá «Calcular y continuar»: "
           "se calculan los vectores de cada candidato y se comparan contra el "
           "catálogo. Puede tardar varios minutos.",
        6: "Candidatos ya calificados, con su grado de compra y sus "
           "comparables.",
        7: "Poné el PDV objetivo y generá el pedido sugerido: cuántos pares de "
           "cada referencia conviene comprar.",
    }

    # Guía del paso 3 MIENTRAS el lote se está limpiando: lo que el comprador
    # tiene que saber ahí no es "tocá Limpiar fotos" (ya lo tocó), es que puede
    # empezar a trabajar sobre lo que ya salió.
    GUIA_LIMPIANDO = ("Se están limpiando las fotos. A medida que cada una "
                      "termina aparece abajo y ya podés decidirla: no hace "
                      "falta esperar a que termine todo el lote.")

    # Guía alterna del paso 3 cuando el lote viene de una sesión anterior: las
    # fotos ya están limpias, lo único que falta es decidirlas.
    GUIA_RETOMANDO = ("Estas fotos ya se limpiaron antes. Decidí foto por foto: "
                      "aprobar o descartar. Cuando termines, «Continuar».")

    # Qué dice el botón principal en cada paso. En el 4 no hay siguiente: es
    # el final del camino, así que el botón queda apagado en vez de
    # desaparecer (un botón que se va de su lugar desorienta más que uno gris).
    #
    # Paso 2 (2026-09-08, reclamo real del usuario): "Terminar lote" daba la
    # falsa sensación de ser el último paso -- igual que pasaba con "Lote
    # terminado" (renombrado a "Confirmar y enviar"). Esto SOLO avanza a esa
    # pantalla de confirmación, no termina nada. "Continuar" es honesto sobre
    # lo que hace: seguir al siguiente paso.
    TEXTO_SIGUIENTE = {
        1: "Siguiente  →",
        2: "Continuar  →",
        3: "Continuar  →",
        # Paso 4: el botón de pie ES la acción. Dice "Continuar  →" como en el
        # resto del asistente y queda en el mismo lugar de siempre, pero
        # `_paso_siguiente` le manda la lógica inteligente de compras
        # (`_accion_boton_compras`): manda al paso de vectorización si el lote
        # nunca se envió, o abre los candidatos si ya estaba enviado. Antes
        # había un botón aparte dentro de la tarjeta y este se escondía; el
        # usuario pidió un único botón, con el nombre y la posición del resto
        # (2026-09-09).
        4: "Continuar  →",
        # Paso 5: el botón de pie dispara el cálculo real (vectores +
        # similitud), igual que el botón grande de la pantalla. Se llama por lo
        # que hace, no "Continuar": son varios minutos de trabajo.
        5: "Calcular y continuar  →",
        6: "Armar sugerido  →",
        7: "Siguiente  →",
    }

    def _armar_ui(self) -> None:
        self._armar_cabecera()
        self._armar_pantalla_inicio()
        self._armar_monitor()
        self._armar_cuerpo_revision()
        self._armar_pie()

        self.v_log = tk.StringVar(value="")
        self._lbl_log = Etiqueta(self, textvariable=self.v_log, style="Suave.TLabel",
                                 padding=(24, 2))
        # Oculta cuando no tiene texto -- reclamo del usuario en el paso 4:
        # "46 recorte(s) ya procesados encontrados en esta carpeta" quedaba
        # anotado UNA vez al retomar el lote y de ahí en más ocupaba una
        # franja entera al pie de la ventana, vacía, sin volver a limpiarse.
        def _refrescar_visibilidad_log(*_a) -> None:
            if self.v_log.get().strip():
                if not self._lbl_log.winfo_ismapped():
                    self._lbl_log.pack(side="bottom", fill="x")
            elif self._lbl_log.winfo_ismapped():
                self._lbl_log.pack_forget()
        self.v_log.trace_add("write", _refrescar_visibilidad_log)

        self._paso_flujo = 1
        self._ir_a_paso(1)

    # ── cabecera: en qué paso estoy ──────────────────────────────────────

    def _armar_cabecera(self) -> None:
        cab = ctk.CTkFrame(self, fg_color=PAR_PANEL, corner_radius=0)
        cab.pack(side="top", fill="x")

        # Pastillas de progreso: los 7 pasos siempre a la vista, el actual
        # relleno de azul, los ya hechos en verde suave, los que faltan
        # apagados. Reemplazan a la línea de texto "Paso N de 4 · …", que no
        # dejaba ver ni dónde empieza ni cuánto falta. Un origen que no usa el
        # paso 2 lo ve en verde (pasado), no desaparecido: el mapa no cambia.
        #
        # Van en HILERA y en su propia fila, no apiladas al costado del
        # título: apiladas, las pastillas fijaban un piso de ~110px de alto
        # para toda la cabecera, y en una pantalla de portátil (donde la
        # ventana entra a 619px de alto) eso le comía al panel de decisión de
        # abajo justo los botones «✓ Aprobar / ✗ Descartar». Visto en captura.
        self._pills: dict[int, ctk.CTkLabel] = {}
        fila_pills = Marco(cab, padding=(26, 10, 26, 0))
        fila_pills.pack(fill="x")
        for n in sorted(self.PASOS_FLUJO):
            p = ctk.CTkLabel(fila_pills,
                             text=f"  {n}  {self.PASOS_PILL.get(n, self.PASOS_FLUJO[n])}  ",
                             font=F["chica"], anchor="w", height=26,
                             corner_radius=RADIO_CONTROL, fg_color="transparent",
                             text_color=PAR_TEXTO_SUAVE)
            p.pack(side="left", padx=(0, 6))
            self._pills[n] = p
        self._fila_pills = fila_pills

        fila = Marco(cab, padding=(26, 8, 26, 10))
        fila.pack(fill="x")
        # Este bloque (número de paso + titulazo + guía) ocupa ~110px y se
        # esconde cuando está abierta una de las dos grillas de elegir fotos
        # — ver `_compactar_cabecera`.
        self._bloque_titulo_paso = fila

        izq = Marco(fila)
        izq.pack(side="left", fill="x", expand=True)
        self.v_paso_num = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_num,
                 style="Paso.TLabel").pack(anchor="w", fill="x")
        self.v_paso_titulo = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_titulo,
                 style="Titulazo.TLabel").pack(anchor="w", fill="x", pady=(1, 0))
        self.v_paso_guia = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_guia, style="Guia.TLabel",
                 wraplength=780).pack(anchor="w", fill="x", pady=(3, 0))

        # Franja fina de contexto: proveedor/avance a la izquierda, carpeta
        # activa a la derecha. Separadas por lado, no apiladas en la misma
        # tira: así ya no pueden pisarse cuando cualquiera de las dos crece.
        franja = ctk.CTkFrame(cab, fg_color=PAR_PANEL_SUAVE, corner_radius=0)
        franja.pack(fill="x")
        interior_franja = Marco(franja, padding=(26, 5, 26, 5))
        interior_franja.pack(fill="x")
        self.v_paso_flujo = tk.StringVar(value="")
        Etiqueta(interior_franja, textvariable=self.v_paso_flujo,
                 style="Suave.TLabel").pack(side="left")
        self.v_carpeta_activa = tk.StringVar(
            value="Carpeta activa: ninguna todavía")
        Etiqueta(interior_franja, textvariable=self.v_carpeta_activa,
                 style="Suave.TLabel", anchor="e").pack(side="right")

        Separador(cab).pack(fill="x", side="bottom")

    # ── pantalla 1: proveedor + origen ───────────────────────────────────

    def _armar_pantalla_inicio(self) -> None:
        """Los tres datos con que arranca un catálogo, cada uno en su propia
        tarjeta y en el orden en que el comprador los tiene en la cabeza.
        Antes eran cinco controles apretados en la primera barra, mezclados
        con el engranaje de ajustes y sin ningún rótulo que dijera para qué
        sirve cada uno."""
        self.pantalla_inicio = Marco(self)

        # Con scroll (2026-09-17): las tarjetas de esta pantalla venían
        # creciendo sesión tras sesión (canal de venta, agregar proveedor...)
        # sin que nadie ajustara el contenedor -- en una ventana no maximizada
        # o con letra grande de Windows, la tarjeta "2 · ¿Dónde están las
        # fotos?" quedaba tapada abajo, sin ninguna forma de llegar a ella.
        # `CTkScrollableFrame` reemplaza al `Marco` plano de antes; el resto
        # de la pantalla no se entera del cambio porque expone `pack()` igual.
        scroll = ctk.CTkScrollableFrame(self.pantalla_inicio, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        col = Marco(scroll)
        col.pack(anchor="n", pady=(26, 0))
        # Regla de ancho: fija la columna en 740px sin tener que apagar la
        # propagación de tamaño (que obligaría a declarar también un alto).
        tk.Frame(col, width=740, height=1, bg=COLOR_FONDO).pack()

        def tarjeta(rotulo: str, guia: str) -> Marco:
            t = Tarjeta(col)
            t.pack(fill="x", pady=(0, 14))
            dentro = Marco(t, padding=(20, 16, 20, 18))
            dentro.pack(fill="x")
            Etiqueta(dentro, text=rotulo, style="Titulo.TLabel").pack(anchor="w", fill="x")
            Etiqueta(dentro, text=guia, style="Suave.TLabel",
                     wraplength=680).pack(anchor="w", fill="x", pady=(2, 12))
            return dentro

        # ── 1. proveedor ──
        caja_prov = tarjeta("1 · ¿De quién es este catálogo?",
                            "El proveedor se elige una vez y el resto del flujo lo "
                            "usa sin volver a preguntar.")
        fila_prov = Marco(caja_prov)
        fila_prov.pack(fill="x")
        self.v_proveedor = tk.StringVar(value="Proveedor: sin elegir")
        Boton(fila_prov, text="🏷  Elegir proveedor…", style="Tarjeta.TButton",
              width=230, command=self._elegir_proveedor_inicial).pack(side="left")
        Etiqueta(fila_prov, textvariable=self.v_proveedor,
                 style="Subtitulo.TLabel").pack(side="left", padx=(16, 0))
        # FASE 1 multi-proveedor: un lote puede traer los catálogos de dos (o
        # más) proveedores. Este botón NO reemplaza lo elegido: lo guarda en la
        # lista del lote y vuelve a pedir proveedor + origen para el siguiente.
        Boton(caja_prov, text="➕  Agregar otro proveedor a este lote",
              style="Sutil.TButton", width=300,
              command=self._agregar_otro_proveedor).pack(anchor="w", pady=(10, 0))
        self.v_lista_proveedores = tk.StringVar(
            value="Todavía no se agregó ningún proveedor a este lote.")
        Etiqueta(caja_prov, textvariable=self.v_lista_proveedores,
                 style="Suave.TLabel", justify="left",
                 wraplength=680).pack(anchor="w", fill="x", pady=(8, 0))

        # ── 1b. canal de venta del lote ──
        #
        # Decisión del dueño (2026-09-10): TU Calzado vende por dos canales y
        # una compra es para uno o para el otro. De esto depende contra QUÉ se
        # califica el catálogo entero (lo vendido en TC Marcas, o lo vendido en
        # TU Calzado), así que se pregunta acá, en el paso 1, y no al final.
        #
        # SIN opción preseleccionada a propósito: que el comprador la elija
        # activamente en vez de heredar en silencio un default que después
        # decide el puntaje de todo el lote.
        caja_canal = tarjeta("1b · ¿Para qué canal es esta compra?",
                             "De esto depende contra qué se compara y se califica todo "
                             "el catálogo: lo que se ha vendido en TC Marcas, o lo que "
                             "se ha vendido en TU Calzado. Se elige una vez por lote.")
        self.v_canal_venta = tk.StringVar(value="")
        fila_canal = Marco(caja_canal)
        fila_canal.pack(fill="x")
        ctk.CTkRadioButton(fila_canal, text="TC Marcas",
                           variable=self.v_canal_venta, value="marca",
                           command=self._guardar_canal_venta_elegido).pack(side="left")
        ctk.CTkRadioButton(fila_canal, text="TU Calzado",
                           variable=self.v_canal_venta, value="tuc",
                           command=self._guardar_canal_venta_elegido).pack(side="left", padx=(24, 0))
        self.v_canal_estado = tk.StringVar(value="Canal: sin elegir")
        Etiqueta(caja_canal, textvariable=self.v_canal_estado,
                 style="Suave.TLabel").pack(anchor="w", pady=(8, 0))

        # ── 2. origen de las fotos ──
        caja_org = tarjeta("2 · ¿Dónde están las fotos?",
                           "Una carpeta de imágenes, o un Excel / PDF / correo del que "
                           "hay que sacarlas primero.")
        menu_origen = tk.Menu(self, tearoff=False)
        menu_origen.add_command(label="Carpeta de imágenes…", command=self._elegir_entrada)
        menu_origen.add_command(label="Archivos sueltos…", command=self._elegir_archivos)
        menu_origen.add_separator()
        menu_excel = tk.Menu(menu_origen, tearoff=False)
        menu_excel.add_command(label="Carpeta de Excel…", command=self._elegir_carpeta_excel)
        menu_excel.add_command(label="Archivo(s) de Excel…", command=self._elegir_archivos_excel)
        menu_origen.add_cascade(label="Excel (fotos incrustadas)", menu=menu_excel)
        menu_pdf = tk.Menu(menu_origen, tearoff=False)
        menu_pdf.add_command(label="Carpeta de PDF…", command=self._elegir_carpeta_pdf)
        menu_pdf.add_command(label="Archivo(s) de PDF…", command=self._elegir_archivos_pdf)
        menu_origen.add_cascade(label="PDF (catálogo/preventa)", menu=menu_pdf)
        menu_msg = tk.Menu(menu_origen, tearoff=False)
        menu_msg.add_command(label="Carpeta de correos…", command=self._elegir_carpeta_msg)
        menu_msg.add_command(label="Archivo(s) de correo…", command=self._elegir_archivos_msg)
        menu_origen.add_cascade(label="Correo (.msg, adjuntos)", menu=menu_msg)

        fila_org = Marco(caja_org)
        fila_org.pack(fill="x")
        BotonMenu(fila_org, menu=menu_origen, text="📂  Elegir origen  ▾",
                  style="Tarjeta.TButton", width=230).pack(side="left")
        self.v_entrada = tk.StringVar()
        ctk.CTkEntry(fila_org, textvariable=self.v_entrada, corner_radius=RADIO_CONTROL,
                     height=40, font=F["base"], fg_color=PAR_PANEL_SUAVE,
                     border_color=PAR_BORDE, text_color=PAR_TEXTO,
                     placeholder_text="todavía sin elegir"
                     ).pack(side="left", fill="x", expand=True, padx=(12, 0))

        # ── 3. carpeta de trabajo (dato técnico: tarjeta más callada) ──
        caja_sal = tarjeta("3 · Carpeta de trabajo",
                           "Dónde deja el programa los recortes mientras trabaja. "
                           "Si no la cambiás, se reusa la última.")
        fila_sal = Marco(caja_sal)
        fila_sal.pack(fill="x")
        self.v_salida = tk.StringVar()
        entry_salida = ctk.CTkEntry(fila_sal, textvariable=self.v_salida,
                                    corner_radius=RADIO_CONTROL, height=36, font=F["base"],
                                    fg_color=PAR_PANEL_SUAVE, border_color=PAR_BORDE,
                                    text_color=PAR_TEXTO)
        entry_salida.pack(side="left", fill="x", expand=True)
        entry_salida.bind("<Return>", lambda _e: self._elegir_salida(usar_texto=True))
        Boton(fila_sal, text="Elegir…", command=self._elegir_salida,
              width=110, height=36).pack(side="left", padx=(10, 0))
        # "Reprocesar aunque ya esté hecho" es una OPCIÓN del procesamiento, no
        # una acción: acá, con el resto de la preparación del lote, en vez de
        # colgando de la barra de botones donde parecía un control más del día
        # a día.
        self.v_rehacer = tk.BooleanVar(value=False)
        Casilla(caja_sal, text="Reprocesar aunque ya esté hecho",
                variable=self.v_rehacer).pack(anchor="w", pady=(12, 0))

    # ── monitor de avance (visible solo mientras se procesa) ─────────────

    def _armar_monitor(self) -> None:
        self.monitor = Tarjeta(self)
        monitor_int = Marco(self.monitor, style="Panel.TFrame", padding=14)
        monitor_int.configure(corner_radius=0)
        monitor_int.pack(fill="x")

        fila_top = Marco(monitor_int)
        fila_top.pack(fill="x")
        Etiqueta(fila_top, text="Progreso general del lote",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_lamina_actual = tk.StringVar(value="")
        Etiqueta(fila_top, textvariable=self.v_lamina_actual,
                 style="Panel.TLabel").pack(anchor="w", fill="x")

        fila_barra = Marco(monitor_int)
        fila_barra.pack(fill="x", pady=(8, 0))
        self.barra_progreso = Barra(fila_barra, mode="determinate", maximum=100)
        self.barra_progreso.pack(side="left", fill="x", expand=True, pady=6)
        self.v_pct = tk.StringVar(value="0%")
        Etiqueta(fila_barra, textvariable=self.v_pct, width=44,
                 style="SuavePanel.TLabel").pack(side="left", padx=(8, 0))

        # ── tiempo estimado restante ──
        # Se calcula con el promedio real de segundos por lámina TERMINADA
        # (medido en `_bombear_cola`, ver `self._marcas_lamina`), no con una
        # cuenta teórica: con 0 láminas listas no hay promedio y decir un
        # número sería inventarlo, así que dice "Calculando…".
        self.v_eta = tk.StringVar(value="Tiempo estimado restante: calculando…")
        Etiqueta(monitor_int, textvariable=self.v_eta,
                 style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(2, 0))

        # ── aviso de "parece colgado" ──
        # No detiene nada: solo avisa. La decisión de cortar sigue siendo del
        # usuario, con el botón "Detener" que ya existe. Vive en un contenedor
        # propio para poder aparecer y desaparecer sin mover el resto.
        # `tk.Frame` y NO `Marco`/`CTkFrame`, por la misma causa raíz ya
        # documentada en `_zona_aviso`: un CTkFrame SIN hijos empaquetados pide
        # 200x200 (los `width`/`height` por defecto de CTk). Con dos zonas así
        # (esta y la de la consola), el monitor pedía ~900px de alto y se
        # comía TODA la ventana — medido en captura: con el lote corriendo, la
        # grilla de revisión de abajo no se veía en absoluto. Eso era
        # tolerable cuando "limpiar" era una pantalla aparte; con el paso
        # fusionado ("Limpiar y revisar") es justo lo que había que arreglar:
        # el progreso arriba y las fotos que van saliendo abajo, a la vez.
        self._zona_sin_actividad = tk.Frame(monitor_int, background=COLOR_PANEL)
        self._zona_sin_actividad.pack(fill="x")
        self._aviso_sin_actividad = ctk.CTkFrame(
            self._zona_sin_actividad, fg_color=PAR_ALERTA_BG,
            border_color=PAR_ALERTA, border_width=1, corner_radius=RADIO_CONTROL)
        self.v_sin_actividad = tk.StringVar(value="")
        Etiqueta(self._aviso_sin_actividad, textvariable=self.v_sin_actividad,
                 text_color=PAR_ALERTA, style="SubtituloPanel.TLabel",
                 padding=(10, 8), wraplength=1100).pack(anchor="w", fill="x")

        Separador(monitor_int).pack(fill="x", pady=(10, 8))

        fila_top_lamina = Marco(monitor_int)
        fila_top_lamina.pack(fill="x")
        Etiqueta(fila_top_lamina, text="Lámina actual",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_paso_actual = tk.StringVar(value="")
        Etiqueta(fila_top_lamina, textvariable=self.v_paso_actual,
                 style="SuavePanel.TLabel", wraplength=1100).pack(anchor="w", fill="x",
                                                                  pady=(2, 0))

        fila_barra_lamina = Marco(monitor_int)
        fila_barra_lamina.pack(fill="x", pady=(8, 0))
        self.barra_progreso_lamina = Barra(fila_barra_lamina, mode="determinate", maximum=100)
        self.barra_progreso_lamina.pack(side="left", fill="x", expand=True, pady=6)
        self.v_pct_lamina = tk.StringVar(value="")
        Etiqueta(fila_barra_lamina, textvariable=self.v_pct_lamina, width=80,
                 style="SuavePanel.TLabel").pack(side="left", padx=(8, 0))

        self._armar_consola_detalle(monitor_int)

    def _ocultar_monitor(self) -> None:
        """Saca el monitor de avance de la pantalla (no lo destruye: el mismo
        widget se vuelve a empaquetar en el próximo `_procesar`)."""
        monitor = getattr(self, "monitor", None)
        if monitor is None:
            return
        try:
            if monitor.winfo_manager():
                monitor.pack_forget()
        except tk.TclError:
            pass

    # ── consola de detalle técnico (colapsada por defecto) ───────────────

    def _armar_consola_detalle(self, padre) -> None:
        """Log crudo de los procesos worker. Va OCULTO por defecto porque el
        usuario final no es técnico y no le dice nada; existe para cuando algo
        parece colgado y hay que ver si de verdad se movió algo."""
        self._consola_abierta = False

        fila_toggle = Marco(padre)
        fila_toggle.pack(fill="x", pady=(10, 0))
        self.btn_consola = Boton(fila_toggle, text="▸ Ver detalle técnico",
                                 style="Sutil.TButton", width=200, height=30,
                                 command=self._alternar_consola)
        self.btn_consola.pack(side="left")

        # `tk.Frame` vacío = 1x1; un CTkFrame vacío pediría 200x200 y con la
        # consola colapsada eso era alto muerto dentro del monitor (ver
        # `_zona_sin_actividad`).
        self._zona_consola = tk.Frame(padre, background=COLOR_PANEL)
        self._zona_consola.pack(fill="x")
        self.txt_consola = ctk.CTkTextbox(
            self._zona_consola, height=170, corner_radius=RADIO_CONTROL,
            fg_color=("#12161c", "#0c0f13"), text_color=("#d7dde5", "#d7dde5"),
            border_color=PAR_BORDE, border_width=1,
            scrollbar_button_color=PAR_BORDE,
            scrollbar_button_hover_color=PAR_TEXTO_SUAVE,
            font=("Consolas", 11), activate_scrollbars=True, wrap="none")
        self.txt_consola.configure(state="disabled")
        # No se empaqueta acá: arranca colapsada.

    def _alternar_consola(self) -> None:
        self._consola_abierta = not getattr(self, "_consola_abierta", False)
        if self._consola_abierta:
            if not self._zona_consola.winfo_manager():
                self._zona_consola.pack(fill="x")
            self.txt_consola.pack(fill="x", pady=(6, 0))
            self.btn_consola.configure(text="▾ Ocultar detalle técnico")
        else:
            self.txt_consola.pack_forget()
            # También se saca la ZONA, no solo la caja de texto: un contenedor
            # que ya tuvo alto no vuelve solo a 1px, así que al cerrar la
            # consola quedaban ~220px muertos dentro del monitor — y con el
            # paso 3 fusionado ese alto se lo come la grilla de revisión.
            self._zona_consola.pack_forget()
            self.btn_consola.configure(text="▸ Ver detalle técnico")

    _MAX_LINEAS_CONSOLA = 500

    def _limpiar_consola(self) -> None:
        """Vacía el log al arrancar un lote nuevo — mezclar el detalle de dos
        lotes distintos es peor que no tenerlo."""
        caja = getattr(self, "txt_consola", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.delete("1.0", "end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass
        self._lineas_consola = 0

    def _log_consola(self, texto: str) -> None:
        """Agrega una línea cruda al log y deja la vista al final. Recorta las
        más viejas: un lote largo escupe miles de líneas y retenerlas todas
        haría crecer la memoria sin techo."""
        caja = getattr(self, "txt_consola", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.insert("end", texto.rstrip("\r\n") + "\n")
            self._lineas_consola = getattr(self, "_lineas_consola", 0) + 1
            if self._lineas_consola > self._MAX_LINEAS_CONSOLA:
                sobran = self._lineas_consola - self._MAX_LINEAS_CONSOLA
                caja.delete("1.0", f"{sobran + 1}.0")
                self._lineas_consola = self._MAX_LINEAS_CONSOLA
            caja.see("end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    # ── pantalla 2: revisar fotos (lista + visor + decisión) ─────────────

    def _armar_cuerpo_revision(self) -> None:
        self.cuerpo = cuerpo = Marco(self)

        # ── franja de "estás retomando un lote" ──
        # Va en un contenedor propio empaquetado ARRIBA y ANTES que las tres
        # columnas: las columnas van con `side="left"` y se comen todo el alto,
        # así que una franja empaquetada después (cuando ya hay columnas) se
        # quedaría sin cavidad y Tk la desmaparía en silencio — el mismo
        # problema que ya había pasado con la franja de "corregir esta foto".
        # `tk.Frame` y NO `Marco`/`CTkFrame` a propósito — misma razón que
        # `_pie_lote` unas líneas más abajo, y causa raíz del bug medido el
        # 2026-09-09 ("al cerrar el aviso amarillo la pantalla queda cortada y
        # pequeña"): un CTkFrame SIN hijos empaquetados pide 200x200 (los
        # `width`/`height` por defecto de CTk), y esta zona va con `fill="x"`
        # sin `expand`, así que pack le entregaba sus ~200px de ALTO igual —
        # con el aviso escondido y también en un lote nuevo que nunca lo tuvo.
        # Al hacer `pack_forget()` del aviso, la zona volvía a pedir 200px en
        # vez de 1px, así que las tres columnas NO recuperaban el espacio: se
        # veía exactamente como si la franja siguiera ahí.
        # Un `tk.Frame` vacío pide 1x1, así que sin aviso no ocupa nada y los
        # hermanos elásticos (`izq`/`centro`/`der`, todos con `fill`) se
        # reacomodan solos en cuanto el aviso se cierra.
        self._zona_aviso = tk.Frame(cuerpo, background=COLOR_FONDO)
        self._zona_aviso.pack(side="top", fill="x")
        self._armar_aviso_retomar()

        # ── columna izquierda: lista de recortes ──
        izq = Tarjeta(cuerpo, width=310)
        izq.pack(side="left", fill="y", padx=(14, 7), pady=12)
        izq.pack_propagate(False)

        self._lbl_titulo_recortes = Etiqueta(izq, text="Recortes", padding=(14, 14, 14, 0),
                                             style="SubtituloPanel.TLabel")
        self._lbl_titulo_recortes.pack(anchor="w", fill="x")
        # Se guarda en un atributo porque se esconde mientras hay selección
        # múltiple: son 60px de alto en un panel de 236px, y en ese momento la
        # pista ya cumplió su función (el usuario acaba de hacer el Ctrl+clic
        # que explica). Ese espacio es justo el que necesita la barra de
        # acciones en bloque para poder mostrarse.
        self._lbl_ayuda_recortes = Etiqueta(
            izq, text="Hacé clic en una foto para verla en grande y decidir.  "
                      "Ctrl+clic marca varias, Shift+clic marca un rango.",
            style="SuavePanel.TLabel", padding=(14, 2, 14, 6), wraplength=268)
        self._lbl_ayuda_recortes.pack(anchor="w", fill="x")

        # ── barra de acciones en bloque ──
        # Se arma ANTES que la grilla porque la grilla avisa de cambios de lote
        # apenas se puebla, y el aviso toca estos botones. Vive al PIE del
        # panel de la lista (no en el panel de detalle de la derecha, que habla
        # siempre de UNA foto) y aparece sola con 2+ marcadas.
        # El PIE del panel de la lista se reserva ACÁ, antes de empaquetar la
        # grilla, y nunca se desempaqueta.
        #
        # Bug real medido (2026-09-08), preexistente y no introducido por las
        # acciones nuevas: `izq` mide 236px en la ventana por defecto (774px de
        # alto, que es como abre en la pantalla de 864px del usuario), y la
        # barra en bloque pedía ~190px. La grilla va con `expand=True` y se
        # empaquetaba ANTES, así que se quedaba con toda la cavidad y cuando la
        # barra se empaquetaba después (al marcar la 2da foto) no quedaba
        # espacio: Tk la DESMAPEA en silencio -> `winfo_ismapped() == 0`,
        # tamaño real 1x1. O sea "Descartar seleccionados" y "Desechar
        # definitivamente" en bloque no se veían NUNCA salvo con la ventana
        # maximizada (ahí `izq` llega a 453px). Es el mismo problema que ya
        # estaba documentado para `_barra_corregir` unas líneas más abajo.
        #
        # La solución es la misma: el bloque de altura fija se empaqueta primero
        # (con `side="bottom"`), así se queda con su espacio pase lo que pase, y
        # el que se aprieta es la grilla, que es elástica y además scrolleable.
        # `_pie_lote` es el contenedor permanente; lo que aparece y desaparece
        # es la barra de adentro.
        # `tk.Frame` y NO `Marco`/`CTkFrame` a propósito: un CTkFrame vacío pide
        # 200x200 por defecto (medido: con la barra escondida este contenedor
        # seguía pidiendo 250px de alto y se quedaba con 105px del panel), y
        # como se empaqueta primero le robaba la franja visible a la grilla —
        # las tarjetas de recortes desaparecían de la lista. Un `tk.Frame`
        # vacío pide 1x1, así que mientras no hay selección múltiple este pie
        # no ocupa nada.
        self._pie_lote = tk.Frame(izq, background=COLOR_PANEL)
        self._pie_lote.pack(side="bottom", fill="x", padx=6, pady=(0, 8))

        self._barra_lote = Marco(self._pie_lote, style="Panel.TFrame", padding=(8, 6))
        self._barra_lote.configure(corner_radius=0)

        fila_titulo_lote = Marco(self._barra_lote)
        fila_titulo_lote.pack(fill="x")
        self.v_lote = tk.StringVar(value="")
        Etiqueta(fila_titulo_lote, textvariable=self.v_lote,
                 style="SubtituloPanel.TLabel").pack(side="left")
        self.btn_lote_cancelar = Boton(fila_titulo_lote, text="✕", width=28, height=24,
                                       style="Sutil.TButton",
                                       command=self._cancelar_lote)
        self.btn_lote_cancelar.pack(side="right")

        # Dos botones por fila, no uno por fila a lo largo.
        #
        # Son 5 acciones: apiladas a ancho completo la barra pedía 328px, más
        # que el alto entero del panel (236px) — no había forma de que entrara.
        # En dos columnas mide ~140px y entra en la ventana por defecto, que es
        # el requisito real: una barra que solo aparece maximizado es una barra
        # que no existe.
        rejilla_lote = Marco(self._barra_lote)
        rejilla_lote.pack(fill="x", pady=(6, 0))
        rejilla_lote.columnconfigure((0, 1), weight=1, uniform="lote")

        def celda_lote(texto, comando, fila, col, estilo=None, **kw):
            b = Boton(rejilla_lote, text=texto, height=30, command=comando,
                      **({"style": estilo} if estilo else {}), **kw)
            b.grid(row=fila, column=col, sticky="ew", padx=(0, 4) if col == 0 else (4, 0),
                   pady=2)
            return b

        # Las MISMAS dos correcciones de la barra "CORREGIR ESTA FOTO", pero
        # sobre las N marcadas. Esto es lo que reemplaza a la pantalla masiva
        # "Elegir recortes a limpiar" que existía aparte (eliminada 2026-09-08):
        # el usuario dijo que prefiere la experiencia de Revisar, así que la
        # eficiencia de "aplicar a varias de una pasada" se trae ACÁ en vez de
        # mantener una segunda pantalla con las mismas acciones.
        #
        # Arriba y con el mismo icono que los de una sola foto, para que se
        # lean como "lo mismo, pero a las marcadas".
        self.btn_lote_reparar = celda_lote("🔧 Suela", self._reparar_suela_lote,
                                           0, 0, "Primario.TButton")
        self.btn_lote_recortar = celda_lote("✂ Sombra", self._recortar_sombra_lote,
                                            0, 1, "Primario.TButton")
        # "Aprobar en bloque" es el reemplazo de la casilla "Aprobar" que tenía
        # cada tarjeta de la pantalla masiva (con su "Marcar todas para
        # aprobar"): el caso real de un lote donde la mayoría de las fotos ya
        # están bien y no necesitan ninguna corrección.
        self.btn_lote_aprobar = celda_lote("✓ Aprobar", self._aprobar_lote,
                                           1, 0, "Aprobar.TButton")
        self.btn_lote_descartar = celda_lote("✗ Descartar", self._descartar_lote,
                                             1, 1, "Descartar.TButton")
        # El irreversible va solo, a lo ancho, separado de los otros cuatro:
        # es el único que borra archivos del disco.
        self.btn_lote_desechar = Boton(self._barra_lote, text="🗑 Desechar definitivamente",
                                       style="Peligro.TButton", height=30,
                                       command=self._desechar_lote)
        self.btn_lote_desechar.pack(fill="x", pady=(6, 0))

        # Grilla de tarjetas con la FOTO de cada recorte. `GrillaRecortes`
        # expone la misma API mínima que exponía el Treeview, así que la
        # lógica que la puebla y la lee no cambió.
        self.tabla = GrillaRecortes(izq,
                                    proveedor_png=lambda n: self._recortes.get(n, {}).get("png"),
                                    componer=self._componer_sobre_gris,
                                    al_cambiar_lote=self._al_cambiar_lote)
        self.tabla.pack(fill="both", expand=True, padx=7, pady=(0, 10))
        self.tabla.bind("<<TreeviewSelect>>", self._seleccionar)
        self.tabla.tag_configure("aprobado", foreground=COLOR_OK)
        self.tabla.tag_configure("descartado", foreground=COLOR_MAL)

        # ── centro: visor ──
        centro = Tarjeta(cuerpo)
        centro.pack(side="left", fill="both", expand=True, pady=12)

        barra_visor = Marco(centro, style="Panel.TFrame", padding=(14, 12))
        barra_visor.configure(corner_radius=0)
        barra_visor.pack(side="top", fill="x")
        Etiqueta(barra_visor, text="Recorte con líneas de referencia (azul = borde real, "
                                   "roja = curva esperada de la suela)",
                 style="SuavePanel.TLabel", wraplength=640).pack(side="left")
        self.chk_antes_despues = Casilla(
            barra_visor, text="Ver antes / después", variable=self._ver_antes_despues,
            command=self._mostrar_imagen)
        self.chk_antes_despues.pack(side="right")
        self.chk_antes_despues.state(["disabled"])

        # El visor grande sigue siendo un `tk.Label`: es el que recibe los
        # `ImageTk.PhotoImage` que arma el pipeline de imagen, y CTkLabel
        # espera CTkImage — cambiarlo obligaría a tocar el código de imagen.
        # ── el PIE del panel central se reserva ANTES del visor ──
        # Causa raíz del bug que el usuario reportó varias veces ("le doy clic
        # a una foto y los botones de Corregir esta foto no aparecen"),
        # confirmada por captura de pantalla el 2026-09-08:
        #
        #   `marco_visor` va con `expand=True`, pero su tamaño PEDIDO lo fija
        #   la foto que tiene adentro (`lbl_imagen` con un PhotoImage de
        #   480x480 -> pide 484px de alto). `pack` reparte en ORDEN de
        #   empaquetado: le daba al visor sus 484px enteros y, cuando llegaba
        #   a la franja de corregir (empaquetada después, al seleccionar),
        #   ya no quedaba cavidad -> Tk la DESMAPEA en silencio. Medido:
        #   `centro` mide 581px y sus hijos pedían 704px; la franja quedaba
        #   con `winfo_ismapped() == 0`. Agrandar o maximizar la ventana no
        #   ayudaba porque una ventana más alta escala la foto más grande y
        #   el visor vuelve a pedir todo.
        #
        # La solución es la misma que ya usaba el panel de la derecha con
        # `der_pie`: los bloques de altura FIJA del pie se empaquetan primero
        # (con `side="bottom"`), así se quedan con su espacio pase lo que
        # pase, y el que se aprieta es el visor, que es elástico. Además
        # `pack_propagate(False)` en el visor: el tamaño de la foto ya no
        # puede inflar lo que el visor pide.
        self.v_paso_proceso = tk.StringVar(value="")
        self._lbl_paso_proceso = Etiqueta(centro, textvariable=self.v_paso_proceso,
                                          style="SuavePanel.TLabel",
                                          anchor="center", justify="center")
        # Oculta cuando no tiene texto (que es la mayor parte del tiempo
        # revisando fotos): antes quedaba SIEMPRE empaquetada, vacía, y era
        # el "espacio desperdiciado debajo de Corregir esta foto" que
        # reportó el usuario -- una fila en blanco con su padding propio.
        def _refrescar_visibilidad_paso_proceso(*_a) -> None:
            if self.v_paso_proceso.get().strip():
                if not self._lbl_paso_proceso.winfo_ismapped():
                    self._lbl_paso_proceso.pack(side="bottom", fill="x", pady=(0, 4))
            elif self._lbl_paso_proceso.winfo_ismapped():
                self._lbl_paso_proceso.pack_forget()
        self.v_paso_proceso.trace_add("write", _refrescar_visibilidad_paso_proceso)

        # ── corregir la foto, JUSTO DEBAJO de la foto ──
        # Pedido del usuario (2026-09-07): "cuando le doy clic a una foto...
        # me gustaría que los botones de Corregir esta foto estén de forma
        # más visible, tal vez debajo de la foto" -- vivían en el panel
        # lateral derecho (`der_int`, scrollable, lejos de la vista si hay
        # que bajar). Misma fila horizontal, pegada al visor: es lo primero
        # que se ve al mirar la foto, no algo que haya que ir a buscar.
        #
        # Rediseño 2026-09-08 (pedido: "rediseña la ventana", "que sea
        # imposible no verla"): no es más un `Marco` transparente que se
        # funde con el panel blanco de atrás, es una TARJETA con fondo azul
        # suave, borde azul de 2px y esquinas redondeadas. Contra el blanco
        # del panel se lee como un bloque aparte, no como "más panel".
        self._barra_corregir = ctk.CTkFrame(centro, fg_color=PAR_PRIMARIO_SUAVE,
                                            border_color=PAR_PRIMARIO,
                                            border_width=2,
                                            corner_radius=RADIO_PANEL)
        # Dos filas a propósito: el título + la casilla arriba, los 3 botones
        # abajo. En UNA sola fila los 4 controles piden ~970px y la ventana
        # puede bajar a 900px de ancho (`minsize`): a esa altura pack recorta
        # el último botón por la derecha, y volvía a pasar lo mismo que se
        # está arreglando -- un control que existe pero no se ve. Con las
        # filas separadas entra hasta en el ancho mínimo.
        # Compactado 2026-09-09 (pedido: "más abajo y más pequeño, sin perder
        # ningún botón"): mismo diseño de 2 filas, pero con los paddings y el
        # alto de los botones recortados para devolverle ~30px de alto al
        # visor. Los 3 botones siguen presentes y con el mismo comando.
        fila_titulo = Marco(self._barra_corregir)
        fila_titulo.pack(fill="x", padx=12, pady=(5, 0))
        Etiqueta(fila_titulo, text="✎  CORREGIR ESTA FOTO",
                 text_color=PAR_PRIMARIO,
                 style="SubtituloPanel.TLabel").pack(side="left", padx=(0, 16))
        self.chk_lineas = Casilla(fila_titulo, text="Ver líneas azul / roja",
                                  variable=self._ver_lineas, command=self._mostrar_imagen)
        self.chk_lineas.pack(side="left")

        fila_botones = Marco(self._barra_corregir)
        fila_botones.pack(fill="x", padx=12, pady=(4, 7))
        # "Primario.TButton" (relleno azul lleno) en vez del gris por defecto
        # -- pedido del usuario (2026-09-07): "no se ven bien, deberían verse
        # más visibles". Son las 2 acciones de corrección reales de esta
        # pantalla, no controles de rutina como "Ver antes/después".
        self.btn_reparar_suela = Boton(fila_botones, text="🔧 Reparar suela",
                                       style="Primario.TButton", height=30,
                                       command=self._reparar_suela_actual)
        self.btn_reparar_suela.pack(side="left", padx=(0, 8))
        self.btn_recortar_sombra = Boton(fila_botones, text="✂ Recortar sombra",
                                         style="Primario.TButton", height=30,
                                         command=self._recortar_sombra_actual)
        self.btn_recortar_sombra.pack(side="left", padx=(0, 8))
        # Antes iba con "Sutil.TButton" (sin relleno, texto gris): sobre el
        # azul suave de la tarjeta casi no se distinguía. Con relleno blanco
        # y borde azul se ve como botón, y sigue siendo el más callado de los
        # tres.
        self.btn_revertir_recorte = Boton(fila_botones, text="↺ Revertir correcciones",
                                          height=30, fg_color=PAR_PANEL,
                                          hover_color=PAR_BORDE,
                                          text_color=PAR_PRIMARIO,
                                          border_width=1, border_color=PAR_PRIMARIO,
                                          command=self._revertir_recorte_actual)
        self.btn_revertir_recorte.pack(side="left")
        for b in (self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        # Se empaqueta acá, ANTES del visor, para quedarse con su espacio; y
        # se esconde enseguida. `_seleccionar_real` la vuelve a mostrar con
        # `before=self._marco_visor`, que la reinserta en esta misma posición
        # del orden de pack -- si se empaquetara al final volvería el bug.
        self._barra_corregir.pack(side="bottom", fill="x", padx=14, pady=(0, 6))
        self._barra_corregir.pack_forget()

        self._marco_visor = ctk.CTkFrame(centro, fg_color=PAR_VISOR_BG,
                                         corner_radius=RADIO_PANEL, height=200)
        self._marco_visor.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self._marco_visor.pack_propagate(False)
        self.lbl_imagen = tk.Label(self._marco_visor, anchor="center",
                                   background=COLOR_VISOR_BG,
                                   relief="flat", borderwidth=0, highlightthickness=0)
        self.lbl_imagen.pack(fill="both", expand=True, padx=2, pady=2)
        # Reescalar la foto abierta cuando el visor cambia de alto/ancho: es
        # lo que hace que al cerrar la franja amarilla de "retomando" (o al
        # maximizar) la imagen se vuelva a ajustar al espacio nuevo en vez de
        # quedarse con la medida vieja y verse cortada.
        self._medida_visor: tuple[int, int] | None = None
        self._marco_visor.bind("<Configure>", self._al_redimensionar_visor)

        # ── derecha: panel de estado + decisión ──
        # 330 y no 272 (2026-09-09): el bloque de decisión + el destructivo
        # entran completos sin que el scroll de `der_int` tenga que recortar
        # nada en la ventana por defecto -- el reclamo era justamente que la
        # parte de confirmar quedaba achicada dentro de un panel con scroll.
        der = Tarjeta(cuerpo, width=330)
        der.pack(side="left", fill="y", padx=(7, 14), pady=12)
        der.pack_propagate(False)

        # PIE del panel, reservado ANTES que el cuerpo: la acción destructiva
        # se empaqueta primero con `side="bottom"` para que se quede con su
        # espacio pase lo que pase, y sea el contenido de arriba el que se
        # apriete si la ventana es baja.
        der_pie = Marco(der, style="Panel.TFrame", padding=(16, 0, 16, 12))
        der_pie.configure(corner_radius=0)
        der_pie.pack(side="bottom", fill="x")

        # Scrollable, no un Marco fijo: en una portátil la ventana entra a
        # ~619px de alto y este panel se quedaba sin espacio para sus últimos
        # bloques — pack los recortaba en silencio y desaparecían «% de suela
        # faltante», «Limpieza automática» y, lo grave, los botones
        # «✓ Aprobar / ✗ Descartar». Confirmado en captura antes de esto.
        # Expone la misma API de contenedor, así que los hijos se arman igual.
        der_int = ctk.CTkScrollableFrame(der, fg_color="transparent",
                                         corner_radius=0)
        der_int.pack(side="top", fill="both", expand=True, padx=(12, 4), pady=(12, 0))

        self._badges_frame = Marco(der_int)
        self._badges_frame.pack(fill="x", pady=(8, 4))
        self._badge_labels: list[tk.Label] = []

        Etiqueta(der_int, text="% de suela faltante",
                 style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(10, 0))
        self.v_falta = tk.StringVar(value="—")
        Etiqueta(der_int, textvariable=self.v_falta,
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")

        Separador(der_int).pack(fill="x", pady=8)

        Etiqueta(der_int, text="Limpieza automática",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_auto_desc = tk.StringVar(value="—")
        Etiqueta(der_int, textvariable=self.v_auto_desc, style="SuavePanel.TLabel",
                 wraplength=200).pack(anchor="w", fill="x", pady=(4, 8))
        self.btn_deshacer_auto = Boton(der_int, text="Deshacer limpieza automática",
                                       command=self._alternar_limpieza_auto)
        self.btn_deshacer_auto.pack(fill="x")
        self.btn_deshacer_auto.state(["disabled"])

        Separador(der_int).pack(fill="x", pady=8)

        # "Aprobar / Descartar" vive en el PIE del panel, NO en la parte que
        # scrollea: es LA acción del paso 2 y no puede quedar debajo del
        # pliegue. Con el panel scrollable (necesario para que en una portátil
        # no se recorten los bloques de arriba), dejarla adentro la escondía
        # justo en las ventanas donde más falta hace.
        Etiqueta(der_pie, text="Decisión de catálogo", style="SubtituloPanel.TLabel"
                 ).pack(anchor="w", fill="x", pady=(10, 6))
        fila_decision = Marco(der_pie)
        fila_decision.pack(fill="x")
        self.btn_aprobar = Boton(fila_decision, text="✓ Aprobar", style="Aprobar.TButton",
                                 height=38, command=lambda: self._decidir("aprobado"))
        self.btn_aprobar.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.btn_descartar = Boton(fila_decision, text="✗ Descartar", style="Descartar.TButton",
                                   height=38, command=lambda: self._decidir("descartado"))
        self.btn_descartar.pack(side="left", fill="x", expand=True)
        self.btn_aprobar.state(["disabled"])
        self.btn_descartar.state(["disabled"])

        # Bloque destructivo: en el PIE del panel, separado del par
        # Aprobar/Descartar por una línea y estilo de contorno rojo oscuro.
        Separador(der_pie).pack(fill="x", pady=(0, 10))
        Etiqueta(der_pie, text="Si la foto no sirve para nada:",
                 style="SuavePanel.TLabel", wraplength=230
                 ).pack(anchor="w", fill="x", pady=(0, 5))
        self.btn_desechar = Boton(der_pie, text="🗑 Desechar definitivamente",
                                  style="Peligro.TButton",
                                  command=self._desechar_seleccionado)
        self.btn_desechar.pack(fill="x")
        self.btn_desechar.state(["disabled"])

    # ── franja "estás retomando un lote" (paso 2) ────────────────────────
    # Pedido del usuario (2026-09-08): "si estoy retomando una carga anterior,
    # cómo podemos mejorar esta pantalla". Hasta hoy el paso 2 se veía IGUAL
    # retomando un lote de la semana pasada que arrancando uno nuevo: misma
    # guía ("Tocá ▶ Limpiar fotos…"), mismo botón azul de limpiar de primero,
    # y el único indicio de que había algo viejo cargado era la línea gris
    # chiquita de la esquina superior derecha (`v_carpeta_activa`) — que dice
    # una ruta larga y se lee como decoración, no como información.
    #
    # Retomar y empezar son dos tareas distintas: quien retoma NO necesita
    # limpiar nada, necesita seguir DECIDIENDO fotos. Así que la pantalla ahora
    # lo dice de frente, con el número que de verdad importa (cuántas quedan
    # por decidir) y de dónde vino el lote.

    def _armar_aviso_retomar(self) -> None:
        self._aviso_retomar = ctk.CTkFrame(self._zona_aviso, fg_color=PAR_ALERTA_BG,
                                           border_color=PAR_ALERTA, border_width=1,
                                           corner_radius=RADIO_PANEL)
        # Franja de UNA sola línea a propósito (reclamo del usuario, varias
        # veces: le quitaba espacio valioso al visor de foto grande, que vive
        # justo debajo). Antes eran 3 líneas de texto (título + detalle +
        # instrucción) cada una con su propia altura -- ahora es una sola
        # etiqueta con todo el texto junto, y padding mínimo.
        dentro = Marco(self._aviso_retomar, padding=(10, 4, 8, 4))
        dentro.pack(fill="x")

        # El botón de cerrar va PRIMERO (side="right"): así conserva su ancho
        # aunque el texto de la izquierda crezca con una ruta larga.
        Boton(dentro, text="✕", style="Sutil.TButton", width=28, height=22,
              command=self._ocultar_aviso_retomar).pack(side="right", padx=(8, 0))
        self.btn_aviso_seguir = Boton(dentro, text="↓ Ver las pendientes",
                                      style="Sutil.TButton", height=22,
                                      command=self._ir_a_primera_pendiente)
        self.btn_aviso_seguir.pack(side="right")

        # Las 3 variables se conservan (otras partes del código las llenan
        # por separado) pero se muestran TODAS en una sola etiqueta de una
        # línea, uniéndolas con " · " -- ver `_refrescar_texto_aviso_retomar`.
        self.v_aviso_retomar_titulo = tk.StringVar(value="")
        self.v_aviso_retomar_detalle = tk.StringVar(value="")
        self.v_aviso_retomar_pie = tk.StringVar(value="")
        self.v_aviso_retomar_linea = tk.StringVar(value="")
        Etiqueta(dentro, textvariable=self.v_aviso_retomar_linea,
                 text_color=PAR_ALERTA, style="SuavePanel.TLabel",
                 anchor="w").pack(side="left", fill="x", expand=True)

        def _refrescar_texto_aviso_retomar(*_a) -> None:
            partes = [v.get() for v in (self.v_aviso_retomar_titulo,
                                        self.v_aviso_retomar_detalle,
                                        self.v_aviso_retomar_pie) if v.get().strip()]
            self.v_aviso_retomar_linea.set("   ·   ".join(partes))
        for v in (self.v_aviso_retomar_titulo, self.v_aviso_retomar_detalle,
                  self.v_aviso_retomar_pie):
            v.trace_add("write", _refrescar_texto_aviso_retomar)

    def _ocultar_aviso_retomar(self) -> None:
        """Cerrar la franja no "termina" de retomar: solo la saca de la vista
        en esta pantalla. El botón del pie sigue rotulado como corresponde."""
        if self._aviso_retomar.winfo_manager():
            self._aviso_retomar.pack_forget()
            # `_zona_aviso` es un `tk.Frame`, así que al quedar sin hijos pide
            # 1x1 y pack devuelve el alto a las tres columnas. Se asienta el
            # layout YA y se reescala la foto abierta a la cavidad nueva, en
            # vez de esperar a que el usuario mueva la ventana.
            self.update_idletasks()
            if hasattr(self, "_marco_visor"):
                self._al_redimensionar_visor()

    def _ir_a_primera_pendiente(self) -> None:
        """Selecciona la primera foto sin decidir del lote retomado — que es
        exactamente lo que el comprador vino a hacer. Reusa la selección de
        siempre: `selection_set` ya emite `<<TreeviewSelect>>`, que es lo que
        dispara `_seleccionar`. No hay lógica nueva de decisión acá."""
        pendientes = [n for n in self.tabla.get_children()
                      if self._recortes.get(n, {}).get("estado", {}).get("decision")
                      not in (decisiones.APROBADO, decisiones.DESCARTADO)]
        if not pendientes:
            self.v_log.set("No quedan fotos pendientes de decidir en este lote.")
            return
        self.tabla.selection_set(pendientes[0])

    def _refrescar_aviso_retomar(self) -> None:
        """Pone (o saca) la franja y ajusta el rótulo del botón de limpiar,
        según si lo que hay en pantalla es un lote retomado o uno nuevo."""
        if not hasattr(self, "_aviso_retomar"):
            return
        retomando = bool(self._retomando_lote and self._recortes
                         and getattr(self, "_paso_flujo", 1) == 3)

        # El botón de limpiar cambia de rótulo Y de peso visual. Retomando, no
        # es la acción principal de la pantalla (decidir fotos lo es), y su
        # nombre real es otro: lo que hace es buscar fotos NUEVAS del mismo
        # origen y sumarlas al lote (las ya procesadas se saltan solas, salvo
        # que se pida reprocesar).
        if hasattr(self, "btn_procesar"):
            if retomando:
                self.btn_procesar.configure(
                    text="＋ Agregar más fotos", fg_color="transparent",
                    hover_color=PAR_PANEL_SUAVE, text_color=PAR_TEXTO_SUAVE,
                    border_width=1, border_color=PAR_BORDE, font=F["base"])
            else:
                self.btn_procesar.configure(
                    text="▶ Limpiar fotos", fg_color=PAR_PRIMARIO,
                    hover_color=PAR_PRIMARIO_OSCURO, text_color=("#ffffff", "#ffffff"),
                    border_width=0, font=F["subtitulo"])

        if not retomando:
            self._ocultar_aviso_retomar()
            return

        pendientes = sum(1 for r in self._recortes.values()
                         if r.get("estado", {}).get("decision")
                         not in (decisiones.APROBADO, decisiones.DESCARTADO))
        self.v_aviso_retomar_titulo.set(
            "↻  Estás retomando un lote de una sesión anterior")
        partes = [f"{len(self._recortes)} foto(s) ya limpias",
                  f"{pendientes} pendiente(s) de decidir" if pendientes
                  else "todas decididas"]
        origen = self._origen_del_lote_legible(self._salida) if self._salida else ""
        # Los lotes hechos antes de que se empezara a anotar el origen no lo
        # tienen: se dice así, en vez de callarlo y dejar al comprador
        # adivinando por qué "Agregar más fotos" le pide una carpeta.
        partes.append(f"origen: {origen}" if origen
                      else "origen: no quedó anotado (lote viejo)")
        self.v_aviso_retomar_detalle.set("   ·   ".join(partes))
        self.v_aviso_retomar_pie.set(
            "No hace falta limpiar nada de nuevo: seguí decidiendo las fotos de "
            "la izquierda." if pendientes else
            "Ya están todas decididas: podés seguir con «Continuar».")
        self.btn_aviso_seguir.state(["disabled"] if not pendientes else ["!disabled"])
        if not self._aviso_retomar.winfo_manager():
            self._aviso_retomar.pack(side="top", fill="x", padx=14, pady=(12, 0))

    # ── pie fijo: navegación del asistente ───────────────────────────────

    def _armar_pie(self) -> None:
        pie_ext = ctk.CTkFrame(self, fg_color=PAR_PANEL, corner_radius=0)
        pie_ext.pack(side="bottom", fill="x")
        Separador(pie_ext).pack(fill="x", side="top")

        pie = Marco(pie_ext, padding=(24, 12, 24, 14))
        pie.pack(fill="x")

        self.btn_atras = Boton(pie, text="←  Atrás", style="NavAtras.TButton",
                               width=120, command=self._paso_atras)
        self.btn_atras.pack(side="left")

        # Todo lo ocasional o irreversible sale de la cara principal y entra
        # acá. Son las mismas funciones de siempre, con los mismos comandos:
        # lo único que cambia es que ya no ocupan un botón permanente.
        menu_mas = tk.Menu(self, tearoff=False)
        # Acá estaba "Elegir qué limpiar en detalle…", que abría la pantalla
        # masiva de casillas. Ya no existe: reparar/recortar sobre varias fotos
        # se hace marcándolas en la grilla de revisión (Ctrl/Shift+clic).
        menu_mas.add_command(label="Ver resumen del lote…",
                             command=self._mostrar_resumen_final)
        menu_mas.add_command(label="Ver candidatos calificados…",
                             command=self._ver_candidatos_calificados)
        menu_mas.add_separator()
        menu_mas.add_checkbutton(label="Reprocesar aunque ya esté hecho",
                                 variable=self.v_rehacer)
        menu_mas.add_command(label="Ajustes…", command=self._abrir_ajustes)
        menu_mas.add_separator()
        menu_mas.add_command(label="🧹 Limpiar caché (vaciar carpeta de trabajo)…",
                             command=self._vaciar_carpeta_trabajo)
        menu_mas.add_command(label="Revertir TODO…", command=self._revertir_todo)
        self.btn_mas = BotonMenu(pie, menu=menu_mas, text="⋮  Más opciones",
                                 style="Sutil.TButton", width=150, height=44)
        self.btn_mas.pack(side="left", padx=(10, 0))

        self.btn_siguiente = Boton(pie, text="Siguiente  →",
                                   style="Nav.TButton", width=190,
                                   command=self._paso_siguiente)
        self.btn_siguiente.pack(side="right")

        # Control del proceso: SOLO en el paso 2. En el resto de las pantallas
        # no hay nada que procesar, y tenerlos ahí era ruido puro.
        self._grupo_proceso = Marco(pie)
        # "Limpiar fotos" en vez de "Procesar" (2026-09-08, reclamo real del
        # usuario: "no entiendo qué hace el botón Procesar") -- lo que hace
        # de verdad es extraer y limpiar el fondo de las fotos del catálogo
        # elegido en el paso 1, convirtiéndolas en los recortes que se
        # revisan acá abajo. "Procesar" no decía nada de eso.
        self.btn_procesar = Boton(self._grupo_proceso, text="▶ Limpiar fotos",
                                  style="Primario.TButton", height=44, width=150,
                                  command=self._procesar)
        self.btn_procesar.pack(side="left")
        self.btn_pausar = Boton(self._grupo_proceso, text="⏸ Pausar", height=44, width=110,
                                command=self._pausar_reanudar, state="disabled")
        self.btn_pausar.pack(side="left", padx=(8, 0))
        self.btn_detener = Boton(self._grupo_proceso, text="⏹ Detener", height=44, width=110,
                                 command=self._detener, state="disabled")
        self.btn_detener.pack(side="left", padx=(8, 0))
        self._grupo_proceso.pack(side="right", padx=(0, 16))

        self.v_estado_proc = tk.StringVar(value="Sin iniciar")
        Etiqueta(pie, textvariable=self.v_estado_proc,
                 style="Suave.TLabel").pack(side="left", padx=(20, 0))

    # ── flujo por pasos ──────────────────────────────────────────────────

    def _mostrar_cuerpo(self) -> None:
        """Muestra la pantalla de revisión de fotos (pasos 3 y 4) y esconde la de
        inicio. Reemplaza a los `self.cuerpo.pack(...)` sueltos que había
        repartidos por el archivo: con el asistente, mostrar el cuerpo
        implica SIEMPRE sacar del medio la pantalla 1 — si no, las dos
        quedaban empaquetadas a la vez y la ventana mostraba media y media.

        Usa `winfo_manager()` y no `winfo_ismapped()` a propósito: durante
        `__init__` todavía no hay nada mapeado (eso pasa recién en
        `mainloop()`), así que `ismapped` daría 0 para widgets que YA están
        empaquetados y esto los duplicaría."""
        if self.pantalla_inicio.winfo_manager():
            self.pantalla_inicio.pack_forget()
        if not self.cuerpo.winfo_manager():
            self.cuerpo.pack(side="top", fill="both", expand=True)
        # Al salir de una grilla de elegir fotos hay que reponer la cabecera
        # completa, y por acá pasan todas las vueltas al cuerpo principal.
        self._sincronizar_cabecera()

    def _ocultar_pantallas_base(self) -> None:
        """Saca del medio las DOS pantallas base del asistente (inicio y
        revisión) antes de que una pantalla de reemplazo — revisión de
        extracción, selección de limpieza, resumen final, candidatos — se
        empaquete en su lugar. Antes cada una hacía solo
        `self.cuerpo.pack_forget()`; ahora que existe la pantalla 1, olvidarse
        de ella dejaría las dos apiladas."""
        if self.cuerpo.winfo_manager():
            self.cuerpo.pack_forget()
        if self.pantalla_inicio.winfo_manager():
            self.pantalla_inicio.pack_forget()

    def _hay_fotos_para_elegir(self) -> bool:
        """¿El paso 2 («Elegir qué limpiar») tiene algo que mostrar?

        Solo lo tiene cuando el origen fue un Excel, un PDF o un correo: ahí la
        extracción deja una lista de fotos candidatas y hace falta sacar los
        logos y portadas. Si el origen es una carpeta (o archivos) de imágenes
        que el comprador ya eligió a mano, no hay nada que elegir de nuevo."""
        return bool(getattr(self, "_archivos_revision", None))

    def _ir_a_paso(self, paso: int) -> None:
        """Cambia la pantalla visible del asistente -- con un cursor de
        espera mientras arma la pantalla nueva.

        Reclamo del usuario (2026-09-09): al cambiar de paso, sobre todo si
        implica armar una grilla grande o cargar imágenes, no había ninguna
        señal de que el programa estuviera trabajando -- se veía igual que si
        se hubiera colgado. El cursor de reloj de arena (`watch`) es la señal
        mínima universal de Windows para "esperá, estoy trabajando", visible
        apenas se mueve el mouse sobre la ventana."""
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self._ir_a_paso_interna(paso)
        finally:
            self.configure(cursor="")

    def _ir_a_paso_interna(self, paso: int) -> None:
        """Cambia la pantalla visible del asistente.

        Maneja los pasos 1 a 3 (inicio, elegir qué limpiar, limpiar y revisar)
        y el 5 (vectorizar); el 4, 6 y 7 los muestran `_mostrar_resumen_final`,
        `_mostrar_vista_candidatos` y `_mostrar_sugerido_compra`.

        El paso 2 SE SALTA SOLO cuando no aplica (origen = carpeta de
        imágenes): el total de pastillas no cambia — el mapa del proceso es
        siempre el mismo — pero ese origen simplemente no tiene esa parada, y
        el comprador no ve nada ni tiene que tocar un clic de más."""
        if paso == 2 and not self._hay_fotos_para_elegir():
            self._ir_a_paso(3)
            return
        if paso == 1:
            self._asegurar_cuerpo_visible()
            self.cuerpo.pack_forget()
            if self.monitor.winfo_manager():
                self.monitor.pack_forget()
            if not self.pantalla_inicio.winfo_manager():
                self.pantalla_inicio.pack(side="top", fill="both", expand=True)
        elif paso == 2:
            # La grilla de fotos extraídas se reconstruye con la selección tal
            # como quedó: volver atrás y adelante no pierde lo descartado.
            self._mostrar_revision_extraccion(self._archivos_revision,
                                              exclusiones=self._exclusiones_revision)
            return  # ya fija el paso por su cuenta
        elif paso == 5:
            self._mostrar_pantalla_vectorizacion()
            return  # ya fija el paso por su cuenta
        else:
            self._mostrar_cuerpo()
        self._actualizar_paso(paso)

    def _paso_siguiente(self) -> None:
        """El botón principal del pie. Valida lo mínimo de cada pantalla antes
        de dejar avanzar, en vez de fallar más adelante con un error técnico.
        Avanza SIEMPRE de un paso al siguiente, nunca saltea (el único salto es
        el automático del paso 2 cuando no aplica)."""
        paso = getattr(self, "_paso_flujo", 1)
        if paso == 1:
            if self._proveedor_id_actual is None:
                messagebox.showinfo(
                    "Falta el proveedor",
                    "Elegí primero de qué proveedor es este catálogo.")
                return
            if not self.v_entrada.get().strip():
                messagebox.showinfo(
                    "Falta el origen",
                    "Elegí de dónde salen las fotos con «📂 Elegir origen».")
                return
            # El canal decide contra qué se califica TODO el lote: no se deja
            # avanzar con la decisión sin tomar (y sin default heredado).
            if self.v_canal_venta.get().strip().lower() not in self.ETIQUETA_CANAL:
                messagebox.showinfo(
                    "Falta el canal de venta",
                    "Decime si esta compra es para TC Marcas o para TU Calzado.\n\n"
                    "De eso depende contra qué se buscan los comparables y con qué "
                    "se califica el catálogo entero.")
                return
            # FASE 1 multi-proveedor: lo elegido en pantalla queda anotado en
            # su slot, y si el lote tiene varios proveedores se arranca por el
            # primero que todavía no se limpió (los demás se procesan después,
            # uno por uno, al terminar el paso 3).
            self._sincronizar_slot_actual()
            incompletos = [n for n, s in enumerate(self._proveedores_del_lote, start=1)
                           if s.get("proveedor_id") is None
                           or not (s.get("entrada_dir") or s.get("entrada_archivos"))]
            if incompletos:
                messagebox.showinfo(
                    "Falta completar un proveedor",
                    "Estos proveedores del lote quedaron sin proveedor o sin "
                    f"origen de fotos: {', '.join(str(n) for n in incompletos)}.\n\n"
                    "Completalos o volvé a elegirlos antes de continuar.")
                return
            if self._lote_multiproveedor():
                pendiente = self._indice_proveedor_pendiente("limpiado")
                if pendiente is not None and pendiente != self._indice_proveedor_actual:
                    self._activar_slot(pendiente)
            self._ir_a_paso(2)
        elif paso == 2:
            self._confirmar_seleccion_revision()
        elif paso == 3:
            if not self._recortes:
                messagebox.showinfo(
                    "Todavía no hay fotos limpias",
                    "Tocá «▶ Limpiar fotos» y esperá a que salga la primera.")
                return
            # Con el paso fusionado, "Continuar" se puede tocar mientras el
            # lote TODAVÍA está corriendo (la grilla se llena en vivo). Salir
            # ahí dejaría fotos apareciendo en una pantalla que el comprador ya
            # dejó atrás, así que se avisa en vez de avanzar a medias.
            if getattr(self, "_procesando_lote", False):
                messagebox.showinfo(
                    "El lote todavía se está limpiando",
                    "Faltan fotos por limpiar. Podés seguir decidiendo las que "
                    "ya están, o usar «⏹ Detener» si querés cerrar el lote "
                    "con lo que hay hasta ahora.")
                return
            # FASE 1 multi-proveedor: el proveedor activo ya pasó por la
            # limpieza; si el lote tiene otro proveedor sin limpiar, se ofrece
            # seguir con él (mismo pipeline, misma carpeta de trabajo) antes de
            # ir a confirmar y enviar.
            if self._proveedores_del_lote:
                self._slot_actual()["limpiado"] = True
                self._persistir_proveedores_del_lote()
                self._refrescar_resumen_proveedores()
            pendiente = self._indice_proveedor_pendiente("limpiado")
            if pendiente is not None and self._lote_multiproveedor():
                slot = self._proveedores_del_lote[pendiente]
                if messagebox.askyesno(
                        "Siguiente proveedor del lote",
                        f"Las fotos de {self._proveedor_nombre_actual} ya están "
                        "limpias.\n\nEste lote todavía tiene el catálogo de "
                        f"{slot.get('proveedor_nombre')} sin limpiar.\n\n"
                        "¿Seguir ahora con ese proveedor?"):
                    if self._activar_slot(pendiente):
                        self._ir_a_paso(2)
                        return
            self._mostrar_resumen_final()
        elif paso == 4:
            # Acá "Continuar" NO es solo navegar: lleva al paso de vectorizar
            # si el lote todavía no se envió, o abre los candidatos directo si
            # ya estaba enviado.
            self._accion_boton_compras()
        elif paso == 5:
            # El botón de pie hace lo mismo que el botón grande de la pantalla:
            # dispara el cálculo real (vectores + similitud de coseno).
            self._calcular_vectorizacion_y_continuar()
        elif paso == 6:
            self._mostrar_sugerido_compra()

    def _paso_atras(self) -> None:
        paso = getattr(self, "_paso_flujo", 1)
        if paso == 7:
            self._volver_a_candidatos()
        elif paso == 6:
            # Atrás es el paso 5 ("Vectorizar y comparar"), y de ahí el 4: la
            # secuencia se recorre igual en los dos sentidos. Si no hay lote
            # cargado (se entró a candidatos desde el menú), `_cerrar_vista…`
            # ya aterriza donde corresponde.
            self._cerrar_vista_candidatos()
            if self._salida and self._recortes:
                self._ir_a_paso(5)
        elif paso == 5:
            # Volver del paso de vectorización es volver a "Confirmar y enviar"
            # — salvo que el cálculo esté corriendo, que no se puede abandonar
            # a medias sin dejar la barra de progreso huérfana.
            if getattr(self, "_enviando_compras", False):
                messagebox.showinfo(
                    "Cálculo en curso",
                    "Se están calculando los vectores y la similitud. "
                    "Esperá a que termine: al terminar se abren los candidatos "
                    "calificados solo.")
                return
            self._mostrar_resumen_final()
        elif paso == 4:
            self._ocultar_resumen()
        elif paso == 3:
            # Si este origen no tiene paso 2, atrás es el 1: no se muestra una
            # pantalla vacía solo para respetar la numeración.
            self._ir_a_paso(2 if self._hay_fotos_para_elegir() else 1)
        elif paso == 2:
            self._ir_a_paso(1)

    def _actualizar_paso(self, paso: int | None = None) -> None:
        """Reescribe la cabecera (número, título, guía, pastillas), la franja
        de contexto y el pie de navegación. Se llama con un número cuando la
        app CAMBIA de etapa, y sin número cuando solo cambió el detalle
        (proveedor elegido, más fotos aprobadas) y hay que refrescar lo mismo."""
        if paso is not None:
            self._paso_flujo = paso
        paso = getattr(self, "_paso_flujo", 1)

        total_pasos = len(self.PASOS_FLUJO)
        self.v_paso_num.set(f"PASO {paso} DE {total_pasos}")
        self.v_paso_titulo.set(self.PASOS_FLUJO.get(paso, ""))
        # Retomando, la guía de siempre ("Tocá ▶ Limpiar fotos para extraer y
        # limpiar…") manda al comprador a hacer justo lo que NO tiene que
        # hacer: las fotos ya están limpias. Se le cuenta el paso que sí falta.
        if paso == 3 and getattr(self, "_procesando_lote", False):
            self.v_paso_guia.set(self.GUIA_LIMPIANDO)
        elif paso == 3 and self._retomando_lote and self._recortes:
            self.v_paso_guia.set(self.GUIA_RETOMANDO)
        else:
            self.v_paso_guia.set(self.GUIA_PASOS.get(paso, ""))

        for n, pill in getattr(self, "_pills", {}).items():
            if n == paso:
                pill.configure(fg_color=PAR_PRIMARIO, text_color=("#ffffff", "#ffffff"))
            elif n < paso:
                pill.configure(fg_color=PAR_OK_BG, text_color=PAR_OK)
            else:
                pill.configure(fg_color="transparent", text_color=PAR_TEXTO_SUAVE)

        partes = []
        if self._proveedor_nombre_actual:
            partes.append(f"Proveedor: {self._proveedor_nombre_actual}")
        else:
            partes.append("Proveedor: sin elegir")
        if paso in (3, 4) and self._recortes:
            aprobados = sum(1 for r in self._recortes.values()
                            if r.get("estado", {}).get("decision") == "aprobado")
            partes.append(f"{aprobados} aprobada(s) de {len(self._recortes)}")
        self.v_paso_flujo.set("   ·   ".join(partes))

        # Pie: qué se puede hacer desde acá.
        if hasattr(self, "btn_siguiente"):
            self.btn_siguiente.configure(text=self.TEXTO_SIGUIENTE.get(paso, "Siguiente  →"))
            self.btn_siguiente.state(["disabled"] if paso >= total_pasos else ["!disabled"])
            self.btn_atras.state(["disabled"] if paso <= 1 else ["!disabled"])
            # El botón de pie está visible en TODOS los pasos MENOS EL ÚLTIMO,
            # el 4 incluido: ahí es el que lleva a vectorizar / abre los
            # candidatos. Al reempacarlo se saca primero el grupo de proceso,
            # para que conserve su lugar a la izquierda (con `side="right"`, el
            # primero empacado queda más a la derecha).
            #
            # En el último paso (7, "Sugerido de compra") se OCULTA por
            # completo en vez de quedar deshabilitado (2026-09-09, pedido del
            # usuario): no hay paso 8, así que un botón "Siguiente  →" apagado
            # solo se veía como un control roto. "Atrás" sigue igual.
            if paso >= total_pasos:
                if self.btn_siguiente.winfo_manager():
                    self.btn_siguiente.pack_forget()
            elif not self.btn_siguiente.winfo_manager():
                if self._grupo_proceso.winfo_manager():
                    self._grupo_proceso.pack_forget()
                self.btn_siguiente.pack(side="right")
            # Los controles de proceso viven en el paso 3 ("Limpiar y
            # revisar"): ahí se dispara la limpieza, se pausa/detiene mientras
            # corre, y una vez terminada el mismo botón sirve para sumarle
            # fotos nuevas al lote ("＋ Agregar más fotos").
            if paso == 3:
                if not self._grupo_proceso.winfo_manager():
                    self._grupo_proceso.pack(side="right", padx=(0, 16))
            elif self._grupo_proceso.winfo_manager():
                self._grupo_proceso.pack_forget()

        # El monitor de avance (barra general + "Lámina actual" + consola de
        # detalle + aviso de timeout) habla SOLO de la limpieza de láminas: en
        # los pasos 4, 5, 6 y 7 no hay ninguna lámina corriendo, así que ahí
        # ocupaba pantalla mostrando el estado congelado del último lote —
        # incluidos los rótulos "Progreso general del lote" y "Lámina actual",
        # que en esas pantallas no significan nada. El avance de la
        # vectorización tiene su propia barra, en la pantalla del paso 5
        # (`self.barra_compras`), sin el bloque de lámina.
        #
        # Con el paso 3 fusionado ("Limpiar y revisar") el monitor ya no se
        # ata al NÚMERO de paso sino a si hay algo corriendo: aparece al tocar
        # «▶ Limpiar fotos» y se retira solo cuando el lote termina (o se
        # detiene). ESA es la transición automática de "progreso" a "revisión"
        # que antes costaba un clic en "Continuar" y un cambio de pantalla.
        if not (paso == 3 and getattr(self, "_procesando_lote", False)):
            self._ocultar_monitor()

        # Cabecera compacta mientras esté abierta una grilla de elegir fotos.
        # Se decide por ESTADO y no con un llamado suelto en cada pantalla:
        # `_actualizar_paso` también corre en refrescos de detalle (una foto
        # más aprobada), y un llamado suelto se desharía en el primer refresco.
        self._sincronizar_cabecera()

        # Última cosa: la franja de "retomando" y el rótulo del botón de
        # limpiar dependen del paso y del contenido, así que se recalculan
        # acá — el único lugar por donde pasan TODOS los cambios de pantalla.
        self._refrescar_aviso_retomar()


    def _sincronizar_cabecera(self) -> None:
        """Pone la cabecera compacta o completa según qué pantalla está abierta
        AHORA. Se decide por estado y no con llamados sueltos en cada pantalla:
        `_actualizar_paso` también corre en refrescos de detalle (una foto más
        aprobada), y un llamado suelto se desharía en el primer refresco."""
        # Antes también compactaba para la pantalla masiva "Elegir recortes a
        # limpiar" (eliminada 2026-09-08); queda la grilla del paso 2.
        # También en el paso 3 ("Limpiar y revisar") CUANDO YA HAY RECORTES: es
        # la pantalla con el visor de foto grande y tres columnas, y el bloque
        # grande de cabecera ("PASO 3 DE 7" + titulazo + la línea de guía) se
        # llevaba ~130px de alto — medido: con la ventana abierta el cuerpo se
        # quedaba con 462px de 774, y el visor de la foto con 199px de alto. Es
        # el "espacio desperdiciado" del reclamo, y esa pantalla ya se explica
        # sola (la pastilla dice «3 Limpiar y revisar» y la franja de contexto
        # dice de qué lote y cuántas van aprobadas).
        #
        # Con el paso fusionado, la cabecera SÍ se deja completa mientras el
        # lote está vacío: ahí el bloque grande es lo único que le dice al
        # comprador qué hacer ("Tocá ▶ Limpiar fotos"), y no hay ninguna grilla
        # todavía que necesite el espacio. En cuanto empiezan a caer recortes,
        # se compacta y el alto se va a la grilla y al visor.
        en_grilla_revision = (getattr(self, "_paso_flujo", 1) == 3
                              and bool(getattr(self, "_recortes", None)))
        self._compactar_cabecera(
            getattr(self, "_revision_frame", None) is not None
            or en_grilla_revision)

    def _compactar_cabecera(self, compacta: bool) -> None:
        """Esconde (o repone) el bloque grande de la cabecera — "PASO N DE 7",
        el titulazo y la línea de guía — dejando las pastillas de progreso y la
        franja de contexto.

        Es para las dos pantallas de elegir fotos en grilla. En una pantalla de
        864px (la del usuario) la ventana abre a 774 y la cabecera completa se
        lleva 224px: la grilla se quedaba con 191px de franja visible, menos de
        lo que mide una tarjeta, así que no se podía trabajar. Y no se pierde
        información al esconderlo: las pastillas siguen diciendo en qué paso
        está, y estas pantallas ya traen su propio título y su propia
        explicación arriba — el bloque grande decía lo mismo dos veces."""
        bloque = getattr(self, "_bloque_titulo_paso", None)
        if bloque is None:
            return
        if compacta:
            if bloque.winfo_manager():
                bloque.pack_forget()
        elif not bloque.winfo_manager():
            bloque.pack(fill="x", after=self._fila_pills)

    def _elegir_proveedor_inicial(self) -> None:
        """Paso 1 del flujo: de qué proveedor es el catálogo que se va a
        trabajar. Se guarda en el estado de la ventana y el resto del flujo
        lo lee sin volver a preguntar."""
        try:
            import motor_candidatos
            proveedores = motor_candidatos.listar_proveedores()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Proveedor", f"No se pudo consultar proveedores:\n{exc}")
            return
        eleccion = self._elegir_proveedor(proveedores)
        if eleccion is None:
            return
        self._proveedor_id_actual, self._proveedor_nombre_actual = eleccion
        self.v_proveedor.set(f"Proveedor: {self._proveedor_nombre_actual}")
        # Queda anotado en la carpeta del lote: así, al retomarlo otro día, el
        # asistente no vuelve a preguntar por un proveedor que ya se sabe.
        self._guardar_marca_lote(proveedor_id=self._proveedor_id_actual,
                                 proveedor_nombre=self._proveedor_nombre_actual)
        # FASE 1 multi-proveedor: el proveedor elegido queda anotado en el
        # "slot" en edición de la lista del lote, no solo en el escalar.
        self._sincronizar_slot_actual(solo_proveedor=True)
        self._actualizar_paso()

    # ── FASE 1 multi-proveedor: la lista de proveedores del lote ──────────
    #
    # Estructura de cada elemento (slot):
    #   {"proveedor_id": int, "proveedor_nombre": str,
    #    "entrada_dir": str|None, "entrada_archivos": list[str]|None,
    #    "limpiado": bool, "enviado": bool}
    # Se persiste completa en `lote_meta` bajo CLAVE_PROVEEDORES_LOTE (JSON,
    # mismo patrón que `costos_candidatos`/`categorias_candidatos`).
    CLAVE_PROVEEDORES_LOTE = "proveedores_lote"

    def _slot_actual(self) -> dict:
        """El slot en edición, creándolo si la lista todavía está vacía."""
        while len(self._proveedores_del_lote) <= self._indice_proveedor_actual:
            self._proveedores_del_lote.append(
                {"proveedor_id": None, "proveedor_nombre": None,
                 "entrada_dir": None, "entrada_archivos": None,
                 "limpiado": False, "enviado": False})
        return self._proveedores_del_lote[self._indice_proveedor_actual]

    def _sincronizar_slot_actual(self, solo_proveedor: bool = False) -> None:
        """Copia el estado vivo (proveedor + origen de fotos) al slot en
        edición y lo persiste. `solo_proveedor=True` cuando el usuario está
        corrigiendo de quién es el lote y no tocó el origen."""
        slot = self._slot_actual()
        slot["proveedor_id"] = self._proveedor_id_actual
        slot["proveedor_nombre"] = self._proveedor_nombre_actual
        if not solo_proveedor:
            if self._entrada_archivos:
                slot["entrada_archivos"] = [str(p) for p in self._entrada_archivos]
                slot["entrada_dir"] = None
            else:
                texto = self.v_entrada.get().strip()
                slot["entrada_archivos"] = None
                slot["entrada_dir"] = texto or None
        self._persistir_proveedores_del_lote()
        self._refrescar_resumen_proveedores()

    def _persistir_proveedores_del_lote(self) -> None:
        if not self._proveedores_del_lote:
            return
        self._guardar_marca_lote(
            **{self.CLAVE_PROVEEDORES_LOTE: self._proveedores_del_lote})

    def _restaurar_proveedores_del_lote(self, carpeta: Path) -> None:
        """Al retomar un lote: recupera la lista de proveedores agregados.

        Compatibilidad con lotes viejos (y con el caso de un solo proveedor,
        que sigue guardando además `proveedor_id`/`entrada_dir` sueltos): si
        no hay lista guardada, se arma una de un elemento con lo que haya."""
        datos = self._leer_marca_lote(carpeta)
        lista = datos.get(self.CLAVE_PROVEEDORES_LOTE)
        if isinstance(lista, list) and lista:
            self._proveedores_del_lote = [dict(s) for s in lista if isinstance(s, dict)]
        elif datos.get("proveedor_id") is not None:
            self._proveedores_del_lote = [{
                "proveedor_id": int(datos["proveedor_id"]),
                "proveedor_nombre": str(datos.get("proveedor_nombre") or ""),
                "entrada_dir": datos.get("entrada_dir"),
                "entrada_archivos": datos.get("entrada_archivos"),
                "limpiado": True, "enviado": bool(datos.get("enviado_en")),
            }]
        # El slot "en edición" tiene que ser el del proveedor que quedó activo
        # en la marca suelta del lote; si no, la próxima sincronización
        # escribiría los datos de un proveedor sobre el slot de otro.
        self._indice_proveedor_actual = max(0, len(self._proveedores_del_lote) - 1)
        activo = datos.get("proveedor_id")
        if activo is not None:
            for n, slot in enumerate(self._proveedores_del_lote):
                if slot.get("proveedor_id") == int(activo):
                    self._indice_proveedor_actual = n
                    break
        # FASE 3: lotes guardados antes de esta fase tienen la moneda/tipo de
        # cambio/margen como claves sueltas del lote. Se copian a los slots acá
        # mismo, al retomar el lote, para no volver a preguntar algo ya
        # contestado ni perder el tipo de cambio con el que se cotizó.
        self._migrar_config_precio_a_slots(datos, carpeta)
        self._refrescar_resumen_proveedores()

    def _resumen_proveedores_texto(self) -> str:
        if not self._proveedores_del_lote:
            return "Todavía no se agregó ningún proveedor a este lote."
        lineas = []
        for n, slot in enumerate(self._proveedores_del_lote, start=1):
            nombre = slot.get("proveedor_nombre") or "(sin proveedor)"
            if slot.get("entrada_archivos"):
                archivos = slot["entrada_archivos"]
                origen = (Path(archivos[0]).name if len(archivos) == 1
                          else f"{len(archivos)} archivo(s) de {Path(archivos[0]).parent.name}")
            elif slot.get("entrada_dir"):
                origen = Path(str(slot["entrada_dir"])).name or str(slot["entrada_dir"])
            else:
                origen = "(sin origen elegido)"
            marca = ""
            if slot.get("enviado"):
                marca = "  ✔ enviado"
            elif slot.get("limpiado"):
                marca = "  · fotos limpias"
            lineas.append(f"{n}. {nombre}  —  {origen}{marca}")
        return "\n".join(lineas)

    def _refrescar_resumen_proveedores(self) -> None:
        if hasattr(self, "v_lista_proveedores"):
            self.v_lista_proveedores.set(self._resumen_proveedores_texto())

    def _lote_multiproveedor(self) -> bool:
        """¿Este lote tiene más de un proveedor con proveedor elegido?"""
        return sum(1 for s in self._proveedores_del_lote
                   if s.get("proveedor_id") is not None) > 1

    def _agregar_otro_proveedor(self) -> None:
        """Botón «➕ Agregar otro proveedor a este lote» del paso 1.

        No reemplaza nada: guarda lo ya elegido en su slot, abre un slot nuevo
        y vuelve a pedir proveedor + origen. Lo elegido antes queda intacto en
        la lista (y en `lote_meta`)."""
        if self._proveedor_id_actual is None or not self.v_entrada.get().strip():
            messagebox.showinfo(
                "Agregar otro proveedor",
                "Primero completá el proveedor y el origen de las fotos del "
                "proveedor que estás cargando ahora.\n\nDespués sí podés "
                "agregar otro proveedor a este mismo lote.")
            return
        self._sincronizar_slot_actual()
        # Slot nuevo al final, y el estado vivo queda en blanco para que el
        # usuario elija el proveedor/origen del segundo catálogo sin que se
        # pise el primero.
        self._indice_proveedor_actual = len(self._proveedores_del_lote)
        self._slot_actual()
        self._proveedor_id_actual = None
        self._proveedor_nombre_actual = None
        self._entrada_archivos = None
        self._archivos_revision = []
        self._exclusiones_revision = set()
        self.v_entrada.set("")
        self.v_proveedor.set("Proveedor: sin elegir")
        self._refrescar_resumen_proveedores()
        self._actualizar_paso()
        self._elegir_proveedor_inicial()

    def _activar_slot(self, indice: int) -> bool:
        """Pone en el estado vivo el proveedor/origen del slot `indice`, para
        procesarlo con el pipeline de limpieza existente (pasos 2-4)."""
        if not (0 <= indice < len(self._proveedores_del_lote)):
            return False
        slot = self._proveedores_del_lote[indice]
        if slot.get("proveedor_id") is None:
            return False
        self._indice_proveedor_actual = indice
        self._proveedor_id_actual = int(slot["proveedor_id"])
        self._proveedor_nombre_actual = str(slot.get("proveedor_nombre") or "")
        self.v_proveedor.set(f"Proveedor: {self._proveedor_nombre_actual}")
        archivos = slot.get("entrada_archivos")
        if archivos:
            self._entrada_archivos = [Path(a) for a in archivos]
            self.v_entrada.set(f"{len(archivos)} archivo(s) seleccionado(s)")
        else:
            self._entrada_archivos = None
            self.v_entrada.set(str(slot.get("entrada_dir") or ""))
        # El paso 2 (elegir qué limpiar) ya se resolvió cuando se eligió el
        # origen de este slot: su resultado son los archivos de arriba.
        self._archivos_revision = []
        self._exclusiones_revision = set()
        # El proveedor activo también manda en la marca suelta del lote, que
        # es la que leen `_restaurar_proveedor_del_lote` y el envío.
        self._guardar_marca_lote(proveedor_id=self._proveedor_id_actual,
                                 proveedor_nombre=self._proveedor_nombre_actual)
        self._refrescar_resumen_proveedores()
        self._actualizar_paso()
        return True

    def _indice_proveedor_pendiente(self, clave: str) -> int | None:
        """Primer slot del lote con `clave` (`limpiado`/`enviado`) en False."""
        for n, slot in enumerate(self._proveedores_del_lote):
            if slot.get("proveedor_id") is not None and not slot.get(clave):
                return n
        return None

    def _laminas_del_slot(self, slot: dict) -> set[str]:
        """Nombres (con y sin extensión) de las láminas de origen de un slot.

        Es lo que permite saber, en un lote con dos catálogos limpiados en la
        MISMA carpeta de trabajo, cuáles recortes son de cuál proveedor."""
        nombres: set[str] = set()
        archivos = slot.get("entrada_archivos") or []
        rutas = [Path(a) for a in archivos]
        if not rutas and slot.get("entrada_dir"):
            carpeta = Path(str(slot["entrada_dir"]))
            if carpeta.is_dir():
                rutas = [p for p in carpeta.rglob("*") if p.is_file()]
        for p in rutas:
            nombres.add(p.name)
            nombres.add(p.stem)
        return nombres

    def _recortes_del_proveedor_activo(self) -> set[str]:
        """Qué recortes de la carpeta de trabajo le corresponden al proveedor
        activo. Con un solo proveedor son TODOS -- exactamente el
        comportamiento de siempre."""
        if not self._lote_multiproveedor():
            return set(self._recortes)
        propias = self._laminas_del_slot(self._slot_actual())
        elegidos = set()
        for nombre, r in self._recortes.items():
            origen = r.get("_origen_lamina") or ""
            if not origen:
                continue
            p = Path(str(origen))
            if p.name in propias or p.stem in propias:
                elegidos.add(nombre)
        return elegidos

    def _abrir_ajustes(self) -> None:
        """Ajustes que se configuran una vez y no vuelven a tocarse — hoy,
        solo el método de comparación (TI vs. estándar). Salió del diálogo de
        proveedor: mezclado ahí obligaba a decidirlo en cada catálogo, cuando
        en realidad es una configuración de la instalación."""
        import motor_candidatos

        ventana = ctk.CTkToplevel(self)
        ventana.title("Ajustes")
        _centrar_en_ventana_principal(self, ventana, 460, 300)
        Etiqueta(ventana, text="Método de comparación", style="Subtitulo.TLabel").pack(
            anchor="w", padx=14, pady=(14, 4))
        Etiqueta(ventana, text="Con qué espacio de vectores se buscan los comparables. "
                 "No se pueden mezclar: cambiarlo aplica al próximo catálogo que se envíe "
                 "a calificar.", style="Suave.TLabel", justify="left",
                 wraplength=380).pack(anchor="w", padx=14, pady=(0, 8), fill="x")
        v_modelo = tk.StringVar(value=motor_candidatos.modelo_activo_configurado())
        # Las opciones salen de `MODELOS_ACTIVOS_VALIDOS` + `ETIQUETA_METODOS`
        # (los mismos que muestra el paso 5): así agregar un método nuevo no
        # obliga a acordarse de esta ventana, que era justo lo que pasaba antes
        # con las dos opciones escritas a mano.
        for i, valor in enumerate(motor_candidatos.MODELOS_ACTIVOS_VALIDOS):
            titulo, _ = self.ETIQUETA_METODOS.get(valor, (valor, ""))
            ultimo = (i == len(motor_candidatos.MODELOS_ACTIVOS_VALIDOS) - 1)
            ctk.CTkRadioButton(ventana, text=titulo, variable=v_modelo, value=valor).pack(
                anchor="w", padx=14, pady=((4 if i == 0 else 2), 10 if ultimo else 0))

        def guardar() -> None:
            motor_candidatos.establecer_modelo_activo(v_modelo.get())
            ventana.destroy()

        fila = Marco(ventana)
        fila.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila, text="Cancelar", style="Sutil.TButton",
              command=ventana.destroy).pack(side="right")
        Boton(fila, text="Guardar", style="Primario.TButton",
              command=guardar).pack(side="right", padx=(0, 8))
        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)

    def _candidatos_del_lote(self) -> list[dict]:
        """Los candidatos de ESTE lote (proveedor + catálogo actuales), no todo
        lo que haya en staging.

        Bug real corregido el 2026-09-09: `obtener_candidatos()` se llamaba sin
        filtro, así que la pantalla 6 mostraba también los candidatos de otro
        proveedor si alguien había enviado un catálogo con "Agregar" en vez de
        "Empezar nuevo". El filtro se hace por proveedor Y por `catalogo_origen`
        (las dos columnas que ya trae cada fila de `dim_candidato`).

        El filtro es ESTRICTO y sin red de seguridad (2026-09-09, segunda
        vuelta: el primer intento seguía mostrando candidatos ajenos). El
        arreglo anterior tenía tres escapes que, juntos, hacían que en la
        práctica casi nunca filtrara:

        1. `filtrar_candidatos(todos, None)` NO filtra nada -- devuelve la
           lista entera. Si el lote en pantalla todavía no tiene proveedor
           elegido (`_proveedor_id_actual is None`), "el filtro por proveedor"
           era la identidad, y salía TODO staging.
        2. `if not self._salida: return del_proveedor` -- sin carpeta de
           trabajo tampoco se filtraba por catálogo.
        3. `return del_lote or del_proveedor` -- si el `catalogo_origen` no
           coincidía (lo normal cuando el lote en pantalla NO es el que está
           cargado en staging), se devolvía igual todo lo del proveedor. Con
           (1) encima, eso era "todo staging".

        Resultado real observado: staging tenía únicamente ARDY BROS /
        reebok_extraido (51 candidatos), y esos 51 aparecían en el paso 6 de
        cualquier lote. Ahora, si no se puede identificar el lote (sin
        proveedor o sin carpeta) o si staging no tiene NADA de este lote, se
        devuelve lista vacía: `_ver_candidatos_calificados` avisa "no hay
        candidatos" en vez de enseñar los de otro proveedor. Mostrar una
        pantalla vacía es correcto; mostrar el catálogo de otro no lo es.

        FASE 5 multi-proveedor (2026-09-16): el filtro es por TODOS los
        proveedores del lote, no solo por el activo. Antes filtraba por
        `_proveedor_id_actual`, así que en un lote con dos catálogos la
        pantalla 6 mostraba únicamente el del proveedor que se acababa de
        enviar: la pastilla de proveedor, el filtro «Ver: Todos/A/B» y el
        desglose por proveedor del paso 7 (toda la Fase 4) nunca se activaban
        en el flujo real, y el paso 7 quedaba incoherente con el 6 porque
        `api_pedido_sugerido` SÍ reparte el PDV entre los candidatos de todo
        el lote. Con un solo proveedor la lista que sale de acá es exactamente
        la de antes (mismo filtro, mismo orden).
        """
        import motor_candidatos
        pids = [int(s["proveedor_id"]) for s in self._proveedores_del_lote
                if s.get("proveedor_id") is not None]
        if self._proveedor_id_actual is not None and \
                int(self._proveedor_id_actual) not in pids:
            pids.append(int(self._proveedor_id_actual))
        if not pids or not self._salida:
            return []
        posibles = self._catalogos_posibles_del_lote(self._salida)
        if not posibles:
            return []
        # Primero el filtro por catálogo (común a todo el lote), después la
        # unión de los proveedores. Se conserva el orden global por score que
        # devuelve `obtener_candidatos`, y se sigue apoyando en
        # `filtrar_candidatos` por proveedor -- o sea que un `proveedor_id`
        # que el maestro no resuelve sigue aportando CERO candidatos, nunca
        # "todos".
        del_catalogo = motor_candidatos.filtrar_candidatos(
            motor_candidatos.obtener_candidatos(), None, posibles)
        ids = set()
        for pid in pids:
            ids.update(c["candidato_id"]
                       for c in motor_candidatos.filtrar_candidatos(del_catalogo, pid))
        return [c for c in del_catalogo if c["candidato_id"] in ids]

    def _ver_candidatos_calificados(self) -> None:
        """Abre la vista de candidatos con lo que YA está en staging, sin
        reprocesar ni reenviar nada."""
        try:
            candidatos = self._candidatos_del_lote()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudieron consultar los candidatos:\n{exc}")
            return
        if not candidatos:
            messagebox.showinfo(
                "Asistente de Compras",
                "No hay ningún candidato en revisión ahora mismo.\n\n"
                "Procesá un catálogo y usá «🛒 Enviar a Asistente de Compras» "
                "desde la pantalla de Confirmar y enviar.")
            return
        self._mostrar_vista_candidatos(candidatos)

    def _mostrar_vista_candidatos(self, candidatos: list[dict]) -> None:
        """Paso 6: la lista de candidatos calificados, como pantalla DENTRO de
        la ventana principal (antes era un Toplevel aparte que tapaba la app y
        se perdía al cerrarlo)."""
        # Reloj de arena mientras arma la grilla de candidatos (puede haber
        # muchos, con foto cada uno) -- sin esto no había ninguna señal de que
        # el programa seguía vivo entre el clic y que aparezca la pantalla.
        self.configure(cursor="watch")
        self.after(400, lambda: self.configure(cursor=""))
        # Antes de armar la pantalla: en qué moneda cotiza este proveedor (y
        # con qué tipo de cambio). Sin eso no se puede calcular ningún precio
        # de venta sugerido, que es la mitad de lo que se muestra acá. Es UNA
        # VEZ POR PROVEEDOR DEL LOTE (FASE 3): si ya está declarado, no
        # pregunta nada y esto no agrega ninguna fricción al recargar. Con un
        # solo proveedor es una sola pregunta, igual que antes.
        self.asegurar_monedas_de_proveedores()
        # …y para qué mes se espera vender este pedido: es la otra premisa de
        # la compra (temporada), y también se pregunta UNA VEZ POR LOTE.
        self.asegurar_mes_venta_lote()
        self._asegurar_cuerpo_visible()
        self._cerrar_vista_candidatos(volver=False)
        self._ocultar_pantallas_base()
        self._candidatos_frame = VentanaCandidatosCTk(
            self, candidatos, al_volver=self._cerrar_vista_candidatos)
        self._candidatos_frame.pack(side="top", fill="both", expand=True,
                                    padx=6, pady=(0, 6))
        self._candidatos_frame.mostrar_candidatos()
        self._actualizar_paso(6)

    def _mostrar_sugerido_compra(self) -> None:
        """Paso 7 y último: el sugerido de compra (PDV objetivo → cuántos pares
        de cada referencia). Antes era una pestaña adentro de la pantalla de
        candidatos, donde el comprador no la encontraba: es el resultado final
        de todo el proceso, así que tiene su propio paso y su pastilla."""
        if self._candidatos_frame is None:
            # Se puede caer acá desde el menú o retomando un lote ya enviado:
            # se abre primero la pantalla que lo contiene.
            self._ver_candidatos_calificados()
            if self._candidatos_frame is None:
                return
        self._candidatos_frame.mostrar_optimizador()
        self._actualizar_paso(7)

    def _volver_a_candidatos(self) -> None:
        """«← Atrás» desde el paso 7: misma pantalla, sub-vista de candidatos."""
        if self._candidatos_frame is None:
            self._cerrar_vista_candidatos()
            return
        self._candidatos_frame.mostrar_candidatos()
        self._actualizar_paso(6)

    def _cerrar_vista_candidatos(self, volver: bool = True) -> None:
        if self._candidatos_frame is not None:
            self._candidatos_frame.destroy()
            self._candidatos_frame = None
            if volver:
                self._mostrar_cuerpo()
                # Si no hay un lote cargado, volver a "Limpiar y revisar" sería
                # mentira: la pantalla de atrás está vacía y lo que falta es
                # elegir origen (paso 1).
                self._actualizar_paso(3 if self._recortes else 1)

    # ── elegir carpetas ──────────────────────────────────────────────────

    def _elegir_entrada(self) -> None:
        d = filedialog.askdirectory(title="Carpeta de entrada")
        if d:
            self._entrada_archivos = None
            # Origen de imágenes directo: no hay fotos extraídas que elegir, así
            # que el paso 2 no aplica y el asistente lo va a saltar solo.
            self._archivos_revision = []
            self._exclusiones_revision = set()
            self.v_entrada.set(d)
            # Guardar la carpeta de entrada en los metadatos del lote
            self._guardar_marca_lote(entrada_dir=d)

    def _elegir_archivos(self) -> None:
        archivos = filedialog.askopenfilenames(
            title="Láminas a procesar",
            filetypes=[("Imágenes", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                       ("Todos los archivos", "*.*")])
        if archivos:
            self._entrada_archivos = [Path(a) for a in archivos]
            # Igual que la carpeta de imágenes: el comprador ya eligió a mano
            # cuáles son, el paso 2 no tiene nada que agregar.
            self._archivos_revision = []
            self._exclusiones_revision = set()
            self.v_entrada.set(f"{len(archivos)} archivo(s) seleccionado(s)")

    def _elegir_carpeta_excel(self) -> None:
        """Saca las fotos incrustadas de todos los .xlsx de una carpeta
        (identificando el código de producto de cada una — ver
        `extraer_excel.py`) y las deja como entrada lista para procesar."""
        carpeta_excel = filedialog.askdirectory(title="Carpeta con los archivos Excel")
        if not carpeta_excel:
            return
        carpeta_excel = Path(carpeta_excel)
        self._extraer_excel_y_cargar(sorted(carpeta_excel.glob("*.xlsx")),
                                      carpeta_excel / "_fotos_extraidas")

    def _elegir_archivos_excel(self) -> None:
        """Igual que `_elegir_carpeta_excel`, pero para uno o varios .xlsx
        sueltos elegidos a mano, no toda una carpeta."""
        archivos = filedialog.askopenfilenames(
            title="Archivo(s) Excel a extraer",
            filetypes=[("Excel", "*.xlsx"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_excel_y_cargar([Path(a) for a in archivos],
                                      primero.parent / "_fotos_extraidas")

    def _elegir_carpeta_pdf(self) -> None:
        """Igual que `_elegir_carpeta_excel` pero para catálogos/preventas en
        PDF (ver `extraer_pdf.py`): una página por producto, código como
        primer texto de la página y la foto más grande de esa página."""
        carpeta_pdf = filedialog.askdirectory(title="Carpeta con los archivos PDF")
        if not carpeta_pdf:
            return
        carpeta_pdf = Path(carpeta_pdf)
        self._extraer_generico_y_cargar(sorted(carpeta_pdf.glob("*.pdf")),
                                         carpeta_pdf / "_fotos_extraidas",
                                         modulo="extraer_pdf", etiqueta="PDF")

    def _elegir_archivos_pdf(self) -> None:
        """Igual que `_elegir_archivos_excel`, pero para uno o varios .pdf
        sueltos elegidos a mano."""
        archivos = filedialog.askopenfilenames(
            title="Archivo(s) PDF a extraer",
            filetypes=[("PDF", "*.pdf"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_generico_y_cargar([Path(a) for a in archivos],
                                         primero.parent / "_fotos_extraidas",
                                         modulo="extraer_pdf", etiqueta="PDF")

    def _elegir_carpeta_msg(self) -> None:
        """Igual que `_elegir_carpeta_excel`/`_elegir_carpeta_pdf` pero para
        correos de Outlook guardados como .msg (ver `extraer_msg.py`): cada
        adjunto de imagen es una foto de producto, nombrada con su propio
        código."""
        carpeta_msg = filedialog.askdirectory(title="Carpeta con los correos (.msg)")
        if not carpeta_msg:
            return
        carpeta_msg = Path(carpeta_msg)
        self._extraer_generico_y_cargar(sorted(carpeta_msg.glob("*.msg")),
                                         carpeta_msg / "_fotos_extraidas",
                                         modulo="extraer_msg", etiqueta="correo")

    def _elegir_archivos_msg(self) -> None:
        """Igual que `_elegir_archivos_excel`, pero para uno o varios .msg
        sueltos elegidos a mano."""
        archivos = filedialog.askopenfilenames(
            title="Correo(s) (.msg) a extraer",
            filetypes=[("Correo Outlook", "*.msg"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_generico_y_cargar([Path(a) for a in archivos],
                                         primero.parent / "_fotos_extraidas",
                                         modulo="extraer_msg", etiqueta="correo")

    def _extraer_excel_y_cargar(self, archivos_excel: list[Path], destino: Path) -> None:
        self._extraer_generico_y_cargar(archivos_excel, destino,
                                         modulo="extraer_excel", etiqueta="Excel")

    def _extraer_generico_y_cargar(self, archivos: list[Path], destino: Path,
                                    modulo: str, etiqueta: str) -> None:
        # Ventana con avance real y CONTROLES MIENTRAS extrae — la extracción
        # corre en un hilo aparte para que la ventana pueda seguir respondiendo
        # (si corriera en el hilo principal, bloquearía la app entera y los
        # botones no se podrían ni tocar).
        #
        # Pedido del usuario (2026-09-08): antes esto era una espera ciega de
        # principio a fin — una barra que giraba sin decir nada y sin forma de
        # pausar, cortar ni seguir con lo que ya se había sacado. Ahora hay
        # tres acciones distintas, porque son tres intenciones distintas:
        #   ⏸ Pausar / ▶ Reanudar  → atender otra cosa y volver.
        #   ✓ Continuar con lo cargado → ya alcanza con lo extraído; seguir el
        #     flujo normal (revisión de fotos) con el resultado parcial.
        #   ✗ Detener y cancelar → cortar y no avanzar con nada; las fotos
        #     parciales de ESTA extracción se borran (ver `_cancelar_extraccion`).
        # El corte es cooperativo (ver control_extraccion.py): surte efecto en
        # el próximo punto de control, no en el instante del clic.
        control = control_extraccion.ControlExtraccion(total_archivos=len(archivos))

        espera = ctk.CTkToplevel(self)
        espera.title("Extrayendo…")
        _centrar_en_ventana_principal(self, espera, 520, 230)
        espera.resizable(False, False)
        espera.configure(fg_color=PAR_PANEL)
        espera.transient(self)
        espera.protocol("WM_DELETE_WINDOW", lambda: None)  # no cerrar a medias
        # SIN grab_set(): un grab que por cualquier motivo no se libere bien
        # deja TODA la ventana principal sin responder a nada — mejor que este
        # aviso sea no-modal (el usuario puede seguir viendo la app detrás)
        # que arriesgar que un clic en "Procesar" quede sin efecto después.
        Etiqueta(espera, text=f"Extrayendo fotos de {len(archivos)} archivo(s) {etiqueta}",
                 style="SubtituloPanel.TLabel", padding=(16, 16, 16, 4)).pack(anchor="w", fill="x")

        v_detalle = tk.StringVar(value="Abriendo el primer archivo…")
        Etiqueta(espera, textvariable=v_detalle, style="SuavePanel.TLabel",
                 padding=(16, 0, 16, 6), wraplength=480,
                 justify="left").pack(anchor="w", fill="x")

        # Barra con avance REAL por archivo cuando hay más de uno (es el único
        # progreso honesto que se puede dar: adentro de un archivo grande no se
        # sabe de antemano cuántas imágenes trae). Con un solo archivo se deja
        # indeterminada en vez de inventar un porcentaje.
        multiples = len(archivos) > 1
        barra = Barra(espera, mode="determinate" if multiples else "indeterminate",
                      maximum=max(len(archivos), 1))
        barra.pack(fill="x", padx=16, pady=(2, 10))
        if not multiples:
            barra.start(12)

        fila_ctrl = Marco(espera, padding=(16, 0, 16, 14))
        fila_ctrl.pack(fill="x")

        estado = {"pausado": False, "terminado": False}

        def alternar_pausa() -> None:
            if estado["terminado"]:
                return
            if estado["pausado"]:
                control.reanudar()
                estado["pausado"] = False
                btn_pausa.configure(text="⏸ Pausar")
                if not multiples:
                    barra.start(12)
            else:
                control.pausar()
                estado["pausado"] = True
                btn_pausa.configure(text="▶ Reanudar")
                if not multiples:
                    barra.stop()
                v_detalle.set("En pausa. La extracción se detiene al terminar la "
                              "imagen que estaba escribiendo.")

        def pedir_corte(conservar: bool) -> None:
            if estado["terminado"]:
                return
            control.cancelar(conservar=conservar)
            estado["pausado"] = False
            btn_pausa.configure(state="disabled")
            btn_seguir.configure(state="disabled")
            btn_cancelar.configure(state="disabled")
            v_detalle.set("Cerrando el archivo en curso… un momento.")

        btn_pausa = Boton(fila_ctrl, text="⏸ Pausar", command=alternar_pausa)
        btn_pausa.pack(side="left")
        btn_seguir = Boton(fila_ctrl, text="✓ Continuar con lo cargado",
                           style="Primario.TButton",
                           command=lambda: pedir_corte(True))
        btn_seguir.pack(side="left", padx=(8, 0))
        btn_cancelar = Boton(fila_ctrl, text="✗ Detener y cancelar",
                             style="Peligro.TButton",
                             command=lambda: pedir_corte(False))
        btn_cancelar.pack(side="right")

        _centrar_en_ventana_principal(self, espera, 520, 230)

        resultado: dict = {}

        def trabajar() -> None:
            try:
                import importlib
                mod = importlib.import_module(modulo)
                resultado["resumen"] = mod.extraer_lista(archivos, destino, control=control)
            except Exception as exc:  # noqa: BLE001
                resultado["error"] = str(exc)

        hilo = threading.Thread(target=trabajar, daemon=True)
        hilo.start()

        def revisar() -> None:
            if hilo.is_alive():
                if not estado["pausado"] and not control.cancelado:
                    nombre, i, total = control.estado_texto()
                    if nombre:
                        v_detalle.set(f"Archivo {i} de {total}: {nombre}"
                                      + (f"  ·  {control.imagenes_hasta_ahora} foto(s) "
                                         f"hasta ahora" if control.imagenes_hasta_ahora else ""))
                        if multiples:
                            barra.configure(value=i - 1)
                self.after(150, revisar)
                return
            estado["terminado"] = True
            barra.stop()
            espera.destroy()
            self._mostrar_resultado_extraccion(resultado, destino, etiqueta)

        self.after(150, revisar)

    def _mostrar_resultado_extraccion(self, resultado: dict, destino: Path,
                                       etiqueta: str = "Excel") -> None:
        if "error" in resultado:
            messagebox.showerror(f"Extraer de {etiqueta}", f"No se pudo extraer: {resultado['error']}")
            return
        resumen = resultado["resumen"]

        # "✗ Detener y cancelar": no se avanza con nada, y las fotos que la
        # extracción ya había escrito en disco se borran. Se borran SOLO las
        # rutas de ESTA extracción (`resumen["rutas"]`), nunca por glob de la
        # carpeta: el destino puede tener fotos de una extracción anterior y
        # barrerlas sería destruir trabajo que el usuario no pidió tirar.
        if resumen.get("cancelado") and not resumen.get("conservar_parcial", True):
            self._descartar_extraccion_cancelada(resumen, destino, etiqueta)
            return

        errores = {n: v["error"] for n, v in resumen["archivos"].items() if "error" in v}
        cancelada = bool(resumen.get("cancelado"))
        mensaje = (f"{resumen['total_imagenes']} fotos extraídas de "
                   f"{len(resumen['archivos'])} archivo(s) {etiqueta}.")
        if cancelada:
            pendientes = resumen.get("archivos_pendientes", 0)
            mensaje += (f"\n\nSe cortó la extracción a pedido tuyo"
                        + (f": quedaron {pendientes} archivo(s) sin procesar."
                           if pendientes else "."))
        if resumen["total_sin_codigo"]:
            mensaje += (f"\n\n{resumen['total_sin_codigo']} imagen(es) no se pudieron "
                        f"asociar a un código de producto — "
                        f"suelen ser logos u otras imágenes fuera de las páginas/filas de producto.")
        if errores:
            mensaje += "\n\nArchivos con error:\n" + "\n".join(
                f"- {n}: {e}" for n, e in errores.items())

        aviso = ctk.CTkToplevel(self)
        titulo = "Extracción detenida" if cancelada else "Extracción completa"
        aviso.title(titulo)
        aviso.configure(fg_color=PAR_PANEL)
        aviso.transient(self)
        Etiqueta(aviso, text=("⏹ " if cancelada else "✓ ") + titulo,
                 style="SubtituloPanel.TLabel",
                 padding=(16, 16, 16, 4)).pack(anchor="w", fill="x")
        Etiqueta(aviso, text=mensaje, style="Panel.TLabel", padding=(16, 0, 16, 8),
                 wraplength=440, justify="left").pack(anchor="w", fill="x")
        Etiqueta(aviso, text=f"Guardadas en:\n{destino}", style="SuavePanel.TLabel",
                 padding=(16, 0, 16, 12), wraplength=440, justify="left").pack(anchor="w", fill="x")
        fila_botones = Marco(aviso, padding=(16, 0, 16, 16))
        fila_botones.pack(fill="x")
        Boton(fila_botones, text="Abrir carpeta",
              command=lambda: os.startfile(str(destino))).pack(side="left")
        Boton(fila_botones, text="Cerrar", style="Primario.TButton",
              command=aviso.destroy).pack(side="right")
        aviso.update_idletasks()
        _centrar_en_ventana_principal(self, aviso, aviso.winfo_width(), aviso.winfo_height())

        if resumen["total_imagenes"] == 0:
            return
        # Solo lo que se extrajo AHORA (`resumen["rutas"]`) — no releer toda la
        # carpeta de destino, que puede tener fotos de una extracción anterior
        # (mismo Excel reprocesado, u otro Excel de la misma carpeta) y
        # terminar mostrando/procesando algo que no es lo que se acaba de elegir.
        archivos = [Path(r) for r in resumen["rutas"]]
        self._mostrar_revision_extraccion(archivos)

    def _descartar_extraccion_cancelada(self, resumen: dict, destino: Path,
                                         etiqueta: str) -> None:
        """"✗ Detener y cancelar": deshacer la extracción parcial y NO avanzar.

        Las fotos ya escritas en disco se borran porque son producto de una
        extracción que el usuario decidió tirar: dejarlas ahí haría que la
        próxima extracción a la misma carpeta las mezclara con las nuevas
        (mismo bug que ya se cuida al no releer el destino con glob). Se borran
        una por una, solo las de esta corrida; si alguna no se puede borrar
        (antivirus, archivo tomado), se dice cuántas quedaron en vez de fallar
        en silencio."""
        rutas = [Path(r) for r in resumen.get("rutas", [])]
        borradas, fallidas = 0, 0
        for ruta in rutas:
            try:
                if ruta.exists():
                    ruta.unlink()
                borradas += 1
            except OSError:
                fallidas += 1
        self.v_log.set(f"Extracción de {etiqueta} cancelada: se descartaron "
                       f"{borradas} foto(s) parciales.")
        mensaje = (f"No se avanzó con nada.\n\n"
                   f"Se habían extraído {len(rutas)} foto(s) antes de detener; "
                   f"se descartaron {borradas}.")
        if fallidas:
            mensaje += (f"\n\n{fallidas} no se pudieron borrar (pueden estar "
                        f"abiertas en otro programa). Quedaron en:\n{destino}")
        messagebox.showinfo("Extracción cancelada", mensaje)

    # ── revisión previa de fotos extraídas de Excel ─────────────────────

    def _mostrar_revision_extraccion(self, archivos: list[Path],
                                      exclusiones: set[str] | None = None) -> None:
        """PASO 2 del asistente, «Elegir qué limpiar»: antes de limpiar nada,
        mostrar TODAS las fotos extraídas en una grilla de miniaturas, con
        cada una marcada como incluida por defecto y la posibilidad de
        descartar (clic) las que sean logos u otra cosa que no es calzado.
        Al continuar se arma `self._entrada_archivos` y el asistente pasa al
        paso 3, donde se lanza la limpieza.

        `exclusiones` permite reconstruir la pantalla tal como quedó cuando se
        vuelve a ella con «← Atrás» (si no, volver perdía lo descartado)."""
        self._ocultar_resumen()
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None

        self._ocultar_pantallas_base()
        self._archivos_revision = archivos
        self._exclusiones_revision = set(exclusiones or ())
        self._fotos_revision = []
        self._marcos_revision = {}

        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._revision_frame = marco
        self._sincronizar_cabecera()  # cabecera compacta: la grilla necesita el alto

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        fila_titulo = Marco(interior)
        fila_titulo.pack(fill="x")
        Etiqueta(fila_titulo, text=f"Elegir qué limpiar — {len(archivos)} foto(s) extraídas",
                 style="Titulo.TLabel").pack(side="left")
        Boton(fila_titulo, text="Cancelar", command=self._cerrar_revision_extraccion).pack(side="right")

        Etiqueta(interior, style="SuavePanel.TLabel", wraplength=1100,
                 text="Hacé clic en una foto para descartarla (por ejemplo, logos u otras "
                      "imágenes que no son calzado). Por defecto todas quedan incluidas."
                 ).pack(anchor="w", fill="x", pady=(4, 10))

        # El botón de abajo y el separador se empaquetan ANTES que el área con
        # scroll (con `side="bottom"`), para que siempre reserven su espacio
        # primero — con cientos/miles de fotos, si el área con scroll se
        # empaqueta primero puede terminar acaparando toda la ventana y dejar
        # el botón invisible o fuera de la vista.
        fila_inferior = Marco(interior)
        fila_inferior.pack(side="bottom", fill="x", pady=(10, 0))
        Separador(interior).pack(side="bottom", fill="x", pady=(10, 0))

        # `CTkScrollableFrame` reemplaza el Canvas + Scrollbar + `<Configure>`
        # de antes; de paso trae la rueda del mouse funcionando (era un item
        # pendiente: en la versión con Canvas no scrolleaba con la rueda).
        grilla = ctk.CTkScrollableFrame(interior, fg_color="transparent",
                                        scrollbar_button_color=PAR_BORDE,
                                        scrollbar_button_hover_color=PAR_TEXTO_SUAVE)
        grilla.pack(side="top", fill="both", expand=True)

        # Mismo criterio que la grilla de "Elegir qué limpiar": foto más grande
        # y tantas columnas como quepan en el ancho real, no un número fijo.
        self._cols_grilla.pop("revision", None)
        cols_ini = max(1, (max(self.winfo_width() - 90, 300))
                       // (self.ANCHO_CELDA_REVISION + self._SEPARACION_CELDA))
        for idx, path in enumerate(archivos):
            fila_i, col_i = divmod(idx, cols_ini)
            celda = tk.Frame(grilla, background=COLOR_PANEL, highlightthickness=2,
                             highlightbackground=COLOR_BORDE, cursor="hand2",
                             width=self.ANCHO_CELDA_REVISION,
                             height=self.ALTO_CELDA_REVISION)
            celda.grid(row=fila_i, column=col_i, padx=6, pady=6)
            celda.grid_propagate(False)

            lbl_img = tk.Label(celda, background=COLOR_VISOR_BG, cursor="hand2")
            lbl_img.pack(fill="both", expand=True, padx=6, pady=(6, 2))
            try:
                im = Image.open(path)
                try:
                    # decodificar ya reducido cuando el formato lo permite (jpg) —
                    # con cientos de fotos, abrir a tamaño completo sería lento.
                    im.draft("RGB", (self.MINIATURA_REVISION, self.MINIATURA_REVISION))
                except Exception:  # noqa: BLE001
                    pass
                im = im.convert("RGB")
                im.thumbnail((self.MINIATURA_REVISION, self.MINIATURA_REVISION))
            except Exception:  # noqa: BLE001
                im = Image.new("RGB", (self.MINIATURA_REVISION, self.MINIATURA_REVISION),
                               COLOR_VISOR_BG)
            foto = ImageTk.PhotoImage(im)
            self._fotos_revision.append(foto)  # referencia viva — si no, Tk la descarta
            lbl_img.configure(image=foto)

            lbl_nombre = tk.Label(celda, text=path.stem, bg=COLOR_PANEL, fg=COLOR_TEXTO,
                                  font=FUENTE_CHICA,
                                  wraplength=self.ANCHO_CELDA_REVISION - 22,
                                  justify="center")
            lbl_nombre.pack(fill="x", padx=4, pady=(0, 6))

            clave = str(path)
            if clave in self._exclusiones_revision:
                celda.configure(highlightbackground=COLOR_MAL, highlightthickness=3)
            self._marcos_revision[clave] = celda
            for widget in (celda, lbl_img, lbl_nombre):
                widget.bind("<Button-1>", lambda _e, k=clave: self._alternar_exclusion_revision(k))

        Etiqueta(fila_inferior, textvariable=self.v_estado_revision,
                 style="SuavePanel.TLabel").pack(side="left")
        self.btn_procesar_revision = Boton(fila_inferior, text="", style="Primario.TButton",
                                           command=self._confirmar_seleccion_revision)
        self.btn_procesar_revision.pack(side="right")
        self._actualizar_revision_estado()
        # Esta pantalla ES el paso 2: fija el paso acá (y no en quien la llama)
        # porque se llega a ella por dos caminos — al terminar la extracción, y
        # volviendo con «← Atrás» desde el paso 3.
        self._actualizar_paso(2)

        def acomodar(_evento=None) -> None:
            if self._revision_frame is None:
                return
            self._acomodar_grilla(
                "revision", grilla,
                lambda: [self._marcos_revision[str(p)] for p in self._archivos_revision
                         if str(p) in self._marcos_revision],
                self.ANCHO_CELDA_REVISION)

        grilla.bind("<Configure>", acomodar)
        self.after_idle(acomodar)

    def _alternar_exclusion_revision(self, clave: str) -> None:
        celda = self._marcos_revision.get(clave)
        if celda is None:
            return
        if clave in self._exclusiones_revision:
            self._exclusiones_revision.discard(clave)
            celda.configure(highlightbackground=COLOR_BORDE, highlightthickness=2)
        else:
            self._exclusiones_revision.add(clave)
            celda.configure(highlightbackground=COLOR_MAL, highlightthickness=3)
        self._actualizar_revision_estado()

    def _actualizar_revision_estado(self) -> None:
        total = len(self._archivos_revision)
        excluidas = len(self._exclusiones_revision)
        incluidas = total - excluidas
        self.v_estado_revision.set(f"{incluidas} de {total} seleccionadas ({excluidas} descartadas)")
        self.btn_procesar_revision.configure(text=f"Continuar con {incluidas} foto(s)  →")
        self.btn_procesar_revision.state(["disabled"] if incluidas == 0 else ["!disabled"])

    def _cerrar_revision_extraccion(self) -> None:
        """«Cancelar» en el paso 2: se descarta la selección extraída y se
        vuelve al paso 1 a elegir otro origen (quedarse en el paso 3 con una
        entrada que el usuario acaba de cancelar sería mentir sobre el estado)."""
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
        self._archivos_revision = []
        self._exclusiones_revision = set()
        self._ir_a_paso(1)

    def _confirmar_seleccion_revision(self) -> None:
        """Cierra el paso 2 y pasa al 3. NO arranca la limpieza: lanzarla es
        justo lo que hace el paso 3, con su propio botón y sus controles de
        pausar/continuar/detener."""
        incluidas = [p for p in self._archivos_revision if str(p) not in self._exclusiones_revision]
        if not incluidas:
            messagebox.showwarning("Elegir qué limpiar",
                                   "No quedó ninguna foto seleccionada.")
            return
        self._entrada_archivos = incluidas
        self.v_entrada.set(f"{len(incluidas)} foto(s) seleccionadas (de {len(self._archivos_revision)} extraídas)")
        # Guardar la lista de archivos seleccionados en los metadatos del lote,
        # para poder restaurarla si se retoma este lote después.
        self._guardar_marca_lote(entrada_archivos=[str(p) for p in incluidas])
        # FASE 1 multi-proveedor: las fotos realmente elegidas son el origen
        # definitivo de ESTE proveedor del lote (es lo que después permite
        # saber qué recortes son suyos y qué recortes son del otro).
        self._sincronizar_slot_actual()
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
        self._ir_a_paso(3)

    @staticmethod
    def _es_carpeta_de_exportacion(carpeta: Path) -> bool:
        """¿Esta carpeta es un destino de fotos ya exportadas?

        Se reconoce por lo que deja `decisiones.exportar_aprobados`:
        `fondo_blanco/`, `fondo_transparente/` y `exportacion_*.json`.
        """
        try:
            return bool((carpeta / "fondo_blanco").is_dir()
                        or (carpeta / "fondo_transparente").is_dir()
                        or list(carpeta.glob("exportacion_*.json")))
        except OSError:
            return False

    def _elegir_salida(self, usar_texto: bool = False) -> None:
        """UN solo camino para "carpeta de trabajo": sirve tanto para
        arrancar una corrida nueva (carpeta vacía) como para cargar una ya
        procesada (con manifiesto adentro) — antes había 3 formas parecidas
        de hacer esto (este botón, "Cargar corrida anterior…", y la carga
        automática al abrir la app) con comportamientos ligeramente
        distintos, y eso confundía más de lo que ayudaba."""
        if usar_texto:
            texto = self.v_salida.get().strip()
            if not texto:
                return
            d = texto
        else:
            d = filedialog.askdirectory(title="Carpeta de trabajo (nueva o ya procesada)")
            if not d:
                return

        # La carpeta de trabajo es la que se llena de archivos temporales y la
        # que vacía "Vaciar carpeta de trabajo". Elegir por error la carpeta de
        # fotos finales ya pasó de verdad: el pipeline empieza a escribir
        # `blanco/`, `transparente/`, `_pasos/` justo al lado de las fotos
        # entregables. Se rechaza acá, antes de que escriba nada.
        candidata = Path(d)
        if self._es_carpeta_de_exportacion(candidata):
            messagebox.showerror(
                "Esa es tu carpeta de fotos finales",
                f"No se puede usar como carpeta de trabajo:\n{candidata}\n\n"
                "Esa carpeta tiene fotos ya exportadas (fondo_blanco / "
                "fondo_transparente). La carpeta de trabajo se llena de "
                "archivos temporales y es la que se vacía con «Vaciar carpeta "
                "de trabajo».\n\n"
                "Elegí una carpeta aparte para el trabajo, y dejá esta solo "
                "como destino al exportar.")
            self.v_salida.set(str(self._salida) if self._salida else "")
            return

        self.v_salida.set(d)
        self._cargar_salida_existente(candidata, avisar_siempre=True)

    def _cargar_salida_existente(self, salida: Path, avisar_siempre: bool = False) -> bool:
        """Carga lo ya procesado en `salida`, si hay algo. Con
        `avisar_siempre=True` SIEMPRE deja constancia de qué pasó (en el
        indicador fijo de carpeta activa, arriba) — nunca en silencio, como
        si el clic no hubiera hecho nada. Devuelve si encontró recortes."""
        if salida.exists() and not almacen.hay_lote(salida):
            # Error común: el usuario entra a "transparente" o "blanco" (una
            # carpeta ADENTRO de la carpeta real de salida) en vez de quedarse
            # en la carpeta de arriba, donde vive el lote — en vez de
            # fallar ahí, se busca también un nivel arriba antes de rendirse.
            if salida.name in ("transparente", "blanco", "revision", "_originales",
                               "_pasos", "_vecinos") and almacen.hay_lote(salida.parent):
                salida = salida.parent
                # Sin esto, el cuadro de texto seguía mostrando la subcarpeta
                # (ej. "...\_trabajo\blanco") mientras `self._salida` ya
                # apuntaba a la de arriba — al apretar "Procesar" se leía el
                # texto viejo y se creaba "blanco\blanco\" por accidente.
                self.v_salida.set(str(salida))

        if not salida.exists():
            if avisar_siempre:
                self.v_carpeta_activa.set(f"Carpeta activa: {salida}  ·  no existe todavía "
                                          f"(se creará al procesar)")
            self._salida = salida
            return False

        self._salida = salida
        # La caja de texto del paso 1 tiene que quedar mostrando ESTA carpeta:
        # `_procesar` lee `v_salida` (no `self._salida`), así que si se retoma
        # un lote por un camino que no la llenó (el diálogo de arranque
        # "Bienvenido de vuelta"), tocar el botón de limpiar abría un
        # "elegí la carpeta de salida" para una carpeta que ya se sabía —
        # y si el usuario lo cancelaba, el clic no hacía nada en silencio.
        self.v_salida.set(str(salida))
        self._recargar_manifiesto()
        self._guardar_ultima_carpeta()
        # Retomar un lote viejo restaura también DE QUIÉN era: sin esto, el
        # paso 1 volvía a pedir el proveedor de un catálogo que ya se había
        # trabajado entero.
        self._restaurar_proveedor_del_lote(salida)
        # …y también DE DÓNDE salieron las fotos: sin el origen, el paso 2
        # quedaba con el botón "Limpiar fotos" condenado a fallar.
        self._restaurar_entrada_del_lote(salida)
        # …y también el NOMBRE del proyecto: sin él, el usuario vería vacío.
        self._restaurar_nombre_proyecto_del_lote(salida)
        # …y PARA QUÉ CANAL era la compra (TC Marcas / TU Calzado).
        self._restaurar_canal_del_lote(salida)
        if self._recortes:
            self.v_carpeta_activa.set(f"Carpeta activa: {salida}  ·  {len(self._recortes)} recorte(s) cargados")
            self.v_log.set(f"{len(self._recortes)} recorte(s) ya procesados encontrados en esta carpeta")
            # Se borra sola a los 4s -- es un aviso informativo de un instante,
            # no algo que deba quedar ocupando una franja al pie de la
            # ventana para siempre (reclamo del usuario: le robaba espacio al
            # visor de foto grande, justo arriba).
            self.after(4000, lambda: self.v_log.set("") if
                      self.v_log.get().startswith(f"{len(self._recortes)} recorte(s) ya procesados")
                      else None)
            self._retomando_lote = True
            self._saltar_al_paso_del_lote()
            return True
        self._retomando_lote = False

        if avisar_siempre:
            self.v_carpeta_activa.set(f"Carpeta activa: {salida}  ·  sin recortes procesados todavía")
        self._ir_a_paso(1)
        return False

    # ── retomar un lote: saltarse los pasos ya hechos ────────────────────
    # Pedido del usuario (2026-09-07): "si yo cargo fotos de un lote anterior,
    # debería automáticamente llevarme a la ventana adecuada, saltarse los
    # pasos que el lote ya hizo".
    #
    # El avance de un lote NO estaba escrito en ningún lado como tal: había
    # que deducirlo de tres fuentes distintas, cada una en su propio archivo
    # suelto (el manifiesto de recortes, la bitácora de decisiones, y lo que
    # hay en `staging_tuc`). Desde 2026-09-08 las dos primeras viven en el
    # mismo `lote.sqlite` de la carpeta de trabajo, junto con el proveedor y la
    # constancia del envío — un solo archivo transaccional en vez de cinco.

    def _leer_marca_lote(self, carpeta: Path) -> dict:
        try:
            return almacen.leer_meta(carpeta)
        except Exception:  # noqa: BLE001
            return {}

    # ---------- moneda / tipo de cambio / margen DEL LOTE ----------
    #
    # Van en los metadatos LOCALES de la carpeta de trabajo
    # (`_guardar_marca_lote`), NO en Postgres: son datos de ESTE lote de
    # trabajo (con qué tipo de cambio se cotizó este catálogo, con qué margen
    # se sugirieron estos precios), no hechos permanentes del negocio. La base
    # `staging_tuc` además es efímera por diseño.

    # FASE 3 multi-proveedor (2026-09-16): moneda, tipo de cambio y margen
    # pasaron de ser UN valor del lote entero a ser POR PROVEEDOR. Un lote
    # puede traer el catálogo de un proveedor que cotiza en colones y el de
    # otro que cotiza en dólares (o en yuanes); con un solo juego de valores,
    # los precios de uno de los dos salían mal.
    #
    # Persistencia: los 3 campos van DENTRO de cada elemento de la lista
    # `proveedores_lote` (misma clave JSON de la Fase 1), no en claves nuevas
    # paralelas -- así no puede haber una lista de proveedores y una lista de
    # monedas desincronizadas. Las claves sueltas de antes
    # (`moneda_proveedor`/`tipo_cambio`/`margen_venta`) se siguen escribiendo
    # para el proveedor ACTIVO (compatibilidad y punto de partida de la
    # migración), pero el dato que manda es el del slot.
    CAMPO_MONEDA_SLOT = "moneda_proveedor"
    CAMPO_TC_SLOT = "tipo_cambio"
    CAMPO_MARGEN_SLOT = "margen_venta"

    @staticmethod
    def _normalizar_config_precio(moneda, tipo_cambio, margen) -> dict:
        try:
            margen_f = float(margen or MARGEN_VENTA_DEFAULT)
        except (TypeError, ValueError):
            margen_f = MARGEN_VENTA_DEFAULT
        try:
            tc_f = float(tipo_cambio) if tipo_cambio else None
        except (TypeError, ValueError):
            tc_f = None
        return {"moneda": moneda or None, "tipo_cambio": tc_f, "margen": margen_f}

    def _slot_de_proveedor(self, proveedor_id) -> dict | None:
        """El slot del lote cuyo `proveedor_id` coincide (None si no hay)."""
        if proveedor_id is None:
            return None
        for slot in self._proveedores_del_lote:
            if slot.get("proveedor_id") is not None and \
                    int(slot["proveedor_id"]) == int(proveedor_id):
                return slot
        return None

    def config_precio_proveedor(self, proveedor_id=None) -> dict:
        """Moneda, tipo de cambio y margen declarados para UN proveedor de este
        lote. Sin preguntar nada: devuelve lo que haya (moneda None si nunca se
        declaró para ese proveedor).

        `proveedor_id=None` significa "el proveedor activo", que es lo que
        usaba todo el código de antes de esta fase.

        Si el slot no tiene configuración propia (lote de antes de esta fase
        que todavía no pasó por la migración, o pantalla suelta en pruebas) se
        cae a las claves SUELTAS del lote, que es exactamente el valor que ese
        lote venía usando: un lote de un solo proveedor no cambia en nada."""
        if proveedor_id is None:
            proveedor_id = self._proveedor_id_actual
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        datos = self._leer_marca_lote(Path(carpeta)) if carpeta else {}
        slot = self._slot_de_proveedor(proveedor_id) or {}
        if slot.get(self.CAMPO_MONEDA_SLOT):
            return self._normalizar_config_precio(
                slot.get(self.CAMPO_MONEDA_SLOT), slot.get(self.CAMPO_TC_SLOT),
                slot.get(self.CAMPO_MARGEN_SLOT) or datos.get("margen_venta"))
        return self._normalizar_config_precio(
            datos.get("moneda_proveedor"), datos.get("tipo_cambio"),
            slot.get(self.CAMPO_MARGEN_SLOT) or datos.get("margen_venta"))

    def config_precio_lote(self) -> dict:
        """Compatibilidad: la configuración del proveedor ACTIVO. Se conserva
        el nombre porque es el que ya llamaba la pantalla de candidatos (y en
        un lote de un solo proveedor sigue significando lo mismo)."""
        return self.config_precio_proveedor(None)

    def configs_precio_por_proveedor(self) -> dict:
        """`{proveedor_id: config}` de todos los proveedores del lote: es lo
        que la pantalla 6 necesita para calcular cada tarjeta con la moneda de
        SU proveedor."""
        salida = {}
        for slot in self._proveedores_del_lote:
            pid = slot.get("proveedor_id")
            if pid is None:
                continue
            salida[int(pid)] = self.config_precio_proveedor(int(pid))
        return salida

    def nombre_de_proveedor_del_lote(self, proveedor_id) -> str:
        slot = self._slot_de_proveedor(proveedor_id) or {}
        return str(slot.get("proveedor_nombre") or "")

    def guardar_config_precio_proveedor(self, proveedor_id, moneda: str,
                                        tipo_cambio: float | None,
                                        margen: float) -> None:
        """Anota la elección DENTRO del slot de ese proveedor y la persiste. El
        margen se guarda también (y no solo se usa) para que quede registrado
        con qué margen se sugirió cada precio de ESE catálogo."""
        if proveedor_id is None:
            proveedor_id = self._proveedor_id_actual
        slot = self._slot_de_proveedor(proveedor_id)
        if slot is not None:
            slot[self.CAMPO_MONEDA_SLOT] = moneda
            slot[self.CAMPO_TC_SLOT] = tipo_cambio
            slot[self.CAMPO_MARGEN_SLOT] = margen
            self._persistir_proveedores_del_lote()
        # Las claves sueltas siguen reflejando al proveedor ACTIVO: son el
        # punto de partida de la migración y lo que leería una versión
        # anterior del programa si abre este mismo lote.
        if (proveedor_id is None or self._proveedor_id_actual is None
                or int(proveedor_id) == int(self._proveedor_id_actual)):
            self._guardar_marca_lote(moneda_proveedor=moneda,
                                     tipo_cambio=tipo_cambio,
                                     margen_venta=margen)

    def guardar_config_precio_lote(self, moneda: str, tipo_cambio: float | None,
                                   margen: float, proveedor_id=None) -> None:
        """Compatibilidad: el proveedor activo, salvo que se diga otro."""
        self.guardar_config_precio_proveedor(proveedor_id, moneda, tipo_cambio,
                                             margen)

    def _migrar_config_precio_a_slots(self, datos: dict, carpeta=None) -> None:
        """Migración de lotes de antes de la Fase 3: moneda/tipo de
        cambio/margen estaban como claves SUELTAS de `lote_meta`, una sola vez
        para el lote entero. Ese valor es, por definición, el que ese lote usó
        para todos sus proveedores, así que se copia a cada slot que todavía no
        tenga configuración propia.

        Así, al abrir un lote viejo no se pierde lo ya declarado ni se vuelve a
        preguntar una moneda que el comprador ya contestó."""
        moneda = datos.get("moneda_proveedor")
        if not moneda:
            return
        cambio = False
        for slot in self._proveedores_del_lote:
            if slot.get("proveedor_id") is None or slot.get(self.CAMPO_MONEDA_SLOT):
                continue
            slot[self.CAMPO_MONEDA_SLOT] = moneda
            slot[self.CAMPO_TC_SLOT] = datos.get("tipo_cambio")
            slot[self.CAMPO_MARGEN_SLOT] = datos.get("margen_venta")
            cambio = True
        if not cambio:
            return
        if carpeta is None:
            self._persistir_proveedores_del_lote()
            return
        # Se escribe en LA MISMA carpeta de la que se acaba de leer, no en la
        # que tenga el estado vivo: la migración corre justo mientras se
        # retoma el lote y `self._salida` puede no estar fijada todavía.
        try:
            almacen.guardar_meta(
                Path(carpeta),
                **{self.CLAVE_PROVEEDORES_LOTE: self._proveedores_del_lote})
        except Exception:  # noqa: BLE001
            pass  # la migración se reintenta la próxima vez que se abra el lote

    # ---------- respaldo LOCAL de los costos ingresados a mano ----------
    #
    # El hueco real de persistencia de este flujo (2026-09-09): casi todo lo
    # que hace el comprador ya queda guardado al instante en `lote.sqlite`
    # (decisiones, reparaciones, proveedor, moneda, tipo de cambio, margen,
    # nombre del proyecto...), pero los COSTOS que se escriben a mano en el
    # paso 6 viven en la REVISIÓN, que es efímera por diseño: se vacía entera
    # (`_vaciar_revision`) cada vez que el catálogo del lote se reprocesa
    # desde cero, y los `candidato_id` se reasignan.
    #
    # FASE 4: el disparador original era otro y ya no existe. La revisión
    # vivía en `staging_tuc` (Postgres COMPARTIDO), que se truncaba entero en
    # cuanto se procesaba el catálogo de OTRO proveedor -- si el comprador
    # cargaba el catálogo de mañana antes de terminar de decidir sobre el de
    # hoy, los costos se perdían sin aviso. Desde la Fase 2 cada lote tiene su
    # propio `lote.sqlite` y ningún otro lote lo puede tocar; el respaldo se
    # conserva porque reprocesar EL MISMO lote sigue vaciando su revisión.
    #
    # Por eso cada costo se escribe en DOS lugares: en la revisión (para que
    # el score y el precio de referencia lo usen) y en un espejo por clave
    # estable dentro del `lote.sqlite` de ESTA carpeta, que es el respaldo
    # real. La clave del espejo es `catalogo_origen||codigo_proveedor` -- NO el
    # `candidato_id`, que es un serial efímero y cambia con cada recarga.

    CLAVE_COSTOS_LOTE = "costos_candidatos"

    @staticmethod
    def clave_costo(catalogo_origen, codigo_proveedor) -> str:
        return f"{catalogo_origen or ''}||{codigo_proveedor or ''}"

    def leer_costos_lote(self) -> dict:
        """El espejo de costos guardado en la carpeta de este lote. Siempre un
        dict (vacío si no hay nada o si el dato quedó corrupto): esta función
        no puede ser la que rompa la pantalla de candidatos."""
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        datos = self._leer_marca_lote(Path(carpeta)) if carpeta else {}
        guardado = datos.get(self.CLAVE_COSTOS_LOTE)
        if isinstance(guardado, str):
            # Tolerancia: `almacen.guardar_meta` serializa a JSON, pero un
            # lote viejo pudo haber quedado con el JSON como texto.
            try:
                guardado = json.loads(guardado)
            except ValueError:
                guardado = None
        return guardado if isinstance(guardado, dict) else {}

    def guardar_costos_lote(self, costos: dict) -> None:
        """Vuelca el espejo completo a `lote.sqlite` (UPSERT de una sola clave,
        vía `almacen.guardar_meta`). Se llama en cada cambio de costo, así que
        el respaldo no depende de que el comprador se acuerde de guardar."""
        self._guardar_marca_lote(**{self.CLAVE_COSTOS_LOTE: costos})

    # ---------- respaldo LOCAL de tipo/género corregidos a mano ----------
    #
    # Mismo hueco que los costos, y por la misma razón (2026-09-09): cuando el
    # comprador corrige el tipo (categoría de calzado) o el género de una
    # referencia, eso se escribe en `staging_tuc.dim_candidato`, que es
    # Postgres COMPARTIDO y EFÍMERO -- se trunca al procesar el catálogo del
    # próximo proveedor. La corrección a mano es justamente el dato más caro de
    # volver a hacer (hay que mirar la foto de nuevo, una por una), así que se
    # espeja en el `lote.sqlite` de esta carpeta con la MISMA clave que los
    # costos (`catalogo_origen||codigo_proveedor`, nunca el `candidato_id`).

    CLAVE_CATEGORIAS_LOTE = "categorias_candidatos"

    def leer_categorias_lote(self) -> dict:
        """El espejo de tipo/género corregidos en la carpeta de este lote.
        Siempre un dict (vacío si no hay nada o si quedó corrupto)."""
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        datos = self._leer_marca_lote(Path(carpeta)) if carpeta else {}
        guardado = datos.get(self.CLAVE_CATEGORIAS_LOTE)
        if isinstance(guardado, str):
            try:
                guardado = json.loads(guardado)
            except ValueError:
                guardado = None
        return guardado if isinstance(guardado, dict) else {}

    def guardar_categorias_lote(self, categorias: dict) -> None:
        """Vuelca el espejo completo a `lote.sqlite` (UPSERT de una sola clave).
        Se llama en cada corrección, así que el respaldo no depende de que el
        comprador se acuerde de guardar."""
        self._guardar_marca_lote(**{self.CLAVE_CATEGORIAS_LOTE: categorias})

    # ---------- canal de venta del lote (TC Marcas vs TU Calzado) ----------
    #
    # Va en los metadatos LOCALES del lote, igual que la moneda y el mes de
    # venta: es una premisa de ESTA compra ("este pedido es para la línea de
    # marca"), no un hecho permanente del negocio. El motor de calificación lo
    # lee del mismo `lote.sqlite` (`motor_calificacion.canal_venta_lote`) y de
    # ahí decide contra qué dominio busca comparables y calcula el puntaje.

    ETIQUETA_CANAL = {"marca": "TC Marcas (marca reconocida)",
                      "tuc": "TU Calzado"}

    def canal_venta_lote(self) -> str | None:
        """"marca", "tuc", o None si este lote todavía no lo declaró."""
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        datos = self._leer_marca_lote(Path(carpeta)) if carpeta else {}
        valor = str(datos.get("canal_venta") or "").strip().lower()
        return valor if valor in self.ETIQUETA_CANAL else None

    def _guardar_canal_venta_elegido(self) -> None:
        """Anota la elección de los botones del paso 1 en el lote, al instante
        (mismo criterio que el resto del paso 1: nada que el comprador tenga
        que acordarse de guardar)."""
        canal = self.v_canal_venta.get().strip().lower()
        if canal not in self.ETIQUETA_CANAL:
            return
        self._guardar_marca_lote(canal_venta=canal)
        self._refrescar_estado_canal()

    def _refrescar_estado_canal(self) -> None:
        # Tolerante a que la pantalla de inicio todavía no esté armada: esto
        # se llama también desde caminos de arranque (retomar un lote) y no
        # puede ser lo que impida abrir la app.
        if not hasattr(self, "v_canal_venta") or not hasattr(self, "v_canal_estado"):
            return
        canal = self.v_canal_venta.get().strip().lower()
        if canal in self.ETIQUETA_CANAL:
            self.v_canal_estado.set(
                f"Canal: {self.ETIQUETA_CANAL[canal]} — el catálogo se va a comparar y "
                f"calificar contra lo vendido en ese canal.")
        else:
            self.v_canal_estado.set("Canal: sin elegir")

    def _restaurar_canal_del_lote(self, carpeta: Path) -> None:
        """Retomar un lote viejo restaura también PARA QUÉ CANAL era: sin esto,
        el paso 1 volvía a pedir una decisión que el lote ya tenía tomada (y,
        peor, el motor seguiría puntuando con el default 'tuc')."""
        datos = self._leer_marca_lote(carpeta)
        canal = str(datos.get("canal_venta") or "").strip().lower()
        if canal in self.ETIQUETA_CANAL and hasattr(self, "v_canal_venta"):
            self.v_canal_venta.set(canal)
        self._refrescar_estado_canal()

    def _asegurar_canal_venta_envio(self, carpeta: Path) -> bool:
        """Garantiza que el lote tenga canal declarado antes de calificar.

        Primero lo busca en el lote (pudo elegirse en el paso 1 de esta corrida
        o de una anterior). Si no está, lo pregunta acá mismo con las dos
        opciones y sin preseleccionar ninguna. Devuelve False solo si el
        comprador cierra el diálogo sin elegir -- en ese caso el envío se
        cancela, porque calificar sin canal sería elegir por él."""
        self._restaurar_canal_del_lote(carpeta)
        if self.canal_venta_lote():
            return True

        v_canal = tk.StringVar(value="")
        ventana = ctk.CTkToplevel(self)
        ventana.title("Canal de venta de esta compra")
        _centrar_en_ventana_principal(self, ventana, 520, 280)
        ventana.resizable(False, False)
        Etiqueta(ventana, text="¿Esta compra es para TC Marcas o para TU Calzado?",
                 style="Subtitulo.TLabel").pack(anchor="w", padx=16, pady=(16, 4))
        Etiqueta(ventana, text="De esto depende contra qué se buscan los comparables y "
                 "con qué se califica el catálogo entero: con lo que se ha vendido en TC "
                 "Marcas, o con lo que se ha vendido en TU Calzado.",
                 style="Suave.TLabel", justify="left", wraplength=480).pack(
            anchor="w", padx=16, pady=(0, 12), fill="x")
        ctk.CTkRadioButton(ventana, text="TC Marcas (marca reconocida)",
                           variable=v_canal, value="marca").pack(anchor="w", padx=16, pady=(2, 0))
        ctk.CTkRadioButton(ventana, text="TU Calzado (genérico)",
                           variable=v_canal, value="tuc").pack(anchor="w", padx=16, pady=(6, 0))

        elegido = {"canal": None}

        def guardar() -> None:
            canal = v_canal.get().strip().lower()
            if canal not in self.ETIQUETA_CANAL:
                messagebox.showinfo("Canal de venta",
                                    "Elegí una de las dos opciones para continuar.")
                return
            self.v_canal_venta.set(canal)
            self._guardar_canal_venta_elegido()
            elegido["canal"] = canal
            ventana.destroy()

        fila = Marco(ventana)
        fila.pack(fill="x", padx=16, pady=(18, 16))
        Boton(fila, text="Cancelar", style="Sutil.TButton",
              command=ventana.destroy).pack(side="right")
        Boton(fila, text="Continuar", style="Primario.TButton",
              command=guardar).pack(side="right", padx=(0, 8))
        ventana.transient(self)
        ventana.grab_set()
        _traer_al_frente(ventana)
        self.wait_window(ventana)
        return elegido["canal"] is not None

    # ---------- mes de venta esperado del pedido ----------
    #
    # Va en los metadatos LOCALES del lote, igual que la moneda y el tipo de
    # cambio: es un dato de ESTA decisión de compra ("este pedido lo pienso
    # vender en marzo"), no un hecho permanente del negocio. Queda registrado
    # para que después se pueda saber con qué mes se calculó el sugerido.

    MESES_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre")

    def mes_venta_lote(self) -> int | None:
        """El mes (1-12) en que el comprador espera vender este pedido, o None
        si nunca se declaró. Sin preguntar nada."""
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        datos = self._leer_marca_lote(Path(carpeta)) if carpeta else {}
        try:
            mes = int(datos.get("mes_venta_esperado") or 0)
        except (TypeError, ValueError):
            return None
        return mes if 1 <= mes <= 12 else None

    def asegurar_mes_venta_lote(self, forzar: bool = False) -> int | None:
        """Pregunta UNA VEZ POR LOTE para qué mes se espera vender el pedido.

        Se pregunta ANTES de mostrar los candidatos calificados (paso 6) porque
        es una premisa de la compra, no un detalle del final: el mismo catálogo
        se compra distinto si el pedido se vende en la entrada a clases que si
        se vende en diciembre. Hoy el mes se GUARDA y se muestra; lo que
        todavía no hace es mover el sugerido del paso 7 (ver la nota de
        `factor_estacional` más abajo)."""
        actual = self.mes_venta_lote()
        if actual and not forzar:
            return actual

        v_mes = tk.StringVar(value=self.MESES_ES[(actual or datetime.now().month) - 1])

        ventana = ctk.CTkToplevel(self)
        ventana.title("Mes de venta esperado")
        _centrar_en_ventana_principal(self, ventana, 500, 250)
        ventana.resizable(False, False)

        Etiqueta(ventana, text="¿Para qué mes esperás vender este pedido?",
                 style="Subtitulo.TLabel").pack(anchor="w", padx=16, pady=(16, 4))
        Etiqueta(ventana, text="Queda registrado con el lote, junto a la moneda y el margen, "
                 "para saber sobre qué temporada se armó este sugerido de compra.",
                 style="Suave.TLabel", justify="left", wraplength=460).pack(
            anchor="w", padx=16, pady=(0, 12), fill="x")

        combo = ctk.CTkComboBox(ventana, values=list(self.MESES_ES), variable=v_mes,
                                width=200, state="readonly")
        combo.pack(anchor="w", padx=16)

        elegido = {"mes": actual}

        def guardar() -> None:
            try:
                mes = self.MESES_ES.index(v_mes.get()) + 1
            except ValueError:
                return
            self._guardar_marca_lote(mes_venta_esperado=mes)
            elegido["mes"] = mes
            ventana.destroy()

        fila = Marco(ventana)
        fila.pack(fill="x", padx=16, pady=(16, 16))
        Boton(fila, text="Guardar", style="Primario.TButton",
              command=guardar).pack(side="right")

        ventana.transient(self)
        ventana.grab_set()
        _traer_al_frente(ventana)
        self.wait_window(ventana)
        return elegido["mes"]

    def asegurar_monedas_de_proveedores(self) -> None:
        """Asegura la moneda de CADA proveedor del lote (FASE 3).

        Un lote con un solo proveedor -- el caso mayoritario -- hace UNA sola
        pregunta, exactamente como antes de esta fase. Con dos catálogos de
        proveedores distintos se pregunta una vez por cada uno, la primera vez
        que hace falta, y nunca se vuelve a preguntar por el que ya contestó."""
        pendientes = [s for s in self._proveedores_del_lote
                      if s.get("proveedor_id") is not None]
        if not pendientes:
            self.asegurar_moneda_lote()  # lote sin lista (pantalla suelta/pruebas)
            return
        for slot in pendientes:
            self.asegurar_moneda_lote(proveedor_id=int(slot["proveedor_id"]))

    def asegurar_moneda_lote(self, forzar: bool = False, proveedor_id=None) -> dict:
        """Pregunta UNA VEZ POR PROVEEDOR DEL LOTE en qué moneda cotiza (y, si
        es moneda extranjera, con qué tipo de cambio convertir a colones).

        Una vez por proveedor, no por candidato: es un dato del proveedor y del
        momento de la cotización, igual para las 50 referencias de SU catálogo.
        Si ya está declarado no vuelve a preguntar, salvo `forzar=True` (el
        botón «Cambiar» del paso 6, para corregir un tipo de cambio mal
        tecleado).

        FASE 3 (2026-09-16): antes era una vez por LOTE. Un lote puede traer
        catálogos de dos proveedores, uno en colones y otro en dólares o
        yuanes; con un solo valor, los precios de uno de los dos salían mal.
        `proveedor_id=None` sigue significando "el proveedor activo", así que
        un lote de un solo proveedor se comporta igual que antes.

        TU Calzado vende en colones: todo el cálculo de precio de venta parte
        del costo YA CONVERTIDO. El tipo de cambio NO tiene valor por defecto
        a propósito -- que lo escriba el comprador con el del día, en vez de
        heredar en silencio una constante vieja del código."""
        if proveedor_id is None:
            proveedor_id = self._proveedor_id_actual
        actual = self.config_precio_proveedor(proveedor_id)
        if actual["moneda"] and not forzar:
            return actual

        v_moneda = tk.StringVar(value=actual["moneda"] or "CRC")
        v_tc = tk.StringVar(value=(f"{actual['tipo_cambio']:g}"
                                   if actual["tipo_cambio"] else ""))

        nombre_prov = self.nombre_de_proveedor_del_lote(proveedor_id) \
            or (self._proveedor_nombre_actual or "")

        ventana = ctk.CTkToplevel(self)
        ventana.title("Moneda del proveedor")
        _centrar_en_ventana_principal(self, ventana, 480, 340)
        ventana.resizable(False, False)

        titulo = ("¿En qué moneda está el costo de este proveedor?" if not nombre_prov
                  else f"¿En qué moneda está el costo de {nombre_prov}?")
        Etiqueta(ventana, text=titulo,
                 style="Subtitulo.TLabel").pack(anchor="w", padx=16, pady=(16, 4))
        Etiqueta(ventana, text="TU Calzado vende en colones, así que el precio de venta "
                 "sugerido se calcula siempre sobre el costo convertido a colones. "
                 "Cada proveedor del lote declara su propia moneda.",
                 style="Suave.TLabel", justify="left", wraplength=440).pack(
            anchor="w", padx=16, pady=(0, 10), fill="x")

        # Las opciones se generan desde `MONEDAS_PROVEEDOR`: agregar una moneda
        # (el yuan, 2026-09-16) no es tocar esta pantalla.
        for codigo, rasgos in MONEDAS_PROVEEDOR.items():
            ctk.CTkRadioButton(ventana, text=rasgos["etiqueta"], variable=v_moneda,
                               value=codigo).pack(anchor="w", padx=16, pady=(2, 2))

        marco_tc = Marco(ventana)
        marco_tc.pack(fill="x", padx=16, pady=(8, 4))
        etq_tc = Etiqueta(marco_tc, text="Tipo de cambio a usar (₡ por $)",
                          style="Suave.TLabel")
        etq_tc.pack(side="left")
        entrada_tc = ctk.CTkEntry(marco_tc, textvariable=v_tc, width=90,
                                  placeholder_text="ej. 505")
        entrada_tc.pack(side="left", padx=(8, 0))

        estado = Etiqueta(ventana, text="", style="Suave.TLabel", wraplength=440,
                          justify="left")
        estado.pack(anchor="w", padx=16, pady=(4, 0), fill="x")

        def refrescar_tc(*_a) -> None:
            # El campo de tipo de cambio solo tiene sentido con una moneda que
            # haya que convertir (dólares o yuanes), y dice de qué moneda es.
            moneda = v_moneda.get()
            if moneda_convierte(moneda):
                simbolo = _rasgo_moneda(moneda, "simbolo", "?")
                etq_tc.configure(text=f"Tipo de cambio a usar (₡ por {simbolo})")
                entrada_tc.configure(state="normal",
                                     placeholder_text=_rasgo_moneda(moneda, "ejemplo_tc"))
            else:
                etq_tc.configure(text="Tipo de cambio: no hace falta (ya es en colones)")
                entrada_tc.configure(state="disabled")

        v_moneda.trace_add("write", refrescar_tc)
        refrescar_tc()

        resultado = dict(actual)

        def guardar() -> None:
            moneda = v_moneda.get()
            tipo_cambio = None
            if moneda_convierte(moneda):
                nombre_moneda = _rasgo_moneda(moneda, "plural", moneda)
                texto = v_tc.get().strip().replace(",", "")
                if not texto:
                    estado.configure(
                        text=f"Con {nombre_moneda} hace falta el tipo de cambio.")
                    return
                try:
                    tipo_cambio = float(texto)
                except ValueError:
                    estado.configure(text="Tipo de cambio inválido.")
                    return
                if tipo_cambio <= 0:
                    estado.configure(text="El tipo de cambio tiene que ser mayor que cero.")
                    return
            self.guardar_config_precio_proveedor(proveedor_id, moneda, tipo_cambio,
                                                 actual["margen"])
            resultado.update({"moneda": moneda, "tipo_cambio": tipo_cambio})
            ventana.destroy()

        fila = Marco(ventana)
        fila.pack(fill="x", padx=16, pady=(10, 16))
        Boton(fila, text="Guardar", style="Primario.TButton",
              command=guardar).pack(side="right")

        ventana.transient(self)
        ventana.grab_set()
        _traer_al_frente(ventana)
        self.wait_window(ventana)
        return resultado

    def _guardar_marca_lote(self, **campos) -> None:
        """Anota datos del lote en su carpeta de trabajo, sin pisar lo que ya
        había (UPSERT por clave: el envío no borra el proveedor y viceversa).

        Antes esto era leer el JSON entero, hacer `update` en memoria y
        reescribirlo completo — si dos pantallas anotaban dos claves distintas
        a la vez, la segunda pisaba la primera con su copia vieja del resto.
        Ahora cada clave se escribe sola, en una transacción.

        La carpeta destino se toma de `self._salida` y, si todavía no está
        fijada, de la caja de texto del paso 1 (`v_salida`). Sin ese respaldo,
        todo lo que se anotaba ANTES de «▶ Limpiar fotos» (el proveedor, que
        se elige en el paso 1) se perdía en silencio: en un proyecto nuevo
        `v_salida` ya tiene la carpeta pero `self._salida` sigue en None hasta
        que `_procesar` la fija."""
        carpeta = self._salida or (self.v_salida.get().strip() or None)
        if not carpeta:
            return
        if self._salida is None:
            # Fijarla ya evita que la próxima escritura vuelva a depender del
            # texto de la caja (y que dos anotaciones caigan en carpetas
            # distintas si el usuario cambia el texto en el medio).
            self._salida = Path(carpeta)
        try:
            # La carpeta de trabajo puede no existir todavía la primera vez
            # (la crean los workers al escribir el primer recorte, después de
            # que `_procesar` anota acá el origen del lote). Sin este mkdir la
            # marca se perdía en silencio en el lote NUEVO — justo el caso que
            # después había que poder retomar.
            Path(self._salida).mkdir(parents=True, exist_ok=True)
            almacen.guardar_meta(self._salida, **campos)
        except Exception:  # noqa: BLE001
            pass  # no es crítico: sin la marca solo se pierde el salto de pasos

    def _restaurar_proveedor_del_lote(self, carpeta: Path) -> None:
        datos = self._leer_marca_lote(carpeta)
        pid, nombre = datos.get("proveedor_id"), datos.get("proveedor_nombre")
        if pid is None or not nombre:
            return
        self._proveedor_id_actual = int(pid)
        self._proveedor_nombre_actual = str(nombre)
        self.v_proveedor.set(f"Proveedor: {self._proveedor_nombre_actual}")
        # FASE 1 multi-proveedor: además del proveedor "actual", se recupera la
        # lista completa de proveedores agregados a este lote.
        self._restaurar_proveedores_del_lote(carpeta)

    def _restaurar_entrada_del_lote(self, carpeta: Path) -> None:
        """Restaura DE DÓNDE salieron las fotos de este lote (la carpeta o los
        archivos sueltos del paso 1), tal como `_restaurar_proveedor_del_lote`
        restaura de quién es.

        Sin esto, retomar un lote dejaba el origen en blanco y "▶ Limpiar
        fotos" moría con "Elegí una carpeta o archivos de entrada". No se pisa
        un origen que el usuario ya haya elegido a mano en esta sesión."""
        if self._entrada_archivos or self.v_entrada.get().strip():
            return
        datos = self._leer_marca_lote(carpeta)
        archivos = datos.get("entrada_archivos")
        if archivos:
            self._entrada_archivos = [Path(a) for a in archivos]
            # Mismo texto informativo que pone `_elegir_archivos`: la caja de
            # texto del paso 1 no muestra rutas cuando son varios archivos.
            self.v_entrada.set(f"{len(self._entrada_archivos)} archivo(s) seleccionado(s)")
            return
        carpeta_entrada = datos.get("entrada_dir")
        if carpeta_entrada:
            self._entrada_archivos = None
            self.v_entrada.set(str(carpeta_entrada))

    def _restaurar_nombre_proyecto_del_lote(self, carpeta: Path) -> None:
        """Restaura el nombre del proyecto guardado en metadatos del lote."""
        datos = self._leer_marca_lote(carpeta)
        nombre = datos.get("nombre_proyecto")
        if nombre:
            self._nombre_proyecto = str(nombre)

    def _origen_del_lote_legible(self, carpeta: Path) -> str:
        """Cómo se le cuenta al comprador de dónde vino el lote. Cadena vacía
        si el lote es viejo y no tiene el origen anotado."""
        datos = self._leer_marca_lote(carpeta)
        archivos = datos.get("entrada_archivos")
        if archivos:
            if len(archivos) == 1:
                return Path(archivos[0]).name
            return f"{len(archivos)} archivo(s) de {Path(archivos[0]).parent.name}"
        if datos.get("entrada_dir"):
            return str(datos["entrada_dir"])
        return ""

    def _catalogos_posibles_del_lote(self, carpeta: Path) -> set[str]:
        """Con qué `catalogo_origen` pudo haber entrado este lote a staging.

        `cargar_carpeta_limpia` deriva el nombre del catálogo de la carpeta
        que recibe, y lo que se le manda NO es la carpeta de trabajo sino la
        de exportados (`<trabajo>_para_asistente_compras`, ver
        `_enviar_a_asistente_compras`). Se aceptan los dos nombres porque hay
        cargas viejas hechas por fuera de esta ventana, apuntando directo a
        una carpeta de fotos ya limpias."""
        base = Path(carpeta).name
        posibles = {base, f"{base}_para_asistente_compras"}
        guardado = self._leer_marca_lote(carpeta).get("catalogo_origen")
        if guardado:
            posibles.add(str(guardado))
        return posibles

    def _lote_ya_esta_en_staging(self, carpeta: Path) -> bool:
        """¿Este lote ya se envió a calificar y sigue cargado?

        Se pregunta a `resumen_catalogo_activo()` (lo que hay HOY en la
        revisión de este lote) en vez de confiar solo en la marca de envío: la
        marca dice "se envió alguna vez", no "sigue cargado", y el lote pudo
        haberse vaciado después para reprocesarlo desde cero.

        FASE 4: el motivo original era otro -- con la revisión en `staging_tuc`
        compartido, cargar el catálogo de OTRO lote truncaba este y la marca
        quedaba mintiendo. Eso ya no puede pasar (cada lote tiene su propio
        `lote.sqlite`), pero la comprobación sigue siendo la correcta."""
        if not self._proveedor_nombre_actual:
            return False
        try:
            import motor_candidatos
            resumen = motor_candidatos.resumen_catalogo_activo()
        except Exception:  # noqa: BLE001
            # Sin base disponible no se inventa un salto: se sigue el camino
            # normal, que es lo seguro.
            return False
        posibles = self._catalogos_posibles_del_lote(carpeta)
        return any(f["proveedor"] == self._proveedor_nombre_actual
                   and f["catalogo_origen"] in posibles and f["n"] > 0
                   for f in resumen)

    def _detectar_paso_del_lote(self, carpeta: Path) -> int:
        """En qué pantalla del asistente debería caer este lote."""
        if not self._recortes:
            return 1
        pendientes = [n for n, r in self._recortes.items()
                      if r.get("estado", {}).get("decision") not in
                      (decisiones.APROBADO, decisiones.DESCARTADO)]
        if pendientes:
            # Falta decidir fotos: la pantalla útil es "Limpiar y revisar"
            # (paso 3), aunque el proveedor ya se sepa.
            return 3
        if self._lote_ya_esta_en_staging(carpeta):
            return 6
        return 4

    def _saltar_al_paso_del_lote(self) -> None:
        """Lleva la ventana directo a la pantalla que le corresponde al lote
        que se acaba de cargar, en vez de dejar al comprador rehaciendo pasos
        que ese lote ya tiene hechos."""
        carpeta = self._salida
        if carpeta is None:
            self._ir_a_paso(1)
            return
        try:
            paso = self._detectar_paso_del_lote(carpeta)
        except Exception as exc:  # noqa: BLE001
            # Detectar el avance nunca puede impedir abrir el lote: si algo
            # falla, se cae al camino normal de revisión.
            self.v_log.set(f"No se pudo deducir el avance del lote ({exc}); se abre en revisión.")
            self._ir_a_paso(3)
            return

        if paso == 6:
            try:
                candidatos = self._candidatos_del_lote()
            except Exception as exc:  # noqa: BLE001
                self.v_log.set(f"El lote ya se había enviado, pero no se pudieron leer "
                               f"los candidatos ({exc}).")
                self._mostrar_resumen_final()
                return
            if candidatos:
                self.v_log.set(f"Este lote ya estaba calificado: {len(candidatos)} candidato(s) "
                               f"en revisión. Se abre directo en el paso 6.")
                self._mostrar_vista_candidatos(candidatos)
                return
            paso = 4  # marcado como enviado pero staging quedó vacío

        if paso == 4:
            self.v_log.set("Todas las fotos de este lote ya están decididas — "
                           "se abre directo en el resumen del lote.")
            self._mostrar_resumen_final()
            return

        self._ir_a_paso(3 if paso >= 3 else 1)
        if paso >= 3:
            # Retomar un lote también deja la primera foto abierta, igual que
            # terminar de limpiar (`_aterrizar_en_revision`).
            self._abrir_primera_foto()

    # Esto NO es estado del lote: es una sola preferencia del programa (cuál
    # fue la última carpeta de trabajo). Se deja como JSON a propósito —
    # levantar un `config.sqlite` con una tabla de una fila y una columna para
    # guardar una ruta sería complejidad sin ninguna de las ventajas que sí
    # justifican SQLite en el lote (transacciones sobre varias tablas
    # relacionadas, concurrencia entre procesos, consultas).
    #
    # Lo que SÍ había que arreglar es cómo se lee y se escribe (ver
    # `_leer_ultima_carpeta`): el archivo se leía con un `except` que se comía
    # el error, así que un archivo mal escrito dejaba la app sin retomar nada y
    # sin decir por qué.
    ARCHIVO_CONFIG_SESION = PROYECTO / "_ultima_carpeta.json"

    def _guardar_ultima_carpeta(self) -> None:
        """Escritura atómica: se escribe a un temporal y se reemplaza.

        `write_text` directo puede dejar el archivo truncado si el proceso
        muere a mitad — y el lector de este archivo trataba cualquier problema
        de formato como "no hay nada que retomar", en silencio.
        """
        try:
            tmp = self.ARCHIVO_CONFIG_SESION.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"salida": str(self._salida)}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.ARCHIVO_CONFIG_SESION)
        except OSError:
            pass  # no es crítico — si no se puede guardar, la próxima vez se pide a mano

    def _leer_ultima_carpeta(self) -> Path | None:
        """La última carpeta de trabajo usada, o None si no hay ninguna.

        CAUSA RAÍZ del bug de auto-resume (2026-09-08): el archivo en disco
        decía

            {"salida": "C:\\Users\\Tucalzado\\...\\Prueba 03.09.2026"}

        con UNA barra invertida, no dos. Eso no es JSON válido (`\\U` es un
        escape inexistente), así que `json.loads` lanzaba `JSONDecodeError`, el
        `except` de arriba hacía `return` y la app arrancaba en el paso 1 con
        "Carpeta activa: ninguna todavía" — exactamente el síntoma reportado, y
        sin una sola línea de log que lo explicara. La función a la que
        culpábamos (`_cargar_salida_existente`) nunca se llegaba a ejecutar.

        Se arregla en dos niveles: la escritura ya no puede producir un archivo
        así (arriba), y la lectura ya no se rinde en silencio — si el JSON no
        parsea, se rescata la ruta con una expresión regular sobre el texto
        crudo (que es lo único que hay ahí: una ruta) y se deja constancia en
        el log en vez de fingir que no había nada guardado.
        """
        if not self.ARCHIVO_CONFIG_SESION.exists():
            return None
        try:
            texto = self.ARCHIVO_CONFIG_SESION.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            salida = json.loads(texto).get("salida")
        except (json.JSONDecodeError, AttributeError):
            m = re.search(r'"salida"\s*:\s*"(.+?)"\s*[},]', texto, re.DOTALL)
            if not m:
                self.v_log.set("No se pudo leer la última carpeta usada "
                               "(archivo de sesión ilegible); elegí una carpeta de trabajo.")
                return None
            salida = m.group(1).replace("\\\\", "\\")
            self.v_log.set(f"El archivo de sesión estaba mal escrito; se rescató la ruta "
                           f"({salida}) y se reescribió bien.")
        if not salida:
            return None
        carpeta = Path(salida)
        # Reescribir con el formato correcto para que el rescate sea de una vez
        # y no en cada arranque.
        self._salida = carpeta
        self._guardar_ultima_carpeta()
        return carpeta

    @staticmethod
    def _contar_recortes_en(carpeta: Path) -> int:
        """Cuántos recortes vigentes tiene el lote de `carpeta`, sin cargar
        nada en memoria — para saber si hay algo real que retomar antes de
        tocar `self._recortes`.

        Antes esto duplicaba A MANO el criterio de `_recargar_manifiesto`
        (mismo glob, mismo filtro `png`+`estado`, mismo `decisiones.eliminados`)
        con un comentario avisando que había que actualizar las dos juntas.
        Ahora las dos salen del mismo `WHERE` de `almacen.py`, así que no
        pueden desincronizarse."""
        try:
            return almacen.contar_recortes(carpeta)
        except Exception:  # noqa: BLE001
            return 0

    def _preguntar_retomar_o_nuevo(self, carpeta: Path, n_recortes: int) -> bool:
        """Diálogo modal que pregunta si retomar la última carga o empezar en
        una carpeta nueva. Devuelve True si retomar, False si trabajar en otra.

        Cerrar con X = retomar (es la opción segura).

        Restaurado 2026-09-08 a pedido: antes se preguntaba explícitamente
        (retomar vs. empezar de cero), se cambió a automático, ahora se vuelve
        a preguntar porque había confusión."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("¿Retomar o nuevo?")
        _centrar_en_ventana_principal(self, ventana, 420, 200)
        Etiqueta(ventana, text="Última carga sin terminar",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 4))

        detalle = f"{carpeta.name}\n{n_recortes} recorte{'s' if n_recortes != 1 else ''}"
        Etiqueta(ventana, text=detalle, style="Suave.TLabel",
                justify="left").pack(anchor="w", padx=14, pady=(0, 14))

        Etiqueta(ventana, text="¿Retomar esta carga o trabajar en una carpeta diferente?",
                style="Suave.TLabel", justify="left").pack(anchor="w", padx=14, pady=(0, 14), fill="x")

        resultado: dict = {"retomar": True}  # Por defecto retomar (es lo seguro)

        def elegir_retomar() -> None:
            resultado["retomar"] = True
            ventana.destroy()

        def elegir_otra() -> None:
            resultado["retomar"] = False
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Trabajar en otra carpeta", style="Sutil.TButton",
             command=elegir_otra).pack(side="left")
        Boton(fila_botones, text=f"↺ Retomar esta carga ({n_recortes})", style="Primario.TButton",
             command=elegir_retomar).pack(side="right")

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)
        return resultado["retomar"]

    def _listar_proyectos_anteriores(self) -> list[dict]:
        """Todos los proyectos guardados en la carpeta central de lotes, cada
        uno con su nombre real (el que el usuario escribió), cuántos recortes
        tiene y cuándo se tocó por última vez. Ordenados del más reciente al
        más viejo, para que "el de ayer" siempre esté arriba."""
        proyectos: list[dict] = []
        if not CARPETA_LOTES_CENTRAL.exists():
            return proyectos
        for carpeta in CARPETA_LOTES_CENTRAL.iterdir():
            if not carpeta.is_dir():
                continue
            datos = self._leer_marca_lote(carpeta)
            nombre = datos.get("nombre_proyecto") or carpeta.name
            n = self._contar_recortes_en(carpeta)
            try:
                mtime = carpeta.stat().st_mtime
            except OSError:
                mtime = 0
            proyectos.append({"carpeta": carpeta, "nombre": str(nombre),
                              "n_recortes": n, "mtime": mtime})
        proyectos.sort(key=lambda p: p["mtime"], reverse=True)
        return proyectos

    def _preguntar_retomar_o_nuevo_v2(self) -> Path | None:
        """Diálogo modal al abrir la app: lista TODOS los proyectos guardados
        (con su nombre real) para elegir cuál retomar, o empezar uno nuevo.
        Devuelve la carpeta elegida para retomar, o None si eligió "nuevo"."""
        proyectos = self._listar_proyectos_anteriores()

        ventana = ctk.CTkToplevel(self)
        ventana.title("¿Qué querés hacer?")
        alto = min(560, 180 + 64 * max(1, len(proyectos)))
        _centrar_en_ventana_principal(self, ventana, 460, alto)

        Etiqueta(ventana, text="¿Qué querés hacer?",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 4))

        resultado: dict = {"carpeta": None}

        def elegir_nuevo() -> None:
            resultado["carpeta"] = None
            ventana.destroy()

        Boton(ventana, text="＋ Nuevo proyecto", style="Primario.TButton",
             command=elegir_nuevo).pack(fill="x", padx=14, pady=(0, 10))

        etq_sin_proyectos = {"widget": None}
        etq_titulo_lista = {"widget": None}
        lista = {"widget": None}

        def elegir_retomar(carpeta: Path) -> None:
            resultado["carpeta"] = carpeta
            ventana.destroy()

        def eliminar_proyecto(p: dict) -> None:
            # Confirmación explícita: es irreversible (borra la carpeta del
            # lote completa, incluido su `lote.sqlite`) -- para descartar un
            # borrador o un lote creado por error (pedido 2026-09-16).
            if not messagebox.askyesno(
                "Eliminar proyecto",
                f'¿Eliminar "{p["nombre"]}" ({p["n_recortes"]} recorte(s))?\n\n'
                "Esto borra la carpeta del lote por completo, con sus fotos, "
                "decisiones y costos guardados. No se puede deshacer.",
                parent=ventana,
            ):
                return
            try:
                shutil.rmtree(p["carpeta"])
            except OSError as exc:
                messagebox.showerror("No se pudo eliminar", str(exc), parent=ventana)
                return
            refrescar_lista()

        def refrescar_lista() -> None:
            proyectos_actuales = self._listar_proyectos_anteriores()
            if lista["widget"] is not None:
                lista["widget"].destroy()
                lista["widget"] = None
            if etq_sin_proyectos["widget"] is not None:
                etq_sin_proyectos["widget"].destroy()
                etq_sin_proyectos["widget"] = None
            if etq_titulo_lista["widget"] is not None:
                etq_titulo_lista["widget"].destroy()
                etq_titulo_lista["widget"] = None

            if proyectos_actuales:
                titulo = Etiqueta(ventana, text="O retomar uno anterior:",
                                  style="Suave.TLabel")
                titulo.pack(anchor="w", padx=14, pady=(0, 4), before=None)
                etq_titulo_lista["widget"] = titulo
                cont = ctk.CTkScrollableFrame(ventana, fg_color=PAR_PANEL_SUAVE)
                cont.pack(fill="both", expand=True, padx=14, pady=(0, 14))
                lista["widget"] = cont

                for p in proyectos_actuales:
                    fila = Marco(cont)
                    fila.pack(fill="x", pady=(0, 6))
                    fecha = (datetime.fromtimestamp(p["mtime"]).strftime("%d/%m/%Y %H:%M")
                             if p["mtime"] else "")
                    detalle = f"{p['n_recortes']} recorte{'s' if p['n_recortes'] != 1 else ''} · {fecha}"
                    textos = Marco(fila)
                    textos.pack(side="left", fill="x", expand=True)
                    Etiqueta(textos, text=p["nombre"], style="TLabel",
                            justify="left").pack(anchor="w")
                    Etiqueta(textos, text=detalle, style="Suave.TLabel",
                            justify="left").pack(anchor="w")
                    Boton(fila, text="Retomar", style="Sutil.TButton",
                         command=lambda c=p["carpeta"]: elegir_retomar(c)).pack(side="right")
                    Boton(fila, text="🗑", style="Sutil.TButton", width=36,
                         command=lambda pp=p: eliminar_proyecto(pp)).pack(side="right", padx=(0, 6))
            else:
                etq = Etiqueta(ventana, text="Todavía no hay ningún proyecto anterior.",
                              style="Suave.TLabel")
                etq.pack(anchor="w", padx=14, pady=(0, 14))
                etq_sin_proyectos["widget"] = etq

        refrescar_lista()

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)
        return resultado["carpeta"]

    def _pedir_nombre_proyecto(self, nombre_previo: str | None = None) -> str | None:
        """Diálogo modal para que el usuario escriba el nombre del proyecto
        nuevo: no se puede AVANZAR sin nombre (ni vacío ni repetido), pero sí
        se puede CERRAR EL PROGRAMA desde acá sin guardar nada.

        Antes ni la "X" ni ningún botón cerraban esta ventana -- si alguien
        abría la app por error, quedaba atrapado sin poder salir sin
        completar un nombre de proyecto (reclamo del usuario 2026-09-09).
        Ahora "X" y "✕ Cerrar programa" cierran TODA la app (no hay ningún
        lote ni proceso corriendo todavía en este punto, así que no hay nada
        que se pierda). Devuelve el nombre ya validado, o None si el usuario
        cerró el programa en vez de nombrar el proyecto."""
        nombres_en_uso = {p["nombre"].strip().lower() for p in self._listar_proyectos_anteriores()}

        ventana = ctk.CTkToplevel(self)
        ventana.title("Nombre del proyecto")
        _centrar_en_ventana_principal(self, ventana, 420, 200)
        def _cerrar_programa() -> None:
            ventana.destroy()
            self.destroy()

        ventana.protocol("WM_DELETE_WINDOW", _cerrar_programa)

        Etiqueta(ventana, text="Nombre del proyecto:",
                style="Suave.TLabel").pack(anchor="w", padx=14, pady=(14, 4))

        entrada = ctk.CTkEntry(ventana, corner_radius=RADIO_CONTROL,
                              fg_color=PAR_PANEL_SUAVE, border_color=PAR_BORDE,
                              text_color=PAR_TEXTO)
        entrada.pack(fill="x", padx=14, pady=(0, 4))

        if nombre_previo:
            entrada.insert(0, nombre_previo)
            entrada.select_range(0, "end")  # Seleccionar todo para que pueda reemplazar fácil

        v_error = tk.StringVar(value="")
        Etiqueta(ventana, textvariable=v_error, style="Alerta.TLabel",
                justify="left").pack(anchor="w", padx=14, pady=(0, 10))

        resultado: dict = {"nombre": None}

        def guardar() -> None:
            nombre = entrada.get().strip()
            if not nombre:
                v_error.set("Poné un nombre para el proyecto antes de continuar.")
                return
            if nombre.lower() in nombres_en_uso:
                v_error.set(f'Ya existe un proyecto llamado "{nombre}". Elegí otro nombre.')
                return
            resultado["nombre"] = nombre
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Guardar", style="Primario.TButton",
             command=guardar).pack(side="right")
        Boton(fila_botones, text="✕ Cerrar programa", style="Sutil.TButton",
             command=_cerrar_programa).pack(side="left")

        entrada.bind("<Return>", lambda e: guardar())

        ventana.transient(self)
        ventana.grab_set()
        entrada.focus_set()
        self.wait_window(ventana)
        return resultado["nombre"]

    def _cargar_ultima_carpeta(self) -> None:
        """Al abrir la app: muestra el diálogo inicial con la lista de TODOS
        los proyectos guardados (por su nombre real) para retomar uno, o
        "＋ Nuevo proyecto" para pedir un nombre y arrancar de cero."""
        carpeta = self._preguntar_retomar_o_nuevo_v2()

        if carpeta is not None:
            # Usuario eligió retomar uno de la lista — ya viene identificado
            # por nombre, no hace falta volver a preguntarlo.
            self.v_salida.set(str(carpeta))
            self._cargar_salida_existente(carpeta, avisar_siempre=True)
            self._restaurar_nombre_proyecto_del_lote(carpeta)
        else:
            # Usuario eligió nuevo proyecto: SIEMPRE se pide un nombre, y el
            # diálogo no deja seguir sin uno válido y sin repetir (ver
            # `_pedir_nombre_proyecto`).
            nombre = self._pedir_nombre_proyecto()
            if nombre is None:
                return  # el usuario cerró el programa desde el diálogo, no hay nada más que hacer
            self._nombre_proyecto = nombre
            # La carpeta de trabajo del lote vive SIEMPRE dentro de la
            # carpeta central de lotes, nombrada como el proyecto — así el
            # historial completo de corridas queda junto en un solo lugar en
            # vez de disperso donde el usuario haya elegido.
            carpeta_proyecto = self._carpeta_para_nombre_proyecto(nombre)
            carpeta_proyecto.mkdir(parents=True, exist_ok=True)
            self.v_salida.set(str(carpeta_proyecto))
            # …y también en `self._salida`: es la carpeta donde `_guardar_marca_lote`
            # anota el proveedor, el origen y el nombre del proyecto. Dejarla solo
            # en la caja de texto hacía que TODO lo anotado en el paso 1 (empezando
            # por el proveedor) se descartara sin aviso, y el lote quedaba sin
            # proveedor en disco.
            self._salida = carpeta_proyecto
            self._guardar_marca_lote(nombre_proyecto=nombre)

    @staticmethod
    def _carpeta_para_nombre_proyecto(nombre: str) -> Path:
        """Convierte el nombre que escribe el usuario en una carpeta válida
        dentro de `CARPETA_LOTES_CENTRAL`, sin pisar una corrida existente
        con el mismo nombre (le agrega un sufijo numérico si hace falta)."""
        limpio = re.sub(r'[<>:"/\\|?*]', "", nombre).strip()
        limpio = re.sub(r"\s+", " ", limpio) or "Proyecto sin nombre"
        candidata = CARPETA_LOTES_CENTRAL / limpio
        if not candidata.exists():
            return candidata
        n = 2
        while (CARPETA_LOTES_CENTRAL / f"{limpio} ({n})").exists():
            n += 1
        return CARPETA_LOTES_CENTRAL / f"{limpio} ({n})"


    def _revertir_todo(self) -> None:
        """Revierte TODA la carpeta activa de una — cada recorte que tenga
        un respaldo en `_originales/` vuelve a esa versión. Con confirmación
        antes: afecta a todo el lote de una vez, no una foto puntual."""
        if not self._salida:
            messagebox.showinfo("Revertir todo", "Primero elegí una carpeta de trabajo.")
            return
        respaldo = self._salida / reparar.CARPETA_ORIGINALES
        if not respaldo.exists():
            messagebox.showinfo("Revertir todo", "No hay ningún respaldo en esta carpeta — "
                                "ningún recorte tuvo una corrección aplicada todavía.")
            return
        stems = sorted({p.stem for p in respaldo.glob("*.png")} | {p.stem for p in respaldo.glob("*.jpg")})
        if not stems:
            messagebox.showinfo("Revertir todo", "No hay ningún respaldo en esta carpeta.")
            return
        if not messagebox.askyesno(
                "Revertir todo",
                f"Esto revierte {len(stems)} recorte(s) a su versión original, deshaciendo "
                f"cualquier recorte de sombra o reparación de suela que se les haya aplicado.\n\n"
                f"¿Continuar?"):
            return
        revertidos = sum(1 for stem in stems if reparar.revertir(self._salida, stem))
        self._recargar_manifiesto()
        messagebox.showinfo("Revertir todo", f"{revertidos} de {len(stems)} recorte(s) revertidos.")

    def _vaciar_carpeta_trabajo(self) -> None:
        """Borra SOLO lo que el pipeline genera en la carpeta de trabajo
        (fotos procesadas, manifiesto, decisiones, respaldos) — nunca las
        fotos de entrada/extraídas, aunque vivan en esa misma carpeta.

        Existe porque "Exportar aprobados" mira TODA la bitácora de
        decisiones de la carpeta, de todas las cargas anteriores que se
        hayan hecho ahí — si se reusa la misma carpeta de trabajo para un
        lote nuevo (otro Excel, otro PDF), sin esto las aprobaciones viejas
        se cuelan en la exportación de hoy.

        La primera versión de este botón borraba TODO el contenido de la
        carpeta sin distinguir "caché del programa" de "fotos que el
        usuario puso ahí" — si la carpeta de trabajo era la misma carpeta
        de fotos extraídas (caso real: `Excel\\_fotos_extraidas`), eso
        borraba las fotos de entrada también. Por eso ahora es una lista
        explícita de lo que el pipeline crea, nada más."""
        SUBCARPETAS_PIPELINE = ("transparente", "blanco", "revision", "debug",
                                reparar.CARPETA_ORIGINALES, "_pasos", "_vecinos")
        # El estado del lote se vacía por SQL (`almacen.vaciar`, más abajo), no
        # borrando `lote.sqlite`: la interfaz puede tener el archivo abierto y
        # en Windows eso falla o deja los sidecars `-wal`/`-shm` huérfanos.
        # Estos patrones son los archivos VIEJOS que la migración dejó al lado
        # como red de seguridad: acá sí se borran, porque vaciar la carpeta es
        # justamente decir "no quiero nada de las cargas anteriores".
        PATRONES_ARCHIVO_PIPELINE = ("manifiesto_*.jsonl", "plan_*.jsonl",
                                     "decisiones.jsonl", "reparaciones.jsonl",
                                     "_lote_asistente.json")

        if not self._salida or not self._salida.exists():
            messagebox.showinfo("Vaciar carpeta de trabajo", "Primero elegí una carpeta de trabajo.")
            return

        # Vaciar mientras el worker sigue vivo (pausado o corriendo) deja el
        # vaciado a medias: el proceso sigue escribiendo manifiesto/recortes
        # sobre lo que se acaba de borrar en cuanto se reanuda o simplemente
        # sigue su lámina actual. Pasó de verdad al combinar Pausar + Vaciar.
        if any(p.poll() is None for p in self._procs):
            estado = "pausado" if self._pausado else "corriendo"
            messagebox.showwarning(
                "Vaciar carpeta de trabajo",
                f"Hay un procesamiento {estado} sobre esta carpeta ahora mismo.\n\n"
                f"Detenelo primero (botón «Detener») y después vaciá — si no, el "
                f"proceso sigue escribiendo encima de lo que se borra.")
            return

        a_borrar: list[Path] = [self._salida / c for c in SUBCARPETAS_PIPELINE
                                if (self._salida / c).exists()]
        for patron in PATRONES_ARCHIVO_PIPELINE:
            a_borrar.extend(self._salida.glob(patron))

        if not a_borrar and not almacen.contar_recortes(self._salida):
            messagebox.showinfo("Vaciar carpeta de trabajo",
                                "No hay nada procesado por el programa en esta carpeta todavía.")
            return
        if not messagebox.askyesno(
                "Vaciar carpeta de trabajo",
                f"Esto borra lo que el programa generó en:\n{self._salida}\n\n"
                f"Fotos procesadas, aprobaciones/descartes y respaldos — de esta carga "
                f"y de cualquier carga anterior hecha en la misma carpeta.\n\n"
                f"Las fotos de ENTRADA (las que se extrajeron o elegiste procesar) "
                f"NO se tocan.\n\nNo se puede deshacer.\n\n¿Continuar?"):
            return
        errores = 0
        for item in a_borrar:
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except OSError:
                errores += 1

        try:
            almacen.vaciar(self._salida)
        except Exception:  # noqa: BLE001
            errores += 1

        self._recortes = {}
        self.tabla.delete(*self.tabla.get_children())
        self._limpiar_panel_detalle()
        self._asegurar_cuerpo_visible()
        self.v_carpeta_activa.set(f"Carpeta activa: {self._salida}  ·  sin recortes procesados todavía")
        mensaje = "Carpeta de trabajo vaciada."
        if errores:
            mensaje += f"\n\n{errores} elemento(s) no se pudieron borrar (puede que estén abiertos)."
        messagebox.showinfo("Vaciar carpeta de trabajo", mensaje)

    # ── procesar ─────────────────────────────────────────────────────────

    def _procesar(self) -> None:
        # El monitor de avance se ancla con `pack(before=self.cuerpo)`, así que
        # `self.cuerpo` TIENE que estar empaquetado antes de arrancar. Con el
        # asistente por pasos eso ya no se cumple solo (en el paso 1 la
        # pantalla visible es otra), así que se garantiza acá.
        self._mostrar_cuerpo()
        entrada_dir = self.v_entrada.get().strip()
        entrada: Path | list[Path] | None = self._entrada_archivos or (Path(entrada_dir) if entrada_dir else None)
        if not entrada:
            messagebox.showwarning("Procesar", "Elegí una carpeta o archivos de entrada.")
            return

        salida = self.v_salida.get().strip()
        if not salida:
            # No bloquear con una advertencia y listo — la carpeta de salida
            # es obligatoria pero el botón para elegirla queda ANTES en la
            # barra (se puede cargar el Excel sin haberla llenado todavía).
            # Pedirla acá mismo evita que el usuario tenga que volver atrás.
            elegida = filedialog.askdirectory(title="Elegí la carpeta de salida para continuar")
            if not elegida:
                return
            salida = elegida
            self.v_salida.set(salida)
        try:
            plan = nucleo.planificar(entrada, Path(salida), rehacer=self.v_rehacer.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Procesar", str(exc))
            return
        if not plan.pendientes:
            messagebox.showinfo("Procesar", "No hay láminas nuevas para procesar.")
            return

        self._asegurar_cuerpo_visible()

        # Se está procesando de verdad: lo que va a haber en pantalla ya no es
        # "un lote viejo tal como quedó", así que la franja de retomar y el
        # rótulo "＋ Agregar más fotos" dejan de aplicar.
        self._retomando_lote = False
        self._refrescar_aviso_retomar()

        self._salida = Path(salida)
        self._guardar_ultima_carpeta()
        # De DÓNDE salieron las fotos crudas queda anotado en la carpeta del
        # lote, igual que el proveedor. Sin esto, al retomar el lote otro día
        # `v_entrada`/`_entrada_archivos` arrancaban vacíos y tocar
        # "▶ Limpiar fotos" (por ejemplo para sumar fotos nuevas al mismo lote)
        # fallaba con "Elegí una carpeta o archivos de entrada" — aunque el
        # usuario nunca había elegido mal: el dato simplemente no se guardaba.
        # Se anota acá porque es el único punto donde el origen ya está
        # confirmado de verdad (`planificar` no reventó) y `self._salida` ya
        # apunta a la carpeta definitiva del lote.
        campos_meta = {}
        if self._entrada_archivos:
            campos_meta["entrada_archivos"] = [str(p) for p in self._entrada_archivos]
            campos_meta["entrada_dir"] = None
        elif entrada_dir:
            campos_meta["entrada_dir"] = entrada_dir
            campos_meta["entrada_archivos"] = None
        # Guardar el nombre del proyecto si está definido
        if self._nombre_proyecto:
            campos_meta["nombre_proyecto"] = self._nombre_proyecto
        if campos_meta:
            self._guardar_marca_lote(**campos_meta)
        self.v_carpeta_activa.set(f"Carpeta activa: {self._salida}  ·  procesando…")
        # H4 de la auditoría de flujo: antes SIEMPRE n_procesos=1, aunque
        # `escribir_planes`/`worker.py --id` ya estaban preparados para
        # repartir en varios -- las láminas son independientes entre sí, así
        # que es la paralelización más barata disponible. Tope en 4: cada
        # proceso carga su propia copia de los modelos de detección/matting
        # (memoria real, no gratis), y no tiene sentido pedir más procesos
        # que láminas pendientes.
        n_procesos = max(1, min(4, os.cpu_count() or 1, len(plan.pendientes)))
        planes = nucleo.escribir_planes(plan, self._salida, n_procesos=n_procesos)
        self._procs.clear()
        # Indexado por las DOS claves a propósito: el monitor en vivo busca
        # por nombre de archivo con extensión (lo que manda el worker en
        # `campos["archivo"]`), y el resumen final busca por stem (el nombre
        # de la lámina sin extensión, derivado del nombre del recorte). Con
        # una sola clave, uno de los dos siempre fallaba en silencio — y era
        # el resumen: por eso la columna de "lámina original" salía vacía.
        self._rutas_pendientes = {}
        for p, _ in plan.pendientes:
            self._rutas_pendientes[p.name] = p
            self._rutas_pendientes[p.stem] = p
        self.v_estado_proc.set(f"Procesando 0/{len(plan.pendientes)}")
        self.btn_procesar.state(["disabled"])
        self.btn_detener.state(["!disabled"])
        self.btn_pausar.state(["!disabled"])
        self.btn_pausar.configure(text="⏸ Pausar")
        self._pausado = False
        self._hechas_proc = 0
        self._total_proc = len(plan.pendientes)
        self._pasos_hechos_lamina = 0
        self._pasos_total_lamina = 3  # cargada + detectada + separadas, hasta saber cuántas piezas hay
        self._piezas_lamina_actual = 0

        # Se está limpiando: la pantalla es el paso 3 ("Limpiar y revisar"),
        # tanto si se llegó desde el paso 2 como si se tocó "＋ Agregar más
        # fotos" con el lote ya revisándose. La marca va ANTES de
        # `_actualizar_paso`: es la que decide que el monitor de avance se vea
        # (y la que lo retira sola al terminar).
        self._procesando_lote = True
        self._actualizar_paso(3)
        self.monitor.pack(side="top", fill="x", padx=10, pady=(0, 8), before=self.cuerpo)
        self.v_lamina_actual.set("Arrancando…")
        self.v_paso_actual.set("")
        self.barra_progreso.configure(value=0)
        self.v_pct.set("0%")
        self.barra_progreso_lamina.configure(value=0)
        self.v_pct_lamina.set("")
        self._limpiar_consola()
        self._reiniciar_medicion_lote()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        # Sin CREATE_NO_WINDOW, Windows le abre su propia consola visible al
        # proceso hijo porque la app corre con pythonw (sin consola propia) —
        # esa es la "ventana negra" que aparecía. La salida ya se captura por
        # el pipe, no hace falta ninguna consola.
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        # Todos los procesos empujan sus líneas ("linea") a LA MISMA cola --
        # se intercalan en pantalla (dos láminas de golpe en vez de una por
        # una), que es un efecto secundario visual aceptado a cambio de
        # aprovechar los núcleos disponibles. Lo que sí importa es no cerrar
        # el lote hasta que TODOS terminen -- `self._procs_pendientes` cuenta
        # cuántos siguen vivos; el evento "fin" ya no cierra la pantalla por
        # sí solo (ver `_bombear_cola`).
        self._procs_pendientes = len(planes)

        def lanzar_uno(id_proceso: int) -> None:
            # Ya no se le pasa la ruta de un `plan_N.jsonl`: cada worker lee su
            # tramo de la tabla `plan` del `lote.sqlite` por su `--id`, que es
            # el argumento que ya recibía. Un argumento menos que se puede
            # desincronizar del id.
            cmd = [str(nucleo.python_zawa()), str(PROYECTO / "worker.py"),
                   "--salida", str(self._salida), "--id", str(id_proceso)]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        cwd=str(PROYECTO), env=env, encoding="utf-8", errors="replace",
                                        creationflags=creationflags)
                self._procs.append(proc)
                for linea in proc.stdout:
                    self._q.put(("linea", linea.rstrip()))
                proc.wait()
                self._q.put(("fin", proc.returncode))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("linea", f"ERROR al lanzar proceso {id_proceso}: {exc}"))
                self._q.put(("fin", -1))

        for id_proceso in planes:
            threading.Thread(target=lanzar_uno, args=(id_proceso,), daemon=True).start()

    def _detener(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                # terminate() mata el proceso igual esté pausado o no — no
                # hace falta reanudarlo primero.
                p.terminate()
        self.v_estado_proc.set("Detenido")
        # Ya no corre nada: el monitor de avance se retira y la pantalla queda
        # entera para la revisión de lo que sí se alcanzó a limpiar.
        self._procesando_lote = False
        self.btn_detener.state(["disabled"])
        self.btn_pausar.state(["disabled"])
        self.btn_pausar.configure(text="⏸ Pausar")
        self._pausado = False
        self.v_lamina_actual.set("Detenido por el usuario")
        self.v_paso_actual.set("")
        self.v_eta.set("")
        self._ocultar_sin_actividad()

        # Cortar el lote NO tira lo ya procesado: cada recorte se guardó en
        # disco apenas salió. Pero la lista en pantalla solo se recargaba con
        # el evento "fin" del worker, que al matarlo nunca llega — así que
        # quedaba vacía y parecía que se había perdido todo. Se recarga acá.
        self._recargar_manifiesto()
        if self._salida:
            self.v_carpeta_activa.set(
                f"Carpeta activa: {self._salida}  ·  {len(self._recortes)} recorte(s) cargados")
        if self._recortes:
            self.v_log.set(f"Detenido: {len(self._recortes)} recorte(s) ya procesados y guardados")
            self._aterrizar_en_revision()
        else:
            self.v_log.set("Detenido antes de terminar ninguna lámina")

    def _abrir_primera_foto(self) -> None:
        """Deja la primera foto del lote abierta en el visor.

        Entrar a "Revisar" con el panel de detalle vacío obliga a un clic extra
        antes de poder empezar, y deja la barra "CORREGIR ESTA FOTO" escondida
        (solo aparece con una foto seleccionada) — con la pantalla masiva
        eliminada, ésta es la primera cosa que ve el comprador al terminar de
        limpiar, así que tiene que venir lista para trabajar.
        """
        if self._nombre_actual:
            return
        primero = next(iter(sorted(self._recortes)), None)
        if primero and self.tabla.exists(primero):
            self.tabla.selection_set(primero)

    def _aterrizar_en_revision(self) -> None:
        """Al terminar (o cortar) la limpieza, la MISMA pantalla se queda solo
        con la grilla de revisión: el monitor de avance se retira (ya no hay
        nada corriendo) y el comprador sigue decidiendo donde estaba.

        Antes esto era un cambio de paso (3 → 4) con su propia pantalla; con
        los pasos fusionados (2026-09-16) es la transición interna del paso 3,
        y no le cuesta ningún clic al usuario.

        Antes acá se abría la pantalla masiva "Elegir recortes a limpiar", que
        era un peaje: había que pasar por una grilla de casillas ANTES de poder
        ver las fotos una por una. Ofrecía las mismas dos correcciones que la
        barra "CORREGIR ESTA FOTO", así que el comprador elegía a ciegas en una
        pantalla lo que después revisaba de verdad en la otra.

        Ahora las correcciones conviven con la foto grande y las líneas de
        referencia. El caso "aplicar a varias de una pasada" sigue disponible
        ahí mismo: Ctrl/Shift+clic en la grilla levanta la barra de acciones en
        bloque.
        """
        if not self._recortes:
            self._mostrar_resumen_final()
            return
        self._asegurar_cuerpo_visible()
        self._ir_a_paso(3)
        self._abrir_primera_foto()
        self.v_log.set(
            f"{len(self._recortes)} recorte(s) listos para revisar. "
            "Marcá varias con Ctrl+clic para corregirlas de una sola vez.")

    def _pausar_reanudar(self) -> None:
        """Pausa/reanuda de verdad el proceso (via `psutil`, congela sus
        hilos), a diferencia de "Detener" que lo mata. Útil para dejar la
        computadora en algo urgente sin perder el avance ni tener que
        reprocesar nada al volver — la lámina que estaba a medias sigue
        exactamente ahí cuando se reanuda."""
        procesos_vivos = [p for p in self._procs if p.poll() is None]
        if not procesos_vivos:
            return
        try:
            if not self._pausado:
                for p in procesos_vivos:
                    psutil.Process(p.pid).suspend()
                self._pausado = True
                self.btn_pausar.configure(text="▶ Reanudar")
                self.v_estado_proc.set("Pausado")
                self.v_lamina_actual.set("Pausado por el usuario — el avance no se pierde")
            else:
                for p in procesos_vivos:
                    psutil.Process(p.pid).resume()
                self._pausado = False
                self.btn_pausar.configure(text="⏸ Pausar")
                self.v_estado_proc.set(f"Procesando {self._hechas_proc}/{self._total_proc}")
        except psutil.Error as exc:
            messagebox.showerror("Pausar/Reanudar", f"No se pudo cambiar el estado del proceso:\n{exc}")

    def _set_estado_compras(self, msg: str) -> None:
        """Escribe el avance del envío tanto en la línea propia de la tarjeta
        "Confirmar y enviar" (`v_estado_compras`, la que ve el usuario si sigue
        ahí) como en el estado general de la barra superior -- por si el
        envío sigue corriendo en segundo plano después de que el usuario
        navegó a otra pantalla ("Volver a revisar" destruye la tarjeta, pero
        el hilo de fondo sigue avisando)."""
        self.v_estado_proc.set(msg)
        v_compras = getattr(self, "v_estado_compras", None)
        if v_compras is not None:
            v_compras.set(msg)

    def _avance_compras(self, pct: float) -> None:
        """Mueve la barra propia del envío a calificar, si la tarjeta de
        "Confirmar y enviar" sigue en pantalla. No usa el monitor de láminas:
        ese habla de otra cosa y ya no se muestra en este paso."""
        barra = getattr(self, "barra_compras", None)
        if barra is None:
            return
        try:
            barra.configure(value=pct)
        except tk.TclError:
            pass

    def _mostrar_progreso_compras(self) -> None:
        """Muestra el bloque "Progreso del envío al Asistente de Compras"
        (rótulo + línea de estado + barra) justo arriba del separador de la
        tarjeta. Fuera de un envío, ese bloque no se empaca: una barra sin
        contexto no le dice nada al comprador."""
        marco = getattr(self, "_marco_progreso_compras", None)
        sep = getattr(self, "_sep_progreso_compras", None)
        if marco is None:
            return
        try:
            if not marco.winfo_manager():
                if sep is not None and sep.winfo_manager():
                    marco.pack(fill="x", pady=(8, 0), before=sep)
                else:
                    marco.pack(fill="x", pady=(8, 0))
        except tk.TclError:
            pass

    def _ocultar_progreso_compras(self) -> None:
        marco = getattr(self, "_marco_progreso_compras", None)
        if marco is None:
            return
        try:
            if marco.winfo_manager():
                marco.pack_forget()
        except tk.TclError:
            pass

    def _cambiar_btn_compras(self, habilitado: bool) -> None:
        """Habilita/deshabilita el botón que dispara el envío a compras, que
        ahora es el botón de navegación del pie ("Continuar  →" en el paso 4).
        Con seguridad: el pie puede no existir todavía durante `__init__`, y el
        usuario puede haber vuelto a "Revisar" mientras un envío anterior sigue
        corriendo en segundo plano."""
        btn = getattr(self, "btn_siguiente", None)
        if btn is None:
            return
        try:
            btn.state(["!disabled"] if habilitado else ["disabled"])
        except tk.TclError:
            pass

    def _deshabilitar_btn_compras(self) -> None:
        self._cambiar_btn_compras(False)

    def _lote_ya_enviado(self) -> bool:
        """¿Este lote ya se mandó a calificar? La constancia la deja
        `cand_fin` en la carpeta del lote (`enviado_en`)."""
        if not self._salida:
            return False
        return bool(self._leer_marca_lote(self._salida).get("enviado_en"))

    def _sincronizar_btn_compras(self) -> None:
        """Devuelve al botón de pie su rótulo de paso 4 ("Continuar  →").
        Ya no cambia de texto según el lote: el usuario pidió un único botón
        con el mismo nombre que en el resto del asistente, y quién manda el
        lote o abre los candidatos lo decide `_accion_boton_compras`. Se sigue
        llamando después de cada envío para reponer el rótulo si algo lo
        cambió mientras el envío corría ("Enviando…")."""
        btn = getattr(self, "btn_siguiente", None)
        if btn is None:
            return
        try:
            btn.configure(text=self.TEXTO_SIGUIENTE.get(4, "Continuar  →"))
        except tk.TclError:
            pass

    def _accion_boton_compras(self) -> None:
        """La acción del botón de pie en el paso 4 ("Confirmar y enviar"). Si el
        lote ya se envió, abre la vista de candidatos sin reprocesar nada; si
        no, avanza al paso 5 ("Vectorizar y comparar"), que es donde se elige
        el método y se dispara el cálculo real."""
        if self._lote_ya_enviado():
            # FASE 1 multi-proveedor: "ya enviado" es del LOTE, así que sin
            # esto el segundo proveedor nunca se podría mandar (el botón solo
            # abriría los candidatos del primero).
            pendiente = self._indice_proveedor_pendiente("enviado")
            if pendiente is not None and self._lote_multiproveedor():
                slot = self._proveedores_del_lote[pendiente]
                if messagebox.askyesno(
                        "Proveedor pendiente de calificar",
                        f"Este lote tiene el catálogo de {slot.get('proveedor_nombre')} "
                        "todavía sin enviar a calificar.\n\n"
                        "¿Enviarlo ahora? (Elegí «No» para ver los candidatos "
                        "ya calificados.)"):
                    if self._activar_slot(pendiente):
                        self._ir_a_paso(5)
                        return
            self._ver_candidatos_calificados()
            return
        self._ir_a_paso(5)

    def _enviar_a_asistente_compras(self) -> None:
        """Exporta los APROBADOS de este lote (mismo criterio que "Exportar
        aprobados...") a una carpeta aparte y automática, y esa es la que se
        manda a calificar -- NUNCA la carpeta de trabajo (`self._salida`)
        directo.

        Bug real corregido acá (H1 de la auditoría): antes se mandaba
        `self._salida`, que tiene la subcarpeta `blanco/`, pero el motor de
        candidatos busca `fondo_blanco/` (el nombre que usa la exportación,
        `decisiones.FORMATOS`) -- no coinciden, así que fallaba con
        `SystemExit` dentro del hilo, que no se atrapa, y el botón se quedaba
        trabado en "Enviando..." para siempre. Exportar primero, siempre a
        una carpeta fija fuera de `self._salida` (nunca se pregunta, para no
        repetir la pregunta de "Exportar aprobados..."), también evita que se
        envíen a calificar fotos descartadas o pendientes de revisión (H2):
        `exportar_aprobados` solo copia lo marcado `decisiones.APROBADO`.

        Import perezoso de `motor_candidatos` (y de todo lo que carga --
        torch, transformers, rembg) para no ralentizar el arranque normal de
        Limpieza de Imagenes cuando esta pantalla no se usa."""
        salida = self.v_salida.get().strip()
        if not salida or not Path(salida).is_dir():
            messagebox.showwarning("Asistente de Compras",
                                   "Elegí y procesá primero una carpeta de salida.")
            return
        if not self._recortes:
            messagebox.showwarning("Asistente de Compras",
                                   "No hay un lote cargado en la pantalla.")
            return
        # El proveedor se elige en el PASO 1 (barra superior). Se comprueba
        # ANTES de exportar: avisar después de copiar decenas de archivos
        # sería trabajo tirado a la basura.
        if self._proveedor_id_actual is None:
            # Último recurso antes de rechazar: puede estar anotado en el lote
            # (elegido en el paso 1 de esta corrida o de una anterior) y solo
            # faltar en memoria — por ejemplo si la carpeta activa se cargó por
            # un camino que no pasa por `_cargar_salida_existente`.
            self._restaurar_proveedor_del_lote(Path(salida))
        if self._proveedor_id_actual is None:
            # En vez de solo avisar y obligar al usuario a ir a buscar el botón
            # en la barra de arriba, se resuelve ACÁ MISMO: se abre el mismo
            # diálogo de elegir proveedor, y si elige uno, el envío continúa
            # sin que el usuario tenga que volver a tocar "Continuar".
            if not messagebox.askyesno(
                    "Falta el proveedor",
                    "Todavía no se eligió de qué proveedor es este catálogo.\n\n"
                    "¿Querés elegirlo ahora para continuar con el envío?"):
                return
            self._elegir_proveedor_inicial()
            if self._proveedor_id_actual is None:
                return  # canceló el diálogo de elegir proveedor

        # Punto de control del CANAL DE VENTA: sin esto, un lote que llegó acá
        # por un camino que no pasa por el paso 1 se calificaría contra el
        # catálogo genérico por default, en silencio -- justo la decisión que
        # el dueño quiere explícita. Se pregunta una vez y queda en el lote.
        if not self._asegurar_canal_venta_envio(Path(salida)):
            return

        # El método de vectorización ya se eligió en el paso 5 ("Vectorizar y
        # comparar"), que es el que dispara esta función: acá no se pregunta
        # nada. Antes había un diálogo emergente (`_preguntar_metodo_
        # vectorizacion`, eliminado 2026-09-16 por pedido del dueño): un popup
        # escondía una decisión que vale su propia pantalla, y no tenía dónde
        # mostrar el progreso real del cálculo que venía después.

        # FASE 2 multi-proveedor: el aviso "esto va a borrar al otro proveedor"
        # que la Fase 1 dejaba acá se eliminó -- ya no es cierto. Enviar el
        # segundo proveedor del lote no toca nada del primero (ver
        # `_confirmar_reemplazo_catalogo` y `_vaciar_revision`). El único aviso
        # que queda es el de más abajo, y es sobre los propios datos anteriores
        # del proveedor que se está enviando.

        destino = Path(salida).parent / f"{Path(salida).name}_para_asistente_compras"
        # Con varios proveedores en la misma carpeta de trabajo, solo se
        # exportan los recortes DE ESTE proveedor: si no, las fotos del primer
        # catálogo entrarían a `candidato_raw` con el `proveedor_id` del
        # segundo. Con un solo proveedor esto es `set(self._recortes)`, igual
        # que antes.
        nombres_envio = self._recortes_del_proveedor_activo()
        if not nombres_envio:
            messagebox.showwarning(
                "Asistente de Compras",
                "No se pudo identificar qué recortes de esta carpeta son de "
                f"{self._proveedor_nombre_actual}.\n\nRevisá el origen de las "
                "fotos de ese proveedor en el paso 1 antes de enviar (no se "
                "envía nada para no cargar fotos con el proveedor equivocado).")
            return
        try:
            res_export = decisiones.exportar_aprobados(
                Path(salida), destino,
                nombres=nombres_envio,
                formatos=("blanco",),  # el motor de candidatos solo necesita fondo_blanco
                entrada=self.v_entrada.get().strip() or None)
        except decisiones.ErrorExportacion as exc:
            messagebox.showerror("Asistente de Compras: exportación", str(exc))
            return
        except OSError as exc:
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo preparar la carpeta a enviar:\n{exc}")
            return

        if res_export["aprobados"] == 0:
            messagebox.showwarning(
                "Asistente de Compras",
                "Ningún recorte de este lote está marcado como Aprobado todavía "
                "-- no hay nada que enviar a calificar.")
            return

        try:
            import motor_candidatos
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo cargar el módulo de calificación:\n{exc}")
            return

        proveedor_id = self._proveedor_id_actual
        modelo_activo = motor_candidatos.modelo_activo_configurado()

        # FASE 5 multi-proveedor: qué archivos de la carpeta de exportados son
        # de ESTE envío. La carpeta es una sola para el lote y
        # `exportar_aprobados` no la vacía, así que las fotos del proveedor
        # anterior siguen ahí; sin esta lista, el motor las volvía a ingestar a
        # nombre del proveedor que se está enviando ahora (bug real medido en
        # la verificación de punta a punta de la Fase 5). En un lote de un solo
        # proveedor se pasa None y el camino queda idéntico al de antes.
        archivos_envio = (set(nombres_envio) if self._lote_multiproveedor()
                          else None)

        reemplazar = self._confirmar_reemplazo_catalogo(destino.name)

        # Aviso residual (FASE 2): el único caso que todavía reemplaza datos
        # ya calificados es "el MISMO proveedor ya había enviado un catálogo
        # con otro origen". Eso sigue siendo válido (reenviar con el archivo
        # corregido) pero conviene decirlo, y solo tiene sentido preguntarlo
        # cuando el lote tiene varios proveedores: en un lote de un solo
        # proveedor el comportamiento queda idéntico al de antes de esta fase.
        if reemplazar == "proveedor" and self._lote_multiproveedor():
            if not messagebox.askyesno(
                    "Reemplazar el envío anterior de este proveedor",
                    f"{self._proveedor_nombre_actual} ya tiene un catálogo "
                    "enviado en este lote, con otro origen.\n\n"
                    "Enviar ahora REEMPLAZA sus datos anteriores (los de los "
                    "demás proveedores del lote no se tocan).\n\n¿Continuar?",
                    icon="warning", default="no"):
                return

        self._enviando_compras = True
        self._deshabilitar_btn_compras()
        self._mostrar_progreso_compras()
        self._set_estado_compras(
            f"Enviando {res_export['aprobados']} aprobado(s) al Asistente de Compras "
            f"(método: {modelo_activo})…")
        self._avance_compras(0)

        def avisar(msg: str) -> None:
            self._q.put(("cand_linea", msg))

        def avanzar(hechos: int, total: int) -> None:
            self._q.put(("cand_avance", (hechos, total)))

        def trabajar() -> None:
            try:
                res = motor_candidatos.cargar_carpeta_limpia(
                    destino, proveedor_id, catalogo_origen=destino.name,
                    on_progreso=avisar, on_avance=avanzar,
                    modelo_activo=modelo_activo, reemplazar_catalogo=reemplazar,
                    solo_archivos=archivos_envio)
                self._q.put(("cand_fin", res.candidatos))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("cand_error", str(exc)))

        # Reloj de arena durante TODO el envío: corre en otro hilo (por eso no
        # se usa el `reloj(...)` de contexto, que terminaría al instante) y se
        # apaga en los manejadores de `cand_fin` / `cand_error`.
        self.configure(cursor="watch")
        threading.Thread(target=trabajar, daemon=True).start()

    # ── paso 5: vectorizar y comparar ────────────────────────────────────
    #
    # Reemplaza al diálogo emergente `_preguntar_metodo_vectorizacion`
    # (2026-09-16, pedido del dueño). Dos razones concretas, las dos reales:
    #   - la decisión del método NO se puede mezclar entre catálogos, así que
    #     merece una pantalla donde se lea qué es cada opción, no un popup que
    #     se contesta de memoria; y
    #   - el trabajo que viene después (vectorizar cada candidato + similitud
    #     de coseno contra el catálogo) son MINUTOS. El popup se cerraba y el
    #     progreso quedaba escondido en una barra de otra pantalla.
    #
    # El progreso NO se reinventa: `motor_calificacion.vectorizar_y_puntuar_
    # candidatos` ya reporta por `_reportar_progreso` (mensaje + sub/sub_total),
    # `motor_candidatos.cargar_carpeta_limpia` lo poletea y lo entrega por
    # `on_progreso`/`on_avance`, y esos van a la cola como `cand_linea`/
    # `cand_avance`. Esta pantalla solo REUSA los mismos nombres de widget que
    # ya leen esos manejadores (`v_estado_compras`, `barra_compras`,
    # `_marco_progreso_compras`), así que no hace falta tocar el plomería.

    # El orden de las opciones lo fija `MODELOS_ACTIVOS_VALIDOS`
    # (motor_candidatos); este dict solo les pone nombre legible.
    ETIQUETA_METODOS = {
        "ti": ("TI (recomendado)",
               "dinov2_ti — el espacio real del equipo de TI"),
        "fashion": ("Fashion-SigLIP",
                    "solo fashion_siglip, sin fusionar — mira estilo/moda. "
                    "Devuelve pocos comparables, pero los que devuelve son "
                    "los más parejos en color y material"),
        "dino": ("DINOv2",
                 "solo dino_v2, sin fusionar — mira forma y estructura. "
                 "Es el que más candidatos alcanza a comparar; puede proponer "
                 "el mismo molde en otro color"),
        "estandar": ("Estándar (fusión)",
                     "fashion_siglip + dino_v2 promediados 50/50. Medido con "
                     "un lote real, rinde PEOR que cada uno por separado: el "
                     "promedio de una señal fuerte y una débil queda debajo "
                     "del umbral más seguido que cualquiera de las dos"),
    }

    def _mostrar_pantalla_vectorizacion(self) -> None:
        """Paso 5: elegir el método de vectorización y calcular.

        Arriba el selector (TI preseleccionado), un botón «Calcular y
        continuar» que dispara el cálculo REAL, y debajo la barra de progreso
        con el texto de estado ("Candidato 4/18…", "Categorizando y puntuando
        el catálogo…") más la consola colapsable de detalle técnico — el mismo
        patrón visual del paso 3."""
        import motor_candidatos

        if not self._salida or not self._recortes:
            messagebox.showinfo(
                "Nada que vectorizar",
                "No hay un lote cargado en la pantalla. Volvé al paso 1 y "
                "elegí un origen de fotos.")
            self._ir_a_paso(1)
            return

        self.configure(cursor="watch")
        self.after(300, lambda: self.configure(cursor=""))
        self._asegurar_cuerpo_visible()
        self._ocultar_pantallas_base()
        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._vector_frame = marco
        self._actualizar_paso(5)

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        # Sin titulazo propio: en este paso la cabecera grande NO se compacta
        # (no hay grilla que necesite el alto), así que "Vectorizar y
        # comparar" ya está escrito arriba — repetirlo dos veces en 150px se
        # veía como un error.
        Etiqueta(interior,
                 text="Con qué espacio de vectores se van a buscar los comparables de "
                      "este catálogo. No se pueden mezclar entre catálogos, así que esta "
                      "decisión vale para el lote entero.",
                 style="SuavePanel.TLabel", justify="left", wraplength=1100).pack(
            anchor="w", fill="x", pady=(4, 12))

        # Queda marcado el método que la instalación tenga persistido, y TI
        # (el recomendado) cuando no hay ninguno o el guardado ya no es válido.
        # El comentario anterior decía "siempre TI aunque haya otra cosa
        # persistida", que NO es lo que hace el código de abajo -- se corrige el
        # comentario, no el comportamiento: pisar la elección guardada del
        # comprador en cada entrada al paso 5 sería peor.
        actual = motor_candidatos.modelo_activo_configurado()
        self._metodo_vector_previo = actual
        inicial = actual if actual in motor_candidatos.MODELOS_ACTIVOS_VALIDOS else "ti"
        self.v_metodo_vector = tk.StringVar(value=inicial)

        # Caja con fondo propio para que el selector se lea como UN bloque de
        # decisión (es lo único que el comprador tiene que elegir acá).
        caja_metodos = Marco(interior, padding=12, fg_color=PAR_PANEL_SUAVE,
                             corner_radius=RADIO_PANEL)
        caja_metodos.pack(fill="x")
        for valor in motor_candidatos.MODELOS_ACTIVOS_VALIDOS:
            titulo, detalle = self.ETIQUETA_METODOS.get(valor, (valor, ""))
            ctk.CTkRadioButton(caja_metodos, text=titulo,
                               variable=self.v_metodo_vector, value=valor,
                               font=F["base"]).pack(anchor="w", pady=(2, 0))
            if detalle:
                # `wraplength`: los detalles de los métodos nuevos son de dos
                # líneas -- sin esto el texto estira la caja a lo ancho.
                Etiqueta(caja_metodos, text=detalle, style="SuavePanel.TLabel",
                         justify="left", wraplength=1000).pack(
                    anchor="w", padx=(26, 0), pady=(0, 6))

        fila_accion = Marco(interior)
        fila_accion.pack(fill="x", pady=(12, 0))
        self.btn_calcular_vector = Boton(
            fila_accion, text="▶ Calcular y continuar", style="Primario.TButton",
            height=44, width=230, command=self._calcular_vectorizacion_y_continuar)
        self.btn_calcular_vector.pack(side="left")
        Etiqueta(fila_accion,
                 text="Calcula el vector de cada candidato y su similitud de coseno "
                      "contra el catálogo. Puede tardar varios minutos.",
                 style="SuavePanel.TLabel", wraplength=800).pack(
            side="left", padx=(12, 0), fill="x", expand=True)

        Separador(interior).pack(fill="x", pady=(14, 12))

        # Bloque de progreso: MISMOS nombres de atributo que usaba la tarjeta
        # de "Confirmar y enviar", porque `_set_estado_compras`/
        # `_avance_compras`/`_mostrar_progreso_compras` ya escriben ahí. El
        # bloque no se empaca hasta que hay un cálculo en curso: una barra
        # vacía no le dice nada al comprador.
        self._marco_progreso_compras = Marco(interior)
        Etiqueta(self._marco_progreso_compras,
                 text="Progreso del cálculo", style="Panel.TLabel").pack(anchor="w")
        self.v_estado_compras = tk.StringVar(value="")
        Etiqueta(self._marco_progreso_compras, textvariable=self.v_estado_compras,
                 style="SuavePanel.TLabel", wraplength=1100).pack(
            anchor="w", fill="x", pady=(2, 0))
        self.barra_compras = Barra(self._marco_progreso_compras,
                                   mode="determinate", maximum=100)
        self.barra_compras.pack(fill="x", pady=(4, 0))

        self._sep_progreso_compras = Separador(interior)
        self._sep_progreso_compras.pack(fill="x", pady=(12, 12))

        # Consola colapsable propia de esta pantalla (no la del monitor de
        # láminas: son dos trabajos distintos y mezclar sus logs confunde).
        self._armar_consola_vector(interior)

        # Si se vuelve a entrar al paso mientras un cálculo sigue corriendo en
        # segundo plano, el bloque tiene que reaparecer y el botón quedar
        # apagado.
        if getattr(self, "_enviando_compras", False):
            self._mostrar_progreso_compras()
            self._bloquear_btn_calcular(True)

    def _armar_consola_vector(self, padre) -> None:
        """Log crudo del cálculo, colapsado por defecto — mismo patrón
        «▸ Ver detalle técnico» del monitor de limpieza."""
        self._consola_vector_abierta = False
        fila_toggle = Marco(padre)
        fila_toggle.pack(fill="x")
        self.btn_consola_vector = Boton(fila_toggle, text="▸ Ver detalle técnico",
                                        style="Sutil.TButton", width=200, height=30,
                                        command=self._alternar_consola_vector)
        self.btn_consola_vector.pack(side="left")

        # `tk.Frame` por lo mismo que `_zona_consola`: colapsada no debe
        # reservar los 200px de un CTkFrame vacío.
        self._zona_consola_vector = tk.Frame(padre, background=COLOR_PANEL)
        self._zona_consola_vector.pack(fill="both", expand=True)
        self.txt_consola_vector = ctk.CTkTextbox(
            self._zona_consola_vector, height=170, corner_radius=RADIO_CONTROL,
            fg_color=("#12161c", "#0c0f13"), text_color=("#d7dde5", "#d7dde5"),
            border_color=PAR_BORDE, border_width=1,
            scrollbar_button_color=PAR_BORDE,
            scrollbar_button_hover_color=PAR_TEXTO_SUAVE,
            font=("Consolas", 11), activate_scrollbars=True, wrap="none")
        self.txt_consola_vector.configure(state="disabled")
        # No se empaqueta acá: arranca colapsada.

    def _alternar_consola_vector(self) -> None:
        self._consola_vector_abierta = not getattr(self, "_consola_vector_abierta", False)
        if self._consola_vector_abierta:
            if not self._zona_consola_vector.winfo_manager():
                self._zona_consola_vector.pack(fill="both", expand=True)
            self.txt_consola_vector.pack(fill="both", expand=True, pady=(6, 0))
            self.btn_consola_vector.configure(text="▾ Ocultar detalle técnico")
        else:
            self.txt_consola_vector.pack_forget()
            self._zona_consola_vector.pack_forget()   # ver `_alternar_consola`
            self.btn_consola_vector.configure(text="▸ Ver detalle técnico")

    def _log_consola_vector(self, texto: str) -> None:
        """Agrega una línea al log del paso 5, si esa pantalla está abierta.
        Silencioso si no lo está: el cálculo puede seguir corriendo en segundo
        plano después de que el usuario navegó a otra parte."""
        caja = getattr(self, "txt_consola_vector", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.insert("end", texto.rstrip("\r\n") + "\n")
            self._lineas_consola_vector = getattr(self, "_lineas_consola_vector", 0) + 1
            if self._lineas_consola_vector > self._MAX_LINEAS_CONSOLA:
                sobran = self._lineas_consola_vector - self._MAX_LINEAS_CONSOLA
                caja.delete("1.0", f"{sobran + 1}.0")
                self._lineas_consola_vector = self._MAX_LINEAS_CONSOLA
            caja.see("end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _bloquear_btn_calcular(self, bloqueado: bool) -> None:
        btn = getattr(self, "btn_calcular_vector", None)
        if btn is None:
            return
        try:
            btn.state(["disabled"] if bloqueado else ["!disabled"])
        except tk.TclError:
            pass

    def _calcular_vectorizacion_y_continuar(self) -> None:
        """Guarda el método elegido y dispara el cálculo real.

        El cálculo es el mismo de siempre (`_enviar_a_asistente_compras` →
        `motor_candidatos.cargar_carpeta_limpia` →
        `vectorizar_y_puntuar_candidatos`), corriendo en su hilo y reportando
        por la cola; al terminar, `cand_fin` abre solo el paso 6 ("Candidatos
        calificados")."""
        import motor_candidatos

        if getattr(self, "_enviando_compras", False):
            messagebox.showinfo(
                "Ya está calculando",
                "El cálculo de vectores y similitud ya está corriendo. "
                "Al terminar se abren los candidatos calificados solos.")
            return

        elegido = getattr(self, "v_metodo_vector", None)
        elegido = elegido.get() if elegido is not None else "ti"
        previo = getattr(self, "_metodo_vector_previo", None)
        if elegido != previo:
            # Se persiste solo si cambió: `establecer_modelo_activo` escribe el
            # config de la instalación, y el resto del cálculo lee de ahí
            # (`modelo_activo_configurado`).
            try:
                motor_candidatos.establecer_modelo_activo(elegido)
                self._metodo_vector_previo = elegido
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Método de vectorización",
                                     f"No se pudo guardar el método:\n{exc}")
                return

        self._limpiar_consola_vector()
        self._bloquear_btn_calcular(True)
        self._set_estado_compras("Preparando el cálculo…")
        self._mostrar_progreso_compras()
        self._avance_compras(0)
        # `_enviar_a_asistente_compras` valida (proveedor, canal, aprobados),
        # exporta y lanza el hilo. Si rechaza algo y no arranca nada, el botón
        # tiene que volver a estar disponible.
        self._enviar_a_asistente_compras()
        if not getattr(self, "_enviando_compras", False):
            self._bloquear_btn_calcular(False)
            self._ocultar_progreso_compras()

    def _limpiar_consola_vector(self) -> None:
        caja = getattr(self, "txt_consola_vector", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.delete("1.0", "end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass
        self._lineas_consola_vector = 0

    def _confirmar_reemplazo_catalogo(self, catalogo_origen: str) -> bool | str:
        """¿Hay que vaciar la revisión DE ESTE LOTE antes de este envío?

        Decide sola, sin preguntarle nada al usuario.

        FASE 4 -- qué ya NO decide esto. Cuando la revisión vivía en
        `staging_tuc` (Postgres compartido con la herramienta web), esta
        función era el guardián de la regla "un solo catálogo activo a la
        vez": lo que encontraba cargado podía ser el catálogo de OTRO lote, y
        había que truncar la tabla global para que no se mezclaran (así se
        mezclaron 20 Converse de prueba con 54 Reebok reales el 2026-09-07).
        Esa regla murió con la Fase 2: cada lote tiene su propio
        `lote.sqlite`, `resumen_catalogo_activo()` solo puede devolver lo de
        ESTE lote, y abrir el catálogo de otro proveedor en otro lote no toca
        nada de acá.

        Lo que SÍ sigue decidiendo, y por eso la función no se borró: dentro
        de un mismo lote todavía hay dos envíos distintos posibles.
          - Revisión vacía -> nada que vaciar (True, camino limpio).
          - Lo que hay es EXACTAMENTE este proveedor + este catálogo -> es un
            reenvío del mismo lote (corregir 1-2 fotos): se agrega/actualiza
            por sha1 sin perder el resto (False).
          - Proveedor distinto al de las filas ya cargadas -> el comprador
            corrigió a mano de quién es este lote (`_elegir_proveedor_inicial`
            reescribe la marca del lote). Las filas viejas quedaron a nombre
            del proveedor equivocado y hay que vaciarlas (True).

        Ya no devuelve None: ese valor existía para "el usuario canceló el
        diálogo", y el diálogo modal "¿Agregar o empezar nuevo?" se eliminó el
        2026-09-09 a pedido del dueño ("no quiero que me pregunte eso, cada
        lote es independiente").

        FASE 2 multi-proveedor (2026-09-16) -- la decisión pasó de ser "todo o
        nada del lote" a ser POR PROVEEDOR. Antes bastaba con que las filas
        cargadas fueran de otro proveedor para devolver "vaciar", y ese vaciado
        no tenía `WHERE`: enviar el segundo proveedor del lote borraba la
        calificación ya hecha del primero (bloqueante confirmado en la Fase 1).
        Ahora devuelve:
          - False -> no se vacía nada (reenvío del mismo proveedor+origen, o
            solo hay filas de OTROS proveedores, que deben convivir).
          - "proveedor" -> este MISMO proveedor ya tiene filas en el lote con
            otro `catalogo_origen`: se reemplaza lo suyo y nada más.
          - True -> vaciado total del lote. Queda solo para el caso de un lote
            de un único proveedor cuyas filas quedaron a nombre del proveedor
            equivocado (el comprador corrigió a mano de quién es el lote), y
            para el lote todavía vacío."""
        import motor_candidatos
        resumen = motor_candidatos.resumen_catalogo_activo()
        if not resumen:
            return True
        pid = self._proveedor_id_actual
        mias = [f for f in resumen
                if f.get("proveedor_id") == pid
                or f["proveedor"] == self._proveedor_nombre_actual]
        if mias:
            # ¿Ya hay datos de ESTE proveedor con un origen distinto al que se
            # está por enviar? Ahí sí hay que reemplazar lo suyo.
            if any(f["catalogo_origen"] != catalogo_origen for f in mias):
                return "proveedor"
            return False
        # No hay nada de este proveedor. Si el lote es multi-proveedor, lo que
        # hay es legítimo de otro proveedor y debe quedar intacto.
        if self._lote_multiproveedor():
            return False
        return True

    def _elegir_proveedor(self, proveedores: list[tuple[int, str]]) -> tuple[int, str] | None:
        """Diálogo simple de selección -- lista real de `silver.dim_tuc_proveedor`,
        no un número de memoria. Devuelve (id, nombre).

        El método de vectorización YA NO se elige acá (plan 2026-09-07, paso
        8): es una configuración de la instalación, no una decisión por
        catálogo, y vive en ⚙ Ajustes (`_abrir_ajustes`).

        Permite agregar un proveedor nuevo si no está en la lista: opción
        "OTROS - Especificar nuevo" al final."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Elegí el proveedor")
        _centrar_en_ventana_principal(self, ventana, 360, 480)
        Etiqueta(ventana, text="¿De qué proveedor es este catálogo?",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 6))
        lista = tk.Listbox(ventana, activestyle="none")
        lista.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # Agregar proveedores existentes
        for _pid, nombre in proveedores:
            lista.insert("end", nombre)

        # Agregar opción para nuevo proveedor
        lista.insert("end", "— OTROS — Especificar nuevo")

        resultado: dict = {"id": None, "nombre": None}

        def confirmar() -> None:
            sel = lista.curselection()
            if sel:
                idx = sel[0]
                # Verificar si se seleccionó la opción "OTROS"
                if idx == len(proveedores):  # La opción OTROS está al final
                    # Pedir nombre del nuevo proveedor
                    nuevo_proveedor = self._solicitar_nuevo_proveedor()
                    if nuevo_proveedor is not None:
                        resultado["id"], resultado["nombre"] = nuevo_proveedor
                else:
                    resultado["id"] = proveedores[idx][0]
                    resultado["nombre"] = proveedores[idx][1]
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Cancelar", style="Sutil.TButton",
             command=ventana.destroy).pack(side="right")
        Boton(fila_botones, text="Confirmar", style="Primario.TButton",
             command=confirmar).pack(side="right", padx=(0, 8))
        lista.bind("<Double-Button-1>", lambda _e: confirmar())

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)
        if resultado["id"] is None:
            return None
        return resultado["id"], resultado["nombre"]

    def _solicitar_nuevo_proveedor(self) -> tuple[int, str] | None:
        """Diálogo para que el usuario especifique un nuevo proveedor. Inserta
        en la base de datos y retorna (proveedor_id, nombre).

        Si el usuario cancela o hay error, retorna None."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Nuevo proveedor")
        _centrar_en_ventana_principal(self, ventana, 380, 180)
        Etiqueta(ventana, text="Nombre del nuevo proveedor",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 4))
        Etiqueta(ventana, text="Escribí el nombre exacto del proveedor tal como lo vas a usar.",
                style="Suave.TLabel", wraplength=350).pack(anchor="w", padx=14, pady=(0, 10))

        entry_nombre = ctk.CTkEntry(ventana, corner_radius=RADIO_CONTROL, height=40,
                                    font=F["base"], fg_color=PAR_PANEL_SUAVE,
                                    border_color=PAR_BORDE, text_color=PAR_TEXTO,
                                    placeholder_text="Ej: Nike, Adidas, etc.")
        entry_nombre.pack(fill="x", padx=14, pady=(0, 14))
        entry_nombre.focus()

        resultado: dict = {"id": None, "nombre": None, "cancelado": False}

        def guardar() -> None:
            nombre = entry_nombre.get().strip()
            if not nombre:
                messagebox.showwarning("Nuevo proveedor", "Por favor escribí el nombre del proveedor.")
                return

            # Intentar insertar en la base de datos
            try:
                import motor_calificacion  # FASE 1: antes era `servidor_pty` (GestionTUC);
                # misma función `conn()`, copiada letra por letra en la copia propia.
                c = motor_calificacion.conn()
                cur = c.cursor()

                # Insertar el nuevo proveedor
                cur.execute(
                    "INSERT INTO silver.dim_tuc_proveedor (nombre) VALUES (%s) RETURNING proveedor_id",
                    (nombre,)
                )
                proveedor_id = cur.fetchone()[0]
                c.commit()
                c.close()

                resultado["id"] = proveedor_id
                resultado["nombre"] = nombre
                ventana.destroy()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    "Error al guardar",
                    f"No se pudo agregar el proveedor a la base de datos:\n{exc}")

        def cancelar() -> None:
            resultado["cancelado"] = True
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Cancelar", style="Sutil.TButton",
             command=cancelar).pack(side="right")
        Boton(fila_botones, text="Guardar", style="Primario.TButton",
             command=guardar).pack(side="right", padx=(0, 8))

        entry_nombre.bind("<Return>", lambda _e: guardar())

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)

        if resultado["cancelado"] or resultado["id"] is None:
            return None
        return resultado["id"], resultado["nombre"]

    def _bombear_cola(self) -> None:
        try:
            while True:
                tipo, dato = self._q.get_nowait()
                # Cualquier mensaje de cualquier worker cuenta como señal de
                # vida: es contra esta marca que se mide el "parece colgado".
                self._ultima_actividad = time.monotonic()
                if tipo == "linea":
                    # El texto crudo se guarda ANTES de interpretarlo: si el
                    # formato cambia o llega algo inesperado, en el log queda
                    # igual (es justo el caso en que sirve de verdad).
                    self._log_consola(dato)
                    hechas_antes = getattr(self, "_hechas_proc", 0)
                    self._procesar_linea(dato)
                    if getattr(self, "_hechas_proc", 0) > hechas_antes:
                        # Se terminó una lámina: se anota CUÁNDO, que es lo
                        # que alimenta el promedio real de segundos/lámina.
                        self._marcas_lamina.append(time.monotonic())
                    self._refrescar_eta()
                elif tipo == "cand_linea":
                    self._set_estado_compras(dato)
                    # …y al detalle técnico del paso 5, que es donde el
                    # comprador puede mirar el log crudo del cálculo si algo
                    # parece colgado (silencioso si esa pantalla no está).
                    self._log_consola_vector(dato)
                elif tipo == "cand_avance":
                    hechos, total = dato
                    pct = (hechos / total * 100) if total else 0
                    # El avance del envío a calificar va en SU propia barra, la
                    # de la tarjeta "Confirmar y enviar": el monitor de láminas
                    # ya no se muestra en ese paso (y su bloque "Lámina actual"
                    # no aplica a candidatos).
                    self._avance_compras(pct)
                elif tipo == "cand_fin":
                    self.configure(cursor="")
                    # Constancia de que ESTE lote ya se envió: es lo que
                    # después deja que al retomar la carpeta la app salte
                    # directo al paso 4 sin reprocesar ni reenviar nada.
                    if self._salida:
                        self._guardar_marca_lote(
                            catalogo_origen=f"{Path(self._salida).name}_para_asistente_compras",
                            enviado_en=datetime.now().isoformat(timespec="seconds"),
                            enviados=len(dato))
                    # FASE 1 multi-proveedor: queda anotado QUÉ proveedor del
                    # lote ya se envió, para poder ofrecer el siguiente (y para
                    # que el aviso de la Fase 2 sepa a quién se le borraría la
                    # calificación).
                    if self._proveedores_del_lote:
                        self._slot_actual()["enviado"] = True
                        self._persistir_proveedores_del_lote()
                        self._refrescar_resumen_proveedores()
                    self._cambiar_btn_compras(True)
                    self._sincronizar_btn_compras()
                    self._set_estado_compras(f"Listo -- {len(dato)} candidato(s) calificado(s).")
                    self._avance_compras(100)
                    self._enviando_compras = False
                    self._ocultar_progreso_compras()
                    # FASE 5: la pantalla 6 se abre con los candidatos de TODO
                    # el lote, no solo con los de este envío. `dato` es lo que
                    # devolvió `cargar_carpeta_limpia`, que está filtrado al
                    # proveedor que se acaba de enviar -- abrir con eso dejaba
                    # al proveedor anterior fuera de la pantalla (y con él, la
                    # pastilla de proveedor, el filtro y el desglose del paso
                    # 7). Con un solo proveedor las dos listas son la misma;
                    # si por lo que sea no se puede identificar el lote, se
                    # cae a `dato`, que es el comportamiento anterior.
                    try:
                        del_lote = self._candidatos_del_lote()
                    except Exception:  # noqa: BLE001
                        del_lote = None
                    self._mostrar_vista_candidatos(del_lote or dato)
                elif tipo == "cand_error":
                    self.configure(cursor="")
                    self._cambiar_btn_compras(True)
                    self._enviando_compras = False
                    self._ocultar_progreso_compras()
                    self._set_estado_compras("Listo")
                    messagebox.showerror("Asistente de Compras", dato)
                elif tipo == "fin":
                    # Con varios procesos en paralelo (H4), cada uno manda su
                    # propio "fin" al terminar -- cerrar el lote con el
                    # PRIMERO dejaría a los demás corriendo en segundo plano
                    # mientras la pantalla ya pasó a la revisión, con
                    # recortes apareciendo después de que el usuario ya
                    # decidió sobre lo que vio. Solo se cierra cuando el
                    # ÚLTIMO termina.
                    self._procs_pendientes = max(0, getattr(self, "_procs_pendientes", 1) - 1)
                    if self._procs_pendientes > 0:
                        self._recargar_manifiesto()
                        continue
                    self.btn_procesar.state(["!disabled"])
                    self.btn_detener.state(["disabled"])
                    self.btn_pausar.state(["disabled"])
                    self.btn_pausar.configure(text="⏸ Pausar")
                    self._pausado = False
                    # Terminó el lote: se apaga la marca ANTES de
                    # `_aterrizar_en_revision`, que es lo que hace que el
                    # monitor se retire y el paso 3 quede solo en modo
                    # revisión (la transición automática 3→4 de antes).
                    self._procesando_lote = False
                    self.v_estado_proc.set("Listo")
                    self.v_lamina_actual.set("Listo — sin procesos activos")
                    self.v_paso_actual.set("")
                    self.barra_progreso.configure(value=100)
                    self.v_pct.set("100%")
                    self.barra_progreso_lamina.configure(value=100)
                    self.v_pct_lamina.set("")
                    self.v_eta.set("")
                    self._ocultar_sin_actividad()
                    self._recargar_manifiesto()
                    if self._salida:
                        self.v_carpeta_activa.set(
                            f"Carpeta activa: {self._salida}  ·  {len(self._recortes)} recorte(s) cargados")
                    self._aterrizar_en_revision()
        except queue.Empty:
            pass
        self._revisar_sin_actividad()
        self.after(150, self._bombear_cola)

    # ── estimación de tiempo y detección de "parece colgado" ─────────────

    SEGUNDOS_SIN_ACTIVIDAD = 90

    def _reiniciar_medicion_lote(self) -> None:
        """Pone en cero las mediciones de tiempo del lote: se llama al arrancar
        a procesar, si no el promedio arrastraría el lote anterior."""
        self._arranque_lote = time.monotonic()
        self._ultima_actividad = time.monotonic()
        self._marcas_lamina = []
        self.v_eta.set("Tiempo estimado restante: calculando…")
        self._ocultar_sin_actividad()

    def _refrescar_eta(self) -> None:
        marcas = getattr(self, "_marcas_lamina", None) or []
        total = getattr(self, "_total_proc", 0)
        hechas = getattr(self, "_hechas_proc", 0)
        faltan = max(total - hechas, 0)
        if not marcas:
            self.v_eta.set("Tiempo estimado restante: calculando…")
            return
        if faltan == 0:
            self.v_eta.set("Tiempo estimado restante: terminando…")
            return
        arranque = getattr(self, "_arranque_lote", None) or marcas[0]
        seg_por_lamina = (marcas[-1] - arranque) / len(marcas)
        restante = seg_por_lamina * faltan
        if restante < 60:
            self.v_eta.set("Tiempo estimado restante: menos de 1 min")
        else:
            self.v_eta.set(f"Tiempo estimado restante: ~{restante / 60:.0f} min")

    def _ocultar_sin_actividad(self) -> None:
        aviso = getattr(self, "_aviso_sin_actividad", None)
        if aviso is not None and aviso.winfo_ismapped():
            aviso.pack_forget()

    def _revisar_sin_actividad(self) -> None:
        """Avisa si hace rato que ningún proceso dice nada. NO detiene nada:
        cortar el lote sigue siendo decisión del usuario (botón "Detener")."""
        aviso = getattr(self, "_aviso_sin_actividad", None)
        if aviso is None:
            return
        # Solo tiene sentido mientras hay procesos vivos y sin pausa: pausado
        # el silencio es esperado, y sin lote no hay nada que vigilar.
        activo = (any(p.poll() is None for p in getattr(self, "_procs", []))
                  and not getattr(self, "_pausado", False))
        if not activo or getattr(self, "_ultima_actividad", None) is None:
            self._ocultar_sin_actividad()
            return
        quieto = time.monotonic() - self._ultima_actividad
        if quieto < self.SEGUNDOS_SIN_ACTIVIDAD:
            self._ocultar_sin_actividad()
            return
        cuanto = (f"{quieto:.0f} segundos" if quieto < 120
                  else f"{quieto / 60:.0f} minutos")
        self.v_sin_actividad.set(
            f"⚠  Hace {cuanto} que no hay avance. El programa sigue "
            "trabajando, pero puede que se haya trabado con esta foto. "
            "Si sigue igual, podés usar “Detener” — lo ya procesado no se pierde.")
        if not aviso.winfo_ismapped():
            aviso.pack(fill="x", pady=(8, 0))

    def _mostrar_imagen_flotante(self, path: Path, titulo: str) -> None:
        """Abre la foto en una ventana propia, en tamaño real (o achicada
        SOLO si no entra en la pantalla).

        Causa raíz de "el clic en la foto original del resumen del lote no hace
        nada visible" (reportado dos veces): `_mostrar_grande` pinta la imagen
        adentro de `self.lbl_imagen`, que vive en el visor del paso de limpiar
        y revisar (`self._marco_visor`). Cuando la pantalla activa es el
        resumen ("Confirmar y enviar", `self._resumen_frame`, que REEMPLAZA a
        `self.cuerpo`), ese visor sigue existiendo pero está tapado/oculto —
        el clic sí disparaba la función, pero actualizaba una imagen que no
        se veía en ningún lado. Una ventana propia funciona sin importar qué
        pantalla del asistente esté activa."""
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(titulo)
        ventana.configure(fg_color=PAR_VISOR_BG)

        # Tamaño real de la foto, pero sin pasarse de lo que entra en la
        # pantalla (con margen para no taparse con la barra de tareas).
        max_ancho = self.winfo_screenwidth() - 120
        max_alto = self.winfo_screenheight() - 160
        mostrar = im.copy()
        mostrar.thumbnail((max(max_ancho, 200), max(max_alto, 200)))
        foto = ImageTk.PhotoImage(mostrar)

        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta y la imagen desaparece
        lbl.pack(padx=10, pady=(10, 4))

        etiqueta_tam = (f"{im.width} × {im.height} px"
                        if mostrar.size == im.size else
                        f"{im.width} × {im.height} px (mostrada más chica para entrar en pantalla)")
        Etiqueta(ventana, text=etiqueta_tam, style="Suave.TLabel").pack(pady=(0, 10))

        ventana.transient(self)
        # Causa raíz de "la ventana de la foto se abre y se cierra sola" (paso 5,
        # reportado 2026-09-10): NO se cerraba -- quedaba viva y mapeada, pero
        # DETRÁS de la ventana principal, que abre maximizada y la tapa por
        # completo, así que se veía un instante y desaparecía.
        #
        # `lift()` + `focus_force()` acá no alcanzan porque corren ANTES de que
        # termine de propagarse el mismo clic que abrió la ventana:
        # `CTkToplevel.__init__` instala un `bind_all("<Button-1>", set_focus)`
        # (binding global, en el bindtag "all", que se dispara DESPUÉS del
        # binding de la miniatura), y ese handler hace `focus_set()` sobre el
        # widget clicado -- la miniatura de la ventana principal. Resultado: el
        # foco vuelve a la principal, y en Windows eso la trae al frente encima
        # de la recién creada. Medido en repro: con clic el foco termina en la
        # raíz; sin clic se queda en la ventana nueva.
        #
        # `_traer_al_frente` (topmost ON/OFF diferido) es el patrón que ya se usa
        # en el resto del archivo para exactamente este problema de apilamiento
        # en Windows, y corre después de que el clic terminó de propagarse.
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    @staticmethod
    def _ajustar_a_espacio(im: Image.Image, espacio: tuple[int, int]) -> Image.Image:
        """Escala la imagen para OCUPAR el espacio disponible, agrandando si
        hace falta -- no solo achicando.

        Causa raíz real de "el recorte se ve muy pequeño" (reportada muchas
        veces, sobrevivió a maximizar la ventana y a achicar todas las
        franjas alrededor): esto usaba `Image.thumbnail()`, que por diseño
        de PIL SOLO reduce -- nunca agranda una imagen más allá de su tamaño
        original. Si el recorte de origen es una foto de resolución modesta
        (algunos cientos de píxeles) y el visor mide mucho más que eso
        (sobre todo ahora que la ventana abre maximizada), `thumbnail()`
        dejaba la foto en su tamaño nativo chico y todo el espacio ganado
        alrededor quedaba vacío -- ningún ajuste de columnas o franjas podía
        arreglar eso, el límite estaba en esta función.

        Se agranda con Lanczos (mejor calidad que el vecino más cercano) y
        se limita a 4x el tamaño original para no llegar a verse pixelado
        cuando el recorte de origen es muy chico."""
        ancho_e, alto_e = espacio
        if im.width <= 0 or im.height <= 0:
            return im
        escala = min(ancho_e / im.width, alto_e / im.height)
        escala = min(escala, 4.0)  # tope: no pixelar de más un recorte muy chico
        nuevo = (max(1, round(im.width * escala)), max(1, round(im.height * escala)))
        if nuevo == (im.width, im.height):
            return im
        return im.resize(nuevo, Image.LANCZOS)

    def _mostrar_grande(self, path: Path, titulo: str) -> None:
        """Muestra una imagen en el visor central, agrandándola si hace
        falta para ocupar el espacio disponible (ver `_ajustar_a_espacio`)
        -- se usa EN VIVO durante el procesamiento, para que cada etapa
        (original, detección, recorte, limpieza) se vea grande de verdad."""
        try:
            im = Image.open(path).convert("RGB")
        except Exception:  # noqa: BLE001
            return
        im = self._ajustar_a_espacio(im, self._espacio_visor())
        self._foto_visor_vivo = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_visor_vivo)
        self.v_paso_proceso.set(titulo)
        self.update_idletasks()  # forzar repintado YA, no esperar al próximo ciclo ocioso

    def _mostrar_imagen_pil(self, im: Image.Image, titulo: str) -> None:
        im = self._ajustar_a_espacio(im.copy(), self._espacio_visor())  # ver nota en `_mostrar_grande`
        self._foto_visor_vivo = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_visor_vivo)
        self.v_paso_proceso.set(titulo)
        self.update_idletasks()  # forzar repintado YA, no esperar al próximo ciclo ocioso

    # descripciones en lenguaje de negocio, no técnico
    _DESCRIPCION_PASO = {
        "cargada": "Cargando la lámina",
        "detectada": "Ubicando los pares de calzado en la foto",
        "separadas": "Separando cada calzado",
        "limpiando": "Quitando el fondo",
        "recorte": "Recorte listo",
        "lista": "Lámina lista",
        "sombra_corregida": "Corrigiendo sombra pegada al calzado",
        "excedente_recortado": "Recortando restos de fondo pegados al calzado",
        "suela_completada": "Completando la suela mordida por otro par",
        "invalidado": "Volviendo a procesar (había un resultado anterior)",
    }

    def _procesar_linea(self, linea: str) -> None:
        if not linea.startswith("[LI] "):
            return
        try:
            campos = json.loads(linea[5:])
        except json.JSONDecodeError:
            return
        evento = campos.get("evento")

        if evento == "empieza":
            nombre_archivo = campos.get("archivo", "")
            self.v_lamina_actual.set(
                f'Lámina {campos.get("n")} de {campos.get("total")}: {nombre_archivo}')
            self._pasos_hechos_lamina = 0
            self._pasos_total_lamina = 3  # cargada + detectada + separadas, hasta saber piezas
            self._piezas_lamina_actual = 0
            self._actualizar_progreso()
            ruta = self._rutas_pendientes.get(nombre_archivo)
            if ruta and ruta.exists():
                self._mostrar_grande(ruta, f"Foto original: {nombre_archivo}")

        elif evento == "paso":
            paso = campos.get("paso", "")
            desc = self._DESCRIPCION_PASO.get(paso, paso)
            extra = ""
            if paso in ("limpiando", "recorte") and campos.get("de"):
                extra = f' (calzado {campos.get("i")} de {campos.get("de")})'
            recorte_nombre = campos.get("recorte") or campos.get("archivo")
            if recorte_nombre and paso not in ("limpiando", "recorte"):
                extra = f' ({Path(str(recorte_nombre)).stem})'

            # "separadas" es el primer evento que dice cuántas piezas hay en
            # esta lámina — recién ahí se sabe cuántos pasos faltan de verdad.
            if paso == "separadas" and campos.get("piezas"):
                piezas = campos["piezas"]
                self._piezas_lamina_actual = piezas
                self._pasos_total_lamina = 3 + piezas * 2 + 1

            if paso in self._DESCRIPCION_PASO:
                self._pasos_hechos_lamina += 1

            # Número de paso visible: el que se acaba de anunciar (1-indexado),
            # topado al total conocido hasta el momento.
            paso_num = min(self._pasos_hechos_lamina, self._pasos_total_lamina) if self._pasos_total_lamina else 0
            self.v_paso_actual.set(
                f"Paso {paso_num} de {self._pasos_total_lamina}: {desc}{extra}")
            self.v_estado_proc.set(
                f'Lámina {campos.get("lamina", "?")} de {self._total_proc}: {desc}{extra}')

            self._actualizar_progreso()

            if paso == "detectada" and campos.get("overlay"):
                ruta_overlay = Path(campos["overlay"])
                if ruta_overlay.exists():
                    self._mostrar_grande(ruta_overlay, "Ubicando los pares de calzado")

            if paso == "recorte" and campos.get("archivo"):
                ruta_recorte = Path(campos["archivo"])
                if ruta_recorte.exists():
                    # En vivo: el recorte CON las líneas azul/roja ya puestas,
                    # no la foto plana — así se ve de inmediato si algo falta.
                    try:
                        im_lineas = reparar.dibujar_contorno(ruta_recorte)
                        self._mostrar_imagen_pil(im_lineas, f"Recorte{extra} — línea azul: borde "
                                                             f"real, línea roja: curva esperada")
                    except Exception:  # noqa: BLE001
                        self._mostrar_grande(ruta_recorte, f"Recorte{extra}")

            if paso == "excedente_recortado" and campos.get("recorte"):
                self._mostrar_recorte_por_stem(campos["recorte"],
                                               f"Recortando restos de fondo pegados al calzado "
                                               f"({campos.get('px', 0)}px)")

            if paso == "suela_completada" and campos.get("recorte"):
                self._mostrar_recorte_por_stem(campos["recorte"],
                                               f"Suela completada ({campos.get('px', 0)}px rellenados)")

        elif evento == "foto":
            self._hechas_proc += 1
            self._pasos_hechos_lamina = self._pasos_total_lamina
            self._actualizar_progreso()
            self.v_estado_proc.set(f"Procesando {self._hechas_proc} de {self._total_proc} láminas")
            self._recargar_manifiesto()

    def _mostrar_recorte_por_stem(self, stem: str, titulo: str) -> None:
        """Busca el png actual de un recorte (por su nombre, sin extensión) en
        la carpeta de salida y lo muestra en vivo — se usa para los eventos de
        limpieza automática (excedente_recortado, suela_completada), que
        llegan DESPUÉS de que el recorte ya se guardó a disco."""
        if not self._salida:
            return
        directo = self._salida / "transparente" / f"{stem}.png"
        candidatos = [directo] if directo.exists() else list(self._salida.glob(f"**/{stem}.png"))
        if not candidatos:
            return
        try:
            im = reparar.dibujar_contorno(candidatos[0])
            self._mostrar_imagen_pil(im, titulo)
        except Exception:  # noqa: BLE001
            self._mostrar_grande(candidatos[0], titulo)

    def _actualizar_progreso(self) -> None:
        """% real: láminas ya terminadas + avance de pasos dentro de la
        lámina actual, sobre el total de láminas del lote — no una animación
        genérica que se mueve sola sin decir cuánto falta de verdad."""
        fraccion_lamina = (self._pasos_hechos_lamina / self._pasos_total_lamina
                            if self._pasos_total_lamina else 0.0)
        fraccion_lamina = min(fraccion_lamina, 1.0)
        total = max(self._total_proc, 1)
        pct = 100.0 * (self._hechas_proc + fraccion_lamina) / total
        pct = min(pct, 100.0)
        self.barra_progreso.configure(value=pct)
        self.v_pct.set(f"{pct:.0f}%")

        pct_lamina = 100.0 * fraccion_lamina
        self.barra_progreso_lamina.configure(value=pct_lamina)
        if self._pasos_total_lamina:
            self.v_pct_lamina.set(
                f"{min(self._pasos_hechos_lamina, self._pasos_total_lamina)}/{self._pasos_total_lamina}")
        else:
            self.v_pct_lamina.set("")

    # ── lista de recortes, leída del manifiesto ─────────────────────────

    def _corregir_rutas_movidas(self, r: dict) -> None:
        """El manifiesto guarda rutas ABSOLUTAS (`r["png"]`, `r["jpg"]`) tal
        como eran al procesar. Si esa carpeta de trabajo se mueve o renombra
        después (pasó de verdad: se movió `_pasos/blanco/transparente/...`
        de `Imagenes finales` a `_trabajo` para separar caché de entregable),
        las rutas viejas dejan de existir aunque el archivo siga ahí, sano,
        con otro padre. Se corrige buscando el mismo nombre de archivo
        dentro de `transparente/`/`blanco/` de la carpeta de trabajo ACTUAL
        — sin esto, un recorte "desaparece" de la lista con el archivo
        intacto en el disco."""
        if not self._salida:
            return
        for clave, sub in (("png", "transparente"), ("jpg", "blanco")):
            ruta = r.get(clave)
            if not ruta or Path(ruta).exists():
                continue
            candidata = self._salida / sub / Path(ruta).name
            if candidata.exists():
                r[clave] = str(candidata)

    def _recargar_manifiesto(self) -> None:
        if not self._salida:
            return
        # Siempre se reconstruye desde cero a partir de LO QUE HAY AHORA en
        # `self._salida` — sin este reset, cambiar de carpeta de trabajo
        # dejaba mezclados los recortes de la carpeta anterior con los de
        # la nueva (mismo síntoma que el "cuadro de recortes" sin limpiar).
        # Los tres criterios que antes estaban acá a mano —solo recortes con
        # imagen y estado medido; sin los eliminados del disco (su archivo ya
        # no está, ver `_desechar_definitivamente`, pero el registro de la
        # lámina sigue porque tiene hermanos buenos); y la decisión VIGENTE de
        # la bitácora en vez de la que quedó congelada al procesar— ahora son
        # el `WHERE` de `almacen.cargar_recortes`.
        #
        # Ese último cruce importa y por eso está en el `WHERE` y no acá: el
        # `estado.decision` que se guardó al procesar la lámina (ver
        # `estado.calcular_estado_grupo`) casi siempre dice "pendiente", y sin
        # el cruce con la bitácora un lote enteramente aprobado volvía a
        # cargarse como "20 pendientes" al reabrir la app — lo que hacía que
        # "Confirmar y enviar" mostrara de nuevo las fotos descartadas y que la
        # detección de avance mandara al paso 2 un lote que ya estaba entero.
        try:
            self._recortes = almacen.cargar_recortes(self._salida)
        except Exception as exc:  # noqa: BLE001
            self._recortes = {}
            self.v_log.set(f"No se pudo leer el lote de esta carpeta: {exc}")
        for r in self._recortes.values():
            self._corregir_rutas_movidas(r)

        seleccion_previa = self._nombre_actual

        # Actualización DIFERENCIAL, no borrar-todo-y-recrear-todo.
        #
        # Bug real corregido (2026-09-03): antes se llamaba `delete(*get_
        # children())` + reinsertar TODO en cada llamada -- y esta función
        # se llama en cada foto nueva mientras el lote está procesando. Cada
        # `insert()` reencola la miniatura (que corre `reparar.dibujar_
        # contorno`, un ajuste de curvas real, no gratis) -- con eso, ya
        # procesada la miniatura de la foto 1 se recalculaba de nuevo al
        # llegar la foto 2, la 3, ..., trabajo O(n²) sobre el lote completo.
        # Efecto colateral: `delete()` también vacía la selección múltiple
        # (`_lote`) en cada recarga, así que una marca de varias fotos no
        # sobrevivía a que llegara una foto nueva del mismo lote.
        #
        # Ahora: solo se borran las tarjetas que genuinamente desaparecieron
        # (se cambió de carpeta, se desechó una foto), solo se insertan las
        # que son nuevas de verdad (dispara su única carga de miniatura), y
        # las que ya existían se actualizan en su lugar con `.set()`/
        # `.item()` -- sin tocar la miniatura ya cargada ni la selección.
        existentes = set(self.tabla.get_children())
        deseados = set(self._recortes)
        for nombre in existentes - deseados:
            self.tabla.delete(nombre)
        for nombre in sorted(self._recortes):
            e = self._recortes[nombre]["estado"]
            tag = e["decision"] if e["decision"] in ("aprobado", "descartado") else ""
            valores = (f'{e["falta_pct"]:.1f}',
                      "sí" if e["mordida_real"] else "no",
                      "sí" if e["sombra_outlier"] else "no",
                      e["decision"])
            if nombre in existentes:
                for col, val in zip(("falta", "mordida", "sombra", "decision"), valores):
                    self.tabla.set(nombre, col, val)
                self.tabla.item(nombre, tags=(tag,) if tag else ())
            else:
                self.tabla.insert("", "end", iid=nombre, text=nombre, values=valores,
                                  tags=(tag,) if tag else ())
        if seleccion_previa and self.tabla.exists(seleccion_previa):
            self.tabla.selection_set(seleccion_previa)
        else:
            # El recorte seleccionado antes de recargar ya no está en la
            # lista (cambió de carpeta de trabajo, o se reprocesó) — el
            # panel de detalle no se puede quedar mostrando ese calzado
            # viejo como si fuera el actual.
            self._limpiar_panel_detalle()

        # El indicador de paso muestra "N aprobada(s) de M": cada recarga del
        # manifiesto puede cambiar ese conteo.
        if self._recortes and getattr(self, "_paso_flujo", 1) == 1:
            self._actualizar_paso(3)
        else:
            self._actualizar_paso()

    # ── selección y visor ────────────────────────────────────────────────

    def _ruta_original(self, nombre: str) -> Path | None:
        """Backup previo a la limpieza automática (excedente/suela), si se
        aplicó alguna vez, para poder mostrar antes/después o revertir."""
        if not self._salida:
            return None
        ruta = self._salida / reparar.CARPETA_ORIGINALES / f"{Path(nombre).stem}.png"
        return ruta if ruta.exists() else None

    def _limpiar_badges(self) -> None:
        for lbl in self._badge_labels:
            lbl.destroy()
        self._badge_labels = []

    def _limpiar_panel_detalle(self) -> None:
        """Vacía el panel de detalle (foto grande, badges, "falta%") del
        recorte que estaba seleccionado — sin esto, después de "Vaciar
        carpeta de trabajo" la tabla queda vacía pero este panel seguía
        mostrando el último calzado que se había mirado antes de vaciar,
        como si fuera "el de ahora"."""
        self._nombre_actual = None
        self._limpiar_badges()
        self.lbl_imagen.configure(image="")
        self._foto_actual = None
        self.v_falta.set("")
        self.v_auto_desc.set("")
        self.v_paso_proceso.set("")
        self.btn_deshacer_auto.state(["disabled"])
        self.chk_antes_despues.state(["disabled"])
        self._ver_antes_despues.set(False)
        for b in (self.btn_aprobar, self.btn_descartar, self.btn_desechar,
                  self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        self._barra_corregir.pack_forget()

    def _agregar_badge(self, texto: str, ok: bool | None) -> None:
        _, fg, bg = _badge(texto, ok)
        lbl = tk.Label(self._badges_frame, text=texto, fg=fg, bg=bg,
                       font=FUENTE_CHICA, padx=8, pady=3)
        lbl.pack(anchor="w", pady=2, fill="x")
        self._badge_labels.append(lbl)

    def _seleccionar(self, _evt=None) -> None:
        # Tkinter manda las excepciones de un callback a stderr y sigue como
        # si nada: si algo falla acá, el panel de detalle queda a medio armar
        # sin ningún aviso en pantalla. Se atrapa para poder decirlo en la
        # barra de log.
        try:
            self._seleccionar_real()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.v_log.set(f"Error mostrando esta foto: {exc}")

    def _seleccionar_real(self) -> None:
        sel = self.tabla.selection()
        if not sel:
            return
        nombre = sel[0]
        self._nombre_actual = nombre
        r = self._recortes.get(nombre)
        if not r:
            return
        e = r["estado"]
        self.v_falta.set(f'{e["falta_pct"]:.1f}%')

        self._limpiar_badges()
        if e["mordida_real"]:
            self._agregar_badge("Suela con mordida", False)
        else:
            self._agregar_badge("Suela completa", True)
        if e["sombra_outlier"]:
            corregido = e.get("sombra_corregida_px", 0) > 0
            self._agregar_badge("Sombra corregida" if corregido else "Sombra detectada", corregido)
        if e.get("tiene_excedente"):
            self._agregar_badge("Restos de fondo detectados", False)

        hubo_auto = bool(self._ruta_original(nombre))
        if hubo_auto:
            partes = []
            if e["mordida_real"] or e.get("sombra_corregida_px", 0) or e.get("tiene_excedente"):
                pass
            if e.get("sombra_corregida_px"):
                partes.append(f"sombra corregida ({e['sombra_corregida_px']}px)")
            if not e["mordida_real"] and (self._ruta_original(nombre)):
                partes.append("suela completada")
            self.v_auto_desc.set("Se corrigió automáticamente: " + (", ".join(partes) if partes
                                  else "recorte ajustado") + ".")
            self.btn_deshacer_auto.state(["!disabled"])
            self.btn_deshacer_auto.configure(text="Deshacer limpieza automática")
        else:
            self.v_auto_desc.set("No hizo falta corregir nada en este recorte.")
            self.btn_deshacer_auto.state(["disabled"])

        for b in (self.btn_aprobar, self.btn_descartar, self.btn_desechar,
                  self.btn_reparar_suela, self.btn_recortar_sombra):
            b.state(["!disabled"])
        # "Revertir" solo tiene sentido si hay una versión anterior guardada.
        self.btn_revertir_recorte.state(["!disabled"] if hubo_auto else ["disabled"])
        self.chk_lineas.state(["!disabled"])

        self.chk_antes_despues.state(["!disabled"] if hubo_auto else ["disabled"])
        if not hubo_auto:
            self._ver_antes_despues.set(False)
        if not self._barra_corregir.winfo_manager():
            # `before=self._marco_visor` es lo que la mantiene ANTES del visor
            # en el orden de pack: sin eso `pack` le reparte al visor toda la
            # cavidad primero y Tk desmapea esta franja sin avisar (ver el
            # comentario largo en `_armar_ui`).
            self._barra_corregir.pack(side="bottom", fill="x", padx=14, pady=(0, 6),
                                      before=self._marco_visor)
            self._barra_corregir.update_idletasks()
        self._mostrar_imagen()

    # ── corregir el recorte seleccionado, desde la pantalla de revisión ──
    # Mismas llamadas a `reparar` que hace `_abrir_visor_recorte`; lo único
    # que cambia es desde dónde se disparan y que el resultado se refresca en
    # el panel de revisión en vez de en el diálogo.

    def _remedir_recorte(self, nombre: str) -> None:
        """Vuelve a medir la suela de un recorte que se acaba de corregir a
        mano y actualiza su entrada en `self._recortes`.

        Hace falta porque el `estado` del manifiesto se calculó UNA vez, al
        procesar el lote (ver `estado.calcular_estado_grupo`): sin esto, el
        panel seguiría diciendo "Suela con mordida · falta 8%" sobre una foto
        que el comprador acaba de reparar. Solo se recalculan los campos
        medibles del PNG; la decisión y lo que depende del grupo (sombra
        outlier) se dejan como estaban."""
        r = self._recortes.get(nombre)
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        if not png.exists():
            return
        try:
            s = reparar.medir_suela(png, color_basura=reparar.COLOR_BASURA)
            r["estado"]["falta_pct"] = round(reparar.porcentaje_faltante(s), 2)
            r["estado"]["mordida_real"] = reparar.tiene_mordida_real(s)
        except ValueError:
            r["estado"]["falta_pct"] = 0.0
            r["estado"]["mordida_real"] = False
        except Exception:  # noqa: BLE001
            return
        try:
            r["estado"]["tiene_excedente"] = reparar.tiene_excedente(
                png, color_basura=reparar.COLOR_BASURA)
        except Exception:  # noqa: BLE001
            pass

    def _tras_corregir(self, nombre: str, mensaje: str) -> None:
        """Refresca todo lo que muestra ese recorte después de tocarlo: la
        medición del estado (que alimenta los badges y el % faltante), las
        miniaturas y el visor grande."""
        self._remedir_recorte(nombre)
        # Se anota que ESTA foto se corrigió a mano en esta sesión: al
        # aprobarla, el visor se cierra solo (ver `_decidir`) en vez de quedar
        # abierto esperando un clic más.
        self._corregidas_a_mano.add(nombre)
        self._refrescar_miniatura_resumen(nombre)
        self.v_log.set(f"{nombre}: {mensaje}")
        # Re-dibuja badges, % faltante y estado de los botones desde la
        # entrada ya remedida, y termina llamando a `_mostrar_imagen()`.
        self._seleccionar()

    # ── las 2 correcciones, UNA sola implementación ──────────────────────
    #
    # Antes esta lógica estaba escrita DOS veces: acá para la foto que se está
    # mirando (la barra "CORREGIR ESTA FOTO") y otra vez dentro de
    # `_aplicar_limpieza_seleccion`, en la pantalla masiva de casillas. Dos
    # copias de la misma secuencia (`reparar.reparar` + `reparar.aplicar`) que
    # ya se habían desincronizado en los hechos: la de la pantalla masiva
    # se tragaba el `ValueError` de "nada que reparar" en silencio, y la de
    # acá lo mostraba en el log.
    #
    # Ahora hay una sola: `_reparar_suela_de` / `_recortar_sombra_de` trabajan
    # sobre UN nombre y devuelven (ok, mensaje). Los dos caminos de la interfaz
    # —una foto, o N seleccionadas en la grilla— son dos llamadores del mismo
    # código, no dos implementaciones.

    def _reparar_suela_de(self, nombre: str) -> tuple[bool, str]:
        r = self._recortes.get(nombre)
        if not r or not r.get("png") or not self._salida:
            return False, "ya no existe"
        try:
            res = reparar.reparar(Path(r["png"]),
                                  color_basura=reparar.COLOR_BASURA, forzar=True)
            reparar.aplicar(self._salida, Path(nombre).stem, res)
        except ValueError as exc:
            # No es un error técnico: es "no hay nada que reparar acá" o
            # "falta demasiado". Se distingue del fallo real (abajo) porque en
            # bloque no tiene sentido contarlo como error.
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"no se pudo reparar la suela ({exc})"
        return True, f"suela reparada ({res.px_rellenados}px rellenados)"

    def _recortar_sombra_de(self, nombre: str) -> tuple[bool, str]:
        r = self._recortes.get(nombre)
        if not r or not r.get("png") or not self._salida:
            return False, "ya no existe"
        try:
            res = reparar.aplicar_recorte_excedente(
                self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
        except Exception as exc:  # noqa: BLE001
            return False, f"no se pudo recortar sombra ({exc})"
        return True, f"sombra recortada ({res['px_recortados']}px)"

    def _correccion_en_curso(self, boton, texto_trabajando: str) -> None:
        """Deja la barra "CORREGIR ESTA FOTO" en modo "estoy trabajando":
        el botón tocado cambia de texto y los tres quedan deshabilitados.

        La reparación corre en el hilo de la interfaz (es lo que ya hacía), así
        que sin esto la ventana se quedaba muda unos segundos y el usuario no
        sabía si su clic había hecho algo. `update_idletasks` es obligatorio:
        sin él el cambio de texto se dibujaría recién DESPUÉS de terminar la
        reparación, o sea nunca."""
        self._texto_btn_correccion = boton.cget("text")
        try:
            boton.configure(text=texto_trabajando)
        except tk.TclError:
            pass
        for b in (self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        self.configure(cursor="watch")
        self.update_idletasks()

    def _correccion_terminada(self, boton) -> None:
        try:
            boton.configure(text=getattr(self, "_texto_btn_correccion", None)
                            or boton.cget("text"))
        except tk.TclError:
            pass
        self.configure(cursor="")
        # Quién queda habilitado y quién no lo decide `_seleccionar_real`
        # (depende de si hay versión anterior guardada): se rehabilitan acá
        # solo los dos que siempre aplican mientras haya foto seleccionada, y
        # el refresco posterior ajusta el resto.
        if self._nombre_actual:
            for b in (self.btn_reparar_suela, self.btn_recortar_sombra):
                b.state(["!disabled"])

    def _reparar_suela_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre:
            return
        self._correccion_en_curso(self.btn_reparar_suela, "⏳ Reparando…")
        try:
            ok, mensaje = self._reparar_suela_de(nombre)
        finally:
            self._correccion_terminada(self.btn_reparar_suela)
        if not ok:
            self.v_log.set(f"{nombre}: {mensaje}")
            return
        self._tras_corregir(nombre, mensaje)

    def _recortar_sombra_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre:
            return
        self._correccion_en_curso(self.btn_recortar_sombra, "⏳ Recortando…")
        try:
            ok, mensaje = self._recortar_sombra_de(nombre)
        finally:
            self._correccion_terminada(self.btn_recortar_sombra)
        if not ok:
            self.v_log.set(f"{nombre}: {mensaje}")
            return
        self._tras_corregir(nombre, mensaje)

    def _revertir_recorte_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre or not self._salida:
            return
        with reloj(self):
            revertido = reparar.revertir(self._salida, Path(nombre).stem)
        if not revertido:
            self.v_log.set(f"{nombre}: no hay una versión anterior guardada para revertir")
            return
        self._tras_corregir(nombre, "correcciones revertidas al recorte original")

    def _mostrar_imagen(self) -> None:
        r = self._recortes.get(self._nombre_actual or "")
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        self.v_paso_proceso.set("")

        try:
            if self._ver_antes_despues.get():
                ruta_antes = self._ruta_original(self._nombre_actual)
                if not ruta_antes or not png.exists():
                    self.v_log.set("No hay versión anterior guardada para este recorte.")
                    return
                antes = self._componer_sobre_gris(Image.open(ruta_antes))
                despues = (reparar.dibujar_contorno(png) if self._ver_lineas.get()
                           else Image.open(png))
                despues = self._componer_sobre_gris(despues) if despues.mode != "RGB" else despues
                alto = max(antes.height, despues.height)
                im = Image.new("RGB", (antes.width + despues.width + 16, alto), COLOR_VISOR_BG)
                im.paste(antes, (0, 0))
                im.paste(despues, (antes.width + 16, 0))
                self.v_paso_proceso.set("Izquierda: antes de la limpieza automática  ·  "
                                        "Derecha: después, con líneas de referencia")
            else:
                if not png.exists():
                    self.v_log.set(f"No encuentro {png}")
                    return
                # Explícito, no automático: el comprador decide cuándo ver
                # las líneas de referencia sobre la foto que está mirando.
                im = (reparar.dibujar_contorno(png) if self._ver_lineas.get()
                      else Image.open(png))
                if im.mode != "RGB":
                    im = self._componer_sobre_gris(im)
        except Exception as exc:  # noqa: BLE001
            self.v_log.set(f"No pude generar la vista: {exc}")
            return
        im = self._ajustar_a_espacio(im, self._espacio_visor())  # agranda si hace falta, no solo achica
        self._foto_actual = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_actual)

    def _espacio_visor(self) -> tuple[int, int]:
        """Espacio REAL disponible para la foto grande, medido sobre
        `_marco_visor`.

        Antes esto eran dos pares de números fijos —(520, 480) normal y
        (960, 460) en antes/después— que no tenían nada que ver con el tamaño
        del visor. Causa raíz del bug de "la imagen grande se ve cortada" al
        retomar un lote: con la franja amarilla en pantalla el visor mide
        bastante menos de 480px de alto, pero la foto se seguía escalando a
        480 y el `tk.Label` (que está dentro de un marco con
        `pack_propagate(False)`) la recortaba en silencio. En un lote nuevo,
        sin franja, los 480 sí entraban — por eso solo se notaba al retomar.

        Si el marco todavía no está medido (`winfo_*` devuelve 1 antes del
        primer ciclo de layout), se cae a los valores fijos de siempre; el
        `<Configure>` de más abajo vuelve a escalar en cuanto Tk asienta el
        layout, así que nunca queda con la medida provisional.

        `update_idletasks()` ANTES de medir (2026-09-09, reclamo repetido de
        "el recorte se ve muy pequeño" que sobrevivió a maximizar la ventana
        y a achicar todas las franjas): sin esto, `winfo_width/height` podían
        devolver la medida VIEJA de antes del último cambio de layout (cerrar
        la franja amarilla, elegir un recorte recién after maximizar, etc.)
        porque Tk todavía no había procesado ese cambio pendiente -- la foto
        se escalaba correcto para un visor que ya no existía, más chico que
        el real.
        """
        self._marco_visor.update_idletasks()
        antes_despues = self._ver_antes_despues.get()
        ancho = self._marco_visor.winfo_width() - 8
        alto = self._marco_visor.winfo_height() - 8
        if ancho < 40 or alto < 40:  # layout aún sin asentar
            return (960, 460) if antes_despues else (520, 480)
        return max(ancho, 120), max(alto, 120)

    def _al_redimensionar_visor(self, _evento=None) -> None:
        """Vuelve a escalar la foto abierta cuando el visor cambia de tamaño:
        al cerrar la franja amarilla de "retomando", al maximizar la ventana o
        al aparecer/desaparecer la barra de "Corregir esta foto". Se pospone
        con `after_idle` para medir DESPUÉS de que Tk asiente el layout, y se
        salta si el tamaño no cambió de verdad (los `<Configure>` llegan en
        ráfaga y reescalar en cada uno haría titilar la imagen)."""
        medida = (self._marco_visor.winfo_width(), self._marco_visor.winfo_height())
        if medida == getattr(self, "_medida_visor", None):
            return
        self._medida_visor = medida
        if not getattr(self, "_nombre_actual", None):
            return
        self.after_idle(self._mostrar_imagen)

    @staticmethod
    def _componer_sobre_gris(origen: Image.Image) -> Image.Image:
        origen = origen.convert("RGBA")
        fondo = Image.new("RGBA", origen.size, (238, 241, 244, 255))
        return Image.alpha_composite(fondo, origen).convert("RGB")

    # ── decisiones y control único de limpieza automática ───────────────

    def _decidir(self, decision: str, nombre: str | None = None) -> bool:
        """Registra "aprobado"/"descartado" para `nombre` (o el de detalle
        actual, si no se pasa ninguno — caso de uso individual de siempre).
        Devuelve si de verdad registró algo, para que quien llame en lote
        pueda contar cuántas se aplicaron de verdad."""
        nombre = nombre if nombre is not None else self._nombre_actual
        if not nombre or not self._salida:
            return False
        r = self._recortes.get(nombre)
        if not r:
            # Ya no existe (desechado definitivamente) — protegido hasta
            # ahora solo "por accidente" (nada llega hasta acá con un nombre
            # borrado en la práctica), pero mejor no confiar en eso: registrar
            # una decisión sobre un recorte inexistente es exactamente el
            # patrón que ya causó el bug de "aprobado" resucitando eliminados.
            return False
        decisiones.registrar(self._salida, nombre, decision)
        r["estado"]["decision"] = decision
        if self.tabla.exists(nombre):
            self.tabla.set(nombre, "decision", decision)
            self.tabla.item(nombre, tags=(decision,))
        self.v_log.set(f"{nombre}: {decision}")
        self._actualizar_paso()
        # Aprobar una foto que se acaba de reparar/recortar cierra su visor:
        # el trabajo sobre esa foto terminó y dejarla abierta obligaba a un
        # clic extra para pasar a la siguiente. Solo en ese caso — si no hubo
        # corrección previa, el comportamiento de siempre no cambia.
        if (decision == decisiones.APROBADO and nombre == self._nombre_actual
                and nombre in self._corregidas_a_mano):
            self._corregidas_a_mano.discard(nombre)
            self._cerrar_visor_foto()
        return True

    def _cerrar_visor_foto(self) -> None:
        """Deja el panel central sin foto abierta y deselecciona la tarjeta,
        volviendo la atención a la grilla."""
        deseleccionar = getattr(self.tabla, "deseleccionar", None)
        if callable(deseleccionar):
            deseleccionar()
        self._limpiar_panel_detalle()

    # ── desechar definitivamente (borrado real de archivos) ──────────────

    def _archivos_del_recorte(self, nombre: str) -> list[Path]:
        """Los archivos EN DISCO que componen un recorte, en la carpeta de
        trabajo actual: el PNG transparente, el JPG con fondo blanco y el
        respaldo previo a la limpieza automática, si existen.

        Mismo patrón de rutas que usan `reparar.py` y `decisiones.py`
        (`<salida>/<formato>/<stem>.<ext>`), para no inventar una tercera
        forma de ubicar el mismo archivo.
        """
        if not self._salida:
            return []
        stem = Path(nombre).stem
        candidatos = [self._salida / fmt / f"{stem}{ext}"
                      for fmt, (_sub, ext) in decisiones.FORMATOS.items()]
        candidatos.append(self._salida / reparar.CARPETA_ORIGINALES / f"{stem}.png")
        return [p for p in candidatos if p.exists()]

    def _desechar_definitivamente(self, nombre: str, al_terminar=None) -> bool:
        """Borra del disco los archivos de un recorte basura y lo saca de la
        pantalla. Irreversible, a diferencia de "Descartar".

        Por qué existe: "Descartar" solo escribe una decisión — el archivo
        basura se queda en la carpeta para siempre y sigue apareciendo en la
        lista cada vez que se abre la carpeta. Para las fotos que directamente
        no sirven (fondo irrecuperable, recorte partido, foto que no es un
        calzado) hacía falta sacarlas de encima de verdad.

        Sobre reprocesar: la línea del manifiesto NO se toca. Está a nivel de
        LÁMINA (sha-1 de la foto de ENTRADA) y una lámina produce varios
        recortes, así que borrarla también haría desaparecer del listado a los
        hermanos buenos del mismo calzado y volvería a procesar la lámina
        entera. La lámina, entonces, sigue contando como "ya hecha" y este
        recorte NO reaparece solo. Es la decisión deliberada: la foto es
        basura por lo que ES, no por cómo se procesó — volver a correrla sola
        daría la misma basura y obligaría al usuario a descartarla de nuevo.
        Si de verdad quiere reintentarla (por ejemplo porque el pipeline
        mejoró), tiene la casilla «Reprocesar aunque ya esté hecho», que
        ignora el manifiesto. Para que no reaparezca al recargar el
        manifiesto, `_recargar_manifiesto` filtra los recortes marcados
        `decisiones.ELIMINADO`.
        """
        if not self._salida:
            return False
        archivos = self._archivos_del_recorte(nombre)
        fuera = self._archivos_fuera_de_trabajo(archivos)
        if fuera:
            messagebox.showerror(
                "Desechar definitivamente",
                "Hay archivos de este recorte fuera de la carpeta de trabajo "
                f"actual:\n{chr(10).join(str(p) for p in fuera)}\n\n"
                "Por seguridad no se borró nada.")
            return False

        detalle = "\n".join(f"  · {p.parent.name}/{p.name}" for p in archivos) or "  (ninguno)"
        if not messagebox.askyesno(
                "Desechar definitivamente",
                f"Vas a BORRAR DEL DISCO el recorte:\n\n{nombre}\n\n"
                f"Se borran estos archivos:\n{detalle}\n\n"
                "Esto NO se puede deshacer — no es lo mismo que «Descartar», "
                "que solo marca la foto y se puede cambiar de opinión después.\n\n"
                "¿Borrarlo definitivamente?",
                icon="warning", default="no"):
            return False

        ok, error = self._desechar_uno(nombre)
        if not ok:
            messagebox.showerror(
                "Desechar definitivamente",
                "No se pudieron borrar todos los archivos (puede que estén "
                f"abiertos en otro programa):\n\n{error}")
            return False

        if callable(al_terminar):
            al_terminar()
        return True

    def _archivos_fuera_de_trabajo(self, archivos: list[Path]) -> list[Path]:
        """Mismo criterio de seguridad que `_vaciar_carpeta_trabajo`: no se
        borra NADA que esté fuera de la carpeta de trabajo activa. Si el
        manifiesto viene de otra carpeta (carpeta movida, ruta corregida),
        mejor no borrar que borrar la foto de otro lote."""
        if not self._salida:
            return list(archivos)
        salida = self._salida.resolve()
        return [p for p in archivos if salida not in p.resolve().parents]

    def _desechar_uno(self, nombre: str) -> tuple[bool, str]:
        """El borrado propiamente dicho de UN recorte, sin ningún cuadro de
        diálogo: borra los archivos, escribe la decisión y saca la tarjeta de
        la pantalla. Devuelve `(ok, motivo_del_fallo)`.

        Está separado de `_desechar_definitivamente` (que es el que pregunta)
        para que el caso individual y el caso en bloque compartan exactamente
        el mismo borrado, en vez de tener dos copias que se van despegando; y
        para que en bloque se pueda preguntar UNA sola vez por las N fotos y
        seguir con las demás si una falla.
        """
        if not self._salida:
            return False, "no hay carpeta de trabajo activa"
        archivos = self._archivos_del_recorte(nombre)
        fuera = self._archivos_fuera_de_trabajo(archivos)
        if fuera:
            return False, ("hay archivos fuera de la carpeta de trabajo: "
                           + ", ".join(str(p) for p in fuera))

        errores: list[str] = []
        for p in archivos:
            try:
                p.unlink()
            except OSError as exc:
                errores.append(f"{p.name}: {exc}")
        if errores:
            # Si quedó algo sin borrar NO se registra "eliminado": la bitácora
            # diría que el recorte ya no está y el archivo seguiría ahí.
            return False, "; ".join(errores)

        # La bitácora queda con "eliminado", no con "descartado": así se sabe
        # que el archivo falta a propósito y no por un accidente.
        decisiones.registrar(self._salida, nombre, decisiones.ELIMINADO,
                             nota="archivos borrados del disco por el usuario")

        self._recortes.pop(nombre, None)
        # `tabla.delete` ya lo saca del lote de selección múltiple (ver
        # `GrillaRecortes.delete`), así que no queda una foto borrada marcada
        # para una acción en bloque.
        if self.tabla.exists(nombre):
            self.tabla.delete(nombre)
        if self._nombre_actual == nombre:
            self._limpiar_panel_detalle()
        self._fotos_visor_recorte.pop(nombre, None)

        # Antes acá había que desarmar A MANO la tarjeta que la pantalla masiva
        # "Elegir recortes a limpiar" había creado para este recorte: esa
        # pantalla armaba sus tarjetas una sola vez al abrirse y no estaba
        # atada a `self._recortes`, así que desechar desde el visor grande
        # dejaba una tarjeta huérfana apuntando a un archivo borrado. Con la
        # pantalla eliminada, la grilla de revisión es la única lista de fotos
        # y sí está atada a `self._recortes` — no hay una segunda copia del
        # lote que se pueda quedar desincronizada.
        self.v_log.set(f"{nombre}: desechado definitivamente "
                       f"({len(archivos)} archivo(s) borrado(s)).")
        return True, ""

    def _desechar_seleccionado(self) -> None:
        if not self._nombre_actual:
            return
        self._desechar_definitivamente(self._nombre_actual)

    # ── acciones en bloque sobre varias tarjetas ─────────────────────────

    def _al_cambiar_lote(self, nombres: list[str]) -> None:
        """La grilla avisó que cambió el conjunto marcado: se muestra o se
        esconde la barra de acciones en bloque y se actualizan los conteos."""
        n = len(nombres)
        if n < 2:
            self._barra_lote.pack_forget()
            if not self._lbl_ayuda_recortes.winfo_manager():
                # Vuelve la pista, y vuelve ARRIBA de todo (`after` con el
                # título): sin `after` reaparecería al final del panel.
                self._lbl_ayuda_recortes.pack(anchor="w", fill="x",
                                              after=self._lbl_titulo_recortes)
            return
        self.v_lote.set(f"{n} fotos seleccionadas")
        self.btn_lote_reparar.configure(text=f"🔧 Suela ({n})")
        self.btn_lote_recortar.configure(text=f"✂ Sombra ({n})")
        self.btn_lote_aprobar.configure(text=f"✓ Aprobar ({n})")
        self.btn_lote_descartar.configure(text=f"✗ Descartar ({n})")
        self.btn_lote_desechar.configure(text=f"🗑 Desechar definitivamente ({n})")
        # La pista de "Ctrl+clic marca varias" se va: ya se usó, y su alto es
        # exactamente lo que le falta a la barra para entrar en la ventana por
        # defecto (ver la nota del bug en `_armar_cuerpo_revision`).
        self._lbl_ayuda_recortes.pack_forget()
        if not self._barra_lote.winfo_manager():
            # Dentro de `_pie_lote`, que ya tiene su lugar reservado al pie del
            # panel desde que se armó la pantalla.
            self._barra_lote.pack(fill="x")

    def _cancelar_lote(self) -> None:
        self.tabla.limpiar_lote()
        self.v_log.set("Selección múltiple cancelada.")

    def _corregir_lote(self, etiqueta: str, corregir) -> None:
        """Aplica una corrección a las N fotos marcadas en la grilla.

        `corregir` es `_reparar_suela_de` o `_recortar_sombra_de` — el MISMO
        código que corre la barra "CORREGIR ESTA FOTO" para una sola foto.
        Acá solo se agrega lo propio del bloque: avance visible mientras corre
        (cada reparación es trabajo real de imagen, no instantáneo), y que un
        fallo suelto no frene al resto.

        Los tres desenlaces se cuentan por separado a propósito: "sin nada que
        corregir" NO es un error (la detección de sombra/mordida es una ayuda,
        no un diagnóstico — el usuario marca con sus propios ojos y a veces la
        foto ya estaba bien), y mezclarlo con los errores reales daba un
        mensaje alarmante sobre un lote que salió perfecto.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        self.configure(cursor="watch")
        ok, sin_nada, errores = 0, 0, []
        try:
            for i, nombre in enumerate(nombres, start=1):
                self.v_lote.set(f"{etiqueta}… {i} de {len(nombres)}")
                self.update_idletasks()
                hecho, mensaje = corregir(nombre)
                if hecho:
                    ok += 1
                    self._remedir_recorte(nombre)
                    self._refrescar_miniatura_resumen(nombre)
                elif mensaje.startswith("no se pudo"):
                    errores.append(f"{nombre}: {mensaje}")
                else:
                    sin_nada += 1
        finally:
            self.configure(cursor="")

        detalle = f"{ok} de {len(nombres)} corregida(s)"
        if sin_nada:
            detalle += f", {sin_nada} sin nada que corregir"
        if errores:
            detalle += f", {len(errores)} con error"
        self.v_log.set(f"{etiqueta}: {detalle}.")
        # Se conserva la selección: es normal querer aplicar las DOS
        # correcciones al mismo conjunto (primero suela, después sombra), y
        # limpiar el lote acá obligaría a volver a marcar las 12 fotos.
        self._al_cambiar_lote(nombres)
        self._seleccionar()  # re-dibuja badges y visor de la foto en detalle
        self._refrescar_resumen_si_visible()
        if errores:
            messagebox.showwarning(
                etiqueta,
                f"{ok} de {len(nombres)} se corrigieron.\n\n"
                "Estas no se pudieron:\n" + "\n".join(f"  · {e}" for e in errores[:12]))

    def _reparar_suela_lote(self) -> None:
        self._corregir_lote("Reparar suela", self._reparar_suela_de)

    def _recortar_sombra_lote(self) -> None:
        self._corregir_lote("Recortar sombra", self._recortar_sombra_de)

    def _aprobar_lote(self) -> None:
        """"Aprobar" en bloque: las fotos que ya están bien tal cual, sin
        ninguna corrección.

        Es el reemplazo de la casilla "Aprobar" de la pantalla masiva
        eliminada. Usa el mismo `_decidir` que el botón "✓ Aprobar" de una
        sola foto — que ya hacía exactamente lo que hacía
        `_aprobar_sin_limpiar` (registrar "aprobado" sin tocar píxeles), más
        la guarda de no escribir decisiones sobre un recorte ya desechado.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        with reloj(self):
            n_ok = sum(1 for nombre in nombres if self._decidir("aprobado", nombre))
        self.tabla.limpiar_lote()
        self.v_log.set(f"{n_ok} de {len(nombres)} recorte(s) aprobado(s) sin corrección.")
        self._refrescar_resumen_si_visible()

    def _descartar_lote(self) -> None:
        """"Descartar" en bloque. Sin confirmación, igual que el «Descartar»
        de una sola foto: no borra nada, solo escribe una decisión que se
        puede cambiar volviendo a aprobar."""
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        with reloj(self):
            n_ok = sum(1 for nombre in nombres if self._decidir("descartado", nombre))
        self.tabla.limpiar_lote()
        self.v_log.set(f"{n_ok} de {len(nombres)} recorte(s) descartado(s).")
        self._refrescar_resumen_si_visible()

    def _desechar_lote(self) -> None:
        """Borrado real en bloque. La confirmación es más dura que la
        individual — dice cuántos recortes y cuántos archivos se van a borrar —
        porque acá el error no cuesta una foto sino N.

        Un fallo suelto (archivo abierto en otro programa, permisos) NO frena
        al resto: se sigue con las demás y al final se muestra un resumen con
        las que no se pudieron borrar.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        total_archivos = sum(len(self._archivos_del_recorte(n)) for n in nombres)
        muestra = "\n".join(f"  · {Path(n).stem}" for n in nombres[:12])
        if len(nombres) > 12:
            muestra += f"\n  · … y {len(nombres) - 12} más"
        if not messagebox.askyesno(
                "Desechar definitivamente",
                f"Esto borra permanentemente {len(nombres)} recorte(s) "
                f"({total_archivos} archivo(s) en disco):\n\n{muestra}\n\n"
                "NO SE PUEDE DESHACER. No es lo mismo que «Descartar», que "
                "solo marca las fotos y se puede revertir.\n\n¿Continuar?",
                icon="warning", default="no"):
            self.v_log.set("Desechado en bloque cancelado — no se borró nada.")
            return

        borrados, fallos = 0, []
        with reloj(self):
            for nombre in nombres:
                ok, error = self._desechar_uno(nombre)
                if ok:
                    borrados += 1
                else:
                    fallos.append(f"{nombre}: {error}")

        self.tabla.limpiar_lote(conservar_actual=False)
        self.v_log.set(f"{borrados} recorte(s) desechado(s) definitivamente"
                       + (f", {len(fallos)} con problemas." if fallos else "."))
        if fallos:
            messagebox.showerror(
                "Desechar definitivamente",
                f"Se borraron {borrados} de {len(nombres)} recortes.\n\n"
                "Estos no se pudieron borrar (puede que estén abiertos en otro "
                f"programa):\n\n{chr(10).join(fallos)}")
        self._refrescar_resumen_si_visible()

    def _refrescar_resumen_si_visible(self) -> None:
        """Si la pantalla de resumen está armada, sus conteos quedaron viejos
        después de un borrado en bloque."""
        if getattr(self, "_resumen_frame", None) is None:
            return
        # Se vuelve a armar de cero (destruir + mostrar): sus conteos salen de
        # `self._recortes` y de `decisiones.resumen`, y ambos cambiaron.
        self._ocultar_resumen()
        self._mostrar_resumen_final()

    def _alternar_limpieza_auto(self) -> None:
        """Un solo control para deshacer/rehacer la limpieza automática de
        este recorte (excedente recortado + suela completada), en vez de los
        4 botones separados que había antes. Por defecto la limpieza ya se
        aplicó sola durante el procesamiento; acá el usuario puede decir
        "no estoy de acuerdo" (deshacer) y, si cambia de opinión, "rehacer"."""
        if not self._nombre_actual or not self._salida:
            return
        nombre = self._nombre_actual
        tiene_original = bool(self._ruta_original(nombre))

        if self.btn_deshacer_auto["text"].startswith("Deshacer"):
            ok = reparar.revertir(self._salida, Path(nombre).stem)
            if not ok:
                self.v_log.set(f"{nombre}: no hay original guardado para revertir")
                return
            self.v_log.set(f"{nombre}: limpieza automática deshecha, vuelve al recorte original")
            self.btn_deshacer_auto.configure(text="Rehacer limpieza automática")
            self._ver_antes_despues.set(False)
            self._mostrar_imagen()
        else:
            r = self._recortes.get(nombre)
            if not r or not r.get("png"):
                return
            png = Path(r["png"])
            try:
                aplicado_algo = False
                if reparar.tiene_excedente(png, color_basura=reparar.COLOR_BASURA):
                    reparar.aplicar_recorte_excedente(self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
                    aplicado_algo = True
                s_actual = reparar.medir_suela(png, color_basura=reparar.COLOR_BASURA)
                if reparar.tiene_mordida_real(s_actual):
                    res = reparar.reparar(png, color_basura=reparar.COLOR_BASURA)
                    reparar.aplicar(self._salida, Path(nombre).stem, res)
                    aplicado_algo = True
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.v_log.set(f"{nombre}: no se pudo rehacer la limpieza ({exc})")
                return
            if not aplicado_algo:
                self.v_log.set(f"{nombre}: ya no había nada que corregir")
            else:
                self.v_log.set(f"{nombre}: limpieza automática vuelta a aplicar")
            self.btn_deshacer_auto.configure(text="Deshacer limpieza automática")
            self._mostrar_imagen()

    # Tamaño de las celdas de la grilla de elección de fotos extraídas
    # (paso 2, "Qué limpiar"). Acá vivían también las constantes de la
    # pantalla masiva "Elegir recortes a limpiar" (MINIATURA_LIMPIEZA,
    # ANCHO/ALTO_CELDA_LIMPIEZA, ANCHO_CONTROLES_LIMPIEZA), eliminadas junto
    # con esa pantalla el 2026-09-08.
    MINIATURA_REVISION = 170
    ANCHO_CELDA_REVISION = 194
    ALTO_CELDA_REVISION = 232
    _SEPARACION_CELDA = 12  # el padx/pady de cada celda, contado dos veces

    @staticmethod
    def _visor_de_grilla(grilla):
        """La franja VISIBLE de un `CTkScrollableFrame`.

        Ojo, que acá había un error real y silencioso: `grilla.winfo_height()`
        de un CTkScrollableFrame NO da el alto visible sino el alto de TODO su
        contenido (medido: 8898 px con 20 tarjetas en una ventana de 774), así
        que cualquier cálculo de "cuánto espacio tengo" hecho con eso da
        siempre gigante y no ajusta nada. El alto visible es el del canvas que
        lo contiene, que es su `master` — API pública de Tk, no interna de CTk."""
        return getattr(grilla, "master", None) or grilla

    def _acomodar_grilla(self, clave: str, grilla, obtener_celdas,
                          ancho_celda: int) -> None:
        """Re-acomoda las celdas de una grilla de miniaturas en tantas
        columnas como quepan en el ancho disponible.

        `obtener_celdas` es una función y no una lista porque las celdas se
        pueden destruir mientras la pantalla está abierta (desechar un recorte
        desde el visor grande saca su tarjeta), y una lista capturada al
        dibujar quedaría con widgets muertos.

        Se guarda el número de columnas ya aplicado: Tk emite `<Configure>` a
        cada píxel de arrastre del borde de la ventana, y re-grillar cientos
        de tarjetas en cada uno de esos eventos trabaría la ventana."""
        ancho = self._visor_de_grilla(grilla).winfo_width()
        if ancho <= 1:  # todavía sin layout: estimar del ancho de la ventana
            ancho = max(self.winfo_width() - 90, ancho_celda)
        cols = max(1, (ancho - 8) // (ancho_celda + self._SEPARACION_CELDA))
        if self._cols_grilla.get(clave) == cols:
            return
        self._cols_grilla[clave] = cols
        for idx, celda in enumerate(obtener_celdas()):
            fila_i, col_i = divmod(idx, cols)
            celda.grid_configure(row=fila_i, column=col_i)













    # ── resumen final del lote ───────────────────────────────────────────

    def _ocultar_resumen(self) -> None:
        if self._resumen_frame is not None:
            self._resumen_frame.destroy()
            self._resumen_frame = None
            self._mostrar_cuerpo()
            self._actualizar_paso(3)

    def _asegurar_cuerpo_visible(self) -> None:
        """Cierra CUALQUIER pantalla de reemplazo (revisión de extracción,
        selección de limpieza, resumen final) y vuelve a mostrar `self.cuerpo`.

        Bug real corregido (2026-09-03): cada pantalla de reemplazo se
        cerraba por su cuenta (o ni eso) sin garantizar que `self.cuerpo`
        quedara re-empaquetado -- eso rompía `_procesar()` (que hace
        `self.monitor.pack(before=self.cuerpo)`, y falla con TclError si
        `self.cuerpo` no está empaquetado) y dejaba la ventana en blanco
        después de "Limpiar caché" u "Omitir limpieza" cuando la pantalla
        siguiente no tenía nada que mostrar. Llamar esto SIEMPRE antes de
        volver a mostrar el cuerpo principal, sin importar cuál pantalla
        estaba abierta."""
        habia_alguna = False
        if self._resumen_frame is not None:
            self._resumen_frame.destroy()
            self._resumen_frame = None
            habia_alguna = True
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
            habia_alguna = True
        if getattr(self, "_candidatos_frame", None) is not None:
            self._candidatos_frame.destroy()
            self._candidatos_frame = None
            habia_alguna = True
        if getattr(self, "_vector_frame", None) is not None:
            self._vector_frame.destroy()
            self._vector_frame = None
            # Los widgets del bloque de progreso vivían DENTRO de esa tarjeta:
            # dejar las referencias colgando haría que `_avance_compras` y
            # compañía escriban sobre widgets destruidos (lo atrapan, pero es
            # mejor no depender de eso) y que la consola del paso 5 siga
            # aceptando líneas de un cálculo cuya pantalla ya no existe.
            self._marco_progreso_compras = None
            self._sep_progreso_compras = None
            self.barra_compras = None
            self.txt_consola_vector = None
            self.btn_calcular_vector = None
            habia_alguna = True
        if habia_alguna:
            self._mostrar_cuerpo()

    def _mostrar_resumen_final(self) -> None:
        """Al terminar todo el lote: la lámina original + sus recortes
        finales, la ruta de salida con acceso directo al Explorador, y el
        botón de exportar bien visible."""
        if not self._salida or not self._recortes:
            return

        # Reloj de arena: armar esta pantalla recorre TODOS los recortes del
        # lote (puede ser un lote grande) antes de que se vea algo -- sin
        # señal, se veía igual que un programa colgado.
        self.configure(cursor="watch")
        self.after(400, lambda: self.configure(cursor=""))
        self._asegurar_cuerpo_visible()
        self._ocultar_pantallas_base()
        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._resumen_frame = marco
        self._actualizar_paso(4)

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        fila_titulo = Marco(interior)
        fila_titulo.pack(fill="x")
        Etiqueta(fila_titulo, text="Confirmar y enviar", style="Titulo.TLabel").pack(side="left")
        # "Atrás" en el pie ya hace exactamente esto (_paso_atras → _ocultar_resumen
        # cuando el paso es 4) — un botón duplicado acá solo confundía.

        conteo = decisiones.resumen(self._salida, len(self._recortes))
        resumen_txt = (f"{len(self._recortes)} calzados procesados  ·  "
                       f"{conteo.get('aprobado', 0)} aprobados  ·  "
                       f"{conteo.get('descartado', 0)} descartados  ·  "
                       f"{conteo.get('pendiente', 0)} pendientes de revisar")
        if conteo.get("eliminado"):
            resumen_txt += f"  ·  {conteo['eliminado']} desechados (borrados del disco)"
        Etiqueta(interior, text=resumen_txt, style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(4, 12))

        fila_ruta = Marco(interior)
        fila_ruta.pack(fill="x", pady=(0, 12))
        Etiqueta(fila_ruta, text=f"Carpeta de salida: {self._salida}",
                 style="Panel.TLabel").pack(side="left")
        Boton(fila_ruta, text="Abrir carpeta", command=self._abrir_carpeta_salida).pack(
            side="left", padx=(10, 0))
        # Acá NO va ningún botón de "Enviar a Asistente de Compras": la acción
        # de avanzar vive en el botón "Continuar  →" del pie, el mismo lugar
        # que en todos los otros pasos (pedido del usuario, 2026-09-09). Ese
        # botón sabe en qué estado está el lote: si nunca se envió, envía (y al
        # terminar abre los candidatos solo, vía `cand_fin`); si ya se envió,
        # solo abre la vista.
        Boton(fila_ruta, text="Exportar aprobados…",
              command=self._exportar).pack(side="right", padx=(0, 8))
        self._sincronizar_btn_compras()

        # Línea de estado propia del envío a comparar -- separada de
        # "Estado:" en la barra superior (esa es del PROCESAMIENTO de
        # limpieza) para que el avance de la comparación se vea pegado al
        # botón que la dispara, no lejos en otra barra.
        # Bloque de progreso del envío: NO se muestra mientras no haya un
        # envío en curso (reclamo real del usuario, 2026-09-09: "no entiendo
        # para qué sirve esa barra"). Antes quedaba siempre a la vista, vacía
        # o al 100%, sin rótulo que dijera de qué hablaba. Ahora aparece al
        # tocar el botón, con título explícito, y desaparece al terminar.
        self._marco_progreso_compras = Marco(interior)
        Etiqueta(self._marco_progreso_compras,
                 text="Progreso del envío al Asistente de Compras",
                 style="Panel.TLabel").pack(anchor="w")
        self.v_estado_compras = tk.StringVar(value="")
        Etiqueta(self._marco_progreso_compras, textvariable=self.v_estado_compras,
                 style="SuavePanel.TLabel", wraplength=1100).pack(
            anchor="w", fill="x", pady=(2, 0))
        # Barra propia del envío a calificar. Reemplaza el uso del monitor de
        # láminas (que ya no se muestra en este paso): acá no hay "lámina
        # actual" que reportar, solo cuántos candidatos se vectorizaron.
        self.barra_compras = Barra(self._marco_progreso_compras,
                                   mode="determinate", maximum=100)
        self.barra_compras.pack(fill="x", pady=(4, 0))

        self._sep_progreso_compras = Separador(interior)
        self._sep_progreso_compras.pack(fill="x", pady=(12, 12))
        # Si se vuelve a entrar al paso mientras un envío sigue corriendo en
        # segundo plano, el bloque tiene que reaparecer.
        if getattr(self, "_enviando_compras", False):
            self._mostrar_progreso_compras()

        # Lista con scroll de miniaturas: lámina por lámina, con sus recortes
        # finales al lado. Igual que las otras dos grillas, ahora sobre
        # `CTkScrollableFrame` (con rueda del mouse).
        lista_int = ctk.CTkScrollableFrame(interior, fg_color="transparent",
                                           scrollbar_button_color=PAR_BORDE,
                                           scrollbar_button_hover_color=PAR_TEXTO_SUAVE)
        lista_int.pack(fill="both", expand=True)

        self._fotos_resumen = []
        self._widgets_resumen_recorte = {}
        por_lamina: dict[str, list[tuple[str, dict]]] = {}
        # Una foto DESCARTADA no se muestra más — ni acá ni en ninguna
        # pantalla posterior a donde se tomó esa decisión.
        #
        # Esto era a propósito y estaba mal pensado: la pantalla listaba TODO
        # el lote con una etiqueta de color por decisión, como registro de
        # auditoría. Para el comprador eso significaba seguir viendo, foto por
        # foto, calzados que ya había sacado del catálogo. Reclamo textual del
        # usuario (2026-09-07): "si descarto una imagen, espero no verla de
        # nuevo definitivamente". El registro no se pierde: sigue entero en la
        # bitácora de decisiones, y el CONTEO de descartadas se sigue
        # mostrando en la línea de resumen de arriba — lo que desaparece es la
        # miniatura, que es lo que molestaba.
        for nombre, r in sorted(self._recortes.items()):
            if r.get("estado", {}).get("decision") == decisiones.DESCARTADO:
                continue
            stem = nombre.rsplit("_", 1)[0]
            por_lamina.setdefault(stem, []).append((nombre, r))

        for stem, items in sorted(por_lamina.items()):
            fila = Marco(lista_int, padding=(0, 8))
            fila.pack(fill="x", anchor="w")

            original = self._rutas_pendientes.get(stem) or self._buscar_original_por_stem(stem)
            marco_orig = tk.Frame(fila, background=COLOR_VISOR_BG, width=110, height=110)
            marco_orig.pack(side="left", padx=(0, 10))
            marco_orig.pack_propagate(False)
            lbl_orig = tk.Label(marco_orig, background=COLOR_VISOR_BG)
            lbl_orig.pack(fill="both", expand=True)
            pie_orig = f"{stem}\n(original)"
            if original and Path(original).exists():
                try:
                    im = Image.open(original).convert("RGB")
                    im.thumbnail((104, 104))
                    foto = ImageTk.PhotoImage(im)
                    self._fotos_resumen.append(foto)
                    lbl_orig.configure(image=foto)
                except Exception:  # noqa: BLE001
                    pass
                # La lámina original también se amplía con un clic, igual que
                # los recortes: es la foto más chica de la fila y la que menos
                # se deja mirar en 104px. Se reusa el mismo visor del paso de
                # revisión (`_mostrar_grande`), no uno nuevo.
                ruta_orig = Path(original)
                marco_orig.configure(cursor="hand2")
                lbl_orig.configure(cursor="hand2")
                for widget in (marco_orig, lbl_orig):
                    widget.bind("<Button-1>",
                                lambda _evt, p=ruta_orig: self._mostrar_imagen_flotante(
                                    p, f"Foto original: {p.name}"))
                pie_orig = f"{stem}\n(original · clic para ampliar)"
            Etiqueta(fila, text=pie_orig, style="SuavePanel.TLabel",
                     justify="center", anchor="center").pack(side="left", padx=(0, 14))

            for nombre, r in items:
                png = r.get("png")
                marco_rec = tk.Frame(fila, background=COLOR_VISOR_BG, width=90, height=90,
                                     cursor="hand2")
                marco_rec.pack(side="left", padx=4)
                marco_rec.pack_propagate(False)
                lbl_rec = tk.Label(marco_rec, background=COLOR_VISOR_BG, cursor="hand2")
                lbl_rec.pack(fill="both", expand=True)
                decision = r.get("estado", {}).get("decision", "pendiente")
                if png and Path(png).exists():
                    try:
                        im = self._componer_sobre_gris(Image.open(png))
                        im.thumbnail((84, 84))
                        foto = ImageTk.PhotoImage(im)
                        self._fotos_resumen.append(foto)
                        lbl_rec.configure(image=foto)
                    except Exception:  # noqa: BLE001
                        pass
                color_dec = {"aprobado": COLOR_OK, "descartado": COLOR_MAL}.get(decision, COLOR_TEXTO_SUAVE)
                lbl_dec = tk.Label(fila, text=decision, fg=color_dec, bg=COLOR_PANEL,
                                   font=FUENTE_CHICA)
                lbl_dec.pack(side="left", padx=(0, 10))

                self._widgets_resumen_recorte[nombre] = (lbl_rec, lbl_dec)
                for widget in (marco_rec, lbl_rec):
                    widget.bind("<Button-1>", lambda _evt, n=nombre: self._abrir_visor_recorte(n))

    def _buscar_original_por_stem(self, stem: str) -> Path | None:
        """La lámina de la que salió un recorte, buscada por orden de
        confiabilidad.

        Lo primero que se mira es la ruta que el manifiesto guardó al
        procesar (`origen`), porque es la única que acierta SIEMPRE — también
        cuando el lote vino de una extracción de PDF o Excel (donde las
        láminas quedan en la carpeta `_fotos_extraidas` del extractor, no en
        la carpeta de entrada) y también cuando se retomó un lote viejo (donde
        ni `_entrada_archivos` ni `v_entrada` existen ya). Sin esto, la
        columna de "lámina original" del resumen salía vacía para todo el
        lote, no en casos sueltos."""
        for r in self._recortes.values():
            origen = r.get("_origen_lamina")
            if origen and Path(origen).stem == stem and Path(origen).exists():
                return Path(origen)
        if self._entrada_archivos:
            for c in self._entrada_archivos:
                if c.stem == stem:
                    return c
        if self.v_entrada.get():
            carpeta = Path(self.v_entrada.get())
            if carpeta.is_dir():
                encontrados = list(carpeta.rglob(f"{stem}.*"))
                if encontrados:
                    return encontrados[0]
        # Último recurso: la lámina con las cajas dibujadas que el worker deja
        # en `_pasos/`. No es el original puro, pero vive DENTRO de la carpeta
        # de trabajo, así que está disponible aunque el archivo de entrada ya
        # no exista — mejor que un recuadro gris vacío.
        if self._salida:
            con_cajas = self._salida / "_pasos" / f"{stem}_cajas.jpg"
            if con_cajas.exists():
                return con_cajas
        return None

    def _abrir_carpeta_salida(self) -> None:
        if not self._salida:
            return
        try:
            os.startfile(str(self._salida))  # noqa: S606 - acción pedida explícitamente por el usuario
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Abrir carpeta", str(exc))

    def _exportar(self) -> None:
        if not self._salida:
            return
        if not self._recortes:
            messagebox.showwarning(
                "Exportar",
                "No hay un lote cargado. Carga la carpeta de trabajo antes de exportar.")
            return
        destino = filedialog.askdirectory(title="Exportar aprobados a… (carpeta aparte)")
        if not destino:
            return
        with reloj(self):
            try:
                res = decisiones.exportar_aprobados(
                    self._salida, Path(destino),
                    nombres=set(self._recortes),
                    entrada=self.v_entrada.get().strip() or None)
            except decisiones.ErrorExportacion as exc:
                messagebox.showerror("Exportar: carpeta no válida", str(exc))
                return
            except OSError as exc:
                messagebox.showerror("Exportar", f"No se pudo exportar:\n{exc}")
                return

        detalle = "  ·  ".join(f"{sub}: {n}" for sub, n in res["por_carpeta"].items())
        msg = (f"{res['copiados']} archivo(s) exportado(s) a:\n{res['destino']}\n\n"
               f"{detalle}\n"
               f"{res['aprobados']} recorte(s) aprobado(s) en este lote.")
        if res["omitidos_otros_lotes"]:
            msg += (f"\n\nSe omitieron {res['omitidos_otros_lotes']} aprobado(s) de "
                    "lotes anteriores guardados en esta misma carpeta.")
        if res["faltantes"]:
            msg += f"\n\n{len(res['faltantes'])} archivo(s) aprobado(s) no estaban en disco."
        if res["manifiesto"]:
            msg += f"\n\nRegistro de la exportación:\n{Path(res['manifiesto']).name}"
            self._actualizar_indice_fuente(Path(destino), Path(res["manifiesto"]))
        if res["errores"]:
            messagebox.showerror(
                "Exportar con problemas",
                msg + "\n\nProblemas:\n" + "\n".join(res["errores"][:10]))
        else:
            messagebox.showinfo("Exportar", msg)

    def _raiz_catalogos_probable(self) -> Path | None:
        """De dónde buscar los PDF/Excel originales para el índice de fuente
        (ver `indice_fuente.py`) — no hay una carpeta "de catálogos" fija en
        la app, así que se infiere de lo último que se usó como entrada."""
        if self._entrada_archivos:
            candidato = self._entrada_archivos[0].parent.parent
            if candidato.exists():
                return candidato
        texto = self.v_entrada.get().strip()
        p = Path(texto)
        if p.exists():
            return p if p.is_dir() else p.parent
        if self._salida and self._salida.parent.exists():
            return self._salida.parent
        return None

    def _actualizar_indice_fuente(self, destino: Path, ruta_manifiesto: Path) -> None:
        """Agrega al Excel de índice (dentro de la carpeta exportada) las
        fotos de este export. Las fotos ya se copiaron bien antes de llegar
        acá, así que un fallo aquí no hace fallar la exportación — pero SÍ
        se avisa (antes quedaba en silencio, y el índice se veía "completo"
        cuando en realidad le faltaban filas, ej. por tener el Excel abierto
        en otro programa al momento de exportar)."""
        raiz = self._raiz_catalogos_probable()
        if not raiz:
            messagebox.showwarning(
                "Índice de fuente no actualizado",
                "Las fotos se exportaron bien, pero no pude adivinar dónde están "
                "los catálogos (PDF/Excel/correo) para buscar el origen de cada "
                "una, así que «indice_fuente_imagenes.xlsx» no se actualizó con "
                "las de este lote.\n\nPodés pedirme que lo actualice a mano.")
            return
        try:
            manifiesto = json.loads(ruta_manifiesto.read_text(encoding="utf-8"))
            indice_fuente.actualizar_indice(destino, manifiesto, raiz)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(
                "Índice de fuente no actualizado",
                f"Las fotos se exportaron bien, pero no se pudo actualizar "
                f"«indice_fuente_imagenes.xlsx» con las de este lote:\n\n{exc}\n\n"
                f"Lo más común es que el archivo esté abierto en Excel — "
                f"cerralo y volvé a exportar, o pedime que lo actualice a mano.")

    # ── visor individual por calzado, desde el resumen final ────────────


    def _refrescar_miniatura_resumen(self, nombre: str) -> None:
        """Vuelve a leer el png actual del recorte y actualiza su miniatura y
        etiqueta de decisión en el panel de resumen, sin reconstruir todo el
        panel (se usa después de revertir la limpieza automática)."""
        widgets = self._widgets_resumen_recorte.get(nombre)
        r = self._recortes.get(nombre)
        if not widgets or not r:
            return
        lbl_rec, lbl_dec = widgets
        png = r.get("png")
        if png and Path(png).exists():
            try:
                im = self._componer_sobre_gris(Image.open(png))
                im.thumbnail((84, 84))
                foto = ImageTk.PhotoImage(im)
                self._fotos_resumen.append(foto)  # mantener referencia viva
                lbl_rec.configure(image=foto)
            except Exception:  # noqa: BLE001
                pass
        decision = r.get("estado", {}).get("decision", "pendiente")
        color_dec = {"aprobado": COLOR_OK, "descartado": COLOR_MAL}.get(decision, COLOR_TEXTO_SUAVE)
        lbl_dec.configure(text=decision, fg=color_dec)

    def _abrir_visor_recorte(self, nombre: str) -> None:
        """Ventana de detalle de un calzado del resumen: verlo a tamaño real,
        copiar/guardar el PNG, o revertir la limpieza automática aplicada."""
        r = self._recortes.get(nombre)
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        if not png.exists():
            messagebox.showerror("Ver calzado", f"No encuentro el archivo:\n{png}")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(nombre)
        ventana.configure(fg_color=PAR_FONDO)

        cuerpo = Marco(ventana, padding=12)
        cuerpo.pack(fill="both", expand=True)

        Etiqueta(cuerpo, text=nombre, style="Titulo.TLabel").pack(anchor="w", fill="x")

        # tamaño real, o lo más grande que quepa en la pantalla, con scroll
        try:
            im_original = Image.open(png)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver calzado", f"No pude abrir la imagen: {exc}")
            ventana.destroy()
            return

        # Tamaño fijo chico, no "toda la pantalla menos un margen" — así
        # siempre entra completa sin tener que mover la ventana. El detalle
        # fino se ve con el botón de líneas o exportando/copiando la imagen
        # real (que sí queda a resolución completa en el archivo).
        ANCHO_MAX, ALTO_MAX = 560, 460

        marco_img = ctk.CTkFrame(cuerpo, fg_color=PAR_VISOR_BG, corner_radius=RADIO_PANEL)
        marco_img.pack(fill="both", expand=True, pady=(8, 8))
        lbl_imagen_visor = tk.Label(marco_img, background=COLOR_VISOR_BG,
                                    borderwidth=0, highlightthickness=0)
        lbl_imagen_visor.pack(padx=8, pady=8)

        v_estado_local = tk.StringVar(value="")
        Etiqueta(cuerpo, textvariable=v_estado_local, style="Suave.TLabel").pack(anchor="w", fill="x")

        estado_visor = {"con_lineas": False}

        def mostrar(im_pil: Image.Image) -> None:
            vista = im_pil.copy()
            vista.thumbnail((ANCHO_MAX, ALTO_MAX))
            foto = ImageTk.PhotoImage(vista)
            self._fotos_visor_recorte[nombre] = foto
            lbl_imagen_visor.configure(image=foto)

        def imagen_actual_para_mostrar() -> Image.Image:
            png_actual = Path(r["png"])
            if estado_visor["con_lineas"]:
                try:
                    return reparar.dibujar_contorno(png_actual, color_basura=reparar.COLOR_BASURA)
                except Exception as exc:  # noqa: BLE001
                    v_estado_local.set(f"No se pudieron calcular las líneas: {exc}")
            return self._componer_sobre_gris(Image.open(png_actual))

        def refrescar_vista() -> None:
            mostrar(imagen_actual_para_mostrar())
            estado_recorte = self._recortes.get(nombre, {}).get("estado", {})
            btn_lineas.state(["!disabled"])
            # Siempre habilitados — la detección automática (tiene_excedente/
            # mordida_real) es una ayuda, no un permiso; falla en casos
            # reales (ej. una mordida real en el talon que la curva no ve).
            btn_recortar.state(["!disabled"])
            btn_reparar_suela.state(["!disabled"])
            tiene_original = bool(self._salida and self._ruta_original(nombre))
            btn_alternar.state(["!disabled"] if tiene_original else ["disabled"])
            btn_revertir.state(["!disabled"] if tiene_original else ["disabled"])

        def copiar() -> None:
            if not _CLIPBOARD_DISPONIBLE:
                v_estado_local.set("Copiar al portapapeles no está disponible en este equipo — "
                                    "usá \"Guardar como…\".")
                return
            try:
                self._copiar_png_al_portapapeles(Path(r["png"]))
                v_estado_local.set("Imagen copiada al portapapeles.")
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo copiar: {exc}")

        def guardar_como() -> None:
            destino = filedialog.asksaveasfilename(
                title="Guardar imagen como…", defaultextension=".png",
                initialfile=f"{Path(nombre).stem}.png", filetypes=[("PNG", "*.png")])
            if not destino:
                return
            try:
                import shutil
                shutil.copyfile(r["png"], destino)
                v_estado_local.set(f"Guardado en {destino}")
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo guardar: {exc}")

        def alternar_lineas() -> None:
            estado_visor["con_lineas"] = not estado_visor["con_lineas"]
            btn_lineas.configure(
                text="Ver recorte normal" if estado_visor["con_lineas"] else "Ver líneas azul/roja")
            mostrar(imagen_actual_para_mostrar())

        def alternar_vista() -> None:
            ruta_original = self._ruta_original(nombre)
            if not ruta_original:
                return
            mostrando_original = estado_visor.get("mostrando_original", False)
            try:
                origen = Path(r["png"]) if mostrando_original else ruta_original
                mostrar(self._componer_sobre_gris(Image.open(origen)))
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo mostrar: {exc}")
                return
            estado_visor["mostrando_original"] = not mostrando_original
            btn_alternar.configure(
                text="Ver con limpieza aplicada" if not mostrando_original else "Ver original (antes)")
            v_estado_local.set("Mostrando: " + ("original, antes de la limpieza" if not mostrando_original
                                                else "actual, con la limpieza aplicada"))

        def revertir() -> None:
            ok = reparar.revertir(self._salida, Path(nombre).stem)
            if not ok:
                v_estado_local.set("No hay una versión anterior guardada para revertir.")
                return
            v_estado_local.set("Limpieza automática revertida — vuelve al recorte original.")
            self._refrescar_miniatura_resumen(nombre)
            estado_visor["mostrando_original"] = False
            btn_alternar.configure(text="Ver original (antes)")
            refrescar_vista()

        def recortar_sombra() -> None:
            v_estado_local.set("Recortando sombra/excedente…")
            ventana.configure(cursor="watch")
            ventana.update_idletasks()
            try:
                res = reparar.aplicar_recorte_excedente(self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo recortar sombra: {exc}")
                return
            finally:
                ventana.configure(cursor="")
            v_estado_local.set(f"Sombra recortada ({res['px_recortados']}px).")
            self._refrescar_miniatura_resumen(nombre)
            refrescar_vista()

        def completar_suela() -> None:
            png_actual = Path(r["png"])
            v_estado_local.set("Reparando suela… (calculando la curva y rellenando)")
            ventana.configure(cursor="watch")
            ventana.update_idletasks()
            try:
                res_reparo = reparar.reparar(png_actual, color_basura=reparar.COLOR_BASURA, forzar=True)
                reparar.aplicar(self._salida, Path(nombre).stem, res_reparo)
            except ValueError as exc:
                v_estado_local.set(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo reparar la suela: {exc}")
                return
            finally:
                ventana.configure(cursor="")
            v_estado_local.set(f"Suela reparada ({res_reparo.px_rellenados}px rellenados).")
            self._refrescar_miniatura_resumen(nombre)
            refrescar_vista()

        fila_botones = Marco(cuerpo)
        fila_botones.pack(fill="x", pady=(6, 2))
        btn_lineas = Boton(fila_botones, text="Ver líneas azul/roja", command=alternar_lineas)
        btn_lineas.pack(side="left")
        btn_recortar = Boton(fila_botones, text="Recortar sombra", command=recortar_sombra)
        btn_recortar.pack(side="left", padx=(8, 0))
        btn_reparar_suela = Boton(fila_botones, text="Reparar suela", command=completar_suela)
        btn_reparar_suela.pack(side="left", padx=(8, 0))

        def reparar_y_recortar() -> None:
            completar_suela()
            recortar_sombra()

        Boton(fila_botones, text="Reparar suela + sombra (los dos)",
              command=reparar_y_recortar).pack(side="left", padx=(8, 0))

        fila_botones2 = Marco(cuerpo)
        fila_botones2.pack(fill="x", pady=(8, 0))
        btn_copiar = Boton(fila_botones2, text="Copiar imagen", command=copiar)
        btn_copiar.pack(side="left")
        if not _CLIPBOARD_DISPONIBLE:
            btn_copiar.state(["disabled"])
        Boton(fila_botones2, text="Guardar como…", command=guardar_como).pack(
            side="left", padx=(8, 0))
        btn_alternar = Boton(fila_botones2, text="Ver original (antes)", command=alternar_vista)
        btn_alternar.pack(side="left", padx=(8, 0))
        btn_revertir = Boton(fila_botones2, text="Revertir cambios automáticos",
                             command=revertir)
        btn_revertir.pack(side="left", padx=(8, 0))

        def aprobar_desde_visor() -> None:
            # `_decidir` es el mismo camino que usa el boton "Aprobar" de la
            # grilla; reemplaza al viejo `_aprobar_sin_limpiar` (hacia lo
            # mismo) y ya trae la guarda de no escribir una decision sobre un
            # recorte que se desecho mientras este visor seguia abierto.
            if not self._decidir("aprobado", nombre):
                v_estado_local.set(f"{nombre}: ya no existe, no se puede aprobar.")
                return
            v_estado_local.set(f"{nombre}: aprobado.")

        Boton(fila_botones2, text="✓ Aprobar", style="Aprobar.TButton",
              command=aprobar_desde_visor).pack(side="left", padx=(8, 0))

        def desechar_desde_visor() -> None:
            # Si el borrado se confirma, esta ventana queda mostrando un
            # archivo que ya no existe: se cierra sola.
            if self._desechar_definitivamente(nombre):
                ventana.destroy()

        # Separado de "Aprobar" con el doble de aire (24px contra 8px) y con
        # contorno rojo oscuro en vez de relleno: es el único botón de esta
        # ventana que borra algo del disco.
        Boton(fila_botones2, text="🗑 Desechar definitivamente",
              style="Peligro.TButton",
              command=desechar_desde_visor).pack(side="left", padx=(24, 0))
        Boton(fila_botones2, text="Cerrar", command=ventana.destroy).pack(side="right")

        refrescar_vista()
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        # Mismo problema de apilamiento que `_mostrar_imagen_flotante` (ver la
        # nota larga allá): esta ventana también nace por un clic en una
        # miniatura del paso 5 y quedaba detrás de la principal maximizada.
        _traer_al_frente(ventana)

    @staticmethod
    def _copiar_png_al_portapapeles(png: Path) -> None:
        """Copia el PNG al portapapeles de Windows como bitmap (DIB), para
        pegarlo directo en Word/Outlook/Excel/etc. El portapapeles clásico de
        Windows no soporta transparencia real en CF_DIB, así que se compone
        sobre blanco para el bitmap que ve el usuario al pegar."""
        im = Image.open(png).convert("RGBA")
        fondo = Image.new("RGB", im.size, (255, 255, 255))
        fondo.paste(im, mask=im.split()[3])
        salida = _io.BytesIO()
        fondo.save(salida, "BMP")
        datos_bmp = salida.getvalue()[14:]  # el DIB no lleva el header de archivo BMP (14 bytes)
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, datos_bmp)
        finally:
            win32clipboard.CloseClipboard()


CATEGORIAS_CONOCIDAS = ["Sandalias", "Casual", "Cuñas y plataformas", "Deportivos",
    "Botas y botines", "Tacones", "Formal", "Futbol", "Escolar"]
# Mismo listado que CATEGORIAS_CONOCIDAS en asistente_compras_pty.html -- "Escolar"
# incluida a propósito aunque el nearest-centroid nunca pueda sugerirla (no hay
# ejemplos en el histórico de ventas TUC para construir su prototipo), para que
# el comprador pueda etiquetarla a mano igual.
GENEROS_CONOCIDOS = ["dama", "caballero", "infantil", "junior", "unisex"]

# ---------------------------------------------------------------------------
# Precio de venta sugerido a partir del costo del proveedor
#
# Misma fórmula que ya usa TU Calzado en el Asistente de Compras TEC
# (`AsistenteComprasTEC/src/config.py`), adaptada a este contexto: acá el
# proveedor es NUEVO, así que no existe el concepto de "oferta"/descuento TEC.
#
#   costo_crc    = costo × tipo_cambio  (si el proveedor cotiza en moneda
#                                        extranjera: dólares o yuanes)
#                = costo               (si ya cotiza en colones)
#   costo_iva    = costo_crc × (1 + IVA)
#   precio_venta = redondear_pvp(costo_iva × margen)
#
# El IVA es una REGLA FISCAL de Costa Rica: se muestra en pantalla pero no se
# puede editar. El margen sí, porque es una decisión comercial.
IVA_CR = 0.13
MARGEN_VENTA_DEFAULT = 2.6105

# Monedas en las que puede cotizar un proveedor (2026-09-16: se agrega el YUAN
# chino, porque varios proveedores de TU Calzado son fábricas en China).
# `convierte=True` = hace falta tipo de cambio a colones; TU Calzado vende en
# colones, así que el colón es la única moneda que no necesita conversión.
# Todo el resto del código pregunta acá en vez de comparar contra "USD": ese
# `moneda == "USD"` cableado era justo lo que habría tratado un costo en
# yuanes como si fueran colones.
MONEDAS_PROVEEDOR: dict[str, dict] = {
    "CRC": {"etiqueta": "Colones (₡)", "simbolo": "₡", "plural": "colones (₡)",
            "nombre_mayus": "COLONES", "convierte": False, "ejemplo_tc": ""},
    "USD": {"etiqueta": "Dólares ($)", "simbolo": "$", "plural": "dólares ($)",
            "nombre_mayus": "DÓLARES", "convierte": True, "ejemplo_tc": "ej. 505"},
    "CNY": {"etiqueta": "Yuanes (¥)", "simbolo": "¥", "plural": "yuanes (¥)",
            "nombre_mayus": "YUANES", "convierte": True, "ejemplo_tc": "ej. 70"},
}


def moneda_valida(moneda) -> bool:
    return moneda in MONEDAS_PROVEEDOR


def moneda_convierte(moneda) -> bool:
    """¿Esta moneda necesita tipo de cambio a colones?"""
    return bool(MONEDAS_PROVEEDOR.get(moneda, {}).get("convierte"))


def _rasgo_moneda(moneda, rasgo: str, si_falta: str = "") -> str:
    return MONEDAS_PROVEEDOR.get(moneda, {}).get(rasgo, si_falta)


def redondear_pvp(precio: float) -> float:
    """Al ₡100 más cercano -- mismo `redondear_pvp` que AsistenteComprasTEC.
    TU Calzado no publica precios con decimales ni con dígitos sueltos."""
    return round(precio / 100) * 100


def calcular_precio_venta(costo, moneda: str, tipo_cambio: float | None,
                          margen: float = MARGEN_VENTA_DEFAULT) -> dict | None:
    """Devuelve el desglose COMPLETO del cálculo, no solo el número final: la
    pantalla muestra cada paso (tarea "mostrar la fórmula, editable") y para
    eso necesita los intermedios, no solo el resultado.

    None si no hay con qué calcular: sin costo, con una moneda que no se
    conoce, o en moneda extranjera (dólares/yuanes) sin tipo de cambio. Igual
    que `_precio_venta_estimado` en `servidor_pty.py`, acá se ABSTIENE en vez
    de asumir -- un costo en dólares (o en yuanes) tratado como colones da un
    precio de venta absurdo."""
    try:
        costo = float(costo)
    except (TypeError, ValueError):
        return None
    if costo <= 0:
        return None
    if not moneda_valida(moneda):
        return None
    if moneda_convierte(moneda):
        if not tipo_cambio or float(tipo_cambio) <= 0:
            return None
        costo_crc = costo * float(tipo_cambio)
    else:
        costo_crc = costo
    monto_iva = costo_crc * IVA_CR
    costo_iva = costo_crc + monto_iva
    bruto = costo_iva * float(margen)
    return {
        "costo_origen": costo,
        "moneda": moneda,
        "tipo_cambio": float(tipo_cambio) if tipo_cambio else None,
        "costo_crc": costo_crc,
        "monto_iva": monto_iva,
        "costo_iva": costo_iva,
        "margen": float(margen),
        "precio_sin_redondear": bruto,
        "precio_venta": redondear_pvp(bruto),
    }


FUENTES_PRECIO_LEGIBLE = {
    "manual": "fijado a mano",
    "costo_proveedor": "costo del proveedor + IVA + margen",
    "similares_tuc": "promedio de similares ya vendidos",
    "comparables_marca": "comparables de marca reconocida",
    "similares_marca_vendidos": "promedio de comparables de TC Marcas ya vendidos",
    "mediana_categoria": "mediana de la categoría",
}

# Dos estimaciones de ORIGEN DISTINTO que antes se rotulaban igual (queja real
# del usuario): una sale del costo que cotizó ESTE proveedor, la otra de lo que
# TU Calzado ya vende en productos parecidos. Nunca deben leerse como si fueran
# lo mismo, así que el rótulo lo decide la fuente real del número.
ROTULO_PRECIO_POR_COSTO = "Precio de venta estimado por costo y margen real"
ROTULO_PRECIO_COMPARABLES = "Precio de venta de comparables internos encontrados"


def texto_precio_referencia(precio, fuente: str | None,
                            por_costo_lote: bool = False) -> str:
    """La línea de precio de referencia de una tarjeta, ya rotulada según de
    dónde salió el número.

    `por_costo_lote=True` = el precio guardado como "manual" en realidad lo
    calculó ESTA app a partir del costo que el comprador escribió a mano, con
    el tipo de cambio y el margen del lote (ver `_guardar_costo_candidato`).
    Sin esta distinción diría "fijado a mano", que es engañoso: el comprador
    escribió un COSTO, no un precio de venta."""
    if not precio:
        return f"{ROTULO_PRECIO_COMPARABLES}: sin estimar (sin comparables ni costo declarado)"
    if fuente == "costo_proveedor" or (fuente == "manual" and por_costo_lote):
        detalle = ("costo ingresado por el comprador + IVA + margen del lote"
                   if por_costo_lote else FUENTES_PRECIO_LEGIBLE["costo_proveedor"])
        return f"{ROTULO_PRECIO_POR_COSTO}: ₡{precio:,.0f} ({detalle})"
    if fuente == "manual":
        return f"Precio de venta fijado a mano: ₡{precio:,.0f}"
    return (f"{ROTULO_PRECIO_COMPARABLES}: ₡{precio:,.0f} "
            f"({FUENTES_PRECIO_LEGIBLE.get(fuente, fuente)})")


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
        pass
    try:
        yield
    finally:
        try:
            widget.configure(cursor="")
        except tk.TclError:
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
            return
        ventana.after(200, _paso2)

    def _paso2() -> None:
        try:
            ventana.attributes("-topmost", False)
        except tk.TclError:
            pass

    try:
        ventana.after(80, _paso1)
    except tk.TclError:
        pass


def _cargar_foto_generico(ruta: Path | None, tam=(90, 90)) -> ImageTk.PhotoImage | None:
    if ruta is None or not ruta.exists():
        return None
    try:
        im = Image.open(ruta).convert("RGBA")
        im.thumbnail(tam)
        return ImageTk.PhotoImage(im)
    except Exception:  # noqa: BLE001
        return None


# ── color legible en las tarjetas de candidatos ──────────────────────────
# El nombre del color venía como texto plano ("NEGRO", "BLANCO", "AMARILLO")
# con el gris de "Suave.TLabel": sobre el panel blanco, los claros no se leían
# y ninguno se distinguía de un dato más. Ahora cada color se muestra como una
# pastilla PINTADA con ese color, y el texto de adentro se elige por luminancia
# (negro sobre claro, blanco sobre oscuro) -- así es legible con cualquier
# fondo, sin una lista de excepciones a mano.
_HEX_POR_COLOR = {
    # Acromáticos y metálicos
    "NEGRO": "#111111", "BLANCO": "#f5f5f5", "GRIS": "#9aa0a6",
    "GRIS CLARO": "#cfd4d9", "GRIS OSCURO": "#5f6368", "GRAFITO": "#4a4f54",
    "PLATEADO": "#c0c4c8", "PLATA": "#c0c4c8", "DORADO": "#d4af37",
    "ORO": "#d4af37", "BRONCE": "#a97142", "COBRE": "#b06a3b",
    # Neutros cálidos
    "BEIGE": "#e8dcc0", "CREMA": "#f2e8d5", "HUESO": "#f0eade",
    "MARFIL": "#f4efe2", "ARENA": "#dfcfae", "CAMEL": "#c19a6b",
    "TAUPE": "#8b7d70", "TIERRA": "#8a5a3b", "NUDE": "#e6c7b0",
    "CAFE": "#6f4e37", "MARRON": "#6f4e37", "CHOCOLATE": "#4a2c1a",
    "CAFE CLARO": "#a9764f", "CAFE OSCURO": "#4a2c1a",
    # Rojos / rosados / morados
    "ROJO": "#c62828", "VINO": "#7b1d2b", "BURDEO": "#7b1d2b",
    "BURDEOS": "#7b1d2b", "GUINDA": "#7b1d2b", "CORAL": "#f4796b",
    "SALMON": "#f59a86", "ROSADO": "#f28ab2", "ROSA": "#f28ab2",
    "PALO DE ROSA": "#d99a9a", "FUCSIA": "#d81b7a", "MAGENTA": "#c2186b",
    "MORADO": "#6a3fa0", "PURPURA": "#6a3fa0", "VIOLETA": "#7d5fb2",
    "LILA": "#b39ddb", "UVA": "#5b3a86",
    # Azules / verdes
    "AZUL": "#1f5fb4", "AZUL MARINO": "#1b2a53", "MARINO": "#1b2a53",
    "AZUL OSCURO": "#173a6b", "AZUL CLARO": "#7fb2e5", "CELESTE": "#7fc4e8",
    "PETROLEO": "#1f5460", "ACERO": "#5c7a99", "TURQUESA": "#33b1b1",
    "AQUA": "#5fd0d0", "MENTA": "#9fe3c5", "VERDE": "#2e7d32",
    "VERDE CLARO": "#8bc34a", "VERDE OSCURO": "#1b5e20", "LIMON": "#c9dd3b",
    "OLIVA": "#6b7b3a", "KAKI": "#8f8b55", "MILITAR": "#6b7b3a",
    # Amarillos / naranjas
    "AMARILLO": "#f4d03f", "MOSTAZA": "#d4a017", "NARANJA": "#e8711a",
    "MANDARINA": "#f08a3c", "OCRE": "#c98b2e",
    # Especiales
    "MULTICOLOR": "#9aa0a6", "VARIOS": "#9aa0a6", "ESTAMPADO": "#9aa0a6",
    "ANIMAL PRINT": "#c8a165", "TRANSPARENTE": "#e3e8ec", "NATURAL": "#e0d5bf",
}

# Con qué se separan los colores de un nombre compuesto. La COMA es la que
# faltaba y la que más importa: `colores_detectados` viene de la base como
# "negro, gris" / "blanco, nude, rojo", y sin la coma en esta lista el primer
# trozo quedaba "NEGRO," (con coma pegada), no matcheaba en el diccionario, y
# la pastilla terminaba pintada con el SEGUNDO color -- "negro, gris" se veía
# gris. Ese era el "el color no se detecta bien" que reportó el usuario.
_SEP_COLOR = re.compile(r"\s*(?:,|/|\||\+|&|-|\bY\b|\bCON\b)\s*")

# Tildes fuera y plural fuera: el dato llega en minúsculas sin tilde desde la
# base ("marron"), pero también a mano desde el catálogo del proveedor
# ("Marrón", "Cafés", "AZULES").
_SIN_TILDE = str.maketrans("ÁÉÍÓÚÜÀÈÌÒÙÂÊÎÔÛÃÕÑ", "AEIOUUAEIOUAEIOUAON")


def _normalizar_color(nombre) -> str:
    """"Café ", "MARRÓN", "marron" → "MARRON". Sin esto, cada variante de
    escritura del mismo color caía al gris neutro."""
    return " ".join(str(nombre).upper().translate(_SIN_TILDE).split())


def _buscar_hex(clave: str) -> str | None:
    """Un solo nombre (ya normalizado) → hex, o None si no se reconoce.
    Prueba el nombre completo primero para que los compuestos de dos palabras
    ("GRIS OSCURO", "AZUL MARINO") ganen sobre su primera palabra sola, y
    recorta el plural antes de rendirse ("AZULES" → "AZUL")."""
    if not clave:
        return None
    if clave in _HEX_POR_COLOR:
        return _HEX_POR_COLOR[clave]
    singular = clave[:-2] if clave.endswith("ES") else clave.rstrip("S")
    if singular in _HEX_POR_COLOR:
        return _HEX_POR_COLOR[singular]
    # "AZUL REY", "VERDE BOTELLA": la primera palabra reconocible manda.
    for palabra in clave.split():
        if palabra in _HEX_POR_COLOR:
            return _HEX_POR_COLOR[palabra]
        p = palabra[:-2] if palabra.endswith("ES") else palabra.rstrip("S")
        if p in _HEX_POR_COLOR:
            return _HEX_POR_COLOR[p]
    return None


def _colores_de_nombre(nombre) -> list[tuple[str, str]]:
    """Un nombre (simple o compuesto) → [(texto, hex), ...], en el mismo orden
    en que viene escrito. "negro, gris" da DOS pastillas, no una: el dato ya
    trae los dos colores y aplastarlos en una sola pastilla era justamente
    perder la detección que sí existe."""
    clave = _normalizar_color(nombre) if nombre else ""
    if not clave:
        return []
    # El orden importa y es la trampa de este helper: hay que PARTIR primero y
    # solo después buscar. Si se busca el nombre completo antes de partir,
    # `_buscar_hex` cae en su fallback de "primera palabra reconocible" y
    # "NEGRO, GRIS" devuelve una única pastilla gris (el trozo "NEGRO," con la
    # coma pegada no matchea, "GRIS" sí) -- el mismo error que se está
    # arreglando. Solo el nombre exacto de dos palabras ("GRIS OSCURO") puede
    # resolverse sin partir, y esos no llevan separador.
    if not _SEP_COLOR.search(clave):
        return [(clave.capitalize(), _buscar_hex(clave) or solido(PAR_BORDE))]
    salida: list[tuple[str, str]] = []
    for parte in _SEP_COLOR.split(clave):
        parte = parte.strip()
        if not parte:
            continue
        salida.append((parte.capitalize(), _buscar_hex(parte) or solido(PAR_BORDE)))
    return salida or [(clave.capitalize(), solido(PAR_BORDE))]


def _hex_de_color(nombre: str | None) -> str:
    """Hex con el que pintar la pastilla de un color por su nombre. Si el
    nombre no está en la lista (o no hay nombre), se usa un gris neutro: nunca
    se inventa un color que podría ser el equivocado."""
    colores = _colores_de_nombre(nombre)
    return colores[0][1] if colores else solido(PAR_BORDE)


def _texto_sobre(hex_fondo: str) -> str:
    """Negro o blanco, el que contraste con ese fondo. Luminancia relativa
    (misma fórmula que usa WCAG para decidir contraste), no "los claros son
    los que empiezan con f"."""
    try:
        h = hex_fondo.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#000000"

    def canal(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)
    return "#000000" if lum > 0.36 else "#ffffff"


_SUFIJO_INTERNO = re.compile(r"_\d+$")


def codigo_visible(codigo) -> str:
    """La referencia TAL COMO LA ESCRIBE EL PROVEEDOR, sin el sufijo que este
    programa le agrega por dentro (2026-09-17, pedido del dueño: "la referencia
    debería ser FOR-025, no FOR-025_3").

    De dónde sale el sufijo: el proveedor manda UNA referencia con varias fotos
    (una por color) dentro del mismo Excel, y la extracción las guarda con un
    correlativo para que no se pisen (`FOR-025.jpg`, `FOR-025_2.jpg`, ...). El
    código del candidato se arma con el nombre del archivo, así que arrastra el
    correlativo. Es un detalle de archivo, no algo que exista en el catálogo
    del proveedor ni en la orden de compra.

    IMPORTANTE: esto es SOLO para mostrar. El `codigo_proveedor` real se sigue
    usando sin tocar para la clave del candidato, el espejo de costos, la
    ingesta y el cruce contra el lote -- cambiarlo ahí rompería la identidad de
    la referencia y la reutilización entre envíos."""
    texto = str(codigo or "").strip()
    return _SUFIJO_INTERNO.sub("", texto) or texto


def _pastilla_color(padre, nombre: str | None, prefijo: str = ""):
    """Pastilla(s) pintadas con el color, con su nombre legible adentro.

    Devuelve UN widget listo para `.pack()`. Si el nombre es compuesto
    ("negro, gris") devuelve un contenedor con una pastilla por color, en
    orden: así el comprador ve los dos colores que el sistema realmente
    detectó, en vez de un solo nombre aplastado (o, peor, el color equivocado
    como pasaba antes de arreglar el separador de coma)."""
    colores = _colores_de_nombre(nombre)
    if not colores:
        bg = solido(PAR_BORDE)
        return tk.Label(padre, text=f"{prefijo}—", bg=bg, fg=_texto_sobre(bg),
                        font=("Segoe UI Semibold", 10), padx=8, pady=2,
                        borderwidth=1, relief="solid")
    if len(colores) == 1 and not prefijo:
        texto, bg = colores[0]
        return tk.Label(padre, text=texto, bg=bg, fg=_texto_sobre(bg),
                        font=("Segoe UI Semibold", 10), padx=8, pady=2,
                        borderwidth=1, relief="solid")
    caja = Marco(padre)
    if prefijo:
        Etiqueta(caja, text=prefijo, style="Suave.TLabel").pack(side="left", padx=(0, 4))
    for texto, bg in colores:
        tk.Label(caja, text=texto, bg=bg, fg=_texto_sobre(bg),
                 font=("Segoe UI Semibold", 10), padx=8, pady=2,
                 borderwidth=1, relief="solid").pack(side="left", padx=(0, 3))
    return caja


class VentanaCandidatosCTk(ctk.CTkFrame):
    """Pasos 6 y 7 del flujo: lista de candidatos ya calificados (foto, categoría,
    género, color, grado), con acceso nativo a los tres pasos que antes solo
    vivían en la web (`servidor_pty.py` en 127.0.0.1:8900) -- comparables por
    candidato, confirmación manual de categoría/género ambiguos, y PDV
    objetivo → pedido sugerido. Todo llama directo a las mismas funciones de
    `servidor_pty.py` que usa la web (vía `motor_candidatos.py`), sin HTTP.

    Plan 2026-09-07, paso 8: dejó de ser un `CTkToplevel` (una ventana aparte
    que tapaba la app y, al cerrarla, solo se recuperaba reenviando el
    catálogo completo a calificar) y pasó a ser una PANTALLA dentro de la
    ventana principal, con el mismo mecanismo de pack/pack_forget que ya usan
    el resumen final y la selección de limpieza. El nombre de la clase se
    conserva para no romper las referencias existentes."""

    def __init__(self, master: tk.Misc, candidatos: list[dict], al_volver=None) -> None:
        super().__init__(master, fg_color="transparent")
        self._fotos_tk: list[ImageTk.PhotoImage] = []  # referencias vivas
        self._candidatos = candidatos
        self._al_volver = al_volver

        import motor_candidatos
        self._mc = motor_candidatos

        # Moneda / tipo de cambio / margen declarados PARA ESTE LOTE (metadatos
        # locales de la carpeta de trabajo, ver `config_precio_lote` en la
        # ventana principal). Si esta pantalla se construyó suelta (pruebas),
        # se cae a los defaults sin reventar.
        leer_cfg = getattr(master, "config_precio_lote", None)
        self._cfg_precio = (leer_cfg() if callable(leer_cfg)
                            else {"moneda": None, "tipo_cambio": None,
                                  "margen": MARGEN_VENTA_DEFAULT})
        # FASE 3 multi-proveedor (2026-09-16): la configuración de precio es
        # POR PROVEEDOR. `self._cfg_precio` sigue siendo la del proveedor
        # activo (y la única que se usa en un lote de un solo proveedor, donde
        # todo funciona exactamente como antes); `self._cfgs_por_proveedor`
        # tiene la de cada proveedor del lote, y es la que consulta cada
        # tarjeta según el `proveedor_id` de SU candidato (ver `_cfg_de`).
        leer_cfgs = getattr(master, "configs_precio_por_proveedor", None)
        self._cfgs_por_proveedor: dict = (leer_cfgs() if callable(leer_cfgs) else {})
        # Nombre de cada proveedor, para rotular su fila de moneda/margen
        # cuando el lote trae más de uno.
        self._nombres_proveedor: dict = {}
        for c in candidatos:
            pid = c.get("proveedor_id")
            if pid is not None and c.get("proveedor"):
                self._nombres_proveedor[int(pid)] = str(c["proveedor"])
        # Variables del campo «Margen de venta ×», una por proveedor.
        self._v_margenes: dict = {}
        # Cada tarjeta registra acá una función que repinta su propio desglose
        # de precio: así, cambiar el margen una vez arriba recalcula TODAS las
        # tarjetas en vivo, sin reconstruir la pantalla (que perdería el scroll
        # y volvería a consultar la base para cada candidato).
        self._repintar_precio: list = []
        # Tarjetas registradas para el botón único «💾 Guardar lote» (Tarea 5,
        # 2026-09-10). Cada entrada sabe leer su propio campo de costo y decir
        # si cambió respecto de lo persistido -- ver `_bloque_precio_costo`.
        self._pendientes_costo: list[dict] = []

        # Espejo LOCAL de los costos ingresados a mano (ver el bloque
        # "respaldo LOCAL de los costos" en la ventana principal). Se carga
        # una sola vez acá y se mantiene en memoria; cada cambio lo vuelca
        # completo a `lote.sqlite`.
        leer_costos = getattr(master, "leer_costos_lote", None)
        self._costos_lote: dict = (leer_costos() if callable(leer_costos) else {})
        self._costos_restaurados = 0

        # Espejo LOCAL de las correcciones de tipo/género (misma idea que los
        # costos, ver `leer_categorias_lote` en la ventana principal).
        leer_cats = getattr(master, "leer_categorias_lote", None)
        self._categorias_lote: dict = (leer_cats() if callable(leer_cats) else {})
        self._categorias_restauradas = 0

        encabezado = Marco(self)
        encabezado.pack(fill="x", padx=12, pady=(4, 0))
        # El botón "← Volver a revisar fotos" salió de acá (2026-09-10, pedido
        # del usuario): el pie del wizard ya trae un "Atrás" estándar en TODAS
        # las pantallas, así que este era un segundo camino para lo mismo,
        # ocupando el lugar más visible del encabezado. `self._volver` sigue
        # existiendo (lo llama el anfitrión del wizard).
        Etiqueta(encabezado, text=f"{len(candidatos)} candidato(s) calificado(s)",
                 style="Subtitulo.TLabel").pack(side="left")
        # Qué catálogo es esto, integrado a la barra que ya existía en vez de
        # la franja aparte de dos renglones que había abajo (2026-09-10): el
        # proveedor y el nombre del catálogo SÍ hacen falta (evitan revisar sin
        # saber si se mezclaron dos catálogos), la frase "Ordenados por
        # calificación..." no -- el orden se ve solo.
        self._etq_catalogo = Etiqueta(encabezado, text="", style="Suave.TLabel")
        self._etq_catalogo.pack(side="left", padx=(12, 0))

        # Dos sub-pantallas, no dos pestañas (2026-09-08, pedido del usuario):
        # el sugerido de compra es el PASO FINAL del asistente, no una pestaña
        # escondida al costado de los candidatos. Cuál se ve la decide el paso
        # del asistente (6 = candidatos, 7 = sugerido) vía `mostrar_*`.
        self._vista_candidatos = Marco(self)
        self._vista_optimizador = Marco(self)
        self._construir_tab_candidatos(self._vista_candidatos)
        self._construir_tab_optimizador(self._vista_optimizador)
        self.mostrar_candidatos()

        # Si hubo que reponer tipo/género desde el respaldo, la lista que se
        # acaba de pintar quedó con los scores VIEJOS (el re-scoring corrió en
        # la base recién ahora, y el score depende del filtro categoría+género).
        # Se recarga una vez, en cuanto la pantalla está armada. La instancia
        # nueva no encontrará nada que reponer, así que no hay ciclo.
        if self._categorias_restauradas:
            self.after_idle(self._recargar)

    # ---------- qué sub-pantalla se ve (la manda el paso del asistente) -----

    def _mostrar_solo(self, vista) -> None:
        for otra in (self._vista_candidatos, self._vista_optimizador):
            if otra is not vista and otra.winfo_manager():
                otra.pack_forget()
        if not vista.winfo_manager():
            # Márgenes más ajustados (2026-09-09): la lista de candidatos es la
            # pantalla donde más falta hace el espacio vertical.
            vista.pack(fill="both", expand=True, padx=8, pady=(2, 6))

    def mostrar_candidatos(self) -> None:
        self._mostrar_solo(self._vista_candidatos)

    def mostrar_optimizador(self) -> None:
        self._mostrar_solo(self._vista_optimizador)
        # Señal de "estás en el último paso" desde el momento en que se entra,
        # no solo después de generar: si todavía no hay selección, se dice qué
        # falta para cerrar el proceso.
        if not getattr(self, "_ultimo_sugerido", None):
            self._pintar_cierre_pendiente()

    def _volver(self) -> None:
        if self._al_volver is not None:
            self._al_volver()
        else:  # construida suelta (pruebas): al menos se saca de la pantalla
            self.destroy()

    # ---------- pestaña Candidatos ----------

    def _construir_tab_candidatos(self, tab) -> None:
        candidatos = self._candidatos
        # Antes de leer cualquier precio de la base: reponer los costos que el
        # comprador ya había ingresado para este lote y que staging_tuc pudo
        # haber perdido (es efímero y compartido). Así las tarjetas se pintan
        # una sola vez, ya con los precios correctos.
        self._restaurar_costos_del_respaldo()
        self._restaurar_categorias_del_respaldo()
        self._autodetectar_marcas()
        # El titulazo "N candidato(s) calificado(s)" estaba DOS veces: acá y en
        # el encabezado de la pantalla, tres líneas más arriba. Se quita el
        # duplicado (2026-09-09) -- eran ~50px de alto que le faltaban a la
        # lista de tarjetas, que es lo que el comprador vino a mirar.
        # Encabezado del catálogo activo (plan 2026-09-07, paso 1): antes no
        # había forma de saber, mirando esta pantalla, si lo que se ve es UN
        # catálogo o varios mezclados sin querer.
        # 2026-09-10: esto eran DOS renglones propios de franja ("Catálogo:
        # ..." + "Ordenados por calificación..."), ~50px que el comprador no
        # usaba para decidir nada. El dato que sí importa (proveedor, catálogo
        # y cuántos) se comprime a una línea y se cuelga de la barra superior
        # que ya existía (`self._etq_catalogo`, ver `__init__`).
        resumen = self._mc.resumen_catalogo_activo()
        texto_resumen = " · ".join(f"{f['proveedor']} — {f['catalogo_origen']} ({f['n']})"
                                    for f in resumen) or "sin catálogo identificado"
        try:
            self._etq_catalogo.configure(text=f"·  {texto_resumen}")
        except (AttributeError, tk.TclError):
            pass  # construida suelta (pruebas): el encabezado puede no existir

        self._barra_precio_lote(tab)

        # Los comparables salieron de la columna lateral y volvieron a una
        # ventana propia (2026-09-09, pedido del usuario: "que sea una ventana
        # despegable"). El panel lateral era de ancho FIJO (400px) y alto
        # heredado, y adentro convivían cuatro bloques de alto fijo (título,
        # selector de color, precio, y la tarjeta grande de "Guardar
        # cotización" anclada abajo) más la lista de comparables con expand.
        # Cuando el alto disponible no alcanzaba para todos, `pack` reparte en
        # ORDEN de empaquetado: los de alto fijo se llevaban todo y la lista de
        # comparables --que se empaqueta última-- se quedaba con 0px de alto.
        # Se veía exactamente como lo describió el usuario: los comparables
        # "tapados detrás del cuadro de Guardar cotización". Con ventana propia
        # y redimensionable hay alto de sobra, y adentro se usa `grid` con peso
        # para que la lista NUNCA pueda quedar en cero.
        self._win_comparables: ctk.CTkToplevel | None = None
        self._panel_comparables_contenido = None

        # FASE 4: pastillas «Todos / <proveedor A> / <proveedor B>». Se
        # construye SOLO si el lote es multi-proveedor; con uno solo no se
        # agrega ningún widget (ni un marco vacío).
        self._tarjetas_por_proveedor: list[tuple] = []
        self._btns_filtro: dict = {}
        self._filtro_proveedor = None  # None = todos
        if self._es_multiproveedor():
            self._barra_filtro_proveedor(tab)

        contenedor = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        contenedor.pack(fill="both", expand=True)

        if not candidatos:
            Etiqueta(contenedor, text="No se encontró ningún candidato calificado.",
                    style="Suave.TLabel").pack(padx=8, pady=8)
            return

        for cand in candidatos:
            tarjeta = self._fila_candidato(contenedor, cand)
            pid, _nombre = self._proveedor_de(cand)
            self._tarjetas_por_proveedor.append((pid, tarjeta))

    # ---------- configuración de precio del proveedor de CADA candidato ------

    def _proveedores_en_pantalla(self) -> list:
        """Los `proveedor_id` presentes en los candidatos, en orden de
        aparición. Lista `[None]` si los candidatos no traen proveedor
        (pantalla suelta en pruebas): ese es el camino de un solo juego de
        parámetros, idéntico al de antes de la Fase 3."""
        vistos = []
        for cand in self._candidatos:
            pid = cand.get("proveedor_id")
            pid = int(pid) if pid is not None else None
            if pid not in vistos:
                vistos.append(pid)
        return vistos or [None]

    # ---------- FASE 4: de qué proveedor es cada candidato ------------------
    #
    # Paleta de las pastillas de proveedor. Deliberadamente NO usa el verde de
    # `PAR_OK` ni el rojo de `PAR_MAL`: esos dos ya significan "grado S/A" y
    # "grado C/D" en el badge de score de la misma tarjeta, y reusarlos acá
    # haría leer el proveedor como una calificación.
    # Tampoco usa el ámbar de `PAR_ALERTA` en los dos primeros lugares (que
    # son los que se ven en el caso real de dos proveedores): una pastilla
    # ámbar se lee como una advertencia sobre esa referencia, y acá no
    # advierte nada -- solo dice de quién es.
    PALETA_PROVEEDOR = (
        (PAR_PRIMARIO_SUAVE, PAR_PRIMARIO),
        (PAR_PANEL_SUAVE, PAR_ACENTO),
        (PAR_SELECCION, PAR_TEXTO),
        (PAR_ALERTA_BG, PAR_ALERTA),
    )

    def _es_multiproveedor(self) -> bool:
        """¿Hay candidatos de MÁS DE UN proveedor en esta pantalla?

        Es el interruptor de toda la Fase 4: con un solo proveedor --el caso
        mayoritario-- devuelve False y la pantalla no muestra ni un elemento
        nuevo respecto de la Fase 3 (ni pastilla en las tarjetas, ni barra de
        filtro, ni columna de proveedor en el paso 7)."""
        return len([p for p in self._proveedores_en_pantalla() if p is not None]) > 1

    def _nombre_de_proveedor(self, pid) -> str:
        if pid is None:
            return ""
        return self._nombres_proveedor.get(int(pid), "") or f"Proveedor {pid}"

    def _proveedor_de(self, cand: dict) -> tuple:
        """`(proveedor_id, nombre legible)` de un candidato. El nombre que trae
        el propio candidato manda (es el que devuelve `api_candidatos`); el
        mapa `self._nombres_proveedor` es el respaldo."""
        pid = cand.get("proveedor_id")
        pid = int(pid) if pid is not None else None
        nombre = str(cand.get("proveedor") or "") or self._nombre_de_proveedor(pid)
        return pid, nombre

    def _colores_pastilla_proveedor(self, pid) -> tuple:
        """Color estable por proveedor: el mismo proveedor se ve igual en todas
        las tarjetas y en el paso 7, según su orden de aparición en el lote."""
        orden = [p for p in self._proveedores_en_pantalla() if p is not None]
        try:
            i = orden.index(pid)
        except ValueError:
            i = len(orden)
        return self.PALETA_PROVEEDOR[i % len(self.PALETA_PROVEEDOR)]

    def _pastilla_proveedor(self, padre, cand: dict):
        """La pastilla con el nombre del proveedor del candidato, en el mismo
        espíritu que los badges de grado y las pastillas de color que ya
        existen. Devuelve el widget listo para `.pack()`/`.grid()`."""
        pid, nombre = self._proveedor_de(cand)
        par_bg, par_fg = self._colores_pastilla_proveedor(pid)
        return tk.Label(padre, text=nombre or "sin proveedor",
                        bg=solido(par_bg), fg=solido(par_fg),
                        font=("Segoe UI Semibold", 9), padx=7, pady=1,
                        borderwidth=1, relief="solid")

    # ---------- FASE 4: filtro por proveedor (solo si hay más de uno) -------

    def _barra_filtro_proveedor(self, tab) -> None:
        """Pastillas «Todos / <proveedor A> / <proveedor B>» arriba de la
        grilla, para revisar un catálogo a la vez sin perder la vista conjunta.

        No reconstruye nada: las tarjetas ya están armadas (con su costo
        tecleado, su marca declarada y su desglose de precio) y el filtro
        solo las despacka y las vuelve a packar en orden. Reconstruirlas
        perdería lo que el comprador tenga a medio escribir."""
        barra = Marco(tab)
        barra.pack(fill="x", padx=4, pady=(0, 6))
        Etiqueta(barra, text="Ver:", style="Suave.TLabel").pack(side="left", padx=(0, 6))

        conteos: dict = {}
        for cand in self._candidatos:
            pid, _n = self._proveedor_de(cand)
            conteos[pid] = conteos.get(pid, 0) + 1

        opciones = [(None, f"Todos ({len(self._candidatos)})")]
        for pid in self._proveedores_en_pantalla():
            opciones.append((pid, f"{self._nombre_de_proveedor(pid)} "
                                  f"({conteos.get(pid, 0)})"))
        for pid, rotulo in opciones:
            btn = Boton(barra, text=rotulo, style="Sutil.TButton",
                        width=max(90, 9 * len(rotulo)))
            btn.configure(command=lambda p=pid: self._aplicar_filtro_proveedor(p))
            btn.pack(side="left", padx=(0, 6))
            self._btns_filtro[pid] = btn
        self._pintar_filtro_activo()

    def _pintar_filtro_activo(self) -> None:
        for pid, btn in self._btns_filtro.items():
            try:
                if pid == self._filtro_proveedor:
                    btn.configure(fg_color=PAR_PRIMARIO, border_color=PAR_PRIMARIO,
                                  text_color=("#ffffff", "#ffffff"))
                else:
                    btn.configure(fg_color="transparent", border_color=PAR_BORDE,
                                  text_color=PAR_TEXTO_SUAVE)
            except tk.TclError:
                pass  # barra destruida (la pantalla se recargó)

    def _aplicar_filtro_proveedor(self, pid) -> None:
        self._filtro_proveedor = pid
        self._pintar_filtro_activo()
        for _p, tarjeta in self._tarjetas_por_proveedor:
            try:
                if tarjeta.winfo_manager():
                    tarjeta.pack_forget()
            except tk.TclError:
                pass
        # Se vuelven a packar EN ORDEN: `pack` respeta el orden de llamada, así
        # que la lista mantiene su orden por calificación.
        for p, tarjeta in self._tarjetas_por_proveedor:
            if pid is not None and p != pid:
                continue
            try:
                tarjeta.pack(fill="x", pady=4)
            except tk.TclError:
                pass

    def _cfg_de(self, cand: dict) -> dict:
        """La configuración de precio del proveedor DE ESTE candidato.

        FASE 3: es el corazón del cambio. Antes todas las tarjetas leían
        `self._cfg_precio` (un solo juego de moneda/tipo de cambio/margen para
        el lote entero), así que en un lote con un proveedor en colones y otro
        en dólares una de las dos mitades calculaba mal. Si el candidato no
        trae proveedor, o ese proveedor no tiene configuración propia, se cae a
        `self._cfg_precio` -- el comportamiento de antes."""
        pid = cand.get("proveedor_id")
        if pid is not None:
            cfg = self._cfgs_por_proveedor.get(int(pid))
            if cfg:
                return cfg
        return self._cfg_precio

    def _cfg_de_proveedor(self, pid) -> dict:
        if pid is not None:
            cfg = self._cfgs_por_proveedor.get(int(pid))
            if cfg:
                return cfg
        return self._cfg_precio

    @staticmethod
    def _texto_moneda_cfg(cfg: dict) -> str:
        moneda = cfg.get("moneda")
        if not moneda_valida(moneda):
            return "Moneda del proveedor sin declarar"
        nombre = _rasgo_moneda(moneda, "nombre_mayus", str(moneda))
        if not moneda_convierte(moneda):
            return f"Costo del proveedor en {nombre}  ·  sin conversión"
        simbolo = _rasgo_moneda(moneda, "simbolo", "?")
        if not cfg.get("tipo_cambio"):
            return f"Costo del proveedor en {nombre}  ·  falta el tipo de cambio"
        return (f"Costo del proveedor en {nombre}  ·  tipo de cambio "
                f"₡{cfg['tipo_cambio']:,.2f} por {simbolo}")

    def _barra_precio_lote(self, tab) -> None:
        """Los parámetros con los que se calcula el precio de venta sugerido:
        moneda del proveedor, tipo de cambio y margen.

        FASE 3 (2026-09-16): son POR PROVEEDOR, no del lote entero. Con un solo
        proveedor -- el caso mayoritario -- se ve exactamente la misma fila de
        siempre; con dos catálogos en el mismo lote se ve una fila por
        proveedor, rotulada con su nombre, y cada una manda sobre las tarjetas
        de SUS candidatos.

        El margen es editable y recalcula en vivo el desglose de las tarjetas
        de ese proveedor; el IVA se muestra pero NO se puede editar: es una
        regla fiscal de Costa Rica, no una decisión comercial."""
        tarjeta = Tarjeta(tab)
        tarjeta.pack(fill="x", padx=4, pady=(0, 8))

        proveedores = self._proveedores_en_pantalla()
        rotular = len(proveedores) > 1
        for pid in proveedores:
            self._fila_precio_proveedor(tarjeta, pid, rotular)

        # Se empaqueta SOLO cuando tiene algo que decir: una etiqueta vacía
        # empaquetada deja una franja en blanco adentro de la tarjeta, que es
        # exactamente el "recuadro vacío sin explicación" que se está
        # corrigiendo en el resto de esta pantalla.
        self._estado_margen = Etiqueta(tarjeta, text="", style="Suave.TLabel",
                                       wraplength=900, justify="left")

        # ── EL botón de guardar (uno solo para toda la pantalla) ──
        # 2026-09-10, pedido del usuario: antes cada tarjeta tenía su propio
        # "✓ Guardar costo" y este botón solo forzaba el respaldo local. Ahora
        # este es el ÚNICO que guarda: recorre las 18 tarjetas, persiste las
        # que cambiaron (mismas escrituras que antes, ver `_persistir_costo`) y
        # después vuelca el espejo local una sola vez.
        fila_guardar = Marco(tarjeta)
        fila_guardar.pack(fill="x", padx=10, pady=(0, 8))
        Boton(fila_guardar, text="💾 Guardar lote",
              style="Primario.TButton", width=170,
              command=self._guardar_avances_lote).pack(side="left")
        self._estado_guardado = Etiqueta(fila_guardar, text="", style="Suave.TLabel",
                                         wraplength=620, justify="left")
        self._estado_guardado.pack(side="left", padx=(10, 0))
        if self._costos_restaurados:
            self._decir_guardado(f"Se recuperaron {self._costos_restaurados} costo(s) "
                                 "guardados de este lote.")

    def _fila_precio_proveedor(self, tarjeta, pid, rotular: bool) -> None:
        """Una fila de parámetros de precio: la moneda/tipo de cambio de ESE
        proveedor (con su botón «Cambiar») y su margen editable.

        `rotular=False` (lote de un solo proveedor) deja la fila idéntica a la
        que había antes de la Fase 3, sin el nombre del proveedor adelante."""
        cfg = self._cfg_de_proveedor(pid)
        fila = Marco(tarjeta)
        fila.pack(fill="x", padx=10, pady=(8, 8))

        if rotular:
            nombre = self._nombres_proveedor.get(int(pid), "") if pid is not None else ""
            Etiqueta(fila, text=f"{nombre or f'Proveedor {pid}'}:",
                     style="Subtitulo.TLabel").pack(side="left", padx=(0, 8))

        Etiqueta(fila, text=self._texto_moneda_cfg(cfg),
                 style="Suave.TLabel").pack(side="left")

        Boton(fila, text="Cambiar", style="Sutil.TButton", width=70,
              command=lambda p=pid: self._cambiar_moneda_lote(p)).pack(
            side="left", padx=(10, 20))

        Etiqueta(fila, text=f"IVA {IVA_CR * 100:.0f}% (fijo)",
                 style="Suave.TLabel").pack(side="left", padx=(0, 18))

        Etiqueta(fila, text="Margen de venta ×", style="Suave.TLabel").pack(side="left")
        v_margen = tk.StringVar(value=f"{cfg['margen']:g}")
        self._v_margenes[pid] = v_margen
        if not hasattr(self, "_v_margen"):
            # Alias del primer proveedor: `self._v_margen` es el nombre que ya
            # usaba el resto de la pantalla cuando había un solo margen.
            self._v_margen = v_margen
        ctk.CTkEntry(fila, textvariable=v_margen, width=80).pack(side="left", padx=(6, 6))
        Boton(fila, text="Aplicar", style="Primario.TButton", width=70,
              command=lambda p=pid: self._aplicar_margen(proveedor_id=p)).pack(side="left")
        # Recalcular al teclear, no solo con el botón: es lo que uno espera.
        v_margen.trace_add("write", lambda *_a, p=pid: self._aplicar_margen(
            silencioso=True, proveedor_id=p))

    def _guardar_avances_lote(self) -> None:
        """«💾 Guardar lote»: EL botón de guardar de esta pantalla (2026-09-10).

        Recorre TODAS las tarjetas registradas en `self._pendientes_costo`,
        persiste las que tengan el campo de costo distinto de lo guardado
        (`_persistir_costo`: costo crudo + precio de venta derivado + espejo),
        vuelca el espejo local UNA vez y recién entonces recarga la pantalla
        (que es lo que refresca las líneas de precio de referencia).

        Nada se guarda por tarjeta ni al perder el foco: si el comprador no
        aprieta este botón, el aviso «● sin guardar» de cada tarjeta se lo
        dice. Y si alguna tarjeta falla, se reportan las que fallaron sin
        perder las que sí se guardaron."""
        guardados, fallos = 0, []
        with reloj(self):
            for p in list(self._pendientes_costo):
                try:
                    if not p["hay_cambio"]():
                        continue
                except tk.TclError:
                    continue  # tarjeta destruida (pantalla reconstruida)
                motivo = self._persistir_costo(p["cand"], p["texto"](), p["estado"])
                if motivo:
                    codigo = codigo_visible(p["cand"].get("codigo_proveedor")) or p["cand"]["candidato_id"]
                    fallos.append(f"{codigo}: {motivo}")
                else:
                    guardados += 1
                    p["marcar"]()
            ok = self._volcar_costos_lote()

        n = len(self._costos_lote)
        partes = []
        if guardados:
            partes.append(f"✓ {guardados} costo(s) guardados.")
        elif not fallos:
            partes.append("No había cambios de costo pendientes.")
        if not ok:
            partes.append("⚠ No se pudo escribir el respaldo en la carpeta del lote: los "
                          "costos quedaron en la base de trabajo, revisá la carpeta de "
                          "salida del paso 1.")
        else:
            partes.append(f"Respaldo del lote al día ({n} referencia(s) con costo).")
        if fallos:
            partes.append("No se pudieron guardar: " + "; ".join(fallos))
        self._decir_guardado("  ".join(partes))
        if fallos:
            messagebox.showwarning("Asistente de Compras",
                                   "Algunas referencias no se pudieron guardar:\n\n"
                                   + "\n".join(fallos))
        if guardados:
            # La recarga va AL FINAL y una sola vez: es lo que actualiza la
            # línea de "precio de venta de comparables internos" de cada
            # tarjeta, que sale de la cascada en la base con el costo nuevo.
            self._recargar()

    def _decir_guardado(self, texto: str) -> None:
        """Escribe el aviso de guardado, tolerando que la pantalla se haya
        reconstruido en el medio (guardar un costo dispara `_recargar`, que
        destruye estas etiquetas y crea otras nuevas). El respaldo ya está
        hecho a esa altura: perder el cartelito no puede tumbar la app."""
        try:
            self._estado_guardado.configure(text=texto)
        except tk.TclError:
            pass

    def _decir_margen(self, texto: str) -> None:
        """Muestra el aviso del margen, empaquetándolo la primera vez. Nunca
        deja una etiqueta vacía ocupando espacio (ver `_barra_precio_lote`)."""
        self._estado_margen.configure(text=texto)
        if not self._estado_margen.winfo_manager():
            self._estado_margen.pack(anchor="w", padx=10, pady=(0, 8))

    def _cambiar_moneda_lote(self, proveedor_id=None) -> None:
        """Vuelve a preguntar moneda/tipo de cambio de UN proveedor (para
        corregir un tipo de cambio mal tecleado) y repinta los precios con el
        valor nuevo. FASE 3: la pregunta y el guardado son de ese proveedor, no
        del lote entero."""
        preguntar = getattr(self.master, "asegurar_moneda_lote", None)
        if not callable(preguntar):
            return
        nueva = preguntar(forzar=True, proveedor_id=proveedor_id)
        cambio = {"moneda": nueva.get("moneda"),
                  "tipo_cambio": nueva.get("tipo_cambio")}
        self._cfg_de_proveedor(proveedor_id).update(cambio)
        if proveedor_id is None:
            self._cfg_precio.update(cambio)
        self._recargar()

    def _aplicar_margen(self, silencioso: bool = False, proveedor_id=None) -> None:
        """Relee el margen del campo de UN proveedor y repinta el desglose de
        cada tarjeta (las de ese proveedor cambian; las de los demás se
        repintan con SU propio margen, que no se toca).

        `silencioso=True` es el camino del `trace` (se dispara con cada tecla,
        incluido el estado intermedio "2." mientras se escribe "2.7"): ahí un
        valor a medio teclear no se reporta como error, simplemente no se
        aplica hasta que sea un número válido."""
        var = self._v_margenes.get(proveedor_id) or getattr(self, "_v_margen", None)
        if var is None:
            return
        texto = var.get().strip().replace(",", "")
        try:
            margen = float(texto)
        except ValueError:
            if not silencioso:
                self._decir_margen("Margen inválido.")
            return
        if margen <= 0:
            if not silencioso:
                self._decir_margen("El margen tiene que ser mayor que cero.")
            return
        cfg = self._cfg_de_proveedor(proveedor_id)
        cfg["margen"] = margen
        if proveedor_id is None:
            self._cfg_precio["margen"] = margen
        guardar = getattr(self.master, "guardar_config_precio_proveedor", None)
        if callable(guardar):
            # Queda registrado en los metadatos del lote con qué margen se
            # sugirieron los precios DE ESE proveedor.
            guardar(proveedor_id, cfg["moneda"] or "CRC", cfg["tipo_cambio"], margen)
        else:
            guardar_viejo = getattr(self.master, "guardar_config_precio_lote", None)
            if callable(guardar_viejo):
                guardar_viejo(cfg["moneda"] or "CRC", cfg["tipo_cambio"], margen)
        nombre = (self._nombres_proveedor.get(int(proveedor_id), "")
                  if proveedor_id is not None else "")
        self._decir_margen(
            f"Precios de {nombre} recalculados con margen ×{margen:g}."
            if nombre and len(self._v_margenes) > 1
            else f"Precios recalculados con margen ×{margen:g}.")
        for repintar in list(self._repintar_precio):
            try:
                repintar()
            except tk.TclError:
                pass  # la tarjeta ya no existe (pantalla reconstruida)

    def _bloque_precio_costo(self, info, cand: dict) -> None:
        """El precio de venta sugerido POR FÓRMULA DE COSTO, con la fórmula a la
        vista paso por paso.

        Es un precio DISTINTO del de arriba ("Precio de venta de comparables
        internos encontrados"), y por eso conviven los dos en la tarjeta con
        etiquetas explícitas: uno sale del costo que cotizó este proveedor, el
        otro de lo que TU Calzado ya vende en productos parecidos. Confundirlos
        era justamente el problema."""
        try:
            costo, moneda_fila = self._mc.obtener_costo_declarado(cand["candidato_id"])
        except Exception:  # noqa: BLE001
            costo, moneda_fila = None, None

        # Mutable para que `guardar_costo` pueda actualizar lo que ve
        # `repintar` sin reconstruir la tarjeta (que perdería el scroll).
        estado = {"costo": costo, "moneda_fila": moneda_fila}

        marco = Tarjeta(info)
        marco.pack(anchor="w", fill="x", pady=(4, 0))
        etq = Etiqueta(marco, text="", style="Suave.TLabel", justify="left",
                       wraplength=620)
        etq.pack(anchor="w", padx=8, pady=6)

        # ── campo editable del costo POR CANDIDATO ──
        # La moneda y el tipo de cambio se preguntan UNA VEZ POR LOTE (barra de
        # arriba), pero el costo es de cada producto: hasta ahora solo podía
        # entrar por el Excel del proveedor, y cuando ese Excel no traía la
        # columna de costo (el caso normal) no había ninguna forma de escribirlo.
        #
        # Todo en UN SOLO RENGLÓN (2026-09-09, pedido del usuario): el costo y
        # el precio de venta que sale de ese costo son las dos mitades de la
        # misma decisión, y tenerlos en filas separadas obligaba a saltar la
        # vista de arriba abajo para leer un número que depende del otro.
        # El desglose paso por paso de la fórmula queda abajo, en `etq`, para
        # quien quiera auditarlo.
        fila_costo = Marco(marco)
        fila_costo.pack(anchor="w", fill="x", padx=8, pady=(0, 8))
        Etiqueta(fila_costo, text="Costo:", style="Suave.TLabel").pack(
            side="left", padx=(0, 4))
        v_costo = tk.StringVar(value=(f"{estado['costo']:g}" if estado["costo"] else ""))
        entrada_costo = ctk.CTkEntry(fila_costo, textvariable=v_costo, width=90)
        entrada_costo.pack(side="left", padx=(0, 4))
        etq_unidad = Etiqueta(fila_costo, text="", style="Suave.TLabel")
        etq_unidad.pack(side="left", padx=(0, 8))
        Etiqueta(fila_costo, text="│", style="Suave.TLabel").pack(side="left", padx=(0, 8))
        etq_precio_vivo = Etiqueta(fila_costo, text="", style="Subtitulo.TLabel")
        etq_precio_vivo.pack(side="left", padx=(0, 10))
        # 2026-09-10, pedido del usuario: se fue el botón "✓ Guardar costo" que
        # tenía CADA tarjeta (18 botones idénticos en el catálogo Prueba2, y
        # cada clic recargaba la pantalla entera y perdía el scroll). Ahora el
        # campo queda "pendiente de guardar" en memoria y hay UN SOLO botón
        # «💾 Guardar lote» arriba que recorre todas las tarjetas. El aviso de
        # abajo es lo que evita que un costo tecleado se pierda en silencio.
        etq_pendiente = Etiqueta(fila_costo, text="", style="Suave.TLabel")
        etq_pendiente.pack(side="left")
        # "Quitar" ya NO escribe en la base: vacía el campo, y el borrado se
        # persiste con el mismo botón único que todo lo demás -- una sola
        # semántica para todos los cambios de costo de la pantalla.
        btn_quitar_costo = Boton(fila_costo, text="Quitar", style="Sutil.TButton", width=56,
                                 command=lambda: (v_costo.set(""), precio_en_vivo()))

        def texto_del_campo() -> str:
            """El costo tecleado, normalizado igual que lo normaliza el
            guardado. Se usa tanto para el aviso de "sin guardar" como para
            decidir, en el guardado en lote, si esta tarjeta cambió."""
            return (v_costo.get() or "").strip().replace(",", "").replace("₡", "").replace("$", "")

        def hay_cambio() -> bool:
            """True si lo que se lee en el campo es DISTINTO del costo que ya
            está persistido para esta referencia. Se compara numéricamente (no
            como texto) para que "1200", "1200.0" y "1,200" no cuenten como
            cambios y el botón único no reescriba lo que ya está bien."""
            texto = texto_del_campo()
            actual = estado["costo"]
            if not texto:
                return actual is not None
            try:
                return actual is None or abs(float(texto) - float(actual)) > 1e-9
            except ValueError:
                return True  # ilegible: que el guardado lo reporte como error

        def marcar_pendiente() -> None:
            try:
                etq_pendiente.configure(text="● sin guardar" if hay_cambio() else "")
            except tk.TclError:
                pass

        # Registro para el botón único «💾 Guardar lote» (2026-09-10): la
        # pantalla es la dueña de la lista, cada tarjeta se anota acá con todo
        # lo que hace falta para persistirla sin volver a mirar la interfaz.
        self._pendientes_costo.append({
            "cand": cand, "estado": estado,
            "texto": texto_del_campo, "hay_cambio": hay_cambio,
            "marcar": marcar_pendiente,
        })

        def precio_en_vivo(*_a) -> None:
            """Recalcula SOLO LA VISTA del precio de venta sugerido, con lo que
            hay tecleado en el campo en este instante.

            Antes el número únicamente aparecía después de apretar «✓ Guardar
            costo» (que además recarga la pantalla entera), así que el comprador
            no podía tantear "si lo compro a X, lo vendo a Y" sin escribir en la
            base. Esto no guarda nada: el guardado sigue siendo explícito.

            El cálculo es la misma `calcular_precio_venta` de siempre (costo →
            colones → +IVA → ×margen → redondeo al ₡100), que es aritmética
            pura sobre tres números: no hace falta debounce."""
            cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
            texto = (v_costo.get() or "").strip().replace(",", "").replace("₡", "").replace("$", "")
            marcar_pendiente()
            if not texto:
                etq_precio_vivo.configure(text="Precio de venta sugerido: —")
                return
            try:
                valor = float(texto)
            except ValueError:
                etq_precio_vivo.configure(text="Precio de venta sugerido: costo inválido")
                return
            if valor <= 0:
                etq_precio_vivo.configure(text="Precio de venta sugerido: —")
                return
            moneda = cfg["moneda"] or estado.get("moneda_fila")
            d = (calcular_precio_venta(valor, moneda, cfg["tipo_cambio"], cfg["margen"])
                 if moneda_valida(moneda) else None)
            if d is None:
                etq_precio_vivo.configure(
                    text="Precio de venta sugerido: falta moneda / tipo de cambio")
                return
            etq_precio_vivo.configure(
                text=f"Precio de venta sugerido: ₡{d['precio_venta']:,.0f}")

        entrada_costo.bind("<KeyRelease>", precio_en_vivo)
        # También al pegar con el mouse o al salir del campo, para que la vista
        # nunca quede mostrando un precio que no corresponde a lo que se lee.
        entrada_costo.bind("<FocusOut>", precio_en_vivo)

        def repintar() -> None:
            cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
            costo = estado["costo"]
            moneda_fila = estado["moneda_fila"]
            # Rótulo de la unidad del campo: el comprador tiene que ver en qué
            # moneda está escribiendo, o el número no significa nada.
            unidad = cfg["moneda"] or moneda_fila
            etq_unidad.configure(text=(
                _rasgo_moneda(unidad, "plural") if moneda_valida(unidad)
                else "(falta declarar la moneda de este proveedor)"))
            # El renglón de arriba se recalcula acá también: así, cambiar el
            # margen del lote actualiza el precio en vivo de todas las tarjetas.
            precio_en_vivo()
            marcar_pendiente()
            if costo and not btn_quitar_costo.winfo_manager():
                btn_quitar_costo.pack(side="left", padx=(4, 0))
            elif not costo and btn_quitar_costo.winfo_manager():
                btn_quitar_costo.pack_forget()
            # La moneda declarada PARA EL PROVEEDOR DE ESTE CANDIDATO manda
            # sobre la de la fila: el comprador la declaró explícitamente
            # mirando el catálogo, mientras que `raw_catalogo.moneda_costo` se
            # infirió del Excel y puede venir en None. Solo se cae a la de la
            # fila si ese proveedor no tiene ninguna declarada.
            moneda = cfg["moneda"] or moneda_fila
            if costo is None:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: todavía no hay costo para esta "
                                   "referencia — escribilo abajo y se calcula al instante.")
                return
            if not moneda:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: falta declarar en qué "
                                   "moneda cotiza este proveedor (botón «Cambiar» arriba).")
                return
            d = calcular_precio_venta(costo, moneda, cfg["tipo_cambio"], cfg["margen"])
            if d is None:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: con "
                                   f"{_rasgo_moneda(moneda, 'plural', moneda)} hace falta "
                                   "el tipo de cambio (botón «Cambiar» arriba).")
                return
            if moneda_convierte(moneda):
                simbolo = _rasgo_moneda(moneda, "simbolo", "")
                linea_costo = (f"Costo del proveedor:  {simbolo}{d['costo_origen']:,.2f}  ×  "
                               f"₡{d['tipo_cambio']:,.2f}  =  ₡{d['costo_crc']:,.0f}")
            else:
                linea_costo = f"Costo del proveedor:  ₡{d['costo_crc']:,.0f}"
            etq.configure(text=(
                f"{ROTULO_PRECIO_POR_COSTO}\n"
                f"{linea_costo}\n"
                f"+ IVA ({IVA_CR * 100:.0f}%):  ₡{d['monto_iva']:,.0f}  →  ₡{d['costo_iva']:,.0f}\n"
                f"× Margen de venta ×{d['margen']:g}  →  ₡{d['precio_sin_redondear']:,.0f}\n"
                f"= Precio de venta sugerido:  ₡{d['precio_venta']:,.0f}  (redondeado al ₡100)"))

        repintar()
        self._repintar_precio.append(repintar)

    # ---------- costo por candidato: base efímera + espejo local ----------

    def _precio_vino_del_costo(self, cand: dict) -> bool:
        """True si el precio guardado como "manual" para este candidato lo
        calculó esta app a partir del costo ingresado por el comprador (y no lo
        escribió el comprador como precio de venta). Lo sabe el espejo local,
        que guarda `origen` junto con cada costo."""
        guardado = self._costos_lote.get(self._clave_costo(cand))
        return (isinstance(guardado, dict)
                and guardado.get("origen") == "costo_lote"
                and bool(guardado.get("precio_venta")))

    def _clave_costo(self, cand: dict) -> str:
        clave = getattr(self.master, "clave_costo", None)
        if callable(clave):
            return clave(cand.get("catalogo_origen"), cand.get("codigo_proveedor"))
        return f"{cand.get('catalogo_origen') or ''}||{cand.get('codigo_proveedor') or ''}"

    def _volcar_costos_lote(self) -> bool:
        """Escribe el espejo completo en `lote.sqlite`. True si se pudo."""
        guardar = getattr(self.master, "guardar_costos_lote", None)
        if not callable(guardar):
            return False
        try:
            guardar(self._costos_lote)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _restaurar_costos_del_respaldo(self) -> None:
        """Vuelve a poner en la revisión los costos que quedaron guardados en
        el respaldo local de este lote -- el caso para el que existe el espejo:
        la revisión de este lote se vació (reprocesarlo desde cero) y al
        retomarlo los costos ya ingresados seguían vivos en `lote.sqlite`.

        FASE 4: antes ese vaciado lo podía provocar OTRO lote (TRUNCATE del
        `staging_tuc` compartido). Ya no: solo lo provoca reprocesar este.

        Se corre UNA VEZ, antes de pintar las tarjetas, para que todo lo que se
        muestre después (incluida la línea de precio de referencia, que sale de
        la cascada en la base) ya vea los costos restaurados.

        Restaura solo cuando el producto es el mismo: la clave del espejo es
        `codigo_proveedor` + `catalogo_origen`, nunca el `candidato_id`, que es
        un serial efímero y se reasigna en cada recarga. Y solo cuando staging
        NO tiene costo: lo que haya en la base (una cotización cerrada, un costo
        del Excel) es más nuevo que el respaldo y no se pisa."""
        if not self._costos_lote:
            return
        for cand in self._candidatos:
            guardado = self._costos_lote.get(self._clave_costo(cand))
            if not isinstance(guardado, dict):
                continue
            try:
                costo = float(guardado.get("costo"))
            except (TypeError, ValueError):
                continue
            moneda = guardado.get("moneda")
            if costo <= 0 or not moneda_valida(moneda):
                continue
            try:
                if self._mc.obtener_costo_declarado(cand["candidato_id"])[0] is not None:
                    continue
                self._mc.fijar_costo_declarado(cand["candidato_id"], costo, moneda)
                precio = guardado.get("precio_venta")
                if precio:
                    self._mc.fijar_precio_manual(cand["candidato_id"], float(precio))
                self._costos_restaurados += 1
            except Exception:  # noqa: BLE001
                # El candidato ya no está en staging (o la base no lo acepta):
                # el respaldo local no se pierde por eso, se intentará de nuevo
                # la próxima vez que se abra el lote.
                continue

    def _persistir_costo(self, cand: dict, texto: str, estado: dict) -> str:
        """Núcleo de guardado de UN costo, sin interfaz: ni messagebox, ni
        reloj, ni recarga de pantalla. Devuelve "" si salió bien, o el motivo
        del fallo (texto para mostrarle al comprador).

        Se separó de `_guardar_costo_candidato` (2026-09-10) para que el botón
        único «💾 Guardar lote» pueda recorrer las 18 tarjetas del catálogo
        haciendo exactamente las MISMAS tres escrituras que hacía el botón por
        tarjeta -- `fijar_costo_declarado` + `fijar_precio_manual` + espejo en
        `lote.sqlite` -- sin 18 messageboxes ni 18 recargas. El volcado del
        espejo y la recarga los hace el llamador, una sola vez al final."""
        texto = (texto or "").strip().replace(",", "").replace("₡", "").replace("$", "")
        costo = None
        if texto:
            try:
                costo = float(texto)
            except ValueError:
                return "costo inválido"
            if costo <= 0:
                return "el costo tiene que ser mayor que cero"

        cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
        moneda = cfg["moneda"] or estado.get("moneda_fila")
        if costo is not None and not moneda_valida(moneda):
            return ("falta declarar en qué moneda cotiza este proveedor "
                    "(botón «Cambiar» arriba)")

        desglose = (calcular_precio_venta(costo, moneda, cfg["tipo_cambio"], cfg["margen"])
                    if costo is not None else None)
        try:
            self._mc.fijar_costo_declarado(cand["candidato_id"], costo, moneda)
            # El precio derivado solo se fija cuando de verdad se pudo
            # calcular (dólares sin tipo de cambio: `calcular_precio_venta` se
            # abstiene, y escribir un precio a medias sería peor que ninguno).
            self._mc.fijar_precio_manual(
                cand["candidato_id"],
                desglose["precio_venta"] if desglose else None)
        except Exception as exc:  # noqa: BLE001
            return str(exc)

        clave = self._clave_costo(cand)
        if costo is None:
            self._costos_lote.pop(clave, None)
        else:
            self._costos_lote[clave] = {
                "codigo_proveedor": cand.get("codigo_proveedor"),
                "catalogo_origen": cand.get("catalogo_origen"),
                "costo": costo,
                "moneda": moneda,
                "tipo_cambio": cfg["tipo_cambio"],
                "margen": cfg["margen"],
                "precio_venta": desglose["precio_venta"] if desglose else None,
                "origen": "costo_lote",
                "guardado_en": datetime.now().isoformat(timespec="seconds"),
            }
        estado["costo"] = costo
        estado["moneda_fila"] = moneda
        return ""

    def _fila_candidato(self, contenedor, cand: dict) -> None:
        fila = Tarjeta(contenedor)
        fila.pack(fill="x", pady=4)

        # UNA SOLA FOTO POR COLOR EN LA TARJETA (2026-09-17, reclamo del dueño:
        # "si la referencia tiene 3 colores, solo quiero ver 3 fotos ...
        # actualmente estás repitiendo una foto" -- aclarado después: "no es
        # una foto repetida, es la misma foto que la ponés en un tamaño
        # ligeramente más grande a la izquierda").
        #
        # Qué pasaba: la foto grande de la izquierda es la del PRIMER color
        # (`_cargar_foto_candidato` lee `colores_vista[0]`), y la fila
        # «Colores de esta referencia» volvía a dibujar ESE MISMO color como
        # miniatura junto a los otros dos. Cuatro imágenes en pantalla para
        # tres colores reales, una de ellas dos veces.
        #
        # Por qué se resolvió sacando la foto grande en vez de sacar el primer
        # color de la fila: el proveedor vende por bulto, los tres colores se
        # compran juntos y ninguno es "el principal" -- darle a uno el lugar
        # grande y a los otros dos una miniatura mentía sobre lo que se compra.
        # Con la fila de colores como única fuente, los tres se ven del mismo
        # tamaño, cada uno con su color, su calificación y sus comparables.
        # Con un solo color no cambia nada: la foto grande de siempre.
        colores_vista = cand.get("colores_vista") or []
        multicolor = len(colores_vista) > 1
        hueco = etq_foto = None
        if not multicolor:
            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6,
                                 width=120, height=120)
            hueco.pack(side="left", padx=10, pady=8)
            hueco.pack_propagate(False)
            foto_tk = self._cargar_foto_candidato(cand)
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                etq_foto = tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0)
                etq_foto.pack(fill="both", expand=True)
            else:
                etq_foto = Etiqueta(hueco, text="sin foto", style="Suave.TLabel")
                etq_foto.pack(fill="both", expand=True)

        info = Marco(fila)
        info.pack(side="left", fill="both", expand=True, pady=10)
        texto_codigo = (codigo_visible(cand.get("codigo_proveedor"))
                        or f"Candidato {cand['candidato_id']}")
        if self._es_multiproveedor():
            # FASE 4: con dos catálogos mezclados en la misma pantalla, el
            # código del proveedor solo no dice de QUIÉN es la referencia (y
            # dos proveedores pueden usar códigos parecidos). La pastilla va
            # pegada al código, que es lo primero que se lee de la tarjeta.
            fila_codigo = Marco(info)
            fila_codigo.pack(anchor="w", fill="x")
            Etiqueta(fila_codigo, text=texto_codigo,
                     style="Subtitulo.TLabel").pack(side="left")
            self._pastilla_proveedor(fila_codigo, cand).pack(side="left", padx=(8, 0))
        else:
            # Un solo proveedor (caso mayoritario): exactamente el mismo
            # widget de siempre, sin contenedor ni pastilla de más.
            Etiqueta(info, text=texto_codigo, style="Subtitulo.TLabel").pack(anchor="w")

        self._fila_tipo_genero(info, cand)
        self._fila_marca(info, cand)
        # Qué color se muestra (2026-09-09): antes SOLO `color_principal`, que
        # es el color de la variante 0 y en la práctica es "negro" en la enorme
        # mayoría del calzado -- de ahí la queja de que "el color no se
        # detecta". La base ya guarda además `colores_detectados` (la
        # combinación real: "negro, gris", "blanco, azul") y `color_suela`, que
        # nunca se pintaban en esta pantalla. Ahora se muestra la combinación
        # completa, con la suela aparte porque es un dato de compra distinto.
        fila_color = Marco(info)
        fila_color.pack(anchor="w", fill="x", pady=(2, 0))
        Etiqueta(fila_color, text="Color:", style="Suave.TLabel").pack(side="left", padx=(0, 6))
        detectados = cand.get("colores_detectados") or cand.get("color_principal")
        _pastilla_color(fila_color, detectados).pack(side="left")
        suela = cand.get("color_suela")
        if suela:
            Etiqueta(fila_color, text="Suela:", style="Suave.TLabel").pack(side="left", padx=(10, 6))
            _pastilla_color(fila_color, suela).pack(side="left")

        if multicolor:
            self._fila_colores(info, cand, colores_vista)

        self._fila_precio(info, cand)
        self._bloque_precio_costo(info, cand)

        grado = cand.get("grado")
        score = cand.get("score_final")
        # REFERENCIA DE VARIOS COLORES (2026-09-17, pedido del dueño): el
        # proveedor genérico vende por bulto -- si la referencia trae 3
        # colores, se compran los 3 o ninguno. El número grande de la tarjeta
        # pasa a ser el PROMEDIO SIMPLE de los colores (cada color pesa igual),
        # que es la calificación de lo que realmente se compra. Con un solo
        # color `score_ponderado` viene en None y acá no cambia absolutamente
        # nada respecto de antes.
        score_ponderado = cand.get("score_ponderado")
        es_ponderado = score_ponderado is not None
        if es_ponderado:
            score = score_ponderado
            grado = cand.get("grado_ponderado") or grado
        if grado == "sin_comparables":
            texto_grado = "sin comparables"
        else:
            texto_grado = grado if grado else "sin calificar"
        if grado in ("S", "A"):
            fg, bg = PAR_OK, PAR_OK_BG
        elif grado in ("C", "D"):
            fg, bg = PAR_MAL, PAR_MAL_BG
        else:
            fg, bg = PAR_NEUTRO, PAR_PANEL
        marco_grado = Marco(fila)
        marco_grado.pack(side="right", padx=14)

        # El SCORE es ahora el número principal del badge (2026-09-09, pedido
        # del usuario), y en letra grande. `score_final` ya viene calculado
        # (`score_base` 0-100 × varios factores acotados, ver
        # `motor_calificacion.py`), así que NO se reescala nada: solo se
        # redondea a entero y se acota al 1 mínimo para que un candidato
        # calificado nunca muestre un "0" que parecería "sin calificar". La
        # letra S/A/B/C/D se conserva debajo, chiquita: sigue siendo el
        # lenguaje con el que se habla del corte de compra.
        #
        # CORREGIDO 2026-09-22: antes también se acotaba ARRIBA en 100 ("la
        # escala que se le prometió al comprador es 1-100"), asumiendo que la
        # fórmula rara vez pasaría de ~105. Medido contra lotes reales:
        # varios factores (f_demanda, f_venta, f_rotacion, f_descuento)
        # llegan a su tope a la vez con más frecuencia de la esperada, y el
        # score real de un lote entero puede ir de 157 a 192 -- con el techo
        # en 100, TODOS esos candidatos se veían idénticos ("100"), perdiendo
        # justo la diferencia que el comprador necesita para elegir la mejor
        # referencia. Mostrar el número real no cambia el orden ni la
        # fórmula, solo deja de esconder la diferencia real entre candidatos.
        if score is not None:
            numero = max(1, int(round(float(score))))
            etq_grado = tk.Label(marco_grado, text=str(numero), fg=solido(fg), bg=solido(bg),
                    font=("Segoe UI Semibold", 34), padx=18, pady=6, cursor="hand2")
            etq_grado.pack()
            leyenda = (f"promedio de {len(cand.get('colores_vista') or [])} colores  ·  grado {texto_grado}"
                       if es_ponderado else f"score  ·  grado {texto_grado}")
            Etiqueta(marco_grado, text=leyenda, style="Suave.TLabel").pack()
        else:
            etq_grado = tk.Label(marco_grado, text=texto_grado, fg=solido(fg), bg=solido(bg),
                    font=("Segoe UI Semibold", 18), padx=12, pady=6, cursor="hand2")
            etq_grado.pack()
        if grado not in (None, "sin_comparables"):
            # Plan 2026-09-07, paso 9 (ampliado): clic en el badge explica el
            # grado -- la defensa permanente contra "las calificaciones son
            # muy altas" es que CUALQUIER número se pueda auditar en un clic.
            etq_grado.bind("<Button-1>", lambda _e: self._mostrar_desglose_score(cand))
            Etiqueta(marco_grado, text="¿por qué? ↑", style="Suave.TLabel").pack()
        # Botón explícito para los comparables (2026-09-09, pedido del
        # usuario): antes la ÚNICA forma visible de abrirlos era hacer clic en
        # la letra del grado, que no se ve como algo cliqueable -- el clic en
        # la letra hoy abre el desglose del score, que es otra cosa. El clic en
        # el resto de la tarjeta sigue funcionando (no molesta a nadie), pero
        # este botón es la forma principal y la que se ve.
        btn_comparables = Boton(marco_grado, text="👁 Ver comparables", style="Primario.TButton")
        # `command` se asigna después de crear el botón porque el lambda tiene
        # que poder pasar EL BOTÓN como `disparador` (se deshabilita mientras
        # se arma el panel: ver `_abrir_comparables`).
        btn_comparables.configure(
            command=lambda: self._abrir_comparables(cand, btn_comparables))
        btn_comparables.pack(pady=(6, 0), fill="x")
        Boton(marco_grado, text="🗑 Eliminar", style="Sutil.TButton",
             command=lambda: self._confirmar_eliminar_candidato(cand)).pack(pady=(4, 0), fill="x")

        # Clic en cualquier parte de la tarjeta (menos los controles del selector
        # ambiguo, que ya tienen su propio manejo) abre los comparables.
        for w in (fila, info, hueco, etq_foto):
            if w is None:
                continue  # candidato multicolor: no hay foto grande a la izquierda
            w.bind("<Button-1>", lambda _e, c=cand: self._abrir_comparables(c))

        return fila

    SIN_MARCA = "(genérico — sin marca)"
    # Catálogos a los que ya se les corrió la autodetección de marca en ESTA
    # sesión: `_recargar` crea una instancia nueva de la pantalla en cada
    # guardado, y la detección no tiene por qué repetirse (es idempotente,
    # pero cuesta una consulta a Postgres y un barrido del lote).
    _MARCAS_AUTODETECTADAS: set = set()

    def _autodetectar_marcas(self) -> None:
        """Rellena la marca declarada de los candidatos cuyo TEXTO de catálogo
        nombre una marca reconocida (`autodetectar_marcas_declaradas`). No
        pisa nunca lo que declaró el comprador.

        En el lote Prueba2 esto detecta 0 marcas -- las fotos vinieron de un
        Excel y el "texto" de cada referencia es el nombre del archivo. Corre
        igual porque los catálogos con OCR/descripción sí se benefician, y
        porque el costo es una consulta."""
        clave = tuple(sorted({c.get("catalogo_origen") for c in self._candidatos}))
        if clave in self._MARCAS_AUTODETECTADAS:
            return
        self._MARCAS_AUTODETECTADAS.add(clave)
        try:
            self._mc.autodetectar_marcas_declaradas()
        except Exception:  # noqa: BLE001
            pass  # sin Postgres o sin la columna: la entrada manual sigue viva

    def _fila_marca(self, info, cand: dict) -> None:
        """«Marca (si aplica)» — Tarea 7, 2026-09-10.

        EL BUG QUE ARREGLA (parcialmente, ver más abajo): el usuario reportó
        que OSIRIS, una marca mundialmente reconocida, se estaba comparando
        contra el catálogo genérico de TU Calzado en vez de contra TC Marcas.
        Investigado: hasta hoy el sistema NO tenía ninguna forma de saber que
        una referencia era de marca -- en el lote real (Prueba2 / Cachos, 18
        referencias) `marca_declarada` está NULL en las 18 y no hay OCR del
        catálogo, así que no había ni dato declarado ni texto que mirar. Este
        control es la señal que faltaba, y la da el comprador.

        Es un desplegable ESCRIBIBLE, no cerrado: la lista gobernada
        (`silver.dim_marca`, 32 marcas) no incluye OSIRIS, así que limitar al
        comprador a esa lista no habría resuelto el caso reportado.

        Qué cambia al declararla, hoy: (1) la ventana de comparables pone la
        lista de MARCA RECONOCIDA primero y avisa que este candidato es de
        marca; (2) `_factor_mercado` (en el score) encuentra la señal real de
        importación de esa marca en `gold.vw_senal_marca`, que sin el dato
        quedaba en el factor neutro 1.0.

        Qué NO cambia (honesto): el score de similitud sigue calculándose
        contra el índice del catálogo TU Calzado. Ver la nota de la ventana de
        comparables."""
        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(2, 0))
        Etiqueta(marco, text="Marca (si aplica):", style="Suave.TLabel").pack(
            side="left", padx=(0, 4))

        actual = (cand.get("marca_declarada") or "").strip()
        opciones = [self.SIN_MARCA] + self._marcas_conocidas()
        if actual and actual not in opciones:
            opciones.insert(1, actual)  # una marca escrita a mano, ej. OSIRIS
        combo = ctk.CTkComboBox(marco, values=opciones, width=190)
        combo.set(actual or self.SIN_MARCA)
        combo.pack(side="left", padx=(0, 6))
        Boton(marco, text="✓", style="Sutil.TButton", width=28,
              command=lambda: self._confirmar_marca(cand, combo.get())).pack(side="left")
        if actual:
            tk.Label(marco, text="marca reconocida", fg=solido(PAR_OK),
                    bg=solido(PAR_OK_BG), font=("Segoe UI Semibold", 10),
                    padx=6).pack(side="left", padx=(8, 0))

    def _marcas_conocidas(self) -> list[str]:
        """La lista gobernada, leída UNA vez por pantalla (son 32 filas de
        Postgres y hay una tarjeta por candidato: 18 consultas idénticas por
        pintada sería absurdo)."""
        if getattr(self, "_cache_marcas", None) is None:
            try:
                self._cache_marcas = self._mc.marcas_reconocidas()
            except Exception:  # noqa: BLE001
                self._cache_marcas = []
        return self._cache_marcas

    def _confirmar_marca(self, cand: dict, texto: str) -> None:
        marca = None if (texto or "").strip() in ("", self.SIN_MARCA) else texto.strip()
        with reloj(self):
            try:
                self._mc.fijar_marca_declarada(cand["candidato_id"], marca)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo guardar la marca:\n{exc}")
                return
        self._recargar()

    def _fila_tipo_genero(self, info, cand: dict) -> None:
        """Tipo (categoría de calzado) y Género, SIEMPRE editables a mano.

        Antes (`_fila_ambigua`, reemplazada por esto) los desplegables solo
        aparecían cuando el nearest-centroid había quedado ambiguo, o sea
        cuando el dato faltaba: si la máquina se equivocaba con confianza --el
        caso que más molesta-- la tarjeta mostraba el valor mal en texto plano
        y no había forma de corregirlo desde la app. Ahora los dos son
        desplegables siempre, con el valor ya calculado como default, y
        cambiarlos GUARDA de una (UPDATE a `staging_tuc.dim_candidato`:
        `categoria_declarada` y `genero_canonico`, vía
        `api_asignar_categoria`, que además vuelve a puntuar el candidato
        porque el score depende del filtro categoria+genero).

        Se conserva el aviso "⚠ ambiguo" cuando el valor venía vacío: sigue
        siendo información útil (la máquina no supo), solo que ya no es lo que
        decide si se puede editar o no."""
        categoria = cand.get("categoria_declarada")
        genero = cand.get("genero_declarado")

        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(2, 0))

        if not categoria or not genero:
            tk.Label(marco, text="⚠ ambiguo", fg=solido(PAR_ALERTA), bg=solido(PAR_PANEL),
                    font=("Segoe UI Semibold", 11)).pack(side="left", padx=(0, 8))

        # Los valores calculados se agregan al listado si no estuvieran ya (la
        # base puede tener una categoría vieja que ya no está en la constante):
        # sin esto, `set()` de un valor ausente deja el combo en blanco y
        # parecería que el dato se perdió.
        cats = list(CATEGORIAS_CONOCIDAS)
        if categoria and categoria not in cats:
            cats.append(categoria)
        gens = list(GENEROS_CONOCIDOS)
        if genero and genero not in gens:
            gens.append(genero)

        Etiqueta(marco, text="Tipo:", style="Suave.TLabel").pack(side="left", padx=(0, 4))
        combo_cat = ctk.CTkComboBox(marco, values=cats, width=160, state="readonly")
        combo_cat.set(categoria or "")
        combo_cat.pack(side="left", padx=(0, 12))

        Etiqueta(marco, text="Género:", style="Suave.TLabel").pack(side="left", padx=(0, 4))
        combo_gen = ctk.CTkComboBox(marco, values=gens, width=120, state="readonly")
        combo_gen.set(genero or "")
        combo_gen.pack(side="left")

        # Botón explícito de refresco (2026-09-09, pedido del usuario).
        # Qué pasaba ANTES: cambiar tipo, género o costo YA refrescaba solo
        # --los tres guardan y llaman `_recargar()`, que vuelve a pedir la
        # lista y reconstruye la pantalla-- pero era invisible: la pantalla
        # parpadeaba y nada decía que el score se había recalculado, así que
        # parecía que el cambio no había tenido efecto. El refresco sigue
        # siendo automático (no se puede perder un guardado por olvidarse de
        # apretar nada); este botón le da al comprador la forma MANIFIESTA de
        # volver a calificar y reordenar la lista cuando quiera.
        Boton(marco, text="🔄 Actualizar", style="Sutil.TButton", width=104,
              command=self._recargar).pack(side="left", padx=(12, 0))

        # El `command` se conecta DESPUÉS del `set()` inicial: CTkComboBox
        # dispara el callback en `set()`, y conectarlo antes provocaría un
        # guardado (y un recargado de pantalla entero) por cada tarjeta al
        # abrir el paso 6.
        combo_cat.configure(command=lambda valor: self._guardar_tipo_genero(
            cand, categoria=valor, actual_categoria=categoria))
        combo_gen.configure(command=lambda valor: self._guardar_tipo_genero(
            cand, genero=valor, actual_genero=genero))

    def _guardar_tipo_genero(self, cand: dict, categoria: str | None = None,
                             genero: str | None = None,
                             actual_categoria: str | None = None,
                             actual_genero: str | None = None) -> None:
        """Guarda la corrección manual de tipo/género de UN candidato.

        No hace nada si el valor elegido es el que ya estaba: así, abrir los
        desplegables para mirar y cerrarlos sin cambiar no dispara un
        re-scoring ni un recargado de la pantalla."""
        categoria = (categoria or "").strip() or None
        genero = (genero or "").strip() or None
        if categoria is not None and categoria == actual_categoria:
            return
        if genero is not None and genero == actual_genero:
            return
        if categoria is None and genero is None:
            return
        try:
            self._mc.asignar_categoria(cand["candidato_id"], categoria=categoria, genero=genero)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo guardar la corrección:\n{exc}")
            return
        # …y en el espejo durable del lote, para que la corrección sobreviva a
        # que se vacíe la revisión de este lote (ver `leer_categorias_lote`).
        self._anotar_categoria_lote(cand, categoria=categoria, genero=genero)
        self._recargar()

    # ---------- tipo/género por candidato: base efímera + espejo local ------

    def _anotar_categoria_lote(self, cand: dict, categoria: str | None = None,
                               genero: str | None = None) -> None:
        """Suma la corrección al espejo local y lo vuelca completo. Se anota
        solo el campo que el comprador cambió: corregir el género no debe
        borrar del respaldo un tipo corregido antes."""
        clave = self._clave_costo(cand)  # misma clave que los costos, a propósito
        fila = dict(self._categorias_lote.get(clave) or {})
        fila.update({"codigo_proveedor": cand.get("codigo_proveedor"),
                     "catalogo_origen": cand.get("catalogo_origen"),
                     "guardado_en": datetime.now().isoformat(timespec="seconds")})
        if categoria:
            fila["categoria"] = categoria
        if genero:
            fila["genero"] = genero
        self._categorias_lote[clave] = fila
        guardar = getattr(self.master, "guardar_categorias_lote", None)
        if callable(guardar):
            try:
                guardar(self._categorias_lote)
            except Exception:  # noqa: BLE001
                pass  # el respaldo no puede ser lo que rompa la pantalla

    def _restaurar_categorias_del_respaldo(self) -> None:
        """Vuelve a aplicar en la revisión los tipos/géneros que el comprador
        ya había corregido en este lote -- el caso para el que existe el espejo:
        la revisión de este lote se vació (reprocesarlo desde cero) y al
        retomarlo la corrección hecha a mano seguía viva en `lote.sqlite`.

        FASE 4: antes ese vaciado lo podía provocar OTRO lote (TRUNCATE del
        `staging_tuc` compartido). Ya no: solo lo provoca reprocesar este.

        Solo escribe cuando el valor en la base DIFIERE del respaldo: si la
        máquina ya llegó al mismo resultado, no hay nada que corregir (y se
        evita un re-scoring inútil por candidato)."""
        if not self._categorias_lote:
            return
        for cand in self._candidatos:
            guardado = self._categorias_lote.get(self._clave_costo(cand))
            if not isinstance(guardado, dict):
                continue
            categoria = guardado.get("categoria") or None
            genero = guardado.get("genero") or None
            if categoria == cand.get("categoria_declarada"):
                categoria = None
            if genero == cand.get("genero_declarado"):
                genero = None
            if categoria is None and genero is None:
                continue
            try:
                self._mc.asignar_categoria(cand["candidato_id"],
                                           categoria=categoria, genero=genero)
            except Exception:  # noqa: BLE001
                # El candidato ya no está en staging (o la base no lo acepta):
                # el respaldo local no se pierde por eso.
                continue
            # La lista en memoria se pinta ANTES de la recarga, así que se
            # actualiza también acá para que la tarjeta no muestre el valor
            # viejo por un instante.
            if categoria:
                cand["categoria_declarada"] = categoria
            if genero:
                cand["genero_declarado"] = genero
            self._categorias_restauradas += 1

    def _fila_colores(self, info, cand: dict, colores_vista: list[dict]) -> None:
        """Una tarjetita CON FOTO por color, cada una con su propio 🗑 -- plan
        2026-09-07, paso 7 (eliminar variante). Solo se muestra cuando el
        candidato trae más de un color (`colores_vista` viene de
        `api_candidatos`, ya filtrado a variantes con foto limpia real).

        Antes esto era una pastilla de color (un cuadradito de color sólido,
        no la foto real) -- el comprador pidió ver la FOTO de cada color, para
        reconocer el color real del producto en vez de adivinar por el nombre
        ("naranja" puede ser cualquier tono). Termina con la calificación
        ponderada del bulto completo (2026-09-18, pedido explícito: "cada
        color con su calificación y la calificación ponderada al final")."""
        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(4, 0))
        Etiqueta(marco, text="Colores de esta referencia:",
                 style="Suave.TLabel").pack(anchor="w")
        fila_fotos = Marco(marco)
        fila_fotos.pack(anchor="w", fill="x", pady=(4, 0))
        for col in colores_vista:
            url = col.get("url", "")
            if not url.startswith("/crop_variante/"):
                continue
            try:
                variante_id = int(url.rsplit("/", 1)[-1])
            except ValueError:
                continue
            chip = Marco(fila_fotos)
            chip.pack(side="left", padx=(0, 10))

            # Foto real del color, no una pastilla de color sólido. 104px
            # (antes 56): desde que la tarjeta ya NO dibuja la foto grande de
            # la izquierda para referencias de varios colores (ver
            # `_fila_candidato`), esta fila es la ÚNICA foto del candidato y
            # tiene que poder mirarse, no solo confirmarse.
            ruta_col = self._mc.ruta_foto_variante(variante_id)
            foto_col = _cargar_foto_generico(ruta_col, tam=(104, 104))
            # `tk.Label` es tk clásico: solo acepta UN color, no el par
            # (claro, oscuro) de `PAR_PANEL_SUAVE` -- eso tiraba
            # `TclError: invalid color name "#eff2f6 #2c313a"` y rompía toda
            # la fila de colores en silencio (bug real, no cosmético).
            lbl_foto = tk.Label(chip, bd=0, bg=solido(PAR_PANEL_SUAVE))
            if foto_col is not None:
                lbl_foto.configure(image=foto_col, cursor="hand2")
                lbl_foto.image = foto_col  # referencia viva: sin esto Tk la recolecta
                # Clic en la miniatura = la misma foto en grande (2026-09-17,
                # pedido del dueño). Mismo patrón que el visor de comparables
                # (`_ver_foto_grande`), `_traer_al_frente` incluido: sin eso la
                # ventana nace DETRÁS y parece que el clic no hizo nada.
                lbl_foto.bind("<Button-1>", lambda _e, r=ruta_col,
                              t=f"{codigo_visible(cand.get('codigo_proveedor'))} — "
                                f"{col.get('color_principal') or 'color'}":
                              self._ver_foto_color(r, t))
            else:
                lbl_foto.configure(text="sin foto", width=13, height=6)
            lbl_foto.pack()

            fila_pastilla = Marco(chip)
            fila_pastilla.pack(pady=(2, 0))
            _pastilla_color(fila_pastilla, col.get("color_principal")).pack(side="left")
            Boton(fila_pastilla, text="🗑", style="Sutil.TButton", width=22,
                 command=lambda vid=variante_id: self._confirmar_eliminar_variante(cand, vid)
                 ).pack(side="left", padx=(2, 0))

            # La calificación de ESTE color (2026-09-17). El bulto no se puede
            # partir, así que el comprador tiene que ver si está pagando un
            # color bueno y dos malos -- antes solo se calificaba el principal.
            # Si un color todavía no tiene número se dice POR QUÉ, nunca se
            # muestra un 0 ni se lo esconde.
            score_col = col.get("score_final")
            if score_col is not None:
                grado_col = col.get("grado") or "—"
                Etiqueta(chip, text=f"{max(1, int(round(float(score_col))))} ({grado_col})",
                         style="Suave.TLabel").pack(pady=(2, 0))
            else:
                falta = col.get("grado")
                texto_falta = ("falta vectorizar" if falta == "sin_vector" else
                               "sin comparables" if falta == "sin_comparables" else
                               "sin calificar")
                Etiqueta(chip, text=texto_falta, style="Suave.TLabel").pack(pady=(2, 0))

            # COMPARABLES DE ESTE COLOR (2026-09-17, pedido del dueño: "la
            # opción de ver comparables debería ser para cada variación de
            # color. Cada variación de color tiene sus propios comparables").
            # El botón general de la derecha abría el panel SIEMPRE parado en
            # el primer color, y para ver los otros dos había que descubrir el
            # combo «Color:» de adentro. La búsqueda por color ya existía y ya
            # usa el embedding de ESE color -- lo que faltaba era el acceso.
            # Ancho fijo = ancho de la miniatura: con `fill="x"` el botón era
            # el widget más ancho del chip y estiraba la columna entera, así
            # que las fotos quedaban separadas por aire muerto.
            btn_col = Boton(chip, text="👁 comparables", style="Sutil.TButton", width=104)
            btn_col.configure(command=lambda i=col.get("indice"), b=btn_col:
                              self._abrir_comparables(cand, b, indice=i))
            btn_col.pack(pady=(2, 0))

        # Calificación ponderada AL FINAL de la fila (además del número grande
        # de la tarjeta, que ya la muestra como score principal) -- pedido
        # explícito: que se lea de corrido "color, color, color → ponderado".
        score_pond = cand.get("score_ponderado")
        if score_pond is not None:
            # `Separador` es horizontal (1px de alto, pensado para `fill="x"`);
            # acá hace falta una línea VERTICAL, así que el frame angosto se
            # arma directo en vez de reusarlo mal.
            tk.Frame(fila_fotos, width=1, bg=COLOR_BORDE_FUERTE
                    ).pack(side="left", padx=(4, 10), fill="y", pady=4)
            final = Marco(fila_fotos)
            final.pack(side="left")
            Etiqueta(final, text="Ponderado", style="Suave.TLabel").pack()
            grado_pond = cand.get("grado_ponderado") or "—"
            Etiqueta(final, text=f"{max(1, int(round(float(score_pond))))} ({grado_pond})",
                     style="Subtitulo.TLabel").pack()

    def _ver_foto_color(self, path, titulo: str) -> None:
        """La foto de UN color de la referencia, en grande, en su propia
        ventana (2026-09-17, pedido del dueño: "cuando le doy clic pueda ver la
        foto de miniatura en tamaño más grande").

        Es el mismo patrón que `PanelComparablesCTk._ver_foto_grande` y que
        `_mostrar_imagen_flotante` del paso 5, `_traer_al_frente` incluido:
        sin eso la ventana nace DETRÁS de la principal (Windows le devuelve el
        foco al widget que se clicó) y el comprador cree que el clic no hizo
        nada."""
        if not path:
            return
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return
        ventana = ctk.CTkToplevel(self)
        ventana.title(titulo)
        ventana.configure(fg_color=PAR_VISOR_BG)
        mostrar = im.copy()
        mostrar.thumbnail((max(self.winfo_screenwidth() - 260, 300),
                           max(self.winfo_screenheight() - 260, 300)))
        foto = ImageTk.PhotoImage(mostrar)
        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta
        lbl.pack(padx=10, pady=(10, 4))
        Etiqueta(ventana, text=f"{titulo}  ·  {im.width} × {im.height} px",
                 style="Suave.TLabel").pack(pady=(0, 10))
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    def _confirmar_eliminar_variante(self, cand: dict, variante_id: int) -> None:
        if not messagebox.askyesno("Asistente de Compras",
                "¿Quitar este color de la referencia? El resto del candidato se conserva."):
            return
        with reloj(self):
            try:
                self._mc.eliminar_variante(variante_id)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo quitar el color:\n{exc}")
                return
        self._recargar()

    def _confirmar_eliminar_candidato(self, cand: dict) -> None:
        codigo = codigo_visible(cand.get("codigo_proveedor")) or f"Candidato {cand['candidato_id']}"
        if not messagebox.askyesno("Asistente de Compras",
                f"¿Eliminar por completo la referencia {codigo}? No se puede deshacer, y si "
                "reenviás este mismo catálogo no va a volver a aparecer."):
            return
        with reloj(self):
            try:
                self._mc.eliminar_candidato(cand["candidato_id"])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras", f"No se pudo eliminar:\n{exc}")
                return
        self._recargar()

    def _fila_precio(self, info, cand: dict) -> None:
        """Precio de venta esperado + su fuente (plan 2026-09-07, paso 6) --
        mismo patrón que `_fila_ambigua`: se muestra editable inline, con un
        botón ✓ para fijarlo a mano. Fijar un precio manual gana siempre
        sobre la cascada automática (`precio_venta_referencia`) y se refleja
        de inmediato en el Optimizador de Compra."""
        precio, fuente = self._mc.obtener_precio_referencia(cand["candidato_id"])
        marco = Marco(info)
        marco.pack(anchor="w", fill="x")

        # Renombrada (2026-09-09, pedido del usuario): decía "Precio venta" a
        # secas y se confundía con el precio sugerido por la fórmula de costo,
        # que es OTRA cosa y está justo debajo (`_bloque_precio_costo`). Este
        # número sale de lo que ya se vende en el catálogo propio / de los
        # comparables encontrados, no del costo que cotizó este proveedor.
        # …y ahora el rótulo lo decide la FUENTE real del número: si salió del
        # costo que el comprador ingresó para esta referencia, dice "estimado
        # por costo y margen real"; si salió de los comparables internos, lo
        # dice así. Las dos etiquetas nunca aparecen como si fueran lo mismo
        # (ver `texto_precio_referencia`).
        texto = texto_precio_referencia(precio, fuente,
                                        por_costo_lote=self._precio_vino_del_costo(cand))
        Etiqueta(marco, text=texto, style="Suave.TLabel", wraplength=560,
                 justify="left").pack(side="left")

        # 2026-09-10: acá vivía un cuadro "Fijar a mano ₡:" con su ✓ y su
        # Quitar. Se eliminó por pedido del usuario. El precio de venta ya no
        # se teclea a mano en esta tarjeta: se DERIVA del costo del proveedor
        # (`_bloque_precio_costo`, justo abajo), que es la decisión real que
        # toma el comprador -- y ese bloque ya escribe `precio_venta_manual`
        # por la fórmula del lote, así que no se pierde ninguna capacidad.
        # Este renglón queda como lo que siempre debió ser: informativo.

    def _confirmar_ambiguo(self, cand: dict, categoria: str | None = None, genero: str | None = None) -> None:
        if not categoria and not genero:
            return
        with reloj(self):
            try:
                self._mc.asignar_categoria(cand["candidato_id"], categoria=categoria or None,
                                           genero=genero or None)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras", f"No se pudo confirmar:\n{exc}")
                return
        self._recargar()

    def _recargar(self) -> None:
        """Vuelve a pedir los candidatos (ya con la confirmación aplicada) y
        reconstruye la pantalla entera -- más simple y robusto que parchear
        in situ las tarjetas afectadas. Se delega en la ventana principal
        (que es la dueña del pack/pack_forget de las pantallas) cuando está
        embebida; si se construyó suelta (pruebas), se reconstruye sola en el
        mismo master."""
        master = self.master
        # Embebida en la ventana principal, se le pide a ELLA la lista: es la
        # que sabe de qué proveedor y de qué catálogo es el lote abierto (sin
        # eso, recargar la pantalla traía de vuelta los candidatos de todos
        # los proveedores que hubiera en staging).
        del_lote = getattr(master, "_candidatos_del_lote", None)
        candidatos = del_lote() if callable(del_lote) else self._mc.obtener_candidatos()
        anfitrion = getattr(master, "_mostrar_vista_candidatos", None)
        if callable(anfitrion):
            anfitrion(candidatos)
            return
        info_pack = self.pack_info() if self.winfo_manager() == "pack" else None
        al_volver = self._al_volver
        self.destroy()
        nueva = VentanaCandidatosCTk(master, candidatos, al_volver=al_volver)
        nueva.pack(**(info_pack or {"fill": "both", "expand": True}))

    def _cargar_foto_candidato(self, cand: dict) -> ImageTk.PhotoImage | None:
        colores = cand.get("colores_vista") or []
        if not colores:
            return None
        primero = colores[0]
        url = primero.get("url", "")
        if not url.startswith("/crop_variante/"):
            return None
        try:
            variante_id = int(url.rsplit("/", 1)[-1])
        except ValueError:
            return None
        # 114px para llenar el hueco de 120 de la tarjeta (antes 90 en un
        # hueco de 96): la foto es lo que el comprador mira primero.
        return _cargar_foto_generico(self._mc.ruta_foto_variante(variante_id),
                                     tam=(114, 114))

    # ---------- comparables ----------

    def _cerrar_comparables(self) -> None:
        """Cierra la ventana de comparables si está abierta. Es el `al_cerrar`
        del panel (la ✕ de adentro) y también lo que se llama antes de abrir
        otro candidato: nunca hay dos ventanas de comparables apiladas."""
        self._limpiar_panel_comparables()

    # Nombre histórico: la vista vacía del panel lateral ya no existe (los
    # comparables viven en su propia ventana), pero varios lugares llamaban a
    # esto como "volver al estado sin candidato seleccionado", que hoy es
    # simplemente cerrar la ventana.
    _mostrar_marcador_comparables = _cerrar_comparables

    def _limpiar_panel_comparables(self) -> None:
        self._panel_comparables_contenido = None
        win = getattr(self, "_win_comparables", None)
        if win is not None:
            self._win_comparables = None
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _mostrar_desglose_score(self, cand: dict) -> None:
        """Diálogo simple (no forma parte de la navegación principal, es una
        consulta puntual) con el desglose completo del score: la frase
        reconstruible a mano + los vecinos reales, con foto y unidades
        vendidas -- plan 2026-09-07, paso 9.

        Mismo cierre contra el doble clic que `_abrir_comparables`: la letra
        del grado también dispara una consulta lenta a la base."""
        if getattr(self, "_abriendo_desglose", False):
            return
        self._abriendo_desglose = True
        try:
            self._mostrar_desglose_score_real(cand)
        finally:
            self._abriendo_desglose = False

    def _mostrar_desglose_score_real(self, cand: dict) -> None:
        with reloj(self):
            try:
                d = self._mc.obtener_desglose_score(cand["candidato_id"])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo calcular el desglose:\n{exc}")
                return
        if d is None:
            messagebox.showinfo("Asistente de Compras", "Este candidato no tiene score todavía.")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(f"¿Por qué {d['clasificacion']}? — "
                      f"{codigo_visible(cand.get('codigo_proveedor')) or cand['candidato_id']}")
        _centrar_en_ventana_principal(self, ventana, 620, 520)
        Etiqueta(ventana, text=d["frase"], style="Suave.TLabel", justify="left",
                wraplength=580).pack(anchor="w", padx=16, pady=(14, 4), fill="x")
        Etiqueta(ventana,
                text=f"filtro: {d['filtro_aplicado'] or '—'}  ·  k_efectivo: {d['k_efectivo']:.2f}  ·  "
                     f"fórmula {d['version_formula'] or '—'}",
                style="Suave.TLabel").pack(anchor="w", padx=16, pady=(0, 10))

        Etiqueta(ventana, text="Similares que produjeron este score (ordenados por peso):",
                style="Subtitulo.TLabel").pack(anchor="w", padx=16, pady=(0, 6))

        contenedor = ctk.CTkScrollableFrame(ventana, fg_color="transparent")
        contenedor.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        fotos_tk: list[ImageTk.PhotoImage] = []
        for v in d["vecinos"]:
            fila = Tarjeta(contenedor)
            fila.pack(fill="x", pady=3)
            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6, width=56, height=56)
            hueco.pack(side="left", padx=8, pady=8)
            hueco.pack_propagate(False)
            foto_tk = _cargar_foto_generico(
                self._mc.ruta_foto_imagen(v["imagen_id"]) if v.get("imagen_id") else None, tam=(52, 52))
            if foto_tk is not None:
                fotos_tk.append(foto_tk)
                tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0).pack(fill="both", expand=True)
            else:
                Etiqueta(hueco, text="—", style="Suave.TLabel").pack(fill="both", expand=True)
            info = Marco(fila)
            info.pack(side="left", fill="both", expand=True, pady=8)
            Etiqueta(info, text=v["codigo_tuc"], style="Suave.TLabel").pack(anchor="w")
            Etiqueta(info, text=f"similitud {v['similitud']:.3f}  ·  peso {v['peso']:.3f}  ·  "
                     f"{v['unidades_vendidas']} vendidas  ·  método: {v['metodo']}",
                    style="Suave.TLabel").pack(anchor="w")
        ventana._fotos_tk = fotos_tk  # referencias vivas, evitar garbage collection
        _traer_al_frente(ventana)  # mismo problema que los comparables: nacía detrás

    def _abrir_comparables(self, cand: dict, disparador=None, indice: int | None = None) -> None:
        """Abre los comparables del candidato en su PROPIA ventana,
        redimensionable (2026-09-09). Se reutiliza una sola ventana: hacer clic
        en otro candidato reemplaza el contenido en vez de apilar ventanas.

        Armar el panel consulta el índice permanente de similitud y carga las
        fotos: tarda, y sin protección un segundo clic impaciente disparaba una
        SEGUNDA consulta en paralelo (2026-09-10, pedido del usuario). Dos
        cierres: `self._abriendo_comparables` corta cualquier reentrada (el
        botón, la letra del grado, el clic en la tarjeta -- todos llegan acá), y
        `disparador` se deshabilita mientras dura, para que además se VEA que
        el clic fue tomado. Los dos se levantan en el `finally`."""
        if getattr(self, "_abriendo_comparables", False):
            return
        self._abriendo_comparables = True
        try:
            self._abrir_comparables_real(cand, disparador, indice)
        finally:
            self._abriendo_comparables = False
            if disparador is not None:
                try:
                    disparador.configure(state="normal")
                except (tk.TclError, ValueError):
                    pass

    def _abrir_comparables_real(self, cand: dict, disparador=None,
                                indice: int | None = None) -> None:
        if disparador is not None:
            try:
                disparador.configure(state="disabled")
                disparador.update_idletasks()
            except (tk.TclError, ValueError):
                pass
        self._limpiar_panel_comparables()

        win = ctk.CTkToplevel(self)
        self._win_comparables = win
        codigo = codigo_visible(cand.get("codigo_proveedor")) or cand["candidato_id"]
        win.title(f"Comparables — {codigo}")
        # Generosa a propósito: acá entran DOS listas de comparables con foto
        # (marca reconocida y TUCALZADO vendido) más el cuadro de cotización.
        _centrar_en_ventana_principal(self, win, 1040, 800)
        win.minsize(720, 520)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._cerrar_comparables)

        # Construir el panel consulta similitud contra el índice permanente y
        # carga las fotos de los comparables: es la operación más lenta de esta
        # pantalla, y sin reloj de arena parecía que el clic no había hecho nada.
        with reloj(self):
            panel = PanelComparablesCTk(win, self._mc, cand,
                                        al_cerrar=self._cerrar_comparables,
                                        precio_por_costo=self._precio_vino_del_costo(cand),
                                        indice_inicial=indice)
            panel.pack(fill="both", expand=True)
        self._panel_comparables_contenido = panel
        # Al frente sin robarle el foco de forma permanente a la app (un
        # `grab_set` acá impediría seguir usando la lista de candidatos, que es
        # justo lo que el comprador quiere hacer con la ventana abierta).
        #
        # `lift()` solo NO alcanzaba (2026-09-09, reclamo del usuario: "queda
        # detrás y hay que buscarla en la barra de tareas"): en Windows, una
        # ventana nueva del mismo proceso no se pone encima de la que tiene el
        # foco solo por hacer `lift`. `_traer_al_frente` usa el patrón
        # `-topmost` ON/OFF, que la trae al frente UNA vez sin dejarla pegada
        # encima para siempre.
        _traer_al_frente(win)

    # ---------- pestaña Optimizador de Compra (PDV objetivo → pedido sugerido) ----------

    def _rango_pdv_estimado(self) -> tuple[float, float, int, int]:
        """Rango orientativo de PDV que se puede armar con el catálogo que hay
        cargado ahora mismo (plan 2026-09-07, paso 8, punto 6).

        Es una ESTIMACIÓN, no una promesa: mínimo = un empaque de cada
        candidato grado S/A con precio de venta calculable; máximo = un
        empaque de TODOS los candidatos con precio calculable. El empaque
        ausente cuenta como 1 par, igual que hace `api_pedido_sugerido`.
        Devuelve (minimo, maximo, n_min, n_max)."""
        minimo = maximo = 0.0
        n_min = n_max = 0
        for cand in self._candidatos:
            try:
                precio, _fuente = self._mc.obtener_precio_referencia(cand["candidato_id"])
            except Exception:  # noqa: BLE001
                continue
            if not precio:
                continue
            empaque = cand.get("empaque_cantidad") or 1
            monto = float(precio) * int(empaque)
            maximo += monto
            n_max += 1
            if cand.get("grado") in ("S", "A"):
                minimo += monto
                n_min += 1
        return minimo, maximo, n_min, n_max

    def _construir_tab_optimizador(self, tab) -> None:
        # Antes del campo de PDV objetivo: con qué rango de pedido se puede
        # trabajar con este catálogo. Sin esto, el comprador tecleaba una
        # cifra a ciegas y descubría después que no alcanzaba (o que sobraba).
        minimo, maximo, n_min, n_max = self._rango_pdv_estimado()
        if n_max:
            texto_rango = (f"Con este catálogo podés armar un pedido de entre "
                           f"₡{minimo:,.0f} ({n_min} referencia(s) grado S/A) y "
                           f"₡{maximo:,.0f} ({n_max} referencia(s) con precio calculable), "
                           f"contando un empaque de cada una. Es una estimación orientativa.")
        else:
            texto_rango = ("Todavía ninguna referencia de este catálogo tiene precio de "
                           "venta calculable, así que no se puede estimar un rango de pedido.")
        Etiqueta(tab, text=texto_rango, style="Suave.TLabel", wraplength=880,
                 justify="left").pack(anchor="w", padx=4, pady=(10, 0))

        # Con qué mes de venta se armó este sugerido (2026-09-09). El mes se
        # pregunta al entrar al paso 6 (`asegurar_mes_venta_lote`) y queda en
        # los metadatos del lote; acá se muestra para que el número del pedido
        # nunca quede sin la temporada que lo justifica, y se puede corregir
        # sin salir del paso.
        self._fila_mes_venta = Marco(tab)
        self._fila_mes_venta.pack(fill="x", padx=4, pady=(6, 0))
        self._etq_mes_venta = Etiqueta(self._fila_mes_venta, text="", style="Suave.TLabel")
        self._etq_mes_venta.pack(side="left")
        Boton(self._fila_mes_venta, text="Cambiar", style="Sutil.TButton", width=64,
              command=self._cambiar_mes_venta).pack(side="left", padx=(8, 0))
        self._pintar_mes_venta()

        # Bloque de CIERRE del proceso (2026-09-09): el paso 7 es el final del
        # asistente, pero se veía igual que cualquier pantalla del medio. Se
        # llena al generar la selección (antes no hay números que celebrar) y
        # queda arriba, donde primero cae la vista.
        self._marco_cierre = Marco(tab)
        self._marco_cierre.pack(fill="x", padx=4, pady=(8, 0))

        controles = Marco(tab)
        controles.pack(fill="x", padx=4, pady=(10, 6))
        Etiqueta(controles, text="PDV Objetivo (₡)", style="Suave.TLabel").pack(side="left")
        self._var_pdv = tk.StringVar()
        entrada = ctk.CTkEntry(controles, textvariable=self._var_pdv, width=140,
                               placeholder_text="ej. 500000")
        entrada.pack(side="left", padx=(8, 8))
        Boton(controles, text="Generar selección", style="Primario.TButton",
             command=self._generar_pedido).pack(side="left")
        # Exportar solo tiene sentido con una selección ya generada: nace
        # apagado y lo enciende `_generar_pedido`.
        self._btn_exportar_excel = Boton(controles, text="📊 Exportar a Excel",
                                         command=self._exportar_sugerido_excel)
        self._btn_exportar_excel.pack(side="left", padx=(8, 0))
        self._btn_exportar_excel.state(["disabled"])
        self._ultimo_sugerido: list[dict] = []

        self._resumen_optimizador = Marco(tab)
        self._resumen_optimizador.pack(fill="x", padx=4, pady=(0, 8))

        self._tabla_optimizador = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self._tabla_optimizador.pack(fill="both", expand=True)

    def _mes_venta(self) -> int | None:
        leer = getattr(self.master, "mes_venta_lote", None)
        return leer() if callable(leer) else None

    def _pintar_mes_venta(self) -> None:
        mes = self._mes_venta()
        nombres = getattr(self.master, "MESES_ES", None)
        nombre = nombres[mes - 1] if (mes and nombres) else None
        self._etq_mes_venta.configure(
            text=(f"Mes de venta esperado de este pedido: {nombre} — las categorías "
                  "de temporada pesan más (o menos) en el reparto."
                  if nombre else
                  "Mes de venta esperado de este pedido: sin declarar (reparto sin "
                  "ajuste de temporada)."))

    def _cambiar_mes_venta(self) -> None:
        preguntar = getattr(self.master, "asegurar_mes_venta_lote", None)
        if callable(preguntar):
            preguntar(forzar=True)
        self._pintar_mes_venta()

    def _generar_pedido(self) -> None:
        try:
            pdv_objetivo = float(self._var_pdv.get())
            if pdv_objetivo <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Asistente de Compras", "Ingresá un PDV objetivo válido.")
            return

        with reloj(self):
            try:
                # El mes de venta esperado (paso 6) ajusta el peso de cada
                # candidato por la estacionalidad real de su categoría; sin mes
                # declarado el reparto es el de siempre (score puro).
                r = self._mc.obtener_pedido_sugerido(pdv_objetivo,
                                                     mes_venta=self._mes_venta())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo calcular el pedido:\n{exc}")
                return

        for w in self._resumen_optimizador.winfo_children():
            w.destroy()
        for w in self._tabla_optimizador.winfo_children():
            w.destroy()

        con_cantidad = [c for c in r["candidatos"] if c.get("cantidad_sugerida")]

        for texto, valor in (
            ("Candidatos con pedido", str(len(con_cantidad))),
            ("Con score pero sin ningún precio de referencia posible", str(r["n_sin_precio"])),
            ("Monto real del pedido", f"₡{r['monto_real_total']:,.0f}"),
            ("vs. objetivo", f"{(r['monto_real_total'] / pdv_objetivo * 100):.0f}%"),
        ):
            kpi = Marco(self._resumen_optimizador)
            kpi.pack(side="left", padx=(0, 22))
            Etiqueta(kpi, text=valor, style="Subtitulo.TLabel").pack(anchor="w")
            Etiqueta(kpi, text=texto, style="Suave.TLabel").pack(anchor="w")

        if not con_cantidad:
            self._ultimo_sugerido = []
            self._btn_exportar_excel.state(["disabled"])
            self._pintar_cierre([], 0.0)
            Etiqueta(self._tabla_optimizador,
                    text="Ningún candidato tiene costo/moneda declarados suficientes "
                         "para convertir el PDV en unidades.",
                    style="Suave.TLabel").pack(padx=8, pady=8)
            return

        self._ultimo_sugerido = con_cantidad
        self._btn_exportar_excel.state(["!disabled"])
        self._pintar_cierre(con_cantidad, r["monto_real_total"])
        self._pintar_tabla_sugerido(con_cantidad)

    # ---------- cierre del proceso + exportación ----------

    def _nombre_proveedor_lote(self) -> str:
        """Proveedor del catálogo que está en revisión, para el encabezado de
        cierre y el nombre del Excel. Sale del mismo resumen que ya se muestra
        arriba en el paso 6."""
        try:
            resumen = self._mc.resumen_catalogo_activo()
        except Exception:  # noqa: BLE001
            return "este proveedor"
        return (resumen[0].get("proveedor") or "este proveedor") if resumen else "este proveedor"

    def _pintar_cierre_pendiente(self) -> None:
        """Encabezado del paso 7 ANTES de generar la selección: dice que este es
        el último paso y qué falta para terminarlo."""
        for w in self._marco_cierre.winfo_children():
            w.destroy()
        caja = tk.Frame(self._marco_cierre, bg=solido(PAR_PANEL))
        caja.pack(fill="x")
        tk.Label(caja, text="Último paso: tu sugerido de compra",
                 bg=solido(PAR_PANEL), fg=solido(PAR_TEXTO),
                 font=("Segoe UI Semibold", 14), anchor="w").pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(caja,
                 text=("Escribí cuánto querés invertir (PDV objetivo) y generá la selección. "
                       "Después podés exportarla a Excel."),
                 bg=solido(PAR_PANEL), fg=solido(PAR_NEUTRO), font=("Segoe UI", 10),
                 anchor="w", justify="left", wraplength=820).pack(anchor="w", padx=12, pady=(2, 8))

    def _pintar_cierre(self, con_cantidad: list[dict], monto_total: float) -> None:
        """Encabezado de cierre del paso 7: qué se logró, en una frase.

        No es decoración: el comprador terminaba el proceso sin ninguna señal
        de haber llegado al final -- el paso 7 se veía igual que el 3 o el 4."""
        if not con_cantidad:
            self._pintar_cierre_pendiente()
            return
        for w in self._marco_cierre.winfo_children():
            w.destroy()
        pares = sum(int(c.get("cantidad_sugerida") or 0) for c in con_cantidad)
        caja = tk.Frame(self._marco_cierre, bg=solido(PAR_OK_BG),
                        highlightbackground=solido(PAR_OK), highlightthickness=1)
        caja.pack(fill="x")
        tk.Label(caja, text="✓", bg=solido(PAR_OK_BG), fg=solido(PAR_OK),
                 font=("Segoe UI Semibold", 26)).pack(side="left", padx=(14, 10), pady=10)
        texto = tk.Frame(caja, bg=solido(PAR_OK_BG))
        texto.pack(side="left", fill="both", expand=True, pady=10)
        tk.Label(texto, text="¡Listo! Llegaste al final del proceso.",
                 bg=solido(PAR_OK_BG), fg=solido(PAR_OK),
                 font=("Segoe UI Semibold", 14), anchor="w").pack(anchor="w")
        multi = self._es_multiproveedor()
        destinatario = ("los proveedores de este lote" if multi
                        else self._nombre_proveedor_lote())
        tk.Label(texto,
                 text=(f"Sugerido de compra final para {destinatario}: "
                       f"{len(con_cantidad)} referencias · {pares:,} pares sugeridos · "
                       f"₡{monto_total:,.0f} de inversión estimada."),
                 bg=solido(PAR_OK_BG), fg=solido(PAR_TEXTO),
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=820).pack(anchor="w", pady=(2, 0))
        # FASE 4: el desglose por proveedor. Un total único mezcla plata que se
        # le va a pagar a proveedores distintos, cada uno con su propia moneda
        # de origen (aunque el número final siempre esté en colones), y de ese
        # total no sale ninguna orden de compra: salen dos.
        if multi:
            desglose = self._desglose_por_proveedor(con_cantidad)
            if desglose:
                partes = [f"{nombre}: {n} ref · ₡{monto:,.0f}"
                          for nombre, n, monto in desglose]
                partes.append(f"Total: ₡{monto_total:,.0f}")
                tk.Label(texto, text="   ·   ".join(partes),
                         bg=solido(PAR_OK_BG), fg=solido(PAR_TEXTO),
                         font=("Segoe UI Semibold", 10), anchor="w", justify="left",
                         wraplength=820).pack(anchor="w", pady=(4, 0))

    def _desglose_por_proveedor(self, con_cantidad: list[dict]) -> list[tuple]:
        """`[(nombre, n_referencias, monto)]` en el orden de aparición de los
        proveedores del lote. El monto es el mismo `monto_real` que ya suma la
        tabla, así que el desglose no puede discrepar del total."""
        por_id = {c["candidato_id"]: c for c in self._candidatos}
        acum: dict = {}
        for c in con_cantidad:
            pid, nombre = self._proveedor_de(por_id.get(c["candidato_id"], {}))
            n, monto = acum.get(pid, (0, 0.0))
            acum[pid] = (n + 1, monto + float(c.get("monto_real") or 0))
        salida = []
        for pid in self._proveedores_en_pantalla():
            if pid in acum:
                n, monto = acum[pid]
                salida.append((self._nombre_de_proveedor(pid) or "sin proveedor",
                               n, monto))
        return salida

    def _carpeta_lote(self) -> Path | None:
        salida = getattr(self.master, "_salida", None)
        return Path(salida) if salida else None

    def _exportar_sugerido_excel(self) -> None:
        """El sugerido de compra como .xlsx real, en la carpeta del lote.

        Mismas columnas que la tabla en pantalla (foto, código de referencia,
        referencia, cantidad, precio de venta, PVD estimado) y en el mismo
        orden -- los números se toman de `self._ultimo_sugerido`, o sea de la
        misma selección que se está viendo: el Excel no puede discrepar de la
        pantalla porque no recalcula nada.

        La foto va EMBEBIDA en la celda (`openpyxl.drawing.image.Image`) a
        partir del mismo archivo ya limpio que pinta la miniatura."""
        if not self._ultimo_sugerido:
            messagebox.showinfo("Asistente de Compras",
                                "Generá primero la selección con «Generar selección».")
            return
        carpeta = self._carpeta_lote()
        if carpeta is None:
            messagebox.showerror("Asistente de Compras",
                                 "No hay carpeta de trabajo para este lote.")
            return

        with reloj(self):
            try:
                destino = self._escribir_excel_sugerido(carpeta)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo exportar el Excel:\n{exc}")
                return
        self._confirmar_exportacion(destino)

    def _escribir_excel_sugerido(self, carpeta: Path) -> Path:
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as ImagenXL
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        datos = self._leer_marca_lote_local()
        # FASE 5: con dos catálogos en el lote, nombrar el archivo con UN
        # proveedor miente -- adentro hay referencias de los dos. Si el lote no
        # tiene nombre de proyecto, se usa el nombre de la carpeta del lote.
        # Con un solo proveedor el nombre sigue siendo el suyo, igual que antes.
        por_defecto = self._nombre_proveedor_lote()
        if self._es_multiproveedor():
            por_defecto = (self._carpeta_lote().name if self._carpeta_lote()
                           else "varios proveedores")
        nombre_proyecto = _nombre_archivo_seguro(
            datos.get("nombre_proyecto") or por_defecto)
        destino = carpeta / (f"Sugerido_de_compra_{nombre_proyecto}_"
                             f"{datetime.now():%Y-%m-%d}.xlsx")

        wb = Workbook()
        ws = wb.active
        ws.title = "Sugerido de compra"

        columnas = self._columnas_sugerido()
        rotulos = [r for r, *_ in columnas]
        ws.append(rotulos)
        for col in range(1, len(rotulos) + 1):
            celda = ws.cell(row=1, column=col)
            celda.font = Font(bold=True)
            celda.fill = PatternFill("solid", fgColor="EEEEEE")
            celda.alignment = Alignment(horizontal="center", vertical="center")
        # Anchos pensados para la foto (columna A) y para que no haya que
        # ajustar nada a mano al abrirlo.
        anchos = {"foto": 12, "codigo": 22, "referencia": 34, "cantidad": 10,
                  "precio": 16, "pvd": 18, "proveedor": 22}
        for i, (_rotulo, clave, *_r) in enumerate(columnas, start=1):
            ws.column_dimensions[get_column_letter(i)].width = anchos.get(clave, 16)
        ws.freeze_panes = "A2"

        por_id = {c["candidato_id"]: c for c in self._candidatos}
        # Las imágenes se abren con PIL y se reescalan antes de incrustarlas:
        # las fotos limpias son de 800x800 y meterlas a tamaño real haría un
        # archivo de decenas de MB para un pedido de 40 referencias.
        temporales: list[Path] = []
        alto_px = 72
        for i, c in enumerate(self._ultimo_sugerido, start=2):
            cand = por_id.get(c["candidato_id"], {})
            precio = c.get("precio_venta_estimado")
            pvd = c.get("monto_real")
            # Las celdas se escriben RECORRIENDO las mismas columnas que pinta
            # la pantalla (no por índice fijo): así, cuando la Fase 4 inserta
            # la columna «Proveedor» en un lote multi-proveedor, el Excel la
            # trae en la misma posición y nada se corre de lugar.
            valores = {
                "codigo": codigo_visible(c.get("codigo_proveedor")) or "—",
                "proveedor": self._proveedor_de(cand)[1] or "—",
                "referencia": cand.get("linea_declarada") or "—",
                "cantidad": int(c.get("cantidad_sugerida") or 0),
                "precio": float(precio) if precio else None,
                "pvd": float(pvd) if pvd is not None else None,
            }
            for col, (_rotulo, clave, *_r) in enumerate(columnas, start=1):
                if clave == "foto":
                    continue
                celda = ws.cell(row=i, column=col, value=valores.get(clave))
                if clave in ("precio", "pvd"):
                    celda.number_format = '"₡"#,##0'
            ws.row_dimensions[i].height = alto_px * 0.78  # px → puntos

            ruta = self._ruta_foto_para_excel(cand)
            if ruta is None:
                continue
            try:
                with Image.open(ruta) as im:
                    im = im.convert("RGB")
                    im.thumbnail((alto_px, alto_px))
                    tmp = carpeta / f"_xl_foto_{c['candidato_id']}.png"
                    im.save(tmp, "PNG")
                temporales.append(tmp)
                img = ImagenXL(str(tmp))
                ws.add_image(img, f"A{i}")
            except Exception:  # noqa: BLE001
                continue  # una foto ilegible no puede impedir la exportación

        wb.save(destino)
        # Los PNG temporales solo hacen falta hasta el `save` (openpyxl copia
        # los bytes dentro del .xlsx), así que se borran para no dejar basura
        # en la carpeta del lote.
        for tmp in temporales:
            tmp.unlink(missing_ok=True)
        return destino

    def _leer_marca_lote_local(self) -> dict:
        leer = getattr(self.master, "_leer_marca_lote", None)
        carpeta = self._carpeta_lote()
        if callable(leer) and carpeta is not None:
            try:
                return leer(carpeta)
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _ruta_foto_para_excel(self, cand: dict) -> Path | None:
        """El archivo real de la foto limpia del candidato -- la variante base,
        la misma que muestra la miniatura de la tabla."""
        colores = (cand or {}).get("colores_vista") or []
        if not colores:
            return None
        # Misma lectura que `_cargar_foto_candidato`: el id de la variante
        # viaja dentro de la URL que arma `api_candidatos` para la web
        # (`/crop_variante/<id>`), no como campo aparte.
        url = colores[0].get("url", "")
        if not url.startswith("/crop_variante/"):
            return None
        try:
            ruta = self._mc.ruta_foto_variante(int(url.rsplit("/", 1)[-1]))
        except Exception:  # noqa: BLE001  (incluye el int() de una URL rara)
            return None
        return ruta if ruta and ruta.exists() else None

    def _confirmar_exportacion(self, destino: Path) -> None:
        """Confirmación con la ruta a la vista y un botón para abrir la
        carpeta -- decir solo "listo" obliga al comprador a ir a buscar el
        archivo a ciegas."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Sugerido de compra exportado")
        _centrar_en_ventana_principal(self, ventana, 620, 220)
        Etiqueta(ventana, text="✓ Sugerido de compra exportado",
                 style="Titulo.TLabel").pack(anchor="w", padx=18, pady=(16, 6))
        Etiqueta(ventana, text=str(destino), style="Suave.TLabel",
                 wraplength=560, justify="left").pack(anchor="w", padx=18)
        fila = Marco(ventana)
        fila.pack(anchor="w", padx=18, pady=16)
        Boton(fila, text="Abrir carpeta", style="Primario.TButton",
              command=lambda: os.startfile(str(destino.parent))).pack(side="left")
        Boton(fila, text="Abrir el Excel",
              command=lambda: os.startfile(str(destino))).pack(side="left", padx=(8, 0))
        Boton(fila, text="Cerrar", style="Sutil.TButton",
              command=ventana.destroy).pack(side="left", padx=(8, 0))
        _traer_al_frente(ventana)

    # Columnas del sugerido de compra, en el orden que pidió el usuario
    # (2026-09-09). Antes era una tira de `Etiqueta(width=N).pack(side="left")`
    # por fila: `width` en un ttk.Label es en CARACTERES, así que las columnas
    # se desalineaban en cuanto un texto se pasaba de largo -- se veía como una
    # lista corrida, no como una tabla. Ahora es `grid` de verdad: una columna
    # por dato, con el mismo peso en el encabezado y en cada fila, así que
    # quedan alineadas siempre.
    #
    # (rótulo, clave interna, alineación, peso, ancho mínimo)
    COLUMNAS_SUGERIDO = (
        ("Foto", "foto", "center", 0, 76),
        ("Código de referencia", "codigo", "w", 0, 150),
        ("Referencia", "referencia", "w", 0, 240),
        ("Cantidad", "cantidad", "e", 0, 90),
        ("Precio de venta", "precio", "e", 0, 120),
        ("PVD estimado", "pvd", "e", 0, 130),
    )

    # FASE 4 (2026-09-16): con dos catálogos en el mismo lote, el sugerido
    # mezcla referencias de proveedores que cotizaron en MONEDAS distintas.
    # Saber de quién es cada fila deja de ser un lujo: es lo que permite
    # convertir esta tabla en dos órdenes de compra.
    COLUMNA_PROVEEDOR = ("Proveedor", "proveedor", "w", 0, 130)

    def _columnas_sugerido(self) -> tuple:
        """Las columnas del paso 7. Con un solo proveedor son exactamente las
        de siempre; con varios se inserta «Proveedor» justo después de la foto
        (y el Excel exportado queda igual que la pantalla, porque los dos leen
        de acá)."""
        cols = list(self.COLUMNAS_SUGERIDO)
        if self._es_multiproveedor():
            cols.insert(1, self.COLUMNA_PROVEEDOR)
        return tuple(cols)

    def _pintar_tabla_sugerido(self, con_cantidad: list[dict]) -> None:
        """La tabla del paso 7, tabulada con `grid`.

        "PVD estimado" (precio de venta al detalle de la línea) es
        `cantidad_sugerida × precio_venta_estimado` -- el campo `monto_real`
        que ya calcula `api_pedido_sugerido`. No se recalcula acá para que no
        puedan divergir dos números que tienen que ser el mismo."""
        tabla = self._tabla_optimizador
        columnas = self._columnas_sugerido()
        for col, (_rotulo, _clave, _anclaje, peso, minimo) in enumerate(columnas):
            tabla.grid_columnconfigure(col, weight=peso, minsize=minimo)
        # Columna de relleno al final: se queda con TODO el espacio sobrante,
        # así las columnas de datos quedan juntas a la izquierda y se leen como
        # una tabla. Sin esto, el sobrante se reparte entre las columnas reales
        # y los montos terminan despegados al otro extremo de la pantalla.
        tabla.grid_columnconfigure(len(columnas), weight=1)

        for col, (rotulo, _clave, anclaje, _peso, _minimo) in enumerate(columnas):
            tk.Label(tabla, text=rotulo, font=("Segoe UI Semibold", 11),
                    fg=solido(PAR_TEXTO), bg=solido(PAR_PANEL), anchor=anclaje,
                    padx=6, pady=6).grid(row=0, column=col, sticky="ew")
        Separador(tabla).grid(row=1, column=0, columnspan=len(columnas),
                              sticky="ew", pady=(0, 2))

        # Índice por candidato_id para sacar la foto y el nombre: el pedido
        # sugerido devuelve códigos y montos, no fotos, pero esta pantalla ya
        # tiene los candidatos completos en memoria.
        por_id = {c["candidato_id"]: c for c in self._candidatos}

        for i, c in enumerate(con_cantidad):
            fila_grid = i + 2
            cand = por_id.get(c["candidato_id"], {})

            hueco = ctk.CTkFrame(tabla, fg_color=PAR_VISOR_BG, corner_radius=4,
                                 width=64, height=64)
            hueco.grid(row=fila_grid, column=0, padx=6, pady=4)
            hueco.grid_propagate(False)
            foto_tk = self._cargar_foto_candidato(cand) if cand else None
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0).pack(fill="both", expand=True)
            else:
                Etiqueta(hueco, text="sin foto", style="Suave.TLabel").pack(
                    fill="both", expand=True)

            # "Referencia" descriptiva: la línea declarada del catálogo del
            # proveedor. Si no vino, se dice "—" en vez de repetir el código
            # (que ya está en su propia columna).
            referencia = cand.get("linea_declarada") or "—"
            precio = c.get("precio_venta_estimado")
            pvd = c.get("monto_real")
            valores = {
                "codigo": codigo_visible(c.get("codigo_proveedor")) or "—",
                "referencia": referencia,
                "cantidad": str(c.get("cantidad_sugerida") or 0),
                "precio": f"₡{precio:,.0f}" if precio else "—",
                "pvd": f"₡{pvd:,.0f}" if pvd is not None else "—",
            }
            for col, (_rotulo, clave, anclaje, _peso, _minimo) in enumerate(columnas):
                if clave == "foto":
                    continue
                if clave == "proveedor":
                    # La MISMA pastilla (y el mismo color) que en el paso 6:
                    # la fila del sugerido se reconoce de un ojo como la
                    # tarjeta de donde salió.
                    self._pastilla_proveedor(tabla, cand).grid(
                        row=fila_grid, column=col, sticky="w", padx=6, pady=4)
                    continue
                Etiqueta(tabla, text=valores[clave], anchor=anclaje,
                        justify=("right" if anclaje == "e" else "left")).grid(
                    row=fila_grid, column=col, sticky="ew", padx=6, pady=4)


class PanelComparablesCTk(ctk.CTkFrame):
    """Comparables de un candidato -- marca reconocida (con precio de
    competencia) y TUCALZADO ya vendido (con precio/unidades reales). Misma
    consulta que `/api/similar_marca`/`/api/similar_tuc` en la web, mostrada
    nativamente. Si el candidato trae varias variantes de color, un selector
    permite recalcular por cada una (mismo criterio que la web: comparable
    independiente por color, no solo la variante 0).

    Plan 2026-09-07, paso 8: era `VentanaComparablesCTk`, un TERCER Toplevel
    apilado sobre la ventana de candidatos, que a su vez estaba sobre la
    principal. Ahora es un panel lateral dentro de la misma pantalla."""

    def __init__(self, master: tk.Misc, motor_candidatos, cand: dict, al_cerrar=None,
                 precio_por_costo: bool = False, indice_inicial: int | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._mc = motor_candidatos
        self._cand = cand
        self._al_cerrar = al_cerrar
        # Si el precio de referencia de este candidato salió del costo que el
        # comprador ingresó en el paso 6 (lo sabe la pantalla que abre este
        # panel, que es la dueña del respaldo local), el rótulo lo tiene que
        # decir. Default False = comportamiento anterior.
        self._precio_por_costo = precio_por_costo
        self._fotos_tk: list[ImageTk.PhotoImage] = []

        colores = cand.get("colores_vista") or [{"indice": 0, "color_principal": "—"}]
        self._colores = colores

        # `grid` en vez de `pack` (2026-09-09): con pack, los bloques de alto
        # fijo se servían primero por orden de empaquetado y la lista de
        # comparables (la última, la que tiene expand) podía quedarse con 0px
        # -- el bug de "los comparables quedan tapados detrás de Guardar
        # cotización". Con grid, SOLO la fila de la lista tiene weight=1, así
        # que es la única que crece, y su `minsize` le garantiza alto propio
        # aunque la ventana se encoja. Las filas se declaran en orden visual;
        # la cotización es la última fila y por lo tanto siempre visible abajo.
        self.grid_columnconfigure(0, weight=1)
        for r in (0, 1, 2, 4):
            self.grid_rowconfigure(r, weight=0)
        self.grid_rowconfigure(3, weight=1, minsize=220)

        titulo = Marco(self)
        titulo.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        Etiqueta(titulo, text=f"Comparables — "
                 f"{codigo_visible(cand.get('codigo_proveedor')) or cand['candidato_id']}",
                 style="Subtitulo.TLabel").pack(side="left")
        Boton(titulo, text="✕", style="Sutil.TButton", width=30,
              command=self._cerrar).pack(side="right")

        controles = Marco(self)
        controles.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 6))
        Etiqueta(controles, text="Color:", style="Suave.TLabel").pack(side="left")
        self._v_indice = tk.StringVar(value=str(colores[0]["indice"]))
        opciones = [f"{c['indice']} — {c.get('color_principal') or '—'}" for c in colores]
        # `indice_inicial` (2026-09-17): con qué color NACE el panel. Lo manda
        # el botón 👁 de cada miniatura del paso 6 -- cada color tiene sus
        # propios comparables y el comprador pidió llegar directo al que clicó,
        # no al primero. None (botón general, clic en la tarjeta) = primer
        # color, exactamente como antes. Si el índice ya no existe (el color se
        # borró entre el dibujo de la tarjeta y el clic) se degrada al primero
        # en vez de reventar.
        pos = next((k for k, c in enumerate(colores) if c["indice"] == indice_inicial), 0)
        combo = ctk.CTkComboBox(controles, values=opciones, width=180,
                                command=lambda _v: self._recalcular(), state="readonly")
        combo.set(opciones[pos])
        combo.pack(side="left", padx=(8, 0))
        self._combo = combo

        # Precio de venta (plan 2026-09-07, paso 6): acá el comprador tiene los
        # precios de los comparables a la vista (marca reconocida, TUCALZADO ya
        # vendido) -- el lugar natural para editar el precio de referencia, no
        # solo en la tarjeta de la lista.
        self._construir_panel_precio()

        # Los comparables (fila 3, la única con peso) van ANTES de la
        # cotización (fila 4) en el orden visual: la lista es lo que el
        # comprador vino a mirar y ahora tiene todo el alto libre de la
        # ventana, mientras el cuadro de Guardar cotización ocupa su propia
        # franja fija abajo sin poder pisarla.
        self._contenedor = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._contenedor.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # Cotización en franja FIJA abajo (fuera del scroll de comparables) --
        # mismo criterio que la web: "esto es lo único que hace permanente al
        # candidato", así que el botón de Guardar tiene que quedar siempre visible,
        # no perdido al hacer scroll entre comparables de marca y de TUCALZADO.
        self._construir_panel_cotizacion()

        self._recalcular()

    def _cerrar(self) -> None:
        if self._al_cerrar is not None:
            self._al_cerrar()  # el dueño del panel destruye este widget
        else:
            self.destroy()

    def _indice_actual(self) -> int:
        texto = self._combo.get()
        return int(texto.split(" — ", 1)[0])

    # ---------- guardar cotización (promueve el candidato a permanente) ----------

    def _ver_foto_grande(self, path, titulo: str) -> None:
        """Abre la foto de UN comparable en tamaño grande, en su propia
        ventana. Mismo patrón que `_mostrar_imagen_flotante` del visor de
        fotos, incluido `_traer_al_frente`: sin eso la ventana nace DETRÁS de
        la de comparables (Windows le devuelve el foco al widget clicado) y
        parece que el clic no hizo nada -- el bug que se corrigió en el paso 5
        el 2026-09-10."""
        if not path:
            return
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return
        ventana = ctk.CTkToplevel(self)
        ventana.title(f"Comparable — {titulo}")
        ventana.configure(fg_color=PAR_VISOR_BG)
        mostrar = im.copy()
        mostrar.thumbnail((max(self.winfo_screenwidth() - 200, 300),
                           max(self.winfo_screenheight() - 220, 300)))
        foto = ImageTk.PhotoImage(mostrar)
        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta
        lbl.pack(padx=10, pady=(10, 4))
        Etiqueta(ventana, text=f"{titulo}  ·  {im.width} × {im.height} px",
                 style="Suave.TLabel").pack(pady=(0, 10))
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    def _construir_panel_precio(self) -> None:
        """Precio de venta esperado + su fuente, INFORMATIVO.

        2026-09-10 (Tarea 9, pedido del usuario): acá había un "Fijar a mano
        ₡:" con su ✓ y su Quitar. Se eliminó -- el usuario reportó que no le
        servía y que no entendía qué era, y es el mismo control que se sacó de
        la tarjeta del paso 6 (Tarea 3). Nada depende de él: el precio de
        referencia sigue saliendo de la cascada (`precio_venta_referencia`) y
        el camino real para fijarlo es el costo del proveedor en el paso 6,
        que ya escribe `precio_venta_manual` por la fórmula del lote. La
        etiqueta se conserva: es el número contra el que el comprador lee los
        precios de los comparables de abajo."""
        panel = Marco(self)
        panel.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        precio, fuente = self._mc.obtener_precio_referencia(self._cand["candidato_id"])
        texto = texto_precio_referencia(precio, fuente,
                                        por_costo_lote=self._precio_por_costo)
        self._etq_precio = Etiqueta(panel, text=texto, style="Suave.TLabel", wraplength=900,
                                    justify="left")
        self._etq_precio.pack(anchor="w", fill="x")

    def _construir_panel_cotizacion(self) -> None:
        panel = Tarjeta(self)
        panel.grid(row=4, column=0, sticky="ew", padx=8, pady=(6, 8))

        Etiqueta(panel, text="Guardar cotización (esto es lo único que hace "
                "permanente al candidato)", style="Subtitulo.TLabel",
                justify="left").pack(anchor="w", padx=10, pady=(10, 6))

        # Dos filas, no una: mantiene los cinco controles legibles también
        # cuando la ventana se encoge al mínimo.
        fila1 = Marco(panel)
        fila1.pack(fill="x", padx=10, pady=(0, 4))
        Etiqueta(fila1, text="Costo lista", style="Suave.TLabel").pack(side="left")
        self._v_costo_lista = tk.StringVar()
        ctk.CTkEntry(fila1, textvariable=self._v_costo_lista, width=80).pack(side="left", padx=(4, 12))

        Etiqueta(fila1, text="Desc. %", style="Suave.TLabel").pack(side="left")
        self._v_descuento = tk.StringVar(value="0")
        ctk.CTkEntry(fila1, textvariable=self._v_descuento, width=54).pack(side="left", padx=(4, 12))

        Etiqueta(fila1, text="Moneda", style="Suave.TLabel").pack(side="left")
        self._v_moneda = tk.StringVar(value="USD")
        ctk.CTkComboBox(fila1, values=list(MONEDAS_PROVEEDOR), variable=self._v_moneda, width=78,
                        state="readonly").pack(side="left", padx=(4, 0))

        fila2 = Marco(panel)
        fila2.pack(fill="x", padx=10, pady=(0, 4))
        Etiqueta(fila2, text="T. cambio →CRC", style="Suave.TLabel").pack(side="left")
        self._v_tipo_cambio = tk.StringVar()
        ctk.CTkEntry(fila2, textvariable=self._v_tipo_cambio, width=80).pack(side="left", padx=(4, 12))

        Boton(fila2, text="💾 Guardar", style="Primario.TButton",
             command=self._guardar_cotizacion).pack(side="left")

        self._estado_cotizacion = Etiqueta(panel, text="", style="Suave.TLabel",
                                           wraplength=900, justify="left")
        self._estado_cotizacion.pack(anchor="w", padx=10, pady=(0, 10))

    def _guardar_cotizacion(self) -> None:
        try:
            costo_lista = float(self._v_costo_lista.get())
            if costo_lista <= 0:
                raise ValueError
        except ValueError:
            self._estado_cotizacion.configure(text="Ingresá el costo de lista.")
            return
        try:
            descuento_pct = float(self._v_descuento.get() or 0)
        except ValueError:
            descuento_pct = 0.0
        moneda = self._v_moneda.get()
        tipo_cambio_txt = self._v_tipo_cambio.get().strip()
        tipo_cambio_usd_crc = None
        if tipo_cambio_txt:
            try:
                tipo_cambio_usd_crc = float(tipo_cambio_txt)
            except ValueError:
                self._estado_cotizacion.configure(text="Tipo de cambio inválido.")
                return
        if moneda_convierte(moneda) and not tipo_cambio_usd_crc:
            self._estado_cotizacion.configure(
                text=f"Con {moneda} necesitás el tipo de cambio.")
            return

        costo_neto = costo_lista * (1 - descuento_pct / 100)
        self._estado_cotizacion.configure(text="⏳ Guardando...")
        self.update_idletasks()
        # Golpea la base y promueve el candidato a permanente: es de las
        # operaciones más lentas de la pantalla, así que además del texto
        # "⏳ Guardando..." va el reloj de arena.
        with reloj(self):
            try:
                r = self._mc.guardar_cotizacion({
                    "candidato_id": self._cand["candidato_id"],
                    "costo_lista": costo_lista,
                    "descuento_pct": descuento_pct,
                    "costo_neto": costo_neto,
                    "moneda": moneda,
                    "tipo_cambio_usd_crc": tipo_cambio_usd_crc,
                })
            except Exception as exc:  # noqa: BLE001
                self._estado_cotizacion.configure(text=f"✗ Error: {exc}")
                return
        self._estado_cotizacion.configure(
            text=f"✓ Cotización guardada. Candidato promovido a permanente "
                 f"(id={r['candidato_id_permanente']}). Podés seguir revisando el resto del catálogo.")

    def _recalcular(self) -> None:
        for w in self._contenedor.winfo_children():
            w.destroy()
        indice = self._indice_actual()
        candidato_id = self._cand["candidato_id"]

        with reloj(self):
            try:
                canal = self._mc.canal_venta_lote()
                marca = self._mc.obtener_similares_marca(candidato_id, indice=indice)
                tuc = self._mc.obtener_similares_tuc(candidato_id, indice=indice)
            except Exception as exc:  # noqa: BLE001
                Etiqueta(self._contenedor, text=f"Error consultando comparables:\n{exc}",
                        style="Suave.TLabel").pack(padx=8, pady=8)
                return

        # Aviso de color (2026-09-09): la búsqueda ahora PREFIERE el mismo
        # color de familia que el candidato (ver `filtrar_color` en
        # servidor_pty). Cuando no había ninguno del mismo color, se muestran
        # los de otro color -- pero se dice, en vez de hacerlos pasar por
        # comparables válidos, que era el bug reportado.
        familia = next((it.get("color_familia_candidato") for it in (marca + tuc)
                        if it.get("color_familia_candidato")), None)
        degradado = any(it.get("color_degradado") for it in (marca + tuc))
        if familia:
            if degradado:
                Etiqueta(self._contenedor,
                        text=f"⚠ No hay comparables del mismo color ({familia}). Se muestran "
                             f"los de OTRO color, marcados como tal: sirven para el molde, "
                             f"no para decidir el color a comprar.",
                        style="Suave.TLabel", wraplength=900, justify="left").pack(
                    anchor="w", pady=(0, 6))
            else:
                Etiqueta(self._contenedor,
                        text=f"Comparables del mismo color del candidato ({familia}), "
                             f"además del mismo tipo y género.",
                        style="Suave.TLabel", wraplength=900, justify="left").pack(
                    anchor="w", pady=(0, 6))

        # Qué lista MANDA lo decide el CANAL DE VENTA declarado para el lote en
        # el paso 1 (2026-09-10), no la marca declarada del candidato suelto:
        # es la misma decisión con la que el motor calculó el puntaje, así que
        # la ventana y el score cuentan la misma historia.
        #
        #   canal "marca" -> comparables Y puntaje contra TC Marcas.
        #   canal "tuc"   -> comparables Y puntaje contra TU Calzado; la lista
        #                    de marca reconocida se muestra igual, pero
        #                    marcada como INFORMATIVA ("a qué producto de marca
        #                    se parece"), sin efecto en el puntaje.
        marca_declarada = (self._cand.get("marca_declarada") or "").strip()
        if canal == "marca":
            Etiqueta(self._contenedor,
                    text="Compra declarada para TC MARCAS: los comparables y el puntaje "
                         "de este lote se calculan contra lo vendido/ofrecido en marca "
                         "reconocida. La lista de TU Calzado va abajo, solo de referencia.",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
            bloques = [("Marca reconocida — TC Marcas / competencia", marca, "marca"),
                       ("TU Calzado ya vendido (genérico) — solo referencia", tuc, "tuc")]
        else:
            # Aviso informativo pedido por el dueño: cuando la compra es para
            # el genérico, igual se dice a qué producto de MARCA RECONOCIDA se
            # parece el candidato. Es una consulta de lectura aparte: no entra
            # en `score_final` de ninguna forma.
            parecidos = ", ".join(
                f"{(it.get('marca') or '').strip()} {(it.get('codigo_tc') or it.get('referencia') or '').strip()}".strip()
                for it in marca[:3]) or "—"
            Etiqueta(self._contenedor,
                    text=f"Compra declarada para TU CALZADO (genérico): el puntaje se "
                         f"calcula contra lo vendido en TU Calzado.\n"
                         f"Se parece a: {parecidos} (marca reconocida) — solo informativo, "
                         f"no afecta el puntaje.",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
            bloques = [("TU Calzado ya vendido (genérico)", tuc, "tuc"),
                       ("Se parece a (marca reconocida) — solo informativo, "
                        "no afecta el puntaje", marca, "marca")]
        if marca_declarada:
            Etiqueta(self._contenedor,
                    text=f"Nota: este candidato viene declarado como marca reconocida "
                         f"({marca_declarada}).",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
        for i, (titulo, items, tipo) in enumerate(bloques):
            if i:
                Separador(self._contenedor).pack(fill="x", pady=10)
            Etiqueta(self._contenedor, text=titulo,
                    style="Subtitulo.TLabel").pack(anchor="w", pady=(0, 4))
            self._pintar_lista(items, tipo=tipo)

    def _pintar_lista(self, items: list[dict], tipo: str) -> None:
        if not items:
            Etiqueta(self._contenedor, text="Sin comparables por encima del umbral.",
                    style="Suave.TLabel").pack(anchor="w", padx=8, pady=(0, 6))
            return
        for it in items:
            fila = Tarjeta(self._contenedor)
            fila.pack(fill="x", pady=3)

            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6, width=64, height=64)
            hueco.pack(side="left", padx=8, pady=8)
            hueco.pack_propagate(False)
            imagen_id = it.get("imagen_id")
            ruta_foto = self._mc.ruta_foto_imagen(imagen_id) if imagen_id else None
            foto_tk = _cargar_foto_generico(ruta_foto, tam=(60, 60))
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                # Foto CLICKEABLE (2026-09-10, pedido del usuario): la
                # miniatura de 60px no alcanza para comparar moldes, que es
                # justo para lo que se abre esta ventana. Mismo patrón que el
                # visor del paso 5 (ver `_ver_foto_grande`).
                lbl_foto = tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0, cursor="hand2")
                lbl_foto.pack(fill="both", expand=True)
                lbl_foto.bind("<Button-1>", lambda _e, p=ruta_foto, t=(
                    it.get("codigo_tc") or it.get("codigo_tuc")
                    or f"{it.get('marca') or ''} {it.get('referencia') or ''}".strip()):
                    self._ver_foto_grande(p, t))
            else:
                Etiqueta(hueco, text="—", style="Suave.TLabel").pack(fill="both", expand=True)

            info = Marco(fila)
            info.pack(side="left", fill="both", expand=True, pady=8)
            if tipo == "marca":
                # DESTACADO el código interno de TU Calzado (2026-09-10): la
                # referencia del fabricante ("ADJS100156-WW0") no se puede
                # buscar en nuestros sistemas; el `codigo_tc` sí. Se muestran
                # los dos, pero el propio arriba y en grande. Cuando el
                # comparable es un producto de mercado que nunca vendimos, se
                # dice así en vez de dejar un renglón vacío.
                codigo_tc = it.get("codigo_tc")
                Etiqueta(info, text=(f"Código TU Calzado: {codigo_tc}" if codigo_tc
                                     else "No está en nuestro catálogo (solo mercado)"),
                        style="Subtitulo.TLabel").pack(anchor="w")
                Etiqueta(info, text=f"{it.get('marca')}  ref. fabricante {it.get('referencia')}",
                        style="Suave.TLabel").pack(anchor="w")
                # Precio de competencia SACADO (2026-09-10): confirmado que no
                # entra en el score (`puntuar_candidatos` nunca llama a
                # `api_similar_marca`, y `margen_factor` está cableado en 1.0
                # para candidatos en staging) -- era puramente informativo y
                # el usuario pidió sacarlo, quedándose con ventas propias.
                precio_vta = it.get("precio_avg_vta")
                unidades = it.get("unidades_vendidas", 0)
                if precio_vta:
                    linea = (f"Vendido por TC Marcas: ₡{precio_vta:,.0f} promedio  ·  "
                             f"{unidades} unidades")
                else:
                    linea = "Sin ventas propias registradas"
                Etiqueta(info, text=f"{linea}  ·  {it.get('tipo') or '—'}",
                        style="Suave.TLabel").pack(anchor="w")
            else:
                Etiqueta(info, text=f"Código TU Calzado: {it.get('codigo_tuc')}",
                        style="Subtitulo.TLabel").pack(anchor="w")
                precio = it.get("precio_avg_vta")
                texto_precio = (f"Vendido a ₡{precio:,.0f} promedio" if precio
                                else "sin ventas registradas")
                Etiqueta(info,
                        text=f"{texto_precio}  ·  {it.get('unidades_vendidas', 0)} unidades  ·  {it.get('categoria') or '—'}",
                        style="Suave.TLabel").pack(anchor="w")

            # Color del comparable, a la vista: es el dato que faltaba para
            # poder confiar (o desconfiar) de la lista. "otro color" se dice
            # explícitamente en vez de dejarlo adivinar por la foto.
            fila_col = Marco(info)
            fila_col.pack(anchor="w", fill="x", pady=(2, 0))
            _pastilla_color(fila_col, it.get("color_familia")).pack(side="left")
            if it.get("mismo_color") is False:
                tk.Label(fila_col, text="⚠ otro color", fg=solido(PAR_ALERTA),
                        bg=solido(PAR_PANEL), font=("Segoe UI Semibold", 10)
                        ).pack(side="left", padx=(6, 0))

            Etiqueta(fila, text=f"similitud {it['similitud']:.2f}", style="Suave.TLabel"
                    ).pack(side="right", padx=10)


if __name__ == "__main__":
    # Ícono de la BARRA DE TAREAS de Windows (distinto del de la titlebar, que
    # ya lo pone `iconbitmap`): Windows agrupa la barra de tareas por
    # "AppUserModelID", y como esto corre bajo python.exe hereda el ID —y el
    # ícono— de Python salvo que el proceso declare el suyo ANTES de crear
    # cualquier ventana. De ahí que esto vaya acá y no dentro de la clase.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "TUCalzado.AsistenteCompras.1")
    except Exception:  # noqa: BLE001
        pass  # otro sistema operativo, o Windows lo rechaza: no es crítico

    HerramientaUnica().mainloop()
