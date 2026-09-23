"""
Panel de comparables de un candidato -- Fase 2 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Se corta primero porque es la hoja del árbol: `VentanaCandidatosCTk` lo usa,
pero él no usa a nadie de la interfaz salvo los widgets puente y la paleta. Un
panel de ~415 líneas que consulta comparables (marca y TUCALZADO) no tiene por
qué estar en el mismo archivo que el flujo de procesamiento de fotos; separado,
se puede leer y tocar sin desplazarse por 10,000 líneas ajenas.
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
    MONEDAS_PROVEEDOR,
    MARGEN_VENTA_DEFAULT, ROTULO_PRECIO_COMPARABLES, ROTULO_PRECIO_POR_COSTO,
    calcular_precio_venta, moneda_convierte, moneda_valida, redondear_pvp,
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


class PanelComparablesCTk(ctk.CTkFrame):
    """Comparables de un candidato -- marca reconocida (con precio de
    competencia) y TUCALZADO ya vendido (con precio/unidades reales). Misma
    consulta que `/api/similar_marca`/`/api/similar_tuc` en la web, mostrada
    nativamente. Si el candidato trae varias variantes de color, un selector
    permite recalcular por cada una (mismo criterio que la web: comparable
    independiente por color, no solo la variante 0).

    Plan 2026-09-07, paso 8: era `VentanaComparablesCTk`, un TERCER Toplevel
    apilado sobre la ventana de candidatos, que a su vez estaba sobre la
    principal. Ahora es un panel lateral dentro de la misma pantalla."""

    def __init__(self, master: tk.Misc, motor_candidatos, cand: dict, al_cerrar=None,
                 precio_por_costo: bool = False, indice_inicial: int | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._mc = motor_candidatos
        self._cand = cand
        self._al_cerrar = al_cerrar
        # Si el precio de referencia de este candidato salió del costo que el
        # comprador ingresó en el paso 6 (lo sabe la pantalla que abre este
        # panel, que es la dueña del respaldo local), el rótulo lo tiene que
        # decir. Default False = comportamiento anterior.
        self._precio_por_costo = precio_por_costo
        self._fotos_tk: list[ImageTk.PhotoImage] = []

        colores = cand.get("colores_vista") or [{"indice": 0, "color_principal": "—"}]
        self._colores = colores

        # `grid` en vez de `pack` (2026-09-09): con pack, los bloques de alto
        # fijo se servían primero por orden de empaquetado y la lista de
        # comparables (la última, la que tiene expand) podía quedarse con 0px
        # -- el bug de "los comparables quedan tapados detrás de Guardar
        # cotización". Con grid, SOLO la fila de la lista tiene weight=1, así
        # que es la única que crece, y su `minsize` le garantiza alto propio
        # aunque la ventana se encoja. Las filas se declaran en orden visual;
        # la cotización es la última fila y por lo tanto siempre visible abajo.
        self.grid_columnconfigure(0, weight=1)
        for r in (0, 1, 2, 4):
            self.grid_rowconfigure(r, weight=0)
        self.grid_rowconfigure(3, weight=1, minsize=220)

        titulo = Marco(self)
        titulo.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        Etiqueta(titulo, text=f"Comparables — "
                 f"{codigo_visible(cand.get('codigo_proveedor')) or cand['candidato_id']}",
                 style="Subtitulo.TLabel").pack(side="left")
        Boton(titulo, text="✕", style="Sutil.TButton", width=30,
              command=self._cerrar).pack(side="right")

        controles = Marco(self)
        controles.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 6))
        Etiqueta(controles, text="Color:", style="Suave.TLabel").pack(side="left")
        self._v_indice = tk.StringVar(value=str(colores[0]["indice"]))
        opciones = [f"{c['indice']} — {c.get('color_principal') or '—'}" for c in colores]
        # `indice_inicial` (2026-09-17): con qué color NACE el panel. Lo manda
        # el botón 👁 de cada miniatura del paso 6 -- cada color tiene sus
        # propios comparables y el comprador pidió llegar directo al que clicó,
        # no al primero. None (botón general, clic en la tarjeta) = primer
        # color, exactamente como antes. Si el índice ya no existe (el color se
        # borró entre el dibujo de la tarjeta y el clic) se degrada al primero
        # en vez de reventar.
        pos = next((k for k, c in enumerate(colores) if c["indice"] == indice_inicial), 0)
        combo = ctk.CTkComboBox(controles, values=opciones, width=180,
                                command=lambda _v: self._recalcular(), state="readonly")
        combo.set(opciones[pos])
        combo.pack(side="left", padx=(8, 0))
        self._combo = combo

        # Precio de venta (plan 2026-09-07, paso 6): acá el comprador tiene los
        # precios de los comparables a la vista (marca reconocida, TUCALZADO ya
        # vendido) -- el lugar natural para editar el precio de referencia, no
        # solo en la tarjeta de la lista.
        self._construir_panel_precio()

        # Los comparables (fila 3, la única con peso) van ANTES de la
        # cotización (fila 4) en el orden visual: la lista es lo que el
        # comprador vino a mirar y ahora tiene todo el alto libre de la
        # ventana, mientras el cuadro de Guardar cotización ocupa su propia
        # franja fija abajo sin poder pisarla.
        self._contenedor = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._contenedor.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # Cotización en franja FIJA abajo (fuera del scroll de comparables) --
        # mismo criterio que la web: "esto es lo único que hace permanente al
        # candidato", así que el botón de Guardar tiene que quedar siempre visible,
        # no perdido al hacer scroll entre comparables de marca y de TUCALZADO.
        self._construir_panel_cotizacion()

        self._recalcular()

    def _cerrar(self) -> None:
        if self._al_cerrar is not None:
            self._al_cerrar()  # el dueño del panel destruye este widget
        else:
            self.destroy()

    def _indice_actual(self) -> int:
        texto = self._combo.get()
        return int(texto.split(" — ", 1)[0])

    # ---------- guardar cotización (promueve el candidato a permanente) ----------

    def _ver_foto_grande(self, path, titulo: str) -> None:
        """Abre la foto de UN comparable en tamaño grande, en su propia
        ventana. Mismo patrón que `_mostrar_imagen_flotante` del visor de
        fotos, incluido `_traer_al_frente`: sin eso la ventana nace DETRÁS de
        la de comparables (Windows le devuelve el foco al widget clicado) y
        parece que el clic no hizo nada -- el bug que se corrigió en el paso 5
        el 2026-09-10."""
        if not path:
            return
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return
        ventana = ctk.CTkToplevel(self)
        ventana.title(f"Comparable — {titulo}")
        ventana.configure(fg_color=PAR_VISOR_BG)
        mostrar = im.copy()
        mostrar.thumbnail((max(self.winfo_screenwidth() - 200, 300),
                           max(self.winfo_screenheight() - 220, 300)))
        foto = ImageTk.PhotoImage(mostrar)
        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta
        lbl.pack(padx=10, pady=(10, 4))
        Etiqueta(ventana, text=f"{titulo}  ·  {im.width} × {im.height} px",
                 style="Suave.TLabel").pack(pady=(0, 10))
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    def _construir_panel_precio(self) -> None:
        """Precio de venta esperado + su fuente, INFORMATIVO.

        2026-09-10 (Tarea 9, pedido del usuario): acá había un "Fijar a mano
        ₡:" con su ✓ y su Quitar. Se eliminó -- el usuario reportó que no le
        servía y que no entendía qué era, y es el mismo control que se sacó de
        la tarjeta del paso 6 (Tarea 3). Nada depende de él: el precio de
        referencia sigue saliendo de la cascada (`precio_venta_referencia`) y
        el camino real para fijarlo es el costo del proveedor en el paso 6,
        que ya escribe `precio_venta_manual` por la fórmula del lote. La
        etiqueta se conserva: es el número contra el que el comprador lee los
        precios de los comparables de abajo."""
        panel = Marco(self)
        panel.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        precio, fuente = self._mc.obtener_precio_referencia(self._cand["candidato_id"])
        texto = texto_precio_referencia(precio, fuente,
                                        por_costo_lote=self._precio_por_costo)
        self._etq_precio = Etiqueta(panel, text=texto, style="Suave.TLabel", wraplength=900,
                                    justify="left")
        self._etq_precio.pack(anchor="w", fill="x")

    def _construir_panel_cotizacion(self) -> None:
        panel = Tarjeta(self)
        panel.grid(row=4, column=0, sticky="ew", padx=8, pady=(6, 8))

        Etiqueta(panel, text="Guardar cotización (esto es lo único que hace "
                "permanente al candidato)", style="Subtitulo.TLabel",
                justify="left").pack(anchor="w", padx=10, pady=(10, 6))

        # Dos filas, no una: mantiene los cinco controles legibles también
        # cuando la ventana se encoge al mínimo.
        fila1 = Marco(panel)
        fila1.pack(fill="x", padx=10, pady=(0, 4))
        Etiqueta(fila1, text="Costo lista", style="Suave.TLabel").pack(side="left")
        self._v_costo_lista = tk.StringVar()
        ctk.CTkEntry(fila1, textvariable=self._v_costo_lista, width=80).pack(side="left", padx=(4, 12))

        Etiqueta(fila1, text="Desc. %", style="Suave.TLabel").pack(side="left")
        self._v_descuento = tk.StringVar(value="0")
        ctk.CTkEntry(fila1, textvariable=self._v_descuento, width=54).pack(side="left", padx=(4, 12))

        Etiqueta(fila1, text="Moneda", style="Suave.TLabel").pack(side="left")
        self._v_moneda = tk.StringVar(value="USD")
        ctk.CTkComboBox(fila1, values=list(MONEDAS_PROVEEDOR), variable=self._v_moneda, width=78,
                        state="readonly").pack(side="left", padx=(4, 0))

        fila2 = Marco(panel)
        fila2.pack(fill="x", padx=10, pady=(0, 4))
        Etiqueta(fila2, text="T. cambio →CRC", style="Suave.TLabel").pack(side="left")
        self._v_tipo_cambio = tk.StringVar()
        ctk.CTkEntry(fila2, textvariable=self._v_tipo_cambio, width=80).pack(side="left", padx=(4, 12))

        Boton(fila2, text="💾 Guardar", style="Primario.TButton",
             command=self._guardar_cotizacion).pack(side="left")

        self._estado_cotizacion = Etiqueta(panel, text="", style="Suave.TLabel",
                                           wraplength=900, justify="left")
        self._estado_cotizacion.pack(anchor="w", padx=10, pady=(0, 10))

    def _guardar_cotizacion(self) -> None:
        try:
            costo_lista = float(self._v_costo_lista.get())
            if costo_lista <= 0:
                raise ValueError
        except ValueError:
            self._estado_cotizacion.configure(text="Ingresá el costo de lista.")
            return
        try:
            descuento_pct = float(self._v_descuento.get() or 0)
        except ValueError:
            # descuento no numerico o vacio: se cotiza sin descuento.
            descuento_pct = 0.0
        moneda = self._v_moneda.get()
        tipo_cambio_txt = self._v_tipo_cambio.get().strip()
        tipo_cambio_usd_crc = None
        if tipo_cambio_txt:
            try:
                tipo_cambio_usd_crc = float(tipo_cambio_txt)
            except ValueError:
                self._estado_cotizacion.configure(text="Tipo de cambio inválido.")
                return
        if moneda_convierte(moneda) and not tipo_cambio_usd_crc:
            self._estado_cotizacion.configure(
                text=f"Con {moneda} necesitás el tipo de cambio.")
            return

        costo_neto = costo_lista * (1 - descuento_pct / 100)
        self._estado_cotizacion.configure(text="⏳ Guardando...")
        self.update_idletasks()
        # Golpea la base y promueve el candidato a permanente: es de las
        # operaciones más lentas de la pantalla, así que además del texto
        # "⏳ Guardando..." va el reloj de arena.
        with reloj(self):
            try:
                r = self._mc.guardar_cotizacion({
                    "candidato_id": self._cand["candidato_id"],
                    "costo_lista": costo_lista,
                    "descuento_pct": descuento_pct,
                    "costo_neto": costo_neto,
                    "moneda": moneda,
                    "tipo_cambio_usd_crc": tipo_cambio_usd_crc,
                })
            except Exception as exc:  # noqa: BLE001
                self._estado_cotizacion.configure(text=f"✗ Error: {exc}")
                return
        self._estado_cotizacion.configure(
            text=f"✓ Cotización guardada. Candidato promovido a permanente "
                 f"(id={r['candidato_id_permanente']}). Podés seguir revisando el resto del catálogo.")

    def _recalcular(self) -> None:
        for w in self._contenedor.winfo_children():
            w.destroy()
        indice = self._indice_actual()
        candidato_id = self._cand["candidato_id"]

        with reloj(self):
            try:
                canal = self._mc.canal_venta_lote()
                marca = self._mc.obtener_similares_marca(candidato_id, indice=indice)
                tuc = self._mc.obtener_similares_tuc(candidato_id, indice=indice)
            except Exception as exc:  # noqa: BLE001
                Etiqueta(self._contenedor, text=f"Error consultando comparables:\n{exc}",
                        style="Suave.TLabel").pack(padx=8, pady=8)
                return

        # Aviso de color (2026-09-09): la búsqueda ahora PREFIERE el mismo
        # color de familia que el candidato (ver `filtrar_color` en
        # servidor_pty). Cuando no había ninguno del mismo color, se muestran
        # los de otro color -- pero se dice, en vez de hacerlos pasar por
        # comparables válidos, que era el bug reportado.
        familia = next((it.get("color_familia_candidato") for it in (marca + tuc)
                        if it.get("color_familia_candidato")), None)
        degradado = any(it.get("color_degradado") for it in (marca + tuc))
        if familia:
            if degradado:
                Etiqueta(self._contenedor,
                        text=f"⚠ No hay comparables del mismo color ({familia}). Se muestran "
                             f"los de OTRO color, marcados como tal: sirven para el molde, "
                             f"no para decidir el color a comprar.",
                        style="Suave.TLabel", wraplength=900, justify="left").pack(
                    anchor="w", pady=(0, 6))
            else:
                Etiqueta(self._contenedor,
                        text=f"Comparables del mismo color del candidato ({familia}), "
                             f"además del mismo tipo y género.",
                        style="Suave.TLabel", wraplength=900, justify="left").pack(
                    anchor="w", pady=(0, 6))

        # Qué lista MANDA lo decide el CANAL DE VENTA declarado para el lote en
        # el paso 1 (2026-09-10), no la marca declarada del candidato suelto:
        # es la misma decisión con la que el motor calculó el puntaje, así que
        # la ventana y el score cuentan la misma historia.
        #
        #   canal "marca" -> comparables Y puntaje contra TC Marcas.
        #   canal "tuc"   -> comparables Y puntaje contra TU Calzado; la lista
        #                    de marca reconocida se muestra igual, pero
        #                    marcada como INFORMATIVA ("a qué producto de marca
        #                    se parece"), sin efecto en el puntaje.
        marca_declarada = (self._cand.get("marca_declarada") or "").strip()
        if canal == "marca":
            Etiqueta(self._contenedor,
                    text="Compra declarada para TC MARCAS: los comparables y el puntaje "
                         "de este lote se calculan contra lo vendido/ofrecido en marca "
                         "reconocida. La lista de TU Calzado va abajo, solo de referencia.",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
            bloques = [("Marca reconocida — TC Marcas / competencia", marca, "marca"),
                       ("TU Calzado ya vendido (genérico) — solo referencia", tuc, "tuc")]
        else:
            # Aviso informativo pedido por el dueño: cuando la compra es para
            # el genérico, igual se dice a qué producto de MARCA RECONOCIDA se
            # parece el candidato. Es una consulta de lectura aparte: no entra
            # en `score_final` de ninguna forma.
            parecidos = ", ".join(
                f"{(it.get('marca') or '').strip()} {(it.get('codigo_tc') or it.get('referencia') or '').strip()}".strip()
                for it in marca[:3]) or "—"
            Etiqueta(self._contenedor,
                    text=f"Compra declarada para TU CALZADO (genérico): el puntaje se "
                         f"calcula contra lo vendido en TU Calzado.\n"
                         f"Se parece a: {parecidos} (marca reconocida) — solo informativo, "
                         f"no afecta el puntaje.",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
            bloques = [("TU Calzado ya vendido (genérico)", tuc, "tuc"),
                       ("Se parece a (marca reconocida) — solo informativo, "
                        "no afecta el puntaje", marca, "marca")]
        if marca_declarada:
            Etiqueta(self._contenedor,
                    text=f"Nota: este candidato viene declarado como marca reconocida "
                         f"({marca_declarada}).",
                    style="Suave.TLabel", wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 6))
        for i, (titulo, items, tipo) in enumerate(bloques):
            if i:
                Separador(self._contenedor).pack(fill="x", pady=10)
            Etiqueta(self._contenedor, text=titulo,
                    style="Subtitulo.TLabel").pack(anchor="w", pady=(0, 4))
            self._pintar_lista(items, tipo=tipo)

    def _pintar_lista(self, items: list[dict], tipo: str) -> None:
        if not items:
            Etiqueta(self._contenedor, text="Sin comparables por encima del umbral.",
                    style="Suave.TLabel").pack(anchor="w", padx=8, pady=(0, 6))
            return
        for it in items:
            fila = Tarjeta(self._contenedor)
            fila.pack(fill="x", pady=3)

            hueco = ctk.CTkFrame(fila, fg_color=PAR_VISOR_BG, corner_radius=6, width=64, height=64)
            hueco.pack(side="left", padx=8, pady=8)
            hueco.pack_propagate(False)
            imagen_id = it.get("imagen_id")
            ruta_foto = self._mc.ruta_foto_imagen(imagen_id) if imagen_id else None
            foto_tk = _cargar_foto_generico(ruta_foto, tam=(60, 60))
            if foto_tk is not None:
                self._fotos_tk.append(foto_tk)
                # Foto CLICKEABLE (2026-09-10, pedido del usuario): la
                # miniatura de 60px no alcanza para comparar moldes, que es
                # justo para lo que se abre esta ventana. Mismo patrón que el
                # visor del paso 5 (ver `_ver_foto_grande`).
                lbl_foto = tk.Label(hueco, image=foto_tk, background=solido(PAR_VISOR_BG),
                        borderwidth=0, cursor="hand2")
                lbl_foto.pack(fill="both", expand=True)
                lbl_foto.bind("<Button-1>", lambda _e, p=ruta_foto, t=(
                    it.get("codigo_tc") or it.get("codigo_tuc")
                    or f"{it.get('marca') or ''} {it.get('referencia') or ''}".strip()):
                    self._ver_foto_grande(p, t))
            else:
                Etiqueta(hueco, text="—", style="Suave.TLabel").pack(fill="both", expand=True)

            info = Marco(fila)
            info.pack(side="left", fill="both", expand=True, pady=8)
            if tipo == "marca":
                # DESTACADO el código interno de TU Calzado (2026-09-10): la
                # referencia del fabricante ("ADJS100156-WW0") no se puede
                # buscar en nuestros sistemas; el `codigo_tc` sí. Se muestran
                # los dos, pero el propio arriba y en grande. Cuando el
                # comparable es un producto de mercado que nunca vendimos, se
                # dice así en vez de dejar un renglón vacío.
                codigo_tc = it.get("codigo_tc")
                Etiqueta(info, text=(f"Código TU Calzado: {codigo_tc}" if codigo_tc
                                     else "No está en nuestro catálogo (solo mercado)"),
                        style="Subtitulo.TLabel").pack(anchor="w")
                Etiqueta(info, text=f"{it.get('marca')}  ref. fabricante {it.get('referencia')}",
                        style="Suave.TLabel").pack(anchor="w")
                # Precio de competencia SACADO (2026-09-10): confirmado que no
                # entra en el score (`puntuar_candidatos` nunca llama a
                # `api_similar_marca`, y `margen_factor` está cableado en 1.0
                # para candidatos en staging) -- era puramente informativo y
                # el usuario pidió sacarlo, quedándose con ventas propias.
                precio_vta = it.get("precio_avg_vta")
                unidades = it.get("unidades_vendidas", 0)
                if precio_vta:
                    linea = (f"Vendido por TC Marcas: ₡{precio_vta:,.0f} promedio  ·  "
                             f"{unidades} unidades")
                else:
                    linea = "Sin ventas propias registradas"
                Etiqueta(info, text=f"{linea}  ·  {it.get('tipo') or '—'}",
                        style="Suave.TLabel").pack(anchor="w")
            else:
                Etiqueta(info, text=f"Código TU Calzado: {it.get('codigo_tuc')}",
                        style="Subtitulo.TLabel").pack(anchor="w")
                precio = it.get("precio_avg_vta")
                texto_precio = (f"Vendido a ₡{precio:,.0f} promedio" if precio
                                else "sin ventas registradas")
                Etiqueta(info,
                        text=f"{texto_precio}  ·  {it.get('unidades_vendidas', 0)} unidades  ·  {it.get('categoria') or '—'}",
                        style="Suave.TLabel").pack(anchor="w")

            # Color del comparable, a la vista: es el dato que faltaba para
            # poder confiar (o desconfiar) de la lista. "otro color" se dice
            # explícitamente en vez de dejarlo adivinar por la foto.
            fila_col = Marco(info)
            fila_col.pack(anchor="w", fill="x", pady=(2, 0))
            _pastilla_color(fila_col, it.get("color_familia")).pack(side="left")
            if it.get("mismo_color") is False:
                tk.Label(fila_col, text="⚠ otro color", fg=solido(PAR_ALERTA),
                        bg=solido(PAR_PANEL), font=("Segoe UI Semibold", 10)
                        ).pack(side="left", padx=(6, 0))

            Etiqueta(fila, text=f"similitud {it['similitud']:.2f}", style="Suave.TLabel"
                    ).pack(side="right", padx=10)
