# Bot Chat Agent para Hermes

[English](README.md)

Hace que la **sesión canónica "Bot Chat"** de un bot (Bot Mode / hermes-bots) se sienta como un asistente
enfocado, sin cambiar ninguna otra sesión, perfil ni el núcleo.

| Capa | Qué hace |
|---|---|
| Estilo (`pre_llm_call`) | Añade un contrato corto de "actúa, no narres": acuse breve, trabajo, resultado corto; máximo una pregunta por turno. |
| Guardia (`pre_tool_call`) | **Bloquea** llamadas riesgosas y le indica al modelo que te pregunte primero. Nunca aprueba nada. |
| Desktop | Una barra discreta "Cargando… · Leyendo archivos" sobre el compositor y el razonamiento colapsado por defecto mientras una Bot Chat está enfocada. Las tarjetas de aprobación no se tocan. |

Solo afecta a sesiones con título **"Bot Chat"** (el que usa Bot Mode de Hermes) y sus hijas por compactación.
Los subagentes y el resto de sesiones se comportan igual que antes.

## Requisitos
Hermes `>= 0.22.5`.

## Instalar
```bash
hermes plugins install Tormen28/hermes-bot-chat-agent   # o copia esta carpeta a ~/.hermes/plugins/bot-chat-agent
hermes plugins enable bot-chat-agent
```
Desktop: **Capacidades → Plugins → Bot Chat Agent** → activar. Reinicia la app si no aparece.

## Desactivar / desinstalar
```bash
hermes plugins disable bot-chat-agent
hermes plugins remove bot-chat-agent
```
Al desactivar la parte Desktop se restaura tu ajuste previo de "colapsar razonamiento".

## Configuración
Ver el bloque YAML en [README.md](README.md#configuration). Para mensajes en español: `language: es`.

## Reglas de la guardia
secretos, borrados, git destructivo, push/merge a ramas protegidas (`main`, `master` por defecto), envíos al
exterior y modelos caros. Cada regla se puede apagar por separado. Es una red de seguridad por patrones, no un
sandbox: mantén activadas las aprobaciones propias de Hermes.

## Divulgaciones
- Lee `<HERMES_HOME>/state.db` **solo lectura** para identificar las sesiones Bot Chat. Nunca escribe.
- Nunca lee `.env`, `auth.json` ni credenciales. Sin red. Sin telemetría.
- Desktop: cambia `reasoning.collapsedByDefault` mientras una Bot Chat está enfocada y lo restaura al salir.

## Limitación conocida
El SDK público de Desktop puede colapsar el razonamiento pero no ocultar las tarjetas de herramientas por sesión.
Para eso hace falta un hook nuevo del SDK (se pedirá a Nous).

## Licencia
MIT. Ver [LICENSE](LICENSE).
