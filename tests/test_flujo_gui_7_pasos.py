"""Caracterización del flujo de 7 pasos de `HerramientaUnica` (la GUI).

Por qué existe
--------------
`HerramientaUnica` (src/gui_profesional_ctk.py) tiene 0% de cobertura. El
plan de división del archivo prevé partirla en mixins (Fase 4), y un mixin
mal cortado NO rompe al importar: rompe en vivo, en un paso que nadie probó
a mano (un método que quedó en la clase equivocada, un atributo que se
inicializa en otro mixin, una pantalla que se arma con la mitad de los
widgets). Este test es la red de seguridad que el dueño pidió
explícitamente: "escribir primero un test que recorra los 7 pasos".

Qué prueba (y qué NO)
---------------------
Es un test de caracterización EX-POST de la NAVEGACIÓN y de las PANTALLAS:
levanta una ventana Tk real, recarga un lote real que ya está terminado en
disco, recorre los 7 pasos llamando los MISMOS métodos que invocan los
botones, y congela un "snapshot" de estado observable por paso.

NO prueba el cálculo pesado (vectorización / `puntuar_candidatos`): eso ya
lo cubre `test_caracterizacion_score.py`. Por eso se elige a propósito un
lote que YA tiene los pasos 5 y 6 hechos en disco y se lee de ahí, en vez de
disparar torch/transformers en cada corrida.

Mismo patrón de `test_caracterizacion_score.py`: se SALTA solo (no falla) si
Postgres o el lote no están disponibles.

Regenerar la línea base (solo si el cambio fue a propósito):
    set REGENERAR_SNAPSHOT_FLUJO=1
    Zawa\\.venv\\Scripts\\python.exe -m pytest tests/test_flujo_gui_7_pasos.py
"""
import json
import os
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# Lote real, canal "marca", el mismo que congela `test_caracterizacion_score`.
# Está TERMINADO en disco (23 recortes todos decididos + 18 candidatos ya
# calificados en staging), así que al recargarlo la app aterriza sola en el
# paso 6 y los 7 pasos se pueden recorrer sin recalcular nada.
LOTE = Path(r"C:\Users\Tucalzado\Proyectos\AsistenteComprasLotes\Prueba2")

REFERENCIA = Path(__file__).resolve().parent / "fixtures" / "snapshot_flujo_7_pasos.json"


def _conexion_postgres_disponible():
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="tcmarcas",
                                user="tcm_etl", password="tcmarcas2025!", connect_timeout=3)
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _normalizar(valor):
    """Saca de los textos capturados todo lo que depende de ESTA máquina (la
    ruta absoluta del lote), para que el archivo de referencia compare la
    pantalla y no el disco de quien corre el test."""
    if isinstance(valor, str):
        return valor.replace(str(LOTE), "<LOTE>").replace(str(LOTE).replace("\\", "/"), "<LOTE>")
    return valor


def _n_hijos(widget):
    try:
        return len(widget.winfo_children())
    except Exception:  # noqa: BLE001
        return None


def _empaquetado(widget):
    """¿El widget está colocado en la pantalla? Se usa `winfo_manager()` (dice
    si tiene geometry manager asignado) y NO `winfo_ismapped()`: sin
    `mainloop()` corriendo nada llega a mapearse de verdad, así que `ismapped`
    daría 0 para todo y no distinguiría una pantalla de otra."""
    try:
        return bool(widget.winfo_manager())
    except Exception:  # noqa: BLE001
        return None


