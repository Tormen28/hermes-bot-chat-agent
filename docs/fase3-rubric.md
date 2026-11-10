# Rúbrica Fase 3 — Conversar mientras trabaja, ruta A (nivel A, offline)

**Versión:** 1.0 — congelada el 2026-11-08, antes de implementar.
**Base:** plan v1.2 §10 Fase 3, ruta A (kanban + outbox + aviso en la UI, sin
cambios en el core). Decisión de Gustavo: la vía B (contrato nuevo del core)
es propuesta aparte y no bloquea esta fase; B2/B3/Q5 siguen siendo solo
diseño (sin cambios fuertes en el core).

## 1. Qué hace la ruta A (y qué no)

1. El trabajo de fondo se lanza como tarjeta kanban (`kanban_create` al
   compañero en equipo; a un perfil worker del propio usuario en modo
   individual — ese perfil debe estar listado en `team.gods`, es el
   contrato que ya usa la Fase 1a). El dispatcher del core lo ejecuta en
   otro proceso; el turno del chat termina enseguida y el usuario sigue
   hablando. **El plugin coordina y entrega; no ejecuta trabajo de fondo
   ni lanza workers ni schedulers propios.**
2. El resultado vuelve por el camino de la Fase 1 (`kanban_task_completed` /
   `blocked` → outbox → sesión de origen, con `session_id`; wow 8).
3. El resultado **nunca** se inyecta a mitad de un turno: se anuncia en el
   siguiente turno (oferta en `pre_llm_call`) o cuando el usuario pida
   "muéstramelo" (el modelo lo lee de la oferta).
4. Los estados de la UI salen de eventos reales, no de suposiciones.

### Máquina de estados de una tarea de fondo (registro durable del plugin)

| Estado | Evento real que lo pone | Qué significa |
|---|---|---|
| `received` | `kanban_create` registrado (`post_tool_call`) | Recibido: la tarjeta existe, el dispatcher aún no la toma |
| `queued` | `kanban_task_claimed` (dispatcher, kwargs verificados: `task_id, board, assignee, run_id, profile_name`; sin `session_id` — se correlaciona por el mapa `creates`) | En cola: reclamada, worker a punto de arrancar |
| `working` | `on_kanban_worker_spawned` (dispatcher, tras persistir el PID) | Trabajando: el proceso worker arrancó |
| `waiting` | Derivado al consultar: tarjeta abierta con un padre abierto en el tablero (`task_links`) | Esperando permiso/compañero: bloqueada por una dependencia |
| `blocked` | `kanban_task_blocked` del worker (sin marca de cancelación del chat) | Bloqueado: el worker se atascó; se registra entrega `blocked` (wow 5) |
| `ready` | `kanban_task_completed` **y** entrega persistida con ID en el outbox | Listo: solo existe si hay fila durable; sin fila no hay `listo` (wow 4) |
| `error` | Entrega a `dead_letter` tras 5 intentos fallidos | Error: contabilizado por ID |
| `cancelled` | Cancelación del chat (ver §3) | Cancelado: el plugin deja de seguirla y de anunciarla |

Transiciones guardadas: `ready`, `error` y `cancelled` son terminales
(nada sale de ahí). `blocked` no es terminal (un `unblock` puede volver
a `queued`/`working` y luego a `ready`). Un `completed` sin entrega
persistida **no** pone `ready` (sin falsos `listo`).

### Límites honestos de la ruta A (documentados, no escondidos)

- **El core no tiene `kanban_cancel` ni estado `cancelled`** (verificado en
  `tools/kanban_tools.py` del core: las herramientas terminales son
  `kanban_complete`, `kanban_block`, `kanban_schedule`,
  `kanban_request_review`, `kanban_request_changes`). La cancelación de
  tarea y el stop-all son **estado del plugin**: el worker del core sigue
  corriendo hasta que termina o se bloquea; el plugin deja de seguir la
  tarjeta y no anuncia su resultado. El resultado queda en `kanban.db`
  (el core), no se pierde, pero Bot Chat no lo anuncia. Cambiar esto es
  la propuesta B3 al core (diseño, no implementada).
- **Timeouts y rate limit son del core** (TTL de claim, breaker de fallos,
  reintentos con guarda en el dispatcher). El plugin no los reimplementa:
  un timeout/crash/rate-limit del core termina en tarjeta auto-bloqueada
  → `kanban_task_blocked` → estado `blocked` con entrega registrada.
- **El aviso en la barra sin turno activo es parcial sin el RPC Q5.**
  Investigación Q5 (verificada): el SDK de escritorio hoy **no** puede
  leer el estado de entregas; el puente B (RPC nuevo en el gateway) es
  propuesta de diseño al core, **no** implementada. Lo que el plugin
  puede hacer por sí solo: `desktop/plugin.js` observa los eventos
  kanban que le llegan mientras la app de escritorio está abierta y
  muestra "N resultado(s) listo(s)" (con deduplicación por `task_id` y
  sin nombrar una sesión que no sea la del evento — wow 8). Lo que NO
  puede: consultar el outbox al arrancar ni saber de entregas que
  ocurrieron con la app cerrada; al recargar, el contador se reinicia y
  la garantía vuelve a ser la oferta del siguiente turno. El p95 ≤ 1 s
  desde la persistencia hasta la barra **no es medible offline** y queda
  para la Fase 5 (medición en vivo); esta puerta no lo promete.

## 2. Cola visible y serialización

- **Máximo 10 encargos de fondo abiertos por sesión** (estados
  `received`/`queued`/`working`/`blocked`). Con la cola llena, el
  `kanban_create` se bloquea en la guardia con un mensaje explícito
  (no un fallo silencioso). La cola se mide en el registro durable del
  plugin, no en memoria (vale entre procesos).
