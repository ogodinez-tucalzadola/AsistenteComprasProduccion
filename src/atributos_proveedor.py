"""
atributos_proveedor.py
=======================
Normaliza el texto libre que un catálogo de proveedor declara sobre tipo,
color y temporada de cada producto ("Botines de dama", "NAVAL ACADEMY-WARM
GREY", "Otoño/Invierno") a valores canónicos, para poder filtrar/comparar
sin depender de cómo cada proveedor redacta.

Plan 2026-09-04, paso 6. Las reglas (mapas de palabras clave) están
ADAPTADAS de `AsistenteComprasTEC/run_atributos.py` (`_norm_tipo`,
`_norm_color`, `_norm_temporada`) -- ese script normaliza los atributos de
NUESTROS propios productos, entregados por TI en un Excel del ERP; acá se
reutiliza el mismo vocabulario de calzado en español (que es genérico, no
específico de TU Calzado) para texto declarado por un PROVEEDOR, que llega
por una vía distinta (columnas de su propio Excel de catálogo/oferta, ver
`extraer_excel.py`). Es una copia deliberada, no una dependencia entre
proyectos -- el vocabulario de cada fuente puede divergir con el tiempo
(ej. un proveedor que usa términos en inglés que TC nunca usó) y no tiene
sentido acoplar dos proyectos distintos por esto.

Cobertura conocida, no exhaustiva: como cualquier normalizador por palabras
clave, un término que ningún proveedor haya usado todavía cae al valor por
defecto ("casual" para tipo, "Otro" para color, "todo_anio" para temporada)
en vez de fallar. Si un tipo aparece seguido cayendo a "casual", es señal de
que falta una regla, no de que el producto sea casual de verdad."""
from __future__ import annotations

import re
import unicodedata


def _nk(s: str | None) -> str:
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return n.lower().strip()


# ── Temporada ────────────────────────────────────────────────────────────────

_TEMP_MAP = {
    "todo el ano": "todo_anio", "todo el año": "todo_anio", "todo": "todo_anio",
    "verano": "verano",
    "primavera; verano": "primavera_verano", "primavera;verano": "primavera_verano",
    "primavera verano": "primavera_verano", "primavera/verano": "primavera_verano",
    "primavera": "primavera_verano",
    "primavera; otono": "primavera_otonio", "primavera; otoño": "primavera_otonio",
    "primavera;otono": "primavera_otonio",
    "otono; invierno": "otonio_invierno", "otoño; invierno": "otonio_invierno",
    "otono;invierno": "otonio_invierno", "otoño;invierno": "otonio_invierno",
    "otono/invierno": "otonio_invierno", "otoño/invierno": "otonio_invierno",
    "invierno": "otonio_invierno", "otono": "otonio_invierno", "otoño": "otonio_invierno",
    "fall": "otonio_invierno", "winter": "otonio_invierno",
    "summer": "verano", "spring": "primavera_verano",
    "lluvias": "todo_anio", "lluvia": "todo_anio",
}
_ES_ESTACIONAL = {"verano", "primavera_verano", "primavera_otonio", "otonio_invierno"}


def norm_temporada(v: str | None) -> str:
    k = _nk(v)
    if not k:
        return "todo_anio"
    if k in _TEMP_MAP:
        return _TEMP_MAP[k]
    for pat, res in _TEMP_MAP.items():
        if pat in k:
            return res
    return "todo_anio"


def es_estacional(temporada_norm: str) -> bool:
    return temporada_norm in _ES_ESTACIONAL


# ── Tipo de calzado ──────────────────────────────────────────────────────────

_TIPO_RULES = [
    ("futbol", ["futbol", "football", "soccer", "futsal"]),
    ("running", ["running", "carrera"]),
    ("outdoor", ["senderismo", "montana", "trekking", "hiking", "trail", "outdoor"]),
    ("skate", ["skate", "skateboard"]),
    ("trabajo", ["seguridad", "industrial", "trabajo", "tactico"]),
    ("slides", ["chancla", "flip", "plastico", "piscina", "playa", "chanclet",
               "pantufla", "clog"]),
    ("tacones", ["tacon", "plataforma", "cuna", "stiletto", "pump",
                "bailarina", "kitten", "wedge", "mule", "slingback"]),
    ("formal", ["formal", "vestir", "oxford", "derby", "brogue",
               "mocasin", "loafer", "elegante", "charol", "mary jane", "escolar"]),
    ("sandalias", ["sandalia", "huarache", "zueco"]),
    ("botas_botines", ["botin", "ankle"]),
    ("botas_botines", ["bota vaquera", "bota de moda", "bota casual",
                       "bota alta", "bota corta", "bota lluvia"]),
    ("deportivo", ["deport", "athletic", "gym", "fitnes", "training",
                  "basket", "voleibol", "tennis", "sneaker"]),
    ("casual", ["casual", "moda", "lifestyle", "espadrille", "lona",
               "canvas", "slip", "mocas", "loafer", "alpargata", "nautico",
               "ballerina", "agua"]),
]
_FALLBACK_CASUAL = "casual"


