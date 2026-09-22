"""
Una sola fuente de verdad por recorte.

Antes, "% faltante", "mordida real" y "sombra vs. grupo" se calculaban en
momentos distintos con funciones distintas (`reparar.medir_suela` en la
pantalla de revisión, `comparar_colores` solo dentro de `reparar_sombra`) —
cada apertura del recorte los recalculaba de cero, y no había un solo lugar
que dijera "esto es lo que se sabe de este recorte ahora mismo".

Acá se calcula todo UNA vez, cuando ya están los 2+ colores de la lámina
disponibles (el mismo momento en que corre `reparar_sombra.corregir_grupo` en
`worker.py`), y se guarda junto al recorte (columnas `falta_pct`,
`mordida_real`, … de la tabla `recorte`, ver `almacen.py`). La interfaz de
revisión lee ese dato ya calculado — no vuelve a correr `medir_suela` ni `comparar_color_borde`
cada vez que se abre un recorte.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import decisiones
import reparar


@dataclass
class EstadoRecorte:
    nombre: str
    falta_pct: float
    mordida_real: bool
    sombra_outlier: bool
    sombra_corregida_px: int
    tiene_excedente: bool
    decision: str

    def a_dict(self) -> dict:
        return asdict(self)


def calcular_estado_grupo(pngs: list[Path], salida: Path,
                           correcciones: dict[str, "reparar_sombra.Correccion"] | None = None
                           ) -> dict[str, EstadoRecorte]:
    """Calcula el estado de cada recorte de una misma lámina, una sola vez.

    `correcciones` es el resultado ya obtenido de `reparar_sombra.corregir_grupo`
    sobre estos mismos `pngs` — no se vuelve a correr acá, para no reparar dos
    veces sobre el mismo grupo.
    """
    correcciones = correcciones or {}
    decs = decisiones.cargar(salida)

    # OJO: `comparar_colores.comparar_color_borde` por sí solo devuelve un
    # tramo para CUALQUIER recorte con algo de autodesvío — eso incluyó a los
    # 4 colores de una lámina real de prueba, no solo al outlier verdadero.
    # El filtro que sí distingue "outlier real del grupo" de "ruido normal de
    # diseño" es el de `reparar_sombra.corregir_grupo` (extensión mínima +
    # razón contra el segundo más alto) — por eso el outlier se lee de
    # `correcciones`, no se recalcula acá con la señal cruda.
    resultado: dict[str, EstadoRecorte] = {}
    for p in pngs:
        try:
            s = reparar.medir_suela(p, color_basura=reparar.COLOR_BASURA)
            falta_pct = round(reparar.porcentaje_faltante(s), 2)
            mordida_real = reparar.tiene_mordida_real(s)
        except ValueError:
            # sin material sólido u otro caso raro: no hay nada que medir
            falta_pct, mordida_real = 0.0, False
        excedente = reparar.tiene_excedente(p, color_basura=reparar.COLOR_BASURA)

        c = correcciones.get(p.name)
        # "outlier confirmado contra el grupo" es el único motivo que indica
        # sombra real; "sin píxeles a corregir" también es un outlier
        # confirmado, solo que no había nada que reparar en los hechos.
        es_outlier = bool(c) and c.motivo.startswith(("outlier confirmado", "outlier detectado"))
        resultado[p.name] = EstadoRecorte(
            nombre=p.name,
            falta_pct=falta_pct,
            mordida_real=mordida_real,
            sombra_outlier=es_outlier,
            sombra_corregida_px=c.px_corregidos if (c and c.aplicada) else 0,
            tiene_excedente=excedente,
            decision=decs.get(p.name, {}).get("decision", decisiones.PENDIENTE),
        )
    return resultado
