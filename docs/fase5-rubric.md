# Rúbrica Fase 5 — Demostrar rapidez y coherencia (nivel A, offline)

**Versión:** 1.0 — congelada el 2026-11-08, antes de medir.
**Base:** plan v1.2 §5 Fase 5. Decisión de Gustavo: el piloto pagado NO está
autorizado (solo con permiso y presupuesto); se documenta como pendiente,
no se ejecuta.

## 1. Qué se mide (definiciones honestas, offline)

Todo se mide en esta VM (Linux), con el runtime objetivo Python 3.14.9
(`~/.hermes/installs/…/pm-runtime/…/bin/python`) y también con el
Python 3.12.3 por defecto; cada cifra publicada dice con cuál se midió.
Sin red, sin modelo, sin coste. Donde algo no es medible offline, se dice
y se documenta qué necesita la máquina viva — no se promete.

| Ruta | Qué es exactamente (offline) | Fría / caliente |
| --- | --- | --- |
| ACK | `can_launch` + `background_tasks.launch`: el acuse local de que un encargo de fondo fue aceptado | Fría: primer lanzamiento con almacén nuevo. Caliente: estado estable |
| TTFT (lado plugin) | Tiempo desde que empieza `_on_pre_llm_call` hasta que el plugin devuelve su primer contenido visible para el usuario (el contexto/oferta del turno). **No** es el TTFT de extremo a extremo: sin modelo no hay primer token; el extremo a extremo necesita la máquina viva | Fría: primer turno con home nuevo. Caliente: turnos seguidos |
| Cola | Latencia de `can_launch` (vacía, 9 abiertas, llena) y del rechazo con la cola llena (10) | Caliente |
| Herramientas | Latencia de la guardia en `pre_tool_call` (permitida y bloqueada) y de `post_tool_call` | Caliente |
| Handoff | `record` (proceso worker) → `offer` (proceso del chat): dos conexiones distintas sobre el mismo archivo SQLite (la coordinación entre procesos ES SQLite; validación real con 2 procesos aparte) | Caliente |
| Entrega | Ciclo `offer` → `confirm` en un turno; relay `claim_for_relay` → `confirm_ids` | Caliente |
| Interrupción | Latencia del handler `agent_loop_stopped` + tiempo hasta que las ofertas quedan liberadas | Caliente |
| Recuperación | Reapertura + `integrity_check` tras `kill -9` a mitad de una escritura; 0 pérdidas | — |
| Barra ≤ 1 s | Descomposición medible: (a) persistencia Python `record()`; (b) JS `applyTeamEvent` + `classifyPhase` en node. **El despacho de eventos del gateway en medio NO es medible offline** y queda para la máquina viva. El ≤ 1 s de extremo a extremo sigue sin probarse offline | Caliente |
| Pocos recursos | (a) almacén precargado con 10.000 filas; (b) contención de CPU (2 procesos busy-loop durante la medición) | Caliente |
| Errores | Hook con la base corrupta o bloqueada: debe fallar cerrado, registrar y no perder nada | — |
| Sesiones largas | 10 secuencias de 100 turnos: por turno `pre_llm_call` + `post_llm_call`, con un `kanban_task_completed` cada 10 turnos y una interrupción cada 25 | Caliente |

## 2. Método

- **Repeticiones:** ≥ 200 por ruta común, 100 por caso de estrés de hook
  (`claimed`, `spawned`, `completed`, `blocked`), diez secuencias de 100 turnos.
- **Publicación por ruta:** p50, p95, n, dispersión (min, max, desviación
  estándar) y fallos. Sin medias solas: la media esconde la cola.
- **Separación:** ACK, TTFT, cola, herramientas, handoff, entrega,
  interrupción y recuperación se miden y se publican por separado; ninguna
  cifra mezcla dos rutas.
- **Sin pérdidas ni regresiones:** en cada secuencia se contabiliza
  registrado / ofrecido / confirmado / perdido / duplicado / filtrado a
  otra sesión. Cualquier pérdida, duplicado visible o filtración entre
  sesiones es un fallo de la puerta, no un dato más.
- **Evals como puerta:** los fixtures de seguridad (A0.7), cancelación,
  contienda entre procesos y privacidad entre sesiones son las puertas
  1a/1b/2/3/3b/4 + la batería completa; corren en cada cambio y el informe
  de medición registra su resultado.

## 3. Metas (solo donde el plan ya anunció una mejora)

