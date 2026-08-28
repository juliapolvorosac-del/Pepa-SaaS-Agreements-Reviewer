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
import time
import urllib.error
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from prompts import (
    MODULOS,
    PROMPT_FASE_0,
    PROMPT_FASE_2,
    construir_prompt_fase_1,
)

# ---------------------------------------------------------------------------
# Proveedores
#
# "anthropic" es el proveedor de producción. "mistral" existe solo para
# preproducción: permite comprobar a coste cero que los cambios de código
# funcionan (que el pipeline corre entero, que el JSON se parsea, que el
# desplazamiento se aplica, que la interfaz pinta lo que debe).
#
# NO sirve para validar la calidad jurídica del análisis: los prompts están
# calibrados contra Claude y el manual, y un modelo distinto dará
# clasificaciones distintas. Un informe generado con Mistral se mira para ver
# si la máquina funciona, nunca para decidir sobre un contrato.
#
# Diferencias que impone Mistral y que el pipeline resuelve automáticamente:
#  - No hay caché de prompts: el manual y el contrato viajan enteros en cada
#    llamada y no se precalienta nada.
#  - No admite el parámetro de esfuerzo de razonamiento.
#  - El nivel gratuito limita a ~1 petición por segundo: la fase 1 se ejecuta
#    en serie, no en paralelo.
#  - Genera menos tokens: el informe se truncará a menudo. En preproducción se
#    muestra igualmente, con un aviso, para poder revisar la pantalla.
# ---------------------------------------------------------------------------

CONFIG_PROVEEDOR = {
    "anthropic": {
        "modelos": {
            "triaje": "claude-sonnet-5",
            "modulos": "claude-opus-5",
            "informe": "claude-sonnet-5",
        },
        # El razonamiento interno son tokens de salida: se pagan y se esperan.
        # Se reduce solo donde no hay juicio jurídico. La fase 1 —el análisis
        # contra el manual— se mantiene en "high".
        "esfuerzo": {"triaje": "medium", "modulos": "high", "informe": "medium"},
        # El informe (67 cláusulas + redlines) puede ser enorme: se usa el
        # máximo de salida que admite el modelo, en streaming.
        "max_tokens": {"triaje": 16000, "modulos": 32000, "informe": 128000},
        "cache": True,
        "paralelo": True,
        "pausa_entre_llamadas": 0,
        "tolerar_truncamiento": False,
    },
    "mistral": {
        "modelos": {
            "triaje": "mistral-small-latest",
            "modulos": "mistral-small-latest",
            "informe": "mistral-small-latest",
        },
        "esfuerzo": {},
        "max_tokens": {"triaje": 8000, "modulos": 16000, "informe": 32000},
        "cache": False,
        "paralelo": False,
        "pausa_entre_llamadas": 1.2,
        "tolerar_truncamiento": True,
    },
}

PROVEEDOR_POR_DEFECTO = "anthropic"


def config(proveedor: str) -> dict:
    return CONFIG_PROVEEDOR.get(proveedor, CONFIG_PROVEEDOR[PROVEEDOR_POR_DEFECTO])


