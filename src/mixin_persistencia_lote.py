"""
Lo que el lote RECUERDA: moneda y margen por proveedor, costos y categorías
escritos a mano, canal de venta, mes de venta esperado, la marca del lote, y
todo el retomar un proyecto anterior.

División 2026-09-23, Fase 4: `HerramientaUnica` era UNA clase de ~7,200 líneas
con los 7 pasos del asistente adentro. Lo que se juntó acá tiene un hilo en
común muy concreto: son los datos que sobreviven a cerrar la app. Cada uno de
estos métodos o escribe algo en los metadatos locales del lote (`lote.sqlite`
de la carpeta de trabajo) o lo vuelve a leer para dejar la ventana como estaba
—incluido deducir en qué paso quedó un lote viejo y saltar directo ahí.

El porqué de agruparlos: un dato mal persistido no se nota en el paso donde se
guardó, se nota tres pasos después (un precio calculado con el tipo de cambio
del proveedor equivocado, un costo escrito a mano que se perdió al reprocesar).
Tenerlos juntos hace que la respuesta a "¿dónde queda esto guardado?" sea un
solo archivo.

Es un mixin, no una jerarquía con comportamiento propio: todo se combina en la
MISMA instancia, así que `self.<lo que sea>` sigue funcionando igual que antes.
Los cuerpos se movieron LITERALMENTE, sin cambiar una línea.
"""

from __future__ import annotations

import json
import re
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import almacen
import decisiones
import rutas_externas

from util_ventana import _centrar_en_ventana_principal, _logger_gui, _traer_al_frente
from tema import PAR_BORDE, PAR_PANEL_SUAVE, PAR_TEXTO, RADIO_CONTROL
from widgets_puente import Boton, Etiqueta, Marco
from precios import MARGEN_VENTA_DEFAULT, MONEDAS_PROVEEDOR, _rasgo_moneda, moneda_convierte

# Mismos dos valores que en `gui_profesional_ctk.py`, calculados igual: este
# archivo vive en la misma carpeta `src/`, así que `PROYECTO` da lo mismo.
PROYECTO = Path(__file__).resolve().parent
CARPETA_LOTES_CENTRAL = rutas_externas.ASISTENTE_COMPRAS_LOTES


class _MixinPersistenciaLote:
    """Guardar y recuperar el estado del lote entre sesiones.

    Ver el docstring del módulo: agrupamiento por "esto sobrevive a cerrar la
    app", no por tecnología ni por pantalla.
    """

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
            _logger_gui().exception("no se pudo inspeccionar la carpeta de trabajo %s", carpeta)
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
            _logger_gui().exception("no se pudieron leer los metadatos del lote en %s", carpeta)
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
            # margen no numerico guardado en el lote: se cae al margen por defecto.
            margen_f = MARGEN_VENTA_DEFAULT
        try:
            tc_f = float(tipo_cambio) if tipo_cambio else None
        except (TypeError, ValueError):
            # tipo de cambio no numerico guardado en el lote: se deja sin convertir.
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
                _logger_gui().exception("respaldo de costos del lote ilegible (JSON invalido)")
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
                _logger_gui().exception("respaldo de categorias del lote ilegible (JSON invalido)")
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
            # mes de venta guardado no numerico: se trata como "no preguntado todavia".
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
                _logger_gui().exception("mes elegido no reconocido en el dialogo de mes de venta")
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
            # primera corrida o archivo de sesion borrado: se pide la carpeta a mano.
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
            _logger_gui().exception("no se pudieron contar los recortes de %s", carpeta)
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
                # carpeta borrada mientras se listaba: queda al final del orden por fecha.
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
