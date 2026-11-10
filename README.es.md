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
| Estilo (`pre_llm_call`) | Un contrato corto de "actúa, no narres": un acuse (`Voy a…`), hace el trabajo, resultado corto. Máximo una pregunta por turno. |
| Guardia de seguridad (`pre_tool_call`) | **Bloquea** acciones riesgosas (secretos, borrados, git destructivo, push a ramas protegidas, mensajes salientes, modelos caros, saltarse el candado del kanban) y te pregunta primero. Cubre tanto el Bot Chat como sus subagentes hijos (`delegate_task`). Nunca aprueba nada por ti. |
| Equipo | Sin `team.assignees`, el bot trabaja solo. Con nombres de perfiles reales, crea subtareas y no las cierra hasta que el tablero las muestra hechas. |
| Desktop | Una barra de estado discreta (`Cargando… · <paso>`, `Esperando tu OK`, `Esperando a <perfil>`, `Listo · <resumen>`, `Error: <resumen corto>`) y el razonamiento colapsado por defecto mientras estás en un Bot Chat. |
| `/memoria` (alias `/memory`) | Lista lo que el plugin recuerda, olvida o edita una entrada por id, y borra todo con confirmación. `/memoria` responde en español. |
| Panel de estado | Una línea compacta `[Estado]` cuando hay algo que mostrar: tareas en curso, pendientes, memorias recordadas en este turno y el tamaño acotado del contexto. Se apaga con `status_panel.enabled: false`. |

Solo las sesiones llamadas **"Bot Chat"** (el título que usa el Modo Bot de Hermes) y sus hijas de compactación reciben el estilo de respuesta.
La guardia de seguridad también protege las sesiones hijas `delegate_task` / subagentes lanzadas desde un Bot Chat (heredan la protección del Bot Chat padre). Las sesiones normales y sus subagentes siguen igual. El trabajador kanban del perfil asignado queda fuera de esa guardia, así que puede entregar su tarjeta.
**0.3.0-dev** en esta rama es un corte privado de prueba, no la publicación del catálogo de Nous. Sin `team.assignees` el bot trabaja solo. Con nombres de perfiles, espera a que el tablero marque la tarjeta como hecha. `relaxed` mantiene el candado del kanban y apaga esa espera. La guía completa está en [README.md](README.md).

**Fase 4 — una versión, dos modos.** Una instalación nueva es individual por defecto; un equipo existente que ya lista compañeros (en `team.assignees` o en la clave antigua `team.gods`, que sigue funcionando) no se desactiva con el nuevo default. Si las dos claves existen y difieren, el plugin bloquea con una explicación en vez de adivinar. En modo individual, crear una tarjeta para un perfil no configurado se bloquea (no se inventan compañeros).

## Instalar

Requiere Hermes `>= 0.22.5` (verificado: esa versión ya tiene los hooks que usa el plugin — `post_llm_call`, `agent_loop_stopped`, `kanban_task_blocked`).

```bash
hermes plugins install /ruta/a/esta/copia
hermes plugins enable bot-chat-agent
```

`hermes plugins install Tormen28/hermes-bot-chat-agent` sigue la rama por defecto (`main`, 0.2.0) hasta que esta rama se fusione.
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

**Memoria.** Va encendida por defecto y es solo local: un resumen extractivo de turnos anteriores y un almacén de decisiones, estado del proyecto y lecciones en `<HERMES_HOME>/bot-chat-agent/memory.db`. No usa un segundo cerebro ni ningún vault externo. En Hermes v0.23 el hook solo puede *añadir* ese bloque acotado; no puede sustituir el historial completo que el core sigue enviando. Se apaga con `memory.enabled: false`. En un Bot Chat, `/memoria` lista, edita y borra esas notas (confirma con `/memoria borrar confirmar`). El panel `[Estado]` aparece solo cuando hay algo que mostrar; `status_panel.enabled: false` lo oculta. Demo sin Hermes Desktop: `demo/hackathon_demo.py`. Detalles y cifras en [README.md](README.md) y `docs/memory-bench.md`.


**Limitación conocida:** el SDK público de Desktop puede colapsar el razonamiento pero aún no puede ocultar las
tarjetas de herramientas por sesión, así que esas tarjetas siguen visibles en Bot Chat.

## Licencia
MIT. Ver [LICENSE](LICENSE).
