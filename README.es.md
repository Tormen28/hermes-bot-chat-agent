# Bot Chat Agent para Hermes

[English](README.md)

> Un Bot Chat de Hermes más tranquilo y simple, para quien solo quiere respuestas.

## Por qué existe

Hermes es muy potente, y esa potencia se ve en pantalla: bloques de razonamiento, tarjetas de herramientas,
diffs, logs, preguntas de seguimiento. Para un desarrollador eso está genial. Para mucha gente (estudiantes,
emprendedores, familia, cualquiera que no sea técnico) puede ser abrumador.

**Bot Chat Agent** les da un Bot Chat de Hermes más amable: preguntas, ves una barra discreta de
*"Cargando…"* y recibes una respuesta corta y directa. Menos cosas en pantalla, menos preguntas, el mismo
Hermes por debajo. Si quieres ver todo el detalle, abre una sesión normal: queda exactamente igual.

## Capturas

| Cargando | Respuesta | Corto y directo |
|---|---|---|
| ![Barra de carga](docs/screenshots/01-loading.png) | ![Respuesta, razonamiento colapsado](docs/screenshots/02-answer.png) | ![Respuesta corta](docs/screenshots/03-short-answer.png) |

## Qué hace

| Capa | Qué cambia |
|---|---|
| Estilo (`pre_llm_call`) | Un contrato corto de "actúa, no narres": acuse breve, hace el trabajo, resultado corto. Máximo una pregunta por turno. |
| Guardia de seguridad (`pre_tool_call`) | **Bloquea** acciones riesgosas (secretos, borrados, git destructivo, push a ramas protegidas, mensajes salientes, modelos caros) y te pregunta primero. Nunca aprueba nada por ti. |
| Desktop | Una barra discreta "Cargando…" sobre el cuadro de texto y el razonamiento colapsado por defecto mientras estás en un Bot Chat. Las tarjetas de aprobación no se tocan. |

Solo afecta a las sesiones llamadas **"Bot Chat"** (el título que usa el Modo Bot de Hermes) y sus hijas de compactación.
Las sesiones normales, los subagentes y los perfiles siguen igual.
La guardia omite las sesiones de subagente, así que los hijos `delegate_task` de un Bot Chat no quedan cubiertos.

## Instalar

Requiere Hermes `>= 0.22.5`.

```bash
hermes plugins install Tormen28/hermes-bot-chat-agent
hermes plugins enable bot-chat-agent
```
Desktop: **Capacidades → Plugins → Bot Chat Agent** → activar. Reinicia la app si no aparece.
Luego abre **Bots**, elige un bot y chatea.

## Desactivar / desinstalar

```bash
hermes plugins disable bot-chat-agent
hermes plugins remove bot-chat-agent
```
O apágalo en **Capacidades → Plugins**. Al desactivarlo se restaura tu ajuste anterior de "colapsar razonamiento".

## Configuración y privacidad

La configuración (`language: es` para respuestas en español), las reglas de la guardia y las divulgaciones
están detalladas arriba en inglés. En resumen: lee `state.db` **solo en modo lectura** para encontrar los Bot Chat,
nunca lee `.env`, `auth.json` ni credenciales, no hace llamadas de red y no envía telemetría.

**Limitación conocida:** el SDK público de Desktop puede colapsar el razonamiento pero aún no puede ocultar las
tarjetas de herramientas por sesión, así que esas tarjetas siguen visibles en Bot Chat.

## Licencia
MIT. Ver [LICENSE](LICENSE).
