"""
Un solo archivo por lote: `lote.sqlite`.

Antes el estado de un lote vivía en cinco archivos sueltos en la misma carpeta
de trabajo (`_lote_asistente.json`, `manifiesto_*.jsonl`, `decisiones.jsonl`,
`plan_*.jsonl`, `reparaciones.jsonl`). Eso traía tres problemas reales, no
teóricos:

1. **Nada era transaccional.** Un `write_text` interrumpido a mitad dejaba el
   archivo truncado, y el lector lo tapaba con `except json.JSONDecodeError:
   continue` — o sea, perdía filas en silencio. Con `_ultima_carpeta.json`
   pasó la versión grave del mismo problema: un archivo mal escrito a mano
   (barras sin escapar) hacía que la app arrancara sin retomar nada y sin
   decir por qué.
2. **El avance del lote no estaba escrito en ningún lado**: había que
   deducirlo cruzando tres archivos en cierto orden, cada uno con su propio
   filtro, y ese criterio estaba duplicado a mano en dos funciones distintas
   (`_recargar_manifiesto` y `_contar_recortes_en`).
3. **No se podía inspeccionar.** Para responder "¿cuántas fotos aprobadas hay
   en este lote?" había que escribir un script.

Ahora es un solo archivo SQLite por carpeta de trabajo, con las columnas
promovidas a columnas de verdad (no un blob JSON), así que se abre con
cualquier herramienta SQL y se consulta con SQL:

    SELECT decision, COUNT(*) FROM vw_decision_vigente GROUP BY decision;

Las listas de largo variable (`caja`, `colores`, `revisar`) sí van como JSON:
normalizarlas en tablas aparte no aporta nada — nunca se consultan por
elemento, siempre se leen completas junto a su recorte.

Concurrencia: los workers son procesos separados que escriben a la vez. Se usa
WAL + `busy_timeout`, que es exactamente el caso de uso para el que existen.
Cada operación abre y cierra su propia conexión: a esta escala (cientos de
filas por lote) el costo es irrelevante y evita tener que razonar sobre
conexiones compartidas entre hilos de Tk y procesos hijos.

La migración de los archivos viejos corre sola la primera vez que se abre una
carpeta que los tenga (ver `_migrar_desde_jsonl`). Los archivos viejos NO se
borran: quedan al lado como red de seguridad.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ARCHIVO = "lote.sqlite"

VERSION_ESQUEMA = 2  # v2 (Fase 2, 2026-09-09): la revisión de catálogo
# (candidato_raw/candidato/candidato_variante/candidato_embedding/candidato_score)
# se mudó del schema `staging_tuc` de Postgres -- compartido entre la app y la
# web vieja, y efímero por diseño (TRUNCATE global al cargar otro catálogo) --
# a ESTE archivo, que ya es privado de cada lote. Sin web, no hace falta que
# sea compartido: dos lotes distintos ya no se pisan, y "descartar el catálogo"
# es borrar filas de este archivo, no de una tabla global.
#
# La versión es informativa: `abrir()` corre `_ESQUEMA` completo (todo
# `IF NOT EXISTS`) en CADA apertura, así que un `lote.sqlite` creado con la v1
# gana las tablas nuevas la primera vez que el programa lo toca, sin migración
# aparte.

# Nombres de los archivos que este módulo reemplaza. Se conservan acá (y no en
# cada módulo suelto) porque son lo que busca la migración.
JSONL_VIEJOS = ("_lote_asistente.json", "manifiesto_*.jsonl", "decisiones.jsonl",
                "plan_*.jsonl", "reparaciones.jsonl")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS esquema (
    version     INTEGER NOT NULL,
    creado      TEXT    NOT NULL
);

-- Reemplaza `_lote_asistente.json`. Clave/valor porque es exactamente eso:
-- un puñado de datos del lote (proveedor, origen de las fotos, constancia de
-- envío) que se van anotando de a uno, sin pisar los demás — el `merge` que
-- antes se hacía leyendo y reescribiendo el JSON entero.
CREATE TABLE IF NOT EXISTS lote_meta (
    clave       TEXT PRIMARY KEY,
    valor_json  TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

-- Reemplaza el nivel LÁMINA de `manifiesto_*.jsonl`. La identidad es la huella
-- del contenido, no la ruta (ver nucleo.py): la misma foto vista por dos rutas
-- es una sola lámina.
CREATE TABLE IF NOT EXISTS lamina (
    sha1             TEXT PRIMARY KEY,
    origen           TEXT,
    ancho            INTEGER,
    alto             INTEGER,
    detecciones      INTEGER,
    matting          TEXT,
    revisar_json     TEXT,
    revision_manual  INTEGER NOT NULL DEFAULT 0,
    ms               INTEGER,
    version_pipeline INTEGER,
    error            TEXT,
    worker_id        INTEGER,
    cuando           TEXT NOT NULL,
    extra_json       TEXT
);

-- Reemplaza el nivel RECORTE de `manifiesto_*.jsonl`. Un recorte por calzado
-- de la lámina; el grano de todas las decisiones del programa.
CREATE TABLE IF NOT EXISTS recorte (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    sha1                  TEXT NOT NULL REFERENCES lamina(sha1) ON DELETE CASCADE,
    indice                INTEGER NOT NULL,
    nombre                TEXT,
    png                   TEXT,
    jpg                   TEXT,
    score                 REAL,
    etiqueta              TEXT,
    caja_json             TEXT,
    cobertura_alfa        REAL,
    vecino_quitado        INTEGER,
    vecino_mascara        TEXT,
    fondo_quitado_px      INTEGER,
    colores_json          TEXT,
    revisar_json          TEXT,
    estado_presente       INTEGER NOT NULL DEFAULT 0,
    falta_pct             REAL,
    mordida_real          INTEGER,
    sombra_outlier        INTEGER,
    sombra_corregida_px   INTEGER,
    tiene_excedente       INTEGER,
    decision_al_procesar  TEXT,
    extra_json            TEXT,
    UNIQUE (sha1, indice)
);
CREATE INDEX IF NOT EXISTS ix_recorte_nombre ON recorte(nombre);

-- Reemplaza `decisiones.jsonl`. Sigue siendo una bitácora que solo crece: cada
-- cambio de opinión agrega una fila y vale la última (mayor `id`). La historia
-- de quién decidió qué y cuándo no se pierde.
CREATE TABLE IF NOT EXISTS decision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorte         TEXT NOT NULL,
    decision        TEXT NOT NULL,
    motivos_qa_json TEXT NOT NULL DEFAULT '[]',
    nota            TEXT NOT NULL DEFAULT '',
    cuando          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_recorte ON decision(recorte);

-- Reemplaza `reparaciones.jsonl`. También bitácora que solo crece: vale la
-- última reparación aplicada a cada recorte.
CREATE TABLE IF NOT EXISTS reparacion (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    recorte        TEXT NOT NULL,
    metodo         TEXT NOT NULL,
    respaldos_json TEXT NOT NULL DEFAULT '{}',
    datos_json     TEXT NOT NULL DEFAULT '{}',
    cuando         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reparacion_recorte ON reparacion(recorte);

-- Reemplaza `plan_*.jsonl`. Cola de trabajo repartida entre los N procesos.
CREATE TABLE IF NOT EXISTS plan (
    worker_id INTEGER NOT NULL,
    orden     INTEGER NOT NULL,
    path      TEXT NOT NULL,
    sha1      TEXT NOT NULL,
    PRIMARY KEY (worker_id, orden)
);

-- De dónde salió cada cosa cuando se migró un lote viejo: cuántas filas trajo
-- cada archivo suelto. Es la prueba auditable de que no se perdió nada.
CREATE TABLE IF NOT EXISTS migracion (
    archivo TEXT NOT NULL,
    filas   INTEGER NOT NULL,
    cuando  TEXT NOT NULL
);

-- Vista de conveniencia: la decisión que VALE para cada recorte, sin tener que
-- reimplementar el "gana la última" en cada consulta a mano.
CREATE VIEW IF NOT EXISTS vw_decision_vigente AS
SELECT d.recorte, d.decision, d.motivos_qa_json, d.nota, d.cuando
FROM decision d
JOIN (SELECT recorte, MAX(id) AS id FROM decision GROUP BY recorte) u
  ON u.id = d.id;

-- Vista de conveniencia: el lote como lo ve la interfaz — recortes con imagen
-- y estado medido, ya cruzados con su decisión vigente y su reparación.
CREATE VIEW IF NOT EXISTS vw_recorte_vigente AS
SELECT r.*,
       l.origen                                        AS origen_lamina,
       COALESCE(dv.decision, r.decision_al_procesar)   AS decision,
       rp.metodo                                       AS reparacion_metodo
FROM recorte r
JOIN lamina l ON l.sha1 = r.sha1
LEFT JOIN vw_decision_vigente dv ON dv.recorte = r.nombre
LEFT JOIN (
    SELECT p.recorte, p.metodo
    FROM reparacion p
    JOIN (SELECT recorte, MAX(id) AS id FROM reparacion GROUP BY recorte) v
      ON v.id = p.id
) rp ON rp.recorte = r.nombre;

-- ═══════════════════════════════════════════════════════════════════════════
-- REVISIÓN DE CATÁLOGO (Fase 2, 2026-09-09) — ex schema `staging_tuc` de Postgres
--
-- Los nombres de COLUMNA se conservan idénticos a los de `staging_tuc` a
-- propósito: el motor de calificación (`motor_calificacion.py`) ya nombra esas
-- columnas en decenas de sitios, y cambiarlas habría sido riesgo puro sin
-- beneficio. Lo que cambia son los nombres de TABLA (sin el prefijo
-- raw_/dim_/fct_/agg_ de la convención de Postgres: acá no hay capas medallón
-- que distinguir, es el archivo de un lote) y tres tipos que SQLite no tiene:
--
--   * numeric        -> REAL
--   * boolean        -> INTEGER 0/1 (NULL sigue siendo NULL = "no se sabe")
--   * jsonb          -> TEXT con JSON (bbox, vecinos_detalle)
--   * real[] vector  -> BLOB de float32 crudo (ver `vector_a_blob`)
--
-- El `candidato_id` NO es autoincrementable por casualidad: se conserva
-- INTEGER PRIMARY KEY para poder SEMBRAR un lote con los ids que ya existían
-- en Postgres (verificación de la Fase 2) sin que cambien los números.

-- ex `staging_tuc.raw_catalogo` — lo que se leyó del catálogo del proveedor
-- tal cual, antes de graduarlo a candidato.
CREATE TABLE IF NOT EXISTS candidato_raw (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    proveedor_id      INTEGER,
    catalogo_origen   TEXT NOT NULL,
    pagina            INTEGER,
    bbox              TEXT,
    codigo_ocr        TEXT,
    confianza_ocr     REAL,
    crop_imagen_path  TEXT,
    texto_crudo       TEXT,
    talla_min         INTEGER,
    talla_max         INTEGER,
    empaque_cantidad  INTEGER,
    empaque_unidad    TEXT,
    estado_revision   TEXT NOT NULL DEFAULT 'pendiente'
                      CHECK (estado_revision IN ('pendiente','aprobado','corregido','descartado')),
    fecha_ingesta     TEXT NOT NULL,
    cargado_en        TEXT NOT NULL,
    sha1_imagen       TEXT,
    tipo_declarado    TEXT,
    tipo_norm         TEXT,
    color_declarado   TEXT,
    color_familia     TEXT,
    marca_declarada   TEXT,
    costo             REAL,
    moneda_costo      TEXT
);
CREATE INDEX IF NOT EXISTS ix_candraw_llave
    ON candidato_raw(proveedor_id, codigo_ocr, catalogo_origen);

-- ex `staging_tuc.dim_candidato` — un producto del catálogo en revisión.
CREATE TABLE IF NOT EXISTS candidato (
    candidato_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    proveedor_id                INTEGER NOT NULL,
    codigo_proveedor            TEXT NOT NULL,
    catalogo_origen             TEXT NOT NULL,
    linea_declarada             TEXT,
    fecha_ingesta               TEXT NOT NULL,
    categoria_declarada         TEXT,
    grupo_declarado             TEXT,
    genero_declarado            TEXT,
    genero_canonico             TEXT,
    n_variantes_color           INTEGER,
    creado_en                   TEXT NOT NULL,
    es_multicolor               INTEGER,
    n_colores_detectados        INTEGER,
    colores_detectados          TEXT,
    posibles_mismo_modelo       TEXT,
    tiene_tacon                 INTEGER,
    tiene_plataforma            INTEGER,
    advertencia_multiples_pares INTEGER,
    color_familia_declarado     TEXT,
    categoria_visual            TEXT,
    categoria_conflicto         INTEGER NOT NULL DEFAULT 0,
    precio_venta_manual         REAL,
    UNIQUE (proveedor_id, codigo_proveedor, catalogo_origen)
);

-- ex `staging_tuc.dim_candidato_variante` — un PAR detectado dentro de la foto
-- (una referencia puede traer varios colores en una sola imagen).
CREATE TABLE IF NOT EXISTS candidato_variante (
    variante_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    candidato_id        INTEGER NOT NULL REFERENCES candidato(candidato_id) ON DELETE CASCADE,
    indice              INTEGER NOT NULL,
    bbox                TEXT,
    confianza_deteccion REAL,
    imagen_limpia_path  TEXT,
    color_principal     TEXT,
    color_suela         TEXT,
    color_suela_delta_e REAL,
    suela_contraste     INTEGER,
    detalle_flor_path   TEXT,
    cobertura_mascara   REAL,
    es_base             INTEGER DEFAULT 0,
    recon_estado        TEXT,
    recon_desde_indice  INTEGER,
    recon_cobertura     REAL,
    recon_path          TEXT,
    area_mascara_px     INTEGER,
    color_plantilla     TEXT,
    UNIQUE (candidato_id, indice)
);

-- ex `staging_tuc.fct_embedding_candidato` — el vector de imagen de cada
-- variante. Solo se usa como vector de CONSULTA contra el índice permanente
-- que sigue viviendo en Postgres (`silver.fct_embedding_imagen` y compañía):
-- nunca forma parte de ese índice, así que sacarlo de Postgres no degrada
-- ninguna búsqueda existente.
CREATE TABLE IF NOT EXISTS candidato_embedding (
    variante_id      INTEGER NOT NULL REFERENCES candidato_variante(variante_id) ON DELETE CASCADE,
    modelo_embedding TEXT NOT NULL,
    modelo_version   TEXT NOT NULL,
    dim              INTEGER NOT NULL,
    vector           BLOB NOT NULL,
    cargado_en       TEXT NOT NULL,
    PRIMARY KEY (variante_id, modelo_embedding)
);

-- ex `staging_tuc.agg_candidato_score` — el resultado de calificar.
CREATE TABLE IF NOT EXISTS candidato_score (
    candidato_id    INTEGER PRIMARY KEY REFERENCES candidato(candidato_id) ON DELETE CASCADE,
    demanda_proxy   REAL,
    tendencia_proxy REAL,
    margen_factor   REAL,
    n_vecinos       INTEGER,
    score_final     REAL,
    clasificacion   TEXT,
    fecha_calculo   TEXT NOT NULL,
    vecinos_detalle TEXT,
    factor_mercado  REAL,
    filtro_aplicado TEXT,
    sim_ponderada   REAL,
    k_efectivo      REAL,
    f_soporte       REAL,
    f_tipo          REAL,
    f_color         REAL,
    f_atrib         REAL,
    f_demanda       REAL,
    f_rotacion      REAL,
    f_venta         REAL,
    rotacion_proxy  REAL,
    venta_proxy     REAL,
    f_descuento     REAL,
    descuento_proxy REAL,
    version_formula TEXT
);

-- El score de CADA color (variante) de la referencia — 2026-09-17, pedido
-- explícito del dueño. El proveedor genérico vende por bulto: si una
-- referencia trae 3 colores, se compran los 3 o ninguno, así que el comprador
-- necesita ver qué tan bueno es cada color por separado y, como número de
-- compra, el PROMEDIO SIMPLE de los tres (decisión del dueño: todos los
-- colores pesan igual, NO se pondera por confianza/evidencia).
--
-- Por qué una tabla aparte y no cambiarle la llave a `candidato_score`:
-- `candidato_score` es la fila de la unidad de compra (la que lee la
-- promoción a Postgres, el desglose «¿por qué?» y toda la GUI) y sigue
-- significando exactamente lo mismo que antes. Esta tabla SUMA el detalle por
-- color sin tocar ni una lectura existente; para un candidato de un solo
-- color trae una sola fila con el mismo número de siempre.
CREATE TABLE IF NOT EXISTS candidato_score_variante (
    candidato_id    INTEGER NOT NULL REFERENCES candidato(candidato_id) ON DELETE CASCADE,
    indice          INTEGER NOT NULL,
    color_principal TEXT,
    score_final     REAL,
    clasificacion   TEXT,
    n_vecinos       INTEGER,
    sim_ponderada   REAL,
    filtro_aplicado TEXT,
    metodo_score    TEXT,
    fecha_calculo   TEXT NOT NULL,
    PRIMARY KEY (candidato_id, indice)
);

-- Las OTRAS fotos de la misma referencia (los demás colores del bulto) —
-- 2026-09-17. Hasta hoy `cargar_desde_carpeta` se quedaba con `fotos[0]` y
-- descartaba el resto: FOR-025/FOR-027/FOR-035 llegaban con 3 fotos cada una
-- (un color por foto) y el lote terminaba con 1 sola variante por referencia,
-- así que los otros dos colores no existían en la base y no había NADA que
-- calificar. Acá se guardan esas fotos extra para que la vectorización cree
-- una variante real por cada una.
CREATE TABLE IF NOT EXISTS candidato_foto_extra (
    raw_id           INTEGER NOT NULL REFERENCES candidato_raw(id) ON DELETE CASCADE,
    orden            INTEGER NOT NULL,
    crop_imagen_path TEXT NOT NULL,
    PRIMARY KEY (raw_id, orden)
);
"""