def _snapshot(app, etiqueta):
    """El estado observable de la pantalla actual.

    Qué se captura y por qué (cada campo apunta a una forma concreta en que
    un mixin mal cortado rompería la app sin que el import falle):

    * `paso_flujo` + los `v_paso_*`  -> la cabecera del asistente: si el
      método que la reescribe (`_actualizar_paso`) queda en otro mixin que el
      que navega, el número/título de paso deja de moverse.
    * `hijos_*` (cuántos widgets hijos tiene cada frame de pantalla) -> una
      pantalla que se arma a medias (falta una sección entera) cambia este
      número aunque no lance ninguna excepción.
    * `empaquetado_*` -> qué pantalla quedó visible: el bug clásico del corte
      es navegar bien "por dentro" y dejar dos frames superpuestos o ninguno.
    * `recortes` (nombre + decisión) y `n_recortes` -> el estado del lote que
      sobrevive a toda la navegación; si un mixin pierde `self._recortes`, el
      paso 3 y el 4 se quedan vacíos.
    * `canal_venta_lote`, `proveedor_id/nombre` -> la decisión más importante
      del lote (contra qué catálogo se califica). Se restaura al recargar; si
      se pierde en el corte, el paso 6 califica contra el catálogo equivocado.
    * `candidatos` (id + clasificación + proveedor) -> el contenido real de
      los pasos 6 y 7, no solo que la pantalla exista.
    """
    import copy

    cands = None
    subvista = None
    if app._candidatos_frame is not None:
        try:
            cands = sorted(
                [int(c["candidato_id"]),
                 str(c.get("grado")),
                 _normalizar(str(c.get("codigo_proveedor"))),
                 _normalizar(str(c.get("proveedor"))),
                 round(float(c["score_final"]), 4) if c.get("score_final") is not None else None]
                for c in app._candidatos_del_lote()
            )
        except Exception as exc:  # noqa: BLE001
            cands = f"ERROR: {type(exc).__name__}"
        # Los pasos 6 y 7 comparten el MISMO frame (`VentanaCandidatosCTk`) y
        # se distinguen solo por cuál de las dos sub-vistas está empaquetada.
        # Sin esto, el snapshot del 6 y el del 7 serían idénticos salvo el
        # número de paso -- y un corte que dejara el paso 7 mostrando la
        # pantalla del 6 pasaría desapercibido.
        subvista = {
            "candidatos_empaquetada": _empaquetado(getattr(app._candidatos_frame, "_vista_candidatos", None)),
            "optimizador_empaquetada": _empaquetado(getattr(app._candidatos_frame, "_vista_optimizador", None)),
            "hijos_vista_candidatos": _n_hijos(getattr(app._candidatos_frame, "_vista_candidatos", None)),
            "hijos_vista_optimizador": _n_hijos(getattr(app._candidatos_frame, "_vista_optimizador", None)),
        }

    def var(nombre):
        v = getattr(app, nombre, None)
        return _normalizar(v.get()) if v is not None else None

    return {
        "etiqueta": etiqueta,
        "paso_flujo": getattr(app, "_paso_flujo", None),
        "v_paso_num": var("v_paso_num"),
        "v_paso_titulo": var("v_paso_titulo"),
        "v_paso_guia": var("v_paso_guia"),
        "v_paso_actual": var("v_paso_actual"),
        "v_canal_venta": var("v_canal_venta"),
        "v_canal_estado": var("v_canal_estado"),
        "v_proveedor": var("v_proveedor"),
        "v_metodo_vector": var("v_metodo_vector"),
        "v_carpeta_activa": var("v_carpeta_activa"),
        "v_salida": var("v_salida"),
        "canal_venta_lote": app.canal_venta_lote(),
        "proveedor_id": app._proveedor_id_actual,
        "proveedor_nombre": app._proveedor_nombre_actual,
        "retomando_lote": bool(getattr(app, "_retomando_lote", False)),
        "n_recortes": len(app._recortes),
        "recortes": sorted(
            [n, (r.get("estado") or {}).get("decision")]
            for n, r in copy.deepcopy(app._recortes).items()
        ),
        "hay_candidatos_frame": app._candidatos_frame is not None,
        "hay_resumen_frame": getattr(app, "_resumen_frame", None) is not None,
        "hijos_pantalla_inicio": _n_hijos(app.pantalla_inicio),
        "hijos_cuerpo": _n_hijos(app.cuerpo),
        "hijos_candidatos": _n_hijos(app._candidatos_frame) if app._candidatos_frame is not None else None,
        "hijos_resumen": _n_hijos(app._resumen_frame) if getattr(app, "_resumen_frame", None) is not None else None,
        "empaquetado_pantalla_inicio": _empaquetado(app.pantalla_inicio),
        "empaquetado_cuerpo": _empaquetado(app.cuerpo),
        "empaquetado_monitor": _empaquetado(app.monitor),
        "candidatos": cands,
        "subvista_candidatos": subvista,
        "hay_revision_frame": getattr(app, "_revision_frame", None) is not None,
        "hijos_revision": _n_hijos(getattr(app, "_revision_frame", None)) if getattr(app, "_revision_frame", None) is not None else None,
        "n_archivos_revision": len(getattr(app, "_archivos_revision", []) or []),
    }


