"""
Grilla de tarjetas de recortes -- Fase 3 de la división de
`gui_profesional_ctk.py` (2026-09-23).

Se deja para el final porque es la única de las piezas extraídas que sigue
acoplada al flujo: `HerramientaUnica` la usa como si fuera el `ttk.Treeview`
que reemplazó (`insert`, `selection_set`, `tag_configure`, el evento virtual
`<<TreeviewSelect>>`). Esa API prestada es justamente lo que permite moverla
sin tocar nada: el contrato entre la ventana y la grilla ya estaba escrito, y
sacarla de en medio deja el archivo principal empezando directo en la lógica
de la aplicación en vez de en 375 líneas de armado de miniaturas.
"""

import tkinter as tk
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageTk

import decisiones
import reparar

from tema import (
    COLOR_BORDE, COLOR_FONDO, COLOR_MAL, COLOR_MAL_BG, COLOR_NEUTRO, COLOR_OK,
    COLOR_OK_BG, COLOR_PANEL, COLOR_TEXTO, COLOR_TEXTO_SUAVE, F, PAR_ALERTA,
    PAR_BORDE,
    PAR_PANEL, PAR_PANEL_SUAVE, PAR_PRIMARIO, PAR_SELECCION, PAR_TEXTO_SUAVE,
    PAR_VISOR_BG,
    RADIO_CONTROL, RADIO_PANEL, solido,
)
from util_ventana import _badge, _logger_gui
from widgets_puente import Boton, Casilla, CasillaOk, Etiqueta, Marco, Tarjeta


