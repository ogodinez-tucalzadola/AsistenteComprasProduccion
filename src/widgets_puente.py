"""
Clases puente ttk -> customtkinter -- Fase 0 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Estas 10 clases existen para que los cuerpos de la lógica original (escritos
contra la API de `ttk`) siguieran funcionando letra por letra sobre
customtkinter. Son infraestructura visual reusable, no lógica de negocio: su
único vínculo con el resto del archivo era la paleta, que ahora vive en
`tema.py`. Sacarlas deja de mezclarlas con las 10,000 líneas de flujo de
compras y permite que otras pantallas las importen sin arrastrar la app.
"""

import tkinter as tk

import customtkinter as ctk

from tema import (  # noqa: E402
    PAR_FONDO, PAR_PANEL, PAR_PANEL_SUAVE, PAR_BORDE,
    PAR_TEXTO, PAR_TEXTO_SUAVE, PAR_PRIMARIO, PAR_PRIMARIO_OSCURO,
    PAR_PRIMARIO_SUAVE, PAR_ACENTO, PAR_OK, PAR_OK_HOVER,
    PAR_OK_BG, PAR_ALERTA, PAR_ALERTA_BG, PAR_MAL,
    PAR_MAL_HOVER, PAR_MAL_BG, PAR_NEUTRO, PAR_VISOR_BG,
    PAR_SELECCION, solido, COLOR_FONDO, COLOR_PANEL,
    COLOR_BORDE, COLOR_TEXTO, COLOR_TEXTO_SUAVE, COLOR_PRIMARIO,
    COLOR_PRIMARIO_OSCURO, COLOR_ACENTO, COLOR_OK, COLOR_OK_BG,
    COLOR_ALERTA, COLOR_ALERTA_BG, COLOR_MAL, COLOR_MAL_BG,
    COLOR_NEUTRO, COLOR_VISOR_BG, COLOR_BORDE_FUERTE, RADIO_CONTROL,
    RADIO_PANEL, FUENTE_BASE, FUENTE_TITULO, FUENTE_SUBTITULO,
    FUENTE_CHICA, F, armar_fuentes, ESTILOS_TEXTO,
    _pad_de_padding,
)


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
                # Barra.set con un valor no numerico: la barra se deja como esta.
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
