# Fase 4 — Revisión manual del paquete (v1.2 §4)

Lista de verificación para la revisión manual. La puerta automática está
en `tests/test_fase4_gate.py` (31 tests); esto es lo que **no** puede
verificar una máquina offline y tiene que revisar una persona (Gustavo)
antes de fusionar o publicar. Nada de esta fase se ha instalado en la
instalación viva ni se ha enviado a ningún sitio.

## Alcance de la fase

Un solo paquete, modo individual en instalaciones nuevas y equipo opcional;
`team.gods` → `team.assignees`; piso de versión del core verificado;
convivencia con Panteón, JEV, Tormen y BSK contra la configuración real
disponible en el repo. Sin cambios en el core, sin red, sin coste.

## 1. Paquete

- [ ] `plugin.yaml` declara `version: 0.3.0-dev` (sigue siendo un corte
      privado de prueba; lo "público" de esta fase es **un paquete para los
      dos modos**, no la publicación del catálogo — eso es Fase 7 y
      requiere autorización separada).
- [ ] `requires_hermes: ">=0.22.5"` — verificado, no elevado (ver F1).
- [ ] `provides_hooks` / `hooks` declaran los 9 hooks que usa el plugin,
      incluidos `post_llm_call`, `agent_loop_stopped` y
      `kanban_task_blocked`. No se añade ninguna herramienta nueva
      (`provides_tools: []`).
- [ ] Un solo paquete: no hay un segundo plugin ni una variante
      "individual" separada. El modo lo decide la configuración.
- [ ] Archivos nuevos de la fase: `team_config.py` (resolución de la
      configuración de equipo, pura, sin I/O), `tests/test_fase4_gate.py`,
      este documento. Ningún módulo se separó sin justificarlo (la
      resolución es testeable por separado; el resto son ediciones en
      `__init__.py` y `team_flow.py`).

## 2. Fixtures y pruebas

- [ ] La puerta (`tests/test_fase4_gate.py`, 31 tests) corre sin red y
      sin tocar la instalación viva: todos los estados son temporales
      (`tempfile.mkdtemp`), el checkout del core solo se lee con
      `git show` (dos tests lo saltan si no está).
- [ ] La batería completa sigue en verde salvo los 5 fallos esperados
      de A0.7 (huecos de diseño de la guarda, conocidos desde Fase 0).
- [ ] Las puertas 1a (9/9), 1b (17/17), 2 (16/16), 3 (23/23) y 3b
      (17/17) + node 10/10 siguen pasando (verificar en la corrida
      final antes del commit).
- [ ] Los tests antiguos siguen usando `team.gods` en sus configs a
      propósito: el camino legacy tiene que seguir funcionando, y esos
      tests lo fijan.

## 3. Licencias

- [ ] Licencia MIT, sin cambios (`LICENSE` intacto).
- [ ] Sin dependencias nuevas, sin código vendorizado, sin
      redistribución de nada de terceros.
- [ ] `author: "Tormen28"`, `homepage` al repo del usuario, sin cambios.

## 4. Privacidad

- [ ] El plugin sigue sin leer `.env`, `auth.json` ni ningún almacén de
      credenciales; `state.db` y `kanban.db` se abren solo en lectura.
- [ ] Los nombres Panteón, JEV, Tormen y BSK no están hardcodeados en
      el código: son valores de configuración (`team.assignees`) y solo
      aparecen en los tests como datos de prueba. El nombre con acento
      ("Panteón") se conserva tal cual y casa sin importar mayúsculas.
- [ ] Cada entrega sigue yendo solo a la sesión que creó la tarjeta
      (wow 8); sin correlación va a la bandeja sin asignar, nunca a
      otro chat. Una configuración contradictoria pierde la correlación
      de las tarjetas creadas mientras está rota (ver F4), pero no
      cruza sesiones.
- [ ] El almacén de entregas (`deliveries.db`) sigue en disco local,
      nunca en carpeta sincronizada (chequeo de arranque, solo aviso).

## Hallazgos (para decidir en la revisión)