def ruta(salida: Path | str) -> Path:
    return Path(salida) / ARCHIVO


def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _j(valor) -> str:
    return json.dumps(valor, ensure_ascii=False)


def _dej(texto, default=None):
    if not texto:
        return default
    try:
        return json.loads(texto)
    except (TypeError, json.JSONDecodeError):
        return default


_COLUMNAS_NUEVAS_CANDIDATO_SCORE = ("f_rotacion", "f_venta", "rotacion_proxy", "venta_proxy",
                                     "f_descuento", "descuento_proxy")


def _migrar_columnas_candidato_score(con) -> None:
    """Plan 2026-09-21: rotación (`% Rotación` real, gold.agg_tuc_metricas),
    `factor_venta` y `pct_sobre_lista` (precio_avg/precio_lista, "Artículos
    por precio" del Power BI) se conectan al score como factores objetivos
    más, igual que `f_demanda` -- `CREATE TABLE IF NOT EXISTS` no agrega
    columnas a una tabla que ya existe, así que un `lote.sqlite` creado ANTES
    de este cambio necesita el ALTER explícito. Idempotente: se fija con
    `PRAGMA table_info` antes de agregar cada columna, así que correr esto
    en cada apertura no falla ni duplica nada."""
    existentes = {fila[1] for fila in con.execute("PRAGMA table_info(candidato_score)")}
    for columna in _COLUMNAS_NUEVAS_CANDIDATO_SCORE:
        if columna not in existentes:
            con.execute(f"ALTER TABLE candidato_score ADD COLUMN {columna} REAL")


