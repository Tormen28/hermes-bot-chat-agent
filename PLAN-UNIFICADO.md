# PLAN UNIFICADO — un solo plugin `bot-chat-agent`

Fecha: 10 nov 2026, Caracas (UTC-4). Solo plan: no se implementó nada, no hay push, no se tocó `main` ni tu PC.
Rama de partida: `feat/bot-chat-generic-memory` (3 commits: 74b7653, cc9119e, 2be9c42).

## 0. Decisión

- Todo va en **un solo plugin**: `Tormen28/hermes-bot-chat-agent` (PR de catálogo #148688).
- El plugin actúa sobre el panel **Bots de Hermes tal cual viene** (`hermes-bots`, sesión "Bot Chat"). No se toca el código de `hermes-bots` ni el core.
- Tus arreglos al core (PR #151935, `core-repairs/plugin-surface`) se usan **si existen** y, si no, el plugin degrada. Ese PR sigue **abierto**, no mergeado, así que en un Hermes normal no existen todavía.
- Se descartan como productos separados: `hermes-bot-ux`, `bot-agente`, `candado-kanban`, `padre-flow`. Sus ideas se absorben aquí (sección 3).

## 1. Cómo se engancha el plugin al panel Bots (verificado en el código de hermes-agent v0.23.1)

Base que leí: tarball de v0.23.1 en `/workspace/hermes-core` (solo lectura). Tu PC corre 0.23.0; la 0.23.1 salió hoy 01:42 Caracas.

**Hermes Desktop y la versión web son el mismo código** (`hermes webapp`, puerto 9120). Es el mismo plugin en las dos.

Cómo se ve "Bot Chat" por dentro:
- Cada bot es un perfil. Su chat es **la sesión titulada exactamente "Bot Chat"** (`canonical-chat.ts`). Esa regla de nombre no se puede cambiar.
- Ese chat usa el mismo hilo de mensajes que Sesiones. El panel Bots solo pinta la lista de bots y las filas.
- Por eso lo único que un plugin necesita es **un hueco en la pantalla del chat**: `COMPOSER_AREAS.top` (encima del cuadro de mensaje). El plugin ya lo usa (`desktop/plugin.js:608`). Existen también `underside` y `STATUSBAR_AREAS`.

Las dos mitades del plugin (carpeta única, "paquete unificado"):

| Mitad | Dónde carga | Para qué |
|---|---|---|
| Python | `$HERMES_HOME/plugins/bot-chat-agent/` | hooks: `pre_llm_call`, `pre_tool_call`, `post_llm_call`, `transform_tool_result`, kanban; comando `/memoria`; guarda memoria y estado |
| Escritorio/web | `plugins/bot-chat-agent/desktop/plugin.js` (sin compilar) | tarjeta de estado dentro del chat; se activa en Capabilities → Plugins |
| Puente | `dashboard/plugin_api.py` → `/api/plugins/bot-chat-agent/status` | la tarjeta lee el estado con `ctx.rest('/status')` |

Hooks nuevos en 0.23.1 que antes no usábamos: `on_stream_start/delta/end`, `on_interim_message`, `transform_llm_output`. Sirven para estados reales sin depender solo de `status.json`. A comprobar si llegan los eventos de delegación (ver P3 abajo).

## 2. Lo que Hermes ya trae (para no duplicarlo)

Esto lo encontré al leer el código. **Hay que revisarlo antes de reconstruir nada**:
- La tarjeta de delegación nativa (`components/assistant-ui/tool/delegate.tsx`) ya muestra subagentes en vivo con tiempo transcurrido.
- Las salas de grupo ya tienen "working / stalled" por miembro (`group-room-status.ts`, 1:1 no).
- `message_agent` (`tools/bot_mode_dm.py`) es la delegación Padre → otro bot: manda el mensaje y la respuesta vuelve después como aviso de proceso en segundo plano.
- El core ya tiene compresión automática de contexto (`agent/context_compressor*`, `conversation_compression*`) y proveedores de memoria (`agent/memory_provider.py`, `plugins/memory`). Existe además memoria MEMORY.md / USER.md.

Consecuencia honesta: la parte nuestra que **sí** aporta es lo que el core no hace en Bot Chat 1:1: **una sola tarjeta que junta** bots activos + delegaciones entre bots (Padre → Hefesto) + pendientes + memoria recordada. Hay que dejar claro en el README qué suma frente a lo nativo.

## 3. Inventario completo y destino de cada idea

Estado: **HECHO** (en la rama), **PORTAR** (falta traerlo/ajustarlo), **PROBAR** (hecho pero sin prueba real), **SOLTAR** (no va, con motivo).

### 3.1 Guard, estilo y Desktop (plugin 0.2.0/0.3.0-dev)

| Idea (origen) | Estado | Nota |
|---|---|---|
| Estilo conversacional: un acuse corto y luego actuar (`bot-agente`, propuesta §3B.5) | HECHO | `pre_llm_call` solo en Bot Chat |
| Guard con presets strict/default/relaxed, cubre subagentes | HECHO | |
| Guard endurecido: evasiones R13 (`/bin/rm`, `\rm`, `bash -c`, `powershell -Command`, `eval`…) | HECHO | `guard_rules.py` |
| Huecos de inyección A0.7 (5/5 de la auditoría) | HECHO | `transform_tool_result` etiqueta como no confiable; la auditoría los llamó "fallo real del plugin" |
| `kanban_lock` fusionado en el mismo `pre_tool_call` (`candado-kanban`, brief `hermes-bot-ux`) | HECHO | un solo guard, sin hooks dobles |
| "Wait-for-god": no cerrar la tarjeta hasta que el compañero entregue (brief §4) | HECHO | `team_flow.py`; con `team.assignees` vacío queda modo solo |
| `team.gods` → `team.assignees` sin reescribir; si difieren, error explícito | HECHO | |
| Entrega durable con SQLite propio, lease, dedupe, migración de JSON | HECHO | `delivery_store.py`; ver nota abajo sobre la ruta durable |
| Pendientes interrumpidos por un mensaje a mitad de turno (hallazgo de tu prueba en vivo) | HECHO | `pending_asks.py` |
| Barra Working / Waiting for your OK / Error en el escritorio | HECHO | `desktop/plugin.js` |
| Razonamiento colapsado por defecto en Bot Chat | HECHO, **con límite** | usa `host.settings`, que es global. La propuesta original usaba CSS por sesión, pero el catálogo lo prohíbe (regla 8). Dejar claro en el README |
| Ocultar tarjetas de herramientas y razonamiento **solo** en Bot Chat | SOLTAR del plugin | no hay API por sesión. Existe el borrador `sdk-hook-issue.md` (`host.thread.setDisplayPolicy`): se queda como propuesta al core, no como parche |
| Allowlist de lectura para aprobaciones (`git status/diff/log`, `hermes kanban`) | SOLTAR del plugin | es **configuración por perfil** del usuario, no código del plugin. Documentarla como receta opcional en el README |

### 3.2 Memoria, `/memoria` y tarjeta (rama `feat/bot-chat-generic-memory`)

| Idea | Estado | Nota |
|---|---|---|
| Memoria corta (resumen extractivo + pendientes) y larga (decisiones, estado, lecciones) dentro de Hermes, sin vault ni claves | HECHO | `bot_chat_memory.py`, `memory.db` |
| Escritura por pistas (`Decision:`, `Decidimos`, `Lesson learned:`, `Lección:`…), no guarda texto con pinta de secreto | HECHO | |
| Recuperación por solapamiento con presupuestos 400/400 tokens | HECHO | |
| Comando `/memoria` (alias `/memory`; `forget`) | HECHO, **PROBAR** | sin probar en una sesión real |
| Cabecera `[Estado]` en `pre_llm_call` | HECHO | |
| Tarjeta de estado: bot activo, otros bots, líneas "Padre → Hefesto: esperando respuesta → listo", tiempo por tarea, aviso "sin avance", pendientes, memorias recordadas, tokens | HECHO, **PROBAR** | lee `status.json` por `/status`. **Nunca se vio en el Hermes real** |
| Memoria larga **privada** con segundo-cerebro | SOLTAR de este plugin | es versión aparte "para nosotros" (t655u). El plugin público no toca el vault |
| Motor de contexto `bot-chat` registrado pero **inactivo** | PROBAR | activarlo cambiaría ~85k tokens de historial por ~750. Es el único cambio que podría arreglar de verdad la lentitud en sesiones largas. Solo se activa tras medir en tu PC |

### 3.3 Informes y auditorías (qué sigue pendiente)

| Fuente | Hallazgo | Destino |
|---|---|---|
| `hermes-mejora-audit` | Los tests son sintéticos: no hay prueba en vivo del chat (ACK, barra, guard) | **Plan P4–P5 (smoke real)** |
| idem | Core 0.23.0 no tiene `record_delegation_delivery` ni `read_session`; en vivo cae a "offer" | HECHO: degradación; mejora cuando exista #151935 |
| idem | El runner da por bueno un `ok` numérico; 12 fallos conocidos comparados por conteo, no por nombre | HECHO (`test_runner_strict.py`, ajuste "estricto"); revisar que compare nombres |
| idem | 14 tests no corrieron en Windows (fork/SIGKILL) | HECHO: se saltan con motivo explícito; correrlos en Linux en la box |
| idem | R26 sin evidencia | HECHO (`test_r26_audit.py`) |
| `hermes-bot-unified-analysis` (plan definitivo) | Fases 0–7: fiabilidad → contexto acotado → ejecución en segundo plano → unificar → medir → canario → publicar | Fases 0–4 en gran parte HECHAS (outbox, contexto acotado, fondo, unificación). **Faltan**: medir sesión larga real, canario 8 h, publicar |
| idem | "No prometer 2× velocidad"; tests verdes ≠ lentitud arreglada | Se mantiene en README y en lo que le digas a los jueces |
| idem | Informe de Antigravity: números sin tokenizer, n=20, fixtures | Evidencia secundaria; no citarlos como medidas |
| `hermes-audit` (evidencia 8-nov) | Presión de contexto confirmada (compresión a ~400k tokens), visión con timeout, turnos de 50–200 s | Son **configuración** (`compression.*`, `auxiliary.vision`), no plugin. Va a una nota aparte para ti; no se cambia nada |
| `hermes-audit` (Tormen, BSK, JEV, Panteón) | otros plugins/skills | **Fuera de este plugin.** Solo comprobar que no chocan: no reactivar `bot-agente`/`candado-kanban`/`padre-flow` (hooks dobles) |
| `hackathon/` (README, CHECKLIST, typeform, x-posts) | Paquete del concurso **pausado** (t669u) | No cambia. Cuando lo reanudes: subir a 0.3.0, re-pin del catálogo (PR nuevo, no el #148688) |

### 3.4 Qué sí se debe descartar o marcar

| Archivo | Qué es | Acción propuesta |
|---|---|---|
| `demo/status_card_harness.html` | página de prueba con datos de ejemplo, **no** es Hermes | Dejarla, pero marcarla "vista previa sintética" en el README. No se mueve ni se borra |
| `docs/screenshots/04-status-card.png` y `/workspace/hackathon/bot-chat-status-card.png` | captura de esa página de prueba | **No usar para el concurso ni presentar como Hermes.** Reemplazar por la captura real (P5) |
| `/workspace/hermes-core` | copia de lectura de Hermes v0.23.1 (tarball, un commit base `2910ab0`, rama `feat/hermes-bots-mejoras` sin cambios) | Sirve como banco de pruebas. **No es una bifurcación y no se le va a cambiar código del panel.** Puede borrarse al final |

No hay nada que sea claramente "dirección equivocada" a nivel de código del plugin, así que **no moví nada a `_archivo-plugin-aparte`**. Lo único equivocado fue presentar la página de prueba como si fuera Hermes.

## 4. Plan paso a paso (cuando des luz verde)

Rama nueva en el repo del plugin: `feat/unificado` desde `feat/bot-chat-generic-memory`. Sin push hasta tu OK.

**P0 — Línea base (box).** Correr los 215 tests actuales en Linux (los de fork/SIGKILL sí corren aquí). Guardar el resultado. Criterio: mismos fallos conocidos y nada nuevo.

**P1 — Compatibilidad con 0.23.0, 0.23.1 y #151935.** Probar el plugin contra el core descargado (v0.23.1) y contra la rama de #151935. Con `ctx.has_capability("state.read")` y `"state.detached_result"`: si están, se usa la ruta durable; si no, se degrada (ya existe). Criterio: `hermes plugins doctor` limpio y 0 excepciones en las dos versiones.

**P2 — Una sola tarjeta con datos reales.** Hoy la tarjeta se alimenta de un archivo que escriben los hooks. Pasos:
 1. Revisar si `on_interim_message` y los eventos de stream dan el estado "trabajando/esperando" más exacto que `status.json`.
 2. Mostrar la delegación Padre → otro bot solo cuando haya evidencia real: llamada a `message_agent` o tarjeta de kanban, y "listo" cuando llegue la respuesta. No inventar "listo" por tiempo.
 3. Quitar de la tarjeta lo que Hermes ya enseña bien (tiempo de subagentes en su propia tarjeta) para no duplicar.
 Criterio: tests de modelo de tarjeta + prueba E2E en P5.

**P3 — Verificar el nombre del bot.** Hoy si la sesión sale como "Bot Chat" la tarjeta dice "Bot Chat → Hefesto" en vez de "Padre". Resolver el nombre desde el perfil de la sesión (`profiles.list`), no desde el título.

**P4 — Memoria y `/memoria` en un Hermes real.** Con un `HERMES_HOME` temporal en la box y un modelo falso (mock): comprobar que el comando aparece en el menú de barra y que la memoria se guarda y se recupera en otra sesión. Criterio: los 3 momentos del guion de demo.

**P5 — Captura REAL.** Ver sección 5. Criterio: captura del Bot Chat de Hermes con la tarjeta, no de una página de prueba.

**P6 — Medir lentitud con honestidad.** En tu PC, con tu Hermes: sesión larga sin y con `context.engine: bot-chat`. Medir tokens enviados y tiempo por turno (n explícito). Solo si mejora, se recomienda activarlo. Hasta entonces el README dice "no demostrado".

**P7 — Instalar en tu PC (cuando vuelva la luz).** Respaldar la carpeta del plugin, `git pull` de la rama, `hermes plugins validate`, reiniciar **Hermes Desktop** solo si hace falta (no reinicio el gateway sin que me lo digas). Luego smoke de 10 min: acuse + barra, guard con un comando de prueba en una carpeta temporal, una delegación real, `/memoria`, captura.

**P8 — Concurso / publicación.** Siguen pausados hasta que digas. Cuando toque: versión 0.3.0, PR de re-pin del catálogo, video, formulario.

## 5. Cómo sacar una captura REAL del Hermes con el plugin

Hay un camino que no necesita Electron: **la versión web de Hermes** (`hermes webapp`, la misma interfaz que tu escritorio), servida por el gateway real.

1. En la box: `uv` y `pnpm` ya están. Chrome está en `/usr/bin/google-chrome`. Falta instalar Playwright (o usar el CDP que ya trae `apps/desktop/scripts/smoke-browser-host.mjs`).
2. Crear un `HERMES_HOME` temporal vacío (no el tuyo, sin claves).
3. Servidor de modelo falso: `tests-js/scripts/mock-server.ts` + `mock-provider-config.ts` (es el mismo que usan los tests de Hermes).
4. Instalar el plugin en ese home (`plugins/bot-chat-agent/` con su `desktop/plugin.js`), `hermes config set webapp.enabled true`, `hermes webapp --no-open`.
5. Abrir el enlace de un solo uso en Chrome sin pantalla, crear un perfil "Padre", abrir su Bot Chat, activar el plugin en Capabilities → Plugins, provocar un turno y una delegación, y capturar.
6. El pie de la captura dice: "Hermes Webapp v0.23.1, perfil de prueba, modelo simulado".

**Límites que hay que decir en voz alta:**
- Es el cliente real de Hermes, pero con **modelo simulado**, así que el texto de las respuestas es de prueba.
- La delegación Padre → Hefesto sale de dos perfiles de prueba reales en ese home, no de tus dioses.
- Cuesta un build del webapp y la instalación de dependencias (unos minutos y espacio: la box tiene 115 GB libres). Si el build falla en la box, lo digo y no entrego captura falsa. La alternativa es hacerla en tu PC con tu Hermes (P7).

## 6. Cómo le llega a todo el mundo (y el choque con el concurso)

- **Plugin del catálogo (el camino correcto).** `hermes plugins install` desde el catálogo de Nous; no se pierde con `hermes update`, porque vive en la carpeta de plugins y no en el core. Es el único camino que sobrevive. El catálogo fija un SHA concreto, así que cada versión nueva es un PR de re-pin al catálogo.
- **Parche al código de `hermes-bots`:** **descartado**. Se borra con `hermes update`, y las reglas de contribución de Hermes rechazan "plugins que tocan archivos del core". Además, el panel Bots tiene un contrato de identidad ("Bot Chat" por nombre) que lleva años rompiéndose y no se reabre en un PR rutinario.
- **Cosas que solo el core puede dar** (por ejemplo ocultar herramientas/razonamiento solo en Bot Chat, o recortar el historial): van como propuestas aparte (#151935 ya es una; `sdk-hook-issue.md` es otra). El plugin degrada sin ellas.
- **Sobre el concurso:** el concurso es de **plugins**, y esto es un plugin. No hay conflicto mientras el plugin sea la entrega. El conflicto sería si alguna mejora exigiera cambiar el core, porque entonces no sería una entrada válida. Con este plan nada lo exige. Y los jueces van a instalar el SHA fijado en el catálogo: si ese SHA no tiene la memoria y la tarjeta, no las ven (por eso el re-pin).
- **Riesgo que dejo escrito:** la tarjeta y `/memoria` **no se han visto en un Hermes real**. No conviene grabar el video antes de P5 y P7.

## 7. Preguntas que sí necesito de ti (mínimas)

1. ¿Doy luz verde a P0–P5 en la box (sin tocar tu PC ni subir nada)?
2. ¿Confirmas que los dioses de tu equipo personal van como configuración (`team.assignees`) y nunca dentro del plugin público?
3. P6 y P7 son en tu PC: ¿los hace Antigravity o yo cuando vuelva la luz?
