r"""
etiquetado_calibracion.py
==========================
Plan 2026-09-07, paso 2.6 (calibración humana) -- la única validación real
pendiente de la fórmula de score determinística. Todo lo hecho en los pasos
2.1-2.5 garantiza que el score sea REPRODUCIBLE y no esté sesgado por color/
tipo/lote; nada de eso garantiza que MIDA lo que el comprador realmente
valora al ver un candidato. Solo el juicio humano puede responder eso.

Herramienta de un solo uso: muestra candidatos del catálogo EN REVISIÓN uno
por uno, CON su foto y sus atributos reales (categoría, género, color,
proveedor) pero SIN mostrar el grado ni el score -- el comprador etiqueta
"lo compraría / lo dudaría / no lo compraría" a ciegas. Cada etiqueta se
guarda de inmediato (`cfg.eval_candidato_etiquetado`, vía
`motor_candidatos.guardar_etiqueta_candidato`) junto con una foto del score
en ese momento, para poder medir después (ver
`GestionTUC/pipeline/analizar_calibracion.py`) si score_final correlaciona
con lo que el comprador de verdad haría.

No hace falta terminar los 54 de una sesión: los ya etiquetados se saltan
la próxima vez (`motor_candidatos.candidatos_ya_etiquetados`).

Sobre qué LOTE trabaja (bug real corregido 2026-09-18): la Fase 2 del motor
mueve la calificación a un `lote.sqlite` propio por carpeta, así que hace
falta decirle a `motor_calificacion` cuál está activa ANTES de pedir
candidatos -- sin esto, `mc.obtener_candidatos()` reventaba con
"RuntimeError: No hay lote activo". La carpeta se resuelve, en orden:
1) argumento de línea de comandos; 2) la última carpeta que usó la app
principal (`_ultima_carpeta.json`, mismo archivo que `gui_profesional_ctk`);
3) un selector de carpeta, apuntado a `AsistenteComprasLotes` si existe.

Uso: python etiquetado_calibracion.py ["C:\...\AsistenteComprasLotes\Prueba2"]
"""
from __future__ import annotations

import json
import random
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import motor_calificacion
import motor_candidatos as mc
from gui_profesional_ctk import (  # reutiliza el mismo tema visual de la app real
    Boton, Etiqueta, Marco, Tarjeta, solido, armar_fuentes,
    PAR_FONDO, PAR_VISOR_BG,
    PAR_OK, PAR_OK_BG, PAR_ALERTA, PAR_ALERTA_BG, PAR_MAL, PAR_MAL_BG,
    PROYECTO,
    _cargar_foto_generico,
)

ctk.set_appearance_mode("system")

# Mismo archivo y misma clave que `HerramientaUnica.ARCHIVO_CONFIG_SESION`
# (gui_profesional_ctk.py) -- ahí es un atributo de clase, no un nombre de
# módulo, así que no se puede importar directo.
_ARCHIVO_CONFIG_SESION = PROYECTO / "_ultima_carpeta.json"


def _leer_ultima_carpeta() -> Path | None:
    """Copia mínima de `HerramientaUnica._leer_ultima_carpeta`: mismo archivo,
    misma clave `"salida"`. No reutiliza el método completo porque ese vive
    colgado de una instancia de la ventana principal; acá solo hace falta
    LEER, nunca reescribir."""
    if not _ARCHIVO_CONFIG_SESION.exists():
        return None
    try:
        datos = json.loads(_ARCHIVO_CONFIG_SESION.read_text(encoding="utf-8"))
        salida = datos.get("salida")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return Path(salida) if salida else None


def _resolver_carpeta_lote() -> Path | None:
    if len(sys.argv) > 1:
        carpeta = Path(sys.argv[1])
        if carpeta.is_dir():
            return carpeta
        messagebox.showwarning("Calibración", f"La carpeta indicada no existe:\n{carpeta}")

    carpeta = _leer_ultima_carpeta()
    if carpeta and carpeta.is_dir():
        return carpeta

    carpeta_lotes = PROYECTO.parent.parent / "AsistenteComprasLotes"
    elegida = filedialog.askdirectory(
        title="Elegí la carpeta del lote a calibrar",
        initialdir=str(carpeta_lotes) if carpeta_lotes.is_dir() else str(PROYECTO))
    return Path(elegida) if elegida else None


