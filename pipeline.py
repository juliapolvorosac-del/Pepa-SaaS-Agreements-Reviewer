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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from prompts import (
    MODULOS,
    PROMPT_FASE_0,
    PROMPT_FASE_2,
    construir_prompt_fase_1,
)

# Modelo por fase. La fase 1 —el análisis jurídico contra el manual— se queda
# en Opus 5. El triaje (extracción de hechos) y la consolidación (que tiene
# prohibido reinterpretar) usan Sonnet 5: ~2,5 veces más barato y más rápido.
MODELO_FASE_0 = "claude-sonnet-5"
MODELO_FASE_1 = "claude-opus-5"
MODELO_FASE_2 = "claude-sonnet-5"
MAX_TOKENS_FASE_0 = 16000
MAX_TOKENS_FASE_1 = 32000
# El informe de la fase 2 (67 cláusulas × 8 sub-apartados + redlines) puede ser
# enorme: se usa el máximo de salida que admite el modelo (128K, en streaming).
MAX_TOKENS_FASE_2 = 128000

# Esfuerzo de razonamiento por fase (parámetro `effort` de la API). El
# razonamiento interno son tokens de salida: se pagan y se esperan. Se reduce
# solo donde no hay juicio jurídico: el triaje extrae hechos y la fase 2 tiene
# prohibido reinterpretar clasificaciones. La fase 1 —el análisis contra el
# manual— se mantiene en "high".
ESFUERZO_FASE_0 = "medium"
ESFUERZO_FASE_1 = "high"
ESFUERZO_FASE_2 = "medium"

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


# ---------------------------------------------------------------------------
# Contador de consumo y coste
#
# Tarifas publicadas por Anthropic, en dólares por millón de tokens. Si cambian,
# se actualizan aquí. La escritura en caché cuesta 1,25 veces la tarifa de
# entrada y la lectura desde caché solo 0,10 veces: de ahí sale el ahorro que
# produce reutilizar el manual y el contrato en los 6-7 módulos de la fase 1.
# ---------------------------------------------------------------------------

PRECIOS_USD_POR_MILLON = {
    "claude-opus-5": {"entrada": 5.00, "salida": 25.00},
    "claude-sonnet-5": {"entrada": 3.00, "salida": 15.00},
}
MULT_ESCRITURA_CACHE = 1.25
MULT_LECTURA_CACHE = 0.10

NOMBRE_FASE = {
    "triaje": "Fase 0 · Triaje",
    "cache": "Precalentamiento de caché",
    "modulos": "Fase 1 · Revisión por módulos",
    "informe": "Fase 2 · Informe",
}


