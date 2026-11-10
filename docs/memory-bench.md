# Memoria genérica — medición antes / después

Benchmark sintético, offline, sin modelo y sin red: `tests/_memory_bench.py`.
El PC de Gustavo estaba apagado, así que no hay cifras de una sesión viva.
Las sesiones son conversaciones deterministas de 10, 25, 50, 100 y 200 turnos
(mismos textos, mismos steers a mitad de turno). Tokens: `context_budget.estimate_tokens`
(`chars/4` en esta VM; Python 3.13.5). Latencia: 5 repeticiones del `pre_llm_call`
final de cada longitud (p50 / p95). Rama `feat/bot-chat-generic-memory`, base `3394829`.

Qué se mide:

- **injected**: lo que devuelve `pre_llm_call` (`{"context": ...}`). En el core v0.23 eso se añade al mensaje de usuario; no sustituye el historial.
- **history**: tamaño del `conversation_history` sintético, que el core seguiría enviando entero.
- **total prompt**: history + injected (aproximación de lo que vería el modelo en v0.23).
- **bounded view** (solo después): resumen + ventana reciente + pendientes + memoria larga relevante. Es el paquete acotado que el plugin *puede* construir; no es lo que el core v0.23 manda, salvo que se active `context.engine: bot-chat` (y aun ahí el motor hoy añade contexto, no recorta el historial).

JSON crudo: `docs/memory-bench-before.json`, `docs/memory-bench-after.json`.

## Cifras

| Turnos | injected antes (tok) | injected después (tok) | history (tok) | bounded view después (tok) | pre_llm p50 antes (ms) | pre_llm p50 después (ms) | pre_llm p95 después (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 297 | 720 | 4 167 | 751 | 4.3 | 9.5 | 21.8 |
| 25 | 297 | 720 | 10 711 | 749 | 4.1 | 6.1 | 8.6 |
| 50 | 297 | 744 | 21 179 | 776 | 6.1 | 10.2 | 11.1 |
| 100 | 297 | 720 | 42 379 | 752 | 4.4 | 8.3 | 9.0 |
| 200 | 297 | 721 | 84 924 | 750 | 4.0 | 11.6 | 13.2 |

Pendiente last vs first (200 / 10 turnos):

- injected antes: **1.00×** (ya era plano: estilo + nota)
- injected después: **1.00×** (el bloque de memoria queda acotado; sube el piso ~420 tokens)
- history: **20.38×** (lineal; igual antes y después)
- bounded view: **1.00×** (~750 tokens en 10 y en 200 turnos)
- total prompt en v0.23 (200 turnos): 85 221 antes → 85 645 después (el historial del core sigue dominando)

## Cómo leerlo

El contexto que inyecta el plugin **ya estaba acotado** antes de esta pieza (297 tokens fijos). La memoria añade un bloque también acotado (~720 tokens inyectados, ~750 en la vista compacta) y no crece con la sesión.

Lo que sí crece, y es lo que pesa en una sesión larga en Hermes v0.23, es el `conversation_history` del core (~85 k tokens a los 200 turnos en este banco). El hook `pre_llm_call` no puede recortarlo: solo puede inyectar. Por eso estas cifras **no demuestran** que la lentitud de una sesión larga viva haya quedado resuelta. Eso haría falta medirlo en el PC, con el core real y, si se quiere recortar historial, con un cambio de core o un `select_context` que sustituya mensajes (hoy no lo hace).

La latencia del hook sigue siendo de unos milisegundos (p50 6–12 ms). Un p95 de 21.8 ms en la serie de 10 turnos es un solo sample en frío (~24.6 ms); no hay pendiente de crecimiento con la longitud.

## Límites honestos

- Banco sintético, no una sesión real de Gustavo.
- Tests en verde no prueban la lentitud en vivo.
- Extracción de memoria larga por pistas literales (`Decision:`, `Lesson learned:`, `Decidimos`, `Lección:`, …), no por un modelo.
- Sin vault externo ni segundo cerebro: el almacén es solo `<HERMES_HOME>/bot-chat-agent/memory.db`.