- **F1 — El piso del core NO se elevó.** El plan pedía elevarlo "a la
  versión que tenga los hooks que se usan". Verificado contra los tags
  del core: `v0.22.5` ya tiene `post_llm_call`, `agent_loop_stopped` y
  `kanban_task_blocked` en `VALID_HOOKS` (y el vivo `2bd378ed`,
  v0.23.0, también). `requires_hermes` queda en `>=0.22.5`; lo que
  aporta la fase es la verificación (test que lo fija contra el tag)
  y la degradación declarada en el README. Si se quiere un piso más
  alto por otro motivo, es una decisión aparte.
- **F2 — Cambio de comportamiento: `kanban_create` con la lista vacía.**
  Antes, con `team.gods` vacío, la comprobación de permiso se saltaba
  entera y el modelo podía crear una tarjeta para cualquier perfil
  (nadie la trabajaba ni la entregaba). Ahora se bloquea con
  `team_assignee` y un mensaje que nombra `team.assignees`. Una tarjeta
  sin asignar sigue permitida (trabajo propio en el tablero, no
  delegación). Está fijado en la puerta; si se prefiere el
  comportamiento anterior, hay que revertir `team_flow.evaluate`.
- **F3 — `team_note` tragaba el aviso de entrega en modo individual.**
  El retorno temprano de single-operator descartaba `delivery_warning`:
  un fallo del almacén o un error de configuración era invisible en
  la nota. Arreglado: el aviso se añade a la línea de single-operator.
- **F4 — Configuración contradictoria: correlación perdida.** Mientras
  `team.assignees` y `team.gods` difieren, `remember_create` no registra
  la tarjeta (no hay equipo resoluble), así que una entrega de esa
  tarjeta cae en la bandeja sin asignar. Nunca en una sesión
  equivocada, pero el usuario tiene que arreglar una línea; el mensaje
  de bloqueo dice cómo. Aceptado como degradación declarada.
- **F5 — Cosmético: la etiqueta del aviso en `team_note`.** En el
  camino con equipo el aviso sale como "Almacén de entregas no
  disponible", que para un error de configuración es engañoso (el
  almacén está bien; lo que está mal es la configuración). El mensaje
  en sí es correcto. Dejar para la revisión de textos de Fase 7.
- **F6 — La instalación viva no se puede probar desde aquí.** La
  convivencia se verificó contra la configuración real disponible en
  el repo (los cuatro nombres como `team.assignees`, tablero y
  sesiones de prueba). Falta verificar a mano en la máquina viva: el
  archivo de configuración real del plugin, que los perfiles
  Panteón/JEV/Tormen/BSK existan como perfiles Hermes, y que el
  dispatcher del gateway los despache.
- **F7 — Convivencia con otros plugins, sin probar.** En este repo no
  hay código de ningún otro plugin; si en la instalación viva Panteón
  (o cualquier otro) es un plugin aparte y no un perfil, su
  convivencia con Bot Chat no está probada aquí. Revisar a mano qué
  plugins están instalados en vivo.
- **F8 — `desktop/plugin.js` no necesitó cambios.** Trabaja con el
  `assignee` que llega en los eventos, nunca con `gods`; la
  renombración no le afecta.
- **F9 — B1/B5 no hacen falta para esta fase.** El camino activo en el
  vivo sigue siendo la oferta en el turno (el core vivo no tiene B1);
  nada de Fase 4 depende de la reparación del core.

## Pasos manuales sugeridos (en una copia, nunca en la instalación viva)

1. Instalar desde un checkout de esta rama (`fase4-version-unificada`).
2. Sin clave `team`: comprobar que trabaja solo y que un
   `kanban_create` con asignatario se bloquea.
3. Con `team: {gods: [BSK]}` (config antigua, sin `enabled`):
   comprobar que el equipo sigue activo.
4. Con `team: {assignees: [Panteón, JEV, Tormen, BSK]}`: asignar una
   tarjeta a cada uno y comprobar que cada resultado llega a su chat.
5. Con las dos claves y valores distintos: comprobar el bloqueo con
   la explicación y que la guarda sigue bloqueando secretos.
6. Desinstalar la copia y comprobar que no quedan restos fuera de lo
   documentado.
