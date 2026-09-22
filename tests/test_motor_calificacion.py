"""
Primeras pruebas automatizadas de motor_calificacion.py -- plan de mejora
2026-09-22, Etapa 4 (red de seguridad). Antes de este archivo: 0 pruebas
para las 5,585 líneas del motor de scoring (verificado, no estimado).

Cubre solo funciones PURAS (sin Postgres, sin lote.sqlite, sin modelos de
visión) -- el objetivo es un colchón rápido (corre en segundos) que confirme
que la mecánica del score no se rompe, no que el score sea "bueno" (eso
sigue siendo criterio objetivo del negocio, no algo que un test decida).

Correr con:
    Zawa\\.venv\\Scripts\\python.exe -m pytest AsistenteComprasProduccion/tests -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import motor_calificacion as mc  # noqa: E402


# ---------- clasificar_score / CORTES_CLASIFICACION ----------

def test_clasificar_score_bordes_exactos():
    """Los cortes son >=, no > -- el valor exacto del corte ya pertenece al
    grado de arriba (motor_calificacion.py:3076-3079)."""
    assert mc.clasificar_score(78) == "S"
    assert mc.clasificar_score(77.999) == "A"
    assert mc.clasificar_score(58) == "A"
    assert mc.clasificar_score(57.999) == "B"
    assert mc.clasificar_score(38) == "B"
    assert mc.clasificar_score(37.999) == "C"
    assert mc.clasificar_score(20) == "C"
    assert mc.clasificar_score(19.999) == "D"


def test_clasificar_score_none_y_extremos():
    assert mc.clasificar_score(None) is None
    assert mc.clasificar_score(0) == "D"
    assert mc.clasificar_score(1000) == "S"  # sin tope superior


# ---------- promedio_simple_variantes (punto 4 del pedido del dueño) ----------

def test_promedio_simple_variantes_promedia_igual_por_color():
    """Decisión explícita del dueño (2026-09-17): cada color pesa igual, el
    bulto se compra entero -- no ponderar por confianza/evidencia."""
    assert mc.promedio_simple_variantes([80.0, 60.0]) == 70.0
    assert mc.promedio_simple_variantes([100.0, 0.0, 50.0]) == 50.0


def test_promedio_simple_variantes_none_no_cuenta_como_cero():
    """Un color sin score (sin comparables o sin vectorizar) NO entra al
    promedio -- no vale 0, simplemente no se sabe. Este es el comportamiento
    que más fácil se rompe si alguien "simplifica" el filtro a futuro."""
    assert mc.promedio_simple_variantes([80.0, None]) == 80.0
    assert mc.promedio_simple_variantes([None, None]) is None
    assert mc.promedio_simple_variantes([]) is None


# ---------- umbral_comparable / ancla_similitud (nunca sin dominio) ----------

def test_umbral_comparable_dominio_requerido():
    """Sin default a propósito -- el bug que esto cierra es todo el canal
    marca corriendo con el umbral calibrado para tuc (ver docstring real)."""
    assert mc.umbral_comparable("estandar", "tuc") == mc.UMBRAL_POR_MODELO["estandar"]
    assert mc.umbral_comparable("estandar", "marca") == mc.UMBRAL_POR_MODELO_MARCA["estandar"]
    assert mc.umbral_comparable("estandar", "tuc") != mc.umbral_comparable("estandar", "marca")


def test_umbral_comparable_dominio_desconocido_revienta():
    with pytest.raises(ValueError):
        mc.umbral_comparable("estandar", "otro")


def test_ancla_similitud_dominio_requerido():
    assert mc.ancla_similitud("ti", "tuc") == mc.ANCLA_SIMILITUD["ti"]
    assert mc.ancla_similitud("ti", "marca") == mc.ANCLA_SIMILITUD_MARCA["ti"]


def test_umbral_y_ancla_todos_los_modelos_conocidos():
    """Si alguien agrega un modelo nuevo a un diccionario y se olvida del
    otro, este test lo delata antes que un KeyError en producción."""
    for modelo in mc.UMBRAL_POR_MODELO:
        assert modelo in mc.ANCLA_SIMILITUD, f"falta ANCLA_SIMILITUD para {modelo!r}"
    for modelo in mc.UMBRAL_POR_MODELO_MARCA:
        assert modelo in mc.ANCLA_SIMILITUD_MARCA, f"falta ANCLA_SIMILITUD_MARCA para {modelo!r}"


# ---------- _precio_venta_estimado ----------

def test_precio_venta_estimado_crc():
    costo = 1000.0
    esperado = round(costo * mc.IVA_CR * mc.MARGEN_VENTA_DEFAULT, 6)
    assert round(mc._precio_venta_estimado(costo, "CRC"), 6) == esperado


def test_precio_venta_estimado_usd_convierte_antes():
    costo_usd = 10.0
    esperado = round(costo_usd * mc.TIPO_CAMBIO_USD_CRC * mc.IVA_CR * mc.MARGEN_VENTA_DEFAULT, 6)
    assert round(mc._precio_venta_estimado(costo_usd, "USD"), 6) == esperado


def test_precio_venta_estimado_sin_costo_o_sin_moneda():
    """Sin costo o sin moneda inferida -> None, nunca una suposición
    silenciosa (ver docstring real: asumir CRC por defecto daría un precio
    sin sentido para un proveedor que cotiza en USD)."""
    assert mc._precio_venta_estimado(None, "CRC") is None
    assert mc._precio_venta_estimado(0, "CRC") is None
    assert mc._precio_venta_estimado(-5, "CRC") is None
    assert mc._precio_venta_estimado(1000, None) is None


# ---------- _norm_txt ----------

def test_norm_txt_minusculas_sin_acentos_sin_espacios_dobles():
    assert mc._norm_txt("Cuñas  y   Plataformas") == "cunas y plataformas"
    assert mc._norm_txt("MUJER") == "mujer"
    assert mc._norm_txt(None) == ""
    assert mc._norm_txt("  ") == ""


# ---------- _iou / _nms (detección de cajas) ----------

def _caja(xmin, ymin, xmax, ymax, score=1.0):
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "score": score}


def test_iou_cajas_identicas_da_uno():
    # _iou usa un epsilon (1e-9) en el denominador para evitar división por
    # cero -- cajas idénticas dan 0.99999999999, no exactamente 1.0.
    a = _caja(0, 0, 10, 10)
    assert mc._iou(a, a) == pytest.approx(1.0)


def test_iou_cajas_sin_overlap_da_cero():
    a = _caja(0, 0, 10, 10)
    b = _caja(20, 20, 30, 30)
    assert mc._iou(a, b) == 0.0


def test_nms_descarta_cajas_solapadas_quedandose_con_el_mejor_score():
    alta = _caja(0, 0, 10, 10, score=0.9)
    solapada_peor = _caja(1, 1, 11, 11, score=0.5)  # mismo objeto, score menor
    lejana = _caja(50, 50, 60, 60, score=0.7)  # objeto distinto, no se toca
    resultado = mc._nms([solapada_peor, alta, lejana], iou_thresh=0.35)
    assert alta in resultado
    assert solapada_peor not in resultado
    assert lejana in resultado
    assert len(resultado) == 2