| Meta | Valor | Origen |
| --- | --- | --- |
| ACK local p95 | ≤ 100 ms (200 repeticiones) | Fase 3, puerta nivel A |
| TTFT lado plugin, caliente, p95 | ≤ 100 ms (200 repeticiones) | Orden de magnitud del ACK; la contribución del plugin al primer token no debe dominar el turno |
| TTFT lado plugin, fría, p95 | ≤ 500 ms (200 repeticiones) | Primera llamada con home nuevo |
| Handlers de hooks kanban, p95 | ≤ 100 ms (100 repeticiones por hook) | Una transacción corta cada uno; mismo orden que el ACK |
| Guardia `pre_tool_call`, p95 | ≤ 100 ms (200 repeticiones) | La guardia no debe ser el cuello de botella de una herramienta |
| Handoff `record` → `offer`, p95 | ≤ 100 ms (200 repeticiones) | La entrega entre procesos no debe esperar al turno |
| Ciclo `offer` → `confirm`, p95 | ≤ 100 ms (200 repeticiones) | Confirmación dentro del turno |
| Interrupción: handler + liberación, p95 | ≤ 500 ms (200 repeticiones) | Incluye maintenance + relay best-effort |
| Recuperación tras kill -9 | 0 pérdidas; reapertura + `integrity_check` ≤ 1 s | Fase 1b: crash tests en verde |
| Sesiones largas (10 × 100 turnos) | 0 pérdidas, 0 duplicados, 0 filtraciones; presupuestos respetados en todos los turnos (dinámica ≤ 1500, estable ≤ 300, pieza ≤ 2000); p50 del último decil ≤ 2× p50 del primero (sin crecimiento sin cota) | Puerta Fase 5: sin pérdidas ni regresiones |
| Pocos recursos (10.000 filas) | 0 pérdidas; p95 caliente ≤ 2× p95 sin carga | Sin regresión crítica bajo carga |
| Pocos recursos (contención CPU) | 0 pérdidas; se publica la degradación, sin meta absoluta (el hardware manda) | Publicar, no prometer |
| Barra JS (pura), p95 | ≤ 50 ms (200 repeticiones en node) | `applyTeamEvent` + `classifyPhase` son funciones puras |
| Barra de extremo a extremo ≤ 1 s | **No se afirma offline.** Se publican (a) persistencia + (b) JS y se documenta el hueco del gateway | Fase 3, pendiente explícito |

## 4. Puerta nivel A (offline, bloquea)

| Criterio | Meta | Cómo se mide |
| --- | --- | --- |
| ACK | p95 ≤ 100 ms, 200 reps | `test_ack_p95` |
| TTFT lado plugin | caliente p95 ≤ 100 ms, fría p95 ≤ 500 ms, 200 reps cada una | `test_ttft_plugin_side` |
| Cola | `can_launch` y rechazo con cola llena medidos; 0 aceptados de más | `test_queue_latency_and_limit` |
| Herramientas | guardia p95 ≤ 100 ms (permitida y bloqueada), 200 reps | `test_tool_guard_latency` |
| Handoff | p95 ≤ 100 ms, 200 reps, 0 pérdidas | `test_handoff_latency` |
| Entrega | ciclo p95 ≤ 100 ms, 200 reps, 0 pérdidas | `test_delivery_cycle_latency` |
| Interrupción | handler + liberación p95 ≤ 500 ms, 200 reps | `test_interruption_latency` |
| Hooks, estrés | cada hook p95 ≤ 100 ms, 100 reps | `test_hook_stress_latency` |
| Sesiones largas | 10 × 100 turnos, 0 pérdidas/duplicados/filtraciones, presupuestos, sin crecimiento sin cota | `test_long_sessions_10x100` |
| Pocos recursos | 10.000 filas: 0 pérdidas, p95 ≤ 2× base | `test_low_resources_loaded_store` |
| Errores | base corrupta/bloqueada: fail-closed, 0 pérdidas | `test_error_paths_fail_closed` |
| Recuperación | kill -9: 0 pérdidas, reapertura + integridad ≤ 1 s | `test_recovery_after_kill9` |
| Barra (medible) | JS p95 ≤ 50 ms, 200 reps en node; persistencia publicada | `test_bar_js_latency` |
| Sin regresiones | Batería 200 tests (mismos 5 FAIL esperados de A0.7); puertas 1a 9/9, 1b 17/17, 2 16/16, 3 23/23, 3b 17/17, 4 31/31 + node 10/10 | Re-ejecución completa |

## 5. Qué queda fuera de esta puerta (pendiente explícito)

- **TTFT de extremo a extremo y barra ≤ 1 s de extremo a extremo:**
  necesitan la máquina viva (modelo + gateway + app de escritorio). Aquí se
  publican las partes medibles offline y el hueco exacto.
- **Nivel B de coherencia (el modelo cumple las restricciones) y
  comparación de modelos (30 tareas emparejadas):** piloto pagado, solo con
  permiso y presupuesto de Gustavo. No se ejecuta. Ver
  `docs/fase5-piloto-pendiente.md`.
- **Medición en Windows:** el vivo es Windows; esta VM es Linux. Las cifras
  de SQLite y de contienda entre procesos son del entorno de desarrollo;
  la confirmación en Windows queda para la Fase 6 (canario), con la
  batería completa corriendo allí.
