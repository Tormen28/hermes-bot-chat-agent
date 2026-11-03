// Bot Chat Agent - Desktop layer. Uses only the public plugin SDK (no direct page access, no injected styles).
// Scope: only the canonical "Bot Chat" session(s) reported by profiles.list.
import { atom, COMPOSER_AREAS, host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const BOT_CHAT_TITLE = 'Bot Chat'
const REASONING_KEY = 'reasoning.collapsedByDefault'

const I18N = {
  en: { working: 'Working…', thinking: 'Thinking…', blocked: 'Paused for your approval' },
  es: { working: 'Cargando…', thinking: 'Pensando…', blocked: 'En pausa: necesita tu aprobación' }
}
const TOOL_LABELS = {
  en: {
    read_file: 'Reading files', write_file: 'Saving changes', patch: 'Editing a file',
    search_files: 'Searching the code', terminal: 'Running a command', execute_code: 'Running a script',
    web_search: 'Searching the web', web_extract: 'Reading a web page', delegate_task: 'Coordinating a helper',
    session_search: 'Checking past sessions', memory: 'Updating memory'
  },
  es: {
    read_file: 'Leyendo archivos', write_file: 'Guardando cambios', patch: 'Editando un archivo',
    search_files: 'Buscando en el código', terminal: 'Ejecutando un comando', execute_code: 'Ejecutando un script',
    web_search: 'Buscando en la web', web_extract: 'Leyendo una página', delegate_task: 'Coordinando un ayudante',
    session_search: 'Consultando sesiones', memory: 'Actualizando memoria'
  }
}

function pickLang() {
  const nav = typeof navigator !== 'undefined' ? String(navigator.language || '') : ''
  return nav.toLowerCase().startsWith('es') ? 'es' : 'en'
}

export function labelFor(lang, toolName) {
  if (!toolName) return I18N[lang].thinking
  return (TOOL_LABELS[lang] && TOOL_LABELS[lang][toolName]) || `${toolName}`
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

const $bar = atom({ active: false, blocked: false, text: '' })

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
  return jsxs('div', {
    role: 'status', 'aria-live': 'polite', style: BAR_STYLE,
    children: [
      jsx('span', { style: { width: 8, height: 8, borderRadius: 9999, flexShrink: 0,
        background: s.blocked ? '#f59e0b' : '#38bdf8', opacity: s.blocked ? 1 : 0.8 } }),
      jsx('span', { children: s.text })
    ]
  })
}

function setup(ctx) {
  const lang = pickLang()
  const t = I18N[lang]
  const ids = new Set()
  let inBotChat = false
  let busy = false
  let activity = ''
  let blocked = false
  let savedReasoning // undefined = we have not changed the user's setting
  let disposed = false
  let syncSeq = 0

  const settings = host && host.settings
  const state = (host && host.state) || {}

  function render() {
    const active = inBotChat && (busy || blocked)
    $bar.set({ active, blocked, text: blocked ? t.blocked : `${t.working} · ${activity || t.thinking}` })
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
    const focused = String((state.focusedStoredSessionId && state.focusedStoredSessionId.get &&
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
      activity = ''
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
    unsubs.push(busyAtom.subscribe(v => { busy = Boolean(v); if (busy) { activity = ''; blocked = false } render() }))
  }
  for (const a of [state.focusedStoredSessionId, state.routeView]) {
    if (a && a.subscribe) unsubs.push(a.subscribe(() => { sync() }))
  }
  if (typeof ctx.onEvent === 'function') {
    unsubs.push(ctx.onEvent('*', (ev, maybePayload) => {
      if (!inBotChat) return
      const isObj = ev && typeof ev === 'object'
      if (isObj && ev.session_id && ids.size > 0 && !ids.has(String(ev.session_id))) return
      const name = isObj ? ev.type : ev
      const payload = isObj ? (ev.payload || ev) : maybePayload
      const tool = payload && (payload.name || payload.tool_name || payload.function_name)
      const resStr = payload && typeof payload.result === 'string' ? payload.result : ''
      const msg = String((payload && (payload.message || payload.error || payload.result_text || payload.summary || resStr)) || '')
      if (msg.includes('[bot-chat-agent]')) { blocked = true; render(); return }
      if (tool || String(name || '').includes('tool')) { blocked = false; activity = labelFor(lang, tool); render() }
    }))
  }

  sync()

  ctx.onDispose(() => {
    disposed = true
    for (const u of unsubs) if (typeof u === 'function') u()
    applyReasoning(false)
    $bar.set({ active: false, blocked: false, text: '' })
  })
}

export default {
  id: 'bot-chat-agent',
  name: 'Bot Chat Agent',
  description: 'Bot Chat only: a quiet "Working…" status bar and reasoning collapsed by default.',
  defaultEnabled: true,
  register(ctx) { setup(ctx) }
}
