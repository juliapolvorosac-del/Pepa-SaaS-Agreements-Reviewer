# -*- coding: utf-8 -*-
"""Revisor de contratos SaaS — interfaz Streamlit.

Tres pantallas (briefing §7): subida, procesando, informe.
Privacidad (briefing §8): nada se persiste; los documentos se procesan en
memoria, se envían únicamente a la API de Anthropic y se descartan.
"""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from extraccion import normalizar_documentos
from pipeline import ErrorTriaje, ErrorTruncamiento, ejecutar_analisis

# --- Límites (briefing §8) --------------------------------------------------
MAX_FICHEROS = 5
MAX_MB_TOTAL = 20
MAX_ANALISIS_POR_SESION = 5

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
    verificacion = resultado["verificacion"]

    # Elementos que la app añade ENCIMA del HTML (briefing §7): el HTML del
    # informe no se toca ni se reestiliza.
    if verificacion["manipulacion"]:
        st.error(
            "🚨 **Posible intento de manipulación detectado en el documento.** "
            "El documento contiene texto que parece dirigido al sistema de "
            "análisis (instrucciones de puntuación u órdenes de ignorar reglas). "
            "El análisis lo ha ignorado, pero revísalo con especial atención:\n\n- "
            + "\n- ".join(verificacion["manipulacion"])
        )

    if verificacion["vetos_disparados"]:
        st.error(
            "⛔ **Vetos disparados** (verificado de forma independiente por la "
            "aplicación): " + " · ".join(verificacion["vetos_disparados"])
        )

    if verificacion["discrepancia_score"]:
        st.warning(
            "⚠️ **Aviso de verificación:** el score que muestra el informe no "
            "coincide con el recálculo independiente realizado por la aplicación "
            f"(**{verificacion['score_recalculado']} %**, con "
            f"Σ peso×puntuación = {verificacion['suma_ponderada']}, "
            f"Σ pesos = {verificacion['suma_pesos']} sobre "
            f"{verificacion['n_denominador']} cláusulas). "
            "Toma como referencia el valor recalculado."
        )

    if resultado["incidencias_pipeline"]:
        st.warning(
            "⚠️ Algunos módulos de revisión no devolvieron resultados válidos y "
            "sus cláusulas no están incluidas en el análisis: "
            + " · ".join(resultado["incidencias_pipeline"])
        )

    st.download_button(
        "⬇️ Descargar informe (HTML)",
        data=resultado["html"],
        file_name="informe_revision_saas.html",
        mime="text/html",
    )
    st.caption(
        "El informe descargado es un documento autónomo: puedes abrirlo en una "
        "pestaña nueva del navegador o compartirlo; los avisos legales viajan "
        "dentro del propio documento."
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

    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        st.error(
            "No hemos podido completar el análisis. Inténtalo más tarde. "
            "(Configuración: falta ANTHROPIC_API_KEY en los Secrets.)"
        )
        return

    # --- Pantalla 2: progreso por fases, no una barra genérica --------------
    with st.status("Preparando el análisis…", expanded=True) as estado:

        def progreso(etapa, actual, total):
            if etapa == "triaje":
                estado.update(label="Analizando el documento…")
            elif etapa == "modulos":
                estado.update(label=f"Revisando cláusulas ({actual} de {total})…")
            elif etapa == "informe":
                estado.update(label="Preparando el informe…")

        try:
            estado.update(label="Leyendo los documentos…")
            contrato = normalizar_documentos([(f.name, f.getvalue()) for f in ficheros])
            manual = _cargar_manual()

            resultado = ejecutar_analisis(contrato, manual, api_key, progreso)

            st.session_state.resultado = resultado
            st.session_state.analisis_realizados += 1
            estado.update(label="Análisis completado", state="complete")
            st.rerun()

        except ErrorTriaje as e:
            estado.update(label="Análisis detenido", state="error")
            if e.codigo == "fuera_de_ambito":
                tipo = e.tipo_detectado or "otro tipo"
                st.error(
                    f"Este documento parece un contrato de {tipo}, y esta "
                    "herramienta solo analiza contratos SaaS."
                )
            else:
                st.error(
                    MENSAJES_ERROR.get(
                        e.codigo, MENSAJES_ERROR["version_manual_no_coincide"]
                    )
                )
        except ErrorTruncamiento:
            estado.update(label="Análisis detenido", state="error")
            st.error(
                "El informe generado excede el tamaño máximo y ha llegado "
                "incompleto. No lo mostramos para evitar conclusiones parciales. "
                "Inténtalo de nuevo o divide el contrato en menos anexos."
            )
        except ValueError:
            estado.update(label="Análisis detenido", state="error")
            st.error(
                "No hemos podido leer alguno de los ficheros. Comprueba que el "
                "formato es PDF, DOCX, TXT o MD."
            )
        except Exception:
            # Errores de red o de API: el SDK ya reintentó con backoff.
            estado.update(label="Análisis detenido", state="error")
            st.error("No hemos podido completar el análisis. Inténtalo más tarde.")


# ---------------------------------------------------------------------------
if st.session_state.resultado is not None:
    pantalla_informe()
else:
    pantalla_subida()
