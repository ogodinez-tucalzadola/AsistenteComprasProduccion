"""
Rutas a carpetas HERMANAS de este repo (fuera de git), de las que
AsistenteComprasProduccion depende en tiempo de ejecución:

- `GESTIONTUC_PIPELINE`: OCR de catálogo de proveedor (`ingestar_catalogo_tuc`,
  importado por `motor_calificacion.py`).
- `GESTIONTUC_DATA`: HTML del Asistente de Compras PTY que `motor_calificacion`
  sabe exportar (`WEB_DIR`), aunque este repo no lo usa por su cuenta.
- `ANALISISMERCADO_SCRIPTS`: vectorización de imágenes (`vectorizar_imagenes`,
  `promover_embeddings_imagen`), modelos fashion_siglip/dino_v2.
- `DATOS_COMPARTIDOS`: banco de imágenes compartido con el resto del
  ecosistema (marca reconocida + TUCALZADO).
- `ASISTENTE_COMPRAS_LOTES`: carpeta central donde vive cada `lote.sqlite`
  de trabajo (ver `AUDITORIA_2026-09-22.md`, hallazgo C2/C3 -- nunca se
  versiona).

Auditoría 2026-09-23 (M9): antes estas 6 rutas estaban repetidas, cada una
escrita a mano, en `motor_calificacion.py` (×4), `motor_candidatos.py` (×1)
y `gui_profesional_ctk.py` (×1) -- si la máquina cambiara o alguna carpeta
se moviera, había que encontrar y editar las 6 copias por separado.
Centralizadas acá en un solo lugar.

Cada una se puede sobrescribir con una variable de entorno del mismo
nombre, por si algún día hace falta correr esto en otra máquina sin tocar
código -- hoy nada las define, así que el comportamiento real no cambia.
"""

from __future__ import annotations

import os
from pathlib import Path

_BASE = Path(r"C:\Users\Tucalzado\Proyectos")


def _ruta(env_var: str, default: Path) -> Path:
    valor = os.environ.get(env_var)
    return Path(valor) if valor else default


GESTIONTUC_PIPELINE = _ruta("ASISTENTE_GESTIONTUC_PIPELINE", _BASE / "GestionTUC" / "pipeline")
GESTIONTUC_DATA = _ruta("ASISTENTE_GESTIONTUC_DATA", _BASE / "GestionTUC" / "data")
ANALISISMERCADO_SCRIPTS = _ruta("ASISTENTE_ANALISISMERCADO_SCRIPTS", _BASE / "AnálisisMercado" / "scripts")
DATOS_COMPARTIDOS = _ruta("ASISTENTE_DATOS_COMPARTIDOS", _BASE / "DatosCompartidos")
ASISTENTE_COMPRAS_LOTES = _ruta("ASISTENTE_COMPRAS_LOTES", _BASE / "AsistenteComprasLotes")
