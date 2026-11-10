# Fase 5 — Medición (publicado)

Generado por `tests/_fase5_bench.py` (rúbrica `docs/fase5-rubric.md` v1.0).
Runtime: offline, esta VM (Linux) · Python 3.14.9 · sin red, sin modelo, sin coste.

## Cifras por ruta

| Ruta | n | p50 (ms) | p95 (ms) | min | max | media | desv. | fallos |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ack/hot | 200 | 4.377 | 4.898 | 4.219 | 18.539 | 4.576 | 1.165 | 0 |
| ack/cold | 200 | 4.816 | 6.144 | 4.428 | 12.837 | 5.046 | 0.956 | 0 |
| ttft/pre_llm_hot | 200 | 8.876 | 9.53 | 8.669 | 20.891 | 9.042 | 0.922 | 0 |
| ttft/pre_llm_hot_pending | 200 | 10.984 | 12.185 | 9.456 | 13.108 | 10.973 | 0.844 | 0 |
| ttft/pre_llm_cold | 200 | 11.964 | 14.013 | 11.642 | 20.136 | 12.305 | 1.287 | 0 |
| delivery/post_llm | 200 | 8.555 | 9.851 | 7.247 | 10.432 | 8.632 | 0.755 | 0 |
| interrupt/agent_loop_stopped | 200 | 8.431 | 8.875 | 7.738 | 11.47 | 8.404 | 0.419 | 0 |
| hook/claimed | 100 | 2.403 | 2.521 | 2.325 | 2.932 | 2.413 | 0.077 | 0 |
| hook/spawned | 100 | 1.367 | 1.49 | 1.323 | 1.723 | 1.383 | 0.06 | 0 |
| hook/completed | 100 | 4.785 | 4.999 | 4.621 | 6.308 | 4.834 | 0.23 | 0 |
| hook/blocked | 100 | 5.796 | 6.305 | 5.612 | 6.9 | 5.859 | 0.239 | 0 |
| tools/guard_allow | 200 | 0.12 | 0.186 | 0.113 | 0.641 | 0.133 | 0.043 | 0 |
| tools/guard_block | 200 | 0.012 | 0.012 | 0.011 | 0.048 | 0.013 | 0.004 | 0 |
| tools/post_tool | 200 | 4.828 | 5.564 | 4.576 | 8.354 | 4.918 | 0.359 | 0 |
| handoff/record_to_offer | 200 | 2.754 | 3.108 | 2.614 | 6.511 | 2.842 | 0.4 | 0 |
| delivery/offer_to_confirm | 200 | 2.848 | 3.285 | 2.702 | 5.337 | 2.911 | 0.252 | 0 |
| delivery/relay_claim_confirm | 200 | 2.878 | 3.162 | 2.726 | 3.916 | 2.905 | 0.149 | 0 |
| handoff/offer_1000_pending | 200 | 12.405 | 13.781 | 10.65 | 21.795 | 12.617 | 0.859 | 0 |
| queue/can_launch_empty | 10 | 0.017 | 0.029 | 0.015 | 0.036 | 0.019 | 0.006 | 0 |
| queue/can_launch_9 | 10 | 2.108 | 2.505 | 2.079 | 2.782 | 2.179 | 0.203 | 0 |
| queue/can_launch_full | 10 | 2.118 | 2.192 | 2.061 | 2.199 | 2.122 | 0.049 | 0 |
| queue/launch_rejected | 10 | 2.114 | 2.185 | 2.057 | 2.208 | 2.117 | 0.04 | 0 |

## Sesiones largas (10 × 100 turnos)

- pre_llm_call p50 9.908 ms, p95 12.181 ms, fallos 0
- post_llm_call / stopped p50 4.938 ms, p95 6.081 ms
- Controles de presupuesto: 100, violaciones: 0
- Pendiente último vs primer decil (p50): 1.05×

| Sesión | registradas | entregadas | perdidas | duplicadas | pendientes | ofrecidas |
| --- | --- | --- | --- | --- | --- | --- |
| s1 | 50 | 50 | 0 | 0 | 0 | 0 |
| s2 | 50 | 50 | 0 | 0 | 0 | 0 |

## Barra ≤ 1 s (descomposición medible)

- Persistencia Python (`record`, p95): 3.108 ms
- JS puro (`applyTeamEvent` + `classifyPhase`, p95): 0.02 ms (n=200)
- Despacho de eventos del gateway: **no medible offline**; queda para la máquina viva. El ≤ 1 s de extremo a extremo sigue sin probarse.

## Pocos recursos y errores

- Almacén con 10.000 filas: pre_llm_call p95 8.653 ms (base 9.986 ms, ratio 0.87×), pérdidas 0
- Contención de CPU (2 busy-loops): pre_llm_call p95 67.154 ms, pérdidas 0
- Base corrupta: turno renderizado sin excepción: True, fila intacta: True
- Base bloqueada (espera 331.185 ms y éxito): True
- Recuperación tras kill -9: reapertura + integridad 1.973 ms, integridad ok, registradas 52, perdidas 0

## Batería y puertas (re-ejecutadas en esta corrida)

- Batería: 215 tests, 210 OK + 5 FAIL (los 5 de A0.7, esperados) en Python 3.14.9 y en Python 3.12.3 (verificado aparte en ambos runtimes)
- Puertas: 1a 9/9, 1b 17/17, 2 16/16, 3 23/23, 3b 17/17, 4 31/31, 5 15/15 — todas dentro de la batería: los únicos 5 fallos son de A0.7, así que ninguna puerta falló
- Suite JS (node): exit 0

## Pendiente (máquina viva / decisión de Gustavo)

- TTFT de extremo a extremo y barra ≤ 1 s de extremo a extremo: necesitan modelo + gateway + app de escritorio (Fase 6, canario).
- Piloto pagado (nivel B de coherencia, comparación de modelos): NO ejecutado; ver `docs/fase5-piloto-pendiente.md`.
- Confirmación en Windows: esta medición es Linux; la batería completa en Windows queda para la Fase 6.
