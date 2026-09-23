"""
Color del producto: nombre -> hex legible -- Fase 1 de la división de
`gui_profesional_ctk.py` (2026-09-23).

El nombre del color llega del proveedor como texto libre ("NEGRO/BLANCO",
"AZUL MARINO") y hay que convertirlo a una pastilla pintada. Esa traducción es
una tabla de datos más su normalización, no interfaz -- se corta acá para que
se pueda revisar y corregir sin abrir la app.

`_pastilla_color` se queda en este archivo aunque sí construya widgets: es el
consumidor inmediato y único de todo lo anterior, y separarlo habría partido
en dos una unidad que siempre se lee junta.
"""

import re
import tkinter as tk

from tema import PAR_BORDE, solido
from widgets_puente import Etiqueta, Marco


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
        # color de fondo con formato inesperado: se asume texto negro.
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