def norm_tipo(v: str | None) -> str:
    k = _nk(v)
    if not k:
        return _FALLBACK_CASUAL
    for tipo, keywords in _TIPO_RULES:
        if any(kw in k for kw in keywords):
            return tipo
    if "bota" in k:
        return "botas_botines"
    if "zapatilla" in k:
        return "deportivo"
    return _FALLBACK_CASUAL


# ── Color principal → familia ────────────────────────────────────────────────

_COLOR_MAP = {
    "negro": "Negro", "negra": "Negro", "black": "Negro", "blk": "Negro", "noir": "Negro",
    "blanco": "Blanco", "blanca": "Blanco", "white": "Blanco", "crema": "Blanco",
    "vanilla": "Blanco", "snow": "Blanco", "ivory": "Blanco", "cream": "Blanco",
    "chalk": "Blanco", "sail": "Blanco", "alabaster": "Blanco", "wht": "Blanco",
    "gris": "Gris", "grey": "Gris", "gray": "Gris", "plata": "Gris", "silver": "Gris",
    "plateado": "Gris", "slate": "Gris", "graphite": "Gris", "charcoal": "Gris",
    "anthracite": "Gris", "steel": "Gris", "asphalt": "Gris", "cement": "Gris",
    "azul": "Azul/Celeste", "celeste": "Azul/Celeste", "blue": "Azul/Celeste",
    "navy": "Azul/Celeste", "marino": "Azul/Celeste", "turquesa": "Azul/Celeste",
    "naval": "Azul/Celeste", "naval academy": "Azul/Celeste", "teal": "Azul/Celeste",
    "aqua": "Azul/Celeste", "denim": "Azul/Celeste", "cobalt": "Azul/Celeste",
    "indigo": "Azul/Celeste", "royal": "Azul/Celeste",
    "rojo": "Rojo/Vino", "red": "Rojo/Vino", "vino": "Rojo/Vino", "bordo": "Rojo/Vino",
    "burgundy": "Rojo/Vino", "crimson": "Rojo/Vino", "maroon": "Rojo/Vino",
    "wine": "Rojo/Vino", "rosso": "Rojo/Vino",
    "rosa": "Rosa/Lila", "rosado": "Rosa/Lila", "pink": "Rosa/Lila", "lila": "Rosa/Lila",
    "morado": "Rosa/Lila", "violeta": "Rosa/Lila", "lavanda": "Rosa/Lila",
    "fucsia": "Rosa/Lila", "lavan": "Rosa/Lila", "magenta": "Rosa/Lila",
    "flamingo": "Rosa/Lila", "purple": "Rosa/Lila", "lavender": "Rosa/Lila",
    "hydrangea": "Rosa/Lila",
    "naranja": "Naranja/Amarillo", "orange": "Naranja/Amarillo",
    "amarillo": "Naranja/Amarillo", "yellow": "Naranja/Amarillo",
    "verde": "Verde", "green": "Verde", "oliva": "Verde", "militar": "Verde",
    "olive": "Verde", "loden": "Verde", "mint": "Verde", "forest": "Verde",
    "lime": "Verde", "sage": "Verde", "emerald": "Verde", "seafoam": "Verde",
    "marron": "Café/Tierra", "cafe": "Café/Tierra", "camel": "Café/Tierra",
    "beige": "Café/Tierra", "nude": "Café/Tierra", "arena": "Café/Tierra",
    "tan": "Café/Tierra", "brown": "Café/Tierra", "taupe": "Café/Tierra",
    "khaki": "Café/Tierra", "sand": "Café/Tierra", "wheat": "Café/Tierra",
    "tobacco": "Café/Tierra", "chocolate": "Café/Tierra", "mocha": "Café/Tierra",
    "clay": "Café/Tierra", "sandstone": "Café/Tierra", "toffee": "Café/Tierra",
    "putty": "Café/Tierra", "earth": "Café/Tierra",
    "dorado": "Metálico", "gold": "Metálico",
    "multicolor": "Multicolor", "multi": "Multicolor",
}
_SEP_COLOR = re.compile(r"[;,/\-]")


def norm_color(v: str | None) -> str:
    if not v:
        return "Otro"
    first = _SEP_COLOR.split(str(v))[0].strip()
    k = _nk(first)
    if k in _COLOR_MAP:
        return _COLOR_MAP[k]
    for pat, fam in _COLOR_MAP.items():
        if pat in k:
            return fam
    return "Otro"
