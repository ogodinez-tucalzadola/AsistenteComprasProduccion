"""
Herramienta única: procesar, revisar y decidir en una sola ventana.

Reemplaza el ir y venir entre pestañas de `gui_limpieza.py` por un solo flujo
continuo: se arranca el procesamiento y, a medida que van saliendo recortes,
la lista de la izquierda se llena — se puede empezar a revisar sin esperar a
que termine el lote completo.

El estado de cada recorte (falta%, mordida real, sombra vs. grupo, decisión)
no se recalcula acá: se lee directo del manifiesto, donde `worker.py` ya lo
dejó calculado una sola vez (ver `estado.py`).

Rediseño 2026-08: se simplificó el visor (un solo modo, contorno + antes/
después), se cambiaron los 5 botones manuales de acción por un solo control
de "deshacer/rehacer limpieza automática" por recorte, se agregó una vista en
vivo del proceso calzado a calzado durante el procesamiento, y una pantalla de
resumen final con la lámina original + los recortes + acceso a la carpeta de
salida. También se aplicó un tema visual propio (paleta + tipografía) en vez
del gris nativo de ttk.

Reescritura visual 2026-08-27 (`gui_profesional_ctk.py`): MISMA aplicación que
`gui_profesional.py`, con la capa visual reconstruida sobre `customtkinter`
(esquinas redondeadas, modo claro/oscuro que sigue al de Windows, casillas y
botones con color propio). La lógica de negocio no se tocó: los cuerpos de las
funciones que hacen algo (procesar, detener, pausar, exportar, aplicar
limpieza, etc.) son los mismos, letra por letra, que en el archivo original.

Para poder reusar esos cuerpos tal cual — que llaman `.state(["disabled"])`,
`btn["text"]` y `barra.configure(value=...)`, API de `ttk`, no de CTk — se
definen abajo unas clases puente (`Boton`, `Casilla`, `Barra`, `Etiqueta`,
`Marco`) que hablan las dos APIs. Así ni una sola línea de lógica tuvo que
reescribirse para el cambio de librería.

La lista de recortes de la izquierda dejó de ser una tabla de texto: ahora es
`GrillaRecortes`, una grilla de tarjetas con la foto de cada recorte y su
estado en badges de color — el mismo patrón que la pantalla "Elegir qué
limpiar". Esa clase expone la MISMA API mínima que exponía el `ttk.Treeview`
que reemplaza, así que la lógica que la puebla y la lee (`_recargar_manifiesto`,
`_seleccionar`, `_decidir`, `_vaciar_carpeta_trabajo`) quedó intacta: lo único
que cambió es cómo se ve y cómo se elige un recorte, no qué pasa al elegirlo.
"""

from __future__ import annotations

import sys

# Bug real (2026-09-09): "NoneType object has no attribute 'write'" en el
# paso 5, al enviar a calificar. Causa raíz: esta app corre con `pythonw.exe`
# (sin consola, para que no aparezca una ventana negra), y sin consola
# `sys.stdout`/`sys.stderr` son `None`. El motor de calificación compartido
# (`servidor_pty.py`, en el proyecto GestionTUC, usado también por su propia
# herramienta web) tiene varios `print(..., flush=True)` de progreso que
# nunca esperaron correr sin consola -- cualquiera de esos `print()` revienta
# el hilo de envío. Se arregla ACÁ, sin tocar ese archivo compartido con otro
# proyecto: si no hay consola, se les da a `print()` un destino que sí sabe
# `.write()` (no hace nada con el texto, pero no explota).
if sys.stdout is None or sys.stderr is None:
    import io
    _sumidero = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = _sumidero
    if sys.stderr is None:
        sys.stderr = _sumidero

import contextlib
import ctypes
import json
import logging
import os
import queue
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

import psutil

import almacen
import control_extraccion
import decisiones
import indice_fuente
import nucleo
import reparar
import rutas_externas

# División 2026-09-23, Fase 0: el logger de la interfaz, los badges y las
# utilidades de ventana/foco/foto se mudaron a `util_ventana.py` (ver el
# porqué allá). Se re-importan acá con los mismos nombres para que la lógica
# de este archivo -- y `etiquetado_calibracion.py`, que importa
# `_cargar_foto_generico` desde acá -- siga funcionando sin cambios.
from util_ventana import (  # noqa: E402
    _LOG_DIR, _log_gui, _logger_gui,
    _badge, _centrar_en_ventana_principal, _nombre_archivo_seguro,
    reloj, _hwnd_real_windows, _foreground_es_de_esta_app,
    _forzar_foreground_windows, _traer_al_frente, _cargar_foto_generico,
)

# Carpeta central donde vive cada corrida de lote, una subcarpeta por
# proyecto (nombrada como el usuario lo bautiza en el diálogo inicial). Antes
# cada lote se guardaba donde el usuario eligiera al vuelo (ej. mezclado con
# las fotos de entrada), sin ningún lugar fijo para ver el historial completo.
# Auditoría 2026-09-23 (M9): la ruta en sí vive centralizada en `rutas_externas.py`.
CARPETA_LOTES_CENTRAL = rutas_externas.ASISTENTE_COMPRAS_LOTES

try:
    import io as _io

    import win32clipboard  # type: ignore
    import win32con  # type: ignore
    _CLIPBOARD_DISPONIBLE = True
except ImportError:  # pywin32 no instalado: se usa "Guardar como…" como opción principal
    _CLIPBOARD_DISPONIBLE = False

PROYECTO = Path(__file__).resolve().parent

# División 2026-09-23, Fase 0: la paleta, las fuentes y los estilos de texto
# se mudaron a `tema.py`. Se re-importan acá con los mismos nombres: hay
# cientos de usos en este archivo y varios en `etiquetado_calibracion.py`.
from tema import (  # noqa: E402
    PAR_FONDO, PAR_PANEL, PAR_PANEL_SUAVE, PAR_BORDE,
    PAR_TEXTO, PAR_TEXTO_SUAVE, PAR_PRIMARIO, PAR_PRIMARIO_OSCURO,
    PAR_PRIMARIO_SUAVE, PAR_ACENTO, PAR_OK, PAR_OK_HOVER,
    PAR_OK_BG, PAR_ALERTA, PAR_ALERTA_BG, PAR_MAL,
    PAR_MAL_HOVER, PAR_MAL_BG, PAR_NEUTRO, PAR_VISOR_BG,
    PAR_SELECCION, solido, COLOR_FONDO, COLOR_PANEL,
    COLOR_BORDE, COLOR_TEXTO, COLOR_TEXTO_SUAVE, COLOR_PRIMARIO,
    COLOR_PRIMARIO_OSCURO, COLOR_ACENTO, COLOR_OK, COLOR_OK_BG,
    COLOR_ALERTA, COLOR_ALERTA_BG, COLOR_MAL, COLOR_MAL_BG,
    COLOR_NEUTRO, COLOR_VISOR_BG, COLOR_BORDE_FUERTE, RADIO_CONTROL,
    RADIO_PANEL, FUENTE_BASE, FUENTE_TITULO, FUENTE_SUBTITULO,
    FUENTE_CHICA, F, armar_fuentes, ESTILOS_TEXTO,
)

# División 2026-09-23, Fase 0: las clases puente ttk->CTk se mudaron a
# `widgets_puente.py`. Se re-importan acá con los mismos nombres.
from widgets_puente import (  # noqa: E402
    _EstadoTtk, Boton, Casilla, CasillaOk,
    Barra, Etiqueta, Marco, Tarjeta,
    Separador, SeparadorV, BotonMenu,
)


# División 2026-09-23, Fase 3: la grilla de tarjetas de recortes se mudó a
# `grilla_recortes.py` (ver el porqué allá). `HerramientaUnica` la sigue
# instanciando por nombre, así que se re-importa acá.
from grilla_recortes import GrillaRecortes  # noqa: E402


# División 2026-09-23, Fase 4: `HerramientaUnica` era UNA clase de ~7,200
# líneas con los 7 pasos del asistente adentro. Se partió en mixins por TEMA
# (ver el docstring de cada archivo). Son mixins y no clases con vida propia a
# propósito: todo se combina en la MISMA instancia, así que `self.<lo que sea>`
# sigue funcionando entre bloques exactamente igual que antes -- lo único que
# se movió es el TEXTO de cada método, letra por letra.
from mixin_persistencia_lote import _MixinPersistenciaLote  # noqa: E402
from mixin_extraccion import _MixinExtraccion  # noqa: E402
from mixin_revision import _MixinRevision  # noqa: E402
from mixin_resumen import _MixinResumen  # noqa: E402


# (`_badge` y `_centrar_en_ventana_principal` viven en `util_ventana.py`
#  desde la Fase 0 de la división 2026-09-23)