def _neutralizar_dialogos(G):
    """Deja la GUI manejable sin usuario -- sin tocar `src/`.

    Tres bloqueos reales encontrados al automatizar esto:

    1. `__init__` programa `after_idle(self._cargar_ultima_carpeta)`, que abre
       el modal "¿Qué querés hacer?". No corre solo (no hay `mainloop()`),
       PERO `_ir_a_paso` llama `update_idletasks()` -- que sí procesa los
       idle -- así que el modal aparecía a mitad de la navegación y colgaba
       el test para siempre. Se anula el método antes de instanciar.
    2. Los `messagebox.*` son modales: cualquier validación que avise algo
       trabaría la corrida. Se reemplazan por funciones que no hacen nada y
       registran lo que se habría mostrado.
    3. `_avisar_si_metricas_viejas` (paso 6) lanza un hilo a Postgres que
       puede terminar en `showwarning`. Queda cubierto por el punto 2 (el
       hilo es daemon y el aviso ya no abre ninguna ventana).

    Lo que NO hizo falta neutralizar, porque el lote elegido ya los tiene
    contestados en disco: `asegurar_monedas_de_proveedores` y
    `asegurar_mes_venta_lote` (moneda USD + tipo de cambio + mes ya guardados
    en `lote.sqlite`) -- si se apuntara este test a un lote sin esos datos,
    los dos abrirían un modal y colgarían la corrida.
    """
    avisos = []
    G.HerramientaUnica._cargar_ultima_carpeta = lambda self: None
    for nombre in ("showinfo", "showerror", "showwarning"):
        setattr(G.messagebox, nombre,
                lambda *a, _n=nombre, **k: avisos.append([_n, a[0] if a else None]))
    # Ningún askyesno del flujo debe dispararse con un lote de un solo
    # proveedor; si alguno lo hace, "No" es la respuesta que no desvía el
    # recorrido (no activa otro slot ni dispara un reenvío).
    G.messagebox.askyesno = lambda *a, **k: avisos.append(["askyesno", a[0] if a else None]) or False
    return avisos


