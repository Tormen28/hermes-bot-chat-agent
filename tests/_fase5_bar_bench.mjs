// Fase 5 bar bench (node): time the pure JS path from a kanban completion
// event to the ready text in the status bar — applyTeamEvent +
// classifyPhase. This is the only part of the "bar ≤ 1 s" budget that is
// measurable offline (rubric docs/fase5-rubric.md §1): the gateway event
// dispatch in between is not. Prints one JSON line.
import { register } from 'node:module'

register('./desktop_loader.mjs', import.meta.url)
const plugin = await import('../desktop/plugin.js')
const { applyTeamEvent, classifyPhase } = plugin

const reps = Number(process.argv[2] || 200)
const EMPTY = {
  teamWait: '', notice: '', activity: '', blocked: false, detail: '', error: '',
  queued: '', working: '', ready: {}, cardBlocked: ''
}
function pct(samples, p) {
  const ordered = [...samples].sort((a, b) => a - b)
  const rank = (p / 100) * (ordered.length - 1)
  const lo = Math.floor(rank)
  const hi = Math.ceil(rank)
  return lo === hi ? ordered[lo] : ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
}

const samples = []
let failures = 0
for (let i = 0; i < reps; i++) {
  const t0 = performance.now()
  try {
    let s = { ...EMPTY, ready: {} }
    s = applyTeamEvent(s, 'kanban_task_completed',
      { task_id: `bar-${i}`, summary: 'done', session_id: 's1' })
    const phase = classifyPhase('es', {
      ...s, inBotChat: true, busy: false, focusedSession: 's1'
    })
    if (phase.phase !== 'ready' || !phase.text.includes('listo')) {
      throw new Error(`unexpected phase: ${JSON.stringify(phase)}`)
    }
  } catch (_) {
    failures++
  }
  samples.push(performance.now() - t0)
}
samples.sort((a, b) => a - b)
const mean = samples.reduce((a, b) => a + b, 0) / samples.length
const stdev = Math.sqrt(
  samples.reduce((a, b) => a + (b - mean) ** 2, 0) / samples.length)
console.log(JSON.stringify({
  route: 'bar/js_pure',
  n: samples.length,
  failures,
  p50_ms: +pct(samples, 50).toFixed(3),
  p95_ms: +pct(samples, 95).toFixed(3),
  min_ms: +samples[0].toFixed(3),
  max_ms: +samples[samples.length - 1].toFixed(3),
  mean_ms: +mean.toFixed(3),
  stdev_ms: +stdev.toFixed(3)
}))