@contextmanager
def abrir(salida: Path | str, crear: bool = True):
    """Conexión al `lote.sqlite` de esta carpeta de trabajo.

    Con `crear=False` no toca el disco si el archivo no existe: sirve para los
    lectores que tienen que poder decir "todavía no hay nada" sin dejar un
    archivo vacío en una carpeta que el usuario apenas está mirando.
    """
    p = ruta(salida)
    if not crear and not p.exists():
        # Una carpeta vieja (con los .jsonl sueltos y sin `lote.sqlite`) SÍ se
        # abre y se migra, aunque el que llame sea un lector: la migración
        # tiene que pasar sola la primera vez que se toca la carpeta, no
        # depender de que el primer acceso resulte ser una escritura.
        if not _hay_jsonl_viejos(Path(salida)):
            yield None
            return
    nuevo = not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=20.0, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=20000")
        con.execute("PRAGMA foreign_keys=ON")
        if nuevo:
            con.executescript(_ESQUEMA)
            con.execute("INSERT INTO esquema (version, creado) VALUES (?, ?)",
                        (VERSION_ESQUEMA, _ahora()))
            _migrar_desde_jsonl(con, Path(salida))
        else:
            # Una carpeta que ya tenía `lote.sqlite` de una versión anterior
            # del programa: crear lo que falte es idempotente y barato.
            con.executescript(_ESQUEMA)
        _migrar_columnas_candidato_score(con)
        yield con
    finally:
        con.close()


