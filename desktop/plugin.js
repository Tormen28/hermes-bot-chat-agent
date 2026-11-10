// Bot Chat Agent - Desktop layer. Uses only the public plugin SDK (no direct page access, no injected styles).
// Scope: only the canonical "Bot Chat" session(s) reported by profiles.list.
// Fase 3: the bar also tracks kanban lifecycle events (claimed/spawned/
// completed/blocked) and shows "N results ready" with no turn active.
// Limit (Q5): the SDK cannot read the delivery outbox, so the badge only
// covers completions observed while the desktop app was running; it
// resets on reload and the next turn in the session re-announces them.
import { atom, COMPOSER_AREAS, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const BOT_CHAT_TITLE = 'Bot Chat'
const REASONING_KEY = 'reasoning.collapsedByDefault'

const I18N = {
  en: {
    working: 'Working…',
    thinking: 'Thinking…',
    blocked: 'Waiting for your OK',
    error: 'Error',
    waitingTeam: 'Waiting on',
    done: 'Done ·',
    queued: 'Queued',
    cardBlocked: 'Blocked',
    readyOne: '1 result ready',
    readyMany: '{n} results ready',
    readyElsewhereOne: '1 result ready in Bot Chat',
    readyElsewhereMany: '{n} results ready in Bot Chat'
  },
  es: {
    working: 'Cargando…',
    thinking: 'Pensando…',
    blocked: 'Esperando tu OK',
    error: 'Error',
    waitingTeam: 'Esperando a',
    done: 'Listo ·',
    queued: 'En cola',
    cardBlocked: 'Bloqueado',
    readyOne: '1 resultado listo',
    readyMany: '{n} resultados listos',
    readyElsewhereOne: '1 resultado listo en Bot Chat',
    readyElsewhereMany: '{n} resultados listos en Bot Chat'
  }
}
const TOOL_LABELS = {
  en: {
    read_file: 'Reading files', write_file: 'Saving changes', patch: 'Editing a file',
    search_files: 'Searching the code', terminal: 'Running a command', execute_code: 'Running a script',
    web_search: 'Searching the web', web_extract: 'Reading a web page',     delegate_task: 'Coordinating a helper',
    kanban_create: 'Assigning a teammate', kanban_list: 'Checking the board',
    kanban_show: 'Reading a card', kanban_complete: 'Closing a card',
    session_search: 'Checking past sessions', memory: 'Updating memory', clarify: 'Waiting for your OK'
  },
  es: {
    read_file: 'Leyendo archivos', write_file: 'Guardando cambios', patch: 'Editando un archivo',
    search_files: 'Buscando en el código', terminal: 'Ejecutando un comando', execute_code: 'Ejecutando un script',
    web_search: 'Buscando en la web', web_extract: 'Leyendo una página',     delegate_task: 'Coordinando un ayudante',
    kanban_create: 'Asignando a un compañero', kanban_list: 'Revisando el tablero',
    kanban_show: 'Leyendo una tarjeta', kanban_complete: 'Cerrando una tarjeta',
    session_search: 'Consultando sesiones', memory: 'Actualizando memoria', clarify: 'Esperando tu OK'
  }
}

function pickLang() {
  const nav = typeof navigator !== 'undefined' ? String(navigator.language || '') : ''
  return nav.toLowerCase().startsWith('es') ? 'es' : 'en'
}

export function labelFor(lang, toolName) {
  const l = I18N[lang] ? lang : 'en'
  if (!toolName) return I18N[l].thinking
  return (TOOL_LABELS[l] && TOOL_LABELS[l][toolName]) || `${toolName}`
}

export function shortError(raw, maxLen = 68) {
  const first = String(raw || '').replace(/\[bot-chat-agent\]\s*/i, '').split(/\r?\n/)[0].trim()
  if (!first) return ''
  return first.length > maxLen ? `${first.slice(0, maxLen - 1).trimEnd()}…` : first
}

export function readyCount(ready, sessionId) {
  const map = ready && typeof ready === 'object' ? ready : {}
  if (sessionId) {
    const ids = Array.isArray(map[sessionId]) ? map[sessionId] : []
    return ids.length
  }
  return Object.values(map).reduce((n, ids) => n + (Array.isArray(ids) ? ids.length : 0), 0)
}

export function classifyPhase(lang, state) {
  const l = I18N[lang] ? lang : 'en'
  const t = I18N[l]
  const s = state || {}
  const panel = String(s.panel || '').trim()
  const focused = String(s.focusedSession || '')
  const total = readyCount(s.ready)
  const mine = readyCount(s.ready, focused)
  // Fase 3: a ready result is news even outside a Bot Chat session —
  // the badge names Bot Chat, never a session it cannot attribute
  // (wow 8). Counts only cover completions observed while the desktop
  // app was running; the next turn in the session re-announces them.
  if (!s.inBotChat) {
    if (total > 0) {
      return {
        active: true,
        phase: 'ready',
        blocked: false,
        text: total === 1 ? t.readyElsewhereOne : t.readyElsewhereMany.replace('{n}', String(total))
      }
    }
    return { active: false, phase: 'idle', blocked: false, text: '' }
  }
  if (s.blocked) {
    const detail = shortError(s.detail, 56)
    return {
      active: true,
      phase: 'waiting_ok',
      blocked: true,
      text: detail ? `${t.blocked} · ${detail}` : t.blocked
    }
  }
  if (s.error) {
    const detail = shortError(s.error, 60)
    return {
      active: true,
      phase: 'error',
      blocked: false,
      text: detail ? `${t.error}: ${detail}` : t.error
    }
  }
  // The badge shows when idle: while a turn runs, the turn's offer
  // announces the result instead (never injected mid-turn).
  if (!s.busy && mine > 0) {
    return {
      active: true,
      phase: 'ready',
      blocked: false,
      text: mine === 1 ? t.readyOne : t.readyMany.replace('{n}', String(mine))
    }
  }
  if (!s.busy && s.cardBlocked) {
    const detail = shortError(s.cardBlocked, 60)
    return {
      active: true,
      phase: 'card_blocked',
      blocked: false,
      text: detail ? `${t.cardBlocked}: ${detail}` : t.cardBlocked
    }
  }
  if (s.teamWait) {
    return {
      active: true,
      phase: 'waiting_team',
      blocked: false,
      text: `${t.waitingTeam} ${s.teamWait}`
    }
  }
  if (!s.busy && s.queued) {
    return {
      active: true,
      phase: 'queued',
      blocked: false,
      text: `${t.queued} · ${s.queued}`
    }
  }
  if (s.busy) {
    const text = `${t.working} · ${s.activity || t.thinking}`
    return {
      active: true,
      phase: 'working',
      blocked: false,
      text: panel ? `${text} · ${panel}` : text
    }
  }
  if (s.working) {
    const text = `${t.working} · ${s.working}`
    return {
      active: true,
      phase: 'working',
      blocked: false,
      text: panel ? `${text} · ${panel}` : text
    }
  }
  if (s.notice && !s.busy) {
    const detail = shortError(s.notice, 60)
    return {
      active: true,
      phase: 'done',
      blocked: false,
      text: detail ? `${t.done} ${detail}` : t.done
    }
  }
  if (panel && s.inBotChat) {
    return { active: true, phase: 'panel', blocked: false, text: panel }
  }
  return { active: false, phase: 'idle', blocked: false, text: '' }
}

export function applyTeamEvent(state, name, payload) {
  const base = Object.assign(
    { teamWait: '', notice: '', activity: '', blocked: false, detail: '', error: '',
      queued: '', working: '', ready: {}, cardBlocked: '', panel: '' },
    state || {})
  const body = payload && typeof payload === 'object' ? payload : {}
  const bag = body.args && typeof body.args === 'object'
    ? body.args
    : (body.arguments && typeof body.arguments === 'object' ? body.arguments : body)
  const tool = String(body.name || body.tool_name || body.function_name || '')
  const lname = String(name || '').toLowerCase()
  const taskId = String(body.task_id || body.taskId || body.id || '')
  const assignee = String(body.assignee || bag.assignee || '').trim()
  const sessionId = String(body.session_id || body.sessionId || '')
  if (lname.includes('kanban_task_claimed') || lname.includes('task_claimed') || lname.includes('task.claimed')) {
    return Object.assign({}, base, { queued: assignee || taskId, teamWait: '', notice: '' })
  }
  if (lname.includes('worker_spawned') || lname.includes('worker.spawned')) {
    return Object.assign({}, base, { working: assignee || taskId, queued: '' })
  }
  if (lname.includes('kanban_task_completed') || lname.includes('task.completed') || lname.includes('task_completed')) {
    // Ready is a per-session set, deduplicated by task id. A completion
    // without a session goes to the unattributed bucket (''): the badge
    // then names Bot Chat, never a session it cannot prove (wow 8).
    const key = taskId || String(body.summary || body.result || body.message || 'done')
    const next = {}
    for (const [sid, ids] of Object.entries(base.ready || {})) next[sid] = Array.isArray(ids) ? [...ids] : []
    const bucket = next[sessionId] || []
    if (!bucket.includes(key)) bucket.push(key)
    next[sessionId] = bucket
    const clear = (v) => (assignee && v === assignee) || (taskId && v === taskId) ? '' : v
    return Object.assign({}, base, {
      ready: next, teamWait: '', notice: '', blocked: false, error: '', detail: '',
      queued: clear(base.queued), working: clear(base.working)
    })
  }
  if (lname.includes('kanban_task_blocked') || lname.includes('task_blocked') || lname.includes('task.blocked')) {
    const reason = String(body.reason || body.message || body.summary || '')
    const text = taskId ? `${taskId}: ${reason}`.trim() : reason
    const clear = (v) => (assignee && v === assignee) || (taskId && v === taskId) ? '' : v
    return Object.assign({}, base, {
      cardBlocked: text, teamWait: '', notice: '',
      queued: clear(base.queued), working: clear(base.working)
    })
  }
  if (lname.includes('bot-chat-status') || lname.includes('bot_chat_panel')) {
    return Object.assign({}, base, { panel: String(body.panel || body.text || '') })
  }
  if (tool === 'kanban_create' || lname === 'kanban_create') {
    const who = assignee
    if (!who) return Object.assign({}, base)
    return Object.assign({}, base, { teamWait: who, notice: '', blocked: false, error: '' })
  }
  return null
}

export function canonicalIdsFrom(res, title = BOT_CHAT_TITLE) {
  const ids = new Set()
  const rows = Array.isArray(res && res.profiles) ? res.profiles : []
  for (const row of rows) {
    const cs = row && row.canonical_session
    if (!cs) continue
    if (String(cs.title || '').trim() === title || String(cs.root_title || '').trim() === title) {
      if (cs.id) ids.add(String(cs.id))
      if (cs.resolved_id) ids.add(String(cs.resolved_id))
    }
  }
  return ids
}

const $bar = atom({ active: false, phase: 'idle', blocked: false, text: '' })

const DOT_COLOR = {
  working: '#38bdf8',
  waiting_ok: '#f59e0b',
  waiting_team: '#f59e0b',
  queued: '#38bdf8',
  ready: '#34d399',
  card_blocked: '#f59e0b',
  done: '#34d399',
  error: '#f87171'
}

const BAR_STYLE = {
  display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 12px', margin: '0 8px 6px',
  borderRadius: '8px', fontSize: '12px', lineHeight: 1.35, whiteSpace: 'nowrap', overflow: 'hidden',
  textOverflow: 'ellipsis', color: 'var(--ui-text-secondary, #cbd5e1)',
  background: 'var(--ui-surface-secondary, rgba(30,41,59,.65))',
  border: '1px solid var(--ui-stroke-tertiary, rgba(148,163,184,.18))'
}

function StatusBar() {
  const s = useValue($bar)
  if (!s.active) return null
  const dot = DOT_COLOR[s.phase] || DOT_COLOR.working
  return jsxs('div', {
    role: 'status', 'aria-live': 'polite', style: BAR_STYLE,
    children: [
      jsx('span', { style: { width: 8, height: 8, borderRadius: 9999, flexShrink: 0,
        background: dot, opacity: s.phase === 'working' ? 0.85 : 1 } }),
      jsx('span', { children: s.text })
    ]
  })
}

function setup(ctx) {
  const lang = pickLang()
  const ids = new Set()
  let inBotChat = false
  let busy = false
  let activity = ''
  let blocked = false
  let detail = ''
  let error = ''
  let teamWait = ''
  let notice = ''
  let savedReasoning // undefined = we have not changed the user's setting
  let disposed = false
  let syncSeq = 0

  const settings = host && host.settings
  const state = (host && host.state) || {}
  // Fase 3: background-task state, tracked across sessions so the
  // "N results ready" badge works with no turn active. `ready` maps
  // session id -> task ids ('' = unattributed: the badge then names
  // Bot Chat, never a session it cannot prove).
  let focused = ''
  let queued = ''
  let working = ''
  let ready = {}
  let cardBlocked = ''

  function render() {
    $bar.set(classifyPhase(lang, {
      inBotChat, busy, activity, blocked, detail, error, teamWait, notice,
      queued, working, ready, cardBlocked, focusedSession: focused
    }))
  }

  async function applyReasoning(on) {
    if (!settings || typeof settings.set !== 'function') return
    try {
      if (on && savedReasoning === undefined) {
        const prev = typeof settings.get === 'function' ? await settings.get(REASONING_KEY) : false
        savedReasoning = Boolean(prev)
        await settings.set(REASONING_KEY, true)
      } else if (!on && savedReasoning !== undefined) {
        const restore = savedReasoning
        savedReasoning = undefined
        await settings.set(REASONING_KEY, restore)
      }
    } catch (_) { /* never break the UI */ }
  }

  async function refresh() {
    if (!host || typeof host.request !== 'function') return
    try {
      const next = canonicalIdsFrom(await host.request('profiles.list', {}))
      ids.clear()
      next.forEach(id => ids.add(id))
    } catch (_) { /* daemon restarting */ }
  }

  async function sync() {
    if (disposed) return
    const seq = ++syncSeq
    const route = (state.routeView && state.routeView.get && state.routeView.get()) || 'chat'
    focused = String((state.focusedStoredSessionId && state.focusedStoredSessionId.get &&
      state.focusedStoredSessionId.get()) || '').trim()
    let hit = route === 'chat' && focused !== '' && ids.has(focused)
    if (!hit && route === 'chat' && focused) {
      await refresh()
      if (disposed || seq !== syncSeq) return
      hit = ids.has(focused)
    }
    if (disposed || seq !== syncSeq) return
    if (hit !== inBotChat) {
      inBotChat = hit
      blocked = false
      detail = ''
      error = ''
      activity = ''
      teamWait = ''
      notice = ''
      await applyReasoning(hit)
      if (disposed || seq !== syncSeq) return
    }
    render()
  }

  ctx.register({ id: 'bot-chat-agent-status', area: COMPOSER_AREAS.top, order: 5,
    render: () => jsx(StatusBar, {}) })

  const unsubs = []
  const busyAtom = state.busy || state.awaitingResponse
  if (busyAtom && typeof busyAtom.get === 'function') {
    busy = Boolean(busyAtom.get())
  }
  if (busyAtom && busyAtom.subscribe) {
    unsubs.push(busyAtom.subscribe(v => {
      const wasBusy = busy
      busy = Boolean(v)
      if (busy && !wasBusy && focused) {
        // A turn just started in the focused session: it will announce
        // the ready results through the offer, so the badge hands over.
        const next = { ...ready }
        delete next[focused]
        ready = next
      }
      if (busy) { activity = ''; blocked = false; detail = ''; error = ''; notice = '' }
      render()
    }))
  }
  for (const a of [state.focusedStoredSessionId, state.routeView]) {
    if (a && a.subscribe) unsubs.push(a.subscribe(() => { sync() }))
  }
  if (typeof ctx.onEvent === 'function') {
    unsubs.push(ctx.onEvent('*', (ev, maybePayload) => {
      const isObj = ev && typeof ev === 'object'
      const name = String((isObj ? ev.type : ev) || '').toLowerCase()
      const payload = isObj ? (ev.payload || ev) : maybePayload
      // Fase 3: kanban lifecycle events are tracked even outside a Bot
      // Chat session — that is how the "N results ready" badge works
      // with no turn active. Attribution uses the event session when
      // it names one, else the unattributed bucket (never a guess).
      if (name.includes('kanban_task_') || name.includes('worker_spawned') ||
          name.includes('task_completed') || name.includes('task.completed') ||
          name.includes('task_blocked') || name.includes('task.blocked')) {
        const merged = (payload && typeof payload === 'object') ? { ...payload } : {}
        if (!merged.session_id && !merged.sessionId && isObj && ev.session_id) {
          merged.session_id = String(ev.session_id)
        }
        const next = applyTeamEvent(
          { teamWait, notice, queued, working, ready, cardBlocked }, name, merged)
        if (next) {
          teamWait = next.teamWait || ''
          notice = next.notice || ''
          queued = next.queued || ''
          working = next.working || ''
          ready = next.ready || {}
          cardBlocked = next.cardBlocked || ''
          blocked = false
          detail = ''
          error = ''
          render()
        }
        return
      }
      if (!inBotChat) return
      if (isObj && ev.session_id && ids.size > 0 && !ids.has(String(ev.session_id))) return
      const tool = payload && (payload.name || payload.tool_name || payload.function_name)
      const resStr = payload && typeof payload.result === 'string' ? payload.result : ''
      const errRaw = payload && (payload.error || (payload.status === 'error' ? payload.message : ''))
      const msg = String((payload && (payload.message || payload.error || payload.result_text || payload.summary || resStr)) || '')
      const team = applyTeamEvent({ teamWait, notice, queued, working, ready, cardBlocked }, name, payload || {})
      if (team && !(msg.includes('[bot-chat-agent]') || name.includes('approval') || name.includes('permission') || tool === 'clarify')) {
        teamWait = team.teamWait || ''
        notice = team.notice || ''
        queued = team.queued || ''
        working = team.working || ''
        ready = team.ready || {}
        cardBlocked = team.cardBlocked || ''
        if (tool) activity = labelFor(lang, tool)
        blocked = false
        detail = ''
        error = ''
        render()
        return
      }
      if (msg.includes('[bot-chat-agent]') || name.includes('approval') || name.includes('permission') || tool === 'clarify') {
        blocked = true
        error = ''
        detail = msg.includes('[bot-chat-agent]') ? msg.replace(/^.*?(?:Blocked:|Bloqueado:)\s*/i, '').replace(/\s*(?:Ask the user|Pide aprobación).*$/i, '') : ''
        render()
        return
      }
      if (errRaw || name.includes('error') || name.includes('failed')) {
        blocked = false
        error = String(errRaw || msg || 'Turn failed')
        render()
        return
      }
      if (tool || name.includes('tool')) {
        blocked = false
        detail = ''
        error = ''
        activity = labelFor(lang, tool)
        render()
      }
    }))
  }

  sync()

  ctx.onDispose(() => {
    disposed = true
    for (const u of unsubs) if (typeof u === 'function') u()
    applyReasoning(false)
    $bar.set({ active: false, phase: 'idle', blocked: false, text: '' })
  })
}

export default {
  id: 'bot-chat-agent',
  name: 'Bot Chat Agent',
  description: 'Bot Chat only: Working, Queued, Waiting for your OK, Waiting on a teammate, Ready (results waiting), Blocked card, Done, or Error. Reasoning collapsed by default.',
  defaultEnabled: true,
  register(ctx) { setup(ctx) }
}
