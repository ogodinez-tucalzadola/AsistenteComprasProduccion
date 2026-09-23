"""
De dónde salen las fotos: elegir el origen (carpeta, Excel, PDF o .msg),
correr la extracción, y la pantalla donde el comprador elige cuáles de las
fotos extraídas entran al lote.

División 2026-09-23, Fase 4: `HerramientaUnica` era UNA clase de ~7,200 líneas
con los 7 pasos del asistente adentro. Acá vive la BOCA del flujo: todo lo que
pasa antes de que exista un solo recorte. Son los pasos 1 y 2 del asistente —
señalar el origen y depurar lo que ese origen trajo.

Se agrupó así porque los cuatro orígenes (carpeta suelta, catálogo en Excel,
catálogo en PDF, correo .msg) son variantes de la MISMA operación y comparten
el mismo final: `_extraer_generico_y_cargar` y la revisión previa. Tenerlos
juntos es lo que deja ver de un vistazo que un origen nuevo no necesita una
pantalla nueva.

Es un mixin, no una jerarquía con comportamiento propio: todo se combina en la
MISMA instancia, así que `self.<lo que sea>` sigue funcionando igual que antes.
Los cuerpos se movieron LITERALMENTE, sin cambiar una línea.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import control_extraccion

from util_ventana import _centrar_en_ventana_principal, _logger_gui
from tema import (
    PAR_BORDE, PAR_PANEL, PAR_TEXTO_SUAVE,
    COLOR_BORDE, COLOR_MAL, COLOR_PANEL, COLOR_TEXTO, COLOR_VISOR_BG,
    FUENTE_CHICA,
)
from widgets_puente import Barra, Boton, Etiqueta, Marco, Separador, Tarjeta


class _MixinExtraccion:
    """Elegir el origen de las fotos, extraerlas y depurar lo extraído.

    Ver el docstring del módulo: agrupamiento por "lo que pasa ANTES de que
    exista un recorte", con los cuatro orígenes como variantes de lo mismo.
    """

    # ── elegir carpetas ──────────────────────────────────────────────────

    def _elegir_entrada(self) -> None:
        d = filedialog.askdirectory(title="Carpeta de entrada")
        if d:
            self._entrada_archivos = None
            # Origen de imágenes directo: no hay fotos extraídas que elegir, así
            # que el paso 2 no aplica y el asistente lo va a saltar solo.
            self._archivos_revision = []
            self._exclusiones_revision = set()
            self.v_entrada.set(d)
            # Guardar la carpeta de entrada en los metadatos del lote
            self._guardar_marca_lote(entrada_dir=d)

    def _elegir_archivos(self) -> None:
        archivos = filedialog.askopenfilenames(
            title="Láminas a procesar",
            filetypes=[("Imágenes", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                       ("Todos los archivos", "*.*")])
        if archivos:
            self._entrada_archivos = [Path(a) for a in archivos]
            # Igual que la carpeta de imágenes: el comprador ya eligió a mano
            # cuáles son, el paso 2 no tiene nada que agregar.
            self._archivos_revision = []
            self._exclusiones_revision = set()
            self.v_entrada.set(f"{len(archivos)} archivo(s) seleccionado(s)")

    def _elegir_carpeta_excel(self) -> None:
        """Saca las fotos incrustadas de todos los .xlsx de una carpeta
        (identificando el código de producto de cada una — ver
        `extraer_excel.py`) y las deja como entrada lista para procesar."""
        carpeta_excel = filedialog.askdirectory(title="Carpeta con los archivos Excel")
        if not carpeta_excel:
            return
        carpeta_excel = Path(carpeta_excel)
        self._extraer_excel_y_cargar(sorted(carpeta_excel.glob("*.xlsx")),
                                      carpeta_excel / "_fotos_extraidas")

    def _elegir_archivos_excel(self) -> None:
        """Igual que `_elegir_carpeta_excel`, pero para uno o varios .xlsx
        sueltos elegidos a mano, no toda una carpeta."""
        archivos = filedialog.askopenfilenames(
            title="Archivo(s) Excel a extraer",
            filetypes=[("Excel", "*.xlsx"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_excel_y_cargar([Path(a) for a in archivos],
                                      primero.parent / "_fotos_extraidas")

    def _elegir_carpeta_pdf(self) -> None:
        """Igual que `_elegir_carpeta_excel` pero para catálogos/preventas en
        PDF (ver `extraer_pdf.py`): una página por producto, código como
        primer texto de la página y la foto más grande de esa página."""
        carpeta_pdf = filedialog.askdirectory(title="Carpeta con los archivos PDF")
        if not carpeta_pdf:
            return
        carpeta_pdf = Path(carpeta_pdf)
        self._extraer_generico_y_cargar(sorted(carpeta_pdf.glob("*.pdf")),
                                         carpeta_pdf / "_fotos_extraidas",
                                         modulo="extraer_pdf", etiqueta="PDF")

    def _elegir_archivos_pdf(self) -> None:
        """Igual que `_elegir_archivos_excel`, pero para uno o varios .pdf
        sueltos elegidos a mano."""
        archivos = filedialog.askopenfilenames(
            title="Archivo(s) PDF a extraer",
            filetypes=[("PDF", "*.pdf"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_generico_y_cargar([Path(a) for a in archivos],
                                         primero.parent / "_fotos_extraidas",
                                         modulo="extraer_pdf", etiqueta="PDF")

    def _elegir_carpeta_msg(self) -> None:
        """Igual que `_elegir_carpeta_excel`/`_elegir_carpeta_pdf` pero para
        correos de Outlook guardados como .msg (ver `extraer_msg.py`): cada
        adjunto de imagen es una foto de producto, nombrada con su propio
        código."""
        carpeta_msg = filedialog.askdirectory(title="Carpeta con los correos (.msg)")
        if not carpeta_msg:
            return
        carpeta_msg = Path(carpeta_msg)
        self._extraer_generico_y_cargar(sorted(carpeta_msg.glob("*.msg")),
                                         carpeta_msg / "_fotos_extraidas",
                                         modulo="extraer_msg", etiqueta="correo")

    def _elegir_archivos_msg(self) -> None:
        """Igual que `_elegir_archivos_excel`, pero para uno o varios .msg
        sueltos elegidos a mano."""
        archivos = filedialog.askopenfilenames(
            title="Correo(s) (.msg) a extraer",
            filetypes=[("Correo Outlook", "*.msg"), ("Todos los archivos", "*.*")])
        if not archivos:
            return
        primero = Path(archivos[0])
        self._extraer_generico_y_cargar([Path(a) for a in archivos],
                                         primero.parent / "_fotos_extraidas",
                                         modulo="extraer_msg", etiqueta="correo")

    def _extraer_excel_y_cargar(self, archivos_excel: list[Path], destino: Path) -> None:
        self._extraer_generico_y_cargar(archivos_excel, destino,
                                         modulo="extraer_excel", etiqueta="Excel")

    def _extraer_generico_y_cargar(self, archivos: list[Path], destino: Path,
                                    modulo: str, etiqueta: str) -> None:
        # Ventana con avance real y CONTROLES MIENTRAS extrae — la extracción
        # corre en un hilo aparte para que la ventana pueda seguir respondiendo
        # (si corriera en el hilo principal, bloquearía la app entera y los
        # botones no se podrían ni tocar).
        #
        # Pedido del usuario (2026-09-08): antes esto era una espera ciega de
        # principio a fin — una barra que giraba sin decir nada y sin forma de
        # pausar, cortar ni seguir con lo que ya se había sacado. Ahora hay
        # tres acciones distintas, porque son tres intenciones distintas:
        #   ⏸ Pausar / ▶ Reanudar  → atender otra cosa y volver.
        #   ✓ Continuar con lo cargado → ya alcanza con lo extraído; seguir el
        #     flujo normal (revisión de fotos) con el resultado parcial.
        #   ✗ Detener y cancelar → cortar y no avanzar con nada; las fotos
        #     parciales de ESTA extracción se borran (ver `_cancelar_extraccion`).
        # El corte es cooperativo (ver control_extraccion.py): surte efecto en
        # el próximo punto de control, no en el instante del clic.
        control = control_extraccion.ControlExtraccion(total_archivos=len(archivos))

        espera = ctk.CTkToplevel(self)
        espera.title("Extrayendo…")
        _centrar_en_ventana_principal(self, espera, 520, 230)
        espera.resizable(False, False)
        espera.configure(fg_color=PAR_PANEL)
        espera.transient(self)
        espera.protocol("WM_DELETE_WINDOW", lambda: None)  # no cerrar a medias
        # SIN grab_set(): un grab que por cualquier motivo no se libere bien
        # deja TODA la ventana principal sin responder a nada — mejor que este
        # aviso sea no-modal (el usuario puede seguir viendo la app detrás)
        # que arriesgar que un clic en "Procesar" quede sin efecto después.
        Etiqueta(espera, text=f"Extrayendo fotos de {len(archivos)} archivo(s) {etiqueta}",
                 style="SubtituloPanel.TLabel", padding=(16, 16, 16, 4)).pack(anchor="w", fill="x")

        v_detalle = tk.StringVar(value="Abriendo el primer archivo…")
        Etiqueta(espera, textvariable=v_detalle, style="SuavePanel.TLabel",
                 padding=(16, 0, 16, 6), wraplength=480,
                 justify="left").pack(anchor="w", fill="x")

        # Barra con avance REAL por archivo cuando hay más de uno (es el único
        # progreso honesto que se puede dar: adentro de un archivo grande no se
        # sabe de antemano cuántas imágenes trae). Con un solo archivo se deja
        # indeterminada en vez de inventar un porcentaje.
        multiples = len(archivos) > 1
        barra = Barra(espera, mode="determinate" if multiples else "indeterminate",
                      maximum=max(len(archivos), 1))
        barra.pack(fill="x", padx=16, pady=(2, 10))
        if not multiples:
            barra.start(12)

        fila_ctrl = Marco(espera, padding=(16, 0, 16, 14))
        fila_ctrl.pack(fill="x")

        estado = {"pausado": False, "terminado": False}

        def alternar_pausa() -> None:
            if estado["terminado"]:
                return
            if estado["pausado"]:
                control.reanudar()
                estado["pausado"] = False
                btn_pausa.configure(text="⏸ Pausar")
                if not multiples:
                    barra.start(12)
            else:
                control.pausar()
                estado["pausado"] = True
                btn_pausa.configure(text="▶ Reanudar")
                if not multiples:
                    barra.stop()
                v_detalle.set("En pausa. La extracción se detiene al terminar la "
                              "imagen que estaba escribiendo.")

        def pedir_corte(conservar: bool) -> None:
            if estado["terminado"]:
                return
            control.cancelar(conservar=conservar)
            estado["pausado"] = False
            btn_pausa.configure(state="disabled")
            btn_seguir.configure(state="disabled")
            btn_cancelar.configure(state="disabled")
            v_detalle.set("Cerrando el archivo en curso… un momento.")

        btn_pausa = Boton(fila_ctrl, text="⏸ Pausar", command=alternar_pausa)
        btn_pausa.pack(side="left")
        btn_seguir = Boton(fila_ctrl, text="✓ Continuar con lo cargado",
                           style="Primario.TButton",
                           command=lambda: pedir_corte(True))
        btn_seguir.pack(side="left", padx=(8, 0))
        btn_cancelar = Boton(fila_ctrl, text="✗ Detener y cancelar",
                             style="Peligro.TButton",
                             command=lambda: pedir_corte(False))
        btn_cancelar.pack(side="right")

        _centrar_en_ventana_principal(self, espera, 520, 230)

        resultado: dict = {}

        def trabajar() -> None:
            try:
                import importlib
                mod = importlib.import_module(modulo)
                resultado["resumen"] = mod.extraer_lista(archivos, destino, control=control)
            except Exception as exc:  # noqa: BLE001
                _logger_gui().exception("fallo la extraccion con el modulo %s", modulo)
                resultado["error"] = str(exc)

        hilo = threading.Thread(target=trabajar, daemon=True)
        hilo.start()

        def revisar() -> None:
            if hilo.is_alive():
                if not estado["pausado"] and not control.cancelado:
                    nombre, i, total = control.estado_texto()
                    if nombre:
                        v_detalle.set(f"Archivo {i} de {total}: {nombre}"
                                      + (f"  ·  {control.imagenes_hasta_ahora} foto(s) "
                                         f"hasta ahora" if control.imagenes_hasta_ahora else ""))
                        if multiples:
                            barra.configure(value=i - 1)
                self.after(150, revisar)
                return
            estado["terminado"] = True
            barra.stop()
            espera.destroy()
            self._mostrar_resultado_extraccion(resultado, destino, etiqueta)

        self.after(150, revisar)

    def _mostrar_resultado_extraccion(self, resultado: dict, destino: Path,
                                       etiqueta: str = "Excel") -> None:
        if "error" in resultado:
            messagebox.showerror(f"Extraer de {etiqueta}", f"No se pudo extraer: {resultado['error']}")
            return
        resumen = resultado["resumen"]

        # "✗ Detener y cancelar": no se avanza con nada, y las fotos que la
        # extracción ya había escrito en disco se borran. Se borran SOLO las
        # rutas de ESTA extracción (`resumen["rutas"]`), nunca por glob de la
        # carpeta: el destino puede tener fotos de una extracción anterior y
        # barrerlas sería destruir trabajo que el usuario no pidió tirar.
        if resumen.get("cancelado") and not resumen.get("conservar_parcial", True):
            self._descartar_extraccion_cancelada(resumen, destino, etiqueta)
            return

        errores = {n: v["error"] for n, v in resumen["archivos"].items() if "error" in v}
        cancelada = bool(resumen.get("cancelado"))
        mensaje = (f"{resumen['total_imagenes']} fotos extraídas de "
                   f"{len(resumen['archivos'])} archivo(s) {etiqueta}.")
        if cancelada:
            pendientes = resumen.get("archivos_pendientes", 0)
            mensaje += (f"\n\nSe cortó la extracción a pedido tuyo"
                        + (f": quedaron {pendientes} archivo(s) sin procesar."
                           if pendientes else "."))
        if resumen["total_sin_codigo"]:
            mensaje += (f"\n\n{resumen['total_sin_codigo']} imagen(es) no se pudieron "
                        f"asociar a un código de producto — "
                        f"suelen ser logos u otras imágenes fuera de las páginas/filas de producto.")
        if errores:
            mensaje += "\n\nArchivos con error:\n" + "\n".join(
                f"- {n}: {e}" for n, e in errores.items())

        aviso = ctk.CTkToplevel(self)
        titulo = "Extracción detenida" if cancelada else "Extracción completa"
        aviso.title(titulo)
        aviso.configure(fg_color=PAR_PANEL)
        aviso.transient(self)
        Etiqueta(aviso, text=("⏹ " if cancelada else "✓ ") + titulo,
                 style="SubtituloPanel.TLabel",
                 padding=(16, 16, 16, 4)).pack(anchor="w", fill="x")
        Etiqueta(aviso, text=mensaje, style="Panel.TLabel", padding=(16, 0, 16, 8),
                 wraplength=440, justify="left").pack(anchor="w", fill="x")
        Etiqueta(aviso, text=f"Guardadas en:\n{destino}", style="SuavePanel.TLabel",
                 padding=(16, 0, 16, 12), wraplength=440, justify="left").pack(anchor="w", fill="x")
        fila_botones = Marco(aviso, padding=(16, 0, 16, 16))
        fila_botones.pack(fill="x")
        Boton(fila_botones, text="Abrir carpeta",
              command=lambda: os.startfile(str(destino))).pack(side="left")
        Boton(fila_botones, text="Cerrar", style="Primario.TButton",
              command=aviso.destroy).pack(side="right")
        aviso.update_idletasks()
        _centrar_en_ventana_principal(self, aviso, aviso.winfo_width(), aviso.winfo_height())

        if resumen["total_imagenes"] == 0:
            return
        # Solo lo que se extrajo AHORA (`resumen["rutas"]`) — no releer toda la
        # carpeta de destino, que puede tener fotos de una extracción anterior
        # (mismo Excel reprocesado, u otro Excel de la misma carpeta) y
        # terminar mostrando/procesando algo que no es lo que se acaba de elegir.
        archivos = [Path(r) for r in resumen["rutas"]]
        self._mostrar_revision_extraccion(archivos)

    def _descartar_extraccion_cancelada(self, resumen: dict, destino: Path,
                                         etiqueta: str) -> None:
        """"✗ Detener y cancelar": deshacer la extracción parcial y NO avanzar.

        Las fotos ya escritas en disco se borran porque son producto de una
        extracción que el usuario decidió tirar: dejarlas ahí haría que la
        próxima extracción a la misma carpeta las mezclara con las nuevas
        (mismo bug que ya se cuida al no releer el destino con glob). Se borran
        una por una, solo las de esta corrida; si alguna no se puede borrar
        (antivirus, archivo tomado), se dice cuántas quedaron en vez de fallar
        en silencio."""
        rutas = [Path(r) for r in resumen.get("rutas", [])]
        borradas, fallidas = 0, 0
        for ruta in rutas:
            try:
                if ruta.exists():
                    ruta.unlink()
                borradas += 1
            except OSError:
                _logger_gui().exception("no se pudo borrar la foto parcial %s", ruta)
                fallidas += 1
        self.v_log.set(f"Extracción de {etiqueta} cancelada: se descartaron "
                       f"{borradas} foto(s) parciales.")
        mensaje = (f"No se avanzó con nada.\n\n"
                   f"Se habían extraído {len(rutas)} foto(s) antes de detener; "
                   f"se descartaron {borradas}.")
        if fallidas:
            mensaje += (f"\n\n{fallidas} no se pudieron borrar (pueden estar "
                        f"abiertas en otro programa). Quedaron en:\n{destino}")
        messagebox.showinfo("Extracción cancelada", mensaje)

    # ── revisión previa de fotos extraídas de Excel ─────────────────────

    def _mostrar_revision_extraccion(self, archivos: list[Path],
                                      exclusiones: set[str] | None = None) -> None:
        """PASO 2 del asistente, «Elegir qué limpiar»: antes de limpiar nada,
        mostrar TODAS las fotos extraídas en una grilla de miniaturas, con
        cada una marcada como incluida por defecto y la posibilidad de
        descartar (clic) las que sean logos u otra cosa que no es calzado.
        Al continuar se arma `self._entrada_archivos` y el asistente pasa al
        paso 3, donde se lanza la limpieza.

        `exclusiones` permite reconstruir la pantalla tal como quedó cuando se
        vuelve a ella con «← Atrás» (si no, volver perdía lo descartado)."""
        self._ocultar_resumen()
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None

        self._ocultar_pantallas_base()
        self._archivos_revision = archivos
        self._exclusiones_revision = set(exclusiones or ())
        self._fotos_revision = []
        self._marcos_revision = {}

        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._revision_frame = marco
        self._sincronizar_cabecera()  # cabecera compacta: la grilla necesita el alto

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        fila_titulo = Marco(interior)
        fila_titulo.pack(fill="x")
        Etiqueta(fila_titulo, text=f"Elegir qué limpiar — {len(archivos)} foto(s) extraídas",
                 style="Titulo.TLabel").pack(side="left")
        Boton(fila_titulo, text="Cancelar", command=self._cerrar_revision_extraccion).pack(side="right")

        Etiqueta(interior, style="SuavePanel.TLabel", wraplength=1100,
                 text="Hacé clic en una foto para descartarla (por ejemplo, logos u otras "
                      "imágenes que no son calzado). Por defecto todas quedan incluidas."
                 ).pack(anchor="w", fill="x", pady=(4, 10))

        # El botón de abajo y el separador se empaquetan ANTES que el área con
        # scroll (con `side="bottom"`), para que siempre reserven su espacio
        # primero — con cientos/miles de fotos, si el área con scroll se
        # empaqueta primero puede terminar acaparando toda la ventana y dejar
        # el botón invisible o fuera de la vista.
        fila_inferior = Marco(interior)
        fila_inferior.pack(side="bottom", fill="x", pady=(10, 0))
        Separador(interior).pack(side="bottom", fill="x", pady=(10, 0))

        # `CTkScrollableFrame` reemplaza el Canvas + Scrollbar + `<Configure>`
        # de antes; de paso trae la rueda del mouse funcionando (era un item
        # pendiente: en la versión con Canvas no scrolleaba con la rueda).
        grilla = ctk.CTkScrollableFrame(interior, fg_color="transparent",
                                        scrollbar_button_color=PAR_BORDE,
                                        scrollbar_button_hover_color=PAR_TEXTO_SUAVE)
        grilla.pack(side="top", fill="both", expand=True)

        # Mismo criterio que la grilla de "Elegir qué limpiar": foto más grande
        # y tantas columnas como quepan en el ancho real, no un número fijo.
        self._cols_grilla.pop("revision", None)
        cols_ini = max(1, (max(self.winfo_width() - 90, 300))
                       // (self.ANCHO_CELDA_REVISION + self._SEPARACION_CELDA))
        for idx, path in enumerate(archivos):
            fila_i, col_i = divmod(idx, cols_ini)
            celda = tk.Frame(grilla, background=COLOR_PANEL, highlightthickness=2,
                             highlightbackground=COLOR_BORDE, cursor="hand2",
                             width=self.ANCHO_CELDA_REVISION,
                             height=self.ALTO_CELDA_REVISION)
            celda.grid(row=fila_i, column=col_i, padx=6, pady=6)
            celda.grid_propagate(False)

            lbl_img = tk.Label(celda, background=COLOR_VISOR_BG, cursor="hand2")
            lbl_img.pack(fill="both", expand=True, padx=6, pady=(6, 2))
            try:
                im = Image.open(path)
                try:
                    # decodificar ya reducido cuando el formato lo permite (jpg) —
                    # con cientos de fotos, abrir a tamaño completo sería lento.
                    im.draft("RGB", (self.MINIATURA_REVISION, self.MINIATURA_REVISION))
                except Exception:  # noqa: BLE001
                    # draft() es solo una optimizacion de lectura: si no aplica, se sigue igual.
                    pass
                im = im.convert("RGB")
                im.thumbnail((self.MINIATURA_REVISION, self.MINIATURA_REVISION))
            except Exception:  # noqa: BLE001
                _logger_gui().exception("no se pudo abrir la miniatura de revision %s", path)
                im = Image.new("RGB", (self.MINIATURA_REVISION, self.MINIATURA_REVISION),
                               COLOR_VISOR_BG)
            foto = ImageTk.PhotoImage(im)
            self._fotos_revision.append(foto)  # referencia viva — si no, Tk la descarta
            lbl_img.configure(image=foto)

            lbl_nombre = tk.Label(celda, text=path.stem, bg=COLOR_PANEL, fg=COLOR_TEXTO,
                                  font=FUENTE_CHICA,
                                  wraplength=self.ANCHO_CELDA_REVISION - 22,
                                  justify="center")
            lbl_nombre.pack(fill="x", padx=4, pady=(0, 6))

            clave = str(path)
            if clave in self._exclusiones_revision:
                celda.configure(highlightbackground=COLOR_MAL, highlightthickness=3)
            self._marcos_revision[clave] = celda
            for widget in (celda, lbl_img, lbl_nombre):
                widget.bind("<Button-1>", lambda _e, k=clave: self._alternar_exclusion_revision(k))

        Etiqueta(fila_inferior, textvariable=self.v_estado_revision,
                 style="SuavePanel.TLabel").pack(side="left")
        self.btn_procesar_revision = Boton(fila_inferior, text="", style="Primario.TButton",
                                           command=self._confirmar_seleccion_revision)
        self.btn_procesar_revision.pack(side="right")
        self._actualizar_revision_estado()
        # Esta pantalla ES el paso 2: fija el paso acá (y no en quien la llama)
        # porque se llega a ella por dos caminos — al terminar la extracción, y
        # volviendo con «← Atrás» desde el paso 3.
        self._actualizar_paso(2)

        def acomodar(_evento=None) -> None:
            if self._revision_frame is None:
                return
            self._acomodar_grilla(
                "revision", grilla,
                lambda: [self._marcos_revision[str(p)] for p in self._archivos_revision
                         if str(p) in self._marcos_revision],
                self.ANCHO_CELDA_REVISION)

        grilla.bind("<Configure>", acomodar)
        self.after_idle(acomodar)

    def _alternar_exclusion_revision(self, clave: str) -> None:
        celda = self._marcos_revision.get(clave)
        if celda is None:
            return
        if clave in self._exclusiones_revision:
            self._exclusiones_revision.discard(clave)
            celda.configure(highlightbackground=COLOR_BORDE, highlightthickness=2)
        else:
            self._exclusiones_revision.add(clave)
            celda.configure(highlightbackground=COLOR_MAL, highlightthickness=3)
        self._actualizar_revision_estado()

    def _actualizar_revision_estado(self) -> None:
        total = len(self._archivos_revision)
        excluidas = len(self._exclusiones_revision)
        incluidas = total - excluidas
        self.v_estado_revision.set(f"{incluidas} de {total} seleccionadas ({excluidas} descartadas)")
        self.btn_procesar_revision.configure(text=f"Continuar con {incluidas} foto(s)  →")
        self.btn_procesar_revision.state(["disabled"] if incluidas == 0 else ["!disabled"])

    def _cerrar_revision_extraccion(self) -> None:
        """«Cancelar» en el paso 2: se descarta la selección extraída y se
        vuelve al paso 1 a elegir otro origen (quedarse en el paso 3 con una
        entrada que el usuario acaba de cancelar sería mentir sobre el estado)."""
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
        self._archivos_revision = []
        self._exclusiones_revision = set()
        self._ir_a_paso(1)

    def _confirmar_seleccion_revision(self) -> None:
        """Cierra el paso 2 y pasa al 3. NO arranca la limpieza: lanzarla es
        justo lo que hace el paso 3, con su propio botón y sus controles de
        pausar/continuar/detener."""
        incluidas = [p for p in self._archivos_revision if str(p) not in self._exclusiones_revision]
        if not incluidas:
            messagebox.showwarning("Elegir qué limpiar",
                                   "No quedó ninguna foto seleccionada.")
            return
        self._entrada_archivos = incluidas
        self.v_entrada.set(f"{len(incluidas)} foto(s) seleccionadas (de {len(self._archivos_revision)} extraídas)")
        # Guardar la lista de archivos seleccionados en los metadatos del lote,
        # para poder restaurarla si se retoma este lote después.
        self._guardar_marca_lote(entrada_archivos=[str(p) for p in incluidas])
        # FASE 1 multi-proveedor: las fotos realmente elegidas son el origen
        # definitivo de ESTE proveedor del lote (es lo que después permite
        # saber qué recortes son suyos y qué recortes son del otro).
        self._sincronizar_slot_actual()
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
        self._ir_a_paso(3)