class HerramientaUnica(_MixinPersistenciaLote, _MixinExtraccion, _MixinRevision,
                       _MixinResumen, ctk.CTk):
    # ── carpeta del lote activo ──────────────────────────────────────────────
    #
    # `_salida` es una PROPIEDAD y no un atributo simple por una razón concreta
    # de la Fase 2: desde que la revisión de catálogo vive en el `lote.sqlite`
    # del lote, el motor de calificación necesita saber CUÁL lote es antes de
    # que se lo consulte, y esa carpeta es justamente `_salida`.
    #
    # Se asigna en seis lugares distintos del flujo (elegir carpeta, retomar el
    # último lote, crear proyecto nuevo, procesar, anotar la marca del lote…).
    # Poner el aviso al motor en cada uno de esos seis lugares habría dejado el
    # séptimo sin avisar la próxima vez que alguien agregue un camino -- y el
    # síntoma es un error a mitad del flujo ("No hay lote activo") en el paso 6,
    # lejos de la causa. Con la propiedad, declarar el lote es una consecuencia
    # automática de fijar la carpeta: no hay forma de fijar una sin lo otro.
    #
    # El default a nivel de CLASE existe para que leer `self._salida` antes de
    # que `__init__` la fije (customtkinter corre bastante código propio en su
    # `super().__init__()`) devuelva None en vez de romper con AttributeError.
    __salida = None

    @property
    def _salida(self):
        return self.__salida

    @_salida.setter
    def _salida(self, valor) -> None:
        self.__salida = Path(valor) if valor is not None else None
        try:
            import motor_calificacion
            motor_calificacion.fijar_lote(self.__salida)
        except Exception as exc:  # noqa: BLE001
            _logger_gui().exception("no se pudo declarar el lote activo al motor de calificacion")
            # Que el motor no se pueda avisar NO debe tumbar la interfaz: la
            # carpeta igual queda fijada y los pasos 1-5 (limpieza de fotos) no
            # dependen del motor de calificación en absoluto. El paso 6 sí, y
            # ahí el error se ve con su mensaje propio.
            print(f"  [aviso] no se pudo declarar el lote activo al motor: {exc}", flush=True)

    def __init__(self) -> None:
        super().__init__()
        armar_fuentes()
        self.title("Asistente de Compras")
        self._geometria_que_entre(1240, 760)
        self.minsize(900, 560)
        # Abrir MAXIMIZADA por defecto: con 3 columnas (grilla de recortes +
        # visor + panel de decisión), una ventana de 1240x760 sin maximizar le
        # deja al visor central menos de 600px de ancho real -- causa raíz de
        # que la foto grande del paso 4 se viera "muy pequeña" pese a que el
        # cálculo de escalado (`_espacio_visor`) ya usa el tamaño real: el
        # tamaño real disponible era chico de entrada. `state("zoomed")` es
        # la forma correcta en Windows (Tk no tiene un "maximizar" portable).
        # Y hay que REPETIRLO con `after`: customtkinter, dentro de su propio
        # arranque, restaura el estado de la ventana cuando aplica el color de
        # la barra de título -- medido el 2026-09-09, la ventana volvía a
        # `state() == "normal"` con 1495x774 pese a este llamado, y por eso el
        # paso 4 seguía viéndose chico (el cuerpo se quedaba con 462px de alto
        # y el visor de la foto con 199px).
        def _maximizar() -> None:
            try:
                if self.state() != "zoomed":
                    self.state("zoomed")
            except Exception:  # noqa: BLE001
                pass  # plataforma sin soporte -- se queda con la geometría fija

        _maximizar()
        self.after(200, _maximizar)
        self.after(700, _maximizar)
        self.configure(fg_color=PAR_FONDO)
        # Bug real de customtkinter (2026-09-08, reclamo del usuario -- se veía
        # el logo de Python en la barra de tareas): CTk.__init__ (llamado en
        # super().__init__() arriba) programa su PROPIO ícono por defecto con
        # un `after()` interno que corre DESPUÉS de este punto, pisando
        # cualquier iconbitmap ya puesto acá. Hay que reforzarlo con un
        # after() propio que corra más tarde -- el llamado inmediato no
        # alcanza, se ve el ícono correcto un instante y después vuelve al de
        # Python.
        ruta_icono = str(PROYECTO / "icono_app.ico")

        def _forzar_icono() -> None:
            try:
                self.iconbitmap(ruta_icono)
            except tk.TclError:
                pass  # sin ícono no rompe la app, solo se ve la genérica de Tk

        _forzar_icono()
        self.after(150, _forzar_icono)
        self.after(500, _forzar_icono)

        # Pedido del usuario (2026-09-23): que la ventana principal se ponga
        # AL FRENTE al abrir (con doble clic en el ícono del escritorio), y
        # también al restaurarla desde la barra de tareas. El simple toggle
        # de `-topmost` (`_traer_al_frente`, alcanza para las ventanas
        # emergentes) NO alcanza acá: el arranque de esta ventana tarda lo
        # suficiente (varias librerías pesadas) como para que Windows le
        # retire a la app el permiso de auto-enfocarse que le da a un
        # programa recién abierto -- ver `_forzar_foreground_windows` para
        # el porqué exacto y la técnica real (Win32 `AttachThreadInput`).
        # `<Map>` es el evento que dispara Tk cuando la ventana pasa de
        # minimizada/oculta a visible -- restaurarla desde la barra de
        # tareas es exactamente eso.
        #
        # OJO con el filtro `evento.widget is self`: `<Map>` no es solo del
        # root, lo dispara CADA widget hijo al hacerse visible y burbujea
        # hasta acá. Sin el filtro esto corría cientos de veces durante el
        # armado de la pantalla (medido el 2026-09-23: ~300 llamadas en 2
        # segundos), peleando con customtkinter mientras rehace la ventana.
        def _al_mapear(evento) -> None:
            if evento.widget is self:
                _forzar_foreground_windows(self, intentos=3)

        self.after(750, lambda: _forzar_foreground_windows(self))
        self.bind("<Map>", _al_mapear)

        self._armar_estilo()

        self._q: queue.Queue = queue.Queue()
        self._procs: list[subprocess.Popen] = []
        self._procs_pendientes: int = 0  # cuántos workers de limpieza siguen vivos (H4: puede ser > 1)
        self._pausado: bool = False
        self._salida = None   # ver la propiedad `_salida` más abajo: asignarla
        # también le declara el lote activo al motor de calificación (Fase 2)
        self._entrada_archivos: list[Path] | None = None  # si se eligió "Archivos…" en vez de carpeta
        self._recortes: dict[str, dict] = {}   # nombre -> entrada de recorte (manifiesto)
        # ¿Los recortes que hay en pantalla vienen de una sesión ANTERIOR
        # (lote retomado) o se acaban de procesar acá? El paso 3 se explica
        # distinto en cada caso — ver `_refrescar_aviso_retomar`.
        self._retomando_lote: bool = False
        self._foto_actual: ImageTk.PhotoImage | None = None
        self._foto_visor_vivo: ImageTk.PhotoImage | None = None
        self._fotos_resumen: list[ImageTk.PhotoImage] = []
        self._rutas_pendientes: dict[str, Path] = {}
        self._nombre_actual: str | None = None
        self._nombre_proyecto: str | None = None  # nombre del proyecto actual
        self._ver_antes_despues = tk.BooleanVar(value=False)
        # Las líneas de referencia (azul = borde real, roja = curva esperada)
        # se dibujaban SIEMPRE y sin forma de apagarlas en esta pantalla; el
        # interruptor para verlas solo existía en el visor del resumen final.
        self._ver_lineas = tk.BooleanVar(value=True)
        self._resumen_frame: ctk.CTkFrame | None = None
        self._widgets_resumen_recorte: dict[str, tuple[tk.Label, tk.Label]] = {}
        # Fotos que el comprador reparó/recortó a mano en esta sesión: al
        # aprobarlas, su visor se cierra solo (ver `_decidir`).
        self._corregidas_a_mano: set[str] = set()
        # ¿Hay un envío a calificar corriendo en segundo plano? Lo usa el
        # avance propio del paso 5 de vectorización (`barra_compras`).
        self._enviando_compras = False
        # ── flujo por pasos (plan 2026-09-07, paso 8) ──
        # El proveedor se elige al PRINCIPIO (barra 1), no después de procesar
        # las fotos: es lo primero que el comprador sabe del catálogo que tiene
        # en la mano. `_enviar_a_asistente_compras` lo lee de acá.
        self._proveedor_id_actual: int | None = None
        self._proveedor_nombre_actual: str | None = None
        # FASE 1 multi-proveedor (plan 2026-09-16): un lote puede traer los
        # catálogos de VARIOS proveedores. La lista es el historial completo
        # de pares (proveedor, fuente de fotos) agregados a este lote; los dos
        # escalares de arriba siguen existiendo y apuntan SIEMPRE al proveedor
        # "en edición"/activo, porque decenas de lugares del código los leen y
        # el caso normal (un solo proveedor) tiene que seguir funcionando
        # exactamente igual. Con un solo proveedor la lista tiene un elemento
        # y no cambia ningún comportamiento.
        self._proveedores_del_lote: list[dict] = []
        self._indice_proveedor_actual: int = 0
        # La vista de candidatos ya NO es un Toplevel aparte: es una pantalla
        # de reemplazo más dentro de la ventana principal, igual que el
        # resumen final o la selección de limpieza.
        self._candidatos_frame: ctk.CTkFrame | None = None
        self._fotos_visor_recorte: dict[str, ImageTk.PhotoImage] = {}

        # Pantalla del paso 5 ("Vectorizar y comparar"): reemplaza al diálogo
        # emergente que preguntaba el método justo antes del envío.
        self._vector_frame: ctk.CTkFrame | None = None

        # ¿Hay workers de limpieza corriendo AHORA? Es lo que decide si el
        # monitor de avance se ve en el paso 3 fusionado ("Limpiar y revisar"):
        # mientras corre se ve arriba, y al terminar se retira solo y la
        # pantalla queda entera para decidir fotos.
        self._procesando_lote: bool = False

        # ── revisión previa de fotos extraídas de Excel ──
        self._revision_frame: tk.Frame | None = None
        self._fotos_revision: list[ImageTk.PhotoImage] = []
        self._archivos_revision: list[Path] = []
        self._exclusiones_revision: set[str] = set()
        self._marcos_revision: dict[str, tk.Frame] = {}
        self.v_estado_revision = tk.StringVar(value="")

        # Acá vivía el estado de la pantalla masiva "Elegir recortes a
        # limpiar" (`_limpieza_frame`, `_seleccion_recorte`,
        # `_seleccion_reparo`, `_seleccion_aprobar`, sus miniaturas y marcos).
        # Se eliminó el 2026-09-08: ofrecía las MISMAS dos correcciones que la
        # barra "CORREGIR ESTA FOTO" de la grilla de revisión, y el usuario
        # eligió esa experiencia ("me gusta más la pestaña revisar, queda más
        # bonito la parte de revisión manual"). La eficiencia de aplicar a
        # varias fotos de una pasada se conserva con la selección múltiple que
        # la grilla YA tenía (Ctrl/Shift+clic → barra de acciones en bloque).

        # Cuántas columnas entran AHORA en cada grilla de miniaturas, para no
        # re-acomodar en cada evento de `<Configure>` si el número no cambió
        # (Tk emite `<Configure>` a cada píxel de arrastre del borde).
        self._cols_grilla: dict[str, int] = {}

        self._armar_ui()
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        # `after_idle`, no llamada directa: acá la ventana todavía no está
        # mapeada (eso pasa recién en mainloop()), así que `winfo_width()` da
        # basura (200) y el diálogo de arranque se centraba mal — quedaba
        # pegado a la esquina superior izquierda. Confirmado con captura real.
        # Reloj de arena mientras la ventana termina de armarse y aparece el
        # primer diálogo (retomar/nuevo proyecto) -- ese primer instante en
        # blanco es justo cuando más parece que el programa no abrió.
        self.configure(cursor="watch")
        self.after(600, lambda: self.configure(cursor=""))
        self.after_idle(self._cargar_ultima_carpeta)
        self.after(150, self._bombear_cola)

    def _geometria_que_entre(self, ancho: int, alto: int) -> None:
        """Abre la ventana del tamaño pedido, pero nunca más grande que la
        pantalla — y centrada.

        Bug real (medido 2026-09-07): customtkinter multiplica la geometría
        por el escalado de Windows, así que el `geometry("1240x760")` de toda
        la vida se convertía en una ventana de 1550x950 px reales sobre un
        monitor de 1536x864. La franja de abajo quedaba fuera de la pantalla.
        Antes eso solo escondía la línea de log; con el asistente por pasos,
        lo que se perdía era el botón «Siguiente →» — o sea, la app entera.
        """
        try:
            from customtkinter.windows.widgets.scaling import ScalingTracker
            escala = ScalingTracker.get_window_scaling(self) or 1.0
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo leer la escala de la ventana; se asume 1.0")
            escala = 1.0
        # Margen para la barra de tareas y el borde de la ventana.
        max_ancho = int((self.winfo_screenwidth() - 40) / escala)
        max_alto = int((self.winfo_screenheight() - 90) / escala)
        ancho, alto = min(ancho, max_ancho), min(alto, max_alto)
        x = max(0, int((self.winfo_screenwidth() - ancho * escala) / 2))
        y = max(0, int((self.winfo_screenheight() - alto * escala) / 3))
        self.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _al_cerrar(self) -> None:
        """Si hay un proceso de limpieza vivo (corriendo o pausado) al cerrar
        la ventana, hay que reanudarlo antes de matarlo — un proceso que
        `psutil` dejó suspendido queda congelado para siempre si se cierra
        así nomás: invisible, sin ventana, reteniendo memoria y bloqueando
        archivos de la carpeta de trabajo hasta que alguien lo mate a mano
        desde el Administrador de tareas."""
        vivos = [p for p in self._procs if p.poll() is None]
        if vivos:
            if self._pausado:
                for p in vivos:
                    try:
                        psutil.Process(p.pid).resume()
                    except psutil.Error:
                        # el proceso ya termino por su cuenta: no hay nada que reanudar.
                        pass
            for p in vivos:
                p.terminate()
        self.destroy()

    # ── tema visual ──────────────────────────────────────────────────────

    def _armar_estilo(self) -> None:
        """Toda la interfaz es customtkinter, así que ya no hay nada de ttk
        que estilizar (la tabla de texto que quedaba se reemplazó por la
        grilla de tarjetas `GrillaRecortes`). Se deja igual el tema base y los
        tokens de texto/fondo por si algún diálogo del sistema hereda de ahí
        — así ninguna ventana nace con el gris nativo de Windows."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            # tema "clam" no disponible en esta instalacion de Tk: se usa el nativo.
            pass
        style.configure(".", background=COLOR_PANEL, foreground=COLOR_TEXTO,
                        fieldbackground=COLOR_PANEL, font=FUENTE_BASE,
                        borderwidth=0, relief="flat")

    # ── construcción de la ventana ───────────────────────────────────────

    # ══ construcción de la ventana: asistente por pasos ═════════════════
    # Rediseño 2026-09-07. Antes la ventana era UNA pantalla con tres barras
    # de herramientas apiladas arriba (11 controles compitiendo a la vez) que
    # seguían visibles incluso en pantallas donde no servían de nada — y con
    # la fila de contexto tan llena que los textos se pisaban entre sí
    # ("0 aprobada(s) de 20" encima de "Carpeta activa: …", visto en captura).
    #
    # Ahora es un asistente: CABECERA que dice en qué paso se está, UNA
    # pantalla a la vez en el medio, y un PIE fijo con "← Atrás" y
    # "Siguiente →". Todo lo que no es el camino principal (elegir qué
    # limpiar, resumen, candidatos, ajustes, caché, revertir) vive en el menú
    # "⋮ Más opciones" del pie.
    #
    # Los widgets y las variables conservan sus nombres de siempre: cambia
    # DÓNDE viven y CUÁNDO se ven, no la lógica que los lee ni los comandos
    # que disparan.

    # "Confirmar y enviar" (2026-09-08): daba la falsa sensación de ser el último
    # paso -- reclamo real del usuario, tenía razón: esta pantalla es un
    # CHECKPOINT antes de enviar al Asistente de Compras (paso 4), no el
    # final del flujo. "Confirmar y enviar" deja claro que falta un paso más.
    # Un paso = una cosa que el comprador hace (2026-09-08, pedido repetido del
    # usuario). Antes el paso 2 ("Revisar fotos") metía TRES trabajos distintos
    # en una sola pantalla: elegir cuáles de las fotos extraídas entran, correr
    # la limpieza, y decidir foto por foto. Eran tres, y ahora se numeran como
    # tres — el mapa del proceso es el mismo siempre, aunque un origen puntual
    # no tenga que pararse en todas las paradas (ver `_ir_a_paso`).
    # Fusión de "Limpiar fotos" + "Revisar y decidir" en UN solo paso
    # (2026-09-16, pedido del dueño: "se parecen mucho, deberían ser uno").
    # Eran dos pastillas para la MISMA pantalla: `_ir_a_paso` mostraba
    # `self.cuerpo` en los dos casos, y la única diferencia era que en el 3
    # arriba se veía el monitor de avance. Encima la grilla de abajo ya se
    # llenaba EN VIVO (cada evento "foto" del worker llama a
    # `_recargar_manifiesto`, que es diferencial), así que el comprador ya
    # podía decidir fotos mientras el lote seguía corriendo — y aun así el
    # asistente lo obligaba a tocar "Continuar" para "pasar" a una pantalla
    # idéntica. Ahora es un paso: el monitor está arriba mientras corre y se
    # retira solo al terminar (ver `_actualizar_paso`), y las fotos van
    # apareciendo abajo listas para decidir.
    #
    # "Vectorizar y comparar" (paso 5) reemplaza al diálogo emergente que
    # preguntaba el método justo antes de enviar: elegir el espacio de
    # vectores, calcular los vectores de cada candidato y comparar contra el
    # catálogo es trabajo real, con minutos de espera, así que tiene su propia
    # pantalla con su progreso en vez de esconderse en un popup.
    PASOS_FLUJO = {
        1: "Elegir proveedor y origen",
        2: "Elegir qué limpiar",
        3: "Limpiar y revisar",
        4: "Confirmar y enviar",
        5: "Vectorizar y comparar",
        6: "Candidatos calificados",
        7: "Sugerido de compra",
    }

    # Rótulo corto para las pastillas de progreso. Con 7 pasos, los títulos
    # completos no entran en una hilera (en una pantalla de portátil la fila se
    # cortaba por el borde y los últimos pasos quedaban invisibles). El título
    # largo sigue leyéndose entero abajo, en el encabezado del paso actual.
    PASOS_PILL = {
        1: "Proveedor",
        2: "Qué limpiar",
        3: "Limpiar y revisar",
        4: "Confirmar",
        5: "Vectorizar",
        6: "Candidatos",
        7: "Sugerido",
    }

    GUIA_PASOS = {
        1: "Decime de quién es el catálogo y dónde están las fotos. "
           "Después tocá «Siguiente».",
        2: "Estas son las fotos que salieron del catálogo. Hacé clic en las que "
           "no son calzado (logos, portadas) para dejarlas afuera. "
           "Después, «Continuar».",
        3: "Tocá «▶ Limpiar fotos»: recorta el fondo y repara lo que puede. "
           "Cada foto que termina aparece abajo y ya se puede decidir "
           "(aprobar o descartar), sin esperar al resto del lote.",
        4: "Este es el resultado del lote. Desde acá se exporta y se manda al "
           "Asistente de Compras.",
        5: "Elegí con qué método se vectoriza y tocá «Calcular y continuar»: "
           "se calculan los vectores de cada candidato y se comparan contra el "
           "catálogo. Puede tardar varios minutos.",
        6: "Candidatos ya calificados, con su grado de compra y sus "
           "comparables.",
        7: "Poné el PDV objetivo y generá el pedido sugerido: cuántos pares de "
           "cada referencia conviene comprar.",
    }

    # Guía del paso 3 MIENTRAS el lote se está limpiando: lo que el comprador
    # tiene que saber ahí no es "tocá Limpiar fotos" (ya lo tocó), es que puede
    # empezar a trabajar sobre lo que ya salió.
    GUIA_LIMPIANDO = ("Se están limpiando las fotos. A medida que cada una "
                      "termina aparece abajo y ya podés decidirla: no hace "
                      "falta esperar a que termine todo el lote.")

    # Guía alterna del paso 3 cuando el lote viene de una sesión anterior: las
    # fotos ya están limpias, lo único que falta es decidirlas.
    GUIA_RETOMANDO = ("Estas fotos ya se limpiaron antes. Decidí foto por foto: "
                      "aprobar o descartar. Cuando termines, «Continuar».")

    # Qué dice el botón principal en cada paso. En el 4 no hay siguiente: es
    # el final del camino, así que el botón queda apagado en vez de
    # desaparecer (un botón que se va de su lugar desorienta más que uno gris).
    #
    # Paso 2 (2026-09-08, reclamo real del usuario): "Terminar lote" daba la
    # falsa sensación de ser el último paso -- igual que pasaba con "Lote
    # terminado" (renombrado a "Confirmar y enviar"). Esto SOLO avanza a esa
    # pantalla de confirmación, no termina nada. "Continuar" es honesto sobre
    # lo que hace: seguir al siguiente paso.
    TEXTO_SIGUIENTE = {
        1: "Siguiente  →",
        2: "Continuar  →",
        3: "Continuar  →",
        # Paso 4: el botón de pie ES la acción. Dice "Continuar  →" como en el
        # resto del asistente y queda en el mismo lugar de siempre, pero
        # `_paso_siguiente` le manda la lógica inteligente de compras
        # (`_accion_boton_compras`): manda al paso de vectorización si el lote
        # nunca se envió, o abre los candidatos si ya estaba enviado. Antes
        # había un botón aparte dentro de la tarjeta y este se escondía; el
        # usuario pidió un único botón, con el nombre y la posición del resto
        # (2026-09-09).
        4: "Continuar  →",
        # Paso 5: el botón de pie dispara el cálculo real (vectores +
        # similitud), igual que el botón grande de la pantalla. Se llama por lo
        # que hace, no "Continuar": son varios minutos de trabajo.
        5: "Calcular y continuar  →",
        6: "Armar sugerido  →",
        7: "Siguiente  →",
    }

    def _armar_ui(self) -> None:
        self._armar_cabecera()
        self._armar_pantalla_inicio()
        self._armar_monitor()
        self._armar_cuerpo_revision()
        self._armar_pie()

        self.v_log = tk.StringVar(value="")
        self._lbl_log = Etiqueta(self, textvariable=self.v_log, style="Suave.TLabel",
                                 padding=(24, 2))
        # Oculta cuando no tiene texto -- reclamo del usuario en el paso 4:
        # "46 recorte(s) ya procesados encontrados en esta carpeta" quedaba
        # anotado UNA vez al retomar el lote y de ahí en más ocupaba una
        # franja entera al pie de la ventana, vacía, sin volver a limpiarse.
        def _refrescar_visibilidad_log(*_a) -> None:
            if self.v_log.get().strip():
                if not self._lbl_log.winfo_ismapped():
                    self._lbl_log.pack(side="bottom", fill="x")
            elif self._lbl_log.winfo_ismapped():
                self._lbl_log.pack_forget()
        self.v_log.trace_add("write", _refrescar_visibilidad_log)

        self._paso_flujo = 1
        self._ir_a_paso(1)

    # ── cabecera: en qué paso estoy ──────────────────────────────────────

    def _armar_cabecera(self) -> None:
        cab = ctk.CTkFrame(self, fg_color=PAR_PANEL, corner_radius=0)
        cab.pack(side="top", fill="x")

        # Pastillas de progreso: los 7 pasos siempre a la vista, el actual
        # relleno de azul, los ya hechos en verde suave, los que faltan
        # apagados. Reemplazan a la línea de texto "Paso N de 4 · …", que no
        # dejaba ver ni dónde empieza ni cuánto falta. Un origen que no usa el
        # paso 2 lo ve en verde (pasado), no desaparecido: el mapa no cambia.
        #
        # Van en HILERA y en su propia fila, no apiladas al costado del
        # título: apiladas, las pastillas fijaban un piso de ~110px de alto
        # para toda la cabecera, y en una pantalla de portátil (donde la
        # ventana entra a 619px de alto) eso le comía al panel de decisión de
        # abajo justo los botones «✓ Aprobar / ✗ Descartar». Visto en captura.
        self._pills: dict[int, ctk.CTkLabel] = {}
        fila_pills = Marco(cab, padding=(26, 10, 26, 0))
        fila_pills.pack(fill="x")
        for n in sorted(self.PASOS_FLUJO):
            p = ctk.CTkLabel(fila_pills,
                             text=f"  {n}  {self.PASOS_PILL.get(n, self.PASOS_FLUJO[n])}  ",
                             font=F["chica"], anchor="w", height=26,
                             corner_radius=RADIO_CONTROL, fg_color="transparent",
                             text_color=PAR_TEXTO_SUAVE)
            p.pack(side="left", padx=(0, 6))
            self._pills[n] = p
        self._fila_pills = fila_pills

        fila = Marco(cab, padding=(26, 8, 26, 10))
        fila.pack(fill="x")
        # Este bloque (número de paso + titulazo + guía) ocupa ~110px y se
        # esconde cuando está abierta una de las dos grillas de elegir fotos
        # — ver `_compactar_cabecera`.
        self._bloque_titulo_paso = fila

        izq = Marco(fila)
        izq.pack(side="left", fill="x", expand=True)
        self.v_paso_num = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_num,
                 style="Paso.TLabel").pack(anchor="w", fill="x")
        self.v_paso_titulo = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_titulo,
                 style="Titulazo.TLabel").pack(anchor="w", fill="x", pady=(1, 0))
        self.v_paso_guia = tk.StringVar(value="")
        Etiqueta(izq, textvariable=self.v_paso_guia, style="Guia.TLabel",
                 wraplength=780).pack(anchor="w", fill="x", pady=(3, 0))

        # Franja fina de contexto: proveedor/avance a la izquierda, carpeta
        # activa a la derecha. Separadas por lado, no apiladas en la misma
        # tira: así ya no pueden pisarse cuando cualquiera de las dos crece.
        franja = ctk.CTkFrame(cab, fg_color=PAR_PANEL_SUAVE, corner_radius=0)
        franja.pack(fill="x")
        interior_franja = Marco(franja, padding=(26, 5, 26, 5))
        interior_franja.pack(fill="x")
        self.v_paso_flujo = tk.StringVar(value="")
        Etiqueta(interior_franja, textvariable=self.v_paso_flujo,
                 style="Suave.TLabel").pack(side="left")
        self.v_carpeta_activa = tk.StringVar(
            value="Carpeta activa: ninguna todavía")
        Etiqueta(interior_franja, textvariable=self.v_carpeta_activa,
                 style="Suave.TLabel", anchor="e").pack(side="right")

        Separador(cab).pack(fill="x", side="bottom")

    # ── pantalla 1: proveedor + origen ───────────────────────────────────

    def _armar_pantalla_inicio(self) -> None:
        """Los tres datos con que arranca un catálogo, cada uno en su propia
        tarjeta y en el orden en que el comprador los tiene en la cabeza.
        Antes eran cinco controles apretados en la primera barra, mezclados
        con el engranaje de ajustes y sin ningún rótulo que dijera para qué
        sirve cada uno."""
        self.pantalla_inicio = Marco(self)

        # Con scroll (2026-09-17): las tarjetas de esta pantalla venían
        # creciendo sesión tras sesión (canal de venta, agregar proveedor...)
        # sin que nadie ajustara el contenedor -- en una ventana no maximizada
        # o con letra grande de Windows, la tarjeta "2 · ¿Dónde están las
        # fotos?" quedaba tapada abajo, sin ninguna forma de llegar a ella.
        # `CTkScrollableFrame` reemplaza al `Marco` plano de antes; el resto
        # de la pantalla no se entera del cambio porque expone `pack()` igual.
        scroll = ctk.CTkScrollableFrame(self.pantalla_inicio, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        col = Marco(scroll)
        col.pack(anchor="n", pady=(26, 0))
        # Regla de ancho: fija la columna en 740px sin tener que apagar la
        # propagación de tamaño (que obligaría a declarar también un alto).
        tk.Frame(col, width=740, height=1, bg=COLOR_FONDO).pack()

        def tarjeta(rotulo: str, guia: str) -> Marco:
            t = Tarjeta(col)
            t.pack(fill="x", pady=(0, 14))
            dentro = Marco(t, padding=(20, 16, 20, 18))
            dentro.pack(fill="x")
            Etiqueta(dentro, text=rotulo, style="Titulo.TLabel").pack(anchor="w", fill="x")
            Etiqueta(dentro, text=guia, style="Suave.TLabel",
                     wraplength=680).pack(anchor="w", fill="x", pady=(2, 12))
            return dentro

        # ── 1. proveedor ──
        caja_prov = tarjeta("1 · ¿De quién es este catálogo?",
                            "El proveedor se elige una vez y el resto del flujo lo "
                            "usa sin volver a preguntar.")
        fila_prov = Marco(caja_prov)
        fila_prov.pack(fill="x")
        self.v_proveedor = tk.StringVar(value="Proveedor: sin elegir")
        Boton(fila_prov, text="🏷  Elegir proveedor…", style="Tarjeta.TButton",
              width=230, command=self._elegir_proveedor_inicial).pack(side="left")
        Etiqueta(fila_prov, textvariable=self.v_proveedor,
                 style="Subtitulo.TLabel").pack(side="left", padx=(16, 0))
        # FASE 1 multi-proveedor: un lote puede traer los catálogos de dos (o
        # más) proveedores. Este botón NO reemplaza lo elegido: lo guarda en la
        # lista del lote y vuelve a pedir proveedor + origen para el siguiente.
        Boton(caja_prov, text="➕  Agregar otro proveedor a este lote",
              style="Sutil.TButton", width=300,
              command=self._agregar_otro_proveedor).pack(anchor="w", pady=(10, 0))
        self.v_lista_proveedores = tk.StringVar(
            value="Todavía no se agregó ningún proveedor a este lote.")
        Etiqueta(caja_prov, textvariable=self.v_lista_proveedores,
                 style="Suave.TLabel", justify="left",
                 wraplength=680).pack(anchor="w", fill="x", pady=(8, 0))

        # ── 1b. canal de venta del lote ──
        #
        # Decisión del dueño (2026-09-10): TU Calzado vende por dos canales y
        # una compra es para uno o para el otro. De esto depende contra QUÉ se
        # califica el catálogo entero (lo vendido en TC Marcas, o lo vendido en
        # TU Calzado), así que se pregunta acá, en el paso 1, y no al final.
        #
        # SIN opción preseleccionada a propósito: que el comprador la elija
        # activamente en vez de heredar en silencio un default que después
        # decide el puntaje de todo el lote.
        caja_canal = tarjeta("1b · ¿Para qué canal es esta compra?",
                             "De esto depende contra qué se compara y se califica todo "
                             "el catálogo: lo que se ha vendido en TC Marcas, o lo que "
                             "se ha vendido en TU Calzado. Se elige una vez por lote.")
        self.v_canal_venta = tk.StringVar(value="")
        fila_canal = Marco(caja_canal)
        fila_canal.pack(fill="x")
        ctk.CTkRadioButton(fila_canal, text="TC Marcas",
                           variable=self.v_canal_venta, value="marca",
                           command=self._guardar_canal_venta_elegido).pack(side="left")
        ctk.CTkRadioButton(fila_canal, text="TU Calzado",
                           variable=self.v_canal_venta, value="tuc",
                           command=self._guardar_canal_venta_elegido).pack(side="left", padx=(24, 0))
        self.v_canal_estado = tk.StringVar(value="Canal: sin elegir")
        Etiqueta(caja_canal, textvariable=self.v_canal_estado,
                 style="Suave.TLabel").pack(anchor="w", pady=(8, 0))

        # ── 2. origen de las fotos ──
        caja_org = tarjeta("2 · ¿Dónde están las fotos?",
                           "Una carpeta de imágenes, o un Excel / PDF / correo del que "
                           "hay que sacarlas primero.")
        menu_origen = tk.Menu(self, tearoff=False)
        menu_origen.add_command(label="Carpeta de imágenes…", command=self._elegir_entrada)
        menu_origen.add_command(label="Archivos sueltos…", command=self._elegir_archivos)
        menu_origen.add_separator()
        menu_excel = tk.Menu(menu_origen, tearoff=False)
        menu_excel.add_command(label="Carpeta de Excel…", command=self._elegir_carpeta_excel)
        menu_excel.add_command(label="Archivo(s) de Excel…", command=self._elegir_archivos_excel)
        menu_origen.add_cascade(label="Excel (fotos incrustadas)", menu=menu_excel)
        menu_pdf = tk.Menu(menu_origen, tearoff=False)
        menu_pdf.add_command(label="Carpeta de PDF…", command=self._elegir_carpeta_pdf)
        menu_pdf.add_command(label="Archivo(s) de PDF…", command=self._elegir_archivos_pdf)
        menu_origen.add_cascade(label="PDF (catálogo/preventa)", menu=menu_pdf)
        menu_msg = tk.Menu(menu_origen, tearoff=False)
        menu_msg.add_command(label="Carpeta de correos…", command=self._elegir_carpeta_msg)
        menu_msg.add_command(label="Archivo(s) de correo…", command=self._elegir_archivos_msg)
        menu_origen.add_cascade(label="Correo (.msg, adjuntos)", menu=menu_msg)

        fila_org = Marco(caja_org)
        fila_org.pack(fill="x")
        BotonMenu(fila_org, menu=menu_origen, text="📂  Elegir origen  ▾",
                  style="Tarjeta.TButton", width=230).pack(side="left")
        self.v_entrada = tk.StringVar()
        ctk.CTkEntry(fila_org, textvariable=self.v_entrada, corner_radius=RADIO_CONTROL,
                     height=40, font=F["base"], fg_color=PAR_PANEL_SUAVE,
                     border_color=PAR_BORDE, text_color=PAR_TEXTO,
                     placeholder_text="todavía sin elegir"
                     ).pack(side="left", fill="x", expand=True, padx=(12, 0))

        # ── 3. carpeta de trabajo (dato técnico: tarjeta más callada) ──
        caja_sal = tarjeta("3 · Carpeta de trabajo",
                           "Dónde deja el programa los recortes mientras trabaja. "
                           "Si no la cambiás, se reusa la última.")
        fila_sal = Marco(caja_sal)
        fila_sal.pack(fill="x")
        self.v_salida = tk.StringVar()
        entry_salida = ctk.CTkEntry(fila_sal, textvariable=self.v_salida,
                                    corner_radius=RADIO_CONTROL, height=36, font=F["base"],
                                    fg_color=PAR_PANEL_SUAVE, border_color=PAR_BORDE,
                                    text_color=PAR_TEXTO)
        entry_salida.pack(side="left", fill="x", expand=True)
        entry_salida.bind("<Return>", lambda _e: self._elegir_salida(usar_texto=True))
        Boton(fila_sal, text="Elegir…", command=self._elegir_salida,
              width=110, height=36).pack(side="left", padx=(10, 0))
        # "Reprocesar aunque ya esté hecho" es una OPCIÓN del procesamiento, no
        # una acción: acá, con el resto de la preparación del lote, en vez de
        # colgando de la barra de botones donde parecía un control más del día
        # a día.
        self.v_rehacer = tk.BooleanVar(value=False)
        Casilla(caja_sal, text="Reprocesar aunque ya esté hecho",
                variable=self.v_rehacer).pack(anchor="w", pady=(12, 0))

    # ── monitor de avance (visible solo mientras se procesa) ─────────────

    def _armar_monitor(self) -> None:
        self.monitor = Tarjeta(self)
        monitor_int = Marco(self.monitor, style="Panel.TFrame", padding=14)
        monitor_int.configure(corner_radius=0)
        monitor_int.pack(fill="x")

        fila_top = Marco(monitor_int)
        fila_top.pack(fill="x")
        Etiqueta(fila_top, text="Progreso general del lote",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_lamina_actual = tk.StringVar(value="")
        Etiqueta(fila_top, textvariable=self.v_lamina_actual,
                 style="Panel.TLabel").pack(anchor="w", fill="x")

        fila_barra = Marco(monitor_int)
        fila_barra.pack(fill="x", pady=(8, 0))
        self.barra_progreso = Barra(fila_barra, mode="determinate", maximum=100)
        self.barra_progreso.pack(side="left", fill="x", expand=True, pady=6)
        self.v_pct = tk.StringVar(value="0%")
        Etiqueta(fila_barra, textvariable=self.v_pct, width=44,
                 style="SuavePanel.TLabel").pack(side="left", padx=(8, 0))

        # ── tiempo estimado restante ──
        # Se calcula con el promedio real de segundos por lámina TERMINADA
        # (medido en `_bombear_cola`, ver `self._marcas_lamina`), no con una
        # cuenta teórica: con 0 láminas listas no hay promedio y decir un
        # número sería inventarlo, así que dice "Calculando…".
        self.v_eta = tk.StringVar(value="Tiempo estimado restante: calculando…")
        Etiqueta(monitor_int, textvariable=self.v_eta,
                 style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(2, 0))

        # ── aviso de "parece colgado" ──
        # No detiene nada: solo avisa. La decisión de cortar sigue siendo del
        # usuario, con el botón "Detener" que ya existe. Vive en un contenedor
        # propio para poder aparecer y desaparecer sin mover el resto.
        # `tk.Frame` y NO `Marco`/`CTkFrame`, por la misma causa raíz ya
        # documentada en `_zona_aviso`: un CTkFrame SIN hijos empaquetados pide
        # 200x200 (los `width`/`height` por defecto de CTk). Con dos zonas así
        # (esta y la de la consola), el monitor pedía ~900px de alto y se
        # comía TODA la ventana — medido en captura: con el lote corriendo, la
        # grilla de revisión de abajo no se veía en absoluto. Eso era
        # tolerable cuando "limpiar" era una pantalla aparte; con el paso
        # fusionado ("Limpiar y revisar") es justo lo que había que arreglar:
        # el progreso arriba y las fotos que van saliendo abajo, a la vez.
        self._zona_sin_actividad = tk.Frame(monitor_int, background=COLOR_PANEL)
        self._zona_sin_actividad.pack(fill="x")
        self._aviso_sin_actividad = ctk.CTkFrame(
            self._zona_sin_actividad, fg_color=PAR_ALERTA_BG,
            border_color=PAR_ALERTA, border_width=1, corner_radius=RADIO_CONTROL)
        self.v_sin_actividad = tk.StringVar(value="")
        Etiqueta(self._aviso_sin_actividad, textvariable=self.v_sin_actividad,
                 text_color=PAR_ALERTA, style="SubtituloPanel.TLabel",
                 padding=(10, 8), wraplength=1100).pack(anchor="w", fill="x")

        Separador(monitor_int).pack(fill="x", pady=(10, 8))

        fila_top_lamina = Marco(monitor_int)
        fila_top_lamina.pack(fill="x")
        Etiqueta(fila_top_lamina, text="Lámina actual",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_paso_actual = tk.StringVar(value="")
        Etiqueta(fila_top_lamina, textvariable=self.v_paso_actual,
                 style="SuavePanel.TLabel", wraplength=1100).pack(anchor="w", fill="x",
                                                                  pady=(2, 0))

        fila_barra_lamina = Marco(monitor_int)
        fila_barra_lamina.pack(fill="x", pady=(8, 0))
        self.barra_progreso_lamina = Barra(fila_barra_lamina, mode="determinate", maximum=100)
        self.barra_progreso_lamina.pack(side="left", fill="x", expand=True, pady=6)
        self.v_pct_lamina = tk.StringVar(value="")
        Etiqueta(fila_barra_lamina, textvariable=self.v_pct_lamina, width=80,
                 style="SuavePanel.TLabel").pack(side="left", padx=(8, 0))

        self._armar_consola_detalle(monitor_int)

    def _ocultar_monitor(self) -> None:
        """Saca el monitor de avance de la pantalla (no lo destruye: el mismo
        widget se vuelve a empaquetar en el próximo `_procesar`)."""
        monitor = getattr(self, "monitor", None)
        if monitor is None:
            return
        try:
            if monitor.winfo_manager():
                monitor.pack_forget()
        except tk.TclError:
            # monitor ya destruido/no empaquetado: no hay nada que ocultar.
            pass

    # ── consola de detalle técnico (colapsada por defecto) ───────────────

    def _armar_consola_detalle(self, padre) -> None:
        """Log crudo de los procesos worker. Va OCULTO por defecto porque el
        usuario final no es técnico y no le dice nada; existe para cuando algo
        parece colgado y hay que ver si de verdad se movió algo."""
        self._consola_abierta = False

        fila_toggle = Marco(padre)
        fila_toggle.pack(fill="x", pady=(10, 0))
        self.btn_consola = Boton(fila_toggle, text="▸ Ver detalle técnico",
                                 style="Sutil.TButton", width=200, height=30,
                                 command=self._alternar_consola)
        self.btn_consola.pack(side="left")

        # `tk.Frame` vacío = 1x1; un CTkFrame vacío pediría 200x200 y con la
        # consola colapsada eso era alto muerto dentro del monitor (ver
        # `_zona_sin_actividad`).
        self._zona_consola = tk.Frame(padre, background=COLOR_PANEL)
        self._zona_consola.pack(fill="x")
        self.txt_consola = ctk.CTkTextbox(
            self._zona_consola, height=170, corner_radius=RADIO_CONTROL,
            fg_color=("#12161c", "#0c0f13"), text_color=("#d7dde5", "#d7dde5"),
            border_color=PAR_BORDE, border_width=1,
            scrollbar_button_color=PAR_BORDE,
            scrollbar_button_hover_color=PAR_TEXTO_SUAVE,
            font=("Consolas", 11), activate_scrollbars=True, wrap="none")
        self.txt_consola.configure(state="disabled")
        # No se empaqueta acá: arranca colapsada.

    def _alternar_consola(self) -> None:
        self._consola_abierta = not getattr(self, "_consola_abierta", False)
        if self._consola_abierta:
            if not self._zona_consola.winfo_manager():
                self._zona_consola.pack(fill="x")
            self.txt_consola.pack(fill="x", pady=(6, 0))
            self.btn_consola.configure(text="▾ Ocultar detalle técnico")
        else:
            self.txt_consola.pack_forget()
            # También se saca la ZONA, no solo la caja de texto: un contenedor
            # que ya tuvo alto no vuelve solo a 1px, así que al cerrar la
            # consola quedaban ~220px muertos dentro del monitor — y con el
            # paso 3 fusionado ese alto se lo come la grilla de revisión.
            self._zona_consola.pack_forget()
            self.btn_consola.configure(text="▸ Ver detalle técnico")

    _MAX_LINEAS_CONSOLA = 500

    def _limpiar_consola(self) -> None:
        """Vacía el log al arrancar un lote nuevo — mezclar el detalle de dos
        lotes distintos es peor que no tenerlo."""
        caja = getattr(self, "txt_consola", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.delete("1.0", "end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            # consola de detalle ya destruida (pantalla reconstruida): nada que limpiar.
            pass
        self._lineas_consola = 0

    def _log_consola(self, texto: str) -> None:
        """Agrega una línea cruda al log y deja la vista al final. Recorta las
        más viejas: un lote largo escupe miles de líneas y retenerlas todas
        haría crecer la memoria sin techo."""
        caja = getattr(self, "txt_consola", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.insert("end", texto.rstrip("\r\n") + "\n")
            self._lineas_consola = getattr(self, "_lineas_consola", 0) + 1
            if self._lineas_consola > self._MAX_LINEAS_CONSOLA:
                sobran = self._lineas_consola - self._MAX_LINEAS_CONSOLA
                caja.delete("1.0", f"{sobran + 1}.0")
                self._lineas_consola = self._MAX_LINEAS_CONSOLA
            caja.see("end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            # consola de detalle ya destruida: la linea de log simplemente no se ve.
            pass

    # ── pantalla 2: revisar fotos (lista + visor + decisión) ─────────────

    def _armar_cuerpo_revision(self) -> None:
        self.cuerpo = cuerpo = Marco(self)

        # ── franja de "estás retomando un lote" ──
        # Va en un contenedor propio empaquetado ARRIBA y ANTES que las tres
        # columnas: las columnas van con `side="left"` y se comen todo el alto,
        # así que una franja empaquetada después (cuando ya hay columnas) se
        # quedaría sin cavidad y Tk la desmaparía en silencio — el mismo
        # problema que ya había pasado con la franja de "corregir esta foto".
        # `tk.Frame` y NO `Marco`/`CTkFrame` a propósito — misma razón que
        # `_pie_lote` unas líneas más abajo, y causa raíz del bug medido el
        # 2026-09-09 ("al cerrar el aviso amarillo la pantalla queda cortada y
        # pequeña"): un CTkFrame SIN hijos empaquetados pide 200x200 (los
        # `width`/`height` por defecto de CTk), y esta zona va con `fill="x"`
        # sin `expand`, así que pack le entregaba sus ~200px de ALTO igual —
        # con el aviso escondido y también en un lote nuevo que nunca lo tuvo.
        # Al hacer `pack_forget()` del aviso, la zona volvía a pedir 200px en
        # vez de 1px, así que las tres columnas NO recuperaban el espacio: se
        # veía exactamente como si la franja siguiera ahí.
        # Un `tk.Frame` vacío pide 1x1, así que sin aviso no ocupa nada y los
        # hermanos elásticos (`izq`/`centro`/`der`, todos con `fill`) se
        # reacomodan solos en cuanto el aviso se cierra.
        self._zona_aviso = tk.Frame(cuerpo, background=COLOR_FONDO)
        self._zona_aviso.pack(side="top", fill="x")
        self._armar_aviso_retomar()

        # ── columna izquierda: lista de recortes ──
        izq = Tarjeta(cuerpo, width=310)
        izq.pack(side="left", fill="y", padx=(14, 7), pady=12)
        izq.pack_propagate(False)

        self._lbl_titulo_recortes = Etiqueta(izq, text="Recortes", padding=(14, 14, 14, 0),
                                             style="SubtituloPanel.TLabel")
        self._lbl_titulo_recortes.pack(anchor="w", fill="x")
        # Se guarda en un atributo porque se esconde mientras hay selección
        # múltiple: son 60px de alto en un panel de 236px, y en ese momento la
        # pista ya cumplió su función (el usuario acaba de hacer el Ctrl+clic
        # que explica). Ese espacio es justo el que necesita la barra de
        # acciones en bloque para poder mostrarse.
        self._lbl_ayuda_recortes = Etiqueta(
            izq, text="Hacé clic en una foto para verla en grande y decidir.  "
                      "Ctrl+clic marca varias, Shift+clic marca un rango.",
            style="SuavePanel.TLabel", padding=(14, 2, 14, 6), wraplength=268)
        self._lbl_ayuda_recortes.pack(anchor="w", fill="x")

        # ── barra de acciones en bloque ──
        # Se arma ANTES que la grilla porque la grilla avisa de cambios de lote
        # apenas se puebla, y el aviso toca estos botones. Vive al PIE del
        # panel de la lista (no en el panel de detalle de la derecha, que habla
        # siempre de UNA foto) y aparece sola con 2+ marcadas.
        # El PIE del panel de la lista se reserva ACÁ, antes de empaquetar la
        # grilla, y nunca se desempaqueta.
        #
        # Bug real medido (2026-09-08), preexistente y no introducido por las
        # acciones nuevas: `izq` mide 236px en la ventana por defecto (774px de
        # alto, que es como abre en la pantalla de 864px del usuario), y la
        # barra en bloque pedía ~190px. La grilla va con `expand=True` y se
        # empaquetaba ANTES, así que se quedaba con toda la cavidad y cuando la
        # barra se empaquetaba después (al marcar la 2da foto) no quedaba
        # espacio: Tk la DESMAPEA en silencio -> `winfo_ismapped() == 0`,
        # tamaño real 1x1. O sea "Descartar seleccionados" y "Desechar
        # definitivamente" en bloque no se veían NUNCA salvo con la ventana
        # maximizada (ahí `izq` llega a 453px). Es el mismo problema que ya
        # estaba documentado para `_barra_corregir` unas líneas más abajo.
        #
        # La solución es la misma: el bloque de altura fija se empaqueta primero
        # (con `side="bottom"`), así se queda con su espacio pase lo que pase, y
        # el que se aprieta es la grilla, que es elástica y además scrolleable.
        # `_pie_lote` es el contenedor permanente; lo que aparece y desaparece
        # es la barra de adentro.
        # `tk.Frame` y NO `Marco`/`CTkFrame` a propósito: un CTkFrame vacío pide
        # 200x200 por defecto (medido: con la barra escondida este contenedor
        # seguía pidiendo 250px de alto y se quedaba con 105px del panel), y
        # como se empaqueta primero le robaba la franja visible a la grilla —
        # las tarjetas de recortes desaparecían de la lista. Un `tk.Frame`
        # vacío pide 1x1, así que mientras no hay selección múltiple este pie
        # no ocupa nada.
        self._pie_lote = tk.Frame(izq, background=COLOR_PANEL)
        self._pie_lote.pack(side="bottom", fill="x", padx=6, pady=(0, 8))

        self._barra_lote = Marco(self._pie_lote, style="Panel.TFrame", padding=(8, 6))
        self._barra_lote.configure(corner_radius=0)

        fila_titulo_lote = Marco(self._barra_lote)
        fila_titulo_lote.pack(fill="x")
        self.v_lote = tk.StringVar(value="")
        Etiqueta(fila_titulo_lote, textvariable=self.v_lote,
                 style="SubtituloPanel.TLabel").pack(side="left")
        self.btn_lote_cancelar = Boton(fila_titulo_lote, text="✕", width=28, height=24,
                                       style="Sutil.TButton",
                                       command=self._cancelar_lote)
        self.btn_lote_cancelar.pack(side="right")

        # Dos botones por fila, no uno por fila a lo largo.
        #
        # Son 5 acciones: apiladas a ancho completo la barra pedía 328px, más
        # que el alto entero del panel (236px) — no había forma de que entrara.
        # En dos columnas mide ~140px y entra en la ventana por defecto, que es
        # el requisito real: una barra que solo aparece maximizado es una barra
        # que no existe.
        rejilla_lote = Marco(self._barra_lote)
        rejilla_lote.pack(fill="x", pady=(6, 0))
        rejilla_lote.columnconfigure((0, 1), weight=1, uniform="lote")

        def celda_lote(texto, comando, fila, col, estilo=None, **kw):
            b = Boton(rejilla_lote, text=texto, height=30, command=comando,
                      **({"style": estilo} if estilo else {}), **kw)
            b.grid(row=fila, column=col, sticky="ew", padx=(0, 4) if col == 0 else (4, 0),
                   pady=2)
            return b

        # Las MISMAS dos correcciones de la barra "CORREGIR ESTA FOTO", pero
        # sobre las N marcadas. Esto es lo que reemplaza a la pantalla masiva
        # "Elegir recortes a limpiar" que existía aparte (eliminada 2026-09-08):
        # el usuario dijo que prefiere la experiencia de Revisar, así que la
        # eficiencia de "aplicar a varias de una pasada" se trae ACÁ en vez de
        # mantener una segunda pantalla con las mismas acciones.
        #
        # Arriba y con el mismo icono que los de una sola foto, para que se
        # lean como "lo mismo, pero a las marcadas".
        self.btn_lote_reparar = celda_lote("🔧 Suela", self._reparar_suela_lote,
                                           0, 0, "Primario.TButton")
        self.btn_lote_recortar = celda_lote("✂ Sombra", self._recortar_sombra_lote,
                                            0, 1, "Primario.TButton")
        # "Aprobar en bloque" es el reemplazo de la casilla "Aprobar" que tenía
        # cada tarjeta de la pantalla masiva (con su "Marcar todas para
        # aprobar"): el caso real de un lote donde la mayoría de las fotos ya
        # están bien y no necesitan ninguna corrección.
        self.btn_lote_aprobar = celda_lote("✓ Aprobar", self._aprobar_lote,
                                           1, 0, "Aprobar.TButton")
        self.btn_lote_descartar = celda_lote("✗ Descartar", self._descartar_lote,
                                             1, 1, "Descartar.TButton")
        # El irreversible va solo, a lo ancho, separado de los otros cuatro:
        # es el único que borra archivos del disco.
        self.btn_lote_desechar = Boton(self._barra_lote, text="🗑 Desechar definitivamente",
                                       style="Peligro.TButton", height=30,
                                       command=self._desechar_lote)
        self.btn_lote_desechar.pack(fill="x", pady=(6, 0))

        # Grilla de tarjetas con la FOTO de cada recorte. `GrillaRecortes`
        # expone la misma API mínima que exponía el Treeview, así que la
        # lógica que la puebla y la lee no cambió.
        self.tabla = GrillaRecortes(izq,
                                    proveedor_png=lambda n: self._recortes.get(n, {}).get("png"),
                                    componer=self._componer_sobre_gris,
                                    al_cambiar_lote=self._al_cambiar_lote)
        self.tabla.pack(fill="both", expand=True, padx=7, pady=(0, 10))
        self.tabla.bind("<<TreeviewSelect>>", self._seleccionar)
        self.tabla.tag_configure("aprobado", foreground=COLOR_OK)
        self.tabla.tag_configure("descartado", foreground=COLOR_MAL)

        # ── centro: visor ──
        centro = Tarjeta(cuerpo)
        centro.pack(side="left", fill="both", expand=True, pady=12)

        barra_visor = Marco(centro, style="Panel.TFrame", padding=(14, 12))
        barra_visor.configure(corner_radius=0)
        barra_visor.pack(side="top", fill="x")
        Etiqueta(barra_visor, text="Recorte con líneas de referencia (azul = borde real, "
                                   "roja = curva esperada de la suela)",
                 style="SuavePanel.TLabel", wraplength=640).pack(side="left")
        self.chk_antes_despues = Casilla(
            barra_visor, text="Ver antes / después", variable=self._ver_antes_despues,
            command=self._mostrar_imagen)
        self.chk_antes_despues.pack(side="right")
        self.chk_antes_despues.state(["disabled"])

        # El visor grande sigue siendo un `tk.Label`: es el que recibe los
        # `ImageTk.PhotoImage` que arma el pipeline de imagen, y CTkLabel
        # espera CTkImage — cambiarlo obligaría a tocar el código de imagen.
        # ── el PIE del panel central se reserva ANTES del visor ──
        # Causa raíz del bug que el usuario reportó varias veces ("le doy clic
        # a una foto y los botones de Corregir esta foto no aparecen"),
        # confirmada por captura de pantalla el 2026-09-08:
        #
        #   `marco_visor` va con `expand=True`, pero su tamaño PEDIDO lo fija
        #   la foto que tiene adentro (`lbl_imagen` con un PhotoImage de
        #   480x480 -> pide 484px de alto). `pack` reparte en ORDEN de
        #   empaquetado: le daba al visor sus 484px enteros y, cuando llegaba
        #   a la franja de corregir (empaquetada después, al seleccionar),
        #   ya no quedaba cavidad -> Tk la DESMAPEA en silencio. Medido:
        #   `centro` mide 581px y sus hijos pedían 704px; la franja quedaba
        #   con `winfo_ismapped() == 0`. Agrandar o maximizar la ventana no
        #   ayudaba porque una ventana más alta escala la foto más grande y
        #   el visor vuelve a pedir todo.
        #
        # La solución es la misma que ya usaba el panel de la derecha con
        # `der_pie`: los bloques de altura FIJA del pie se empaquetan primero
        # (con `side="bottom"`), así se quedan con su espacio pase lo que
        # pase, y el que se aprieta es el visor, que es elástico. Además
        # `pack_propagate(False)` en el visor: el tamaño de la foto ya no
        # puede inflar lo que el visor pide.
        self.v_paso_proceso = tk.StringVar(value="")
        self._lbl_paso_proceso = Etiqueta(centro, textvariable=self.v_paso_proceso,
                                          style="SuavePanel.TLabel",
                                          anchor="center", justify="center")
        # Oculta cuando no tiene texto (que es la mayor parte del tiempo
        # revisando fotos): antes quedaba SIEMPRE empaquetada, vacía, y era
        # el "espacio desperdiciado debajo de Corregir esta foto" que
        # reportó el usuario -- una fila en blanco con su padding propio.
        def _refrescar_visibilidad_paso_proceso(*_a) -> None:
            if self.v_paso_proceso.get().strip():
                if not self._lbl_paso_proceso.winfo_ismapped():
                    self._lbl_paso_proceso.pack(side="bottom", fill="x", pady=(0, 4))
            elif self._lbl_paso_proceso.winfo_ismapped():
                self._lbl_paso_proceso.pack_forget()
        self.v_paso_proceso.trace_add("write", _refrescar_visibilidad_paso_proceso)

        # ── corregir la foto, JUSTO DEBAJO de la foto ──
        # Pedido del usuario (2026-09-07): "cuando le doy clic a una foto...
        # me gustaría que los botones de Corregir esta foto estén de forma
        # más visible, tal vez debajo de la foto" -- vivían en el panel
        # lateral derecho (`der_int`, scrollable, lejos de la vista si hay
        # que bajar). Misma fila horizontal, pegada al visor: es lo primero
        # que se ve al mirar la foto, no algo que haya que ir a buscar.
        #
        # Rediseño 2026-09-08 (pedido: "rediseña la ventana", "que sea
        # imposible no verla"): no es más un `Marco` transparente que se
        # funde con el panel blanco de atrás, es una TARJETA con fondo azul
        # suave, borde azul de 2px y esquinas redondeadas. Contra el blanco
        # del panel se lee como un bloque aparte, no como "más panel".
        self._barra_corregir = ctk.CTkFrame(centro, fg_color=PAR_PRIMARIO_SUAVE,
                                            border_color=PAR_PRIMARIO,
                                            border_width=2,
                                            corner_radius=RADIO_PANEL)
        # Dos filas a propósito: el título + la casilla arriba, los 3 botones
        # abajo. En UNA sola fila los 4 controles piden ~970px y la ventana
        # puede bajar a 900px de ancho (`minsize`): a esa altura pack recorta
        # el último botón por la derecha, y volvía a pasar lo mismo que se
        # está arreglando -- un control que existe pero no se ve. Con las
        # filas separadas entra hasta en el ancho mínimo.
        # Compactado 2026-09-09 (pedido: "más abajo y más pequeño, sin perder
        # ningún botón"): mismo diseño de 2 filas, pero con los paddings y el
        # alto de los botones recortados para devolverle ~30px de alto al
        # visor. Los 3 botones siguen presentes y con el mismo comando.
        fila_titulo = Marco(self._barra_corregir)
        fila_titulo.pack(fill="x", padx=12, pady=(5, 0))
        Etiqueta(fila_titulo, text="✎  CORREGIR ESTA FOTO",
                 text_color=PAR_PRIMARIO,
                 style="SubtituloPanel.TLabel").pack(side="left", padx=(0, 16))
        self.chk_lineas = Casilla(fila_titulo, text="Ver líneas azul / roja",
                                  variable=self._ver_lineas, command=self._mostrar_imagen)
        self.chk_lineas.pack(side="left")

        fila_botones = Marco(self._barra_corregir)
        fila_botones.pack(fill="x", padx=12, pady=(4, 7))
        # "Primario.TButton" (relleno azul lleno) en vez del gris por defecto
        # -- pedido del usuario (2026-09-07): "no se ven bien, deberían verse
        # más visibles". Son las 2 acciones de corrección reales de esta
        # pantalla, no controles de rutina como "Ver antes/después".
        self.btn_reparar_suela = Boton(fila_botones, text="🔧 Reparar suela",
                                       style="Primario.TButton", height=30,
                                       command=self._reparar_suela_actual)
        self.btn_reparar_suela.pack(side="left", padx=(0, 8))
        self.btn_recortar_sombra = Boton(fila_botones, text="✂ Recortar sombra",
                                         style="Primario.TButton", height=30,
                                         command=self._recortar_sombra_actual)
        self.btn_recortar_sombra.pack(side="left", padx=(0, 8))
        # Antes iba con "Sutil.TButton" (sin relleno, texto gris): sobre el
        # azul suave de la tarjeta casi no se distinguía. Con relleno blanco
        # y borde azul se ve como botón, y sigue siendo el más callado de los
        # tres.
        self.btn_revertir_recorte = Boton(fila_botones, text="↺ Revertir correcciones",
                                          height=30, fg_color=PAR_PANEL,
                                          hover_color=PAR_BORDE,
                                          text_color=PAR_PRIMARIO,
                                          border_width=1, border_color=PAR_PRIMARIO,
                                          command=self._revertir_recorte_actual)
        self.btn_revertir_recorte.pack(side="left")
        for b in (self.btn_reparar_suela, self.btn_recortar_sombra,
                  self.btn_revertir_recorte):
            b.state(["disabled"])
        # Se empaqueta acá, ANTES del visor, para quedarse con su espacio; y
        # se esconde enseguida. `_seleccionar_real` la vuelve a mostrar con
        # `before=self._marco_visor`, que la reinserta en esta misma posición
        # del orden de pack -- si se empaquetara al final volvería el bug.
        self._barra_corregir.pack(side="bottom", fill="x", padx=14, pady=(0, 6))
        self._barra_corregir.pack_forget()

        self._marco_visor = ctk.CTkFrame(centro, fg_color=PAR_VISOR_BG,
                                         corner_radius=RADIO_PANEL, height=200)
        self._marco_visor.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self._marco_visor.pack_propagate(False)
        self.lbl_imagen = tk.Label(self._marco_visor, anchor="center",
                                   background=COLOR_VISOR_BG,
                                   relief="flat", borderwidth=0, highlightthickness=0)
        self.lbl_imagen.pack(fill="both", expand=True, padx=2, pady=2)
        # Reescalar la foto abierta cuando el visor cambia de alto/ancho: es
        # lo que hace que al cerrar la franja amarilla de "retomando" (o al
        # maximizar) la imagen se vuelva a ajustar al espacio nuevo en vez de
        # quedarse con la medida vieja y verse cortada.
        self._medida_visor: tuple[int, int] | None = None
        self._marco_visor.bind("<Configure>", self._al_redimensionar_visor)

        # ── derecha: panel de estado + decisión ──
        # 330 y no 272 (2026-09-09): el bloque de decisión + el destructivo
        # entran completos sin que el scroll de `der_int` tenga que recortar
        # nada en la ventana por defecto -- el reclamo era justamente que la
        # parte de confirmar quedaba achicada dentro de un panel con scroll.
        der = Tarjeta(cuerpo, width=330)
        der.pack(side="left", fill="y", padx=(7, 14), pady=12)
        der.pack_propagate(False)

        # PIE del panel, reservado ANTES que el cuerpo: la acción destructiva
        # se empaqueta primero con `side="bottom"` para que se quede con su
        # espacio pase lo que pase, y sea el contenido de arriba el que se
        # apriete si la ventana es baja.
        der_pie = Marco(der, style="Panel.TFrame", padding=(16, 0, 16, 12))
        der_pie.configure(corner_radius=0)
        der_pie.pack(side="bottom", fill="x")

        # Scrollable, no un Marco fijo: en una portátil la ventana entra a
        # ~619px de alto y este panel se quedaba sin espacio para sus últimos
        # bloques — pack los recortaba en silencio y desaparecían «% de suela
        # faltante», «Limpieza automática» y, lo grave, los botones
        # «✓ Aprobar / ✗ Descartar». Confirmado en captura antes de esto.
        # Expone la misma API de contenedor, así que los hijos se arman igual.
        der_int = ctk.CTkScrollableFrame(der, fg_color="transparent",
                                         corner_radius=0)
        der_int.pack(side="top", fill="both", expand=True, padx=(12, 4), pady=(12, 0))

        self._badges_frame = Marco(der_int)
        self._badges_frame.pack(fill="x", pady=(8, 4))
        self._badge_labels: list[tk.Label] = []

        Etiqueta(der_int, text="% de suela faltante",
                 style="SuavePanel.TLabel").pack(anchor="w", fill="x", pady=(10, 0))
        self.v_falta = tk.StringVar(value="—")
        Etiqueta(der_int, textvariable=self.v_falta,
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")

        Separador(der_int).pack(fill="x", pady=8)

        Etiqueta(der_int, text="Limpieza automática",
                 style="SubtituloPanel.TLabel").pack(anchor="w", fill="x")
        self.v_auto_desc = tk.StringVar(value="—")
        Etiqueta(der_int, textvariable=self.v_auto_desc, style="SuavePanel.TLabel",
                 wraplength=200).pack(anchor="w", fill="x", pady=(4, 8))
        self.btn_deshacer_auto = Boton(der_int, text="Deshacer limpieza automática",
                                       command=self._alternar_limpieza_auto)
        self.btn_deshacer_auto.pack(fill="x")
        self.btn_deshacer_auto.state(["disabled"])

        Separador(der_int).pack(fill="x", pady=8)

        # "Aprobar / Descartar" vive en el PIE del panel, NO en la parte que
        # scrollea: es LA acción del paso 2 y no puede quedar debajo del
        # pliegue. Con el panel scrollable (necesario para que en una portátil
        # no se recorten los bloques de arriba), dejarla adentro la escondía
        # justo en las ventanas donde más falta hace.
        Etiqueta(der_pie, text="Decisión de catálogo", style="SubtituloPanel.TLabel"
                 ).pack(anchor="w", fill="x", pady=(10, 6))
        fila_decision = Marco(der_pie)
        fila_decision.pack(fill="x")
        self.btn_aprobar = Boton(fila_decision, text="✓ Aprobar", style="Aprobar.TButton",
                                 height=38, command=lambda: self._decidir("aprobado"))
        self.btn_aprobar.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.btn_descartar = Boton(fila_decision, text="✗ Descartar", style="Descartar.TButton",
                                   height=38, command=lambda: self._decidir("descartado"))
        self.btn_descartar.pack(side="left", fill="x", expand=True)
        self.btn_aprobar.state(["disabled"])
        self.btn_descartar.state(["disabled"])

        # Bloque destructivo: en el PIE del panel, separado del par
        # Aprobar/Descartar por una línea y estilo de contorno rojo oscuro.
        Separador(der_pie).pack(fill="x", pady=(0, 10))
        Etiqueta(der_pie, text="Si la foto no sirve para nada:",
                 style="SuavePanel.TLabel", wraplength=230
                 ).pack(anchor="w", fill="x", pady=(0, 5))
        self.btn_desechar = Boton(der_pie, text="🗑 Desechar definitivamente",
                                  style="Peligro.TButton",
                                  command=self._desechar_seleccionado)
        self.btn_desechar.pack(fill="x")
        self.btn_desechar.state(["disabled"])

    # ── franja "estás retomando un lote" (paso 2) ────────────────────────
    # Pedido del usuario (2026-09-08): "si estoy retomando una carga anterior,
    # cómo podemos mejorar esta pantalla". Hasta hoy el paso 2 se veía IGUAL
    # retomando un lote de la semana pasada que arrancando uno nuevo: misma
    # guía ("Tocá ▶ Limpiar fotos…"), mismo botón azul de limpiar de primero,
    # y el único indicio de que había algo viejo cargado era la línea gris
    # chiquita de la esquina superior derecha (`v_carpeta_activa`) — que dice
    # una ruta larga y se lee como decoración, no como información.
    #
    # Retomar y empezar son dos tareas distintas: quien retoma NO necesita
    # limpiar nada, necesita seguir DECIDIENDO fotos. Así que la pantalla ahora
    # lo dice de frente, con el número que de verdad importa (cuántas quedan
    # por decidir) y de dónde vino el lote.

    def _armar_aviso_retomar(self) -> None:
        self._aviso_retomar = ctk.CTkFrame(self._zona_aviso, fg_color=PAR_ALERTA_BG,
                                           border_color=PAR_ALERTA, border_width=1,
                                           corner_radius=RADIO_PANEL)
        # Franja de UNA sola línea a propósito (reclamo del usuario, varias
        # veces: le quitaba espacio valioso al visor de foto grande, que vive
        # justo debajo). Antes eran 3 líneas de texto (título + detalle +
        # instrucción) cada una con su propia altura -- ahora es una sola
        # etiqueta con todo el texto junto, y padding mínimo.
        dentro = Marco(self._aviso_retomar, padding=(10, 4, 8, 4))
        dentro.pack(fill="x")

        # El botón de cerrar va PRIMERO (side="right"): así conserva su ancho
        # aunque el texto de la izquierda crezca con una ruta larga.
        Boton(dentro, text="✕", style="Sutil.TButton", width=28, height=22,
              command=self._ocultar_aviso_retomar).pack(side="right", padx=(8, 0))
        self.btn_aviso_seguir = Boton(dentro, text="↓ Ver las pendientes",
                                      style="Sutil.TButton", height=22,
                                      command=self._ir_a_primera_pendiente)
        self.btn_aviso_seguir.pack(side="right")

        # Las 3 variables se conservan (otras partes del código las llenan
        # por separado) pero se muestran TODAS en una sola etiqueta de una
        # línea, uniéndolas con " · " -- ver `_refrescar_texto_aviso_retomar`.
        self.v_aviso_retomar_titulo = tk.StringVar(value="")
        self.v_aviso_retomar_detalle = tk.StringVar(value="")
        self.v_aviso_retomar_pie = tk.StringVar(value="")
        self.v_aviso_retomar_linea = tk.StringVar(value="")
        Etiqueta(dentro, textvariable=self.v_aviso_retomar_linea,
                 text_color=PAR_ALERTA, style="SuavePanel.TLabel",
                 anchor="w").pack(side="left", fill="x", expand=True)

        def _refrescar_texto_aviso_retomar(*_a) -> None:
            partes = [v.get() for v in (self.v_aviso_retomar_titulo,
                                        self.v_aviso_retomar_detalle,
                                        self.v_aviso_retomar_pie) if v.get().strip()]
            self.v_aviso_retomar_linea.set("   ·   ".join(partes))
        for v in (self.v_aviso_retomar_titulo, self.v_aviso_retomar_detalle,
                  self.v_aviso_retomar_pie):
            v.trace_add("write", _refrescar_texto_aviso_retomar)

    def _ocultar_aviso_retomar(self) -> None:
        """Cerrar la franja no "termina" de retomar: solo la saca de la vista
        en esta pantalla. El botón del pie sigue rotulado como corresponde."""
        if self._aviso_retomar.winfo_manager():
            self._aviso_retomar.pack_forget()
            # `_zona_aviso` es un `tk.Frame`, así que al quedar sin hijos pide
            # 1x1 y pack devuelve el alto a las tres columnas. Se asienta el
            # layout YA y se reescala la foto abierta a la cavidad nueva, en
            # vez de esperar a que el usuario mueva la ventana.
            self.update_idletasks()
            if hasattr(self, "_marco_visor"):
                self._al_redimensionar_visor()

    def _ir_a_primera_pendiente(self) -> None:
        """Selecciona la primera foto sin decidir del lote retomado — que es
        exactamente lo que el comprador vino a hacer. Reusa la selección de
        siempre: `selection_set` ya emite `<<TreeviewSelect>>`, que es lo que
        dispara `_seleccionar`. No hay lógica nueva de decisión acá."""
        pendientes = [n for n in self.tabla.get_children()
                      if self._recortes.get(n, {}).get("estado", {}).get("decision")
                      not in (decisiones.APROBADO, decisiones.DESCARTADO)]
        if not pendientes:
            self.v_log.set("No quedan fotos pendientes de decidir en este lote.")
            return
        self.tabla.selection_set(pendientes[0])

    def _refrescar_aviso_retomar(self) -> None:
        """Pone (o saca) la franja y ajusta el rótulo del botón de limpiar,
        según si lo que hay en pantalla es un lote retomado o uno nuevo."""
        if not hasattr(self, "_aviso_retomar"):
            return
        retomando = bool(self._retomando_lote and self._recortes
                         and getattr(self, "_paso_flujo", 1) == 3)

        # El botón de limpiar cambia de rótulo Y de peso visual. Retomando, no
        # es la acción principal de la pantalla (decidir fotos lo es), y su
        # nombre real es otro: lo que hace es buscar fotos NUEVAS del mismo
        # origen y sumarlas al lote (las ya procesadas se saltan solas, salvo
        # que se pida reprocesar).
        if hasattr(self, "btn_procesar"):
            if retomando:
                self.btn_procesar.configure(
                    text="＋ Agregar más fotos", fg_color="transparent",
                    hover_color=PAR_PANEL_SUAVE, text_color=PAR_TEXTO_SUAVE,
                    border_width=1, border_color=PAR_BORDE, font=F["base"])
            else:
                self.btn_procesar.configure(
                    text="▶ Limpiar fotos", fg_color=PAR_PRIMARIO,
                    hover_color=PAR_PRIMARIO_OSCURO, text_color=("#ffffff", "#ffffff"),
                    border_width=0, font=F["subtitulo"])

        if not retomando:
            self._ocultar_aviso_retomar()
            return

        pendientes = sum(1 for r in self._recortes.values()
                         if r.get("estado", {}).get("decision")
                         not in (decisiones.APROBADO, decisiones.DESCARTADO))
        self.v_aviso_retomar_titulo.set(
            "↻  Estás retomando un lote de una sesión anterior")
        partes = [f"{len(self._recortes)} foto(s) ya limpias",
                  f"{pendientes} pendiente(s) de decidir" if pendientes
                  else "todas decididas"]
        origen = self._origen_del_lote_legible(self._salida) if self._salida else ""
        # Los lotes hechos antes de que se empezara a anotar el origen no lo
        # tienen: se dice así, en vez de callarlo y dejar al comprador
        # adivinando por qué "Agregar más fotos" le pide una carpeta.
        partes.append(f"origen: {origen}" if origen
                      else "origen: no quedó anotado (lote viejo)")
        self.v_aviso_retomar_detalle.set("   ·   ".join(partes))
        self.v_aviso_retomar_pie.set(
            "No hace falta limpiar nada de nuevo: seguí decidiendo las fotos de "
            "la izquierda." if pendientes else
            "Ya están todas decididas: podés seguir con «Continuar».")
        self.btn_aviso_seguir.state(["disabled"] if not pendientes else ["!disabled"])
        if not self._aviso_retomar.winfo_manager():
            self._aviso_retomar.pack(side="top", fill="x", padx=14, pady=(12, 0))

    # ── pie fijo: navegación del asistente ───────────────────────────────

    def _armar_pie(self) -> None:
        pie_ext = ctk.CTkFrame(self, fg_color=PAR_PANEL, corner_radius=0)
        pie_ext.pack(side="bottom", fill="x")
        Separador(pie_ext).pack(fill="x", side="top")

        pie = Marco(pie_ext, padding=(24, 12, 24, 14))
        pie.pack(fill="x")

        self.btn_atras = Boton(pie, text="←  Atrás", style="NavAtras.TButton",
                               width=120, command=self._paso_atras)
        self.btn_atras.pack(side="left")

        # Todo lo ocasional o irreversible sale de la cara principal y entra
        # acá. Son las mismas funciones de siempre, con los mismos comandos:
        # lo único que cambia es que ya no ocupan un botón permanente.
        menu_mas = tk.Menu(self, tearoff=False)
        # Acá estaba "Elegir qué limpiar en detalle…", que abría la pantalla
        # masiva de casillas. Ya no existe: reparar/recortar sobre varias fotos
        # se hace marcándolas en la grilla de revisión (Ctrl/Shift+clic).
        menu_mas.add_command(label="Ver resumen del lote…",
                             command=self._mostrar_resumen_final)
        menu_mas.add_command(label="Ver candidatos calificados…",
                             command=self._ver_candidatos_calificados)
        menu_mas.add_separator()
        menu_mas.add_checkbutton(label="Reprocesar aunque ya esté hecho",
                                 variable=self.v_rehacer)
        menu_mas.add_command(label="Ajustes…", command=self._abrir_ajustes)
        menu_mas.add_separator()
        menu_mas.add_command(label="🧹 Limpiar caché (vaciar carpeta de trabajo)…",
                             command=self._vaciar_carpeta_trabajo)
        menu_mas.add_command(label="Revertir TODO…", command=self._revertir_todo)
        self.btn_mas = BotonMenu(pie, menu=menu_mas, text="⋮  Más opciones",
                                 style="Sutil.TButton", width=150, height=44)
        self.btn_mas.pack(side="left", padx=(10, 0))

        self.btn_siguiente = Boton(pie, text="Siguiente  →",
                                   style="Nav.TButton", width=190,
                                   command=self._paso_siguiente)
        self.btn_siguiente.pack(side="right")

        # Control del proceso: SOLO en el paso 2. En el resto de las pantallas
        # no hay nada que procesar, y tenerlos ahí era ruido puro.
        self._grupo_proceso = Marco(pie)
        # "Limpiar fotos" en vez de "Procesar" (2026-09-08, reclamo real del
        # usuario: "no entiendo qué hace el botón Procesar") -- lo que hace
        # de verdad es extraer y limpiar el fondo de las fotos del catálogo
        # elegido en el paso 1, convirtiéndolas en los recortes que se
        # revisan acá abajo. "Procesar" no decía nada de eso.
        self.btn_procesar = Boton(self._grupo_proceso, text="▶ Limpiar fotos",
                                  style="Primario.TButton", height=44, width=150,
                                  command=self._procesar)
        self.btn_procesar.pack(side="left")
        self.btn_pausar = Boton(self._grupo_proceso, text="⏸ Pausar", height=44, width=110,
                                command=self._pausar_reanudar, state="disabled")
        self.btn_pausar.pack(side="left", padx=(8, 0))
        self.btn_detener = Boton(self._grupo_proceso, text="⏹ Detener", height=44, width=110,
                                 command=self._detener, state="disabled")
        self.btn_detener.pack(side="left", padx=(8, 0))
        self._grupo_proceso.pack(side="right", padx=(0, 16))

        self.v_estado_proc = tk.StringVar(value="Sin iniciar")
        Etiqueta(pie, textvariable=self.v_estado_proc,
                 style="Suave.TLabel").pack(side="left", padx=(20, 0))

    # ── flujo por pasos ──────────────────────────────────────────────────

    def _mostrar_cuerpo(self) -> None:
        """Muestra la pantalla de revisión de fotos (pasos 3 y 4) y esconde la de
        inicio. Reemplaza a los `self.cuerpo.pack(...)` sueltos que había
        repartidos por el archivo: con el asistente, mostrar el cuerpo
        implica SIEMPRE sacar del medio la pantalla 1 — si no, las dos
        quedaban empaquetadas a la vez y la ventana mostraba media y media.

        Usa `winfo_manager()` y no `winfo_ismapped()` a propósito: durante
        `__init__` todavía no hay nada mapeado (eso pasa recién en
        `mainloop()`), así que `ismapped` daría 0 para widgets que YA están
        empaquetados y esto los duplicaría."""
        if self.pantalla_inicio.winfo_manager():
            self.pantalla_inicio.pack_forget()
        if not self.cuerpo.winfo_manager():
            self.cuerpo.pack(side="top", fill="both", expand=True)
        # Al salir de una grilla de elegir fotos hay que reponer la cabecera
        # completa, y por acá pasan todas las vueltas al cuerpo principal.
        self._sincronizar_cabecera()

    def _ocultar_pantallas_base(self) -> None:
        """Saca del medio las DOS pantallas base del asistente (inicio y
        revisión) antes de que una pantalla de reemplazo — revisión de
        extracción, selección de limpieza, resumen final, candidatos — se
        empaquete en su lugar. Antes cada una hacía solo
        `self.cuerpo.pack_forget()`; ahora que existe la pantalla 1, olvidarse
        de ella dejaría las dos apiladas."""
        if self.cuerpo.winfo_manager():
            self.cuerpo.pack_forget()
        if self.pantalla_inicio.winfo_manager():
            self.pantalla_inicio.pack_forget()

    def _hay_fotos_para_elegir(self) -> bool:
        """¿El paso 2 («Elegir qué limpiar») tiene algo que mostrar?

        Solo lo tiene cuando el origen fue un Excel, un PDF o un correo: ahí la
        extracción deja una lista de fotos candidatas y hace falta sacar los
        logos y portadas. Si el origen es una carpeta (o archivos) de imágenes
        que el comprador ya eligió a mano, no hay nada que elegir de nuevo."""
        return bool(getattr(self, "_archivos_revision", None))

    def _ir_a_paso(self, paso: int) -> None:
        """Cambia la pantalla visible del asistente -- con un cursor de
        espera mientras arma la pantalla nueva.

        Reclamo del usuario (2026-09-09): al cambiar de paso, sobre todo si
        implica armar una grilla grande o cargar imágenes, no había ninguna
        señal de que el programa estuviera trabajando -- se veía igual que si
        se hubiera colgado. El cursor de reloj de arena (`watch`) es la señal
        mínima universal de Windows para "esperá, estoy trabajando", visible
        apenas se mueve el mouse sobre la ventana."""
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self._ir_a_paso_interna(paso)
        finally:
            self.configure(cursor="")

    def _ir_a_paso_interna(self, paso: int) -> None:
        """Cambia la pantalla visible del asistente.

        Maneja los pasos 1 a 3 (inicio, elegir qué limpiar, limpiar y revisar)
        y el 5 (vectorizar); el 4, 6 y 7 los muestran `_mostrar_resumen_final`,
        `_mostrar_vista_candidatos` y `_mostrar_sugerido_compra`.

        El paso 2 SE SALTA SOLO cuando no aplica (origen = carpeta de
        imágenes): el total de pastillas no cambia — el mapa del proceso es
        siempre el mismo — pero ese origen simplemente no tiene esa parada, y
        el comprador no ve nada ni tiene que tocar un clic de más."""
        if paso == 2 and not self._hay_fotos_para_elegir():
            self._ir_a_paso(3)
            return
        if paso == 1:
            self._asegurar_cuerpo_visible()
            self.cuerpo.pack_forget()
            if self.monitor.winfo_manager():
                self.monitor.pack_forget()
            if not self.pantalla_inicio.winfo_manager():
                self.pantalla_inicio.pack(side="top", fill="both", expand=True)
        elif paso == 2:
            # La grilla de fotos extraídas se reconstruye con la selección tal
            # como quedó: volver atrás y adelante no pierde lo descartado.
            self._mostrar_revision_extraccion(self._archivos_revision,
                                              exclusiones=self._exclusiones_revision)
            return  # ya fija el paso por su cuenta
        elif paso == 5:
            self._mostrar_pantalla_vectorizacion()
            return  # ya fija el paso por su cuenta
        else:
            self._mostrar_cuerpo()
        self._actualizar_paso(paso)

    def _paso_siguiente(self) -> None:
        """El botón principal del pie. Valida lo mínimo de cada pantalla antes
        de dejar avanzar, en vez de fallar más adelante con un error técnico.
        Avanza SIEMPRE de un paso al siguiente, nunca saltea (el único salto es
        el automático del paso 2 cuando no aplica)."""
        paso = getattr(self, "_paso_flujo", 1)
        if paso == 1:
            if self._proveedor_id_actual is None:
                messagebox.showinfo(
                    "Falta el proveedor",
                    "Elegí primero de qué proveedor es este catálogo.")
                return
            if not self.v_entrada.get().strip():
                messagebox.showinfo(
                    "Falta el origen",
                    "Elegí de dónde salen las fotos con «📂 Elegir origen».")
                return
            # El canal decide contra qué se califica TODO el lote: no se deja
            # avanzar con la decisión sin tomar (y sin default heredado).
            if self.v_canal_venta.get().strip().lower() not in self.ETIQUETA_CANAL:
                messagebox.showinfo(
                    "Falta el canal de venta",
                    "Decime si esta compra es para TC Marcas o para TU Calzado.\n\n"
                    "De eso depende contra qué se buscan los comparables y con qué "
                    "se califica el catálogo entero.")
                return
            # FASE 1 multi-proveedor: lo elegido en pantalla queda anotado en
            # su slot, y si el lote tiene varios proveedores se arranca por el
            # primero que todavía no se limpió (los demás se procesan después,
            # uno por uno, al terminar el paso 3).
            self._sincronizar_slot_actual()
            incompletos = [n for n, s in enumerate(self._proveedores_del_lote, start=1)
                           if s.get("proveedor_id") is None
                           or not (s.get("entrada_dir") or s.get("entrada_archivos"))]
            if incompletos:
                messagebox.showinfo(
                    "Falta completar un proveedor",
                    "Estos proveedores del lote quedaron sin proveedor o sin "
                    f"origen de fotos: {', '.join(str(n) for n in incompletos)}.\n\n"
                    "Completalos o volvé a elegirlos antes de continuar.")
                return
            if self._lote_multiproveedor():
                pendiente = self._indice_proveedor_pendiente("limpiado")
                if pendiente is not None and pendiente != self._indice_proveedor_actual:
                    self._activar_slot(pendiente)
            self._ir_a_paso(2)
        elif paso == 2:
            self._confirmar_seleccion_revision()
        elif paso == 3:
            if not self._recortes:
                messagebox.showinfo(
                    "Todavía no hay fotos limpias",
                    "Tocá «▶ Limpiar fotos» y esperá a que salga la primera.")
                return
            # Con el paso fusionado, "Continuar" se puede tocar mientras el
            # lote TODAVÍA está corriendo (la grilla se llena en vivo). Salir
            # ahí dejaría fotos apareciendo en una pantalla que el comprador ya
            # dejó atrás, así que se avisa en vez de avanzar a medias.
            if getattr(self, "_procesando_lote", False):
                messagebox.showinfo(
                    "El lote todavía se está limpiando",
                    "Faltan fotos por limpiar. Podés seguir decidiendo las que "
                    "ya están, o usar «⏹ Detener» si querés cerrar el lote "
                    "con lo que hay hasta ahora.")
                return
            # FASE 1 multi-proveedor: el proveedor activo ya pasó por la
            # limpieza; si el lote tiene otro proveedor sin limpiar, se ofrece
            # seguir con él (mismo pipeline, misma carpeta de trabajo) antes de
            # ir a confirmar y enviar.
            if self._proveedores_del_lote:
                self._slot_actual()["limpiado"] = True
                self._persistir_proveedores_del_lote()
                self._refrescar_resumen_proveedores()
            pendiente = self._indice_proveedor_pendiente("limpiado")
            if pendiente is not None and self._lote_multiproveedor():
                slot = self._proveedores_del_lote[pendiente]
                if messagebox.askyesno(
                        "Siguiente proveedor del lote",
                        f"Las fotos de {self._proveedor_nombre_actual} ya están "
                        "limpias.\n\nEste lote todavía tiene el catálogo de "
                        f"{slot.get('proveedor_nombre')} sin limpiar.\n\n"
                        "¿Seguir ahora con ese proveedor?"):
                    if self._activar_slot(pendiente):
                        self._ir_a_paso(2)
                        return
            self._mostrar_resumen_final()
        elif paso == 4:
            # Acá "Continuar" NO es solo navegar: lleva al paso de vectorizar
            # si el lote todavía no se envió, o abre los candidatos directo si
            # ya estaba enviado.
            self._accion_boton_compras()
        elif paso == 5:
            # El botón de pie hace lo mismo que el botón grande de la pantalla:
            # dispara el cálculo real (vectores + similitud de coseno).
            self._calcular_vectorizacion_y_continuar()
        elif paso == 6:
            self._mostrar_sugerido_compra()

    def _paso_atras(self) -> None:
        paso = getattr(self, "_paso_flujo", 1)
        if paso == 7:
            self._volver_a_candidatos()
        elif paso == 6:
            # Atrás es el paso 5 ("Vectorizar y comparar"), y de ahí el 4: la
            # secuencia se recorre igual en los dos sentidos. Si no hay lote
            # cargado (se entró a candidatos desde el menú), `_cerrar_vista…`
            # ya aterriza donde corresponde.
            self._cerrar_vista_candidatos()
            if self._salida and self._recortes:
                self._ir_a_paso(5)
        elif paso == 5:
            # Volver del paso de vectorización es volver a "Confirmar y enviar"
            # — salvo que el cálculo esté corriendo, que no se puede abandonar
            # a medias sin dejar la barra de progreso huérfana.
            if getattr(self, "_enviando_compras", False):
                messagebox.showinfo(
                    "Cálculo en curso",
                    "Se están calculando los vectores y la similitud. "
                    "Esperá a que termine: al terminar se abren los candidatos "
                    "calificados solo.")
                return
            self._mostrar_resumen_final()
        elif paso == 4:
            self._ocultar_resumen()
        elif paso == 3:
            # Si este origen no tiene paso 2, atrás es el 1: no se muestra una
            # pantalla vacía solo para respetar la numeración.
            self._ir_a_paso(2 if self._hay_fotos_para_elegir() else 1)
        elif paso == 2:
            self._ir_a_paso(1)

    def _actualizar_paso(self, paso: int | None = None) -> None:
        """Reescribe la cabecera (número, título, guía, pastillas), la franja
        de contexto y el pie de navegación. Se llama con un número cuando la
        app CAMBIA de etapa, y sin número cuando solo cambió el detalle
        (proveedor elegido, más fotos aprobadas) y hay que refrescar lo mismo."""
        if paso is not None:
            self._paso_flujo = paso
        paso = getattr(self, "_paso_flujo", 1)

        total_pasos = len(self.PASOS_FLUJO)
        self.v_paso_num.set(f"PASO {paso} DE {total_pasos}")
        self.v_paso_titulo.set(self.PASOS_FLUJO.get(paso, ""))
        # Retomando, la guía de siempre ("Tocá ▶ Limpiar fotos para extraer y
        # limpiar…") manda al comprador a hacer justo lo que NO tiene que
        # hacer: las fotos ya están limpias. Se le cuenta el paso que sí falta.
        if paso == 3 and getattr(self, "_procesando_lote", False):
            self.v_paso_guia.set(self.GUIA_LIMPIANDO)
        elif paso == 3 and self._retomando_lote and self._recortes:
            self.v_paso_guia.set(self.GUIA_RETOMANDO)
        else:
            self.v_paso_guia.set(self.GUIA_PASOS.get(paso, ""))

        for n, pill in getattr(self, "_pills", {}).items():
            if n == paso:
                pill.configure(fg_color=PAR_PRIMARIO, text_color=("#ffffff", "#ffffff"))
            elif n < paso:
                pill.configure(fg_color=PAR_OK_BG, text_color=PAR_OK)
            else:
                pill.configure(fg_color="transparent", text_color=PAR_TEXTO_SUAVE)

        partes = []
        if self._proveedor_nombre_actual:
            partes.append(f"Proveedor: {self._proveedor_nombre_actual}")
        else:
            partes.append("Proveedor: sin elegir")
        if paso in (3, 4) and self._recortes:
            aprobados = sum(1 for r in self._recortes.values()
                            if r.get("estado", {}).get("decision") == "aprobado")
            partes.append(f"{aprobados} aprobada(s) de {len(self._recortes)}")
        self.v_paso_flujo.set("   ·   ".join(partes))

        # Pie: qué se puede hacer desde acá.
        if hasattr(self, "btn_siguiente"):
            self.btn_siguiente.configure(text=self.TEXTO_SIGUIENTE.get(paso, "Siguiente  →"))
            self.btn_siguiente.state(["disabled"] if paso >= total_pasos else ["!disabled"])
            self.btn_atras.state(["disabled"] if paso <= 1 else ["!disabled"])
            # El botón de pie está visible en TODOS los pasos MENOS EL ÚLTIMO,
            # el 4 incluido: ahí es el que lleva a vectorizar / abre los
            # candidatos. Al reempacarlo se saca primero el grupo de proceso,
            # para que conserve su lugar a la izquierda (con `side="right"`, el
            # primero empacado queda más a la derecha).
            #
            # En el último paso (7, "Sugerido de compra") se OCULTA por
            # completo en vez de quedar deshabilitado (2026-09-09, pedido del
            # usuario): no hay paso 8, así que un botón "Siguiente  →" apagado
            # solo se veía como un control roto. "Atrás" sigue igual.
            if paso >= total_pasos:
                if self.btn_siguiente.winfo_manager():
                    self.btn_siguiente.pack_forget()
            elif not self.btn_siguiente.winfo_manager():
                if self._grupo_proceso.winfo_manager():
                    self._grupo_proceso.pack_forget()
                self.btn_siguiente.pack(side="right")
            # Los controles de proceso viven en el paso 3 ("Limpiar y
            # revisar"): ahí se dispara la limpieza, se pausa/detiene mientras
            # corre, y una vez terminada el mismo botón sirve para sumarle
            # fotos nuevas al lote ("＋ Agregar más fotos").
            if paso == 3:
                if not self._grupo_proceso.winfo_manager():
                    self._grupo_proceso.pack(side="right", padx=(0, 16))
            elif self._grupo_proceso.winfo_manager():
                self._grupo_proceso.pack_forget()

        # El monitor de avance (barra general + "Lámina actual" + consola de
        # detalle + aviso de timeout) habla SOLO de la limpieza de láminas: en
        # los pasos 4, 5, 6 y 7 no hay ninguna lámina corriendo, así que ahí
        # ocupaba pantalla mostrando el estado congelado del último lote —
        # incluidos los rótulos "Progreso general del lote" y "Lámina actual",
        # que en esas pantallas no significan nada. El avance de la
        # vectorización tiene su propia barra, en la pantalla del paso 5
        # (`self.barra_compras`), sin el bloque de lámina.
        #
        # Con el paso 3 fusionado ("Limpiar y revisar") el monitor ya no se
        # ata al NÚMERO de paso sino a si hay algo corriendo: aparece al tocar
        # «▶ Limpiar fotos» y se retira solo cuando el lote termina (o se
        # detiene). ESA es la transición automática de "progreso" a "revisión"
        # que antes costaba un clic en "Continuar" y un cambio de pantalla.
        if not (paso == 3 and getattr(self, "_procesando_lote", False)):
            self._ocultar_monitor()

        # Cabecera compacta mientras esté abierta una grilla de elegir fotos.
        # Se decide por ESTADO y no con un llamado suelto en cada pantalla:
        # `_actualizar_paso` también corre en refrescos de detalle (una foto
        # más aprobada), y un llamado suelto se desharía en el primer refresco.
        self._sincronizar_cabecera()

        # Última cosa: la franja de "retomando" y el rótulo del botón de
        # limpiar dependen del paso y del contenido, así que se recalculan
        # acá — el único lugar por donde pasan TODOS los cambios de pantalla.
        self._refrescar_aviso_retomar()


    def _sincronizar_cabecera(self) -> None:
        """Pone la cabecera compacta o completa según qué pantalla está abierta
        AHORA. Se decide por estado y no con llamados sueltos en cada pantalla:
        `_actualizar_paso` también corre en refrescos de detalle (una foto más
        aprobada), y un llamado suelto se desharía en el primer refresco."""
        # Antes también compactaba para la pantalla masiva "Elegir recortes a
        # limpiar" (eliminada 2026-09-08); queda la grilla del paso 2.
        # También en el paso 3 ("Limpiar y revisar") CUANDO YA HAY RECORTES: es
        # la pantalla con el visor de foto grande y tres columnas, y el bloque
        # grande de cabecera ("PASO 3 DE 7" + titulazo + la línea de guía) se
        # llevaba ~130px de alto — medido: con la ventana abierta el cuerpo se
        # quedaba con 462px de 774, y el visor de la foto con 199px de alto. Es
        # el "espacio desperdiciado" del reclamo, y esa pantalla ya se explica
        # sola (la pastilla dice «3 Limpiar y revisar» y la franja de contexto
        # dice de qué lote y cuántas van aprobadas).
        #
        # Con el paso fusionado, la cabecera SÍ se deja completa mientras el
        # lote está vacío: ahí el bloque grande es lo único que le dice al
        # comprador qué hacer ("Tocá ▶ Limpiar fotos"), y no hay ninguna grilla
        # todavía que necesite el espacio. En cuanto empiezan a caer recortes,
        # se compacta y el alto se va a la grilla y al visor.
        en_grilla_revision = (getattr(self, "_paso_flujo", 1) == 3
                              and bool(getattr(self, "_recortes", None)))
        self._compactar_cabecera(
            getattr(self, "_revision_frame", None) is not None
            or en_grilla_revision)

    def _compactar_cabecera(self, compacta: bool) -> None:
        """Esconde (o repone) el bloque grande de la cabecera — "PASO N DE 7",
        el titulazo y la línea de guía — dejando las pastillas de progreso y la
        franja de contexto.

        Es para las dos pantallas de elegir fotos en grilla. En una pantalla de
        864px (la del usuario) la ventana abre a 774 y la cabecera completa se
        lleva 224px: la grilla se quedaba con 191px de franja visible, menos de
        lo que mide una tarjeta, así que no se podía trabajar. Y no se pierde
        información al esconderlo: las pastillas siguen diciendo en qué paso
        está, y estas pantallas ya traen su propio título y su propia
        explicación arriba — el bloque grande decía lo mismo dos veces."""
        bloque = getattr(self, "_bloque_titulo_paso", None)
        if bloque is None:
            return
        if compacta:
            if bloque.winfo_manager():
                bloque.pack_forget()
        elif not bloque.winfo_manager():
            bloque.pack(fill="x", after=self._fila_pills)

    def _elegir_proveedor_inicial(self) -> None:
        """Paso 1 del flujo: de qué proveedor es el catálogo que se va a
        trabajar. Se guarda en el estado de la ventana y el resto del flujo
        lo lee sin volver a preguntar."""
        try:
            import motor_candidatos
            proveedores = motor_candidatos.listar_proveedores()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Proveedor", f"No se pudo consultar proveedores:\n{exc}")
            return
        eleccion = self._elegir_proveedor(proveedores)
        if eleccion is None:
            return
        self._proveedor_id_actual, self._proveedor_nombre_actual = eleccion
        self.v_proveedor.set(f"Proveedor: {self._proveedor_nombre_actual}")
        # Queda anotado en la carpeta del lote: así, al retomarlo otro día, el
        # asistente no vuelve a preguntar por un proveedor que ya se sabe.
        self._guardar_marca_lote(proveedor_id=self._proveedor_id_actual,
                                 proveedor_nombre=self._proveedor_nombre_actual)
        # FASE 1 multi-proveedor: el proveedor elegido queda anotado en el
        # "slot" en edición de la lista del lote, no solo en el escalar.
        self._sincronizar_slot_actual(solo_proveedor=True)
        self._actualizar_paso()

    # ── FASE 1 multi-proveedor: la lista de proveedores del lote ──────────
    #
    # Estructura de cada elemento (slot):
    #   {"proveedor_id": int, "proveedor_nombre": str,
    #    "entrada_dir": str|None, "entrada_archivos": list[str]|None,
    #    "limpiado": bool, "enviado": bool}
    # Se persiste completa en `lote_meta` bajo CLAVE_PROVEEDORES_LOTE (JSON,
    # mismo patrón que `costos_candidatos`/`categorias_candidatos`).
    CLAVE_PROVEEDORES_LOTE = "proveedores_lote"

    def _slot_actual(self) -> dict:
        """El slot en edición, creándolo si la lista todavía está vacía."""
        while len(self._proveedores_del_lote) <= self._indice_proveedor_actual:
            self._proveedores_del_lote.append(
                {"proveedor_id": None, "proveedor_nombre": None,
                 "entrada_dir": None, "entrada_archivos": None,
                 "limpiado": False, "enviado": False})
        return self._proveedores_del_lote[self._indice_proveedor_actual]

    def _sincronizar_slot_actual(self, solo_proveedor: bool = False) -> None:
        """Copia el estado vivo (proveedor + origen de fotos) al slot en
        edición y lo persiste. `solo_proveedor=True` cuando el usuario está
        corrigiendo de quién es el lote y no tocó el origen."""
        slot = self._slot_actual()
        slot["proveedor_id"] = self._proveedor_id_actual
        slot["proveedor_nombre"] = self._proveedor_nombre_actual
        if not solo_proveedor:
            if self._entrada_archivos:
                slot["entrada_archivos"] = [str(p) for p in self._entrada_archivos]
                slot["entrada_dir"] = None
            else:
                texto = self.v_entrada.get().strip()
                slot["entrada_archivos"] = None
                slot["entrada_dir"] = texto or None
        self._persistir_proveedores_del_lote()
        self._refrescar_resumen_proveedores()

    def _persistir_proveedores_del_lote(self) -> None:
        if not self._proveedores_del_lote:
            return
        self._guardar_marca_lote(
            **{self.CLAVE_PROVEEDORES_LOTE: self._proveedores_del_lote})

    def _restaurar_proveedores_del_lote(self, carpeta: Path) -> None:
        """Al retomar un lote: recupera la lista de proveedores agregados.

        Compatibilidad con lotes viejos (y con el caso de un solo proveedor,
        que sigue guardando además `proveedor_id`/`entrada_dir` sueltos): si
        no hay lista guardada, se arma una de un elemento con lo que haya."""
        datos = self._leer_marca_lote(carpeta)
        lista = datos.get(self.CLAVE_PROVEEDORES_LOTE)
        if isinstance(lista, list) and lista:
            self._proveedores_del_lote = [dict(s) for s in lista if isinstance(s, dict)]
        elif datos.get("proveedor_id") is not None:
            self._proveedores_del_lote = [{
                "proveedor_id": int(datos["proveedor_id"]),
                "proveedor_nombre": str(datos.get("proveedor_nombre") or ""),
                "entrada_dir": datos.get("entrada_dir"),
                "entrada_archivos": datos.get("entrada_archivos"),
                "limpiado": True, "enviado": bool(datos.get("enviado_en")),
            }]
        # El slot "en edición" tiene que ser el del proveedor que quedó activo
        # en la marca suelta del lote; si no, la próxima sincronización
        # escribiría los datos de un proveedor sobre el slot de otro.
        self._indice_proveedor_actual = max(0, len(self._proveedores_del_lote) - 1)
        activo = datos.get("proveedor_id")
        if activo is not None:
            for n, slot in enumerate(self._proveedores_del_lote):
                if slot.get("proveedor_id") == int(activo):
                    self._indice_proveedor_actual = n
                    break
        # FASE 3: lotes guardados antes de esta fase tienen la moneda/tipo de
        # cambio/margen como claves sueltas del lote. Se copian a los slots acá
        # mismo, al retomar el lote, para no volver a preguntar algo ya
        # contestado ni perder el tipo de cambio con el que se cotizó.
        self._migrar_config_precio_a_slots(datos, carpeta)
        self._refrescar_resumen_proveedores()

    def _resumen_proveedores_texto(self) -> str:
        if not self._proveedores_del_lote:
            return "Todavía no se agregó ningún proveedor a este lote."
        lineas = []
        for n, slot in enumerate(self._proveedores_del_lote, start=1):
            nombre = slot.get("proveedor_nombre") or "(sin proveedor)"
            if slot.get("entrada_archivos"):
                archivos = slot["entrada_archivos"]
                origen = (Path(archivos[0]).name if len(archivos) == 1
                          else f"{len(archivos)} archivo(s) de {Path(archivos[0]).parent.name}")
            elif slot.get("entrada_dir"):
                origen = Path(str(slot["entrada_dir"])).name or str(slot["entrada_dir"])
            else:
                origen = "(sin origen elegido)"
            marca = ""
            if slot.get("enviado"):
                marca = "  ✔ enviado"
            elif slot.get("limpiado"):
                marca = "  · fotos limpias"
            lineas.append(f"{n}. {nombre}  —  {origen}{marca}")
        return "\n".join(lineas)

    def _refrescar_resumen_proveedores(self) -> None:
        if hasattr(self, "v_lista_proveedores"):
            self.v_lista_proveedores.set(self._resumen_proveedores_texto())

    def _lote_multiproveedor(self) -> bool:
        """¿Este lote tiene más de un proveedor con proveedor elegido?"""
        return sum(1 for s in self._proveedores_del_lote
                   if s.get("proveedor_id") is not None) > 1

    def _agregar_otro_proveedor(self) -> None:
        """Botón «➕ Agregar otro proveedor a este lote» del paso 1.

        No reemplaza nada: guarda lo ya elegido en su slot, abre un slot nuevo
        y vuelve a pedir proveedor + origen. Lo elegido antes queda intacto en
        la lista (y en `lote_meta`)."""
        if self._proveedor_id_actual is None or not self.v_entrada.get().strip():
            messagebox.showinfo(
                "Agregar otro proveedor",
                "Primero completá el proveedor y el origen de las fotos del "
                "proveedor que estás cargando ahora.\n\nDespués sí podés "
                "agregar otro proveedor a este mismo lote.")
            return
        self._sincronizar_slot_actual()
        # Slot nuevo al final, y el estado vivo queda en blanco para que el
        # usuario elija el proveedor/origen del segundo catálogo sin que se
        # pise el primero.
        self._indice_proveedor_actual = len(self._proveedores_del_lote)
        self._slot_actual()
        self._proveedor_id_actual = None
        self._proveedor_nombre_actual = None
        self._entrada_archivos = None
        self._archivos_revision = []
        self._exclusiones_revision = set()
        self.v_entrada.set("")
        self.v_proveedor.set("Proveedor: sin elegir")
        self._refrescar_resumen_proveedores()
        self._actualizar_paso()
        self._elegir_proveedor_inicial()

    def _activar_slot(self, indice: int) -> bool:
        """Pone en el estado vivo el proveedor/origen del slot `indice`, para
        procesarlo con el pipeline de limpieza existente (pasos 2-4)."""
        if not (0 <= indice < len(self._proveedores_del_lote)):
            return False
        slot = self._proveedores_del_lote[indice]
        if slot.get("proveedor_id") is None:
            return False
        self._indice_proveedor_actual = indice
        self._proveedor_id_actual = int(slot["proveedor_id"])
        self._proveedor_nombre_actual = str(slot.get("proveedor_nombre") or "")
        self.v_proveedor.set(f"Proveedor: {self._proveedor_nombre_actual}")
        archivos = slot.get("entrada_archivos")
        if archivos:
            self._entrada_archivos = [Path(a) for a in archivos]
            self.v_entrada.set(f"{len(archivos)} archivo(s) seleccionado(s)")
        else:
            self._entrada_archivos = None
            self.v_entrada.set(str(slot.get("entrada_dir") or ""))
        # El paso 2 (elegir qué limpiar) ya se resolvió cuando se eligió el
        # origen de este slot: su resultado son los archivos de arriba.
        self._archivos_revision = []
        self._exclusiones_revision = set()
        # El proveedor activo también manda en la marca suelta del lote, que
        # es la que leen `_restaurar_proveedor_del_lote` y el envío.
        self._guardar_marca_lote(proveedor_id=self._proveedor_id_actual,
                                 proveedor_nombre=self._proveedor_nombre_actual)
        self._refrescar_resumen_proveedores()
        self._actualizar_paso()
        return True

    def _indice_proveedor_pendiente(self, clave: str) -> int | None:
        """Primer slot del lote con `clave` (`limpiado`/`enviado`) en False."""
        for n, slot in enumerate(self._proveedores_del_lote):
            if slot.get("proveedor_id") is not None and not slot.get(clave):
                return n
        return None

    def _laminas_del_slot(self, slot: dict) -> set[str]:
        """Nombres (con y sin extensión) de las láminas de origen de un slot.

        Es lo que permite saber, en un lote con dos catálogos limpiados en la
        MISMA carpeta de trabajo, cuáles recortes son de cuál proveedor."""
        nombres: set[str] = set()
        archivos = slot.get("entrada_archivos") or []
        rutas = [Path(a) for a in archivos]
        if not rutas and slot.get("entrada_dir"):
            carpeta = Path(str(slot["entrada_dir"]))
            if carpeta.is_dir():
                rutas = [p for p in carpeta.rglob("*") if p.is_file()]
        for p in rutas:
            nombres.add(p.name)
            nombres.add(p.stem)
        return nombres

    def _recortes_del_proveedor_activo(self) -> set[str]:
        """Qué recortes de la carpeta de trabajo le corresponden al proveedor
        activo. Con un solo proveedor son TODOS -- exactamente el
        comportamiento de siempre."""
        if not self._lote_multiproveedor():
            return set(self._recortes)
        propias = self._laminas_del_slot(self._slot_actual())
        elegidos = set()
        for nombre, r in self._recortes.items():
            origen = r.get("_origen_lamina") or ""
            if not origen:
                continue
            p = Path(str(origen))
            if p.name in propias or p.stem in propias:
                elegidos.add(nombre)
        return elegidos

    def _abrir_ajustes(self) -> None:
        """Ajustes que se configuran una vez y no vuelven a tocarse — hoy,
        solo el método de comparación (TI vs. estándar). Salió del diálogo de
        proveedor: mezclado ahí obligaba a decidirlo en cada catálogo, cuando
        en realidad es una configuración de la instalación."""
        import motor_candidatos

        ventana = ctk.CTkToplevel(self)
        ventana.title("Ajustes")
        _centrar_en_ventana_principal(self, ventana, 460, 300)
        Etiqueta(ventana, text="Método de comparación", style="Subtitulo.TLabel").pack(
            anchor="w", padx=14, pady=(14, 4))
        Etiqueta(ventana, text="Con qué espacio de vectores se buscan los comparables. "
                 "No se pueden mezclar: cambiarlo aplica al próximo catálogo que se envíe "
                 "a calificar.", style="Suave.TLabel", justify="left",
                 wraplength=380).pack(anchor="w", padx=14, pady=(0, 8), fill="x")
        v_modelo = tk.StringVar(value=motor_candidatos.modelo_activo_configurado())
        # Las opciones salen de `MODELOS_ACTIVOS_VALIDOS` + `ETIQUETA_METODOS`
        # (los mismos que muestra el paso 5): así agregar un método nuevo no
        # obliga a acordarse de esta ventana, que era justo lo que pasaba antes
        # con las dos opciones escritas a mano.
        for i, valor in enumerate(motor_candidatos.MODELOS_ACTIVOS_VALIDOS):
            titulo, _ = self.ETIQUETA_METODOS.get(valor, (valor, ""))
            ultimo = (i == len(motor_candidatos.MODELOS_ACTIVOS_VALIDOS) - 1)
            ctk.CTkRadioButton(ventana, text=titulo, variable=v_modelo, value=valor).pack(
                anchor="w", padx=14, pady=((4 if i == 0 else 2), 10 if ultimo else 0))

        def guardar() -> None:
            motor_candidatos.establecer_modelo_activo(v_modelo.get())
            ventana.destroy()

        fila = Marco(ventana)
        fila.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila, text="Cancelar", style="Sutil.TButton",
              command=ventana.destroy).pack(side="right")
        Boton(fila, text="Guardar", style="Primario.TButton",
              command=guardar).pack(side="right", padx=(0, 8))
        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)

    def _candidatos_del_lote(self) -> list[dict]:
        """Los candidatos de ESTE lote (proveedor + catálogo actuales), no todo
        lo que haya en staging.

        Bug real corregido el 2026-09-09: `obtener_candidatos()` se llamaba sin
        filtro, así que la pantalla 6 mostraba también los candidatos de otro
        proveedor si alguien había enviado un catálogo con "Agregar" en vez de
        "Empezar nuevo". El filtro se hace por proveedor Y por `catalogo_origen`
        (las dos columnas que ya trae cada fila de `dim_candidato`).

        El filtro es ESTRICTO y sin red de seguridad (2026-09-09, segunda
        vuelta: el primer intento seguía mostrando candidatos ajenos). El
        arreglo anterior tenía tres escapes que, juntos, hacían que en la
        práctica casi nunca filtrara:

        1. `filtrar_candidatos(todos, None)` NO filtra nada -- devuelve la
           lista entera. Si el lote en pantalla todavía no tiene proveedor
           elegido (`_proveedor_id_actual is None`), "el filtro por proveedor"
           era la identidad, y salía TODO staging.
        2. `if not self._salida: return del_proveedor` -- sin carpeta de
           trabajo tampoco se filtraba por catálogo.
        3. `return del_lote or del_proveedor` -- si el `catalogo_origen` no
           coincidía (lo normal cuando el lote en pantalla NO es el que está
           cargado en staging), se devolvía igual todo lo del proveedor. Con
           (1) encima, eso era "todo staging".

        Resultado real observado: staging tenía únicamente ARDY BROS /
        reebok_extraido (51 candidatos), y esos 51 aparecían en el paso 6 de
        cualquier lote. Ahora, si no se puede identificar el lote (sin
        proveedor o sin carpeta) o si staging no tiene NADA de este lote, se
        devuelve lista vacía: `_ver_candidatos_calificados` avisa "no hay
        candidatos" en vez de enseñar los de otro proveedor. Mostrar una
        pantalla vacía es correcto; mostrar el catálogo de otro no lo es.

        FASE 5 multi-proveedor (2026-09-16): el filtro es por TODOS los
        proveedores del lote, no solo por el activo. Antes filtraba por
        `_proveedor_id_actual`, así que en un lote con dos catálogos la
        pantalla 6 mostraba únicamente el del proveedor que se acababa de
        enviar: la pastilla de proveedor, el filtro «Ver: Todos/A/B» y el
        desglose por proveedor del paso 7 (toda la Fase 4) nunca se activaban
        en el flujo real, y el paso 7 quedaba incoherente con el 6 porque
        `api_pedido_sugerido` SÍ reparte el PDV entre los candidatos de todo
        el lote. Con un solo proveedor la lista que sale de acá es exactamente
        la de antes (mismo filtro, mismo orden).
        """
        import motor_candidatos
        pids = [int(s["proveedor_id"]) for s in self._proveedores_del_lote
                if s.get("proveedor_id") is not None]
        if self._proveedor_id_actual is not None and \
                int(self._proveedor_id_actual) not in pids:
            pids.append(int(self._proveedor_id_actual))
        if not pids or not self._salida:
            return []
        posibles = self._catalogos_posibles_del_lote(self._salida)
        if not posibles:
            return []
        # Primero el filtro por catálogo (común a todo el lote), después la
        # unión de los proveedores. Se conserva el orden global por score que
        # devuelve `obtener_candidatos`, y se sigue apoyando en
        # `filtrar_candidatos` por proveedor -- o sea que un `proveedor_id`
        # que el maestro no resuelve sigue aportando CERO candidatos, nunca
        # "todos".
        del_catalogo = motor_candidatos.filtrar_candidatos(
            motor_candidatos.obtener_candidatos(), None, posibles)
        ids = set()
        for pid in pids:
            ids.update(c["candidato_id"]
                       for c in motor_candidatos.filtrar_candidatos(del_catalogo, pid))
        return [c for c in del_catalogo if c["candidato_id"] in ids]

    def _ver_candidatos_calificados(self) -> None:
        """Abre la vista de candidatos con lo que YA está en staging, sin
        reprocesar ni reenviar nada."""
        try:
            candidatos = self._candidatos_del_lote()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudieron consultar los candidatos:\n{exc}")
            return
        if not candidatos:
            messagebox.showinfo(
                "Asistente de Compras",
                "No hay ningún candidato en revisión ahora mismo.\n\n"
                "Procesá un catálogo y usá «🛒 Enviar a Asistente de Compras» "
                "desde la pantalla de Confirmar y enviar.")
            return
        self._mostrar_vista_candidatos(candidatos)

    def _mostrar_vista_candidatos(self, candidatos: list[dict]) -> None:
        """Paso 6: la lista de candidatos calificados, como pantalla DENTRO de
        la ventana principal (antes era un Toplevel aparte que tapaba la app y
        se perdía al cerrarlo)."""
        # Reloj de arena mientras arma la grilla de candidatos (puede haber
        # muchos, con foto cada uno) -- sin esto no había ninguna señal de que
        # el programa seguía vivo entre el clic y que aparezca la pantalla.
        self.configure(cursor="watch")
        self.after(400, lambda: self.configure(cursor=""))
        # Antes de armar la pantalla: en qué moneda cotiza este proveedor (y
        # con qué tipo de cambio). Sin eso no se puede calcular ningún precio
        # de venta sugerido, que es la mitad de lo que se muestra acá. Es UNA
        # VEZ POR PROVEEDOR DEL LOTE (FASE 3): si ya está declarado, no
        # pregunta nada y esto no agrega ninguna fricción al recargar. Con un
        # solo proveedor es una sola pregunta, igual que antes.
        self.asegurar_monedas_de_proveedores()
        # …y para qué mes se espera vender este pedido: es la otra premisa de
        # la compra (temporada), y también se pregunta UNA VEZ POR LOTE.
        self.asegurar_mes_venta_lote()
        self._asegurar_cuerpo_visible()
        self._cerrar_vista_candidatos(volver=False)
        self._ocultar_pantallas_base()
        self._candidatos_frame = VentanaCandidatosCTk(
            self, candidatos, al_volver=self._cerrar_vista_candidatos)
        self._candidatos_frame.pack(side="top", fill="both", expand=True,
                                    padx=6, pady=(0, 6))
        self._candidatos_frame.mostrar_candidatos()
        self._actualizar_paso(6)
        self._avisar_si_metricas_viejas()

    def _avisar_si_metricas_viejas(self) -> None:
        """Auditoría 2026-09-23 (M4): si el proceso que recalcula rotación/
        venta/precio (`GestionTUC/_actualizar_metricas.bat`) se dejó de
        correr, los scores de este lote se calculan en silencio contra datos
        cada vez más viejos -- sin este aviso, nadie lo nota mirando la
        pantalla. Una sola consulta por sesión (`avisar_si_metricas_viejas`
        se auto-limita), en un hilo aparte para no trabar la pantalla de
        candidatos con un viaje a Postgres."""
        canal = self.canal_venta_lote()
        if canal is None:
            return

        def _consultar() -> None:
            try:
                import motor_candidatos
                mensaje = motor_candidatos.avisar_si_metricas_viejas(canal)
            except Exception:  # noqa: BLE001 -- un aviso que falla no puede tumbar el paso 6
                _logger_gui().exception("no se pudo verificar la frescura de las métricas")
                return
            if mensaje:
                self.after(0, lambda: messagebox.showwarning("Datos de venta desactualizados",
                                                              mensaje))

        threading.Thread(target=_consultar, daemon=True).start()

    def _mostrar_sugerido_compra(self) -> None:
        """Paso 7 y último: el sugerido de compra (PDV objetivo → cuántos pares
        de cada referencia). Antes era una pestaña adentro de la pantalla de
        candidatos, donde el comprador no la encontraba: es el resultado final
        de todo el proceso, así que tiene su propio paso y su pastilla."""
        if self._candidatos_frame is None:
            # Se puede caer acá desde el menú o retomando un lote ya enviado:
            # se abre primero la pantalla que lo contiene.
            self._ver_candidatos_calificados()
            if self._candidatos_frame is None:
                return
        self._candidatos_frame.mostrar_optimizador()
        self._actualizar_paso(7)

    def _volver_a_candidatos(self) -> None:
        """«← Atrás» desde el paso 7: misma pantalla, sub-vista de candidatos."""
        if self._candidatos_frame is None:
            self._cerrar_vista_candidatos()
            return
        self._candidatos_frame.mostrar_candidatos()
        self._actualizar_paso(6)

    def _cerrar_vista_candidatos(self, volver: bool = True) -> None:
        if self._candidatos_frame is not None:
            self._candidatos_frame.destroy()
            self._candidatos_frame = None
            if volver:
                self._mostrar_cuerpo()
                # Si no hay un lote cargado, volver a "Limpiar y revisar" sería
                # mentira: la pantalla de atrás está vacía y lo que falta es
                # elegir origen (paso 1).
                self._actualizar_paso(3 if self._recortes else 1)

    def _revertir_todo(self) -> None:
        """Revierte TODA la carpeta activa de una — cada recorte que tenga
        un respaldo en `_originales/` vuelve a esa versión. Con confirmación
        antes: afecta a todo el lote de una vez, no una foto puntual."""
        if not self._salida:
            messagebox.showinfo("Revertir todo", "Primero elegí una carpeta de trabajo.")
            return
        respaldo = self._salida / reparar.CARPETA_ORIGINALES
        if not respaldo.exists():
            messagebox.showinfo("Revertir todo", "No hay ningún respaldo en esta carpeta — "
                                "ningún recorte tuvo una corrección aplicada todavía.")
            return
        stems = sorted({p.stem for p in respaldo.glob("*.png")} | {p.stem for p in respaldo.glob("*.jpg")})
        if not stems:
            messagebox.showinfo("Revertir todo", "No hay ningún respaldo en esta carpeta.")
            return
        if not messagebox.askyesno(
                "Revertir todo",
                f"Esto revierte {len(stems)} recorte(s) a su versión original, deshaciendo "
                f"cualquier recorte de sombra o reparación de suela que se les haya aplicado.\n\n"
                f"¿Continuar?"):
            return
        revertidos = sum(1 for stem in stems if reparar.revertir(self._salida, stem))
        self._recargar_manifiesto()
        messagebox.showinfo("Revertir todo", f"{revertidos} de {len(stems)} recorte(s) revertidos.")

    def _vaciar_carpeta_trabajo(self) -> None:
        """Borra SOLO lo que el pipeline genera en la carpeta de trabajo
        (fotos procesadas, manifiesto, decisiones, respaldos) — nunca las
        fotos de entrada/extraídas, aunque vivan en esa misma carpeta.

        Existe porque "Exportar aprobados" mira TODA la bitácora de
        decisiones de la carpeta, de todas las cargas anteriores que se
        hayan hecho ahí — si se reusa la misma carpeta de trabajo para un
        lote nuevo (otro Excel, otro PDF), sin esto las aprobaciones viejas
        se cuelan en la exportación de hoy.

        La primera versión de este botón borraba TODO el contenido de la
        carpeta sin distinguir "caché del programa" de "fotos que el
        usuario puso ahí" — si la carpeta de trabajo era la misma carpeta
        de fotos extraídas (caso real: `Excel\\_fotos_extraidas`), eso
        borraba las fotos de entrada también. Por eso ahora es una lista
        explícita de lo que el pipeline crea, nada más."""
        SUBCARPETAS_PIPELINE = ("transparente", "blanco", "revision", "debug",
                                reparar.CARPETA_ORIGINALES, "_pasos", "_vecinos")
        # El estado del lote se vacía por SQL (`almacen.vaciar`, más abajo), no
        # borrando `lote.sqlite`: la interfaz puede tener el archivo abierto y
        # en Windows eso falla o deja los sidecars `-wal`/`-shm` huérfanos.
        # Estos patrones son los archivos VIEJOS que la migración dejó al lado
        # como red de seguridad: acá sí se borran, porque vaciar la carpeta es
        # justamente decir "no quiero nada de las cargas anteriores".
        PATRONES_ARCHIVO_PIPELINE = ("manifiesto_*.jsonl", "plan_*.jsonl",
                                     "decisiones.jsonl", "reparaciones.jsonl",
                                     "_lote_asistente.json")

        if not self._salida or not self._salida.exists():
            messagebox.showinfo("Vaciar carpeta de trabajo", "Primero elegí una carpeta de trabajo.")
            return

        # Vaciar mientras el worker sigue vivo (pausado o corriendo) deja el
        # vaciado a medias: el proceso sigue escribiendo manifiesto/recortes
        # sobre lo que se acaba de borrar en cuanto se reanuda o simplemente
        # sigue su lámina actual. Pasó de verdad al combinar Pausar + Vaciar.
        if any(p.poll() is None for p in self._procs):
            estado = "pausado" if self._pausado else "corriendo"
            messagebox.showwarning(
                "Vaciar carpeta de trabajo",
                f"Hay un procesamiento {estado} sobre esta carpeta ahora mismo.\n\n"
                f"Detenelo primero (botón «Detener») y después vaciá — si no, el "
                f"proceso sigue escribiendo encima de lo que se borra.")
            return

        a_borrar: list[Path] = [self._salida / c for c in SUBCARPETAS_PIPELINE
                                if (self._salida / c).exists()]
        for patron in PATRONES_ARCHIVO_PIPELINE:
            a_borrar.extend(self._salida.glob(patron))

        if not a_borrar and not almacen.contar_recortes(self._salida):
            messagebox.showinfo("Vaciar carpeta de trabajo",
                                "No hay nada procesado por el programa en esta carpeta todavía.")
            return
        if not messagebox.askyesno(
                "Vaciar carpeta de trabajo",
                f"Esto borra lo que el programa generó en:\n{self._salida}\n\n"
                f"Fotos procesadas, aprobaciones/descartes y respaldos — de esta carga "
                f"y de cualquier carga anterior hecha en la misma carpeta.\n\n"
                f"Las fotos de ENTRADA (las que se extrajeron o elegiste procesar) "
                f"NO se tocan.\n\nNo se puede deshacer.\n\n¿Continuar?"):
            return
        errores = 0
        for item in a_borrar:
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except OSError:
                _logger_gui().exception("no se pudo borrar %s al vaciar la carpeta de trabajo", item)
                errores += 1

        try:
            almacen.vaciar(self._salida)
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo vaciar el almacen del lote %s", self._salida)
            errores += 1

        self._recortes = {}
        self.tabla.delete(*self.tabla.get_children())
        self._limpiar_panel_detalle()
        self._asegurar_cuerpo_visible()
        self.v_carpeta_activa.set(f"Carpeta activa: {self._salida}  ·  sin recortes procesados todavía")
        mensaje = "Carpeta de trabajo vaciada."
        if errores:
            mensaje += f"\n\n{errores} elemento(s) no se pudieron borrar (puede que estén abiertos)."
        messagebox.showinfo("Vaciar carpeta de trabajo", mensaje)

    # ── procesar ─────────────────────────────────────────────────────────

    def _procesar(self) -> None:
        # El monitor de avance se ancla con `pack(before=self.cuerpo)`, así que
        # `self.cuerpo` TIENE que estar empaquetado antes de arrancar. Con el
        # asistente por pasos eso ya no se cumple solo (en el paso 1 la
        # pantalla visible es otra), así que se garantiza acá.
        self._mostrar_cuerpo()
        entrada_dir = self.v_entrada.get().strip()
        entrada: Path | list[Path] | None = self._entrada_archivos or (Path(entrada_dir) if entrada_dir else None)
        if not entrada:
            messagebox.showwarning("Procesar", "Elegí una carpeta o archivos de entrada.")
            return

        salida = self.v_salida.get().strip()
        if not salida:
            # No bloquear con una advertencia y listo — la carpeta de salida
            # es obligatoria pero el botón para elegirla queda ANTES en la
            # barra (se puede cargar el Excel sin haberla llenado todavía).
            # Pedirla acá mismo evita que el usuario tenga que volver atrás.
            elegida = filedialog.askdirectory(title="Elegí la carpeta de salida para continuar")
            if not elegida:
                return
            salida = elegida
            self.v_salida.set(salida)
        try:
            plan = nucleo.planificar(entrada, Path(salida), rehacer=self.v_rehacer.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Procesar", str(exc))
            return
        if not plan.pendientes:
            messagebox.showinfo("Procesar", "No hay láminas nuevas para procesar.")
            return

        self._asegurar_cuerpo_visible()

        # Se está procesando de verdad: lo que va a haber en pantalla ya no es
        # "un lote viejo tal como quedó", así que la franja de retomar y el
        # rótulo "＋ Agregar más fotos" dejan de aplicar.
        self._retomando_lote = False
        self._refrescar_aviso_retomar()

        self._salida = Path(salida)
        self._guardar_ultima_carpeta()
        # De DÓNDE salieron las fotos crudas queda anotado en la carpeta del
        # lote, igual que el proveedor. Sin esto, al retomar el lote otro día
        # `v_entrada`/`_entrada_archivos` arrancaban vacíos y tocar
        # "▶ Limpiar fotos" (por ejemplo para sumar fotos nuevas al mismo lote)
        # fallaba con "Elegí una carpeta o archivos de entrada" — aunque el
        # usuario nunca había elegido mal: el dato simplemente no se guardaba.
        # Se anota acá porque es el único punto donde el origen ya está
        # confirmado de verdad (`planificar` no reventó) y `self._salida` ya
        # apunta a la carpeta definitiva del lote.
        campos_meta = {}
        if self._entrada_archivos:
            campos_meta["entrada_archivos"] = [str(p) for p in self._entrada_archivos]
            campos_meta["entrada_dir"] = None
        elif entrada_dir:
            campos_meta["entrada_dir"] = entrada_dir
            campos_meta["entrada_archivos"] = None
        # Guardar el nombre del proyecto si está definido
        if self._nombre_proyecto:
            campos_meta["nombre_proyecto"] = self._nombre_proyecto
        if campos_meta:
            self._guardar_marca_lote(**campos_meta)
        self.v_carpeta_activa.set(f"Carpeta activa: {self._salida}  ·  procesando…")
        # H4 de la auditoría de flujo: antes SIEMPRE n_procesos=1, aunque
        # `escribir_planes`/`worker.py --id` ya estaban preparados para
        # repartir en varios -- las láminas son independientes entre sí, así
        # que es la paralelización más barata disponible. Tope en 4: cada
        # proceso carga su propia copia de los modelos de detección/matting
        # (memoria real, no gratis), y no tiene sentido pedir más procesos
        # que láminas pendientes.
        n_procesos = max(1, min(4, os.cpu_count() or 1, len(plan.pendientes)))
        planes = nucleo.escribir_planes(plan, self._salida, n_procesos=n_procesos)
        self._procs.clear()
        # Indexado por las DOS claves a propósito: el monitor en vivo busca
        # por nombre de archivo con extensión (lo que manda el worker en
        # `campos["archivo"]`), y el resumen final busca por stem (el nombre
        # de la lámina sin extensión, derivado del nombre del recorte). Con
        # una sola clave, uno de los dos siempre fallaba en silencio — y era
        # el resumen: por eso la columna de "lámina original" salía vacía.
        self._rutas_pendientes = {}
        for p, _ in plan.pendientes:
            self._rutas_pendientes[p.name] = p
            self._rutas_pendientes[p.stem] = p
        self.v_estado_proc.set(f"Procesando 0/{len(plan.pendientes)}")
        self.btn_procesar.state(["disabled"])
        self.btn_detener.state(["!disabled"])
        self.btn_pausar.state(["!disabled"])
        self.btn_pausar.configure(text="⏸ Pausar")
        self._pausado = False
        self._hechas_proc = 0
        self._total_proc = len(plan.pendientes)
        self._pasos_hechos_lamina = 0
        self._pasos_total_lamina = 3  # cargada + detectada + separadas, hasta saber cuántas piezas hay
        self._piezas_lamina_actual = 0

        # Se está limpiando: la pantalla es el paso 3 ("Limpiar y revisar"),
        # tanto si se llegó desde el paso 2 como si se tocó "＋ Agregar más
        # fotos" con el lote ya revisándose. La marca va ANTES de
        # `_actualizar_paso`: es la que decide que el monitor de avance se vea
        # (y la que lo retira sola al terminar).
        self._procesando_lote = True
        self._actualizar_paso(3)
        self.monitor.pack(side="top", fill="x", padx=10, pady=(0, 8), before=self.cuerpo)
        self.v_lamina_actual.set("Arrancando…")
        self.v_paso_actual.set("")
        self.barra_progreso.configure(value=0)
        self.v_pct.set("0%")
        self.barra_progreso_lamina.configure(value=0)
        self.v_pct_lamina.set("")
        self._limpiar_consola()
        self._reiniciar_medicion_lote()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        # Sin CREATE_NO_WINDOW, Windows le abre su propia consola visible al
        # proceso hijo porque la app corre con pythonw (sin consola propia) —
        # esa es la "ventana negra" que aparecía. La salida ya se captura por
        # el pipe, no hace falta ninguna consola.
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        # Todos los procesos empujan sus líneas ("linea") a LA MISMA cola --
        # se intercalan en pantalla (dos láminas de golpe en vez de una por
        # una), que es un efecto secundario visual aceptado a cambio de
        # aprovechar los núcleos disponibles. Lo que sí importa es no cerrar
        # el lote hasta que TODOS terminen -- `self._procs_pendientes` cuenta
        # cuántos siguen vivos; el evento "fin" ya no cierra la pantalla por
        # sí solo (ver `_bombear_cola`).
        self._procs_pendientes = len(planes)

        def lanzar_uno(id_proceso: int) -> None:
            # Ya no se le pasa la ruta de un `plan_N.jsonl`: cada worker lee su
            # tramo de la tabla `plan` del `lote.sqlite` por su `--id`, que es
            # el argumento que ya recibía. Un argumento menos que se puede
            # desincronizar del id.
            cmd = [str(nucleo.python_zawa()), str(PROYECTO / "worker.py"),
                   "--salida", str(self._salida), "--id", str(id_proceso)]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        cwd=str(PROYECTO), env=env, encoding="utf-8", errors="replace",
                                        creationflags=creationflags)
                self._procs.append(proc)
                for linea in proc.stdout:
                    self._q.put(("linea", linea.rstrip()))
                proc.wait()
                self._q.put(("fin", proc.returncode))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("linea", f"ERROR al lanzar proceso {id_proceso}: {exc}"))
                self._q.put(("fin", -1))

        for id_proceso in planes:
            threading.Thread(target=lanzar_uno, args=(id_proceso,), daemon=True).start()

    def _detener(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                # terminate() mata el proceso igual esté pausado o no — no
                # hace falta reanudarlo primero.
                p.terminate()
        self.v_estado_proc.set("Detenido")
        # Ya no corre nada: el monitor de avance se retira y la pantalla queda
        # entera para la revisión de lo que sí se alcanzó a limpiar.
        self._procesando_lote = False
        self.btn_detener.state(["disabled"])
        self.btn_pausar.state(["disabled"])
        self.btn_pausar.configure(text="⏸ Pausar")
        self._pausado = False
        self.v_lamina_actual.set("Detenido por el usuario")
        self.v_paso_actual.set("")
        self.v_eta.set("")
        self._ocultar_sin_actividad()

        # Cortar el lote NO tira lo ya procesado: cada recorte se guardó en
        # disco apenas salió. Pero la lista en pantalla solo se recargaba con
        # el evento "fin" del worker, que al matarlo nunca llega — así que
        # quedaba vacía y parecía que se había perdido todo. Se recarga acá.
        self._recargar_manifiesto()
        if self._salida:
            self.v_carpeta_activa.set(
                f"Carpeta activa: {self._salida}  ·  {len(self._recortes)} recorte(s) cargados")
        if self._recortes:
            self.v_log.set(f"Detenido: {len(self._recortes)} recorte(s) ya procesados y guardados")
            self._aterrizar_en_revision()
        else:
            self.v_log.set("Detenido antes de terminar ninguna lámina")

    def _abrir_primera_foto(self) -> None:
        """Deja la primera foto del lote abierta en el visor.

        Entrar a "Revisar" con el panel de detalle vacío obliga a un clic extra
        antes de poder empezar, y deja la barra "CORREGIR ESTA FOTO" escondida
        (solo aparece con una foto seleccionada) — con la pantalla masiva
        eliminada, ésta es la primera cosa que ve el comprador al terminar de
        limpiar, así que tiene que venir lista para trabajar.
        """
        if self._nombre_actual:
            return
        primero = next(iter(sorted(self._recortes)), None)
        if primero and self.tabla.exists(primero):
            self.tabla.selection_set(primero)

    def _aterrizar_en_revision(self) -> None:
        """Al terminar (o cortar) la limpieza, la MISMA pantalla se queda solo
        con la grilla de revisión: el monitor de avance se retira (ya no hay
        nada corriendo) y el comprador sigue decidiendo donde estaba.

        Antes esto era un cambio de paso (3 → 4) con su propia pantalla; con
        los pasos fusionados (2026-09-16) es la transición interna del paso 3,
        y no le cuesta ningún clic al usuario.

        Antes acá se abría la pantalla masiva "Elegir recortes a limpiar", que
        era un peaje: había que pasar por una grilla de casillas ANTES de poder
        ver las fotos una por una. Ofrecía las mismas dos correcciones que la
        barra "CORREGIR ESTA FOTO", así que el comprador elegía a ciegas en una
        pantalla lo que después revisaba de verdad en la otra.

        Ahora las correcciones conviven con la foto grande y las líneas de
        referencia. El caso "aplicar a varias de una pasada" sigue disponible
        ahí mismo: Ctrl/Shift+clic en la grilla levanta la barra de acciones en
        bloque.
        """
        if not self._recortes:
            self._mostrar_resumen_final()
            return
        self._asegurar_cuerpo_visible()
        self._ir_a_paso(3)
        self._abrir_primera_foto()
        self.v_log.set(
            f"{len(self._recortes)} recorte(s) listos para revisar. "
            "Marcá varias con Ctrl+clic para corregirlas de una sola vez.")

    def _pausar_reanudar(self) -> None:
        """Pausa/reanuda de verdad el proceso (via `psutil`, congela sus
        hilos), a diferencia de "Detener" que lo mata. Útil para dejar la
        computadora en algo urgente sin perder el avance ni tener que
        reprocesar nada al volver — la lámina que estaba a medias sigue
        exactamente ahí cuando se reanuda."""
        procesos_vivos = [p for p in self._procs if p.poll() is None]
        if not procesos_vivos:
            return
        try:
            if not self._pausado:
                for p in procesos_vivos:
                    psutil.Process(p.pid).suspend()
                self._pausado = True
                self.btn_pausar.configure(text="▶ Reanudar")
                self.v_estado_proc.set("Pausado")
                self.v_lamina_actual.set("Pausado por el usuario — el avance no se pierde")
            else:
                for p in procesos_vivos:
                    psutil.Process(p.pid).resume()
                self._pausado = False
                self.btn_pausar.configure(text="⏸ Pausar")
                self.v_estado_proc.set(f"Procesando {self._hechas_proc}/{self._total_proc}")
        except psutil.Error as exc:
            messagebox.showerror("Pausar/Reanudar", f"No se pudo cambiar el estado del proceso:\n{exc}")

    def _set_estado_compras(self, msg: str) -> None:
        """Escribe el avance del envío tanto en la línea propia de la tarjeta
        "Confirmar y enviar" (`v_estado_compras`, la que ve el usuario si sigue
        ahí) como en el estado general de la barra superior -- por si el
        envío sigue corriendo en segundo plano después de que el usuario
        navegó a otra pantalla ("Volver a revisar" destruye la tarjeta, pero
        el hilo de fondo sigue avisando)."""
        self.v_estado_proc.set(msg)
        v_compras = getattr(self, "v_estado_compras", None)
        if v_compras is not None:
            v_compras.set(msg)

    def _avance_compras(self, pct: float) -> None:
        """Mueve la barra propia del envío a calificar, si la tarjeta de
        "Confirmar y enviar" sigue en pantalla. No usa el monitor de láminas:
        ese habla de otra cosa y ya no se muestra en este paso."""
        barra = getattr(self, "barra_compras", None)
        if barra is None:
            return
        try:
            barra.configure(value=pct)
        except tk.TclError:
            # barra de progreso de compras ya destruida: el % simplemente no se pinta.
            pass

    def _mostrar_progreso_compras(self) -> None:
        """Muestra el bloque "Progreso del envío al Asistente de Compras"
        (rótulo + línea de estado + barra) justo arriba del separador de la
        tarjeta. Fuera de un envío, ese bloque no se empaca: una barra sin
        contexto no le dice nada al comprador."""
        marco = getattr(self, "_marco_progreso_compras", None)
        sep = getattr(self, "_sep_progreso_compras", None)
        if marco is None:
            return
        try:
            if not marco.winfo_manager():
                if sep is not None and sep.winfo_manager():
                    marco.pack(fill="x", pady=(8, 0), before=sep)
                else:
                    marco.pack(fill="x", pady=(8, 0))
        except tk.TclError:
            # bloque de progreso de compras ya destruido: no hay nada que mostrar.
            pass

    def _ocultar_progreso_compras(self) -> None:
        marco = getattr(self, "_marco_progreso_compras", None)
        if marco is None:
            return
        try:
            if marco.winfo_manager():
                marco.pack_forget()
        except tk.TclError:
            # bloque de progreso de compras ya destruido: no hay nada que ocultar.
            pass

    def _cambiar_btn_compras(self, habilitado: bool) -> None:
        """Habilita/deshabilita el botón que dispara el envío a compras, que
        ahora es el botón de navegación del pie ("Continuar  →" en el paso 4).
        Con seguridad: el pie puede no existir todavía durante `__init__`, y el
        usuario puede haber vuelto a "Revisar" mientras un envío anterior sigue
        corriendo en segundo plano."""
        btn = getattr(self, "btn_siguiente", None)
        if btn is None:
            return
        try:
            btn.state(["!disabled"] if habilitado else ["disabled"])
        except tk.TclError:
            # boton de pie ya destruido (cambio de paso): no hay nada que habilitar.
            pass

    def _deshabilitar_btn_compras(self) -> None:
        self._cambiar_btn_compras(False)

    def _lote_ya_enviado(self) -> bool:
        """¿Este lote ya se mandó a calificar? La constancia la deja
        `cand_fin` en la carpeta del lote (`enviado_en`)."""
        if not self._salida:
            return False
        return bool(self._leer_marca_lote(self._salida).get("enviado_en"))

    def _sincronizar_btn_compras(self) -> None:
        """Devuelve al botón de pie su rótulo de paso 4 ("Continuar  →").
        Ya no cambia de texto según el lote: el usuario pidió un único botón
        con el mismo nombre que en el resto del asistente, y quién manda el
        lote o abre los candidatos lo decide `_accion_boton_compras`. Se sigue
        llamando después de cada envío para reponer el rótulo si algo lo
        cambió mientras el envío corría ("Enviando…")."""
        btn = getattr(self, "btn_siguiente", None)
        if btn is None:
            return
        try:
            btn.configure(text=self.TEXTO_SIGUIENTE.get(4, "Continuar  →"))
        except tk.TclError:
            # boton de pie ya destruido: el rotulo se vuelve a fijar al rearmar el paso.
            pass

    def _accion_boton_compras(self) -> None:
        """La acción del botón de pie en el paso 4 ("Confirmar y enviar"). Si el
        lote ya se envió, abre la vista de candidatos sin reprocesar nada; si
        no, avanza al paso 5 ("Vectorizar y comparar"), que es donde se elige
        el método y se dispara el cálculo real."""
        if self._lote_ya_enviado():
            # FASE 1 multi-proveedor: "ya enviado" es del LOTE, así que sin
            # esto el segundo proveedor nunca se podría mandar (el botón solo
            # abriría los candidatos del primero).
            pendiente = self._indice_proveedor_pendiente("enviado")
            if pendiente is not None and self._lote_multiproveedor():
                slot = self._proveedores_del_lote[pendiente]
                if messagebox.askyesno(
                        "Proveedor pendiente de calificar",
                        f"Este lote tiene el catálogo de {slot.get('proveedor_nombre')} "
                        "todavía sin enviar a calificar.\n\n"
                        "¿Enviarlo ahora? (Elegí «No» para ver los candidatos "
                        "ya calificados.)"):
                    if self._activar_slot(pendiente):
                        self._ir_a_paso(5)
                        return
            self._ver_candidatos_calificados()
            return
        self._ir_a_paso(5)

    def _enviar_a_asistente_compras(self) -> None:
        """Exporta los APROBADOS de este lote (mismo criterio que "Exportar
        aprobados...") a una carpeta aparte y automática, y esa es la que se
        manda a calificar -- NUNCA la carpeta de trabajo (`self._salida`)
        directo.

        Bug real corregido acá (H1 de la auditoría): antes se mandaba
        `self._salida`, que tiene la subcarpeta `blanco/`, pero el motor de
        candidatos busca `fondo_blanco/` (el nombre que usa la exportación,
        `decisiones.FORMATOS`) -- no coinciden, así que fallaba con
        `SystemExit` dentro del hilo, que no se atrapa, y el botón se quedaba
        trabado en "Enviando..." para siempre. Exportar primero, siempre a
        una carpeta fija fuera de `self._salida` (nunca se pregunta, para no
        repetir la pregunta de "Exportar aprobados..."), también evita que se
        envíen a calificar fotos descartadas o pendientes de revisión (H2):
        `exportar_aprobados` solo copia lo marcado `decisiones.APROBADO`.

        Import perezoso de `motor_candidatos` (y de todo lo que carga --
        torch, transformers, rembg) para no ralentizar el arranque normal de
        Limpieza de Imagenes cuando esta pantalla no se usa."""
        salida = self.v_salida.get().strip()
        if not salida or not Path(salida).is_dir():
            messagebox.showwarning("Asistente de Compras",
                                   "Elegí y procesá primero una carpeta de salida.")
            return
        if not self._recortes:
            messagebox.showwarning("Asistente de Compras",
                                   "No hay un lote cargado en la pantalla.")
            return
        # El proveedor se elige en el PASO 1 (barra superior). Se comprueba
        # ANTES de exportar: avisar después de copiar decenas de archivos
        # sería trabajo tirado a la basura.
        if self._proveedor_id_actual is None:
            # Último recurso antes de rechazar: puede estar anotado en el lote
            # (elegido en el paso 1 de esta corrida o de una anterior) y solo
            # faltar en memoria — por ejemplo si la carpeta activa se cargó por
            # un camino que no pasa por `_cargar_salida_existente`.
            self._restaurar_proveedor_del_lote(Path(salida))
        if self._proveedor_id_actual is None:
            # En vez de solo avisar y obligar al usuario a ir a buscar el botón
            # en la barra de arriba, se resuelve ACÁ MISMO: se abre el mismo
            # diálogo de elegir proveedor, y si elige uno, el envío continúa
            # sin que el usuario tenga que volver a tocar "Continuar".
            if not messagebox.askyesno(
                    "Falta el proveedor",
                    "Todavía no se eligió de qué proveedor es este catálogo.\n\n"
                    "¿Querés elegirlo ahora para continuar con el envío?"):
                return
            self._elegir_proveedor_inicial()
            if self._proveedor_id_actual is None:
                return  # canceló el diálogo de elegir proveedor

        # Punto de control del CANAL DE VENTA: sin esto, un lote que llegó acá
        # por un camino que no pasa por el paso 1 se calificaría contra el
        # catálogo genérico por default, en silencio -- justo la decisión que
        # el dueño quiere explícita. Se pregunta una vez y queda en el lote.
        if not self._asegurar_canal_venta_envio(Path(salida)):
            return

        # El método de vectorización ya se eligió en el paso 5 ("Vectorizar y
        # comparar"), que es el que dispara esta función: acá no se pregunta
        # nada. Antes había un diálogo emergente (`_preguntar_metodo_
        # vectorizacion`, eliminado 2026-09-16 por pedido del dueño): un popup
        # escondía una decisión que vale su propia pantalla, y no tenía dónde
        # mostrar el progreso real del cálculo que venía después.

        # FASE 2 multi-proveedor: el aviso "esto va a borrar al otro proveedor"
        # que la Fase 1 dejaba acá se eliminó -- ya no es cierto. Enviar el
        # segundo proveedor del lote no toca nada del primero (ver
        # `_confirmar_reemplazo_catalogo` y `_vaciar_revision`). El único aviso
        # que queda es el de más abajo, y es sobre los propios datos anteriores
        # del proveedor que se está enviando.

        destino = Path(salida).parent / f"{Path(salida).name}_para_asistente_compras"
        # Con varios proveedores en la misma carpeta de trabajo, solo se
        # exportan los recortes DE ESTE proveedor: si no, las fotos del primer
        # catálogo entrarían a `candidato_raw` con el `proveedor_id` del
        # segundo. Con un solo proveedor esto es `set(self._recortes)`, igual
        # que antes.
        nombres_envio = self._recortes_del_proveedor_activo()
        if not nombres_envio:
            messagebox.showwarning(
                "Asistente de Compras",
                "No se pudo identificar qué recortes de esta carpeta son de "
                f"{self._proveedor_nombre_actual}.\n\nRevisá el origen de las "
                "fotos de ese proveedor en el paso 1 antes de enviar (no se "
                "envía nada para no cargar fotos con el proveedor equivocado).")
            return
        try:
            res_export = decisiones.exportar_aprobados(
                Path(salida), destino,
                nombres=nombres_envio,
                formatos=("blanco",),  # el motor de candidatos solo necesita fondo_blanco
                entrada=self.v_entrada.get().strip() or None)
        except decisiones.ErrorExportacion as exc:
            messagebox.showerror("Asistente de Compras: exportación", str(exc))
            return
        except OSError as exc:
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo preparar la carpeta a enviar:\n{exc}")
            return

        if res_export["aprobados"] == 0:
            messagebox.showwarning(
                "Asistente de Compras",
                "Ningún recorte de este lote está marcado como Aprobado todavía "
                "-- no hay nada que enviar a calificar.")
            return

        try:
            import motor_candidatos
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Asistente de Compras",
                                 f"No se pudo cargar el módulo de calificación:\n{exc}")
            return

        proveedor_id = self._proveedor_id_actual
        modelo_activo = motor_candidatos.modelo_activo_configurado()

        # FASE 5 multi-proveedor: qué archivos de la carpeta de exportados son
        # de ESTE envío. La carpeta es una sola para el lote y
        # `exportar_aprobados` no la vacía, así que las fotos del proveedor
        # anterior siguen ahí; sin esta lista, el motor las volvía a ingestar a
        # nombre del proveedor que se está enviando ahora (bug real medido en
        # la verificación de punta a punta de la Fase 5). En un lote de un solo
        # proveedor se pasa None y el camino queda idéntico al de antes.
        archivos_envio = (set(nombres_envio) if self._lote_multiproveedor()
                          else None)

        reemplazar = self._confirmar_reemplazo_catalogo(destino.name)

        # Aviso residual (FASE 2): el único caso que todavía reemplaza datos
        # ya calificados es "el MISMO proveedor ya había enviado un catálogo
        # con otro origen". Eso sigue siendo válido (reenviar con el archivo
        # corregido) pero conviene decirlo, y solo tiene sentido preguntarlo
        # cuando el lote tiene varios proveedores: en un lote de un solo
        # proveedor el comportamiento queda idéntico al de antes de esta fase.
        if reemplazar == "proveedor" and self._lote_multiproveedor():
            if not messagebox.askyesno(
                    "Reemplazar el envío anterior de este proveedor",
                    f"{self._proveedor_nombre_actual} ya tiene un catálogo "
                    "enviado en este lote, con otro origen.\n\n"
                    "Enviar ahora REEMPLAZA sus datos anteriores (los de los "
                    "demás proveedores del lote no se tocan).\n\n¿Continuar?",
                    icon="warning", default="no"):
                return

        self._enviando_compras = True
        self._deshabilitar_btn_compras()
        self._mostrar_progreso_compras()
        self._set_estado_compras(
            f"Enviando {res_export['aprobados']} aprobado(s) al Asistente de Compras "
            f"(método: {modelo_activo})…")
        self._avance_compras(0)

        def avisar(msg: str) -> None:
            self._q.put(("cand_linea", msg))

        def avanzar(hechos: int, total: int) -> None:
            self._q.put(("cand_avance", (hechos, total)))

        def trabajar() -> None:
            try:
                res = motor_candidatos.cargar_carpeta_limpia(
                    destino, proveedor_id, catalogo_origen=destino.name,
                    on_progreso=avisar, on_avance=avanzar,
                    modelo_activo=modelo_activo, reemplazar_catalogo=reemplazar,
                    solo_archivos=archivos_envio)
                self._q.put(("cand_fin", res.candidatos))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("cand_error", str(exc)))

        # Reloj de arena durante TODO el envío: corre en otro hilo (por eso no
        # se usa el `reloj(...)` de contexto, que terminaría al instante) y se
        # apaga en los manejadores de `cand_fin` / `cand_error`.
        self.configure(cursor="watch")
        threading.Thread(target=trabajar, daemon=True).start()

    # ── paso 5: vectorizar y comparar ────────────────────────────────────
    #
    # Reemplaza al diálogo emergente `_preguntar_metodo_vectorizacion`
    # (2026-09-16, pedido del dueño). Dos razones concretas, las dos reales:
    #   - la decisión del método NO se puede mezclar entre catálogos, así que
    #     merece una pantalla donde se lea qué es cada opción, no un popup que
    #     se contesta de memoria; y
    #   - el trabajo que viene después (vectorizar cada candidato + similitud
    #     de coseno contra el catálogo) son MINUTOS. El popup se cerraba y el
    #     progreso quedaba escondido en una barra de otra pantalla.
    #
    # El progreso NO se reinventa: `motor_calificacion.vectorizar_y_puntuar_
    # candidatos` ya reporta por `_reportar_progreso` (mensaje + sub/sub_total),
    # `motor_candidatos.cargar_carpeta_limpia` lo poletea y lo entrega por
    # `on_progreso`/`on_avance`, y esos van a la cola como `cand_linea`/
    # `cand_avance`. Esta pantalla solo REUSA los mismos nombres de widget que
    # ya leen esos manejadores (`v_estado_compras`, `barra_compras`,
    # `_marco_progreso_compras`), así que no hace falta tocar el plomería.

    # El orden de las opciones lo fija `MODELOS_ACTIVOS_VALIDOS`
    # (motor_candidatos); este dict solo les pone nombre legible.
    ETIQUETA_METODOS = {
        "ti": ("TI (recomendado)",
               "dinov2_ti — el espacio real del equipo de TI"),
        "fashion": ("Fashion-SigLIP",
                    "solo fashion_siglip, sin fusionar — mira estilo/moda. "
                    "Devuelve pocos comparables, pero los que devuelve son "
                    "los más parejos en color y material"),
        "dino": ("DINOv2",
                 "solo dino_v2, sin fusionar — mira forma y estructura. "
                 "Es el que más candidatos alcanza a comparar; puede proponer "
                 "el mismo molde en otro color"),
        "estandar": ("Estándar (fusión)",
                     "fashion_siglip + dino_v2 promediados 50/50. Medido con "
                     "un lote real, rinde PEOR que cada uno por separado: el "
                     "promedio de una señal fuerte y una débil queda debajo "
                     "del umbral más seguido que cualquiera de las dos"),
    }

    def _mostrar_pantalla_vectorizacion(self) -> None:
        """Paso 5: elegir el método de vectorización y calcular.

        Arriba el selector (TI preseleccionado), un botón «Calcular y
        continuar» que dispara el cálculo REAL, y debajo la barra de progreso
        con el texto de estado ("Candidato 4/18…", "Categorizando y puntuando
        el catálogo…") más la consola colapsable de detalle técnico — el mismo
        patrón visual del paso 3."""
        import motor_candidatos

        if not self._salida or not self._recortes:
            messagebox.showinfo(
                "Nada que vectorizar",
                "No hay un lote cargado en la pantalla. Volvé al paso 1 y "
                "elegí un origen de fotos.")
            self._ir_a_paso(1)
            return

        self.configure(cursor="watch")
        self.after(300, lambda: self.configure(cursor=""))
        self._asegurar_cuerpo_visible()
        self._ocultar_pantallas_base()
        marco = Tarjeta(self)
        marco.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))
        self._vector_frame = marco
        self._actualizar_paso(5)

        interior = Marco(marco, padding=16)
        interior.pack(fill="both", expand=True)

        # Sin titulazo propio: en este paso la cabecera grande NO se compacta
        # (no hay grilla que necesite el alto), así que "Vectorizar y
        # comparar" ya está escrito arriba — repetirlo dos veces en 150px se
        # veía como un error.
        Etiqueta(interior,
                 text="Con qué espacio de vectores se van a buscar los comparables de "
                      "este catálogo. No se pueden mezclar entre catálogos, así que esta "
                      "decisión vale para el lote entero.",
                 style="SuavePanel.TLabel", justify="left", wraplength=1100).pack(
            anchor="w", fill="x", pady=(4, 12))

        # Queda marcado el método que la instalación tenga persistido, y TI
        # (el recomendado) cuando no hay ninguno o el guardado ya no es válido.
        # El comentario anterior decía "siempre TI aunque haya otra cosa
        # persistida", que NO es lo que hace el código de abajo -- se corrige el
        # comentario, no el comportamiento: pisar la elección guardada del
        # comprador en cada entrada al paso 5 sería peor.
        actual = motor_candidatos.modelo_activo_configurado()
        self._metodo_vector_previo = actual
        inicial = actual if actual in motor_candidatos.MODELOS_ACTIVOS_VALIDOS else "ti"
        self.v_metodo_vector = tk.StringVar(value=inicial)

        # Caja con fondo propio para que el selector se lea como UN bloque de
        # decisión (es lo único que el comprador tiene que elegir acá).
        caja_metodos = Marco(interior, padding=12, fg_color=PAR_PANEL_SUAVE,
                             corner_radius=RADIO_PANEL)
        caja_metodos.pack(fill="x")
        for valor in motor_candidatos.MODELOS_ACTIVOS_VALIDOS:
            titulo, detalle = self.ETIQUETA_METODOS.get(valor, (valor, ""))
            ctk.CTkRadioButton(caja_metodos, text=titulo,
                               variable=self.v_metodo_vector, value=valor,
                               font=F["base"]).pack(anchor="w", pady=(2, 0))
            if detalle:
                # `wraplength`: los detalles de los métodos nuevos son de dos
                # líneas -- sin esto el texto estira la caja a lo ancho.
                Etiqueta(caja_metodos, text=detalle, style="SuavePanel.TLabel",
                         justify="left", wraplength=1000).pack(
                    anchor="w", padx=(26, 0), pady=(0, 6))

        fila_accion = Marco(interior)
        fila_accion.pack(fill="x", pady=(12, 0))
        self.btn_calcular_vector = Boton(
            fila_accion, text="▶ Calcular y continuar", style="Primario.TButton",
            height=44, width=230, command=self._calcular_vectorizacion_y_continuar)
        self.btn_calcular_vector.pack(side="left")
        Etiqueta(fila_accion,
                 text="Calcula el vector de cada candidato y su similitud de coseno "
                      "contra el catálogo. Puede tardar varios minutos.",
                 style="SuavePanel.TLabel", wraplength=800).pack(
            side="left", padx=(12, 0), fill="x", expand=True)

        Separador(interior).pack(fill="x", pady=(14, 12))

        # Bloque de progreso: MISMOS nombres de atributo que usaba la tarjeta
        # de "Confirmar y enviar", porque `_set_estado_compras`/
        # `_avance_compras`/`_mostrar_progreso_compras` ya escriben ahí. El
        # bloque no se empaca hasta que hay un cálculo en curso: una barra
        # vacía no le dice nada al comprador.
        self._marco_progreso_compras = Marco(interior)
        Etiqueta(self._marco_progreso_compras,
                 text="Progreso del cálculo", style="Panel.TLabel").pack(anchor="w")
        self.v_estado_compras = tk.StringVar(value="")
        Etiqueta(self._marco_progreso_compras, textvariable=self.v_estado_compras,
                 style="SuavePanel.TLabel", wraplength=1100).pack(
            anchor="w", fill="x", pady=(2, 0))
        self.barra_compras = Barra(self._marco_progreso_compras,
                                   mode="determinate", maximum=100)
        self.barra_compras.pack(fill="x", pady=(4, 0))

        self._sep_progreso_compras = Separador(interior)
        self._sep_progreso_compras.pack(fill="x", pady=(12, 12))

        # Consola colapsable propia de esta pantalla (no la del monitor de
        # láminas: son dos trabajos distintos y mezclar sus logs confunde).
        self._armar_consola_vector(interior)

        # Si se vuelve a entrar al paso mientras un cálculo sigue corriendo en
        # segundo plano, el bloque tiene que reaparecer y el botón quedar
        # apagado.
        if getattr(self, "_enviando_compras", False):
            self._mostrar_progreso_compras()
            self._bloquear_btn_calcular(True)

    def _armar_consola_vector(self, padre) -> None:
        """Log crudo del cálculo, colapsado por defecto — mismo patrón
        «▸ Ver detalle técnico» del monitor de limpieza."""
        self._consola_vector_abierta = False
        fila_toggle = Marco(padre)
        fila_toggle.pack(fill="x")
        self.btn_consola_vector = Boton(fila_toggle, text="▸ Ver detalle técnico",
                                        style="Sutil.TButton", width=200, height=30,
                                        command=self._alternar_consola_vector)
        self.btn_consola_vector.pack(side="left")

        # `tk.Frame` por lo mismo que `_zona_consola`: colapsada no debe
        # reservar los 200px de un CTkFrame vacío.
        self._zona_consola_vector = tk.Frame(padre, background=COLOR_PANEL)
        self._zona_consola_vector.pack(fill="both", expand=True)
        self.txt_consola_vector = ctk.CTkTextbox(
            self._zona_consola_vector, height=170, corner_radius=RADIO_CONTROL,
            fg_color=("#12161c", "#0c0f13"), text_color=("#d7dde5", "#d7dde5"),
            border_color=PAR_BORDE, border_width=1,
            scrollbar_button_color=PAR_BORDE,
            scrollbar_button_hover_color=PAR_TEXTO_SUAVE,
            font=("Consolas", 11), activate_scrollbars=True, wrap="none")
        self.txt_consola_vector.configure(state="disabled")
        # No se empaqueta acá: arranca colapsada.

    def _alternar_consola_vector(self) -> None:
        self._consola_vector_abierta = not getattr(self, "_consola_vector_abierta", False)
        if self._consola_vector_abierta:
            if not self._zona_consola_vector.winfo_manager():
                self._zona_consola_vector.pack(fill="both", expand=True)
            self.txt_consola_vector.pack(fill="both", expand=True, pady=(6, 0))
            self.btn_consola_vector.configure(text="▾ Ocultar detalle técnico")
        else:
            self.txt_consola_vector.pack_forget()
            self._zona_consola_vector.pack_forget()   # ver `_alternar_consola`
            self.btn_consola_vector.configure(text="▸ Ver detalle técnico")

    def _log_consola_vector(self, texto: str) -> None:
        """Agrega una línea al log del paso 5, si esa pantalla está abierta.
        Silencioso si no lo está: el cálculo puede seguir corriendo en segundo
        plano después de que el usuario navegó a otra parte."""
        caja = getattr(self, "txt_consola_vector", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.insert("end", texto.rstrip("\r\n") + "\n")
            self._lineas_consola_vector = getattr(self, "_lineas_consola_vector", 0) + 1
            if self._lineas_consola_vector > self._MAX_LINEAS_CONSOLA:
                sobran = self._lineas_consola_vector - self._MAX_LINEAS_CONSOLA
                caja.delete("1.0", f"{sobran + 1}.0")
                self._lineas_consola_vector = self._MAX_LINEAS_CONSOLA
            caja.see("end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            # consola del paso de vectorizacion ya destruida: la linea no se ve.
            pass

    def _bloquear_btn_calcular(self, bloqueado: bool) -> None:
        btn = getattr(self, "btn_calcular_vector", None)
        if btn is None:
            return
        try:
            btn.state(["disabled"] if bloqueado else ["!disabled"])
        except tk.TclError:
            # boton "calcular vectorizacion" ya destruido: nada que bloquear.
            pass

    def _calcular_vectorizacion_y_continuar(self) -> None:
        """Guarda el método elegido y dispara el cálculo real.

        El cálculo es el mismo de siempre (`_enviar_a_asistente_compras` →
        `motor_candidatos.cargar_carpeta_limpia` →
        `vectorizar_y_puntuar_candidatos`), corriendo en su hilo y reportando
        por la cola; al terminar, `cand_fin` abre solo el paso 6 ("Candidatos
        calificados")."""
        import motor_candidatos

        if getattr(self, "_enviando_compras", False):
            messagebox.showinfo(
                "Ya está calculando",
                "El cálculo de vectores y similitud ya está corriendo. "
                "Al terminar se abren los candidatos calificados solos.")
            return

        elegido = getattr(self, "v_metodo_vector", None)
        elegido = elegido.get() if elegido is not None else "ti"
        previo = getattr(self, "_metodo_vector_previo", None)
        if elegido != previo:
            # Se persiste solo si cambió: `establecer_modelo_activo` escribe el
            # config de la instalación, y el resto del cálculo lee de ahí
            # (`modelo_activo_configurado`).
            try:
                motor_candidatos.establecer_modelo_activo(elegido)
                self._metodo_vector_previo = elegido
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Método de vectorización",
                                     f"No se pudo guardar el método:\n{exc}")
                return

        self._limpiar_consola_vector()
        self._bloquear_btn_calcular(True)
        self._set_estado_compras("Preparando el cálculo…")
        self._mostrar_progreso_compras()
        self._avance_compras(0)
        # `_enviar_a_asistente_compras` valida (proveedor, canal, aprobados),
        # exporta y lanza el hilo. Si rechaza algo y no arranca nada, el botón
        # tiene que volver a estar disponible.
        self._enviar_a_asistente_compras()
        if not getattr(self, "_enviando_compras", False):
            self._bloquear_btn_calcular(False)
            self._ocultar_progreso_compras()

    def _limpiar_consola_vector(self) -> None:
        caja = getattr(self, "txt_consola_vector", None)
        if caja is None:
            return
        try:
            caja.configure(state="normal")
            caja.delete("1.0", "end")
            caja.configure(state="disabled")
        except Exception:  # noqa: BLE001
            # consola de vectorizacion ya destruida: nada que limpiar.
            pass
        self._lineas_consola_vector = 0

    def _confirmar_reemplazo_catalogo(self, catalogo_origen: str) -> bool | str:
        """¿Hay que vaciar la revisión DE ESTE LOTE antes de este envío?

        Decide sola, sin preguntarle nada al usuario.

        FASE 4 -- qué ya NO decide esto. Cuando la revisión vivía en
        `staging_tuc` (Postgres compartido con la herramienta web), esta
        función era el guardián de la regla "un solo catálogo activo a la
        vez": lo que encontraba cargado podía ser el catálogo de OTRO lote, y
        había que truncar la tabla global para que no se mezclaran (así se
        mezclaron 20 Converse de prueba con 54 Reebok reales el 2026-09-07).
        Esa regla murió con la Fase 2: cada lote tiene su propio
        `lote.sqlite`, `resumen_catalogo_activo()` solo puede devolver lo de
        ESTE lote, y abrir el catálogo de otro proveedor en otro lote no toca
        nada de acá.

        Lo que SÍ sigue decidiendo, y por eso la función no se borró: dentro
        de un mismo lote todavía hay dos envíos distintos posibles.
          - Revisión vacía -> nada que vaciar (True, camino limpio).
          - Lo que hay es EXACTAMENTE este proveedor + este catálogo -> es un
            reenvío del mismo lote (corregir 1-2 fotos): se agrega/actualiza
            por sha1 sin perder el resto (False).
          - Proveedor distinto al de las filas ya cargadas -> el comprador
            corrigió a mano de quién es este lote (`_elegir_proveedor_inicial`
            reescribe la marca del lote). Las filas viejas quedaron a nombre
            del proveedor equivocado y hay que vaciarlas (True).

        Ya no devuelve None: ese valor existía para "el usuario canceló el
        diálogo", y el diálogo modal "¿Agregar o empezar nuevo?" se eliminó el
        2026-09-09 a pedido del dueño ("no quiero que me pregunte eso, cada
        lote es independiente").

        FASE 2 multi-proveedor (2026-09-16) -- la decisión pasó de ser "todo o
        nada del lote" a ser POR PROVEEDOR. Antes bastaba con que las filas
        cargadas fueran de otro proveedor para devolver "vaciar", y ese vaciado
        no tenía `WHERE`: enviar el segundo proveedor del lote borraba la
        calificación ya hecha del primero (bloqueante confirmado en la Fase 1).
        Ahora devuelve:
          - False -> no se vacía nada (reenvío del mismo proveedor+origen, o
            solo hay filas de OTROS proveedores, que deben convivir).
          - "proveedor" -> este MISMO proveedor ya tiene filas en el lote con
            otro `catalogo_origen`: se reemplaza lo suyo y nada más.
          - True -> vaciado total del lote. Queda solo para el caso de un lote
            de un único proveedor cuyas filas quedaron a nombre del proveedor
            equivocado (el comprador corrigió a mano de quién es el lote), y
            para el lote todavía vacío."""
        import motor_candidatos
        resumen = motor_candidatos.resumen_catalogo_activo()
        if not resumen:
            return True
        pid = self._proveedor_id_actual
        mias = [f for f in resumen
                if f.get("proveedor_id") == pid
                or f["proveedor"] == self._proveedor_nombre_actual]
        if mias:
            # ¿Ya hay datos de ESTE proveedor con un origen distinto al que se
            # está por enviar? Ahí sí hay que reemplazar lo suyo.
            if any(f["catalogo_origen"] != catalogo_origen for f in mias):
                return "proveedor"
            return False
        # No hay nada de este proveedor. Si el lote es multi-proveedor, lo que
        # hay es legítimo de otro proveedor y debe quedar intacto.
        if self._lote_multiproveedor():
            return False
        return True

    def _elegir_proveedor(self, proveedores: list[tuple[int, str]]) -> tuple[int, str] | None:
        """Diálogo simple de selección -- lista real de `silver.dim_tuc_proveedor`,
        no un número de memoria. Devuelve (id, nombre).

        El método de vectorización YA NO se elige acá (plan 2026-09-07, paso
        8): es una configuración de la instalación, no una decisión por
        catálogo, y vive en ⚙ Ajustes (`_abrir_ajustes`).

        Permite agregar un proveedor nuevo si no está en la lista: opción
        "OTROS - Especificar nuevo" al final."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Elegí el proveedor")
        _centrar_en_ventana_principal(self, ventana, 360, 480)
        Etiqueta(ventana, text="¿De qué proveedor es este catálogo?",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 6))
        lista = tk.Listbox(ventana, activestyle="none")
        lista.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # Agregar proveedores existentes
        for _pid, nombre in proveedores:
            lista.insert("end", nombre)

        # Agregar opción para nuevo proveedor
        lista.insert("end", "— OTROS — Especificar nuevo")

        resultado: dict = {"id": None, "nombre": None}

        def confirmar() -> None:
            sel = lista.curselection()
            if sel:
                idx = sel[0]
                # Verificar si se seleccionó la opción "OTROS"
                if idx == len(proveedores):  # La opción OTROS está al final
                    # Pedir nombre del nuevo proveedor
                    nuevo_proveedor = self._solicitar_nuevo_proveedor()
                    if nuevo_proveedor is not None:
                        resultado["id"], resultado["nombre"] = nuevo_proveedor
                else:
                    resultado["id"] = proveedores[idx][0]
                    resultado["nombre"] = proveedores[idx][1]
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Cancelar", style="Sutil.TButton",
             command=ventana.destroy).pack(side="right")
        Boton(fila_botones, text="Confirmar", style="Primario.TButton",
             command=confirmar).pack(side="right", padx=(0, 8))
        lista.bind("<Double-Button-1>", lambda _e: confirmar())

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)
        if resultado["id"] is None:
            return None
        return resultado["id"], resultado["nombre"]

    def _solicitar_nuevo_proveedor(self) -> tuple[int, str] | None:
        """Diálogo para que el usuario especifique un nuevo proveedor. Inserta
        en la base de datos y retorna (proveedor_id, nombre).

        Si el usuario cancela o hay error, retorna None."""
        ventana = ctk.CTkToplevel(self)
        ventana.title("Nuevo proveedor")
        _centrar_en_ventana_principal(self, ventana, 380, 180)
        Etiqueta(ventana, text="Nombre del nuevo proveedor",
                style="Subtitulo.TLabel").pack(anchor="w", padx=14, pady=(14, 4))
        Etiqueta(ventana, text="Escribí el nombre exacto del proveedor tal como lo vas a usar.",
                style="Suave.TLabel", wraplength=350).pack(anchor="w", padx=14, pady=(0, 10))

        entry_nombre = ctk.CTkEntry(ventana, corner_radius=RADIO_CONTROL, height=40,
                                    font=F["base"], fg_color=PAR_PANEL_SUAVE,
                                    border_color=PAR_BORDE, text_color=PAR_TEXTO,
                                    placeholder_text="Ej: Nike, Adidas, etc.")
        entry_nombre.pack(fill="x", padx=14, pady=(0, 14))
        entry_nombre.focus()

        resultado: dict = {"id": None, "nombre": None, "cancelado": False}

        def guardar() -> None:
            nombre = entry_nombre.get().strip()
            if not nombre:
                messagebox.showwarning("Nuevo proveedor", "Por favor escribí el nombre del proveedor.")
                return

            # Intentar insertar en la base de datos
            try:
                import motor_calificacion  # FASE 1: antes era `servidor_pty` (GestionTUC);
                # misma función `conn()`, copiada letra por letra en la copia propia.
                c = motor_calificacion.conn()
                cur = c.cursor()

                # Insertar el nuevo proveedor
                cur.execute(
                    "INSERT INTO silver.dim_tuc_proveedor (nombre) VALUES (%s) RETURNING proveedor_id",
                    (nombre,)
                )
                proveedor_id = cur.fetchone()[0]
                c.commit()
                c.close()

                resultado["id"] = proveedor_id
                resultado["nombre"] = nombre
                ventana.destroy()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    "Error al guardar",
                    f"No se pudo agregar el proveedor a la base de datos:\n{exc}")

        def cancelar() -> None:
            resultado["cancelado"] = True
            ventana.destroy()

        fila_botones = Marco(ventana)
        fila_botones.pack(fill="x", padx=14, pady=(0, 14))
        Boton(fila_botones, text="Cancelar", style="Sutil.TButton",
             command=cancelar).pack(side="right")
        Boton(fila_botones, text="Guardar", style="Primario.TButton",
             command=guardar).pack(side="right", padx=(0, 8))

        entry_nombre.bind("<Return>", lambda _e: guardar())

        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)

        if resultado["cancelado"] or resultado["id"] is None:
            return None
        return resultado["id"], resultado["nombre"]

    def _bombear_cola(self) -> None:
        try:
            while True:
                tipo, dato = self._q.get_nowait()
                # Cualquier mensaje de cualquier worker cuenta como señal de
                # vida: es contra esta marca que se mide el "parece colgado".
                self._ultima_actividad = time.monotonic()
                if tipo == "linea":
                    # El texto crudo se guarda ANTES de interpretarlo: si el
                    # formato cambia o llega algo inesperado, en el log queda
                    # igual (es justo el caso en que sirve de verdad).
                    self._log_consola(dato)
                    hechas_antes = getattr(self, "_hechas_proc", 0)
                    self._procesar_linea(dato)
                    if getattr(self, "_hechas_proc", 0) > hechas_antes:
                        # Se terminó una lámina: se anota CUÁNDO, que es lo
                        # que alimenta el promedio real de segundos/lámina.
                        self._marcas_lamina.append(time.monotonic())
                    self._refrescar_eta()
                elif tipo == "cand_linea":
                    self._set_estado_compras(dato)
                    # …y al detalle técnico del paso 5, que es donde el
                    # comprador puede mirar el log crudo del cálculo si algo
                    # parece colgado (silencioso si esa pantalla no está).
                    self._log_consola_vector(dato)
                elif tipo == "cand_avance":
                    hechos, total = dato
                    pct = (hechos / total * 100) if total else 0
                    # El avance del envío a calificar va en SU propia barra, la
                    # de la tarjeta "Confirmar y enviar": el monitor de láminas
                    # ya no se muestra en ese paso (y su bloque "Lámina actual"
                    # no aplica a candidatos).
                    self._avance_compras(pct)
                elif tipo == "cand_fin":
                    self.configure(cursor="")
                    # Constancia de que ESTE lote ya se envió: es lo que
                    # después deja que al retomar la carpeta la app salte
                    # directo al paso 4 sin reprocesar ni reenviar nada.
                    if self._salida:
                        self._guardar_marca_lote(
                            catalogo_origen=f"{Path(self._salida).name}_para_asistente_compras",
                            enviado_en=datetime.now().isoformat(timespec="seconds"),
                            enviados=len(dato))
                    # FASE 1 multi-proveedor: queda anotado QUÉ proveedor del
                    # lote ya se envió, para poder ofrecer el siguiente (y para
                    # que el aviso de la Fase 2 sepa a quién se le borraría la
                    # calificación).
                    if self._proveedores_del_lote:
                        self._slot_actual()["enviado"] = True
                        self._persistir_proveedores_del_lote()
                        self._refrescar_resumen_proveedores()
                    self._cambiar_btn_compras(True)
                    self._sincronizar_btn_compras()
                    self._set_estado_compras(f"Listo -- {len(dato)} candidato(s) calificado(s).")
                    self._avance_compras(100)
                    self._enviando_compras = False
                    self._ocultar_progreso_compras()
                    # FASE 5: la pantalla 6 se abre con los candidatos de TODO
                    # el lote, no solo con los de este envío. `dato` es lo que
                    # devolvió `cargar_carpeta_limpia`, que está filtrado al
                    # proveedor que se acaba de enviar -- abrir con eso dejaba
                    # al proveedor anterior fuera de la pantalla (y con él, la
                    # pastilla de proveedor, el filtro y el desglose del paso
                    # 7). Con un solo proveedor las dos listas son la misma;
                    # si por lo que sea no se puede identificar el lote, se
                    # cae a `dato`, que es el comportamiento anterior.
                    try:
                        del_lote = self._candidatos_del_lote()
                    except Exception:  # noqa: BLE001
                        _logger_gui().exception("no se pudieron releer los candidatos del lote tras el envio")
                        del_lote = None
                    self._mostrar_vista_candidatos(del_lote or dato)
                elif tipo == "cand_error":
                    self.configure(cursor="")
                    self._cambiar_btn_compras(True)
                    self._enviando_compras = False
                    self._ocultar_progreso_compras()
                    self._set_estado_compras("Listo")
                    messagebox.showerror("Asistente de Compras", dato)
                elif tipo == "fin":
                    # Con varios procesos en paralelo (H4), cada uno manda su
                    # propio "fin" al terminar -- cerrar el lote con el
                    # PRIMERO dejaría a los demás corriendo en segundo plano
                    # mientras la pantalla ya pasó a la revisión, con
                    # recortes apareciendo después de que el usuario ya
                    # decidió sobre lo que vio. Solo se cierra cuando el
                    # ÚLTIMO termina.
                    self._procs_pendientes = max(0, getattr(self, "_procs_pendientes", 1) - 1)
                    if self._procs_pendientes > 0:
                        self._recargar_manifiesto()
                        continue
                    self.btn_procesar.state(["!disabled"])
                    self.btn_detener.state(["disabled"])
                    self.btn_pausar.state(["disabled"])
                    self.btn_pausar.configure(text="⏸ Pausar")
                    self._pausado = False
                    # Terminó el lote: se apaga la marca ANTES de
                    # `_aterrizar_en_revision`, que es lo que hace que el
                    # monitor se retire y el paso 3 quede solo en modo
                    # revisión (la transición automática 3→4 de antes).
                    self._procesando_lote = False
                    self.v_estado_proc.set("Listo")
                    self.v_lamina_actual.set("Listo — sin procesos activos")
                    self.v_paso_actual.set("")
                    self.barra_progreso.configure(value=100)
                    self.v_pct.set("100%")
                    self.barra_progreso_lamina.configure(value=100)
                    self.v_pct_lamina.set("")
                    self.v_eta.set("")
                    self._ocultar_sin_actividad()
                    self._recargar_manifiesto()
                    if self._salida:
                        self.v_carpeta_activa.set(
                            f"Carpeta activa: {self._salida}  ·  {len(self._recortes)} recorte(s) cargados")
                    self._aterrizar_en_revision()
        except queue.Empty:
            # cola vacia: es el caso normal de cada ciclo del bombeo, no un error.
            pass
        self._revisar_sin_actividad()
        self.after(150, self._bombear_cola)

    # ── estimación de tiempo y detección de "parece colgado" ─────────────

    SEGUNDOS_SIN_ACTIVIDAD = 90

    def _reiniciar_medicion_lote(self) -> None:
        """Pone en cero las mediciones de tiempo del lote: se llama al arrancar
        a procesar, si no el promedio arrastraría el lote anterior."""
        self._arranque_lote = time.monotonic()
        self._ultima_actividad = time.monotonic()
        self._marcas_lamina = []
        self.v_eta.set("Tiempo estimado restante: calculando…")
        self._ocultar_sin_actividad()

    def _refrescar_eta(self) -> None:
        marcas = getattr(self, "_marcas_lamina", None) or []
        total = getattr(self, "_total_proc", 0)
        hechas = getattr(self, "_hechas_proc", 0)
        faltan = max(total - hechas, 0)
        if not marcas:
            self.v_eta.set("Tiempo estimado restante: calculando…")
            return
        if faltan == 0:
            self.v_eta.set("Tiempo estimado restante: terminando…")
            return
        arranque = getattr(self, "_arranque_lote", None) or marcas[0]
        seg_por_lamina = (marcas[-1] - arranque) / len(marcas)
        restante = seg_por_lamina * faltan
        if restante < 60:
            self.v_eta.set("Tiempo estimado restante: menos de 1 min")
        else:
            self.v_eta.set(f"Tiempo estimado restante: ~{restante / 60:.0f} min")

    def _ocultar_sin_actividad(self) -> None:
        aviso = getattr(self, "_aviso_sin_actividad", None)
        if aviso is not None and aviso.winfo_ismapped():
            aviso.pack_forget()

    def _revisar_sin_actividad(self) -> None:
        """Avisa si hace rato que ningún proceso dice nada. NO detiene nada:
        cortar el lote sigue siendo decisión del usuario (botón "Detener")."""
        aviso = getattr(self, "_aviso_sin_actividad", None)
        if aviso is None:
            return
        # Solo tiene sentido mientras hay procesos vivos y sin pausa: pausado
        # el silencio es esperado, y sin lote no hay nada que vigilar.
        activo = (any(p.poll() is None for p in getattr(self, "_procs", []))
                  and not getattr(self, "_pausado", False))
        if not activo or getattr(self, "_ultima_actividad", None) is None:
            self._ocultar_sin_actividad()
            return
        quieto = time.monotonic() - self._ultima_actividad
        if quieto < self.SEGUNDOS_SIN_ACTIVIDAD:
            self._ocultar_sin_actividad()
            return
        cuanto = (f"{quieto:.0f} segundos" if quieto < 120
                  else f"{quieto / 60:.0f} minutos")
        self.v_sin_actividad.set(
            f"⚠  Hace {cuanto} que no hay avance. El programa sigue "
            "trabajando, pero puede que se haya trabado con esta foto. "
            "Si sigue igual, podés usar “Detener” — lo ya procesado no se pierde.")
        if not aviso.winfo_ismapped():
            aviso.pack(fill="x", pady=(8, 0))

    def _mostrar_imagen_flotante(self, path: Path, titulo: str) -> None:
        """Abre la foto en una ventana propia, en tamaño real (o achicada
        SOLO si no entra en la pantalla).

        Causa raíz de "el clic en la foto original del resumen del lote no hace
        nada visible" (reportado dos veces): `_mostrar_grande` pinta la imagen
        adentro de `self.lbl_imagen`, que vive en el visor del paso de limpiar
        y revisar (`self._marco_visor`). Cuando la pantalla activa es el
        resumen ("Confirmar y enviar", `self._resumen_frame`, que REEMPLAZA a
        `self.cuerpo`), ese visor sigue existiendo pero está tapado/oculto —
        el clic sí disparaba la función, pero actualizaba una imagen que no
        se veía en ningún lado. Una ventana propia funciona sin importar qué
        pantalla del asistente esté activa."""
        try:
            im = Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ver imagen", f"No se pudo abrir la foto:\n{exc}")
            return

        ventana = ctk.CTkToplevel(self)
        ventana.title(titulo)
        ventana.configure(fg_color=PAR_VISOR_BG)

        # Tamaño real de la foto, pero sin pasarse de lo que entra en la
        # pantalla (con margen para no taparse con la barra de tareas).
        max_ancho = self.winfo_screenwidth() - 120
        max_alto = self.winfo_screenheight() - 160
        mostrar = im.copy()
        mostrar.thumbnail((max(max_ancho, 200), max(max_alto, 200)))
        foto = ImageTk.PhotoImage(mostrar)

        lbl = tk.Label(ventana, image=foto, background=COLOR_VISOR_BG)
        lbl.image = foto  # referencia viva: sin esto Tk la recolecta y la imagen desaparece
        lbl.pack(padx=10, pady=(10, 4))

        etiqueta_tam = (f"{im.width} × {im.height} px"
                        if mostrar.size == im.size else
                        f"{im.width} × {im.height} px (mostrada más chica para entrar en pantalla)")
        Etiqueta(ventana, text=etiqueta_tam, style="Suave.TLabel").pack(pady=(0, 10))

        ventana.transient(self)
        # Causa raíz de "la ventana de la foto se abre y se cierra sola" (paso 5,
        # reportado 2026-09-10): NO se cerraba -- quedaba viva y mapeada, pero
        # DETRÁS de la ventana principal, que abre maximizada y la tapa por
        # completo, así que se veía un instante y desaparecía.
        #
        # `lift()` + `focus_force()` acá no alcanzan porque corren ANTES de que
        # termine de propagarse el mismo clic que abrió la ventana:
        # `CTkToplevel.__init__` instala un `bind_all("<Button-1>", set_focus)`
        # (binding global, en el bindtag "all", que se dispara DESPUÉS del
        # binding de la miniatura), y ese handler hace `focus_set()` sobre el
        # widget clicado -- la miniatura de la ventana principal. Resultado: el
        # foco vuelve a la principal, y en Windows eso la trae al frente encima
        # de la recién creada. Medido en repro: con clic el foco termina en la
        # raíz; sin clic se queda en la ventana nueva.
        #
        # `_traer_al_frente` (topmost ON/OFF diferido) es el patrón que ya se usa
        # en el resto del archivo para exactamente este problema de apilamiento
        # en Windows, y corre después de que el clic terminó de propagarse.
        ventana.update_idletasks()
        _centrar_en_ventana_principal(self, ventana, ventana.winfo_width(), ventana.winfo_height())
        _traer_al_frente(ventana)

    @staticmethod
    def _ajustar_a_espacio(im: Image.Image, espacio: tuple[int, int]) -> Image.Image:
        """Escala la imagen para OCUPAR el espacio disponible, agrandando si
        hace falta -- no solo achicando.

        Causa raíz real de "el recorte se ve muy pequeño" (reportada muchas
        veces, sobrevivió a maximizar la ventana y a achicar todas las
        franjas alrededor): esto usaba `Image.thumbnail()`, que por diseño
        de PIL SOLO reduce -- nunca agranda una imagen más allá de su tamaño
        original. Si el recorte de origen es una foto de resolución modesta
        (algunos cientos de píxeles) y el visor mide mucho más que eso
        (sobre todo ahora que la ventana abre maximizada), `thumbnail()`
        dejaba la foto en su tamaño nativo chico y todo el espacio ganado
        alrededor quedaba vacío -- ningún ajuste de columnas o franjas podía
        arreglar eso, el límite estaba en esta función.

        Se agranda con Lanczos (mejor calidad que el vecino más cercano) y
        se limita a 4x el tamaño original para no llegar a verse pixelado
        cuando el recorte de origen es muy chico."""
        ancho_e, alto_e = espacio
        if im.width <= 0 or im.height <= 0:
            return im
        escala = min(ancho_e / im.width, alto_e / im.height)
        escala = min(escala, 4.0)  # tope: no pixelar de más un recorte muy chico
        nuevo = (max(1, round(im.width * escala)), max(1, round(im.height * escala)))
        if nuevo == (im.width, im.height):
            return im
        return im.resize(nuevo, Image.LANCZOS)

    def _mostrar_grande(self, path: Path, titulo: str) -> None:
        """Muestra una imagen en el visor central, agrandándola si hace
        falta para ocupar el espacio disponible (ver `_ajustar_a_espacio`)
        -- se usa EN VIVO durante el procesamiento, para que cada etapa
        (original, detección, recorte, limpieza) se vea grande de verdad."""
        try:
            im = Image.open(path).convert("RGB")
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo abrir la foto del visor en vivo: %s", path)
            return
        im = self._ajustar_a_espacio(im, self._espacio_visor())
        self._foto_visor_vivo = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_visor_vivo)
        self.v_paso_proceso.set(titulo)
        self.update_idletasks()  # forzar repintado YA, no esperar al próximo ciclo ocioso

    def _mostrar_imagen_pil(self, im: Image.Image, titulo: str) -> None:
        im = self._ajustar_a_espacio(im.copy(), self._espacio_visor())  # ver nota en `_mostrar_grande`
        self._foto_visor_vivo = ImageTk.PhotoImage(im)
        self.lbl_imagen.configure(image=self._foto_visor_vivo)
        self.v_paso_proceso.set(titulo)
        self.update_idletasks()  # forzar repintado YA, no esperar al próximo ciclo ocioso

    # descripciones en lenguaje de negocio, no técnico
    _DESCRIPCION_PASO = {
        "cargada": "Cargando la lámina",
        "detectada": "Ubicando los pares de calzado en la foto",
        "separadas": "Separando cada calzado",
        "limpiando": "Quitando el fondo",
        "recorte": "Recorte listo",
        "lista": "Lámina lista",
        "sombra_corregida": "Corrigiendo sombra pegada al calzado",
        "excedente_recortado": "Recortando restos de fondo pegados al calzado",
        "suela_completada": "Completando la suela mordida por otro par",
        "invalidado": "Volviendo a procesar (había un resultado anterior)",
    }

    def _procesar_linea(self, linea: str) -> None:
        if not linea.startswith("[LI] "):
            return
        try:
            campos = json.loads(linea[5:])
        except json.JSONDecodeError:
            _logger_gui().exception("linea de progreso del worker ilegible: %r", linea)
            return
        evento = campos.get("evento")

        if evento == "empieza":
            nombre_archivo = campos.get("archivo", "")
            self.v_lamina_actual.set(
                f'Lámina {campos.get("n")} de {campos.get("total")}: {nombre_archivo}')
            self._pasos_hechos_lamina = 0
            self._pasos_total_lamina = 3  # cargada + detectada + separadas, hasta saber piezas
            self._piezas_lamina_actual = 0
            self._actualizar_progreso()
            ruta = self._rutas_pendientes.get(nombre_archivo)
            if ruta and ruta.exists():
                self._mostrar_grande(ruta, f"Foto original: {nombre_archivo}")

        elif evento == "paso":
            paso = campos.get("paso", "")
            desc = self._DESCRIPCION_PASO.get(paso, paso)
            extra = ""
            if paso in ("limpiando", "recorte") and campos.get("de"):
                extra = f' (calzado {campos.get("i")} de {campos.get("de")})'
            recorte_nombre = campos.get("recorte") or campos.get("archivo")
            if recorte_nombre and paso not in ("limpiando", "recorte"):
                extra = f' ({Path(str(recorte_nombre)).stem})'

            # "separadas" es el primer evento que dice cuántas piezas hay en
            # esta lámina — recién ahí se sabe cuántos pasos faltan de verdad.
            if paso == "separadas" and campos.get("piezas"):
                piezas = campos["piezas"]
                self._piezas_lamina_actual = piezas
                self._pasos_total_lamina = 3 + piezas * 2 + 1

            if paso in self._DESCRIPCION_PASO:
                self._pasos_hechos_lamina += 1

            # Número de paso visible: el que se acaba de anunciar (1-indexado),
            # topado al total conocido hasta el momento.
            paso_num = min(self._pasos_hechos_lamina, self._pasos_total_lamina) if self._pasos_total_lamina else 0
            self.v_paso_actual.set(
                f"Paso {paso_num} de {self._pasos_total_lamina}: {desc}{extra}")
            self.v_estado_proc.set(
                f'Lámina {campos.get("lamina", "?")} de {self._total_proc}: {desc}{extra}')

            self._actualizar_progreso()

            if paso == "detectada" and campos.get("overlay"):
                ruta_overlay = Path(campos["overlay"])
                if ruta_overlay.exists():
                    self._mostrar_grande(ruta_overlay, "Ubicando los pares de calzado")

            if paso == "recorte" and campos.get("archivo"):
                ruta_recorte = Path(campos["archivo"])
                if ruta_recorte.exists():
                    # En vivo: el recorte CON las líneas azul/roja ya puestas,
                    # no la foto plana — así se ve de inmediato si algo falta.
                    try:
                        im_lineas = reparar.dibujar_contorno(ruta_recorte)
                        self._mostrar_imagen_pil(im_lineas, f"Recorte{extra} — línea azul: borde "
                                                             f"real, línea roja: curva esperada")
                    except Exception:  # noqa: BLE001
                        _logger_gui().exception("no se pudo dibujar el contorno del recorte %s", ruta_recorte)
                        self._mostrar_grande(ruta_recorte, f"Recorte{extra}")

            if paso == "excedente_recortado" and campos.get("recorte"):
                self._mostrar_recorte_por_stem(campos["recorte"],
                                               f"Recortando restos de fondo pegados al calzado "
                                               f"({campos.get('px', 0)}px)")

            if paso == "suela_completada" and campos.get("recorte"):
                self._mostrar_recorte_por_stem(campos["recorte"],
                                               f"Suela completada ({campos.get('px', 0)}px rellenados)")

        elif evento == "foto":
            self._hechas_proc += 1
            self._pasos_hechos_lamina = self._pasos_total_lamina
            self._actualizar_progreso()
            self.v_estado_proc.set(f"Procesando {self._hechas_proc} de {self._total_proc} láminas")
            self._recargar_manifiesto()

    def _mostrar_recorte_por_stem(self, stem: str, titulo: str) -> None:
        """Busca el png actual de un recorte (por su nombre, sin extensión) en
        la carpeta de salida y lo muestra en vivo — se usa para los eventos de
        limpieza automática (excedente_recortado, suela_completada), que
        llegan DESPUÉS de que el recorte ya se guardó a disco."""
        if not self._salida:
            return
        directo = self._salida / "transparente" / f"{stem}.png"
        candidatos = [directo] if directo.exists() else list(self._salida.glob(f"**/{stem}.png"))
        if not candidatos:
            return
        try:
            im = reparar.dibujar_contorno(candidatos[0])
            self._mostrar_imagen_pil(im, titulo)
        except Exception:  # noqa: BLE001
            _logger_gui().exception("no se pudo dibujar el contorno de %s; se muestra sin lineas", candidatos[0])
            self._mostrar_grande(candidatos[0], titulo)

    def _actualizar_progreso(self) -> None:
        """% real: láminas ya terminadas + avance de pasos dentro de la
        lámina actual, sobre el total de láminas del lote — no una animación
        genérica que se mueve sola sin decir cuánto falta de verdad."""
        fraccion_lamina = (self._pasos_hechos_lamina / self._pasos_total_lamina
                            if self._pasos_total_lamina else 0.0)
        fraccion_lamina = min(fraccion_lamina, 1.0)
        total = max(self._total_proc, 1)
        pct = 100.0 * (self._hechas_proc + fraccion_lamina) / total
        pct = min(pct, 100.0)
        self.barra_progreso.configure(value=pct)
        self.v_pct.set(f"{pct:.0f}%")

        pct_lamina = 100.0 * fraccion_lamina
        self.barra_progreso_lamina.configure(value=pct_lamina)
        if self._pasos_total_lamina:
            self.v_pct_lamina.set(
                f"{min(self._pasos_hechos_lamina, self._pasos_total_lamina)}/{self._pasos_total_lamina}")
        else:
            self.v_pct_lamina.set("")

    # Tamaño de las celdas de la grilla de elección de fotos extraídas
    # (paso 2, "Qué limpiar"). Acá vivían también las constantes de la
    # pantalla masiva "Elegir recortes a limpiar" (MINIATURA_LIMPIEZA,
    # ANCHO/ALTO_CELDA_LIMPIEZA, ANCHO_CONTROLES_LIMPIEZA), eliminadas junto
    # con esa pantalla el 2026-09-08.
    MINIATURA_REVISION = 170
    ANCHO_CELDA_REVISION = 194
    ALTO_CELDA_REVISION = 232
    _SEPARACION_CELDA = 12  # el padx/pady de cada celda, contado dos veces

    @staticmethod
    def _visor_de_grilla(grilla):
        """La franja VISIBLE de un `CTkScrollableFrame`.

        Ojo, que acá había un error real y silencioso: `grilla.winfo_height()`
        de un CTkScrollableFrame NO da el alto visible sino el alto de TODO su
        contenido (medido: 8898 px con 20 tarjetas en una ventana de 774), así
        que cualquier cálculo de "cuánto espacio tengo" hecho con eso da
        siempre gigante y no ajusta nada. El alto visible es el del canvas que
        lo contiene, que es su `master` — API pública de Tk, no interna de CTk."""
        return getattr(grilla, "master", None) or grilla

    def _acomodar_grilla(self, clave: str, grilla, obtener_celdas,
                          ancho_celda: int) -> None:
        """Re-acomoda las celdas de una grilla de miniaturas en tantas
        columnas como quepan en el ancho disponible.

        `obtener_celdas` es una función y no una lista porque las celdas se
        pueden destruir mientras la pantalla está abierta (desechar un recorte
        desde el visor grande saca su tarjeta), y una lista capturada al
        dibujar quedaría con widgets muertos.

        Se guarda el número de columnas ya aplicado: Tk emite `<Configure>` a
        cada píxel de arrastre del borde de la ventana, y re-grillar cientos
        de tarjetas en cada uno de esos eventos trabaría la ventana."""
        ancho = self._visor_de_grilla(grilla).winfo_width()
        if ancho <= 1:  # todavía sin layout: estimar del ancho de la ventana
            ancho = max(self.winfo_width() - 90, ancho_celda)
        cols = max(1, (ancho - 8) // (ancho_celda + self._SEPARACION_CELDA))
        if self._cols_grilla.get(clave) == cols:
            return
        self._cols_grilla[clave] = cols
        for idx, celda in enumerate(obtener_celdas()):
            fila_i, col_i = divmod(idx, cols)
            celda.grid_configure(row=fila_i, column=col_i)


# División 2026-09-23, Fase 1: el cálculo del precio de venta sugerido se
# mudó a `precios.py` (regla de negocio, no interfaz). Se re-importa acá con
# los mismos nombres.
from precios import (  # noqa: E402
    CATEGORIAS_CONOCIDAS, GENEROS_CONOCIDOS, IVA_CR, MONEDAS_PROVEEDOR,
    MARGEN_VENTA_DEFAULT, moneda_valida, moneda_convierte,
    _rasgo_moneda, redondear_pvp, calcular_precio_venta,
    FUENTES_PRECIO_LEGIBLE, ROTULO_PRECIO_POR_COSTO, ROTULO_PRECIO_COMPARABLES,
    texto_precio_referencia,
)


# (`_nombre_archivo_seguro`, `reloj`, el forzado de foreground y
#  `_cargar_foto_generico` viven en `util_ventana.py` desde la Fase 0
#  de la división 2026-09-23)


# División 2026-09-23, Fase 1: la traducción de nombre de color a hex legible
# se mudó a `colores_producto.py`. Se re-importa acá con los mismos nombres.
from colores_producto import (  # noqa: E402
    _HEX_POR_COLOR, _SEP_COLOR, _SIN_TILDE,
    _normalizar_color, _buscar_hex, _colores_de_nombre,
    _hex_de_color, _texto_sobre, _SUFIJO_INTERNO,
    codigo_visible, _pastilla_color,
)


# División 2026-09-23, Fase 2: las pantallas de candidatos y de comparables
# (~2,750 líneas entre las dos) se mudaron a `pantalla_candidatos.py` y
# `panel_comparables.py`. Se re-importan acá porque `HerramientaUnica` las
# instancia por nombre.
from pantalla_candidatos import VentanaCandidatosCTk  # noqa: E402
from panel_comparables import PanelComparablesCTk  # noqa: E402

# (`PanelComparablesCTk` vive en `panel_comparables.py` desde la Fase 2
#  de la división 2026-09-23)


if __name__ == "__main__":
    # Ícono de la BARRA DE TAREAS de Windows (distinto del de la titlebar, que
    # ya lo pone `iconbitmap`): Windows agrupa la barra de tareas por
    # "AppUserModelID", y como esto corre bajo python.exe hereda el ID —y el
    # ícono— de Python salvo que el proceso declare el suyo ANTES de crear
    # cualquier ventana. De ahí que esto vaya acá y no dentro de la clase.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "TUCalzado.AsistenteCompras.1")
    except Exception:  # noqa: BLE001
        pass  # otro sistema operativo, o Windows lo rechaza: no es crítico

    HerramientaUnica().mainloop()
