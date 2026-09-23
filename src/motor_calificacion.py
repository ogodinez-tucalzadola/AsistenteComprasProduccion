"""
motor_calificacion.py
=====================
FASE 1 de la migracion de AsistenteComprasProduccion: copia PROPIA del motor de
calificacion que hasta hoy se importaba desde
`C:\\Users\\Tucalzado\\Proyectos\\GestionTUC\\pipeline\\servidor_pty.py`
(archivo compartido con una herramienta web que ya no nos importa).

QUE ES ESTO
-----------
Es una COPIA FIEL, LETRA POR LETRA, de las lineas 50-3492 de `servidor_pty.py`
(todo el modulo excepto su capa HTTP: `_extraer_campo_multipart`, `Handler`,
`_apagar_todo`, `_limpiar_al_iniciar`, `main`, que empiezan en la linea 3494 bajo
el comentario `# ---------- servidor HTTP ----------`).

Se copio el bloque COMPLETO de logica en vez de solo las 20 funciones que importa
`motor_candidatos.py`, precisamente para no equivocarse eligiendo dependencias
transitivas: esas 20 funciones se llaman entre ellas y a ~50 auxiliares,
constantes y caches globales de este mismo bloque.

NO SE "MEJORO" NADA a proposito. Cualquier cosa que parezca mejorable aca queda
anotada como pendiente para una fase futura, no cambiada ahora. El unico cambio
respecto al original son DOS constantes de ruta que dependian de `__file__`
(`PIPELINE_DIR` y `WEB_DIR`), fijadas a la carpeta de GestionTUC para conservar
EXACTAMENTE el valor que tenian alla; estan marcadas con `# FASE 1:`.

DEPENDENCIAS EXTERNAS QUE NO SE PUDIERON INTERNALIZAR (documentadas a proposito)
-------------------------------------------------------------------------------
Este archivo ya no importa `servidor_pty`, pero sigue importando tres modulos que
son librerias propias compartidas, no el motor:
  * `ingestar_catalogo_tuc`      (GestionTUC/pipeline)      -- OCR de catalogos
  * `vectorizar_imagenes`        (AnalisisMercado/scripts)   -- Vectorizador/embeddings
  * `promover_embeddings_imagen` (AnalisisMercado/scripts)   -- nombrado de color
Copiarlos seria copiar el pipeline de ML entero; queda fuera del alcance de esta
fase (y `motor_candidatos.py` ya importa `ingestar_limpios_tuc` de GestionTUC por
la misma razon). Lo que esta fase elimina es la dependencia de `servidor_pty.py`.

NOTAS PARA FASES FUTURAS (no tocar ahora)
-----------------------------------------
  * `PORT` / `WEB_DIR` son residuos del servidor web y no los usa nadie aca.
  * `api_pendientes`, `api_confirmar_pendiente`, `procesar_catalogo` y los
    detectores/segmentadores ML solo los usaba el flujo web de ingesta; conviene
    revisar en la Fase 2/3 si la app de escritorio los necesita.
  * `_progreso` es estado global en memoria (venia del servidor HTTP).
"""
import io
import decimal
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections import Counter
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np
import psycopg2
import psycopg2.extras

# FASE 2: el catálogo EN REVISIÓN ya no vive en el schema `staging_tuc` de
# Postgres sino en el `lote.sqlite` de cada lote, cuyo dueño es `almacen.py`
# (mismo módulo que ya administra las decisiones y reparaciones del lote).
import almacen

PIPELINE_DIR = Path(r"C:\Users\Tucalzado\Proyectos\GestionTUC\pipeline")  # FASE 1: era
# `Path(__file__).resolve().parent`. Este archivo ya NO vive en esa carpeta, pero el valor
# debe seguir apuntando ahi para que el `sys.path.insert` de abajo siga encontrando
# `ingestar_catalogo_tuc` (modulo aparte de GestionTUC que NO se copio: es del OCR de
# catalogos, no del motor de calificacion).
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(Path(r"C:\Users\Tucalzado\Proyectos\AnálisisMercado\scripts")))

from ingestar_catalogo_tuc import paginas_desde_entrada, detectar_items, parsear_metadata  # noqa: E402
from vectorizar_imagenes import Vectorizador, MODELOS_DISPONIBLES, extraer_color, _kmeans_simple  # noqa: E402
from promover_embeddings_imagen import nombrar_color, _clasificar_centroide, UMBRAL_SECUNDARIO  # noqa: E402

MODELO_SEGMENTADOR = "facebook/sam-vit-base"  # SAM (Meta) vía transformers, checkpoint vit_b
# (~375MB). Se eligió el SAM de `transformers` (ya instalado, v5.13) en vez de los paquetes
# `segment-anything`/`mobile_sam`/`ultralytics`: esos arrastran pins viejos de numpy/opencv sin
# wheels para Python 3.14 (misma incompatibilidad que ya tumbó craft-text-detector/paddlepaddle
# en este venv, ver _get_detector_texto). `transformers.SamModel` soporta box-prompt nativo
# (`input_boxes`) -- justo lo que necesitamos: "dame la máscara del objeto DENTRO de esta caja",
# razonando sobre píxeles reales, no sobre aritmética de rectángulos que se solapan. Probado en
# CPU 2026-07-22: ~7-8s/candidato con el modelo ya cargado, carga única ~26s.
MODELO_DETECTOR_PARES = "google/owlvit-base-patch32"  # NO usar el checkpoint "ensemble" de
# OWLv2 -- promedia varios prompts internamente y es varias veces más lento en CPU para el
# mismo tamaño de imagen (medido: ~30-60s/imagen con "ensemble" vs. este). patch32 (grilla
# más gruesa) es más rápido que patch16 -- suficiente para detección gruesa de props/pares,
# no necesitamos localización milimétrica.
_detector_pares = None
# Lock compartido para toda inicialización perezosa de modelos/cachés globales --
# `ThreadingHTTPServer` atiende requests en hilos concurrentes de verdad (ej. una subida
# en curso + un GET /api/similar_marca de otra pestaña), y sin lock dos hilos podían
# entrar a la vez a un `if _x is None:` y cargar el mismo modelo pesado dos veces (o usar
# uno a medio construir) -- hallazgo de auditoría 2026-07-22. Un solo lock global basta
# (no hay contención real en uso monousuario) en vez de uno por caché.
_lock_inicializacion = threading.Lock()


def get_detector_pares():
    """Detección zero-shot por texto (OWLv2) de instancias de calzado dentro de una
    foto -- reemplaza el intento inicial de componentes conexos/watershed sobre la
    máscara de rembg, que falló empíricamente en fotos reales con pares que se tocan
    (ver [[project_gestiontuc]] §3: rembg fusionó 3 pares en un blob y perdió el par
    blanco por completo). OWLv2 trabaja por cajas delimitadoras, no por máscara --
    más robusto a objetos que se tocan/superponen."""
    global _detector_pares
    with _lock_inicializacion:
        if _detector_pares is None:
            from transformers import pipeline
            print("  Cargando detector de pares (OWLv2, una sola vez)...", flush=True)
            _detector_pares = pipeline("zero-shot-object-detection", model=MODELO_DETECTOR_PARES)
    return _detector_pares


_sam_model = None
_sam_processor = None


def get_segmentador():
    """Carga perezosa (una sola vez, thread-safe) de SAM vía transformers para
    segmentación PROMPTED por caja. Mismo patrón que get_detector_pares/get_vectorizador/
    _get_detector_texto. Retorna (modelo, processor)."""
    global _sam_model, _sam_processor
    with _lock_inicializacion:
        if _sam_model is None:
            from transformers import SamModel, SamProcessor
            print("  Cargando segmentador (SAM vit_b, una sola vez)...", flush=True)
            _sam_model = SamModel.from_pretrained(MODELO_SEGMENTADOR)
            _sam_model.eval()
            _sam_processor = SamProcessor.from_pretrained(MODELO_SEGMENTADOR)
    return _sam_model, _sam_processor


def _mascara_por_segmentacion(img_original, box_owlvit):
    """Segmentación PROMPTED: dada la imagen ORIGINAL (RGB) y UNA caja de OWL-ViT ya
    elegida (`box_owlvit` con xmin/ymin/xmax/ymax), devuelve una máscara binaria booleana
    (mismo alto/ancho que la imagen) del calzado que está DENTRO de esa caja, ignorando el
    resto de la foto -- incluido un calzado vecino aunque su caja de detección se solape en
    pantalla, porque SAM razona sobre el contenido de píxeles, no sobre la geometría de las
    cajas. Reemplaza a `_bbox_por_contorno_alpha` (componentes conexos del alpha de rembg)
    en el camino con múltiples calzados, donde ese enfoque fallaba: si rembg fusionaba dos
    calzados que se solapan en un solo blob, no había forma de decir qué píxel es de cuál.

    Retorna None si SAM no produce ninguna máscara (caso atípico -- el llamador degrada al
    camino anterior)."""
    import torch
    from PIL import Image
    modelo, proc = get_segmentador()
    if img_original.mode != "RGB":
        img_original = img_original.convert("RGB")
    W, H = img_original.size
    bx0 = max(0.0, min(float(W), float(box_owlvit["xmin"])))
    by0 = max(0.0, min(float(H), float(box_owlvit["ymin"])))
    bx1 = max(0.0, min(float(W), float(box_owlvit["xmax"])))
    by1 = max(0.0, min(float(H), float(box_owlvit["ymax"])))
    if bx1 <= bx0 or by1 <= by0:
        return None
    inputs = proc(img_original, input_boxes=[[[bx0, by0, bx1, by1]]], return_tensors="pt")
    with torch.no_grad():
        out = modelo(**inputs)
    masks = proc.image_processor.post_process_masks(
        out.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu())
    if not masks or masks[0].shape[0] == 0:
        return None
    scores = out.iou_scores.cpu().numpy()  # (batch, n_boxes, n_masks)
    m = masks[0][0]  # (n_masks, H, W) -- SAM devuelve 3 propuestas por caja
    mejor = int(scores[0, 0].argmax())
    mascara = m[mejor].cpu().numpy().astype(bool)
    if not mascara.any():
        return None
    return mascara


def _iou(a, b):
    xa, ya = max(a["xmin"], b["xmin"]), max(a["ymin"], b["ymin"])
    xb, yb = min(a["xmax"], b["xmax"]), min(a["ymax"], b["ymax"])
    inter = max(0, xb - xa) * max(0, yb - ya)
    area_a = (a["xmax"] - a["xmin"]) * (a["ymax"] - a["ymin"])
    area_b = (b["xmax"] - b["xmin"]) * (b["ymax"] - b["ymin"])
    return inter / (area_a + area_b - inter + 1e-9)


def _nms(cajas, iou_thresh=0.35):
    cajas = sorted(cajas, key=lambda c: -c["score"])
    quedan = []
    for c in cajas:
        if all(_iou(c, k) < iou_thresh for k in quedan):
            quedan.append(c)
    return quedan


DETECCION_MULTI_COLOR_ACTIVA = False  # ver [[project_gestiontuc]] §3: OWLv2 no convergió
# tras varias rondas de calibración (con NMS estricto pierde pares reales fusionándolos de a 2;
# con NMS laxo sobre-detecta 2x por objeto). Desactivado hasta resolverlo con más tiempo --
# mientras tanto cada candidato se trata como 1 sola variante (comportamiento previo, seguro).

# Banderas de apagado individuales para el plan de atributos de diseño (Etapa 0-6,
# ver plan acordado con el usuario 2026-07-23). Cada fase se activa solo cuando pasó
# su propia verificación; si algo falla en producción se vuelve a False sin afectar
# el resto del pipeline (ni schema ni candidatos ya guardados).
ATRIBUTOS_DISENO = {
    "suela_contraste": True,   # Fase 2 -- delta-E CIEDE2000, ver _suela_contraste
}
UMBRAL_DELTA_E_SUELA_CONTRASTE = 15.0  # calibrar contra catálogo TUC real antes de confiar a ciegas
# Altura relativa (0 = tope del calzado, 1 = piso) hasta donde se considera que
# el centroide de una región pertenece a la CAPELLADA -- lo que cubre el pie.
# NO es una franja de recorte: la segmentación ya encontró las regiones reales
# de color, esto solo decide CUÁL de ellas es la de arriba (ver
# `extraer_colores_por_parte`). 0.55 deja pasar el empeine completo de un
# zapato de tacón/plataforma sin tragarse plantilla ni suela.
UMBRAL_Y_CAPELLADA = 0.55


def detectar_pares_calzado(img, umbral=0.2):
    """Retorna [{score, xmin, ymin, xmax, ymax}, ...] -- una caja por PAR de calzado
    detectado en la imagen, tras NMS. Si no detecta nada (foto atípica), retorna la
    imagen completa como una única "caja" -- degrada con gracia a "1 candidato = 1 foto"
    (el comportamiento de antes de esta migración)."""
    w, h = img.size
    if not DETECCION_MULTI_COLOR_ACTIVA:
        return [{"score": 1.0, "xmin": 0, "ymin": 0, "xmax": w, "ymax": h}]
    detector = get_detector_pares()
    crudo = detector(img, candidate_labels=["shoe", "sandal"], threshold=umbral)
    cajas = [{"score": r["score"], **r["box"]} for r in crudo]
    finales = _nms(cajas)
    if not finales:
        return [{"score": 1.0, "xmin": 0, "ymin": 0, "xmax": w, "ymax": h}]
    return sorted(finales, key=lambda c: c["xmin"])


PROPS_DECORATIVOS = ["flower", "vase", "magazine"]  # menos etiquetas = menos costo (el costo
# de OWL-ViT escala con el número de queries de texto por imagen); probado que "flower" solo
# ya detecta arreglos florales con confianza alta, no hace falta duplicar con sinónimos.
ETIQUETAS_CALZADO = ["footwear", "shoe"]  # "footwear" detecta con MUCHA más confianza que
# "shoe" solo en fotos con varios pares por cuadrante (probado: 0.22-0.31 vs 0.03-0.06 en la
# misma imagen real) -- hallazgo de vocabulario del modelo zero-shot, no específico de un
# catálogo. Se conserva "shoe" como respaldo por si "footwear" falla en otro estilo de foto.
MAX_DIM_DETECCION = 640  # las celdas vienen a 200 DPI (~1000-2200px) -- innecesariamente
# grande para detección gruesa de "hay una flor aquí"/"cuántos pares hay". Reescalar antes
# de inferir es la optimización de mayor impacto en CPU (el costo de OWLv2 crece con el
# área de la imagen); las cajas se reescalan de vuelta a coordenadas originales.


def _detectar_objetos_unificado(img, umbral: float = 0.15):
    """UNA sola pasada de OWLv2 con las etiquetas de props decorativos + calzado juntas
    (antes eran 2 llamadas separadas -- enmascarar_props + contar_pares_aproximado -- el
    doble de costo por imagen para el mismo modelo). Reescala la imagen a
    MAX_DIM_DETECCION antes de inferir (más rápido en CPU) y las cajas de vuelta a
    coordenadas originales al final. Retorna (cajas_props, cajas_calzado) en coordenadas
    de la imagen ORIGINAL (sin reescalar)."""
    detector = get_detector_pares()
    w, h = img.size
    escala = MAX_DIM_DETECCION / max(w, h)
    img_chica = img.resize((max(1, int(w * escala)), max(1, int(h * escala)))) if escala < 1 else img
    inv = 1 / escala if escala < 1 else 1.0

    crudo = detector(img_chica, candidate_labels=PROPS_DECORATIVOS + ETIQUETAS_CALZADO, threshold=umbral)
    props, calzado = [], []
    for r in crudo:
        b = r["box"]
        caja = {"score": r["score"], "label": r["label"], "xmin": b["xmin"] * inv, "ymin": b["ymin"] * inv,
                "xmax": b["xmax"] * inv, "ymax": b["ymax"] * inv}
        (calzado if r["label"] in ETIQUETAS_CALZADO else props).append(caja)
    return props, calzado


_centroide_tuc = None


_centroide_tuc_intentado = False


def get_centroide_tuc(cur):
    """Vector medio L2-normalizado (fashion_siglip) de TODAS las fotos TUC ya vendidas --
    aproxima "cómo se ve una foto de catálogo real" de esta línea. Se usa como oráculo
    para elegir la toma principal cuando una foto trae varios ángulos/pares (Fase 2 de
    la guía Opus 2026-07-22) -- la caja cuyo embedding cae más cerca de esta nube es la
    más parecida a una foto de producto típica, en vez de adivinar por área/posición.
    Cacheado en memoria (no cambia durante la vida del proceso). Retorna None (nunca
    lanza excepción) si todavía no hay ningún embedding TUC en la base -- bug real
    encontrado en auditoría 2026-07-22: sin este guard, un índice TUC vacío hacía
    `np.linalg.norm(V, axis=1)` sobre un array 1-D vacío -> `AxisError`, y como la
    excepción ocurría ANTES de cachear, cada candidato reintentaba y refallaba en
    silencio (todo el candidato se saltaba vía el `except` de vectorizar_y_puntuar_candidatos,
    sin recorte/color/embedding/score). `_elegir_calzado_principal` ya sabía degradar a
    "mayor área" con `centroide_tuc=None` -- el bug era que nunca llegaba a recibir None,
    llegaba una excepción."""
    global _centroide_tuc, _centroide_tuc_intentado
    with _lock_inicializacion:
        if _centroide_tuc is None and not _centroide_tuc_intentado:
            _centroide_tuc_intentado = True
            cur.execute("""
                SELECT vector FROM silver.fct_embedding_imagen
                WHERE modelo_embedding = 'fashion_siglip' AND dominio = 'tuc'
            """)
            filas = cur.fetchall()
            if not filas:
                return None
            V = np.array([r[0] for r in filas], dtype=np.float32)
            V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
            m = V.mean(axis=0)
            _centroide_tuc = m / (np.linalg.norm(m) + 1e-9)
    return _centroide_tuc


def _elegir_calzado_principal(cajas_calzado, img_rgba_completa=None, vec=None, centroide_tuc=None, umbral=0.2):
    """Cuando una sola foto trae varios pares/ángulos, el pedido del usuario es mostrar
    y vectorizar SOLO uno -- en vez de mezclar todo en un solo embedding/imagen (lo que
    ya se sabía impreciso, ver §3). No requiere separar TODAS las instancias de forma
    confiable (eso falló, sigue desactivado vía DETECCION_MULTI_COLOR_ACTIVA) -- alcanza
    con elegir UNA caja ya detectada por _detectar_objetos_unificado.

    Criterio (Fase 2, 2026-07-22): si hay más de una candidata Y se recibió el contexto
    de embedding (img_rgba_completa+vec+centroide_tuc), se recorta cada caja, se vectoriza
    en fashion_siglip y se elige la de mayor similitud coseno contra el centroide del
    catálogo TUC ya vendido -- esa nube es casi toda fotos de perfil limpias, así que la
    caja más parecida a ella es la toma principal real, no la de mayor área. Bug real que
    esto corrige: "área más grande" elegía sistemáticamente la foto de ángulo/exhibición
    en vitrina en vez del perfil normal (el ángulo de vitrina se veía más grande en el
    encuadre). Si no hay contexto de embedding (llamada antigua/tests), degrada a MAYOR
    ÁREA (comportamiento de la corrección anterior). Retorna None si no se detectó ningún
    calzado con confianza suficiente (foto atípica -- el llamador usa la imagen completa
    como fallback)."""
    candidatas = [c for c in cajas_calzado if c["score"] >= umbral]
    if not candidatas:
        return None
    if len(candidatas) == 1 or img_rgba_completa is None or vec is None or centroide_tuc is None:
        return max(candidatas, key=lambda b: (b["xmax"] - b["xmin"]) * (b["ymax"] - b["ymin"]))

    from PIL import Image
    mejor, mejor_score = None, -1.0
    for c in candidatas:
        recorte = img_rgba_completa.crop((int(c["xmin"]), int(c["ymin"]), int(c["xmax"]), int(c["ymax"])))
        fondo = Image.new("RGB", recorte.size, (255, 255, 255))
        fondo.paste(recorte, mask=recorte.split()[3])
        emb = np.array(vec.vectorizar_pil_siglip(fondo), dtype=np.float32)
        emb /= (np.linalg.norm(emb) + 1e-9)
        sim = float(emb @ centroide_tuc)
        w, h = recorte.size
        ar = w / max(h, 1)
        ar_bonus = 0.05 if 1.2 <= ar <= 1.9 else 0.0  # bonus suave: perfil de calzado típico
        total = sim + ar_bonus
        if total > mejor_score:
            mejor, mejor_score = c, total
    return mejor


def detectar_instancias_calzado(img_original, cajas_calzado, umbral=0.15):
    # umbral bajado de 0.2->0.15 (2026-07-23): bug real encontrado con foto de 4 sandalias
    # -- el 4to par (score genuino 0.175) quedaba fuera con el umbral anterior y la
    # Referencia se mostraba con solo 3 de 4 colores, sin avisar que faltaba uno. Evidencia
    # real: las 4 cajas verdaderas de esa foto cayeron en el rango 0.175-0.293. 0.15 da
    # margen sin bajar tanto como para aceptar ruido de fondo (los props/fondo en pruebas
    # anteriores puntuaban mucho más bajo, <0.10).
    """Intento v2 (2026-07-23) de separar cada PAR FÍSICO distinto de calzado dentro de
    una misma foto -- pedido explícito del usuario: si una Referencia (codigo_proveedor)
    trae 3 colores en la foto, generar 3 variantes reales, cada una con su propio color/
    suela/contraste, todas bajo el mismo candidato_id (misma Referencia).

    Intento anterior (ver DETECCION_MULTI_COLOR_ACTIVA, sigue desactivado) decidía "es el
    mismo par" SOLO por superposición de CAJAS 2D de OWL-ViT -- NMS estricto fusionaba
    pares reales cercanos, NMS laxo duplicaba el mismo par 2x. Este intento reutiliza la
    MISMA idea que ya resolvió el bug de "dos calzados fusionados en una foto" (ver
    _mascara_por_segmentacion, SAM box-prompt validado y confirmado por el usuario):
    decidir por superposición de MÁSCARAS reales (píxeles), no de cajas. Dos cajas cuya
    máscara SAM casi no se solapa son dos calzados físicos reales (aunque sus cajas 2D se
    crucen en pantalla -- vitrinas, ángulos); dos cajas cuya máscara SÍ se solapa mucho
    (>50% del área del menor) son la MISMA detección duplicada de OWL-ViT -- se descarta
    la de menor score (candidatas ya vienen ordenadas score desc).

    Retorna [(caja, mascara_bool)] ordenado de izquierda a derecha (xmin), o [] si no hay
    ninguna caja con score suficiente o SAM no produjo ninguna máscara utilizable -- el
    llamador debe degradar con gracia al camino de 1 sola instancia (comportamiento
    previo, seguro) cuando esto retorna vacío."""
    candidatas = sorted([c for c in cajas_calzado if c["score"] >= umbral], key=lambda c: -c["score"])
    if not candidatas:
        return []
    instancias = []  # [(caja, mascara)]
    for c in candidatas:
        m = _mascara_por_segmentacion(img_original, c)
        if m is None or not m.any():
            continue
        duplicado = False
        for _, m2 in instancias:
            inter = int((m & m2).sum())
            solapamiento = inter / max(1, min(int(m.sum()), int(m2.sum())))
            if solapamiento > 0.5:
                duplicado = True
                break
        if not duplicado:
            instancias.append((c, m))
    return sorted(instancias, key=lambda t: t[0]["xmin"])


UMBRAL_DELTA_E_MISMO_COLOR = 4.0  # por debajo de esto, dos instancias del mismo molde se
# consideran el MISMO color (no dos variantes distintas) -- ver _consolidar_mismo_color.
# Subido a la baja de 12->4 (2026-07-24, regresión real reportada por el usuario): con
# 12, una foto de 4 sandalias en tonos pastel (nude/blanco/blanco/rosa) colapsó a solo 2
# colores -- el delta-E entre nude y dos blancos DISTINTOS (5.95, 3.66) cayó bajo ese
# umbral, tan bajo como el delta-E entre el MISMO par en dos ángulos (el caso que motivó
# esto). Un umbral global no puede separar de forma confiable "mismo color, otro ángulo"
# de "dos colores pastel genuinamente distintos pero parecidos" -- el ruido de medición
# (recorte, ángulo, luz) se solapa con la señal real en ese rango. Se prefiere ERRAR hacia
# NO fusionar (mostrar de más un ángulo duplicado del mismo color es un error menor) que
# fusionar colores reales que el proveedor sí ofrece por separado (error de negocio
# mucho peor: esconder una opción de compra real). 4.0 solo fusiona cuando el color es
# prácticamente IDÉNTICO.


UMBRAL_AREA_NO_EXPLICADA = 0.15  # fracción máxima del área de una instancia que puede
# quedar "sin explicar" (sin ningún color parecido en la otra) para seguir considerándolas
# el mismo color -- ver _consolidar_mismo_color.


def _firma_color_instancia(img_original, mascara, umbral_area_relevante=0.08):
    """Firma de color de una instancia: TODAS sus regiones de color relevantes (no solo
    la dominante) -- bug real 2026-07-24 (botas negro+acento amarillo/gris/rojo/naranja):
    comparar solo el centroide dominante fallaba porque las 4 botas comparten el MISMO
    negro dominante (>45% de área en las 4) y solo se distinguen por el color de ACENTO
    (secundario en área) -- con un solo centroide, las 4 parecían "el mismo color".
    Reutiliza `segmentar_regiones_color` (ya construida y verificada). Retorna
    [(lab, area_pct), ...] de las regiones con área >= umbral_area_relevante."""
    from PIL import Image
    arr = np.array(img_original.convert("RGB"))
    alpha = mascara.astype(np.uint8) * 255
    rgba = Image.fromarray(np.dstack([arr, alpha]), mode="RGBA")
    regiones = segmentar_regiones_color(rgba)
    return [(r["lab"], r["area_pct"]) for r in regiones if r["area_pct"] >= umbral_area_relevante]


def _mismo_color_por_firma(firma_a, firma_b, umbral_delta_e=UMBRAL_DELTA_E_MISMO_COLOR,
                          umbral_area_no_explicada=UMBRAL_AREA_NO_EXPLICADA):
    """Dos instancias son "el mismo color" solo si CADA región relevante de una tiene una
    región de color parecido (delta-E bajo) en la otra -- si una bota es negro+amarillo y
    otra es negro+gris, el negro se explica mutuamente pero el amarillo/gris no tienen
    pareja -> quedan "sin explicar" y NO se consideran el mismo color, aunque compartan el
    color dominante. Retorna True solo si la fracción de área sin explicar (en cualquiera
    de las dos direcciones) es baja."""
    from skimage.color import deltaE_ciede2000

    def _area_no_explicada(f1, f2):
        total = 0.0
        for lab1, area1 in f1:
            if not any(float(deltaE_ciede2000(np.array(lab1), np.array(lab2))) < umbral_delta_e
                       for lab2, _ in f2):
                total += area1
        return total

    if not firma_a or not firma_b:
        return False
    peor = max(_area_no_explicada(firma_a, firma_b), _area_no_explicada(firma_b, firma_a))
    return peor <= umbral_area_no_explicada


def _consolidar_mismo_color(img_original, instancias):
    """Colapsa instancias que son el MISMO calzado fotografiado desde ángulos distintos --
    pedido explícito del usuario 2026-07-24: "un mismo calzado puede tener dos [pares]
    desde diferentes ángulos... es el mismo calzado tomado de dos perspectivas distintas".
    `detectar_instancias_calzado` ya separa correctamente objetos físicos distintos en la
    foto (por solapamiento de MÁSCARA, no de caja) -- pero dos ángulos del mismo par SON
    dos objetos físicamente distintos en la foto (no se tocan, no solapan), así que esa
    separación por sí sola no basta para saber si son "colores distintos" o "el mismo
    color, otro ángulo". La señal que sí distingue esto es el COLOR -- comparando TODAS
    las regiones relevantes (`_mismo_color_por_firma`), no solo la dominante (ver bug real
    de las 4 botas negro+acento arriba). Si son el mismo color, se descarta la de menor
    calidad (menos área de máscara) y se conserva solo la mejor.

    Retorna la lista de instancias filtrada, mismo formato [(caja, mascara_bool)]."""
    if len(instancias) <= 1:
        return instancias
    firmas = [_firma_color_instancia(img_original, mascara) for _, mascara in instancias]

    orden = sorted(range(len(instancias)), key=lambda i: -int(instancias[i][1].sum()))
    descartadas = set()
    for pos_i, i in enumerate(orden):
        if i in descartadas:
            continue
        for j in orden[pos_i + 1:]:
            if j in descartadas:
                continue
            if _mismo_color_por_firma(firmas[i], firmas[j]):
                descartadas.add(j)  # mismo color, ángulo distinto -> se queda la de más área
    return [ins for k, ins in enumerate(instancias) if k not in descartadas]


def _extraer_detalle_flor(img_original, cajas_props, x0, y0, x1, y1, umbral=0.15):
    """Recorta el adorno tipo FLOR de esta instancia, si OWL-ViT detectó uno dentro de su
    zona -- pedido explícito del usuario 2026-07-23: "este modelo tiene un detalle de una
    flor, podemos extraer ese detalle de la flor por separado". Reutiliza las cajas de
    PROPS_DECORATIVOS que `_detectar_objetos_unificado` ya detecta (antes solo se usaban
    para BORRAR el adorno del fondo/embedding -- aquí además se guarda como recorte propio).
    Elige la caja "flower" cuyo CENTRO cae dentro de (x0,y0,x1,y1) -- así, si la foto trae
    3 pares con 3 flores, cada instancia se queda con SU flor, no la de la vecina. Recorta
    de la imagen ORIGINAL (no la aislada) porque el adorno puede estar parcialmente fuera
    del alpha del calzado (ej. pétalos que sobresalen). Retorna la imagen recortada (RGB)
    o None si no hay ninguna flor dentro de esta zona."""
    candidatas = [p for p in cajas_props if p.get("label") == "flower" and p["score"] >= umbral]
    if not candidatas:
        return None
    dentro = []
    for p in candidatas:
        cx, cy = (p["xmin"] + p["xmax"]) / 2, (p["ymin"] + p["ymax"]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            dentro.append(p)
    if not dentro:
        return None
    mejor = max(dentro, key=lambda p: p["score"])
    W, H = img_original.size
    mx = (mejor["xmax"] - mejor["xmin"]) * 0.10
    my = (mejor["ymax"] - mejor["ymin"]) * 0.10
    fx0, fy0 = max(0, int(mejor["xmin"] - mx)), max(0, int(mejor["ymin"] - my))
    fx1, fy1 = min(W, int(mejor["xmax"] + mx)), min(H, int(mejor["ymax"] + my))
    if fx1 <= fx0 or fy1 <= fy0:
        return None
    return img_original.crop((fx0, fy0, fx1, fy1))


def contar_pares_aproximado(cajas_calzado, iou_thresh=0.35) -> int:
    """QC liviano: cuenta cuántos pares de calzado parece haber en la foto (NO separa,
    solo cuenta), a partir de las cajas ya detectadas por _detectar_objetos_unificado --
    para poder advertir '¿esta foto trae más de un par?' aunque la separación real
    (DETECCION_MULTI_COLOR_ACTIVA) siga desactivada."""
    return len(_nms([c for c in cajas_calzado if c["score"] >= 0.2], iou_thresh))


def enmascarar_props(img, cajas_props):
    """Pinta de blanco los objetos decorativos ya detectados por _detectar_objetos_unificado
    (flores, floreros, revistas, libros...) ANTES de quitar el fondo -- reduce el ruido que
    meten al embedding y al color. No requiere que las cajas sean exactas, con que cubran
    el objeto alcanza -- caso de uso mucho más tolerante que separar pares que se tocan."""
    from PIL import ImageDraw
    relevantes = [c for c in cajas_props if c["score"] >= 0.2]
    if not relevantes:
        return img
    img = img.copy()
    draw = ImageDraw.Draw(img)
    for b in relevantes:
        draw.rectangle([b["xmin"], b["ymin"], b["xmax"], b["ymax"]], fill=(255, 255, 255))
    return img


_easyocr_reader = None


def _get_detector_texto():
    """Detector de texto real (EasyOCR, basado en CRAFT) -- reemplaza el enfoque anterior
    (Tesseract `--psm 11` reutilizado como detector, Ronda 10). Tesseract NUNCA fue
    diseñado para DETECTAR texto (solo para leerlo asumiendo que ya lo encontraron) --
    por eso alucinaba "texto" sobre costuras/texturas del calzado (confianza baja
    necesaria para no perder watermarks tenues, pero esa misma sensibilidad disparaba
    sobre patrones de cuero). CRAFT/EasyOCR sí fue entrenado para separar texto de fondo,
    con mucha menos tendencia a falsos positivos sobre textura.
    Intentos previos de instalar `craft-text-detector` y `paddlepaddle` (recomendados por
    la guía Opus 2026-07-22) fallaron en este venv -- ambos requieren versiones de
    `numpy`/`opencv-python` incompatibles con Python 3.14 (demasiado nuevo, sin wheels
    publicados). `easyocr` sí instaló limpio (usa `opencv-python-headless` moderno) y
    usa internamente la misma arquitectura CRAFT para el detector -- mismo resultado
    técnico, dependencia distinta por restricción real del entorno."""
    global _easyocr_reader
    with _lock_inicializacion:
        if _easyocr_reader is None:
            import easyocr
            print("  Cargando detector de texto (EasyOCR/CRAFT, una sola vez)...", flush=True)
            _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader


def enmascarar_texto_ocr(img, confianza_min=None):
    """Pinta de blanco cualquier texto impreso EN LA FOTO misma (código, talla, empaque,
    marca watermark) -- pedido del usuario: la referencia/talla no debe verse en la
    imagen mostrada. `reader.detect()` es SOLO detección (sin reconocer el texto, más
    rápido que un OCR completo) y da polígonos ajustados al texto real -- no rectángulos
    inflados como el enfoque anterior. `confianza_min` se conserva en la firma por
    compatibilidad con el llamador pero ya no se usa (el detector no da un score de
    confianza por caja, solo geometría)."""
    import numpy as np
    from PIL import ImageDraw
    reader = _get_detector_texto()
    arr = np.array(img.convert("RGB"))
    horizontal_list, free_list = reader.detect(arr)
    img = img.copy()
    draw = ImageDraw.Draw(img)
    pad = 3
    for x0, x1, y0, y1 in horizontal_list[0]:
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=(255, 255, 255))
    for poligono in free_list[0]:
        draw.polygon([tuple(p) for p in poligono], fill=(255, 255, 255))
    return img


PORT = 8900
WEB_DIR = Path(r"C:\Users\Tucalzado\Proyectos\GestionTUC\data")  # FASE 1: era
# `Path(__file__).resolve().parent.parent / "data"`. Conserva el MISMO valor que tenia en
# servidor_pty.py (solo lo usaba el Handler HTTP, que no se copio).
DATOS_COMPARTIDOS = Path(r"C:\Users\Tucalzado\Proyectos\DatosCompartidos")
CARPETA_CROPS = "tuc_catalogo_staging"
DB_PARAMS = dict(host="127.0.0.1", port=5432, dbname="tcmarcas",
                  user="tcm_etl", password="tcmarcas2025!")
MODELOS = ["fashion_siglip", "dino_v2"]
MODELO_TI = ["dinov2_ti"]
CONFIANZA_AUTO_APROBAR = 80

_vectorizador = None
_vectorizador_ti = None

# Estado global de progreso de la subida en curso -- polled por el frontend cada ~700ms
# (GET /api/progreso) para mostrar una barra REAL con pasos, no solo un cronómetro
# indeterminado (pedido explícito del usuario 2026-07-22, dos veces). "Un catálogo a la
# vez" (Ronda 3) ya implica que solo hay una subida en curso, así que un solo dict global
# (sin locks) alcanza -- no hay dos subidas concurrentes por diseño.
_progreso = {"activo": False, "paso": 0, "total": 1, "mensaje": "", "sub": 0, "sub_total": 0}


def _reportar_progreso(paso: int, total: int, mensaje: str, sub: int = 0, sub_total: int = 0):
    # sub/sub_total = progreso DENTRO del paso actual (ej. candidato i/N, o micro-fase de
    # UN candidato lento). El frontend lo usa para interpolar el ancho de la barra y para
    # que el mensaje cambie cada pocos segundos aunque el paso mayor no avance -- evita la
    # sensación de "colgado" durante la vectorización, que es lo más lento.
    _progreso.update(activo=True, paso=paso, total=total, mensaje=mensaje,
                     sub=sub, sub_total=sub_total)
    print(f"  [progreso {paso}/{total}{f' · {sub}/{sub_total}' if sub_total else ''}] {mensaje}", flush=True)


def conn():
    return psycopg2.connect(**DB_PARAMS)


# ═══════════════════════════════════════════════════════════════════════════
# El catálogo EN REVISIÓN vive en el `lote.sqlite` del lote activo (Fase 2)
# ═══════════════════════════════════════════════════════════════════════════
#
# Antes vivía en el schema `staging_tuc` de Postgres: una sola copia global,
# compartida entre esta app y el servidor web viejo, que se borraba por
# completo (TRUNCATE) cada vez que alguien cargaba otro catálogo. Eso obligaba
# a la regla de "un solo catálogo activo a la vez" y hacía que retomar un lote
# viejo encontrara la tabla vacía o, peor, llena con el catálogo de otro lote.
#
# Ahora cada lote guarda su propia revisión en su propio archivo, gestionado
# por `almacen.py` -- el mismo `lote.sqlite` donde ya viven las decisiones y
# reparaciones de ese lote. Lo que NO se mueve: el catálogo permanente de TU
# Calzado contra el que se comparan los candidatos (`silver.dim_tuc_producto`,
# `silver.fct_embedding_imagen`, `gold.agg_tuc_metricas`, las tablas `cfg.*`),
# que sigue en Postgres y se sigue leyendo de ahí -- es dato permanente de la
# empresa, no dato de exploración de un lote.
#
# Cómo sabe una función CUÁL lote: `fijar_lote(carpeta)` lo declara una vez
# (la GUI lo llama al elegir/retomar la carpeta de trabajo) y todas las
# funciones lo consultan por `_cl()`. Se elige estado de módulo, y no un
# parámetro más en cada firma, por consistencia: la conexión a Postgres
# (`conn()`), los vectorizadores y los cachés de prototipos YA son estado de
# módulo en este archivo, y la mitad de las funciones públicas (`api_*`) no
# reciben cursor alguno. Agregar un parámetro obligatorio a las 20 firmas
# habría cambiado la interfaz de todos los llamadores sin ganar nada: no hay
# dos lotes abiertos a la vez en una app de escritorio de una sola ventana.
#
# LIMITACIÓN CONOCIDA Y ACEPTADA (medida en la Fase 4, no supuesta).
#
# Consecuencia exacta de que `_lote_salida` sea estado de MÓDULO: hay UN SOLO
# LOTE ACTIVO POR PROCESO. Dos hilos del mismo proceso no pueden sostener dos
# lotes distintos -- el último que llama a `fijar_lote` le cambia el lote a
# todos. Comprobado: el hilo A fijó un lote de 2 candidatos y leyó 2; el hilo
# B fijó otro lote; A volvió a leer y vio 0 (los del lote de B), porque
# `_lote_gen` invalidó también la conexión de A.
#
# Esto NO es un resto de la vieja regla "un solo catálogo a la vez en la base
# compartida" (eso murió en la Fase 2, y dos lotes en PROCESOS separados o uno
# después del otro ya conviven sin pisarse -- también comprobado). Es una
# limitación interna de la app, y hoy no molesta: una ventana = un lote, y el
# comprador trabaja un lote a la vez en su propia sesión.
#
# Y NO se arregla haciendo `_lote_salida` thread-local, que es la tentación
# obvia: la GUI DEPENDE de que el lote se comparta entre hilos (el hilo de Tk
# llama a `fijar_lote` al elegir la carpeta, y el hilo de envío lo consume
# después). Volverlo thread-local rompería el envío. El arreglo de verdad es
# pasar el lote por parámetro en las ~20 firmas públicas, o encapsular el
# motor en una clase con el lote como atributo -- ninguno de los dos es
# barato, y no hay necesidad real que lo justifique.

_lote_salida: Path | None = None   # carpeta de trabajo del lote activo

# La conexión al `lote.sqlite` es POR HILO, no una sola de módulo.
#
# Bug real corregido (paso 5 -> «Continuar»): sqlite3 abre sus conexiones con
# `check_same_thread=True`, así que una conexión solo se puede usar desde el
# mismo hilo que la creó. La GUI llamaba primero a lectores del lote desde el
# hilo de Tk (`_lote_ya_enviado`, `modelo_activo_configurado`,
# `_confirmar_reemplazo_catalogo`), que dejaban la conexión cacheada a nombre
# de ESE hilo, y después arrancaba el envío en un `threading.Thread` que
# volvía a pedir la misma conexión -> `ProgrammingError: SQLite objects
# created in a thread can only be used in that same thread`.
#
# Se elige una conexión por hilo (y no `check_same_thread=False`) porque acá
# los dos hilos SÍ se pueden solapar: mientras el hilo de envío escribe
# candidatos, el hilo de Tk sigue atendiendo la pantalla y puede leer el lote.
# Compartir una conexión entre hilos que se solapan necesitaría además un lock
# propio; con una conexión por hilo el aislamiento lo da SQLite (el archivo
# está en WAL con `busy_timeout=20000`, que es justo el caso de varios
# lectores y un escritor sobre el mismo archivo).
#
# `_lote_gen` es la generación del lote activo: `fijar_lote`/`cerrar_lote`
# la incrementan, y cada hilo compara la suya al pedir cursor. Así, cambiar de
# lote invalida también las conexiones de los otros hilos sin tener que
# cerrarlas desde afuera (cerrar una conexión ajena volvería a violar la
# misma regla de sqlite3). La conexión de un hilo que muere se libera cuando
# se libera su almacenamiento local.
_lote_gen: int = 0
_lote_tls = threading.local()


def _cerrar_con_de_este_hilo() -> None:
    """Cierra la conexión que ESTE hilo tenga abierta (nunca la de otro)."""
    con = getattr(_lote_tls, "con", None)
    if con is not None:
        try:
            con.close()
        except Exception:  # noqa: BLE001 -- cerrar nunca debe tumbar al llamador
            pass
    _lote_tls.con = None
    _lote_tls.gen = None


def fijar_lote(salida) -> None:
    """Declara cuál carpeta de lote es la activa; cierra la anterior si había.

    Idempotente: volver a fijar la MISMA carpeta no reabre nada (la GUI puede
    llamarlo en cada cambio de pantalla sin costo).
    """
    global _lote_salida, _lote_gen
    nueva = Path(salida) if salida is not None else None
    if nueva is not None and _lote_salida is not None and nueva == _lote_salida:
        return
    cerrar_lote()
    _lote_salida = nueva
    _lote_gen += 1


def lote_activo() -> Path | None:
    return _lote_salida


_log = logging.getLogger("motor_calificacion")
_log.setLevel(logging.INFO)
_log_lote_actual: Path | None = None


def _logger() -> logging.Logger:
    """Logger del motor -- plan de mejora 2026-09-22, Etapa 3: antes los
    fallos (candidato que no se pudo vectorizar, canal de venta ilegible)
    solo se imprimían con `print()`, que en la GUI de escritorio no tiene
    consola visible -- el fallo quedaba sin ningún rastro. Escribe a
    `<lote>/motor.log`, cambiando de archivo cuando cambia el lote activo
    (mismo criterio que `_cl()` para saber cuál es el lote de ahora)."""
    global _log_lote_actual
    lote = lote_activo()
    if lote != _log_lote_actual:
        for h in list(_log.handlers):
            _log.removeHandler(h)
            h.close()
        if lote is not None:
            try:
                handler = logging.FileHandler(Path(lote) / "motor.log", encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                _log.addHandler(handler)
            except Exception:  # noqa: BLE001 -- no poder loguear no puede tumbar al motor
                pass
        _log_lote_actual = lote
    return _log


def cerrar_lote() -> None:
    global _lote_salida, _lote_gen
    _cerrar_con_de_este_hilo()
    _lote_salida = None
    _lote_gen += 1


def _cl():
    """Cursor al `lote.sqlite` del lote activo (`c`atálogo `l`ocal).

    Falla con un mensaje explícito si nadie fijó el lote: es un error de
    programación del llamador, y prefiero que se vea en la primera prueba a
    que el motor invente un lote vacío y devuelva "no hay candidatos" -- ese
    silencio es exactamente el tipo de bug que la Fase 2 viene a cerrar.

    La conexión que devuelve es la de ESTE hilo (ver la nota de arriba).
    """
    if _lote_salida is None:
        raise RuntimeError(
            "No hay lote activo: llamá motor_calificacion.fijar_lote(carpeta) "
            "antes de usar el motor de calificación.")
    con = getattr(_lote_tls, "con", None)
    if con is None or getattr(_lote_tls, "gen", None) != _lote_gen:
        if con is not None:
            _cerrar_con_de_este_hilo()  # quedó apuntando a un lote viejo
        _lote_tls.con = almacen.conexion(_lote_salida)
        _lote_tls.gen = _lote_gen
    return _lote_tls.con.cursor()


def _en(valores) -> str:
    """`(?, ?, ?)` para un IN de N elementos -- el equivalente en SQLite del
    `= ANY(%s)` de Postgres, que no existe acá."""
    return "(" + ",".join("?" * len(valores)) + ")"


def get_vectorizador():
    global _vectorizador
    with _lock_inicializacion:
        if _vectorizador is None:
            print("  Cargando modelos ML (una sola vez)...", flush=True)
            _vectorizador = Vectorizador(MODELOS, quitar_fondo=True, determinista=True)
            _vectorizador.cargar()
    return _vectorizador


def get_vectorizador_ti():
    """Instancia SEPARADA para `dinov2_ti` -- NO se puede compartir con
    `get_vectorizador()` porque TI no quita fondo (vectoriza la foto tal cual
    la recibe) mientras que el vectorizador estándar sí (`quitar_fondo=True`
    es atributo de instancia, no un parámetro por llamada). Usado solo por
    AsistenteComprasEscritorio cuando el método activo es 'ti' -- el flujo
    web de catálogos de proveedor (GestionTUC/PTY) nunca la toca."""
    global _vectorizador_ti
    with _lock_inicializacion:
        if _vectorizador_ti is None:
            print("  Cargando modelo TI (dinov2_ti, una sola vez)...", flush=True)
            _vectorizador_ti = Vectorizador(MODELO_TI, quitar_fondo=False, determinista=True)
            _vectorizador_ti.cargar()
    return _vectorizador_ti


# ---------- pipeline completo al subir un catálogo (todo en staging_tuc) ----------

def _vaciar_revision(proveedor_id: int | None = None,
                     catalogo_origen: str | None = None) -> list[str]:
    """Borra la revisión de catálogo de ESTE lote (y solo de este lote).

    FASE 2 multi-proveedor (2026-09-16) -- el borrado ahora puede ser PARCIAL.
    Un mismo lote puede tener el catálogo de varios proveedores conviviendo
    (`_proveedores_del_lote` en la GUI), y hasta esta fase reprocesar el
    segundo proveedor borraba la calificación ya hecha del primero: este
    `DELETE` no tenía ningún `WHERE`. Ahora:

      - `_vaciar_revision()` (los dos parámetros en None) = comportamiento
        histórico, vacía TODO el lote. Sigue siendo el caso legítimo de
        "reprocesar el lote entero desde cero" y el que usa
        `procesar_catalogo` (un solo catálogo por lote).
      - `_vaciar_revision(proveedor_id=N)` = solo las filas de ESE proveedor,
        con cualquier `catalogo_origen`. Es lo que necesita "reprocesar el
        proveedor N con un origen distinto" sin tocar a los demás
        proveedores del lote.
      - `catalogo_origen` además acota a un origen puntual.

    Devuelve las rutas (`crop_imagen_path`) de las filas borradas, para que
    quien llama pueda limpiar del disco SOLO esas fotos en vez de vaciar la
    carpeta de recortes entera (que es compartida por todos los proveedores
    del lote).

    FASE 2 -- reemplaza a `_truncar_staging`, que hacía un TRUNCATE del schema
    `staging_tuc` completo: una tabla GLOBAL compartida, así que cargar un
    catálogo en un lote borraba la revisión de cualquier otro. Ese era el
    precio de la regla "un solo catálogo activo a la vez". Ahora cada lote
    tiene su propio archivo y esto vacía nada más el suyo.

    El orden de borrado respeta las claves foráneas (embedding -> variante ->
    score -> candidato -> raw) para no depender del ON DELETE CASCADE: con
    `PRAGMA foreign_keys=ON` el cascade funciona, pero borrar explícito deja
    dicho en el código qué se va y en qué orden.
    """
    cl = _cl()
    cond, params = [], []
    if proveedor_id is not None:
        cond.append("proveedor_id = ?")
        params.append(proveedor_id)
    if catalogo_origen is not None:
        cond.append("catalogo_origen = ?")
        params.append(catalogo_origen)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""

    cl.execute(f"SELECT crop_imagen_path FROM candidato_raw{where}", params)
    rutas = [r[0] for r in cl.fetchall() if r[0]]

    # `candidato` se acota por su propia copia de (proveedor_id,
    # catalogo_origen) -- las tres tablas hijas cuelgan de él por
    # `candidato_id`, así que se acotan con un subselect sobre el mismo
    # alcance. Sin alcance el subselect es toda la tabla y esto queda
    # equivalente al borrado total de antes.
    sub = f"SELECT candidato_id FROM candidato{where}"
    cl.execute(f"DELETE FROM candidato_embedding WHERE variante_id IN "
               f"(SELECT variante_id FROM candidato_variante "
               f"WHERE candidato_id IN ({sub}))", params)
    cl.execute(f"DELETE FROM candidato_variante WHERE candidato_id IN ({sub})", params)
    cl.execute(f"DELETE FROM candidato_score WHERE candidato_id IN ({sub})", params)
    cl.execute(f"DELETE FROM candidato{where}", params)
    cl.execute(f"DELETE FROM candidato_raw{where}", params)
    return rutas


# Nota (plan de mejora 2026-09-22, Etapa 6): sin llamador en la app de
# escritorio (gui_profesional_ctk.py/motor_candidatos.py) -- es parte del
# subsistema de ingesta OCR de catálogo de proveedor, exclusivo de
# servidor_pty.py (llamada real en GestionTUC/pipeline/servidor_pty.py). No
# es código muerto: no se borra.
def procesar_catalogo(path: Path, proveedor_id: int, catalogo_origen: str) -> dict:
    _reportar_progreso(0, 4, "Leyendo el archivo y preparando...")
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT nombre FROM silver.dim_tuc_proveedor WHERE proveedor_id = %s", (proveedor_id,))
    if not cur.fetchone():
        c.close()
        raise ValueError(f"proveedor_id={proveedor_id} no existe")

    # FASE 2: el catálogo anterior EN REVISIÓN DE ESTE LOTE desaparece por
    # completo -- ya no el de todos los lotes (ver `_vaciar_revision`).
    _vaciar_revision()

    carpeta_salida = DATOS_COMPARTIDOS / CARPETA_CROPS
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    for viejo in carpeta_salida.glob("*"):
        viejo.unlink(missing_ok=True)

    _reportar_progreso(1, 4, "Leyendo el código, talla y empaque (OCR)...")
    paginas = paginas_desde_entrada(path)
    filas = []
    for num_pagina, img in enumerate(paginas, start=1):
        for idx, item in enumerate(detectar_items(img), start=1):
            nombre_archivo = f"pagina_{num_pagina:03d}_item_{idx}.png"
            ruta_relativa = f"{CARPETA_CROPS}/{nombre_archivo}"
            item["imagen_celda"].save(carpeta_salida / nombre_archivo)
            meta = parsear_metadata(item["texto_crudo"])  # talla_min/max + empaque -- antes se ignoraba por completo
            filas.append({
                "proveedor_id": proveedor_id, "catalogo_origen": catalogo_origen, "pagina": num_pagina,
                "bbox": {"x0": item["bbox"][0], "y0": item["bbox"][1], "x1": item["bbox"][2], "y1": item["bbox"][3]},
                "codigo_ocr": item["codigo"], "confianza_ocr": item["confianza"],
                "crop_imagen_path": ruta_relativa, "texto_crudo": item["texto_crudo"],
                "fecha_ingesta": date.today(), **meta,
            })

    # FASE 2: la ingesta escribe en el `lote.sqlite`. `execute_batch` de
    # psycopg2 pasa a ser `executemany` de sqlite3 (el equivalente directo), y
    # `bbox` se serializa a TEXT con json.dumps en vez de con
    # `psycopg2.extras.Json`.
    cl = _cl()
    cl.execute("BEGIN")
    try:
        cl.executemany("""
            INSERT INTO candidato_raw
                (proveedor_id, catalogo_origen, pagina, bbox, codigo_ocr, confianza_ocr,
                 crop_imagen_path, texto_crudo, talla_min, talla_max, empaque_cantidad, empaque_unidad,
                 estado_revision, fecha_ingesta, cargado_en)
            VALUES (:proveedor_id, :catalogo_origen, :pagina, :bbox,
                    :codigo_ocr, :confianza_ocr, :crop_imagen_path, :texto_crudo,
                    :talla_min, :talla_max, :empaque_cantidad, :empaque_unidad,
                    'pendiente', :fecha_ingesta, :cargado_en)
        """, [{**f,
               "bbox": json.dumps(f["bbox"], ensure_ascii=False),
               "fecha_ingesta": f["fecha_ingesta"].isoformat(),
               "cargado_en": almacen.ahora()} for f in filas])

        cl.execute("""
            UPDATE candidato_raw SET estado_revision = 'aprobado'
            WHERE estado_revision = 'pendiente' AND codigo_ocr IS NOT NULL AND confianza_ocr > ?
        """, (CONFIANZA_AUTO_APROBAR,))
        cl.execute("COMMIT")
    except Exception:
        cl.execute("ROLLBACK")
        raise

    _reportar_progreso(2, 4, "Graduando ítems aprobados a candidatos...")
    candidato_ids = _graduar_candidatos_aprobados()

    n_total, n_ocr = len(filas), sum(1 for f in filas if f["codigo_ocr"])
    n_pendientes = n_total - len(candidato_ids)
    print(f"  OCR: {n_ocr}/{n_total} con código, {len(candidato_ids)} candidatos en revisión, "
          f"{n_pendientes} pendientes de confirmación manual", flush=True)

    vectorizar_y_puntuar_candidatos(c, cur, candidato_ids)
    _progreso.update(activo=False)
    c.close()
    return {"items_detectados": n_total, "con_codigo": n_ocr, "candidatos_graduados": len(candidato_ids),
            "pendientes_revision": n_pendientes}


# Plan 2026-09-04, paso 6 (segunda mitad): tipo declarado por el proveedor
# (vocabulario de atributos_proveedor.py, AsistenteComprasEscritorio) -> el
# vocabulario real de `categoria` que ya usa dim_tuc_producto/categorizar_
# candidatos. Solo se mapean los tipos con equivalente CLARO; "outdoor",
# "skate" y "trabajo" no tienen categoría propia en este catálogo y se dejan
# sin mapear a propósito -- inventar una categoría aproximada sería peor que
# dejar que la inferencia visual (categorizar_candidatos) decida.
# Plan 2026-09-04, paso 7 (señal de importación/tendencia): mismo vocabulario
# `tipo_norm` de arriba -> el `tipo` que usa `gold.vw_senal_tipo` (aduana).
# Copiado deliberadamente de `AsistenteComprasTEC/pipeline/0b_senal_importacion.py`
# (`TIPO_TEC_A_ADUANA`) -- calza sin traducir porque `atributos_proveedor.py`
# ya reusa el mismo vocabulario de tipo que TEC (ver paso 6). "None" = sin
# señal confiable para ese tipo; queda en factor neutro.
TIPO_NORM_A_TIPO_ADUANA: dict[str, str | None] = {
    "casual": "lifestyle",
    "sandalias": "sandalia",
    "slides": "sandalia",
    "running": "running",
    "skate": "skate",
    "futbol": "futbol",
    "outdoor": "outdoor",
    "deportivo": None,
    "trabajo": None,
}


def _factor_mercado(cur, candidato_id: int) -> float:
    """Factor de importación/tendencia (aduana) para un candidato, a partir
    del tipo y la marca que declaró el proveedor -- 1.0 (neutro) si no hay
    dato declarado, no hay mapeo, o la señal no es 'confiable'. Multiplica
    directo al score final (mismo criterio que ya usa el optimizador de
    AsistenteComprasTEC). Consulta `gold.vw_senal_tipo`/`gold.vw_senal_marca`
    en vivo -- son vistas ya existentes en tcmarcas, no hace falta cachear."""
    # FASE 2: tipo/marca DECLARADOS por el proveedor salen del `lote.sqlite`;
    # las señales de aduana (`gold.vw_senal_*`) siguen en Postgres -- son datos
    # permanentes de importaciones, ajenos a este lote.
    cl = _cl()
    cl.execute("""
        SELECT r.tipo_norm, r.marca_declarada
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        WHERE c.candidato_id = ?
    """, (candidato_id,))
    fila = cl.fetchone()
    if not fila:
        return 1.0
    tipo_norm, marca_declarada = tuple(fila)

    factor_tipo = 1.0
    tipo_aduana = TIPO_NORM_A_TIPO_ADUANA.get(tipo_norm) if tipo_norm else None
    if tipo_aduana:
        cur.execute("SELECT factor_sugerido FROM gold.vw_senal_tipo WHERE tipo = %s AND confiable",
                    (tipo_aduana,))
        r = cur.fetchone()
        if r:
            factor_tipo = float(r[0])

    factor_marca = 1.0
    if marca_declarada:
        # Normalización simple (solo letras/números, mayúsculas) en vez de un
        # diccionario de alias -- cubre "O'Neill"/"ONEILL"/"O NEILL" etc. sin
        # mantener una lista aparte; los nombres de marca reales en
        # vw_senal_marca ya son directos (NIKE, HOKA, DC SHOES...).
        cur.execute("""
            SELECT factor_sugerido FROM gold.vw_senal_marca
            WHERE upper(regexp_replace(marca, '[^A-Za-z0-9]', '', 'g'))
                = upper(regexp_replace(%s, '[^A-Za-z0-9]', '', 'g'))
              AND confiable
        """, (marca_declarada,))
        r = cur.fetchone()
        if r:
            factor_marca = float(r[0])

    return factor_tipo * factor_marca


_TIPO_NORM_A_CATEGORIA = {
    "futbol": "Futbol",
    "running": "Deportivos",
    "deportivo": "Deportivos",
    "slides": "Sandalias",
    "sandalias": "Sandalias",
    "tacones": "Tacones",
    "formal": "Formal",
    "botas_botines": "Botas y botines",
    "casual": "Casual",
}


def _graduar_candidatos_aprobados() -> list[int]:
    """Crea el candidato local para todo item de `candidato_raw` (ex
    staging_tuc.raw_catalogo) ya aprobado/corregido (por auto-aprobación de confianza O
    por confirmación manual del comprador) que aún no tenga su candidato. Se llama tanto
    al subir un catálogo como al confirmar un pendiente uno por uno.

    Copia, si existe, el atributo DECLARADO por el proveedor (`tipo_norm`/
    `color_familia` en `raw_catalogo`, poblado solo por AsistenteComprasEscritorio
    -- NULL para el flujo web de catálogos crudos) hacia `categoria_declarada`/
    `color_familia_declarado` en el momento de graduar. `categoria_declarada`
    usa COALESCE en `categorizar_candidatos`, así que si ya queda seteada acá
    con el dato declarado, la inferencia visual NUNCA la pisa -- "declarado
    gana, inferido rellena" queda gratis con el mecanismo que ya existía.

    FASE 2: 100% local. Ya no recibe `(c, cur)` de Postgres -- no le hacen
    falta: tanto el origen (`candidato_raw`) como el destino (`candidato`)
    viven en el `lote.sqlite`."""
    candidato_ids = []
    cl = _cl()
    cl.execute("""
        SELECT proveedor_id, catalogo_origen, codigo_ocr, fecha_ingesta,
               MAX(tipo_norm) AS tipo_norm, MAX(color_familia) AS color_familia
        FROM candidato_raw
        WHERE estado_revision IN ('aprobado', 'corregido')
        GROUP BY proveedor_id, catalogo_origen, codigo_ocr, fecha_ingesta
        ORDER BY proveedor_id, catalogo_origen, codigo_ocr, fecha_ingesta
    """)
    aprobados = cl.fetchall()
    cl.execute("BEGIN")
    try:
        for prov_id, cat_origen, codigo, fecha, tipo_norm, color_familia in aprobados:
            categoria_declarada = _TIPO_NORM_A_CATEGORIA.get(tipo_norm)
            # `RETURNING` con `DO NOTHING` se comporta igual que en Postgres:
            # si la fila ya existía no devuelve nada, y el candidato NO se
            # agrega a la lista -- que es justo el criterio de "cuáles son
            # NUEVOS y hay que vectorizar".
            cl.execute("""
                INSERT INTO candidato
                    (proveedor_id, codigo_proveedor, catalogo_origen, fecha_ingesta,
                     categoria_declarada, color_familia_declarado, creado_en)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (proveedor_id, codigo_proveedor, catalogo_origen) DO NOTHING
                RETURNING candidato_id
            """, (prov_id, codigo, cat_origen, fecha, categoria_declarada, color_familia,
                  almacen.ahora()))
            r2 = cl.fetchone()
            if r2:
                candidato_ids.append(r2[0])
        cl.execute("COMMIT")
    except Exception:
        cl.execute("ROLLBACK")
        raise
    return candidato_ids


# Nota (plan de mejora 2026-09-22, Etapa 6): mismo caso que
# `procesar_catalogo` de arriba -- sin llamador en la app de escritorio,
# parte de la cola de revisión "pendientes"/staging_tuc, exclusiva de
# servidor_pty.py. No se borra.
def api_pendientes() -> list[dict]:
    """Items EN staging cuyo código no se auto-aprobó (confianza <= umbral) -- "la
    máquina propone, el humano dispone": el comprador confirma o corrige el código a
    mano en vez de que el ítem desaparezca en silencio (bug real encontrado 2026-07-22:
    un código bien leído con confianza=0 nunca llegaba a candidato y no había forma de
    rescatarlo desde la UI).

    FASE 2: 100% local -- esta función ya no toca Postgres para nada."""
    cl = _cl()
    cl.execute("""
        SELECT id, codigo_ocr, confianza_ocr, crop_imagen_path, texto_crudo
        FROM candidato_raw
        WHERE estado_revision = 'pendiente'
        ORDER BY confianza_ocr DESC NULLS LAST
    """)
    filas = [dict(row) for row in cl.fetchall()]
    for f in filas:
        if f["confianza_ocr"] is not None:
            f["confianza_ocr"] = float(f["confianza_ocr"])
    return filas


# Nota (plan de mejora 2026-09-22, Etapa 6): mismo caso que
# `procesar_catalogo`/`api_pendientes` -- sin llamador en la app de
# escritorio, exclusiva de servidor_pty.py. No se borra.
def api_confirmar_pendiente(raw_id: int, codigo: str, descartar: bool = False) -> dict:
    """FASE 2: la corrección del código y la graduación son locales; la
    vectorización sigue necesitando Postgres (lee el índice permanente para
    puntuar), así que la conexión se abre solo cuando de verdad hace falta."""
    cl = _cl()
    if descartar:
        cl.execute("UPDATE candidato_raw SET estado_revision = 'descartado' WHERE id = ?", (raw_id,))
        return {"ok": True, "candidatos_graduados": 0}
    cl.execute("""
        UPDATE candidato_raw SET codigo_ocr = ?, estado_revision = 'corregido'
        WHERE id = ?
    """, (codigo.strip().upper(), raw_id))
    candidato_ids = _graduar_candidatos_aprobados()
    if candidato_ids:
        c = conn(); cur = c.cursor()
        try:
            vectorizar_y_puntuar_candidatos(c, cur, candidato_ids)
        finally:
            c.close()
    return {"ok": True, "candidatos_graduados": len(candidato_ids)}


def api_asignar_categoria(candidato_id: int, categoria: str = None, genero: str = None,
                          modelo_activo: str = "estandar") -> dict:
    """Confirmación manual de categoria/genero cuando `categorizar_candidatos` (Fase 5,
    nearest-centroid) queda en "ambiguo" (None) -- pedido explícito 2026-07-22: la
    categoría es crítica para filtrar comparables, y un empate real entre 2-3 categorías
    (ej. Casual 0.729 vs Formal 0.719 vs Deportivos 0.707, diferencia ~1%) es un caso
    genuinamente difícil para el algoritmo, no un bug de umbral -- "la máquina propone,
    el humano dispone" (mismo principio que los pendientes de OCR). Re-corre
    `puntuar_candidatos` para este candidato porque el score depende del filtro
    categoria+genero.

    `modelo_activo` (2026-09-09): antes el re-scoring corría SIEMPRE con
    "estandar" (el default de `puntuar_candidatos`), sin importar con qué
    método se hubiera calificado el catálogo. Bug real: en un catálogo
    calificado con TI, corregir a mano el tipo o el género volvía a puntuar
    ESE candidato en el espacio de vectores equivocado, y su score quedaba
    incomparable con el del resto de la lista. El default "estandar" mantiene
    intacto el comportamiento de la herramienta web, que nunca pasa este
    parámetro; la app de escritorio pasa el método configurado del catálogo.

    FASE 2: la corrección del comprador se guarda en el `lote.sqlite`; el
    re-scoring sigue leyendo el índice permanente de Postgres, así que
    `puntuar_candidatos` sigue recibiendo un cursor de Postgres."""
    cl = _cl()
    if categoria:
        cl.execute("UPDATE candidato SET categoria_declarada = ? WHERE candidato_id = ?",
                   (categoria, candidato_id))
    if genero:
        cl.execute("UPDATE candidato SET genero_canonico = ? WHERE candidato_id = ?",
                   (genero, candidato_id))
    c = conn(); cur = c.cursor()
    try:
        puntuar_candidatos(cur, [candidato_id], modelo_activo=modelo_activo)
    finally:
        c.close()
    return {"ok": True}


# Nota (plan de mejora 2026-09-22, Etapa 6): sin llamador en la app de
# escritorio -- gui_profesional_ctk.py resuelve esto por su cuenta con
# motor_candidatos.modelo_activo_configurado(). servidor_pty.py SÍ define y
# usa su propia copia de esta función (rutas /api/similar_marca,
# /api/similar_tuc, /api/asignar_categoria). No se borra.
def modelo_activo_del_catalogo() -> str:
    """Con qué método está calificado el catálogo EN REVISIÓN de este lote --
    deducido de los propios embeddings guardados, no de una config.

    `dinov2_ti` presente => "ti" (`vectorizar_y_puntuar_candidatos` con
    modelo_activo="ti" guarda ese y solo ese); si no, "estandar"
    (fashion_siglip+dino_v2). Sin datos => "estandar", el default histórico.

    LÍMITE CONOCIDO (2026-09-16): los tres métodos no-TI ("estandar",
    "fashion", "dino") guardan EXACTAMENTE los mismos dos embeddings, así que
    desde los vectores no se puede distinguir cuál se eligió -- esta función
    devuelve "estandar" para los tres. Quién quiera el método real de un lote
    no-TI tiene que leerlo de la configuración
    (`motor_candidatos.modelo_activo_configurado`), no de acá. Hoy nadie llama
    a esta función; queda documentado para que el próximo que la use no se
    confíe.

    FASE 2: se deduce del `lote.sqlite` de ESTE lote, no de una tabla global.
    Antes la pregunta era ambigua a propósito ("con qué método está calificado
    el único catálogo que hay") porque `staging_tuc` era compartido entre esta
    app y el servidor web; ahora la respuesta es por lote, que es lo que
    siempre se quiso saber."""
    cl = _cl()
    cl.execute("SELECT 1 FROM candidato_embedding WHERE modelo_embedding = ? LIMIT 1",
               (MODELO_TI[0],))
    return "ti" if cl.fetchone() else "estandar"


_ACROMATICOS = {"negro", "gris", "blanco"}


def _listar_colores(centroides: list, umbral: float = UMBRAL_SECUNDARIO) -> str | None:
    """Lista TODOS los nombres de color con área >= umbral (no solo principal+secundario)
    -- aproxima "en qué colores viene esta referencia" sin necesitar separar instancias:
    en una foto con N pares de distinto color, cada uno ocupa una fracción del área total
    y aparece aquí como su propio cluster.

    Filtro anti-brillo (2026-07-22, bug real reportado): en un calzado de cuero/charol
    brillante de UN SOLO color, el reflejo especular puede formar su propio cluster de
    k-means (más claro/desaturado que el material real) que pasa el umbral de área y se
    reporta como si fuera "otro color" -- ej. "negro, gris" para un tenis 100% negro. Si
    el color dominante es acromático (negro/gris/blanco) y un cluster secundario TAMBIÉN
    acromático tiene área chica (<25%), se descarta como variación de brillo del MISMO
    material, no como un color adicional real. No aplica entre colores CROMÁTICOS (ahí sí
    puede ser un segundo par genuino) ni cuando el secundario tiene área grande (ahí es
    más probablemente un segundo objeto real, no solo brillo)."""
    if not centroides:
        return None
    ordenados = sorted(centroides, key=lambda c: -c["area_pct"])
    dominante = _clasificar_centroide(ordenados[0]["lab"], ordenados[0].get("lab_p20_L"))
    vistos, nombres = {dominante}, [dominante]
    for c in ordenados[1:]:
        if c["area_pct"] < umbral:
            continue
        nombre = _clasificar_centroide(c["lab"], c.get("lab_p20_L"))
        if nombre in vistos:
            continue
        if dominante in _ACROMATICOS and nombre in _ACROMATICOS and c["area_pct"] < 0.25:
            continue  # brillo/reflejo del mismo material, no un color distinto
        vistos.add(nombre)
        nombres.append(nombre)
    return ", ".join(nombres) if nombres else None


def _aislar_por_fondo_blanco(img_original, umbral_blanco: int = 245):
    """Alfa sintético para fotos que YA vienen limpias -- un solo calzado por
    imagen, fondo blanco real, sin props ni texto -- que es exactamente el
    contrato de AsistenteComprasEscritorio (app de escritorio separada que
    limpia fotos ANTES de mandarlas a calificar aquí). Sustituye a rembg
    cuando ya no hace falta detectar nada: cualquier píxel casi blanco es
    fondo, el resto es el calzado.

    Por qué existe (2026-09-03): sin esto, un candidato que ya pasó por
    Limpieza de Imágenes se sometía OTRA VEZ a detección OWL-ViT + rembg +
    SAM aquí -- trabajo duplicado sobre una foto que ya estaba aislada,
    identificado como el cuello de botella dominante (~1 min/candidato) en
    la auditoría de flujo de esa app. Coexiste con `_remover_fondo_completo`
    sin tocarla -- este pipeline (GestionTUC/PTY, catálogos de proveedor SIN
    procesar) sigue usando rembg como antes; `ya_aislado` en
    `vectorizar_y_puntuar_candidatos` es el único punto que elige cuál de
    las dos correr.

    Cierre morfológico chico (no apertura: cerrar rellena huecos internos del
    calzado sin comerse su silueta) para no dejar agujeros por reflejos
    brillantes o charoles casi blancos dentro del propio calzado."""
    import numpy as np
    from PIL import Image
    from scipy.ndimage import binary_closing
    rgb = np.array(img_original.convert("RGB"))
    es_fondo = np.all(rgb >= umbral_blanco, axis=-1)
    objeto = ~es_fondo
    objeto = binary_closing(objeto, structure=np.ones((5, 5)))
    alpha = np.where(objeto, 255, 0).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb, alpha]).astype(np.uint8), mode="RGBA")


def _remover_fondo_completo(vec, img_original, cajas_props):
    """Enmascara props decorativos (flores/floreros/revistas) y CUALQUIER texto impreso
    (código/talla/marca de agua) y corre rembg UNA SOLA VEZ sobre la foto COMPLETA --
    reemplaza el diseño anterior (rembg por separado para el análisis de color Y otra
    vez por cada calzado recortado, 2-3 llamadas por candidato). Retorna la imagen RGBA
    resultante, con bordes CURVOS reales (silueta de rembg) para TODOS los calzados de
    la foto -- aislar UNO solo después es simplemente recortar+poner-en-transparente-los-
    otros esta misma imagen, sin volver a correr rembg ni pintar rectángulos sobre fondo
    real (eso causaba el artefacto de borde recto/diagonal reportado 2026-07-22: pintar
    de blanco ANTES de rembg sobre un fondo con textura dejaba un corte duro visible).

    Endurecimiento de borde (hard edge) contra el parche gris/verdoso fantasma
    (2026-07-22, bug real reportado con foto de tenis negro sobre fondo de hojas verdes):
    diagnóstico exhaustivo, verificado paso a paso contra la foto real, descartó una por
    una las causas anteriores (texto/props pintados, alpha degradado uniforme, color
    premultiplicado hacia blanco, agujeros internos de alpha aislados del fondo). La causa
    real es que rembg (u2net) produce en TODO el contorno del objeto una BANDA de
    transición suave (feathering) de unos pocos píxeles con alpha PARCIAL (valores
    intermedios, ej. 100-220). Esos píxeles de borde tienen un RGB que es una MEZCLA real
    object+background capturada por el sensor de la cámara (no una alucinación de rembg).
    Contra fondo blanco liso la mezcla es invisible; contra el fondo real oscuro/texturizado
    (hojas verdes) esa mezcla se ve como un tono gris-verdoso fantasma.

    Fix definitivo (reemplaza el intento anterior de inpainting de agujeros internos, que
    resultó insuficiente): ENDURECER el borde. Se clasifica cada píxel en solido
    (alpha>200, alta confianza de ser calzado), vacio (alpha<50, alta confianza de ser
    fondo) y banda_ambigua (el resto). El color de la banda ambigua se REEMPLAZA por el
    del píxel solido más cercano (via distance_transform_edt), en vez de confiar en su RGB
    mezclado con el fondo. Luego la banda ambigua se vuelve OPACA (se une al objeto -- es
    borde real del calzado) y el fondo genuino sigue transparente. Esto endurece de forma
    pareja TODO el contorno, no solo la zona reportada -- mejora general del pipeline, no
    un parche ad-hoc."""
    from rembg import remove
    from PIL import Image
    from scipy.ndimage import distance_transform_edt
    import numpy as np
    img_sin_props = enmascarar_props(img_original, cajas_props)
    img_sin_texto = enmascarar_texto_ocr(img_sin_props)
    buf = io.BytesIO()
    img_sin_texto.save(buf, format="PNG")
    out = remove(buf.getvalue(), session=vec._rembg_session)
    img_rgba = Image.open(io.BytesIO(out)).convert("RGBA")

    alpha = np.array(img_rgba.split()[3])
    rgb = np.array(img_rgba.convert("RGB"))

    UMBRAL_SOLIDO = 200   # alta confianza de ser el objeto real
    UMBRAL_VACIO = 50     # alta confianza de ser fondo real
    DISTANCIA_MAX_ENDURECIDA = 4  # píxeles -- ver comentario abajo (bug real 2026-07-22)
    solido = alpha > UMBRAL_SOLIDO
    vacio = alpha < UMBRAL_VACIO
    banda_ambigua_total = ~solido & ~vacio  # franja de transición -- ni claramente objeto ni fondo

    if banda_ambigua_total.any() and solido.any():
        # para cada pixel, distancia y coordenadas del pixel "solido" mas cercano
        distancias, (iy, ix) = distance_transform_edt(~solido, return_indices=True)
        # Límite de distancia (bug real reportado 2026-07-22: "aparecen los DOS calzados
        # otra vez" -- endurecer TODA la banda ambigua sin límite fusionó dos calzados
        # cercanos en un solo componente conexo, porque el hueco ENTRE ellos también tenía
        # alpha intermedio y se volvía opaco de punta a punta, cerrando la separación real.
        # El feathering genuino de rembg es una franja de solo 2-4 píxeles de ancho -- un
        # hueco real entre dos objetos separados es mucho más ancho que eso. Limitando el
        # endurecido a píxeles a <= 4px de un solido real, se arregla el borde fino sin
        # cerrar huecos genuinos entre objetos distintos.
        banda_ambigua = banda_ambigua_total & (distancias <= DISTANCIA_MAX_ENDURECIDA)
        rgb_endurecido = rgb.copy()
        rgb_endurecido[banda_ambigua] = rgb[iy[banda_ambigua], ix[banda_ambigua]]
        rgb = rgb_endurecido
    else:
        banda_ambigua = banda_ambigua_total

    alpha_final = np.where(solido | banda_ambigua, 255, 0).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb, alpha_final]).astype(np.uint8), mode="RGBA")


def _analizar_multicolor(img_rgba_completa):
    """es_multicolor + lista de TODOS los colores -- SOBRE LA FOTO COMPLETA (todos los
    pares/colores que trae la foto), NO sobre el calzado principal ya aislado -- si se
    corriera sobre el recorte aislado (bug real encontrado 2026-07-22) solo vería los
    colores de ESE par, perdiendo los otros 2-3 de la foto. Reutiliza nombrar_color()
    (k-means LAB) -- no requiere separar instancias, cada par de distinto color ya
    aparece como su propio cluster de área significativa. Recibe el RGBA ya calculado
    por `_remover_fondo_completo` -- no corre rembg de nuevo."""
    info_completa = extraer_color(img_rgba_completa)
    info_full = nombrar_color(info_completa["centroides"]) if info_completa["centroides"] else None
    es_multicolor = info_full["es_multicolor"] if info_full else None
    colores_lista = _listar_colores(info_completa["centroides"])
    n_colores = len(colores_lista.split(", ")) if colores_lista else (1 if info_full else None)
    return es_multicolor, n_colores, colores_lista


def _bbox_por_contorno_alpha(img_rgba_completa, box_owlvit, margen_pct: float = 0.03):
    """Encuentra la extensión REAL del calzado seleccionado usando el canal ALPHA de
    rembg (la silueta CURVA ya calculada sobre la foto completa) en vez de la caja
    rectangular de OWL-ViT, que en detección zero-shot suele quedar más chica que el
    objeto y CORTA la punta/talón en línea recta (artefacto reportado 2026-07-22).

    Procedimiento (guía Opus 2026-07-22, Tarea 2):
      1. Umbraliza el alpha completo (> 128) en una máscara binaria.
      2. Etiqueta componentes conexos (scipy.ndimage.label) sobre esa máscara.
      3. Elige el componente con MAYOR SOLAPAMIENTO (área de intersección) con la caja
         de OWL-ViT ya elegida (`box_owlvit`) -- asocia el blob correcto al calzado que
         `_elegir_calzado_principal` ya decidió mostrar, sin re-decidir CUÁL calzado es.
      4. Retorna (bbox, etiquetas, etiqueta_ganadora) -- el mapa de etiquetas completo se
         reutiliza después para borrar a nivel de PIXEL cualquier pixel de OTRO componente
         que caiga dentro del recorte (más preciso que borrar el rectángulo OWL-ViT del
         vecino, que dejaba restos reales -- sombra/reflejo/borde -- fuera de ese
         rectángulo pero dentro del recorte; bug real reportado 2026-07-22, foto de tenis
         negro con un parche gris fantasma). Retorna None si no hay ningún pixel con alpha
         significativo dentro/solapando la caja (foto atípica -- el llamador degrada a la
         caja de OWL-ViT + margen fijo anterior).

    LIMITACIÓN CONOCIDA: si rembg fusionó el calzado con una sombra/reflejo pegado (o con
    un par vecino que se toca), ese blob unido cuenta como UN solo componente conexo y su
    bounding box saldrá más grande de lo real -- no hay forma de separarlos sin lógica
    adicional (watershed/etc.) que ya se probó y falló (ver §3). En ese caso borde el
    recorte puede quedar más grande que la caja de OWL-ViT, no más ajustado; se acepta
    a propósito para no reintroducir heurísticas no pedidas."""
    from scipy import ndimage
    alpha = np.asarray(img_rgba_completa.split()[3])
    H, W = alpha.shape
    mascara = alpha > 128
    if not mascara.any():
        return None
    etiquetas, n = ndimage.label(mascara)
    if n == 0:
        return None

    # caja de OWL-ViT en coordenadas de pixel, recortada al lienzo
    bx0 = max(0, min(W, int(box_owlvit["xmin"])))
    by0 = max(0, min(H, int(box_owlvit["ymin"])))
    bx1 = max(0, min(W, int(box_owlvit["xmax"])))
    by1 = max(0, min(H, int(box_owlvit["ymax"])))
    if bx1 <= bx0 or by1 <= by0:
        return None

    # solapamiento de cada componente con la caja: cuenta pixeles de cada etiqueta dentro
    # de la caja (bincount es O(area_caja), no O(imagen*n_componentes)).
    sub = etiquetas[by0:by1, bx0:bx1]
    conteos = np.bincount(sub.ravel(), minlength=n + 1)
    conteos[0] = 0  # etiqueta 0 = fondo
    etiqueta_ganadora = int(conteos.argmax())
    if conteos[etiqueta_ganadora] == 0:
        return None  # ningún componente solapa la caja -> que el llamador use el fallback

    ys, xs = np.where(etiquetas == etiqueta_ganadora)
    cx0, cy0, cx1, cy1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1

    # Salvaguarda REAL (bug reportado 2026-07-22: "ahora aparecen los DOS calzados en vez
    # de uno" -- peor que antes): si el componente ganador fusionó el calzado con el vecino
    # (se tocan/se superponen en la foto -- ver LIMITACIÓN arriba), su bbox sale MUCHO más
    # grande que la caja de OWL-ViT que se pidió aislar. Usar ESE BBOX para el recorte sería
    # contraproducente (saldría mucho más grande de lo pedido) -- PERO el mapa de etiquetas
    # sigue siendo útil: aunque el bbox no sea confiable, `etiqueta_ganadora` sigue
    # identificando el componente correcto, así que el borrado a nivel de pixel en
    # `_aislar_calzado_y_color_zonas` (zonas != etiqueta_ganadora) sigue funcionando incluso
    # sobre el recorte del método de respaldo (rectángulo+12%) -- esto de PASO también borra
    # props transparentes que la detección zero-shot no vio (ej. vitrinas de vidrio tipo
    # "botella", que OWL-ViT no detecta pero SÍ quedan como un componente de alpha aparte).
    # Si el área del componente supera 2.2x el área de la caja OWL-ViT, se retorna bbox=None
    # (el llamador degrada el RECORTE al método anterior) pero SÍ se retornan las etiquetas.
    area_componente = (cx1 - cx0) * (cy1 - cy0)
    area_caja = (bx1 - bx0) * (by1 - by0)
    if area_caja > 0 and area_componente > 2.2 * area_caja:
        return None, etiquetas, etiqueta_ganadora

    mx = int((cx1 - cx0) * margen_pct)
    my = int((cy1 - cy0) * margen_pct)
    bbox = (max(0, cx0 - mx), max(0, cy0 - my), min(W, cx1 + mx), min(H, cy1 + my))
    return bbox, etiquetas, etiqueta_ganadora


WL_LUMINANCIA = 0.5          # peso de L en el clustering de regiones (a,b pesan 1.0) --
# auditoría Opus ronda 4 (2026-07-24): una sombra/pliegue sobre el MISMO material varía
# sobre todo en L; bajar su peso evita que una sombra abra una región falsa. No se puede
# ignorar L del todo -- negro/gris/blanco viven todos en a≈b≈0 y colapsarían en un solo
# cluster -- 0.5 es el compromiso.
K_INICIAL_REGIONES = 6       # sobre-segmentar a propósito; la fusión por ΔE reduce a las
# regiones de color realmente distintas -- más barato que acertar K de una vez.
DELTA_E_FUSION = 10.0        # CIEDE2000: por debajo de esto, dos clusters son "el mismo
# color" (sombra/ruido de k-means), se fusionan.
MIN_AREA_REGION = 0.015      # <1.5% del calzado = ruido -- se absorbe en la región de
# color más parecida en vez de quedar suelta.
LADO_TRABAJO_REGIONES = 256  # resolución de trabajo para el clustering (rápido en CPU,
# el resultado se reescala a la resolución original con NEAREST al final).


def _lab_y_mascara_trabajo(img_rgba):
    """Reescala a LADO_TRABAJO_REGIONES (lado mayor) para que el clustering sea barato.
    Retorna (lab HxWx3, mask_fg bool, rgb HxWx3 [0-1], (H0,W0) tamaño original)."""
    from PIL import Image
    W0, H0 = img_rgba.size
    escala = LADO_TRABAJO_REGIONES / max(W0, H0)
    Wt, Ht = max(1, int(W0 * escala)), max(1, int(H0 * escala))
    chica = img_rgba.resize((Wt, Ht), Image.BILINEAR)
    arr = np.array(chica)
    mask = arr[:, :, 3] > 128 if arr.shape[2] == 4 else np.ones(arr.shape[:2], dtype=bool)
    rgb = arr[:, :, :3].astype(np.float32) / 255.0
    from skimage.color import rgb2lab
    lab = rgb2lab(rgb)
    return lab, mask, rgb, (H0, W0)


def _lab_robusto_region(lab_pix, rgb_pix):
    """LAB representativo de una región de píxeles: descarta especulares (V alto, S bajo
    -- mismo criterio que extraer_color) del CÁLCULO del centroide (el brillo no debe
    sesgar el color reportado), usa MEDIANA de a,b (estable ante outliers) y percentil-20
    de L (material real, no el brillo que sobrevive al filtro -- igual que lab_p20_L en
    extraer_color). Retorna ([L,a,b] mediana, l_p20)."""
    v_max = rgb_pix.max(axis=1)
    v_min = rgb_pix.min(axis=1)
    sat = np.where(v_max > 0, (v_max - v_min) / np.where(v_max > 0, v_max, 1), 0.0)
    especular = (v_max > 0.9) & (sat < 0.15)
    sel = ~especular if especular.mean() <= 0.5 else np.ones(len(lab_pix), dtype=bool)
    l_p20 = float(np.percentile(lab_pix[sel, 0], 20))
    a_med = float(np.median(lab_pix[sel, 1]))
    b_med = float(np.median(lab_pix[sel, 2]))
    l_med = float(np.median(lab_pix[sel, 0]))
    return [l_med, a_med, b_med], l_p20


def _fusionar_clusters(centros_lab, areas):
    """Fusiona iterativamente los dos clusters más parecidos (CIEDE2000) mientras su
    distancia sea < DELTA_E_FUSION -- colapsa la sobre-segmentación normal de k-means y
    cualquier par sombra/luz del mismo material que haya sobrevivido al peso reducido de
    L. Pondera por área al recalcular el centro fusionado. Retorna dict
    {label_viejo: label_nuevo}."""
    from skimage.color import deltaE_ciede2000
    centros = [list(c) for c in centros_lab]
    areas = list(areas)
    vivos = list(range(len(centros)))
    mapa = {i: i for i in range(len(centros))}
    cambiado = True
    while cambiado and len(vivos) > 1:
        cambiado = False
        mejor, mejor_de = None, 1e9
        for ii in range(len(vivos)):
            for jj in range(ii + 1, len(vivos)):
                i, j = vivos[ii], vivos[jj]
                de = float(deltaE_ciede2000(np.array(centros[i]), np.array(centros[j])))
                if de < mejor_de:
                    mejor_de, mejor = de, (i, j)
        if mejor and mejor_de < DELTA_E_FUSION:
            i, j = mejor
            wa, wb = areas[i], areas[j]
            centros[i] = [(centros[i][k] * wa + centros[j][k] * wb) / (wa + wb) for k in range(3)]
            areas[i] += areas[j]
            vivos.remove(j)
            for k in list(mapa):
                if mapa[k] == j:
                    mapa[k] = i
            cambiado = True
    reetq = {v: n for n, v in enumerate(sorted(set(mapa.values())))}
    return {k: reetq[v] for k, v in mapa.items()}


def segmentar_regiones_color(img_rgba):
    """Segmentación GENERAL del calzado en regiones de color REALES (auditoría Opus
    ronda 4, 2026-07-24) -- reemplaza todos los intentos anteriores basados en geometría
    fija (franjas por altura), luminosidad, o textura, que fallaban uno tras otro porque
    asumían de antemano "2 zonas fijas" (cuerpo/suela). El usuario pidió explícitamente
    "contar cuántos colores tiene la imagen" y "definir contornos" -- este método hace
    exactamente eso: k-means de color en LAB (peso de L reducido para no confundir
    sombra con color distinto) sobre TODOS los píxeles del calzado, seguido de fusión de
    clusters parecidos por CIEDE2000 y absorción de regiones ínfimas -- da entre N
    regiones reales (no 2 fijas), cada una de color PLANO por construcción (nunca un
    degradado, porque cada región es un cluster, no un campo continuo interpolado).

    Retorna lista de regiones ordenada por área desc, cada una:
    {'mask': HxW bool (resolución ORIGINAL), 'lab': [L,a,b], 'lab_p20_L': float,
     'nombre': str, 'area_pct': float, 'y_centro': float (0=arriba,1=abajo),
     'toca_inferior': bool, 'es_acromatica': bool}. [] si la silueta es demasiado chica."""
    from PIL import Image
    lab, mask, rgb, (H0, W0) = _lab_y_mascara_trabajo(img_rgba)
    Ht, Wt = mask.shape
    idx = np.flatnonzero(mask)
    if idx.size < 200:
        return []

    lab_flat = lab.reshape(-1, 3)[idx]
    rgb_flat = rgb.reshape(-1, 3)[idx]

    feat = lab_flat.copy()
    feat[:, 0] *= WL_LUMINANCIA
    k_eff = min(K_INICIAL_REGIONES, len(feat))
    _, labels = _kmeans_simple(feat, k_eff)

    centros_lab, areas = [], []
    for i in range(k_eff):
        sel = labels == i
        if sel.sum() == 0:
            centros_lab.append([0, 0, 0])
            areas.append(0)
            continue
        lab_c, _ = _lab_robusto_region(lab_flat[sel], rgb_flat[sel])
        centros_lab.append(lab_c)
        areas.append(int(sel.sum()))

    mapa = _fusionar_clusters(centros_lab, areas)
    labels = np.array([mapa[l] for l in labels])
    n_reg = labels.max() + 1

    lab2d = np.full(Ht * Wt, -1, dtype=np.int32)
    lab2d[idx] = labels
    lab2d = lab2d.reshape(Ht, Wt)

    total = idx.size
    regiones = []
    for r in range(n_reg):
        sel = labels == r
        if sel.sum() == 0:
            continue
        lab_r, l_p20 = _lab_robusto_region(lab_flat[sel], rgb_flat[sel])
        regiones.append({"r": r, "lab": lab_r, "lab_p20_L": l_p20, "area": int(sel.sum())})

    from skimage.color import deltaE_ciede2000
    grandes = [g for g in regiones if g["area"] >= MIN_AREA_REGION * total]
    if not grandes:  # todo salió "chico" -- quedarse con la mayor en vez de reportar nada
        grandes = [max(regiones, key=lambda g: g["area"])]
    for g in regiones:
        if g in grandes:
            continue
        destino = min(grandes, key=lambda t: float(deltaE_ciede2000(np.array(g["lab"]), np.array(t["lab"]))))
        lab2d[lab2d == g["r"]] = destino["r"]

    lab2d_full = np.array(Image.fromarray(lab2d.astype(np.int32), mode="I").resize((W0, H0), Image.NEAREST))
    ys_all, _ = np.where(lab2d_full >= 0)
    y_min, y_max = (int(ys_all.min()), int(ys_all.max())) if ys_all.size else (0, H0 - 1)
    alto = max(1, y_max - y_min)
    borde_inferior = int(y_max - 0.12 * alto)

    salida = []
    for g in grandes:
        m = lab2d_full == g["r"]
        area_total = int((lab2d_full >= 0).sum()) or 1
        area_pct = float(m.sum()) / area_total
        if area_pct <= 0:
            continue
        ys, _ = np.where(m)
        y_centro = float((ys.mean() - y_min) / alto)
        toca = bool((ys >= borde_inferior).any())
        L, a, b = g["lab"]
        chroma = (a * a + b * b) ** 0.5
        nombre = _clasificar_centroide([L, a, b], g["lab_p20_L"])
        salida.append({"mask": m, "lab": [round(L, 2), round(a, 2), round(b, 2)],
                       "lab_p20_L": round(g["lab_p20_L"], 2), "nombre": nombre,
                       "area_pct": round(area_pct, 4), "y_centro": round(y_centro, 3),
                       "toca_inferior": toca, "es_acromatica": chroma < 12})
    return sorted(salida, key=lambda s: -s["area_pct"])


def extraer_colores_por_parte(img_zona_rgba):
    """Reemplaza _color_zona + _segmentar_plantilla_capellada_suela + _plantilla_y_suela
    -- ya NO se distingue plantilla/suela/cuerpo por franjas de altura, luminosidad ni
    textura: si son colores distintos, `segmentar_regiones_color` ya los separó en
    regiones reales; si son del mismo color, se reportan iguales (honesto, no se inventa
    una diferencia que no existe). Retorna dict con color_principal/color_plantilla/
    color_suela (+ LAB de cada uno) y n_colores/colores (lista completa, para "cuántos
    colores tiene la imagen")."""
    regiones = segmentar_regiones_color(img_zona_rgba)
    if not regiones:
        return {"color_principal": None, "lab_principal": None,
                "color_plantilla": None, "lab_plantilla": None,
                "color_suela": None, "lab_suela": None,
                "n_colores": None, "colores": None}

    # CAPELLADA PRIMERO, NO "LA REGIÓN MÁS GRANDE" (2026-09-17, pedido del
    # dueño: "el color debería ser el color de la capellada, no el de la suela
    # o la plantilla").
    #
    # Antes `principal` era literalmente `regiones[0]` (la de mayor área) y la
    # suela se elegía DESPUÉS, descartándola. Eso falla cada vez que la
    # suela/plataforma ocupa más superficie que el empeine, que en sandalia y
    # plataforma es lo normal. Evidencia real (Packing List 134, foto ya
    # aislada de cada color):
    #   FOR-035 color 3 -> regiones: marrón 25.21% (y=0.599, toca piso),
    #   AZUL 25.12% (y=0.389, NO toca piso), marrón 24.26% (y=0.716), ...
    #   La capellada es la azul (el Excel declara AZUL MARINO) pero ganaba el
    #   marrón de la suela por 0.09 puntos de área.
    #
    # Orden nuevo: primero se aparta la SUELA (la región que toca el piso y
    # cuyo centroide está más abajo -- eso ya es un hecho medido de la
    # segmentación, no una franja fija), y la capellada se busca entre lo que
    # queda, dando prioridad a las regiones cuyo centroide está en la parte
    # ALTA del calzado (lo que cubre el pie). Si ninguna región queda arriba
    # (foto muy cenital, chancleta plana), se degrada a "la de mayor área de
    # las que no son suela", y si solo hay una región, capellada == suela y se
    # reporta igual: honesto, no se inventa una distinción que la foto no tiene.
    cand_suela = [r for r in regiones if r["toca_inferior"]]
    suela = max(cand_suela, key=lambda r: r["y_centro"]) if cand_suela else regiones[-1]
    resto = [r for r in regiones if r is not suela] or [suela]
    arriba = [r for r in resto if r["y_centro"] <= UMBRAL_Y_CAPELLADA]
    principal = max(arriba or resto, key=lambda r: r["area_pct"])
    cand_pl = [r for r in regiones if r["y_centro"] > 0.5 and r is not principal and r is not suela]
    plantilla = max(cand_pl, key=lambda r: r["area_pct"]) if cand_pl else suela

    vistos, colores = set(), []
    for r in regiones:
        if r["nombre"] not in vistos:
            vistos.add(r["nombre"])
            colores.append(r["nombre"])

    return {"color_principal": principal["nombre"], "lab_principal": principal["lab"],
            "color_plantilla": plantilla["nombre"], "lab_plantilla": plantilla["lab"],
            "color_suela": suela["nombre"], "lab_suela": suela["lab"],
            "n_colores": len(colores), "colores": ", ".join(colores)}


def recalcular_colores_variantes(candidato_ids: list[int] | None = None) -> int:
    """Vuelve a calcular color de CAPELLADA / plantilla / suela de las variantes
    que ya están en el lote, sobre la MISMA foto limpia que ya tienen guardada
    (`imagen_limpia_path`). Devuelve cuántas variantes cambiaron de color.

    POR QUÉ EXISTE. El color por parte se calcula una sola vez, cuando se
    vectoriza la referencia; cuando se corrige el criterio (2026-09-17: la
    capellada ya no es "la región más grande", ver `extraer_colores_por_parte`)
    los lotes ya cargados seguirían mostrando el color viejo hasta que alguien
    los vuelva a procesar ENTERO -- que cuesta OWL-ViT + rembg + embeddings por
    cada foto, para un dato que no depende de nada de eso.

    Es la corrección gobernada y repetible (nunca un UPDATE a mano): no toca
    embeddings, ni scores, ni recortes; solo recalcula lo que este archivo sabe
    calcular, y se puede volver a correr las veces que haga falta."""
    from PIL import Image
    cl = _cl()
    if candidato_ids:
        cl.execute(f"""SELECT variante_id, imagen_limpia_path FROM candidato_variante
                       WHERE imagen_limpia_path IS NOT NULL
                         AND candidato_id IN {_en(candidato_ids)}""", list(candidato_ids))
    else:
        cl.execute("""SELECT variante_id, imagen_limpia_path FROM candidato_variante
                      WHERE imagen_limpia_path IS NOT NULL""")
    cambiadas = 0
    for variante_id, ruta_rel in cl.fetchall():
        ruta = DATOS_COMPARTIDOS / ruta_rel
        if not ruta.exists():
            continue  # la foto ya no está: no se inventa un color, se deja el que hay
        img = Image.open(ruta)
        # La foto limpia puede estar guardada compuesta sobre blanco (sin alfa):
        # en ese caso se recupera la máscara por fondo blanco, exactamente como
        # hace la ruta `ya_aislado` de `vectorizar_y_puntuar_candidatos`.
        if img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255:
            rgba = img
        else:
            rgba = _aislar_por_fondo_blanco(img.convert("RGB"))
        partes = extraer_colores_por_parte(rgba)
        if not partes["color_principal"]:
            continue
        suela_contraste = delta_e = None
        if ATRIBUTOS_DISENO.get("suela_contraste"):
            suela_contraste, delta_e = _suela_contraste(partes["lab_principal"],
                                                        partes["lab_suela"])
        cl.execute("SELECT color_principal FROM candidato_variante WHERE variante_id = ?",
                   (variante_id,))
        previo = (cl.fetchone() or [None])[0]
        cl.execute("""
            UPDATE candidato_variante
            SET color_principal = ?, color_suela = ?, color_plantilla = ?,
                suela_contraste = ?, color_suela_delta_e = ?
            WHERE variante_id = ?
        """, (partes["color_principal"], partes["color_suela"], partes["color_plantilla"],
              None if suela_contraste is None else int(suela_contraste), delta_e, variante_id))
        # El color que quedó COPIADO en la tabla de scores por color se corrige
        # en el mismo paso (2026-09-17). `candidato_score_variante` guarda el
        # `color_principal` con el que se calificó ese color; si acá se corrige
        # la capellada y allá no, las dos tablas dicen colores distintos para la
        # MISMA variante (medido: candidato 25 índice 1 = "negro" en
        # `candidato_variante`, "marron" en `candidato_score_variante`) y
        # cualquier reporte que lea la de scores muestra el dato viejo.
        #
        # Se sincronizan las dos en vez de auditar cada lectura porque la
        # desincronización solo puede nacer ACÁ -- este es el único lugar que
        # cambia el color de una variante ya cargada -- y así no depende de que
        # el próximo reporte se acuerde de leer de la tabla correcta. NO se
        # recalcula ningún score: el número, la clasificación y los vecinos
        # quedan intactos; lo único que cambia es la etiqueta del color.
        cl.execute("""
            UPDATE candidato_score_variante SET color_principal = ?
            WHERE (candidato_id, indice) =
                  (SELECT candidato_id, indice FROM candidato_variante
                   WHERE variante_id = ?)
        """, (partes["color_principal"], variante_id))
        if previo != partes["color_principal"]:
            cambiadas += 1
    cl.connection.commit()
    return cambiadas


# Marca en `lote_meta` de que el lote ya tiene los colores calculados con el
# criterio de CAPELLADA. Se sube el número solo si el criterio vuelve a
# cambiar; entonces cada lote ya cargado se recalcula una única vez al abrirlo.
VERSION_COLOR_CAPELLADA = 1


def _asegurar_color_capellada() -> None:
    """Corrige, UNA sola vez por lote, el color de los lotes que se cargaron
    antes de que la capellada dejara de ser "la región más grande" (ver
    `extraer_colores_por_parte`). Sin esto, un lote ya procesado seguiría
    mostrando el color de la suela y el comprador no tendría forma de saber
    por qué su pantalla no cambió."""
    cl = _cl()
    cl.execute("SELECT valor_json FROM lote_meta WHERE clave = 'version_color_capellada'")
    fila = cl.fetchone()
    if fila and json.loads(fila[0]) >= VERSION_COLOR_CAPELLADA:
        return
    try:
        recalcular_colores_variantes()
    except Exception:  # noqa: BLE001
        # Nunca impedir que se abra el paso 6 por esto: si falla, el lote
        # queda sin marcar y se reintenta la próxima vez.
        return
    cl.execute("""INSERT INTO lote_meta (clave, valor_json, actualizado) VALUES (?,?,?)
                  ON CONFLICT (clave) DO UPDATE SET valor_json = excluded.valor_json,
                                                    actualizado = excluded.actualizado""",
               ("version_color_capellada", json.dumps(VERSION_COLOR_CAPELLADA), almacen.ahora()))
    cl.connection.commit()


def _aislar_calzado_y_color_zonas(img_rgba_completa, box_principal, otras_cajas, x0, y0, x1, y1,
                                   ruta_limpia: Path, etiquetas_alpha=None, etiqueta_propia=None,
                                   bbox_contorno_confiable=False):
    """Recorta el calzado YA AISLADO directamente del RGBA de la foto completa (bordes
    curvos reales de rembg, sin pintar rectángulos) y pone en transparente cualquier
    OTRO calzado detectado que caiga dentro del recorte. Compone sobre blanco para
    mostrar en la UI y extrae color por ZONA (heurística geométrica, no segmentación
    semántica -- ver migración 16): franja superior = "color principal", franja inferior
    = "color de suela". Retorna (color_principal, color_suela, lab_principal, lab_suela,
    color_plantilla, lab_plantilla) -- plantilla y suela separadas por luminosidad dentro
    de la misma franja inferior (ver _plantilla_y_suela), 2026-07-23.

    `etiquetas_alpha`/`etiqueta_propia`: cuando vienen de `_bbox_por_contorno_alpha`, se
    borra a nivel de PIXEL cualquier componente conexo que NO sea el propio -- sigue el
    contorno CURVO real, sin cortes rectos.

    `bbox_contorno_confiable` (2026-07-22, bug real -- "¿por qué hay cortes cuadrados si
    ya calculás el borde real?"): el borrado por RECTÁNGULO de `otras_cajas` es literal --
    una caja recta pintada encima -- y si esa caja (imprecisa, viene de OWL-ViT) se mete
    un poco DENTRO del calzado correcto, corta una línea recta real sobre el calzado que
    sí queríamos mostrar completo. Antes corría SIEMPRE (fix de la ronda anterior para el
    caso de calzados fusionados) -- pero eso reintroducía cortes cuadrados incluso cuando
    el contorno YA había separado todo correctamente sin necesitar el rectángulo. Ahora
    el rectángulo SOLO se aplica cuando el bbox de contorno NO fue confiable (fusión, o
    sin datos de alpha) -- en el caso normal (contorno separó bien), el borrado por
    etiqueta YA es suficiente y preciso, y el rectángulo queda desactivado para no
    arriesgar cortar el calzado correcto."""
    from PIL import Image
    img_zona = img_rgba_completa.crop((x0, y0, x1, y1)).copy()
    alpha = np.array(img_zona.split()[3])
    if etiquetas_alpha is not None and etiqueta_propia is not None:
        sub_etiquetas = etiquetas_alpha[y0:y1, x0:x1]
        alpha[(sub_etiquetas != 0) & (sub_etiquetas != etiqueta_propia)] = 0
    if not bbox_contorno_confiable:
        for otra in otras_cajas:
            if otra is box_principal:
                continue
            ox0, oy0 = max(0, int(otra["xmin"]) - x0), max(0, int(otra["ymin"]) - y0)
            ox1, oy1 = min(x1 - x0, int(otra["xmax"]) - x0), min(y1 - y0, int(otra["ymax"]) - y0)
            if ox1 > ox0 and oy1 > oy0:
                alpha[oy0:oy1, ox0:ox1] = 0
    # rembg (u2net) a veces deja transparencia PARCIAL en cavidades oscuras del objeto
    # (ej. el interior del cuello/collar de un tenis) -- limitación conocida del modelo,
    # no algo que este pipeline introduzca. Componer con alpha parcial mezcla el color
    # oscuro real con el blanco de fondo y se ve como un parche GRIS fantasma (bug real
    # reportado 2026-07-22 con foto de tenis negro). Fix: binarizar el alpha (>128->255,
    # si no->0) ANTES de componer -- cada pixel queda o totalmente opaco o totalmente
    # transparente, eliminando cualquier mezcla gris, a cambio de un borde levemente más
    # duro en esa zona puntual (trade-off aceptado, mejor que mostrar un color falso).
    alpha = np.where(alpha > 128, 255, 0).astype(np.uint8)
    r, g, b, _ = img_zona.split()
    img_zona = Image.merge("RGBA", (r, g, b, Image.fromarray(alpha)))

    fondo_blanco = Image.new("RGB", img_zona.size, (255, 255, 255))
    fondo_blanco.paste(img_zona, mask=img_zona.split()[3])
    ruta_limpia.parent.mkdir(parents=True, exist_ok=True)
    fondo_blanco.save(ruta_limpia)

    # Segmentación GENERAL por regiones de color reales (auditoría Opus ronda 4,
    # 2026-07-24) -- reemplaza franjas por altura/luminosidad/textura, que fallaban
    # sistemáticamente porque asumían de antemano "2 zonas fijas". Ahora se detectan
    # tantas regiones de color como REALMENTE haya (k-means LAB + fusión CIEDE2000), y
    # cada una es un color PLANO por construcción -- sin degradados ni tornasol.
    partes = extraer_colores_por_parte(img_zona)
    return (partes["color_principal"], partes["color_suela"],
            partes["lab_principal"], partes["lab_suela"],
            partes["color_plantilla"], partes["lab_plantilla"])


def _suela_contraste(lab_cuerpo, lab_suela, umbral_delta_e=UMBRAL_DELTA_E_SUELA_CONTRASTE):
    """True si la suela contrasta visualmente con el cuerpo del calzado. Usa delta-E
    CIEDE2000 (percepción de color estándar, no distancia euclidiana simple) sobre los
    centroides LAB dominantes que ya calcula extraer_color por zona. Umbral 15 es un
    punto de partida razonable (negro L~15 vs blanco L~95 da deltaE >40); calibrar contra
    una muestra real del catálogo TUC antes de confiar en producción sin supervisión.
    Devuelve (bool|None, delta_e|None) -- None cuando falta algún LAB (zona sin color)."""
    if lab_cuerpo is None or lab_suela is None:
        return None, None
    from skimage.color import deltaE_ciede2000
    dE = float(deltaE_ciede2000(np.array([lab_cuerpo]), np.array([lab_suela]))[0])
    return (dE > umbral_delta_e), round(dE, 2)


def vectorizar_y_puntuar_candidatos(c, cur, candidato_ids: list[int], ya_aislado: bool = False,
                                     modelo_activo: str = "estandar"):
    """`ya_aislado=True` (usado por AsistenteComprasEscritorio, NUNCA por el
    flujo web de catálogos de proveedor -- default False mantiene ese camino
    intacto): la foto ya viene con un solo calzado y fondo blanco real, así
    que se salta OWL-ViT + rembg + SAM (ver `_aislar_por_fondo_blanco`) en
    vez de repetir esa detección sobre algo que ya está aislado.

    `modelo_activo="ti"` (idem, exclusivo de AsistenteComprasEscritorio):
    vectoriza con `dinov2_ti` (instancia separada, `quitar_fondo=False`) en
    vez de fashion_siglip+dino_v2. La categorización/score que corre al final
    (`categorizar_candidatos`, `puntuar_candidatos`, etc.) recibe el mismo
    `modelo_activo` para comparar contra el índice correcto -- ver esas
    funciones para el detalle de por qué necesitan saberlo."""
    if not candidato_ids:
        return
    from PIL import Image
    # "fashion" y "dino" NO necesitan un vectorizador propio: `get_vectorizador()`
    # ya corre los dos modelos de `MODELOS` (fashion_siglip + dino_v2) y los
    # guarda como DOS filas separadas en `candidato_embedding` (una por
    # modelo_embedding, ver el INSERT de más abajo). Elegir "fashion" o "dino"
    # cambia solo qué fila se lee al buscar comparables, no qué se calcula --
    # así que un lote vectorizado con cualquiera de los tres métodos no-TI se
    # puede volver a puntuar con otro de ellos sin re-vectorizar nada.
    vec = get_vectorizador_ti() if modelo_activo == "ti" else get_vectorizador()
    # FASE 2: qué candidatos vectorizar, con qué foto y con qué color
    # declarado, sale del `lote.sqlite`. Lo que sigue en Postgres dentro de
    # esta función es el centroide TUC (`get_centroide_tuc`, usado para elegir
    # el calzado principal de la foto) y el scoring del final -- ambos leen el
    # catálogo permanente, así que `cur` sigue siendo necesario.
    cl = _cl()
    cl.execute(f"""
        SELECT c.candidato_id, r.crop_imagen_path, c.color_familia_declarado
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        WHERE c.candidato_id IN {_en(candidato_ids)}
        ORDER BY c.candidato_id
    """, list(candidato_ids))
    filas = [tuple(f) for f in cl.fetchall()]

    # UNA FILA POR FOTO (2026-09-17). El proveedor genérico manda la misma
    # referencia en varios colores, un color por foto; `cargar_desde_carpeta`
    # guarda la primera en `candidato_raw.crop_imagen_path` y las demás en
    # `candidato_foto_extra`. Cada foto se procesa igual que siempre y aporta
    # sus variantes; `base_indice` (el corrido de variantes ya creadas para ese
    # candidato) evita que la segunda foto pise la variante 0 de la primera.
    # Una referencia de UNA sola foto produce exactamente la misma fila que
    # antes, con base_indice=0: cero cambios para el caso mayoritario.
    cl.execute(f"""
        SELECT c.candidato_id, f.crop_imagen_path, f.orden
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        JOIN candidato_foto_extra f ON f.raw_id = r.id
        WHERE c.candidato_id IN {_en(candidato_ids)}
        ORDER BY c.candidato_id, f.orden
    """, list(candidato_ids))
    extras_por_candidato = {}
    for candidato_id_e, ruta_e, _orden in cl.fetchall():
        extras_por_candidato.setdefault(candidato_id_e, []).append(ruta_e)

    trabajos = []
    for candidato_id_f, crop_path_f, color_decl_f in filas:
        for ruta in [crop_path_f, *extras_por_candidato.get(candidato_id_f, [])]:
            trabajos.append((candidato_id_f, ruta, color_decl_f))
    filas = trabajos
    # Variantes ya creadas en ESTA corrida para cada candidato -- también es lo
    # que decide, al terminar, cuántas variantes debe tener y cuáles sobran de
    # una corrida anterior con más fotos.
    base_por_candidato: dict[int, int] = {}
    colores_por_candidato: dict[int, list[str]] = {}

    n = len(filas)
    for i, (candidato_id, crop_path, color_declarado_candidato) in enumerate(filas, start=1):
        base_indice = base_por_candidato.get(candidato_id, 0)
        # micro-fases: el mensaje cambia varias veces DENTRO de un mismo candidato (que
        # puede tardar varios s), para que nunca se congele el texto y se note actividad.
        def _fase(txt):
            _reportar_progreso(3, 4, f"Candidato {i}/{n}: {txt}", sub=i - 1, sub_total=n)
        _fase("preparando imagen...")
        path = DATOS_COMPARTIDOS / crop_path
        try:
            img_original = Image.open(path).convert("RGB")
            if ya_aislado:
                # Ruta rápida: la foto ya viene aislada (un calzado, fondo blanco
                # real) -- sin OWL-ViT, sin rembg, sin SAM.
                _fase("aislando por fondo blanco (foto ya limpia)...")
                w0, h0 = img_original.size
                cajas_props = []
                cajas_calzado = [{"xmin": 0, "ymin": 0, "xmax": w0, "ymax": h0, "score": 1.0}]
                n_pares_estimado = 1
                img_rgba_completa = _aislar_por_fondo_blanco(img_original)
            else:
                # UNA sola pasada de OWLv2 por imagen (antes eran 2 -- props y conteo de
                # pares por separado) + reescalado a MAX_DIM_DETECCION -- optimización de
                # rendimiento pedida explícitamente tras medir ~1 min/candidato.
                _fase("detectando calzado y props (OWL-ViT)...")
                cajas_props, cajas_calzado = _detectar_objetos_unificado(img_original)
                n_pares_estimado = contar_pares_aproximado(cajas_calzado)
                _fase("quitando el fondo (rembg)...")
                # rembg UNA SOLA VEZ sobre la foto completa (antes: 1 vez para color + 1 vez
                # más por cada calzado recortado) -- bordes curvos reales para todo, ver
                # _remover_fondo_completo. Perf: mitad de llamadas a rembg por candidato.
                img_rgba_completa = _remover_fondo_completo(vec, img_original, cajas_props)
            _fase("analizando colores...")
            es_multicolor, n_colores, colores_lista = _analizar_multicolor(img_rgba_completa)
            # Si la foto trae varios pares/colores de la MISMA Referencia, generar una
            # variante real POR PAR (separación por máscara SAM, ver detectar_instancias_
            # calzado) -- pedido explícito del usuario 2026-07-23: cada color debe tener su
            # propio análisis (color, suela, contraste), no solo listarse. Si la separación
            # no produce nada confiable (foto de 1 solo par, o SAM no convergió), degrada
            # con gracia al camino anterior (elegir 1 sola instancia principal).
            mascaras_por_caja = {}
            cajas = []
            if ya_aislado:
                # Un solo calzado por foto, ya aislado -- sin SAM ni el
                # comparador de centroide TUC (`_elegir_calzado_principal`),
                # que solo hacen falta cuando hay que ELEGIR entre varios.
                cajas = list(cajas_calzado)
            elif len(cajas_calzado) > 1:
                _fase("separando instancias por color (SAM)...")
                instancias = detectar_instancias_calzado(img_original, cajas_calzado)
                # Consolidar ángulos distintos del MISMO color (pedido usuario 2026-07-24):
                # dos instancias físicamente separadas pero de color prácticamente igual
                # son el mismo par mostrado dos veces, no dos variantes -- se queda la de
                # mejor calidad (más área de máscara).
                instancias = _consolidar_mismo_color(img_original, instancias)
                if instancias:
                    cajas = [ins[0] for ins in instancias]
                    mascaras_por_caja = {i: ins[1] for i, ins in enumerate(instancias)}
            if not cajas:
                box_principal = _elegir_calzado_principal(
                    cajas_calzado, img_rgba_completa, vec, get_centroide_tuc(cur))
                if box_principal is not None:
                    cajas = [box_principal]
                else:
                    cajas = detectar_pares_calzado(img_original)
            with c:
                for idx_en_foto, caja in enumerate(cajas):
                    # `idx` es el índice de la variante DENTRO DEL CANDIDATO
                    # (no dentro de la foto): con una sola foto vale
                    # 0,1,2... igual que siempre; con varias, la segunda foto
                    # sigue numerando donde terminó la primera.
                    idx = base_indice + idx_en_foto
                    # Recorte por el CONTORNO REAL de rembg (canal alpha), no por la caja
                    # rectangular de OWL-ViT: esta última en zero-shot suele quedar más
                    # chica que el calzado y cortaba la punta/talón en línea recta (bug
                    # reportado 2026-07-22: "el calzado queda recortado en una parte"). El
                    # alpha de rembg YA tiene la silueta curva completa; usamos el bounding
                    # box del componente conexo que más solapa la caja de OWL-ViT (margen
                    # chico 3%). Si no hay componente que solape (foto atípica / alpha
                    # vacío), degrada al comportamiento anterior: caja de OWL-ViT + 12%.
                    w, h = img_original.size
                    # img_para_aislar = el RGBA sobre el que se recorta/compone. Por defecto
                    # es el resultado de rembg sobre la foto completa (camino de 1 calzado);
                    # en el camino de VARIOS calzados se sustituye por una copia con la
                    # instancia correcta ya aislada por SAM.
                    img_para_aislar = img_rgba_completa
                    cobertura_mascara = None
                    area_mascara_px = None
                    mascara_instancia = mascaras_por_caja.get(idx)
                    if mascara_instancia is None and len(cajas_calzado) > 1:
                        # VARIOS calzados en la foto pero sin máscara precomputada (camino de
                        # "1 sola instancia principal", ver arriba): el problema es de
                        # SEGMENTACIÓN DE INSTANCIAS, no de geometría de cajas. Las cajas de
                        # OWL-ViT de dos calzados en ángulos/vitrinas distintas se solapan en
                        # pantalla aunque los calzados no se toquen en 3D, y
                        # `_bbox_por_contorno_alpha` (componentes conexos del alpha de rembg)
                        # no puede decir qué píxel es de cuál cuando rembg los fusiona. SAM en
                        # modo box-prompt SÍ puede: razona sobre el contenido de píxeles dentro
                        # de la caja e ignora al vecino. Ver _mascara_por_segmentacion.
                        _fase("aislando la instancia correcta (SAM)...")
                        mascara_instancia = _mascara_por_segmentacion(img_original, caja)
                    if mascara_instancia is not None:
                        # INTERSECCIÓN de dos máscaras: SAM aísla la INSTANCIA correcta (quita
                        # al vecino), rembg ya quitó fondo/props/texto y endureció el borde
                        # (trabajo validado que no se debe perder). El AND se queda solo con
                        # los píxeles que ambos consideran "este calzado".
                        from PIL import Image as _Image
                        alpha_rembg = np.asarray(img_rgba_completa.split()[3]) > 128
                        combinada = mascara_instancia & alpha_rembg
                        if not combinada.any():
                            combinada = mascara_instancia  # rembg no coincidió -> confiar en SAM
                        arr = np.array(img_rgba_completa)
                        arr[~combinada, 3] = 0  # todo lo que NO es la instancia -> transparente
                        img_para_aislar = _Image.fromarray(arr, mode="RGBA")
                        # cobertura = qué fracción de la caja de OWL-ViT terminó realmente
                        # cubierta por la máscara final -- señal de "este recorte quedó
                        # incompleto/roto" (visto en pruebas reales: máscaras sanas en píxeles
                        # pero recortes que en pantalla se ven como una esquirla, causa aún no
                        # resuelta del todo). Usada más abajo para decidir cuándo un color debe
                        # apoyarse en los datos de OTRO color de la misma Referencia al buscar
                        # comparables (pedido explícito del usuario, ver _variante_saludable).
                        area_caja = max(1.0, (caja["xmax"] - caja["xmin"]) * (caja["ymax"] - caja["ymin"]))
                        cobertura_mascara = round(float(combinada.sum()) / area_caja, 3)
                        area_mascara_px = int(combinada.sum())  # área absoluta -- cobertura
                        # sola no distingue una "esquirla" de un recorte sano si ambos tienen
                        # caja OWL-ViT chica.
                        ys, xs = np.where(combinada)
                        cx0, cy0, cx1, cy1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
                        mx, my = int((cx1 - cx0) * 0.03), int((cy1 - cy0) * 0.03)
                        x0, y0 = max(0, cx0 - mx), max(0, cy0 - my)
                        x1, y1 = min(w, cx1 + mx), min(h, cy1 + my)
                        # SAM ya separó la instancia -> NO usar borrado por etiqueta ni por
                        # rectángulo del vecino (bbox_contorno_confiable=True los desactiva).
                        etiquetas_alpha = etiqueta_propia = None
                        bbox_contorno_confiable = True
                    else:
                        # UN solo calzado (o SAM no produjo máscara): camino anterior INTACTO
                        # -- recorte por CONTORNO REAL de rembg (canal alpha), no por la caja
                        # rectangular de OWL-ViT, que en zero-shot suele quedar más chica y
                        # cortaba la punta/talón en línea recta (bug 2026-07-22). Si no hay
                        # componente que solape (foto atípica / alpha vacío), degrada a la caja
                        # de OWL-ViT + 12%.
                        resultado_contorno = _bbox_por_contorno_alpha(img_rgba_completa, caja)
                        bbox_contorno, etiquetas_alpha, etiqueta_propia = (
                            resultado_contorno if resultado_contorno is not None else (None, None, None))
                        bbox_contorno_confiable = bbox_contorno is not None
                        if bbox_contorno is not None:
                            x0, y0, x1, y1 = bbox_contorno
                        else:
                            # bbox no confiable (fusión con vecino) o sin componente alguno --
                            # el RECORTE degrada al rectángulo+12%, pero si sí hay etiquetas
                            # (caso fusión), el borrado por pixel abajo las sigue usando --
                            # borra de PASO cualquier prop transparente que la detección
                            # zero-shot no vio (ej. vitrinas de vidrio, bug real 2026-07-22).
                            mx, my = int((caja["xmax"] - caja["xmin"]) * 0.12), int((caja["ymax"] - caja["ymin"]) * 0.12)
                            x0, y0 = max(0, int(caja["xmin"]) - mx), max(0, int(caja["ymin"]) - my)
                            x1, y1 = min(w, int(caja["xmax"]) + mx), min(h, int(caja["ymax"]) + my)

                    # FASE 2: la variante se crea en el `lote.sqlite`. `bbox`
                    # pasa de `jsonb` a TEXT con JSON; el resto es igual,
                    # incluido el `RETURNING variante_id` (SQLite lo soporta
                    # desde 3.35, y acá corre 3.50).
                    cl.execute("""
                        INSERT INTO candidato_variante
                            (candidato_id, indice, bbox, confianza_deteccion)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (candidato_id, indice) DO UPDATE SET bbox = excluded.bbox
                        RETURNING variante_id
                    """, (candidato_id, idx, json.dumps(
                        {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1}), caja["score"]))
                    variante_id = cl.fetchone()[0]

                    ruta_limpia_rel = crop_path.replace(".png", f"_v{idx}_limpia.png")
                    ruta_limpia_abs = DATOS_COMPARTIDOS / ruta_limpia_rel
                    (color_principal, color_suela, lab_principal, lab_suela,
                     color_plantilla, lab_plantilla) = _aislar_calzado_y_color_zonas(
                        img_para_aislar, caja, cajas_calzado, x0, y0, x1, y1, ruta_limpia_abs,
                        etiquetas_alpha, etiqueta_propia, bbox_contorno_confiable)
                    # Plan 2026-09-04, paso 6: si el proveedor declaró el color
                    # en su propio Excel (AsistenteComprasEscritorio), ese dato
                    # gana sobre el que acaba de inferir el análisis visual --
                    # el declarado no tiene el ruido de fondo/iluminación/
                    # reflejo que sí puede confundir al k-means. `color_suela`
                    # y los LAB (usados para contraste de suela) NO se tocan:
                    # el proveedor solo declara UN color por producto, no por
                    # zona, así que no hay con qué reemplazar esos otros dos.
                    if color_declarado_candidato:
                        color_principal = color_declarado_candidato

                    ruta_flor_rel = None
                    detalle_flor = _extraer_detalle_flor(img_original, cajas_props, x0, y0, x1, y1)
                    if detalle_flor is not None:
                        ruta_flor_rel = crop_path.replace(".png", f"_v{idx}_flor.png")
                        detalle_flor.save(DATOS_COMPARTIDOS / ruta_flor_rel)
                    suela_contraste = color_suela_delta_e = None
                    if ATRIBUTOS_DISENO.get("suela_contraste"):
                        suela_contraste, color_suela_delta_e = _suela_contraste(lab_principal, lab_suela)

                    _fase("calculando embeddings visuales...")
                    resultados = vec.procesar_imagen(ruta_limpia_abs, necesita_embeddings=True,
                                                      necesita_color=False, ya_sin_fondo=True)
                    for modelo, r in resultados["embeddings"].items():
                        # El vector deja de ser un `real[]` de Postgres y pasa
                        # a BLOB de float32 -- mismo contenido bit por bit
                        # (ver `almacen.vector_a_blob`), así que las
                        # similitudes coseno no se mueven ni en el último
                        # decimal.
                        cl.execute("""
                            INSERT INTO candidato_embedding
                                (variante_id, modelo_embedding, modelo_version, dim, vector, cargado_en)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (variante_id, modelo_embedding) DO NOTHING
                        """, (variante_id, modelo, MODELOS_DISPONIBLES[modelo]["checkpoint"],
                              r["dim"], almacen.vector_a_blob(r["vector"]), almacen.ahora()))

                    # Los tres booleanos (`suela_contraste` acá, `es_multicolor`
                    # y `advertencia_multiples_pares` abajo) se escriben con
                    # `int(...)` porque SQLite no tiene tipo boolean; el None
                    # se conserva como None ("no se pudo determinar"), que es
                    # un valor distinto de False en este dominio.
                    cl.execute("""
                        UPDATE candidato_variante
                        SET imagen_limpia_path = ?, color_principal = ?, color_suela = ?,
                            suela_contraste = ?, color_suela_delta_e = ?, detalle_flor_path = ?,
                            cobertura_mascara = ?, area_mascara_px = ?, color_plantilla = ?
                        WHERE variante_id = ?
                    """, (ruta_limpia_rel, color_principal, color_suela,
                          None if suela_contraste is None else int(suela_contraste),
                          color_suela_delta_e, ruta_flor_rel, cobertura_mascara,
                          area_mascara_px, color_plantilla, variante_id))

                # Estado acumulado del candidato tras ESTA foto. Con una sola
                # foto los tres valores son los de siempre (base_indice=0, un
                # solo `colores_lista`); con varias, `n_variantes_color` cuenta
                # todas las variantes creadas hasta acá y la lista de colores
                # une la de cada foto sin repetir.
                total_variantes = base_indice + len(cajas)
                base_por_candidato[candidato_id] = total_variantes
                acumulado = colores_por_candidato.setdefault(candidato_id, [])
                for col in (colores_lista or "").split(","):
                    col = col.strip()
                    if col and col not in acumulado:
                        acumulado.append(col)
                colores_todos = ", ".join(acumulado) if acumulado else colores_lista
                cl.execute("""
                    UPDATE candidato
                    SET n_variantes_color = ?, es_multicolor = ?,
                        n_colores_detectados = ?, colores_detectados = ?,
                        advertencia_multiples_pares = ?
                    WHERE candidato_id = ?
                """, (total_variantes,
                      None if es_multicolor is None else int(es_multicolor),
                      max(n_colores or 0, len(acumulado)) or n_colores, colores_todos,
                      int(n_pares_estimado > 1), candidato_id))
                # Variantes que sobraron de una corrida anterior con MÁS fotos
                # (el proveedor mandó 3 colores y ahora manda 2): se van con su
                # embedding por ON DELETE CASCADE.
                if i == n or filas[i][0] != candidato_id:
                    cl.execute("DELETE FROM candidato_variante "
                               "WHERE candidato_id = ? AND indice >= ?",
                               (candidato_id, total_variantes))
        except Exception as exc:
            print(f"  [error] vectorizando candidato_id={candidato_id}: {exc}", flush=True)
            _logger().exception(f"vectorizando candidato_id={candidato_id}")

    _reportar_progreso(3, 4, "Categorizando y puntuando el catálogo...", sub=n, sub_total=n)
    categorizar_candidatos(cur, candidato_ids, modelo_activo=modelo_activo)
    inferir_genero_por_talla(cur, candidato_ids)
    derivar_estructura_calzado(cur, candidato_ids)
    detectar_mismo_modelo_otro_color(cur, candidato_ids, modelo_activo=modelo_activo)
    puntuar_candidatos(cur, candidato_ids, modelo_activo=modelo_activo)
    # FASE 2: el `c.commit()` que había acá ya no tiene nada que confirmar --
    # todo lo que escribe esta función (variantes, embeddings, atributos,
    # score) va al `lote.sqlite`, que trabaja en autocommit. Se deja el
    # rollback explícito para cerrar limpia la transacción de LECTURA que
    # psycopg2 abrió sola al consultar el índice permanente, y que si no se
    # cierra queda como "idle in transaction" en la base de producción.
    c.rollback()


def derivar_estructura_calzado(cur, candidato_ids):
    """v1: tiene_tacon/tiene_plataforma derivados de categoria_declarada (texto) --
    barato y ya disponible, pero NO es un clasificador visual independiente. Las
    categorías reales observadas ya distinguen esto razonablemente bien ('Tacones',
    'Cuñas y plataformas' vs 'Sandalias'/'Casual'/'Deportivos'). Un clasificador propio
    sobre DINOv2 (entrenable en segundos con estas mismas categorías como supervisión
    débil) es la mejora natural si esta heurística no alcanza -- queda documentado
    como siguiente paso, no implementado todavía.

    FASE 2: escribe en el `lote.sqlite`, y la comparación de texto se hace en
    PYTHON en vez de en SQL. No es un capricho: el `ILIKE` de Postgres plega
    mayúsculas con reglas Unicode completas, y el `LIKE` de SQLite solo lo
    hace para ASCII -- con 'cuñ' de por medio (y una 'Ñ' mayúscula
    perfectamente posible en el catálogo de un proveedor) traducir el ILIKE a
    LIKE habría cambiado el resultado en silencio para justo esas categorías.
    `str.lower()` de Python sí plega Unicode, igual que Postgres."""
    cl = _cl()
    if not candidato_ids:
        return
    cl.execute(f"""
        SELECT candidato_id, categoria_declarada FROM candidato
        WHERE candidato_id IN {_en(candidato_ids)} AND categoria_declarada IS NOT NULL
    """, list(candidato_ids))
    for candidato_id, categoria in cl.fetchall():
        cat = categoria.lower()
        tiene_tacon = "tac" in cat
        tiene_plataforma = "plataforma" in cat or "cuñ" in cat
        cl.execute("""
            UPDATE candidato SET tiene_tacon = ?, tiene_plataforma = ?
            WHERE candidato_id = ?
        """, (int(tiene_tacon), int(tiene_plataforma), candidato_id))


def detectar_mismo_modelo_otro_color(cur, candidato_ids, umbral_estructura=0.96,
                                      modelo_activo: str = "estandar"):
    # NOTA (probado con catálogo real 2026-07-21): 0.90 generó demasiados falsos positivos
    # -- sandalias planas genéricas de la misma línea comparten estructura DINOv2 muy alta
    # entre sí sin ser el mismo molde. 0.96 es más conservador; sigue siendo una calibración
    # v1, no validada contra un ground truth real de "mismo molde" -- ajustar si hace falta.
    """Cruza TODOS los candidatos del mismo lote entre sí por similitud DINOv2 (forma/
    estructura, no color). Si dos candidatos son estructuralmente casi idénticos
    (>= umbral) pero su color_principal difiere, son casi seguro el MISMO modelo vendido
    en otro color bajo un código de proveedor distinto -- se listan como
    'posibles_mismo_modelo'. Si la similitud estructural NO es alta, son modelos
    genuinamente distintos (aunque el color coincida) y no se listan. No requiere
    separar instancias dentro de una foto -- compara candidatos completos entre sí."""
    modelo_estructura = "dinov2_ti" if modelo_activo == "ti" else "dino_v2"
    # FASE 2: todo lo que compara esta función es de ESTE lote (candidatos del
    # mismo catálogo entre sí), así que ahora es 100% local -- ni una consulta
    # a Postgres. El `ORDER BY` es nuevo y necesario: la matriz de similitudes
    # se indexa por posición de fila, y sin orden explícito SQLite no garantiza
    # cuál viene primero.
    cl = _cl()
    if not candidato_ids:
        return
    cl.execute(f"""
        SELECT c.candidato_id, c.codigo_proveedor, v.color_principal, e.vector
        FROM candidato c
        JOIN candidato_variante v ON v.candidato_id = c.candidato_id AND v.indice = 0
        JOIN candidato_embedding e ON e.variante_id = v.variante_id AND e.modelo_embedding = ?
        WHERE c.candidato_id IN {_en(candidato_ids)}
        ORDER BY c.candidato_id
    """, [modelo_estructura, *candidato_ids])
    filas = cl.fetchall()
    if len(filas) < 2:
        return
    ids = [f[0] for f in filas]
    codigos = [f[1] for f in filas]
    colores = [f[2] for f in filas]
    vectores = np.array([almacen.blob_a_vector(f[3]) for f in filas], dtype=np.float32)
    normas = np.linalg.norm(vectores, axis=1, keepdims=True); normas[normas == 0] = 1.0
    vectores = vectores / normas
    sims = vectores @ vectores.T

    hermanos = {i: [] for i in range(len(ids))}
    for i in range(len(ids)):
        for j in range(len(ids)):
            if i == j:
                continue
            if sims[i, j] >= umbral_estructura and colores[i] and colores[j] and colores[i] != colores[j]:
                hermanos[i].append(codigos[j])

    for i, candidato_id in enumerate(ids):
        if hermanos[i]:
            cl.execute("UPDATE candidato SET posibles_mismo_modelo = ? WHERE candidato_id = ?",
                       (", ".join(sorted(set(hermanos[i]))), candidato_id))


def inferir_genero_por_talla(cur, candidato_ids):
    """La curva de tallas (cuando el catálogo la muestra como texto, ej. 'TALLA: 22-27')
    es una señal MÁS CONFIABLE que el voto visual -- la talla no depende de qué tan
    parecida se vea la foto a otros productos. Se aplica DESPUÉS de categorizar_candidatos
    y SOBREESCRIBE el genero_canonico votado cuando la regla de talla es decisiva (rangos
    no solapados: infantil/junior/caballero -- ver cfg.rule_tuc_talla_genero). En la zona
    ambigua entre dama/caballero (37-40) no hay regla y se conserva el voto visual.

    FASE 2: la curva de tallas declarada sale del `lote.sqlite`; las REGLAS
    (`cfg.rule_tuc_talla_genero`) siguen en Postgres, que es donde debe vivir
    una tabla de gobernanza compartida por todos los lotes."""
    cl = _cl()
    if not candidato_ids:
        return
    cl.execute(f"""
        SELECT c.candidato_id, r.talla_min, r.talla_max
        FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor AND r.catalogo_origen = c.catalogo_origen
        WHERE c.candidato_id IN {_en(candidato_ids)}
          AND r.talla_min IS NOT NULL AND r.talla_max IS NOT NULL
        ORDER BY c.candidato_id
    """, list(candidato_ids))
    filas = cl.fetchall()
    if not filas:
        return
    cur.execute("SELECT genero_inferido, talla_desde, talla_hasta, prioridad FROM cfg.rule_tuc_talla_genero WHERE activo")
    reglas = cur.fetchall()
    for candidato_id, talla_min, talla_max in filas:
        # Contención total (talla_desde<=min Y talla_hasta>=max) deja sin regla cualquier
        # rango que CRUCE dos tramos gobernados (ej. "29-34" pisa infantil 19-33 Y junior
        # 34-36 sin caer completo en ninguno -- bug real observado con TALLA:29-34, RP9309).
        # Se reemplaza por mayor SOLAPAMIENTO: la regla cuyo tramo cubre más tallas del
        # rango declarado gana (empate lo resuelve `prioridad`), en vez de exigir que el
        # rango completo quepa adentro.
        mejor, mejor_solape = None, 0
        for genero_inferido, talla_desde, talla_hasta, prioridad in reglas:
            solape = min(talla_max, talla_hasta) - max(talla_min, talla_desde) + 1
            if solape > mejor_solape or (solape == mejor_solape and solape > 0 and
                                          mejor is not None and prioridad < mejor[1]):
                mejor, mejor_solape = (genero_inferido, prioridad), solape
        if mejor and mejor_solape > 0:
            cl.execute("UPDATE candidato SET genero_canonico = ? WHERE candidato_id = ?",
                       (mejor[0], candidato_id))


def _vector_representativo(candidato_id: int, modelo: str = "fashion_siglip"):
    """Promedio L2-normalizado de los vectores de TODAS las variantes de color de un
    candidato -- usado para categorizar/puntuar el candidato como unidad de compra
    (independiente de cuántos colores traiga la foto). Para búsquedas de similitud
    "este color específico" se usa el vector de una variante puntual, no este promedio.

    FASE 2: lee del `lote.sqlite` del lote activo, ya no de `staging_tuc`. El
    ORDER BY es nuevo y NO es cosmético: el promedio de N vectores float32 no
    es exactamente asociativo, así que sin un orden fijo el resultado podría
    variar en el último bit entre corridas -- Postgres devolvía las filas en
    un orden estable de hecho (inserción), acá se pide explícito."""
    cl = _cl()
    cl.execute("""
        SELECT e.vector FROM candidato_embedding e
        JOIN candidato_variante v ON v.variante_id = e.variante_id
        WHERE v.candidato_id = ? AND e.modelo_embedding = ?
        ORDER BY v.indice
    """, (candidato_id, modelo))
    vectores = [np.array(almacen.blob_a_vector(r[0]), dtype=np.float32) for r in cl.fetchall()]
    if not vectores:
        return None
    normados = [v / (np.linalg.norm(v) or 1.0) for v in vectores]
    promedio = np.mean(normados, axis=0)
    return promedio / (np.linalg.norm(promedio) or 1.0)


ALPHA_FUSION_SIGLIP = 0.5  # §1 del análisis de similitud: SigLIP (estilo/moda) + DINOv2 (forma/
# estructura) fusionados en vez de usar solo SigLIP -- DINOv2 se calculaba pero no se usaba en
# ningún ranking hasta ahora. Bajado de 0.6->0.5 (2026-07-22): con 0.6 (más peso a estilo)
# un tenis chunky tipo "lifestyle" votó erróneamente como "Sandalias" -- estructuralmente
# (DINOv2) un tenis y una sandalia son muy distintos, pero su "estilo" superficial (SigLIP)
# puede confundirse. Sigue siendo v1 sin calibrar contra ground truth amplio.
SIMILITUD_MINIMA_COMPARABLE = 0.75  # ajustado 0.45->0.80->0.75 a pedido del usuario
# 2026-07-23 ("abajo de 80% hace comparaciones muy diferentes, no me gustan", luego
# "creo que el umbral puede quedar en un 75%"). ADVERTENCIA dejada por escrito porque
# sigue en tensión con la calibración anterior: los matches REALES observados hasta
# ahora (fotos válidas, mismo modelo/estilo genuino) caían en el rango 0.53-0.69 --
# ningún caso observado hasta hoy llegó a 0.75. Es probable que la mayoría de candidatos
# queden sin ningún comparable ("no hay nada por recomendar"), lo cual el usuario ya
# confirmó que prefiere ("mejor no comparables que una mala comparación"). Si tras
# probar con fotos reales la mayoría de candidatos aparecen sin comparables, el problema
# no es "bajar el umbral" sino que el score en sí no refleja bien la similitud percibida
# -- reportarlo antes de simplemente revertir el número.
# Válido SOLO para modelo_activo="estandar" (fusión fashion_siglip+dino_v2). Ver
# UMBRAL_POR_MODELO para el valor de modo "ti", que vive en un espacio de coseno
# con otra distribución -- reutilizar 0.75 ahí filtraba (o dejaba pasar) sin
# ningún fundamento medido.

# Métodos que trabajan con UN SOLO espacio de vectores (sin fusión) y con qué
# modelo de embedding lo hacen. Todo lo que antes se bifurcaba con
# `modelo_activo == "ti"` se bifurca ahora con `modelo_activo in MODELO_UNICO`:
# la mecánica es idéntica (un vector del candidato, un índice, similitud
# directa), lo único que cambia es el nombre del modelo.
#
# "fashion"/"dino" (2026-09-16, pedido explícito del dueño): la fusión 50/50 de
# "estandar" resultó CONTRAPRODUCENTE medida sobre los 18 candidatos reales del
# lote Prueba2 contra el catálogo TUC -- promediar una señal fuerte (dino_v2)
# con una más débil (fashion_siglip) tira el promedio abajo del umbral más
# seguido que cualquiera de los dos componentes solos. Ahora cada componente se
# puede usar SOLO, sin tocar el cálculo del vector: `vectorizar_y_puntuar_
# candidatos` con cualquier método no-TI ya guarda fashion_siglip Y dino_v2 por
# separado en `candidato_embedding` (MODELOS = [...]), así que "fashion" y
# "dino" no computan nada nuevo: solo eligen cuál de los dos leer.
MODELO_UNICO = {
    "ti": "dinov2_ti",
    "fashion": "fashion_siglip",
    "dino": "dino_v2",
}

# OJO: este dict es el del DOMINIO 'tuc' ÚNICAMENTE. No leerlo directo -- usar
# `umbral_comparable(modelo_activo, dominio)`. Contra el índice del dominio
# 'marca' estos valores dejan el canal sin un solo comparable (medido); ese
# dominio tiene su propio dict calibrado, `UMBRAL_POR_MODELO_MARCA`.
UMBRAL_POR_MODELO = {
    "estandar": SIMILITUD_MINIMA_COMPARABLE,
    # Estimación inicial 2026-09-07 (plan 2026-09-04, paso 4), medida sobre
    # silver.fct_embedding_ti_codigo (vectores REALES de TI, 9,379 productos):
    # entre productos de la MISMA categoría (el filtro de categoría ya corre
    # antes de este umbral, así que es la población real donde se aplica) la
    # similitud tiene mediana 0.71, cuartil inferior 0.52, cuartil superior
    # 0.82 -- categorías amplias como "Casual"/"Sandalias" agrupan productos
    # visualmente muy distintos entre sí, así que el cuartil inferior es en
    # buena parte ruido de taxonomía, no similitud real. Un lote de 20
    # calzados genuinamente comparables entre sí (misma línea Converse) dio
    # similitudes de 0.70 a 0.86, alineado con el cuartil superior. 0.65 corta
    # el peor cuarto de "mismos en categoría pero mal parecidos" sin excluir
    # matches genuinos como esos. NO ES una calibración validada contra casos
    # reales etiquetados a mano (el paso pendiente real) -- es un punto de
    # partida medido, a ajustar con el uso real.
    "ti": 0.65,

    # ── "fashion" y "dino": 0.75 los dos, MEDIDO, no copiado ────────────────
    # Calibrados 2026-09-16 sobre los 18 candidatos REALES de Prueba2 contra el
    # índice TUC (10,190 fotos), con el criterio del dueño: "es mejor un solo
    # comparable, que muchos comparables que no son buenos". NO se eligió
    # mirando cuántos resultados da cada corte, sino si los comparables que
    # devuelve a ese corte son razonables de verdad. Como no se pueden ver las
    # fotos desde el código, "razonable" se midió con dos proxies sobre los
    # atributos reales del catálogo (bronze.raw_tuc_atributos): (a) que la
    # familia de color del comparable coincida con la del candidato, y (b)
    # COHERENCIA ESTRUCTURAL del conjunto devuelto -- acuerdo par a par en
    # tipo_norm/sistema_cierre/forma_puntera/material_norm/color_familia,
    # contra la línea base de pares al azar DENTRO de la misma máscara de
    # categoría+género (0.459). La categoría no sirve de proxy: ya es filtro
    # duro, así que da 1.00 en todos los cortes.
    #
    #   fashion_siglip solo -- corte: cands con comparable / coherencia (lift)
    #     0.70: 14/18  0.829 (1.80x)   color_ok 25/39
    #     0.72:  6/18  0.832 (1.81x)   color_ok 10/16
    #     0.74:  2/18  1.000 (2.18x)   color_ok 3/3
    #     0.75:  2/18  1.000 (2.18x)   color_ok 3/3
    #     0.76:  1/18  (un solo comparable, sin par que medir)
    #     0.77+: 0/18  (nada pasa; el mejor match de TODO el lote es 0.768)
    #   La precisión SÍ mejora al subir el corte (0.83 -> 1.00 de coherencia,
    #   color 64% -> 100%), así que se sube hasta el último punto donde todavía
    #   se puede verificar que lo que pasa es bueno: 0.75. 0.76 deja un único
    #   comparable suelto (no verificable) y 0.77 deja el método inservible.
    "fashion": 0.75,

    #   dino_v2 solo -- corte: cands con comparable / coherencia (lift)
    #     0.72: 17/18  0.854 (1.86x)   color_ok 0.66
    #     0.74: 16/18  0.852 (1.86x)   color_ok 0.64
    #     0.75: 15/18  0.846 (1.84x)   color_ok 0.63
    #     0.76: 13/18  0.829 (1.80x)   color_ok 0.57
    #     0.78:  8/18  0.800 (1.74x)   color_ok 0.55
    #     0.79:  5/18  0.800 (1.74x)   color_ok 0.43
    #   Acá subir el corte NO compra precisión: los dos proxies EMPEORAN por
    #   encima de 0.75 (coherencia 0.85 -> 0.80, color 0.66 -> 0.43). Tiene
    #   sentido para un modelo de forma pura: sus vecinos más extremos son
    #   estructuralmente casi idénticos pero de otro color/material. Así que no
    #   hay un "canje precisión vs cantidad" que resolver a favor de la
    #   precisión: 0.75 es el borde superior de la meseta de mejor precisión
    #   (0.72-0.75, indistinguibles entre sí) y el último corte con muestra
    #   suficiente para medirlo (130 pares). Pasado ese punto se paga cantidad
    #   Y calidad a la vez.
    "dino": 0.75,
}

# ═══════════════════════════════════════════════════════════════════════════
# El umbral es POR DOMINIO, no solo por modelo (medido 2026-09-16)
# ═══════════════════════════════════════════════════════════════════════════
#
# `UMBRAL_POR_MODELO` (arriba) quedó calibrado contra el índice del dominio
# 'tuc' y SOLO vale para ese dominio. Reutilizarlo contra el dominio 'marca'
# dejaba el canal TC Marcas inservible: con 0.75, los 18 candidatos reales de
# Prueba2 daban 0 comparables en "fashion" (su MEJOR match contra marca va de
# 0.495 a 0.664) y 0 en "estandar" (máximo 0.717).
#
# Por qué el mismo modelo da otro rango contra otro índice -- medido, no
# supuesto. Se usó cada foto DEL PROPIO índice de marca como pseudo-candidato
# contra el resto del índice (400 productos con género+tipo+subtipo, misma
# máscara género+palabra_tipo que aplica el motor). Dentro del mismo estilo
# fotográfico la mejor similitud es altísima:
#     fashion  p10 0.831  mediana 0.898  p90 0.944
#     dino     p10 0.872  mediana 0.946  p90 0.971
#     estandar p10 0.841  mediana 0.915  p90 0.950
# Contra los 18 candidatos REALES (foto de catálogo de proveedor -> índice de
# marca, o sea comparación CRUZADA entre estilos fotográficos):
#     fashion  mediana 0.621  máximo 0.664
#     dino     mediana 0.769  máximo 0.836
#     estandar mediana 0.668  máximo 0.717
# O sea: la hipótesis del techo por estilo fotográfico se CONFIRMA para
# fashion/estandar (fashion pierde ~0.28 al cruzar estilos, y contra tuc su
# máximo cruzado era 0.768 vs 0.664 contra marca), pero NO para dino, cuyo
# máximo cruzado contra marca (0.836) es igual al que da contra tuc (0.829).
# dino aguanta el cambio de estilo; fashion no. Por eso el umbral se separa
# por dominio Y sigue siendo distinto por modelo dentro de cada dominio.
#
# ── Cómo se eligió cada corte (mismo principio del dueño: "es mejor un solo
#    comparable, que muchos comparables que no son buenos") ──────────────────
#
# Dos proxies independientes sobre los 18 candidatos reales, porque tipo/
# subtipo NO sirven de proxy acá (la máscara género+palabra_tipo ya los fuerza:
# la línea base de pares al azar dentro de la máscara da 1.000 en los dos, y
# además solo 875 de las 8,436 fotos del índice tienen subtipo poblado):
#   (a) COLOR -- el mismo proxy (a) de la calibración tuc: que la familia de
#       color del comparable (`silver.fct_color_imagen`, poblado en las 8,436
#       fotos) coincida con la de la variante del candidato. Línea base = la
#       prevalencia real de esa familia DENTRO de la máscara del candidato
#       (0.235), o sea lo que daría elegir al azar de lo que el filtro duro ya
#       dejó pasar.
#   (b) ACUERDO ENTRE MODELOS -- percentil que el comparable elegido por un
#       modelo ocupa en la distribución de similitud del OTRO modelo, dentro de
#       la misma máscara. Línea base 0.500 por construcción.
#
#   fashion  (corte: cands con comparable / color_ok (lift) / percentil testigo)
#     0.45  18/18   0.287 (1.22)   0.591
#     0.48  18/18   0.283 (1.21)   0.591      <-- elegido
#     0.50  17/18   0.260 (1.11)   0.598
#     0.55  16/18   0.228 (0.97)   0.603
#     0.60  16/18   0.143 (0.61)   0.561
#     0.65   2/18   0.000 (0.00)   0.326
#     0.70   0/18      --             --
#   Subir el corte NO compra precisión: la destruye (lift de color 1.22 -> 0.61
#   -> 0.00). 0.48 es el corte MÁS ALTO que todavía está en la meseta de mejor
#   coherencia; en 0.50 el color ya cae y de 0.65 para arriba el método queda
#   inservible. El acuerdo entre modelos confirma que no hay nada que ganar
#   arriba (plano ~0.59 y luego se hunde a 0.33).
#
#   dino
#     0.65  15/18   0.511 (2.18)   0.517      <-- elegido
#     0.70  14/18   0.493 (2.10)   0.507
#     0.75  11/18   0.510 (2.17)   0.471
#   dino es el único que sobrevive al 0.75 heredado (11/18), pero tampoco gana
#   nada con él: los dos proxies son planos hasta 0.65 y de ahí empeoran -- en
#   0.75 el acuerdo entre modelos cae POR DEBAJO de la línea base (0.471 < 0.500),
#   o sea que los comparables que solo pasan a 0.75 son los que el otro espacio
#   considera peores que el promedio. 0.65 es el borde superior de la meseta:
#   se gana cantidad (11/18 -> 15/18) sin pagar nada de precisión.
#
#   estandar
#     0.58  15/18   0.389 (1.66)
#     0.60  15/18   0.402 (1.71)
#     0.62  14/18   0.385 (1.64)   <-- elegido
#     0.65  14/18   0.347 (1.48)
#     0.70   3/18   0.333 (1.42)   (3 comparables, ya no es medible)
#     0.75   0/18      --
#   Misma forma: meseta plana hasta 0.62 y caída real en 0.65. 0.62 es el corte
#   más alto todavía respaldado por la evidencia y con muestra suficiente (78
#   comparables) para haberlo medido.
#
#   ti -- SIN MEDIR contra el dominio marca, a propósito. Los candidatos de
#   Prueba2 no tienen vector `dinov2_ti` guardado (18 de 18 sin vector), así que
#   NO hay forma de medir la comparación cruzada en ese espacio con datos
#   reales. Se deja el mismo 0.65 del dominio tuc porque no hay evidencia para
#   moverlo, NO porque se haya verificado que sirve acá. Dentro del propio
#   índice de marca la mejor similitud TI tiene mediana 0.922 (271 productos),
#   pero ese número es del mismo estilo fotográfico y no dice nada del cruce.
UMBRAL_POR_MODELO_MARCA = {
    "fashion": 0.48,
    "dino": 0.65,
    "estandar": 0.62,
    "ti": 0.65,  # heredado del dominio tuc, sin medición cruzada propia (ver arriba)
}


def umbral_comparable(modelo_activo: str, dominio: str) -> float:
    """Umbral de similitud mínima para aceptar un comparable, POR DOMINIO.

    `dominio` es 'marca' (índice TC Marcas) o 'tuc' (catálogo genérico). No
    tiene default a propósito: el llamador siempre sabe contra qué índice está
    buscando, y un default silencioso es justo el bug que esto viene a cerrar
    (todo el canal marca corriendo con el umbral calibrado para tuc).
    """
    if dominio == "marca":
        return UMBRAL_POR_MODELO_MARCA[modelo_activo]
    if dominio == "tuc":
        return UMBRAL_POR_MODELO[modelo_activo]
    raise ValueError(f"dominio desconocido: {dominio!r} (esperaba 'marca' o 'tuc')")


# Plan 2026-09-07, paso 2.4 -- fórmula determinística de score. Constantes
# FIJAS, derivadas UNA VEZ de datos reales (71 candidatos puntuables +
# 8,718 productos del índice kNN TUC), escritas en el código en vez de
# consultadas en tiempo de ejecución. Requisito explícito del usuario: "bajo
# las mismas condiciones, un producto debería replicar su calificación, sin
# importar el lote que se esté corriendo" -- un percentil contra una tabla
# (aunque esa tabla estuviera congelada y versionada) seguía siendo una
# dependencia externa variable. Estos números NO se recalculan nunca en
# `puntuar_candidatos`; si algún día hay que revisarlos, es un cambio de
# código deliberado (subir ANCLA_SIMILITUD/CORTES_CLASIFICACION es análogo a
# cambiar MARGEN_VENTA_DEFAULT), nunca un recálculo automático.
#
# Punto de partida medido, NO una calibración validada contra casos reales
# etiquetados a mano -- esa calibración (Spearman contra 40 candidatos que el
# comprador etiquete sin ver el grado) es la que puede decir si estos cortes
# son correctos, no solo consistentes. Cambiar estos números cambia el grado
# de TODO lo ya calificado -- hacerlo a propósito, nunca al vuelo.
ANCLA_SIMILITUD = {
    # Techo de "match innegable": el extremo superior del rango de
    # similitudes ya documentado arriba (un lote de 20 calzados genuinamente
    # comparables dio 0.70-0.86 en "ti"). Es el ancla del TECHO de la escala
    # (sim_norm=1.0 = "no se puede parecer más"), no su punto medio.
    "ti": 0.86,
    "estandar": 0.90,  # mismo criterio, sin lote de referencia propio medido
                        # todavía para SigLIP+DINOv2 -- punto de partida.
    # Medido 2026-09-16 sobre el mejor match de cada uno de los 18 candidatos
    # de Prueba2 contra el índice TUC (con la máscara categoría+género ya
    # aplicada, o sea la población real donde esto se usa):
    #   fashion_siglip solo: mediana 0.715, p90 0.742, MÁXIMO 0.768
    #   dino_v2 solo:        mediana 0.776, p90 0.807, MÁXIMO 0.829
    # El ancla es el TECHO de la escala ("no se puede parecer más"), así que va
    # apenas por encima del mejor match observado en cada espacio, no en su
    # mediana. OJO: el techo es de comparación CRUZADA (foto de proveedor vs
    # foto de catálogo). Dentro del propio catálogo dos fotos del mismo
    # producto dan ~0.98-1.00 en los dos modelos, pero ese número no aplica acá
    # y usarlo como ancla dejaría todo el lote con sim_norm casi 0.
    "fashion": 0.80,
    "dino": 0.85,
}

# ── ancla del DOMINIO MARCA ─────────────────────────────────────────────────
# `ANCLA_SIMILITUD` (arriba) es el techo medido contra el índice del dominio
# 'tuc'. El índice del dominio 'marca' es otro estilo fotográfico (estudio de
# marca vs. catálogo de proveedor) y su techo real de comparación CRUZADA es
# distinto, así que heredarle el ancla de tuc comprimía el score de todo el
# canal marca hacia abajo (mismo bug de fondo que ya se cerró para el umbral
# con `UMBRAL_POR_MODELO_MARCA`).
#
# METODOLOGÍA (la MISMA que documenta `ANCLA_SIMILITUD` para tuc): mejor match
# de cada candidato REAL de lote (foto de catálogo de proveedor) contra el
# índice del dominio marca, con la máscara categoría+género ya aplicada, o sea
# la población exacta donde esto se usa. Medido 2026-09-16 barriendo los 6
# lotes existentes en disco (Prueba1, Prueba2, Prueba 12.09.2026,
# ComprasChinaCR09, Prueba 03.09.2026, Prueba 08.09.2026). El ancla va apenas
# por encima del mejor match observado (+0.025/+0.03, igual que en tuc), NO en
# la mediana: es el techo de la escala ("no se puede parecer más").
#
#   modelo    n   mediana   p90      p95      MÁXIMO    ancla
#   fashion   18  0.7281    0.7824   0.7881   0.7904 -> 0.82
#   dino      18  0.8411    0.8603   0.8690   0.8771 -> 0.90
#   estandar  18  0.7574    0.7840   0.7895   0.8188 -> 0.85
#   ti        14  0.6889    0.7903   0.8244   0.8358 -> 0.86
#
# Muestra: fashion/dino/estandar siguen siendo los 18 candidatos de Prueba2 --
# NO por no haber buscado más, sino porque los otros 5 lotes no tienen vectores
# fashion_siglip/dino_v2 guardados (solo `dinov2_ti`). Se deja escrito para que
# el próximo que amplíe la muestra sepa que el cuello es ese, no el método.
#
# `ti` SÍ ganó evidencia propia acá (14 candidatos reales de Prueba1 +
# ComprasChinaCR09 + Prueba 12.09.2026, contra el índice ti de marca de 3,570
# productos): su máximo cruzado real es 0.8358, o sea que el 0.86 heredado de
# tuc resultó estar bien puesto. Se queda en 0.86, pero ahora POR MEDICIÓN y no
# por falta de datos (a diferencia de UMBRAL_POR_MODELO_MARCA["ti"], que sigue
# sin medición cruzada).
#
# DIFERENCIA con la estimación rápida de la sesión anterior (fashion 0.664 /
# dino 0.836 / estandar 0.717): dino coincide en orden de magnitud, fashion y
# estandar salen bastante más altos. Aquella medición fue un tanteo sobre un
# solo lote sin reproducir el camino real de `_puntuar_candidatos_marca`; esta
# usa exactamente ese camino (vector representativo promediado por variantes,
# máscara género+tipo con la misma degradación, fusión ALPHA_FUSION_SIGLIP para
# "estandar"). Ante la discrepancia mando la medición que replica el código de
# producción.
#
# DESCARTADO a propósito: medir el techo con los productos del propio índice
# 'tuc' como pseudo-candidatos (2,000 muestreados) daba fashion 0.957 / dino
# 0.961 / estandar 0.943 -- muchísimo más alto porque buena parte del catálogo
# genérico es el MISMO producto físico que está en el índice de marca (casi
# duplicados) y además comparte estilo de foto limpia. Esa población no es la
# de un candidato real de catálogo de proveedor; usarla como ancla habría
# hundido el score de todo el canal.
ANCLA_SIMILITUD_MARCA = {
    "fashion": 0.82,
    "dino": 0.90,
    "estandar": 0.85,
    "ti": 0.86,
}


def ancla_similitud(modelo_activo: str, dominio: str) -> float:
    """Techo de la escala de similitud (sim_norm = 1.0), POR DOMINIO.

    Mismo contrato que `umbral_comparable`: `dominio` es 'marca' o 'tuc' y NO
    tiene default a propósito -- el default silencioso es justamente el bug que
    esto cierra (el canal marca normalizando contra el techo de tuc).
    """
    if dominio == "marca":
        return ANCLA_SIMILITUD_MARCA[modelo_activo]
    if dominio == "tuc":
        return ANCLA_SIMILITUD[modelo_activo]
    raise ValueError(f"dominio desconocido: {dominio!r} (esperaba 'marca' o 'tuc')")


REFERENCIA_DEMANDA = 17.4  # u/mes, media real de velocidad_mensual sobre el
# índice kNN TUC (8,718 productos, gold.agg_tuc_metricas). "Tus similares
# venden al ritmo del promedio del catálogo" -> f_demanda neutro-alto.

# Plan 2026-09-21 (pedido directo del dueño: criterios objetivos, no
# calibración humana). Dos variables que YA se median en el negocio pero
# nunca llegaban al score -- se conectan con el mismo patrón que f_demanda:
# un factor acotado, anclado a una medición real, nunca dominante.
#
# REFERENCIA_ROTACION: mediana real de `rot_mensual` (AVG de "% Rotación" del
# Power BI, gold.agg_tuc_metricas, 8,366 códigos con dato, medido 2026-09-21).
# Mediana = 100.0 porque la mayoría del catálogo rota al 100% (el techo
# natural de la métrica); la media (76.3) queda más abajo por una cola de
# códigos con rotación baja o negativa (11 casos, probablemente ajustes de
# inventario) -- se usa la mediana por ser más representativa del caso típico,
# igual criterio que REFERENCIA_FACTOR_VENTA. Ver [[feedback_rotacion_powerbi]]:
# rotación SIEMPRE sale del % Rotación del Power BI, nunca se recalcula.
REFERENCIA_ROTACION = 100.0

# REFERENCIA_FACTOR_VENTA: mediana real de `factor_venta` ("Factor venta" del
# Excel de rotación TUC, silver.fct_tuc_rotacion, 46,365 filas, medido
# 2026-09-21). Mediana = 0.73; se usa mediana y no media (1.82) porque la
# distribución tiene cola larga (máximo real 85) que la media no representa
# bien. Confirmado invariante mes a mes (correlación 1.0, comentario de
# cargar_tuc_rotacion.py) -- es un dato estable del producto, no una métrica
# que fluctúe por temporada.
REFERENCIA_FACTOR_VENTA = 0.73

# REFERENCIA_PCT_SOBRE_LISTA: mediana real de `pct_sobre_lista`
# (precio_avg/precio_lista, gold.agg_tuc_metricas, 9,930 códigos con dato,
# medido 2026-09-21 desde silver.fct_tuc_precio -- reporte Power BI
# "Artículos por precio"). Mediana = 0.7985: proxy objetivo de "ventas sin
# descuento" (equivalente al pct_sin_promo de AsistenteComprasTEC, pero TUC no
# tiene columna de "venta en promoción" por transacción, así que se infiere
# del precio efectivo de venta contra el precio de lista). 1.0 = se vendió
# siempre al precio de lista; valores menores = se vendió con descuento en
# promedio.
REFERENCIA_PCT_SOBRE_LISTA = 0.7985

K_SOPORTE_PLENO = 3.0  # vecinos efectivos (k_efectivo = 1/Σpeso²) a partir de
# los cuales no hay castigo por poca evidencia. Medido: 68 de 71 candidatos
# reales tienen k_efectivo >= 6.75 -- el umbral en 3.0 solo toca casos
# genuinamente flacos (ej. un candidato con un solo vecino comparable).
# Nota de transparencia (plan de mejora 2026-09-22, Etapa 6): estos 4 cortes
# son un umbral fijado por criterio de negocio, no un resultado calibrado
# contra ventas reales ni contra ningún historial de compras -- no hay
# medición detrás de por qué "S" empieza en 78 y no en 75 u 80. Documentado
# acá para que quede claro que es una decisión de negocio, no un hallazgo
# empírico, y para que quien lo toque a futuro sepa que no rompe ninguna
# validación existente si lo ajusta.
CORTES_CLASIFICACION = {"S": 78, "A": 58, "B": 38, "C": 20}  # score_final >= corte

# ── canal de venta del lote: TC Marcas vs TU Calzado ────────────────────────
#
# Decisión de negocio del dueño (2026-09-10): TU Calzado vende por DOS canales
# y una compra es para uno o para el otro, nunca para los dos a la vez. El
# comprador lo declara UNA VEZ POR LOTE en el paso 1 del asistente y queda en
# los metadatos locales del lote (`lote_meta.canal_venta`, mismo patrón que
# `moneda_proveedor`/`mes_venta_esperado`). Desde acá manda TODO: contra qué
# índice se buscan comparables y contra qué se calcula el puntaje.
#
#   "marca" -> dominio 'marca' (TC Marcas, la línea de marca reconocida)
#   "tuc"   -> dominio 'tuc'   (TU Calzado, el catálogo genérico)
#
# Default "tuc" si el lote no lo declaró: es el comportamiento que tenía el
# motor antes de este cambio, así que un lote viejo se sigue puntuando igual.
CANALES_VENTA = ("marca", "tuc")

# Referencia de demanda PROPIA del canal TC Marcas: 6.30 u/mes, la media real
# de velocidad mensual (unidades netas / meses con venta registrada) sobre las
# 309 referencias del índice de marca que SÍ tienen ventas propias en
# `silver.fct_tcm_ventas` (medido 2026-09-10). NO se reutiliza el 17.4 del
# catálogo genérico: los productos de marca reconocida rotan a otro ritmo y
# medirlos contra la referencia del genérico castigaría a todo el canal.
REFERENCIA_DEMANDA_MARCA = 6.30

# REFERENCIA_ROT30_MARCA / REFERENCIA_PCT_SIN_PROMO_MARCA: plan de mejora
# 2026-09-22, Etapa 3 (unificación TUC/TCM) -- reemplaza la versión anterior
# que dependía de `gold.agg_tcm_metricas_prod` (calculada del CSV
# transaccional del ERP). Ahora ambas salen 100% de Power BI, mismo patrón
# que el canal genérico:
#   - `pct_rotacion`: silver.fct_tcm_rotacion (reporte "Rotación por
#     artículo", filtrado a Catalogo=TCMARCAS -- mismo reporte y misma
#     columna "% Rotación" que usa REFERENCIA_ROTACION del genérico).
#   - `pct_sobre_lista`: silver.fct_tcm_precio (reporte "Artículos por
#     precio", filtrado a Catalogo=TCMARCAS -- mismo cálculo
#     precio_avg/precio_lista que usa REFERENCIA_PCT_SOBRE_LISTA).
# Cobertura real verificada 2026-09-22 sobre el universo ACTIVO (con
# inventario o venta en el período -- una referencia sin inventario inicial
# ni venta no tiene rotación definida, no es "dato faltante"): 3,423/3,490 =
# 98.1%, con 6 meses cargados (2026-02 a 2026-06). Medianas reales: rot30
# (ahora `pct_rotacion`) = 100.0 (techo natural de la métrica, igual patrón
# que REFERENCIA_ROTACION del genérico); pct_sobre_lista = 0.8813 (4,981
# referencias, silver.fct_tcm_precio).
REFERENCIA_ROT30_MARCA = 100.0
REFERENCIA_PCT_SIN_PROMO_MARCA = 0.8813

# REFERENCIA_FACTOR_VENTA_MARCA: mediana real de `factor_venta` para TC Marcas
# (silver.fct_tcm_rotacion, 4,986 referencias con dato, medido 2026-09-21).
# Mismo reporte Power BI "Rotación por artículo" que ya usaba TUCALZADO, esta
# vez filtrado a Catalogo='TCMARCAS' en vez de a codigo_tuc (cargar_tcm_rotacion.py).
# Mediana = 0.13 -- bastante menor que el 0.73 de TUCALZADO, consistente con
# que son catálogos y dinámicas de venta distintas; no se reutiliza la
# referencia del genérico por la misma razón que REFERENCIA_DEMANDA_MARCA.
REFERENCIA_FACTOR_VENTA_MARCA = 0.13

# Cuántos vecinos con velocidad REAL hacen falta para que f_demanda deje de ser
# neutro en el canal marca. De las 8,436 fotos del índice de marca, solo 513
# corresponden a referencias con ventas propias registradas: la mayoría de los
# comparables son producto de mercado que TU Calzado nunca vendió. "Sin venta
# registrada" NO es "vendió cero" -- si no hay al menos 2 vecinos con dato, el
# factor queda en 1.00 en vez de inventar una demanda de cero.
MIN_VECINOS_DEMANDA_MARCA = 2

# Velocidad mensual y tendencia 3m del canal TC Marcas, calculadas al vuelo
# desde las ventas crudas (`silver.fct_tcm_ventas`, 145,110 filas por
# año/mes/referencia/talla). `gold.agg_tcm_metricas` NO tiene estas dos
# columnas (a diferencia de `gold.agg_tuc_metricas`), así que se derivan acá
# con la MISMA definición que usa `calcular_tuc_metricas.py` para el genérico:
#   velocidad = unidades netas totales / meses CON registro de venta
#   tendencia = unidades de los 3 últimos meses / los 3 anteriores
# Va como CTE en la consulta del índice y no como vista nueva en la base a
# propósito: es una agregación de una sola tabla, no hace falta gobernar un
# objeto nuevo para tenerla.
SQL_CTE_VELOCIDAD_MARCA = """
    WITH ventas_mes AS (
        SELECT referencia, ano, mes, SUM(unidades_netas) AS un
        FROM silver.fct_tcm_ventas
        GROUP BY referencia, ano, mes
    ),
    ventas_ord AS (
        SELECT referencia, un,
               ROW_NUMBER() OVER (PARTITION BY referencia ORDER BY ano DESC, mes DESC) AS rn
        FROM ventas_mes
    ),
    vel_marca AS (
        SELECT referencia,
               SUM(un)::numeric / COUNT(*) AS velocidad_mensual,
               CASE
                   WHEN COALESCE(SUM(un) FILTER (WHERE rn BETWEEN 4 AND 6), 0) > 0
                       THEN COALESCE(SUM(un) FILTER (WHERE rn <= 3), 0)::numeric
                            / SUM(un) FILTER (WHERE rn BETWEEN 4 AND 6)
                   WHEN COALESCE(SUM(un) FILTER (WHERE rn <= 3), 0) = 0 THEN 1.0
                   ELSE 2.0
               END AS tendencia_3m
        FROM ventas_ord
        GROUP BY referencia
    ),
    -- factor_venta Y rotación reales de TC Marcas: silver.fct_tcm_rotacion
    -- (mismo reporte Power BI de rotación que TUCALZADO, filtrado a
    -- Catalogo=TCMARCAS -- ver REFERENCIA_FACTOR_VENTA_MARCA /
    -- REFERENCIA_ROT30_MARCA). Promedio simple si hay más de un mes cargado
    -- (hoy solo hay 2026-09). `pct_rotacion` reemplaza a `rot30` del CSV del
    -- ERP -- plan de mejora 2026-09-22, Etapa 3 (unificación TUC/TCM): mismo
    -- dato/fuente que `rot_mensual` usa para TUCALZADO.
    venta_marca AS (
        SELECT referencia, AVG(factor_venta) AS factor_venta, AVG(pct_rotacion) AS pct_rotacion
        FROM silver.fct_tcm_rotacion
        GROUP BY referencia
    ),
    -- "ventas sin descuento" de TC Marcas: silver.fct_tcm_precio (mismo
    -- reporte Power BI "Artículos por precio" que TUCALZADO, filtrado a
    -- Catalogo=TCMARCAS -- ver REFERENCIA_PCT_SOBRE_LISTA_MARCA). Reemplaza a
    -- `pct_sin_promo` del CSV del ERP -- mismo proxy objetivo que ya usa
    -- TUCALZADO (precio_avg/precio_lista), 100% Power BI para ambos canales.
    precio_marca AS (
        SELECT referencia,
               AVG(precio_avg) / NULLIF(AVG(precio_lista), 0) AS pct_sobre_lista
        FROM silver.fct_tcm_precio
        WHERE precio_lista > 0
        GROUP BY referencia
    )
"""


def canal_venta_lote() -> str:
    """"marca" o "tuc" -- para qué canal es la compra de ESTE lote.

    Se lee del `lote.sqlite` del lote activo (mismo mecanismo que usa el resto
    del motor vía `fijar_lote`/`lote_activo`). Nunca revienta: si no hay lote
    fijado, si el archivo no se puede leer o si el valor guardado es basura,
    devuelve "tuc" -- el comportamiento histórico del motor.
    """
    salida = lote_activo()
    if salida is None:
        return "tuc"
    try:
        meta = almacen.leer_meta(salida)
    except Exception:  # noqa: BLE001 -- leer el canal no puede tumbar al motor
        _logger().exception(
            f"no se pudo leer canal_venta de {salida} -- degradando a 'tuc' en silencio")
        return "tuc"
    valor = str(meta.get("canal_venta") or "").strip().lower()
    if valor and valor not in CANALES_VENTA:
        # Auditoría 2026-09-23: antes esto degradaba a "tuc" sin dejar rastro
        # -- un lote de TC Marcas con el valor corrupto se calificaba contra
        # el catálogo equivocado sin que nadie se enterara. Ojo: NO se loguea
        # cuando `valor` está simplemente vacío (lote que aún no pasó por el
        # paso 1) -- eso es normal, no un dato corrupto.
        _logger().error(
            f"canal_venta guardado en {salida} es inválido ({valor!r}, "
            f"esperaba uno de {sorted(CANALES_VENTA)}) -- degradando a 'tuc'")
        return "tuc"
    return valor if valor in CANALES_VENTA else "tuc"


def _norm_txt(texto) -> str:
    """Minúsculas, sin acentos, sin espacios de sobra -- para comparar
    vocabularios escritos por manos distintas.

    El vocabulario de `silver.dim_tcm_producto` está sucio de verdad: el mismo
    género aparece como "MUJER" (2,376 filas) y "Mujer" (1,644), y las
    categorías como "Casual"/"CASUAL" y "Cuñas y plataformas"/"Cunas y
    plataformas". Sin esto, el filtro de tipo/género del canal marca fallaría
    EN SILENCIO (máscara vacía -> se degrada sin filtro) en la mitad del
    índice, que es exactamente el modo de falla que no queremos.
    """
    limpio = unicodedata.normalize("NFKD", str(texto or ""))
    limpio = "".join(ch for ch in limpio if not unicodedata.combining(ch))
    return " ".join(limpio.lower().split())


def variantes_a_calificar(candidato_id: int, modelo_activo: str = "estandar"):
    """Cada COLOR de la referencia, con su propio vector, para calificarlo por
    separado -- 2026-09-17, pedido explícito del dueño.

    Por qué existe: el proveedor genérico vende por bulto. Si una referencia
    trae 3 colores, el comprador se lleva los 3 o ninguno, así que necesita ver
    qué tan bueno es CADA color y no solo el principal. Hasta hoy el puntaje
    usaba `_vector_representativo` (el promedio de todas las variantes) y
    escribía UN score por candidato: un color excelente y uno malo se fundían
    en un número que no existía en la realidad.

    Devuelve `(calificables, sin_vector)`:
      - `calificables`: [(indice, color_principal, v, v_dino)] -- `v_dino` es
        None en los métodos de un solo espacio (`MODELO_UNICO`), igual que en
        `_vectores_duales_representativos`.
      - `sin_vector`: [(indice, color_principal)] de los colores que todavía no
        tienen embedding. NO se les inventa un score: se reportan para que la
        pantalla pueda decir "falta vectorizar este color" en vez de mostrar un
        0 engañoso o esconderlos.

    Para una referencia de UN SOLO color devuelve una sola entrada con el
    vector de esa variante, que es exactamente el mismo vector que devolvía el
    promedio de `_vector_representativo` sobre una sola variante: el caso
    mayoritario no cambia ni en el último decimal.
    """
    cl = _cl()
    cl.execute("""
        SELECT indice, color_principal, variante_id FROM candidato_variante
        WHERE candidato_id = ? ORDER BY indice
    """, (candidato_id,))
    variantes = [tuple(r) for r in cl.fetchall()]
    if not variantes:
        return [], []

    modelos = ([MODELO_UNICO[modelo_activo]] if modelo_activo in MODELO_UNICO
               else ["fashion_siglip", "dino_v2"])
    cl.execute(f"""
        SELECT variante_id, modelo_embedding, vector FROM candidato_embedding
        WHERE variante_id IN {_en([v[2] for v in variantes])}
          AND modelo_embedding IN {_en(modelos)}
    """, [*[v[2] for v in variantes], *modelos])
    por_variante: dict[tuple[int, str], np.ndarray] = {}
    for variante_id, modelo, blob in cl.fetchall():
        v = np.array(almacen.blob_a_vector(blob), dtype=np.float32)
        por_variante[(variante_id, modelo)] = v / (np.linalg.norm(v) or 1.0)

    calificables, sin_vector = [], []
    for indice, color_principal, variante_id in variantes:
        v = por_variante.get((variante_id, modelos[0]))
        if v is None:
            sin_vector.append((indice, color_principal))
            continue
        v_dino = None if len(modelos) == 1 else por_variante.get((variante_id, "dino_v2"))
        calificables.append((indice, color_principal, v, v_dino))
    return calificables, sin_vector


def _guardar_score_variante(candidato_id: int, indice: int, color_principal, score_final,
                            clasificacion: str, n_vecinos: int, sim_ponderada,
                            filtro_aplicado, metodo_score,
                            modelo_activo: str | None = None, dominio: str | None = None) -> None:
    """El score de UN color. `candidato_score` (la fila de la unidad de compra)
    no se toca acá: sigue escribiéndose como siempre desde el camino de la
    variante 0.

    `modelo_activo`/`dominio` (plan de mejora 2026-09-22): con qué modelo de
    vectorización (ti/estandar/fashion/dino) y contra qué dominio (tuc/marca)
    se calculó ESTE score -- sin esto, medir a futuro si el ancla de
    similitud sigue calibrada obliga a INFERIR el modelo por qué embeddings
    tiene el lote, en vez de leerlo directo (ver PLAN_MEJORA_ETAPAS.md)."""
    _cl().execute("""
        INSERT INTO candidato_score_variante
            (candidato_id, indice, color_principal, score_final, clasificacion, n_vecinos,
             sim_ponderada, filtro_aplicado, metodo_score, modelo_activo, dominio, fecha_calculo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (candidato_id, indice) DO UPDATE SET
            color_principal=excluded.color_principal, score_final=excluded.score_final,
            clasificacion=excluded.clasificacion, n_vecinos=excluded.n_vecinos,
            sim_ponderada=excluded.sim_ponderada, filtro_aplicado=excluded.filtro_aplicado,
            metodo_score=excluded.metodo_score, modelo_activo=excluded.modelo_activo,
            dominio=excluded.dominio, fecha_calculo=excluded.fecha_calculo
    """, (candidato_id, indice, color_principal, score_final, clasificacion, n_vecinos,
          sim_ponderada, filtro_aplicado, metodo_score, modelo_activo, dominio, almacen.ahora()))


def _limpiar_scores_variante(candidato_ids) -> None:
    """Borra los scores por color de una corrida anterior (una referencia pudo
    perder un color), para que nunca queden colores fantasma en la tarjeta."""
    if not candidato_ids:
        return
    _cl().execute(
        f"DELETE FROM candidato_score_variante WHERE candidato_id IN {_en(candidato_ids)}",
        list(candidato_ids))


def promedio_simple_variantes(scores) -> float | None:
    """La calificación de la REFERENCIA COMPLETA: promedio SIMPLE de sus
    colores. Decisión explícita del dueño (2026-09-17), elegida sobre la
    alternativa de ponderar por confianza/evidencia: como el bulto se compra
    entero, cada color pesa igual. Los colores sin score (sin comparables o sin
    vectorizar) no entran al promedio -- no valen 0, simplemente no se saben."""
    validos = [float(s) for s in scores if s is not None]
    if not validos:
        return None
    return round(sum(validos) / len(validos), 4)


def clasificar_score(score) -> str | None:
    """La letra S/A/B/C/D de un score, con los MISMOS cortes que usa el
    cálculo por variante -- para no tener dos escalas conviviendo cuando la
    referencia completa se califica con el promedio de sus colores."""
    if score is None:
        return None
    score = float(score)
    return ("S" if score >= CORTES_CLASIFICACION["S"] else
            "A" if score >= CORTES_CLASIFICACION["A"] else
            "B" if score >= CORTES_CLASIFICACION["B"] else
            "C" if score >= CORTES_CLASIFICACION["C"] else "D")


# Género crudo de `dim_tcm_producto` -> el MISMO vocabulario canónico que usa
# el candidato (`cfg.lkp_tuc_genero`: caballero/dama/infantil/junior/unisex).
# `cfg.lkp_tuc_genero` solo mapea las 6 variantes que aparecen en
# `dim_tuc_producto` ("Hombre", "Mujer", "Niñas"...) y NO cubre las de marca
# ("MUJER", "HOMBRE", "NINAS", "BEBES", "_SIN GENERO"), así que el mapeo que
# falta va acá, en el código, aplicado sobre el texto ya normalizado.
# `silver.lkp_referencia_modelo.genero` (la otra fuente de género del canal
# marca) ya viene canónica, salvo "preschool"/"infant", que son infantil.
MAPA_GENERO_MARCA = {
    "mujer": "dama", "dama": "dama", "damas": "dama",
    "hombre": "caballero", "caballero": "caballero", "caballeros": "caballero",
    "ninas": "infantil", "ninos": "infantil", "nina": "infantil",
    "nino": "infantil", "bebes": "infantil", "bebe": "infantil",
    "infantil": "infantil", "preschool": "infantil", "infant": "infantil",
    "juvenil": "junior", "junior": "junior",
    "unisex": "unisex",
    # Sin género utilizable: se deja en None a propósito (mejor "no sé" que
    # una etiqueta inventada que después filtra mal).
    "_sin genero": None, "sin genero": None, "hogar": None,
}

# Categoría del candidato (vocabulario TU Calzado, el que produce
# `categorizar_candidatos`) -> palabras que identifican ese mismo tipo de
# calzado en el vocabulario del canal marca (`atrib_tipo_norm`,
# `dim_modelo_cr.tipo/subtipo`, `dim_tcm_producto.categoria`). Los dos
# vocabularios existen y no hay tabla de equivalencia entre ellos; esta es la
# equivalencia mínima escrita a mano sobre los valores REALES de ambos lados
# (medidos 2026-09-10), no una lista de marcas ni un filtro por marca.
MAPA_CATEGORIA_TUC_A_TIPO_MARCA = {
    "sandalias": ("sandalia", "slide", "chancla"),
    "sandalias bajas": ("sandalia", "slide", "chancla"),
    "casual": ("casual", "lifestyle", "skate"),
    "cunas y plataformas": ("cuna", "plataforma", "tacon", "wedge"),
    "deportivos": ("deportivo", "running", "lifestyle", "entrenamiento",
                    "baloncesto", "basketball", "futbol", "trail", "skate"),
    "botas y botines": ("bota", "botin", "botas_botines", "outdoor", "hiking"),
    "tacones": ("tacon", "formal", "stiletto"),
    "formal": ("formal", "casual", "vestir"),
    "futbol": ("futbol",),
    "gimnasio": ("entrenamiento", "deportivo", "running"),
    "articulos deportivos": ("deportivo", "running", "lifestyle"),
}


def _palabras_tipo_marca(categoria_candidato) -> tuple[str, ...]:
    """Con qué palabras se busca, en el vocabulario del canal marca, el mismo
    tipo de calzado que declara la categoría del candidato.

    Si la categoría no está en el mapa se cae al mismo heurístico que ya usa
    `api_similar_marca` (primera palabra, en singular) en vez de devolver nada:
    una categoría nueva degrada la precisión del filtro, no lo apaga.
    """
    cat = _norm_txt(categoria_candidato)
    if not cat:
        return ()
    palabras = MAPA_CATEGORIA_TUC_A_TIPO_MARCA.get(cat)
    if palabras:
        return palabras
    primera = cat.split()[0]
    return (primera[:-1] if primera.endswith("s") and len(primera) > 4 else primera,)


def _vectores_duales_representativos(candidato_id):
    v_sig = _vector_representativo(candidato_id, "fashion_siglip")
    v_dino = _vector_representativo(candidato_id, "dino_v2")
    return v_sig, v_dino


_prototipos_cache = {}


def get_prototipos(cur, campo, min_ejemplos=10, modelo_activo: str = "estandar"):
    """Centroide de cada valor distinto de `campo` ('categoria' o 'genero_canonico')
    en el catálogo TUC ya vendido. Fase 5 (guía Opus 2026-07-22): reemplaza el voto
    de mayoría sobre K vecinos puntuales -- que era inestable entre corridas del
    MISMO candidato cuando dos vecinos casi empatados entraban/salían del top-k --
    por comparación directa contra un prototipo por clase, determinista por
    construcción (no depende de qué vecino puntual entra a una lista). Clases con
    menos de `min_ejemplos` fotos no generan prototipo (un centroide de 2-3 fotos es
    ruido, no una clase confiable). Cacheado en memoria por (modelo_activo, campo)
    -- protegido por `_lock_inicializacion` igual que los demás cachés globales.

    `modelo_activo="estandar"` (default, usado por GestionTUC/PTY sin cambios):
    centroide DOBLE, fashion_siglip + dino_v2 por separado -- `protos[clase] =
    (ms, md)`, consumido por `categorizar_candidatos`/`puntuar_candidatos` con la
    fusión ALPHA_FUSION_SIGLIP de siempre.

    `modelo_activo="ti"` (exclusivo AsistenteComprasEscritorio): centroide simple
    desde `silver.fct_embedding_ti` -- OJO, pese al nombre de la tabla esto NO
    son los vectores que TI entregó en su JSON: es nuestra RÉPLICA propia del
    método de TI (dinov2_ti), calculada sobre fotos que nosotros mismos
    descargamos -- ya NO se usa acá (ver abajo). `protos[clase] = ms` (un
    solo array, no tupla).

    Migrado 2026-09-07 (plan 2026-09-04, paso 3) a los vectores REALES que TI
    entregó: `silver.fct_embedding_ti_codigo` (llave por `codigo_tuc`,
    poblada desde `silver.vw_ti_vector_vigente` por
    `promover_embedding_ti_codigo.py` -- 9,379 productos, más honesto que la
    réplica aunque similar en cobertura para dominio 'tuc'). La metadata
    (categoría/género) sale directo de `dim_tuc_producto` por `codigo_tuc`,
    sin pasar por `fct_embedding_imagen`/`imagen_id` como antes -- ya no hace
    falta ese puente porque esta tabla nueva ya está llaveada por producto."""
    global _prototipos_cache
    clave_cache = (modelo_activo, campo)
    with _lock_inicializacion:
        if clave_cache in _prototipos_cache:
            return _prototipos_cache[clave_cache]
        columna = "p.categoria" if campo == "categoria" else "g.genero_canonico"

        if modelo_activo == "ti":
            cur.execute(f"""
                SELECT t.vector, {columna}
                FROM silver.fct_embedding_ti_codigo t
                JOIN silver.dim_tuc_producto p ON p.codigo_tuc = t.codigo_tuc
                LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
                WHERE t.modelo = 'dinov2_ti' AND {columna} IS NOT NULL
            """)
            grupos = {}
            for vt, clase in cur.fetchall():
                grupos.setdefault(clase, []).append(vt)
            protos = {}
            for clase, vectores in grupos.items():
                if len(vectores) < min_ejemplos:
                    continue
                Vt = np.array(vectores, dtype=np.float32)
                Vt /= (np.linalg.norm(Vt, axis=1, keepdims=True) + 1e-9)
                mt = Vt.mean(axis=0); mt /= (np.linalg.norm(mt) + 1e-9)
                protos[clase] = mt
            if protos:
                _prototipos_cache[clave_cache] = protos
            return protos

        cur.execute(f"""
            SELECT s.vector, d.vector, {columna}
            FROM silver.fct_embedding_imagen s
            JOIN silver.fct_embedding_imagen d ON d.imagen_id = s.imagen_id AND d.modelo_embedding = 'dino_v2'
            JOIN silver.dim_tuc_producto p ON p.codigo_tuc = s.codigo_tuc
            LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
            WHERE s.modelo_embedding = 'fashion_siglip' AND s.dominio = 'tuc' AND {columna} IS NOT NULL
        """)
        grupos = {}
        for vs, vd, clase in cur.fetchall():
            grupos.setdefault(clase, []).append((vs, vd))
        protos = {}
        for clase, pares in grupos.items():
            if len(pares) < min_ejemplos:
                continue
            Vs = np.array([p[0] for p in pares], dtype=np.float32)
            Vd = np.array([p[1] for p in pares], dtype=np.float32)
            Vs /= (np.linalg.norm(Vs, axis=1, keepdims=True) + 1e-9)
            Vd /= (np.linalg.norm(Vd, axis=1, keepdims=True) + 1e-9)
            ms = Vs.mean(axis=0); ms /= (np.linalg.norm(ms) + 1e-9)
            md = Vd.mean(axis=0); md /= (np.linalg.norm(md) + 1e-9)
            # "fashion"/"dino": el MISMO centroide, pero devuelto como un solo
            # array (no la tupla de la fusión) -- exactamente la forma que
            # espera `_clasificar` para un método de un solo espacio, igual que
            # "ti". No hace falta otra consulta: los dos componentes ya vienen
            # en la misma fila.
            if modelo_activo == "fashion":
                protos[clase] = ms
            elif modelo_activo == "dino":
                protos[clase] = md
            else:
                protos[clase] = (ms, md)
        # Solo cachear si el resultado NO está vacío (auditoría Opus 2026-07-22, hallazgo
        # real): si esta función corre antes de que existan embeddings TUC (BD nueva, o
        # durante un rebuild que trunca y repuebla fct_embedding_imagen), `protos={}`
        # quedaría cacheado PARA SIEMPRE en un servidor de larga duración -- a diferencia
        # del centroide de Fase 2 (que sí marca explícitamente "ya lo intenté, no hay
        # datos" con `_centroide_tuc_intentado`), aquí no había forma de reintentar tras
        # repoblar el índice sin reiniciar el proceso. No cachear vacío permite que la
        # SIGUIENTE subida vuelva a intentar la query una vez que el índice ya tenga datos.
        if protos:
            _prototipos_cache[clave_cache] = protos
        return protos


def categorizar_candidatos(cur, candidato_ids, margen_min=0.04, modelo_activo: str = "estandar"):
    """Clasifica categoria (tipo) Y genero_canonico (via cfg.lkp_tuc_genero, poblada
    2026-07-21) por NEAREST-CENTROID (Fase 5, 2026-07-22) -- reemplaza el voto de
    mayoría sobre K vecinos puntuales (Ronda 4 a 10, k subido de 7->15 buscando
    estabilidad sin resolver la causa real). Determinista por construcción: la clase
    ganadora es la de mayor similitud fusionada contra su PROTOTIPO, no depende de qué
    vecino puntual entra al top-k. `margen_min`: si la diferencia entre la 1ª y 2ª clase
    es menor a esto, se considera AMBIGUO y se deja `None` en vez de forzar una etiqueta
    al azar -- honesto para una decisión de compra ("no sé" es una respuesta válida,
    mismo principio ya aplicado a los comparables de marca en Ronda 9). genero_canonico
    queda en el vocabulario compartido con marca reconocida para usarlo como FILTRO DURO
    en las búsquedas de similitud, no solo como dato informativo.

    `modelo_activo="ti"`: un solo vector (dinov2_ti) contra un prototipo simple, sin
    la fusión ALPHA_FUSION_SIGLIP -- ver `get_prototipos`."""
    protos_cat = get_prototipos(cur, "categoria", modelo_activo=modelo_activo)
    protos_gen = get_prototipos(cur, "genero_canonico", modelo_activo=modelo_activo)
    if not protos_cat and not protos_gen:
        return

    def _clasificar(v, v_dino, protos):
        if not protos:
            return None
        if modelo_activo in MODELO_UNICO:
            # Un solo espacio, sin fusión que ponderar -- similitud directa contra
            # el prototipo (un array, no una tupla, ver `get_prototipos`).
            scores = {nombre: float(p @ v) for nombre, p in protos.items()}
        else:
            # Peso efectivo usado (auditoría Opus 2026-07-22, hallazgo real): si v_dino
            # es None, el score fusionado solo lleva el término SigLIP -- sin
            # renormalizar, el margen resultante vive en una escala más chica (ej. la
            # mitad, con ALPHA_FUSION_SIGLIP=0.5) que `margen_min` (calibrado para la
            # fusión COMPLETA), sesgando sistemáticamente hacia "ambiguo" a cualquier
            # candidato sin embedding DINOv2 aunque el poder discriminante real sea el
            # mismo. Se divide por el peso efectivo para que el margen quede en la
            # misma escala en ambos casos.
            peso_efectivo = ALPHA_FUSION_SIGLIP if v_dino is None else 1.0
            scores = {}
            for nombre, (ps, pd) in protos.items():
                s = ALPHA_FUSION_SIGLIP * float(ps @ v)
                if v_dino is not None:
                    s += (1 - ALPHA_FUSION_SIGLIP) * float(pd @ v_dino)
                scores[nombre] = s / peso_efectivo
        orden = sorted(scores.items(), key=lambda x: -x[1])
        if len(orden) >= 2 and (orden[0][1] - orden[1][1]) < margen_min:
            return None  # ambiguo -- 1ª y 2ª clase casi empatadas, no forzar etiqueta
        return orden[0][0]

    for candidato_id in candidato_ids:
        if modelo_activo in MODELO_UNICO:
            v = _vector_representativo(candidato_id, MODELO_UNICO[modelo_activo])
            v_dino = None
        else:
            v, v_dino = _vectores_duales_representativos(candidato_id)
        if v is None:
            continue
        cat_final = _clasificar(v, v_dino, protos_cat)
        gen_final = _clasificar(v, v_dino, protos_gen)
        # Plan 2026-09-07, paso 2.5: antes, si el proveedor ya había declarado
        # categoría, el resultado visual se descartaba por completo (el
        # COALESCE de abajo no lo tocaba) -- no quedaba ningún registro de si
        # el clasificador visual coincidía o no con lo declarado. Eso deja
        # abierto un vector de gaming: declarar una categoría de alta
        # población (ej. "Deportivos" en una sandalia) sin que nada lo
        # contradiga. Ahora `categoria_visual` SIEMPRE se guarda (aunque haya
        # declarada), y `categoria_conflicto` compara contra el valor de
        # categoria_declarada QUE YA EXISTÍA antes de este UPDATE (Postgres
        # evalúa el lado derecho del SET contra la fila vieja, no contra el
        # COALESCE de la misma sentencia) -- puntuar_candidatos penaliza
        # f_tipo cuando hay conflicto, sin importar qué diga filtro_aplicado.
        # FASE 2: el UPDATE va al `lote.sqlite`. La sutileza que hacía correcto
        # a este UPDATE se conserva tal cual: SQLite, igual que Postgres, evalúa
        # el lado derecho de cada SET contra la fila VIEJA, así que
        # `categoria_conflicto` sigue comparando contra la categoría declarada
        # que existía ANTES del COALESCE de la misma sentencia -- no contra el
        # valor que esta misma línea está escribiendo.
        _cl().execute("""
            UPDATE candidato
            SET categoria_visual = ?,
                categoria_conflicto = (categoria_declarada IS NOT NULL AND ? IS NOT NULL
                                        AND categoria_declarada <> ?),
                categoria_declarada = COALESCE(categoria_declarada, ?),
                genero_canonico = COALESCE(genero_canonico, ?)
            WHERE candidato_id = ?
        """, (cat_final, cat_final, cat_final, cat_final, gen_final, candidato_id))


# Paso 8 del plan 2026-09-04 (respaldo por precio cuando no hay comparables
# visuales, a pedido explícito del usuario -- "todos los productos deberían
# tener un score, deben ser similares a algo, incluso por precio"). Margen
# real de TU Calzado, mismo documentado en AsistenteComprasTEC/CLAUDE.md
# ("Margen de venta sobre costo+IVA | ×2.6105 por defecto") -- no es una
# regla propia de TEC, es la política de margen de la empresa.
MARGEN_VENTA_DEFAULT = 2.6105
IVA_CR = 1.13
TOLERANCIA_PRECIO = 0.30  # medido 2026-09-07 sobre silver.dim_tuc_producto: el
# rango intercuartil real por categoría (ej. Deportivos p25=5,248 / mediana=7,425
# / p75=10,553) ronda +/-30% de la mediana -- punto de partida medido, no
# calibrado contra casos etiquetados a mano (misma salvedad que UMBRAL_POR_MODELO).


TIPO_CAMBIO_USD_CRC = 505.0  # referencia manual (BCCR, 2026-09-07) -- NO se
# consulta en vivo. Actualizar este número si el tipo de cambio real se
# aleja bastante; un desfase de pocos colones no cambia la categoría de
# tolerancia (TOLERANCIA_PRECIO=30%), pero uno de varios meses sí podría.


def _precio_venta_estimado(costo, moneda: str | None = "CRC") -> float | None:
    """costo del proveedor -> precio de venta esperado, misma fórmula que ya
    usa TU Calzado en AsistenteComprasTEC (costo × IVA × margen). None si no
    hay costo declarado -- sin esto no hay con qué comparar precios.

    `moneda` viene de raw_catalogo.moneda_costo (plan 2026-09-07, paso 8):
    "USD" convierte primero a colones con TIPO_CAMBIO_USD_CRC; "CRC" no
    convierte. Si moneda es None (no se pudo inferir del Excel del proveedor
    en qué moneda cotiza) esta función se ABSTIENE y devuelve None -- asumir
    CRC por defecto daría un precio de venta sin sentido para un proveedor
    que cotiza en USD (confirmado con el usuario: la moneda varía según el
    proveedor, no siempre es colones)."""
    if costo is None or costo <= 0:
        return None
    if moneda is None:
        return None
    costo_crc = float(costo) * TIPO_CAMBIO_USD_CRC if moneda == "USD" else float(costo)
    return costo_crc * IVA_CR * MARGEN_VENTA_DEFAULT


# Plan 2026-09-07, paso 4: descuento típico entre marca reconocida y lo que
# TU Calzado efectivamente cobra por TUCALZADO -- medido, no inventado, sobre
# TODO el histórico real: silver.fct_tuc_ventas.precio_avg_vta promedia
# ₡7,572 (28,211 filas) contra silver.fct_oferta_competidor.precio promedio
# ₡39,468 (103,080 filas) de marca reconocida. Es un promedio GLOBAL, no por
# categoría -- punto de partida medido, no una calibración validada por
# categoría (una sandalia y una bota probablemente no comparten el mismo
# descuento real).
DESCUENTO_MARCA_A_TUC = round(7572.40 / 39467.62, 3)  # 0.192


def precio_venta_referencia(cur, candidato_id: int) -> tuple[float | None, str | None]:
    """Precio de venta esperado para un candidato, en cascada de fuentes --
    plan 2026-09-07, paso 4. Antes `api_pedido_sugerido` solo usaba el costo
    declarado por el proveedor (raw_catalogo.costo), que en la práctica casi
    nunca viene (0 de 74 candidatos reales en la prueba del catálogo Reebok)
    -- eso dejaba el Optimizador de Compra funcionalmente muerto. La cascada:

    1. precio_venta_manual (el comprador lo escribió a mano en la tabla
       `candidato` del lote -- paso 5, ver `api_fijar_precio_manual`) --
       gana siempre porque es una decisión explícita del comprador, no una
       estimación. Se lee de la columna, no se recibe por parámetro, para que
       CUALQUIER llamador (pedido sugerido, tarjeta de candidato, etc.) vea
       siempre el mismo precio sin tener que acordarse de pasarlo.
    2. costo declarado por el proveedor × IVA × margen (`_precio_venta_estimado`)
       -- el proveedor mismo dio el dato, es la fuente más directa cuando existe.
    3. promedio ponderado de precio_avg_vta de los vecinos TUC que ya
       determinaron el score (`vecinos_detalle`, del mismo candidato) -- el
       precio al que ESTA empresa efectivamente vende productos parecidos.
       Medido: 71/74 candidatos reales del catálogo Reebok tenían este dato
       disponible (96%), promedio ₡12,775 -- es la fuente que en la práctica
       cubre casi todo.
    4. promedio de comparables de marca reconocida (`api_similar_marca`),
       ajustado por `DESCUENTO_MARCA_A_TUC` -- una marca reconocida cuesta
       mucho más que TUCALZADO; usar su precio sin ajustar sobreestimaría
       groseramente.
    5. mediana de `precio_mediano` de `dim_tuc_producto` por categoría --
       último recurso, ni siquiera mira los vecinos del candidato, solo "qué
       vende una categoría entera en promedio".

    Devuelve (precio, fuente) -- fuente es uno de "manual"/"costo_proveedor"/
    "similares_tuc"/"comparables_marca"/"mediana_categoria", o (None, None)
    si ningún nivel de la cascada tuvo con qué calcular nada.

    FASE 2: los tres primeros niveles de la cascada (precio manual, costo
    declarado, y los vecinos que ya determinaron el score) salen ahora del
    `lote.sqlite`. Los dos últimos (comparables de marca, mediana de
    categoría) siguen consultando Postgres, porque son precios REALES del
    mercado y del catálogo histórico de la empresa. `cur` sigue en la firma y
    sigue siendo el cursor de Postgres: se usa para esos dos niveles y para
    los precios de venta reales de los vecinos."""
    cl = _cl()
    cl.execute("SELECT precio_venta_manual FROM candidato WHERE candidato_id = ?", (candidato_id,))
    r_manual = cl.fetchone()
    if r_manual and r_manual[0] and r_manual[0] > 0:
        return round(float(r_manual[0]), 2), "manual"

    cl.execute("""
        SELECT r.costo, r.moneda_costo FROM candidato c
        JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
           AND r.catalogo_origen = c.catalogo_origen
        WHERE c.candidato_id = ?
    """, (candidato_id,))
    fila_costo = cl.fetchone()
    precio_costo = _precio_venta_estimado(*tuple(fila_costo)) if fila_costo else None
    if precio_costo:
        return round(precio_costo, 2), "costo_proveedor"

    cl.execute("""
        SELECT s.vecinos_detalle, d.categoria_declarada, d.genero_canonico
        FROM candidato_score s
        JOIN candidato d ON d.candidato_id = s.candidato_id
        WHERE s.candidato_id = ?
    """, (candidato_id,))
    fila = cl.fetchone()
    vecinos_detalle, cat_c, gen_c = tuple(fila) if fila else (None, None, None)
    # `vecinos_detalle` era `jsonb` (ya deserializado por psycopg2) y acá es TEXT.
    vecinos_detalle = json.loads(vecinos_detalle) if vecinos_detalle else None

    # Canal TC Marcas: los vecinos que determinaron el score son referencias de
    # marca, no códigos TUC -- su precio de venta real sale de
    # `silver.fct_tcm_ventas` (`precio_rv`), la contraparte exacta del
    # `precio_avg_vta` de `fct_tuc_ventas` que usa el canal genérico.
    vecinos_marca = [v for v in (vecinos_detalle or [])
                     if v.get("dominio") == "marca" and v.get("referencia")]
    if vecinos_marca:
        pesos_por_ref = {v["referencia"]: v["peso"] for v in vecinos_marca}
        cur.execute("""
            SELECT referencia, AVG(precio_rv) FROM silver.fct_tcm_ventas
            WHERE referencia = ANY(%s) AND precio_rv IS NOT NULL
            GROUP BY referencia
        """, (list(pesos_por_ref),))
        precios_reales = cur.fetchall()
        num = sum(pesos_por_ref[ref] * float(p) for ref, p in precios_reales)
        den = sum(pesos_por_ref[ref] for ref, _p in precios_reales)
        if den > 0:
            return round(num / den, 2), "similares_marca_vendidos"

    if vecinos_detalle and not vecinos_marca:
        codigos = [v["codigo_tuc"] for v in vecinos_detalle]
        pesos_por_codigo = {v["codigo_tuc"]: v["peso"] for v in vecinos_detalle}
        cur.execute("""
            SELECT codigo_tuc, AVG(precio_avg_vta) FROM silver.fct_tuc_ventas
            WHERE codigo_tuc = ANY(%s) AND precio_avg_vta IS NOT NULL
            GROUP BY codigo_tuc
        """, (codigos,))
        precios_reales = cur.fetchall()
        num = sum(pesos_por_codigo[cod] * float(p) for cod, p in precios_reales)
        den = sum(pesos_por_codigo[cod] for cod, _p in precios_reales)
        if den > 0:
            return round(num / den, 2), "similares_tuc"

    if cat_c:
        similares_marca = api_similar_marca(candidato_id, k=6, modelo_activo="ti")
        precios_marca = [s["precio"] for s in similares_marca if s.get("precio")]
        if precios_marca:
            precio_marca_prom = sum(precios_marca) / len(precios_marca)
            # El descuento marca->genérico SOLO aplica si lo que se está
            # comprando se va a vender como genérico. Si la compra es para TC
            # Marcas (canal declarado del lote), el precio de una marca
            # reconocida es el precio que corresponde, sin castigo.
            ajuste = 1.0 if canal_venta_lote() == "marca" else DESCUENTO_MARCA_A_TUC
            return round(precio_marca_prom * ajuste, 2), "comparables_marca"

    if cat_c:
        cur.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY precio_mediano)
            FROM silver.dim_tuc_producto WHERE categoria = %s AND precio_mediano IS NOT NULL
        """, (cat_c,))
        r = cur.fetchone()
        if r and r[0]:
            return round(float(r[0]), 2), "mediana_categoria"

    return None, None


_LOTE_INDICE = 2000  # filas por lote al leer el índice de comparables -- ver _consumir_por_lotes


def _consumir_por_lotes(cur, query, params=None, tam_lote=_LOTE_INDICE):
    """Ejecuta `query` con un cursor de SERVIDOR (nombrado) sobre la MISMA
    conexión que `cur`, leyendo de a `tam_lote` filas -- plan de mejora
    2026-09-22, Etapa 5.

    Por qué: un cursor NORMAL de psycopg2 buferea el resultado COMPLETO en el
    cliente ni bien se llama `execute()` -- `fetchall()`/`fetchmany()` después
    de eso no cambian el pico de memoria, solo cómo se retira lo que ya está
    todo cargado. Medido con tracemalloc real (índice tuc completo, 9,795
    filas, vectores de 768 floats): cursor normal = 617 MB de pico. Cursor de
    SERVIDOR (streaming real desde Postgres) + convertir cada lote a numpy
    ANTES de pedir el siguiente (sin acumular listas Python de todos los
    lotes) = 258 MB (-58%). Ninguna de las dos reduce el tamaño del RESULTADO
    final (69 MB de arreglos numpy) -- lo que baja es el excedente de listas
    Python intermedias que existían solo por cómo se leía.

    Requiere que la conexión NO esté en autocommit (confirmado: no lo está
    en ningún punto de este módulo) -- un cursor de servidor con autocommit
    activado no transmite en lotes de verdad, psycopg2 lo trae todo igual.

    Es un generador: cede listas de filas crudas (tuplas), una lista por
    lote. El llamador arma sus columnas np.array POR LOTE y concatena al
    final -- si en cambio junta todas las listas primero y arma un solo
    np.array al final, pierde el beneficio (vuelve a acumular todo antes de
    convertir)."""
    nombre = f"_cursor_indice_{id(cur)}_{id(query)}"
    cur_srv = cur.connection.cursor(name=nombre)
    cur_srv.itersize = tam_lote
    cur_srv.execute(query, params)
    try:
        while True:
            lote = cur_srv.fetchmany(tam_lote)
            if not lote:
                break
            yield lote
    finally:
        cur_srv.close()


def _indice_marca_para_score(cur, modelo_activo: str):
    """El índice del canal TC Marcas contra el que se puntúa: TODAS las fotos
    del dominio 'marca', sin restringir por marca.

    Decisión explícita del dueño (2026-09-10): NO se limita a las marcas mejor
    cubiertas. Las 8,436 fotos del índice entran; el que una marca tenga poca
    evidencia lo dice después `f_soporte` (y `f_demanda`, que queda neutro
    cuando no hay ventas propias con las que medir), no una lista blanca.

    Devuelve None si el índice está vacío. Los arreglos que devuelve tienen el
    mismo rol que los del canal genérico en `puntuar_candidatos`:
      vectores / vectores_dino, velocidades, tendencias (NaN = sin dato),
      generos (canónico), tipos (texto normalizado para buscar palabras),
      marcas, referencias, precios (NaN = sin dato), colores, materiales.
    """
    if modelo_activo == "ti":
        query = SQL_CTE_VELOCIDAD_MARCA + """
            SELECT v.vector, NULL::real[],
                   -- `marca_tec` viene vacía en la mayoría de los productos de
                   -- nuestro propio catálogo TC Marcas; la marca real está al
                   -- principio de la descripción ("SKECHERS NEGRO HOMBRE"), así
                   -- que se lleva la descripción entera como etiqueta legible en
                   -- vez de inventarle una marca.
                   COALESCE(p.marca_tec, p.descripcion) AS marca, p.referencia, p.genero, v.codigo,
                   COALESCE(p.atrib_tipo_norm, '') || ' ' || COALESCE(p.tipo_tec, '') || ' ' ||
                   COALESCE(p.subtipo_tec, '') || ' ' || COALESCE(p.categoria, '') AS tipo_txt,
                   p.atrib_material_norm, p.atrib_color_familia, p.precio_pub_tec,
                   vm.velocidad_mensual, vm.tendencia_3m, ven.pct_rotacion, prm.pct_sobre_lista,
                   ven.factor_venta
            FROM silver.vw_ti_vector_vigente v
            JOIN silver.dim_tcm_producto p ON p.codigo_tc = v.codigo
            LEFT JOIN vel_marca vm ON vm.referencia = p.referencia
            LEFT JOIN venta_marca ven ON ven.referencia = p.referencia
            LEFT JOIN precio_marca prm ON prm.referencia = p.referencia
            WHERE v.dominio = 'marca' AND v.modelo = 'dinov2_vitb14'
        """
    else:
        query = SQL_CTE_VELOCIDAD_MARCA + """
            SELECT s.vector, d.vector, COALESCE(s.marca, p.descripcion) AS marca, s.referencia,
                   COALESCE(lrm.genero, p.genero) AS genero, p.codigo_tc,
                   COALESCE(dm.tipo, '') || ' ' || COALESCE(dm.subtipo, '') || ' ' ||
                   COALESCE(p.atrib_tipo_norm, '') || ' ' || COALESCE(p.categoria, '') AS tipo_txt,
                   p.atrib_material_norm, p.atrib_color_familia, p.precio_pub_tec,
                   vm.velocidad_mensual, vm.tendencia_3m, ven.pct_rotacion, prm.pct_sobre_lista,
                   ven.factor_venta
            FROM silver.fct_embedding_imagen s
            JOIN silver.fct_embedding_imagen d
                ON d.imagen_id = s.imagen_id AND d.modelo_embedding = 'dino_v2'
            LEFT JOIN silver.lkp_referencia_modelo lrm
                ON lrm.marca = s.marca AND lrm.referencia = s.referencia
            LEFT JOIN silver.dim_modelo_cr dm
                ON dm.marca = lrm.marca AND dm.codigo_modelo = lrm.codigo_modelo
            LEFT JOIN silver.dim_tcm_producto p ON p.referencia = s.referencia
            LEFT JOIN vel_marca vm ON vm.referencia = s.referencia
            LEFT JOIN venta_marca ven ON ven.referencia = s.referencia
            LEFT JOIN precio_marca prm ON prm.referencia = s.referencia
            WHERE s.modelo_embedding = 'fashion_siglip' AND s.dominio = 'marca'
        """
    def _num(valor):
        return float(valor) if valor is not None else np.nan

    # "dino": el índice que se consulta es el de dino_v2, no el de
    # fashion_siglip -- la consulta de arriba ya trae los dos vectores por
    # fila, así que solo hay que quedarse con la columna del modelo elegido.
    # "fashion" ya usa f[0] (que ES fashion_siglip) sin cambio.
    columna_vector = 1 if modelo_activo == "dino" else 0
    necesita_dino = modelo_activo not in MODELO_UNICO

    # Cursor servidor + numpy por lote -- ver `_consumir_por_lotes`: acumular
    # las columnas ligeras como listas Python y convertir SOLO los vectores
    # (lo pesado) a numpy lote por lote evita duplicar el resultado completo
    # en memoria del cliente (medido: 617MB -> 258MB, plan de mejora
    # 2026-09-22 Etapa 5).
    resto, trozos_vec, trozos_dino = [], [], []
    for lote in _consumir_por_lotes(cur, query):
        trozos_vec.append(np.array([f[columna_vector] for f in lote], dtype=np.float32))
        if necesita_dino:
            trozos_dino.append(np.array([f[1] for f in lote], dtype=np.float32))
        # `resto` guarda SOLO metadata (f[2:]), nunca los vectores -- si
        # guardara la fila completa, el vector quedaría duplicado (lista
        # Python cruda + numpy de `trozos_vec`), anulando el ahorro de
        # memoria que motiva este cambio.
        resto.extend(f[2:] for f in lote)
    if not resto:
        return None
    filas = resto

    vectores = np.concatenate(trozos_vec, axis=0)
    normas = np.linalg.norm(vectores, axis=1, keepdims=True); normas[normas == 0] = 1.0
    vectores /= normas
    if not necesita_dino:
        vectores_dino = None
    else:
        vectores_dino = np.concatenate(trozos_dino, axis=0)
        normas_d = np.linalg.norm(vectores_dino, axis=1, keepdims=True); normas_d[normas_d == 0] = 1.0
        vectores_dino /= normas_d

    return {
        "vectores": vectores,
        "vectores_dino": vectores_dino,
        "marcas": np.array([f[0] for f in filas], dtype=object),
        "referencias": np.array([f[1] for f in filas], dtype=object),
        # Género al vocabulario canónico del candidato (ver MAPA_GENERO_MARCA):
        # sin esto el filtro compararía "MUJER" contra "dama" y no calzaría
        # nunca, degradando en silencio a "sin filtro de género".
        "generos": np.array([MAPA_GENERO_MARCA.get(_norm_txt(f[2]), _norm_txt(f[2]) or None)
                              for f in filas], dtype=object),
        # Código interno de TU Calzado del comparable, cuando existe: es lo
        # único de un comparable de marca que se puede buscar en nuestros
        # sistemas (la referencia del fabricante no sirve para eso).
        "codigos_tc": np.array([f[3] for f in filas], dtype=object),
        "tipos": np.array([_norm_txt(f[4]) for f in filas], dtype=object),
        "materiales": np.array([f[5] for f in filas], dtype=object),
        "colores": np.array([f[6] for f in filas], dtype=object),
        "precios": np.array([_num(f[7]) for f in filas]),
        # NaN (no 0) cuando la referencia no tiene ventas propias: "no hay
        # registro" no es "vendió cero", y confundirlos hundiría el puntaje de
        # todo comparable de mercado que nunca compramos.
        "velocidades": np.array([_num(f[8]) for f in filas]),
        "tendencias": np.array([_num(f[9]) for f in filas]),
        # Los nombres de llave ("rot30"/"pct_sin_promo") son históricos --
        # desde el plan de mejora 2026-09-22 el dato real es `pct_rotacion` y
        # `pct_sobre_lista` de Power BI (ver REFERENCIA_ROT30_MARCA /
        # REFERENCIA_PCT_SIN_PROMO_MARCA), no se renombran para no tocar más
        # líneas de las necesarias. A diferencia de velocidades/tendencias
        # (donde "sin dato" NO es "vendió cero"), acá se imputa la mediana
        # real cuando falta -- mismo criterio que rotaciones/factores_venta
        # del canal genérico en `puntuar_candidatos`: ese vecino queda
        # neutro, no penaliza ni infla el proxy.
        "rot30": np.array([float(f[10]) if f[10] is not None else REFERENCIA_ROT30_MARCA
                            for f in filas]),
        "pct_sin_promo": np.array([float(f[11]) if f[11] is not None else REFERENCIA_PCT_SIN_PROMO_MARCA
                                    for f in filas]),
        "factor_venta": np.array([float(f[12]) if f[12] is not None else REFERENCIA_FACTOR_VENTA_MARCA
                                   for f in filas]),
    }


def _guardar_score_candidato(cl, candidato_id, *, demanda_proxy, tendencia_proxy,
                              margen_factor, n_vecinos, score_final, clasificacion,
                              vecinos_detalle, factor_mercado, filtro_aplicado,
                              sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color,
                              f_atrib, f_demanda, f_rotacion, rotacion_proxy,
                              f_venta, venta_proxy,
                              f_descuento, descuento_proxy, version_formula,
                              modelo_activo: str | None = None, dominio: str | None = None):
    """El UPSERT de `candidato_score`, para el canal marca.

    Es el MISMO INSERT que escribe el canal genérico más abajo, escrito una
    vez acá para no duplicarlo. El camino del canal genérico se dejó tal cual
    a propósito (requisito de la tarea: su resultado no puede cambiar en nada),
    así que sí, la sentencia aparece dos veces en el archivo -- es deliberado.
    `f_venta`/`venta_proxy` usan `silver.fct_tcm_rotacion` (mismo reporte
    Power BI de rotación que TUCALZADO, filtrado a Catalogo=TCMARCAS).

    `modelo_activo`/`dominio`: ver nota en `_guardar_score_variante` -- misma
    razón, distinta tabla (plan de mejora 2026-09-22).
    """
    cl.execute("""
        INSERT INTO candidato_score
            (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos,
             score_final, clasificacion, vecinos_detalle, factor_mercado, filtro_aplicado,
             sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color, f_atrib, f_demanda,
             f_rotacion, rotacion_proxy, f_venta, venta_proxy, f_descuento, descuento_proxy,
             version_formula, modelo_activo, dominio, fecha_calculo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (candidato_id) DO UPDATE SET
            demanda_proxy=excluded.demanda_proxy, tendencia_proxy=excluded.tendencia_proxy,
            margen_factor=excluded.margen_factor, n_vecinos=excluded.n_vecinos,
            score_final=excluded.score_final, clasificacion=excluded.clasificacion,
            vecinos_detalle=excluded.vecinos_detalle, factor_mercado=excluded.factor_mercado,
            filtro_aplicado=excluded.filtro_aplicado,
            sim_ponderada=excluded.sim_ponderada, k_efectivo=excluded.k_efectivo,
            f_soporte=excluded.f_soporte, f_tipo=excluded.f_tipo, f_color=excluded.f_color,
            f_atrib=excluded.f_atrib, f_demanda=excluded.f_demanda,
            f_rotacion=excluded.f_rotacion, rotacion_proxy=excluded.rotacion_proxy,
            f_venta=excluded.f_venta, venta_proxy=excluded.venta_proxy,
            f_descuento=excluded.f_descuento, descuento_proxy=excluded.descuento_proxy,
            version_formula=excluded.version_formula, modelo_activo=excluded.modelo_activo,
            dominio=excluded.dominio, fecha_calculo=excluded.fecha_calculo
    """, (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos,
          score_final, clasificacion, vecinos_detalle, factor_mercado, filtro_aplicado,
          sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color, f_atrib, f_demanda,
          f_rotacion, rotacion_proxy, f_venta, venta_proxy, f_descuento, descuento_proxy,
          version_formula, modelo_activo, dominio, almacen.ahora()))


def _puntuar_candidatos_marca_impl(cur, candidato_ids, k=7, modelo_activo: str = "estandar"):
    """Puntaje del canal TC MARCAS: los mismos 8 factores que el canal
    genérico (`score_base · f_soporte · f_tipo · f_color · f_atrib · f_demanda
    · margen_factor · factor_mercado`), pero medidos contra lo que se ha
    vendido/ofrecido en marca reconocida (dominio 'marca'), nunca contra el
    catálogo genérico.

    OJO con las dos formas del índice de marca (medidas 2026-09-10):

      modelo_activo="ti" (el default de esta app): 3,570 productos de NUESTRO
        catálogo TC Marcas (`silver.vw_ti_vector_vigente` × `dim_tcm_producto`
        por `codigo_tc`). Los 3,570 tienen género, tipo, código interno Y
        velocidad de venta propia -- acá f_demanda mide de verdad.
      modelo_activo="estandar": 8,436 fotos (`fct_embedding_imagen`,
        dominio 'marca'), que incluyen producto de mercado que nunca
        compramos. Género 5,669, tipo 5,296, material 386, y solo 513 con
        ventas propias: ahí f_demanda queda neutro en la mayoría de los casos.

    Qué queda COMPLETO y qué queda NEUTRO en este canal:

      score_base  COMPLETO -- comparación vectorial contra el índice ENTERO,
                  sin restricción por marca.
      f_soporte   COMPLETO -- idéntico mecanismo (k_efectivo). Es el que dice
                  la verdad cuando una marca tiene poca evidencia.
      f_tipo      COMPLETO -- género canonizado (MAPA_GENERO_MARCA) + tipo por
                  palabras (MAPA_CATEGORIA_TUC_A_TIPO_MARCA); ver arriba la
                  cobertura de cada índice.
      f_color     NEUTRO SIEMPRE (1.00) -- `cfg.prevalencia_color_categoria`
                  solo tiene prevalencias de las categorías del catálogo
                  genérico; no existe la tabla equivalente para las categorías
                  de marca. Construirla es trabajo aparte. No se inventa un
                  lift con una prevalencia que no corresponde.
      f_atrib     PARCIAL -- `atrib_material_norm`. En el índice "ti" mide
                  cuando el material está poblado; en el "estandar" existe
                  solo para 386 de 8,436 filas, así que ahí queda neutro casi
                  siempre. Neutro con menos de 2 vecinos con material.
      f_demanda   COMPLETO en el índice "ti" (los 3,570 productos son de
                  nuestro catálogo y tienen velocidad real), PARCIAL en el
                  "estandar". Velocidad mensual desde `silver.fct_tcm_ventas`
                  con referencia propia del canal (REFERENCIA_DEMANDA_MARCA =
                  6.30 u/mes, NO el 17.4 del genérico). Si el top-k trae menos
                  de MIN_VECINOS_DEMANDA_MARCA vecinos con dato, queda en 1.00
                  en vez de tratar "sin registro de venta" como "vendió cero".
      margen_factor / factor_mercado  sin cambios (el mercado ya funciona para
                  marca vía `gold.vw_senal_marca`).
    """
    idx = _indice_marca_para_score(cur, modelo_activo)
    if idx is None:
        return
    n = len(idx["referencias"])

    # Un puntaje por COLOR, igual que en el canal genérico (ver
    # `variantes_a_calificar`): el bulto del proveedor tampoco se puede partir
    # por color en este canal.
    _limpiar_scores_variante(candidato_ids)
    tareas = []
    for candidato_id_t in candidato_ids:
        calificables, sin_vector = variantes_a_calificar(candidato_id_t, modelo_activo)
        for indice_t, color_t, v_t, v_dino_t in calificables:
            tareas.append((candidato_id_t, indice_t, color_t, v_t, v_dino_t))
        for indice_t, color_t in sin_vector:
            _guardar_score_variante(candidato_id_t, indice_t, color_t, None,
                                    "sin_vector", 0, None, None, None,
                                    modelo_activo=modelo_activo, dominio="marca")

    for candidato_id, indice_variante, color_variante, v, v_dino in tareas:
        cl = _cl()
        cl.execute("""
            SELECT categoria_declarada, genero_canonico, categoria_conflicto
            FROM candidato WHERE candidato_id = ?
        """, (candidato_id,))
        cat_c, gen_c, categoria_conflicto = tuple(cl.fetchone())
        categoria_conflicto = almacen.booleano(categoria_conflicto)

        # Filtro duro: género canónico y tipo por palabras. Mismo criterio de
        # degradación que el canal genérico -- si el filtro queda vacío se
        # afloja y `f_tipo` lo castiga, en vez de dejar el candidato sin score.
        mascara = np.ones(n, dtype=bool)
        usa_genero = False
        if gen_c:
            m_gen = mascara & (idx["generos"] == gen_c)
            if m_gen.any():
                mascara, usa_genero = m_gen, True

        usa_tipo = False
        palabras = _palabras_tipo_marca(cat_c)
        if palabras:
            m_tipo = mascara & np.array(
                [any(p in t for p in palabras) for t in idx["tipos"]], dtype=bool)
            if m_tipo.any():
                mascara, usa_tipo = m_tipo, True

        filtro_aplicado = ("categoria+genero" if (usa_tipo and usa_genero) else
                            "solo_categoria" if usa_tipo else
                            "solo_genero" if usa_genero else "ninguno")

        if modelo_activo in MODELO_UNICO:
            sims = idx["vectores"] @ v
        else:
            sims = ALPHA_FUSION_SIGLIP * (idx["vectores"] @ v)
            if v_dino is not None:
                sims = sims + (1 - ALPHA_FUSION_SIGLIP) * (idx["vectores_dino"] @ v_dino)
        sims = np.where(mascara, sims, -np.inf)

        metodo_score = "visual"
        # Umbral del DOMINIO MARCA, no el de tuc: ver `UMBRAL_POR_MODELO_MARCA`.
        umbral_dominio = umbral_comparable(modelo_activo, "marca")
        mascara_comparable = mascara & (sims >= umbral_dominio)

        if not mascara_comparable.any():
            # Mismo respaldo por PRECIO que el canal genérico, con el precio
            # de publicación del canal marca (`precio_pub_tec`).
            cl.execute("""
                SELECT r.costo, r.moneda_costo FROM candidato c
                JOIN candidato_raw r
                    ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
                   AND r.catalogo_origen = c.catalogo_origen
                WHERE c.candidato_id = ?
            """, (candidato_id,))
            fila_costo = cl.fetchone()
            precio_candidato = _precio_venta_estimado(*tuple(fila_costo)) if fila_costo else None
            if precio_candidato:
                precios_validos = ~np.isnan(idx["precios"]) & mascara
                if precios_validos.any():
                    dif_rel = np.abs(idx["precios"] - precio_candidato) / precio_candidato
                    mascara_precio = precios_validos & (dif_rel <= TOLERANCIA_PRECIO)
                    if mascara_precio.any():
                        sims = np.where(mascara_precio, 1 - (dif_rel / TOLERANCIA_PRECIO), -np.inf)
                        mascara_comparable = mascara_precio
                        metodo_score = "precio"

        if not mascara_comparable.any():
            _guardar_score_variante(candidato_id, indice_variante, color_variante, None,
                                    "sin_comparables", 0, None, filtro_aplicado, None,
                                    modelo_activo=modelo_activo, dominio="marca")
            if indice_variante != 0:
                continue
            cl.execute("""
                INSERT INTO candidato_score
                    (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos,
                     score_final, clasificacion, vecinos_detalle, filtro_aplicado,
                     sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color, f_atrib, f_demanda,
                     f_rotacion, f_venta, rotacion_proxy, venta_proxy,
                     f_descuento, descuento_proxy,
                     version_formula, modelo_activo, dominio, fecha_calculo)
                VALUES (?, NULL, NULL, NULL, 0, NULL, 'sin_comparables', '[]', ?,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, 'marca', ?)
                ON CONFLICT (candidato_id) DO UPDATE SET
                    demanda_proxy=NULL, tendencia_proxy=NULL, margen_factor=NULL, n_vecinos=0,
                    score_final=NULL, clasificacion='sin_comparables', vecinos_detalle='[]',
                    filtro_aplicado=excluded.filtro_aplicado,
                    sim_ponderada=NULL, k_efectivo=NULL, f_soporte=NULL, f_tipo=NULL,
                    f_color=NULL, f_atrib=NULL, f_demanda=NULL,
                    f_rotacion=NULL, f_venta=NULL, rotacion_proxy=NULL, venta_proxy=NULL,
                    f_descuento=NULL, descuento_proxy=NULL,
                    version_formula=NULL, modelo_activo=excluded.modelo_activo,
                    dominio=excluded.dominio, fecha_calculo=excluded.fecha_calculo
            """, (candidato_id, filtro_aplicado, modelo_activo, almacen.ahora()))
            continue

        kk = min(k, int(mascara_comparable.sum()))
        sims = np.where(mascara_comparable, sims, -np.inf)
        top = np.argsort(-sims, kind="stable")[:kk]
        umbral_activo = umbral_dominio if metodo_score == "visual" else 0.0
        pesos = np.clip(sims[top] - umbral_activo, 0, None)
        pesos = pesos / pesos.sum() if pesos.sum() > 0 else np.ones(kk) / kk

        sim_pond = float(np.sum(pesos * sims[top]))
        if metodo_score == "visual":
            # Ancla del DOMINIO MARCA, no la de tuc: ver `ANCLA_SIMILITUD_MARCA`.
            umbral_sim = umbral_dominio
            ancla_sim = ancla_similitud(modelo_activo, "marca")
        else:
            umbral_sim, ancla_sim = 0.0, 1.0
        sim_norm = min(1.0, max(0.0, (sim_pond - umbral_sim) / (ancla_sim - umbral_sim)))
        score_base = 100.0 * sim_norm

        k_efectivo = float(1.0 / np.sum(pesos ** 2))
        f_soporte = 0.70 + 0.30 * min(1.0, k_efectivo / K_SOPORTE_PLENO)

        f_tipo = (1.00 if filtro_aplicado == "categoria+genero" else
                  0.92 if filtro_aplicado in ("solo_categoria", "solo_genero") else
                  0.85)
        if categoria_conflicto:
            f_tipo = min(f_tipo, 0.92)

        # f_color: NEUTRO en este canal, a propósito -- ver el docstring. No
        # hay tabla de prevalencia de color por categoría de marca; inventar un
        # lift con la del catálogo genérico sería peor que no medirlo.
        f_color = 1.00

        f_atrib = 1.00
        materiales_top = idx["materiales"][top]
        con_material = materiales_top != None  # noqa: E711
        if con_material.sum() >= 2:
            peso_con_material = float(np.sum(pesos[con_material]))
            if peso_con_material > 0:
                valores_material = materiales_top[con_material]
                homogeneidad = max(
                    float(np.sum(pesos[con_material][valores_material == val])) / peso_con_material
                    for val in set(valores_material)
                )
                f_atrib = min(1.05, max(0.95, 0.98 + 0.04 * homogeneidad))

        # f_demanda: velocidad real del canal marca, ponderada SOLO entre los
        # vecinos que tienen ventas propias registradas (el resto es producto
        # de mercado que nunca compramos; NaN, no cero).
        velocidades_top = idx["velocidades"][top]
        tendencias_top = idx["tendencias"][top]
        con_venta = ~np.isnan(velocidades_top)
        demanda_proxy = tendencia_proxy = None
        f_demanda = 1.00
        if con_venta.sum() >= MIN_VECINOS_DEMANDA_MARCA:
            peso_con_venta = float(np.sum(pesos[con_venta]))
            if peso_con_venta > 0:
                demanda_proxy = float(np.sum(pesos[con_venta] * velocidades_top[con_venta])
                                       / peso_con_venta)
                t_validas = tendencias_top[con_venta]
                t_validas = np.where(np.isnan(t_validas), 1.0, t_validas)
                tendencia_proxy = float(np.sum(pesos[con_venta] * t_validas) / peso_con_venta)
                f_demanda = min(1.20, max(0.85, 0.85 + 0.35 * (demanda_proxy / REFERENCIA_DEMANDA_MARCA)))

        # f_rotacion / f_descuento / f_venta: mismo pedido explícito del dueño
        # que el canal genérico, y desde el plan de mejora 2026-09-22, con la
        # MISMA fuente 100% Power BI que el canal genérico -- ver
        # REFERENCIA_ROT30_MARCA/REFERENCIA_PCT_SIN_PROMO_MARCA/
        # REFERENCIA_FACTOR_VENTA_MARCA. `pct_rotacion` y `pct_sobre_lista`
        # salen de `silver.fct_tcm_rotacion`/`silver.fct_tcm_precio`
        # (Catalogo=TCMARCAS de los mismos 2 reportes que TUCALZADO); ninguno
        # depende ya de AsistenteComprasTEC ni del CSV del ERP.
        rotacion_proxy = float(np.sum(pesos * idx["rot30"][top]))
        descuento_proxy = float(np.sum(pesos * idx["pct_sin_promo"][top]))
        venta_proxy = float(np.sum(pesos * idx["factor_venta"][top]))
        f_rotacion = min(1.15, max(0.85, 0.85 + 0.30 * (rotacion_proxy / REFERENCIA_ROT30_MARCA)))
        f_descuento = min(1.15, max(0.85, 0.85 + 0.30 * (descuento_proxy / REFERENCIA_PCT_SIN_PROMO_MARCA)))
        f_venta = min(1.20, max(0.85, 0.85 + 0.35 * (venta_proxy / REFERENCIA_FACTOR_VENTA_MARCA)))

        margen_factor = 1.0  # igual que el canal genérico: se recalcula al cotizar
        factor_mercado = _factor_mercado(cur, candidato_id)

        score_final = round(score_base * f_soporte * f_tipo * f_color * f_atrib
                             * f_demanda * f_rotacion * f_venta * f_descuento
                             * margen_factor * factor_mercado, 4)
        clasificacion = ("S" if score_final >= CORTES_CLASIFICACION["S"] else
                          "A" if score_final >= CORTES_CLASIFICACION["A"] else
                          "B" if score_final >= CORTES_CLASIFICACION["B"] else
                          "C" if score_final >= CORTES_CLASIFICACION["C"] else "D")

        # Los vecinos de este canal se identifican por marca+referencia del
        # fabricante, no por `codigo_tuc` (no existe en dominio marca). La
        # clave `codigo_tuc` va explícita en None para que los consumidores
        # que la leen (`api_desglose_score`, `precio_venta_referencia`) vean
        # "no hay" en vez de reventar con KeyError.
        orden_peso = np.argsort(-pesos)
        vecinos_detalle = [
            {"codigo_tuc": None,
             "dominio": "marca",
             "marca": (str(idx["marcas"][top[j]]) if idx["marcas"][top[j]] else None),
             "referencia": str(idx["referencias"][top[j]]),
             "codigo_tc": (str(idx["codigos_tc"][top[j]]) if idx["codigos_tc"][top[j]] else None),
             "similitud": round(float(sims[top[j]]), 4),
             "peso": round(float(pesos[j]), 4),
             "metodo": metodo_score}
            for j in orden_peso
        ]

        _guardar_score_variante(candidato_id, indice_variante, color_variante, score_final,
                                clasificacion, kk, round(sim_pond, 4), filtro_aplicado,
                                metodo_score, modelo_activo=modelo_activo, dominio="marca")
        if indice_variante != 0:
            continue  # `candidato_score` = la fila de la variante 0, como siempre

        _guardar_score_candidato(
            cl, candidato_id,
            demanda_proxy=round(demanda_proxy, 3) if demanda_proxy is not None else None,
            tendencia_proxy=round(tendencia_proxy, 3) if tendencia_proxy is not None else None,
            margen_factor=round(margen_factor, 3), n_vecinos=kk,
            score_final=score_final, clasificacion=clasificacion,
            vecinos_detalle=json.dumps(vecinos_detalle, ensure_ascii=False),
            factor_mercado=round(factor_mercado, 4), filtro_aplicado=filtro_aplicado,
            sim_ponderada=round(sim_pond, 4), k_efectivo=round(k_efectivo, 3),
            f_soporte=round(f_soporte, 4), f_tipo=round(f_tipo, 4),
            f_color=round(f_color, 4), f_atrib=round(f_atrib, 4),
            f_demanda=round(f_demanda, 4),
            f_rotacion=round(f_rotacion, 4), rotacion_proxy=round(rotacion_proxy, 3),
            f_venta=round(f_venta, 4), venta_proxy=round(venta_proxy, 3),
            f_descuento=round(f_descuento, 4), descuento_proxy=round(descuento_proxy, 3),
            version_formula="2026-09-21-marca-v3",
            modelo_activo=modelo_activo, dominio="marca")


def puntuar_candidatos(cur, candidato_ids, k=7, modelo_activo: str = "estandar"):
    """Envoltorio transaccional de `_puntuar_candidatos_impl` (que a su vez
    puede derivar a `_puntuar_candidatos_marca_impl`) -- plan de mejora
    2026-09-22, Etapa 2: antes, `_limpiar_scores_variante` borraba los scores
    por color de una corrida anterior ANTES de recalcularlos, sin ninguna
    transacción ni manejo de fallo -- si Postgres se caía, la red fallaba, o
    el usuario cerraba la app a mitad del bucle, el lote quedaba con
    candidatos SIN NINGÚN SCORE y sin aviso. `lote.sqlite` abre con
    `isolation_level=None` (autocommit, ver `almacen.abrir`), pero eso no
    impide un `BEGIN`/`COMMIT` explícito -- solo significa que sin este
    envoltorio cada sentencia se confirmaba sola, sin ninguna atomicidad.

    Con esto: o el lote queda ENTERAMENTE recalculado, o queda EXACTAMENTE
    como estaba antes de llamar a esta función. Nunca a medias."""
    cl = _cl()
    cl.execute("BEGIN")
    try:
        _puntuar_candidatos_impl(cur, candidato_ids, k=k, modelo_activo=modelo_activo)
    except Exception:
        cl.execute("ROLLBACK")
        raise
    else:
        cl.execute("COMMIT")


def _puntuar_candidatos_impl(cur, candidato_ids, k=7, modelo_activo: str = "estandar"):
    """Filtro duro por categoria+genero_canonico antes de rankear por similitud
    (antes comparaba contra TODO el índice sin distinguir tipo/género -- una
    sandalia de dama podía heredar demanda-proxy de botas de hombre). Si el
    candidato aún no tiene categoria/genero resueltos, se degrada sin filtro
    (mejor una estimación ruidosa que ninguna).

    `modelo_activo="ti"`: índice y similitud con un solo vector (dinov2_ti).
    Migrado 2026-09-07 (plan 2026-09-04, paso 3) de la réplica propia
    (`silver.fct_embedding_ti`, vía `imagen_id`) a los vectores REALES que TI
    entregó (`silver.fct_embedding_ti_codigo`, llave directa por
    `codigo_tuc` -- ya no hace falta el puente por `fct_embedding_imagen`).

    CANAL DE VENTA (2026-09-10): si el lote declaró que la compra es para TC
    Marcas (`lote_meta.canal_venta = 'marca'`), el puntaje se calcula contra el
    dominio 'marca' -- ver `_puntuar_candidatos_marca`. Si es para TU Calzado
    (o si el lote no lo declaró), corre el camino de siempre contra el dominio
    'tuc', sin un solo cambio respecto de antes de esta bifurcación."""
    if canal_venta_lote() == "marca":
        _puntuar_candidatos_marca_impl(cur, candidato_ids, k=k, modelo_activo=modelo_activo)
        return
    # Plan 2026-09-07, paso 2.4: mapas fijos de color, cargados una sola vez
    # por llamada (no por candidato) -- cfg.lkp_color_candidato_a_familia
    # (paso 2.1) y cfg.prevalencia_color_categoria (paso 2.2). Son tablas
    # CONSTANTES en el sentido de negocio (se actualizan a mano, nunca se
    # recalculan acá), pero siguen viviendo en Postgres para no duplicar su
    # contenido en el código -- la lectura en sí no rompe el determinismo
    # porque el contenido no cambia entre corridas normales.
    cur.execute("SELECT color_candidato, color_familia FROM cfg.lkp_color_candidato_a_familia WHERE activo")
    mapa_color_candidato = dict(cur.fetchall())
    cur.execute("SELECT categoria, color_familia, prevalencia FROM cfg.prevalencia_color_categoria")
    mapa_prevalencia = {(cat, col): float(prev) for cat, col, prev in cur.fetchall()}

    if modelo_activo == "ti":
        query = """
            SELECT t.vector, m.velocidad_mensual, m.tendencia_3m, m.rot_mensual, m.factor_venta,
                   m.pct_sobre_lista,
                   p.categoria, g.genero_canonico,
                   t.codigo_tuc, p.precio_mediano, a.color_familia, a.material_norm
            FROM silver.fct_embedding_ti_codigo t
            JOIN gold.agg_tuc_metricas m ON m.codigo_tuc = t.codigo_tuc
            JOIN silver.dim_tuc_producto p ON p.codigo_tuc = t.codigo_tuc
            LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
            LEFT JOIN bronze.raw_tuc_atributos a ON a.codigo = t.codigo_tuc
            WHERE t.modelo = 'dinov2_ti'
        """
        # Cursor servidor + numpy por lote -- ver `_consumir_por_lotes` (plan
        # de mejora 2026-09-22 Etapa 5, medido: 617MB -> 258MB de pico).
        # `resto` guarda SOLO metadata (f[1:]), nunca el vector -- si también
        # se guardara la fila completa, el vector quedaría duplicado (una vez
        # en la lista Python cruda, otra vez en el numpy de `trozos_vec`) y
        # se perdería el ahorro de memoria que motiva este cambio.
        trozos_vec, resto = [], []
        for lote in _consumir_por_lotes(cur, query):
            trozos_vec.append(np.array([f[0] for f in lote], dtype=np.float32))
            resto.extend(f[1:] for f in lote)
        if not resto:
            return
        filas = resto
        indice = np.concatenate(trozos_vec, axis=0)
        indice_dino = None
        velocidades = np.array([float(f[0] or 0) for f in filas])
        tendencias = np.array([float(f[1] or 1.0) for f in filas])
        rotaciones = np.array([float(f[2]) if f[2] is not None else REFERENCIA_ROTACION for f in filas])
        factores_venta = np.array([float(f[3]) if f[3] is not None else REFERENCIA_FACTOR_VENTA for f in filas])
        pcts_sobre_lista = np.array([float(f[4]) if f[4] is not None else REFERENCIA_PCT_SOBRE_LISTA for f in filas])
        categorias_idx = np.array([f[5] for f in filas], dtype=object)
        generos_idx = np.array([f[6] for f in filas], dtype=object)
        codigos_idx = np.array([f[7] for f in filas], dtype=object)
        precios_idx = np.array([float(f[8]) if f[8] is not None else np.nan for f in filas])
        colores_idx = np.array([f[9] for f in filas], dtype=object)
        materiales_idx = np.array([f[10] for f in filas], dtype=object)
    else:
        query = """
            SELECT s.vector, d.vector, m.velocidad_mensual, m.tendencia_3m, m.rot_mensual, m.factor_venta,
                   m.pct_sobre_lista,
                   p.categoria, g.genero_canonico,
                   s.codigo_tuc, p.precio_mediano, a.color_familia, a.material_norm
            FROM silver.fct_embedding_imagen s
            JOIN silver.fct_embedding_imagen d ON d.imagen_id = s.imagen_id AND d.modelo_embedding = 'dino_v2'
            JOIN gold.agg_tuc_metricas m ON m.codigo_tuc = s.codigo_tuc
            JOIN silver.dim_tuc_producto p ON p.codigo_tuc = s.codigo_tuc
            LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
            LEFT JOIN bronze.raw_tuc_atributos a ON a.codigo = s.codigo_tuc
            WHERE s.modelo_embedding = 'fashion_siglip' AND s.dominio = 'tuc'
        """
        # "dino": el índice es la columna dino_v2 (f[1]); "fashion" se queda
        # con fashion_siglip (f[0]), que es lo que ya traía. En los dos casos
        # `indice_dino` queda en None: no hay fusión que ponderar.
        columna_vector = 1 if modelo_activo == "dino" else 0
        necesita_dino = modelo_activo not in MODELO_UNICO
        # Cursor servidor + numpy por lote -- ver `_consumir_por_lotes` (plan
        # de mejora 2026-09-22 Etapa 5, medido: 617MB -> 258MB de pico).
        # `resto` guarda SOLO metadata (f[2:]), nunca los vectores -- ver la
        # nota equivalente en la rama "ti" de arriba.
        trozos_vec, trozos_dino, resto = [], [], []
        for lote in _consumir_por_lotes(cur, query):
            trozos_vec.append(np.array([f[columna_vector] for f in lote], dtype=np.float32))
            if necesita_dino:
                trozos_dino.append(np.array([f[1] for f in lote], dtype=np.float32))
            resto.extend(f[2:] for f in lote)
        if not resto:
            return
        filas = resto
        indice = np.concatenate(trozos_vec, axis=0)
        if not necesita_dino:
            indice_dino = None
        else:
            indice_dino = np.concatenate(trozos_dino, axis=0)
            normas = np.linalg.norm(indice_dino, axis=1, keepdims=True); normas[normas == 0] = 1.0
            indice_dino /= normas
        velocidades = np.array([float(f[0] or 0) for f in filas])
        tendencias = np.array([float(f[1] or 1.0) for f in filas])
        rotaciones = np.array([float(f[2]) if f[2] is not None else REFERENCIA_ROTACION for f in filas])
        factores_venta = np.array([float(f[3]) if f[3] is not None else REFERENCIA_FACTOR_VENTA for f in filas])
        pcts_sobre_lista = np.array([float(f[4]) if f[4] is not None else REFERENCIA_PCT_SOBRE_LISTA for f in filas])
        categorias_idx = np.array([f[5] for f in filas], dtype=object)
        generos_idx = np.array([f[6] for f in filas], dtype=object)
        codigos_idx = np.array([f[7] for f in filas], dtype=object)
        precios_idx = np.array([float(f[8]) if f[8] is not None else np.nan for f in filas])
        colores_idx = np.array([f[9] for f in filas], dtype=object)
        materiales_idx = np.array([f[10] for f in filas], dtype=object)
    normas = np.linalg.norm(indice, axis=1, keepdims=True); normas[normas == 0] = 1.0
    indice /= normas

    # UN PUNTAJE POR COLOR (2026-09-17). Antes este bucle corría una vez por
    # candidato con el vector PROMEDIO de sus variantes; ahora corre una vez
    # por COLOR, con el vector real de ese color. Para una referencia de un
    # solo color es la misma corrida de siempre con el mismo vector, así que
    # su número no se mueve.
    _limpiar_scores_variante(candidato_ids)
    tareas = []
    for candidato_id_t in candidato_ids:
        calificables, sin_vector = variantes_a_calificar(candidato_id_t, modelo_activo)
        for indice_t, color_t, v_t, v_dino_t in calificables:
            tareas.append((candidato_id_t, indice_t, color_t, v_t, v_dino_t))
        for indice_t, color_t in sin_vector:
            # Honestidad explícita (pedido del usuario): un color sin vector NO
            # recibe un score inventado ni desaparece -- queda registrado como
            # "sin_vector" para que la tarjeta pueda decir que falta
            # vectorizarlo.
            _guardar_score_variante(candidato_id_t, indice_t, color_t, None,
                                    "sin_vector", 0, None, None, None,
                                    modelo_activo=modelo_activo, dominio="tuc")

    for candidato_id, indice_variante, color_variante, v, v_dino in tareas:
        # FASE 2: los atributos del CANDIDATO (tipo, género, conflicto, color)
        # salen del `lote.sqlite`. El índice contra el que se compara --
        # embeddings, métricas, precios y atributos del catálogo TUC ya
        # vendido, cargado más arriba de una sola vez-- sigue viniendo de
        # Postgres sin un solo cambio: es el dato permanente de la empresa.
        cl = _cl()
        cl.execute("""
            SELECT categoria_declarada, genero_canonico, categoria_conflicto
            FROM candidato WHERE candidato_id = ?
        """, (candidato_id,))
        cat_c, gen_c, categoria_conflicto = tuple(cl.fetchone())
        # `categoria_conflicto` es boolean en Postgres e INTEGER 0/1 acá; se
        # convierte porque más abajo se evalúa como condición y termina
        # topando `f_tipo`.
        categoria_conflicto = almacen.booleano(categoria_conflicto)
        # Color propio (paso 2.4): ahora es el color de ESTA variante, no el de
        # la variante 0 -- que es justamente lo que hace que un color malo y
        # uno bueno de la misma referencia puedan salir distintos. Para una
        # referencia de un solo color es la variante 0, o sea el mismo valor
        # que se usaba antes.
        color_candidato_familia = (mapa_color_candidato.get(color_variante)
                                   if color_variante else None)

        mascara = np.ones(len(filas), dtype=bool)
        if cat_c:
            mascara &= (categorias_idx == cat_c)
        if gen_c:
            mascara &= (generos_idx == gen_c)
        # Plan 2026-09-07, paso 2.3 (fórmula determinística): instrumenta el
        # camino que REALMENTE corrió, no lo que se podría inferir de
        # categoria_declarada/genero_canonico. Hallazgo del auditor: mirar
        # solo categoria_declarada no detecta esta degradación -- un
        # candidato CON categoría declarada puede terminar sin filtro igual
        # si esa categoría+género no tiene ningún vecino real, y antes eso
        # era invisible. `filtro_aplicado` se guarda en agg_candidato_score
        # para que f_tipo (paso 2.4) lea el hecho real, no la columna.
        if mascara.any():
            filtro_aplicado = ("categoria+genero" if (cat_c and gen_c) else
                                "solo_categoria" if cat_c else
                                "solo_genero" if gen_c else "ninguno")
        else:  # filtro demasiado estricto (ej. género raro sin vecinos) -> degradar sin filtro
            mascara = np.ones(len(filas), dtype=bool)
            filtro_aplicado = "ninguno"

        if modelo_activo in MODELO_UNICO:
            sims = indice @ v
        else:
            sims = ALPHA_FUSION_SIGLIP * (indice @ v)
            if v_dino is not None:
                sims = sims + (1 - ALPHA_FUSION_SIGLIP) * (indice_dino @ v_dino)
        sims = np.where(mascara, sims, -np.inf)

        # Auditoría Opus 2026-09-03, hallazgo real: `SIMILITUD_MINIMA_COMPARABLE`
        # existía pero NUNCA se aplicaba acá -- la única máscara era categoría/
        # género, así que un candidato podía salir "grado A" ponderado contra
        # vecinos con 50% de similitud real. A diferencia del filtro de
        # categoría/género (que si queda vacío SE DEGRADA sin filtro, porque un
        # género raro sin vecinos es un caso legítimo), el umbral de similitud
        # NO se degrada: si nadie lo pasa, es que de verdad no hay comparables,
        # y un producto sin comparables debe quedar SIN GRADO, no con un score
        # inventado sobre vecinos parecidos a medias.
        # Umbral por modelo (paso 4 del plan 2026-09-04) -- 0.75 se calibró para
        # la fusión SigLIP+DINOv2 y NO es válido para el espacio DINOv2 puro de
        # TI (otra distribución de coseno); ver UMBRAL_POR_MODELO.
        # Dominio 'tuc' explícito: este camino busca contra el catálogo genérico
        # y su umbral NO cambia (sigue siendo el ya calibrado y verificado).
        umbral_dominio = umbral_comparable(modelo_activo, "tuc")
        mascara_comparable = mascara & (sims >= umbral_dominio)

        metodo_score = "visual"
        if not mascara_comparable.any():
            # Paso 8 del plan 2026-09-04: sin nada visualmente comparable, se
            # intenta un respaldo por PRECIO -- un producto de precio parecido,
            # dentro de la misma categoría/género, es una segunda forma
            # razonable de estimar demanda-proxy, pedida explícitamente por el
            # usuario para que ningún candidato quede sin score.
            cl.execute("""
                SELECT r.costo, r.moneda_costo FROM candidato c
                JOIN candidato_raw r
                    ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
                   AND r.catalogo_origen = c.catalogo_origen
                WHERE c.candidato_id = ?
            """, (candidato_id,))
            fila_costo = cl.fetchone()
            precio_candidato = _precio_venta_estimado(*tuple(fila_costo)) if fila_costo else None
            if precio_candidato:
                precios_validos = ~np.isnan(precios_idx) & mascara
                if precios_validos.any():
                    dif_rel = np.abs(precios_idx - precio_candidato) / precio_candidato
                    mascara_precio = precios_validos & (dif_rel <= TOLERANCIA_PRECIO)
                    if mascara_precio.any():
                        # 1.0 = precio idéntico, 0 justo en el borde de la tolerancia --
                        # mismo rol que `sims` para el resto del cálculo (top-k, pesos).
                        sims = np.where(mascara_precio, 1 - (dif_rel / TOLERANCIA_PRECIO), -np.inf)
                        mascara_comparable = mascara_precio
                        metodo_score = "precio"

        if not mascara_comparable.any():
            _guardar_score_variante(candidato_id, indice_variante, color_variante, None,
                                    "sin_comparables", 0, None, filtro_aplicado, None,
                                    modelo_activo=modelo_activo, dominio="tuc")
            if indice_variante != 0:
                continue
            # FASE 2: mismo UPSERT, en el `lote.sqlite`. `'[]'::jsonb` pasa a
            # ser el texto `'[]'` (la columna es TEXT con JSON) y `now()` lo
            # pone Python (`almacen.ahora()`), que es lo que ya hace el resto
            # de este archivo para las marcas de tiempo.
            cl.execute("""
                INSERT INTO candidato_score
                    (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos,
                     score_final, clasificacion, vecinos_detalle, filtro_aplicado,
                     sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color, f_atrib, f_demanda,
                     f_rotacion, f_venta, rotacion_proxy, venta_proxy,
                     f_descuento, descuento_proxy,
                     version_formula, modelo_activo, dominio, fecha_calculo)
                VALUES (?, NULL, NULL, NULL, 0, NULL, 'sin_comparables', '[]', ?,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, 'tuc', ?)
                ON CONFLICT (candidato_id) DO UPDATE SET
                    demanda_proxy=NULL, tendencia_proxy=NULL, margen_factor=NULL, n_vecinos=0,
                    score_final=NULL, clasificacion='sin_comparables', vecinos_detalle='[]',
                    filtro_aplicado=excluded.filtro_aplicado,
                    sim_ponderada=NULL, k_efectivo=NULL, f_soporte=NULL, f_tipo=NULL,
                    f_color=NULL, f_atrib=NULL, f_demanda=NULL,
                    f_rotacion=NULL, f_venta=NULL, rotacion_proxy=NULL, venta_proxy=NULL,
                    f_descuento=NULL, descuento_proxy=NULL,
                    version_formula=NULL, modelo_activo=excluded.modelo_activo,
                    dominio=excluded.dominio, fecha_calculo=excluded.fecha_calculo
            """, (candidato_id, filtro_aplicado, modelo_activo, almacen.ahora()))
            continue

        kk = min(k, int(mascara_comparable.sum()))
        sims = np.where(mascara_comparable, sims, -np.inf)
        top = np.argsort(-sims, kind="stable")[:kk]
        # Paso 5 del plan 2026-09-04: antes se ponderaba por la similitud CRUDA
        # (`clip(sims,0)`), y como `top` ya viene filtrado por el umbral, TODOS
        # los vecinos aquí tienen similitud >= umbral -- un vecino apenas sobre
        # el corte (ej. 0.66 con umbral 0.65) pesaba casi lo mismo, en términos
        # relativos, que uno con match casi perfecto (0.95). Restar el umbral
        # antes de ponderar hace que un vecino "apenas comparable" pese casi
        # nada de verdad, no solo nominalmente menos.
        # El respaldo por precio ya construyó `sims` en su propia escala
        # (0 = borde de la tolerancia, 1 = precio idéntico) -- no tiene
        # sentido restarle el umbral de similitud VISUAL, que vive en otra
        # escala (coseno).
        umbral_activo = umbral_dominio if metodo_score == "visual" else 0.0
        pesos = np.clip(sims[top] - umbral_activo, 0, None)
        pesos = pesos / pesos.sum() if pesos.sum() > 0 else np.ones(kk) / kk
        demanda_proxy = float(np.sum(pesos * velocidades[top]))
        tendencia_proxy = float(np.sum(pesos * tendencias[top]))
        rotacion_proxy = float(np.sum(pesos * rotaciones[top]))
        venta_proxy = float(np.sum(pesos * factores_venta[top]))
        descuento_proxy = float(np.sum(pesos * pcts_sobre_lista[top]))
        margen_factor = 1.0  # candidato en staging aún no tiene cotización -- se recalcula tras promover

        # ============================================================
        # Plan 2026-09-07, paso 2.4: fórmula determinística. La comparación
        # vectorial es el TÉRMINO PRINCIPAL (score_base, spread medido 9.2x);
        # tipo/color/atributos/demanda son AJUSTES acotados cerca de 1.0 que
        # nunca pueden dominarla -- requisito explícito del usuario. Todo lo
        # que entra acá es (a) un atributo propio del candidato/sus vecinos,
        # o (b) una constante fija de módulo/tabla congelada -- NADA depende
        # de qué más exista en staging_tuc ni del estado del catálogo TUC en
        # el momento de puntuar (mismo candidato -> mismo score, siempre).
        sim_pond = float(np.sum(pesos * sims[top]))
        # El respaldo por precio (metodo_score="precio") ya deja sims en una
        # escala propia [0,1] (0=borde de tolerancia, 1=precio idéntico) --
        # no es una similitud coseno, así que usa su propio umbral/ancla
        # (0/1) en vez de UMBRAL_POR_MODELO/ANCLA_SIMILITUD.
        if metodo_score == "visual":
            # Dominio 'tuc' explícito (mismo valor de siempre: este canal no
            # cambia). Ver `ancla_similitud`.
            umbral_sim = umbral_dominio
            ancla_sim = ancla_similitud(modelo_activo, "tuc")
        else:
            umbral_sim, ancla_sim = 0.0, 1.0
        sim_norm = min(1.0, max(0.0, (sim_pond - umbral_sim) / (ancla_sim - umbral_sim)))
        score_base = 100.0 * sim_norm

        # f_soporte: cuánta evidencia real hay detrás del match -- k_efectivo
        # (inverso de Herfindahl de los pesos) distingue "7 vecinos genuinos"
        # de "7 vecinos donde 1 solo se lleva todo el peso", cosa que un
        # conteo simple de vecinos no ve.
        k_efectivo = float(1.0 / np.sum(pesos ** 2))
        f_soporte = 0.70 + 0.30 * min(1.0, k_efectivo / K_SOPORTE_PLENO)

        # f_tipo: lee filtro_aplicado (paso 2.3, el camino que REALMENTE
        # corrió), no categoria_declarada -- detecta también la degradación
        # invisible de la línea de "mascara.any()" de más arriba.
        f_tipo = (1.00 if filtro_aplicado == "categoria+genero" else
                  0.92 if filtro_aplicado in ("solo_categoria", "solo_genero") else
                  0.85)
        # Paso 2.5: cierra el vector de gaming "declarar una categoría que no
        # corresponde a la foto real" -- si el clasificador visual
        # (categorizar_candidatos) contradice lo que el proveedor declaró,
        # se aplica el mismo tope que un filtro parcial, sin importar que el
        # filtro haya corrido completo (categoria+genero ya no basta para
        # confiar en la categoría si la propia foto la contradice).
        if categoria_conflicto:
            f_tipo = min(f_tipo, 0.92)

        # f_color: lift sobre la prevalencia FIJA de ese color en esa
        # categoría (cfg.prevalencia_color_categoria, paso 2.2) -- un negro
        # entre negros no es la misma señal en Botas y botines (50% del
        # catálogo) que en Deportivos (37%). Neutro (1.00) si el candidato no
        # tiene color mapeado, si no hay categoría de referencia para buscar
        # la prevalencia, o si menos de 3 vecinos del top tienen color
        # poblado (evidencia insuficiente para una fracción confiable).
        f_color = 1.00
        colores_top = colores_idx[top]
        con_color = colores_top != None  # noqa: E711 -- np.object_ None real, no NaN
        if color_candidato_familia and cat_c and con_color.sum() >= 3:
            peso_con_color = float(np.sum(pesos[con_color]))
            if peso_con_color > 0:
                peso_mismo_color = float(np.sum(pesos[con_color & (colores_top == color_candidato_familia)]))
                frac_obs = peso_mismo_color / peso_con_color
                prevalencia = mapa_prevalencia.get((cat_c, color_candidato_familia))
                if prevalencia and prevalencia > 0:
                    lift = frac_obs / prevalencia
                    f_color = min(1.10, max(0.90, 0.90 + 0.10 * min(2.0, lift)))

        # f_atrib: homogeneidad de material_norm ENTRE los vecinos -- señal
        # de CONFIANZA (¿los vecinos concuerdan entre sí?), no de match
        # candidato-vecino (el candidato no tiene material propio, solo se
        # deriva de vectorización de vecinos). Neutro si <2 vecinos con
        # material poblado (homogeneidad de 0-1 muestra no significa nada).
        f_atrib = 1.00
        materiales_top = materiales_idx[top]
        con_material = materiales_top != None  # noqa: E711
        if con_material.sum() >= 2:
            peso_con_material = float(np.sum(pesos[con_material]))
            if peso_con_material > 0:
                valores_material = materiales_top[con_material]
                homogeneidad = max(
                    float(np.sum(pesos[con_material][valores_material == val])) / peso_con_material
                    for val in set(valores_material)
                )
                f_atrib = min(1.05, max(0.95, 0.98 + 0.04 * homogeneidad))

        # f_demanda: SECUNDARIO -- el candidato nunca va a tener demanda
        # propia (es un producto nuevo, la norma, no la excepción), así que
        # esto solo matiza el resultado que ya dio la similitud vectorial,
        # nunca lo reemplaza. REFERENCIA_DEMANDA es la media real fija del
        # catálogo TUC (17.4 u/mes), no un percentil recalculado.
        f_demanda = min(1.20, max(0.85, 0.85 + 0.35 * (demanda_proxy / REFERENCIA_DEMANDA)))

        # f_rotacion / f_venta: plan 2026-09-21, pedido directo del dueño --
        # dos variables medidas que ya existían en la base (rot_mensual =
        # % Rotación real del Power BI; factor_venta = "Factor venta" del
        # Excel de rotación) pero nunca llegaban al score. Mismo patrón que
        # f_demanda: factor acotado, ancla a una mediana real, nunca domina
        # sobre la similitud vectorial. Sin dato para un vecino, ya se
        # imputó la referencia (arriba, al armar `rotaciones`/
        # `factores_venta`) -- ese vecino queda neutro, no penaliza.
        f_rotacion = min(1.15, max(0.85, 0.85 + 0.30 * (rotacion_proxy / REFERENCIA_ROTACION)))
        f_venta = min(1.20, max(0.85, 0.85 + 0.35 * (venta_proxy / REFERENCIA_FACTOR_VENTA)))

        # f_descuento: plan 2026-09-21, mismo pedido -- precio_avg/precio_lista
        # ("Artículos por precio" del Power BI, silver.fct_tuc_precio) es el
        # proxy objetivo de "ventas sin descuento" que faltaba (ver
        # REFERENCIA_PCT_SOBRE_LISTA). Mismo patrón: factor acotado, ancla a
        # la mediana real, nunca domina sobre la similitud vectorial.
        f_descuento = min(1.15, max(0.85, 0.85 + 0.30 * (descuento_proxy / REFERENCIA_PCT_SOBRE_LISTA)))

        # Paso 7 del plan 2026-09-04: factor de importación/tendencia real
        # (aduana) por tipo+marca declarados -- 1.0 si no hay dato declarado
        # (siempre el caso para GestionTUC/PTY, que no puebla esas columnas).
        # Se mantiene sin cambios (no es parte de las 8 fallas auditadas del
        # paso 2.4) -- documentado como inerte en este flujo, no removido.
        factor_mercado = _factor_mercado(cur, candidato_id)

        score_final = round(score_base * f_soporte * f_tipo * f_color * f_atrib
                             * f_demanda * f_rotacion * f_venta * f_descuento
                             * margen_factor * factor_mercado, 4)
        clasificacion = ("S" if score_final >= CORTES_CLASIFICACION["S"] else
                          "A" if score_final >= CORTES_CLASIFICACION["A"] else
                          "B" if score_final >= CORTES_CLASIFICACION["B"] else
                          "C" if score_final >= CORTES_CLASIFICACION["C"] else "D")

        # Paso 2 del plan 2026-09-04: sin esto no había forma de auditar POR QUÉ
        # un candidato salió "S" o "D" -- antes solo se guardaba `n_vecinos` (un
        # número). Ordenado por peso descendente para que el primero de la
        # lista sea el que más influyó en el score.
        orden_peso = np.argsort(-pesos)
        vecinos_detalle = [
            {"codigo_tuc": str(codigos_idx[top[j]]),
             "similitud": round(float(sims[top[j]]), 4),
             "peso": round(float(pesos[j]), 4),
             "metodo": metodo_score}
            for j in orden_peso
        ]

        _guardar_score_variante(candidato_id, indice_variante, color_variante, score_final,
                                clasificacion, kk, round(sim_pond, 4), filtro_aplicado,
                                metodo_score, modelo_activo=modelo_activo, dominio="tuc")
        if indice_variante != 0:
            # `candidato_score` sigue siendo la fila de la VARIANTE 0 tal cual
            # se escribía antes (es la que lee el desglose «¿por qué?» y la
            # promoción a Postgres). La calificación de la referencia completa
            # -- promedio simple de los colores -- se arma al vuelo desde
            # `candidato_score_variante` en `api_candidatos`.
            continue

        # FASE 2: mismo UPSERT, en el `lote.sqlite`. `psycopg2.extras.Json`
        # (que serializaba a `jsonb`) pasa a ser un `json.dumps` explícito a
        # TEXT, y `now()` lo pone Python.
        cl.execute("""
            INSERT INTO candidato_score
                (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos,
                 score_final, clasificacion, vecinos_detalle, factor_mercado, filtro_aplicado,
                 sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color, f_atrib, f_demanda,
                 f_rotacion, f_venta, rotacion_proxy, venta_proxy,
                 f_descuento, descuento_proxy,
                 version_formula, modelo_activo, dominio, fecha_calculo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidato_id) DO UPDATE SET
                demanda_proxy=excluded.demanda_proxy, tendencia_proxy=excluded.tendencia_proxy,
                margen_factor=excluded.margen_factor, n_vecinos=excluded.n_vecinos,
                score_final=excluded.score_final, clasificacion=excluded.clasificacion,
                vecinos_detalle=excluded.vecinos_detalle, factor_mercado=excluded.factor_mercado,
                filtro_aplicado=excluded.filtro_aplicado,
                sim_ponderada=excluded.sim_ponderada, k_efectivo=excluded.k_efectivo,
                f_soporte=excluded.f_soporte, f_tipo=excluded.f_tipo, f_color=excluded.f_color,
                f_atrib=excluded.f_atrib, f_demanda=excluded.f_demanda,
                f_rotacion=excluded.f_rotacion, f_venta=excluded.f_venta,
                rotacion_proxy=excluded.rotacion_proxy, venta_proxy=excluded.venta_proxy,
                f_descuento=excluded.f_descuento, descuento_proxy=excluded.descuento_proxy,
                version_formula=excluded.version_formula, modelo_activo=excluded.modelo_activo,
                dominio=excluded.dominio, fecha_calculo=excluded.fecha_calculo
        """, (candidato_id, round(demanda_proxy, 3), round(tendencia_proxy, 3), round(margen_factor, 3),
              kk, score_final, clasificacion, json.dumps(vecinos_detalle, ensure_ascii=False),
              round(factor_mercado, 4), filtro_aplicado,
              round(sim_pond, 4), round(k_efectivo, 3), round(f_soporte, 4), round(f_tipo, 4),
              round(f_color, 4), round(f_atrib, 4), round(f_demanda, 4),
              round(f_rotacion, 4), round(f_venta, 4), round(rotacion_proxy, 3), round(venta_proxy, 3),
              round(f_descuento, 4), round(descuento_proxy, 3),
              "2026-09-21-v3", modelo_activo, "tuc", almacen.ahora()))


# ---------- promoción staging -> permanente (solo al cotizar) ----------

def _promover_candidato(cur, candidato_id_staging: int) -> int:
    """El único momento en que un candidato en revisión se vuelve dato
    PERMANENTE: se lee del `lote.sqlite` y se escribe en Postgres
    (`silver.dim_tuc_candidato` + `gold.agg_tuc_candidato_score`).

    FASE 2: esta es exactamente la frontera que el plan quería dejar nítida.
    Antes leía de `staging_tuc` (Postgres) y escribía a `silver` (Postgres), y
    era fácil confundir las dos cosas porque estaban en la misma base. Ahora la
    LECTURA es local y la ESCRITURA es a Postgres: la promoción es visible en
    el código, no un detalle de qué schema se nombró.

    Los booleanos se convierten de INTEGER 0/1 a True/False/None antes de
    escribirlos: las columnas destino en Postgres SÍ son `boolean`, y aunque
    psycopg2 acepta un 1, guardar el tipo correcto evita que el próximo lector
    reciba algo distinto de lo que había antes de la migración.
    """
    cl = _cl()
    cl.execute("""
        SELECT proveedor_id, codigo_proveedor, catalogo_origen, linea_declarada, fecha_ingesta,
               categoria_declarada, grupo_declarado, genero_declarado, genero_canonico, n_variantes_color,
               es_multicolor, n_colores_detectados, colores_detectados, posibles_mismo_modelo,
               tiene_tacon, tiene_plataforma
        FROM candidato WHERE candidato_id = ?
    """, (candidato_id_staging,))
    r = cl.fetchone()
    if not r:
        raise ValueError(f"candidato_id={candidato_id_staging} no existe en la revisión de este lote")
    (proveedor_id, codigo_proveedor, catalogo_origen, linea_declarada, fecha_ingesta,
     categoria_declarada, grupo_declarado, genero_declarado, genero_canonico, n_variantes_color,
     es_multicolor, n_colores_detectados, colores_detectados, posibles_mismo_modelo,
     tiene_tacon, tiene_plataforma) = tuple(r)
    es_multicolor = almacen.booleano(es_multicolor)
    tiene_tacon = almacen.booleano(tiene_tacon)
    tiene_plataforma = almacen.booleano(tiene_plataforma)

    cl.execute("""
        SELECT imagen_limpia_path, color_principal, color_suela, suela_contraste, color_suela_delta_e
        FROM candidato_variante WHERE candidato_id = ? ORDER BY indice LIMIT 1
    """, (candidato_id_staging,))
    v0 = cl.fetchone()
    (imagen_limpia_path, color_principal, color_suela, suela_contraste,
     color_suela_delta_e) = tuple(v0) if v0 else (None, None, None, None, None)
    suela_contraste = almacen.booleano(suela_contraste)

    cur.execute("""
        INSERT INTO silver.dim_tuc_candidato
            (proveedor_id, codigo_proveedor, catalogo_origen, linea_declarada, fecha_ingesta,
             categoria_declarada, grupo_declarado, genero_declarado, genero_canonico, n_variantes_color,
             imagen_limpia_path, color_principal, color_suela, es_multicolor,
             n_colores_detectados, colores_detectados, posibles_mismo_modelo, tiene_tacon, tiene_plataforma,
             suela_contraste, color_suela_delta_e)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (proveedor_id, codigo_proveedor, catalogo_origen) DO UPDATE SET
            categoria_declarada = COALESCE(silver.dim_tuc_candidato.categoria_declarada, EXCLUDED.categoria_declarada)
        RETURNING candidato_id
    """, (proveedor_id, codigo_proveedor, catalogo_origen, linea_declarada, fecha_ingesta,
          categoria_declarada, grupo_declarado, genero_declarado, genero_canonico, n_variantes_color,
          imagen_limpia_path, color_principal, color_suela, es_multicolor,
          n_colores_detectados, colores_detectados, posibles_mismo_modelo, tiene_tacon, tiene_plataforma,
          suela_contraste, color_suela_delta_e))
    candidato_id_permanente = cur.fetchone()[0]

    # Snapshot del score visto al momento de decidir. El motivo original era
    # que `staging_tuc` no sobrevivía al TRUNCATE siguiente; con la Fase 2 el
    # score local SÍ sobrevive (vive en el lote), pero el snapshot se conserva
    # igual y a propósito: es la prueba permanente de con qué número se tomó la
    # decisión de compra, y no debe cambiar si después se recalifica el lote.
    cl.execute("SELECT demanda_proxy, tendencia_proxy, margen_factor, n_vecinos, score_final, clasificacion "
               "FROM candidato_score WHERE candidato_id = ?", (candidato_id_staging,))
    s_local = cl.fetchone()
    s = tuple(s_local) if s_local else None
    if s:
        cur.execute("""
            INSERT INTO gold.agg_tuc_candidato_score
                (candidato_id, demanda_proxy, tendencia_proxy, margen_factor, n_vecinos, score_final, clasificacion)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidato_id) DO NOTHING
        """, (candidato_id_permanente, *s))
    return candidato_id_permanente


def api_guardar_cotizacion(payload: dict) -> dict:
    c = conn(); cur = c.cursor()
    with c:
        candidato_id_staging = payload["candidato_id"]
        candidato_id_permanente = _promover_candidato(cur, candidato_id_staging)
        cur.execute("""
            INSERT INTO silver.tuc_cotizacion
                (candidato_id, costo_lista, descuento_pct, costo_neto, moneda,
                 tipo_cambio_usd_crc, fecha_cotizacion, fuente, ingresado_por)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'catalogo_proveedor', 'comprador')
        """, (candidato_id_permanente, payload["costo_lista"], payload.get("descuento_pct", 0),
              payload["costo_neto"], payload.get("moneda", "USD"), payload.get("tipo_cambio_usd_crc"),
              date.today()))
        # Plan 2026-09-07, paso 5: hallazgo real -- la cotización se guardaba
        # pero NUNCA volvía a raw_catalogo.costo/moneda_costo, así que el
        # Optimizador de Compra seguía sin precio para ese candidato aunque
        # el comprador ya hubiera negociado uno. El candidato SIGUE en
        # staging_tuc hasta que se descarte/reemplace el catálogo (promover
        # no lo borra de ahí), así que esta escritura tiene efecto inmediato.
    # FASE 2: el costo negociado vuelve al catálogo EN REVISIÓN, que ahora es
    # local. El `UPDATE ... FROM` de Postgres se reescribe como un UPDATE con
    # subconsultas correlacionadas, que es lo que SQLite entiende (no soporta
    # `UPDATE ... FROM` con esta forma de junta múltiple).
    #
    # Va DESPUÉS del `with c:` a propósito: si la escritura permanente a
    # Postgres falla y hace rollback, no queremos haber dejado ya escrito el
    # costo en el lote como si la cotización se hubiera guardado.
    _cl().execute("""
        UPDATE candidato_raw
        SET costo = ?, moneda_costo = ?
        WHERE EXISTS (
            SELECT 1 FROM candidato d
            WHERE d.candidato_id = ?
              AND candidato_raw.proveedor_id    = d.proveedor_id
              AND candidato_raw.codigo_ocr      = d.codigo_proveedor
              AND candidato_raw.catalogo_origen = d.catalogo_origen
        )
    """, (payload["costo_neto"], payload.get("moneda", "USD"), candidato_id_staging))
    c.close()
    return {"ok": True, "candidato_id_permanente": candidato_id_permanente}


def api_fijar_precio_manual(candidato_id: int, precio: float | None) -> dict:
    """Plan 2026-09-07, paso 5: el comprador escribe a mano el precio de
    venta esperado de un candidato -- gana siempre sobre cualquier estimación
    (ver `precio_venta_referencia`). `precio=None` o <=0 lo BORRA (vuelve a
    la cascada automática) en vez de tratarlo como un precio de cero.

    FASE 2: 100% local, y con una ganancia concreta -- antes este precio vivía
    en una tabla que se borraba entera al cargar otro catálogo, y por eso
    `gui_profesional_ctk.py` tenía que guardarlo en DOS lugares y volver a
    aplicarlo al retomar el lote. Ahora vive donde vive el lote."""
    _cl().execute("""
        UPDATE candidato SET precio_venta_manual = ?
        WHERE candidato_id = ?
    """, (precio if precio and precio > 0 else None, candidato_id))
    return {"ok": True}


# ---------- estacionalidad: mes de venta esperado -> factor por categoría ----
#
# De dónde sale (investigado 2026-09-09, para no inventar una fórmula nueva):
# el Asistente de Compras de TEC ya resuelve esto y su fuente es
# `gold.vw_senal_tipo`, columnas `idx_01..idx_12` -- el share promedio de pares
# IMPORTADOS a Costa Rica en ese mes por tipo de calzado, normalizado a 1.0 =
# promedio del año, y YA desplazado de mes de importación a mes de VENTA con el
# rezago de cada tipo (escolar 3 meses, sandalia/bota/futbol/outdoor 2, resto 1).
# Es señal de MERCADO agregado (aduana CR), no de una marca, así que aplica
# igual a la línea TUCALZADO.
#
# Dónde lo multiplica TEC: NO en el reparto de pares por talla, sino un nivel
# arriba, en la cuota del segmento -- `cuotaAjust = cuota × mktFactor × seaIdx`
# (ver `_calcOptimizacion` en `AsistenteComprasTEC/data/catalogo_tec.html`).
# Acá el equivalente exacto de "la cuota del segmento" es el peso con el que
# cada candidato se lleva parte del PDV objetivo, y ahí es donde se multiplica.
#
# Qué NO se pudo copiar, y por qué: TEC amortigua el índice con `seaStrength`,
# que mide la amplitud estacional del historial de ventas propio de ESE
# segmento, y lo mezcla con el índice de ventas propio (peso ≤ 0.10). Para
# TUCALZADO ese historial no alcanza: `cfg.lkp_tuc_estacionalidad` existe pero
# está vacía y declarada DIFERIDA ("requiere >12 meses de historia"), y
# `gold.agg_tuc_metricas` no tiene ninguna columna mensual. Sin amortiguador,
# los índices crudos son brutales (bota 4.64 en enero, escolar 4.30 en abril) y
# un pedido entero se iría a una sola categoría. Se acota entonces el factor a
# [0.60, 1.60]: conserva el ORDEN y el signo de la señal real (qué categoría
# conviene y cuál no para ese mes) sin dejar que una sola la monopolice. Es el
# mismo criterio con el que TEC acota su `mktFactor` a [0.82, 1.22], con banda
# más ancha porque acá el factor es lo único estacional del cálculo.
FACTOR_ESTACIONAL_MIN = 0.60
FACTOR_ESTACIONAL_MAX = 1.60

# `dim_candidato.categoria_declarada` (vocabulario TUC, el de los desplegables
# del asistente) -> `tipo` de `gold.vw_senal_tipo` (vocabulario aduana).
# Se usa la CATEGORÍA y no `raw_catalogo.tipo_norm` como `_factor_mercado`: en
# el flujo del asistente `tipo_norm` no se puebla (por eso ese factor es inerte
# ahí), mientras que la categoría siempre está --la calcula el nearest-centroid
# y el comprador la puede corregir a mano en el paso 6--.
# None = sin señal utilizable para esa categoría; queda en factor neutro.
CATEGORIA_TUC_A_TIPO_ADUANA: dict[str, str | None] = {
    "Sandalias": "sandalia",
    "Casual": "lifestyle",     # 'casual' existe en la vista pero no es confiable
    "Deportivos": "running",
    "Botas y botines": "bota",
    "Tacones": "tacon",        # hoy no confiable -> neutro, sin romper nada
    "Futbol": "futbol",
    "Escolar": "escolar",
    "Formal": None,
    "Cuñas y plataformas": None,
}


def indices_estacionales_mes(cur, mes: int) -> dict[str, float]:
    """`{tipo_aduana: idx}` para UN mes de venta, solo de las señales marcadas
    `confiable` en `gold.vw_senal_tipo`. Lo no confiable no se devuelve: quien
    consulta cae a 1.0 (neutro), que es lo que hace TEC también."""
    if not isinstance(mes, int) or not 1 <= mes <= 12:
        return {}
    # El mes es un entero ya validado en el rango 1-12: no hay entrada de
    # usuario en el nombre de la columna.
    cur.execute(f"SELECT tipo, idx_{mes:02d} FROM gold.vw_senal_tipo WHERE confiable")
    return {t: float(v) for t, v in cur.fetchall() if v is not None}


def factor_estacional(indices: dict[str, float], categoria: str | None) -> float:
    """El factor con el que se ajusta el peso de un candidato para el mes
    elegido. 1.0 (neutro) cuando no hay categoría, no hay mapeo a un tipo de
    aduana, o ese tipo no tiene señal confiable."""
    tipo = CATEGORIA_TUC_A_TIPO_ADUANA.get((categoria or "").strip()) if categoria else None
    if not tipo or tipo not in indices:
        return 1.0
    return max(FACTOR_ESTACIONAL_MIN, min(FACTOR_ESTACIONAL_MAX, indices[tipo]))


def api_pedido_sugerido(pdv_objetivo: float, mes_venta: int | None = None) -> dict:
    """Distribuye un PDV objetivo (monto en colones que el comprador quiere
    invertir en este catálogo) entre los candidatos del catálogo EN REVISIÓN,
    proporcional a su score_final -- plan 2026-09-04, paso 8, última parte.

    Por qué proporcional al score y no a otra cosa: score_final ya integra
    similitud visual/precio contra lo que TU Calzado vende + demanda +
    tendencia + señal de importación (pasos 2-7) -- es la única variable que
    ya resume "qué tan buena apuesta es este candidato" para TODOS los
    candidatos, incluyendo los que solo tienen respaldo por precio (paso 8,
    primera parte). Excluye candidatos con score_final NULL (sin_comparables
    -- ni visual ni precio encontraron nada, no hay base para asignarles
    dinero) en vez de darles una porción arbitraria.

    cantidad_sugerida = valor_asignado / precio_venta_referencia, redondeada al
    múltiplo de empaque_cantidad más cercano (confirmado con el usuario
    2026-09-07) -- un pedido a un proveedor de calzado casi siempre viene en
    cajas/docenas fijas, pedir "7 pares" cuando el empaque es de a 12 no es
    ejecutable.

    El precio de venta viene de `precio_venta_referencia` (plan 2026-09-07,
    paso 4) -- cascada manual > costo declarado > similares TUC > comparables
    de marca > mediana de categoría, en vez de depender solo del costo
    declarado (que en la práctica casi nunca viene: 0 de 74 candidatos reales
    en la prueba del catálogo Reebok tenían costo, lo que dejaba esta pestaña
    sin generar ni una cantidad). Si NINGÚN nivel de la cascada tiene con qué
    calcular nada, el candidato se devuelve con valor_asignado pero
    cantidad_sugerida=None, en vez de inventar un precio.

    FASE 2: los candidatos, sus scores y su empaque salen del `lote.sqlite`;
    el nombre del proveedor sigue viniendo del maestro permanente en Postgres,
    igual que en `api_candidatos`. El reparto del PDV en sí no cambia."""
    cl = _cl()
    cl.execute("""
        SELECT c.candidato_id, c.proveedor_id, c.codigo_proveedor,
               c.categoria_declarada, c.genero_canonico,
               s.score_final, s.clasificacion, r.empaque_cantidad
        FROM candidato c
        JOIN candidato_score s ON s.candidato_id = c.candidato_id
        LEFT JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor
               AND r.catalogo_origen = c.catalogo_origen
        WHERE s.score_final IS NOT NULL
        ORDER BY s.score_final DESC
    """)
    candidatos = [dict(row) for row in cl.fetchall()]

    c = conn(); cur = c.cursor()
    prov_ids = sorted({f["proveedor_id"] for f in candidatos if f["proveedor_id"] is not None})
    nombres_proveedor = {}
    if prov_ids:
        cur.execute("SELECT proveedor_id, nombre FROM silver.dim_tuc_proveedor "
                    "WHERE proveedor_id = ANY(%s)", (prov_ids,))
        nombres_proveedor = dict(cur.fetchall())
    # El `JOIN` de antes descartaba el candidato sin proveedor en el maestro.
    candidatos = [f for f in candidatos if f["proveedor_id"] in nombres_proveedor]
    for f in candidatos:
        f["proveedor"] = nombres_proveedor[f.pop("proveedor_id")]

    # Ajuste por MES DE VENTA esperado (opcional, ver `factor_estacional`
    # arriba): `mes_venta=None` deja el cálculo exactamente como estaba --el
    # peso es el score puro-- para no cambiarle el resultado a ningún llamador
    # que no pida temporada (la interfaz web, entre otros).
    indices = indices_estacionales_mes(cur, mes_venta) if mes_venta else {}
    for f in candidatos:
        f["_factor_estacional"] = (factor_estacional(indices, f["categoria_declarada"])
                                    if indices else 1.0)

    # El peso se normaliza sobre score×factor, no sobre el score: así el PDV
    # objetivo se reparte COMPLETO igual que antes (los factores mueven la
    # proporción entre candidatos, no el total invertido).
    suma_score = sum(float(f["score_final"]) * f["_factor_estacional"] for f in candidatos)
    resultado = []
    monto_asignado_total = 0.0
    for f in candidatos:
        score = float(f["score_final"])
        peso = (score * f["_factor_estacional"] / suma_score) if suma_score > 0 else 0.0
        valor_asignado = pdv_objetivo * peso

        precio_venta, fuente_precio = precio_venta_referencia(cur, f["candidato_id"])
        empaque = f["empaque_cantidad"] if f["empaque_cantidad"] and f["empaque_cantidad"] > 0 else 1

        cantidad_sugerida = None
        monto_real = None
        if precio_venta:
            unidades_crudas = valor_asignado / precio_venta
            multiplos = round(unidades_crudas / empaque)
            cantidad_sugerida = max(multiplos, 1) * empaque if multiplos > 0 else 0
            monto_real = cantidad_sugerida * precio_venta

        resultado.append({
            "candidato_id": f["candidato_id"], "proveedor": f["proveedor"],
            "codigo_proveedor": f["codigo_proveedor"],
            "categoria": f["categoria_declarada"], "genero": f["genero_canonico"],
            "score_final": round(score, 4), "clasificacion": f["clasificacion"],
            "factor_estacional": round(f["_factor_estacional"], 4),
            "peso_pct": round(peso * 100, 2),
            "valor_asignado": round(valor_asignado, 2),
            "precio_venta_estimado": round(precio_venta, 2) if precio_venta else None,
            "precio_fuente": fuente_precio,
            "empaque_cantidad": empaque,
            "cantidad_sugerida": cantidad_sugerida,
            "monto_real": round(monto_real, 2) if monto_real is not None else None,
        })
        if monto_real is not None:
            monto_asignado_total += monto_real
    c.close()

    n_sin_precio = sum(1 for r in resultado if r["cantidad_sugerida"] is None)
    n_estacional = sum(1 for r in resultado if r["factor_estacional"] != 1.0)
    return {
        "pdv_objetivo": pdv_objetivo,
        "mes_venta": mes_venta,
        "n_con_factor_estacional": n_estacional,
        "candidatos": resultado,
        "monto_real_total": round(monto_asignado_total, 2),
        "n_candidatos": len(resultado),
        "n_sin_precio": n_sin_precio,  # tuvieron score pero no costo/moneda utilizable
    }


# ---------- lecturas para la API (todo desde staging_tuc, el catálogo activo) ----------

def api_candidatos() -> list[dict]:
    """FASE 2: el candidato, su score, su empaque y su variante 0 salen del
    `lote.sqlite`; lo ÚNICO que sigue viniendo de Postgres es el NOMBRE del
    proveedor (`silver.dim_tuc_proveedor`), que es maestro permanente de la
    empresa y no dato de este lote.

    Antes era un solo JOIN de 5 tablas porque las cinco vivían en la misma
    base. Ahora son dos consultas: el JOIN local (4 tablas) y una resolución
    de nombres por lote de `proveedor_id`. Se conserva el `JOIN` (no `LEFT
    JOIN`) contra el proveedor: un candidato con un `proveedor_id` que no
    existe en el maestro se sigue omitiendo, igual que antes."""
    _asegurar_color_capellada()
    cl = _cl()
    cl.execute("""
        SELECT c.candidato_id, c.proveedor_id, c.linea_declarada, c.codigo_proveedor,
               c.catalogo_origen, c.categoria_declarada, c.genero_canonico AS genero_declarado,
               c.n_variantes_color, c.es_multicolor,
               c.n_colores_detectados, c.colores_detectados, c.posibles_mismo_modelo,
               c.tiene_tacon, c.tiene_plataforma, c.advertencia_multiples_pares,
               v0.color_principal, v0.color_suela, v0.suela_contraste, v0.color_suela_delta_e, v0.color_plantilla,
               s.score_final, s.clasificacion AS grado,
               r.empaque_cantidad, r.empaque_unidad, r.marca_declarada
        FROM candidato c
        LEFT JOIN candidato_score s ON s.candidato_id = c.candidato_id
        LEFT JOIN candidato_raw r
            ON r.proveedor_id = c.proveedor_id AND r.codigo_ocr = c.codigo_proveedor AND r.catalogo_origen = c.catalogo_origen
        LEFT JOIN candidato_variante v0 ON v0.candidato_id = c.candidato_id AND v0.indice = 0
        ORDER BY s.score_final DESC NULLS LAST
    """)
    filas = [dict(row) for row in cl.fetchall()]

    c = conn(); cur = c.cursor()
    prov_ids = sorted({f["proveedor_id"] for f in filas if f["proveedor_id"] is not None})
    nombres_proveedor = {}
    if prov_ids:
        cur.execute("SELECT proveedor_id, nombre FROM silver.dim_tuc_proveedor "
                    "WHERE proveedor_id = ANY(%s)", (prov_ids,))
        nombres_proveedor = dict(cur.fetchall())
    # El `JOIN` de antes descartaba la fila sin proveedor en el maestro: se
    # replica el mismo descarte en Python, en el mismo lugar del cálculo.
    filas = [f for f in filas if f["proveedor_id"] in nombres_proveedor]
    for f in filas:
        # FASE 3 multi-proveedor (2026-09-16): `proveedor_id` YA NO se descarta
        # (antes era un `pop`). La pantalla 6 necesita saber de qué proveedor es
        # cada candidato para aplicarle SU moneda/tipo de cambio/margen, que en
        # un lote con dos catálogos pueden ser distintos. El nombre se sigue
        # agregando igual, así que nada de lo que ya leía `proveedor` cambia.
        f["proveedor"] = nombres_proveedor[f["proveedor_id"]]
        for campo in ("es_multicolor", "tiene_tacon", "tiene_plataforma",
                      "advertencia_multiples_pares", "suela_contraste"):
            f[campo] = almacen.booleano(f[campo])

    # Tira de colores -- pedido explícito del usuario, ABANDONADA la reconstrucción de
    # color (auditoría Opus 2026-07-24): resultó ser 100% cosmética (la búsqueda de
    # comparables ya usa el embedding de la foto REAL de cada variante, nunca el
    # reconstruido) y el subsistema más frágil del proyecto (rondas de bugs de color en
    # calzado multicolor). Ahora cada color de la Referencia muestra directamente su
    # propia foto real recortada -- nunca puede tener el color mal, es la foto real.
    ids_filas = [f["candidato_id"] for f in filas]
    por_candidato = {}
    # `IN ()` con la lista vacía es error de sintaxis en SQLite (Postgres sí
    # tragaba `= ANY('{}')`), así que la lista vacía se corta antes.
    if ids_filas:
        cl.execute(f"""
            SELECT candidato_id, indice, color_principal, variante_id
            FROM candidato_variante
            WHERE candidato_id IN {_en(ids_filas)} AND imagen_limpia_path IS NOT NULL
            ORDER BY candidato_id, indice
        """, ids_filas)
        filas_variante = cl.fetchall()
    else:
        filas_variante = []
    # El score de CADA color (2026-09-17). Se lee aparte de `candidato_score`
    # porque esa tabla sigue siendo la de la unidad de compra; acá se arma la
    # tira de colores con su calificación individual y, más abajo, el promedio
    # SIMPLE de la referencia completa (decisión del dueño: todos los colores
    # pesan igual).
    scores_variante = {}
    if ids_filas:
        cl.execute(f"""
            SELECT candidato_id, indice, score_final, clasificacion
            FROM candidato_score_variante
            WHERE candidato_id IN {_en(ids_filas)}
        """, ids_filas)
        scores_variante = {(cid, ind): (sc, cls) for cid, ind, sc, cls in cl.fetchall()}

    for candidato_id, indice, color_principal, variante_id in filas_variante:
        score_v, grado_v = scores_variante.get((candidato_id, indice), (None, None))
        por_candidato.setdefault(candidato_id, []).append({
            "indice": indice, "color_principal": color_principal,
            "url": f"/crop_variante/{variante_id}",
            # None = ese color todavía no tiene número; `grado` dice por qué
            # ("sin_vector" = falta vectorizarlo, "sin_comparables" = no hay
            # con qué compararlo). Nunca se rellena con 0.
            "score_final": float(score_v) if score_v is not None else None,
            "grado": grado_v,
        })

    for f in filas:
        for k, v in f.items():
            if hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
                f[k] = float(v)
        f["costo_neto"] = None  # staging nunca tiene cotización -- eso vive solo tras promover
        f["moneda"] = None
        f["tipo_cambio_usd_crc"] = None
        f["colores_vista"] = por_candidato.get(f["candidato_id"], [])
        # Calificación de la REFERENCIA COMPLETA: promedio SIMPLE de sus
        # colores (2026-09-17, decisión explícita del dueño). Solo se expone
        # cuando la referencia trae MÁS DE UN color: con un solo color el
        # promedio es el mismo `score_final` de siempre y la tarjeta no debe
        # cambiar en nada.
        colores = f["colores_vista"]
        if len(colores) > 1:
            f["score_ponderado"] = promedio_simple_variantes(
                [col.get("score_final") for col in colores])
            f["grado_ponderado"] = clasificar_score(f["score_ponderado"])
            f["colores_sin_calificar"] = [
                {"color_principal": col.get("color_principal"), "grado": col.get("grado")}
                for col in colores if col.get("score_final") is None]
        else:
            f["score_ponderado"] = None
            f["grado_ponderado"] = None
            f["colores_sin_calificar"] = []
    c.close()
    return filas


def api_marcas_reconocidas() -> list[str]:
    """Las marcas que TU Calzado considera "mundialmente reconocidas", o sea
    las que se venden por la línea TC Marcas -- `silver.dim_marca` (32 filas
    activas al 2026-09-10: ADIDAS, NIKE, PUMA, VANS, HOKA, DC SHOES...).

    Es la ÚNICA lista gobernada de marcas reconocidas que existe hoy en la
    base. No es exhaustiva: OSIRIS, el ejemplo que reportó el usuario, NO está
    en ella (verificado 2026-09-10) -- por eso el control de la pantalla
    acepta escribir una marca que no esté en la lista, en vez de limitar al
    comprador a lo que el maestro ya conoce."""
    c = conn(); cur = c.cursor()
    try:
        cur.execute("SELECT marca FROM silver.dim_marca WHERE activo ORDER BY marca")
        return [r[0] for r in cur.fetchall()]
    finally:
        c.close()


def api_fijar_marca_declarada(candidato_id: int, marca: str | None) -> dict:
    """El comprador declara A MANO de qué marca es una referencia del catálogo
    que está revisando -- Tarea 7, 2026-09-10.

    POR QUÉ A MANO. El usuario reportó que OSIRIS (marca mundialmente
    reconocida) se estaba comparando contra el catálogo genérico de TU
    Calzado. Investigado sobre el lote real (`Prueba2`, proveedor Cachos, 18
    referencias): NINGUNA trae la marca en ningún campo -- `marca_declarada`,
    `tipo_norm` y `color_declarado` están todos en NULL, y `texto_crudo` dice
    literalmente "[sin OCR] código del nombre de archivo: 09-160906-1_2.jpg"
    porque las fotos salieron de un Excel y el código se derivó del nombre de
    archivo. No hay texto de catálogo que mirar. Y la lista gobernada
    (`silver.dim_marca`, 32 marcas) tampoco tiene OSIRIS. O sea: con lo que
    existe hoy, la detección automática es IMPOSIBLE para este catálogo, y la
    entrada manual del comprador es la única señal honesta disponible. La
    regla de memoria ya establecida (nunca deducir marca de aduana/fábrica) se
    respeta: esto no toca ningún dato de importación.

    Escribe en `candidato_raw.marca_declarada` del `lote.sqlite` -- la columna
    que YA existía y que YA consume `_factor_mercado` para buscar la señal de
    importación por marca (`gold.vw_senal_marca`). O sea que declarar la marca
    no solo cambia qué comparables se destacan: también hace que el score
    considere la tendencia real de importación de esa marca, que hasta ahora
    quedaba en el factor neutro 1.0 por falta del dato.

    `marca=None` o vacío borra la declaración (vuelve a genérico)."""
    marca = (marca or "").strip().upper() or None
    cl = _cl()
    cl.execute("""
        UPDATE candidato_raw
        SET marca_declarada = ?
        WHERE EXISTS (
            SELECT 1 FROM candidato d
            WHERE d.candidato_id = ?
              AND candidato_raw.proveedor_id    = d.proveedor_id
              AND candidato_raw.codigo_ocr      = d.codigo_proveedor
              AND candidato_raw.catalogo_origen = d.catalogo_origen
        )
        RETURNING id
    """, (marca, candidato_id))
    if cl.fetchone() is None:
        raise ValueError(
            f"candidato_id={candidato_id} no tiene fila en la revisión de este lote "
            "(¿se reemplazó el catálogo?)")
    return {"ok": True, "marca_declarada": marca}


# Nota (plan de mejora 2026-09-22, Etapa 6): sin llamador en la app de
# escritorio -- servidor_pty.py define su propia copia y la expone en
# /api/variantes. No se borra.
def api_variantes(candidato_id: int) -> list[dict]:
    """Todas las variantes (colores) de una misma Referencia (codigo_proveedor) --
    pedido explícito del usuario 2026-07-23: si la foto trae 3 colores, mostrarlos
    como 3 fichas independientes, cada una con su propio color/suela/contraste, dejando
    claro visualmente que son la MISMA referencia (mismo candidato_id/encabezado).

    FASE 2: 100% local -- esta función ya no toca Postgres para nada."""
    cl = _cl()
    cl.execute("""
        SELECT variante_id, indice, color_principal, color_suela, suela_contraste, color_suela_delta_e,
               detalle_flor_path, color_plantilla
        FROM candidato_variante
        WHERE candidato_id = ? AND imagen_limpia_path IS NOT NULL
        ORDER BY indice
    """, (candidato_id,))
    filas = [dict(row) for row in cl.fetchall()]
    for f in filas:
        if f["color_suela_delta_e"] is not None:
            f["color_suela_delta_e"] = float(f["color_suela_delta_e"])
        # `suela_contraste` es boolean en Postgres e INTEGER en SQLite: se
        # devuelve como True/False/None para que la interfaz (y el comparador
        # de snapshots) vea exactamente el mismo valor que antes.
        f["suela_contraste"] = almacen.booleano(f["suela_contraste"])
    return filas


def api_desglose_score(candidato_id: int) -> dict | None:
    """Plan 2026-09-07, paso 9 (ampliado): explica un grado en una frase
    reconstruible a mano, más la lista real de vecinos que lo produjeron --
    la defensa permanente contra "las calificaciones son muy altas" es que
    CUALQUIER grado se pueda auditar en un clic, no solo confiar en el
    número. None si el candidato todavía no tiene score (sin_comparables o
    nunca puntuado).

    FASE 2: el score y sus factores salen del `lote.sqlite`; las FOTOS y las
    UNIDADES VENDIDAS de los vecinos siguen saliendo de Postgres, porque son
    datos del catálogo permanente de TU Calzado (qué se vendió y con qué
    imagen), no del catálogo en revisión."""
    cl = _cl()
    cl.execute("""
        SELECT score_final, clasificacion, sim_ponderada, k_efectivo, f_soporte, f_tipo,
               f_color, f_atrib, f_demanda, factor_mercado, filtro_aplicado, vecinos_detalle,
               version_formula
        FROM candidato_score WHERE candidato_id = ?
    """, (candidato_id,))
    r = cl.fetchone()
    if not r or r[0] is None:
        return None
    (score_final, clasificacion, sim_ponderada, k_efectivo, f_soporte, f_tipo, f_color,
     f_atrib, f_demanda, factor_mercado, filtro_aplicado, vecinos_detalle, version_formula) = tuple(r)
    # `vecinos_detalle` era `jsonb` (psycopg2 lo entregaba ya deserializado) y
    # acá es TEXT: se deserializa a mano para que el resto de la función siga
    # recibiendo la misma lista de dicts.
    vecinos_detalle = json.loads(vecinos_detalle) if vecinos_detalle else None

    c = conn(); cur = c.cursor()
    # Los vecinos del canal TC Marcas no tienen `codigo_tuc` (no existe en el
    # dominio 'marca'): se identifican por marca+referencia del fabricante y su
    # foto/ventas se buscan en sus propias tablas. `.get` en vez de `[...]`
    # porque esta lista ahora puede venir de cualquiera de los dos canales.
    codigos = [v.get("codigo_tuc") for v in (vecinos_detalle or []) if v.get("codigo_tuc")]
    # La llave es la REFERENCIA del fabricante, no marca+referencia: en el
    # índice TI del dominio marca la marca viene de `dim_tcm_producto.marca_tec`
    # y está vacía en la mayoría de las filas, así que exigirla dejaba a casi
    # todos los vecinos sin foto ni unidades. La referencia sí está siempre, y
    # `fct_embedding_imagen` la trae con su marca real: se aprovecha para
    # rellenar la marca del vecino cuando el índice no la tenía.
    refs_marca = [v.get("referencia") for v in (vecinos_detalle or [])
                  if v.get("dominio") == "marca" and v.get("referencia")]
    fotos_y_ventas_marca = {}
    if refs_marca:
        # Unidades vendidas: directo de `fct_tcm_ventas` por referencia, SIN
        # pasar por el índice de fotos. El índice de fotos del dominio marca y
        # nuestro catálogo TC Marcas se solapan poco (513 de 8,436 filas), así
        # que pedir las ventas "a través" del índice dejaba en cero a vecinos
        # que sí tienen ventas propias -- y son justo las que alimentan
        # f_demanda.
        cur.execute("""
            SELECT referencia, SUM(unidades_netas) FROM silver.fct_tcm_ventas
            WHERE referencia = ANY(%s) GROUP BY referencia
        """, (refs_marca,))
        unidades_por_ref = {ref: int(u or 0) for ref, u in cur.fetchall()}
        # Foto: mejor esfuerzo contra el índice de imágenes del dominio marca.
        # Cuando el comparable es un producto de nuestro catálogo que no está
        # en ese índice, queda sin miniatura (misma limitación que ya tiene
        # `api_similar_marca` en su rama TI), no con una foto equivocada.
        cur.execute("""
            SELECT s.referencia, MIN(s.imagen_id), MIN(s.marca)
            FROM silver.fct_embedding_imagen s
            WHERE s.dominio = 'marca' AND s.referencia = ANY(%s)
            GROUP BY s.referencia
        """, (refs_marca,))
        fotos_por_ref = {ref: (img, marca_v) for ref, img, marca_v in cur.fetchall()}
        for ref_v in set(refs_marca):
            imagen_id, marca_v = fotos_por_ref.get(ref_v, (None, None))
            fotos_y_ventas_marca[ref_v] = (imagen_id, marca_v, unidades_por_ref.get(ref_v, 0))
    fotos_y_ventas = {}
    if codigos:
        cur.execute("""
            SELECT s.codigo_tuc, s.imagen_id, SUM(v.unidades_netas)
            FROM silver.fct_embedding_imagen s
            LEFT JOIN silver.fct_tuc_ventas v ON v.codigo_tuc = s.codigo_tuc
            WHERE s.codigo_tuc = ANY(%s) AND s.dominio = 'tuc'
            GROUP BY s.codigo_tuc, s.imagen_id
        """, (codigos,))
        for cod, imagen_id, unidades in cur.fetchall():
            fotos_y_ventas.setdefault(cod, (imagen_id, int(unidades or 0)))
    c.close()

    vecinos_enriquecidos = []
    for v in (vecinos_detalle or []):
        if v.get("dominio") == "marca":
            imagen_id, marca_real, unidades = fotos_y_ventas_marca.get(
                v.get("referencia"), (None, None, 0))
            vecinos_enriquecidos.append({**v, "imagen_id": imagen_id,
                                          "marca": v.get("marca") or marca_real,
                                          "unidades_vendidas": unidades})
            continue
        imagen_id, unidades = fotos_y_ventas.get(v.get("codigo_tuc"), (None, 0))
        vecinos_enriquecidos.append({**v, "imagen_id": imagen_id, "unidades_vendidas": unidades})

    # La frase reconstruible: cada término, con su etiqueta y su valor --
    # multiplicarlos en el orden mostrado da score_final (redondeo aparte).
    # score_base se despeja dividiendo score_final entre los factores ya
    # conocidos, en vez de recalcularlo con ANCLA_SIMILITUD/UMBRAL_POR_MODELO
    # -- así la frase queda correcta también para el respaldo por precio
    # (metodo_score="precio"), que usa un umbral/ancla distintos.
    divisor = float(f_soporte) * float(f_tipo) * float(f_color) * float(f_atrib) * float(f_demanda) * (float(factor_mercado) or 1.0)
    score_base = round(float(score_final) / divisor, 1) if divisor else None
    frase = (f"Similitud ponderada {float(sim_ponderada):.3f} → base "
             f"{score_base if score_base is not None else '?'} pts")
    partes = [
        frase,
        f"{k_efectivo:.1f} similar(es) efectivo(s) ×{float(f_soporte):.2f}",
        f"tipo ({filtro_aplicado or '—'}) ×{float(f_tipo):.2f}",
        f"color ×{float(f_color):.2f}",
        f"atributos ×{float(f_atrib):.2f}",
        f"demanda de similares ×{float(f_demanda):.2f}",
    ]
    if factor_mercado and abs(float(factor_mercado) - 1.0) > 0.001:
        partes.append(f"señal de importación ×{float(factor_mercado):.2f}")
    partes.append(f"→ {float(score_final):.1f} = grado {clasificacion}")

    return {
        "score_final": float(score_final), "clasificacion": clasificacion,
        "sim_ponderada": float(sim_ponderada), "k_efectivo": float(k_efectivo),
        "f_soporte": float(f_soporte), "f_tipo": float(f_tipo), "f_color": float(f_color),
        "f_atrib": float(f_atrib), "f_demanda": float(f_demanda),
        "factor_mercado": float(factor_mercado) if factor_mercado else 1.0,
        "filtro_aplicado": filtro_aplicado, "version_formula": version_formula,
        "frase": "  ·  ".join(partes),
        "vecinos": vecinos_enriquecidos,
    }


# Nota (plan de mejora 2026-09-22, Etapa 6): sin llamador en la app de
# escritorio -- servidor_pty.py define su propia copia y la expone en
# /api/candidatos, junto a api_candidatos(). No se borra.
def api_margen_global() -> float:
    c = conn(); cur = c.cursor()
    cur.execute("SELECT valor FROM gold.vw_tuc_parametro_vigente WHERE clave = 'margen_markup_global'")
    r = cur.fetchone(); c.close()
    return float(r[0]) if r else 0.80



UMBRAL_COBERTURA_DEGRADADA = 0.5  # por debajo de esto, el recorte de esta variante se
# considera potencialmente roto (esquirla en vez de calzado completo -- ver hallazgo real
# 2026-07-23 con foto de 3 pares donde 2 de 3 recortes salieron incompletos). v1, sin
# calibrar contra muchos casos reales -- ajustar si genera falsos positivos/negativos.


def _variante_saludable(candidato_id: int, indice: int):
    """Si la variante `indice` de este candidato tiene un recorte SOSPECHOSO de estar
    incompleto (cobertura_mascara baja), busca la variante HERMANA (mismo candidato_id =
    misma Referencia, otro color) con mejor cobertura para usar SU embedding/color al
    buscar comparables -- pedido explícito del usuario: "es posible completar una mala
    foto de un color usando una buena foto de otro color?". NO fusiona píxeles de las dos
    imágenes (eso produciría una imagen sintética engañosa) -- la variante degradada
    sigue MOSTRÁNDOSE tal cual es; solo la BÚSQUEDA de comparables toma prestados los
    datos de la hermana sana. Retorna (indice_a_usar, uso_alternativa: bool).

    FASE 2: lee del `lote.sqlite` del lote activo, ya no de `staging_tuc`."""
    cl = _cl()
    cl.execute("""
        SELECT indice, cobertura_mascara FROM candidato_variante
        WHERE candidato_id = ? ORDER BY indice
    """, (candidato_id,))
    filas = cl.fetchall()
    propia = next((f for f in filas if f[0] == indice), None)
    # cobertura_mascara NULL = camino de "1 solo calzado" (sin el bug de esquirla, no
    # aplica el chequeo) o variante sin procesar todavía -- en ambos casos usar tal cual.
    if propia is None or propia[1] is None or float(propia[1]) >= UMBRAL_COBERTURA_DEGRADADA:
        return indice, False
    candidatas = [f for f in filas if f[0] != indice and f[1] is not None and float(f[1]) >= UMBRAL_COBERTURA_DEGRADADA]
    if not candidatas:
        return indice, False
    mejor = max(candidatas, key=lambda f: float(f[1]))
    return mejor[0], True


def _vector_variante(candidato_id: int, indice: int = 0, modelo: str = "fashion_siglip"):
    """FASE 2: lee del `lote.sqlite` del lote activo, ya no de `staging_tuc`.

    Devuelve una tupla `(vector,)` -- misma forma que devolvía `cur.fetchone()`
    sobre una columna `real[]` de Postgres -- para que los llamadores
    (`api_similar_marca`/`api_similar_tuc`, que hacen `r[0]`) no cambien.
    """
    cl = _cl()
    cl.execute("""
        SELECT e.vector FROM candidato_embedding e
        JOIN candidato_variante v ON v.variante_id = e.variante_id
        WHERE v.candidato_id = ? AND v.indice = ? AND e.modelo_embedding = ?
    """, (candidato_id, indice, modelo))
    r = cl.fetchone()
    return (almacen.blob_a_vector(r[0]),) if r else None


# ---------------------------------------------------------------------------
# Color en la lista de comparables (2026-09-09)
#
# Bug real reportado por el usuario: "me compara un calzado con productos de
# OTRO COLOR". Era cierto, y el diagnóstico es que había DOS caminos con
# criterios distintos:
#
#   - `puntuar_candidatos` (el score/grado del candidato) SÍ mira el color:
#     `f_color` es uno de los factores de `score_final`.
#   - `api_similar_marca`/`api_similar_tuc` (la LISTA de comparables que el
#     comprador ve en pantalla) filtraban por género y por tipo/categoría,
#     pero NO por color: el ranking era similitud de embedding pura dentro de
#     ese filtro. Y los embeddings de imagen no separan bien el color del
#     molde, así que el vecino más cercano de un tenis blanco es muchas veces
#     el mismo modelo en negro.
#
# El criterio que se aplica acá es el MISMO que ya usa el filtro de tipo unas
# líneas más abajo, que salió de un pedido explícito del usuario ("es mejor no
# comparar con productos que no sean comparables"): se prefiere duro el mismo
# color, y solo si eso deja la lista vacía se degrada -- pero nunca en
# silencio: cada comparable devuelve su `color_familia` y un `mismo_color`
# booleano, para que la pantalla pueda decir "otro color" en vez de hacer
# pasar un negro por un blanco.
#
# No se inventa una fórmula nueva: la familia de color se resuelve con la
# misma tabla `cfg.lkp_color_candidato_a_familia` que ya usa `f_color` en
# `puntuar_candidatos`.


def _mapa_color_familia(cur) -> dict:
    """`cfg.lkp_color_candidato_a_familia` como dict. Los nombres crudos de
    color son los mismos en los tres lados (variante de candidato,
    `fct_color_imagen.color_detectado` de marca, y las claves de esta tabla):
    minúsculas sin tilde, "negro"/"gris"/"marron"."""
    cur.execute("SELECT color_candidato, color_familia FROM cfg.lkp_color_candidato_a_familia WHERE activo")
    return dict(cur.fetchall())


def _familia_color_variante(candidato_id: int, indice: int, mapa: dict) -> str | None:
    """Familia de color de LA VARIANTE que se está mirando -- no la variante 0.

    Importa que sea la variante en pantalla: el candidato puede venir en tres
    colores y el comprador está viendo uno. (`puntuar_candidatos` sí usa la
    variante 0 a propósito, porque el score es por candidato, no por color.)

    FASE 2: el color de la variante sale del `lote.sqlite`; el `mapa` de
    familias lo sigue trayendo `_mapa_color_familia` desde `cfg` en Postgres
    (tabla de gobernanza permanente, no dato de este lote)."""
    cl = _cl()
    cl.execute("""
        SELECT color_principal FROM candidato_variante
        WHERE candidato_id = ? AND indice = ?
    """, (candidato_id, indice))
    r = cl.fetchone()
    return mapa.get(r[0]) if r and r[0] else None


def _familias_por_imagen(cur, imagen_ids: list, mapa: dict) -> dict:
    """imagen_id -> familia de color, desde `silver.fct_color_imagen` (el color
    dominante ya extraído de la foto real). Se resuelve en UNA consulta para
    todo el índice en vez de tocar las 4 consultas de índice existentes."""
    ids = [i for i in imagen_ids if i is not None]
    if not ids:
        return {}
    cur.execute("SELECT imagen_id, color_detectado FROM silver.fct_color_imagen WHERE imagen_id = ANY(%s)",
                (ids,))
    return {int(iid): mapa.get(col) for iid, col in cur.fetchall() if col}


def _familias_por_codigo_tuc(cur, codigos: list) -> dict:
    """codigo_tuc -> familia de color. `bronze.raw_tuc_atributos.color_familia`
    YA viene en familias ("Negro", "Café/Tierra"), no en nombres crudos, así
    que no pasa por el mapa."""
    cods = [c for c in codigos if c]
    if not cods:
        return {}
    cur.execute("""
        SELECT codigo, color_familia FROM bronze.raw_tuc_atributos
        WHERE codigo = ANY(%s) AND color_familia IS NOT NULL
    """, (cods,))
    return dict(cur.fetchall())


def _preferir_mismo_color(orden, familias, familia_candidato, sims, umbral):
    """Reordena el top para que el MISMO color de familia vaya primero.

    Devuelve (orden_nuevo, se_degrado). `se_degrado` es True cuando no había
    ningún comparable del mismo color por encima del umbral: en ese caso se
    devuelven los de otro color (mejor eso que una pantalla vacía) y el
    llamador los marca `mismo_color=False` para que se vea en la interfaz.

    Es un reordenamiento, no un descarte: dentro de cada grupo se conserva
    intacto el orden por similitud que ya venía."""
    if not familia_candidato:
        return orden, False  # sin color propio no hay nada que preferir
    mismo = [i for i in orden if familias.get(int(i)) == familia_candidato and sims[i] >= umbral]
    if not mismo:
        return list(orden), True
    otro = [i for i in orden if familias.get(int(i)) != familia_candidato]
    return mismo + otro, False


def api_similar_marca(candidato_id: int, k: int = 6, indice: int = 0, modelo_activo: str = "estandar",
                      filtrar_color: bool = False) -> list[dict]:
    # `indice` (2026-07-23, pedido explícito del usuario -- "cada color se debe hacer una
    # selección de comparables independiente"): antes SIEMPRE se buscaba con la variante
    # 0, aunque el candidato tuviera 3 colores -- las otras 2 nunca tenían su propio
    # comparable. Ahora el llamador (HTML, un buscador por tarjeta de color) pasa el
    # variante_id/indice real de la ficha que se está mostrando.
    #
    # `modelo_activo` (plan 2026-09-04, paso 9): la misma bifurcación fashion_siglip+dino_v2
    # (fusión) vs dinov2_ti (un solo vector, sin fusión) que ya usa `puntuar_candidatos` --
    # ver `get_prototipos`/`vectorizar_y_puntuar_candidatos`. Default "estandar" preserva el
    # comportamiento previo exacto para GestionTUC/PTY, que nunca pasa este parámetro.
    c = conn(); cur = c.cursor()
    indice_usar, uso_alternativa = _variante_saludable(candidato_id, indice)
    # Esta función busca SIEMPRE contra el índice del dominio 'marca', así que
    # su umbral es el de ese dominio -- ver `UMBRAL_POR_MODELO_MARCA`. Antes
    # leía el de tuc y por eso devolvía la lista vacía para todo el lote.
    umbral_activo = umbral_comparable(modelo_activo, "marca")

    # Un solo espacio (ti / fashion / dino): el vector de ESE modelo y nada de
    # fusión. `MODELO_UNICO` dice cuál leer de `candidato_embedding` -- los
    # componentes ya se guardan por separado, no hay que recalcular nada.
    if modelo_activo in MODELO_UNICO:
        r = _vector_variante(candidato_id, indice_usar, modelo=MODELO_UNICO[modelo_activo])
        if not r:
            c.close(); return []
        v = np.array(r[0], dtype=np.float32); v = v / (np.linalg.norm(v) or 1.0)
        v_dino = None
    else:
        r = _vector_variante(candidato_id, indice_usar, modelo="fashion_siglip")
        r_dino = _vector_variante(candidato_id, indice_usar, modelo="dino_v2")
        if not r:
            c.close(); return []
        v = np.array(r[0], dtype=np.float32); v = v / (np.linalg.norm(v) or 1.0)
        v_dino = np.array(r_dino[0], dtype=np.float32) if r_dino else None
        if v_dino is not None:
            v_dino = v_dino / (np.linalg.norm(v_dino) or 1.0)

    # FASE 2: tipo/género del candidato salen del `lote.sqlite`; TODO el índice
    # de comparables de marca reconocida que se consulta más abajo sigue en
    # Postgres, que es donde vive el dato permanente.
    _cl_smarca = _cl()
    _cl_smarca.execute("SELECT genero_canonico, categoria_declarada FROM candidato WHERE candidato_id = ?",
                       (candidato_id,))
    r_gen = _cl_smarca.fetchone()
    gen_c, cat_c = (r_gen[0], r_gen[1]) if r_gen else (None, None)
    # Filtro de tipo aproximado por palabra clave: marca reconocida usa una taxonomía
    # de tipo (lifestyle/running/sandalia/escolar/...) distinta a categoria TUC
    # (Sandalias/Cuñas y plataformas/Tacones/...), sin mapeo 1:1 todavía -- en vez de
    # construir esa tabla completa (trabajo aparte), se usa la primera palabra de la
    # categoría (ej. "Sandalias"->"sandalia") como filtro de texto sobre tipo/subtipo.
    # Bug real corregido (2026-07-22): sin este filtro, "junior" sin filtro de tipo
    # devolvía calzado escolar/deportivo junior en vez de sandalias junior -- matches
    # técnicamente del género correcto pero no comparables en absoluto.
    palabra_tipo = None
    if cat_c:
        primera = cat_c.strip().split()[0].lower()
        palabra_tipo = primera[:-1] if primera.endswith("s") and len(primera) > 4 else primera

    # A partir de acá, ambas ramas producen la MISMA forma: `entradas` (lista de
    # dict con imagen_id/marca/referencia/genero/tipo/subtipo) + `indice_np`
    # (y `indice_dino`, solo en modo estándar) -- el resto de la función ya no
    # necesita distinguir modelo_activo.
    if modelo_activo == "ti":
        # Vectores REALES de TI (dominio='marca', keyed por codigo_tc -- ver
        # silver.vw_ti_vector_vigente). imagen_id se resuelve vía marca+referencia
        # contra el índice fashion_siglip existente (mismo codigo_tc -> misma foto),
        # reutilizando la infraestructura de fotos sin duplicar dim_imagen por esto.
        cur.execute("""
            SELECT v.vector, p.marca_tec AS marca, p.referencia, p.genero, p.tipo_tec, p.subtipo_tec,
                   (SELECT ei.imagen_id FROM silver.fct_embedding_imagen ei
                    WHERE ei.dominio = 'marca' AND ei.marca = p.marca_tec AND ei.referencia = p.referencia
                    LIMIT 1) AS imagen_id
            FROM silver.vw_ti_vector_vigente v
            JOIN silver.dim_tcm_producto p ON p.codigo_tc = v.codigo
            WHERE v.dominio = 'marca' AND v.modelo = 'dinov2_vitb14'
        """)
        filas = cur.fetchall()
        if not filas:
            c.close(); return []
        indice_np = np.array([f[0] for f in filas], dtype=np.float32)
        indice_dino = None
        entradas = [{"marca": f[1], "referencia": f[2], "genero": f[3],
                     "tipo": f[4], "subtipo": f[5], "imagen_id": f[6]} for f in filas]
    else:
        cur.execute("""
            SELECT s.imagen_id, s.marca, s.referencia, s.vector, d.vector, lrm.genero, dm.tipo, dm.subtipo
            FROM silver.fct_embedding_imagen s
            JOIN silver.fct_embedding_imagen d ON d.imagen_id = s.imagen_id AND d.modelo_embedding = 'dino_v2'
            LEFT JOIN silver.lkp_referencia_modelo lrm ON lrm.marca = s.marca AND lrm.referencia = s.referencia
            LEFT JOIN silver.dim_modelo_cr dm ON dm.marca = lrm.marca AND dm.codigo_modelo = lrm.codigo_modelo
            WHERE s.modelo_embedding = 'fashion_siglip' AND s.dominio = 'marca'
        """)
        filas = cur.fetchall()
        if not filas:
            c.close(); return []
        # "dino" compara contra el índice dino_v2 (f[4]); "fashion" contra el de
        # fashion_siglip (f[3]). La consulta ya trae los dos, así que elegir es
        # solo cambiar de columna -- y en los dos casos `indice_dino=None`, que
        # es lo que apaga la fusión más abajo.
        indice_np = np.array([f[4 if modelo_activo == "dino" else 3] for f in filas],
                             dtype=np.float32)
        indice_dino = (None if modelo_activo in MODELO_UNICO
                       else np.array([f[4] for f in filas], dtype=np.float32))
        entradas = [{"imagen_id": f[0], "marca": f[1], "referencia": f[2], "genero": f[5],
                     "tipo": f[6], "subtipo": f[7]} for f in filas]

    for idx in ((indice_np,) if indice_dino is None else (indice_np, indice_dino)):
        normas = np.linalg.norm(idx, axis=1, keepdims=True); normas[normas == 0] = 1.0
        idx /= normas
    generos_idx = np.array([e["genero"] for e in entradas], dtype=object)
    tipos_idx = np.array([" ".join(t for t in (e["tipo"], e["subtipo"]) if t).lower() for e in entradas], dtype=object)

    mascara = np.ones(len(entradas), dtype=bool)
    if gen_c:
        mascara &= (generos_idx == gen_c)
    if not mascara.any():
        mascara = np.ones(len(entradas), dtype=bool)

    if palabra_tipo:
        mascara_tipo = mascara & np.array([palabra_tipo in t for t in tipos_idx])
        if not mascara_tipo.any():
            # pedido explícito del usuario: "es mejor no comparar con productos que no
            # sean comparables" -- si nada calza en tipo (ej. candidato es sandalia y el
            # índice de marca solo tiene botas/tenis para ese género), es preferible NO
            # recomendar nada que degradar a un match de tipo completamente distinto.
            c.close(); return []
        mascara = mascara_tipo

    if v_dino is None:
        sims = indice_np @ v
    else:
        sims = ALPHA_FUSION_SIGLIP * (indice_np @ v) + (1 - ALPHA_FUSION_SIGLIP) * (indice_dino @ v_dino)
    sims = np.where(mascara, sims, -np.inf)
    # Se pide un top más ancho cuando hay que preferir color: si de los k*3
    # mejores por similitud ninguno es del color correcto, no habría con qué
    # armar la lista del mismo color aunque exista más abajo en el ranking.
    top = np.argsort(-sims, kind="stable")[:(k * 12 if filtrar_color else k * 3)]

    familias_idx: dict = {}
    familia_candidato = None
    color_degradado = False
    if filtrar_color:
        mapa_fam = _mapa_color_familia(cur)
        familia_candidato = _familia_color_variante(candidato_id, indice_usar, mapa_fam)
        mapa_img = _familias_por_imagen(cur, [entradas[int(i)]["imagen_id"] for i in top], mapa_fam)
        familias_idx = {int(i): mapa_img.get(entradas[int(i)]["imagen_id"]) for i in top}
        top, color_degradado = _preferir_mismo_color(
            top, familias_idx, familia_candidato, sims, umbral_activo)

    vistos, resultados = set(), []
    for i in top:
        e = entradas[i]
        marca, referencia = e["marca"], e["referencia"]
        if (marca, referencia) in vistos:
            continue
        if sims[i] < umbral_activo:
            continue
        vistos.add((marca, referencia))
        cur.execute("""
            SELECT precio, precio_regular FROM silver.fct_oferta_competidor
            WHERE marca = %s AND referencia_normalizada = %s
            ORDER BY fecha_cruce DESC LIMIT 1
        """, (marca, referencia))
        precio_row = cur.fetchone()
        # Código interno de TU Calzado + ventas REALES de la línea TC Marcas
        # para este comparable (2026-09-10, pedido del usuario: la ventana
        # mostraba solo la referencia del fabricante, que no sirve para buscar
        # nada en nuestros sistemas). Todo dato ya existente:
        #   - `dim_tcm_producto.codigo_tc` = el código interno.
        #   - `fct_tcm_ventas` = precio de venta promedio y unidades netas,
        #     el mismo AVG/SUM que `api_similar_tuc` ya hace sobre
        #     `fct_tuc_ventas`. No hay cálculo nuevo.
        # La junta es por `referencia` (con `marca_tec` como preferencia
        # cuando coincide): medido, `referencia` sola cubre 513 de las 8,436
        # entradas del índice de marca y `marca+referencia` solo 198 -- el
        # resto del índice son productos de mercado que TU Calzado nunca
        # vendió, así que NO tienen código interno y eso se dice explícito.
        cur.execute("""
            SELECT codigo_tc FROM silver.dim_tcm_producto
            WHERE referencia = %s
            ORDER BY (marca_tec = %s) DESC NULLS LAST, codigo_tc
            LIMIT 1
        """, (referencia, marca))
        r_cod = cur.fetchone()
        cur.execute("""
            SELECT AVG(precio_rv), SUM(unidades_netas) FROM silver.fct_tcm_ventas
            WHERE referencia = %s
        """, (referencia,))
        r_vta = cur.fetchone()
        resultados.append({
            "imagen_id": int(e["imagen_id"]) if e["imagen_id"] is not None else None,
            "marca": marca, "referencia": referencia,
            "codigo_tc": r_cod[0] if r_cod else None,
            "precio_avg_vta": float(r_vta[0]) if r_vta and r_vta[0] else None,
            "unidades_vendidas": int(r_vta[1]) if r_vta and r_vta[1] else 0,
            "similitud": float(sims[i]),
            "precio": precio_row[0] if precio_row else None,
            "genero": e["genero"],
            "tipo": " / ".join(t for t in (e["tipo"], e["subtipo"]) if t) or None,
            "via_otro_color": uso_alternativa,
            # Color del comparable + si coincide con el del candidato. Van
            # siempre (también con filtrar_color=False, donde quedan en None):
            # son campos ADITIVOS, ningún consumidor existente los mira.
            "color_familia": familias_idx.get(int(i)),
            "color_familia_candidato": familia_candidato,
            "mismo_color": (None if not familia_candidato
                            else familias_idx.get(int(i)) == familia_candidato),
            "color_degradado": color_degradado,
        })
        if len(resultados) >= k:
            break
    c.close()
    return resultados


def api_similar_tuc(candidato_id: int, k: int = 6, indice: int = 0, modelo_activo: str = "estandar",
                    filtrar_color: bool = False) -> list[dict]:
    # ver nota de `indice` en api_similar_marca -- misma idea, comparable independiente
    # por color/variante en vez de siempre la variante 0. `modelo_activo`: ver nota en
    # api_similar_marca (plan 2026-09-04, paso 9) -- misma bifurcación fusión vs TI.
    c = conn(); cur = c.cursor()
    indice_usar, uso_alternativa = _variante_saludable(candidato_id, indice)
    # Dominio 'tuc' explícito (busca contra el catálogo genérico): su umbral no
    # cambia. Ver `umbral_comparable` / `UMBRAL_POR_MODELO_MARCA`.
    umbral_activo = umbral_comparable(modelo_activo, "tuc")

    # Igual que en `api_similar_marca`: ti/fashion/dino usan un solo vector,
    # el de su propio modelo, sin ALPHA_FUSION_SIGLIP.
    if modelo_activo in MODELO_UNICO:
        r = _vector_variante(candidato_id, indice_usar, modelo=MODELO_UNICO[modelo_activo])
        if not r:
            c.close(); return []
        v = np.array(r[0], dtype=np.float32); v = v / (np.linalg.norm(v) or 1.0)
        v_dino = None
    else:
        r = _vector_variante(candidato_id, indice_usar, modelo="fashion_siglip")
        r_dino = _vector_variante(candidato_id, indice_usar, modelo="dino_v2")
        if not r:
            c.close(); return []
        v = np.array(r[0], dtype=np.float32); v = v / (np.linalg.norm(v) or 1.0)
        v_dino = np.array(r_dino[0], dtype=np.float32) if r_dino else None
        if v_dino is not None:
            v_dino = v_dino / (np.linalg.norm(v_dino) or 1.0)

    # FASE 2: igual que en `api_similar_marca` -- tipo/género del candidato del
    # `lote.sqlite`, índice de comparables TUC de Postgres.
    _cl_stuc = _cl()
    _cl_stuc.execute("SELECT categoria_declarada, genero_canonico FROM candidato WHERE candidato_id = ?",
                     (candidato_id,))
    cat_c, gen_c = tuple(_cl_stuc.fetchone() or (None, None))

    if modelo_activo == "ti":
        # Vectores REALES de TI ya promovidos por codigo_tuc (paso 3, silver.
        # fct_embedding_ti_codigo) -- a diferencia de marca, acá SÍ tenemos
        # codigo_tuc directo (no hace falta resolver imagen_id vía join extra
        # para el vector, solo para la foto).
        cur.execute("""
            SELECT e.vector, e.codigo_tuc, p.categoria, g.genero_canonico,
                   (SELECT ei.imagen_id FROM silver.fct_embedding_imagen ei
                    WHERE ei.dominio = 'tuc' AND ei.codigo_tuc = e.codigo_tuc
                    LIMIT 1) AS imagen_id
            FROM silver.fct_embedding_ti_codigo e
            JOIN silver.dim_tuc_producto p ON p.codigo_tuc = e.codigo_tuc
            LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
            WHERE e.modelo = 'dinov2_ti'
        """)
        filas = cur.fetchall()
        if not filas:
            c.close(); return []
        indice_np = np.array([f[0] for f in filas], dtype=np.float32)
        indice_dino = None
        entradas = [{"codigo_tuc": f[1], "categoria": f[2], "genero": f[3], "imagen_id": f[4]} for f in filas]
    else:
        cur.execute("""
            SELECT s.imagen_id, s.codigo_tuc, s.vector, d.vector, p.categoria, g.genero_canonico
            FROM silver.fct_embedding_imagen s
            JOIN silver.fct_embedding_imagen d ON d.imagen_id = s.imagen_id AND d.modelo_embedding = 'dino_v2'
            JOIN silver.dim_tuc_producto p ON p.codigo_tuc = s.codigo_tuc
            LEFT JOIN cfg.lkp_tuc_genero g ON g.genero_crudo = p.genero
            WHERE s.modelo_embedding = 'fashion_siglip' AND s.dominio = 'tuc'
        """)
        filas = cur.fetchall()
        if not filas:
            c.close(); return []
        # Misma elección de columna que en `api_similar_marca`: f[2] =
        # fashion_siglip, f[3] = dino_v2.
        indice_np = np.array([f[3 if modelo_activo == "dino" else 2] for f in filas],
                             dtype=np.float32)
        indice_dino = (None if modelo_activo in MODELO_UNICO
                       else np.array([f[3] for f in filas], dtype=np.float32))
        entradas = [{"imagen_id": f[0], "codigo_tuc": f[1], "categoria": f[4], "genero": f[5]} for f in filas]

    for idx in ((indice_np,) if indice_dino is None else (indice_np, indice_dino)):
        normas = np.linalg.norm(idx, axis=1, keepdims=True); normas[normas == 0] = 1.0
        idx /= normas
    categorias_idx = np.array([e["categoria"] for e in entradas], dtype=object)
    generos_idx = np.array([e["genero"] for e in entradas], dtype=object)

    mascara = np.ones(len(entradas), dtype=bool)
    if cat_c:
        mascara &= (categorias_idx == cat_c)
    if gen_c:
        mascara &= (generos_idx == gen_c)
    if not mascara.any():
        mascara = np.ones(len(entradas), dtype=bool)

    if v_dino is None:
        sims = indice_np @ v
    else:
        sims = ALPHA_FUSION_SIGLIP * (indice_np @ v) + (1 - ALPHA_FUSION_SIGLIP) * (indice_dino @ v_dino)
    sims = np.where(mascara, sims, -np.inf)
    top = np.argsort(-sims, kind="stable")[:(k * 12 if filtrar_color else k * 3)]

    # Mismo criterio de color que en `api_similar_marca` -- ver el bloque de
    # comentarios de `_mapa_color_familia`. La única diferencia es de dónde
    # sale el color del comparable: acá `bronze.raw_tuc_atributos.color_familia`
    # por codigo_tuc (ya viene en familias), no `fct_color_imagen` por foto.
    familias_idx: dict = {}
    familia_candidato = None
    color_degradado = False
    if filtrar_color:
        mapa_fam = _mapa_color_familia(cur)
        familia_candidato = _familia_color_variante(candidato_id, indice_usar, mapa_fam)
        mapa_cod = _familias_por_codigo_tuc(cur, [entradas[int(i)]["codigo_tuc"] for i in top])
        familias_idx = {int(i): mapa_cod.get(entradas[int(i)]["codigo_tuc"]) for i in top}
        top, color_degradado = _preferir_mismo_color(
            top, familias_idx, familia_candidato, sims, umbral_activo)

    vistos, resultados = set(), []
    for i in top:
        e = entradas[i]
        codigo_tuc = e["codigo_tuc"]
        if codigo_tuc in vistos:
            continue
        if sims[i] < umbral_activo:
            continue
        vistos.add(codigo_tuc)
        cur.execute("""
            SELECT AVG(precio_avg_vta), SUM(unidades_netas) FROM silver.fct_tuc_ventas WHERE codigo_tuc = %s
        """, (codigo_tuc,))
        precio_row = cur.fetchone()
        resultados.append({
            "imagen_id": int(e["imagen_id"]) if e["imagen_id"] is not None else None,
            "codigo_tuc": codigo_tuc, "similitud": float(sims[i]),
            "precio_avg_vta": float(precio_row[0]) if precio_row and precio_row[0] else None,
            "unidades_vendidas": int(precio_row[1]) if precio_row and precio_row[1] else 0,
            "categoria": e["categoria"], "genero": e["genero"],
            "via_otro_color": uso_alternativa,
            "color_familia": familias_idx.get(int(i)),
            "color_familia_candidato": familia_candidato,
            "mismo_color": (None if not familia_candidato
                            else familias_idx.get(int(i)) == familia_candidato),
            "color_degradado": color_degradado,
        })
        if len(resultados) >= k:
            break
    c.close()
    return resultados
