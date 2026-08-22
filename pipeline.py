# -*- coding: utf-8 -*-
"""Pipeline de revisión: Fase 0 (triaje) → Fase 1 (módulos en paralelo) → Fase 2 (informe).

Detalles de API que respeta este módulo (briefing §3):
- Modelo `claude-opus-5` (obligatorio, no hay modelo por defecto).
- No se fija `temperature`: en Opus 5 el thinking adaptativo está activo por
  defecto y los parámetros de sampling ya no se admiten.
- Prompt caching: el manual y el contrato se marcan como bloques cacheables y
  se pre-calientan con una llamada mínima antes de lanzar la fase 1 en paralelo,
  para que las 6-7 llamadas lean del caché en vez de reescribirlo cada una.
- `max_tokens` generoso en fase 2 y detección de truncamiento.
- Todas las llamadas van en streaming para evitar timeouts HTTP.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from prompts import (
    MODULOS,
    PROMPT_FASE_0,
    PROMPT_FASE_2,
    construir_prompt_fase_1,
)

MODELO = "claude-opus-5"
MAX_TOKENS_FASE_0 = 8000
MAX_TOKENS_FASE_1 = 32000
MAX_TOKENS_FASE_2 = 64000

# Mapa de desplazamiento del paso A de la fase 2, replicado en código para el
# recálculo independiente del score. Clave: (módulo, índice de cláusula en la
# lista cerrada). Las listas son cerradas y ordenadas, por lo que el índice es
# un identificador fiable.
DESPLAZADAS_EEUU = {
    ("M1", 2): "8.2 Precio y revisión anual",           # 1.2 Precio
    ("M1", 3): "8.2 Facturación e impago",              # 1.2 Facturación
    ("M1", 4): "8.2 Facturación e impago",              # 1.2 Penalizaciones por impago
    ("M3", 0): "8.4 Cap de responsabilidad",            # 3.1 Cap de responsabilidad
    ("M3", 1): "8.4 Exclusión de daños indirectos",     # 3.1 Exclusión de daños indirectos
    ("M3", 4): "8.6 Protección ante insolvencia",       # 3.2 Escenario de insolvencia
    ("M4", 9): "8.1 Ley aplicable",                     # 5 Ley aplicable y jurisdicción
    ("M5", 4): "8.3 Mecanismo de garantía EEUU",        # 6.3 Localización de datos
    ("M5", 5): "8.3 Mecanismo de garantía EEUU",        # 6.3 SCCs
}


class ErrorTriaje(Exception):
    """La fase 0 (o la comprobación de versión de la fase 1) rechazó el documento."""

    def __init__(self, codigo: str, detalle: str = "", tipo_detectado: str = ""):
        self.codigo = codigo
        self.detalle = detalle
        self.tipo_detectado = tipo_detectado
        super().__init__(codigo)


class ErrorTruncamiento(Exception):
    """La fase 2 devolvió un informe cortado por max_tokens."""


def _crear_cliente(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


def _texto_respuesta(mensaje) -> str:
    return "".join(b.text for b in mensaje.content if b.type == "text")


def _extraer_json(texto: str):
    """Aísla el JSON de la respuesta (tolera bloques de código o texto residual)."""
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
    inicio_obj = texto.find("{")
    inicio_arr = texto.find("[")
    if inicio_arr != -1 and (inicio_obj == -1 or inicio_arr < inicio_obj):
        inicio, fin = inicio_arr, texto.rfind("]") + 1
    elif inicio_obj != -1:
        inicio, fin = inicio_obj, texto.rfind("}") + 1
    else:
        raise ValueError("La respuesta no contiene JSON")
    return json.loads(texto[inicio:fin])


def _llamada(cliente, *, system=None, contenido_usuario, max_tokens):
    """Llamada en streaming; devuelve el mensaje final completo."""
    kwargs = {
        "model": MODELO,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": contenido_usuario}],
    }
    if system is not None:
        kwargs["system"] = system
    with cliente.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


# ---------------------------------------------------------------------------
# FASE 0 · Triaje
# ---------------------------------------------------------------------------

def fase_0(cliente, contrato: str) -> dict:
    mensaje = _llamada(
        cliente,
        system=PROMPT_FASE_0,
        contenido_usuario=contrato,
        max_tokens=MAX_TOKENS_FASE_0,
    )
    resultado = _extraer_json(_texto_respuesta(mensaje))
    if isinstance(resultado, dict) and "error" in resultado:
        raise ErrorTriaje(
            codigo=resultado["error"],
            detalle=resultado.get("detalle", ""),
            tipo_detectado=resultado.get("tipo_detectado", ""),
        )
    return resultado


# ---------------------------------------------------------------------------
# FASE 1 · Revisión por módulo, en paralelo
# ---------------------------------------------------------------------------

def _bloques_cacheados(manual: str, contrato: str) -> list:
    """Los dos bloques compartidos por todas las llamadas de fase 1, marcados
    como cacheables. Deben ir en el MISMO orden en todas las llamadas: el caché
    de la API es un prefijo exacto."""
    return [
        {
            "type": "text",
            "text": "## MANUAL DE REVISIÓN\n\n" + manual,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "## CONTRATO A REVISAR\n\n" + contrato,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _precalentar_cache(cliente, manual: str, contrato: str) -> None:
    """Escribe el prefijo manual+contrato en el caché con una llamada mínima,
    para que las llamadas paralelas de la fase 1 lo lean en vez de escribirlo
    cada una por su cuenta. Si falla, se continúa sin caché: solo afecta al coste."""
    try:
        cliente.messages.create(
            model=MODELO,
            max_tokens=0,
            messages=[{"role": "user", "content": _bloques_cacheados(manual, contrato)}],
        )
    except anthropic.BadRequestError:
        try:
            cliente.messages.create(
                model=MODELO,
                max_tokens=1,
                messages=[{"role": "user", "content": _bloques_cacheados(manual, contrato)}],
            )
        except Exception:
            pass
    except Exception:
        pass


def _revisar_modulo(cliente, id_modulo: str, manual: str, contrato: str, json_fase_0: str):
    """Una llamada de fase 1. El prompt del módulo va DESPUÉS de los bloques
    cacheados para no romper el prefijo compartido del caché (el prompt cambia
    por módulo; el manual y el contrato no)."""
    contenido = _bloques_cacheados(manual, contrato) + [
        {"type": "text", "text": construir_prompt_fase_1(id_modulo, json_fase_0)}
    ]
    mensaje = _llamada(cliente, contenido_usuario=contenido, max_tokens=MAX_TOKENS_FASE_1)
    if mensaje.stop_reason == "max_tokens":
        raise ValueError("respuesta truncada")
    resultado = _extraer_json(_texto_respuesta(mensaje))
    if isinstance(resultado, dict) and "error" in resultado:
        raise ErrorTriaje(codigo=resultado["error"], detalle=str(resultado))
    esperadas = len(MODULOS[id_modulo]["clausulas"])
    if not isinstance(resultado, list) or len(resultado) != esperadas:
        recibidas = len(resultado) if isinstance(resultado, list) else "?"
        raise ValueError(f"esperadas {esperadas} cláusulas, recibidas {recibidas}")
    return resultado


def fase_1(cliente, fase0: dict, manual: str, contrato: str, progreso=None):
    """Lanza los módulos en paralelo. Si un módulo falla (recuento, JSON o
    truncamiento), se reintenta UNA vez; si vuelve a fallar, se continúa y se
    registra como incidencia para la fase 2 (briefing §2)."""
    modulos = ["M1", "M2", "M3", "M4", "M5", "M6"]
    if fase0.get("modulo_aplicable") == "EEUU":
        modulos.append("M7")

    json_fase_0 = json.dumps(fase0, ensure_ascii=False, indent=2)
    _precalentar_cache(cliente, manual, contrato)

    resultados = {}
    incidencias = []

    def tarea(id_modulo):
        try:
            return id_modulo, _revisar_modulo(cliente, id_modulo, manual, contrato, json_fase_0), None
        except ErrorTriaje:
            raise
        except Exception as primera:
            try:
                return id_modulo, _revisar_modulo(cliente, id_modulo, manual, contrato, json_fase_0), None
            except ErrorTriaje:
                raise
            except Exception as segunda:
                return id_modulo, None, f"Módulo {id_modulo}: falló dos veces ({primera}; {segunda})"

    completados = 0
    with ThreadPoolExecutor(max_workers=len(modulos)) as pool:
        futuros = [pool.submit(tarea, m) for m in modulos]
        for futuro in as_completed(futuros):
            id_modulo, resultado, incidencia = futuro.result()
            if resultado is not None:
                resultados[id_modulo] = resultado
            if incidencia:
                incidencias.append(incidencia)
            completados += 1
            if progreso:
                progreso(completados, len(modulos))

    return resultados, incidencias, modulos


# ---------------------------------------------------------------------------
# FASE 2 · Consolidación e informe
# ---------------------------------------------------------------------------

def fase_2(cliente, fase0: dict, resultados: dict, incidencias_pipeline: list) -> str:
    """Consolidación. NO se envían ni el contrato ni el manual (briefing §2)."""
    partes = [
        "## JSON DE LA FASE 0 (TRIAJE)\n\n"
        + json.dumps(fase0, ensure_ascii=False, indent=2)
    ]
    for id_modulo in ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]:
        if id_modulo in resultados:
            partes.append(
                f"## RESULTADOS FASE 1 — MÓDULO {id_modulo} · {MODULOS[id_modulo]['nombre']}\n\n"
                + json.dumps(resultados[id_modulo], ensure_ascii=False, indent=2)
            )
    if incidencias_pipeline:
        partes.append(
            "## INCIDENCIAS TÉCNICAS DEL PIPELINE\n\n"
            "Los siguientes módulos no devolvieron resultados válidos pese a un "
            "reintento. Sus cláusulas NO están incluidas en los datos anteriores; "
            "regístralo en el apartado de incidencias del informe:\n- "
            + "\n- ".join(incidencias_pipeline)
        )

    mensaje = _llamada(
        cliente,
        system=PROMPT_FASE_2,
        contenido_usuario="\n\n---\n\n".join(partes),
        max_tokens=MAX_TOKENS_FASE_2,
    )
    if mensaje.stop_reason == "max_tokens":
        raise ErrorTruncamiento()
    html = _texto_respuesta(mensaje).strip()
    if html.startswith("```"):
        html = re.sub(r"^```[a-zA-Z]*\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    return html


# ---------------------------------------------------------------------------
# Verificaciones independientes en código (briefing §6)
# ---------------------------------------------------------------------------

def _es_desplazada(id_modulo: str, indice: int, es_eeuu: bool, resultados: dict) -> bool:
    if not es_eeuu or (id_modulo, indice) not in DESPLAZADAS_EEUU:
        return False
    # Solo desplaza si el módulo M7 devolvió resultados (si M7 falló, las
    # cláusulas de las secciones 1-7 siguen siendo lo único que puntúa).
    return "M7" in resultados


def verificar(fase0: dict, resultados: dict, html: str) -> dict:
    """Recalcula el score con la misma fórmula que la fase 2 y comprueba vetos
    y señales de manipulación. Un fallo aritmético del modelo es silencioso y
    plausible: nadie lo detecta leyendo."""
    es_eeuu = fase0.get("modulo_aplicable") == "EEUU"
    suma_ponderada = 0.0
    suma_pesos = 0.0
    n_denominador = 0
    vetos_disparados = []

    for id_modulo, clausulas in resultados.items():
        for indice, c in enumerate(clausulas):
            if not isinstance(c, dict):
                continue
            estado = c.get("estado", "")
            desplazada = _es_desplazada(id_modulo, indice, es_eeuu, resultados)
            if c.get("es_veto") and c.get("veto_disparado") and not desplazada:
                vetos_disparados.append(
                    f"{c.get('seccion_manual', '')} {c.get('nombre_clausula', '')}".strip()
                )
            if estado in ("NO_APLICA", "NO_EXIGIBLE") or desplazada:
                continue
            try:
                peso = float(c.get("peso") or 0)
                puntuacion = float(c.get("puntuacion") or 0)
            except (TypeError, ValueError):
                continue
            suma_ponderada += peso * puntuacion
            suma_pesos += peso
            n_denominador += 1

    score = round(100 * suma_ponderada / suma_pesos, 1) if suma_pesos else None

    # Discrepancia con el score del informe: si ningún porcentaje del HTML está
    # a menos de 1 punto del recalculado, el modelo se equivocó en la aritmética.
    discrepancia = False
    if score is not None and html:
        porcentajes = [
            float(p.replace(",", "."))
            for p in re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s*%", html)
        ]
        if porcentajes and not any(abs(p - score) <= 1.0 for p in porcentajes):
            discrepancia = True

    # Intentos de manipulación detectados por fase 0 o fase 1
    manipulacion = [
        str(alerta)
        for alerta in fase0.get("alertas_triaje", []) or []
        if "manipul" in str(alerta).lower()
    ]
    for clausulas in resultados.values():
        for c in clausulas:
            if isinstance(c, dict) and c.get("contradice_fase_0"):
                if "manipul" in str(c["contradice_fase_0"]).lower():
                    manipulacion.append(str(c["contradice_fase_0"]))

    return {
        "score_recalculado": score,
        "suma_ponderada": round(suma_ponderada, 2),
        "suma_pesos": round(suma_pesos, 2),
        "n_denominador": n_denominador,
        "discrepancia_score": discrepancia,
        "vetos_disparados": vetos_disparados,
        "manipulacion": manipulacion,
    }


# ---------------------------------------------------------------------------
# Orquestación completa
# ---------------------------------------------------------------------------

def ejecutar_analisis(contrato: str, manual: str, api_key: str, progreso=None) -> dict:
    """Ejecuta las tres fases. `progreso` es un callable(etapa, actual, total)
    con etapa en {"triaje", "modulos", "informe"}.

    Nada se persiste: todo vive en memoria y se descarta al terminar (briefing §8).
    """
    cliente = _crear_cliente(api_key)

    if progreso:
        progreso("triaje", 0, 0)
    fase0 = fase_0(cliente, contrato)

    def progreso_modulos(actual, total):
        if progreso:
            progreso("modulos", actual, total)

    resultados, incidencias, _ = fase_1(cliente, fase0, manual, contrato, progreso_modulos)

    if not resultados:
        raise ErrorTriaje(codigo="version_manual_no_coincide", detalle="ningún módulo devolvió resultados")

    if progreso:
        progreso("informe", 0, 0)
    html = fase_2(cliente, fase0, resultados, incidencias)

    return {
        "fase0": fase0,
        "resultados": resultados,
        "incidencias_pipeline": incidencias,
        "html": html,
        "verificacion": verificar(fase0, resultados, html),
    }
