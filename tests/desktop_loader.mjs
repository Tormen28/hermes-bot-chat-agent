// Test-only module loader: stubs the Hermes desktop plugin SDK and
// react/jsx-runtime so desktop/plugin.js can be imported under plain node.
// Never shipped: it only exists for tests/test_fase3_desktop.mjs.
const SDK_SOURCE = `
export const COMPOSER_AREAS = { top: 'top' }
export const host = null
export function atom(initial) {
  let value = initial
  return {
    get: () => value,
    set: (v) => { value = v },
    subscribe: () => () => {}
  }
}
export function useValue(a) { return a.get() }
`
const JSX_SOURCE = `
export function jsx() { return null }
export function jsxs() { return null }
`

export async function resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk' || specifier === 'react/jsx-runtime') {
    return { url: `stub:${specifier}`, shortCircuit: true }
  }
  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  if (url === 'stub:@hermes/plugin-sdk') {
    return { format: 'module', source: SDK_SOURCE, shortCircuit: true }
  }
  if (url === 'stub:react/jsx-runtime') {
    return { format: 'module', source: JSX_SOURCE, shortCircuit: true }
  }
  return nextLoad(url, context)
}