def existe(salida: Path | str) -> bool:
    return ruta(salida).exists()


def _hay_jsonl_viejos(salida: Path) -> bool:
    try:
        for patron in JSONL_VIEJOS:
            if "*" in patron:
                if next(salida.glob(patron), None) is not None:
                    return True
            elif (salida / patron).exists():
                return True
    except OSError:
        return False
    return False


def asegurar(salida: Path | str) -> Path:
    """Crea (y migra, si hace falta) el `lote.sqlite` de esta carpeta.

    Punto de entrada explícito para cuando el programa decide "esta es la
    carpeta de trabajo": deja el archivo listo y la migración hecha antes de
    que cualquier pantalla empiece a leer.
    """
    with abrir(salida) as _con:
        pass
    return ruta(salida)


def hay_lote(salida: Path | str) -> bool:
    """¿Esta carpeta es una carpeta de trabajo con un lote adentro?

    Reemplaza al viejo `list(salida.glob("manifiesto_*.jsonl"))`: incluye
    también las carpetas viejas que todavía no se han migrado, para que la
    migración pueda dispararse al abrirlas.
    """
    salida = Path(salida)
    if ruta(salida).exists():
        return True
    return bool(list(salida.glob("manifiesto_*.jsonl")))


# ── metadatos del lote (ex `_lote_asistente.json`) ───────────────────────────


def leer_meta(salida: Path | str) -> dict:
    with abrir(salida, crear=False) as con:
        if con is None:
            return _leer_meta_json_viejo(salida)
        filas = con.execute("SELECT clave, valor_json FROM lote_meta").fetchall()
    return {f["clave"]: _dej(f["valor_json"]) for f in filas}


