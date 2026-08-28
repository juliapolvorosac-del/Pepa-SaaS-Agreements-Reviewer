# -*- coding: utf-8 -*-
"""Revisor de contratos SaaS — interfaz Streamlit.

Tres pantallas (briefing §7): subida, procesando, informe.
Privacidad (briefing §8): nada se persiste; los documentos se procesan en
memoria, se envían únicamente a la API de Anthropic y se descartan.
"""

import time
import traceback
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from extraccion import normalizar_documentos
from pipeline import (
    ErrorProveedorNoDisponible,
    ErrorSaldoInsuficiente,
    ErrorTriaje,
    ErrorTruncamiento,
    ejecutar_analisis,
)

# --- Límites (briefing §8) --------------------------------------------------
MAX_FICHEROS = 5
MAX_MB_TOTAL = 20
MAX_ANALISIS_POR_SESION = 5

# Modo depuración: se activa poniendo MODO_DEPURACION = "true" en los Secrets
# de la app de Streamlit Cloud (pensado para la app desplegada desde la rama
# `depuracion`). Muestra el detalle técnico de los errores. En la app pública
# no debe activarse.
MODO_DEPURACION = str(st.secrets.get("MODO_DEPURACION", "")).lower() in (
    "true", "1", "si", "sí",
)

# Proveedor del modelo. "anthropic" es producción. "mistral" es preproducción a
# coste cero: sirve para comprobar que los cambios de código funcionan, NUNCA
# para valorar un contrato — los prompts están calibrados contra Claude.
PROVEEDOR = str(st.secrets.get("PROVEEDOR", "anthropic")).strip().lower()
if PROVEEDOR not in ("anthropic", "mistral"):
    PROVEEDOR = "anthropic"

CLAVE_API_POR_PROVEEDOR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

TEXTO_CASILLA = (
    "Confirmo que este documento no está sujeto a un acuerdo de confidencialidad "
    "y no contiene datos personales."
)

# Mensajes de error de la fase 0 (briefing §5)
MENSAJES_ERROR = {
    "extraccion_insuficiente": (
        "No hemos podido leer el texto del documento. Si es un PDF escaneado, "
        "necesita OCR previo. Prueba con una versión con texto seleccionable."
    ),
    "documento_no_contractual": (
        "El documento no parece ser un contrato ni un anexo contractual."
    ),
    "version_manual_no_coincide": (
        "No hemos podido completar el análisis. Inténtalo más tarde."
    ),
}

