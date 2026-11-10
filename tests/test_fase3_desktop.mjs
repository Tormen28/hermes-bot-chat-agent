// Fase 3 desktop tests (node:test). The desktop plugin imports the Hermes
// plugin SDK and react/jsx-runtime, which do not exist in this repo: the
// loader below stubs them before the plugin module is imported.
import { register } from 'node:module'
import { test } from 'node:test'
import assert from 'node:assert/strict'

register('./desktop_loader.mjs', import.meta.url)
const plugin = await import('../desktop/plugin.js')
const { labelFor, shortError, classifyPhase, applyTeamEvent, canonicalIdsFrom } = plugin

const EMPTY = {
  teamWait: '', notice: '', activity: '', blocked: false, detail: '', error: '',
  queued: '', working: '', ready: {}, cardBlocked: ''
}

function state(over = {}) {
  return { ...EMPTY, ready: {}, ...over }
}

test('completed adds a ready result, deduplicated by task id', () => {
  let s = state()
  s = applyTeamEvent(s, 'kanban_task_completed', { task_id: 't-1', summary: 'done', session_id: 's1' })
  assert.deepEqual(s.ready, { s1: ['t-1'] })
  s = applyTeamEvent(s, 'kanban_task_completed', { task_id: 't-1', summary: 'done', session_id: 's1' })
  assert.deepEqual(s.ready, { s1: ['t-1'] }, 'el mismo aviso dos veces cuenta una')
  s = applyTeamEvent(s, 'kanban_task_completed', { task_id: 't-2', summary: 'done', session_id: 's1' })
  assert.deepEqual(s.ready, { s1: ['t-1', 't-2'] })
})

test('completed without a session never names one (wow 8)', () => {
  const s = applyTeamEvent(state(), 'kanban_task_completed', { task_id: 't-9', summary: 'done' })
  assert.deepEqual(s.ready, { '': ['t-9'] })
  const phase = classifyPhase('es', { ...s, inBotChat: false, busy: false, focusedSession: '' })
  assert.equal(phase.phase, 'ready')
  assert.match(phase.text, /Bot Chat/)
  assert.doesNotMatch(phase.text, /s1|s2/)
})

test('claimed means queued, spawned means working', () => {
  let s = applyTeamEvent(state(), 'kanban_create', { assignee: 'BSK' })
  assert.equal(s.teamWait, 'BSK')
  s = applyTeamEvent(s, 'kanban_task_claimed', { task_id: 't-1', assignee: 'BSK' })
  assert.equal(s.queued, 'BSK')
  assert.equal(s.teamWait, '')
  s = applyTeamEvent(s, 'on_kanban_worker_spawned', { task_id: 't-1', assignee: 'BSK', worker_pid: 42 })
  assert.equal(s.working, 'BSK')
})

test('blocked card is a blocked phase, not an approval wait', () => {
  const s = applyTeamEvent(state(), 'kanban_task_blocked', { task_id: 't-1', reason: 'needs an API key' })
  assert.match(s.cardBlocked, /t-1/)
  assert.match(s.cardBlocked, /API key/)
  const phase = classifyPhase('en', { ...s, inBotChat: true, busy: false })
  assert.equal(phase.phase, 'card_blocked')
  assert.match(phase.text, /Blocked/)
})

test('ready badge shows when idle, not while a turn is running', () => {
  const s = state({ ready: { s1: ['t-1'] } })
  const idle = classifyPhase('es', { ...s, inBotChat: true, busy: false, focusedSession: 's1' })
  assert.equal(idle.phase, 'ready')
  assert.equal(idle.text, '1 resultado listo')
  const busy = classifyPhase('es', { ...s, inBotChat: true, busy: true, focusedSession: 's1', activity: 'Pensando…' })
  assert.equal(busy.phase, 'working', 'con turno activo el aviso espera a la oferta')
  const two = classifyPhase('es', { ...state({ ready: { s1: ['t-1', 't-2'] } }), inBotChat: true, busy: false, focusedSession: 's1' })
  assert.equal(two.text, '2 resultados listos')
})

test('ready in another session shows a generic badge, not the wrong session', () => {
  const s = state({ ready: { s2: ['t-7'] } })
  const here = classifyPhase('es', { ...s, inBotChat: true, busy: false, focusedSession: 's1' })
  assert.notEqual(here.phase, 'ready', 'el resultado de s2 no se anuncia en s1')
  const elsewhere = classifyPhase('es', { ...s, inBotChat: false, busy: false, focusedSession: '' })
  assert.equal(elsewhere.phase, 'ready')
  assert.match(elsewhere.text, /1 resultado listo en Bot Chat/)
})

test('approval wait still outranks ready', () => {
  const s = state({ ready: { s1: ['t-1'] }, blocked: true, detail: 'needs approval' })
  const phase = classifyPhase('en', { ...s, inBotChat: true, busy: false, focusedSession: 's1' })
  assert.equal(phase.phase, 'waiting_ok')
})

test('queued phase renders when idle', () => {
  const s = state({ queued: 'BSK' })
  const phase = classifyPhase('es', { ...s, inBotChat: true, busy: false })
  assert.equal(phase.phase, 'queued')
  assert.match(phase.text, /En cola/)
  assert.match(phase.text, /BSK/)
})

test('canonicalIdsFrom still finds Bot Chat sessions', () => {
  const ids = canonicalIdsFrom({ profiles: [
    { canonical_session: { id: 's1', title: 'Bot Chat' } },
    { canonical_session: { id: 's2', title: 'Other' } }
  ] })
  assert.deepEqual([...ids], ['s1'])
})

test('labelFor and shortError unchanged', () => {
  assert.equal(labelFor('es', 'kanban_create'), 'Asignando a un compañero')
  assert.equal(shortError('[bot-chat-agent] Blocked: x\nsecond line'), 'Blocked: x')
})

test('status panel suffix is idle-visible and does not alter existing phases without panel', () => {
  const idle = classifyPhase('en', { ...state(), inBotChat: true, busy: false })
  assert.equal(idle.phase, 'idle')
  assert.equal(idle.active, false)
  const panel = classifyPhase('en', {
    ...state(), inBotChat: true, busy: false,
    panel: '1 working · 2 recalled (token auth) · 720 tok'
  })
  assert.equal(panel.active, true)
  assert.equal(panel.phase, 'panel')
  assert.match(panel.text, /720 tok/)
  assert.match(panel.text, /token auth/)
  const working = classifyPhase('en', {
    ...state(), inBotChat: true, busy: true, activity: 'Reading files',
    panel: '2 recalled (token auth) · 720 tok'
  })
  assert.equal(working.phase, 'working')
  assert.match(working.text, /Working/)
  assert.match(working.text, /720 tok/)
  const blocked = classifyPhase('en', {
    ...state(), inBotChat: true, busy: false, blocked: true, detail: 'needs approval',
    panel: '2 recalled'
  })
  assert.equal(blocked.phase, 'waiting_ok')
  assert.doesNotMatch(blocked.text, /recalled/)
})

test('applyTeamEvent records a bot-chat-status panel line', () => {
  const s = applyTeamEvent(state(), 'bot-chat-status', {
    panel: '1 pending · 2 recalled (token auth) · 400 tok'
  })
  assert.match(s.panel, /2 recalled/)
})
