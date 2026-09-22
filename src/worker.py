"""
Procesa un plan de láminas. Uno de estos por proceso paralelo.

No se invoca a mano: lo lanza la interfaz, que antes reparte el trabajo en la
tabla `plan` del `lote.sqlite` de la carpeta de trabajo — un tramo por `--id`,
para que ningún proceso vuelva a calcular huellas ni pise el trabajo de otro.

Emite líneas [LI] con clave=valor para que la interfaz muestre el avance real,
incluido el recorte actual dentro de la lámina — que es lo que hace que una
corrida larga no parezca colgada.

    python worker.py --salida salida --id 0 --size 500
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

import almacen
import decisiones
import estado
import nucleo
import pasos
import reparar
import reparar_sombra

warnings.filterwarnings("ignore")


def emitir(**campos) -> None:
    """Un evento por línea, en JSON.

    Antes iba como `clave=valor` separado por espacios, y cualquier ruta con un
    espacio —una carpeta llamada "prueba manual"— partía el campo en dos: la
    interfaz recibía una ruta truncada y no encontraba ninguna imagen.
    """
    print("[LI] " + json.dumps(campos, ensure_ascii=False), flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Procesa un plan de láminas con el pipeline de Zawa.")
    p.add_argument("--salida", required=True)
    p.add_argument("--id", type=int, default=0, help="identificador de este proceso")
    p.add_argument("--size", type=int)
    p.add_argument("--esperados", type=int)
    p.add_argument("--padding", type=float)
    p.add_argument("--umbral", type=float)
    p.add_argument("--prompt")
    p.add_argument("--fondo", choices=["white", "transparent", "both"])
    p.add_argument("--debug", action="store_true")
    p.add_argument("--hilos", type=int, default=1)
    p.add_argument("--erosion-fondo", type=int,
                   help="px de contorno a descartar (default 4)")
    p.add_argument("--sin-limpiar-fondo", action="store_true",
                   help="no quitar follaje/sombra pegados al calzado")
    args = p.parse_args()

    salida = Path(args.salida)
    items = nucleo.leer_plan(salida, args.id)

    emitir(evento="inicio", id=args.id, total=len(items))
    if not items:
        emitir(evento="fin", id=args.id, hechas=0, recortes=0, segundos=0)
        return

    z = nucleo.importar_zawa()
    cfg = z["Config"]()
    if args.size:
        cfg.canvas_size = args.size
    if args.esperados:
        cfg.expected_count = args.esperados
    if args.padding is not None:
        cfg.padding = args.padding
    if args.umbral is not None:
        cfg.box_threshold = args.umbral
    if args.prompt:
        cfg.prompt = args.prompt
    if args.fondo:
        cfg.background = args.fondo

    device = z["pick_device"]()
    emitir(evento="dispositivo", id=args.id, valor=str(device))

    detector, matter, instancer = nucleo.construir_pipeline(z, cfg, device, hilos=args.hilos)
    emitir(evento="listo", id=args.id, matting=matter.name, dtype=str(matter.dtype))

    dirs = nucleo.preparar_dirs(salida, con_debug=args.debug)

    hechas = 0
    recortes_total = 0
    marcadas = 0
    t0 = time.perf_counter()

    for n, (path, huella) in enumerate(items, start=1):
        emitir(evento="empieza", id=args.id, n=n, total=len(items), archivo=path.name)
        t_foto = time.perf_counter()

        def avisar(_lamina=n, **campos):
            # `lamina` y no `n`: los eventos de etapa traen sus propias
            # claves y una colisión de nombres tumbaba la lámina entera.
            emitir(evento="paso", id=args.id, lamina=_lamina, **campos)

        # Si esta lámina ya tenía recortes reparados o decididos, esas
        # entradas describen píxeles que están por sobrescribirse. Sin esto,
        # la interfaz sigue mostrando "completada" o "aprobado" sobre una
        # imagen que en los hechos cambió — pasó de verdad con RP9300_02.
        borrados_rep = reparar.purgar_stem(salida, path.stem)
        borrados_dec = decisiones.purgar_stem(salida, path.stem)
        if borrados_rep or borrados_dec:
            emitir(evento="paso", id=args.id, lamina=n, paso="invalidado",
                  reparaciones=borrados_rep, decisiones=borrados_dec)

        try:
            registro = pasos.procesar_lamina(
                path, detector, matter, instancer, cfg, dirs, z, avisar,
                limpiar_fondo_activo=not args.sin_limpiar_fondo,
                erosion_fondo=args.erosion_fondo)
            registro["sha1"] = huella  # identidad estable, no la ruta
            registro["version_pipeline"] = nucleo.VERSION_PIPELINE
        except Exception as exc:  # noqa: BLE001 - una lámina mala no tumba el lote
            registro = {
                "origen": str(path),
                "sha1": huella,
                "error": str(exc),
                "revision_manual": True,
                "recortes": [],
            }

        # Sombra/follaje fusionado como píxel opaco: solo se puede CONFIRMAR
        # comparando contra los demás colores de la misma lámina, nunca
        # adivinando sobre una sola foto (ver reparar_sombra.py). Con menos
        # de 2 colores no hay con qué comparar y no se toca nada.
        pngs_lamina = [Path(r["png"]) for r in registro.get("recortes", []) if r.get("png")]
        correcciones: dict[str, reparar_sombra.Correccion] = {}
        if len(pngs_lamina) >= 2:
            try:
                correcciones = reparar_sombra.corregir_grupo(pngs_lamina)
                for nombre, c in correcciones.items():
                    if c.aplicada:
                        emitir(evento="paso", id=args.id, lamina=n, paso="sombra_corregida",
                              recorte=nombre, px=c.px_corregidos)
            except Exception as exc:  # noqa: BLE001 - no tumba la lámina
                emitir(evento="paso", id=args.id, lamina=n, paso="sombra_error", error=str(exc))

        # SIN limpieza automática: cortar sombra/excedente y completar
        # suela mordida quedan como paso manual aparte, elegido recorte
        # por recorte en la pantalla de selección de la app — la
        # detección automática (tiene_excedente/tiene_mordida_real) tiene
        # falsos positivos reales en calzado con relieve profundo (tacos
        # segmentados, plataformas altas) y aplicarla sola terminaba
        # cortando/rellenando diseño real del producto sin que el usuario
        # lo pidiera. `estado.py` sigue midiendo el estado (para mostrar
        # falta_pct/mordida/excedente en la lista), solo que ya no actúa
        # sobre eso automáticamente.

        # Una sola fuente de verdad por recorte (ver estado.py): se calcula
        # acá, una vez, y se guarda junto a la lámina — la interfaz de
        # revisión lee esto en vez de recalcular medir_suela/comparar_color
        # cada vez que se abre un recorte.
        if pngs_lamina:
            try:
                estados = estado.calcular_estado_grupo(pngs_lamina, salida, correcciones)
                for r in registro.get("recortes", []):
                    if r.get("png"):
                        e = estados.get(Path(r["png"]).name)
                        if e:
                            r["estado"] = e.a_dict()
            except Exception as exc:  # noqa: BLE001 - no tumba la lámina
                emitir(evento="paso", id=args.id, lamina=n, paso="estado_error", error=str(exc))

        ms = int((time.perf_counter() - t_foto) * 1000)
        registro["ms"] = ms
        # Una transacción por lámina: o entra completa con todos sus
        # recortes, o no entra nada. Antes esto era una línea de texto en
        # `manifiesto_N.jsonl`, y un proceso muerto entre el write y el
        # flush dejaba media línea que el lector descartaba entera — se
        # perdían los recortes buenos de esa lámina.
        almacen.guardar_lamina(salida, registro, worker_id=args.id)

        hechas += 1
        n_rec = len(registro.get("recortes", []))
        recortes_total += n_rec
        if registro.get("revision_manual"):
            marcadas += 1

        emitir(
            evento="foto",
            id=args.id,
            n=n,
            total=len(items),
            archivo=path.name,
            recortes=n_rec,
            ms=ms,
            revisar=int(bool(registro.get("revision_manual"))),
            error=int("error" in registro),
        )

    segundos = time.perf_counter() - t0
    emitir(
        evento="fin",
        id=args.id,
        hechas=hechas,
        recortes=recortes_total,
        marcadas=marcadas,
        segundos=round(segundos, 1),
    )


if __name__ == "__main__":
    main()