class ContadorUso:
    """Acumula el consumo de tokens de todas las llamadas del análisis.

    Las llamadas de la fase 1 corren en paralelo, así que el acumulador va
    protegido por un cerrojo.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._registros = []

    def registrar(self, fase: str, modelo: str, usage) -> None:
        if usage is None:
            return
        with self._lock:
            self._registros.append({
                "fase": fase,
                "modelo": modelo,
                "entrada": getattr(usage, "input_tokens", 0) or 0,
                "salida": getattr(usage, "output_tokens", 0) or 0,
                "cache_escritura": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_lectura": getattr(usage, "cache_read_input_tokens", 0) or 0,
            })

    @staticmethod
    def _coste(registro: dict) -> float:
        tarifa = PRECIOS_USD_POR_MILLON.get(registro["modelo"])
        if tarifa is None:
            return 0.0
        return (
            registro["entrada"] * tarifa["entrada"]
            + registro["cache_escritura"] * tarifa["entrada"] * MULT_ESCRITURA_CACHE
            + registro["cache_lectura"] * tarifa["entrada"] * MULT_LECTURA_CACHE
            + registro["salida"] * tarifa["salida"]
        ) / 1_000_000

    def resumen(self) -> dict:
        with self._lock:
            registros = list(self._registros)

        por_fase = {}
        total = 0.0
        tokens_entrada = tokens_salida = cache_lectura = cache_escritura = 0
        ahorro = 0.0

        for r in registros:
            coste = self._coste(r)
            total += coste
            tokens_entrada += r["entrada"]
            tokens_salida += r["salida"]
            cache_lectura += r["cache_lectura"]
            cache_escritura += r["cache_escritura"]

            tarifa = PRECIOS_USD_POR_MILLON.get(r["modelo"])
            if tarifa:
                # Lo que habrían costado los tokens leídos de caché si se
                # hubieran enviado enteros en cada llamada.
                ahorro += (
                    r["cache_lectura"] * tarifa["entrada"] * (1 - MULT_LECTURA_CACHE)
                ) / 1_000_000

            fila = por_fase.setdefault(
                r["fase"],
                {"nombre": NOMBRE_FASE.get(r["fase"], r["fase"]), "llamadas": 0,
                 "modelo": r["modelo"], "coste_usd": 0.0, "salida": 0},
            )
            fila["llamadas"] += 1
            fila["coste_usd"] += coste
            fila["salida"] += r["salida"]

        for fila in por_fase.values():
            fila["coste_usd"] = round(fila["coste_usd"], 4)

        return {
            "coste_total_usd": round(total, 4),
            "ahorro_cache_usd": round(ahorro, 4),
            "tokens_entrada": tokens_entrada,
            "tokens_salida": tokens_salida,
            "cache_lectura": cache_lectura,
            "cache_escritura": cache_escritura,
            "n_llamadas": len(registros),
            "por_fase": [
                por_fase[f] for f in ("triaje", "cache", "modulos", "informe")
                if f in por_fase
            ],
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


class ErrorSaldoInsuficiente(Exception):
    """La cuenta de Anthropic no tiene saldo. No es un fallo del análisis:
    es un problema de facturación del titular de la clave API."""


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


def _llamada(cliente, *, modelo, system=None, contenido_usuario, max_tokens,
             esfuerzo=None, contador=None, fase=None):
    """Llamada en streaming; devuelve el mensaje final completo."""
    kwargs = {
        "model": modelo,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": contenido_usuario}],
    }
    if system is not None:
        kwargs["system"] = system
    if esfuerzo is not None:
        kwargs["output_config"] = {"effort": esfuerzo}
    try:
        with cliente.messages.stream(**kwargs) as stream:
            mensaje = stream.get_final_message()
        if contador is not None:
            contador.registrar(fase, modelo, getattr(mensaje, "usage", None))
        return mensaje
    except anthropic.BadRequestError as e:
        # Falta de saldo: no es un fallo del análisis, y reintentarlo no sirve
        # de nada. Se distingue para poder dar un mensaje honesto.
        if "credit balance" in str(e).lower():
            raise ErrorSaldoInsuficiente() from e
        raise


# ---------------------------------------------------------------------------
# FASE 0 · Triaje
# ---------------------------------------------------------------------------

def fase_0(cliente, contrato: str, contador=None) -> dict:
    mensaje = _llamada(
        cliente,
        modelo=MODELO_FASE_0,
        system=PROMPT_FASE_0,
        contenido_usuario=contrato,
        max_tokens=MAX_TOKENS_FASE_0,
        esfuerzo=ESFUERZO_FASE_0,
        contador=contador,
        fase="triaje",
    )
    if mensaje.stop_reason == "max_tokens":
        raise ValueError("fase 0: respuesta truncada por max_tokens")
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


def _precalentar_cache(cliente, manual: str, contrato: str, contador=None) -> None:
    """Escribe el prefijo manual+contrato en el caché con una llamada mínima,
    para que las llamadas paralelas de la fase 1 lo lean en vez de escribirlo
    cada una por su cuenta. Si falla, se continúa sin caché: solo afecta al coste."""
    # El caché es por modelo: el precalentamiento debe usar el MISMO modelo que
    # las llamadas de la fase 1.
    def _intento(max_tokens):
        mensaje = cliente.messages.create(
            model=MODELO_FASE_1,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": _bloques_cacheados(manual, contrato)}],
        )
        if contador is not None:
            contador.registrar("cache", MODELO_FASE_1, getattr(mensaje, "usage", None))

    try:
        _intento(0)
    except anthropic.BadRequestError:
        try:
            _intento(1)
        except Exception:
            pass
    except Exception:
        pass


def _revisar_modulo(cliente, id_modulo: str, manual: str, contrato: str, json_fase_0: str,
                    reintento: bool = False, contador=None):
    """Una llamada de fase 1. El prompt del módulo va DESPUÉS de los bloques
    cacheados para no romper el prefijo compartido del caché (el prompt cambia
    por módulo; el manual y el contrato no)."""
    contenido = _bloques_cacheados(manual, contrato) + [
        {"type": "text", "text": construir_prompt_fase_1(id_modulo, json_fase_0, reintento=reintento)}
    ]
    mensaje = _llamada(
        cliente,
        modelo=MODELO_FASE_1,
        contenido_usuario=contenido,
        max_tokens=MAX_TOKENS_FASE_1,
        esfuerzo=ESFUERZO_FASE_1,
        contador=contador,
        fase="modulos",
    )
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


def fase_1(cliente, fase0: dict, manual: str, contrato: str, progreso=None,
           cache_precalentada=False, contador=None):
    """Lanza los módulos en paralelo. Si un módulo falla (recuento, JSON o
    truncamiento), se reintenta UNA vez con una nota correctiva —sin ella, un
    reintento con la misma entrada exacta puede reproducir el mismo fallo
    determinista—; si vuelve a fallar, se continúa y se registra como
    incidencia para la fase 2 (briefing §2)."""
    modulos = ["M1", "M2", "M3", "M4", "M5", "M6"]
    if fase0.get("modulo_aplicable") == "EEUU":
        modulos.append("M7")

    json_fase_0 = json.dumps(fase0, ensure_ascii=False, indent=2)
    if not cache_precalentada:
        _precalentar_cache(cliente, manual, contrato, contador)

    resultados = {}
    incidencias = []

    def tarea(id_modulo):
        try:
            return id_modulo, _revisar_modulo(
                cliente, id_modulo, manual, contrato, json_fase_0, contador=contador
            ), None
        except (ErrorTriaje, ErrorSaldoInsuficiente):
            # Sin saldo no tiene sentido reintentar ni seguir con los demás
            # módulos: se propaga de inmediato.
            raise
        except Exception as primera:
            try:
                resultado = _revisar_modulo(
                    cliente, id_modulo, manual, contrato, json_fase_0,
                    reintento=True, contador=contador
                )
                return id_modulo, resultado, None
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

def fase_2(cliente, fase0: dict, resultados: dict, incidencias_pipeline: list,
           agregado: dict, contador=None) -> str:
    """Consolidación. NO se envían ni el contrato ni el manual (briefing §2)."""
    partes = [
        "## JSON DE LA FASE 0 (TRIAJE)\n\n"
        + json.dumps(fase0, ensure_ascii=False, indent=2),
        "## RESULTADO AGREGADO (calculado y verificado por la aplicación — "
        "úsalo tal cual, no lo recalcules)\n\n"
        + json.dumps(agregado, ensure_ascii=False, indent=2),
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
        modelo=MODELO_FASE_2,
        system=PROMPT_FASE_2,
        contenido_usuario="\n\n---\n\n".join(partes),
        max_tokens=MAX_TOKENS_FASE_2,
        esfuerzo=ESFUERZO_FASE_2,
        contador=contador,
        fase="informe",
    )
    if mensaje.stop_reason == "max_tokens":
        raise ErrorTruncamiento()
    html = _texto_respuesta(mensaje).strip()
    if html.startswith("```"):
        html = re.sub(r"^```[a-zA-Z]*\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    return html


# ---------------------------------------------------------------------------
# Cálculo del score y de los vetos: LO HACE ÚNICAMENTE LA APLICACIÓN.
#
# El modelo de la fase 2 ya no calcula el score ni decide si algún veto se ha
# disparado: recibe estos valores hechos en el bloque RESULTADO AGREGADO y
# solo los transcribe. Una sola fuente de verdad — sin ella, dos cálculos
# independientes del mismo dato pueden divergir de forma silenciosa y
# plausible, que es justo el fallo que esto sustituye.
# ---------------------------------------------------------------------------

def _es_desplazada(id_modulo: str, indice: int, es_eeuu: bool, resultados: dict) -> bool:
    if not es_eeuu or (id_modulo, indice) not in DESPLAZADAS_EEUU:
        return False
    # Solo desplaza si el módulo M7 devolvió resultados (si M7 falló, las
    # cláusulas de las secciones 1-7 siguen siendo lo único que puntúa).
    return "M7" in resultados


def calcular_agregado(fase0: dict, resultados: dict) -> dict:
    """Score, semáforo, vetos, cláusulas desplazadas e intentos de
    manipulación — todo calculado en código a partir de los JSON de fase 0 y
    fase 1, ANTES de pedir el informe. Se llama con este resultado como dato
    de entrada de la fase 2 (briefing §6)."""
    es_eeuu = fase0.get("modulo_aplicable") == "EEUU"
    suma_ponderada = 0.0
    suma_pesos = 0.0
    n_denominador = 0
    vetos_disparados = []
    clausulas_desplazadas = []
    intentos_manipulacion = []

    im0 = fase0.get("intento_manipulacion") or {}
    if isinstance(im0, dict) and im0.get("detectado"):
        intentos_manipulacion.append({
            "origen": "Fase 0 · Triaje",
            "texto_detectado": im0.get("texto_detectado", ""),
            "localizador": im0.get("localizador", ""),
        })

    for id_modulo, clausulas in resultados.items():
        for indice, c in enumerate(clausulas):
            if not isinstance(c, dict):
                continue
            estado = c.get("estado", "")
            desplazada = _es_desplazada(id_modulo, indice, es_eeuu, resultados)
            if desplazada:
                clausulas_desplazadas.append({
                    "modulo": id_modulo,
                    "seccion_manual": c.get("seccion_manual", ""),
                    "nombre_clausula": c.get("nombre_clausula", ""),
                    "desplazada_por": DESPLAZADAS_EEUU.get((id_modulo, indice), ""),
                })
            if c.get("es_veto") and c.get("veto_disparado") and not desplazada:
                vetos_disparados.append(
                    f"{c.get('seccion_manual', '')} {c.get('nombre_clausula', '')}".strip()
                )
            im = c.get("intento_manipulacion") or {}
            if isinstance(im, dict) and im.get("detectado"):
                intentos_manipulacion.append({
                    "origen": f"Fase 1 · Módulo {id_modulo} · {c.get('nombre_clausula', '')}",
                    "texto_detectado": im.get("texto_detectado", ""),
                    "localizador": im.get("localizador", ""),
                })
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
    hay_veto = bool(vetos_disparados)
    if hay_veto or score is None:
        semaforo = "🔴"
    elif score >= 85:
        semaforo = "🟢"
    elif score >= 60:
        semaforo = "🟡"
    else:
        semaforo = "🔴"

    return {
        "score_pct": score,
        "suma_ponderada": round(suma_ponderada, 2),
        "suma_pesos": round(suma_pesos, 2),
        "n_denominador": n_denominador,
        "semaforo": semaforo,
        "hay_veto_disparado": hay_veto,
        "vetos_disparados": vetos_disparados,
        "clausulas_desplazadas": clausulas_desplazadas,
        "intentos_manipulacion": intentos_manipulacion,
    }


def verificar_transcripcion(agregado: dict, html: str) -> bool:
    """Comprobación de seguridad, no de cálculo: ¿el informe transcribió la
    cifra de score que se le dio? Si no aparece, es un error de transcripción
    del modelo (raro, pero posible), no una discrepancia de cálculo."""
    score = agregado.get("score_pct")
    if score is None or not html:
        return False
    porcentajes = [
        float(p.replace(",", "."))
        for p in re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s*%", html)
    ]
    return bool(porcentajes) and not any(abs(p - score) <= 0.15 for p in porcentajes)


# ---------------------------------------------------------------------------
# Orquestación completa
# ---------------------------------------------------------------------------

def ejecutar_analisis(contrato: str, manual: str, api_key: str, progreso=None) -> dict:
    """Ejecuta las tres fases. `progreso` es un callable(etapa, actual, total)
    con etapa en {"triaje", "modulos", "informe"}.

    Nada se persiste: todo vive en memoria y se descarta al terminar (briefing §8).
    """
    cliente = _crear_cliente(api_key)
    contador = ContadorUso()

    if progreso:
        progreso("triaje", 0, 0)
    # El precalentamiento de caché no depende del triaje (solo necesita el
    # manual y el contrato), así que se lanza en paralelo con la fase 0 en vez
    # de esperar a que termine: se ahorra ese tiempo por completo.
    with ThreadPoolExecutor(max_workers=1) as pre_pool:
        futuro_cache = pre_pool.submit(_precalentar_cache, cliente, manual, contrato, contador)
        fase0 = fase_0(cliente, contrato, contador)
        futuro_cache.result()

    def progreso_modulos(actual, total):
        if progreso:
            progreso("modulos", actual, total)

    resultados, incidencias, _ = fase_1(
        cliente, fase0, manual, contrato, progreso_modulos,
        cache_precalentada=True, contador=contador,
    )

    if not resultados:
        raise ErrorTriaje(codigo="version_manual_no_coincide", detalle="ningún módulo devolvió resultados")

    agregado = calcular_agregado(fase0, resultados)

    if progreso:
        progreso("informe", 0, 0)
    html = fase_2(cliente, fase0, resultados, incidencias, agregado, contador)

    return {
        "fase0": fase0,
        "resultados": resultados,
        "incidencias_pipeline": incidencias,
        "html": html,
        "agregado": agregado,
        "aviso_transcripcion": verificar_transcripcion(agregado, html),
        "consumo": contador.resumen(),
    }