def _recorrer_los_7_pasos():
    """Levanta la ventana real, recarga el lote y devuelve
    (snapshots_por_paso, avisos)."""
    import gui_profesional_ctk as G

    avisos = _neutralizar_dialogos(G)

    app = G.HerramientaUnica()
    app.withdraw()  # la ventana EXISTE de verdad (los widgets se construyen); solo no se muestra
    snaps = []
    try:
        # --- Carga del lote: es lo que hace "Retomar" en el diálogo inicial.
        # `_cargar_salida_existente` restaura proveedor, origen, nombre y
        # canal, y después `_saltar_al_paso_del_lote` aterriza sola en la
        # pantalla que le corresponde al avance del lote (acá: el paso 6).
        cargo = app._cargar_salida_existente(LOTE, avisar_siempre=True)
        assert cargo, f"el lote {LOTE.name} no trajo recortes -- ¿se movió o se vació?"
        snaps.append(_snapshot(app, "carga_inicial"))

        # --- Camino de VUELTA (botón «← Atrás»), desde donde aterrizó hasta
        # el paso 1. Se recorre primero hacia atrás para poder después subir
        # los 7 pasos de corrido desde el principio real del asistente.
        for _ in range(10):
            if getattr(app, "_paso_flujo", 1) <= 1:
                break
            app._paso_atras()
            snaps.append(_snapshot(app, f"atras_a_paso_{getattr(app, '_paso_flujo', None)}"))
        assert getattr(app, "_paso_flujo", None) == 1, (
            f"«Atrás» no llegó al paso 1, quedó en {getattr(app, '_paso_flujo', None)}")

        # --- Camino de IDA, los mismos métodos que invocan los botones.
        # Paso 1 -> 2: `_paso_siguiente` valida proveedor + origen + canal.
        app._paso_siguiente()
        snaps.append(_snapshot(app, f"siguiente_desde_1_quedo_en_{getattr(app, '_paso_flujo', None)}"))

        # Paso 3 -> 4 ("Confirmar y enviar"): mismo botón de pie.
        if getattr(app, "_paso_flujo", None) == 2:
            app._paso_siguiente()
            snaps.append(_snapshot(app, "siguiente_desde_2"))
        assert getattr(app, "_paso_flujo", None) == 3, (
            f"no se llegó al paso 3, se quedó en {getattr(app, '_paso_flujo', None)}")
        app._paso_siguiente()
        snaps.append(_snapshot(app, "paso_4_confirmar_y_enviar"))

        # Paso 5 ("Vectorizar y comparar"): se abre la PANTALLA con
        # `_ir_a_paso(5)` -- el mismo camino que usa «Atrás» desde el paso 6 --
        # y NO con el botón "▶ Calcular y continuar"
        # (`_calcular_vectorizacion_y_continuar`), que dispararía la
        # vectorización real con torch. Este test caracteriza la navegación y
        # las pantallas; el cálculo ya lo cubre test_caracterizacion_score.py.
        app._ir_a_paso(5)
        snaps.append(_snapshot(app, "paso_5_vectorizar"))

        # Paso 6: lee lo que YA está calificado en staging, sin recalcular.
        app._ver_candidatos_calificados()
        snaps.append(_snapshot(app, "paso_6_candidatos_calificados"))
        assert getattr(app, "_paso_flujo", None) == 6

        # Paso 7: el botón de pie del paso 6.
        app._paso_siguiente()
        snaps.append(_snapshot(app, "paso_7_sugerido_de_compra"))
        assert getattr(app, "_paso_flujo", None) == 7, (
            "no se llegó al paso 7 (sugerido de compra)")

        # Y el «Atrás» del paso 7 vuelve a la sub-vista de candidatos.
        app._paso_atras()
        snaps.append(_snapshot(app, "atras_desde_7_a_candidatos"))

        # --- PASO 2 ("Elegir qué limpiar"), aparte y al final.
        #
        # Por qué no aparece en el recorrido de arriba: el paso 2 SOLO existe
        # cuando el origen fue un Excel/PDF/correo, y la lista de fotos
        # extraídas (`_archivos_revision`) vive en memoria -- NO se guarda en
        # `lote.sqlite`, así que recargar un lote terminado nunca la repone y
        # `_hay_fotos_para_elegir()` da False: el asistente salta del 1 al 3
        # solo (comportamiento real y documentado del código, no una falla).
        # Volver a producirla exigiría re-correr la extracción del catálogo.
        #
        # Así que la pantalla se arma llamando directo al método que la
        # construye, con fotos REALES del lote (su carpeta `revision/`), que
        # es exactamente lo que le pasaría la extracción. Se cierra con
        # «Cancelar» (`_cerrar_revision_extraccion`), que NO escribe nada en
        # disco -- a diferencia de "Continuar" (`_confirmar_seleccion_revision`),
        # que grabaría `entrada_archivos` en el `lote.sqlite` del lote de
        # prueba y lo dejaría distinto para la corrida siguiente (adiós
        # determinismo).
        fotos = sorted((LOTE / "revision").glob("*.jpg"))
        assert fotos, f"el lote {LOTE.name} no tiene fotos en revision/ para armar el paso 2"
        app._mostrar_revision_extraccion(fotos, exclusiones={str(fotos[0])})
        snaps.append(_snapshot(app, "paso_2_elegir_que_limpiar"))
        app._cerrar_revision_extraccion()
        snaps.append(_snapshot(app, "cancelar_paso_2_vuelve_al_1"))
    finally:
        # Cerrar la ventana de verdad: sin esto quedan el intérprete Tcl y los
        # hilos daemon de la app vivos entre tests.
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass

    return snaps, avisos