# Semilla fija: mismo orden entre corridas de una misma sesión (si se cierra
# la ventana y se vuelve a abrir sobre el mismo lote, no se re-mezcla al
# azar) pero suficientemente arbitraria para no coincidir con ningún orden
# con significado (score, fecha de ingesta, código).
_SEMILLA_ORDEN = 20260918


def _orden_calibracion(candidatos: list[dict]) -> list[dict]:
    """Orden de presentación para el etiquetado a ciegas.

    Los 20 candidatos de Reebok etiquetados antes de este cambio salieron
    casi todos S/A -- la sospecha real es que `obtener_candidatos()` los
    devuelve ordenados por score descendente y el comprador etiquetó en ese
    orden hasta cansarse, así que nunca llegó a ver los B/C/D del final de
    la lista. Dos correcciones, no una sola:

    1. Aleatorizar DENTRO de cada grado (nunca en orden de score) -- el
       comprador no puede inferir el grado por dónde aparece en la sesión.
    2. Entrelazar los grados round-robin (S, A, B, C, D, S, A, B, C, D, ...)
       en vez de agotar un grado antes de pasar al siguiente -- así, si la
       sesión se corta a la mitad (lo normal: "no hace falta terminar los
       54 de una sesión"), lo ya etiquetado igual cubre los 5 grados en vez
       de quedar concentrado en uno solo, como pasó la vez pasada.
    """
    azar = random.Random(_SEMILLA_ORDEN)
    por_grado: dict[str, list[dict]] = {}
    for c in candidatos:
        por_grado.setdefault(c.get("grado") or "sin_grado", []).append(c)
    for grupo in por_grado.values():
        azar.shuffle(grupo)

    orden_grados = sorted(por_grado)  # determinista: A,B,C,D,S,sin_grado
    azar.shuffle(orden_grados)  # pero sin privilegiar a ninguno por orden alfabético

    entrelazado: list[dict] = []
    indices = {g: 0 for g in orden_grados}
    while True:
        agregado = False
        for g in orden_grados:
            i = indices[g]
            if i < len(por_grado[g]):
                entrelazado.append(por_grado[g][i])
                indices[g] += 1
                agregado = True
        if not agregado:
            break
    return entrelazado


