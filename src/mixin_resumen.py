"""
Resumen final del lote: la pantalla de cierre, la exportación y el visor por
calzado.

División 2026-09-23, Fase 4: `HerramientaUnica` era UNA clase de ~7,200 líneas
que hacía los 7 pasos del asistente. Acá vive el ÚLTIMO tramo del camino: lo
que el comprador ve cuando el lote ya está decidido (la lámina original con
sus recortes al lado), lo que se lleva (exportar a la carpeta de salida +
actualizar el índice de fuente) y el visor individual que se abre al tocar una
miniatura de ese resumen.

Se separó de la revisión porque son dos momentos distintos del trabajo: la
revisión DECIDE foto por foto (y puede corregir), el resumen solo MUESTRA lo
ya decidido y lo entrega. Comparten la instancia, así que `self.<lo que sea>`
sigue funcionando igual que cuando todo esto era un solo bloque de texto —
esto es un mixin, no una jerarquía con comportamiento propio: los cuerpos de
los métodos se movieron LITERALMENTE, sin cambiar una línea.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import decisiones
import indice_fuente
import reparar

from util_ventana import _centrar_en_ventana_principal, _logger_gui, _traer_al_frente, reloj
from tema import (
    PAR_BORDE, PAR_FONDO, PAR_TEXTO_SUAVE, PAR_VISOR_BG, RADIO_PANEL,
    COLOR_MAL, COLOR_OK, COLOR_PANEL, COLOR_TEXTO_SUAVE, COLOR_VISOR_BG,
    FUENTE_CHICA,
)
from widgets_puente import Barra, Boton, Etiqueta, Marco, Separador, Tarjeta

# Mismo bloque que en `gui_profesional_ctk.py`: sin pywin32 instalado, el
# visor ofrece "Guardar como…" en vez de copiar al portapapeles.
try:
    import io as _io

    import win32clipboard  # type: ignore
    import win32con  # type: ignore
    _CLIPBOARD_DISPONIBLE = True
except ImportError:
    _CLIPBOARD_DISPONIBLE = False


class _MixinResumen:
    """Pantalla de resumen final, exportación y visor de recorte individual.

    Ver el docstring del módulo: agrupamiento por MOMENTO del flujo (lo ya
    decidido se muestra y se entrega), no por tecnología.
    """

    # ── resumen final del lote ───────────────────────────────────────────

    def _ocultar_resumen(self) -> None:
        if self._resumen_frame is not None:
            self._resumen_frame.destroy()
            self._resumen_frame = None
            self._mostrar_cuerpo()
            self._actualizar_paso(3)

    def _asegurar_cuerpo_visible(self) -> None:
        """Cierra CUALQUIER pantalla de reemplazo (revisión de extracción,
        selección de limpieza, resumen final) y vuelve a mostrar `self.cuerpo`.

        Bug real corregido (2026-09-03): cada pantalla de reemplazo se
        cerraba por su cuenta (o ni eso) sin garantizar que `self.cuerpo`
        quedara re-empaquetado -- eso rompía `_procesar()` (que hace
        `self.monitor.pack(before=self.cuerpo)`, y falla con TclError si
        `self.cuerpo` no está empaquetado) y dejaba la ventana en blanco
        después de "Limpiar caché" u "Omitir limpieza" cuando la pantalla
        siguiente no tenía nada que mostrar. Llamar esto SIEMPRE antes de
        volver a mostrar el cuerpo principal, sin importar cuál pantalla
        estaba abierta."""
        habia_alguna = False
        if self._resumen_frame is not None:
            self._resumen_frame.destroy()
            self._resumen_frame = None
            habia_alguna = True
        if self._revision_frame is not None:
            self._revision_frame.destroy()
            self._revision_frame = None
            habia_alguna = True
        if getattr(self, "_candidatos_frame", None) is not None:
            self._candidatos_frame.destroy()
            self._candidatos_frame = None
            habia_alguna = True
        if getattr(self, "_vector_frame", None) is not None:
            self._vector_frame.destroy()
            self._vector_frame = None
            # Los widgets del bloque de progreso vivían DENTRO de esa tarjeta:
            # dejar las referencias colgando haría que `_avance_compras` y
            # compañía escriban sobre widgets destruidos (lo atrapan, pero es
            # mejor no depender de eso) y que la consola del paso 5 siga
            # aceptando líneas de un cálculo cuya pantalla ya no existe.
            self._marco_progreso_compras = None
            self._sep_progreso_compras = None
            self.barra_compras = None
            self.txt_consola_vector = None
            self.btn_calcular_vector = None
            habia_alguna = True
        if habia_alguna:
            self._mostrar_cuerpo()

    def _mostrar_resumen_final(self) -> None:
        """Al terminar todo el lote: la lámina original + sus recortes
        finales, la ruta de salida con acceso directo al Explorador, y el
        botón de exportar bien visible."""
        if not self._salida or not self._recortes:
            return

        # Reloj de arena: armar esta pantalla recorre TODOS los recortes del
        # lote (puede ser un lote grande) antes de que se vea algo -- sin
        # señal, se veía igual que un programa colgado.
        self.configure(cursor="watch")
        self.after(400, lambda: self.configure(cursor=""))
        self._asegurar_cuerpo_visible()
        self._ocultar_pantallas_base()
        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._resumen_frame = marco
        self._actualizar_paso(4)

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        fila_titulo = Marco(interior)
        fila_titulo.pack(fill="x")
        Etiqueta(fila_titulo, text="Confirmar y enviar", style="Titulo.TLabel").pack(side="left")
        # "Atrás" en el pie ya hace exactamente esto (_paso_atras → _ocultar_resumen
        # cuando el paso es 4) — un botón duplicado acá solo confundía.

        conteo = decisiones.resumen(self._salida, len(self._recortes))
        resumen_txt = (f"{len(self._recortes)} calzados procesados  ·  "
                       f"{conteo.get('aprobado', 0)} aprobados  ·  "
                       f"{conteo.get('descartado', 0)} descartados  ·  "
                       f"{conteo.get('pendiente', 0)} pendientes de revisar")
        if conteo.get("eliminado"):
            resumen_txt += f"  ·  {conteo['eliminado']} desechados (borrados del disco)"
        Etiqueta(interior, text=resumen_txt, style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(4, 12))

        fila_ruta = Marco(interior)
        fila_ruta.pack(fill="x", pady=(0, 12))
        Etiqueta(fila_ruta, text=f"Carpeta de salida: {self._salida}",
                 style="Panel.TLabel").pack(side="left")
        Boton(fila_ruta, text="Abrir carpeta", command=self._abrir_carpeta_salida).pack(
            side="left", padx=(10, 0))
        # Acá NO va ningún botón de "Enviar a Asistente de Compras": la acción
        # de avanzar vive en el botón "Continuar  →" del pie, el mismo lugar
        # que en todos los otros pasos (pedido del usuario, 2026-09-09). Ese
        # botón sabe en qué estado está el lote: si nunca se envió, envía (y al
        # terminar abre los candidatos solo, vía `cand_fin`); si ya se envió,
        # solo abre la vista.
        Boton(fila_ruta, text="Exportar aprobados…",
              command=self._exportar).pack(side="right", padx=(0, 8))
        self._sincronizar_btn_compras()

        # Línea de estado propia del envío a comparar -- separada de
        # "Estado:" en la barra superior (esa es del PROCESAMIENTO de
        # limpieza) para que el avance de la comparación se vea pegado al
        # botón que la dispara, no lejos en otra barra.
        # Bloque de progreso del envío: NO se muestra mientras no haya un
        # envío en curso (reclamo real del usuario, 2026-09-09: "no entiendo
        # para qué sirve esa barra"). Antes quedaba siempre a la vista, vacía
        # o al 100%, sin rótulo que dijera de qué hablaba. Ahora aparece al
        # tocar el botón, con título explícito, y desaparece al terminar.
        self._marco_progreso_compras = Marco(interior)
        Etiqueta(self._marco_progreso_compras,
                 text="Progreso del envío al Asistente de Compras",
                 style="Panel.TLabel").pack(anchor="w")
        self.v_estado_compras = tk.StringVar(value="")
        Etiqueta(self._marco_progreso_compras, textvariable=self.v_estado_compras,
                 style="SuavePanel.TLabel", wraplength=1100).pack(
            anchor="w", fill="x", pady=(2, 0))
        # Barra propia del envío a calificar. Reemplaza el uso del monitor de
        # láminas (que ya no se muestra en este paso): acá no hay "lámina
        # actual" que reportar, solo cuántos candidatos se vectorizaron.
        self.barra_compras = Barra(self._marco_progreso_compras,
                                   mode="determinate", maximum=100)
        self.barra_compras.pack(fill="x", pady=(4, 0))

        self._sep_progreso_compras = Separador(interior)
        self._sep_progreso_compras.pack(fill="x", pady=(12, 12))
        # Si se vuelve a entrar al paso mientras un envío sigue corriendo en
        # segundo plano, el bloque tiene que reaparecer.
        if getattr(self, "_enviando_compras", False):
            self._mostrar_progreso_compras()

        # Lista con scroll de miniaturas: lámina por lámina, con sus recortes
        # finales al lado. Igual que las otras dos grillas, ahora sobre
        # `CTkScrollableFrame` (con rueda del mouse).
        lista_int = ctk.CTkScrollableFrame(interior, fg_color="transparent",
                                           scrollbar_button_color=PAR_BORDE,
                                           scrollbar_button_hover_color=PAR_TEXTO_SUAVE)
        lista_int.pack(fill="both", expand=True)

        self._fotos_resumen = []
        self._widgets_resumen_recorte = {}
        por_lamina: dict[str, list[tuple[str, dict]]] = {}
        # Una foto DESCARTADA no se muestra más — ni acá ni en ninguna
        # pantalla posterior a donde se tomó esa decisión.
        #
        # Esto era a propósito y estaba mal pensado: la pantalla listaba TODO
        # el lote con una etiqueta de color por decisión, como registro de
        # auditoría. Para el comprador eso significaba seguir viendo, foto por
        # foto, calzados que ya había sacado del catálogo. Reclamo textual del
        # usuario (2026-09-07): "si descarto una imagen, espero no verla de
        # nuevo definitivamente". El registro no se pierde: sigue entero en la
        # bitácora de decisiones, y el CONTEO de descartadas se sigue
        # mostrando en la línea de resumen de arriba — lo que desaparece es la
        # miniatura, que es lo que molestaba.
        for nombre, r in sorted(self._recortes.items()):
            if r.get("estado", {}).get("decision") == decisiones.DESCARTADO:
                continue
            stem = nombre.rsplit("_", 1)[0]
            por_lamina.setdefault(stem, []).append((nombre, r))

        for stem, items in sorted(por_lamina.items()):
            fila = Marco(lista_int, padding=(0, 8))
            fila.pack(fill="x", anchor="w")

            original = self._rutas_pendientes.get(stem) or self._buscar_original_por_stem(stem)
            marco_orig = tk.Frame(fila, background=COLOR_VISOR_BG, width=110, height=110)
            marco_orig.pack(side="left", padx=(0, 10))
            marco_orig.pack_propagate(False)
            lbl_orig = tk.Label(marco_orig, background=COLOR_VISOR_BG)
            lbl_orig.pack(fill="both", expand=True)
            pie_orig = f"{stem}\n(original)"
            if original and Path(original).exists():
                try:
                    im = Image.open(original).convert("RGB")
                    im.thumbnail((104, 104))
                    foto = ImageTk.PhotoImage(im)
                    self._fotos_resumen.append(foto)
                    lbl_orig.configure(image=foto)
                except Exception:  # noqa: BLE001
                    _logger_gui().exception("no se pudo cargar la lamina original en el resumen final")
                # La lámina original también se amplía con un clic, igual que
                # los recortes: es la foto más chica de la fila y la que menos
                # se deja mirar en 104px. Se reusa el mismo visor del paso de
                # revisión (`_mostrar_grande`), no uno nuevo.
                ruta_orig = Path(original)
                marco_orig.configure(cursor="hand2")
                lbl_orig.configure(cursor="hand2")
                for widget in (marco_orig, lbl_orig):
                    widget.bind("<Button-1>",
                                lambda _evt, p=ruta_orig: self._mostrar_imagen_flotante(
                                    p, f"Foto original: {p.name}"))
                pie_orig = f"{stem}\n(original · clic para ampliar)"
            Etiqueta(fila, text=pie_orig, style="SuavePanel.TLabel",
                     justify="center", anchor="center").pack(side="left", padx=(0, 14))

            for nombre, r in items:
                png = r.get("png")
                marco_rec = tk.Frame(fila, background=COLOR_VISOR_BG, width=90, height=90,
                                     cursor="hand2")
                marco_rec.pack(side="left", padx=4)
                marco_rec.pack_propagate(False)
                lbl_rec = tk.Label(marco_rec, background=COLOR_VISOR_BG, cursor="hand2")
                lbl_rec.pack(fill="both", expand=True)
                decision = r.get("estado", {}).get("decision", "pendiente")
                if png and Path(png).exists():
                    try:
                        im = self._componer_sobre_gris(Image.open(png))
                        im.thumbnail((84, 84))
                        foto = ImageTk.PhotoImage(im)
                        self._fotos_resumen.append(foto)
                        lbl_rec.configure(image=foto)
                    except Exception:  # noqa: BLE001
                        _logger_gui().exception("no se pudo cargar la miniatura del recorte en el resumen final")
                color_dec = {"aprobado": COLOR_OK, "descartado": COLOR_MAL}.get(decision, COLOR_TEXTO_SUAVE)
                lbl_dec = tk.Label(fila, text=decision, fg=color_dec, bg=COLOR_PANEL,
                                   font=FUENTE_CHICA)
                lbl_dec.pack(side="left", padx=(0, 10))

                self._widgets_resumen_recorte[nombre] = (lbl_rec, lbl_dec)
                for widget in (marco_rec, lbl_rec):
                    widget.bind("<Button-1>", lambda _evt, n=nombre: self._abrir_visor_recorte(n))

    def _buscar_original_por_stem(self, stem: str) -> Path | None:
        """La lámina de la que salió un recorte, buscada por orden de
        confiabilidad.

        Lo primero que se mira es la ruta que el manifiesto guardó al
        procesar (`origen`), porque es la única que acierta SIEMPRE — también
        cuando el lote vino de una extracción de PDF o Excel (donde las
        láminas quedan en la carpeta `_fotos_extraidas` del extractor, no en
        la carpeta de entrada) y también cuando se retomó un lote viejo (donde
        ni `_entrada_archivos` ni `v_entrada` existen ya). Sin esto, la
        columna de "lámina original" del resumen salía vacía para todo el
        lote, no en casos sueltos."""
        for r in self._recortes.values():
            origen = r.get("_origen_lamina")
            if origen and Path(origen).stem == stem and Path(origen).exists():
                return Path(origen)
        if self._entrada_archivos:
            for c in self._entrada_archivos:
                if c.stem == stem:
                    return c
        if self.v_entrada.get():
            carpeta = Path(self.v_entrada.get())
            if carpeta.is_dir():
                encontrados = list(carpeta.rglob(f"{stem}.*"))
                if encontrados:
                    return encontrados[0]
        # Último recurso: la lámina con las cajas dibujadas que el worker deja
        # en `_pasos/`. No es el original puro, pero vive DENTRO de la carpeta
        # de trabajo, así que está disponible aunque el archivo de entrada ya
        # no exista — mejor que un recuadro gris vacío.
        if self._salida:
            con_cajas = self._salida / "_pasos" / f"{stem}_cajas.jpg"
            if con_cajas.exists():
                return con_cajas
        return None

    def _abrir_carpeta_salida(self) -> None:
        if not self._salida:
            return
        try:
            os.startfile(str(self._salida))  # noqa: S606 - acción pedida explícitamente por el usuario
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Abrir carpeta", str(exc))

    def _exportar(self) -> None:
        if not self._salida:
            return
        if not self._recortes:
            messagebox.showwarning(
                "Exportar",
                "No hay un lote cargado. Carga la carpeta de trabajo antes de exportar.")
            return
        destino = filedialog.askdirectory(title="Exportar aprobados a… (carpeta aparte)")
        if not destino:
            return
        with reloj(self):
            try:
                res = decisiones.exportar_aprobados(
                    self._salida, Path(destino),
                    nombres=set(self._recortes),
                    entrada=self.v_entrada.get().strip() or None)
            except decisiones.ErrorExportacion as exc:
                messagebox.showerror("Exportar: carpeta no válida", str(exc))
                return
            except OSError as exc:
                messagebox.showerror("Exportar", f"No se pudo exportar:\n{exc}")
                return

        detalle = "  ·  ".join(f"{sub}: {n}" for sub, n in res["por_carpeta"].items())
        msg = (f"{res['copiados']} archivo(s) exportado(s) a:\n{res['destino']}\n\n"
               f"{detalle}\n"
               f"{res['aprobados']} recorte(s) aprobado(s) en este lote.")
        if res["omitidos_otros_lotes"]:
            msg += (f"\n\nSe omitieron {res['omitidos_otros_lotes']} aprobado(s) de "
                    "lotes anteriores guardados en esta misma carpeta.")
        if res["faltantes"]:
            msg += f"\n\n{len(res['faltantes'])} archivo(s) aprobado(s) no estaban en disco."
        if res["manifiesto"]:
            msg += f"\n\nRegistro de la exportación:\n{Path(res['manifiesto']).name}"
            self._actualizar_indice_fuente(Path(destino), Path(res["manifiesto"]))
        if res["errores"]:
            messagebox.showerror(
                "Exportar con problemas",
                msg + "\n\nProblemas:\n" + "\n".join(res["errores"][:10]))
        else:
            messagebox.showinfo("Exportar", msg)

    def _raiz_catalogos_probable(self) -> Path | None:
        """De dónde buscar los PDF/Excel originales para el índice de fuente
        (ver `indice_fuente.py`) — no hay una carpeta "de catálogos" fija en
        la app, así que se infiere de lo último que se usó como entrada."""
        if self._entrada_archivos:
            candidato = self._entrada_archivos[0].parent.parent
            if candidato.exists():
                return candidato
        texto = self.v_entrada.get().strip()
        p = Path(texto)
        if p.exists():
            return p if p.is_dir() else p.parent
        if self._salida and self._salida.parent.exists():
            return self._salida.parent
        return None

    def _actualizar_indice_fuente(self, destino: Path, ruta_manifiesto: Path) -> None:
        """Agrega al Excel de índice (dentro de la carpeta exportada) las
        fotos de este export. Las fotos ya se copiaron bien antes de llegar
        acá, así que un fallo aquí no hace fallar la exportación — pero SÍ
        se avisa (antes quedaba en silencio, y el índice se veía "completo"
        cuando en realidad le faltaban filas, ej. por tener el Excel abierto
        en otro programa al momento de exportar)."""
        raiz = self._raiz_catalogos_probable()
        if not raiz:
            messagebox.showwarning(
                "Índice de fuente no actualizado",
                "Las fotos se exportaron bien, pero no pude adivinar dónde están "
                "los catálogos (PDF/Excel/correo) para buscar el origen de cada "
                "una, así que «indice_fuente_imagenes.xlsx» no se actualizó con "
                "las de este lote.\n\nPodés pedirme que lo actualice a mano.")
            return
        try:
            manifiesto = json.loads(ruta_manifiesto.read_text(encoding="utf-8"))
            indice_fuente.actualizar_indice(destino, manifiesto, raiz)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(
                "Índice de fuente no actualizado",
                f"Las fotos se exportaron bien, pero no se pudo actualizar "
                f"«indice_fuente_imagenes.xlsx» con las de este lote:\n\n{exc}\n\n"
                f"Lo más común es que el archivo esté abierto en Excel — "
                f"cerralo y volvé a exportar, o pedime que lo actualice a mano.")

    # ── visor individual por calzado, desde el resumen final ────────────


    def _refrescar_miniatura_resumen(self, nombre: str) -> None:
        """Vuelve a leer el png actual del recorte y actualiza su miniatura y
        etiqueta de decisión en el panel de resumen, sin reconstruir todo el
        panel (se usa después de revertir la limpieza automática)."""
        widgets = self._widgets_resumen_recorte.get(nombre)
        r = self._recortes.get(nombre)
        if not widgets or not r:
            return
        lbl_rec, lbl_dec = widgets
        png = r.get("png")
        if png and Path(png).exists():
            try:
                im = self._componer_sobre_gris(Image.open(png))
                im.thumbnail((84, 84))
                foto = ImageTk.PhotoImage(im)
                self._fotos_resumen.append(foto)  # mantener referencia viva
                lbl_rec.configure(image=foto)
            except Exception:  # noqa: BLE001
                _logger_gui().exception("no se pudo cargar la miniatura del recorte en la fila de resumen")
        decision = r.get("estado", {}).get("decision", "pendiente")
        color_dec = {"aprobado": COLOR_OK, "descartado": COLOR_MAL}.get(decision, COLOR_TEXTO_SUAVE)
        lbl_dec.configure(text=decision, fg=color_dec)

    def _abrir_visor_recorte(self, nombre: str) -> None:
        """Ventana de detalle de un calzado del resumen: verlo a tamaño real,
        copiar/guardar el PNG, o revertir la limpieza automática aplicada."""
        r = self._recortes.get(nombre)
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        if not png.exists():
            messagebox.showerror("Ver calzado", f"No encuentro el archivo:\n{png}")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(nombre)
        ventana.configure(fg_color=PAR_FONDO)

        cuerpo = Marco(ventana, padding=12)
        cuerpo.pack(fill="both", expand=True)

        Etiqueta(cuerpo, text=nombre, style="Titulo.TLabel").pack(anchor="w", fill="x")

        # tamaño real, o lo más grande que quepa en la pantalla, con scroll
        try:
            im_original = Image.open(png)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver calzado", f"No pude abrir la imagen: {exc}")
            ventana.destroy()
            return

        # Tamaño fijo chico, no "toda la pantalla menos un margen" — así
        # siempre entra completa sin tener que mover la ventana. El detalle
        # fino se ve con el botón de líneas o exportando/copiando la imagen
        # real (que sí queda a resolución completa en el archivo).
        ANCHO_MAX, ALTO_MAX = 560, 460

        marco_img = ctk.CTkFrame(cuerpo, fg_color=PAR_VISOR_BG, corner_radius=RADIO_PANEL)
        marco_img.pack(fill="both", expand=True, pady=(8, 8))
        lbl_imagen_visor = tk.Label(marco_img, background=COLOR_VISOR_BG,
                                    borderwidth=0, highlightthickness=0)
        lbl_imagen_visor.pack(padx=8, pady=8)

        v_estado_local = tk.StringVar(value="")
        Etiqueta(cuerpo, textvariable=v_estado_local, style="Suave.TLabel").pack(anchor="w", fill="x")

        estado_visor = {"con_lineas": False}

        def mostrar(im_pil: Image.Image) -> None:
            vista = im_pil.copy()
            vista.thumbnail((ANCHO_MAX, ALTO_MAX))
            foto = ImageTk.PhotoImage(vista)
            self._fotos_visor_recorte[nombre] = foto
            lbl_imagen_visor.configure(image=foto)

        def imagen_actual_para_mostrar() -> Image.Image:
            png_actual = Path(r["png"])
            if estado_visor["con_lineas"]:
                try:
                    return reparar.dibujar_contorno(png_actual, color_basura=reparar.COLOR_BASURA)
                except Exception as exc:  # noqa: BLE001
                    v_estado_local.set(f"No se pudieron calcular las líneas: {exc}")
            return self._componer_sobre_gris(Image.open(png_actual))

        def refrescar_vista() -> None:
            mostrar(imagen_actual_para_mostrar())
            estado_recorte = self._recortes.get(nombre, {}).get("estado", {})
            btn_lineas.state(["!disabled"])
            # Siempre habilitados — la detección automática (tiene_excedente/
            # mordida_real) es una ayuda, no un permiso; falla en casos
            # reales (ej. una mordida real en el talon que la curva no ve).
            btn_recortar.state(["!disabled"])
            btn_reparar_suela.state(["!disabled"])
            tiene_original = bool(self._salida and self._ruta_original(nombre))
            btn_alternar.state(["!disabled"] if tiene_original else ["disabled"])
            btn_revertir.state(["!disabled"] if tiene_original else ["disabled"])

        def copiar() -> None:
            if not _CLIPBOARD_DISPONIBLE:
                v_estado_local.set("Copiar al portapapeles no está disponible en este equipo — "
                                    "usá \"Guardar como…\".")
                return
            try:
                self._copiar_png_al_portapapeles(Path(r["png"]))
                v_estado_local.set("Imagen copiada al portapapeles.")
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo copiar: {exc}")

        def guardar_como() -> None:
            destino = filedialog.asksaveasfilename(
                title="Guardar imagen como…", defaultextension=".png",
                initialfile=f"{Path(nombre).stem}.png", filetypes=[("PNG", "*.png")])
            if not destino:
                return
            try:
                import shutil
                shutil.copyfile(r["png"], destino)
                v_estado_local.set(f"Guardado en {destino}")
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo guardar: {exc}")

        def alternar_lineas() -> None:
            estado_visor["con_lineas"] = not estado_visor["con_lineas"]
            btn_lineas.configure(
                text="Ver recorte normal" if estado_visor["con_lineas"] else "Ver líneas azul/roja")
            mostrar(imagen_actual_para_mostrar())

        def alternar_vista() -> None:
            ruta_original = self._ruta_original(nombre)
            if not ruta_original:
                return
            mostrando_original = estado_visor.get("mostrando_original", False)
            try:
                origen = Path(r["png"]) if mostrando_original else ruta_original
                mostrar(self._componer_sobre_gris(Image.open(origen)))
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo mostrar: {exc}")
                return
            estado_visor["mostrando_original"] = not mostrando_original
            btn_alternar.configure(
                text="Ver con limpieza aplicada" if not mostrando_original else "Ver original (antes)")
            v_estado_local.set("Mostrando: " + ("original, antes de la limpieza" if not mostrando_original
                                                else "actual, con la limpieza aplicada"))

        def revertir() -> None:
            ok = reparar.revertir(self._salida, Path(nombre).stem)
            if not ok:
                v_estado_local.set("No hay una versión anterior guardada para revertir.")
                return
            v_estado_local.set("Limpieza automática revertida — vuelve al recorte original.")
            self._refrescar_miniatura_resumen(nombre)
            estado_visor["mostrando_original"] = False
            btn_alternar.configure(text="Ver original (antes)")
            refrescar_vista()

        def recortar_sombra() -> None:
            v_estado_local.set("Recortando sombra/excedente…")
            ventana.configure(cursor="watch")
            ventana.update_idletasks()
            try:
                res = reparar.aplicar_recorte_excedente(self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo recortar sombra: {exc}")
                return
            finally:
                ventana.configure(cursor="")
            v_estado_local.set(f"Sombra recortada ({res['px_recortados']}px).")
            self._refrescar_miniatura_resumen(nombre)
            refrescar_vista()

        def completar_suela() -> None:
            png_actual = Path(r["png"])
            v_estado_local.set("Reparando suela… (calculando la curva y rellenando)")
            ventana.configure(cursor="watch")
            ventana.update_idletasks()
            try:
                res_reparo = reparar.reparar(png_actual, color_basura=reparar.COLOR_BASURA, forzar=True)
                reparar.aplicar(self._salida, Path(nombre).stem, res_reparo)
            except ValueError as exc:
                v_estado_local.set(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                v_estado_local.set(f"No se pudo reparar la suela: {exc}")
                return
            finally:
                ventana.configure(cursor="")
            v_estado_local.set(f"Suela reparada ({res_reparo.px_rellenados}px rellenados).")
            self._refrescar_miniatura_resumen(nombre)
            refrescar_vista()

        fila_botones = Marco(cuerpo)
        fila_botones.pack(fill="x", pady=(6, 2))
        btn_lineas = Boton(fila_botones, text="Ver líneas azul/roja", command=alternar_lineas)
        btn_lineas.pack(side="left")
        btn_recortar = Boton(fila_botones, text="Recortar sombra", command=recortar_sombra)
        btn_recortar.pack(side="left", padx=(8, 0))
        btn_reparar_suela = Boton(fila_botones, text="Reparar suela", command=completar_suela)
        btn_reparar_suela.pack(side="left", padx=(8, 0))

        def reparar_y_recortar() -> None:
            completar_suela()
            recortar_sombra()

        Boton(fila_botones, text="Reparar suela + sombra (los dos)",
              command=reparar_y_recortar).pack(side="left", padx=(8, 0))

        fila_botones2 = Marco(cuerpo)
        fila_botones2.pack(fill="x", pady=(8, 0))
        btn_copiar = Boton(fila_botones2, text="Copiar imagen", command=copiar)
        btn_copiar.pack(side="left")
        if not _CLIPBOARD_DISPONIBLE:
            btn_copiar.state(["disabled"])
        Boton(fila_botones2, text="Guardar como…", command=guardar_como).pack(
            side="left", padx=(8, 0))
        btn_alternar = Boton(fila_botones2, text="Ver original (antes)", command=alternar_vista)
        btn_alternar.pack(side="left", padx=(8, 0))
        btn_revertir = Boton(fila_botones2, text="Revertir cambios automáticos",
                             command=revertir)
        btn_revertir.pack(side="left", padx=(8, 0))

        def aprobar_desde_visor() -> None:
            # `_decidir` es el mismo camino que usa el boton "Aprobar" de la
            # grilla; reemplaza al viejo `_aprobar_sin_limpiar` (hacia lo
            # mismo) y ya trae la guarda de no escribir una decision sobre un
            # recorte que se desecho mientras este visor seguia abierto.
            if not self._decidir("aprobado", nombre):
                v_estado_local.set(f"{nombre}: ya no existe, no se puede aprobar.")
                return
            v_estado_local.set(f"{nombre}: aprobado.")

        Boton(fila_botones2, text="✓ Aprobar", style="Aprobar.TButton",
              command=aprobar_desde_visor).pack(side="left", padx=(8, 0))

        def desechar_desde_visor() -> None:
            # Si el borrado se confirma, esta ventana queda mostrando un
            # archivo que ya no existe: se cierra sola.
            if self._desechar_definitivamente(nombre):
                ventana.destroy()

        # Separado de "Aprobar" con el doble de aire (24px contra 8px) y con
        # contorno rojo oscuro en vez de relleno: es el único botón de esta
        # ventana que borra algo del disco.
        Boton(fila_botones2, text="🗑 Desechar definitivamente",
              style="Peligro.TButton",
              command=desechar_desde_visor).pack(side="left", padx=(24, 0))
        Boton(fila_botones2, text="Cerrar", command=ventana.destroy).pack(side="right")

        refrescar_vista()
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        # Mismo problema de apilamiento que `_mostrar_imagen_flotante` (ver la
        # nota larga allá): esta ventana también nace por un clic en una
        # miniatura del paso 5 y quedaba detrás de la principal maximizada.
        _traer_al_frente(ventana)

    @staticmethod
    def _copiar_png_al_portapapeles(png: Path) -> None:
        """Copia el PNG al portapapeles de Windows como bitmap (DIB), para
        pegarlo directo en Word/Outlook/Excel/etc. El portapapeles clásico de
        Windows no soporta transparencia real en CF_DIB, así que se compone
        sobre blanco para el bitmap que ve el usuario al pegar."""
        im = Image.open(png).convert("RGBA")
        fondo = Image.new("RGB", im.size, (255, 255, 255))
        fondo.paste(im, mask=im.split()[3])
        salida = _io.BytesIO()
        fondo.save(salida, "BMP")
        datos_bmp = salida.getvalue()[14:]  # el DIB no lleva el header de archivo BMP (14 bytes)
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, datos_bmp)
        finally:
            win32clipboard.CloseClipboard()
