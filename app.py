# -*- coding: utf-8 -*-
"""Revisor de contratos SaaS — interfaz Streamlit.

Tres pantallas (briefing §7): subida, procesando, informe.
Privacidad (briefing §8): nada se persiste; los documentos se procesan en
memoria, se envían únicamente a la API de Anthropic y se descartan.
"""

import hmac
import html
import time
import traceback
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from extraccion import normalizar_documentos
from prompts import TRAMOS
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

# Contraseña de acceso. La app es pública en internet pero cada análisis se
# paga con la clave API de la titular: sin esta barrera, cualquiera podría
# agotar el saldo (el límite por sesión se esquiva recargando la página). Se
# configura en los Secrets de Streamlit Cloud y se comparte solo con quien
# deba probar la herramienta. Si falta en Secrets, la app queda cerrada:
# mejor cerrada que abierta por accidente.
CONTRASENA_ACCESO = str(st.secrets.get("CONTRASENA_ACCESO", ""))

TRAMO_POR_DEFECTO = "B"

# Mensajes de error de la fase 0 (briefing §5). La interfaz está en inglés.
MENSAJES_ERROR = {
    "extraccion_insuficiente": (
        "We could not read the text of the document. If it is a scanned PDF, it "
        "needs OCR first. Try a version with selectable text."
    ),
    "documento_no_contractual": (
        "This document does not appear to be a contract or a contractual annex."
    ),
    "version_manual_no_coincide": (
        "We could not complete the analysis. Please try again later."
    ),
}