class VentanaCalibracion(ctk.CTk):
    def __init__(self, carpeta_lote: Path) -> None:
        super().__init__()
        armar_fuentes()
        self.title(f"Calibración del score — etiquetado a ciegas — {carpeta_lote.name}")
        self.geometry("560x640")
        self.configure(fg_color=solido(PAR_FONDO))
        self._foto_tk: ImageTk.PhotoImage | None = None

        motor_calificacion.fijar_lote(carpeta_lote)
        self._candidatos = [c for c in mc.obtener_candidatos() if c.get("score_final") is not None]
        ya = mc.candidatos_ya_etiquetados()
        pendientes = [c for c in self._candidatos if c.get("codigo_proveedor") not in ya]
        self._pendientes = _orden_calibracion(pendientes)
        self._total_catalogo = len(self._candidatos)
        self._ya_etiquetados = len(ya)
        self._indice = 0

        Etiqueta(self, text="Calibración del score", style="Titulo.TLabel").pack(
                anchor="w", padx=18, pady=(16, 2))
        Etiqueta(self, text="Mirá la foto y decidí como comprador -- el grado y el score "
                "están escondidos a propósito. Esto es lo único que puede confirmar si "
                "el score mide lo que de verdad importa.",
                style="Suave.TLabel", justify="left", wraplength=520).pack(
                anchor="w", padx=18, pady=(0, 10), fill="x")

        self._etq_progreso = Etiqueta(self, text="", style="Suave.TLabel")
        self._etq_progreso.pack(anchor="w", padx=18, pady=(0, 10))

        self._tarjeta = Tarjeta(self)
        self._tarjeta.pack(padx=18, pady=(0, 14), fill="both", expand=True)

        self._hueco_foto = ctk.CTkFrame(self._tarjeta, fg_color=PAR_VISOR_BG,
                                        corner_radius=10, width=320, height=320)
        self._hueco_foto.pack(pady=(20, 14))
        self._hueco_foto.pack_propagate(False)
        self._etq_foto = tk.Label(self._hueco_foto, background=solido(PAR_VISOR_BG), borderwidth=0)
        self._etq_foto.pack(fill="both", expand=True)

        self._etq_codigo = Etiqueta(self._tarjeta, text="", style="Subtitulo.TLabel")
        self._etq_codigo.pack(pady=(0, 2))
        self._etq_atributos = Etiqueta(self._tarjeta, text="", style="Suave.TLabel")
        self._etq_atributos.pack(pady=(0, 20))

        botones = Marco(self._tarjeta)
        botones.pack(pady=(0, 20))
        tk.Button(botones, text="✅  Lo compraría", font=("Segoe UI Semibold", 13),
                 fg=solido(PAR_OK), bg=solido(PAR_OK_BG), relief="flat", padx=16, pady=10,
                 command=lambda: self._etiquetar("compraria")).pack(side="left", padx=6)
        tk.Button(botones, text="🤔  Lo dudaría", font=("Segoe UI Semibold", 13),
                 fg=solido(PAR_ALERTA), bg=solido(PAR_ALERTA_BG), relief="flat", padx=16, pady=10,
                 command=lambda: self._etiquetar("dudaria")).pack(side="left", padx=6)
        tk.Button(botones, text="❌  No lo compraría", font=("Segoe UI Semibold", 13),
                 fg=solido(PAR_MAL), bg=solido(PAR_MAL_BG), relief="flat", padx=16, pady=10,
                 command=lambda: self._etiquetar("no_compraria")).pack(side="left", padx=6)

        Boton(self, text="Saltar (no etiquetar este)", style="Sutil.TButton",
             command=self._siguiente).pack(pady=(0, 16))

        self._mostrar_actual()

    def _mostrar_actual(self) -> None:
        if self._indice >= len(self._pendientes):
            self._tarjeta.pack_forget()
            Etiqueta(self, text=f"✓ Listo -- no quedan candidatos pendientes de etiquetar "
                     f"({self._ya_etiquetados + self._indice}/{self._total_catalogo} en total). "
                     "Podés cerrar esta ventana.",
                    style="Subtitulo.TLabel", wraplength=520, justify="left").pack(
                    padx=18, pady=40, fill="x")
            return

        cand = self._pendientes[self._indice]
        self._etq_progreso.configure(
            text=f"{self._ya_etiquetados + self._indice} de {self._total_catalogo} candidatos del catálogo "
                 f"({len(self._pendientes) - self._indice} pendientes ahora)")

        colores = cand.get("colores_vista") or []
        ruta = None
        if colores:
            url = colores[0].get("url", "")
            if url.startswith("/crop_variante/"):
                try:
                    # El respaldo para lotes viejos (imagen_limpia_path
                    # apuntando a una carpeta compartida ya pisada) vive
                    # ahora dentro de ruta_foto_variante() -- así lo
                    # aprovecha también la app principal.
                    ruta = mc.ruta_foto_variante(int(url.rsplit("/", 1)[-1]))
                except ValueError:
                    ruta = None
        foto_tk = _cargar_foto_generico(ruta, tam=(300, 300))
        self._foto_tk = foto_tk
        if foto_tk is not None:
            self._etq_foto.configure(image=foto_tk, text="")
        else:
            self._etq_foto.configure(image="", text="sin foto")

        self._etq_codigo.configure(text=cand.get("codigo_proveedor") or f"Candidato {cand['candidato_id']}")
        categoria = cand.get("categoria_declarada") or "categoría sin resolver"
        genero = cand.get("genero_declarado") or "género sin resolver"
        color = cand.get("color_principal") or "—"
        proveedor = cand.get("proveedor") or "—"
        self._etq_atributos.configure(
            text=f"{categoria}  ·  {genero}  ·  color {color}\nProveedor: {proveedor}")

    def _etiquetar(self, etiqueta: str) -> None:
        cand = self._pendientes[self._indice]
        try:
            mc.guardar_etiqueta_candidato(cand["candidato_id"], etiqueta)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Calibración", f"No se pudo guardar:\n{exc}")
            return
        self._siguiente()

    def _siguiente(self) -> None:
        self._indice += 1
        self._mostrar_actual()


if __name__ == "__main__":
    _carpeta = _resolver_carpeta_lote()
    if _carpeta is None:
        messagebox.showinfo("Calibración", "No se eligió ninguna carpeta de lote. Cerrando.")
    elif not _carpeta.is_dir():
        messagebox.showerror("Calibración", f"La carpeta no existe:\n{_carpeta}")
    else:
        VentanaCalibracion(_carpeta).mainloop()
