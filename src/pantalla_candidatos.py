"""
Pantalla de candidatos (pasos 6 y 7) -- Fase 2 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Es el corte que más devuelve: ~2,300 líneas que son una pantalla COMPLETA y
autónoma (lista de candidatos calificados, confirmación de categoría/género,
PDV objetivo y pedido sugerido) y que no compartían nada con el resto del
archivo salvo utilidades ya extraídas en las fases 0 y 1. Verificado antes de
cortar: no hay ciclo -- esta pantalla usa `panel_comparables`, `precios`,
`colores_producto`, `widgets_puente`, `tema` y `util_ventana`, y ninguno de
ellos la usa a ella.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

import almacen
import decisiones
import indice_fuente
import nucleo
import rutas_externas

from colores_producto import (
    _colores_de_nombre, _hex_de_color, _normalizar_color, _pastilla_color,
    _texto_sobre, codigo_visible,
)
from precios import (
    CATEGORIAS_CONOCIDAS, FUENTES_PRECIO_LEGIBLE, GENEROS_CONOCIDOS, IVA_CR,
    MARGEN_VENTA_DEFAULT, ROTULO_PRECIO_COMPARABLES, ROTULO_PRECIO_POR_COSTO,
    _rasgo_moneda, calcular_precio_venta, moneda_convierte, moneda_valida,
    redondear_pvp,
    texto_precio_referencia,
)
from tema import (
    COLOR_BORDE, COLOR_BORDE_FUERTE, COLOR_FONDO, COLOR_MAL, COLOR_MAL_BG,
    COLOR_NEUTRO, COLOR_OK, COLOR_OK_BG, COLOR_PANEL, COLOR_PRIMARIO,
    COLOR_TEXTO, COLOR_TEXTO_SUAVE, COLOR_VISOR_BG, F, PAR_ACENTO, PAR_ALERTA,
    PAR_ALERTA_BG, PAR_BORDE, PAR_FONDO, PAR_MAL, PAR_MAL_BG, PAR_MAL_HOVER,
    PAR_NEUTRO, PAR_OK, PAR_OK_BG, PAR_OK_HOVER, PAR_PANEL, PAR_PANEL_SUAVE,
    PAR_PRIMARIO, PAR_PRIMARIO_OSCURO, PAR_PRIMARIO_SUAVE, PAR_SELECCION,
    PAR_TEXTO, PAR_TEXTO_SUAVE, PAR_VISOR_BG, RADIO_CONTROL, RADIO_PANEL,
    solido,
)
from util_ventana import (
    _badge, _cargar_foto_generico, _centrar_en_ventana_principal,
    _logger_gui, _nombre_archivo_seguro, _traer_al_frente, reloj,
)
from widgets_puente import (
    Barra, Boton, BotonMenu, Casilla, CasillaOk, Etiqueta, Marco, Separador,
    SeparadorV, Tarjeta,
)
from panel_comparables import PanelComparablesCTk


class VentanaCandidatosCTk(ctk.CTkFrame):
    """Pasos 6 y 7 del flujo: lista de candidatos ya calificados (foto, categoría,
    género, color, grado), con acceso nativo a los tres pasos que antes solo
    vivían en la web (`servidor_pty.py` en 127.0.0.1:8900) -- comparables por
    candidato, confirmación manual de categoría/género ambiguos, y PDV
    objetivo → pedido sugerido. Todo llama directo a las mismas funciones de
    `servidor_pty.py` que usa la web (vía `motor_candidatos.py`), sin HTTP.

    Plan 2026-09-07, paso 8: dejó de ser un `CTkToplevel` (una ventana aparte
    que tapaba la app y, al cerrarla, solo se recuperaba reenviando el
    catálogo completo a calificar) y pasó a ser una PANTALLA dentro de la
    ventana principal, con el mismo mecanismo de pack/pack_forget que ya usan
    el resumen final y la selección de limpieza. El nombre de la clase se
    conserva para no romper las referencias existentes."""

    def __init__(self, master: tk.Misc, candidatos: list[dict], al_volver=None) -> None:
        super().__init__(master, fg_color="transparent")
        self._fotos_tk: list[ImageTk.PhotoImage] = []  # referencias vivas
        self._candidatos = candidatos
        self._al_volver = al_volver

        import motor_candidatos
        self._mc = motor_candidatos

        # Moneda / tipo de cambio / margen declarados PARA ESTE LOTE (metadatos
        # locales de la carpeta de trabajo, ver `config_precio_lote` en la
        # ventana principal). Si esta pantalla se construyó suelta (pruebas),
        # se cae a los defaults sin reventar.
        leer_cfg = getattr(master, "config_precio_lote", None)
        self._cfg_precio = (leer_cfg() if callable(leer_cfg)
                            else {"moneda": None, "tipo_cambio": None,
                                  "margen": MARGEN_VENTA_DEFAULT})
        # FASE 3 multi-proveedor (2026-09-16): la configuración de precio es
        # POR PROVEEDOR. `self._cfg_precio` sigue siendo la del proveedor
        # activo (y la única que se usa en un lote de un solo proveedor, donde
        # todo funciona exactamente como antes); `self._cfgs_por_proveedor`
        # tiene la de cada proveedor del lote, y es la que consulta cada
        # tarjeta según el `proveedor_id` de SU candidato (ver `_cfg_de`).
        leer_cfgs = getattr(master, "configs_precio_por_proveedor", None)
        self._cfgs_por_proveedor: dict = (leer_cfgs() if callable(leer_cfgs) else {})
        # Nombre de cada proveedor, para rotular su fila de moneda/margen
        # cuando el lote trae más de uno.
        self._nombres_proveedor: dict = {}
        for c in candidatos:
            pid = c.get("proveedor_id")
            if pid is not None and c.get("proveedor"):
                self._nombres_proveedor[int(pid)] = str(c["proveedor"])
        # Variables del campo «Margen de venta ×», una por proveedor.
        self._v_margenes: dict = {}
        # Cada tarjeta registra acá una función que repinta su propio desglose
        # de precio: así, cambiar el margen una vez arriba recalcula TODAS las
        # tarjetas en vivo, sin reconstruir la pantalla (que perdería el scroll
        # y volvería a consultar la base para cada candidato).
        self._repintar_precio: list = []
        # Tarjetas registradas para el botón único «💾 Guardar lote» (Tarea 5,
        # 2026-09-10). Cada entrada sabe leer su propio campo de costo y decir
        # si cambió respecto de lo persistido -- ver `_bloque_precio_costo`.
        self._pendientes_costo: list[dict] = []

        # Espejo LOCAL de los costos ingresados a mano (ver el bloque
        # "respaldo LOCAL de los costos" en la ventana principal). Se carga
        # una sola vez acá y se mantiene en memoria; cada cambio lo vuelca
        # completo a `lote.sqlite`.
        leer_costos = getattr(master, "leer_costos_lote", None)
        self._costos_lote: dict = (leer_costos() if callable(leer_costos) else {})
        self._costos_restaurados = 0

        # Espejo LOCAL de las correcciones de tipo/género (misma idea que los
        # costos, ver `leer_categorias_lote` en la ventana principal).
        leer_cats = getattr(master, "leer_categorias_lote", None)
        self._categorias_lote: dict = (leer_cats() if callable(leer_cats) else {})
        self._categorias_restauradas = 0

        encabezado = Marco(self)
        encabezado.pack(fill="x", padx=12, pady=(4, 0))
        # El botón "← Volver a revisar fotos" salió de acá (2026-09-10, pedido
        # del usuario): el pie del wizard ya trae un "Atrás" estándar en TODAS
        # las pantallas, así que este era un segundo camino para lo mismo,
        # ocupando el lugar más visible del encabezado. `self._volver` sigue
        # existiendo (lo llama el anfitrión del wizard).
        Etiqueta(encabezado, text=f"{len(candidatos)} candidato(s) calificado(s)",
                 style="Subtitulo.TLabel").pack(side="left")
        # Qué catálogo es esto, integrado a la barra que ya existía en vez de
        # la franja aparte de dos renglones que había abajo (2026-09-10): el
        # proveedor y el nombre del catálogo SÍ hacen falta (evitan revisar sin
        # saber si se mezclaron dos catálogos), la frase "Ordenados por
        # calificación..." no -- el orden se ve solo.
        self._etq_catalogo = Etiqueta(encabezado, text="", style="Suave.TLabel")
        self._etq_catalogo.pack(side="left", padx=(12, 0))

        # Dos sub-pantallas, no dos pestañas (2026-09-08, pedido del usuario):
        # el sugerido de compra es el PASO FINAL del asistente, no una pestaña
        # escondida al costado de los candidatos. Cuál se ve la decide el paso
        # del asistente (6 = candidatos, 7 = sugerido) vía `mostrar_*`.
        self._vista_candidatos = Marco(self)
        self._vista_optimizador = Marco(self)
        self._construir_tab_candidatos(self._vista_candidatos)
        self._construir_tab_optimizador(self._vista_optimizador)
        self.mostrar_candidatos()

        # Si hubo que reponer tipo/género desde el respaldo, la lista que se
        # acaba de pintar quedó con los scores VIEJOS (el re-scoring corrió en
        # la base recién ahora, y el score depende del filtro categoría+género).
        # Se recarga una vez, en cuanto la pantalla está armada. La instancia
        # nueva no encontrará nada que reponer, así que no hay ciclo.
        if self._categorias_restauradas:
            self.after_idle(self._recargar)

    # ---------- qué sub-pantalla se ve (la manda el paso del asistente) -----

    def _mostrar_solo(self, vista) -> None:
        for otra in (self._vista_candidatos, self._vista_optimizador):
            if otra is not vista and otra.winfo_manager():
                otra.pack_forget()
        if not vista.winfo_manager():
            # Márgenes más ajustados (2026-09-09): la lista de candidatos es la
            # pantalla donde más falta hace el espacio vertical.
            vista.pack(fill="both", expand=True, padx=8, pady=(2, 6))

    def mostrar_candidatos(self) -> None:
        self._mostrar_solo(self._vista_candidatos)

    def mostrar_optimizador(self) -> None:
        self._mostrar_solo(self._vista_optimizador)
        # Señal de "estás en el último paso" desde el momento en que se entra,
        # no solo después de generar: si todavía no hay selección, se dice qué
        # falta para cerrar el proceso.
        if not getattr(self, "_ultimo_sugerido", None):
            self._pintar_cierre_pendiente()

    def _volver(self) -> None:
        if self._al_volver is not None:
            self._al_volver()
        else:  # construida suelta (pruebas): al menos se saca de la pantalla
            self.destroy()

    # ---------- pestaña Candidatos ----------

    def _construir_tab_candidatos(self, tab) -> None:
        candidatos = self._candidatos
        # Antes de leer cualquier precio de la base: reponer los costos que el
        # comprador ya había ingresado para este lote y que staging_tuc pudo
        # haber perdido (es efímero y compartido). Así las tarjetas se pintan
        # una sola vez, ya con los precios correctos.
        self._restaurar_costos_del_respaldo()
        self._restaurar_categorias_del_respaldo()
        self._autodetectar_marcas()
        # El titulazo "N candidato(s) calificado(s)" estaba DOS veces: acá y en
        # el encabezado de la pantalla, tres líneas más arriba. Se quita el
        # duplicado (2026-09-09) -- eran ~50px de alto que le faltaban a la
        # lista de tarjetas, que es lo que el comprador vino a mirar.
        # Encabezado del catálogo activo (plan 2026-09-07, paso 1): antes no
        # había forma de saber, mirando esta pantalla, si lo que se ve es UN
        # catálogo o varios mezclados sin querer.
        # 2026-09-10: esto eran DOS renglones propios de franja ("Catálogo:
        # ..." + "Ordenados por calificación..."), ~50px que el comprador no
        # usaba para decidir nada. El dato que sí importa (proveedor, catálogo
        # y cuántos) se comprime a una línea y se cuelga de la barra superior
        # que ya existía (`self._etq_catalogo`, ver `__init__`).
        resumen = self._mc.resumen_catalogo_activo()
        texto_resumen = " · ".join(f"{f['proveedor']} — {f['catalogo_origen']} ({f['n']})"
                                    for f in resumen) or "sin catálogo identificado"
        try:
            self._etq_catalogo.configure(text=f"·  {texto_resumen}")
        except (AttributeError, tk.TclError):
            pass  # construida suelta (pruebas): el encabezado puede no existir

        self._barra_precio_lote(tab)

        # Los comparables salieron de la columna lateral y volvieron a una
        # ventana propia (2026-09-09, pedido del usuario: "que sea una ventana
        # despegable"). El panel lateral era de ancho FIJO (400px) y alto
        # heredado, y adentro convivían cuatro bloques de alto fijo (título,
        # selector de color, precio, y la tarjeta grande de "Guardar
        # cotización" anclada abajo) más la lista de comparables con expand.
        # Cuando el alto disponible no alcanzaba para todos, `pack` reparte en
        # ORDEN de empaquetado: los de alto fijo se llevaban todo y la lista de
        # comparables --que se empaqueta última-- se quedaba con 0px de alto.
        # Se veía exactamente como lo describió el usuario: los comparables
        # "tapados detrás del cuadro de Guardar cotización". Con ventana propia
        # y redimensionable hay alto de sobra, y adentro se usa `grid` con peso
        # para que la lista NUNCA pueda quedar en cero.
        self._win_comparables: ctk.CTkToplevel | None = None
        self._panel_comparables_contenido = None

        # FASE 4: pastillas «Todos / <proveedor A> / <proveedor B>». Se
        # construye SOLO si el lote es multi-proveedor; con uno solo no se
        # agrega ningún widget (ni un marco vacío).
        self._tarjetas_por_proveedor: list[tuple] = []
        self._btns_filtro: dict = {}
        self._filtro_proveedor = None  # None = todos
        if self._es_multiproveedor():
            self._barra_filtro_proveedor(tab)

        contenedor = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        contenedor.pack(fill="both", expand=True)

        if not candidatos:
            Etiqueta(contenedor, text="No se encontró ningún candidato calificado.",
                    style="Suave.TLabel").pack(padx=8, pady=8)
            return

        for cand in candidatos:
            tarjeta = self._fila_candidato(contenedor, cand)
            pid, _nombre = self._proveedor_de(cand)
            self._tarjetas_por_proveedor.append((pid, tarjeta))

    # ---------- configuración de precio del proveedor de CADA candidato ------

    def _proveedores_en_pantalla(self) -> list:
        """Los `proveedor_id` presentes en los candidatos, en orden de
        aparición. Lista `[None]` si los candidatos no traen proveedor
        (pantalla suelta en pruebas): ese es el camino de un solo juego de
        parámetros, idéntico al de antes de la Fase 3."""
        vistos = []
        for cand in self._candidatos:
            pid = cand.get("proveedor_id")
            pid = int(pid) if pid is not None else None
            if pid not in vistos:
                vistos.append(pid)
        return vistos or [None]

    # ---------- FASE 4: de qué proveedor es cada candidato ------------------
    #
    # Paleta de las pastillas de proveedor. Deliberadamente NO usa el verde de
    # `PAR_OK` ni el rojo de `PAR_MAL`: esos dos ya significan "grado S/A" y
    # "grado C/D" en el badge de score de la misma tarjeta, y reusarlos acá
    # haría leer el proveedor como una calificación.
    # Tampoco usa el ámbar de `PAR_ALERTA` en los dos primeros lugares (que
    # son los que se ven en el caso real de dos proveedores): una pastilla
    # ámbar se lee como una advertencia sobre esa referencia, y acá no
    # advierte nada -- solo dice de quién es.
    PALETA_PROVEEDOR = (
        (PAR_PRIMARIO_SUAVE, PAR_PRIMARIO),
        (PAR_PANEL_SUAVE, PAR_ACENTO),
        (PAR_SELECCION, PAR_TEXTO),
        (PAR_ALERTA_BG, PAR_ALERTA),
    )

    def _es_multiproveedor(self) -> bool:
        """¿Hay candidatos de MÁS DE UN proveedor en esta pantalla?

        Es el interruptor de toda la Fase 4: con un solo proveedor --el caso
        mayoritario-- devuelve False y la pantalla no muestra ni un elemento
        nuevo respecto de la Fase 3 (ni pastilla en las tarjetas, ni barra de
        filtro, ni columna de proveedor en el paso 7)."""
        return len([p for p in self._proveedores_en_pantalla() if p is not None]) > 1

    def _nombre_de_proveedor(self, pid) -> str:
        if pid is None:
            return ""
        return self._nombres_proveedor.get(int(pid), "") or f"Proveedor {pid}"

    def _proveedor_de(self, cand: dict) -> tuple:
        """`(proveedor_id, nombre legible)` de un candidato. El nombre que trae
        el propio candidato manda (es el que devuelve `api_candidatos`); el
        mapa `self._nombres_proveedor` es el respaldo."""
        pid = cand.get("proveedor_id")
        pid = int(pid) if pid is not None else None
        nombre = str(cand.get("proveedor") or "") or self._nombre_de_proveedor(pid)
        return pid, nombre

    def _colores_pastilla_proveedor(self, pid) -> tuple:
        """Color estable por proveedor: el mismo proveedor se ve igual en todas
        las tarjetas y en el paso 7, según su orden de aparición en el lote."""
        orden = [p for p in self._proveedores_en_pantalla() if p is not None]
        try:
            i = orden.index(pid)
        except ValueError:
            # proveedor fuera de la lista en pantalla: se le da el ultimo color libre.
            i = len(orden)
        return self.PALETA_PROVEEDOR[i % len(self.PALETA_PROVEEDOR)]

    def _pastilla_proveedor(self, padre, cand: dict):
        """La pastilla con el nombre del proveedor del candidato, en el mismo
        espíritu que los badges de grado y las pastillas de color que ya
        existen. Devuelve el widget listo para `.pack()`/`.grid()`."""
        pid, nombre = self._proveedor_de(cand)
        par_bg, par_fg = self._colores_pastilla_proveedor(pid)
        return tk.Label(padre, text=nombre or "sin proveedor",
                        bg=solido(par_bg), fg=solido(par_fg),
                        font=("Segoe UI Semibold", 9), padx=7, pady=1,
                        borderwidth=1, relief="solid")

    # ---------- FASE 4: filtro por proveedor (solo si hay más de uno) -------

    def _barra_filtro_proveedor(self, tab) -> None:
        """Pastillas «Todos / <proveedor A> / <proveedor B>» arriba de la
        grilla, para revisar un catálogo a la vez sin perder la vista conjunta.

        No reconstruye nada: las tarjetas ya están armadas (con su costo
        tecleado, su marca declarada y su desglose de precio) y el filtro
        solo las despacka y las vuelve a packar en orden. Reconstruirlas
        perdería lo que el comprador tenga a medio escribir."""
        barra = Marco(tab)
        barra.pack(fill="x", padx=4, pady=(0, 6))
        Etiqueta(barra, text="Ver:", style="Suave.TLabel").pack(side="left", padx=(0, 6))

        conteos: dict = {}
        for cand in self._candidatos:
            pid, _n = self._proveedor_de(cand)
            conteos[pid] = conteos.get(pid, 0) + 1

        opciones = [(None, f"Todos ({len(self._candidatos)})")]
        for pid in self._proveedores_en_pantalla():
            opciones.append((pid, f"{self._nombre_de_proveedor(pid)} "
                                  f"({conteos.get(pid, 0)})"))
        for pid, rotulo in opciones:
            btn = Boton(barra, text=rotulo, style="Sutil.TButton",
                        width=max(90, 9 * len(rotulo)))
            btn.configure(command=lambda p=pid: self._aplicar_filtro_proveedor(p))
            btn.pack(side="left", padx=(0, 6))
            self._btns_filtro[pid] = btn
        self._pintar_filtro_activo()

    def _pintar_filtro_activo(self) -> None:
        for pid, btn in self._btns_filtro.items():
            try:
                if pid == self._filtro_proveedor:
                    btn.configure(fg_color=PAR_PRIMARIO, border_color=PAR_PRIMARIO,
                                  text_color=("#ffffff", "#ffffff"))
                else:
                    btn.configure(fg_color="transparent", border_color=PAR_BORDE,
                                  text_color=PAR_TEXTO_SUAVE)
            except tk.TclError:
                pass  # barra destruida (la pantalla se recargó)

    def _aplicar_filtro_proveedor(self, pid) -> None:
        self._filtro_proveedor = pid
        self._pintar_filtro_activo()
        for _p, tarjeta in self._tarjetas_por_proveedor:
            try:
                if tarjeta.winfo_manager():
                    tarjeta.pack_forget()
            except tk.TclError:
                # tarjeta ya destruida (pantalla recargada): no hay nada que despackar.
                pass
        # Se vuelven a packar EN ORDEN: `pack` respeta el orden de llamada, así
        # que la lista mantiene su orden por calificación.
        for p, tarjeta in self._tarjetas_por_proveedor:
            if pid is not None and p != pid:
                continue
            try:
                tarjeta.pack(fill="x", pady=4)
            except tk.TclError:
                # tarjeta ya destruida (pantalla recargada): no hay nada que volver a packar.
                pass

    def _cfg_de(self, cand: dict) -> dict:
        """La configuración de precio del proveedor DE ESTE candidato.

        FASE 3: es el corazón del cambio. Antes todas las tarjetas leían
        `self._cfg_precio` (un solo juego de moneda/tipo de cambio/margen para
        el lote entero), así que en un lote con un proveedor en colones y otro
        en dólares una de las dos mitades calculaba mal. Si el candidato no
        trae proveedor, o ese proveedor no tiene configuración propia, se cae a
        `self._cfg_precio` -- el comportamiento de antes."""
        pid = cand.get("proveedor_id")
        if pid is not None:
            cfg = self._cfgs_por_proveedor.get(int(pid))
            if cfg:
                return cfg
        return self._cfg_precio

    def _cfg_de_proveedor(self, pid) -> dict:
        if pid is not None:
            cfg = self._cfgs_por_proveedor.get(int(pid))
            if cfg:
                return cfg
        return self._cfg_precio

    @staticmethod
    def _texto_moneda_cfg(cfg: dict) -> str:
        moneda = cfg.get("moneda")
        if not moneda_valida(moneda):
            return "Moneda del proveedor sin declarar"
        nombre = _rasgo_moneda(moneda, "nombre_mayus", str(moneda))
        if not moneda_convierte(moneda):
            return f"Costo del proveedor en {nombre}  ·  sin conversión"
        simbolo = _rasgo_moneda(moneda, "simbolo", "?")
        if not cfg.get("tipo_cambio"):
            return f"Costo del proveedor en {nombre}  ·  falta el tipo de cambio"
        return (f"Costo del proveedor en {nombre}  ·  tipo de cambio "
                f"₡{cfg['tipo_cambio']:,.2f} por {simbolo}")

    def _barra_precio_lote(self, tab) -> None:
        """Los parámetros con los que se calcula el precio de venta sugerido:
        moneda del proveedor, tipo de cambio y margen.

        FASE 3 (2026-09-16): son POR PROVEEDOR, no del lote entero. Con un solo
        proveedor -- el caso mayoritario -- se ve exactamente la misma fila de
        siempre; con dos catálogos en el mismo lote se ve una fila por
        proveedor, rotulada con su nombre, y cada una manda sobre las tarjetas
        de SUS candidatos.

        El margen es editable y recalcula en vivo el desglose de las tarjetas
        de ese proveedor; el IVA se muestra pero NO se puede editar: es una
        regla fiscal de Costa Rica, no una decisión comercial."""
        tarjeta = Tarjeta(tab)
        tarjeta.pack(fill="x", padx=4, pady=(0, 8))

        proveedores = self._proveedores_en_pantalla()
        rotular = len(proveedores) > 1
        for pid in proveedores:
            self._fila_precio_proveedor(tarjeta, pid, rotular)

        # Se empaqueta SOLO cuando tiene algo que decir: una etiqueta vacía
        # empaquetada deja una franja en blanco adentro de la tarjeta, que es
        # exactamente el "recuadro vacío sin explicación" que se está
        # corrigiendo en el resto de esta pantalla.
        self._estado_margen = Etiqueta(tarjeta, text="", style="Suave.TLabel",
                                       wraplength=900, justify="left")

        # ── EL botón de guardar (uno solo para toda la pantalla) ──
        # 2026-09-10, pedido del usuario: antes cada tarjeta tenía su propio
        # "✓ Guardar costo" y este botón solo forzaba el respaldo local. Ahora
        # este es el ÚNICO que guarda: recorre las 18 tarjetas, persiste las
        # que cambiaron (mismas escrituras que antes, ver `_persistir_costo`) y
        # después vuelca el espejo local una sola vez.
        fila_guardar = Marco(tarjeta)
        fila_guardar.pack(fill="x", padx=10, pady=(0, 8))
        Boton(fila_guardar, text="💾 Guardar lote",
              style="Primario.TButton", width=170,
              command=self._guardar_avances_lote).pack(side="left")
        self._estado_guardado = Etiqueta(fila_guardar, text="", style="Suave.TLabel",
                                         wraplength=620, justify="left")
        self._estado_guardado.pack(side="left", padx=(10, 0))
        if self._costos_restaurados:
            self._decir_guardado(f"Se recuperaron {self._costos_restaurados} costo(s) "
                                 "guardados de este lote.")

    def _fila_precio_proveedor(self, tarjeta, pid, rotular: bool) -> None:
        """Una fila de parámetros de precio: la moneda/tipo de cambio de ESE
        proveedor (con su botón «Cambiar») y su margen editable.

        `rotular=False` (lote de un solo proveedor) deja la fila idéntica a la
        que había antes de la Fase 3, sin el nombre del proveedor adelante."""
        cfg = self._cfg_de_proveedor(pid)
        fila = Marco(tarjeta)
        fila.pack(fill="x", padx=10, pady=(8, 8))

        if rotular:
            nombre = self._nombres_proveedor.get(int(pid), "") if pid is not None else ""
            Etiqueta(fila, text=f"{nombre or f'Proveedor {pid}'}:",
                     style="Subtitulo.TLabel").pack(side="left", padx=(0, 8))

        Etiqueta(fila, text=self._texto_moneda_cfg(cfg),
                 style="Suave.TLabel").pack(side="left")

        Boton(fila, text="Cambiar", style="Sutil.TButton", width=70,
              command=lambda p=pid: self._cambiar_moneda_lote(p)).pack(
            side="left", padx=(10, 20))

        Etiqueta(fila, text=f"IVA {IVA_CR * 100:.0f}% (fijo)",
                 style="Suave.TLabel").pack(side="left", padx=(0, 18))

        Etiqueta(fila, text="Margen de venta ×", style="Suave.TLabel").pack(side="left")
        v_margen = tk.StringVar(value=f"{cfg['margen']:g}")
        self._v_margenes[pid] = v_margen
        if not hasattr(self, "_v_margen"):
            # Alias del primer proveedor: `self._v_margen` es el nombre que ya
            # usaba el resto de la pantalla cuando había un solo margen.
            self._v_margen = v_margen
        ctk.CTkEntry(fila, textvariable=v_margen, width=80).pack(side="left", padx=(6, 6))
        Boton(fila, text="Aplicar", style="Primario.TButton", width=70,
              command=lambda p=pid: self._aplicar_margen(proveedor_id=p)).pack(side="left")
        # Recalcular al teclear, no solo con el botón: es lo que uno espera.
        v_margen.trace_add("write", lambda *_a, p=pid: self._aplicar_margen(
            silencioso=True, proveedor_id=p))

    def _guardar_avances_lote(self) -> None:
        """«💾 Guardar lote»: EL botón de guardar de esta pantalla (2026-09-10).

        Recorre TODAS las tarjetas registradas en `self._pendientes_costo`,
        persiste las que tengan el campo de costo distinto de lo guardado
        (`_persistir_costo`: costo crudo + precio de venta derivado + espejo),
        vuelca el espejo local UNA vez y recién entonces recarga la pantalla
        (que es lo que refresca las líneas de precio de referencia).

        Nada se guarda por tarjeta ni al perder el foco: si el comprador no
        aprieta este botón, el aviso «● sin guardar» de cada tarjeta se lo
        dice. Y si alguna tarjeta falla, se reportan las que fallaron sin
        perder las que sí se guardaron."""
        guardados, fallos = 0, []
        with reloj(self):
            for p in list(self._pendientes_costo):
                try:
                    if not p["hay_cambio"]():
                        continue
                except tk.TclError:
                    continue  # tarjeta destruida (pantalla reconstruida)
                motivo = self._persistir_costo(p["cand"], p["texto"](), p["estado"])
                if motivo:
                    codigo = codigo_visible(p["cand"].get("codigo_proveedor")) or p["cand"]["candidato_id"]
                    fallos.append(f"{codigo}: {motivo}")
                else:
                    guardados += 1
                    p["marcar"]()
            ok = self._volcar_costos_lote()

        n = len(self._costos_lote)
        partes = []
        if guardados:
            partes.append(f"✓ {guardados} costo(s) guardados.")
        elif not fallos:
            partes.append("No había cambios de costo pendientes.")
        if not ok:
            partes.append("⚠ No se pudo escribir el respaldo en la carpeta del lote: los "
                          "costos quedaron en la base de trabajo, revisá la carpeta de "
                          "salida del paso 1.")
        else:
            partes.append(f"Respaldo del lote al día ({n} referencia(s) con costo).")
        if fallos:
            partes.append("No se pudieron guardar: " + "; ".join(fallos))
        self._decir_guardado("  ".join(partes))
        if fallos:
            messagebox.showwarning("Asistente de Compras",
                                   "Algunas referencias no se pudieron guardar:\n\n"
                                   + "\n".join(fallos))
        if guardados:
            # La recarga va AL FINAL y una sola vez: es lo que actualiza la
            # línea de "precio de venta de comparables internos" de cada
            # tarjeta, que sale de la cascada en la base con el costo nuevo.
            self._recargar()

    def _decir_guardado(self, texto: str) -> None:
        """Escribe el aviso de guardado, tolerando que la pantalla se haya
        reconstruido en el medio (guardar un costo dispara `_recargar`, que
        destruye estas etiquetas y crea otras nuevas). El respaldo ya está
        hecho a esa altura: perder el cartelito no puede tumbar la app."""
        try:
            self._estado_guardado.configure(text=texto)
        except tk.TclError:
            # cartelito de "guardado" ya destruido: el aviso simplemente no se ve.
            pass

    def _decir_margen(self, texto: str) -> None:
        """Muestra el aviso del margen, empaquetándolo la primera vez. Nunca
        deja una etiqueta vacía ocupando espacio (ver `_barra_precio_lote`)."""
        self._estado_margen.configure(text=texto)
        if not self._estado_margen.winfo_manager():
            self._estado_margen.pack(anchor="w", padx=10, pady=(0, 8))

    def _cambiar_moneda_lote(self, proveedor_id=None) -> None:
        """Vuelve a preguntar moneda/tipo de cambio de UN proveedor (para
        corregir un tipo de cambio mal tecleado) y repinta los precios con el
        valor nuevo. FASE 3: la pregunta y el guardado son de ese proveedor, no
        del lote entero."""
        preguntar = getattr(self.master, "asegurar_moneda_lote", None)
        if not callable(preguntar):
            return
        nueva = preguntar(forzar=True, proveedor_id=proveedor_id)
        cambio = {"moneda": nueva.get("moneda"),
                  "tipo_cambio": nueva.get("tipo_cambio")}
        self._cfg_de_proveedor(proveedor_id).update(cambio)
        if proveedor_id is None:
            self._cfg_precio.update(cambio)
        self._recargar()

    def _aplicar_margen(self, silencioso: bool = False, proveedor_id=None) -> None:
        """Relee el margen del campo de UN proveedor y repinta el desglose de
        cada tarjeta (las de ese proveedor cambian; las de los demás se
        repintan con SU propio margen, que no se toca).

        `silencioso=True` es el camino del `trace` (se dispara con cada tecla,
        incluido el estado intermedio "2." mientras se escribe "2.7"): ahí un
        valor a medio teclear no se reporta como error, simplemente no se
        aplica hasta que sea un número válido."""
        var = self._v_margenes.get(proveedor_id) or getattr(self, "_v_margen", None)
        if var is None:
            return
        texto = var.get().strip().replace(",", "")
        try:
            margen = float(texto)
        except ValueError:
            if not silencioso:
                self._decir_margen("Margen inválido.")
            return
        if margen <= 0:
            if not silencioso:
                self._decir_margen("El margen tiene que ser mayor que cero.")
            return
        cfg = self._cfg_de_proveedor(proveedor_id)
        cfg["margen"] = margen
        if proveedor_id is None:
            self._cfg_precio["margen"] = margen
        guardar = getattr(self.master, "guardar_config_precio_proveedor", None)
        if callable(guardar):
            # Queda registrado en los metadatos del lote con qué margen se
            # sugirieron los precios DE ESE proveedor.
            guardar(proveedor_id, cfg["moneda"] or "CRC", cfg["tipo_cambio"], margen)
        else:
            guardar_viejo = getattr(self.master, "guardar_config_precio_lote", None)
            if callable(guardar_viejo):
                guardar_viejo(cfg["moneda"] or "CRC", cfg["tipo_cambio"], margen)
        nombre = (self._nombres_proveedor.get(int(proveedor_id), "")
                  if proveedor_id is not None else "")
        self._decir_margen(
            f"Precios de {nombre} recalculados con margen ×{margen:g}."
            if nombre and len(self._v_margenes) > 1
            else f"Precios recalculados con margen ×{margen:g}.")
        for repintar in list(self._repintar_precio):
            try:
                repintar()
            except tk.TclError:
                pass  # la tarjeta ya no existe (pantalla reconstruida)

    def _bloque_precio_costo(self, info, cand: dict) -> None:
        """El precio de venta sugerido POR FÓRMULA DE COSTO, con la fórmula a la
        vista paso por paso.

        Es un precio DISTINTO del de arriba ("Precio de venta de comparables
        internos encontrados"), y por eso conviven los dos en la tarjeta con
        etiquetas explícitas: uno sale del costo que cotizó este proveedor, el
        otro de lo que TU Calzado ya vende en productos parecidos. Confundirlos
        era justamente el problema."""
        try:
            costo, moneda_fila = self._mc.obtener_costo_declarado(cand["candidato_id"])
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo leer el costo declarado del candidato %s", cand["candidato_id"])
            costo, moneda_fila = None, None

        # Mutable para que `guardar_costo` pueda actualizar lo que ve
        # `repintar` sin reconstruir la tarjeta (que perdería el scroll).
        estado = {"costo": costo, "moneda_fila": moneda_fila}

        marco = Tarjeta(info)
        marco.pack(anchor="w", fill="x", pady=(4, 0))
        etq = Etiqueta(marco, text="", style="Suave.TLabel", justify="left",
                       wraplength=620)
        etq.pack(anchor="w", padx=8, pady=6)

        # ── campo editable del costo POR CANDIDATO ──
        # La moneda y el tipo de cambio se preguntan UNA VEZ POR LOTE (barra de
        # arriba), pero el costo es de cada producto: hasta ahora solo podía
        # entrar por el Excel del proveedor, y cuando ese Excel no traía la
        # columna de costo (el caso normal) no había ninguna forma de escribirlo.
        #
        # Todo en UN SOLO RENGLÓN (2026-09-09, pedido del usuario): el costo y
        # el precio de venta que sale de ese costo son las dos mitades de la
        # misma decisión, y tenerlos en filas separadas obligaba a saltar la
        # vista de arriba abajo para leer un número que depende del otro.
        # El desglose paso por paso de la fórmula queda abajo, en `etq`, para
        # quien quiera auditarlo.
        fila_costo = Marco(marco)
        fila_costo.pack(anchor="w", fill="x", padx=8, pady=(0, 8))
        Etiqueta(fila_costo, text="Costo:", style="Suave.TLabel").pack(
            side="left", padx=(0, 4))
        v_costo = tk.StringVar(value=(f"{estado['costo']:g}" if estado["costo"] else ""))
        entrada_costo = ctk.CTkEntry(fila_costo, textvariable=v_costo, width=90)
        entrada_costo.pack(side="left", padx=(0, 4))
        etq_unidad = Etiqueta(fila_costo, text="", style="Suave.TLabel")
        etq_unidad.pack(side="left", padx=(0, 8))
        Etiqueta(fila_costo, text="│", style="Suave.TLabel").pack(side="left", padx=(0, 8))
        etq_precio_vivo = Etiqueta(fila_costo, text="", style="Subtitulo.TLabel")
        etq_precio_vivo.pack(side="left", padx=(0, 10))
        # 2026-09-10, pedido del usuario: se fue el botón "✓ Guardar costo" que
        # tenía CADA tarjeta (18 botones idénticos en el catálogo Prueba2, y
        # cada clic recargaba la pantalla entera y perdía el scroll). Ahora el
        # campo queda "pendiente de guardar" en memoria y hay UN SOLO botón
        # «💾 Guardar lote» arriba que recorre todas las tarjetas. El aviso de
        # abajo es lo que evita que un costo tecleado se pierda en silencio.
        etq_pendiente = Etiqueta(fila_costo, text="", style="Suave.TLabel")
        etq_pendiente.pack(side="left")
        # "Quitar" ya NO escribe en la base: vacía el campo, y el borrado se
        # persiste con el mismo botón único que todo lo demás -- una sola
        # semántica para todos los cambios de costo de la pantalla.
        btn_quitar_costo = Boton(fila_costo, text="Quitar", style="Sutil.TButton", width=56,
                                 command=lambda: (v_costo.set(""), precio_en_vivo()))

        def texto_del_campo() -> str:
            """El costo tecleado, normalizado igual que lo normaliza el
            guardado. Se usa tanto para el aviso de "sin guardar" como para
            decidir, en el guardado en lote, si esta tarjeta cambió."""
            return (v_costo.get() or "").strip().replace(",", "").replace("₡", "").replace("$", "")

        def hay_cambio() -> bool:
            """True si lo que se lee en el campo es DISTINTO del costo que ya
            está persistido para esta referencia. Se compara numéricamente (no
            como texto) para que "1200", "1200.0" y "1,200" no cuenten como
            cambios y el botón único no reescriba lo que ya está bien."""
            texto = texto_del_campo()
            actual = estado["costo"]
            if not texto:
                return actual is not None
            try:
                return actual is None or abs(float(texto) - float(actual)) > 1e-9
            except ValueError:
                return True  # ilegible: que el guardado lo reporte como error

        def marcar_pendiente() -> None:
            try:
                etq_pendiente.configure(text="● sin guardar" if hay_cambio() else "")
            except tk.TclError:
                # etiqueta "sin guardar" ya destruida: no hay marca pendiente que pintar.
                pass

        # Registro para el botón único «💾 Guardar lote» (2026-09-10): la
        # pantalla es la dueña de la lista, cada tarjeta se anota acá con todo
        # lo que hace falta para persistirla sin volver a mirar la interfaz.
        self._pendientes_costo.append({
            "cand": cand, "estado": estado,
            "texto": texto_del_campo, "hay_cambio": hay_cambio,
            "marcar": marcar_pendiente,
        })

        def precio_en_vivo(*_a) -> None:
            """Recalcula SOLO LA VISTA del precio de venta sugerido, con lo que
            hay tecleado en el campo en este instante.

            Antes el número únicamente aparecía después de apretar «✓ Guardar
            costo» (que además recarga la pantalla entera), así que el comprador
            no podía tantear "si lo compro a X, lo vendo a Y" sin escribir en la
            base. Esto no guarda nada: el guardado sigue siendo explícito.

            El cálculo es la misma `calcular_precio_venta` de siempre (costo →
            colones → +IVA → ×margen → redondeo al ₡100), que es aritmética
            pura sobre tres números: no hace falta debounce."""
            cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
            texto = (v_costo.get() or "").strip().replace(",", "").replace("₡", "").replace("$", "")
            marcar_pendiente()
            if not texto:
                etq_precio_vivo.configure(text="Precio de venta sugerido: —")
                return
            try:
                valor = float(texto)
            except ValueError:
                etq_precio_vivo.configure(text="Precio de venta sugerido: costo inválido")
                return
            if valor <= 0:
                etq_precio_vivo.configure(text="Precio de venta sugerido: —")
                return
            moneda = cfg["moneda"] or estado.get("moneda_fila")
            d = (calcular_precio_venta(valor, moneda, cfg["tipo_cambio"], cfg["margen"])
                 if moneda_valida(moneda) else None)
            if d is None:
                etq_precio_vivo.configure(
                    text="Precio de venta sugerido: falta moneda / tipo de cambio")
                return
            etq_precio_vivo.configure(
                text=f"Precio de venta sugerido: ₡{d['precio_venta']:,.0f}")

        entrada_costo.bind("<KeyRelease>", precio_en_vivo)
        # También al pegar con el mouse o al salir del campo, para que la vista
        # nunca quede mostrando un precio que no corresponde a lo que se lee.
        entrada_costo.bind("<FocusOut>", precio_en_vivo)

        def repintar() -> None:
            cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
            costo = estado["costo"]
            moneda_fila = estado["moneda_fila"]
            # Rótulo de la unidad del campo: el comprador tiene que ver en qué
            # moneda está escribiendo, o el número no significa nada.
            unidad = cfg["moneda"] or moneda_fila
            etq_unidad.configure(text=(
                _rasgo_moneda(unidad, "plural") if moneda_valida(unidad)
                else "(falta declarar la moneda de este proveedor)"))
            # El renglón de arriba se recalcula acá también: así, cambiar el
            # margen del lote actualiza el precio en vivo de todas las tarjetas.
            precio_en_vivo()
            marcar_pendiente()
            if costo and not btn_quitar_costo.winfo_manager():
                btn_quitar_costo.pack(side="left", padx=(4, 0))
            elif not costo and btn_quitar_costo.winfo_manager():
                btn_quitar_costo.pack_forget()
            # La moneda declarada PARA EL PROVEEDOR DE ESTE CANDIDATO manda
            # sobre la de la fila: el comprador la declaró explícitamente
            # mirando el catálogo, mientras que `raw_catalogo.moneda_costo` se
            # infirió del Excel y puede venir en None. Solo se cae a la de la
            # fila si ese proveedor no tiene ninguna declarada.
            moneda = cfg["moneda"] or moneda_fila
            if costo is None:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: todavía no hay costo para esta "
                                   "referencia — escribilo abajo y se calcula al instante.")
                return
            if not moneda:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: falta declarar en qué "
                                   "moneda cotiza este proveedor (botón «Cambiar» arriba).")
                return
            d = calcular_precio_venta(costo, moneda, cfg["tipo_cambio"], cfg["margen"])
            if d is None:
                etq.configure(text=f"{ROTULO_PRECIO_POR_COSTO}: con "
                                   f"{_rasgo_moneda(moneda, 'plural', moneda)} hace falta "
                                   "el tipo de cambio (botón «Cambiar» arriba).")
                return
            if moneda_convierte(moneda):
                simbolo = _rasgo_moneda(moneda, "simbolo", "")
                linea_costo = (f"Costo del proveedor:  {simbolo}{d['costo_origen']:,.2f}  ×  "
                               f"₡{d['tipo_cambio']:,.2f}  =  ₡{d['costo_crc']:,.0f}")
            else:
                linea_costo = f"Costo del proveedor:  ₡{d['costo_crc']:,.0f}"
            etq.configure(text=(
                f"{ROTULO_PRECIO_POR_COSTO}\n"
                f"{linea_costo}\n"
                f"+ IVA ({IVA_CR * 100:.0f}%):  ₡{d['monto_iva']:,.0f}  →  ₡{d['costo_iva']:,.0f}\n"
                f"× Margen de venta ×{d['margen']:g}  →  ₡{d['precio_sin_redondear']:,.0f}\n"
                f"= Precio de venta sugerido:  ₡{d['precio_venta']:,.0f}  (redondeado al ₡100)"))

        repintar()
        self._repintar_precio.append(repintar)

    # ---------- costo por candidato: base efímera + espejo local ----------

    def _precio_vino_del_costo(self, cand: dict) -> bool:
        """True si el precio guardado como "manual" para este candidato lo
        calculó esta app a partir del costo ingresado por el comprador (y no lo
        escribió el comprador como precio de venta). Lo sabe el espejo local,
        que guarda `origen` junto con cada costo."""
        guardado = self._costos_lote.get(self._clave_costo(cand))
        return (isinstance(guardado, dict)
                and guardado.get("origen") == "costo_lote"
                and bool(guardado.get("precio_venta")))

    def _clave_costo(self, cand: dict) -> str:
        clave = getattr(self.master, "clave_costo", None)
        if callable(clave):
            return clave(cand.get("catalogo_origen"), cand.get("codigo_proveedor"))
        return f"{cand.get('catalogo_origen') or ''}||{cand.get('codigo_proveedor') or ''}"

    def _volcar_costos_lote(self) -> bool:
        """Escribe el espejo completo en `lote.sqlite`. True si se pudo."""
        guardar = getattr(self.master, "guardar_costos_lote", None)
        if not callable(guardar):
            return False
        try:
            guardar(self._costos_lote)
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo guardar el respaldo de costos del lote")
            return False
        return True

    def _restaurar_costos_del_respaldo(self) -> None:
        """Vuelve a poner en la revisión los costos que quedaron guardados en
        el respaldo local de este lote -- el caso para el que existe el espejo:
        la revisión de este lote se vació (reprocesarlo desde cero) y al
        retomarlo los costos ya ingresados seguían vivos en `lote.sqlite`.

        FASE 4: antes ese vaciado lo podía provocar OTRO lote (TRUNCATE del
        `staging_tuc` compartido). Ya no: solo lo provoca reprocesar este.

        Se corre UNA VEZ, antes de pintar las tarjetas, para que todo lo que se
        muestre después (incluida la línea de precio de referencia, que sale de
        la cascada en la base) ya vea los costos restaurados.

        Restaura solo cuando el producto es el mismo: la clave del espejo es
        `codigo_proveedor` + `catalogo_origen`, nunca el `candidato_id`, que es
        un serial efímero y se reasigna en cada recarga. Y solo cuando staging
        NO tiene costo: lo que haya en la base (una cotización cerrada, un costo
        del Excel) es más nuevo que el respaldo y no se pisa."""
        if not self._costos_lote:
            return
        for cand in self._candidatos:
            guardado = self._costos_lote.get(self._clave_costo(cand))
            if not isinstance(guardado, dict):
                continue
            try:
                costo = float(guardado.get("costo"))
            except (TypeError, ValueError):
                # costo respaldado no numerico: se salta ese candidato al restaurar.
                continue
            moneda = guardado.get("moneda")
            if costo <= 0 or not moneda_valida(moneda):
                continue
            try:
                if self._mc.obtener_costo_declarado(cand["candidato_id"])[0] is not None:
                    continue
                self._mc.fijar_costo_declarado(cand["candidato_id"], costo, moneda)
                precio = guardado.get("precio_venta")
                if precio:
                    self._mc.fijar_precio_manual(cand["candidato_id"], float(precio))
                self._costos_restaurados += 1
            except Exception:  # noqa: BLE001
                # El candidato ya no está en staging (o la base no lo acepta):
                # el respaldo local no se pierde por eso, se intentará de nuevo
                # la próxima vez que se abra el lote.
                continue

    def _persistir_costo(self, cand: dict, texto: str, estado: dict) -> str:
        """Núcleo de guardado de UN costo, sin interfaz: ni messagebox, ni
        reloj, ni recarga de pantalla. Devuelve "" si salió bien, o el motivo
        del fallo (texto para mostrarle al comprador).

        Se separó de `_guardar_costo_candidato` (2026-09-10) para que el botón
        único «💾 Guardar lote» pueda recorrer las 18 tarjetas del catálogo
        haciendo exactamente las MISMAS tres escrituras que hacía el botón por
        tarjeta -- `fijar_costo_declarado` + `fijar_precio_manual` + espejo en
        `lote.sqlite` -- sin 18 messageboxes ni 18 recargas. El volcado del
        espejo y la recarga los hace el llamador, una sola vez al final."""
        texto = (texto or "").strip().replace(",", "").replace("₡", "").replace("$", "")
        costo = None
        if texto:
            try:
                costo = float(texto)
            except ValueError:
                return "costo inválido"
            if costo <= 0:
                return "el costo tiene que ser mayor que cero"

        cfg = self._cfg_de(cand)   # FASE 3: la del proveedor de ESTE candidato
        moneda = cfg["moneda"] or estado.get("moneda_fila")
        if costo is not None and not moneda_valida(moneda):
            return ("falta declarar en qué moneda cotiza este proveedor "
                    "(botón «Cambiar» arriba)")

        desglose = (calcular_precio_venta(costo, moneda, cfg["tipo_cambio"], cfg["margen"])
                    if costo is not None else None)
        try:
            self._mc.fijar_costo_declarado(cand["candidato_id"], costo, moneda)
            # El precio derivado solo se fija cuando de verdad se pudo
            # calcular (dólares sin tipo de cambio: `calcular_precio_venta` se
            # abstiene, y escribir un precio a medias sería peor que ninguno).
            self._mc.fijar_precio_manual(
                cand["candidato_id"],
                desglose["precio_venta"] if desglose else None)
        except Exception as exc:  # noqa: BLE001
            return str(exc)

        clave = self._clave_costo(cand)
        if costo is None:
            self._costos_lote.pop(clave, None)
        else:
            self._costos_lote[clave] = {
                "codigo_proveedor": cand.get("codigo_proveedor"),
                "catalogo_origen": cand.get("catalogo_origen"),
                "costo": costo,
                "moneda": moneda,
                "tipo_cambio": cfg["tipo_cambio"],
                "margen": cfg["margen"],
                "precio_venta": desglose["precio_venta"] if desglose else None,
                "origen": "costo_lote",
                "guardado_en": datetime.now().isoformat(timespec="seconds"),
            }
        estado["costo"] = costo
        estado["moneda_fila"] = moneda
        return ""

    def _fila_candidato(self, contenedor, cand: dict) -> None:
        fila = Tarjeta(contenedor)
        fila.pack(fill="x", pady=4)

        # UNA SOLA FOTO POR COLOR EN LA TARJETA (2026-09-17, reclamo del dueño:
        # "si la referencia tiene 3 colores, solo quiero ver 3 fotos ...
        # actualmente estás repitiendo una foto" -- aclarado después: "no es
        # una foto repetida, es la misma foto que la ponés en un tamaño
        # ligeramente más grande a la izquierda").
        #
        # Qué pasaba: la foto grande de la izquierda es la del PRIMER color
        # (`_cargar_foto_candidato` lee `colores_vista[0]`), y la fila
        # «Colores de esta referencia» volvía a dibujar ESE MISMO color como
        # miniatura junto a los otros dos. Cuatro imágenes en pantalla para
        # tres colores reales, una de ellas dos veces.
        #
        # Por qué se resolvió sacando la foto grande en vez de sacar el primer
        # color de la fila: el proveedor vende por bulto, los tres colores se
        # compran juntos y ninguno es "el principal" -- darle a uno el lugar
        # grande y a los otros dos una miniatura mentía sobre lo que se compra.
        # Con la fila de colores como única fuente, los tres se ven del mismo
        # tamaño, cada uno con su color, su calificación y sus comparables.
        # Con un solo color no cambia nada: la foto grande de siempre.
        colores_vista = cand.get("colores_vista") or []
        multicolor = len(colores_vista) > 1
        hueco = etq_foto = None
        if not multicolor:
            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6,
                                 width=120, height=120)
            hueco.pack(side="left", padx=10, pady=8)
            hueco.pack_propagate(False)
            foto_tk = self._cargar_foto_candidato(cand)
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                etq_foto = tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0)
                etq_foto.pack(fill="both", expand=True)
            else:
                etq_foto = Etiqueta(hueco, text="sin foto", style="Suave.TLabel")
                etq_foto.pack(fill="both", expand=True)

        info = Marco(fila)
        info.pack(side="left", fill="both", expand=True, pady=10)
        texto_codigo = (codigo_visible(cand.get("codigo_proveedor"))
                        or f"Candidato {cand['candidato_id']}")
        if self._es_multiproveedor():
            # FASE 4: con dos catálogos mezclados en la misma pantalla, el
            # código del proveedor solo no dice de QUIÉN es la referencia (y
            # dos proveedores pueden usar códigos parecidos). La pastilla va
            # pegada al código, que es lo primero que se lee de la tarjeta.
            fila_codigo = Marco(info)
            fila_codigo.pack(anchor="w", fill="x")
            Etiqueta(fila_codigo, text=texto_codigo,
                     style="Subtitulo.TLabel").pack(side="left")
            self._pastilla_proveedor(fila_codigo, cand).pack(side="left", padx=(8, 0))
        else:
            # Un solo proveedor (caso mayoritario): exactamente el mismo
            # widget de siempre, sin contenedor ni pastilla de más.
            Etiqueta(info, text=texto_codigo, style="Subtitulo.TLabel").pack(anchor="w")

        self._fila_tipo_genero(info, cand)
        self._fila_marca(info, cand)
        # Qué color se muestra (2026-09-09): antes SOLO `color_principal`, que
        # es el color de la variante 0 y en la práctica es "negro" en la enorme
        # mayoría del calzado -- de ahí la queja de que "el color no se
        # detecta". La base ya guarda además `colores_detectados` (la
        # combinación real: "negro, gris", "blanco, azul") y `color_suela`, que
        # nunca se pintaban en esta pantalla. Ahora se muestra la combinación
        # completa, con la suela aparte porque es un dato de compra distinto.
        fila_color = Marco(info)
        fila_color.pack(anchor="w", fill="x", pady=(2, 0))
        Etiqueta(fila_color, text="Color:", style="Suave.TLabel").pack(side="left", padx=(0, 6))
        detectados = cand.get("colores_detectados") or cand.get("color_principal")
        _pastilla_color(fila_color, detectados).pack(side="left")
        suela = cand.get("color_suela")
        if suela:
            Etiqueta(fila_color, text="Suela:", style="Suave.TLabel").pack(side="left", padx=(10, 6))
            _pastilla_color(fila_color, suela).pack(side="left")

        if multicolor:
            self._fila_colores(info, cand, colores_vista)

        self._fila_precio(info, cand)
        self._bloque_precio_costo(info, cand)

        grado = cand.get("grado")
        score = cand.get("score_final")
        # REFERENCIA DE VARIOS COLORES (2026-09-17, pedido del dueño): el
        # proveedor genérico vende por bulto -- si la referencia trae 3
        # colores, se compran los 3 o ninguno. El número grande de la tarjeta
        # pasa a ser el PROMEDIO SIMPLE de los colores (cada color pesa igual),
        # que es la calificación de lo que realmente se compra. Con un solo
        # color `score_ponderado` viene en None y acá no cambia absolutamente
        # nada respecto de antes.
        score_ponderado = cand.get("score_ponderado")
        es_ponderado = score_ponderado is not None
        if es_ponderado:
            score = score_ponderado
            grado = cand.get("grado_ponderado") or grado
        if grado == "sin_comparables":
            texto_grado = "sin comparables"
        else:
            texto_grado = grado if grado else "sin calificar"
        if grado in ("S", "A"):
            fg, bg = PAR_OK, PAR_OK_BG
        elif grado in ("C", "D"):
            fg, bg = PAR_MAL, PAR_MAL_BG
        else:
            fg, bg = PAR_NEUTRO, PAR_PANEL
        marco_grado = Marco(fila)
        marco_grado.pack(side="right", padx=14)

        # El SCORE es ahora el número principal del badge (2026-09-09, pedido
        # del usuario), y en letra grande. `score_final` ya viene calculado
        # (`score_base` 0-100 × varios factores acotados, ver
        # `motor_calificacion.py`), así que NO se reescala nada: solo se
        # redondea a entero y se acota al 1 mínimo para que un candidato
        # calificado nunca muestre un "0" que parecería "sin calificar". La
        # letra S/A/B/C/D se conserva debajo, chiquita: sigue siendo el
        # lenguaje con el que se habla del corte de compra.
        #
        # CORREGIDO 2026-09-22: antes también se acotaba ARRIBA en 100 ("la
        # escala que se le prometió al comprador es 1-100"), asumiendo que la
        # fórmula rara vez pasaría de ~105. Medido contra lotes reales:
        # varios factores (f_demanda, f_venta, f_rotacion, f_descuento)
        # llegan a su tope a la vez con más frecuencia de la esperada, y el
        # score real de un lote entero puede ir de 157 a 192 -- con el techo
        # en 100, TODOS esos candidatos se veían idénticos ("100"), perdiendo
        # justo la diferencia que el comprador necesita para elegir la mejor
        # referencia. Mostrar el número real no cambia el orden ni la
        # fórmula, solo deja de esconder la diferencia real entre candidatos.
        if score is not None:
            numero = max(1, int(round(float(score))))
            etq_grado = tk.Label(marco_grado, text=str(numero), fg=solido(fg), bg=solido(bg),
                    font=("Segoe UI Semibold", 34), padx=18, pady=6, cursor="hand2")
            etq_grado.pack()
            leyenda = (f"promedio de {len(cand.get('colores_vista') or [])} colores  ·  grado {texto_grado}"
                       if es_ponderado else f"score  ·  grado {texto_grado}")
            Etiqueta(marco_grado, text=leyenda, style="Suave.TLabel").pack()
        else:
            etq_grado = tk.Label(marco_grado, text=texto_grado, fg=solido(fg), bg=solido(bg),
                    font=("Segoe UI Semibold", 18), padx=12, pady=6, cursor="hand2")
            etq_grado.pack()
        if grado not in (None, "sin_comparables"):
            # Plan 2026-09-07, paso 9 (ampliado): clic en el badge explica el
            # grado -- la defensa permanente contra "las calificaciones son
            # muy altas" es que CUALQUIER número se pueda auditar en un clic.
            etq_grado.bind("<Button-1>", lambda _e: self._mostrar_desglose_score(cand))
            Etiqueta(marco_grado, text="¿por qué? ↑", style="Suave.TLabel").pack()
        # Botón explícito para los comparables (2026-09-09, pedido del
        # usuario): antes la ÚNICA forma visible de abrirlos era hacer clic en
        # la letra del grado, que no se ve como algo cliqueable -- el clic en
        # la letra hoy abre el desglose del score, que es otra cosa. El clic en
        # el resto de la tarjeta sigue funcionando (no molesta a nadie), pero
        # este botón es la forma principal y la que se ve.
        btn_comparables = Boton(marco_grado, text="👁 Ver comparables", style="Primario.TButton")
        # `command` se asigna después de crear el botón porque el lambda tiene
        # que poder pasar EL BOTÓN como `disparador` (se deshabilita mientras
        # se arma el panel: ver `_abrir_comparables`).
        btn_comparables.configure(
            command=lambda: self._abrir_comparables(cand, btn_comparables))
        btn_comparables.pack(pady=(6, 0), fill="x")
        Boton(marco_grado, text="🗑 Eliminar", style="Sutil.TButton",
             command=lambda: self._confirmar_eliminar_candidato(cand)).pack(pady=(4, 0), fill="x")

        # Clic en cualquier parte de la tarjeta (menos los controles del selector
        # ambiguo, que ya tienen su propio manejo) abre los comparables.
        for w in (fila, info, hueco, etq_foto):
            if w is None:
                continue  # candidato multicolor: no hay foto grande a la izquierda
            w.bind("<Button-1>", lambda _e, c=cand: self._abrir_comparables(c))

        return fila

    SIN_MARCA = "(genérico — sin marca)"
    # Catálogos a los que ya se les corrió la autodetección de marca en ESTA
    # sesión: `_recargar` crea una instancia nueva de la pantalla en cada
    # guardado, y la detección no tiene por qué repetirse (es idempotente,
    # pero cuesta una consulta a Postgres y un barrido del lote).
    _MARCAS_AUTODETECTADAS: set = set()

    def _autodetectar_marcas(self) -> None:
        """Rellena la marca declarada de los candidatos cuyo TEXTO de catálogo
        nombre una marca reconocida (`autodetectar_marcas_declaradas`). No
        pisa nunca lo que declaró el comprador.

        En el lote Prueba2 esto detecta 0 marcas -- las fotos vinieron de un
        Excel y el "texto" de cada referencia es el nombre del archivo. Corre
        igual porque los catálogos con OCR/descripción sí se benefician, y
        porque el costo es una consulta."""
        clave = tuple(sorted({c.get("catalogo_origen") for c in self._candidatos}))
        if clave in self._MARCAS_AUTODETECTADAS:
            return
        self._MARCAS_AUTODETECTADAS.add(clave)
        try:
            self._mc.autodetectar_marcas_declaradas()
        except Exception:  # noqa: BLE001
            pass  # sin Postgres o sin la columna: la entrada manual sigue viva

    def _fila_marca(self, info, cand: dict) -> None:
        """«Marca (si aplica)» — Tarea 7, 2026-09-10.

        EL BUG QUE ARREGLA (parcialmente, ver más abajo): el usuario reportó
        que OSIRIS, una marca mundialmente reconocida, se estaba comparando
        contra el catálogo genérico de TU Calzado en vez de contra TC Marcas.
        Investigado: hasta hoy el sistema NO tenía ninguna forma de saber que
        una referencia era de marca -- en el lote real (Prueba2 / Cachos, 18
        referencias) `marca_declarada` está NULL en las 18 y no hay OCR del
        catálogo, así que no había ni dato declarado ni texto que mirar. Este
        control es la señal que faltaba, y la da el comprador.

        Es un desplegable ESCRIBIBLE, no cerrado: la lista gobernada
        (`silver.dim_marca`, 32 marcas) no incluye OSIRIS, así que limitar al
        comprador a esa lista no habría resuelto el caso reportado.

        Qué cambia al declararla, hoy: (1) la ventana de comparables pone la
        lista de MARCA RECONOCIDA primero y avisa que este candidato es de
        marca; (2) `_factor_mercado` (en el score) encuentra la señal real de
        importación de esa marca en `gold.vw_senal_marca`, que sin el dato
        quedaba en el factor neutro 1.0.

        Qué NO cambia (honesto): el score de similitud sigue calculándose
        contra el índice del catálogo TU Calzado. Ver la nota de la ventana de
        comparables."""
        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(2, 0))
        Etiqueta(marco, text="Marca (si aplica):", style="Suave.TLabel").pack(
            side="left", padx=(0, 4))

        actual = (cand.get("marca_declarada") or "").strip()
        opciones = [self.SIN_MARCA] + self._marcas_conocidas()
        if actual and actual not in opciones:
            opciones.insert(1, actual)  # una marca escrita a mano, ej. OSIRIS
        combo = ctk.CTkComboBox(marco, values=opciones, width=190)
        combo.set(actual or self.SIN_MARCA)
        combo.pack(side="left", padx=(0, 6))
        Boton(marco, text="✓", style="Sutil.TButton", width=28,
              command=lambda: self._confirmar_marca(cand, combo.get())).pack(side="left")
        if actual:
            tk.Label(marco, text="marca reconocida", fg=solido(PAR_OK),
                    bg=solido(PAR_OK_BG), font=("Segoe UI Semibold", 10),
                    padx=6).pack(side="left", padx=(8, 0))

    def _marcas_conocidas(self) -> list[str]:
        """La lista gobernada, leída UNA vez por pantalla (son 32 filas de
        Postgres y hay una tarjeta por candidato: 18 consultas idénticas por
        pintada sería absurdo)."""
        if getattr(self, "_cache_marcas", None) is None:
            try:
                self._cache_marcas = self._mc.marcas_reconocidas()
            except Exception:  # noqa: BLE001
                _logger_gui().exception("no se pudieron consultar las marcas reconocidas")
                self._cache_marcas = []
        return self._cache_marcas

    def _confirmar_marca(self, cand: dict, texto: str) -> None:
        marca = None if (texto or "").strip() in ("", self.SIN_MARCA) else texto.strip()
        with reloj(self):
            try:
                self._mc.fijar_marca_declarada(cand["candidato_id"], marca)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo guardar la marca:\n{exc}")
                return
        self._recargar()

    def _fila_tipo_genero(self, info, cand: dict) -> None:
        """Tipo (categoría de calzado) y Género, SIEMPRE editables a mano.

        Antes (`_fila_ambigua`, reemplazada por esto) los desplegables solo
        aparecían cuando el nearest-centroid había quedado ambiguo, o sea
        cuando el dato faltaba: si la máquina se equivocaba con confianza --el
        caso que más molesta-- la tarjeta mostraba el valor mal en texto plano
        y no había forma de corregirlo desde la app. Ahora los dos son
        desplegables siempre, con el valor ya calculado como default, y
        cambiarlos GUARDA de una (UPDATE a `staging_tuc.dim_candidato`:
        `categoria_declarada` y `genero_canonico`, vía
        `api_asignar_categoria`, que además vuelve a puntuar el candidato
        porque el score depende del filtro categoria+genero).

        Se conserva el aviso "⚠ ambiguo" cuando el valor venía vacío: sigue
        siendo información útil (la máquina no supo), solo que ya no es lo que
        decide si se puede editar o no."""
        categoria = cand.get("categoria_declarada")
        genero = cand.get("genero_declarado")

        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(2, 0))

        if not categoria or not genero:
            tk.Label(marco, text="⚠ ambiguo", fg=solido(PAR_ALERTA), bg=solido(PAR_PANEL),
                    font=("Segoe UI Semibold", 11)).pack(side="left", padx=(0, 8))

        # Los valores calculados se agregan al listado si no estuvieran ya (la
        # base puede tener una categoría vieja que ya no está en la constante):
        # sin esto, `set()` de un valor ausente deja el combo en blanco y
        # parecería que el dato se perdió.
        cats = list(CATEGORIAS_CONOCIDAS)
        if categoria and categoria not in cats:
            cats.append(categoria)
        gens = list(GENEROS_CONOCIDOS)
        if genero and genero not in gens:
            gens.append(genero)

        Etiqueta(marco, text="Tipo:", style="Suave.TLabel").pack(side="left", padx=(0, 4))
        combo_cat = ctk.CTkComboBox(marco, values=cats, width=160, state="readonly")
        combo_cat.set(categoria or "")
        combo_cat.pack(side="left", padx=(0, 12))

        Etiqueta(marco, text="Género:", style="Suave.TLabel").pack(side="left", padx=(0, 4))
        combo_gen = ctk.CTkComboBox(marco, values=gens, width=120, state="readonly")
        combo_gen.set(genero or "")
        combo_gen.pack(side="left")

        # Botón explícito de refresco (2026-09-09, pedido del usuario).
        # Qué pasaba ANTES: cambiar tipo, género o costo YA refrescaba solo
        # --los tres guardan y llaman `_recargar()`, que vuelve a pedir la
        # lista y reconstruye la pantalla-- pero era invisible: la pantalla
        # parpadeaba y nada decía que el score se había recalculado, así que
        # parecía que el cambio no había tenido efecto. El refresco sigue
        # siendo automático (no se puede perder un guardado por olvidarse de
        # apretar nada); este botón le da al comprador la forma MANIFIESTA de
        # volver a calificar y reordenar la lista cuando quiera.
        Boton(marco, text="🔄 Actualizar", style="Sutil.TButton", width=104,
              command=self._recargar).pack(side="left", padx=(12, 0))

        # El `command` se conecta DESPUÉS del `set()` inicial: CTkComboBox
        # dispara el callback en `set()`, y conectarlo antes provocaría un
        # guardado (y un recargado de pantalla entero) por cada tarjeta al
        # abrir el paso 6.
        combo_cat.configure(command=lambda valor: self._guardar_tipo_genero(
            cand, categoria=valor, actual_categoria=categoria))
        combo_gen.configure(command=lambda valor: self._guardar_tipo_genero(
            cand, genero=valor, actual_genero=genero))

    def _guardar_tipo_genero(self, cand: dict, categoria: str | None = None,
                             genero: str | None = None,
                             actual_categoria: str | None = None,
                             actual_genero: str | None = None) -> None:
        """Guarda la corrección manual de tipo/género de UN candidato.

        No hace nada si el valor elegido es el que ya estaba: así, abrir los
        desplegables para mirar y cerrarlos sin cambiar no dispara un
        re-scoring ni un recargado de la pantalla."""
        categoria = (categoria or "").strip() or None
        genero = (genero or "").strip() or None
        if categoria is not None and categoria == actual_categoria:
            return
        if genero is not None and genero == actual_genero:
            return
        if categoria is None and genero is None:
            return
        try:
            self._mc.asignar_categoria(cand["candidato_id"], categoria=categoria, genero=genero)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo guardar la corrección:\n{exc}")
            return
        # …y en el espejo durable del lote, para que la corrección sobreviva a
        # que se vacíe la revisión de este lote (ver `leer_categorias_lote`).
        self._anotar_categoria_lote(cand, categoria=categoria, genero=genero)
        self._recargar()

    # ---------- tipo/género por candidato: base efímera + espejo local ------

    def _anotar_categoria_lote(self, cand: dict, categoria: str | None = None,
                               genero: str | None = None) -> None:
        """Suma la corrección al espejo local y lo vuelca completo. Se anota
        solo el campo que el comprador cambió: corregir el género no debe
        borrar del respaldo un tipo corregido antes."""
        clave = self._clave_costo(cand)  # misma clave que los costos, a propósito
        fila = dict(self._categorias_lote.get(clave) or {})
        fila.update({"codigo_proveedor": cand.get("codigo_proveedor"),
                     "catalogo_origen": cand.get("catalogo_origen"),
                     "guardado_en": datetime.now().isoformat(timespec="seconds")})
        if categoria:
            fila["categoria"] = categoria
        if genero:
            fila["genero"] = genero
        self._categorias_lote[clave] = fila
        guardar = getattr(self.master, "guardar_categorias_lote", None)
        if callable(guardar):
            try:
                guardar(self._categorias_lote)
            except Exception:  # noqa: BLE001
                pass  # el respaldo no puede ser lo que rompa la pantalla

    def _restaurar_categorias_del_respaldo(self) -> None:
        """Vuelve a aplicar en la revisión los tipos/géneros que el comprador
        ya había corregido en este lote -- el caso para el que existe el espejo:
        la revisión de este lote se vació (reprocesarlo desde cero) y al
        retomarlo la corrección hecha a mano seguía viva en `lote.sqlite`.

        FASE 4: antes ese vaciado lo podía provocar OTRO lote (TRUNCATE del
        `staging_tuc` compartido). Ya no: solo lo provoca reprocesar este.

        Solo escribe cuando el valor en la base DIFIERE del respaldo: si la
        máquina ya llegó al mismo resultado, no hay nada que corregir (y se
        evita un re-scoring inútil por candidato)."""
        if not self._categorias_lote:
            return
        for cand in self._candidatos:
            guardado = self._categorias_lote.get(self._clave_costo(cand))
            if not isinstance(guardado, dict):
                continue
            categoria = guardado.get("categoria") or None
            genero = guardado.get("genero") or None
            if categoria == cand.get("categoria_declarada"):
                categoria = None
            if genero == cand.get("genero_declarado"):
                genero = None
            if categoria is None and genero is None:
                continue
            try:
                self._mc.asignar_categoria(cand["candidato_id"],
                                           categoria=categoria, genero=genero)
            except Exception:  # noqa: BLE001
                # El candidato ya no está en staging (o la base no lo acepta):
                # el respaldo local no se pierde por eso.
                continue
            # La lista en memoria se pinta ANTES de la recarga, así que se
            # actualiza también acá para que la tarjeta no muestre el valor
            # viejo por un instante.
            if categoria:
                cand["categoria_declarada"] = categoria
            if genero:
                cand["genero_declarado"] = genero
            self._categorias_restauradas += 1

    def _fila_colores(self, info, cand: dict, colores_vista: list[dict]) -> None:
        """Una tarjetita CON FOTO por color, cada una con su propio 🗑 -- plan
        2026-09-07, paso 7 (eliminar variante). Solo se muestra cuando el
        candidato trae más de un color (`colores_vista` viene de
        `api_candidatos`, ya filtrado a variantes con foto limpia real).

        Antes esto era una pastilla de color (un cuadradito de color sólido,
        no la foto real) -- el comprador pidió ver la FOTO de cada color, para
        reconocer el color real del producto en vez de adivinar por el nombre
        ("naranja" puede ser cualquier tono). Termina con la calificación
        ponderada del bulto completo (2026-09-18, pedido explícito: "cada
        color con su calificación y la calificación ponderada al final")."""
        marco = Marco(info)
        marco.pack(anchor="w", fill="x", pady=(4, 0))
        Etiqueta(marco, text="Colores de esta referencia:",
                 style="Suave.TLabel").pack(anchor="w")
        fila_fotos = Marco(marco)
        fila_fotos.pack(anchor="w", fill="x", pady=(4, 0))
        for col in colores_vista:
            url = col.get("url", "")
            if not url.startswith("/crop_variante/"):
                continue
            try:
                variante_id = int(url.rsplit("/", 1)[-1])
            except ValueError:
                # URL de variante sin id numerico al final: se salta ese chip de color.
                continue
            chip = Marco(fila_fotos)
            chip.pack(side="left", padx=(0, 10))

            # Foto real del color, no una pastilla de color sólido. 104px
            # (antes 56): desde que la tarjeta ya NO dibuja la foto grande de
            # la izquierda para referencias de varios colores (ver
            # `_fila_candidato`), esta fila es la ÚNICA foto del candidato y
            # tiene que poder mirarse, no solo confirmarse.
            ruta_col = self._mc.ruta_foto_variante(variante_id)
            foto_col = _cargar_foto_generico(ruta_col, tam=(104, 104))
            # `tk.Label` es tk clásico: solo acepta UN color, no el par
            # (claro, oscuro) de `PAR_PANEL_SUAVE` -- eso tiraba
            # `TclError: invalid color name "#eff2f6 #2c313a"` y rompía toda
            # la fila de colores en silencio (bug real, no cosmético).
            lbl_foto = tk.Label(chip, bd=0, bg=solido(PAR_PANEL_SUAVE))
            if foto_col is not None:
                lbl_foto.configure(image=foto_col, cursor="hand2")
                lbl_foto.image = foto_col  # referencia viva: sin esto Tk la recolecta
                # Clic en la miniatura = la misma foto en grande (2026-09-17,
                # pedido del dueño). Mismo patrón que el visor de comparables
                # (`_ver_foto_grande`), `_traer_al_frente` incluido: sin eso la
                # ventana nace DETRÁS y parece que el clic no hizo nada.
                lbl_foto.bind("<Button-1>", lambda _e, r=ruta_col,
                              t=f"{codigo_visible(cand.get('codigo_proveedor'))} — "
                                f"{col.get('color_principal') or 'color'}":
                              self._ver_foto_color(r, t))
            else:
                lbl_foto.configure(text="sin foto", width=13, height=6)
            lbl_foto.pack()

            fila_pastilla = Marco(chip)
            fila_pastilla.pack(pady=(2, 0))
            _pastilla_color(fila_pastilla, col.get("color_principal")).pack(side="left")
            Boton(fila_pastilla, text="🗑", style="Sutil.TButton", width=22,
                 command=lambda vid=variante_id: self._confirmar_eliminar_variante(cand, vid)
                 ).pack(side="left", padx=(2, 0))

            # La calificación de ESTE color (2026-09-17). El bulto no se puede
            # partir, así que el comprador tiene que ver si está pagando un
            # color bueno y dos malos -- antes solo se calificaba el principal.
            # Si un color todavía no tiene número se dice POR QUÉ, nunca se
            # muestra un 0 ni se lo esconde.
            score_col = col.get("score_final")
            if score_col is not None:
                grado_col = col.get("grado") or "—"
                Etiqueta(chip, text=f"{max(1, int(round(float(score_col))))} ({grado_col})",
                         style="Suave.TLabel").pack(pady=(2, 0))
            else:
                falta = col.get("grado")
                texto_falta = ("falta vectorizar" if falta == "sin_vector" else
                               "sin comparables" if falta == "sin_comparables" else
                               "sin calificar")
                Etiqueta(chip, text=texto_falta, style="Suave.TLabel").pack(pady=(2, 0))

            # COMPARABLES DE ESTE COLOR (2026-09-17, pedido del dueño: "la
            # opción de ver comparables debería ser para cada variación de
            # color. Cada variación de color tiene sus propios comparables").
            # El botón general de la derecha abría el panel SIEMPRE parado en
            # el primer color, y para ver los otros dos había que descubrir el
            # combo «Color:» de adentro. La búsqueda por color ya existía y ya
            # usa el embedding de ESE color -- lo que faltaba era el acceso.
            # Ancho fijo = ancho de la miniatura: con `fill="x"` el botón era
            # el widget más ancho del chip y estiraba la columna entera, así
            # que las fotos quedaban separadas por aire muerto.
            btn_col = Boton(chip, text="👁 comparables", style="Sutil.TButton", width=104)
            btn_col.configure(command=lambda i=col.get("indice"), b=btn_col:
                              self._abrir_comparables(cand, b, indice=i))
            btn_col.pack(pady=(2, 0))

        # Calificación ponderada AL FINAL de la fila (además del número grande
        # de la tarjeta, que ya la muestra como score principal) -- pedido
        # explícito: que se lea de corrido "color, color, color → ponderado".
        score_pond = cand.get("score_ponderado")
        if score_pond is not None:
            # `Separador` es horizontal (1px de alto, pensado para `fill="x"`);
            # acá hace falta una línea VERTICAL, así que el frame angosto se
            # arma directo en vez de reusarlo mal.
            tk.Frame(fila_fotos, width=1, bg=COLOR_BORDE_FUERTE
                    ).pack(side="left", padx=(4, 10), fill="y", pady=4)
            final = Marco(fila_fotos)
            final.pack(side="left")
            Etiqueta(final, text="Ponderado", style="Suave.TLabel").pack()
            grado_pond = cand.get("grado_ponderado") or "—"
            Etiqueta(final, text=f"{max(1, int(round(float(score_pond))))} ({grado_pond})",
                     style="Subtitulo.TLabel").pack()

    def _ver_foto_color(self, path, titulo: str) -> None:
        """La foto de UN color de la referencia, en grande, en su propia
        ventana (2026-09-17, pedido del dueño: "cuando le doy clic pueda ver la
        foto de miniatura en tamaño más grande").

        Es el mismo patrón que `PanelComparablesCTk._ver_foto_grande` y que
        `_mostrar_imagen_flotante` del paso 5, `_traer_al_frente` incluido:
        sin eso la ventana nace DETRÁS de la principal (Windows le devuelve el
        foco al widget que se clicó) y el comprador cree que el clic no hizo
        nada."""
        if not path:
            return
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return
        ventana = ctk.CTkToplevel(self)
        ventana.title(titulo)
        ventana.configure(fg_color=PAR_VISOR_BG)
        mostrar = im.copy()
        mostrar.thumbnail((max(self.winfo_screenwidth() - 260, 300),
                           max(self.winfo_screenheight() - 260, 300)))
        foto = ImageTk.PhotoImage(mostrar)
        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta
        lbl.pack(padx=10, pady=(10, 4))
        Etiqueta(ventana, text=f"{titulo}  ·  {im.width} × {im.height} px",
                 style="Suave.TLabel").pack(pady=(0, 10))
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    def _confirmar_eliminar_variante(self, cand: dict, variante_id: int) -> None:
        if not messagebox.askyesno("Asistente de Compras",
                "¿Quitar este color de la referencia? El resto del candidato se conserva."):
            return
        with reloj(self):
            try:
                self._mc.eliminar_variante(variante_id)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo quitar el color:\n{exc}")
                return
        self._recargar()

    def _confirmar_eliminar_candidato(self, cand: dict) -> None:
        codigo = codigo_visible(cand.get("codigo_proveedor")) or f"Candidato {cand['candidato_id']}"
        if not messagebox.askyesno("Asistente de Compras",
                f"¿Eliminar por completo la referencia {codigo}? No se puede deshacer, y si "
                "reenviás este mismo catálogo no va a volver a aparecer."):
            return
        with reloj(self):
            try:
                self._mc.eliminar_candidato(cand["candidato_id"])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras", f"No se pudo eliminar:\n{exc}")
                return
        self._recargar()

    def _fila_precio(self, info, cand: dict) -> None:
        """Precio de venta esperado + su fuente (plan 2026-09-07, paso 6) --
        mismo patrón que `_fila_ambigua`: se muestra editable inline, con un
        botón ✓ para fijarlo a mano. Fijar un precio manual gana siempre
        sobre la cascada automática (`precio_venta_referencia`) y se refleja
        de inmediato en el Optimizador de Compra."""
        precio, fuente = self._mc.obtener_precio_referencia(cand["candidato_id"])
        marco = Marco(info)
        marco.pack(anchor="w", fill="x")

        # Renombrada (2026-09-09, pedido del usuario): decía "Precio venta" a
        # secas y se confundía con el precio sugerido por la fórmula de costo,
        # que es OTRA cosa y está justo debajo (`_bloque_precio_costo`). Este
        # número sale de lo que ya se vende en el catálogo propio / de los
        # comparables encontrados, no del costo que cotizó este proveedor.
        # …y ahora el rótulo lo decide la FUENTE real del número: si salió del
        # costo que el comprador ingresó para esta referencia, dice "estimado
        # por costo y margen real"; si salió de los comparables internos, lo
        # dice así. Las dos etiquetas nunca aparecen como si fueran lo mismo
        # (ver `texto_precio_referencia`).
        texto = texto_precio_referencia(precio, fuente,
                                        por_costo_lote=self._precio_vino_del_costo(cand))
        Etiqueta(marco, text=texto, style="Suave.TLabel", wraplength=560,
                 justify="left").pack(side="left")

        # 2026-09-10: acá vivía un cuadro "Fijar a mano ₡:" con su ✓ y su
        # Quitar. Se eliminó por pedido del usuario. El precio de venta ya no
        # se teclea a mano en esta tarjeta: se DERIVA del costo del proveedor
        # (`_bloque_precio_costo`, justo abajo), que es la decisión real que
        # toma el comprador -- y ese bloque ya escribe `precio_venta_manual`
        # por la fórmula del lote, así que no se pierde ninguna capacidad.
        # Este renglón queda como lo que siempre debió ser: informativo.

    def _confirmar_ambiguo(self, cand: dict, categoria: str | None = None, genero: str | None = None) -> None:
        if not categoria and not genero:
            return
        with reloj(self):
            try:
                self._mc.asignar_categoria(cand["candidato_id"], categoria=categoria or None,
                                           genero=genero or None)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras", f"No se pudo confirmar:\n{exc}")
                return
        self._recargar()

    def _recargar(self) -> None:
        """Vuelve a pedir los candidatos (ya con la confirmación aplicada) y
        reconstruye la pantalla entera -- más simple y robusto que parchear
        in situ las tarjetas afectadas. Se delega en la ventana principal
        (que es la dueña del pack/pack_forget de las pantallas) cuando está
        embebida; si se construyó suelta (pruebas), se reconstruye sola en el
        mismo master."""
        master = self.master
        # Embebida en la ventana principal, se le pide a ELLA la lista: es la
        # que sabe de qué proveedor y de qué catálogo es el lote abierto (sin
        # eso, recargar la pantalla traía de vuelta los candidatos de todos
        # los proveedores que hubiera en staging).
        del_lote = getattr(master, "_candidatos_del_lote", None)
        candidatos = del_lote() if callable(del_lote) else self._mc.obtener_candidatos()
        anfitrion = getattr(master, "_mostrar_vista_candidatos", None)
        if callable(anfitrion):
            anfitrion(candidatos)
            return
        info_pack = self.pack_info() if self.winfo_manager() == "pack" else None
        al_volver = self._al_volver
        self.destroy()
        nueva = VentanaCandidatosCTk(master, candidatos, al_volver=al_volver)
        nueva.pack(**(info_pack or {"fill": "both", "expand": True}))

    def _cargar_foto_candidato(self, cand: dict) -> ImageTk.PhotoImage | None:
        colores = cand.get("colores_vista") or []
        if not colores:
            return None
        primero = colores[0]
        url = primero.get("url", "")
        if not url.startswith("/crop_variante/"):
            return None
        try:
            variante_id = int(url.rsplit("/", 1)[-1])
        except ValueError:
            # URL de variante sin id numerico al final: la tarjeta va sin foto.
            return None
        # 114px para llenar el hueco de 120 de la tarjeta (antes 90 en un
        # hueco de 96): la foto es lo que el comprador mira primero.
        return _cargar_foto_generico(self._mc.ruta_foto_variante(variante_id),
                                     tam=(114, 114))

    # ---------- comparables ----------

    def _cerrar_comparables(self) -> None:
        """Cierra la ventana de comparables si está abierta. Es el `al_cerrar`
        del panel (la ✕ de adentro) y también lo que se llama antes de abrir
        otro candidato: nunca hay dos ventanas de comparables apiladas."""
        self._limpiar_panel_comparables()

    # Nombre histórico: la vista vacía del panel lateral ya no existe (los
    # comparables viven en su propia ventana), pero varios lugares llamaban a
    # esto como "volver al estado sin candidato seleccionado", que hoy es
    # simplemente cerrar la ventana.
    _mostrar_marcador_comparables = _cerrar_comparables

    def _limpiar_panel_comparables(self) -> None:
        self._panel_comparables_contenido = None
        win = getattr(self, "_win_comparables", None)
        if win is not None:
            self._win_comparables = None
            try:
                win.destroy()
            except tk.TclError:
                # ventana de comparables ya cerrada por el usuario: nada que destruir.
                pass

    def _mostrar_desglose_score(self, cand: dict) -> None:
        """Diálogo simple (no forma parte de la navegación principal, es una
        consulta puntual) con el desglose completo del score: la frase
        reconstruible a mano + los vecinos reales, con foto y unidades
        vendidas -- plan 2026-09-07, paso 9.

        Mismo cierre contra el doble clic que `_abrir_comparables`: la letra
        del grado también dispara una consulta lenta a la base."""
        if getattr(self, "_abriendo_desglose", False):
            return
        self._abriendo_desglose = True
        try:
            self._mostrar_desglose_score_real(cand)
        finally:
            self._abriendo_desglose = False

    def _mostrar_desglose_score_real(self, cand: dict) -> None:
        with reloj(self):
            try:
                d = self._mc.obtener_desglose_score(cand["candidato_id"])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo calcular el desglose:\n{exc}")
                return
        if d is None:
            messagebox.showinfo("Asistente de Compras", "Este candidato no tiene score todavía.")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(f"¿Por qué {d['clasificacion']}? — "
                      f"{codigo_visible(cand.get('codigo_proveedor')) or cand['candidato_id']}")
        _centrar_en_ventana_principal(self, ventana, 620, 520)
        Etiqueta(ventana, text=d["frase"], style="Suave.TLabel", justify="left",
                wraplength=580).pack(anchor="w", padx=16, pady=(14, 4), fill="x")
        Etiqueta(ventana,
                text=f"filtro: {d['filtro_aplicado'] or '—'}  ·  k_efectivo: {d['k_efectivo']:.2f}  ·  "
                     f"fórmula {d['version_formula'] or '—'}",
                style="Suave.TLabel").pack(anchor="w", padx=16, pady=(0, 10))

        Etiqueta(ventana, text="Similares que produjeron este score (ordenados por peso):",
                style="Subtitulo.TLabel").pack(anchor="w", padx=16, pady=(0, 6))

        contenedor = ctk.CTkScrollableFrame(ventana, fg_color="transparent")
        contenedor.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        fotos_tk: list[ImageTk.PhotoImage] = []
        for v in d["vecinos"]:
            fila = Tarjeta(contenedor)
            fila.pack(fill="x", pady=3)
            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6, width=56, height=56)
            hueco.pack(side="left", padx=8, pady=8)
            hueco.pack_propagate(False)
            foto_tk = _cargar_foto_generico(
                self._mc.ruta_foto_imagen(v["imagen_id"]) if v.get("imagen_id") else None, tam=(52, 52))
            if foto_tk is not None:
                fotos_tk.append(foto_tk)
                tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0).pack(fill="both", expand=True)
            else:
                Etiqueta(hueco, text="—", style="Suave.TLabel").pack(fill="both", expand=True)
            info = Marco(fila)
            info.pack(side="left", fill="both", expand=True, pady=8)
            Etiqueta(info, text=v["codigo_tuc"], style="Suave.TLabel").pack(anchor="w")
            Etiqueta(info, text=f"similitud {v['similitud']:.3f}  ·  peso {v['peso']:.3f}  ·  "
                     f"{v['unidades_vendidas']} vendidas  ·  método: {v['metodo']}",
                    style="Suave.TLabel").pack(anchor="w")
        ventana._fotos_tk = fotos_tk  # referencias vivas, evitar garbage collection
        _traer_al_frente(ventana)  # mismo problema que los comparables: nacía detrás

    def _abrir_comparables(self, cand: dict, disparador=None, indice: int | None = None) -> None:
        """Abre los comparables del candidato en su PROPIA ventana,
        redimensionable (2026-09-09). Se reutiliza una sola ventana: hacer clic
        en otro candidato reemplaza el contenido en vez de apilar ventanas.

        Armar el panel consulta el índice permanente de similitud y carga las
        fotos: tarda, y sin protección un segundo clic impaciente disparaba una
        SEGUNDA consulta en paralelo (2026-09-10, pedido del usuario). Dos
        cierres: `self._abriendo_comparables` corta cualquier reentrada (el
        botón, la letra del grado, el clic en la tarjeta -- todos llegan acá), y
        `disparador` se deshabilita mientras dura, para que además se VEA que
        el clic fue tomado. Los dos se levantan en el `finally`."""
        if getattr(self, "_abriendo_comparables", False):
            return
        self._abriendo_comparables = True
        try:
            self._abrir_comparables_real(cand, disparador, indice)
        finally:
            self._abriendo_comparables = False
            if disparador is not None:
                try:
                    disparador.configure(state="normal")
                except (tk.TclError, ValueError):
                    # boton disparador ya destruido: no hay nada que rehabilitar.
                    pass

    def _abrir_comparables_real(self, cand: dict, disparador=None,
                                indice: int | None = None) -> None:
        if disparador is not None:
            try:
                disparador.configure(state="disabled")
                disparador.update_idletasks()
            except (tk.TclError, ValueError):
                # boton disparador ya destruido: no hay nada que deshabilitar.
                pass
        self._limpiar_panel_comparables()

        win = ctk.CTkToplevel(self)
        self._win_comparables = win
        codigo = codigo_visible(cand.get("codigo_proveedor")) or cand["candidato_id"]
        win.title(f"Comparables — {codigo}")
        # Generosa a propósito: acá entran DOS listas de comparables con foto
        # (marca reconocida y TUCALZADO vendido) más el cuadro de cotización.
        _centrar_en_ventana_principal(self, win, 1040, 800)
        win.minsize(720, 520)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._cerrar_comparables)

        # Construir el panel consulta similitud contra el índice permanente y
        # carga las fotos de los comparables: es la operación más lenta de esta
        # pantalla, y sin reloj de arena parecía que el clic no había hecho nada.
        with reloj(self):
            panel = PanelComparablesCTk(win, self._mc, cand,
                                        al_cerrar=self._cerrar_comparables,
                                        precio_por_costo=self._precio_vino_del_costo(cand),
                                        indice_inicial=indice)
            panel.pack(fill="both", expand=True)
        self._panel_comparables_contenido = panel
        # Al frente sin robarle el foco de forma permanente a la app (un
        # `grab_set` acá impediría seguir usando la lista de candidatos, que es
        # justo lo que el comprador quiere hacer con la ventana abierta).
        #
        # `lift()` solo NO alcanzaba (2026-09-09, reclamo del usuario: "queda
        # detrás y hay que buscarla en la barra de tareas"): en Windows, una
        # ventana nueva del mismo proceso no se pone encima de la que tiene el
        # foco solo por hacer `lift`. `_traer_al_frente` usa el patrón
        # `-topmost` ON/OFF, que la trae al frente UNA vez sin dejarla pegada
        # encima para siempre.
        _traer_al_frente(win)

    # ---------- pestaña Optimizador de Compra (PDV objetivo → pedido sugerido) ----------

    def _rango_pdv_estimado(self) -> tuple[float, float, int, int]:
        """Rango orientativo de PDV que se puede armar con el catálogo que hay
        cargado ahora mismo (plan 2026-09-07, paso 8, punto 6).

        Es una ESTIMACIÓN, no una promesa: mínimo = un empaque de cada
        candidato grado S/A con precio de venta calculable; máximo = un
        empaque de TODOS los candidatos con precio calculable. El empaque
        ausente cuenta como 1 par, igual que hace `api_pedido_sugerido`.
        Devuelve (minimo, maximo, n_min, n_max)."""
        minimo = maximo = 0.0
        n_min = n_max = 0
        for cand in self._candidatos:
            try:
                precio, _fuente = self._mc.obtener_precio_referencia(cand["candidato_id"])
            except Exception:  # noqa: BLE001
                _logger_gui().exception("no se pudo leer el precio de referencia del candidato %s", cand["candidato_id"])
                continue
            if not precio:
                continue
            empaque = cand.get("empaque_cantidad") or 1
            monto = float(precio) * int(empaque)
            maximo += monto
            n_max += 1
            if cand.get("grado") in ("S", "A"):
                minimo += monto
                n_min += 1
        return minimo, maximo, n_min, n_max

    def _construir_tab_optimizador(self, tab) -> None:
        # Antes del campo de PDV objetivo: con qué rango de pedido se puede
        # trabajar con este catálogo. Sin esto, el comprador tecleaba una
        # cifra a ciegas y descubría después que no alcanzaba (o que sobraba).
        minimo, maximo, n_min, n_max = self._rango_pdv_estimado()
        if n_max:
            texto_rango = (f"Con este catálogo podés armar un pedido de entre "
                           f"₡{minimo:,.0f} ({n_min} referencia(s) grado S/A) y "
                           f"₡{maximo:,.0f} ({n_max} referencia(s) con precio calculable), "
                           f"contando un empaque de cada una. Es una estimación orientativa.")
        else:
            texto_rango = ("Todavía ninguna referencia de este catálogo tiene precio de "
                           "venta calculable, así que no se puede estimar un rango de pedido.")
        Etiqueta(tab, text=texto_rango, style="Suave.TLabel", wraplength=880,
                 justify="left").pack(anchor="w", padx=4, pady=(10, 0))

        # Con qué mes de venta se armó este sugerido (2026-09-09). El mes se
        # pregunta al entrar al paso 6 (`asegurar_mes_venta_lote`) y queda en
        # los metadatos del lote; acá se muestra para que el número del pedido
        # nunca quede sin la temporada que lo justifica, y se puede corregir
        # sin salir del paso.
        self._fila_mes_venta = Marco(tab)
        self._fila_mes_venta.pack(fill="x", padx=4, pady=(6, 0))
        self._etq_mes_venta = Etiqueta(self._fila_mes_venta, text="", style="Suave.TLabel")
        self._etq_mes_venta.pack(side="left")
        Boton(self._fila_mes_venta, text="Cambiar", style="Sutil.TButton", width=64,
              command=self._cambiar_mes_venta).pack(side="left", padx=(8, 0))
        self._pintar_mes_venta()

        # Bloque de CIERRE del proceso (2026-09-09): el paso 7 es el final del
        # asistente, pero se veía igual que cualquier pantalla del medio. Se
        # llena al generar la selección (antes no hay números que celebrar) y
        # queda arriba, donde primero cae la vista.
        self._marco_cierre = Marco(tab)
        self._marco_cierre.pack(fill="x", padx=4, pady=(8, 0))

        controles = Marco(tab)
        controles.pack(fill="x", padx=4, pady=(10, 6))
        Etiqueta(controles, text="PDV Objetivo (₡)", style="Suave.TLabel").pack(side="left")
        self._var_pdv = tk.StringVar()
        entrada = ctk.CTkEntry(controles, textvariable=self._var_pdv, width=140,
                               placeholder_text="ej. 500000")
        entrada.pack(side="left", padx=(8, 8))
        Boton(controles, text="Generar selección", style="Primario.TButton",
             command=self._generar_pedido).pack(side="left")
        # Exportar solo tiene sentido con una selección ya generada: nace
        # apagado y lo enciende `_generar_pedido`.
        self._btn_exportar_excel = Boton(controles, text="📊 Exportar a Excel",
                                         command=self._exportar_sugerido_excel)
        self._btn_exportar_excel.pack(side="left", padx=(8, 0))
        self._btn_exportar_excel.state(["disabled"])
        self._ultimo_sugerido: list[dict] = []

        self._resumen_optimizador = Marco(tab)
        self._resumen_optimizador.pack(fill="x", padx=4, pady=(0, 8))

        self._tabla_optimizador = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self._tabla_optimizador.pack(fill="both", expand=True)

    def _mes_venta(self) -> int | None:
        leer = getattr(self.master, "mes_venta_lote", None)
        return leer() if callable(leer) else None

    def _pintar_mes_venta(self) -> None:
        mes = self._mes_venta()
        nombres = getattr(self.master, "MESES_ES", None)
        nombre = nombres[mes - 1] if (mes and nombres) else None
        self._etq_mes_venta.configure(
            text=(f"Mes de venta esperado de este pedido: {nombre} — las categorías "
                  "de temporada pesan más (o menos) en el reparto."
                  if nombre else
                  "Mes de venta esperado de este pedido: sin declarar (reparto sin "
                  "ajuste de temporada)."))

    def _cambiar_mes_venta(self) -> None:
        preguntar = getattr(self.master, "asegurar_mes_venta_lote", None)
        if callable(preguntar):
            preguntar(forzar=True)
        self._pintar_mes_venta()

    def _generar_pedido(self) -> None:
        try:
            pdv_objetivo = float(self._var_pdv.get())
            if pdv_objetivo <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Asistente de Compras", "Ingresá un PDV objetivo válido.")
            return

        with reloj(self):
            try:
                # El mes de venta esperado (paso 6) ajusta el peso de cada
                # candidato por la estacionalidad real de su categoría; sin mes
                # declarado el reparto es el de siempre (score puro).
                r = self._mc.obtener_pedido_sugerido(pdv_objetivo,
                                                     mes_venta=self._mes_venta())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo calcular el pedido:\n{exc}")
                return

        for w in self._resumen_optimizador.winfo_children():
            w.destroy()
        for w in self._tabla_optimizador.winfo_children():
            w.destroy()

        con_cantidad = [c for c in r["candidatos"] if c.get("cantidad_sugerida")]

        for texto, valor in (
            ("Candidatos con pedido", str(len(con_cantidad))),
            ("Con score pero sin ningún precio de referencia posible", str(r["n_sin_precio"])),
            ("Monto real del pedido", f"₡{r['monto_real_total']:,.0f}"),
            ("vs. objetivo", f"{(r['monto_real_total'] / pdv_objetivo * 100):.0f}%"),
        ):
            kpi = Marco(self._resumen_optimizador)
            kpi.pack(side="left", padx=(0, 22))
            Etiqueta(kpi, text=valor, style="Subtitulo.TLabel").pack(anchor="w")
            Etiqueta(kpi, text=texto, style="Suave.TLabel").pack(anchor="w")

        if not con_cantidad:
            self._ultimo_sugerido = []
            self._btn_exportar_excel.state(["disabled"])
            self._pintar_cierre([], 0.0)
            Etiqueta(self._tabla_optimizador,
                    text="Ningún candidato tiene costo/moneda declarados suficientes "
                         "para convertir el PDV en unidades.",
                    style="Suave.TLabel").pack(padx=8, pady=8)
            return

        self._ultimo_sugerido = con_cantidad
        self._btn_exportar_excel.state(["!disabled"])
        self._pintar_cierre(con_cantidad, r["monto_real_total"])
        self._pintar_tabla_sugerido(con_cantidad)

    # ---------- cierre del proceso + exportación ----------

    def _nombre_proveedor_lote(self) -> str:
        """Proveedor del catálogo que está en revisión, para el encabezado de
        cierre y el nombre del Excel. Sale del mismo resumen que ya se muestra
        arriba en el paso 6."""
        try:
            resumen = self._mc.resumen_catalogo_activo()
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo leer el resumen del catalogo activo")
            return "este proveedor"
        return (resumen[0].get("proveedor") or "este proveedor") if resumen else "este proveedor"

    def _pintar_cierre_pendiente(self) -> None:
        """Encabezado del paso 7 ANTES de generar la selección: dice que este es
        el último paso y qué falta para terminarlo."""
        for w in self._marco_cierre.winfo_children():
            w.destroy()
        caja = tk.Frame(self._marco_cierre, bg=solido(PAR_PANEL))
        caja.pack(fill="x")
        tk.Label(caja, text="Último paso: tu sugerido de compra",
                 bg=solido(PAR_PANEL), fg=solido(PAR_TEXTO),
                 font=("Segoe UI Semibold", 14), anchor="w").pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(caja,
                 text=("Escribí cuánto querés invertir (PDV objetivo) y generá la selección. "
                       "Después podés exportarla a Excel."),
                 bg=solido(PAR_PANEL), fg=solido(PAR_NEUTRO), font=("Segoe UI", 10),
                 anchor="w", justify="left", wraplength=820).pack(anchor="w", padx=12, pady=(2, 8))

    def _pintar_cierre(self, con_cantidad: list[dict], monto_total: float) -> None:
        """Encabezado de cierre del paso 7: qué se logró, en una frase.

        No es decoración: el comprador terminaba el proceso sin ninguna señal
        de haber llegado al final -- el paso 7 se veía igual que el 3 o el 4."""
        if not con_cantidad:
            self._pintar_cierre_pendiente()
            return
        for w in self._marco_cierre.winfo_children():
            w.destroy()
        pares = sum(int(c.get("cantidad_sugerida") or 0) for c in con_cantidad)
        caja = tk.Frame(self._marco_cierre, bg=solido(PAR_OK_BG),
                        highlightbackground=solido(PAR_OK), highlightthickness=1)
        caja.pack(fill="x")
        tk.Label(caja, text="✓", bg=solido(PAR_OK_BG), fg=solido(PAR_OK),
                 font=("Segoe UI Semibold", 26)).pack(side="left", padx=(14, 10), pady=10)
        texto = tk.Frame(caja, bg=solido(PAR_OK_BG))
        texto.pack(side="left", fill="both", expand=True, pady=10)
        tk.Label(texto, text="¡Listo! Llegaste al final del proceso.",
                 bg=solido(PAR_OK_BG), fg=solido(PAR_OK),
                 font=("Segoe UI Semibold", 14), anchor="w").pack(anchor="w")
        multi = self._es_multiproveedor()
        destinatario = ("los proveedores de este lote" if multi
                        else self._nombre_proveedor_lote())
        tk.Label(texto,
                 text=(f"Sugerido de compra final para {destinatario}: "
                       f"{len(con_cantidad)} referencias · {pares:,} pares sugeridos · "
                       f"₡{monto_total:,.0f} de inversión estimada."),
                 bg=solido(PAR_OK_BG), fg=solido(PAR_TEXTO),
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=820).pack(anchor="w", pady=(2, 0))
        # FASE 4: el desglose por proveedor. Un total único mezcla plata que se
        # le va a pagar a proveedores distintos, cada uno con su propia moneda
        # de origen (aunque el número final siempre esté en colones), y de ese
        # total no sale ninguna orden de compra: salen dos.
        if multi:
            desglose = self._desglose_por_proveedor(con_cantidad)
            if desglose:
                partes = [f"{nombre}: {n} ref · ₡{monto:,.0f}"
                          for nombre, n, monto in desglose]
                partes.append(f"Total: ₡{monto_total:,.0f}")
                tk.Label(texto, text="   ·   ".join(partes),
                         bg=solido(PAR_OK_BG), fg=solido(PAR_TEXTO),
                         font=("Segoe UI Semibold", 10), anchor="w", justify="left",
                         wraplength=820).pack(anchor="w", pady=(4, 0))

    def _desglose_por_proveedor(self, con_cantidad: list[dict]) -> list[tuple]:
        """`[(nombre, n_referencias, monto)]` en el orden de aparición de los
        proveedores del lote. El monto es el mismo `monto_real` que ya suma la
        tabla, así que el desglose no puede discrepar del total."""
        por_id = {c["candidato_id"]: c for c in self._candidatos}
        acum: dict = {}
        for c in con_cantidad:
            pid, nombre = self._proveedor_de(por_id.get(c["candidato_id"], {}))
            n, monto = acum.get(pid, (0, 0.0))
            acum[pid] = (n + 1, monto + float(c.get("monto_real") or 0))
        salida = []
        for pid in self._proveedores_en_pantalla():
            if pid in acum:
                n, monto = acum[pid]
                salida.append((self._nombre_de_proveedor(pid) or "sin proveedor",
                               n, monto))
        return salida

    def _carpeta_lote(self) -> Path | None:
        salida = getattr(self.master, "_salida", None)
        return Path(salida) if salida else None

    def _exportar_sugerido_excel(self) -> None:
        """El sugerido de compra como .xlsx real, en la carpeta del lote.

        Mismas columnas que la tabla en pantalla (foto, código de referencia,
        referencia, cantidad, precio de venta, PVD estimado) y en el mismo
        orden -- los números se toman de `self._ultimo_sugerido`, o sea de la
        misma selección que se está viendo: el Excel no puede discrepar de la
        pantalla porque no recalcula nada.

        La foto va EMBEBIDA en la celda (`openpyxl.drawing.image.Image`) a
        partir del mismo archivo ya limpio que pinta la miniatura."""
        if not self._ultimo_sugerido:
            messagebox.showinfo("Asistente de Compras",
                                "Generá primero la selección con «Generar selección».")
            return
        carpeta = self._carpeta_lote()
        if carpeta is None:
            messagebox.showerror("Asistente de Compras",
                                 "No hay carpeta de trabajo para este lote.")
            return

        with reloj(self):
            try:
                destino = self._escribir_excel_sugerido(carpeta)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Asistente de Compras",
                                     f"No se pudo exportar el Excel:\n{exc}")
                return
        self._confirmar_exportacion(destino)

    def _escribir_excel_sugerido(self, carpeta: Path) -> Path:
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as ImagenXL
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        datos = self._leer_marca_lote_local()
        # FASE 5: con dos catálogos en el lote, nombrar el archivo con UN
        # proveedor miente -- adentro hay referencias de los dos. Si el lote no
        # tiene nombre de proyecto, se usa el nombre de la carpeta del lote.
        # Con un solo proveedor el nombre sigue siendo el suyo, igual que antes.
        por_defecto = self._nombre_proveedor_lote()
        if self._es_multiproveedor():
            por_defecto = (self._carpeta_lote().name if self._carpeta_lote()
                           else "varios proveedores")
        nombre_proyecto = _nombre_archivo_seguro(
            datos.get("nombre_proyecto") or por_defecto)
        destino = carpeta / (f"Sugerido_de_compra_{nombre_proyecto}_"
                             f"{datetime.now():%Y-%m-%d}.xlsx")

        wb = Workbook()
        ws = wb.active
        ws.title = "Sugerido de compra"

        columnas = self._columnas_sugerido()
        rotulos = [r for r, *_ in columnas]
        ws.append(rotulos)
        for col in range(1, len(rotulos) + 1):
            celda = ws.cell(row=1, column=col)
            celda.font = Font(bold=True)
            celda.fill = PatternFill("solid", fgColor="EEEEEE")
            celda.alignment = Alignment(horizontal="center", vertical="center")
        # Anchos pensados para la foto (columna A) y para que no haya que
        # ajustar nada a mano al abrirlo.
        anchos = {"foto": 12, "codigo": 22, "referencia": 34, "cantidad": 10,
                  "precio": 16, "pvd": 18, "proveedor": 22}
        for i, (_rotulo, clave, *_r) in enumerate(columnas, start=1):
            ws.column_dimensions[get_column_letter(i)].width = anchos.get(clave, 16)
        ws.freeze_panes = "A2"

        por_id = {c["candidato_id"]: c for c in self._candidatos}
        # Las imágenes se abren con PIL y se reescalan antes de incrustarlas:
        # las fotos limpias son de 800x800 y meterlas a tamaño real haría un
        # archivo de decenas de MB para un pedido de 40 referencias.
        temporales: list[Path] = []
        alto_px = 72
        for i, c in enumerate(self._ultimo_sugerido, start=2):
            cand = por_id.get(c["candidato_id"], {})
            precio = c.get("precio_venta_estimado")
            pvd = c.get("monto_real")
            # Las celdas se escriben RECORRIENDO las mismas columnas que pinta
            # la pantalla (no por índice fijo): así, cuando la Fase 4 inserta
            # la columna «Proveedor» en un lote multi-proveedor, el Excel la
            # trae en la misma posición y nada se corre de lugar.
            valores = {
                "codigo": codigo_visible(c.get("codigo_proveedor")) or "—",
                "proveedor": self._proveedor_de(cand)[1] or "—",
                "referencia": cand.get("linea_declarada") or "—",
                "cantidad": int(c.get("cantidad_sugerida") or 0),
                "precio": float(precio) if precio else None,
                "pvd": float(pvd) if pvd is not None else None,
            }
            for col, (_rotulo, clave, *_r) in enumerate(columnas, start=1):
                if clave == "foto":
                    continue
                celda = ws.cell(row=i, column=col, value=valores.get(clave))
                if clave in ("precio", "pvd"):
                    celda.number_format = '"₡"#,##0'
            ws.row_dimensions[i].height = alto_px * 0.78  # px → puntos

            ruta = self._ruta_foto_para_excel(cand)
            if ruta is None:
                continue
            try:
                with Image.open(ruta) as im:
                    im = im.convert("RGB")
                    im.thumbnail((alto_px, alto_px))
                    tmp = carpeta / f"_xl_foto_{c['candidato_id']}.png"
                    im.save(tmp, "PNG")
                temporales.append(tmp)
                img = ImagenXL(str(tmp))
                ws.add_image(img, f"A{i}")
            except Exception:  # noqa: BLE001
                continue  # una foto ilegible no puede impedir la exportación

        wb.save(destino)
        # Los PNG temporales solo hacen falta hasta el `save` (openpyxl copia
        # los bytes dentro del .xlsx), así que se borran para no dejar basura
        # en la carpeta del lote.
        for tmp in temporales:
            tmp.unlink(missing_ok=True)
        return destino

    def _leer_marca_lote_local(self) -> dict:
        leer = getattr(self.master, "_leer_marca_lote", None)
        carpeta = self._carpeta_lote()
        if callable(leer) and carpeta is not None:
            try:
                return leer(carpeta)
            except Exception:  # noqa: BLE001
                _logger_gui().exception("no se pudo leer el respaldo de costos del lote en %s", carpeta)
                return {}
        return {}

    def _ruta_foto_para_excel(self, cand: dict) -> Path | None:
        """El archivo real de la foto limpia del candidato -- la variante base,
        la misma que muestra la miniatura de la tabla."""
        colores = (cand or {}).get("colores_vista") or []
        if not colores:
            return None
        # Misma lectura que `_cargar_foto_candidato`: el id de la variante
        # viaja dentro de la URL que arma `api_candidatos` para la web
        # (`/crop_variante/<id>`), no como campo aparte.
        url = colores[0].get("url", "")
        if not url.startswith("/crop_variante/"):
            return None
        try:
            ruta = self._mc.ruta_foto_variante(int(url.rsplit("/", 1)[-1]))
        except Exception:  # noqa: BLE001  (incluye el int() de una URL rara)
            return None
        return ruta if ruta and ruta.exists() else None

    def _confirmar_exportacion(self, destino: Path) -> None:
        """Confirmación con la ruta a la vista y un botón para abrir la
        carpeta -- decir solo "listo" obliga al comprador a ir a buscar el
        archivo a ciegas."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Sugerido de compra exportado")
        _centrar_en_ventana_principal(self, ventana, 620, 220)
        Etiqueta(ventana, text="✓ Sugerido de compra exportado",
                 style="Titulo.TLabel").pack(anchor="w", padx=18, pady=(16, 6))
        Etiqueta(ventana, text=str(destino), style="Suave.TLabel",
                 wraplength=560, justify="left").pack(anchor="w", padx=18)
        fila = Marco(ventana)
        fila.pack(anchor="w", padx=18, pady=16)
        Boton(fila, text="Abrir carpeta", style="Primario.TButton",
              command=lambda: os.startfile(str(destino.parent))).pack(side="left")
        Boton(fila, text="Abrir el Excel",
              command=lambda: os.startfile(str(destino))).pack(side="left", padx=(8, 0))
        Boton(fila, text="Cerrar", style="Sutil.TButton",
              command=ventana.destroy).pack(side="left", padx=(8, 0))
        _traer_al_frente(ventana)

    # Columnas del sugerido de compra, en el orden que pidió el usuario
    # (2026-09-09). Antes era una tira de `Etiqueta(width=N).pack(side="left")`
    # por fila: `width` en un ttk.Label es en CARACTERES, así que las columnas
    # se desalineaban en cuanto un texto se pasaba de largo -- se veía como una
    # lista corrida, no como una tabla. Ahora es `grid` de verdad: una columna
    # por dato, con el mismo peso en el encabezado y en cada fila, así que
    # quedan alineadas siempre.
    #
    # (rótulo, clave interna, alineación, peso, ancho mínimo)
    COLUMNAS_SUGERIDO = (
        ("Foto", "foto", "center", 0, 76),
        ("Código de referencia", "codigo", "w", 0, 150),
        ("Referencia", "referencia", "w", 0, 240),
        ("Cantidad", "cantidad", "e", 0, 90),
        ("Precio de venta", "precio", "e", 0, 120),
        ("PVD estimado", "pvd", "e", 0, 130),
    )

    # FASE 4 (2026-09-16): con dos catálogos en el mismo lote, el sugerido
    # mezcla referencias de proveedores que cotizaron en MONEDAS distintas.
    # Saber de quién es cada fila deja de ser un lujo: es lo que permite
    # convertir esta tabla en dos órdenes de compra.
    COLUMNA_PROVEEDOR = ("Proveedor", "proveedor", "w", 0, 130)

    def _columnas_sugerido(self) -> tuple:
        """Las columnas del paso 7. Con un solo proveedor son exactamente las
        de siempre; con varios se inserta «Proveedor» justo después de la foto
        (y el Excel exportado queda igual que la pantalla, porque los dos leen
        de acá)."""
        cols = list(self.COLUMNAS_SUGERIDO)
        if self._es_multiproveedor():
            cols.insert(1, self.COLUMNA_PROVEEDOR)
        return tuple(cols)

    def _pintar_tabla_sugerido(self, con_cantidad: list[dict]) -> None:
        """La tabla del paso 7, tabulada con `grid`.

        "PVD estimado" (precio de venta al detalle de la línea) es
        `cantidad_sugerida × precio_venta_estimado` -- el campo `monto_real`
        que ya calcula `api_pedido_sugerido`. No se recalcula acá para que no
        puedan divergir dos números que tienen que ser el mismo."""
        tabla = self._tabla_optimizador
        columnas = self._columnas_sugerido()
        for col, (_rotulo, _clave, _anclaje, peso, minimo) in enumerate(columnas):
            tabla.grid_columnconfigure(col, weight=peso, minsize=minimo)
        # Columna de relleno al final: se queda con TODO el espacio sobrante,
        # así las columnas de datos quedan juntas a la izquierda y se leen como
        # una tabla. Sin esto, el sobrante se reparte entre las columnas reales
        # y los montos terminan despegados al otro extremo de la pantalla.
        tabla.grid_columnconfigure(len(columnas), weight=1)

        for col, (rotulo, _clave, anclaje, _peso, _minimo) in enumerate(columnas):
            tk.Label(tabla, text=rotulo, font=("Segoe UI Semibold", 11),
                    fg=solido(PAR_TEXTO), bg=solido(PAR_PANEL), anchor=anclaje,
                    padx=6, pady=6).grid(row=0, column=col, sticky="ew")
        Separador(tabla).grid(row=1, column=0, columnspan=len(columnas),
                              sticky="ew", pady=(0, 2))

        # Índice por candidato_id para sacar la foto y el nombre: el pedido
        # sugerido devuelve códigos y montos, no fotos, pero esta pantalla ya
        # tiene los candidatos completos en memoria.
        por_id = {c["candidato_id"]: c for c in self._candidatos}

        for i, c in enumerate(con_cantidad):
            fila_grid = i + 2
            cand = por_id.get(c["candidato_id"], {})

            hueco = ctk.CTkFrame(tabla, fg_color=PAR_VISOR_BG, corner_radius=4,
                                 width=64, height=64)
            hueco.grid(row=fila_grid, column=0, padx=6, pady=4)
            hueco.grid_propagate(False)
            foto_tk = self._cargar_foto_candidato(cand) if cand else None
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0).pack(fill="both", expand=True)
            else:
                Etiqueta(hueco, text="sin foto", style="Suave.TLabel").pack(
                    fill="both", expand=True)

            # "Referencia" descriptiva: la línea declarada del catálogo del
            # proveedor. Si no vino, se dice "—" en vez de repetir el código
            # (que ya está en su propia columna).
            referencia = cand.get("linea_declarada") or "—"
            precio = c.get("precio_venta_estimado")
            pvd = c.get("monto_real")
            valores = {
                "codigo": codigo_visible(c.get("codigo_proveedor")) or "—",
                "referencia": referencia,
                "cantidad": str(c.get("cantidad_sugerida") or 0),
                "precio": f"₡{precio:,.0f}" if precio else "—",
                "pvd": f"₡{pvd:,.0f}" if pvd is not None else "—",
            }
            for col, (_rotulo, clave, anclaje, _peso, _minimo) in enumerate(columnas):
                if clave == "foto":
                    continue
                if clave == "proveedor":
                    # La MISMA pastilla (y el mismo color) que en el paso 6:
                    # la fila del sugerido se reconoce de un ojo como la
                    # tarjeta de donde salió.
                    self._pastilla_proveedor(tabla, cand).grid(
                        row=fila_grid, column=col, sticky="w", padx=6, pady=4)
                    continue
                Etiqueta(tabla, text=valores[clave], anchor=anclaje,
                        justify=("right" if anclaje == "e" else "left")).grid(
                    row=fila_grid, column=col, sticky="ew", padx=6, pady=4)
