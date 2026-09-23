"""
Tests de `_puntuar_variante_tuc` / `_indice_tuc_para_score` -- plan de
mejora 2026-09-23, Etapa C (M6). Antes de la Etapa C (M7), esta matemática
vivía mezclada con SQL adentro de `_puntuar_candidatos_impl` (483 líneas) y
no se podía probar sin una base real montada. Ahora que `_puntuar_variante_
tuc` no toca ninguna base, se prueba acá con arreglos numpy inventados a
mano -- exactamente el objetivo que motivó separarla.

No repite la garantía de "no cambió ningún resultado real" (eso ya se
verificó aparte, byte a byte contra 2 lotes reales y 4,000 casos
sintéticos, ver el commit de la Etapa C). Estos tests documentan y
protegen el COMPORTAMIENTO esperado hacia adelante -- si alguien cambia
esta función mañana y rompe alguna de estas reglas, se entera acá, sin
necesitar Postgres.

Correr con:
    Zawa\\.venv\\Scripts\\python.exe -m pytest AsistenteComprasProduccion/tests -v
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import motor_calificacion as mc  # noqa: E402


def _indice_sintetico(n=10, dim=8, seed=0):
    """Un índice TUC inventado a mano: `n` productos con vectores L2-
    normalizados aleatorios, categoría/género fijos, sin material/color (los
    tests que necesiten esos campos poblados los arman aparte)."""
    rng = np.random.default_rng(seed)
    vectores = rng.normal(size=(n, dim)).astype(np.float32)
    vectores /= np.linalg.norm(vectores, axis=1, keepdims=True)
    return {
        "n": n,
        "indice": vectores,
        "indice_dino": None,
        "velocidades": np.zeros(n),
        "tendencias": np.ones(n),
        "rotaciones": np.full(n, mc.REFERENCIA_ROTACION),
        "factores_venta": np.full(n, mc.REFERENCIA_FACTOR_VENTA),
        "pcts_sobre_lista": np.full(n, mc.REFERENCIA_PCT_SOBRE_LISTA),
        "categorias": np.array(["Deportivos"] * n, dtype=object),
        "generos": np.array(["caballero"] * n, dtype=object),
        "codigos": np.array([f"COD{i}" for i in range(n)], dtype=object),
        "precios": np.full(n, np.nan),
        "colores": np.array([None] * n, dtype=object),
        "materiales": np.array([None] * n, dtype=object),
    }


def _llamar(idx, v, **overrides):
    kwargs = dict(
        idx=idx, v=v, v_dino=None, cat_c="Deportivos", gen_c="caballero",
        categoria_conflicto=False, color_candidato_familia=None,
        mapa_prevalencia={}, modelo_activo="ti", k=7,
        precio_candidato_fn=lambda: None, factor_mercado_fn=lambda: 1.0,
    )
    kwargs.update(overrides)
    return mc._puntuar_variante_tuc(**kwargs)


# ---------- caso base: candidato idéntico a un vecino del índice ----------

def test_candidato_identico_a_un_vecino_da_visual_y_score_alto():
    idx = _indice_sintetico(n=10)
    v = idx["indice"][3].copy()  # coseno 1.0 contra ese vecino
    res = _llamar(idx, v)
    assert res["metodo_score"] == "visual"
    assert res["clasificacion"] != "sin_comparables"
    assert res["score_final"] > 0
    assert res["n_vecinos"] >= 1
    assert res["filtro_aplicado"] == "categoria+genero"


# ---------- sin_comparables: nada pasa el umbral de similitud ----------

def test_sin_ningun_vecino_similar_da_sin_comparables():
    idx = _indice_sintetico(n=10, dim=64, seed=1)
    # vector ortogonal-ish a todo el índice (dim alta, aleatorio, sin
    # relación con las filas reales) -- coseno bajo con todos.
    v = np.zeros(64, dtype=np.float32)
    v[0] = 1.0
    res = _llamar(idx, v)
    assert res["clasificacion"] == "sin_comparables"
    assert res["score_final"] is None
    assert res["metodo_score"] is None
    assert res["n_vecinos"] == 0
    # filtro sí se calcula incluso sin comparables (categoria+genero, porque
    # el índice entero es de esa categoría/género)
    assert res["filtro_aplicado"] == "categoria+genero"


# ---------- filtro degradado a "ninguno" cuando la máscara queda vacía ----

def test_filtro_se_degrada_a_ninguno_si_no_hay_vecinos_de_esa_categoria():
    idx = _indice_sintetico(n=10)  # todo "Deportivos"/"caballero"
    v = idx["indice"][0].copy()
    res = _llamar(idx, v, cat_c="Sandalias", gen_c="dama")
    assert res["filtro_aplicado"] == "ninguno"
    # al degradarse sin filtro, el candidato sí puede tener comparables
    # (coseno 1.0 contra el vecino 0)
    assert res["clasificacion"] != "sin_comparables"


# ---------- respaldo por precio cuando no hay comparable visual ----------

def test_respaldo_por_precio_cuando_no_hay_comparable_visual():
    idx = _indice_sintetico(n=10, dim=64, seed=2)
    idx["precios"] = np.array([1000.0 * (i + 1) for i in range(10)])
    v = np.zeros(64, dtype=np.float32)
    v[0] = 1.0  # sin comparable visual (mismo criterio que el test de arriba)
    res = _llamar(idx, v, precio_candidato_fn=lambda: 1000.0)
    assert res["metodo_score"] == "precio"
    assert res["clasificacion"] != "sin_comparables"


def test_sin_precio_candidato_y_sin_comparable_visual_da_sin_comparables():
    idx = _indice_sintetico(n=10, dim=64, seed=2)
    idx["precios"] = np.array([1000.0 * (i + 1) for i in range(10)])
    v = np.zeros(64, dtype=np.float32)
    v[0] = 1.0
    res = _llamar(idx, v, precio_candidato_fn=lambda: None)
    assert res["clasificacion"] == "sin_comparables"


# ---------- categoria_conflicto topa f_tipo a 0.92 ----------

def test_categoria_conflicto_topa_f_tipo():
    idx = _indice_sintetico(n=10)
    v = idx["indice"][0].copy()
    sin_conflicto = _llamar(idx, v, categoria_conflicto=False)
    con_conflicto = _llamar(idx, v, categoria_conflicto=True)
    assert sin_conflicto["f_tipo"] == 1.00  # categoria+genero, sin conflicto
    assert con_conflicto["f_tipo"] == pytest.approx(0.92)


# ---------- todos los factores acotados quedan dentro de su rango ----------

def test_factores_acotados_quedan_dentro_de_sus_limites_documentados():
    idx = _indice_sintetico(n=10)
    v = idx["indice"][0].copy()
    res = _llamar(idx, v)
    assert 0.70 <= res["f_soporte"] <= 1.00
    assert 0.85 <= res["f_tipo"] <= 1.00
    assert 0.90 <= res["f_color"] <= 1.10
    assert 0.95 <= res["f_atrib"] <= 1.05
    assert 0.85 <= res["f_demanda"] <= 1.20
    assert 0.85 <= res["f_rotacion"] <= 1.15
    assert 0.85 <= res["f_venta"] <= 1.20
    assert 0.85 <= res["f_descuento"] <= 1.15


# ---------- vecinos_detalle: mismo candidato -> mismo orden determinista --

def test_vecinos_detalle_ordenado_por_peso_descendente():
    idx = _indice_sintetico(n=10, dim=16, seed=3)
    v = idx["indice"][0].copy()
    res = _llamar(idx, v)
    pesos = [d["peso"] for d in res["vecinos_detalle"]]
    assert pesos == sorted(pesos, reverse=True)


def test_mismo_candidato_da_siempre_el_mismo_score_determinista():
    idx = _indice_sintetico(n=10, dim=16, seed=4)
    v = idx["indice"][2].copy()
    r1 = _llamar(idx, v)
    r2 = _llamar(idx, v)
    assert r1["score_final"] == r2["score_final"]
    assert r1["vecinos_detalle"] == r2["vecinos_detalle"]


# ---------- _indice_tuc_para_score: índice vacío ----------

def test_indice_tuc_vacio_devuelve_none():
    class _CursorVacio:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

    # simula _consumir_por_lotes devolviendo cero filas: se llama a
    # _indice_tuc_para_score con un cursor real de mentira via monkeypatch
    # del generador, para no requerir Postgres.
    def _sin_filas(cur, query, params=None, tam_lote=2000):
        return iter(())

    original = mc._consumir_por_lotes
    mc._consumir_por_lotes = _sin_filas
    try:
        assert mc._indice_tuc_para_score(_CursorVacio(), "ti") is None
        assert mc._indice_tuc_para_score(_CursorVacio(), "estandar") is None
    finally:
        mc._consumir_por_lotes = original
