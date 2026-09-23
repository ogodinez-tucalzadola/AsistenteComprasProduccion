"""
Precio de venta sugerido y su explicación en texto -- Fase 1 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Es la única regla de NEGOCIO que estaba enterrada en el archivo de la
interfaz: margen, IVA, redondeo de PVP y cómo se le explica al comprador de
dónde salió el precio. Vivir dentro de la GUI la hacía imposible de probar sin
levantar una ventana, y la dejaba invisible para cualquier otro consumidor (un
informe, un export). No importa nada de tkinter a propósito: ese es justamente
el corte.
"""


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
        # costo no numerico (campo vacio o texto): no hay precio que calcular.
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
