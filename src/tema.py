"""
Paleta, tipografía y estilos de texto de la app -- Fase 0 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Se cortó por acá porque es el único bloque del archivo que no depende de NADA
suyo: son constantes y dos funciones puras. Mientras vivía adentro de las
11,438 líneas, cualquier archivo que quisiera el mismo color (por ejemplo
`etiquetado_calibracion.py`) tenía que importar la interfaz entera -- o sea,
levantar customtkinter, la ventana y toda la lógica de negocio -- para leer un
hex. `ctk.set_appearance_mode()` se mudó acá junto con `_MODO_OSCURO` porque
son inseparables: el modo hay que fijarlo ANTES de resolver los COLOR_*.

Los nombres se re-exportan desde `gui_profesional_ctk` para no romper a quien
ya los importaba de allá.
"""

import customtkinter as ctk


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
