"""
Revisión foto por foto: la lista de recortes, el visor, las correcciones y las
decisiones.

División 2026-09-23, Fase 4: `HerramientaUnica` era UNA clase de ~7,200 líneas
con los 7 pasos del asistente adentro. Acá vive el corazón del paso 3, que es
donde el comprador pasa la mayor parte del tiempo: leer el manifiesto y
poblar la grilla, elegir un recorte y verlo, corregirle la suela o la sombra,
aprobarlo o descartarlo, desecharlo de verdad (borrando archivos), hacer todo
eso en bloque sobre varias tarjetas a la vez, y prender o apagar la limpieza
automática de una foto.

Se separó del ARMADO de esa misma pantalla (que sigue en `HerramientaUnica`,
en `_armar_cuerpo_revision`) a propósito: una cosa es construir los widgets
una vez al arrancar, y otra muy distinta es la lógica que corre cada vez que
el comprador toca algo. Lo segundo es lo que cambia seguido y lo que conviene
poder leer solo.

Es un mixin, no una jerarquía con comportamiento propio: todo se combina en la
MISMA instancia, así que `self.<lo que sea>` sigue funcionando igual que
cuando esto era un bloque de texto más adentro de la clase. Los cuerpos se
movieron LITERALMENTE, sin cambiar una línea.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from PIL import Image, ImageTk

import almacen
import decisiones
import reparar

from util_ventana import _badge, _logger_gui, reloj
from tema import COLOR_VISOR_BG, FUENTE_CHICA


class _MixinRevision:
    """Todo lo que PASA en la pantalla de revisión (paso 3), no cómo se arma.

    Ver el docstring del módulo: agrupamiento por lo que el comprador HACE
    sobre las fotos, no por tecnología.
    """

    # ── lista de recortes, leída del manifiesto ─────────────────────────

    def _corregir_rutas_movidas(self, r: dict) -> None:
        """El manifiesto guarda rutas ABSOLUTAS (`r["png"]`, `r["jpg"]`) tal
        como eran al procesar. Si esa carpeta de trabajo se mueve o renombra
        después (pasó de verdad: se movió `_pasos/blanco/transparente/...`
        de `Imagenes finales` a `_trabajo` para separar caché de entregable),
        las rutas viejas dejan de existir aunque el archivo siga ahí, sano,
        con otro padre. Se corrige buscando el mismo nombre de archivo
        dentro de `transparente/`/`blanco/` de la carpeta de trabajo ACTUAL
        — sin esto, un recorte "desaparece" de la lista con el archivo
        intacto en el disco."""
        if not self._salida:
            return
        for clave, sub in (("png", "transparente"), ("jpg", "blanco")):
            ruta = r.get(clave)
            if not ruta or Path(ruta).exists():
                continue
            candidata = self._salida / sub / Path(ruta).name
            if candidata.exists():
                r[clave] = str(candidata)

    def _recargar_manifiesto(self) -> None:
        if not self._salida:
            return
        # Siempre se reconstruye desde cero a partir de LO QUE HAY AHORA en
        # `self._salida` — sin este reset, cambiar de carpeta de trabajo
        # dejaba mezclados los recortes de la carpeta anterior con los de
        # la nueva (mismo síntoma que el "cuadro de recortes" sin limpiar).
        # Los tres criterios que antes estaban acá a mano —solo recortes con
        # imagen y estado medido; sin los eliminados del disco (su archivo ya
        # no está, ver `_desechar_definitivamente`, pero el registro de la
        # lámina sigue porque tiene hermanos buenos); y la decisión VIGENTE de
        # la bitácora en vez de la que quedó congelada al procesar— ahora son
        # el `WHERE` de `almacen.cargar_recortes`.
        #
        # Ese último cruce importa y por eso está en el `WHERE` y no acá: el
        # `estado.decision` que se guardó al procesar la lámina (ver
        # `estado.calcular_estado_grupo`) casi siempre dice "pendiente", y sin
        # el cruce con la bitácora un lote enteramente aprobado volvía a
        # cargarse como "20 pendientes" al reabrir la app — lo que hacía que
        # "Confirmar y enviar" mostrara de nuevo las fotos descartadas y que la
        # detección de avance mandara al paso 2 un lote que ya estaba entero.
        try:
            self._recortes = almacen.cargar_recortes(self._salida)
        except Exception as exc:  # noqa: BLE001
            self._recortes = {}
            self.v_log.set(f"No se pudo leer el lote de esta carpeta: {exc}")
        for r in self._recortes.values():
            self._corregir_rutas_movidas(r)

        seleccion_previa = self._nombre_actual

        # Actualización DIFERENCIAL, no borrar-todo-y-recrear-todo.
        #
        # Bug real corregido (2026-09-03): antes se llamaba `delete(*get_
        # children())` + reinsertar TODO en cada llamada -- y esta función
        # se llama en cada foto nueva mientras el lote está procesando. Cada
        # `insert()` reencola la miniatura (que corre `reparar.dibujar_
        # contorno`, un ajuste de curvas real, no gratis) -- con eso, ya
        # procesada la miniatura de la foto 1 se recalculaba de nuevo al
        # llegar la foto 2, la 3, ..., trabajo O(n²) sobre el lote completo.
        # Efecto colateral: `delete()` también vacía la selección múltiple
        # (`_lote`) en cada recarga, así que una marca de varias fotos no
        # sobrevivía a que llegara una foto nueva del mismo lote.
        #
        # Ahora: solo se borran las tarjetas que genuinamente desaparecieron
        # (se cambió de carpeta, se desechó una foto), solo se insertan las
        # que son nuevas de verdad (dispara su única carga de miniatura), y
        # las que ya existían se actualizan en su lugar con `.set()`/
        # `.item()` -- sin tocar la miniatura ya cargada ni la selección.
        existentes = set(self.tabla.get_children())
        deseados = set(self._recortes)
        for nombre in existentes - deseados:
            self.tabla.delete(nombre)
        for nombre in sorted(self._recortes):
            e = self._recortes[nombre]["estado"]
            tag = e["decision"] if e["decision"] in ("aprobado", "descartado") else ""
            valores = (f'{e["falta_pct"]:.1f}',
                      "sí" if e["mordida_real"] else "no",
                      "sí" if e["sombra_outlier"] else "no",
                      e["decision"])
            if nombre in existentes:
                for col, val in zip(("falta", "mordida", "sombra", "decision"), valores):
                    self.tabla.set(nombre, col, val)
                self.tabla.item(nombre, tags=(tag,) if tag else ())
            else:
                self.tabla.insert("", "end", iid=nombre, text=nombre, values=valores,
                                  tags=(tag,) if tag else ())
        if seleccion_previa and self.tabla.exists(seleccion_previa):
            self.tabla.selection_set(seleccion_previa)
        else:
            # El recorte seleccionado antes de recargar ya no está en la
            # lista (cambió de carpeta de trabajo, o se reprocesó) — el
            # panel de detalle no se puede quedar mostrando ese calzado
            # viejo como si fuera el actual.
            self._limpiar_panel_detalle()

        # El indicador de paso muestra "N aprobada(s) de M": cada recarga del
        # manifiesto puede cambiar ese conteo.
        if self._recortes and getattr(self, "_paso_flujo", 1) == 1:
            self._actualizar_paso(3)
        else:
            self._actualizar_paso()

    # ── selección y visor ────────────────────────────────────────────────

    def _ruta_original(self, nombre: str) -> Path | None:
        """Backup previo a la limpieza automática (excedente/suela), si se
        aplicó alguna vez, para poder mostrar antes/después o revertir."""
        if not self._salida:
            return None
        ruta = self._salida / reparar.CARPETA_ORIGINALES / f"{Path(nombre).stem}.png"
        return ruta if ruta.exists() else None

    def _limpiar_badges(self) -> None:
        for lbl in self._badge_labels:
            lbl.destroy()
        self._badge_labels = []

    def _limpiar_panel_detalle(self) -> None:
        """Vacía el panel de detalle (foto grande, badges, "falta%") del
        recorte que estaba seleccionado — sin esto, después de "Vaciar
        carpeta de trabajo" la tabla queda vacía pero este panel seguía
        mostrando el último calzado que se había mirado antes de vaciar,
        como si fuera "el de ahora"."""
        self._nombre_actual = None
        self._limpiar_badges()
        self.lbl_imagen.configure(image="")
        self._foto_actual = None
        self.v_falta.set("")
        self.v_auto_desc.set("")
        self.v_paso_proceso.set("")
        self.btn_deshacer_auto.state(["disabled"])
        self.chk_antes_despues.state(["disabled"])
        self._ver_antes_despues.set(False)
        for b in (self.btn_aprobar, self.btn_descartar, self.btn_desechar,
                  self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        self._barra_corregir.pack_forget()

    def _agregar_badge(self, texto: str, ok: bool | None) -> None:
        _, fg, bg = _badge(texto, ok)
        lbl = tk.Label(self._badges_frame, text=texto, fg=fg, bg=bg,
                       font=FUENTE_CHICA, padx=8, pady=3)
        lbl.pack(anchor="w", pady=2, fill="x")
        self._badge_labels.append(lbl)

    def _seleccionar(self, _evt=None) -> None:
        # Tkinter manda las excepciones de un callback a stderr y sigue como
        # si nada: si algo falla acá, el panel de detalle queda a medio armar
        # sin ningún aviso en pantalla. Se atrapa para poder decirlo en la
        # barra de log.
        try:
            self._seleccionar_real()
        except Exception as exc:  # noqa: BLE001
            _logger_gui().exception("fallo al mostrar el recorte seleccionado")
            import traceback
            traceback.print_exc()
            self.v_log.set(f"Error mostrando esta foto: {exc}")

    def _seleccionar_real(self) -> None:
        sel = self.tabla.selection()
        if not sel:
            return
        nombre = sel[0]
        self._nombre_actual = nombre
        r = self._recortes.get(nombre)
        if not r:
            return
        e = r["estado"]
        self.v_falta.set(f'{e["falta_pct"]:.1f}%')

        self._limpiar_badges()
        if e["mordida_real"]:
            self._agregar_badge("Suela con mordida", False)
        else:
            self._agregar_badge("Suela completa", True)
        if e["sombra_outlier"]:
            corregido = e.get("sombra_corregida_px", 0) > 0
            self._agregar_badge("Sombra corregida" if corregido else "Sombra detectada", corregido)
        if e.get("tiene_excedente"):
            self._agregar_badge("Restos de fondo detectados", False)

        hubo_auto = bool(self._ruta_original(nombre))
        if hubo_auto:
            partes = []
            if e["mordida_real"] or e.get("sombra_corregida_px", 0) or e.get("tiene_excedente"):
                pass
            if e.get("sombra_corregida_px"):
                partes.append(f"sombra corregida ({e['sombra_corregida_px']}px)")
            if not e["mordida_real"] and (self._ruta_original(nombre)):
                partes.append("suela completada")
            self.v_auto_desc.set("Se corrigió automáticamente: " + (", ".join(partes) if partes
                                  else "recorte ajustado") + ".")
            self.btn_deshacer_auto.state(["!disabled"])
            self.btn_deshacer_auto.configure(text="Deshacer limpieza automática")
        else:
            self.v_auto_desc.set("No hizo falta corregir nada en este recorte.")
            self.btn_deshacer_auto.state(["disabled"])

        for b in (self.btn_aprobar, self.btn_descartar, self.btn_desechar,
                  self.btn_reparar_suela, self.btn_recortar_sombra):
            b.state(["!disabled"])
        # "Revertir" solo tiene sentido si hay una versión anterior guardada.
        self.btn_revertir_recorte.state(["!disabled"] if hubo_auto else ["disabled"])
        self.chk_lineas.state(["!disabled"])

        self.chk_antes_despues.state(["!disabled"] if hubo_auto else ["disabled"])
        if not hubo_auto:
            self._ver_antes_despues.set(False)
        if not self._barra_corregir.winfo_manager():
            # `before=self._marco_visor` es lo que la mantiene ANTES del visor
            # en el orden de pack: sin eso `pack` le reparte al visor toda la
            # cavidad primero y Tk desmapea esta franja sin avisar (ver el
            # comentario largo en `_armar_ui`).
            self._barra_corregir.pack(side="bottom", fill="x", padx=14, pady=(0, 6),
                                      before=self._marco_visor)
            self._barra_corregir.update_idletasks()
        self._mostrar_imagen()

    # ── corregir el recorte seleccionado, desde la pantalla de revisión ──
    # Mismas llamadas a `reparar` que hace `_abrir_visor_recorte`; lo único
    # que cambia es desde dónde se disparan y que el resultado se refresca en
    # el panel de revisión en vez de en el diálogo.

    def _remedir_recorte(self, nombre: str) -> None:
        """Vuelve a medir la suela de un recorte que se acaba de corregir a
        mano y actualiza su entrada en `self._recortes`.

        Hace falta porque el `estado` del manifiesto se calculó UNA vez, al
        procesar el lote (ver `estado.calcular_estado_grupo`): sin esto, el
        panel seguiría diciendo "Suela con mordida · falta 8%" sobre una foto
        que el comprador acaba de reparar. Solo se recalculan los campos
        medibles del PNG; la decisión y lo que depende del grupo (sombra
        outlier) se dejan como estaban."""
        r = self._recortes.get(nombre)
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        if not png.exists():
            return
        try:
            s = reparar.medir_suela(png, color_basura=reparar.COLOR_BASURA)
            r["estado"]["falta_pct"] = round(reparar.porcentaje_faltante(s), 2)
            r["estado"]["mordida_real"] = reparar.tiene_mordida_real(s)
        except ValueError:
            # medicion imposible (recorte vacio): se reporta como 0% faltante.
            r["estado"]["falta_pct"] = 0.0
            r["estado"]["mordida_real"] = False
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo medir el estado del recorte %s", png)
            return
        try:
            r["estado"]["tiene_excedente"] = reparar.tiene_excedente(
                png, color_basura=reparar.COLOR_BASURA)
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo medir el excedente del recorte %s", png)

    def _tras_corregir(self, nombre: str, mensaje: str) -> None:
        """Refresca todo lo que muestra ese recorte después de tocarlo: la
        medición del estado (que alimenta los badges y el % faltante), las
        miniaturas y el visor grande."""
        self._remedir_recorte(nombre)
        # Se anota que ESTA foto se corrigió a mano en esta sesión: al
        # aprobarla, el visor se cierra solo (ver `_decidir`) en vez de quedar
        # abierto esperando un clic más.
        self._corregidas_a_mano.add(nombre)
        self._refrescar_miniatura_resumen(nombre)
        self.v_log.set(f"{nombre}: {mensaje}")
        # Re-dibuja badges, % faltante y estado de los botones desde la
        # entrada ya remedida, y termina llamando a `_mostrar_imagen()`.
        self._seleccionar()

    # ── las 2 correcciones, UNA sola implementación ──────────────────────
    #
    # Antes esta lógica estaba escrita DOS veces: acá para la foto que se está
    # mirando (la barra "CORREGIR ESTA FOTO") y otra vez dentro de
    # `_aplicar_limpieza_seleccion`, en la pantalla masiva de casillas. Dos
    # copias de la misma secuencia (`reparar.reparar` + `reparar.aplicar`) que
    # ya se habían desincronizado en los hechos: la de la pantalla masiva
    # se tragaba el `ValueError` de "nada que reparar" en silencio, y la de
    # acá lo mostraba en el log.
    #
    # Ahora hay una sola: `_reparar_suela_de` / `_recortar_sombra_de` trabajan
    # sobre UN nombre y devuelven (ok, mensaje). Los dos caminos de la interfaz
    # —una foto, o N seleccionadas en la grilla— son dos llamadores del mismo
    # código, no dos implementaciones.

    def _reparar_suela_de(self, nombre: str) -> tuple[bool, str]:
        r = self._recortes.get(nombre)
        if not r or not r.get("png") or not self._salida:
            return False, "ya no existe"
        try:
            res = reparar.reparar(Path(r["png"]),
                                  color_basura=reparar.COLOR_BASURA, forzar=True)
            reparar.aplicar(self._salida, Path(nombre).stem, res)
        except ValueError as exc:
            # No es un error técnico: es "no hay nada que reparar acá" o
            # "falta demasiado". Se distingue del fallo real (abajo) porque en
            # bloque no tiene sentido contarlo como error.
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"no se pudo reparar la suela ({exc})"
        return True, f"suela reparada ({res.px_rellenados}px rellenados)"

    def _recortar_sombra_de(self, nombre: str) -> tuple[bool, str]:
        r = self._recortes.get(nombre)
        if not r or not r.get("png") or not self._salida:
            return False, "ya no existe"
        try:
            res = reparar.aplicar_recorte_excedente(
                self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
        except Exception as exc:  # noqa: BLE001
            return False, f"no se pudo recortar sombra ({exc})"
        return True, f"sombra recortada ({res['px_recortados']}px)"

    def _correccion_en_curso(self, boton, texto_trabajando: str) -> None:
        """Deja la barra "CORREGIR ESTA FOTO" en modo "estoy trabajando":
        el botón tocado cambia de texto y los tres quedan deshabilitados.

        La reparación corre en el hilo de la interfaz (es lo que ya hacía), así
        que sin esto la ventana se quedaba muda unos segundos y el usuario no
        sabía si su clic había hecho algo. `update_idletasks` es obligatorio:
        sin él el cambio de texto se dibujaría recién DESPUÉS de terminar la
        reparación, o sea nunca."""
        self._texto_btn_correccion = boton.cget("text")
        try:
            boton.configure(text=texto_trabajando)
        except tk.TclError:
            # barra "CORREGIR ESTA FOTO" ya destruida: no hay rotulo que cambiar.
            pass
        for b in (self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        self.configure(cursor="watch")
        self.update_idletasks()

    def _correccion_terminada(self, boton) -> None:
        try:
            boton.configure(text=getattr(self, "_texto_btn_correccion", None)
                            or boton.cget("text"))
        except tk.TclError:
            # barra "CORREGIR ESTA FOTO" ya destruida: no hay rotulo que restaurar.
            pass
        self.configure(cursor="")
        # Quién queda habilitado y quién no lo decide `_seleccionar_real`
        # (depende de si hay versión anterior guardada): se rehabilitan acá
        # solo los dos que siempre aplican mientras haya foto seleccionada, y
        # el refresco posterior ajusta el resto.
        if self._nombre_actual:
            for b in (self.btn_reparar_suela, self.btn_recortar_sombra):
                b.state(["!disabled"])

    def _reparar_suela_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre:
            return
        self._correccion_en_curso(self.btn_reparar_suela, "⏳ Reparando…")
        try:
            ok, mensaje = self._reparar_suela_de(nombre)
        finally:
            self._correccion_terminada(self.btn_reparar_suela)
        if not ok:
            self.v_log.set(f"{nombre}: {mensaje}")
            return
        self._tras_corregir(nombre, mensaje)

    def _recortar_sombra_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre:
            return
        self._correccion_en_curso(self.btn_recortar_sombra, "⏳ Recortando…")
        try:
            ok, mensaje = self._recortar_sombra_de(nombre)
        finally:
            self._correccion_terminada(self.btn_recortar_sombra)
        if not ok:
            self.v_log.set(f"{nombre}: {mensaje}")
            return
        self._tras_corregir(nombre, mensaje)

    def _revertir_recorte_actual(self) -> None:
        nombre = self._nombre_actual
        if not nombre or not self._salida:
            return
        with reloj(self):
            revertido = reparar.revertir(self._salida, Path(nombre).stem)
        if not revertido:
            self.v_log.set(f"{nombre}: no hay una versión anterior guardada para revertir")
            return
        self._tras_corregir(nombre, "correcciones revertidas al recorte original")

    def _mostrar_imagen(self) -> None:
        r = self._recortes.get(self._nombre_actual or "")
        if not r or not r.get("png"):
            return
        png = Path(r["png"])
        self.v_paso_proceso.set("")

        try:
            if self._ver_antes_despues.get():
                ruta_antes = self._ruta_original(self._nombre_actual)
                if not ruta_antes or not png.exists():
                    self.v_log.set("No hay versión anterior guardada para este recorte.")
                    return
                antes = self._componer_sobre_gris(Image.open(ruta_antes))
                despues = (reparar.dibujar_contorno(png) if self._ver_lineas.get()
                           else Image.open(png))
                despues = self._componer_sobre_gris(despues) if despues.mode != "RGB" else despues
                alto = max(antes.height, despues.height)
                im = Image.new("RGB", (antes.width + despues.width + 16, alto), COLOR_VISOR_BG)
                im.paste(antes, (0, 0))
                im.paste(despues, (antes.width + 16, 0))
                self.v_paso_proceso.set("Izquierda: antes de la limpieza automática  ·  "
                                        "Derecha: después, con líneas de referencia")
            else:
                if not png.exists():
                    self.v_log.set(f"No encuentro {png}")
                    return
                # Explícito, no automático: el comprador decide cuándo ver
                # las líneas de referencia sobre la foto que está mirando.
                im = (reparar.dibujar_contorno(png) if self._ver_lineas.get()
                      else Image.open(png))
                if im.mode != "RGB":
                    im = self._componer_sobre_gris(im)
        except Exception as exc:  # noqa: BLE001
            self.v_log.set(f"No pude generar la vista: {exc}")
            return
        im = self._ajustar_a_espacio(im, self._espacio_visor())  # agranda si hace falta, no solo achica
        self._foto_actual = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_actual)

    def _espacio_visor(self) -> tuple[int, int]:
        """Espacio REAL disponible para la foto grande, medido sobre
        `_marco_visor`.

        Antes esto eran dos pares de números fijos —(520, 480) normal y
        (960, 460) en antes/después— que no tenían nada que ver con el tamaño
        del visor. Causa raíz del bug de "la imagen grande se ve cortada" al
        retomar un lote: con la franja amarilla en pantalla el visor mide
        bastante menos de 480px de alto, pero la foto se seguía escalando a
        480 y el `tk.Label` (que está dentro de un marco con
        `pack_propagate(False)`) la recortaba en silencio. En un lote nuevo,
        sin franja, los 480 sí entraban — por eso solo se notaba al retomar.

        Si el marco todavía no está medido (`winfo_*` devuelve 1 antes del
        primer ciclo de layout), se cae a los valores fijos de siempre; el
        `<Configure>` de más abajo vuelve a escalar en cuanto Tk asienta el
        layout, así que nunca queda con la medida provisional.

        `update_idletasks()` ANTES de medir (2026-09-09, reclamo repetido de
        "el recorte se ve muy pequeño" que sobrevivió a maximizar la ventana
        y a achicar todas las franjas): sin esto, `winfo_width/height` podían
        devolver la medida VIEJA de antes del último cambio de layout (cerrar
        la franja amarilla, elegir un recorte recién after maximizar, etc.)
        porque Tk todavía no había procesado ese cambio pendiente -- la foto
        se escalaba correcto para un visor que ya no existía, más chico que
        el real.
        """
        self._marco_visor.update_idletasks()
        antes_despues = self._ver_antes_despues.get()
        ancho = self._marco_visor.winfo_width() - 8
        alto = self._marco_visor.winfo_height() - 8
        if ancho < 40 or alto < 40:  # layout aún sin asentar
            return (960, 460) if antes_despues else (520, 480)
        return max(ancho, 120), max(alto, 120)

    def _al_redimensionar_visor(self, _evento=None) -> None:
        """Vuelve a escalar la foto abierta cuando el visor cambia de tamaño:
        al cerrar la franja amarilla de "retomando", al maximizar la ventana o
        al aparecer/desaparecer la barra de "Corregir esta foto". Se pospone
        con `after_idle` para medir DESPUÉS de que Tk asiente el layout, y se
        salta si el tamaño no cambió de verdad (los `<Configure>` llegan en
        ráfaga y reescalar en cada uno haría titilar la imagen)."""
        medida = (self._marco_visor.winfo_width(), self._marco_visor.winfo_height())
        if medida == getattr(self, "_medida_visor", None):
            return
        self._medida_visor = medida
        if not getattr(self, "_nombre_actual", None):
            return
        self.after_idle(self._mostrar_imagen)

    @staticmethod
    def _componer_sobre_gris(origen: Image.Image) -> Image.Image:
        origen = origen.convert("RGBA")
        fondo = Image.new("RGBA", origen.size, (238, 241, 244, 255))
        return Image.alpha_composite(fondo, origen).convert("RGB")

    # ── decisiones y control único de limpieza automática ───────────────

    def _decidir(self, decision: str, nombre: str | None = None) -> bool:
        """Registra "aprobado"/"descartado" para `nombre` (o el de detalle
        actual, si no se pasa ninguno — caso de uso individual de siempre).
        Devuelve si de verdad registró algo, para que quien llame en lote
        pueda contar cuántas se aplicaron de verdad."""
        nombre = nombre if nombre is not None else self._nombre_actual
        if not nombre or not self._salida:
            return False
        r = self._recortes.get(nombre)
        if not r:
            # Ya no existe (desechado definitivamente) — protegido hasta
            # ahora solo "por accidente" (nada llega hasta acá con un nombre
            # borrado en la práctica), pero mejor no confiar en eso: registrar
            # una decisión sobre un recorte inexistente es exactamente el
            # patrón que ya causó el bug de "aprobado" resucitando eliminados.
            return False
        decisiones.registrar(self._salida, nombre, decision)
        r["estado"]["decision"] = decision
        if self.tabla.exists(nombre):
            self.tabla.set(nombre, "decision", decision)
            self.tabla.item(nombre, tags=(decision,))
        self.v_log.set(f"{nombre}: {decision}")
        self._actualizar_paso()
        # Aprobar una foto que se acaba de reparar/recortar cierra su visor:
        # el trabajo sobre esa foto terminó y dejarla abierta obligaba a un
        # clic extra para pasar a la siguiente. Solo en ese caso — si no hubo
        # corrección previa, el comportamiento de siempre no cambia.
        if (decision == decisiones.APROBADO and nombre == self._nombre_actual
                and nombre in self._corregidas_a_mano):
            self._corregidas_a_mano.discard(nombre)
            self._cerrar_visor_foto()
        return True

    def _cerrar_visor_foto(self) -> None:
        """Deja el panel central sin foto abierta y deselecciona la tarjeta,
        volviendo la atención a la grilla."""
        deseleccionar = getattr(self.tabla, "deseleccionar", None)
        if callable(deseleccionar):
            deseleccionar()
        self._limpiar_panel_detalle()

    # ── desechar definitivamente (borrado real de archivos) ──────────────

    def _archivos_del_recorte(self, nombre: str) -> list[Path]:
        """Los archivos EN DISCO que componen un recorte, en la carpeta de
        trabajo actual: el PNG transparente, el JPG con fondo blanco y el
        respaldo previo a la limpieza automática, si existen.

        Mismo patrón de rutas que usan `reparar.py` y `decisiones.py`
        (`<salida>/<formato>/<stem>.<ext>`), para no inventar una tercera
        forma de ubicar el mismo archivo.
        """
        if not self._salida:
            return []
        stem = Path(nombre).stem
        candidatos = [self._salida / fmt / f"{stem}{ext}"
                      for fmt, (_sub, ext) in decisiones.FORMATOS.items()]
        candidatos.append(self._salida / reparar.CARPETA_ORIGINALES / f"{stem}.png")
        return [p for p in candidatos if p.exists()]

    def _desechar_definitivamente(self, nombre: str, al_terminar=None) -> bool:
        """Borra del disco los archivos de un recorte basura y lo saca de la
        pantalla. Irreversible, a diferencia de "Descartar".

        Por qué existe: "Descartar" solo escribe una decisión — el archivo
        basura se queda en la carpeta para siempre y sigue apareciendo en la
        lista cada vez que se abre la carpeta. Para las fotos que directamente
        no sirven (fondo irrecuperable, recorte partido, foto que no es un
        calzado) hacía falta sacarlas de encima de verdad.

        Sobre reprocesar: la línea del manifiesto NO se toca. Está a nivel de
        LÁMINA (sha-1 de la foto de ENTRADA) y una lámina produce varios
        recortes, así que borrarla también haría desaparecer del listado a los
        hermanos buenos del mismo calzado y volvería a procesar la lámina
        entera. La lámina, entonces, sigue contando como "ya hecha" y este
        recorte NO reaparece solo. Es la decisión deliberada: la foto es
        basura por lo que ES, no por cómo se procesó — volver a correrla sola
        daría la misma basura y obligaría al usuario a descartarla de nuevo.
        Si de verdad quiere reintentarla (por ejemplo porque el pipeline
        mejoró), tiene la casilla «Reprocesar aunque ya esté hecho», que
        ignora el manifiesto. Para que no reaparezca al recargar el
        manifiesto, `_recargar_manifiesto` filtra los recortes marcados
        `decisiones.ELIMINADO`.
        """
        if not self._salida:
            return False
        archivos = self._archivos_del_recorte(nombre)
        fuera = self._archivos_fuera_de_trabajo(archivos)
        if fuera:
            messagebox.showerror(
                "Desechar definitivamente",
                "Hay archivos de este recorte fuera de la carpeta de trabajo "
                f"actual:\n{chr(10).join(str(p) for p in fuera)}\n\n"
                "Por seguridad no se borró nada.")
            return False

        detalle = "\n".join(f"  · {p.parent.name}/{p.name}" for p in archivos) or "  (ninguno)"
        if not messagebox.askyesno(
                "Desechar definitivamente",
                f"Vas a BORRAR DEL DISCO el recorte:\n\n{nombre}\n\n"
                f"Se borran estos archivos:\n{detalle}\n\n"
                "Esto NO se puede deshacer — no es lo mismo que «Descartar», "
                "que solo marca la foto y se puede cambiar de opinión después.\n\n"
                "¿Borrarlo definitivamente?",
                icon="warning", default="no"):
            return False

        ok, error = self._desechar_uno(nombre)
        if not ok:
            messagebox.showerror(
                "Desechar definitivamente",
                "No se pudieron borrar todos los archivos (puede que estén "
                f"abiertos en otro programa):\n\n{error}")
            return False

        if callable(al_terminar):
            al_terminar()
        return True

    def _archivos_fuera_de_trabajo(self, archivos: list[Path]) -> list[Path]:
        """Mismo criterio de seguridad que `_vaciar_carpeta_trabajo`: no se
        borra NADA que esté fuera de la carpeta de trabajo activa. Si el
        manifiesto viene de otra carpeta (carpeta movida, ruta corregida),
        mejor no borrar que borrar la foto de otro lote."""
        if not self._salida:
            return list(archivos)
        salida = self._salida.resolve()
        return [p for p in archivos if salida not in p.resolve().parents]

    def _desechar_uno(self, nombre: str) -> tuple[bool, str]:
        """El borrado propiamente dicho de UN recorte, sin ningún cuadro de
        diálogo: borra los archivos, escribe la decisión y saca la tarjeta de
        la pantalla. Devuelve `(ok, motivo_del_fallo)`.

        Está separado de `_desechar_definitivamente` (que es el que pregunta)
        para que el caso individual y el caso en bloque compartan exactamente
        el mismo borrado, en vez de tener dos copias que se van despegando; y
        para que en bloque se pueda preguntar UNA sola vez por las N fotos y
        seguir con las demás si una falla.
        """
        if not self._salida:
            return False, "no hay carpeta de trabajo activa"
        archivos = self._archivos_del_recorte(nombre)
        fuera = self._archivos_fuera_de_trabajo(archivos)
        if fuera:
            return False, ("hay archivos fuera de la carpeta de trabajo: "
                           + ", ".join(str(p) for p in fuera))

        errores: list[str] = []
        for p in archivos:
            try:
                p.unlink()
            except OSError as exc:
                errores.append(f"{p.name}: {exc}")
        if errores:
            # Si quedó algo sin borrar NO se registra "eliminado": la bitácora
            # diría que el recorte ya no está y el archivo seguiría ahí.
            return False, "; ".join(errores)

        # La bitácora queda con "eliminado", no con "descartado": así se sabe
        # que el archivo falta a propósito y no por un accidente.
        decisiones.registrar(self._salida, nombre, decisiones.ELIMINADO,
                             nota="archivos borrados del disco por el usuario")

        self._recortes.pop(nombre, None)
        # `tabla.delete` ya lo saca del lote de selección múltiple (ver
        # `GrillaRecortes.delete`), así que no queda una foto borrada marcada
        # para una acción en bloque.
        if self.tabla.exists(nombre):
            self.tabla.delete(nombre)
        if self._nombre_actual == nombre:
            self._limpiar_panel_detalle()
        self._fotos_visor_recorte.pop(nombre, None)

        # Antes acá había que desarmar A MANO la tarjeta que la pantalla masiva
        # "Elegir recortes a limpiar" había creado para este recorte: esa
        # pantalla armaba sus tarjetas una sola vez al abrirse y no estaba
        # atada a `self._recortes`, así que desechar desde el visor grande
        # dejaba una tarjeta huérfana apuntando a un archivo borrado. Con la
        # pantalla eliminada, la grilla de revisión es la única lista de fotos
        # y sí está atada a `self._recortes` — no hay una segunda copia del
        # lote que se pueda quedar desincronizada.
        self.v_log.set(f"{nombre}: desechado definitivamente "
                       f"({len(archivos)} archivo(s) borrado(s)).")
        return True, ""

    def _desechar_seleccionado(self) -> None:
        if not self._nombre_actual:
            return
        self._desechar_definitivamente(self._nombre_actual)

    # ── acciones en bloque sobre varias tarjetas ─────────────────────────

    def _al_cambiar_lote(self, nombres: list[str]) -> None:
        """La grilla avisó que cambió el conjunto marcado: se muestra o se
        esconde la barra de acciones en bloque y se actualizan los conteos."""
        n = len(nombres)
        if n < 2:
            self._barra_lote.pack_forget()
            if not self._lbl_ayuda_recortes.winfo_manager():
                # Vuelve la pista, y vuelve ARRIBA de todo (`after` con el
                # título): sin `after` reaparecería al final del panel.
                self._lbl_ayuda_recortes.pack(anchor="w", fill="x",
                                              after=self._lbl_titulo_recortes)
            return
        self.v_lote.set(f"{n} fotos seleccionadas")
        self.btn_lote_reparar.configure(text=f"🔧 Suela ({n})")
        self.btn_lote_recortar.configure(text=f"✂ Sombra ({n})")
        self.btn_lote_aprobar.configure(text=f"✓ Aprobar ({n})")
        self.btn_lote_descartar.configure(text=f"✗ Descartar ({n})")
        self.btn_lote_desechar.configure(text=f"🗑 Desechar definitivamente ({n})")
        # La pista de "Ctrl+clic marca varias" se va: ya se usó, y su alto es
        # exactamente lo que le falta a la barra para entrar en la ventana por
        # defecto (ver la nota del bug en `_armar_cuerpo_revision`).
        self._lbl_ayuda_recortes.pack_forget()
        if not self._barra_lote.winfo_manager():
            # Dentro de `_pie_lote`, que ya tiene su lugar reservado al pie del
            # panel desde que se armó la pantalla.
            self._barra_lote.pack(fill="x")

    def _cancelar_lote(self) -> None:
        self.tabla.limpiar_lote()
        self.v_log.set("Selección múltiple cancelada.")

    def _corregir_lote(self, etiqueta: str, corregir) -> None:
        """Aplica una corrección a las N fotos marcadas en la grilla.

        `corregir` es `_reparar_suela_de` o `_recortar_sombra_de` — el MISMO
        código que corre la barra "CORREGIR ESTA FOTO" para una sola foto.
        Acá solo se agrega lo propio del bloque: avance visible mientras corre
        (cada reparación es trabajo real de imagen, no instantáneo), y que un
        fallo suelto no frene al resto.

        Los tres desenlaces se cuentan por separado a propósito: "sin nada que
        corregir" NO es un error (la detección de sombra/mordida es una ayuda,
        no un diagnóstico — el usuario marca con sus propios ojos y a veces la
        foto ya estaba bien), y mezclarlo con los errores reales daba un
        mensaje alarmante sobre un lote que salió perfecto.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        self.configure(cursor="watch")
        ok, sin_nada, errores = 0, 0, []
        try:
            for i, nombre in enumerate(nombres, start=1):
                self.v_lote.set(f"{etiqueta}… {i} de {len(nombres)}")
                self.update_idletasks()
                hecho, mensaje = corregir(nombre)
                if hecho:
                    ok += 1
                    self._remedir_recorte(nombre)
                    self._refrescar_miniatura_resumen(nombre)
                elif mensaje.startswith("no se pudo"):
                    errores.append(f"{nombre}: {mensaje}")
                else:
                    sin_nada += 1
        finally:
            self.configure(cursor="")

        detalle = f"{ok} de {len(nombres)} corregida(s)"
        if sin_nada:
            detalle += f", {sin_nada} sin nada que corregir"
        if errores:
            detalle += f", {len(errores)} con error"
        self.v_log.set(f"{etiqueta}: {detalle}.")
        # Se conserva la selección: es normal querer aplicar las DOS
        # correcciones al mismo conjunto (primero suela, después sombra), y
        # limpiar el lote acá obligaría a volver a marcar las 12 fotos.
        self._al_cambiar_lote(nombres)
        self._seleccionar()  # re-dibuja badges y visor de la foto en detalle
        self._refrescar_resumen_si_visible()
        if errores:
            messagebox.showwarning(
                etiqueta,
                f"{ok} de {len(nombres)} se corrigieron.\n\n"
                "Estas no se pudieron:\n" + "\n".join(f"  · {e}" for e in errores[:12]))

    def _reparar_suela_lote(self) -> None:
        self._corregir_lote("Reparar suela", self._reparar_suela_de)

    def _recortar_sombra_lote(self) -> None:
        self._corregir_lote("Recortar sombra", self._recortar_sombra_de)

    def _aprobar_lote(self) -> None:
        """"Aprobar" en bloque: las fotos que ya están bien tal cual, sin
        ninguna corrección.

        Es el reemplazo de la casilla "Aprobar" de la pantalla masiva
        eliminada. Usa el mismo `_decidir` que el botón "✓ Aprobar" de una
        sola foto — que ya hacía exactamente lo que hacía
        `_aprobar_sin_limpiar` (registrar "aprobado" sin tocar píxeles), más
        la guarda de no escribir decisiones sobre un recorte ya desechado.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        with reloj(self):
            n_ok = sum(1 for nombre in nombres if self._decidir("aprobado", nombre))
        self.tabla.limpiar_lote()
        self.v_log.set(f"{n_ok} de {len(nombres)} recorte(s) aprobado(s) sin corrección.")
        self._refrescar_resumen_si_visible()

    def _descartar_lote(self) -> None:
        """"Descartar" en bloque. Sin confirmación, igual que el «Descartar»
        de una sola foto: no borra nada, solo escribe una decisión que se
        puede cambiar volviendo a aprobar."""
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        with reloj(self):
            n_ok = sum(1 for nombre in nombres if self._decidir("descartado", nombre))
        self.tabla.limpiar_lote()
        self.v_log.set(f"{n_ok} de {len(nombres)} recorte(s) descartado(s).")
        self._refrescar_resumen_si_visible()

    def _desechar_lote(self) -> None:
        """Borrado real en bloque. La confirmación es más dura que la
        individual — dice cuántos recortes y cuántos archivos se van a borrar —
        porque acá el error no cuesta una foto sino N.

        Un fallo suelto (archivo abierto en otro programa, permisos) NO frena
        al resto: se sigue con las demás y al final se muestra un resumen con
        las que no se pudieron borrar.
        """
        nombres = self.tabla.lote()
        if not self._salida or len(nombres) < 2:
            return
        total_archivos = sum(len(self._archivos_del_recorte(n)) for n in nombres)
        muestra = "\n".join(f"  · {Path(n).stem}" for n in nombres[:12])
        if len(nombres) > 12:
            muestra += f"\n  · … y {len(nombres) - 12} más"
        if not messagebox.askyesno(
                "Desechar definitivamente",
                f"Esto borra permanentemente {len(nombres)} recorte(s) "
                f"({total_archivos} archivo(s) en disco):\n\n{muestra}\n\n"
                "NO SE PUEDE DESHACER. No es lo mismo que «Descartar», que "
                "solo marca las fotos y se puede revertir.\n\n¿Continuar?",
                icon="warning", default="no"):
            self.v_log.set("Desechado en bloque cancelado — no se borró nada.")
            return

        borrados, fallos = 0, []
        with reloj(self):
            for nombre in nombres:
                ok, error = self._desechar_uno(nombre)
                if ok:
                    borrados += 1
                else:
                    fallos.append(f"{nombre}: {error}")

        self.tabla.limpiar_lote(conservar_actual=False)
        self.v_log.set(f"{borrados} recorte(s) desechado(s) definitivamente"
                       + (f", {len(fallos)} con problemas." if fallos else "."))
        if fallos:
            messagebox.showerror(
                "Desechar definitivamente",
                f"Se borraron {borrados} de {len(nombres)} recortes.\n\n"
                "Estos no se pudieron borrar (puede que estén abiertos en otro "
                f"programa):\n\n{chr(10).join(fallos)}")
        self._refrescar_resumen_si_visible()

    def _refrescar_resumen_si_visible(self) -> None:
        """Si la pantalla de resumen está armada, sus conteos quedaron viejos
        después de un borrado en bloque."""
        if getattr(self, "_resumen_frame", None) is None:
            return
        # Se vuelve a armar de cero (destruir + mostrar): sus conteos salen de
        # `self._recortes` y de `decisiones.resumen`, y ambos cambiaron.
        self._ocultar_resumen()
        self._mostrar_resumen_final()

    def _alternar_limpieza_auto(self) -> None:
        """Un solo control para deshacer/rehacer la limpieza automática de
        este recorte (excedente recortado + suela completada), en vez de los
        4 botones separados que había antes. Por defecto la limpieza ya se
        aplicó sola durante el procesamiento; acá el usuario puede decir
        "no estoy de acuerdo" (deshacer) y, si cambia de opinión, "rehacer"."""
        if not self._nombre_actual or not self._salida:
            return
        nombre = self._nombre_actual
        tiene_original = bool(self._ruta_original(nombre))

        if self.btn_deshacer_auto["text"].startswith("Deshacer"):
            ok = reparar.revertir(self._salida, Path(nombre).stem)
            if not ok:
                self.v_log.set(f"{nombre}: no hay original guardado para revertir")
                return
            self.v_log.set(f"{nombre}: limpieza automática deshecha, vuelve al recorte original")
            self.btn_deshacer_auto.configure(text="Rehacer limpieza automática")
            self._ver_antes_despues.set(False)
            self._mostrar_imagen()
        else:
            r = self._recortes.get(nombre)
            if not r or not r.get("png"):
                return
            png = Path(r["png"])
            try:
                aplicado_algo = False
                if reparar.tiene_excedente(png, color_basura=reparar.COLOR_BASURA):
                    reparar.aplicar_recorte_excedente(self._salida, Path(nombre).stem, color_basura=reparar.COLOR_BASURA)
                    aplicado_algo = True
                s_actual = reparar.medir_suela(png, color_basura=reparar.COLOR_BASURA)
                if reparar.tiene_mordida_real(s_actual):
                    res = reparar.reparar(png, color_basura=reparar.COLOR_BASURA)
                    reparar.aplicar(self._salida, Path(nombre).stem, res)
                    aplicado_algo = True
            except ValueError:
                # ValueError aca no es fallo: es "no habia nada que corregir" en esa foto.
                pass
            except Exception as exc:  # noqa: BLE001
                self.v_log.set(f"{nombre}: no se pudo rehacer la limpieza ({exc})")
                return
            if not aplicado_algo:
                self.v_log.set(f"{nombre}: ya no había nada que corregir")
            else:
                self.v_log.set(f"{nombre}: limpieza automática vuelta a aplicar")
            self.btn_deshacer_auto.configure(text="Deshacer limpieza automática")
            self._mostrar_imagen()