- **Sin dobles despachos:** mismo `(sesión, assignee, título)` genera la
  misma `idempotency_key` (`bca-<sha256>`, ya inyectada por la guardia en
  la Fase 1a) y el core devuelve la tarjeta existente en vez de crear
  otra (verificado: `create_task` busca por `idempotency_key` y devuelve
  la no archivada). El registro del plugin hace upsert por `task_id`:
  dos `kanban_create` iguales dejan **una** sola fila.
- **Sin tareas sin permiso:** un `kanban_create` a un perfil que no está
  en `team.gods` se bloquea en la guardia (cuando hay equipo
  configurado). En modo individual el perfil worker debe estar listado
  en `team.gods`.
- **Serialización por recurso:** dos tarjetas de la misma sesión sobre el
  mismo recurso (mismo `workspace_path` absoluto o mismo `project`
  explícito en los args de `kanban_create`) se serializan: la segunda se
  crea con la primera como `parents` (el core la deja en `todo` hasta
  que el padre llega a `done` y luego la auto-promueve a `ready`;
  verificado en `tools/kanban_tools_schemas.py`). Los `parents` que ya
  puso el modelo se conservan (se fusionan, no se sustituyen). Sin
  recurso explícito no hay serialización (cada tarjeta usa su scratch
  propio, que es lo seguro por defecto del core).

## 3. Cancelación en tres niveles

| Nivel | Disparador en ruta A | Efecto |
|---|---|---|
| Respuesta | `/stop` → `agent_loop_stopped` | Se liberan las ofertas del turno (ya en Fase 1a); la tarea de fondo **sigue** |
| Tarea | El modelo bloquea la tarjeta (`kanban_block`) en la sesión Bot Chat: la guardia registra una marca de cancelación y el hook `kanban_task_blocked` la consume → `cancelled` (sin marca, un bloqueo del worker es `blocked`, no `cancelled`) | El plugin deja de seguirla; un `completed` posterior **no** se anuncia (su entrega no se registra) |
| Stop-all | `stop_all_session(sesión)`: cancela todas las abiertas de la sesión, pone el flag de sesión y **conserva lo persistido** (entregas `delivered_in_turn` intactas; ofertas no confirmadas vuelven a `pending`) | Mientras el flag está activo, ningún `kanban_create` nuevo se acepta |

**Reanudar tras stop-all (sin inventar una herramienta nueva):** cuando la
guardia bloquea un `kanban_create` por stop-all, deja una marca de
reanudación ofrecida (por `idempotency_key`) y el mensaje le dice al
modelo que pregunte al usuario. Si el modelo reintenta **el mismo**
encargo en un **turno posterior** (misma key, distinto `turn_id`), se
entiende que el usuario confirmó y el flag se limpia: el encargo pasa.
Un reintento en el mismo turno sigue bloqueado. Sin `turn_id` no hay
reanudación automática (fail-closed).

## 4. Puerta nivel A (offline, bloquea)

| Criterio | Meta | Cómo se mide |
|---|---|---|
| Turno nuevo no espera al worker | ACK local p95 ≤ 100 ms en 200 repeticiones, con una tarea en `working` | `test_turn_does_not_wait_for_worker` |
| Estados conectados a eventos reales | `received → queued → working → ready` solo por sus hooks; `blocked` por `kanban_task_blocked`; `waiting` derivado de padres abiertos | `test_claimed_and_spawned_transitions`, `test_blocked_state`, `test_waiting_derived_from_open_parent` |
| Sin falsos `listo` | `completed` sin entrega persistida no pone `ready` | `test_no_false_ready_when_store_fails` |
| Sin dobles despachos | Misma key → una sola fila de tarea | `test_no_double_dispatch_same_key` |
| Sin tareas sin permiso | `kanban_create` a perfil no listado se bloquea; con cola llena se bloquea | `test_create_to_unlisted_profile_blocked`, `test_queue_limit_10` |
| Serialización por recurso | Mismo recurso → `parents` fusionados; distinto recurso → sin `parents` | `test_serialization_same_resource_via_parents` |
| Cancelación en 3 niveles | Respuesta no cancela la tarea; tarea cancelada no se anuncia; stop-all cancela las abiertas, bloquea nuevos encargos y conserva lo persistido | `test_cancel_three_levels`, `test_stop_all_keeps_persisted` |
| Stop-all reanudable | Reintento mismo turno bloqueado; reintento turno posterior con la misma key pasa y limpia el flag | `test_stop_all_resume_on_retry` |
| Estados visibles en el turno | La nota dinámica muestra la tarea con su estado real tras cada evento | `test_states_in_turn_note` |
| Aviso en la barra (JS) | `kanban_task_completed` → contador listo (dedup por `task_id`); `claimed` → en cola; `blocked` → bloqueado; sin sesión en el evento no se nombra sesión | `tests/test_fase3_desktop.mjs` (node) + `test_desktop_js_suite` |
| Sin regresiones | Batería 129 tests (mismos 5 FAIL esperados de A0.7); puertas 1a 9/9, 1b 17/17, 2 16/16 | Re-ejecución completa |

## 5. Qué queda fuera de esta puerta (pendiente explícito)

- Bar ≤ 1 s desde persistencia (no medible offline; Fase 5).
- Aviso en la barra de entregas ocurridas con la app cerrada (necesita el
  RPC Q5 puente B; propuesta al core, no implementada).
- Cancelación real del worker del core (necesita B3; diseño, no
  implementado). En ruta A el worker termina por su cuenta.
- Disparo por evento / tareas programadas (Fase 3b, solo horario).
