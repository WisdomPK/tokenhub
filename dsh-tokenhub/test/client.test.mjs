// dsh-tokenhub — Client-half render test.
// Mounts the widget with real React inside jsdom and drives it end to end:
//   pnpm install && node test/client.test.mjs

import { readFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'
import { createRequire } from 'node:module'

const results = []
const check = (label, cond, extra) => results.push((cond ? 'PASS  ' : 'FAIL  ') + label + (extra ? '   [' + extra + ']' : ''))

const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { url: 'http://127.0.0.1:3000/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
// The widget picks its dictionary from navigator.language at factory time; pin
// it to Chinese so this covers the zh strings the author reads.
Object.defineProperty(dom.window.navigator, 'language', { value: 'zh-CN', configurable: true })
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.IS_REACT_ACT_ENVIRONMENT = true

let spec = null
dom.window.__ModuleLoader__ = { load: (s) => { spec = s } }
await import('../lib/client.js')
check('bundle registers with the module loader', spec !== null && spec.id === 'dsh-tokenhub', spec && spec.id)

const require = createRequire(import.meta.url)
const React = require('react')
const { createRoot } = require('react-dom/client')
const act = React.act

const materialize = (loaded) =>
  loaded.factory((name) => {
    if (name === 'react') return React
    throw new Error('unexpected external request: ' + name)
  })

const mod = materialize(spec)
check('client module exports apply/inject', typeof mod.apply === 'function' && mod.inject[0] === 'slots')
check(
  'stylesheet injected on materialization',
  document.querySelectorAll('style[data-plugin-css="dsh-tokenhub/css"]').length === 1,
  'styles=' + document.querySelectorAll('style').length,
)

let Widget = null
let registered = null
mod.apply({
  get: (k) =>
    k === 'slots'
      ? {
          inject: (slotName, fn) => fn(),
          register: (s, component) => {
            registered = s
            Widget = component
            return () => {}
          },
        }
      : undefined,
})
check('registered into shell.overlay', registered && registered.name === 'shell.overlay' && registered.id === 'dsh-tokenhub', JSON.stringify(registered))

const NOW = Date.now()
const win = (percent, hours) => ({ status: 'ok', percent: percent, resetsAt: new Date(NOW + hours * 3600e3).toISOString() })
const poolRow = (name, active, windows, extra) =>
  Object.assign({ id: 'pool:' + name, name: name, masked: 'sk-' + name.toUpperCase() + '\u2026zzzz', active: active, inPool: true, error: null, detail: null, windows: windows }, extra || {})

// The account in force mixes the three bands on purpose: rolling calm, weekly
// caution, monthly alarm.
let SNAPSHOT = {
  updatedAt: NOW,
  error: null,
  thresholds: { warn: 60, error: 85 },
  scale: 2,
  entries: [
    poolRow('go1', false, { rolling: win(10, 1), weekly: win(70, 120), monthly: win(90, 480) }),
    poolRow('go2', true, { rolling: win(5, 1), weekly: win(70, 120), monthly: win(92, 480) }),
    poolRow('go3', false, null, { error: 'unauthorized', detail: 'HTTP 401' }),
  ],
}
const calls = []
let switchAuthFailed = false
let switchWarnings = null
let switchFailure = null
let holdSwitch = null
let accountWarnings = null
let accountFailure = false
globalThis.fetch = async (url, init) => {
  const target = String(url)
  const reply = (payload) => ({ ok: true, status: 200, async json() { return payload } })
  if (target.indexOf('/snapshot') !== -1) return reply(SNAPSHOT)
  const body = init && init.body ? JSON.parse(init.body) : null
  calls.push({ url: target, body })
  if (target === '/plugins/dsh-tokenhub/switch') {
    if (holdSwitch) {
      const gate = holdSwitch
      holdSwitch = null
      await gate
    }
    if (switchFailure) {
      return reply({ ok: false, error: switchFailure.error, warnings: switchFailure.warnings || [] })
    }
    const warnings = (switchAuthFailed
      ? [{ code: 'opencode-auth', message: 'opencode \u540c\u6b65\u5931\u8d25\uff1a\u6743\u9650\u4e0d\u8db3' }]
      : []).concat(switchWarnings || [])
    return reply({
      ok: true,
      name: body.name,
      message:
        '\u5df2\u5207\u6362\u5230\u300c' + body.name + '\u300d\uff0c\u4e0b\u4e00\u4e2a\u8bf7\u6c42\u5373\u751f\u6548' +
        warnings.map((w) => '\uff1b' + w.message).join(''),
      warnings,
      opencodeAuth: { enabled: true, ok: !switchAuthFailed, message: switchAuthFailed ? '\u6743\u9650\u4e0d\u8db3' : '\u5df2\u540c\u6b65' },
    })
  }
  if (target === '/plugins/dsh-tokenhub/account') {
    if (accountFailure) {
      return reply({ ok: false, error: '\u90e8\u5206\u5b8c\u6210\uff1a\u65e7\u540d\u672a\u5220\u9664', warnings: accountWarnings || [] })
    }
    return reply({ ok: true, name: body.name, message: '\u5df2\u5904\u7406\u300c' + body.name + '\u300d', warnings: accountWarnings || [] })
  }
  return reply({ ok: true, name: body.name, message: '\u5df2\u5904\u7406\u300c' + body.name + '\u300d' })
}

const mount = async () => {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  await act(async () => { root.render(React.createElement(Widget)) })
  await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
  return host
}
const q = (host, sel) => host.querySelector(sel)
const all = (host, sel) => Array.from(host.querySelectorAll(sel))
const byText = (host, sel, label) => all(host, sel).filter((b) => (b.textContent || '').trim() === label)[0]
const click = async (el) => { await act(async () => { el.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await new Promise((r) => setTimeout(r, 20)) }) }
const txt = (host) => host.textContent || ''

try {
  // ================= pool mode =================
  const host = await mount()
  const text = () => txt(host)
  const fab = () => q(host, '.th-gauge')

  check('gauge rendered', fab() !== null)
  check(
    'the widget publishes its scale to the document root',
    document.documentElement.style.getPropertyValue('--th-scale') === '2',
    JSON.stringify(document.documentElement.style.getPropertyValue('--th-scale')),
  )
  const cssText = document.querySelector('style[data-plugin-css="dsh-tokenhub/css"]').textContent
  const rule = (selector) => (cssText.match(new RegExp(selector.replace(/\./g, '\\.') + '\\{[^}]*\\}')) || [''])[0]
  check('the gauge scales with the variable', rule('.th-gauge').indexOf('scale(var(--th-scale,1))') !== -1, rule('.th-gauge'))
  check('the panel keeps its own size', rule('.th-panel').indexOf('scale(') === -1, rule('.th-panel'))
  check('the dialogs keep their own size', rule('.th-modal').indexOf('scale(') === -1, rule('.th-modal'))
  check(
    'but the panel still steps aside by the gauge scaled width',
    rule('.th-panel').indexOf('calc(16px + 34px * var(--th-scale,1))') !== -1,
    rule('.th-panel'),
  )
  check('the gauge sits in the lower fifth, not mid-page', rule('.th-gauge').indexOf('top:80%') !== -1, rule('.th-gauge'))
  check('the gauge hugs the right edge', rule('.th-gauge').indexOf('right:8px') !== -1, rule('.th-gauge'))
  check('the panel hangs off the gauge centre line', rule('.th-panel').indexOf('bottom:20vh') !== -1, rule('.th-panel'))
  check('the panel is no longer centred with a translate', rule('.th-panel').indexOf('translateY') === -1, rule('.th-panel'))
  check('the gauge is three stacked tracks, one per window', all(host, '.th-gauge-row').length === 3, String(all(host, '.th-gauge-row').length))
  check(
    'bars run top to bottom: rolling, weekly, monthly',
    all(host, '.th-gauge-fill').map((f) => f.style.width).join(',') === '5%,70%,92%',
    all(host, '.th-gauge-fill').map((f) => f.style.width).join(','),
  )
  check(
    'each bar is coloured by its own window band',
    all(host, '.th-gauge-fill').map((f) => f.className.replace('th-gauge-fill ', '')).join(',') === 'ok,warn,err',
    all(host, '.th-gauge-fill').map((f) => f.className).join(','),
  )
  check('no badge is drawn any more', q(host, '.th-fab-dot') === null && !/5h\d/.test(fab().textContent), JSON.stringify(fab().textContent))
  check(
    'tooltip lists the three windows and stops after the monthly one',
    /5\u5c0f\u65f6 5%/.test(fab().title) && /\u5468 70%/.test(fab().title) && /\u6708 92%$/.test(fab().title),
    fab().title,
  )
  check(
    'tooltip carries no badge, tightest, or hint tail',
    !/\u89d2\u6807|\u6700\u7d27|\u70b9\u51fb\u5c55\u5f00/.test(fab().title),
    fab().title,
  )
  check('panel closed initially', q(host, '.th-panel') === null)

  await click(fab())
  check('panel opens on click', q(host, '.th-panel') !== null)
  check('three account cards', all(host, '.th-card').length === 3, String(all(host, '.th-card').length))
  check('active card carries the marker class', q(host, '.th-card-active').textContent.indexOf('go2') !== -1)
  check('in-use chip present', text().indexOf('\u4f7f\u7528\u4e2d') !== -1)
  check('masks shown', text().indexOf('sk-GO1') !== -1 && text().indexOf('sk-GO3') !== -1)
  check('percentages rendered', text().indexOf('5%') !== -1 && text().indexOf('70%') !== -1 && text().indexOf('92%') !== -1)
  check('countdowns rendered in H/M and days', /\u27f3 0H\d+M/.test(text()) && /\u27f3 \d\u5929\d+H/.test(text()))
  check(
    'threshold bands applied per metric',
    all(host, '.th-pct.ok').length === 2 && all(host, '.th-pct.warn').length === 2 && all(host, '.th-pct.err').length === 2,
    'ok=' + all(host, '.th-pct.ok').length + ' warn=' + all(host, '.th-pct.warn').length + ' err=' + all(host, '.th-pct.err').length,
  )
  check(
    'bar widths follow the percentages',
    all(host, '.th-fill').map((f) => f.style.width).join(',') === '10%,70%,90%,5%,70%,92%',
    all(host, '.th-fill').map((f) => f.style.width).join(','),
  )
  check('failed account renders one offline line', all(host, '.th-offline').length === 1 && text().indexOf('\u79bb\u5c94') !== -1)
  check('offline account renders no metric rows', all(host, '.th-card')[2].querySelectorAll('.th-row').length === 0)
  check('Add offered on the active row only', byText(host, 'button', '\u65b0\u589e') !== undefined && all(host, 'button').filter((b) => (b.textContent || '').trim() === '\u65b0\u589e').length === 1)
  check('Switch offered on both spare rows', all(host, 'button').filter((b) => (b.textContent || '').trim() === '\u5207\u6362').length === 2)
  check('Remove offered on both spare rows', all(host, 'button').filter((b) => (b.textContent || '').trim() === '\u5220\u9664').length === 2)
  check(
    'Rename offered on every pooled row, active or spare',
    all(host, 'button').filter((b) => (b.textContent || '').trim() === '\u6539\u540d').length === 3,
    String(all(host, 'button').filter((b) => (b.textContent || '').trim() === '\u6539\u540d').length),
  )

  await click(byText(host, 'button', '\u5207\u6362'))
  check('Switch asks for confirmation phrasing the account', q(host, '.th-modal') !== null && /go1/.test(q(host, '.th-modal').textContent))
  check('no request before confirming', calls.length === 0)
  await click(byText(host, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  check('switch posted after confirming', calls.length === 1 && calls[0].url === '/plugins/dsh-tokenhub/switch' && calls[0].body.name === 'go1', JSON.stringify(calls[0] || null))
  check('switch result surfaced', text().indexOf('\u4e0b\u4e00\u4e2a\u8bf7\u6c42\u5373\u751f\u6548') !== -1)

  // A warning is not an ordinary success toast: it stays until dismissed or
  // replaced, so it must not schedule the usual auto-dismiss timer.
  switchWarnings = [{ code: 'active-marker', message: '\u300c\u4f7f\u7528\u4e2d\u300d\u6807\u8bb0\u5199\u5165\u5931\u8d25\uff1adisk' }]
  const realSetTimeout = globalThis.setTimeout
  let dismissTimer = null
  globalThis.setTimeout = (fn, ms, ...args) => {
    if (ms === 4500) {
      dismissTimer = fn
      return 0
    }
    return realSetTimeout(fn, ms, ...args)
  }
  await click(byText(host, 'button', '\u5207\u6362'))
  await click(byText(host, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  globalThis.setTimeout = realSetTimeout
  check('a warning toast is styled as a warning', q(host, '.th-toast.warn') !== null, q(host, '.th-toast') && q(host, '.th-toast').className)
  check('the warning text is visible', /标记写入失败/.test(txt(host)), txt(host).slice(-80))
  check('a warning offers an explicit close', q(host, '.th-toast-close') !== null)
  check('a warning does not schedule the short success dismissal', dismissTimer === null)
  await click(q(host, '.th-toast-close'))
  check('closing the warning clears it', q(host, '.th-toast') === null)
  switchWarnings = null

  // A failed mutation that carries warnings is still a warning to keep visible.
  switchFailure = {
    error: '\u8bfb\u56de\u6821\u9a8c\u5931\u8d25\uff0c\u72b6\u6001\u5f85\u786e\u8ba4',
    warnings: [{ code: 'rollback', message: '\u56de\u6eda\u672a\u786e\u8ba4\uff0c\u72b6\u6001\u5f85\u786e\u8ba4' }],
  }
  await click(byText(host, 'button', '\u5207\u6362'))
  await click(byText(host, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  check('a failed mutation with warnings uses the sticky warning toast', q(host, '.th-toast.warn') !== null, q(host, '.th-toast') && q(host, '.th-toast').className)
  check('the failure warning states the reason', /回滚未确认/.test(txt(host)), txt(host).slice(-90))
  await click(q(host, '.th-toast-close'))
  switchFailure = null

  // While one mutation is in flight every conflicting action is disabled.
  let releaseSwitch
  holdSwitch = new Promise((r) => { releaseSwitch = r })
  await click(byText(host, 'button', '\u5207\u6362'))
  await click(byText(host, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  const inFlight = all(host, 'button').filter((b) => {
    const label = (b.textContent || '').trim()
    return label === '\u5207\u6362' || label === '\u5220\u9664'
  })
  check(
    'an in-flight mutation disables switch and remove everywhere',
    inFlight.length > 0 && inFlight.every((b) => b.disabled),
    inFlight.map((b) => (b.textContent || '').trim() + ':' + b.disabled).join(','),
  )
  check('no second dialog can be opened while busy', q(host, '.th-modal') === null)
  await act(async () => {
    releaseSwitch()
    await new Promise((r) => setTimeout(r, 40))
  })
  check('the in-flight mutation resumes and reports', q(host, '.th-toast.ok') !== null, q(host, '.th-toast') && q(host, '.th-toast').className)

  switchAuthFailed = true
  await click(byText(host, 'button', '\u5207\u6362'))
  await click(byText(host, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  check('a failed opencode sync is a persistent warning', q(host, '.th-toast.warn') !== null, q(host, '.th-toast') && q(host, '.th-toast').className)
  check(
    'and the failure is stated once, not twice',
    (txt(host).match(/\u540c\u6b65\u5931\u8d25/g) || []).length === 1,
    (txt(host).match(/\u540c\u6b65\u5931\u8d25/g) || []).join(' | '),
  )
  switchAuthFailed = false

  calls.length = 0
  await click(byText(host, 'button', '\u5220\u9664'))
  await click(byText(host, '.th-modal button', '\u5220\u9664'))
  check('remove posted with op=remove', calls.length === 1 && calls[0].body.op === 'remove', JSON.stringify(calls[0] || null))

  // ================= no row in force: Add must survive =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('go1', false, { rolling: win(1, 1), weekly: win(2, 2), monthly: win(3, 3) })],
  }
  const host2 = await mount()
  check(
    'with no row in force the gauge shows unavailable, not the first spare',
    all(host2, '.th-gauge-fill').map((f) => f.className.replace('th-gauge-fill ', '')).join(',') === 'na,na,na' &&
      all(host2, '.th-gauge-fill').every((f) => f.style.width === '100%'),
    all(host2, '.th-gauge-fill').map((f) => f.className + '@' + f.style.width).join(','),
  )
  check(
    'and its tooltip says no key is in force',
    (q(host2, '.th-gauge').title || '').indexOf('\u5f53\u524d\u6ca1\u6709\u751f\u6548\u7684\u5bc6\u94a5') !== -1,
    q(host2, '.th-gauge').title,
  )
  await click(q(host2, '.th-gauge'))
  check(
    'a snapshot with no scale leaves the widget at 1',
    document.documentElement.style.getPropertyValue('--th-scale') === '1',
    JSON.stringify(document.documentElement.style.getPropertyValue('--th-scale')),
  )
  check('with no row in force the panel says so', txt(host2).indexOf('\u5f53\u524d\u6ca1\u6709\u751f\u6548\u7684\u5bc6\u94a5') !== -1)
  check('and still offers Add rather than stranding the user', byText(host2, 'button', '\u65b0\u589e') !== undefined)

  // ================= the key in force with no pool entry =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [
      { id: 'current', name: '', masked: 'sk-XXXX\u2026XXX9', active: true, inPool: false, error: null, detail: null, windows: { rolling: win(3, 1), weekly: win(88, 120), monthly: win(1, 480) } },
      poolRow('Google', false, { rolling: win(4, 1), weekly: win(5, 120), monthly: win(6, 480) }),
    ],
  }
  const host3 = await mount()
  check(
    'the gauge follows the account in force',
    all(host3, '.th-gauge-fill').map((f) => f.style.width).join(',') === '3%,88%,1%',
    all(host3, '.th-gauge-fill').map((f) => f.style.width).join(','),
  )
  check(
    'and each of its bars keeps its own band',
    all(host3, '.th-gauge-fill').map((f) => f.className.replace('th-gauge-fill ', '')).join(',') === 'ok,err,ok',
    all(host3, '.th-gauge-fill').map((f) => f.className).join(','),
  )
  await click(q(host3, '.th-gauge'))
  check('the key in force is labelled, not left nameless', txt(host3).indexOf('\u5f53\u524d\u5bc6\u94a5') !== -1)
  check('it is marked as not in the pool', txt(host3).indexOf('\u672a\u5165\u6c60') !== -1)
  check('it offers Add', byText(host3, 'button', '\u65b0\u589e') !== undefined)
  check('it offers to be filed into the pool', byText(host3, 'button', '\u52a0\u5165\u6c60') !== undefined)
  check('the synthetic row offers no Rename (it has no pool entry)', all(host3, 'button').filter((b) => (b.textContent || '').trim() === '\u6539\u540d').length === 1)
  check('it cannot be switched to or removed', byText(host3, 'button', '\u5207\u6362') === undefined || byText(host3, 'button', '\u5207\u6362').closest('.th-card').textContent.indexOf('Google') !== -1)

  calls.length = 0
  await click(byText(host3, 'button', '\u52a0\u5165\u6c60'))
  check('join dialog asks for a name only', all(host3, '.th-input').length === 1)
  await act(async () => {
    const input = all(host3, '.th-input')[0]
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
    setter.call(input, 'Main')
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 10))
  })
  await click(byText(host3, '.th-modal button', '\u52a0\u5165'))
  check('join posted op=joinPool with the name', calls.length === 1 && calls[0].body.op === 'joinPool' && calls[0].body.name === 'Main', JSON.stringify(calls[0] || null))

  accountWarnings = [{ code: 'active-marker', message: '\u300c\u4f7f\u7528\u4e2d\u300d\u6807\u8bb0\u5199\u5165\u5931\u8d25' }]
  await click(byText(host3, 'button', '\u52a0\u5165\u6c60'))
  await act(async () => {
    const input = all(host3, '.th-input')[0]
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
    setter.call(input, 'Warned')
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 10))
  })
  await click(byText(host3, '.th-modal button', '\u52a0\u5165'))
  check('a successful join with warnings closes the dialog', q(host3, '.th-modal') === null)
  check('a join warning reaches the parent as a sticky warning', q(host3, '.th-toast.warn') !== null && /标记写入失败/.test(txt(host3)), q(host3, '.th-toast') && q(host3, '.th-toast').className)
  await click(q(host3, '.th-toast-close'))
  accountWarnings = null

  // ================= the add dialog =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('go1', true, { rolling: win(1, 1), weekly: win(2, 2), monthly: win(3, 3) })],
  }
  const host5 = await mount()
  await click(q(host5, '.th-gauge'))
  await click(byText(host5, 'button', '\u65b0\u589e'))
  const inputs = () => all(host5, '.th-input')
  check('Add dialog opens with a name field and a key field', inputs().length === 2)
  check(
    'the name field starts empty and hints at the generated form',
    inputs()[0].value === '' && inputs()[0].placeholder === 'go1',
    JSON.stringify([inputs()[0].value, inputs()[0].placeholder]),
  )
  check('the key field is masked by default', inputs()[1].type === 'password')
  check('the name field refuses browser autofill', inputs()[0].getAttribute('autocomplete') === 'off', inputs()[0].getAttribute('autocomplete'))
  check(
    'the key field refuses password-manager autofill',
    inputs()[1].getAttribute('autocomplete') === 'new-password',
    inputs()[1].getAttribute('autocomplete'),
  )
  check(
    'the key field offers a reveal toggle and clipboard paste',
    byText(host5, 'button', '\u663e\u793a') !== undefined && byText(host5, 'button', '\u4ece\u526a\u8d34\u677f\u7c98\u8d34') !== undefined,
  )
  calls.length = 0
  await act(async () => {
    const setValue = (el, value) => {
      const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
      setter.call(el, value)
      el.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    }
    setValue(inputs()[0], 'Zebra')
    setValue(inputs()[1], 'sk-NEWKEY')
    await new Promise((r) => setTimeout(r, 10))
  })
  await click(byText(host5, '.th-modal button', '\u786e\u8ba4'))
  check(
    'Add posted the typed name verbatim, not a substituted one',
    calls.length === 1 && calls[0].body.op === 'add' && calls[0].body.name === 'Zebra' && calls[0].body.key === 'sk-NEWKEY',
    JSON.stringify(calls[0] || null),
  )
  check('the dialog closes after a successful add', q(host5, '.th-modal') === null)

  // ================= a row from an older host =================
  // The bundle is served by revision and the host module is cached by URL, so
  // the two can be a version apart. A row with no inPool must read as the older
  // host meant it: an ordinary pool entry, not a nameless one.
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [{ name: 'legacy', masked: 'sk-LEGA\u2026zzzz', active: false, error: null, detail: null, windows: { rolling: win(1, 1), weekly: win(2, 2), monthly: win(3, 3) } }],
  }
  const host7 = await mount()
  await click(q(host7, '.th-gauge'))
  check('a row with no inPool keeps its own name', txt(host7).indexOf('legacy') !== -1 && txt(host7).indexOf('\u5f53\u524d\u5bc6\u94a5') === -1, txt(host7).slice(0, 90))
  check('a row with no inPool is not marked as unpooled', txt(host7).indexOf('\u672a\u5165\u6c60') === -1)
  check('a row with no inPool still offers Switch, Rename and Remove', byText(host7, 'button', '\u5207\u6362') !== undefined && byText(host7, 'button', '\u6539\u540d') !== undefined && byText(host7, 'button', '\u5220\u9664') !== undefined)
  check('a row with no inPool is not offered a join it cannot need', byText(host7, 'button', '\u52a0\u5165\u6c60') === undefined)
  calls.length = 0
  await click(byText(host7, 'button', '\u5207\u6362'))
  await click(byText(host7, '.th-modal button', '\u786e\u8ba4\u5207\u6362'))
  check(
    'a row with no id still switches by its name',
    calls.length === 1 && calls[0].body.name === 'legacy',
    JSON.stringify(calls[0] || null),
  )
  calls.length = 0
  await click(byText(host7, 'button', '\u6539\u540d'))
  await click(byText(host7, '.th-modal button', '\u4fdd\u5b58'))
  check(
    'a row with no id still renames by its name',
    calls.length === 1 && calls[0].body.op === 'rename' && calls[0].body.name === 'legacy',
    JSON.stringify(calls[0] || null),
  )

  // ================= rename =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('Zebra', true, { rolling: win(1, 1), weekly: win(2, 2), monthly: win(3, 3) })],
  }
  const host6 = await mount()
  await click(q(host6, '.th-gauge'))
  await click(byText(host6, 'button', '\u6539\u540d'))
  check(
    'rename dialog opens prefilled with the current name',
    all(host6, '.th-input').length === 1 && all(host6, '.th-input')[0].value === 'Zebra',
    JSON.stringify(all(host6, '.th-input').map((i) => i.value)),
  )
  check('rename dialog asks for no key', all(host6, '.th-input').length === 1)
  calls.length = 0
  await act(async () => {
    const input = all(host6, '.th-input')[0]
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
    setter.call(input, 'Main')
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 10))
  })
  await click(byText(host6, '.th-modal button', '\u4fdd\u5b58'))
  check(
    'rename posted the old name and the new one',
    calls.length === 1 && calls[0].body.op === 'rename' && calls[0].body.name === 'Zebra' && calls[0].body.to === 'Main',
    JSON.stringify(calls[0] || null),
  )

  accountFailure = true
  accountWarnings = [{ code: 'partial-rename', message: '\u65b0\u540d\u5df2\u5199\u5165\uff0c\u4f46\u65e7\u540d\u672a\u80fd\u5220\u9664' }]
  await click(byText(host6, 'button', '\u6539\u540d'))
  await act(async () => {
    const input = all(host6, '.th-input')[0]
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
    setter.call(input, 'Loaded')
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 10))
  })
  await click(byText(host6, '.th-modal button', '\u4fdd\u5b58'))
  check('a failed rename keeps the dialog open for correction', q(host6, '.th-modal') !== null)
  check('the rename error stays in the dialog', /部分完成/.test(txt(host6)), txt(host6).slice(-120))
  check(
    'the rename warning reaches the parent as a sticky warning',
    q(host6, '.th-toast.warn') !== null && /旧名未能删除/.test(txt(host6)),
    q(host6, '.th-toast') && q(host6, '.th-toast').className,
  )
  await click(q(host6, '.th-modal button', '\u53d6\u6d88'))
  accountFailure = false
  accountWarnings = null

  // ================= a rolling window with no number =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('go9', true, {
      rolling: { status: 'ok', percent: null, resetsAt: null },
      weekly: win(10, 1),
      monthly: win(20, 1),
    })],
  }
  const host8 = await mount()
  check(
    'a window with no reading fills its track in the neutral colour',
    all(host8, '.th-gauge-fill')[0].className.indexOf('na') !== -1 && all(host8, '.th-gauge-fill')[0].style.width === '100%',
    all(host8, '.th-gauge-fill')[0].className + ' @ ' + all(host8, '.th-gauge-fill')[0].style.width,
  )
  check(
    'the windows that did report still read normally',
    all(host8, '.th-gauge-fill').slice(1).map((f) => f.style.width).join(',') === '10%,20%',
    all(host8, '.th-gauge-fill').slice(1).map((f) => f.style.width).join(','),
  )

  // ================= over-100 readings keep their text, cap their bar =================
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('over', true, { rolling: win(120, 1), weekly: win(59.6, 1), monthly: win(0, 1) })],
  }
  const host10 = await mount()
  await click(q(host10, '.th-gauge'))
  check(
    'a reading over 100 caps the bar but not the number',
    all(host10, '.th-gauge-fill')[0].style.width === '100%' && txt(host10).indexOf('120%') !== -1,
    all(host10, '.th-gauge-fill')[0].style.width + ' / ' + txt(host10).slice(0, 80),
  )
  check(
    'a fractional reading is displayed at the nearest integer',
    txt(host10).indexOf('60%') !== -1 && txt(host10).indexOf('0%') !== -1,
    txt(host10).slice(0, 120),
  )

  // ================= a failed snapshot =================
  const liveFetch = globalThis.fetch
  globalThis.fetch = async () => { throw new Error('offline') }
  const host9 = await mount()
  check(
    'a failed snapshot fills every track neutrally rather than calmly',
    all(host9, '.th-gauge-fill').map((f) => f.className.replace('th-gauge-fill ', '')).join(',') === 'na,na,na',
    all(host9, '.th-gauge-fill').map((f) => f.className).join(','),
  )
  globalThis.fetch = liveFetch

  // ================= an older snapshot response cannot overwrite a newer one =================
  {
    const deferred = []
    const liveFetch = globalThis.fetch
    globalThis.fetch = (url) => {
      if (String(url).indexOf('/snapshot') !== -1) {
        return new Promise((resolve) => {
          deferred.push((payload) => resolve({ ok: true, status: 200, async json() { return payload } }))
        })
      }
      return liveFetch(url)
    }
    const host11 = await mount()
    await click(q(host11, '.th-gauge'))
    await click(byText(host11, 'button', '\u5237\u65b0'))
    check('two snapshot requests can overlap', deferred.length === 2, String(deferred.length))
    const newer = {
      updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
      entries: [poolRow('newer', true, { rolling: win(1, 1), weekly: win(2, 2), monthly: win(3, 3) })],
    }
    const older = {
      updatedAt: NOW - 1000, error: null, thresholds: { warn: 60, error: 85 },
      entries: [poolRow('older', true, { rolling: win(9, 1), weekly: win(9, 2), monthly: win(9, 3) })],
    }
    await act(async () => {
      deferred[1](newer)
      await new Promise((r) => setTimeout(r, 10))
    })
    await act(async () => {
      deferred[0](older)
      await new Promise((r) => setTimeout(r, 20))
    })
    check(
      'a late older response does not replace the newer snapshot',
      txt(host11).indexOf('newer') !== -1 && txt(host11).indexOf('older') === -1,
      txt(host11).slice(0, 120),
    )
    globalThis.fetch = liveFetch
  }

  // ================= stale windows are not fresh green readings =================
  {
    SNAPSHOT = {
      updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
      entries: [poolRow('stale', true, {
        rolling: { status: 'ok', percent: 55, resetsAt: null, stale: true, error: 'network', lastSuccessAt: NOW - 60000, lastAttemptAt: NOW },
        weekly: win(10, 1),
        monthly: win(20, 1),
      })],
    }
    const hostStale = await mount()
    await click(q(hostStale, '.th-gauge'))
    check(
      'a stale window uses the neutral band, not the fresh green one',
      all(hostStale, '.th-gauge-fill')[0].className.indexOf('na') !== -1 &&
        all(hostStale, '.th-gauge-fill')[0].style.width === '55%' &&
        all(hostStale, '.th-pct')[0].className.indexOf('na') !== -1,
      all(hostStale, '.th-gauge-fill')[0].className + ' / ' + all(hostStale, '.th-pct')[0].className,
    )
    check(
      'a stale window says when the last reading was and why it is old',
      /上次/.test(txt(hostStale)) && /network/.test(txt(hostStale)),
      txt(hostStale).slice(0, 180),
    )
    check('fresh windows on the same card still read normally', all(hostStale, '.th-gauge-fill')[1].className.indexOf('ok') !== -1)
  }

  // ================= an internal snapshot failure is shown, not "no entries" =================
  {
    const liveFetch = globalThis.fetch
    globalThis.fetch = async () => ({
      ok: false,
      status: 500,
      async json() { return { ok: false, error: 'internal: disk gone' } },
    })
    const hostInternal = await mount()
    await click(q(hostInternal, '.th-gauge'))
    check(
      'a 500 snapshot renders the internal error text',
      /internal: disk gone/.test(txt(hostInternal)),
      txt(hostInternal).slice(0, 160),
    )
    check('the internal failure does not claim the account list is empty', !/未发现账号|未配置 opencode-go/.test(txt(hostInternal)), txt(hostInternal).slice(0, 160))
    globalThis.fetch = liveFetch
  }

  // ================= English dictionary =================
  // Re-execute the bundle so its factory picks the other language.
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US', configurable: true })
  let enSpec = null
  dom.window.__ModuleLoader__ = { load: (s) => { enSpec = s } }
  await import('../lib/client.js?lang=en')
  const enMod = materialize(enSpec)
  let EnWidget = null
  enMod.apply({ get: (k) => (k === 'slots' ? { inject: (n, fn) => fn(), register: (s, c) => { EnWidget = c; return () => {} } } : undefined) })
  SNAPSHOT = {
    updatedAt: NOW, error: null, thresholds: { warn: 60, error: 85 },
    entries: [poolRow('go2', true, { rolling: win(5, 1), weekly: win(70, 120), monthly: win(92, 480) })],
  }
  const host4 = document.createElement('div')
  document.body.appendChild(host4)
  const root4 = createRoot(host4)
  await act(async () => { root4.render(React.createElement(EnWidget)) })
  await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
  await click(q(host4, '.th-gauge'))
  check('English panel uses English window labels', txt(host4).indexOf('Rolling') !== -1 && txt(host4).indexOf('Weekly') !== -1 && txt(host4).indexOf('Monthly') !== -1, txt(host4).slice(0, 120))
  check('English panel has no Chinese window labels left', !/5\u5c0f\u65f6/.test(txt(host4)))
  check(
    'English tooltip uses the English labels and stops at the monthly one',
    /Rolling 5%/.test(q(host4, '.th-gauge').title) &&
      /Weekly 70%/.test(q(host4, '.th-gauge').title) &&
      /Monthly 92%$/.test(q(host4, '.th-gauge').title),
    q(host4, '.th-gauge').title,
  )
  check(
    'English countdown uses an English day unit',
    /\u27f3 \d+d\d+H/.test(txt(host4)) && !/\u5929/.test(txt(host4)),
    (txt(host4).match(/\u27f3[^\u27f3]{0,10}/g) || []).join(' '),
  )
  check('stylesheet not injected twice', document.querySelectorAll('style[data-plugin-css="dsh-tokenhub/css"]').length === 1)
} catch (error) {
  results.push('CRASH  ' + String((error && error.stack) || error))
}

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL') || r.startsWith('CRASH')).length
console.log('\n' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exit(failed === 0 ? 0 : 1)
