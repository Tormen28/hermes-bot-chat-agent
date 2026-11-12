# Fase 5 — Piloto pagado (pendiente de decisión de Gustavo)

**Estado:** NO autorizado, NO ejecutado, sin gasto. Plan v1.2 §5 punto 5:
"Piloto pagado solo con permiso y presupuesto".

## Qué mediría

1. **Nivel B de coherencia (el modelo cumple las restricciones):** con el
   modelo real, turnos reales y la ficha de proyecto real, ¿el modelo
   respeta las 8 restricciones críticas de la Fase 2 (no borrar `main`,
   todo resultado con ID persistido, ningún secreto fuera del chat, etc.)
   en el contexto reconstruido? Rúbrica congelada antes de medir; la
   puerta es el porcentaje de cumplimiento por restricción.
2. **Opcional — comparación de modelos (30 tareas emparejadas):** las
   mismas 30 tareas, mismo punto de partida, en dos modelos; se compara
   cumplimiento de restricciones, entregas completas y tiempo por tarea.
   No se cambia de modelo automáticamente en el plugin (plan §6).

## Qué necesitaría

- **Permiso explícito de Gustavo** (este documento es la solicitud; nada
  se ejecuta sin su "sí").
- **Presupuesto:** el coste de las llamadas al modelo de las sesiones del
  piloto. Sin presupuesto fijado no hay piloto: hace falta que Gustavo
  fije un tope (o que elija correrlo contra su propio uso normal, sin
  gasto extra medible aparte).
- **Máquina viva:** el piloto corre sobre su instalación real (Windows,
  Hermes v0.23.0, plugin instalado), no en esta VM: necesita el modelo,
  el gateway y la app de escritorio. Eso lo pone en la Fase 6 (canario),
  no aquí.
- **Corpus:** 30 tareas emparejadas y la lista de restricciones, a
  congelar antes de medir (como las rúbricas de las fases 2–5).

## Qué NO es

- No es la medición offline de esta fase: la Fase 5 ya publica latencias,
  pérdidas y coherencia estructural sin gastar nada.
- No es un cambio de modelo ni de comportamiento del plugin.

## Decisión que falta

Gustavo decide: (a) si se hace, (b) con qué presupuesto/tope, (c) cuándo
(en la Fase 6, sobre la instalación viva). Hasta entonces este documento
es solo el plan.