st.set_page_config(
    page_title="Revisor de contratos SaaS",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Columna lateral fija azul oscuro con los disclaimers (maqueta). En móvil,
# Streamlit la colapsa en un desplegable de forma nativa.
st.markdown(
    """
    <style>
    [data-testid="stSidebar"] {
        background-color: #262346;
    }
    [data-testid="stSidebar"] * {
        color: #F2F0EB !important;
        text-align: center;
    }
    [data-testid="stSidebar"] a {
        color: #F2F0EB !important;
        text-decoration: underline;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Columna lateral — disclaimers (copy de la maqueta, tal cual)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        """
**Esta herramienta es un proyecto educativo de Julia Polvorosa Cáceres,
abogada in-house. El resultado obtenido mediante el uso de la misma no
sustituye el asesoramiento legal.**

&nbsp;

Esta herramienta utiliza inteligencia artificial mediante la API de Claude.
Al utilizar esta herramienta, aceptas los
[Commercial Terms of Use](https://www.anthropic.com/legal/commercial-terms)
y el
[Data Processing Addendum](https://www.anthropic.com/legal/data-processing-addendum).

&nbsp;

Los datos introducidos en esta herramienta no serán utilizados para entrenar
modelos de inteligencia artificial. Todos los datos de entrada y de salida
serán eliminados por Anthropic de sus servidores a los 30 días, si bien se
recomienda no introducir información confidencial o datos de carácter
personal.

&nbsp;

&nbsp;

**© Todos los derechos reservados**
Contacto: Puedes ponerte en contacto conmigo mediante un email a
juliapolvorosac@gmail.com
        """
    )


# ---------------------------------------------------------------------------
# Estado de sesión
# ---------------------------------------------------------------------------
if "resultado" not in st.session_state:
    st.session_state.resultado = None
if "analisis_realizados" not in st.session_state:
    st.session_state.analisis_realizados = 0


def _duracion(segundos) -> str:
    """Formatea una duración en lenguaje llano: '45 s', '2 min 34 s'."""
    if segundos is None:
        return "—"
    segundos = int(round(segundos))
    if segundos < 60:
        return f"{segundos} s"
    return f"{segundos // 60} min {segundos % 60:02d} s"


def _cargar_manual() -> str:
    # Ruta relativa al fichero (necesario en Streamlit Cloud) y nombre en
    # minúsculas (Linux distingue mayúsculas).
    ruta = Path(__file__).parent / "manual.txt"
    return ruta.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pantalla 3 — Informe
# ---------------------------------------------------------------------------
def pantalla_informe():
    resultado = st.session_state.resultado
    agregado = resultado["agregado"]

    if resultado.get("proveedor") == "mistral":
        st.warning(
            "🧪 **Informe de PREPRODUCCIÓN — no válido para valorar un contrato.** "
            "Generado con la API gratuita de Mistral para comprobar que la "
            "aplicación funciona. Los prompts y el manual están calibrados "
            "contra Claude: las clasificaciones, el score y los vetos de este "
            "informe no son fiables. Sirve para revisar que el circuito corre y "
            "que la pantalla pinta lo que debe, nada más."
        )

    if resultado.get("informe_truncado"):
        st.warning(
            "⚠️ El informe ha llegado incompleto (el modelo alcanzó su límite "
            "de longitud). Se muestra igualmente porque estás en preproducción."
        )

    # El score, el semáforo y los vetos los calcula ÚNICAMENTE la aplicación
    # (nunca el modelo): una sola fuente de verdad, mostrada aquí de forma
    # nativa para que no dependa de cómo lo transcriba el HTML del informe.
    consumo = resultado.get("consumo")
    tiempos = resultado.get("tiempos") or {}

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric(
        "Score (calculado por la aplicación)",
        f"{agregado['score_pct']} %" if agregado["score_pct"] is not None else "N/D",
    )
    col2.metric("Semáforo", agregado["semaforo"])
    col3.metric("Vetos disparados", "Sí" if agregado["hay_veto_disparado"] else "No")
    if consumo:
        col4.metric("Coste de la revisión", f"{consumo['coste_total_usd']:.2f} $")
    if tiempos.get("total"):
        col5.metric("Duración", _duracion(tiempos["total"]))
    st.caption(
        f"Σ peso×puntuación = {agregado['suma_ponderada']} · "
        f"Σ pesos = {agregado['suma_pesos']} · "
        f"{agregado['n_denominador']} cláusulas en el denominador."
    )

    if consumo:
        def _n(valor):
            """Miles con punto, al modo español."""
            return f"{valor:,}".replace(",", ".")

        # Cada línea del consumo lleva su duración; la clave de la fase coincide
        # con la que usa el cronómetro del pipeline.
        clave_tiempo = {
            "Fase 0 · Triaje": "triaje",
            "Fase 1 · Revisión por módulos": "modulos",
            "Fase 2 · Informe": "informe",
        }

        with st.expander("Detalle del consumo y los tiempos"):
            for fila in consumo["por_fase"]:
                entrada = f"{_n(fila['entrada'])} nuevos"
                if fila["cache_lectura"]:
                    entrada += f" + {_n(fila['cache_lectura'])} desde caché"
                if fila["cache_escritura"]:
                    entrada += f" + {_n(fila['cache_escritura'])} escritos en caché"
                duracion = tiempos.get(clave_tiempo.get(fila["nombre"], ""))
                sufijo_tiempo = f" · ⏱ {_duracion(duracion)}" if duracion else ""
                st.markdown(
                    f"**{fila['nombre']}** — {fila['llamadas']} "
                    f"{'llamada' if fila['llamadas'] == 1 else 'llamadas'} · "
                    f"{fila['modelo']}{sufijo_tiempo}  \n"
                    f"Entrada: {entrada} → {fila['coste_entrada_usd']:.3f} $  \n"
                    f"Generados: {_n(fila['salida'])} → {fila['coste_salida_usd']:.3f} $  \n"
                    f"Subtotal: **{fila['coste_usd']:.3f} $**"
                )
            st.markdown("---")
            st.markdown(
                f"**Total: {consumo['coste_total_usd']:.2f} $** "
                f"({consumo['n_llamadas']} llamadas) · "
                f"**Duración: {_duracion(tiempos.get('total'))}**  \n"
                f"Texto enviado (entrada): {consumo['coste_entrada_usd']:.2f} $ · "
                f"Texto generado (salida): {consumo['coste_salida_usd']:.2f} $ "
                f"— **{consumo['pct_salida']} % del total**  \n"
                f"El texto generado se factura a una tarifa cinco veces mayor "
                f"que el enviado."
            )
            if consumo["cache_lectura"]:
                st.markdown(
                    f"El manual y el contrato se reutilizaron desde la caché "
                    f"({_n(consumo['cache_lectura'])} tokens leídos a una décima "
                    f"parte de tarifa): unos **{consumo['ahorro_cache_usd']:.2f} $** "
                    "menos que enviarlos completos en cada llamada."
                )
            st.caption(
                "Estimación calculada sobre los tokens realmente consumidos y "
                "las tarifas públicas de Anthropic. El importe facturado puede "
                "variar ligeramente."
            )

    # Elementos que la app añade ENCIMA del HTML (briefing §7): el HTML del
    # informe no se toca ni se reestiliza.
    if agregado["intentos_manipulacion"]:
        texto = "\n\n".join(
            f"- **{m['origen']}** (localizador: {m['localizador'] or 'no indicado'})"
            f"\n  > {m['texto_detectado'] or '(sin texto registrado)'}"
            for m in agregado["intentos_manipulacion"]
        )
        st.error(
            "🚨 **Intento de manipulación detectado en el documento.** "
            "El documento contiene texto que parece dirigido al sistema de "
            "análisis (instrucciones de puntuación u órdenes de ignorar reglas). "
            "El análisis lo ha ignorado, pero revísalo con especial atención en "
            "el punto exacto indicado:\n\n" + texto
        )

    if agregado["hay_veto_disparado"]:
        st.error(
            "⛔ **Vetos disparados** (calculado por la aplicación): "
            + " · ".join(agregado["vetos_disparados"])
        )

    if resultado["aviso_transcripcion"]:
        st.warning(
            "⚠️ El texto del informe no reproduce con exactitud el score "
            f"calculado arriba (**{agregado['score_pct']} %**). Toma como "
            "referencia siempre la cifra mostrada en esta pantalla, no la del "
            "documento."
        )

    if resultado["incidencias_pipeline"]:
        st.warning(
            "⚠️ Algunos módulos de revisión no devolvieron resultados válidos y "
            "sus cláusulas no están incluidas en el análisis: "
            + " · ".join(resultado["incidencias_pipeline"])
        )

    components.html(resultado["html"], height=900, scrolling=True)

    if st.button("Analizar otro contrato"):
        st.session_state.resultado = None
        st.rerun()


# ---------------------------------------------------------------------------
# Pantalla 1 — Subida  (+ Pantalla 2 — Procesando, en el mismo flujo)
# ---------------------------------------------------------------------------
def pantalla_subida():
    # Copy de la maqueta, tal cual.
    st.markdown(
        "<h2 style='text-align: center;'>Revisor de Contratos de Provisión "
        "de Servicios SaaS</h2>",
        unsafe_allow_html=True,
    )
    st.markdown(
        """
¿Cómo usar esta herramienta?

1. Sube el contrato SaaS en PDF, DOCX, TXT o MD. El PDF debe tener texto seleccionable: un escaneado sin OCR no sirve.
2. Incluye todos los documentos anexos si los tienes: order form, DPA, SLA, anexos técnicos. Cuantos más subas, más preciso será el análisis.
3. No subas documentos confidenciales, sujetos a acuerdos de confidencialidad, ni con datos personales.
4. Espera al informe. La herramienta no te hará preguntas: cuando falte un dato, asumirá lo más prudente y te dirá qué ha asumido.
        """
    )

    ficheros = st.file_uploader(
        "Arrastra aquí el contrato y sus anexos, o haz clic para seleccionarlos",
        type=["pdf", "docx", "txt", "md"],
        accept_multiple_files=True,
    )

    confirmado = st.checkbox(TEXTO_CASILLA)

    analizar = st.button(
        "Analizar contrato",
        type="primary",
        disabled=not (confirmado and ficheros),
    )

    if not analizar:
        return

    # --- Límites por sesión -------------------------------------------------
    if st.session_state.analisis_realizados >= MAX_ANALISIS_POR_SESION:
        st.error(
            "Has alcanzado el límite de análisis de esta sesión. "
            "Recarga la página para empezar una sesión nueva."
        )
        return
    if len(ficheros) > MAX_FICHEROS:
        st.error(f"Máximo {MAX_FICHEROS} ficheros por análisis.")
        return
    total_mb = sum(f.size for f in ficheros) / (1024 * 1024)
    if total_mb > MAX_MB_TOTAL:
        st.error(f"El tamaño total no puede superar {MAX_MB_TOTAL} MB.")
        return

    nombre_clave = CLAVE_API_POR_PROVEEDOR[PROVEEDOR]
    api_key = st.secrets.get(nombre_clave, "")
    if not api_key:
        st.error(
            "No hemos podido completar el análisis. Inténtalo más tarde. "
            f"(Configuración: falta {nombre_clave} en los Secrets.)"
        )
        return

    # --- Pantalla 2: progreso por fases, no una barra genérica --------------
    # Los mensajes de error se guardan y se pintan FUERA del bloque de estado,
    # para que queden siempre visibles aunque la caja de progreso se pliegue.
    mensaje_error = None
    detalle_tecnico = None

    inicio = time.monotonic()

    with st.status("Preparando el análisis…", expanded=True) as estado:

        def progreso(etapa, actual, total):
            # El reloj avanza en cada cambio de fase y cada vez que termina un
            # módulo: es la señal de que el análisis sigue vivo durante la espera.
            reloj = f" · ⏱ {_duracion(time.monotonic() - inicio)}"
            if etapa == "triaje":
                estado.update(label="Analizando el documento…" + reloj)
            elif etapa == "modulos":
                estado.update(
                    label=f"Revisando cláusulas ({actual} de {total})…" + reloj
                )
            elif etapa == "informe":
                estado.update(label="Preparando el informe…" + reloj)

        try:
            estado.update(label="Leyendo los documentos…")
            contrato = normalizar_documentos([(f.name, f.getvalue()) for f in ficheros])
            manual = _cargar_manual()

            resultado = ejecutar_analisis(
                contrato, manual, api_key, progreso, proveedor=PROVEEDOR
            )

            st.session_state.resultado = resultado
            st.session_state.analisis_realizados += 1
            estado.update(
                label="Análisis completado en "
                + _duracion(time.monotonic() - inicio),
                state="complete",
            )

        except ErrorTriaje as e:
            estado.update(label="Análisis detenido", state="error")
            detalle_tecnico = (
                f"ErrorTriaje · codigo={e.codigo!r} · detalle={e.detalle!r} · "
                f"tipo_detectado={e.tipo_detectado!r}\n\n" + traceback.format_exc()
            )
            if e.codigo == "fuera_de_ambito":
                tipo = e.tipo_detectado or "otro tipo"
                mensaje_error = (
                    f"Este documento parece un contrato de {tipo}, y esta "
                    "herramienta solo analiza contratos SaaS."
                )
            else:
                mensaje_error = MENSAJES_ERROR.get(
                    e.codigo, MENSAJES_ERROR["version_manual_no_coincide"]
                )
        except ErrorProveedorNoDisponible as e:
            estado.update(label="Análisis detenido", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "El servicio no está disponible en este momento por un problema "
                "de configuración ajeno al documento."
                + (f"\n\n**(Modo depuración: {e})**" if MODO_DEPURACION else "")
            )
        except ErrorSaldoInsuficiente:
            estado.update(label="Análisis detenido", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "El servicio no está disponible en este momento por un problema "
                "de configuración ajeno al documento. Vuelve a intentarlo más "
                "tarde."
                + (
                    "\n\n**(Modo depuración: la cuenta de Anthropic se ha quedado "
                    "sin saldo. Recarga en console.anthropic.com → Plans & Billing.)**"
                    if MODO_DEPURACION
                    else ""
                )
            )
        except ErrorTruncamiento:
            estado.update(label="Análisis detenido", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "El informe generado excede el tamaño máximo y ha llegado "
                "incompleto. No lo mostramos para evitar conclusiones parciales. "
                "Inténtalo de nuevo o divide el contrato en menos anexos."
            )
        except Exception:
            # Cualquier otro fallo: extracción de ficheros, red o API (el SDK
            # ya reintentó con backoff). El detalle real queda en el modo
            # depuración; al usuario final no se le enseña la tripa técnica.
            estado.update(label="Análisis detenido", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = "No hemos podido completar el análisis. Inténtalo más tarde."

    if mensaje_error:
        st.error(mensaje_error)
        if MODO_DEPURACION and detalle_tecnico:
            with st.expander("🔧 Detalle técnico (modo depuración)", expanded=True):
                st.code(detalle_tecnico)
    elif st.session_state.resultado is not None:
        st.rerun()


# ---------------------------------------------------------------------------
if st.session_state.resultado is not None:
    pantalla_informe()
else:
    pantalla_subida()