class GrillaRecortes(ctk.CTkScrollableFrame):
    """La lista de recortes: una grilla de tarjetas con la FOTO de cada
    recorte, en vez de la tabla de texto con columnas que había antes.

    Es el mismo patrón que la pantalla "Elegir qué limpiar" — miniatura con
    las líneas de referencia, nombre, y el estado en badges de color — para
    que elegir un recorte sea mirar la foto, no leer una fila de números.

    Habla la misma API mínima que usaba el `ttk.Treeview` que reemplaza
    (`insert`, `delete`, `get_children`, `exists`, `selection`,
    `selection_set`, `set`, `item`, `tag_configure`, y el evento virtual
    `<<TreeviewSelect>>`). Eso es a propósito: así la lógica que ya existe
    — `_recargar_manifiesto`, `_seleccionar`, `_decidir`,
    `_vaciar_carpeta_trabajo` — sigue funcionando palabra por palabra, y lo
    único que cambió de verdad es CÓMO se ve y se elige un recorte, no qué
    pasa después de elegirlo.

    Las miniaturas se generan en lotes con `after()` (igual que
    `_cargar_miniaturas_limpieza`): con cientos de recortes, calcular todos
    los contornos de un tirón congelaría la ventana varios segundos.
    """

    # Una sola columna: la lista vive en un panel angosto, así que cada
    # recorte es una fila ancha — miniatura a la izquierda, nombre y estado a
    # la derecha. En dos columnas las tarjetas quedaban cortadas por el borde
    # del panel (probado en pantalla).
    COLS = 1
    LADO_MINIATURA = 78

    def __init__(self, master, proveedor_png, componer, al_cambiar_lote=None, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("scrollbar_button_color", PAR_BORDE)
        kw.setdefault("scrollbar_button_hover_color", PAR_TEXTO_SUAVE)
        super().__init__(master, **kw)
        self._proveedor_png = proveedor_png   # nombre -> Path del png actual
        self._componer = componer             # imagen RGBA -> RGB sobre gris
        self._al_cambiar_lote = al_cambiar_lote  # aviso a la app: cambió el lote
        self._orden: list[str] = []
        self._tarjetas: dict[str, dict] = {}
        self._sel: str | None = None
        # Selección MÚLTIPLE (el "lote"). Convive con `_sel` en vez de
        # reemplazarla: `_sel` sigue siendo "la foto que se está mirando en el
        # panel de detalle" (una sola, la que gobierna `_nombre_actual`), y
        # `_lote` es "sobre cuáles se va a actuar en bloque". Unificarlas
        # habría obligado a decidir qué foto mostrar en grande cuando hay 12
        # marcadas, que es justo lo que no tiene respuesta.
        self._lote: set[str] = set()
        self._ancla: str | None = None        # extremo fijo del Shift+clic
        self._colores_tag: dict[str, str] = {}
        self._fotos: dict[str, ImageTk.PhotoImage] = {}
        self._pendientes: list[str] = []
        self._tarea_lote: str | None = None
        # Las tarjetas se colocan con `grid`; este aviso también, porque Tk no
        # permite mezclar `pack` y `grid` dentro del mismo contenedor.
        self._vacio = Etiqueta(self, text="Todavía no hay recortes en esta carpeta.",
                               style="SuavePanel.TLabel", wraplength=250)
        self._vacio.grid(row=0, column=0, columnspan=self.COLS, padx=6, pady=6, sticky="w")

    # ── API que usaba el Treeview ────────────────────────────────────────

    def get_children(self, item="") -> tuple:
        return tuple(self._orden)

    def exists(self, iid: str) -> bool:
        return iid in self._tarjetas

    def selection(self) -> tuple:
        return (self._sel,) if self._sel in self._tarjetas else ()

    def deseleccionar(self) -> None:
        """Deja la grilla sin foto seleccionada ni lote marcado -- lo usa
        "cerrar el visor" después de aprobar una foto ya corregida."""
        antes = set(self._lote) | ({self._sel} if self._sel else set())
        self._sel = None
        self._lote = set()
        self._ancla = None
        self._repintar(antes)
        self._avisar_lote()
        self.event_generate("<<TreeviewSelect>>")

    def selection_set(self, iid: str) -> None:
        """Clic simple: ésta pasa a ser LA foto seleccionada y el lote se
        reduce a ella sola (el clic sin modificador siempre "empieza de
        nuevo", que es lo que espera cualquiera que venga del explorador de
        archivos)."""
        if iid not in self._tarjetas:
            return
        anterior_lote = set(self._lote)
        anterior, self._sel = self._sel, iid
        self._lote = {iid}
        self._ancla = iid
        self._repintar({anterior, iid} | anterior_lote)
        self._avisar_lote()
        self.event_generate("<<TreeviewSelect>>")

    # ── selección múltiple (lote) ────────────────────────────────────────

    def lote(self) -> list[str]:
        """Los nombres marcados para acción en bloque, en el orden en que se
        ven en pantalla."""
        return [n for n in self._orden if n in self._lote]

    def limpiar_lote(self, conservar_actual: bool = True) -> None:
        antes = set(self._lote)
        self._lote = {self._sel} if (conservar_actual and self._sel in self._tarjetas) else set()
        self._repintar(antes | self._lote)
        self._avisar_lote()

    def _avisar_lote(self) -> None:
        if callable(self._al_cambiar_lote):
            self._al_cambiar_lote(self.lote())

    def _alternar_lote(self, nombre: str) -> str:
        """Ctrl+clic: agrega o quita esta tarjeta del lote SIN tocar cuál es
        la foto del panel de detalle — marcar 8 fotos para desechar no debería
        hacer saltar el visor 8 veces."""
        if nombre not in self._tarjetas:
            return "break"
        antes = set(self._lote)
        if nombre in self._lote:
            self._lote.discard(nombre)
        else:
            self._lote.add(nombre)
            self._ancla = nombre
        # Se repinta TODO el lote (antes y después) y no solo la tarjeta
        # clicada porque la marca ámbar aparece recién con 2+: al pasar de 1 a
        # 2 hay que ir a pintar también a la que ya estaba marcada.
        self._repintar(antes | self._lote | {nombre})
        self._avisar_lote()
        return "break"

    def _repintar(self, nombres) -> None:
        for n in nombres:
            if n in self._tarjetas:
                self._pintar_borde(n)

    def _rango_lote(self, nombre: str) -> str:
        """Shift+clic: agrega al lote todo lo que hay entre el ancla (la
        última tarjeta marcada o seleccionada) y ésta."""
        if nombre not in self._tarjetas:
            return "break"
        ancla = self._ancla if self._ancla in self._tarjetas else self._sel
        if ancla not in self._tarjetas:
            return self._alternar_lote(nombre)
        i, j = sorted((self._orden.index(ancla), self._orden.index(nombre)))
        antes = set(self._lote)
        self._lote |= set(self._orden[i:j + 1])
        self._repintar(antes | self._lote)
        self._avisar_lote()
        return "break"

    def tag_configure(self, tag: str, foreground: str | None = None, **_kw) -> None:
        if foreground:
            self._colores_tag[tag] = foreground

    def delete(self, *iids) -> None:
        for iid in iids:
            tarjeta = self._tarjetas.pop(iid, None)
            if tarjeta is not None:
                tarjeta["marco"].destroy()
            if iid in self._orden:
                self._orden.remove(iid)
            self._fotos.pop(iid, None)
            if iid in self._pendientes:
                self._pendientes.remove(iid)
            if self._sel == iid:
                self._sel = None
            if self._ancla == iid:
                self._ancla = None
            self._lote.discard(iid)
        self._avisar_lote()
        self._recolocar()
        if not self._orden:
            self._vacio.grid(row=0, column=0, columnspan=self.COLS,
                             padx=6, pady=6, sticky="w")

    def _recolocar(self) -> None:
        """Reacomoda las tarjetas que quedan, para que un borrado parcial no
        deje huecos en la grilla."""
        for idx, nombre in enumerate(self._orden):
            fila, col = divmod(idx, self.COLS)
            self._tarjetas[nombre]["marco"].grid_configure(row=fila, column=col)

    def insert(self, parent="", index="end", iid: str = "", text: str = "",
               values=(), tags=()) -> str:
        """Crea la tarjeta de un recorte. Firma compatible con la del
        Treeview: `values` = (falta%, mordida, sombra, decisión)."""
        self._vacio.grid_remove()
        nombre = iid or text
        if nombre in self._tarjetas:
            self.delete(nombre)
        self._orden.append(nombre)

        self.grid_columnconfigure(0, weight=1)
        fila, col = divmod(len(self._orden) - 1, self.COLS)
        marco = ctk.CTkFrame(self, fg_color=PAR_PANEL_SUAVE, corner_radius=RADIO_CONTROL,
                             border_width=2, border_color=PAR_PANEL_SUAVE,
                             height=self.LADO_MINIATURA + 14)
        marco.grid(row=fila, column=col, padx=2, pady=3, sticky="ew")
        marco.grid_propagate(False)

        hueco = ctk.CTkFrame(marco, fg_color=PAR_VISOR_BG, corner_radius=6,
                             width=self.LADO_MINIATURA, height=self.LADO_MINIATURA)
        hueco.pack(side="left", padx=(6, 8), pady=6)
        hueco.pack_propagate(False)
        # tk.Label porque es el que recibe los ImageTk.PhotoImage que ya
        # produce el pipeline de imagen de la app.
        lbl_img = tk.Label(hueco, background=solido(PAR_VISOR_BG), cursor="hand2",
                           borderwidth=0, highlightthickness=0)
        lbl_img.pack(fill="both", expand=True)

        datos = Marco(marco)
        datos.pack(side="left", fill="both", expand=True, pady=6, padx=(0, 6))
        # Se muestra el nombre SIN la extensión (`.png`): no aporta nada y
        # hacía que el nombre se cortara en dos líneas por una "g" suelta.
        # La clave interna sigue siendo el nombre completo.
        lbl_nombre = Etiqueta(datos, text=Path(nombre).stem, style="SubtituloPanel.TLabel",
                              wraplength=170, cursor="hand2")
        lbl_nombre.pack(fill="x")
        lbl_avisos = Etiqueta(datos, text="", style="SuavePanel.TLabel",
                              wraplength=170, cursor="hand2")
        lbl_avisos.pack(fill="x")
        lbl_dec = Etiqueta(datos, text="", style="SuavePanel.TLabel", cursor="hand2")
        lbl_dec.pack(fill="x")

        self._tarjetas[nombre] = {"marco": marco, "img": lbl_img, "dec": lbl_dec,
                                  "avisos": lbl_avisos, "valores": list(values),
                                  "tags": list(tags), "cargada": False}
        for widget in (marco, hueco, lbl_img, datos, lbl_nombre, lbl_avisos, lbl_dec):
            widget.bind("<Button-1>", lambda _e, n=nombre: self.selection_set(n))
            # Tk elige el binding MÁS específico, así que estos dos ganan sobre
            # <Button-1> y el clic simple sigue comportándose como siempre.
            widget.bind("<Control-Button-1>", lambda _e, n=nombre: self._alternar_lote(n))
            widget.bind("<Shift-Button-1>", lambda _e, n=nombre: self._rango_lote(n))

        self._pintar_estado(nombre)
        self._pendientes.append(nombre)
        self._programar_lote()
        return nombre

    def set(self, iid: str, column: str, value=None):
        """`tabla.set(nombre, "decision", "aprobado")` de la lógica original."""
        tarjeta = self._tarjetas.get(iid)
        if tarjeta is None:
            return ""
        indice = {"falta": 0, "mordida": 1, "sombra": 2, "decision": 3}.get(column)
        if indice is None:
            return ""
        if value is None:
            return tarjeta["valores"][indice]
        tarjeta["valores"][indice] = value
        self._pintar_estado(iid)
        return value

    def item(self, iid: str, tags=None, **_kw):
        tarjeta = self._tarjetas.get(iid)
        if tarjeta is None:
            return {}
        if tags is not None:
            tarjeta["tags"] = list(tags)
            self._pintar_estado(iid)
        return {"tags": tuple(tarjeta["tags"])}

    def heading(self, *_a, **_kw) -> None:
        """Sin encabezados de columna: ya no es una tabla."""

    def column(self, *_a, **_kw) -> None:
        """Idem `heading` — se acepta para no tocar el código que la arma."""

    # ── pintado ──────────────────────────────────────────────────────────

    def _color_decision(self, tarjeta: dict) -> tuple[str, str] | None:
        for tag in tarjeta["tags"]:
            if tag in self._colores_tag:
                return (self._colores_tag[tag], self._colores_tag[tag])
        return None

    def _pintar_borde(self, nombre: str) -> None:
        """Tres estados visuales distintos, a propósito:

        · LA seleccionada (la del panel de detalle): fondo azul claro.
        · En el lote (marcada para acción en bloque): borde ámbar grueso.
        · Ninguna de las dos: borde del color de su decisión, o neutro.

        La seleccionada puede además estar en el lote — entonces lleva el
        fondo azul Y el borde ámbar, que es exactamente lo que quiere decir:
        "es la que estoy mirando y también entra en el lote".
        """
        tarjeta = self._tarjetas[nombre]
        en_lote = len(self._lote) > 1 and nombre in self._lote
        if nombre == self._sel:
            tarjeta["marco"].configure(
                border_color=PAR_ALERTA if en_lote else PAR_PRIMARIO,
                border_width=3 if en_lote else 2,
                fg_color=PAR_SELECCION)
            return
        color = self._color_decision(tarjeta)
        if en_lote:
            tarjeta["marco"].configure(border_color=PAR_ALERTA, border_width=3,
                                       fg_color=PAR_PANEL_SUAVE)
            return
        tarjeta["marco"].configure(border_color=color or PAR_PANEL_SUAVE,
                                   border_width=2, fg_color=PAR_PANEL_SUAVE)

    def _pintar_estado(self, nombre: str) -> None:
        """Los 4 datos que antes eran columnas de texto, ahora como color:
        borde verde/rojo según la decisión, y una línea ámbar de avisos con
        falta% + mordida + sombra."""
        tarjeta = self._tarjetas[nombre]
        falta, mordida, sombra, decision = (list(tarjeta["valores"]) + ["", "", "", ""])[:4]

        avisos = [f"falta {falta}%"] if falta not in ("", None) else []
        if str(mordida).lower() in ("sí", "si", "true", "1"):
            avisos.append("mordida")
        if str(sombra).lower() in ("sí", "si", "true", "1"):
            avisos.append("sombra")
        hay_alerta = len(avisos) > 1
        tarjeta["avisos"].configure(text="  ·  ".join(avisos),
                                    text_color=PAR_ALERTA if hay_alerta else PAR_TEXTO_SUAVE)

        texto_dec = str(decision or "")
        color = self._color_decision(tarjeta)
        tarjeta["dec"].configure(
            text={"aprobado": "✓ aprobado", "descartado": "✗ descartado"}.get(texto_dec, texto_dec),
            text_color=color or PAR_TEXTO_SUAVE)
        self._pintar_borde(nombre)

    # ── miniaturas por lotes ─────────────────────────────────────────────

    def _programar_lote(self) -> None:
        if self._tarea_lote is not None:
            try:
                self.after_cancel(self._tarea_lote)
            except (ValueError, tk.TclError):
                # after_cancel de una tarea ya disparada/ventana cerrada: nada que cancelar.
                pass
        self._tarea_lote = self.after(40, self._cargar_lote)

    def _cargar_lote(self, cuantas: int = 10) -> None:
        self._tarea_lote = None
        hechas = 0
        while self._pendientes and hechas < cuantas:
            nombre = self._pendientes.pop(0)
            tarjeta = self._tarjetas.get(nombre)
            if tarjeta is None:
                continue
            im = None
            png = self._proveedor_png(nombre)
            if png and Path(png).exists():
                try:
                    im = reparar.dibujar_contorno(Path(png))
                    if im.mode != "RGB":
                        im = self._componer(im)
                except Exception:  # noqa: BLE001
                    try:
                        im = self._componer(Image.open(png))
                    except Exception:  # noqa: BLE001
                        _logger_gui().exception("no se pudo cargar la miniatura del recorte %s", png)
                        im = None
            if im is None:
                im = Image.new("RGB", (self.LADO_MINIATURA, self.LADO_MINIATURA),
                               solido(PAR_VISOR_BG))
            im = im.copy()
            im.thumbnail((self.LADO_MINIATURA - 6, self.LADO_MINIATURA - 6))
            foto = ImageTk.PhotoImage(im)
            self._fotos[nombre] = foto  # referencia viva — si no, Tk la descarta
            try:
                tarjeta["img"].configure(image=foto)
            except tk.TclError:
                return  # tarjeta destruida entretanto
            tarjeta["cargada"] = True
            hechas += 1
        if self._pendientes:
            self._tarea_lote = self.after(10, self._cargar_lote)
