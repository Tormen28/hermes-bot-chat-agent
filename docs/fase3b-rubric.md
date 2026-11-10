# Rúbrica Fase 3b — Tareas programadas (nivel A, offline)

**Versión:** 1.0 — congelada el 2026-11-08, antes de implementar.
**Base:** plan v1.2 §"Fase 3b · Tareas programadas", wow 6 ("te lo recuerdo y
lo hago solo"). Sin cambios en el core. El plugin es Python; la UX vive en
el chat y en la nota del turno, no se inventa ninguna UI nueva fuera del
plugin.

## 1. Qué hace la Fase 3b (y qué no)

1. **El cron del core programa; el plugin coordina y entrega.** El plugin no
   tiene hilos temporizadores ni scheduler propio: ni `threading.Timer`, ni
   bucle de espera, ni `sched`. La única programación la hace el ticker del
   core. El plugin solo observa, registra y entrega.
2. **Solo disparo por horario en v1.2.** El disparo por evento (mensaje nuevo,
   cambio en el tablero) va al backlog: NO se implementa. Crear con un
   disparador de evento se rechaza (`ValueError`).
3. La creación exige permiso explícito del usuario: una tarea programada solo
   nace de una petición explícita en el chat (el agente la crea con la
   herramienta `cronjob` del core). El plugin registra el consentimiento
   (sesión de origen, instante, fuente `user_request`) en su registro durable.
   **Ninguna tarea se ejecuta sin permiso**: sin registro con consentimiento
   no hay entrega, aunque el trabajo exista en el core.
4. El disparo se entrega a la sesión de origen por el outbox (camino Fase 1:
   `delivery_store.py`, protocolo `pending → offered → delivered_in_turn`).
   El disparo solo entrega (aviso/resultado); nunca ejecuta herramientas en
   silencio.
5. Stop-all pausa las tareas programadas de la sesión (conecta con
   `store.stop_all_active(session_id)` / `background_tasks.stop_all`). La
   reanudación es explícita por tarea (`cronjob resume`); limpiar el flag de
   stop-all NO reanuda las tareas programadas.

## 2. API real del cron del core que se usa (verificada contra el vivo `2bd378ed`)

Checkout: `~/workspace/hermes-work/hermes-agent`, rama
`core-repairs/plugin-surface` (`73c1d5c48`; el cron es código del core sin
tocar). Todas las lecturas/escrituras del almacén del core pasan por estas
funciones; **el plugin nunca escribe `jobs.json` a mano** (eso sería repetir
E1/E4).

| Función | Qué hace | Dónde vive |
|---|---|---|
| `cron.jobs.create_job(prompt, schedule, name=None, repeat=None, deliver=None, origin=None, ..., no_agent=False, script=None, timezone=None, grace_minutes=None, paused=False)` | Crea el trabajo; devuelve el registro con `id` (12-hex), `next_run_at`, `state`, `schedule_display`. `deliver` por defecto es `"origin"` si hay `origin`, si no `"local"`. | `cron/jobs.py:1809` |
| `cron.jobs.parse_schedule(s)` | `"30m"`/`"every 2h"` intervalo; `"in 30m"` one-shot; `"every monday 9am"` / `"0 9 * * *"` cron (necesita `croniter`); ISO `"2026-06-01T09:00:00Z"` one-shot. Sin segundos: la duración mínima es el minuto. | `cron/jobs.py:787` |
| `cron.jobs.get_job(job_id)` / `resolve_job_ref(ref)` / `list_jobs(include_disabled=False)` | Lectura (la lista añade `latest_execution` / `live_execution`). | `cron/jobs.py:1951` |
| `cron.jobs.pause_job(job_id, reason=None)` / `resume_job(job_id)` / `remove_job(job_id)` / `update_job(job_id, updates)` | Pausa/reanuda/elimina/edita **por trabajo**. No existe pausa por sesión: el stop-all pausa cada trabajo de la sesión, uno a uno. | `cron/jobs.py:2119` |
| `cron.jobs.save_job_output(job_id, output)` | Guarda la salida en `<home>/cron/output/<job_id>/YYYY-MM-DD_HH-MM-SS.md` (0600). | `cron/jobs.py:3353` |
| `cron.executions.list_executions(job_id=..., limit=...)` / `latest_execution(job_id)` / `get_execution(id)` | Libro de ejecuciones (SQLite `<home>/cron/executions.db`): `id, job_id, status (claimed/running/completed/failed/unknown/cancelled/missed), claimed_at, started_at, finished_at, error, scheduled_instant, delivery_outcome`. **No lleva el texto de salida.** | `cron/executions.py:640` |
| `cron.scheduler.run_one_job(job, ...)` | Dispara un trabajo debido de punta a punta (ejecuta → guarda salida → entrega → marca). Con `no_agent=True` el trabajo es un script: sin modelo, sin red, sin coste. | `cron/scheduler.py:2797` |
| `cron.jobs.get_due_jobs()` | Escaneo de vencidos real (avanza `next_run_at` ANTES de despachar: at-most-once ante un crash a mitad de disparo). | `cron/jobs.py:2868` |
| `tools.cronjob_tools.cronjob(action, job_id, prompt, schedule, name, repeat, deliver, ...)` | Herramienta del agente: `create/list/remove/pause/resume/run/update/cancel`. `create` devuelve JSON con `job_id, name, schedule, next_run_at, deliver`. | `tools/cronjob_tools.py:1048` |

### Cómo llega un disparo a su sesión (camino elegido)

- El trabajo se crea con **`deliver="local"`**: el core guarda la salida y
  NO entrega nada por su cuenta (`_resolve_delivery_targets` devuelve `[]`
  para `"local"`; `delivery_outcome: suppressed`). El plugin impone
  `deliver="local"` en `pre_tool_call` (parche `modify`, como el parche de
  idempotencia de `kanban_create`): un `deliver` distinto (p. ej.
  `"telegram"`, `"bot-chat"`) sacaría el disparo del outbox y de la sesión
  de origen.
- El plugin observa los disparos **solo desde estado durable**: el libro de
  ejecuciones (fila terminal `completed`/`failed`/`cancelled`) + el fichero
  de salida más reciente del trabajo. La observación ocurre en
  `pre_llm_call` (antes de la oferta del turno), nunca a mitad de turno y
  nunca por un temporizador del plugin. Un disparo cuya sesión no tiene turno
  queda pendiente en el libro y se entrega en el siguiente turno — el mismo
  modelo que las entregas kanban.
- La salida se extrae del documento guardado: tras `## Response` (trabajos
  con agente) o tras la primera línea `---` (trabajos `no_agent`, stdout
  verbatim). El resumen del outbox se trunca a 500 caracteres; el texto
  completo queda en el fichero del core.
- Exactamente una vez: la tabla `scheduled_fires` (clave `execution_id`)
  marca cada ejecución entregada; `record()` del outbox es idempotente por
  `task_id = "cron:<job_id>:<execution_id>"` (único por disparo, así un
  trabajo recurrente entrega cada disparo en su propia fila).

### Qué NO existe en el core (huecos documentados, no inventados)

- **No hay pausa por sesión.** `pause_job` es por trabajo (pone
  `enabled: False`, `state: paused` y conserva `next_run_at`; `resume_job`
  mantiene un instante ya pasado como debido). El stop-all del plugin itera
  su registro y pausa cada trabajo de la sesión.
- **No hay entrega por sesión.** `deliver="bot-chat"` entrega a un *perfil*
  (Bot Chat del perfil) como turno entrante, no a una sesión concreta, y
  puentea el outbox (sin lease, sin wow 8). Por eso la Fase 3b usa
  `deliver="local"` + observación + outbox.
- **El libro de ejecuciones no lleva la salida.** Hay que leer el fichero
  de salida; si falta (disparo fallido antes de guardar), la entrega lleva
  el `error` del libro.
- **`operation()` de `gateway/session_cron.py` (`submit`/`status`/`cancel`/
  `recover`) ejecuta un disparo, no gestiona trabajos.** No se usa.
- **El ticker real dispara cada 60 s** (`tick()` avanza `next_run_at` antes
  de despachar). La puerta corre el disparo real con `run_one_job` sobre
  trabajos `no_agent` (script), que es el mismo camino del ticker sin
  modelo: el plugin solo lee los artefactos durables que ese camino deja,
  idénticos para trabajos con agente.

## 3. Registro durable del plugin (SQLite, mismo estilo que el outbox)

Nuevas tablas en `deliveries.db` (`SCHEMA_VERSION` 3 → 4), nunca un JSON con
read-modify-write entre procesos:

| Tabla | Clave | Contenido |
|---|---|---|
| `scheduled_tasks` | `job_id` (id del core) | `session_id` (origen), `name`, `prompt`, `schedule_display`, `schedule_raw`, `status` (`active`/`paused`/`paused_by_stop_all`/`removed`), `paused_reason`, `consent_at`, `consent_source`, `created_at`, `updated_at` |
| `scheduled_fires` | `execution_id` | `job_id`, `session_id`, `status` (`delivered`/`failed`/`silent`), `delivery_id` (fila del outbox, o NULL en `silent`), `finished_at`, `created_at` |

Reglas: el registro es la única fuente de "esta sesión tiene permiso para
este trabajo"; un trabajo del core sin fila en el registro nunca se entrega.
`paused` (manual) y `paused_by_stop_all` son distintos: la reanudación
explícita solo toca el segundo. `removed` es terminal (el trabajo ya no
existe en el core).

### Máquina de estados (registro del plugin)

| Estado | Evento real que lo pone |
|---|---|
| `active` | `cronjob create` observado en `post_tool_call` (éxito, con consentimiento) |
| `paused` | `cronjob pause` observado en `post_tool_call` |
| `paused_by_stop_all` | stop-all de la sesión (`background_tasks.stop_all`) |
| `active` (de nuevo) | `cronjob resume` observado; si estaba `paused_by_stop_all`, además limpia el flag de stop-all (la reanudación explícita es la confirmación del usuario) |
| `removed` | `cronjob remove` observado |

El core es la fuente de `next_run_at` / `state` en vivo; el registro guarda
`schedule_display` como texto para mostrar sin depender del core.

## 4. Puerta (nivel A, offline, bloqueante) — `tests/test_fase3b_gate.py`

La puerta corre contra el core REAL (checkout vivo `2bd378ed`): creación,
vencimiento, disparo (`run_one_job`, trabajos `no_agent` con script) y libro
de ejecuciones son llamadas reales al core. El disparo del agente (modelo)
no se ejecuta offline (sin red ni coste): el plugin solo lee los artefactos
durables del disparo, que son idénticos para trabajos con agente y
`no_agent`. El proceso del core corre en un subproceso con el Python del
runtime PM (`tests/_fase3b_core_driver.py`, protocolo JSON por stdin); el
proceso de la puerta (Python del sistema) observa y entrega sobre los mismos
ficheros en disco. Sin temporizadores del plugin en ningún punto.

Criterios:

1. **20 tareas dentro de ±60 s:** 20 one-shots con instantes programados en
   una ventana de 10 s; el disparo real las ejecuta todas; el observador las
   entrega a su sesión de origen; cada entrega queda registrada ≤ 60 s
   después de su instante programado; 0 faltantes; el contenido entregado es
   la salida del script.
2. **0 perdidas tras reinicio:** con disparos ya reservados en disco, se
   reconstruyen el almacén y el observador desde cero (objetos nuevos, mismos
   ficheros) y se vuelve a observar: lo no entregado se entrega, lo entregado
   no se duplica.
3. **Sin dobles disparos:** observar dos veces (dos turnos) el mismo
   disparo produce exactamente una fila de entrega y una fila en
   `scheduled_fires`; un trabajo recurrente disparado dos veces produce dos
   entregas con `task_id` distintos.
4. **Sin ejecución sin permiso:** un trabajo del core sin registro (sin
   consentimiento) dispara pero el observador NO lo entrega; un trabajo
   pausado no aparece en el escaneo de vencidos; crear con disparador de
   evento se rechaza.
5. **Stop-all:** pausa los trabajos activos de la sesión en el core
   (`state: paused`, `enabled: False`; `next_run_at` se conserva por
   diseño del core) y los marca `paused_by_stop_all`; no toca los de otra
   sesión; la reanudación explícita de uno limpia el flag y reanuda solo
   ese.
6. **Forzado de `deliver="local"`:** `pre_tool_call` parchea a `local`
   cualquier `deliver`/`failure_deliver` distinto en un `cronjob create` de
   Bot Chat; bloquear la creación mientras stop-all está activo.
7. **Estados visibles:** la nota del turno lista las tareas programadas de
   la sesión (nombre, horario, próximo disparo / pausada); el delta no
   repite lo que no cambió.
8. **Sin scheduler propio:** el tiempo por sí solo nunca entrega nada (sin
   turno no hay observación); la entrega de un disparo fallido es honesta
   (`blocked` con el error, no `completed`).
9. **La batería sigue en verde** (152 tests, mismos 5 FAIL esperados de
   A0.7) y las puertas 1a (9/9), 1b (17/17), 2 (16/16) y 3 (23/23) + node
   10/10 siguen pasando.

## 5. Límites honestos (documentados, no escondidos)

- **El aviso llega en el siguiente turno, no al instante.** El plugin no
  tiene proceso que vigile: si la sesión no tiene turno, el disparo espera
  en el libro hasta el siguiente `pre_llm_call`. Igual que kanban.
- **La barra de estado no se actualiza sin turno.** Sin el RPC Q5 (puente B,
  propuesta al core, no implementada) el SDK no puede leer el outbox; el
  estado visible vive en la nota del turno.
- **Trabajos creados fuera de Bot Chat (CLI, otra sesión) no se entregan.**
  El registro solo conoce los trabajos creados desde una sesión Bot Chat
  (observados en `post_tool_call`). Adoptar trabajos preexistentes es
  backlog.
- **La reanudación tras stop-all es explícita por tarea.** Limpiar el flag
  de stop-all (p. ej. reintentando un `kanban_create`) no reanuda las tareas
  programadas.
- **El observador lee el fichero de salida más reciente por nombre.** Si
  dos disparos del mismo trabajo caen en el mismo segundo, el fichero
  (`YYYY-MM-DD_HH-MM-SS.md`) puede colisionar en el lado del core; la
  entrega seguiría siendo única por `execution_id`, pero el contenido podría
  ser el del otro disparo. En la práctica el ticker no dispara dos veces el
  mismo trabajo en el mismo segundo (el claim lo impide).
- **`tui_gateway/` no está materializado en el clon parcial del core.**
  El scheduler importa `tui_gateway.launch_profile_policy` en cada disparo;
  la puerta extrae esos dos ficheros reales (byte a byte, vivo `2bd378ed`,
  vía `git show`) a un directorio temporal y los pone primeros en
  `sys.path`, sin tocar el checkout. En una instalación viva de Hermes el
  paquete está completo y no hace falta nada.
- **El Python del sistema (3.12.3) no puede importar el core** (faltan
  `ruamel`, `dotenv`, …). La puerta corre el core en un subproceso con el
  Python del runtime PM; el proceso de la puerta lee los mismos artefactos
  en disco con `sqlite3` de la stdlib (el mismo patrón que la lectura de
  `state.db`/`kanban.db`).
