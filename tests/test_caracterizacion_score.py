"""
Pruebas de caracterización del score real -- plan de mejora 2026-09-22,
Etapa 4. A diferencia de test_motor_calificacion.py (funciones puras, sin
dependencias), ESTE archivo SÍ necesita Postgres y un lote real en disco --
se salta automáticamente (no falla) si cualquiera de los dos no está
disponible, para no romper CI/máquinas sin la base montada.

Objetivo: dejar un punto de referencia verificable de que "un cambio no
alteró los puntajes" (o si los alteró, que fue a propósito) -- exactamente
lo que el plan de mejora pide antes de tocar rendimiento (Etapa 5).

Correr con:
    Zawa\\.venv\\Scripts\\python.exe -m pytest AsistenteComprasProduccion/tests -v
"""
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

LOTE_PRUEBA = Path(r"C:\Users\Tucalzado\Proyectos\AsistenteComprasLotes\Prueba2")


def _conexion_postgres_disponible():
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="tcmarcas",
                                 user="tcm_etl", password="tcmarcas2025!", connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


requiere_entorno_real = pytest.mark.skipif(
    not (LOTE_PRUEBA.exists() and _conexion_postgres_disponible()),
    reason="requiere Postgres local + el lote de prueba Prueba2 en disco -- no disponible en este entorno",
)


@requiere_entorno_real
def test_recalculo_normal_no_pierde_ningun_score():
    """Caracterización básica: recalcular un lote completo (18 candidatos,
    canal marca) debe dejar TODOS los candidatos con score_final numérico.
    Si esto empieza a fallar tras un cambio de rendimiento (Etapa 5), el
    cambio rompió algo, no es "más rápido nomás"."""
    import psycopg2
    import psycopg2.extras
    import motor_calificacion as mc

    mc.fijar_lote(LOTE_PRUEBA)
    try:
        pg = psycopg2.connect(host="127.0.0.1", port=5432, dbname="tcmarcas",
                               user="tcm_etl", password="tcmarcas2025!",
                               options="-c search_path=staging,bronze,silver,gold,public")
        cur = pg.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cl = mc._cl()
            ids = [r[0] for r in cl.execute("SELECT candidato_id FROM candidato").fetchall()]
            assert ids, "el lote de prueba no tiene candidatos -- ¿se movió o se vació?"

            mc.puntuar_candidatos(cur, ids, modelo_activo="estandar")

            cl2 = mc._cl()
            filas = cl2.execute(
                "SELECT candidato_id, score_final, clasificacion FROM candidato_score"
            ).fetchall()
            por_id = {r["candidato_id"]: r for r in filas}
            for candidato_id in ids:
                fila = por_id.get(candidato_id)
                assert fila is not None, f"candidato {candidato_id} no tiene fila en candidato_score"
                assert fila["score_final"] is not None, f"candidato {candidato_id} quedó sin score"
                assert fila["clasificacion"] in ("S", "A", "B", "C", "D"), (
                    f"candidato {candidato_id} con clasificación inesperada: {fila['clasificacion']!r}")
        finally:
            pg.close()
    finally:
        mc.cerrar_lote()


@requiere_entorno_real
def test_fallo_a_mitad_del_recalculo_no_borra_los_scores_previos():
    """Regresión directa del hallazgo de la Etapa 2: `puntuar_candidatos()`
    borraba todos los scores de un lote ANTES de recalcularlos, sin
    transacción -- una caída a mitad dejaba el lote sin ningún score. Este
    test simula esa caída forzando una excepción real a mitad del bucle y
    confirma que los scores previos sobreviven intactos."""
    import psycopg2
    import psycopg2.extras
    import motor_calificacion as mc

    mc.fijar_lote(LOTE_PRUEBA)
    try:
        pg = psycopg2.connect(host="127.0.0.1", port=5432, dbname="tcmarcas",
                               user="tcm_etl", password="tcmarcas2025!",
                               options="-c search_path=staging,bronze,silver,gold,public")
        cur = pg.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cl = mc._cl()
            ids = [r[0] for r in cl.execute("SELECT candidato_id FROM candidato").fetchall()]
            assert ids

            # Recalcular una vez de verdad para tener un estado "antes" real.
            mc.puntuar_candidatos(cur, ids, modelo_activo="estandar")
            antes = {
                r["candidato_id"]: r["score_final"]
                for r in mc._cl().execute("SELECT candidato_id, score_final FROM candidato_score").fetchall()
            }
            assert all(v is not None for v in antes.values()), "precondición: todos con score antes de simular el fallo"

            # Forzar una excepción real a mitad del bucle de guardado.
            original = mc._guardar_score_candidato
            contador = {"n": 0}

            def falla_a_mitad(*args, **kwargs):
                contador["n"] += 1
                if contador["n"] == max(1, len(ids) // 2):
                    raise RuntimeError("fallo simulado -- se cae Postgres a mitad de camino")
                return original(*args, **kwargs)

            mc._guardar_score_candidato = falla_a_mitad
            try:
                with pytest.raises(RuntimeError):
                    mc.puntuar_candidatos(cur, ids, modelo_activo="estandar")
            finally:
                mc._guardar_score_candidato = original

            despues = {
                r["candidato_id"]: r["score_final"]
                for r in mc._cl().execute("SELECT candidato_id, score_final FROM candidato_score").fetchall()
            }
            assert despues == antes, (
                "el lote quedó distinto tras un fallo a mitad del recálculo -- "
                "la transacción de la Etapa 2 dejó de proteger el estado previo")
        finally:
            pg.close()
    finally:
        mc.cerrar_lote()