def _hay_entorno():
    return LOTE.exists() and _conexion_postgres_disponible()


def test_flujo_de_7_pasos_no_se_mueve_de_lo_congelado():
    """Recorre los 7 pasos sobre un lote real y compara cada pantalla contra
    la línea base de `tests/fixtures/snapshot_flujo_7_pasos.json`.

    Si esto falla después de partir `HerramientaUnica` en mixins, el mensaje
    dice EXACTAMENTE qué paso y qué campo cambió, con esperado vs. real. Si
    el cambio fue a propósito (se rediseñó una pantalla), se revuelca la
    referencia con REGENERAR_SNAPSHOT_FLUJO=1 -- lo que este test no permite
    es que una pantalla cambie sin que nadie lo note."""
    if not _hay_entorno():
        pytest.skip(f"requiere Postgres local + el lote {LOTE.name} en disco")

    snaps, _avisos = _recorrer_los_7_pasos()

    if os.environ.get("REGENERAR_SNAPSHOT_FLUJO"):
        REFERENCIA.parent.mkdir(parents=True, exist_ok=True)
        REFERENCIA.write_text(json.dumps(snaps, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        pytest.skip(f"línea base regenerada en {REFERENCIA} -- volver a correr sin "
                    "REGENERAR_SNAPSHOT_FLUJO para que el test compare de verdad")

    assert REFERENCIA.exists(), (
        f"falta la línea base {REFERENCIA} -- generarla con REGENERAR_SNAPSHOT_FLUJO=1")
    esperados = json.loads(REFERENCIA.read_text(encoding="utf-8"))

    assert [s["etiqueta"] for s in snaps] == [s["etiqueta"] for s in esperados], (
        "cambió la SECUENCIA de pantallas del asistente:\n"
        f"  esperada: {[s['etiqueta'] for s in esperados]}\n"
        f"  real:     {[s['etiqueta'] for s in snaps]}")

    diferencias = []
    for real, esperado in zip(snaps, esperados):
        for campo in sorted(set(esperado) | set(real)):
            if real.get(campo) != esperado.get(campo):
                diferencias.append(
                    f"[{esperado['etiqueta']}] campo {campo!r}:\n"
                    f"    esperado: {esperado.get(campo)!r}\n"
                    f"    real:     {real.get(campo)!r}")
    assert not diferencias, (
        "el recorrido de los 7 pasos cambió respecto de la línea base "
        f"({REFERENCIA.name}):\n\n" + "\n".join(diferencias))


def test_los_7_pasos_se_visitan_todos():
    """Guarda aparte del snapshot: que el recorrido de arriba TOQUE los 7
    pasos. Sin esto, alguien podría regenerar una línea base que ya no pasa
    por el paso 5 (o por el 7) y el test seguiría "verde" comparando un
    recorrido mutilado contra sí mismo."""
    if not _hay_entorno():
        pytest.skip(f"requiere Postgres local + el lote {LOTE.name} en disco")

    esperados = json.loads(REFERENCIA.read_text(encoding="utf-8")) if REFERENCIA.exists() else []
    if not esperados:
        pytest.skip("todavía no hay línea base -- generarla con REGENERAR_SNAPSHOT_FLUJO=1")

    visitados = {s["paso_flujo"] for s in esperados}
    faltan = sorted(set(range(1, 8)) - visitados)
    assert not faltan, (
        f"la línea base no pasa por los pasos {faltan} -- el recorrido quedó "
        "incompleto y deja esos pasos sin red de seguridad")
