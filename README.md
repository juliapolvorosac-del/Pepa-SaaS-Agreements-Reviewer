# Pepa-SaaS-Agreements-Reviewer - Revisor de Contratos SaaS

Aplicación web que analiza contratos SaaS desde la posición de la **parte adquirente** y genera un informe de asistencia a la revisión jurídica: puntuación de riesgo ponderada, análisis cláusula por cláusula contra un manual de criterios propio y propuestas de redacción alternativa (*redlines*) listas para negociar.

Proyecto educativo desarrollado por **Julia Polvorosa Cáceres**, abogada in-house especializada en contratación tecnológica, como trabajo final del curso *IA para abogados*.

## Qué hace

1. **Triaje** — comprueba que el documento es un contrato SaaS legible y en ámbito de aplicación, identifica partes, ley aplicable y jurisdicción, y determina el nivel de exigencia aplicable según el valor y la criticidad del contrato.
2. **Revisión por módulos** — evalúa 67 cláusulas organizadas en 7 módulos (condiciones comerciales, propiedad intelectual y datos, responsabilidad, duración y terminación, RGPD, Reglamento de IA y un módulo específico para proveedores estadounidenses), en llamadas paralelas a la API de Claude.
3. **Informe** — consolida los resultados en un informe HTML autónomo con veredicto, semáforo de riesgo, estado de las 13 cláusulas de veto y propuestas de redacción. La aplicación recalcula el score de forma independiente en código y avisa si detecta discrepancias.

El análisis se realiza contra un manual de revisión jurídica propio (derecho español y de la Unión Europea), diseñado para su uso conjunto con modelos extensos de lenguaje.

## Características de diseño

- **Sin preguntas al usuario**: toda incógnita se resuelve como asunción declarada o como incidencia visible en el informe.
- **Citas literales con localizador** (cláusula y página), para que el abogado verifique cada conclusión en segundos.
- **Defensa frente a manipulación**: el contenido del documento se trata siempre como dato, nunca como instrucción; los intentos de manipulación se destacan en el informe.
- **El veredicto nunca autoriza una firma**: la herramienta asiste al abogado, no le sustituye.
- **Sin persistencia**: los documentos se procesan en memoria y no se almacenan.

## Tecnología

- [Streamlit](https://streamlit.io/) como interfaz web.
- [API de Claude](https://www.anthropic.com/) (Anthropic) como motor de análisis, con arquitectura de tres fases encadenadas y caché de prompts.

## Aviso legal

El resultado generado por esta herramienta es un análisis preparatorio de asistencia a la revisión jurídica. **No sustituye a la revisión por un abogado cualificado y no constituye asesoramiento jurídico.**

## Propiedad intelectual

**© 2026 Julia Polvorosa Cáceres. Todos los derechos reservados.**

Este repositorio se publica únicamente con fines de visualización como proyecto educativo. No se concede ninguna licencia de uso, reproducción, modificación ni distribución del código, de los prompts ni del manual de revisión. Para cualquier consulta: juliapolvorosac@gmail.com
