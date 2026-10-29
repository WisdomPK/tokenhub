// dsh-tokenhub — usage parsing contract test.
//
// Reads the shared fixture committed at the repository root and drives the Host
// half through its snapshot route. Synthetic data only; no real keys.
//
//   node test/usage-contract.test.mjs

import { readFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { apply } from '../lib/index.js'

const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/usage-contract.json', import.meta.url), 'utf8'))

const results = []
const check = (label, cond, extra) => results.push((cond ? 'PASS  ' : 'FAIL  ') + label + (extra ? '   [' + extra + ']' : ''))

const KEY = 'sk-CONTRACT000000000000000000000000000000000000000000000000000001'
const ROUTE = '/plugins/dsh-tokenhub/snapshot'

function makeStore() {
  const refs = new Map([['OPENCODE_GO_API_KEY', KEY]])
  return {
    refs,
    async resolve(ref) {
      const value = refs.get(ref)
      return value === undefined || value === '' ? undefined : { value, source: 'file' }
    },
    async describe(ref) {
      return { configured: refs.has(ref) && refs.get(ref) !== '', source: 'file', writable: true }
    },
    async set(ref, value) { refs.set(ref, value) },
    async unset(ref) { refs.delete(ref) },
  }
}

function makeCtx(store) {
  const routes = new Map()
  return {
    routes,
    credentials: store,
    interval: () => () => {},
    effect: (fn) => fn(),
    get: (k) => (k === 'webServer'
      ? { register: (route) => { routes.set(route.path, route.handler); return () => routes.delete(route.path) } }
      : undefined),
  }
}

function request(ctx, path, body) {
  return new Promise((resolve) => {
    const req = new EventEmitter()
    req.method = body === undefined ? 'GET' : 'POST'
    req.url = path + (body === undefined ? '?force=1' : '')
    req.headers = { host: '127.0.0.1:3000' }
    req.destroy = () => {}
    const res = {
      _status: 0,
      writeHead(status) { this._status = status },
      end(payload) { resolve({ status: this._status, body: JSON.parse(payload) }) },
    }
    const handler = ctx.routes.get(path)
    if (!handler) return resolve({ status: -1, body: { error: 'no route ' + path } })
    handler(req, res)
    setImmediate(() => {
      if (body !== undefined) req.emit('data', Buffer.from(JSON.stringify(body)))
      req.emit('end')
    })
  })
}

/** Run one usage payload through a fresh Host and return its single row. */
async function rowFor(payload) {
  globalThis.fetch = async () => ({ status: 200, ok: true, async json() { return payload } })
  const store = makeStore()
  const ctx = makeCtx(store)
  apply(ctx, { keyNames: [], syncOpencodeAuth: false, refreshMs: 60000, timeoutMs: 1000 })
  const snap = (await request(ctx, ROUTE, undefined)).body
  return snap.entries[0]
}

/** A response body with one percent under test and two known-good windows. */
function responseWith(percent) {
  return {
    usage: {
      rolling: { status: 'ok', percent, resetsAt: '2027-01-01T00:00:00Z' },
      weekly: { status: 'ok', percent: 10, resetsAt: '2027-01-02T00:00:00Z' },
      monthly: { status: 'ok', percent: 20, resetsAt: '2027-01-03T00:00:00Z' },
    },
  }
}

try {
  // ---- percent boundary conversion ----
  for (const item of fixture.percent) {
    const row = await rowFor(responseWith(item.input))
    check(
      'percent ' + item.id + ' -> ' + String(item.expected),
      row.windows.rolling.percent === item.expected,
      JSON.stringify(row.windows.rolling.percent),
    )
    check(
      'percent ' + item.id + ' leaves other windows alone',
      row.windows.weekly.percent === 10 && row.windows.monthly.percent === 20,
      JSON.stringify([row.windows.weekly.percent, row.windows.monthly.percent]),
    )
  }

  // Non-JSON numbers still have to be refused at the boundary.
  for (const [label, value] of [['NaN', NaN], ['Infinity', Infinity], ['-Infinity', -Infinity]]) {
    const row = await rowFor(responseWith(value))
    check('percent ' + label + ' is unavailable', row.windows.rolling.percent === null, String(row.windows.rolling.percent))
  }

  // ---- reset time conversion ----
  for (const item of fixture.resetsAt) {
    const payload = {
      usage: {
        rolling: { status: 'ok', percent: 12.5, resetsAt: item.input },
        weekly: { status: 'ok', percent: 10 },
        monthly: { status: 'ok', percent: 20 },
      },
    }
    const row = await rowFor(payload)
    check('reset ' + item.id + ' keeps the percentage', row.windows.rolling.percent === 12.5, JSON.stringify(row.windows.rolling.percent))
    if (item.valid) {
      const expected = item.id === 'naive-treated-as-utc' ? Date.parse(item.input + 'Z') : Date.parse(item.input)
      check(
        'reset ' + item.id + ' is a usable timestamp',
        typeof row.windows.rolling.resetsAt === 'string' && Date.parse(row.windows.rolling.resetsAt) === expected,
        String(row.windows.rolling.resetsAt),
      )
    } else {
      check('reset ' + item.id + ' is unavailable without losing the percentage', row.windows.rolling.resetsAt === null, String(row.windows.rolling.resetsAt))
    }
  }

  // resets_at is the older field name and must stay readable.
  {
    const row = await rowFor({
      usage: {
        rolling: { status: 'ok', percent: 1, resets_at: '2026-11-29T12:00:00Z' },
        weekly: { status: 'ok', percent: 2 },
        monthly: { status: 'ok', percent: 3 },
      },
    })
    check('resets_at is accepted', Date.parse(row.windows.rolling.resetsAt) === Date.parse('2026-11-29T12:00:00Z'), String(row.windows.rolling.resetsAt))
  }

  // ---- resetsAt / resets_at fallback and calendar validation ----
  {
    const resetPayload = (resetsAt, resets_at) => ({
      usage: {
        rolling: { status: 'ok', percent: 5, resetsAt, resets_at },
        weekly: { status: 'ok', percent: 6 },
        monthly: { status: 'ok', percent: 7 },
      },
    })
    const expected = Date.parse('2026-11-29T12:00:00Z')
    const fallback = async (label, resetsAt) => {
      const row = await rowFor(resetPayload(resetsAt, '2026-11-29T12:00:00Z'))
      check(label, Date.parse(row.windows.rolling.resetsAt) === expected, String(row.windows.rolling.resetsAt))
    }
    await fallback('resets_at is used when resetsAt is null', null)
    await fallback('resets_at is used when resetsAt is empty', '')
    await fallback('resets_at is used when resetsAt is garbage', 'not-a-time')
    {
      const row = await rowFor(resetPayload('2026-12-01T00:00:00Z', '2026-11-29T12:00:00Z'))
      check(
        'a valid resetsAt wins over resets_at',
        Date.parse(row.windows.rolling.resetsAt) === Date.parse('2026-12-01T00:00:00Z'),
        String(row.windows.rolling.resetsAt),
      )
    }
    {
      const row = await rowFor(resetPayload('2026-02-30T00:00:00Z', '2026-11-29T12:00:00Z'))
      check(
        'an impossible calendar date falls back instead of being normalized',
        Date.parse(row.windows.rolling.resetsAt) === expected,
        String(row.windows.rolling.resetsAt),
      )
    }
    for (const [label, value] of [
      ['February 30', '2026-02-30T00:00:00Z'],
      ['April 31', '2026-04-31T00:00:00Z'],
      ['month 13', '2026-13-01T00:00:00Z'],
    ]) {
      const row = await rowFor(resetPayload(value, undefined))
      check('an impossible calendar date is rejected: ' + label, row.windows.rolling.resetsAt === null, String(row.windows.rolling.resetsAt))
    }
  }

  // ---- whole responses ----
  {
    const row = await rowFor({ usage: fixture.responses.partial })
    check(
      'a partial response keeps every window independent',
      row.windows.rolling.percent === 12.5 && row.windows.weekly.percent === null && row.windows.monthly.percent === 7,
      JSON.stringify([row.windows.rolling.percent, row.windows.weekly.percent, row.windows.monthly.percent]),
    )
    check('a partial response is not a transport failure', row.error === null, String(row.error))
  }
  {
    const row = await rowFor({ usage: fixture.responses['all-missing'] })
    check(
      'all-missing windows stay unavailable, not zero',
      row.windows.rolling.percent === null && row.windows.weekly.percent === null && row.windows.monthly.percent === null,
      JSON.stringify(row.windows),
    )
    check('all-missing windows are not a malformed response', row.error === null, String(row.error))
  }
  {
    const row = await rowFor({ usage: fixture.responses['bad-time'] })
    check(
      'a bad reset time only affects the countdown',
      row.windows.rolling.percent === 12.5 && row.windows.weekly.percent === 40 && row.windows.monthly.percent === 70,
      JSON.stringify([row.windows.rolling.percent, row.windows.weekly.percent, row.windows.monthly.percent]),
    )
    check(
      'invalid reset times become null while the valid resets_at survives',
      row.windows.rolling.resetsAt === null &&
        row.windows.weekly.resetsAt === null &&
        Date.parse(row.windows.monthly.resetsAt) === Date.parse('2026-11-29T12:00:00Z'),
      JSON.stringify([row.windows.rolling.resetsAt, row.windows.weekly.resetsAt, row.windows.monthly.resetsAt]),
    )
  }
  {
    const row = await rowFor([1, 2, 3])
    check(
      'a top-level array is a malformed response with unavailable windows',
      row.error === 'bad-json' && row.windows !== null && row.windows.rolling.percent === null,
      JSON.stringify([row.error, row.windows && row.windows.rolling]),
    )
  }
  {
    const row = await rowFor({ usage: {} })
    check('an empty usage object is window unavailability, not malformed JSON', row.error === null && row.windows !== null, JSON.stringify(row.error))
  }

  // ---- adding an account requires at least one usable window ----
  {
    let fetchPayload = { usage: fixture.responses['all-missing'] }
    globalThis.fetch = async () => ({ status: 200, ok: true, async json() { return fetchPayload } })
    const store = makeStore()
    const ctx = makeCtx(store)
    apply(ctx, { keyNames: [], syncOpencodeAuth: false, refreshMs: 60000, timeoutMs: 1000 })
    const accountRoute = '/plugins/dsh-tokenhub/account'
    const before = JSON.stringify(Array.from(store.refs.entries()))
    const refused = (await request(ctx, accountRoute, { op: 'add', name: 'probe', key: 'sk-NEWKEY000000000000000000000000000000000000000000000000000001' })).body
    check(
      'a 200 response with no usable window is refused',
      refused.ok === false && /接口数据不可用/.test(refused.error || ''),
      JSON.stringify(refused).slice(0, 180),
    )
    check('a refused probe writes no credential', JSON.stringify(Array.from(store.refs.entries())) === before)
    fetchPayload = { usage: fixture.responses.partial }
    const accepted = (await request(ctx, accountRoute, { op: 'add', name: 'probe2', key: 'sk-NEWKEY000000000000000000000000000000000000000000000000000002' })).body
    check('one usable window is enough to accept a key', accepted.ok === true, JSON.stringify(accepted).slice(0, 180))
  }
} catch (error) {
  results.push('CRASH  ' + String((error && error.stack) || error))
}

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL') || r.startsWith('CRASH')).length
console.log('\n' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exit(failed === 0 ? 0 : 1)
