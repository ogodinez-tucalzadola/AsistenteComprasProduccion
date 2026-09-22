"""
motor_candidatos.py
====================
Lógica de candidatos (detección de calzado, aislamiento de fondo, embeddings,
comparación visual, calificación de compra) para la aplicación de escritorio
AsistenteComprasEscritorio -- llamada DIRECTA desde la GUI, sin servidor web
de por medio.

No reimplementa nada: importa las funciones ya construidas y validadas del motor
de calificación, que desde la FASE 1 de la migración (09/09/2026) viven en
`motor_calificacion.py`, aquí mismo, en copia FIEL letra por letra de lo que
antes se importaba de `servidor_pty.py` (proyecto GestionTUC, compartido con la
herramienta web). Ese archivo web ya no se toca ni se importa desde acá.

La parte de "un producto = un código, una foto" (resolver carpeta, código
base, metadatos, avisos de duplicados) se reutiliza igual desde
`ingestar_limpios_tuc.py`, en vez de reescribirla aquí -- es la misma regla
de negocio, ya probada con datos reales el 03/09/2026.

Este módulo NO sabe nada de Tkinter/CustomTkinter -- recibe datos simples
(carpeta, proveedor_id) y devuelve datos simples (listas/diccionarios) más
mensajes de progreso vía un callback opcional, para que la GUI los muestre
como quiera.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

import psycopg2.extras

_PIPELINE_DIR = Path(r"C:\Users\Tucalzado\Proyectos\GestionTUC\pipeline")
sys.path.insert(0, str(_PIPELINE_DIR))

import motor_calificacion as _motor_calif_mod  # noqa: E402  -- para leer _vectorizador (¿ya cargado?)
from motor_calificacion import (  # noqa: E402
    CARPETA_CROPS,
    DATOS_COMPARTIDOS,
    conn as conectar,
    _graduar_candidatos_aprobados,
    vectorizar_y_puntuar_candidatos,
    api_candidatos,
    _progreso as _progreso_pty,
    categorizar_candidatos,
    inferir_genero_por_talla,
    derivar_estructura_calzado,
    detectar_mismo_modelo_otro_color,
    puntuar_candidatos,
    api_similar_marca,
    api_similar_tuc,
    api_pedido_sugerido,
    api_asignar_categoria,
    api_guardar_cotizacion,
    api_fijar_precio_manual,
    api_marcas_reconocidas,
    api_fijar_marca_declarada,
    precio_venta_referencia,
    api_desglose_score,
    _vaciar_revision,
    fijar_lote,          # FASE 2: declarar cuál lote es el activo
    lote_activo,
    canal_venta_lote,    # "marca" / "tuc": para qué canal es la compra del lote
    CANALES_VENTA,
    _cl,                 # cursor al `lote.sqlite` del lote activo
    _en,                 # placeholders `(?,?,?)` para un IN
)
import almacen  # noqa: E402  -- conversores de vector/booleano del lote.sqlite
from ingestar_limpios_tuc import (  # noqa: E402
    codigo_base,
    resolver_carpeta_imagenes,
    recolectar,
    cargar_metadatos,
    _entero,
    PATRON_SIN_CODIGO,
    PATRON_SUFIJO_DUPLICADO,
    resolver_sospechosos_con_metadatos,
)


_CONFIG_PATH = Path(__file__).resolve().parent / "config_asistente_compras.json"


def _cargar_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _guardar_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


MODELO_ACTIVO_DEFAULT = "ti"  # pedido explícito del usuario 2026-09-03: TI por defecto,
# los otros dos (fashion_siglip+dino_v2, clave "estandar") quedan como alternativa.
# "fashion" y "dino" (2026-09-16, pedido explícito del dueño): cada componente
# del par estándar usable POR SEPARADO, sin fusionar. Medido sobre los 18
# candidatos reales de Prueba2 contra el catálogo TUC, la fusión 50/50 de
# "estandar" rinde PEOR que sus dos componentes solos (promedia una señal
# fuerte con una débil y el promedio cae bajo el umbral más seguido que
# cualquiera de los dos). "estandar" se conserva igual: el dueño pidió agregar,
# no eliminar. No hay costo de cálculo: los dos vectores ya se guardan por
# separado en `candidato_embedding` -- ver `MODELO_UNICO` en motor_calificacion.
MODELOS_ACTIVOS_VALIDOS = ("ti", "fashion", "dino", "estandar")


def modelo_activo_configurado() -> str:
    """Método de vectorización que usa esta app por defecto -- persistido en
    `config_asistente_compras.json` para que la elección sobreviva entre
    sesiones. `ti` = dinov2_ti contra los vectores reales de TI
    (`silver.fct_embedding_ti`); `fashion` = solo fashion_siglip; `dino` = solo
    dino_v2; `estandar` = fashion_siglip+dino_v2 fusionados 50/50, el mismo par
    que usa GestionTUC/PTY."""
    modelo = _cargar_config().get("modelo_activo", MODELO_ACTIVO_DEFAULT)
    return modelo if modelo in MODELOS_ACTIVOS_VALIDOS else MODELO_ACTIVO_DEFAULT


def establecer_modelo_activo(modelo: str) -> None:
    if modelo not in MODELOS_ACTIVOS_VALIDOS:
        raise ValueError(f"modelo_activo inválido: {modelo!r} (válidos: {MODELOS_ACTIVOS_VALIDOS})")
    cfg = _cargar_config()
    cfg["modelo_activo"] = modelo
    _guardar_config(cfg)


def estimar_tiempo(n_candidatos: int, modelo_activo: str | None = None) -> tuple[float | None, int]:
    """Segundos totales estimados para vectorizar `n_candidatos` CON EL MÉTODO
    ACTIVO, a partir del promedio real de corridas anteriores en ESTA máquina
    -- o `(None, 0)` si todavía no hay ninguna corrida medida con ese método.
    Nunca se inventa un número: sin historial, es mejor decir "sin estimado"
    que mostrar un tiempo falso. El historial se guarda POR MÉTODO -- TI y el
    par estándar tienen costos de cómputo distintos, mezclarlos daría un
    estimado sin sentido al cambiar de uno a otro."""
    modelo_activo = modelo_activo or modelo_activo_configurado()
    hist = _cargar_config().get("historial", {}).get(modelo_activo, {})
    seg_por_candidato = hist.get("seg_por_candidato")
    muestras = hist.get("muestras", 0)
    if not seg_por_candidato or not muestras or n_candidatos <= 0:
        return None, 0
    return seg_por_candidato * n_candidatos, muestras


def _registrar_tiempo_real(n_candidatos: int, segundos: float, modelo_activo: str | None = None) -> None:
    """Promedio móvil sobre las últimas corridas CON ESTE método: si el
    hardware o el tamaño típico de lote cambia, el estimado se va ajustando
    solo, sin que una corrida atípica (ej. 1 sola foto, o la primera con
    carga de modelos incluida) lo desvíe de golpe."""
    if n_candidatos <= 0 or segundos <= 0:
        return
    modelo_activo = modelo_activo or modelo_activo_configurado()
    cfg = _cargar_config()
    historial = cfg.setdefault("historial", {})
    hist = historial.setdefault(modelo_activo, {})
    nuevo = segundos / n_candidatos
    anterior = hist.get("seg_por_candidato")
    muestras = hist.get("muestras", 0)
    if anterior and muestras:
        peso = min(muestras, 5)
        promedio = (anterior * peso + nuevo) / (peso + 1)
    else:
        promedio = nuevo
    hist["seg_por_candidato"] = promedio
    hist["muestras"] = muestras + 1
    _guardar_config(cfg)


class ResultadoCarga:
    """Resumen de una carga, para que la GUI decida qué mostrar -- no imprime
    nada por su cuenta (a diferencia de `ingestar_limpios_tuc.main()`, que
    imprime a consola porque es un script de línea de comandos)."""

    def __init__(self):
        self.avisos: list[str] = []
        self.productos_sin_codigo: list[str] = []
        self.productos_con_extras: dict[str, list[str]] = {}
        self.total_imagenes = 0
        self.total_productos = 0
        self.candidato_ids: list[int] = []
        self.candidatos: list[dict] = []


def resumen_catalogo_activo() -> list[dict]:
    """Qué hay HOY en la revisión de ESTE lote, agrupado por proveedor+catálogo -- plan
    2026-09-07, paso 1 (aislar el catálogo activo). Antes de este paso no
    había forma de saber, sin consultar la base a mano, que un envío se había
    sumado a otro anterior en vez de reemplazarlo (así se mezclaron los 20
    candidatos Converse de prueba con los 54 Reebok reales). Hoy la usan
    `_confirmar_reemplazo_catalogo` (reenvío del mismo lote vs. proveedor
    corregido) y el encabezado de VentanaCandidatosCTk; el diálogo "¿agregar o
    empezar nuevo?" que también la consultaba se eliminó el 2026-09-09.

    FASE 2: el conteo sale del `lote.sqlite`; el nombre del proveedor sigue
    saliendo del maestro permanente en Postgres. Ojo con la pregunta que esta
    función contesta: antes era "qué hay en la tabla global compartida", y por
    eso podía mostrar el catálogo de OTRO lote (así se mezclaron los 20
    Converse con los 54 Reebok). Ahora es siempre lo de este lote."""
    cl = _cl()
    cl.execute("""
        SELECT proveedor_id, catalogo_origen, count(DISTINCT codigo_ocr)
        FROM candidato_raw
        GROUP BY proveedor_id, catalogo_origen
    """)
    crudas = cl.fetchall()
    if not crudas:
        return []
    c = conectar()
    cur = c.cursor()
    try:
        prov_ids = sorted({f[0] for f in crudas if f[0] is not None})
        cur.execute("SELECT proveedor_id, nombre FROM silver.dim_tuc_proveedor "
                    "WHERE proveedor_id = ANY(%s)", (prov_ids,))
        nombres = dict(cur.fetchall())
    finally:
        c.close()
    # El `JOIN` de antes descartaba la fila sin proveedor en el maestro, y el
    # `ORDER BY` era por NOMBRE de proveedor -- que solo se conoce después de
    # resolverlo, así que el orden se aplica acá.
    # `proveedor_id` se agrega (FASE 2 multi-proveedor, 2026-09-16) para que la
    # regla de reemplazo pueda decidir por proveedor sin depender del nombre.
    filas = [{"proveedor_id": pid, "proveedor": nombres[pid],
              "catalogo_origen": co, "n": n}
             for pid, co, n in crudas if pid in nombres]
    filas.sort(key=lambda f: (f["proveedor"], f["catalogo_origen"] or ""))
    return filas


def cargar_carpeta_limpia(
    carpeta: Path,
    proveedor_id: int,
    catalogo_origen: str | None = None,
    quitar_sufijo_duplicado: bool = False,
    solo_ingesta: bool = False,
    on_progreso=None,
    on_avance=None,
    modelo_activo: str | None = None,
    reemplazar_catalogo: bool | str = False,
    solo_archivos=None,
) -> ResultadoCarga:
    """Carga una carpeta ya extraída y revisada (por Excel, PDF o fotos sueltas,
    da igual -- lo único que importa es que ya tenga fondo blanco y un código
    legible por archivo) al Asistente de Compras: staging_tuc -> vectorizar ->
    calificar. Mismo contrato que `ingestar_limpios_tuc.py --carpeta ... `,
    pero como función que la GUI puede llamar directo y mostrar en pantalla,
    no como script de consola.

    `on_progreso(mensaje: str)`, si se pasa, se llama en cada paso importante
    -- la GUI lo puede conectar a una barra de progreso o a un log en pantalla.

    `on_avance(hechos: int, total: int)`, si se pasa, se llama repetidamente
    DURANTE la vectorización (la fase larga, antes esto quedaba mudo hasta el
    final). `vectorizar_y_puntuar_candidatos` (servidor_pty.py) ya calcula este
    detalle candidato por candidato y lo escribe en el diccionario global
    `_progreso` -- pensado originalmente para el servidor web de GestionTUC,
    nunca antes leído desde esta app. En vez de tocar ese archivo compartido
    con otro proyecto, se lee por fuera con un hilo liviano que hace polling
    mientras la llamada bloqueante corre.

    `modelo_activo`: "ti" (default persistido, ver `modelo_activo_configurado`)
    o "estandar" -- cuál motor de comparación usar. Si no se pasa, se lee de
    `config_asistente_compras.json`.

    `reemplazar_catalogo`: si es verdadero, vacía la revisión DE ESTE LOTE
    (`_vaciar_revision`) antes de cargar esta carpeta. Default False porque
    sigue siendo válido reenviar el MISMO catálogo para corregir 1-2 fotos sin
    perder el resto (la lógica incremental de sha1 de más abajo, sin tocar).

    FASE 2 multi-proveedor (2026-09-16): admite además el valor "proveedor",
    que vacía SOLO las filas de `proveedor_id` (con cualquier
    `catalogo_origen`) y deja intacto lo de cualquier otro proveedor que
    conviva en el mismo lote. `True` sigue significando "vaciar el lote
    entero", que es lo que hace falta cuando las filas cargadas quedaron a
    nombre del proveedor equivocado.

    `solo_archivos`: nombres (con o sin extensión) de los archivos de la
    carpeta que pertenecen a ESTE envío. `None` = todos los de la carpeta, el
    comportamiento histórico y el único que se usa en un lote de un solo
    proveedor.

    FASE 5 multi-proveedor (2026-09-16) -- por qué hizo falta. La carpeta que
    recibe esta función es la de exportados del lote
    (`<trabajo>_para_asistente_compras`), que es UNA SOLA para el lote entero:
    `decisiones.exportar_aprobados` copia ahí y NO vacía el destino, así que
    después de enviar al proveedor A sus fotos siguen en la carpeta cuando se
    envía el proveedor B. Como acá se recolectaba la carpeta COMPLETA, el
    envío de B volvía a ingestar las fotos de A -- a nombre de B. Bug real
    medido en la verificación de punta a punta de la Fase 5: un lote de 4+4
    productos terminaba con 12 candidatos, 4 de ellos las referencias EL-* de
    EVER LASTING SHOES CO duplicadas bajo BASH (que además se vectorizaban de
    nuevo y se llevaban su parte del PDV en el paso 7).

    FASE 4: el alcance de este flag se redujo a un solo lote. Nació (plan
    2026-09-07, paso 1) como el TRUNCATE del schema `staging_tuc` compartido,
    porque enviar un catálogo desde el escritorio se SUMABA al de cualquier
    otro lote (así se mezclaron 20 candidatos Converse de prueba con 54 Reebok
    reales el 2026-09-07). Desde la Fase 2 no hay tabla global que truncar:
    esto vacía nada más el `lote.sqlite` de la carpeta que se está
    trabajando."""
    modelo_activo = modelo_activo or modelo_activo_configurado()

    def avisar(msg: str):
        if on_progreso:
            on_progreso(msg)

    res = ResultadoCarga()
    carpeta = Path(carpeta).expanduser().resolve()
    if not carpeta.is_dir():
        raise ValueError(f"No es una carpeta: {carpeta}")
    catalogo_origen = catalogo_origen or carpeta.name

    carpeta_img, avisos_carpeta = resolver_carpeta_imagenes(carpeta)
    res.avisos.extend(avisos_carpeta)
    imagenes = recolectar(carpeta_img)
    if solo_archivos:
        # Se compara por STEM (nombre sin extensión): es lo que el llamador
        # conoce -- `exportar_aprobados` copia cada recorte con su propio
        # nombre, así que el stem del archivo en la carpeta es exactamente el
        # nombre del recorte. Se acepta igual el nombre con extensión para que
        # el llamador pueda pasar lo uno o lo otro.
        permitidos = {Path(str(n)).stem for n in solo_archivos}
        antes = len(imagenes)
        imagenes = [p for p in imagenes if p.stem in permitidos]
        if len(imagenes) != antes:
            avisar(f"{antes - len(imagenes)} foto(s) de la carpeta de exportados son "
                   "de otro proveedor de este lote -- no entran en este envío.")
    if not imagenes:
        raise ValueError(f"No hay imágenes en {carpeta_img}")
    metadatos = cargar_metadatos(None, carpeta)

    presentes = {codigo_base(p.stem).strip().upper() for p in imagenes}
    sospechosos = {c: m.group(1) for c in presentes
                   if (m := PATRON_SUFIJO_DUPLICADO.match(c)) and m.group(1) not in presentes}
    # Fase 3 (2026-09-18): lo que metadatos.json puede confirmar se corrige
    # solo, código por código -- ya no hace falta el flag manual todo-o-nada
    # para los casos donde SÍ hay evidencia real de cuál código es correcto.
    a_corregir, sin_evidencia = resolver_sospechosos_con_metadatos(sospechosos, metadatos)
    if a_corregir:
        ej = next(iter(a_corregir))
        avisar(f"{len(a_corregir)} código(s) corregido(s) automáticamente por metadatos.json "
               f"(ej. {ej} -> {a_corregir[ej]})")

    por_codigo: dict[str, list[Path]] = {}
    for img in imagenes:
        base = codigo_base(img.stem).strip().upper()
        if PATRON_SIN_CODIGO.match(base):
            res.productos_sin_codigo.append(img.name)
            continue
        if base in a_corregir:
            base = a_corregir[base]
        elif quitar_sufijo_duplicado and base in sin_evidencia:
            base = sospechosos[base]
        por_codigo.setdefault(base, []).append(img)
    # Orden estable de las fotos de una misma referencia: el nombre de archivo
    # (FOR-025_3_01 / _02 / _03) es el único orden que el proveedor declaró, y
    # es el que decide qué color queda como variante 0 (la que sigue mandando
    # en la foto de la tarjeta y en todo lo que ya leía "el color principal").
    for fotos in por_codigo.values():
        fotos.sort(key=lambda p: p.name.lower())

    elegidas = {cod: fotos[0] for cod, fotos in por_codigo.items()}
    # 2026-09-17 (pedido del dueño): las fotos extra de una referencia son los
    # OTROS COLORES del bulto, no sobras. Antes se listaban en
    # `productos_con_extras` y se tiraban; ahora cada una se ingesta como una
    # variante de color más, para poder calificarla por separado. La lista se
    # sigue reportando igual (la GUI la muestra como "fotos adicionales").
    extras = {cod: fotos[1:] for cod, fotos in por_codigo.items() if len(fotos) > 1}
    res.productos_con_extras = {cod: [f.name for f in fotos]
                                 for cod, fotos in extras.items()}
    res.total_imagenes = len(imagenes)
    res.total_productos = len(elegidas)

    avisar(f"{len(elegidas)} productos detectados en {carpeta_img.name}")

    c = conectar()
    cur = c.cursor()
    cur.execute("SELECT nombre FROM silver.dim_tuc_proveedor WHERE proveedor_id = %s",
                (proveedor_id,))
    r = cur.fetchone()
    if not r:
        c.close()
        raise ValueError(f"proveedor_id={proveedor_id} no existe en silver.dim_tuc_proveedor")
    avisar(f"Proveedor confirmado: {r[0]}")

    # Plan 2026-09-07, paso 7: sin esto, un candidato que el comprador borró
    # a mano reaparecería solo con reenviar el mismo catálogo -- esta función
    # reconstruye elegidas/raw_catalogo desde los archivos de la carpeta cada
    # vez, y las fotos descartadas siguen ahí (el descarte vive en la base,
    # no en el disco).
    cur.execute("""
        SELECT codigo_ocr FROM cfg.tuc_candidato_descartado
        WHERE proveedor_id = %s AND catalogo_origen = %s
    """, (proveedor_id, catalogo_origen))
    descartados = {fila[0] for fila in cur.fetchall()}
    if descartados:
        antes = len(elegidas)
        elegidas = {cod: ruta for cod, ruta in elegidas.items() if cod not in descartados}
        res.total_productos = len(elegidas)
        avisar(f"{antes - len(elegidas)} producto(s) descartado(s) a mano en este catálogo -- se excluyen del envío.")

    if reemplazar_catalogo:
        # FASE 2: vacía la revisión de ESTE lote (antes era un TRUNCATE del
        # schema global, que se llevaba de paso la revisión de cualquier otro
        # lote abierto).
        #
        # FASE 2 multi-proveedor: con alcance "proveedor" se borra únicamente
        # lo de este `proveedor_id` -- y del disco solo los recortes de esas
        # filas, porque la carpeta de recortes la comparten todos los
        # proveedores del lote y vaciarla entera dejaba al otro proveedor con
        # candidatos sin foto.
        solo_proveedor = (reemplazar_catalogo == "proveedor")
        carpeta_vieja = DATOS_COMPARTIDOS / CARPETA_CROPS
        if solo_proveedor:
            rutas = _vaciar_revision(proveedor_id=proveedor_id)
            for ruta in rutas:
                (DATOS_COMPARTIDOS / ruta).unlink(missing_ok=True)
            avisar("Envío anterior de este proveedor descartado -- se reemplaza "
                   "por el nuevo (lo de los demás proveedores del lote queda intacto).")
        else:
            _vaciar_revision()
            if carpeta_vieja.is_dir():
                for viejo in carpeta_vieja.glob("*"):
                    viejo.unlink(missing_ok=True)
            avisar("Catálogo anterior descartado -- empezando uno nuevo.")

    # H5/H6 de la auditoría de flujo: antes cada envío hacía TRUNCATE de TODO
    # el staging y volvía a vectorizar el catálogo entero, aunque solo se
    # hubiera corregido 1-2 fotos. Ahora se compara el sha1 de cada foto
    # contra el que quedó guardado la última vez (`raw_catalogo.sha1_imagen`,
    # columna agregada 2026-09-03) -- lo que NO cambió se deja intacto (fila,
    # candidato, variantes y embeddings tal cual, gracias a que
    # `_graduar_candidatos_aprobados` ya es idempotente por código+proveedor
    # y las FK a variante/embedding tienen ON DELETE CASCADE): solo se borra
    # y se vuelve a procesar lo que cambió, es nuevo, o ya no viene en este
    # envío.
    # El sha1 cubre TODAS las fotos de la referencia (la principal y los demás
    # colores), no solo la primera: si el proveedor cambia o agrega un color,
    # la referencia tiene que volver a procesarse aunque la foto 1 sea idéntica.
    def _sha1_referencia(cod: str) -> str:
        h = hashlib.sha1()
        for foto in [elegidas[cod], *extras.get(cod, ())]:
            h.update(foto.read_bytes())
        return h.hexdigest()

    sha1_actual = {cod: _sha1_referencia(cod) for cod in elegidas}

    # El modelo cuya presencia se exige para considerar un código "listo" --
    # si el usuario cambió de método activo desde el último envío, la foto
    # puede seguir igual (mismo sha1) pero el vector de ESTE método todavía
    # no existe, así que igual hay que (re)vectorizarla. Basta revisar uno
    # de los dos del par estándar: siempre se insertan juntos -- y eso vale
    # para los TRES métodos no-TI ("estandar", "fashion", "dino"), que comparten
    # exactamente el mismo par de vectores y solo difieren en cuál se lee al
    # buscar comparables. O sea: cambiar entre esos tres NO obliga a
    # re-vectorizar el lote; solo cambiar a/desde "ti" lo hace.
    modelo_verificar = "dinov2_ti" if modelo_activo == "ti" else "fashion_siglip"
    # FASE 2: el estado previo del envío sale del `lote.sqlite`. `EXISTS(...)`
    # devuelve 0/1 en SQLite en vez de false/true, y el valor se usa como
    # condición más abajo -- se normaliza a bool al armar el dict.
    cl = _cl()
    cl.execute("""
        SELECT r.codigo_ocr, r.sha1_imagen,
               EXISTS (
                   SELECT 1 FROM candidato_embedding e
                   JOIN candidato_variante v ON v.variante_id = e.variante_id
                   WHERE v.candidato_id = d.candidato_id AND e.modelo_embedding = ?
               ) AS tiene_embedding_modelo
        FROM candidato_raw r
        JOIN candidato d
            ON d.proveedor_id = r.proveedor_id AND d.codigo_proveedor = r.codigo_ocr
           AND d.catalogo_origen = r.catalogo_origen
        WHERE r.proveedor_id = ? AND r.catalogo_origen = ?
    """, (modelo_verificar, proveedor_id, catalogo_origen))
    estado_previo = {cod: (sha1, bool(tiene)) for cod, sha1, tiene in cl.fetchall()}

    codigos_sin_cambios = {
        cod for cod, h in sha1_actual.items()
        if (prev := estado_previo.get(cod)) is not None and prev[0] == h and prev[1]
    }
    codigos_a_quitar = set(estado_previo) - set(elegidas)
    codigos_a_rehacer = (set(elegidas) - codigos_sin_cambios) | codigos_a_quitar

    if codigos_sin_cambios:
        avisar(f"{len(codigos_sin_cambios)} producto(s) sin cambios desde el último envío "
               "a este proveedor -- se reutiliza su comparación anterior.")

    if codigos_a_rehacer:
        rehacer = sorted(codigos_a_rehacer)
        cl.execute("BEGIN")
        try:
            cl.execute(f"""
                DELETE FROM candidato_raw
                WHERE proveedor_id = ? AND catalogo_origen = ? AND codigo_ocr IN {_en(rehacer)}
            """, [proveedor_id, catalogo_origen, *rehacer])
            # ON DELETE CASCADE se encarga de variante/embedding/score de estos
            # códigos -- en el `lote.sqlite` funciona porque `almacen.conexion`
            # abre con `PRAGMA foreign_keys=ON` (SQLite las ignora si no).
            cl.execute(f"""
                DELETE FROM candidato
                WHERE proveedor_id = ? AND catalogo_origen = ? AND codigo_proveedor IN {_en(rehacer)}
            """, [proveedor_id, catalogo_origen, *rehacer])
            cl.execute("COMMIT")
        except Exception:
            cl.execute("ROLLBACK")
            raise

    carpeta_staging = DATOS_COMPARTIDOS / CARPETA_CROPS
    carpeta_staging.mkdir(parents=True, exist_ok=True)
    # Solo se borran del disco los archivos de códigos que YA NO vienen en
    # este envío -- los que se reutilizan o se van a rehacer se sobreescriben
    # con la copia de abajo, nunca hace falta vaciar la carpeta entera.
    for cod in codigos_a_quitar:
        for patron in (f"{cod}.*", f"{cod}__color*"):  # foto principal + demás colores
            for viejo in carpeta_staging.glob(patron):
                viejo.unlink(missing_ok=True)

    import shutil
    a_insertar = sorted(set(elegidas) - codigos_sin_cambios)
    filas = []
    fotos_extra_por_codigo: dict[str, list[str]] = {}
    for cod in a_insertar:
        origen = elegidas[cod]
        nombre = f"{cod}{origen.suffix.lower()}"
        shutil.copy2(origen, carpeta_staging / nombre)
        # Los demás colores de la misma referencia (2026-09-17): se copian con
        # un nombre propio para que convivan con la foto principal en la
        # carpeta de recortes, y su ruta queda en `candidato_foto_extra` para
        # que la vectorización cree una variante por cada uno.
        rutas_extra = []
        for n, foto in enumerate(extras.get(cod, ()), start=1):
            nombre_extra = f"{cod}__color{n}{foto.suffix.lower()}"
            shutil.copy2(foto, carpeta_staging / nombre_extra)
            rutas_extra.append(f"{CARPETA_CROPS}/{nombre_extra}")
        if rutas_extra:
            fotos_extra_por_codigo[cod] = rutas_extra
        meta = metadatos.get(cod, {})
        filas.append({
            "proveedor_id": proveedor_id,
            "catalogo_origen": catalogo_origen,
            "pagina": None,
            "bbox": None,
            "codigo_ocr": cod,
            "confianza_ocr": None,
            "crop_imagen_path": f"{CARPETA_CROPS}/{nombre}",
            "texto_crudo": f"[sin OCR] código del nombre de archivo: {origen.name}",
            "talla_min": _entero(meta.get("talla_min")),
            "talla_max": _entero(meta.get("talla_max")),
            "empaque_cantidad": _entero(meta.get("empaque_cantidad")),
            "empaque_unidad": (str(meta["empaque_unidad"]).strip().upper()
                               if meta.get("empaque_unidad") else None),
            "fecha_ingesta": date.today(),
            "sha1_imagen": sha1_actual[cod],
            # Se capturaba desde el Excel (extraer_excel.py) pero nunca llegaba
            # a la base -- hallazgo real del paso 8 (respaldo de score por
            # precio, necesita el costo del proveedor para estimar precio de
            # venta y compararlo contra el catálogo).
            "costo": meta.get("costo"),
            # Plan 2026-09-07, paso 8: moneda del costo, inferida del
            # encabezado de la columna en el Excel (extraer_excel.py). None si
            # no se pudo inferir -- servidor_pty.py NO asume CRC en ese caso.
            "moneda_costo": meta.get("moneda_costo"),
            # Plan 2026-09-04, paso 6: atributo declarado por el proveedor en
            # su propio Excel (ver extraer_excel.py/atributos_proveedor.py).
            # NULL si el Excel no traía esa columna -- la categorización
            # visual (categorizar_candidatos, servidor_pty.py) rellena el
            # hueco solo cuando esto viene vacío.
            "tipo_declarado": meta.get("tipo_declarado"),
            "tipo_norm": meta.get("tipo_norm"),
            "color_declarado": meta.get("color_declarado"),
            "color_familia": meta.get("color_familia"),
            "marca_declarada": meta.get("marca_declarada"),
        })

    if filas:
        # FASE 2: la ingesta escribe en el `lote.sqlite`. `execute_batch` de
        # psycopg2 -> `executemany` de sqlite3; `date.today()` -> texto ISO,
        # porque SQLite no tiene tipo fecha.
        cl.execute("BEGIN")
        try:
            cl.executemany("""
                INSERT INTO candidato_raw
                    (proveedor_id, catalogo_origen, pagina, bbox, codigo_ocr, confianza_ocr,
                     crop_imagen_path, texto_crudo, talla_min, talla_max,
                     empaque_cantidad, empaque_unidad, estado_revision, fecha_ingesta, sha1_imagen,
                     costo, moneda_costo, tipo_declarado, tipo_norm, color_declarado, color_familia,
                     marca_declarada, cargado_en)
                VALUES (:proveedor_id, :catalogo_origen, :pagina, :bbox,
                        :codigo_ocr, :confianza_ocr, :crop_imagen_path, :texto_crudo,
                        :talla_min, :talla_max, :empaque_cantidad, :empaque_unidad,
                        'aprobado', :fecha_ingesta, :sha1_imagen,
                        :costo, :moneda_costo, :tipo_declarado, :tipo_norm, :color_declarado,
                        :color_familia, :marca_declarada, :cargado_en)
            """, [{**f,
                   "fecha_ingesta": f["fecha_ingesta"].isoformat(),
                   "cargado_en": almacen.ahora()} for f in filas])
            # Las fotos de los demás colores, colgadas de la fila `raw` recién
            # insertada (se resuelve el id por código, que es único dentro de
            # proveedor+catálogo). Sin filas acá el comportamiento es
            # exactamente el de siempre: una foto, una variante.
            if fotos_extra_por_codigo:
                cods = sorted(fotos_extra_por_codigo)
                cl.execute(f"""
                    SELECT id, codigo_ocr FROM candidato_raw
                    WHERE proveedor_id = ? AND catalogo_origen = ? AND codigo_ocr IN {_en(cods)}
                """, [proveedor_id, catalogo_origen, *cods])
                ids_raw = dict((cod, rid) for rid, cod in cl.fetchall())
                cl.executemany("""
                    INSERT INTO candidato_foto_extra (raw_id, orden, crop_imagen_path)
                    VALUES (?, ?, ?)
                    ON CONFLICT (raw_id, orden) DO UPDATE SET
                        crop_imagen_path = excluded.crop_imagen_path
                """, [(ids_raw[cod], n, ruta)
                      for cod, rutas in fotos_extra_por_codigo.items() if cod in ids_raw
                      for n, ruta in enumerate(rutas, start=1)])
            cl.execute("COMMIT")
        except Exception:
            cl.execute("ROLLBACK")
            raise
    avisar(f"{len(filas)} producto(s) nuevo(s)/cambiado(s) cargados en la revisión del lote.")

    nuevos_ids = _graduar_candidatos_aprobados()
    avisar(f"{len(nuevos_ids)} candidato(s) nuevo(s)/cambiado(s) graduados.")

    cl.execute("""
        SELECT candidato_id FROM candidato
        WHERE proveedor_id = ? AND catalogo_origen = ?
        ORDER BY candidato_id
    """, (proveedor_id, catalogo_origen))
    res.candidato_ids = [r[0] for r in cl.fetchall()]  # TODOS los activos (nuevos + reutilizados)

    if solo_ingesta:
        c.close()
        return res

    n_cand = len(nuevos_ids)  # solo lo que REALMENTE se va a vectorizar
    if n_cand == 0:
        avisar("Ningún producto cambió desde el último envío -- no hace falta "
               "vectorizar nada, solo se vuelve a puntuar el catálogo completo.")
    else:
        segundos_est, muestras = estimar_tiempo(n_cand, modelo_activo)
        if segundos_est:
            minutos = max(1, round(segundos_est / 60))
            avisar(f"Vectorizando y puntuando {n_cand} candidato(s) nuevo(s)/cambiado(s) — "
                   f"estimado ~{minutos} min (promedio de {muestras} corrida(s) anteriores)...")
        else:
            avisar(f"Vectorizando y puntuando {n_cand} candidato(s) nuevo(s)/cambiado(s) — "
                   f"primera corrida medida, todavía sin estimado de tiempo...")
        vectorizador_actual = (_motor_calif_mod._vectorizador_ti if modelo_activo == "ti"
                              else _motor_calif_mod._vectorizador)
        if vectorizador_actual is None:
            avisar("Preparando el motor de comparación (cargando modelos de IA en memoria — "
                   "solo la primera vez en esta sesión, puede tardar 1-3 min)...")

    detener_poll = threading.Event()
    ultimo_msg = [None]

    def _poll_progreso() -> None:
        # `_progreso_pty` lo actualiza `vectorizar_y_puntuar_candidatos` en
        # OTRO hilo (este) -- se lee una foto (dict(...)) en cada vuelta para
        # no pisar valores a medio escribir.
        while not detener_poll.is_set():
            snap = dict(_progreso_pty)
            msg = snap.get("mensaje")
            if msg and msg != ultimo_msg[0]:
                ultimo_msg[0] = msg
                avisar(msg)
            sub_total = snap.get("sub_total") or 0
            if on_avance and sub_total:
                on_avance(snap.get("sub", 0), sub_total)
            detener_poll.wait(0.4)

    hilo_poll = threading.Thread(target=_poll_progreso, daemon=True)
    hilo_poll.start()
    inicio = time.monotonic()
    try:
        if nuevos_ids:
            # ya_aislado=True: estas fotos ya pasaron por Limpieza de Imágenes
            # -- un calzado por foto, fondo blanco real -- así que se salta la
            # detección/quitado de fondo/SAM que sí hace falta para catálogos
            # de proveedor sin procesar (flujo web de GestionTUC, que sigue
            # usando ya_aislado=False por defecto). Solo los NUEVOS/CAMBIADOS
            # -- los que no cambiaron ya tienen su embedding de una corrida
            # anterior, intacto porque no se borró.
            vectorizar_y_puntuar_candidatos(c, cur, nuevos_ids, ya_aislado=True,
                                           modelo_activo=modelo_activo)
        # Puntuar SIEMPRE corre sobre el catálogo COMPLETO (nuevos + reutilizados),
        # no solo lo nuevo -- categorías, mismo-modelo-otro-color y el score
        # comparan candidatos ENTRE SÍ, así que agregar 2 productos puede
        # cambiar la lectura de los otros 18 aunque su foto no haya cambiado.
        # `vectorizar_y_puntuar_candidatos` ya corre esto mismo internamente,
        # pero solo sobre `nuevos_ids` -- se repite acá con la lista completa
        # para que quede consistente (repetir sobre los mismos nuevos_ids es
        # barato, es SQL, no vuelve a tocar ningún modelo de IA).
        if res.candidato_ids:
            categorizar_candidatos(cur, res.candidato_ids, modelo_activo=modelo_activo)
            inferir_genero_por_talla(cur, res.candidato_ids)
            derivar_estructura_calzado(cur, res.candidato_ids)
            detectar_mismo_modelo_otro_color(cur, res.candidato_ids, modelo_activo=modelo_activo)
            puntuar_candidatos(cur, res.candidato_ids, modelo_activo=modelo_activo)
            c.commit()
    finally:
        detener_poll.set()
        hilo_poll.join(timeout=2)
    if n_cand:
        # Se mide la corrida COMPLETA (incluye la carga de modelos si tocó
        # cargarlos ahora) a propósito: es el tiempo real que experimentó el
        # usuario, y es lo que `estimar_tiempo` necesita predecir la próxima
        # vez -- separar "modelo" de "candidatos" solo serviría si esta app
        # quedara corriendo entre envíos, y hoy no es el caso (cada envío es
        # un proceso nuevo).
        _registrar_tiempo_real(n_cand, time.monotonic() - inicio, modelo_activo)
    c.close()

    avisar("Listo. Consultando candidatos finales...")
    # Solo los de ESTE envío: con "Agregar" (reemplazar_catalogo=False) staging
    # puede tener también el catálogo de otro proveedor, y devolverlo acá era
    # lo que hacía que la pantalla de candidatos mezclara lotes.
    res.candidatos = filtrar_candidatos(api_candidatos(), proveedor_id, catalogo_origen)
    return res


def eliminar_candidato(candidato_id: int) -> None:
    """Elimina POR COMPLETO una referencia (candidato) de staging_tuc --
    plan 2026-09-07, paso 7. Hard-delete, no soft-delete: staging_tuc ya es
    efímero por diseño explícito del usuario (memoria
    feedback_datos_efimeros_vs_permanentes), así que no hay razón para cargar
    el resto de las ~8 consultas que leen staging con el peso de filtrar un
    estado "descartado" que podría olvidarse en alguna.

    Además de borrar de la base (dim_candidato con ON DELETE CASCADE se
    encarga de variantes/embeddings/score; raw_catalogo no tiene FK, se
    borra aparte) y del disco (el crop en DatosCompartidos), registra el
    código en `cfg.tuc_candidato_descartado` -- sin esto, reenviar el MISMO
    catálogo (mismo proveedor+catalogo_origen) desde `cargar_carpeta_limpia`
    reconstruiría la fila desde la foto que sigue en la carpeta de origen,
    resucitando un candidato que el comprador ya decidió descartar.

    FASE 2: el borrado es local (la revisión de ESTE lote); el REGISTRO del
    descarte sigue yendo a `cfg.tuc_candidato_descartado` en Postgres, y eso es
    deliberado -- "este código ya lo descarté" es una decisión del comprador
    que debe valer para cualquier lote futuro del mismo catálogo, no solo para
    este archivo."""
    cl = _cl()
    cl.execute("""
        SELECT proveedor_id, catalogo_origen, codigo_proveedor
        FROM candidato WHERE candidato_id = ?
    """, (candidato_id,))
    r = cl.fetchone()
    if not r:
        raise ValueError(f"candidato_id={candidato_id} no existe en la revisión de este lote "
                         f"(¿ya se descartó?)")
    proveedor_id, catalogo_origen, codigo_ocr = tuple(r)

    cl.execute("BEGIN")
    try:
        cl.execute("""
            DELETE FROM candidato_raw
            WHERE proveedor_id = ? AND catalogo_origen = ? AND codigo_ocr = ?
        """, (proveedor_id, catalogo_origen, codigo_ocr))
        cl.execute("DELETE FROM candidato WHERE candidato_id = ?", (candidato_id,))
        cl.execute("COMMIT")
    except Exception:
        cl.execute("ROLLBACK")
        raise

    c = conectar(); cur = c.cursor()
    try:
        with c:
            cur.execute("""
                INSERT INTO cfg.tuc_candidato_descartado (proveedor_id, catalogo_origen, codigo_ocr)
                VALUES (%s, %s, %s)
                ON CONFLICT (proveedor_id, catalogo_origen, codigo_ocr) DO NOTHING
            """, (proveedor_id, catalogo_origen, codigo_ocr))
    finally:
        c.close()

    carpeta_staging = DATOS_COMPARTIDOS / CARPETA_CROPS
    for viejo in carpeta_staging.glob(f"{codigo_ocr}.*"):
        viejo.unlink(missing_ok=True)


def eliminar_variante(variante_id: int, modelo_activo: str | None = None) -> None:
    """Elimina UN color de un candidato multicolor -- plan 2026-09-07, paso
    7. A diferencia de `eliminar_candidato`, esto NO borra el candidato
    entero: solo la variante puntual (ON DELETE CASCADE se encarga del
    embedding de esa variante), y como el vector representativo del
    candidato es el PROMEDIO de todas sus variantes (`_vector_representativo`
    en servidor_pty.py), hay que re-categorizar y re-puntuar después de
    quitar una -- el vector cambió.

    Bloqueado si es la ÚLTIMA variante del candidato: eso es borrar el
    candidato completo, para lo cual ya existe `eliminar_candidato` (con su
    registro en cfg.tuc_candidato_descartado, que esto no hace).

    FASE 2: el borrado de la variante es local; la re-categorización y el
    re-scoring siguen necesitando el índice permanente de Postgres, así que la
    conexión se abre para eso y solo para eso."""
    cl = _cl()
    cl.execute("SELECT candidato_id FROM candidato_variante WHERE variante_id = ?",
               (variante_id,))
    r = cl.fetchone()
    if not r:
        raise ValueError(f"variante_id={variante_id} no existe")
    candidato_id = r[0]

    cl.execute("SELECT count(*) FROM candidato_variante WHERE candidato_id = ?",
               (candidato_id,))
    n_variantes = cl.fetchone()[0]
    if n_variantes <= 1:
        raise ValueError("Es la única variante de este candidato -- usá 'eliminar candidato' en vez de esto.")

    cl.execute("BEGIN")
    try:
        cl.execute("DELETE FROM candidato_variante WHERE variante_id = ?", (variante_id,))
        cl.execute("""
            UPDATE candidato SET n_variantes_color = n_variantes_color - 1
            WHERE candidato_id = ?
        """, (candidato_id,))
        cl.execute("COMMIT")
    except Exception:
        cl.execute("ROLLBACK")
        raise

    modelo_activo = modelo_activo or modelo_activo_configurado()
    c = conectar(); cur = c.cursor()
    try:
        categorizar_candidatos(cur, [candidato_id], modelo_activo=modelo_activo)
        puntuar_candidatos(cur, [candidato_id], modelo_activo=modelo_activo)
    finally:
        c.close()


def _nombre_proveedor(proveedor_id: int) -> str | None:
    c = conectar(); cur = c.cursor()
    cur.execute("SELECT nombre FROM silver.dim_tuc_proveedor WHERE proveedor_id = %s",
                (proveedor_id,))
    r = cur.fetchone()
    c.close()
    return r[0] if r else None


def filtrar_candidatos(candidatos: list[dict], proveedor_id: int | None = None,
                       catalogo_origen=None) -> list[dict]:
    """Deja solo los candidatos del proveedor y/o del catálogo indicados.

    Bug real (2026-09-09): la app de escritorio mostraba TODO lo que hubiera
    en `staging_tuc.dim_candidato`, sin importar de qué proveedor era el lote
    que el comprador tenía en pantalla -- si alguien había enviado otro
    catálogo con "Agregar" (sin reemplazar), la pantalla 6 mezclaba dos
    proveedores como si fueran uno.

    Se filtra en Python, sobre el resultado de `api_candidatos()`, a propósito:
    esa consulta vive en `servidor_pty.py` (GestionTUC), compartida con la
    interfaz web -- agregarle parámetros ahí tocaría un archivo de otro
    proyecto. Las dos claves que hacen falta (`proveedor` por nombre y
    `catalogo_origen`) ya vienen en cada fila, así que el filtro es exacto, no
    una aproximación. `catalogo_origen` acepta un nombre o un conjunto de
    nombres posibles (la GUI conoce dos variantes del mismo lote).

    Endurecido el 2026-09-09 (segunda vuelta del mismo bug): antes, si se
    pedía un `proveedor_id` que la base no resolvía a nombre, `nombre` quedaba
    en None y el filtro por proveedor se desactivaba EN SILENCIO -- devolviendo
    todo staging justo cuando más había que filtrar. Ahora un proveedor que no
    existe (o un `catalogo_origen` que queda vacío) devuelve lista vacía: "no
    pude identificar el lote" nunca puede significar "mostrá todo". La
    comparación además normaliza espacios y mayúsculas, porque el nombre del
    proveedor viaja por metadatos de la carpeta y volvía con formato distinto."""
    if proveedor_id is None and catalogo_origen is None:
        return candidatos

    def _norm(v) -> str:
        return " ".join(str(v).split()).casefold() if v is not None else ""

    if catalogo_origen is None:
        origenes = None
    elif isinstance(catalogo_origen, str):
        origenes = {_norm(catalogo_origen)}
    else:
        origenes = {_norm(o) for o in catalogo_origen if str(o).strip()}
        if not origenes:
            # Se pidió filtrar por catálogo y no quedó ninguno válido: eso es
            # "ningún candidato", no "todos".
            return []

    nombre = None
    if proveedor_id is not None:
        nombre = _nombre_proveedor(proveedor_id)
        if not nombre:
            return []
        nombre = _norm(nombre)

    salida = []
    for cand in candidatos:
        if nombre is not None and _norm(cand.get("proveedor")) != nombre:
            continue
        if origenes is not None and _norm(cand.get("catalogo_origen")) not in origenes:
            continue
        salida.append(cand)
    return salida


def obtener_candidatos(proveedor_id: int | None = None,
                       catalogo_origen=None) -> list[dict]:
    """Los candidatos actuales en revisión, ya con foto/score/color -- misma
    consulta que usa la interfaz web hoy, reutilizada tal cual.

    Sin argumentos devuelve TODO lo que hay en staging (comportamiento
    histórico, para no romper a nadie que la llame así). Con `proveedor_id`
    y/o `catalogo_origen` devuelve solo lo del lote pedido (ver
    `filtrar_candidatos`)."""
    return filtrar_candidatos(api_candidatos(), proveedor_id, catalogo_origen)


def ruta_foto_variante(variante_id: int) -> Path | None:
    """Archivo real de la foto ya limpia de una variante -- la interfaz web
    expone esto como URL (`/crop_variante/<id>`); la app de escritorio no
    tiene servidor HTTP, así que necesita el archivo directo. Misma consulta
    que usa `_servir_crop_variante` en `servidor_pty.py`.

    FASE 2: 100% local. Esta es la función que hacía IMPRESCINDIBLE migrar
    también este archivo: `api_candidatos` ya entrega los `variante_id` del
    `lote.sqlite`, así que buscarlos en Postgres devolvía None (o, peor, la
    foto de otro lote que casualmente tuviera ese id) y el paso 6 se quedaba
    sin imágenes sin dar ningún error."""
    cl = _cl()
    cl.execute("""
        SELECT v.imagen_limpia_path, v.indice, d.codigo_proveedor
        FROM candidato_variante v JOIN candidato d ON d.candidato_id = v.candidato_id
        WHERE v.variante_id = ?
    """, (variante_id,))
    r = cl.fetchone()
    if not r or not r[0]:
        return None
    imagen_limpia_path, indice, codigo_proveedor = tuple(r)
    ruta = DATOS_COMPARTIDOS / imagen_limpia_path
    if ruta.exists():
        return ruta
    # Bug real (2026-09-18): `imagen_limpia_path` cae bajo
    # DATOS_COMPARTIDOS/tuc_catalogo_staging/, una carpeta COMPARTIDA entre
    # catálogos -- al procesar uno nuevo, sus fotos pisan/limpian las del
    # anterior, así que cualquier lote que no sea el último procesado se
    # queda sin foto por esta vía. La foto real sigue viva dentro de la
    # propia carpeta del lote (blanco/<código>_NN.jpg o
    # transparente/<código>_NN.png, que el pipeline nunca borra), así que se
    # busca ahí como respaldo antes de rendirse. Antes este respaldo solo
    # existía en etiquetado_calibracion.py; ahora vive acá, donde también lo
    # aprovechan los 3 usos de VentanaCandidatosCTk en gui_profesional_ctk.py.
    carpeta_lote = _motor_calif_mod.lote_activo()
    if not carpeta_lote or not codigo_proveedor:
        return None
    sufijo = f"_{(indice or 0) + 1:02d}"
    for sub, ext in (("blanco", ".jpg"), ("transparente", ".png")):
        candidata = Path(carpeta_lote) / sub / f"{codigo_proveedor}{sufijo}{ext}"
        if candidata.exists():
            return candidata
    return None


def ruta_foto_imagen(imagen_id: int) -> Path | None:
    """Archivo real de una foto de `silver.dim_imagen` (marca reconocida o
    TUCALZADO ya vendido) -- misma consulta que usa `_servir_imagen` en
    servidor_pty.py, pero devolviendo el archivo directo en vez de servirlo
    por HTTP. Necesaria para pintar las fotos de comparables en la app de
    escritorio (plan 2026-09-07, integración nativa)."""
    c = conectar()
    cur = c.cursor()
    cur.execute("SELECT imagen_path_local FROM silver.dim_imagen WHERE imagen_id = %s", (imagen_id,))
    r = cur.fetchone()
    c.close()
    if not r or not r[0]:
        return None
    p = Path(r[0])
    return p if p.is_absolute() else DATOS_COMPARTIDOS / p


def obtener_similares_marca(candidato_id: int, k: int = 6, indice: int = 0,
                             modelo_activo: str | None = None,
                             filtrar_color: bool = True) -> list[dict]:
    """Comparables de marca reconocida para un candidato -- misma consulta que
    usa la interfaz web (`/api/similar_marca`), reutilizada tal cual. Sin
    `modelo_activo` explícito usa el método configurado para este catálogo
    (ver `establecer_modelo_activo`/`modelo_activo_configurado`).

    `filtrar_color=True` acá (y False en `servidor_pty`, para no cambiarle el
    comportamiento a la herramienta web sin avisar): bug real reportado por el
    usuario, la lista de comparables mostraba calzado de OTRO COLOR porque
    ordenaba por similitud de embedding pura -- el filtro de género y tipo ya
    existía, el de color no. Ver el bloque de comentarios de
    `_mapa_color_familia` en `servidor_pty.py`."""
    return api_similar_marca(candidato_id, k=k, indice=indice,
                              modelo_activo=modelo_activo or modelo_activo_configurado(),
                              filtrar_color=filtrar_color)


def obtener_similares_tuc(candidato_id: int, k: int = 6, indice: int = 0,
                          modelo_activo: str | None = None,
                          filtrar_color: bool = True) -> list[dict]:
    """Comparables de TUCALZADO ya vendido -- misma consulta que
    `/api/similar_tuc`. `filtrar_color`: ver `obtener_similares_marca`."""
    return api_similar_tuc(candidato_id, k=k, indice=indice,
                            modelo_activo=modelo_activo or modelo_activo_configurado(),
                            filtrar_color=filtrar_color)


def obtener_similares_del_canal(candidato_id: int, k: int = 6, indice: int = 0,
                                 modelo_activo: str | None = None,
                                 filtrar_color: bool = True) -> dict:
    """Comparables ENRUTADOS por el canal de venta declarado para el lote.

    Devuelve `{"canal", "principales", "informativos"}`:

      canal = "marca"  ->  principales: comparables de TC Marcas (el mismo
                           índice contra el que se calculó el puntaje);
                           informativos: los del catálogo genérico.
      canal = "tuc"    ->  principales: comparables del catálogo genérico (los
                           que determinaron el puntaje);
                           informativos: a qué productos de MARCA RECONOCIDA se
                           parece -- pura lectura, no toca el puntaje.

    Las dos listas se consultan igual que siempre (`api_similar_marca` /
    `api_similar_tuc`, sin restricción por marca); lo único que decide el canal
    es cuál manda y cuál es informativa.
    """
    canal = canal_venta_lote()
    modelo = modelo_activo or modelo_activo_configurado()
    marca = obtener_similares_marca(candidato_id, k=k, indice=indice,
                                     modelo_activo=modelo, filtrar_color=filtrar_color)
    tuc = obtener_similares_tuc(candidato_id, k=k, indice=indice,
                                 modelo_activo=modelo, filtrar_color=filtrar_color)
    if canal == "marca":
        return {"canal": canal, "principales": marca, "informativos": tuc}
    return {"canal": canal, "principales": tuc, "informativos": marca}


def obtener_costo_declarado(candidato_id: int) -> tuple[float | None, str | None]:
    """Costo declarado por el proveedor para un candidato, tal cual está en
    `staging_tuc.raw_catalogo` -- (costo, moneda_costo), o (None, None).

    Es SOLO LECTURA y vive acá (no en `servidor_pty.py`) a propósito: la
    conversión a colones y el precio de venta sugerido de esta app se calculan
    con la moneda y el tipo de cambio que el comprador declaró PARA ESTE LOTE
    (metadatos locales de la carpeta de trabajo), no con la constante global
    `TIPO_CAMBIO_USD_CRC` de `servidor_pty.py`. Devolver el costo crudo deja
    esa decisión afuera.

    `moneda_costo` puede venir en None cuando no se pudo inferir del Excel del
    proveedor: en ese caso manda la moneda declarada para el lote.

    FASE 2: 100% local."""
    cl = _cl()
    cl.execute("""
        SELECT r.costo, r.moneda_costo
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        WHERE c.candidato_id = ?
    """, (candidato_id,))
    fila = cl.fetchone()
    if not fila or fila[0] is None:
        return None, None
    return float(fila[0]), fila[1]


def fijar_costo_declarado(candidato_id: int, costo: float | None,
                          moneda: str | None = None) -> dict:
    """El comprador escribe a mano el costo que le cotizó el proveedor para
    UNA referencia -- paso 6 de la app de escritorio (2026-09-09).

    Escribe el costo CRUDO en `staging_tuc.raw_catalogo` (columnas `costo` /
    `moneda_costo`, ver `sql/29_costo_precio_manual_staging_tuc.sql`), que es
    el lugar donde ya lo esperan tanto la cascada de precio
    (`precio_venta_referencia`, nivel "costo_proveedor") como el respaldo de
    score por precio (`puntuar_candidatos`). Crudo y no convertido a propósito:
    la conversión a colones depende del tipo de cambio declarado PARA EL LOTE
    (metadatos locales de la carpeta de trabajo), no de la constante global de
    `servidor_pty.py`.

    Vive acá y no en `servidor_pty.py` (compartido con la web) porque es una
    escritura nueva que la web no hace: agregarla allá cambiaría un archivo de
    otro proyecto sin necesidad. Usa exactamente el mismo `UPDATE ... FROM` que
    ya hace `api_guardar_cotizacion` al cerrar una cotización, así que las dos
    rutas dejan la base en el mismo estado.

    `costo=None` (o <= 0) BORRA el costo declarado -- vuelve a la cascada
    automática, igual que hace `api_fijar_precio_manual` con el precio.

    Devuelve {"ok": True, "costo": ..., "moneda": ...} con lo que quedó
    realmente guardado."""
    try:
        costo_num = float(costo) if costo is not None else None
    except (TypeError, ValueError):
        raise ValueError(f"costo inválido: {costo!r}") from None
    if costo_num is not None and costo_num <= 0:
        costo_num = None
    if costo_num is None:
        moneda = None  # sin costo no hay moneda de costo que guardar
    elif moneda not in ("USD", "CRC"):
        raise ValueError(f"moneda inválida: {moneda!r} (esperado USD o CRC)")

    # FASE 2: 100% local. El `UPDATE ... FROM` de Postgres se reescribe con una
    # subconsulta correlacionada (SQLite no soporta esa forma de junta en un
    # UPDATE), igual que en `api_guardar_cotizacion` -- las dos rutas siguen
    # dejando el lote en el mismo estado.
    cl = _cl()
    cl.execute("""
        UPDATE candidato_raw
        SET costo = ?, moneda_costo = ?
        WHERE EXISTS (
            SELECT 1 FROM candidato d
            WHERE d.candidato_id = ?
              AND candidato_raw.proveedor_id    = d.proveedor_id
              AND candidato_raw.codigo_ocr      = d.codigo_proveedor
              AND candidato_raw.catalogo_origen = d.catalogo_origen
        )
        RETURNING id
    """, (costo_num, moneda, candidato_id))
    if cl.fetchone() is None:
        raise ValueError(
            f"candidato_id={candidato_id} no tiene fila en la revisión de este lote "
            "(¿se reemplazó el catálogo?)")
    return {"ok": True, "costo": costo_num, "moneda": moneda}


def obtener_pedido_sugerido(pdv_objetivo: float, mes_venta: int | None = None) -> dict:
    """Reparte un PDV objetivo entre los candidatos del catálogo en revisión --
    misma lógica que `/api/pedido_sugerido` (plan 2026-09-04, paso 8).

    `mes_venta` (1-12) es el mes en que el comprador espera VENDER el pedido:
    ajusta el peso de cada candidato por la estacionalidad real de su categoría
    (índices mensuales de importación de `gold.vw_senal_tipo`, la misma fuente
    que usa el Asistente de Compras de TEC). `None` = sin ajuste, el peso es el
    score puro."""
    return api_pedido_sugerido(pdv_objetivo, mes_venta=mes_venta)


def asignar_categoria(candidato_id: int, categoria: str | None = None, genero: str | None = None,
                      modelo_activo: str | None = None) -> dict:
    """Corrige a mano la categoría (tipo) y/o el género de un candidato --
    misma acción que el selector de la interfaz web
    (`/api/asignar_categoria`): UPDATE a `staging_tuc.dim_candidato`
    (`categoria_declarada` / `genero_canonico`) + re-scoring del candidato,
    porque el score depende del filtro categoria+genero.

    Se pasa SIEMPRE el método configurado para este catálogo: el re-scoring
    tiene que correr en el mismo espacio de vectores con el que se calificó
    el resto de la lista, o el score corregido queda incomparable (ver la nota
    de `modelo_activo` en `api_asignar_categoria`)."""
    return api_asignar_categoria(candidato_id, categoria, genero,
                                 modelo_activo=modelo_activo or modelo_activo_configurado())


def marcas_reconocidas() -> list[str]:
    """Lista gobernada de marcas reconocidas (`silver.dim_marca`) para el
    desplegable del paso 6. Ver `api_marcas_reconocidas`: NO es exhaustiva
    (OSIRIS no está), así que el control de la pantalla deja escribir libre."""
    try:
        return api_marcas_reconocidas()
    except Exception:  # noqa: BLE001
        # Sin Postgres a mano el paso 6 no se puede quedar sin abrir por esto:
        # el comprador puede escribir la marca igual.
        return []


def fijar_marca_declarada(candidato_id: int, marca: str | None) -> dict:
    """El comprador declara a mano la marca de una referencia (Tarea 7). Ver
    `api_fijar_marca_declarada` para el porqué de que sea manual."""
    return api_fijar_marca_declarada(candidato_id, marca)


def detectar_marca_en_texto(texto: str | None, marcas: list[str]) -> str | None:
    """Busca un nombre de marca reconocida DENTRO del texto que trae el
    catálogo del proveedor (referencia OCR, descripción). Fuente legítima y
    disponible: es el catálogo que se está revisando, no un dato de aduana.

    Coincidencia por palabra completa sobre el texto normalizado (solo
    letras/números y espacios, en mayúsculas), para que "NIKEA" no cuente como
    NIKE y "DC SHOES" (dos palabras) sí. Devuelve la marca canónica o None.

    AVISO DE COBERTURA REAL (medido 2026-09-10 sobre el lote Prueba2 /
    proveedor Cachos): las 18 referencias tienen `texto_crudo` = "[sin OCR]
    código del nombre de archivo: ..." y ningún otro campo de texto, así que
    esto detecta CERO marcas ahí. Sirve para catálogos que sí traen texto
    (PDF/Excel con descripción); para el resto la única vía es el control
    manual de la pantalla."""
    if not texto or not marcas:
        return None
    limpio = re.sub(r"[^A-Za-z0-9]+", " ", texto).upper()
    tokens = f" {limpio} "
    # Las más largas primero: "NEW BALANCE" antes que un hipotético "NEW".
    for marca in sorted(marcas, key=len, reverse=True):
        patron = re.sub(r"[^A-Za-z0-9]+", " ", marca).upper().strip()
        if patron and f" {patron} " in tokens:
            return marca
    return None


def autodetectar_marcas_declaradas() -> int:
    """Rellena `marca_declarada` para los candidatos del lote activo que
    todavía no la tengan y cuyo texto de catálogo SÍ nombre una marca
    reconocida. Devuelve cuántas se pudieron declarar (0 es el resultado
    esperado en catálogos sin OCR -- ver `detectar_marca_en_texto`).

    No pisa nunca una marca ya declarada: lo que escribió el comprador manda
    sobre cualquier detección."""
    marcas = marcas_reconocidas()
    if not marcas:
        return 0
    cl = _cl()
    cl.execute("""
        SELECT c.candidato_id, r.codigo_ocr, r.texto_crudo
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        WHERE r.marca_declarada IS NULL OR r.marca_declarada = ''
    """)
    filas = cl.fetchall()
    detectadas = 0
    for candidato_id, codigo_ocr, texto_crudo in filas:
        marca = (detectar_marca_en_texto(codigo_ocr, marcas)
                 or detectar_marca_en_texto(texto_crudo, marcas))
        if not marca:
            continue
        try:
            api_fijar_marca_declarada(candidato_id, marca)
            detectadas += 1
        except Exception:  # noqa: BLE001
            continue
    return detectadas


def fijar_precio_manual(candidato_id: int, precio: float | None) -> dict:
    """El comprador escribe a mano el precio de venta esperado de un
    candidato -- plan 2026-09-07, paso 5. Gana siempre sobre la cascada
    automática (costo declarado / similares TUC / comparables de marca /
    mediana de categoría, ver `precio_venta_referencia`). `precio=None` o
    <=0 lo borra, volviendo a la cascada."""
    return api_fijar_precio_manual(candidato_id, precio)


def obtener_precio_referencia(candidato_id: int) -> tuple[float | None, str | None]:
    """(precio, fuente) para un candidato -- misma cascada que usa el
    Optimizador de Compra. `fuente` es uno de "manual"/"costo_proveedor"/
    "similares_tuc"/"comparables_marca"/"mediana_categoria", o (None, None)."""
    c = conectar(); cur = c.cursor()
    r = precio_venta_referencia(cur, candidato_id)
    c.close()
    return r


def guardar_etiqueta_candidato(candidato_id: int, etiqueta: str,
                                fuente: str = "comprador") -> None:
    """Plan 2026-09-07, paso 2.6 (calibración humana): guarda el juicio real
    del comprador (compraria/dudaria/no_compraria) sobre un candidato, JUNTO
    con una foto del score en ese momento -- para que el análisis posterior
    (`analizar_calibracion.py`, en GestionTUC/pipeline) no dependa de que
    staging_tuc siga teniendo esa fila viva. Clave por
    (proveedor_id, catalogo_origen, codigo_proveedor), no por candidato_id
    (efímero) -- re-etiquetar el mismo candidato actualiza la fila.

    FASE 2: el candidato y su score se leen del `lote.sqlite`; la ETIQUETA
    sigue yendo a `cfg.eval_candidato_etiquetado` en Postgres, y con más razón
    que antes: es el juicio humano que alimenta la calibración del modelo, así
    que debe sobrevivir al lote, no vivir dentro de él.

    Plan 2026-09-18, paso 2: además del score/clasificación, guarda el
    desglose completo por factor (f_soporte/f_tipo/f_color/f_atrib/f_demanda/
    sim_ponderada/k_efectivo/vecinos_detalle) y la carpeta del lote de origen.
    Antes solo se guardaba score_final/clasificacion/version_formula -- los 20
    candidatos etiquetados antes de este cambio (catálogo Reebok) quedaron
    inauditables por eso, porque staging_tuc/el lote de origen ya no existían
    para volver a consultar el desglose. `vecinos_detalle` ya viene como JSON
    (texto) desde el `lote.sqlite`; se reempaqueta con `Json(..., dumps=lambda
    s: s)` para insertarlo tal cual en la columna `jsonb` sin
    doble-serializar."""
    if etiqueta not in ("compraria", "dudaria", "no_compraria"):
        raise ValueError(f"etiqueta inválida: {etiqueta!r}")
    if fuente not in ("comprador", "ia_claude"):
        raise ValueError(f"fuente inválida: {fuente!r}")
    cl = _cl()
    cl.execute("""
        SELECT d.proveedor_id, d.catalogo_origen, d.codigo_proveedor,
               d.categoria_declarada, d.genero_canonico,
               s.score_final, s.clasificacion, s.version_formula,
               s.sim_ponderada, s.k_efectivo, s.n_vecinos,
               s.f_soporte, s.f_tipo, s.f_color, s.f_atrib, s.f_demanda,
               s.factor_mercado, s.filtro_aplicado, s.vecinos_detalle
        FROM candidato d
        LEFT JOIN candidato_score s ON s.candidato_id = d.candidato_id
        WHERE d.candidato_id = ?
    """, (candidato_id,))
    r = cl.fetchone()
    if not r:
        raise ValueError(f"candidato_id={candidato_id} no existe en la revisión de este lote")
    (proveedor_id, catalogo_origen, codigo_proveedor, categoria_declarada, genero_canonico,
     score_final, clasificacion, version_formula,
     sim_ponderada, k_efectivo, n_vecinos,
     f_soporte, f_tipo, f_color, f_atrib, f_demanda,
     factor_mercado, filtro_aplicado, vecinos_detalle_json) = tuple(r)
    lote_carpeta = str(_motor_calif_mod.lote_activo() or "")
    vecinos_detalle = (psycopg2.extras.Json(vecinos_detalle_json, dumps=lambda s: s)
                       if vecinos_detalle_json else None)
    c = conectar(); cur = c.cursor()
    with c:
        cur.execute("""
            INSERT INTO cfg.eval_candidato_etiquetado
                (proveedor_id, catalogo_origen, codigo_proveedor, etiqueta, fuente,
                 score_final, clasificacion, version_formula,
                 sim_ponderada, k_efectivo, n_vecinos,
                 f_soporte, f_tipo, f_color, f_atrib, f_demanda, factor_mercado,
                 filtro_aplicado, vecinos_detalle, categoria_declarada, genero_canonico,
                 lote_carpeta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s)
            ON CONFLICT (proveedor_id, catalogo_origen, codigo_proveedor, fuente) DO UPDATE SET
                etiqueta = EXCLUDED.etiqueta, score_final = EXCLUDED.score_final,
                clasificacion = EXCLUDED.clasificacion, version_formula = EXCLUDED.version_formula,
                sim_ponderada = EXCLUDED.sim_ponderada, k_efectivo = EXCLUDED.k_efectivo,
                n_vecinos = EXCLUDED.n_vecinos, f_soporte = EXCLUDED.f_soporte,
                f_tipo = EXCLUDED.f_tipo, f_color = EXCLUDED.f_color,
                f_atrib = EXCLUDED.f_atrib, f_demanda = EXCLUDED.f_demanda,
                factor_mercado = EXCLUDED.factor_mercado, filtro_aplicado = EXCLUDED.filtro_aplicado,
                vecinos_detalle = EXCLUDED.vecinos_detalle,
                categoria_declarada = EXCLUDED.categoria_declarada,
                genero_canonico = EXCLUDED.genero_canonico,
                lote_carpeta = EXCLUDED.lote_carpeta,
                etiquetado_en = now()
        """, (proveedor_id, catalogo_origen, codigo_proveedor, etiqueta, fuente,
              score_final, clasificacion, version_formula,
              sim_ponderada, k_efectivo, n_vecinos,
              f_soporte, f_tipo, f_color, f_atrib, f_demanda, factor_mercado,
              filtro_aplicado, vecinos_detalle, categoria_declarada, genero_canonico,
              lote_carpeta))
    c.commit(); c.close()


def candidatos_ya_etiquetados() -> set[str]:
    """Códigos (codigo_proveedor) ya etiquetados en el catálogo actualmente
    en revisión -- para que la GUI de calibración no vuelva a preguntar por
    los mismos. Asume un solo (proveedor_id, catalogo_origen) activo, que es
    la norma tras el paso 1 (aislar catálogo).

    FASE 2: qué catálogo está en revisión se pregunta al `lote.sqlite`; qué
    códigos ya se etiquetaron sigue saliendo de `cfg` en Postgres."""
    cl = _cl()
    cl.execute("SELECT DISTINCT proveedor_id, catalogo_origen FROM candidato_raw")
    origenes = cl.fetchall()
    if not origenes:
        return set()
    codigos: set[str] = set()
    c = conectar(); cur = c.cursor()
    try:
        for proveedor_id, catalogo_origen in origenes:
            cur.execute("""
                SELECT codigo_proveedor FROM cfg.eval_candidato_etiquetado
                WHERE proveedor_id = %s AND catalogo_origen = %s
            """, (proveedor_id, catalogo_origen))
            codigos.update(r[0] for r in cur.fetchall())
    finally:
        c.close()
    return codigos


def obtener_desglose_score(candidato_id: int) -> dict | None:
    """Explica un grado -- plan 2026-09-07, paso 9 (ampliado): frase
    reconstruible a mano + los vecinos reales que lo produjeron (con foto y
    unidades vendidas). None si el candidato no tiene score todavía."""
    return api_desglose_score(candidato_id)


def guardar_cotizacion(payload: dict) -> dict:
    """Promueve un candidato de staging a permanente con una cotización real
    (dinero negociado) -- misma acción que `/api/cotizacion`. `payload` sigue
    el mismo contrato: candidato_id, costo_lista, descuento_pct, costo_neto,
    moneda, tipo_cambio_usd_crc."""
    return api_guardar_cotizacion(payload)


def listar_proveedores() -> list[tuple[int, str]]:
    """(proveedor_id, nombre) de todos los proveedores conocidos, para que la
    GUI ofrezca un selector en vez de pedir el número de memoria."""
    c = conectar()
    cur = c.cursor()
    cur.execute("SELECT proveedor_id, nombre FROM silver.dim_tuc_proveedor ORDER BY nombre")
    filas = cur.fetchall()
    c.close()
    return filas