# Respuesta normalizada: los dos proveedores devuelven objetos distintos y el
# resto del pipeline no debe enterarse de cuál está en uso.
Respuesta = namedtuple("Respuesta", ["texto", "truncada", "uso"])

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
    "claude-haiku-4-5": {"entrada": 1.00, "salida": 5.00},
    # Nivel gratuito de Mistral: coste 0. Los tokens se siguen contando para
    # poder comparar volúmenes entre proveedores.
    "mistral-small-latest": {"entrada": 0.0, "salida": 0.0},
    "mistral-medium-latest": {"entrada": 0.0, "salida": 0.0},
    "mistral-large-latest": {"entrada": 0.0, "salida": 0.0},
    "open-mistral-nemo": {"entrada": 0.0, "salida": 0.0},
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

    def registrar(self, fase: str, modelo: str, uso: dict) -> None:
        if not uso:
            return
        with self._lock:
            self._registros.append({"fase": fase, "modelo": modelo, **uso})

    @staticmethod
    def _coste(registro: dict):
        """Devuelve (coste_entrada, coste_salida) en dólares. Se separan porque
        el texto generado cuesta 5 veces más que el leído: saber cuál domina es
        lo que dice dónde merece la pena optimizar."""
        tarifa = PRECIOS_USD_POR_MILLON.get(registro["modelo"])
        if tarifa is None:
            return 0.0, 0.0
        entrada = (
            registro["entrada"] * tarifa["entrada"]
            + registro["cache_escritura"] * tarifa["entrada"] * MULT_ESCRITURA_CACHE
            + registro["cache_lectura"] * tarifa["entrada"] * MULT_LECTURA_CACHE
        ) / 1_000_000
        salida = registro["salida"] * tarifa["salida"] / 1_000_000
        return entrada, salida

    def resumen(self) -> dict:
        with self._lock:
            registros = list(self._registros)

        por_fase = {}
        total_entrada = total_salida = 0.0
        tokens_entrada = tokens_salida = cache_lectura = cache_escritura = 0
        ahorro = 0.0

        for r in registros:
            coste_entrada, coste_salida = self._coste(r)
            total_entrada += coste_entrada
            total_salida += coste_salida
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
                 "modelo": r["modelo"], "coste_entrada_usd": 0.0,
                 "coste_salida_usd": 0.0, "coste_usd": 0.0,
                 "entrada": 0, "salida": 0, "cache_lectura": 0, "cache_escritura": 0},
            )
            fila["llamadas"] += 1
            fila["coste_entrada_usd"] += coste_entrada
            fila["coste_salida_usd"] += coste_salida
            fila["coste_usd"] += coste_entrada + coste_salida
            fila["entrada"] += r["entrada"]
            fila["salida"] += r["salida"]
            fila["cache_lectura"] += r["cache_lectura"]
            fila["cache_escritura"] += r["cache_escritura"]

        for fila in por_fase.values():
            for clave in ("coste_entrada_usd", "coste_salida_usd", "coste_usd"):
                fila[clave] = round(fila[clave], 4)

        total = total_entrada + total_salida
        return {
            "coste_total_usd": round(total, 4),
            "coste_entrada_usd": round(total_entrada, 4),
            "coste_salida_usd": round(total_salida, 4),
            "pct_salida": round(100 * total_salida / total, 1) if total else 0,
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


class ErrorProveedorNoDisponible(Exception):
    """El proveedor seleccionado no se ha podido inicializar."""


class ClienteMistral:
    """Cliente mínimo para la API de Mistral, construido sobre `urllib`.

    Preproducción no debe poder romperse por un problema de empaquetado, así que
    aquí no se depende de NADA externo: `urllib` forma parte de la biblioteca
    estándar de Python. Ni el SDK `mistralai` (cuyas dependencias no siempre
    tienen versión compatible con el Python del entorno de despliegue) ni
    `httpx`. La API de Mistral es un POST con formato estilo OpenAI y eso se
    resuelve con la librería estándar sin dificultad.
    """

    URL = "https://api.mistral.ai/v1/chat/completions"

    def __init__(self, api_key: str, timeout: float = 600.0):
        self.api_key = api_key
        self.timeout = timeout

    def completar(self, modelo: str, mensajes: list, max_tokens: int) -> dict:
        cuerpo = json.dumps(
            {"model": modelo, "messages": mensajes, "max_tokens": max_tokens}
        ).encode("utf-8")

        for intento in range(3):
            peticion = urllib.request.Request(
                self.URL,
                data=cuerpo,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(peticion, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 429 = límite de peticiones del plan gratuito. Se espera y
                # se reintenta; el resto de errores se propagan con su detalle.
                if e.code == 429 and intento < 2:
                    time.sleep(3 * (intento + 1))
                    continue
                detalle = e.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Mistral devolvió HTTP {e.code}: {detalle}") from e
        raise RuntimeError("Mistral: límite de peticiones superado tras 3 intentos")


def _crear_cliente(api_key: str, proveedor: str):
    if proveedor == "mistral":
        try:
            return ClienteMistral(api_key)
        except Exception as e:
            raise ErrorProveedorNoDisponible(
                f"No se ha podido inicializar el cliente de Mistral: {e}"
            ) from e
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


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


def _llamada_anthropic(cliente, modelo, system, contenido_usuario, max_tokens, esfuerzo):
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
    except anthropic.BadRequestError as e:
        # Falta de saldo: no es un fallo del análisis, y reintentarlo no sirve
        # de nada. Se distingue para poder dar un mensaje honesto.
        if "credit balance" in str(e).lower():
            raise ErrorSaldoInsuficiente() from e
        raise

    uso = getattr(mensaje, "usage", None)
    return Respuesta(
        texto="".join(b.text for b in mensaje.content if b.type == "text"),
        truncada=(mensaje.stop_reason == "max_tokens"),
        uso={
            "entrada": getattr(uso, "input_tokens", 0) or 0,
            "salida": getattr(uso, "output_tokens", 0) or 0,
            "cache_escritura": getattr(uso, "cache_creation_input_tokens", 0) or 0,
            "cache_lectura": getattr(uso, "cache_read_input_tokens", 0) or 0,
        },
    )


def _llamada_mistral(cliente, modelo, system, contenido_usuario, max_tokens):
    """Mistral usa el formato estilo OpenAI: el prompt de sistema es un mensaje
    más, el contenido es texto plano y no hay caché ni esfuerzo."""
    mensajes = []
    if system is not None:
        mensajes.append({"role": "system", "content": system})
    mensajes.append({"role": "user", "content": _a_texto_plano(contenido_usuario)})

    datos = cliente.completar(modelo, mensajes, max_tokens)
    eleccion = (datos.get("choices") or [{}])[0]
    uso = datos.get("usage") or {}
    return Respuesta(
        texto=(eleccion.get("message") or {}).get("content") or "",
        truncada=(eleccion.get("finish_reason") == "length"),
        uso={
            "entrada": uso.get("prompt_tokens", 0) or 0,
            "salida": uso.get("completion_tokens", 0) or 0,
            "cache_escritura": 0,
            "cache_lectura": 0,
        },
    )


def _a_texto_plano(contenido) -> str:
    """Aplana los bloques de contenido al texto que espera Mistral."""
    if isinstance(contenido, str):
        return contenido
    return "\n\n".join(
        bloque.get("text", "") for bloque in contenido if isinstance(bloque, dict)
    )


def _llamada(cliente, *, proveedor, modelo, system=None, contenido_usuario,
             max_tokens, esfuerzo=None, contador=None, fase=None):
    """Llamada al proveedor activo; devuelve una `Respuesta` normalizada."""
    if proveedor == "mistral":
        respuesta = _llamada_mistral(cliente, modelo, system, contenido_usuario, max_tokens)
    else:
        respuesta = _llamada_anthropic(
            cliente, modelo, system, contenido_usuario, max_tokens, esfuerzo
        )
    if contador is not None:
        contador.registrar(fase, modelo, respuesta.uso)
    return respuesta


# ---------------------------------------------------------------------------
# FASE 0 · Triaje
# ---------------------------------------------------------------------------

def fase_0(cliente, contrato: str, contador=None, proveedor=PROVEEDOR_POR_DEFECTO) -> dict:
    cfg = config(proveedor)
    respuesta = _llamada(
        cliente,
        proveedor=proveedor,
        modelo=cfg["modelos"]["triaje"],
        system=PROMPT_FASE_0,
        contenido_usuario=contrato,
        max_tokens=cfg["max_tokens"]["triaje"],
        esfuerzo=cfg["esfuerzo"].get("triaje"),
        contador=contador,
        fase="triaje",
    )
    if respuesta.truncada:
        raise ValueError("fase 0: respuesta truncada por max_tokens")
    resultado = _extraer_json(respuesta.texto)
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

def _bloques_cacheados(manual: str, contrato: str, cache: bool = True) -> list:
    """Los dos bloques compartidos por todas las llamadas de fase 1. Con caché,
    van marcados como cacheables y deben ir en el MISMO orden en todas las
    llamadas: el caché de la API es un prefijo exacto. Sin caché (Mistral) son
    bloques de texto normales que viajan enteros en cada llamada."""
    bloques = [
        {"type": "text", "text": "## MANUAL DE REVISIÓN\n\n" + manual},
        {"type": "text", "text": "## CONTRATO A REVISAR\n\n" + contrato},
    ]
    if cache:
        for bloque in bloques:
            bloque["cache_control"] = {"type": "ephemeral"}
    return bloques


def _precalentar_cache(cliente, manual: str, contrato: str, contador=None,
                       proveedor=PROVEEDOR_POR_DEFECTO) -> None:
    """Escribe el prefijo manual+contrato en el caché con una llamada mínima,
    para que las llamadas paralelas de la fase 1 lo lean en vez de escribirlo
    cada una por su cuenta. Si falla, se continúa sin caché: solo afecta al coste."""
    cfg = config(proveedor)
    if not cfg["cache"]:
        return  # Mistral no tiene caché de prompts: no hay nada que precalentar.

    # El caché es por modelo: el precalentamiento debe usar el MISMO modelo que
    # las llamadas de la fase 1.
    modelo = cfg["modelos"]["modulos"]

    def _intento(max_tokens):
        mensaje = cliente.messages.create(
            model=modelo,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": _bloques_cacheados(manual, contrato)}],
        )
        uso = getattr(mensaje, "usage", None)
        if contador is not None and uso is not None:
            contador.registrar("cache", modelo, {
                "entrada": getattr(uso, "input_tokens", 0) or 0,
                "salida": getattr(uso, "output_tokens", 0) or 0,
                "cache_escritura": getattr(uso, "cache_creation_input_tokens", 0) or 0,
                "cache_lectura": getattr(uso, "cache_read_input_tokens", 0) or 0,
            })

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
                    reintento: bool = False, contador=None, proveedor=PROVEEDOR_POR_DEFECTO):
    """Una llamada de fase 1. El prompt del módulo va DESPUÉS de los bloques
    cacheados para no romper el prefijo compartido del caché (el prompt cambia
    por módulo; el manual y el contrato no)."""
    cfg = config(proveedor)
    contenido = _bloques_cacheados(manual, contrato, cfg["cache"]) + [
        {"type": "text", "text": construir_prompt_fase_1(id_modulo, json_fase_0, reintento=reintento)}
    ]
    respuesta = _llamada(
        cliente,
        proveedor=proveedor,
        modelo=cfg["modelos"]["modulos"],
        contenido_usuario=contenido,
        max_tokens=cfg["max_tokens"]["modulos"],
        esfuerzo=cfg["esfuerzo"].get("modulos"),
        contador=contador,
        fase="modulos",
    )
    if respuesta.truncada:
        raise ValueError("respuesta truncada")
    resultado = _extraer_json(respuesta.texto)
    if isinstance(resultado, dict) and "error" in resultado:
        raise ErrorTriaje(codigo=resultado["error"], detalle=str(resultado))
    esperadas = len(MODULOS[id_modulo]["clausulas"])
    if not isinstance(resultado, list) or len(resultado) != esperadas:
        recibidas = len(resultado) if isinstance(resultado, list) else "?"
        raise ValueError(f"esperadas {esperadas} cláusulas, recibidas {recibidas}")
    return resultado


def fase_1(cliente, fase0: dict, manual: str, contrato: str, progreso=None,
           cache_precalentada=False, contador=None, proveedor=PROVEEDOR_POR_DEFECTO):
    """Lanza los módulos en paralelo. Si un módulo falla (recuento, JSON o
    truncamiento), se reintenta UNA vez con una nota correctiva —sin ella, un
    reintento con la misma entrada exacta puede reproducir el mismo fallo
    determinista—; si vuelve a fallar, se continúa y se registra como
    incidencia para la fase 2 (briefing §2)."""
    modulos = ["M1", "M2", "M3", "M4", "M5", "M6"]
    if fase0.get("modulo_aplicable") == "EEUU":
        modulos.append("M7")

    cfg = config(proveedor)
    json_fase_0 = json.dumps(fase0, ensure_ascii=False, indent=2)
    if not cache_precalentada:
        _precalentar_cache(cliente, manual, contrato, contador, proveedor)

    resultados = {}
    incidencias = []
    pausa = cfg["pausa_entre_llamadas"]

    def tarea(id_modulo):
        try:
            return id_modulo, _revisar_modulo(
                cliente, id_modulo, manual, contrato, json_fase_0,
                contador=contador, proveedor=proveedor
            ), None
        except (ErrorTriaje, ErrorSaldoInsuficiente):
            # Sin saldo no tiene sentido reintentar ni seguir con los demás
            # módulos: se propaga de inmediato.
            raise
        except Exception as primera:
            try:
                if pausa:
                    time.sleep(pausa)
                resultado = _revisar_modulo(
                    cliente, id_modulo, manual, contrato, json_fase_0,
                    reintento=True, contador=contador, proveedor=proveedor
                )
                return id_modulo, resultado, None
            except (ErrorTriaje, ErrorSaldoInsuficiente):
                raise
            except Exception as segunda:
                return id_modulo, None, f"Módulo {id_modulo}: falló dos veces ({primera}; {segunda})"

    def acumular(id_modulo, resultado, incidencia, completados):
        if resultado is not None:
            resultados[id_modulo] = resultado
        if incidencia:
            incidencias.append(incidencia)
        if progreso:
            progreso(completados, len(modulos))

    if cfg["paralelo"]:
        completados = 0
        with ThreadPoolExecutor(max_workers=len(modulos)) as pool:
            futuros = [pool.submit(tarea, m) for m in modulos]
            for futuro in as_completed(futuros):
                id_modulo, resultado, incidencia = futuro.result()
                completados += 1
                acumular(id_modulo, resultado, incidencia, completados)
    else:
        # Nivel gratuito de Mistral: ~1 petición por segundo. En paralelo daría
        # error 429 en todos los módulos menos el primero.
        for completados, m in enumerate(modulos, start=1):
            id_modulo, resultado, incidencia = tarea(m)
            acumular(id_modulo, resultado, incidencia, completados)
            if pausa and completados < len(modulos):
                time.sleep(pausa)

    return resultados, incidencias, modulos


# ---------------------------------------------------------------------------
# FASE 2 · Consolidación e informe
# ---------------------------------------------------------------------------

def fase_2(cliente, fase0: dict, resultados: dict, incidencias_pipeline: list,
           agregado: dict, contador=None, proveedor=PROVEEDOR_POR_DEFECTO):
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

    cfg = config(proveedor)
    respuesta = _llamada(
        cliente,
        proveedor=proveedor,
        modelo=cfg["modelos"]["informe"],
        system=PROMPT_FASE_2,
        contenido_usuario="\n\n---\n\n".join(partes),
        max_tokens=cfg["max_tokens"]["informe"],
        esfuerzo=cfg["esfuerzo"].get("informe"),
        contador=contador,
        fase="informe",
    )
    if respuesta.truncada and not cfg["tolerar_truncamiento"]:
        raise ErrorTruncamiento()
    html = respuesta.texto.strip()
    if html.startswith("```"):
        html = re.sub(r"^```[a-zA-Z]*\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    # En preproducción se devuelve el informe aunque esté cortado, con el aviso,
    # para poder revisar la pantalla. En producción esto no ocurre nunca.
    return html, respuesta.truncada


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

def ejecutar_analisis(contrato: str, manual: str, api_key: str, progreso=None,
                      proveedor=PROVEEDOR_POR_DEFECTO) -> dict:
    """Ejecuta las tres fases. `progreso` es un callable(etapa, actual, total)
    con etapa en {"triaje", "modulos", "informe"}.

    `proveedor` es "anthropic" (producción) o "mistral" (preproducción a coste
    cero, solo para comprobar que el código funciona).

    Nada se persiste: todo vive en memoria y se descarta al terminar (briefing §8).
    """
    cfg = config(proveedor)
    cliente = _crear_cliente(api_key, proveedor)
    contador = ContadorUso()
    tiempos = {}
    inicio_total = time.monotonic()

    if progreso:
        progreso("triaje", 0, 0)
    marca = time.monotonic()
    if cfg["cache"]:
        # El precalentamiento de caché no depende del triaje (solo necesita el
        # manual y el contrato), así que se lanza en paralelo con la fase 0 en
        # vez de esperar a que termine: se ahorra ese tiempo por completo.
        with ThreadPoolExecutor(max_workers=1) as pre_pool:
            futuro_cache = pre_pool.submit(
                _precalentar_cache, cliente, manual, contrato, contador, proveedor
            )
            fase0 = fase_0(cliente, contrato, contador, proveedor)
            futuro_cache.result()
    else:
        fase0 = fase_0(cliente, contrato, contador, proveedor)
    tiempos["triaje"] = time.monotonic() - marca

    def progreso_modulos(actual, total):
        if progreso:
            progreso("modulos", actual, total)

    marca = time.monotonic()
    resultados, incidencias, _ = fase_1(
        cliente, fase0, manual, contrato, progreso_modulos,
        cache_precalentada=True, contador=contador, proveedor=proveedor,
    )
    tiempos["modulos"] = time.monotonic() - marca

    if not resultados:
        raise ErrorTriaje(codigo="version_manual_no_coincide", detalle="ningún módulo devolvió resultados")

    agregado = calcular_agregado(fase0, resultados)

    if progreso:
        progreso("informe", 0, 0)
    marca = time.monotonic()
    html, informe_truncado = fase_2(
        cliente, fase0, resultados, incidencias, agregado, contador, proveedor
    )
    tiempos["informe"] = time.monotonic() - marca
    tiempos["total"] = time.monotonic() - inicio_total

    return {
        "fase0": fase0,
        "resultados": resultados,
        "incidencias_pipeline": incidencias,
        "html": html,
        "agregado": agregado,
        "aviso_transcripcion": verificar_transcripcion(agregado, html),
        "consumo": contador.resumen(),
        "tiempos": tiempos,
        "proveedor": proveedor,
        "informe_truncado": informe_truncado,
    }