def _leer_meta_json_viejo(salida: Path | str) -> dict:
    """Último recurso: la carpeta no tiene `lote.sqlite` todavía.

    Pasa cuando alguien pregunta por los metadatos de una carpeta que solo se
    está inspeccionando (no se abrió como lote), y no se quiere crear el
    archivo SQLite solo por leer.
    """
    try:
        return json.loads((Path(salida) / "_lote_asistente.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def guardar_meta(salida: Path | str, **campos) -> None:
    """Anota datos del lote sin pisar los demás (UPSERT por clave).

    Es la misma semántica de `merge` que tenía el JSON, pero atómica: si dos
    pantallas anotan dos claves distintas a la vez, ya no hay una que gane
    reescribiendo el archivo completo con su copia vieja del resto.
    """
    if not campos:
        return
    with abrir(salida) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for clave, valor in campos.items():
                con.execute(
                    "INSERT INTO lote_meta (clave, valor_json, actualizado) VALUES (?, ?, ?) "
                    "ON CONFLICT(clave) DO UPDATE SET valor_json=excluded.valor_json, "
                    "actualizado=excluded.actualizado",
                    (clave, _j(valor), _ahora()))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


# ── láminas y recortes (ex `manifiesto_*.jsonl`) ─────────────────────────────

# Campos del recorte que tienen columna propia. El resto de las claves que
# traiga el registro van a `extra_json`, así que agregar un campo nuevo en
# `pasos.py` no pierde el dato aunque nadie se acuerde de tocar el esquema.
_COLS_RECORTE = {
    "indice": "indice", "score": "score", "etiqueta": "etiqueta",
    "cobertura_alfa": "cobertura_alfa", "vecino_mascara": "vecino_mascara",
    "fondo_quitado_px": "fondo_quitado_px", "png": "png", "jpg": "jpg",
}
_COLS_LAMINA = {
    "origen": "origen", "detecciones": "detecciones", "matting": "matting",
    "ms": "ms", "version_pipeline": "version_pipeline", "error": "error",
}
_CAMPOS_ESTADO = ("falta_pct", "mordida_real", "sombra_outlier",
                  "sombra_corregida_px", "tiene_excedente", "decision")


def guardar_lamina(salida: Path | str, registro: dict, worker_id: int = 0) -> None:
    """Guarda una lámina procesada y sus recortes, en UNA transacción.

    Reemplaza el `mf.write(json.dumps(registro))` del worker. La diferencia que
    importa: antes, si el proceso moría entre escribir la línea y hacer flush,
    quedaba media línea que el lector descartaba entera — se perdían los
    recortes buenos de esa lámina. Acá o entra la lámina completa con todos sus
    recortes, o no entra nada.

    Es idempotente por `sha1`: reprocesar la misma lámina reemplaza su registro
    en vez de dejar dos versiones y que el lector adivine cuál vale (el viejo
    manifiesto acumulaba las dos líneas).
    """
    sha1 = registro.get("sha1")
    if not sha1:
        raise ValueError("el registro de la lámina no trae sha1")

    tamano = registro.get("tamano") or [None, None]
    extra_lamina = {k: v for k, v in registro.items()
                    if k not in set(_COLS_LAMINA) | {"sha1", "tamano", "recortes",
                                                     "revisar", "revision_manual"}}

    with abrir(salida) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM recorte WHERE sha1 = ?", (sha1,))
            con.execute(
                "INSERT INTO lamina (sha1, origen, ancho, alto, detecciones, matting, "
                "  revisar_json, revision_manual, ms, version_pipeline, error, worker_id, "
                "  cuando, extra_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sha1) DO UPDATE SET "
                "  origen=excluded.origen, ancho=excluded.ancho, alto=excluded.alto, "
                "  detecciones=excluded.detecciones, matting=excluded.matting, "
                "  revisar_json=excluded.revisar_json, revision_manual=excluded.revision_manual, "
                "  ms=excluded.ms, version_pipeline=excluded.version_pipeline, "
                "  error=excluded.error, worker_id=excluded.worker_id, "
                "  cuando=excluded.cuando, extra_json=excluded.extra_json",
                (sha1, registro.get("origen"), tamano[0], tamano[1],
                 registro.get("detecciones"), registro.get("matting"),
                 _j(registro.get("revisar") or []),
                 int(bool(registro.get("revision_manual"))),
                 registro.get("ms"), registro.get("version_pipeline"),
                 registro.get("error"), worker_id, _ahora(),
                 _j(extra_lamina) if extra_lamina else None))

            for r in registro.get("recortes") or []:
                _insertar_recorte(con, sha1, r)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def _insertar_recorte(con, sha1: str, r: dict) -> None:
    est = r.get("estado") or {}
    nombre = est.get("nombre") or (Path(r["png"]).name if r.get("png") else None)
    conocidas = set(_COLS_RECORTE) | {"caja", "colores", "revisar", "estado",
                                      "vecino_quitado"}
    extra = {k: v for k, v in r.items() if k not in conocidas}
    con.execute(
        "INSERT INTO recorte (sha1, indice, nombre, png, jpg, score, etiqueta, caja_json, "
        "  cobertura_alfa, vecino_quitado, vecino_mascara, fondo_quitado_px, colores_json, "
        "  revisar_json, estado_presente, falta_pct, mordida_real, sombra_outlier, "
        "  sombra_corregida_px, tiene_excedente, decision_al_procesar, extra_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha1, r.get("indice"), nombre, r.get("png"), r.get("jpg"), r.get("score"),
         r.get("etiqueta"), _j(r.get("caja")) if r.get("caja") is not None else None,
         r.get("cobertura_alfa"),
         None if r.get("vecino_quitado") is None else int(bool(r.get("vecino_quitado"))),
         r.get("vecino_mascara"), r.get("fondo_quitado_px"),
         _j(r.get("colores")) if r.get("colores") is not None else None,
         _j(r.get("revisar") or []),
         1 if est else 0,
         est.get("falta_pct"),
         None if est.get("mordida_real") is None else int(bool(est.get("mordida_real"))),
         None if est.get("sombra_outlier") is None else int(bool(est.get("sombra_outlier"))),
         est.get("sombra_corregida_px"),
         None if est.get("tiene_excedente") is None else int(bool(est.get("tiene_excedente"))),
         est.get("decision"),
         _j(extra) if extra else None))


def _fila_a_recorte(f: sqlite3.Row) -> dict:
    """Reconstruye el dict de recorte tal como lo espera la interfaz.

    La interfaz consume (y muta) este dict en decenas de lugares; devolver la
    misma forma que traía el manifiesto es lo que permite cambiar el
    almacenamiento sin tocar la lógica de negocio.
    """
    r: dict = {
        "indice": f["indice"],
        "score": f["score"],
        "etiqueta": f["etiqueta"],
        "cobertura_alfa": f["cobertura_alfa"],
        "vecino_mascara": f["vecino_mascara"],
        "fondo_quitado_px": f["fondo_quitado_px"],
        "caja": _dej(f["caja_json"]),
        "colores": _dej(f["colores_json"]),
        "revisar": _dej(f["revisar_json"], []) or [],
    }
    if f["vecino_quitado"] is not None:
        r["vecino_quitado"] = bool(f["vecino_quitado"])
    if f["png"]:
        r["png"] = f["png"]
    if f["jpg"]:
        r["jpg"] = f["jpg"]
    if f["estado_presente"]:
        r["estado"] = {
            "nombre": f["nombre"],
            "falta_pct": f["falta_pct"],
            "mordida_real": bool(f["mordida_real"]),
            "sombra_outlier": bool(f["sombra_outlier"]),
            "sombra_corregida_px": f["sombra_corregida_px"],
            "tiene_excedente": bool(f["tiene_excedente"]),
            "decision": f["decision_al_procesar"],
        }
    extra = _dej(f["extra_json"], {}) or {}
    r.update(extra)
    return r


def cargar_recortes(salida: Path | str) -> dict[str, dict]:
    """Los recortes vigentes del lote, indexados por nombre.

    Aplica de una vez los tres filtros/cruces que antes estaban repartidos (y
    duplicados) entre `_recargar_manifiesto` y `_contar_recortes_en`:
    solo recortes con imagen y estado medido, sin los eliminados del disco, y
    con la decisión VIGENTE de la bitácora (no la que quedó congelada al
    procesar). Ahora es una consulta, no un criterio que hay que recordar
    mantener sincronizado en dos lugares.
    """
    with abrir(salida, crear=False) as con:
        if con is None:
            return {}
        filas = con.execute(
            "SELECT * FROM vw_recorte_vigente "
            "WHERE png IS NOT NULL AND estado_presente = 1 "
            "  AND COALESCE(decision, '') <> 'eliminado' "
            "ORDER BY nombre").fetchall()
    salida_dict: dict[str, dict] = {}
    for f in filas:
        r = _fila_a_recorte(f)
        r["_origen_lamina"] = f["origen_lamina"]
        if f["decision"]:
            r["estado"]["decision"] = f["decision"]
        salida_dict[f["nombre"]] = r
    return salida_dict


def contar_recortes(salida: Path | str) -> int:
    """Cuántos recortes vigentes tiene el lote, sin cargar nada en memoria.

    Antes esto era una función aparte que duplicaba a mano el criterio de
    `_recargar_manifiesto` (con un comentario avisando que había que
    actualizar las dos juntas). Ahora las dos salen del mismo `WHERE`.
    """
    with abrir(salida, crear=False) as con:
        if con is None:
            return 0
        return con.execute(
            "SELECT COUNT(*) FROM vw_recorte_vigente "
            "WHERE png IS NOT NULL AND estado_presente = 1 "
            "  AND COALESCE(decision, '') <> 'eliminado'").fetchone()[0]


def huellas(salida: Path | str, version_pipeline: int) -> set[str]:
    """Láminas ya procesadas CON esta versión del pipeline (ver nucleo.py)."""
    with abrir(salida, crear=False) as con:
        if con is None:
            return set()
        return {f[0] for f in con.execute(
            "SELECT sha1 FROM lamina WHERE version_pipeline = ?",
            (version_pipeline,)).fetchall()}


# ── decisiones (ex `decisiones.jsonl`) ───────────────────────────────────────


def decisiones_vigentes(salida: Path | str) -> dict[str, dict]:
    with abrir(salida, crear=False) as con:
        if con is None:
            return {}
        filas = con.execute("SELECT * FROM vw_decision_vigente").fetchall()
    return {f["recorte"]: {"recorte": f["recorte"], "decision": f["decision"],
                           "motivos_qa": _dej(f["motivos_qa_json"], []) or [],
                           "nota": f["nota"], "cuando": f["cuando"]}
            for f in filas}


def registrar_decision(salida: Path | str, recorte: str, decision: str,
                       motivos_qa: list[str] | None = None, nota: str = "") -> dict:
    reg = {"recorte": recorte, "decision": decision,
           "motivos_qa": motivos_qa or [], "nota": nota, "cuando": _ahora()}
    with abrir(salida) as con:
        con.execute(
            "INSERT INTO decision (recorte, decision, motivos_qa_json, nota, cuando) "
            "VALUES (?,?,?,?,?)",
            (recorte, decision, _j(reg["motivos_qa"]), nota, reg["cuando"]))
    return reg


def purgar_decisiones_stem(salida: Path | str, stem: str) -> int:
    with abrir(salida, crear=False) as con:
        if con is None:
            return 0
        cur = con.execute("DELETE FROM decision WHERE recorte LIKE ? ESCAPE '\\'",
                          (_como_prefijo(stem),))
        return cur.rowcount or 0


def _como_prefijo(stem: str) -> str:
    """`stem` como patrón LIKE de prefijo `stem_`, con los comodines escapados.

    Sin escapar, una lámina llamada `100%_algodon` haría que `%` actuara como
    comodín y purgara decisiones de recortes que no son de esa lámina.
    """
    seguro = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{seguro}\\_%"


# ── reparaciones (ex `reparaciones.jsonl`) ───────────────────────────────────


def reparaciones_vigentes(salida: Path | str) -> dict[str, dict]:
    with abrir(salida, crear=False) as con:
        if con is None:
            return {}
        filas = con.execute(
            "SELECT p.* FROM reparacion p "
            "JOIN (SELECT recorte, MAX(id) AS id FROM reparacion GROUP BY recorte) v "
            "  ON v.id = p.id").fetchall()
    out: dict[str, dict] = {}
    for f in filas:
        reg = {"recorte": f["recorte"], "metodo": f["metodo"],
               "respaldos": _dej(f["respaldos_json"], {}) or {},
               "cuando": f["cuando"]}
        reg.update(_dej(f["datos_json"], {}) or {})
        out[f["recorte"]] = reg
    return out


def registrar_reparacion(salida: Path | str, recorte: str, metodo: str,
                         respaldos: dict | None = None, **datos) -> dict:
    reg = {"recorte": recorte, "metodo": metodo, "respaldos": respaldos or {},
           "cuando": _ahora(), **datos}
    with abrir(salida) as con:
        con.execute(
            "INSERT INTO reparacion (recorte, metodo, respaldos_json, datos_json, cuando) "
            "VALUES (?,?,?,?,?)",
            (recorte, metodo, _j(reg["respaldos"]), _j(datos), reg["cuando"]))
    return reg


def purgar_reparaciones_stem(salida: Path | str, stem: str) -> tuple[int, list[str]]:
    """Borra las reparaciones de una lámina y devuelve los respaldos a eliminar.

    Los archivos NO se borran acá: quién decide borrar píxeles del disco es
    `reparar.py`, y este módulo solo maneja el registro. Devolver las rutas en
    vez de borrarlas mantiene esa separación (y hace la función testeable sin
    tocar archivos).
    """
    with abrir(salida, crear=False) as con:
        if con is None:
            return 0, []
        patron = _como_prefijo(stem)
        filas = con.execute(
            "SELECT respaldos_json FROM reparacion WHERE recorte LIKE ? ESCAPE '\\'",
            (patron,)).fetchall()
        rutas = [ruta_r for f in filas
                 for ruta_r in (_dej(f["respaldos_json"], {}) or {}).values()]
        cur = con.execute("DELETE FROM reparacion WHERE recorte LIKE ? ESCAPE '\\'",
                          (patron,))
        return cur.rowcount or 0, rutas


# ── plan de trabajo (ex `plan_*.jsonl`) ──────────────────────────────────────


def escribir_plan(salida: Path | str, pendientes: list[tuple[Path, str]],
                  n_procesos: int) -> list[int]:
    """Reparte las láminas pendientes entre N procesos y devuelve sus ids.

    Se reparte alternando (0,1,2,0,1,2...) para que ningún proceso quede con
    todas las láminas grandes por accidente de orden alfabético — igual que
    hacía `nucleo.escribir_planes` con los archivos.
    """
    with abrir(salida) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM plan")
            for i in range(n_procesos):
                trozo = pendientes[i::n_procesos]
                con.executemany(
                    "INSERT INTO plan (worker_id, orden, path, sha1) VALUES (?,?,?,?)",
                    [(i, orden, str(path), sha1)
                     for orden, (path, sha1) in enumerate(trozo)])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return list(range(n_procesos))


def leer_plan(salida: Path | str, worker_id: int) -> list[tuple[Path, str]]:
    with abrir(salida, crear=False) as con:
        if con is None:
            return []
        filas = con.execute(
            "SELECT path, sha1 FROM plan WHERE worker_id = ? ORDER BY orden",
            (worker_id,)).fetchall()
    return [(Path(f["path"]), f["sha1"]) for f in filas]


# ── vaciar el lote ───────────────────────────────────────────────────────────


def vaciar(salida: Path | str) -> None:
    """Deja el lote sin nada procesado, sin borrar el archivo.

    Se vacía por tablas en vez de borrar `lote.sqlite` porque la carpeta de
    trabajo puede estar abierta por la interfaz: borrar el archivo debajo de
    una conexión viva de Windows falla o deja los sidecars `-wal`/`-shm`
    huérfanos.
    """
    with abrir(salida, crear=False) as con:
        if con is None:
            return
        con.execute("BEGIN IMMEDIATE")
        try:
            for tabla in ("recorte", "lamina", "decision", "reparacion", "plan",
                          "lote_meta"):
                con.execute(f"DELETE FROM {tabla}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


# ── migración de los archivos sueltos ────────────────────────────────────────


def _lineas_json(path: Path):
    """Registros de un .jsonl, salteando líneas ilegibles (las hay: una corrida
    matada a mitad deja la última línea truncada)."""
    try:
        texto = path.read_text(encoding="utf-8")
    except OSError:
        return
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            yield json.loads(linea)
        except json.JSONDecodeError:
            continue


def _migrar_desde_jsonl(con, salida: Path) -> dict[str, int]:
    """Trae a SQLite lo que haya en los archivos sueltos de una carpeta vieja.

    Corre UNA vez, dentro de la creación del `lote.sqlite` (así que no hay
    ventana en la que dos procesos migren a la vez: el que pierde la carrera
    encuentra el archivo ya creado). Los archivos viejos quedan intactos en
    disco — esto es una migración con red de seguridad, no una reescritura.
    """
    conteo: dict[str, int] = {}

    # 1. metadatos del lote
    meta = _leer_meta_json_viejo(salida)
    for clave, valor in meta.items():
        con.execute(
            "INSERT INTO lote_meta (clave, valor_json, actualizado) VALUES (?,?,?) "
            "ON CONFLICT(clave) DO UPDATE SET valor_json=excluded.valor_json",
            (clave, _j(valor), _ahora()))
    if meta:
        conteo["_lote_asistente.json"] = len(meta)

    # 2. manifiestos: una lámina por línea, con sus recortes anidados
    laminas = recortes = 0
    for manifiesto in sorted(salida.glob("manifiesto_*.jsonl")):
        try:
            worker_id = int(manifiesto.stem.split("_")[-1])
        except ValueError:
            worker_id = 0
        for reg in _lineas_json(manifiesto):
            sha1 = reg.get("sha1")
            if not sha1:
                # Manifiesto anterior a la reanudación por huella: sin sha1 no
                # hay identidad estable, se deriva de la ruta de origen para no
                # perder la lámina.
                sha1 = f"sinsha1:{reg.get('origen', '')}"
            tamano = reg.get("tamano") or [None, None]
            extra = {k: v for k, v in reg.items()
                     if k not in set(_COLS_LAMINA) | {"sha1", "tamano", "recortes",
                                                      "revisar", "revision_manual"}}
            con.execute("DELETE FROM recorte WHERE sha1 = ?", (sha1,))
            con.execute(
                "INSERT INTO lamina (sha1, origen, ancho, alto, detecciones, matting, "
                "  revisar_json, revision_manual, ms, version_pipeline, error, worker_id, "
                "  cuando, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sha1) DO UPDATE SET origen=excluded.origen, "
                "  ancho=excluded.ancho, alto=excluded.alto, detecciones=excluded.detecciones, "
                "  matting=excluded.matting, revisar_json=excluded.revisar_json, "
                "  revision_manual=excluded.revision_manual, ms=excluded.ms, "
                "  version_pipeline=excluded.version_pipeline, error=excluded.error, "
                "  worker_id=excluded.worker_id, extra_json=excluded.extra_json",
                (sha1, reg.get("origen"), tamano[0], tamano[1], reg.get("detecciones"),
                 reg.get("matting"), _j(reg.get("revisar") or []),
                 int(bool(reg.get("revision_manual"))), reg.get("ms"),
                 reg.get("version_pipeline"), reg.get("error"), worker_id, _ahora(),
                 _j(extra) if extra else None))
            laminas += 1
            for i, r in enumerate(reg.get("recortes") or [], start=1):
                if r.get("indice") is None:
                    r = dict(r, indice=i)
                _insertar_recorte(con, sha1, r)
                recortes += 1
    if laminas:
        conteo["manifiesto_*.jsonl"] = laminas
        conteo["recortes"] = recortes

    # 3. decisiones: bitácora completa, en orden, para no perder la historia
    n = 0
    for reg in _lineas_json(salida / "decisiones.jsonl"):
        if not reg.get("recorte"):
            continue
        con.execute(
            "INSERT INTO decision (recorte, decision, motivos_qa_json, nota, cuando) "
            "VALUES (?,?,?,?,?)",
            (reg["recorte"], reg.get("decision", "pendiente"),
             _j(reg.get("motivos_qa") or []), reg.get("nota") or "",
             reg.get("cuando") or _ahora()))
        n += 1
    if n:
        conteo["decisiones.jsonl"] = n

    # 4. reparaciones
    n = 0
    for reg in _lineas_json(salida / "reparaciones.jsonl"):
        if not reg.get("recorte"):
            continue
        datos = {k: v for k, v in reg.items()
                 if k not in ("recorte", "metodo", "respaldos", "cuando")}
        con.execute(
            "INSERT INTO reparacion (recorte, metodo, respaldos_json, datos_json, cuando) "
            "VALUES (?,?,?,?,?)",
            (reg["recorte"], reg.get("metodo") or "desconocido",
             _j(reg.get("respaldos") or {}), _j(datos), reg.get("cuando") or _ahora()))
        n += 1
    if n:
        conteo["reparaciones.jsonl"] = n

    # 5. planes pendientes
    n = 0
    for plan_path in sorted(salida.glob("plan_*.jsonl")):
        try:
            worker_id = int(plan_path.stem.split("_")[-1])
        except ValueError:
            worker_id = 0
        for orden, reg in enumerate(_lineas_json(plan_path)):
            if not reg.get("path"):
                continue
            con.execute(
                "INSERT OR REPLACE INTO plan (worker_id, orden, path, sha1) VALUES (?,?,?,?)",
                (worker_id, orden, reg["path"], reg.get("sha1") or ""))
            n += 1
    if n:
        conteo["plan_*.jsonl"] = n

    for archivo, filas in conteo.items():
        con.execute("INSERT INTO migracion (archivo, filas, cuando) VALUES (?,?,?)",
                    (archivo, filas, _ahora()))
    return conteo


def resumen_migracion(salida: Path | str) -> list[dict]:
    """Qué trajo la migración de esta carpeta (para verificarla sin adivinar)."""
    with abrir(salida, crear=False) as con:
        if con is None:
            return []
        return [dict(f) for f in con.execute(
            "SELECT archivo, filas, cuando FROM migracion ORDER BY rowid").fetchall()]


# ── revisión de catálogo: conexión y conversores (Fase 2) ────────────────────
#
# `abrir()` es un contextmanager que CIERRA al salir: perfecto para las
# lecturas/escrituras puntuales del pipeline de imágenes, pero el motor de
# calificación consulta este archivo decenas de veces dentro de un mismo
# cálculo (una vez por candidato, y varias por candidato). Para ese uso
# `conexion()` entrega una conexión ABIERTA, con el esquema ya asegurado, que
# el llamador conserva mientras trabaja y cierra al terminar. Es el mismo
# archivo y el mismo esquema -- no un mecanismo paralelo.


def conexion(salida: Path | str) -> sqlite3.Connection:
    """Conexión abierta al `lote.sqlite` de esta carpeta, esquema al día.

    A diferencia de `abrir()`, NO la cierra sola: la cierra quien la pide.
    Mismos PRAGMA que `abrir()` para que el comportamiento (WAL, timeouts,
    claves foráneas) sea idéntico por los dos caminos.
    """
    with abrir(salida) as _con:  # crea/migra el archivo y asegura el esquema
        pass
    con = sqlite3.connect(str(ruta(salida)), timeout=20.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ahora() -> str:
    """Marca de tiempo en el mismo formato que usa el resto del archivo.

    Reemplaza al `now()` de Postgres en las tablas de revisión de catálogo:
    SQLite no tiene `timestamptz`, así que la hora la pone Python (igual que
    ya hacía este módulo para `lamina.cuando`, `decision.cuando`, etc.).
    """
    return _ahora()


def vector_a_blob(vector) -> bytes:
    """Vector de embedding (lista/array de floats) -> BLOB de float32 crudo.

    Se elige BLOB sobre JSON de floats por dos razones concretas: ocupa 4
    bytes por dimensión en vez de ~20 (un vector de 1024d pasa de ~20 KB de
    texto a 4 KB), y NO pierde precisión al ida-y-vuelta -- `float32` entra y
    `float32` sale, bit por bit, que es exactamente lo que guardaba el
    `real[]` de Postgres. Un JSON de floats obligaría a decidir cuántos
    decimales imprimir, y cualquier recorte ahí movería las similitudes
    coseno del motor de calificación.
    """
    import numpy as np  # local: almacen.py no depende de numpy para nada más
    return np.asarray(vector, dtype=np.float32).tobytes()


def blob_a_vector(blob):
    """BLOB de float32 -> lista de floats de Python.

    Devuelve `list`, no `numpy.ndarray`, para que el motor reciba EXACTAMENTE
    la misma forma que le daba psycopg2 con una columna `real[]` (una lista) y
    ninguna de las llamadas de más arriba tenga que cambiar.
    """
    import numpy as np
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32).tolist()


def booleano(v):
    """INTEGER 0/1/NULL de SQLite -> True/False/None de Python.

    SQLite no tiene `boolean`: sin esta conversión el motor recibiría `0` donde
    Postgres le daba `False`, y aunque los dos son falsos, `0 != False` para
    cualquier comparación estricta (la del comparador de snapshots, entre
    otras). NULL se conserva como None: en estas tablas "no se sabe" es un
    valor legítimo, distinto de "no".
    """
    return None if v is None else bool(v)