st.set_page_config(
    page_title="PEPA. SaaS Contract Reviewer",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Tema de marca -----------------------------------------------------------
# Todo el tema vive aquí, en CSS, para no depender de un fichero de
# configuración aparte. Colores:
#   #F8F3F3  blanco cálido → fondo
#   #2F2E48  índigo        → texto, barra lateral, botones y barra de progreso
#   #ABA8E8  malva claro   → bordes, acentos y enlaces sobre fondo oscuro
#
# El malva es un color CLARO: sobre el fondo blanco no tiene contraste
# suficiente para texto, así que nunca se usa para leer, solo para delimitar.
# El texto va siempre índigo sobre claro, o blanco cálido sobre índigo.
st.markdown(
    """
    <style>
    /* ---------- Fondo y tipografía generales ----------
       Solo se usan los tres colores de marca. Donde hace falta jerarquía
       (textos secundarios, elementos deshabilitados) se recurre a la
       transparencia del índigo, no a un color nuevo. */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"] {
        background-color: #F8F3F3;
    }
    [data-testid="stHeader"] { background: transparent; }

    /* Todo el texto de la zona principal en índigo, nunca en negro */
    [data-testid="stMain"], [data-testid="stMain"] * {
        color: #2F2E48;
    }
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {
        color: rgba(47, 46, 72, 0.72) !important;
    }

    /* ---------- Barra lateral: fija, sin desplazamiento ---------- */
    [data-testid="stSidebar"],
    [data-testid="stSidebarContent"],
    [data-testid="stSidebarUserContent"] {
        background-color: #2F2E48 !important;
    }
    /* Todo el texto de la barra lateral en blanco cálido, enlaces incluidos */
    [data-testid="stSidebar"] *,
    [data-testid="stSidebar"] a,
    [data-testid="stSidebar"] a * {
        color: #F8F3F3 !important;
        text-align: center;
    }
    [data-testid="stSidebar"] a { text-decoration: underline; }

    /* Se oculta el control de plegado: la columna es siempre visible */
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    [data-testid="stSidebarHeader"] button,
    [data-testid="stSidebarNavSeparator"] {
        display: none !important;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
        padding-top: 0.5rem;
        height: auto;
    }

    /* El contenido se reparte por toda la franja, de arriba abajo, en vez de
       amontonarse en la cabecera. Se maqueta como una columna flexible de
       altura completa para no depender de espaciados manuales. */
    .lateral-pepa {
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        min-height: calc(100vh - 7rem);
        padding: 0.5rem 0 1rem;
    }
    .lateral-pepa p {
        margin: 0;
        font-size: 0.95rem;
        line-height: 1.5;
    }
    .lateral-pepa p.destacado {
        font-size: 1rem;
        font-weight: 700;
        line-height: 1.45;
    }
    .lateral-pepa a {
        color: #F8F3F3 !important;
        text-decoration: underline;
    }
    [data-testid="stSidebarUserContent"] {
        padding-top: 1.4rem;
    }

    /* Anclada en pantallas de escritorio. Por debajo de 768 px se deja que
       Streamlit la recoja, o el contenido taparía la aplicación en el móvil. */
    @media (min-width: 768px) {
        [data-testid="stSidebar"] {
            min-width: 330px !important;
            max-width: 330px !important;
            transform: none !important;
            visibility: visible !important;
        }
    }

    /* ---------- Botones ----------
       Todo botón de la zona principal va en índigo con texto blanco cálido.
       El color hay que forzarlo TAMBIÉN en los elementos internos: Streamlit
       envuelve la etiqueta en otra etiqueta, y la regla general de arriba la
       pintaría de índigo sobre índigo, dejándola ilegible. */
    .stButton > button,
    .stDownloadButton > button,
    [data-testid="stBaseButton-primary"],
    [data-testid="stBaseButton-secondary"],
    [data-testid="stBaseButton-secondaryFormSubmit"],
    [data-testid="stFileUploader"] button,
    [data-testid="stFileUploaderDropzone"] button {
        background-color: #2F2E48 !important;
        border: 1px solid #2F2E48 !important;
        border-radius: 8px !important;
        font-weight: 600;
    }
    .stButton > button, .stButton > button *,
    .stDownloadButton > button, .stDownloadButton > button *,
    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primary"] *,
    [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondary"] *,
    [data-testid="stBaseButton-secondaryFormSubmit"],
    [data-testid="stBaseButton-secondaryFormSubmit"] *,
    [data-testid="stFileUploader"] button,
    [data-testid="stFileUploader"] button *,
    [data-testid="stFileUploaderDropzone"] button,
    [data-testid="stFileUploaderDropzone"] button * {
        color: #F8F3F3 !important;
        fill: #F8F3F3 !important;
    }
    .stButton > button:hover:enabled,
    [data-testid="stFileUploader"] button:hover,
    [data-testid="stBaseButton-secondary"]:hover {
        background-color: rgba(47, 46, 72, 0.86) !important;
        border-color: #2F2E48 !important;
    }
    .stButton > button:disabled, .stButton > button:disabled * {
        background-color: #ABA8E8 !important;
        border-color: #ABA8E8 !important;
        color: rgba(47, 46, 72, 0.62) !important;
        fill: rgba(47, 46, 72, 0.62) !important;
    }

    /* ---------- Barra de progreso ---------- */
    [data-testid="stProgress"] div[role="progressbar"] > div,
    [data-testid="stProgress"] > div > div > div > div {
        background-color: #2F2E48 !important;
    }
    [data-testid="stProgress"] > div > div > div {
        background-color: #ABA8E8 !important;
    }

    /* ---------- Selector del nivel de exigencia ---------- */
    [role="radiogroup"] {
        background: rgba(171, 168, 232, 0.15);
        border: 1px solid #ABA8E8;
        border-radius: 10px;
        padding: 12px 16px;
    }
    [role="radiogroup"] [data-baseweb="radio"] div[aria-checked="true"],
    [role="radiogroup"] [data-baseweb="radio"] > div:first-child {
        border-color: #2F2E48 !important;
    }
    [role="radiogroup"] [data-baseweb="radio"] div[aria-checked="true"] {
        background-color: #2F2E48 !important;
    }

    /* ---------- Métricas del informe ---------- */
    [data-testid="stMetric"] {
        background: rgba(171, 168, 232, 0.15);
        border: 1px solid #ABA8E8;
        border-radius: 10px;
        padding: 14px 16px;
    }
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
        color: #2F2E48 !important;
        font-weight: 600;
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {
        color: rgba(47, 46, 72, 0.72) !important;
    }

    /* ---------- Zona de subida, desplegables y estado ---------- */
    [data-testid="stFileUploaderDropzone"] {
        background-color: rgba(171, 168, 232, 0.15) !important;
        border: 1.5px dashed #ABA8E8 !important;
    }
    /* El nombre del fichero subido y la cruz para quitarlo van sobre fondo
       claro: aquí el índigo es lo correcto, no el blanco de los botones. */
    [data-testid="stFileUploaderFile"],
    [data-testid="stFileUploaderFile"] *,
    [data-testid="stFileUploaderDeleteBtn"],
    [data-testid="stFileUploaderDeleteBtn"] * {
        color: #2F2E48 !important;
        fill: #2F2E48 !important;
        background-color: transparent !important;
        border: none !important;
    }
    [data-testid="stExpander"] details, [data-testid="stExpander"] summary {
        border-color: #ABA8E8 !important;
        border-radius: 10px;
        background: transparent;
    }

    /* ---------- Título ---------- */
    .titulo-pepa {
        text-align: center;
        color: #2F2E48;
        font-weight: 700;
        letter-spacing: -0.01em;
        margin: 0.2em 0 0.1em;
    }
    .regla-pepa {
        border: none;
        border-top: 2px solid #ABA8E8;
        width: 84px;
        margin: 0 auto 1.2em;
    }
    /* Marco de referencia: se declara en la propia pantalla de subida, no solo
       dentro del informe, para que el usuario sepa a qué derecho responde el
       análisis antes de subir nada. */
    .marco-pepa {
        text-align: center;
        max-width: 640px;
        margin: 0 auto 2em;
        padding: 10px 18px;
        border: 1px solid #ABA8E8;
        border-radius: 8px;
        background: rgba(171, 168, 232, 0.15);
        font-size: 0.92rem;
        line-height: 1.45;
        color: #2F2E48;
    }

    /* ---------- Línea de vetos, bajo las métricas ---------- */
    .vetos-pepa {
        border: 1px solid #ABA8E8;
        border-left: 5px solid #2F2E48;
        border-radius: 8px;
        padding: 12px 16px;
        margin: 4px 0 14px;
        font-size: 0.94rem;
        color: #2F2E48;
    }
    .vetos-pepa strong { font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Columna lateral — disclaimers (copy de la maqueta, tal cual)
# ---------------------------------------------------------------------------
with st.sidebar:
    # Se maqueta en HTML directo para poder repartir los bloques por toda la
    # altura de la franja: Streamlit los apilaría arriba dejando el resto vacío.
    st.markdown(
        """
<div class="lateral-pepa">
  <p class="destacado">This tool is an educational project by Julia Polvorosa
  Cáceres, in-house lawyer. The result obtained through its use does not
  replace legal advice.</p>

  <p>Reviews are carried out from the standpoint of the party acquiring the
  service, against a review playbook built on <strong>Spanish and European
  Union law</strong>.</p>

  <p>This tool uses artificial intelligence through the Claude API. By using
  this tool, you accept the
  <a href="https://www.anthropic.com/legal/commercial-terms">Commercial Terms
  of Use</a> and the
  <a href="https://www.anthropic.com/legal/data-processing-addendum">Data
  Processing Addendum</a>.</p>

  <p>Data entered into this tool will not be used to train artificial
  intelligence models.</p>

  <p class="destacado">© All rights reserved</p>

  <p>Contact: juliapolvorosac@gmail.com</p>
</div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Estado de sesión
# ---------------------------------------------------------------------------
if "resultado" not in st.session_state:
    st.session_state.resultado = None
if "analisis_realizados" not in st.session_state:
    st.session_state.analisis_realizados = 0
if "autenticado" not in st.session_state:
    st.session_state.autenticado = False


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
# Pantalla 0 — Acceso
# ---------------------------------------------------------------------------
def pantalla_acceso():
    st.markdown(
        "<h2 class='titulo-pepa'>PEPA. SaaS Contract Reviewer</h2>"
        "<hr class='regla-pepa'>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "This is a private demo: every analysis makes real calls to the Claude "
        "API, so access is limited to invited users. To request the password, "
        "write to juliapolvorosac@gmail.com."
    )
    # Formulario para que Enter también envíe la contraseña.
    with st.form("acceso"):
        clave = st.text_input("Access password", type="password")
        entrar = st.form_submit_button("Enter")
    if entrar:
        # compare_digest: comparación en tiempo constante, no filtra por
        # tiempos de respuesta cuánto prefijo de la contraseña coincide.
        if hmac.compare_digest(clave, CONTRASENA_ACCESO):
            st.session_state.autenticado = True
            st.rerun()
        else:
            st.error("Incorrect password.")


# ---------------------------------------------------------------------------
# Pantalla 3 — Informe
# ---------------------------------------------------------------------------
def pantalla_informe():
    resultado = st.session_state.resultado
    agregado = resultado["agregado"]

    if resultado.get("informe_truncado"):
        st.warning(
            "⚠️ The report arrived incomplete: the length limit was reached "
            "before it finished. Read it with that caveat in mind."
        )

    # El score, el semáforo y los vetos los calcula ÚNICAMENTE la aplicación
    # (nunca el modelo): una sola fuente de verdad, mostrada aquí de forma
    # nativa para que no dependa de cómo lo transcriba el HTML del informe.
    consumo = resultado.get("consumo")
    tiempos = resultado.get("tiempos") or {}

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Score (computed by the app)",
        f"{agregado['score_pct']} %" if agregado["score_pct"] is not None else "N/A",
    )
    col2.metric("Traffic light", agregado["semaforo"])
    if consumo:
        col3.metric("Cost of the review", f"${consumo['coste_total_usd']:.2f}")
    if tiempos.get("total"):
        col4.metric("Duration", _duracion(tiempos["total"]))
    st.caption(
        f"Σ weight×score = {agregado['suma_ponderada']} · "
        f"Σ weights = {agregado['suma_pesos']} · "
        f"{agregado['n_denominador']} clauses in the denominator."
    )

    # Los vetos ya no van en una tarjeta estrecha: ocupan una línea entera bajo
    # las métricas, donde caben los nombres de las cláusulas afectadas.
    if agregado["hay_veto_disparado"]:
        # Los nombres de las cláusulas vienen de la salida del modelo, que a su
        # vez lee el contrato: se escapan antes de insertarlos en HTML.
        st.markdown(
            "<div class='vetos-pepa'><strong>⛔ Vetoes triggered:</strong> "
            + " · ".join(html.escape(v) for v in agregado["vetos_disparados"])
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='vetos-pepa'><strong>Vetoes triggered:</strong> none. "
            "None of the playbook's thirteen critical clauses falls into a "
            "rejected position.</div>",
            unsafe_allow_html=True,
        )

    if consumo:
        def _n(valor):
            """Separador de miles con coma, al modo inglés."""
            return f"{valor:,}"

        # Cada línea del consumo lleva su duración; la clave de la fase coincide
        # con la que usa el cronómetro del pipeline.
        clave_tiempo = {
            "Phase 0 · Triage": "triaje",
            "Phase 1 · Clause review": "modulos",
            "Phase 2 · Report": "informe",
        }

        with st.expander("Usage and timing detail"):
            for fila in consumo["por_fase"]:
                entrada = f"{_n(fila['entrada'])} new"
                if fila["cache_lectura"]:
                    entrada += f" + {_n(fila['cache_lectura'])} read from cache"
                if fila["cache_escritura"]:
                    entrada += f" + {_n(fila['cache_escritura'])} written to cache"
                duracion = tiempos.get(clave_tiempo.get(fila["nombre"], ""))
                sufijo_tiempo = f" · ⏱ {_duracion(duracion)}" if duracion else ""
                st.markdown(
                    f"**{fila['nombre']}** — {fila['llamadas']} "
                    f"{'call' if fila['llamadas'] == 1 else 'calls'} · "
                    f"{fila['modelo']}{sufijo_tiempo}  \n"
                    f"Input: {entrada} → ${fila['coste_entrada_usd']:.3f}  \n"
                    f"Generated: {_n(fila['salida'])} → ${fila['coste_salida_usd']:.3f}  \n"
                    f"Subtotal: **${fila['coste_usd']:.3f}**"
                )
            st.markdown("---")
            st.markdown(
                f"**Total: ${consumo['coste_total_usd']:.2f}** "
                f"({consumo['n_llamadas']} calls) · "
                f"**Duration: {_duracion(tiempos.get('total'))}**  \n"
                f"Text sent (input): ${consumo['coste_entrada_usd']:.2f} · "
                f"Text generated (output): ${consumo['coste_salida_usd']:.2f} "
                f"— **{consumo['pct_salida']} % of the total**  \n"
                f"Generated text is billed at five times the rate of text sent."
            )
            if consumo["cache_lectura"]:
                st.markdown(
                    f"The playbook and the contract were reused from cache "
                    f"({_n(consumo['cache_lectura'])} tokens read at one tenth of "
                    f"the rate): about **${consumo['ahorro_cache_usd']:.2f}** less "
                    "than sending them in full on every call."
                )
            # Si la revisión por módulos escribió caché en vez de leerla, el
            # manual se ha pagado una vez por módulo. No rompe el análisis, pero
            # encarece la revisión y conviene saberlo.
            modulos = next(
                (f for f in consumo["por_fase"] if f["nombre"].startswith("Phase 1")),
                None,
            )
            if modulos and modulos["cache_escritura"] and not modulos["cache_lectura"]:
                st.warning(
                    "⚠️ The cache was not reused during the clause review: each "
                    "module rewrote the playbook instead of reading it, which made "
                    "this review roughly 20 % more expensive. It does not affect "
                    "the analysis. If it happens again, it is worth looking into."
                )
            st.caption(
                "Estimate based on the tokens actually consumed and Anthropic's "
                "published rates. The amount invoiced may differ slightly."
            )

    # Elementos que la app añade ENCIMA del HTML (briefing §7): el HTML del
    # informe no se toca ni se reestiliza.
    if agregado["intentos_manipulacion"]:
        texto = "\n\n".join(
            f"- **{m['origen']}** (location: {m['localizador'] or 'not stated'})"
            f"\n  > {m['texto_detectado'] or '(no text recorded)'}"
            for m in agregado["intentos_manipulacion"]
        )
        st.error(
            "🚨 **Manipulation attempt detected in the document.** The document "
            "contains text that appears to be addressed to the analysis system "
            "(scoring instructions, or orders to ignore rules). The analysis "
            "ignored it, but review it carefully at the exact point indicated:"
            "\n\n" + texto
        )

    if resultado["aviso_transcripcion"]:
        st.warning(
            "⚠️ The text of the report does not exactly reproduce the score "
            f"computed above (**{agregado['score_pct']} %**). Always take the "
            "figure shown on this screen as the reference, not the one inside "
            "the document."
        )

    if resultado["incidencias_pipeline"]:
        st.warning(
            "⚠️ Some review modules did not return valid results, and their "
            "clauses are not included in the analysis: "
            + " · ".join(resultado["incidencias_pipeline"])
        )

    # El informe es un documento HTML autónomo: se ofrece descargarlo tal
    # cual, para archivarlo en el expediente o compartirlo. Sin esto, el
    # informe solo vive dentro de esta pestaña y se pierde al cerrarla.
    st.download_button(
        "⬇️ Download the report (HTML)",
        data=resultado["html"],
        file_name=f"saas-contract-review-{time.strftime('%Y-%m-%d')}.html",
        mime="text/html",
    )

    components.html(resultado["html"], height=900, scrolling=True)

    if st.button("Analyse another contract"):
        st.session_state.resultado = None
        st.rerun()


# ---------------------------------------------------------------------------
# Pantalla 1 — Subida  (+ Pantalla 2 — Procesando, en el mismo flujo)
# ---------------------------------------------------------------------------
def pantalla_subida():
    st.markdown(
        "<h2 class='titulo-pepa'>PEPA. SaaS Contract Reviewer</h2>"
        "<hr class='regla-pepa'>"
        "<p class='marco-pepa'>Contracts are reviewed from the standpoint of the "
        "<strong>acquiring party</strong>, against a review playbook built on "
        "<strong>Spanish and European Union law</strong>.</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        """
**How to use this tool**

1. Upload the SaaS contract in PDF, DOCX, TXT or MD. PDFs must contain selectable text: a scan without OCR will not work.
2. Include every annex you have: order form, DPA, SLA, technical schedules. The more you upload, the more accurate the analysis.
3. Do not upload confidential documents, documents subject to a non-disclosure agreement, or documents containing personal data.
4. Choose the level of scrutiny according to the contract value and how important the service is to your business.
5. Wait for the report. The tool will ask you nothing else: where a fact is missing, it assumes the most prudent reading and tells you what it assumed.
        """
    )

    ficheros = st.file_uploader(
        "Drag the contract and its annexes here, or click to select them",
        type=["pdf", "docx", "txt", "md"],
        accept_multiple_files=True,
    )

    # Nivel de exigencia. Lo elige el usuario porque conoce el importe y la
    # criticidad del servicio, dos datos que el contrato a menudo no recoge y
    # que la herramienta tendría que asumir.
    st.markdown("**What level of scrutiny should we apply to this contract?**")
    tramo = st.radio(
        "Level of scrutiny",
        options=list(TRAMOS.keys()),
        index=list(TRAMOS.keys()).index(TRAMO_POR_DEFECTO),
        format_func=lambda t: f"{TRAMOS[t]['name']} — {TRAMOS[t]['summary']}",
        label_visibility="collapsed",
    )
    st.caption(
        TRAMOS[tramo]["detail"]
        + " At every level, the tool always assesses the clauses that can veto "
        "the contract on their own, everything concerning data protection and "
        "artificial intelligence, and the four minimum-core clauses."
    )

    analizar = st.button(
        "Analyse contract",
        type="primary",
        disabled=not ficheros,
    )

    if not analizar:
        return

    # --- Límites por sesión -------------------------------------------------
    if st.session_state.analisis_realizados >= MAX_ANALISIS_POR_SESION:
        st.error(
            "You have reached the limit of analyses for this session. "
            "Reload the page to start a new one."
        )
        return
    if len(ficheros) > MAX_FICHEROS:
        st.error(f"A maximum of {MAX_FICHEROS} files per analysis.")
        return
    total_mb = sum(f.size for f in ficheros) / (1024 * 1024)
    if total_mb > MAX_MB_TOTAL:
        st.error(f"The total size cannot exceed {MAX_MB_TOTAL} MB.")
        return

    nombre_clave = CLAVE_API_POR_PROVEEDOR[PROVEEDOR]
    api_key = st.secrets.get(nombre_clave, "")
    if not api_key:
        st.error(
            "We could not complete the analysis. Please try again later. "
            f"(Configuration: {nombre_clave} is missing from Secrets.)"
        )
        return

    # --- Pantalla 2: progreso por fases, no una barra genérica --------------
    # Los mensajes de error se guardan y se pintan FUERA del bloque de estado,
    # para que queden siempre visibles aunque la caja de progreso se pliegue.
    mensaje_error = None
    detalle_tecnico = None

    inicio = time.monotonic()

    with st.status("Preparing the analysis…", expanded=True) as estado:
        barra = st.progress(0)

        def _avance(pct, texto):
            """El reloj y el porcentaje avanzan en cada cambio de fase y cada
            vez que termina un módulo: es la señal de que el análisis sigue vivo."""
            barra.progress(pct)
            estado.update(
                label=f"{texto} · ⏱ {_duracion(time.monotonic() - inicio)} · {pct} %"
            )

        def progreso(etapa, actual, total):
            # El avance se reparte en proporción a lo que tarda cada fase: el
            # triaje es corto, la revisión por módulos se lleva el grueso y el
            # informe ocupa el tramo final.
            if etapa == "triaje":
                _avance(4, "Analysing the document")
            elif etapa == "modulos":
                pct = 12 + int(70 * actual / total) if total else 12
                _avance(pct, f"Reviewing clauses ({actual} of {total})")
            elif etapa == "informe":
                _avance(84, "Preparing the report")

        try:
            _avance(1, "Reading the documents")
            contrato = normalizar_documentos([(f.name, f.getvalue()) for f in ficheros])
            manual = _cargar_manual()

            resultado = ejecutar_analisis(
                contrato, manual, api_key, progreso,
                proveedor=PROVEEDOR, tramo=tramo,
            )

            st.session_state.resultado = resultado
            st.session_state.analisis_realizados += 1
            barra.progress(100)
            estado.update(
                label="Analysis completed in "
                + _duracion(time.monotonic() - inicio) + " · 100 %",
                state="complete",
            )

        except ErrorTriaje as e:
            estado.update(label="Analysis stopped", state="error")
            detalle_tecnico = (
                f"ErrorTriaje · codigo={e.codigo!r} · detalle={e.detalle!r} · "
                f"tipo_detectado={e.tipo_detectado!r}\n\n" + traceback.format_exc()
            )
            if e.codigo == "fuera_de_ambito":
                tipo = e.tipo_detectado or "another kind"
                mensaje_error = (
                    f"This document appears to be a {tipo} contract, and this "
                    "tool only reviews SaaS agreements."
                )
            else:
                mensaje_error = MENSAJES_ERROR.get(
                    e.codigo, MENSAJES_ERROR["version_manual_no_coincide"]
                )
        except ErrorProveedorNoDisponible as e:
            estado.update(label="Analysis stopped", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "The service is unavailable right now because of a configuration "
                "issue unrelated to your document."
                + (f"\n\n**(Debug mode: {e})**" if MODO_DEPURACION else "")
            )
        except ErrorSaldoInsuficiente:
            estado.update(label="Analysis stopped", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "The service is unavailable right now because of a configuration "
                "issue unrelated to your document. Please try again later."
                + (
                    "\n\n**(Debug mode: the Anthropic account has run out of "
                    "credit. Top it up at console.anthropic.com → Plans & Billing.)**"
                    if MODO_DEPURACION
                    else ""
                )
            )
        except ErrorTruncamiento:
            estado.update(label="Analysis stopped", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = (
                "The report exceeds the maximum length and arrived incomplete. "
                "We are not showing it, to avoid partial conclusions. Please try "
                "again, or split the contract into fewer annexes."
            )
        except Exception:
            # Cualquier otro fallo: extracción de ficheros, red o API (el SDK
            # ya reintentó con backoff). El detalle real queda en el modo
            # depuración; al usuario final no se le enseña la tripa técnica.
            estado.update(label="Analysis stopped", state="error")
            detalle_tecnico = traceback.format_exc()
            mensaje_error = "We could not complete the analysis. Please try again later."

    if mensaje_error:
        st.error(mensaje_error)
        if MODO_DEPURACION and detalle_tecnico:
            with st.expander("🔧 Technical detail (debug mode)", expanded=True):
                st.code(detalle_tecnico)
    elif st.session_state.resultado is not None:
        st.rerun()


# ---------------------------------------------------------------------------
if not CONTRASENA_ACCESO:
    # Sin contraseña configurada, la app no analiza: abierta por accidente
    # pagaría los análisis de cualquiera que pase.
    st.error(
        "The app is not available right now. Please try again later. "
        "(Configuration: CONTRASENA_ACCESO is missing from Secrets.)"
    )
elif not st.session_state.autenticado:
    pantalla_acceso()
elif st.session_state.resultado is not None:
    pantalla_informe()
else:
    pantalla_subida()
