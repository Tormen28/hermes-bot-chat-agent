# Rúbrica Fase 2 — Contexto acotado (nivel A, offline)

**Versión:** 1.0 — congelada el 2026-11-08, antes de implementar.
**Base:** plan v1.2 §6 y Fase 2. Presupuesto: 1.500 tokens dinámicos +
300 estables (plan §11). Decisión de Gustavo: B4 Opción 0 (sin cambio en
el core; Bot Chat registra un `ContextEngine` delegante).

## 1. Presupuestos (medidos con `context_budget.estimate_tokens`)

| Sección | Tope | Qué contiene |
|---|---|---|
| Estable | ≤ 300 tok | Contrato de estilo (STYLE), byte-estable |
| Dinámica | ≤ 1.500 tok | Ficha de proyecto + nota de equipo (delta o completa) |
| Por pieza | ≤ 2.000 tok | Cada pieza de contexto (política del plugin, B4-2) |
| Hook (core) | ≤ 10.000 caracteres | Límite del core (`tools/hook_output_spill.py`) |

Estimador: `tiktoken` (`cl100k_base`) cuando está instalado; si no,
`ceil(chars/4)` — la misma aproximación calibrada en la Fase 0 (A0.8,
contrastada con palabras × 1,33, ±5 %). El estimador es conservador:
nunca se promete un ahorro facturado.

## 2. Corpus fijo (el mismo en todos los escenarios de la puerta)

- **8 restricciones críticas** (de la ficha de proyecto): nunca se
  recortan, nunca se reescriben, nunca se pierden en compactación,
  cancelación ni reapertura.
- **20 hechos**: objetivo, decisiones, pendientes, estado de tarjetas,
  resultados de entregas (por ID) y log mínimo de herramientas con
  efectos.
- **3 ediciones manuales** de la ficha: la edición manda sobre cualquier
  texto derivado; queda versionada.

## 3. Puerta nivel A (offline, bloquea)

| Criterio | Meta | Cómo se mide |
|---|---|---|
| Presupuesto respetado | 100 % de los renders | estable ≤ 300, dinámica ≤ 1.500, pieza ≤ 2.000 |
| Restricciones críticas | 100 % presentes | en el contexto reconstruido, textuales |
| Hechos | ≥ 95 % presentes (≥ 19/20) | en el contexto reconstruido |
| Ediciones manuales | 100 % respetadas | texto editado textual; texto derivado en conflicto ausente |
| Nota incremental | turno sin cambios ≤ 15 tok | solo la línea "Sin cambios" |
| Delta | solo lo que cambió | ni una tarjeta sin cambio ni un aviso ya confirmado |

## 4. Escenarios de reconstrucción (el contexto se reconstruye desde el estado durable)

1. **Tras cancelación:** turno interrumpido (`agent_loop_stopped`) →
   la huella se borra, el turno siguiente reconstruye completo desde el
   store + kanban + ficha.
2. **Tras compactación:** `compress()` delega en el `ContextCompressor`
   interno y le pasa como `memory_context` el bloque protegido
   (restricciones críticas + IDs de resultados + log mínimo de efectos).
   La reconstrucción posterior contiene el 100 % de las restricciones y
   ≥ 95 % de los hechos.
3. **Tras reapertura:** proceso nuevo, sin estado en memoria → el
   contexto se reconstruye completo desde el store + kanban + ficha.

## 5. Política de recorte (orden de prioridad, de mayor a menor)

1. Restricciones críticas (nunca se recortan; si solo ellas superan el
   presupuesto, se renderizan igual y se registra un `warning`).
2. Objetivo de la ficha.
3. Avisos nuevos (entregas ofrecidas este turno).
4. Cambios de tarjetas (nuevas, cambio de estado, terminadas).
5. Decisiones y pendientes de la ficha.
6. Líneas de bandeja sin asignar y avisos de almacén.
7. IDs de resultados entregados.

Lo recortado queda contabilizado en el campo `dropped` del render
(nunca se pierde en silencio).

## 6. Reglas de la nota incremental

- La huella por sesión guarda: tarjetas `{id: estado}`, avisos ya
  confirmados, contador de bandeja, versión de la ficha, aviso de
  almacén. Vive en SQLite (`context_state`), una fila por sesión.
- Un aviso ofrecido pero no confirmado se vuelve a renderizar en el
  turno siguiente (un turno interrumpido no confirma nada).
- La huella se borra al cancelar (`agent_loop_stopped`), al compactar
  (el propio engine) y al reiniciar sesión: el turno siguiente
  reconstruye completo.
- La huella la escribe solo el proceso del chat (un solo escritor).

## 7. Ficha de proyecto (wow 7)

- Vive en `<HERMES_HOME>/bot-chat-agent/project-card.md`: visible y
  editable en cualquier editor. Es la fuente de verdad.
- Cada cambio de contenido crea una versión nueva en SQLite
  (`project_card_versions`): contenido derivado **nunca** sobrescribe el
  archivo; solo una edición explícita (manual o `edit_card`) escribe.
- Sin archivo no hay sección de ficha (sin cambio de comportamiento
  para quien no la usa).

## 8. ContextEngine delegante (Opción 0)

- `BotChatContextEngine` extiende el ABC del core
  (`agent/context_engine.py`); delega `compress`, `should_compress` y
  `update_from_response` en una instancia interna de
  `ContextCompressor`; solo sobrescribe `select_context` (contexto
  acotado por petición, no persistido) y `on_turn_complete`.
- `clone_for_agent()` crea un engine nuevo con un compresor nuevo (el
  engine no guarda conexiones ni locks).
- Activación: el usuario pone `context.engine: bot-chat` en la config
  del core. Sin eso, el engine queda registrado pero inactivo y el
  camino `pre_llm_call` sigue siendo el activo (fail-open).
- `select_context` añade el contexto acotado solo si el mensaje de
  usuario del turno no trae ya el estilo (marca `[Bot Chat style]`):
  el camino del hook y el del engine nunca duplican.
- "Congelado en `api_content`" aceptado (lo implica la Opción 0): el
  contexto del hook se congela en el turno; el del engine es por
  petición y no se persiste.

## 9. Qué NO mide esta puerta (nivel B, Fase 5, pagado)

Que el modelo cumpla las restricciones o recuerde los hechos. El nivel
A solo mide que el contexto reconstruido los contenga.
