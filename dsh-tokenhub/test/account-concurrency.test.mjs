// dsh-tokenhub — account mutation serialization test.
//
// Every scenario uses an in-memory credential store and a stubbed usage
// endpoint. Deferred promises hold one operation in flight so interleavings are
// deterministic; no sleeps decide the order.
//
//   node test/account-concurrency.test.mjs

import { EventEmitter } from 'node:events'
import { apply } from '../lib/index.js'

const results = []
const check = (label, cond, extra) => results.push((cond ? 'PASS  ' : 'FAIL  ') + label + (extra ? '   [' + extra + ']' : ''))

const KEY_A = 'sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1'
const KEY_B = 'sk-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2'
const KEY_C = 'sk-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC3'
const KEY_D = 'sk-DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD4'

const ACCOUNT = '/plugins/dsh-tokenhub/account'
const SWITCH = '/plugins/dsh-tokenhub/switch'
const SNAP = '/plugins/dsh-tokenhub/snapshot'

function makeStore(initial) {
  const refs = new Map(Object.entries(initial || {}))
  const writes = []
  return {
    refs,
    writes,
    async resolve(ref) {
      const value = refs.get(ref)
      return value === undefined || value === '' ? undefined : { value, source: 'file' }
    },
    async describe(ref) {
      return { configured: refs.has(ref) && refs.get(ref) !== '', source: 'file', writable: true }
    },
    async set(ref, value) {
      writes.push({ op: 'set', ref, value })
      refs.set(ref, value)
    },
    async unset(ref) {
      writes.push({ op: 'unset', ref })
      refs.delete(ref)
    },
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
    req.url = path
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

const usageFor = (key) => {
  const n = key === KEY_A ? 10 : key === KEY_B ? 70 : 5
  return {
    usage: {
      rolling: { status: 'ok', percent: n, resetsAt: '2027-01-01T00:00:00Z' },
      weekly: { status: 'ok', percent: n + 1, resetsAt: '2027-01-02T00:00:00Z' },
      monthly: { status: 'ok', percent: n + 2, resetsAt: '2027-01-03T00:00:00Z' },
    },
  }
}

/** The default stub: every key validates. */
function standardFetch(url, init) {
  const key = String((init && init.headers && init.headers.Authorization) || '').replace('Bearer ', '')
  return Promise.resolve({ status: 200, ok: true, async json() { return usageFor(key) } })
}

function start(refs, config) {
  const store = makeStore(refs)
  const ctx = makeCtx(store)
  apply(ctx, Object.assign({ keyNames: [], syncOpencodeAuth: false, refreshMs: 60000, timeoutMs: 1000 }, config || {}))
  return { store, ctx }
}

async function waitFor(condition, label = 'condition') {
  const until = Date.now() + 2000
  while (Date.now() < until) {
    if (condition()) return true
    await new Promise((r) => setTimeout(r, 5))
  }
  throw new Error('timed out waiting for ' + label)
}

let store

try {
  globalThis.fetch = standardFetch

  // ================= concurrent adds of one name =================
  {
    let release
    const gate = new Promise((r) => { release = r })
    const probes = []
    globalThis.fetch = async (url, init) => {
      const key = String((init && init.headers && init.headers.Authorization) || '').replace('Bearer ', '')
      if (key === KEY_A || key === KEY_B) {
        probes.push(key)
        await gate
      }
      return { status: 200, ok: true, async json() { return usageFor(key) } }
    }
    const run = start({})
    store = run.store
    const first = request(run.ctx, ACCOUNT, { op: 'add', name: 'race', key: KEY_A })
    const second = request(run.ctx, ACCOUNT, { op: 'add', name: 'race', key: KEY_B })
    await waitFor(() => probes.length === 2, 'both remote probes')
    release()
    const [r1, r2] = await Promise.all([first, second])
    const winners = [r1.body, r2.body].filter((r) => r.ok)
    const losers = [r1.body, r2.body].filter((r) => !r.ok)
    check(
      'concurrent same-name adds: exactly one succeeds',
      winners.length === 1 && losers.length === 1,
      JSON.stringify([r1.body.ok, r2.body.ok]),
    )
    check('the losing add reports the existing account', /已存在/.test(losers[0] && losers[0].error || ''), losers[0] && losers[0].error)
    const winnerKey = r1.body.ok ? KEY_A : KEY_B
    check('the final credential is the winner, never overwritten', store.refs.get('OPENCODE_GO_KEY_race') === winnerKey, String(store.refs.get('OPENCODE_GO_KEY_race')))
    check(
      'the name is written only once',
      store.writes.filter((w) => w.op === 'set' && w.ref === 'OPENCODE_GO_KEY_race').length === 1,
      String(store.writes.filter((w) => w.op === 'set' && w.ref === 'OPENCODE_GO_KEY_race').length),
    )
    check('mutation results carry a warnings array', Array.isArray(winners[0] && winners[0].warnings) && Array.isArray(losers[0] && losers[0].warnings))
  }

  // ================= rename and add racing for the same target =================
  {
    let releaseProbe
    const gate = new Promise((r) => { releaseProbe = r })
    const probes = []
    globalThis.fetch = async (url, init) => {
      const key = String((init && init.headers && init.headers.Authorization) || '').replace('Bearer ', '')
      if (key === KEY_D) {
        probes.push(key)
        await gate
      }
      return { status: 200, ok: true, async json() { return usageFor(key) } }
    }
    const run = start({
      OPENCODE_GO_API_KEY: KEY_A,
      OPENCODE_GO_KEY_src: KEY_B,
    }, { keyNames: ['src'] })
    store = run.store
    let releaseRename
    const renameGate = new Promise((r) => { releaseRename = r })
    const realSet = store.set.bind(store)
    let renameHeld = false
    store.set = async (ref, value) => {
      if (ref === 'OPENCODE_GO_KEY_dst' && !renameHeld) {
        renameHeld = true
        await renameGate
      }
      return realSet(ref, value)
    }
    const renamed = request(run.ctx, ACCOUNT, { op: 'rename', name: 'src', to: 'dst' })
    const added = request(run.ctx, ACCOUNT, { op: 'add', name: 'dst', key: KEY_D })
    // The add has passed its early conflict check and is waiting on the endpoint.
    await waitFor(() => probes.length === 1, 'the add probe')
    releaseRename()
    const renameResult = await renamed
    releaseProbe()
    const addResult = await added
    check('rename to dst completed', renameResult.body.ok === true, JSON.stringify(renameResult.body).slice(0, 160))
    check(
      'an add cannot overwrite the just-renamed target',
      addResult.body.ok === false && /已存在/.test(addResult.body.error || ''),
      JSON.stringify(addResult.body).slice(0, 160),
    )
    check('the renamed key survives', store.refs.get('OPENCODE_GO_KEY_dst') === KEY_B, String(store.refs.get('OPENCODE_GO_KEY_dst')))
    check('the old name is gone', !store.refs.has('OPENCODE_GO_KEY_src'))
    check('the key in force is untouched', store.refs.get('OPENCODE_GO_API_KEY') === KEY_A)
  }

  // ================= switch then remove: the delete sees the queued current =================
  {
    let releaseSwitch
    const gate = new Promise((r) => { releaseSwitch = r })
    const run = start({
      OPENCODE_GO_API_KEY: KEY_A,
      OPENCODE_GO_KEY_go1: KEY_A,
      OPENCODE_GO_KEY_go2: KEY_B,
    }, { keyNames: ['go1', 'go2'] })
    store = run.store
    const realSet = store.set.bind(store)
    let held = false
    store.set = async (ref, value) => {
      if (ref === 'OPENCODE_GO_API_KEY' && !held) {
        held = true
        await gate
      }
      return realSet(ref, value)
    }
    const switched = request(run.ctx, SWITCH, { name: 'go2' })
    await waitFor(() => held, 'the queued switch write')
    const removed = request(run.ctx, ACCOUNT, { op: 'remove', name: 'go1' })
    releaseSwitch()
    const switchResult = await switched
    const removeResult = await removed
    check('the switch succeeds', switchResult.body.ok === true, JSON.stringify(switchResult.body).slice(0, 160))
    check(
      'remove runs after the switch and uses the latest current key',
      removeResult.body.ok === true && !store.refs.has('OPENCODE_GO_KEY_go1'),
      JSON.stringify(removeResult.body).slice(0, 180),
    )
    check('the new current key is still in force', store.refs.get('OPENCODE_GO_API_KEY') === KEY_B)
    check('the new account is still in the pool', store.refs.get('OPENCODE_GO_KEY_go2') === KEY_B)
  }

  // ================= a failed operation does not wedge the queue =================
  {
    globalThis.fetch = standardFetch
    const run = start({})
    store = run.store
    const failed = await request(run.ctx, ACCOUNT, { op: 'add', name: 'bad name', key: KEY_A })
    check('an invalid name fails', failed.body.ok === false && /账号名/.test(failed.body.error || ''), failed.body.error)
    const next = await request(run.ctx, ACCOUNT, { op: 'add', name: 'after', key: KEY_A })
    check('the queue still accepts the next operation', next.body.ok === true && store.refs.get('OPENCODE_GO_KEY_after') === KEY_A, JSON.stringify(next.body).slice(0, 160))
  }

  // ================= partial success: current key changed, marker failed =================
  {
    globalThis.fetch = standardFetch
    const run = start({
      OPENCODE_GO_API_KEY: KEY_A,
      OPENCODE_GO_KEY_go1: KEY_A,
      OPENCODE_GO_KEY_go2: KEY_B,
      OPENCODE_GO_KEY_ACTIVE: 'go1',
    }, { keyNames: ['go1', 'go2'] })
    store = run.store
    const realSet = store.set.bind(store)
    store.set = async (ref, value) => {
      if (ref === 'OPENCODE_GO_KEY_ACTIVE') throw new Error('marker is read-only')
      return realSet(ref, value)
    }
    const switched = await request(run.ctx, SWITCH, { name: 'go2' })
    check('a verified switch stays successful when only the marker fails', switched.body.ok === true, JSON.stringify(switched.body).slice(0, 180))
    check('the key really changed', store.refs.get('OPENCODE_GO_API_KEY') === KEY_B)
    check(
      'the response carries a structured warning',
      Array.isArray(switched.body.warnings) &&
        switched.body.warnings.some((w) => w.code === 'active-marker' && /标记/.test(w.message || '')),
      JSON.stringify(switched.body.warnings),
    )
    check('the old-client message also names the warning', /标记/.test(switched.body.message || ''), switched.body.message)
  }

  // ================= describe catches targets the pool scan cannot list =================
  {
    globalThis.fetch = standardFetch
    const run = start(
      {
        OPENCODE_GO_API_KEY: KEY_A,
        OPENCODE_GO_KEY_go1: KEY_B,
        OPENCODE_GO_KEY_hidden: KEY_C,
      },
      { keyNames: ['go1'] },
    )
    store = run.store
    const renameHidden = await request(run.ctx, ACCOUNT, { op: 'rename', name: 'go1', to: 'hidden' })
    check(
      'rename refuses an unlisted but configured target',
      renameHidden.body.ok === false && /已存在/.test(renameHidden.body.error || ''),
      JSON.stringify(renameHidden.body).slice(0, 160),
    )
    check('the unlisted credential is not overwritten', store.refs.get('OPENCODE_GO_KEY_hidden') === KEY_C)
    const joinHidden = await request(run.ctx, ACCOUNT, { op: 'joinPool', name: 'hidden' })
    check(
      'joinPool refuses an unlisted but configured target',
      joinHidden.body.ok === false && /已存在/.test(joinHidden.body.error || ''),
      JSON.stringify(joinHidden.body).slice(0, 160),
    )
    check('the unlisted credential survives joinPool', store.refs.get('OPENCODE_GO_KEY_hidden') === KEY_C)
  }

  // ================= partial rename: new name written, old name not removed =================
  {
    globalThis.fetch = standardFetch
    const run = start({ OPENCODE_GO_KEY_src: KEY_B }, { keyNames: ['src', 'dst'] })
    store = run.store
    const primed = (await request(run.ctx, SNAP)).body
    check('the cache is primed with only the old name', primed.entries.length === 1 && primed.entries[0].name === 'src', JSON.stringify(primed.entries.map((e) => e.name)))
    const realUnset = store.unset.bind(store)
    store.unset = async (ref) => {
      if (ref === 'OPENCODE_GO_KEY_src') throw new Error('unset denied')
      return realUnset(ref)
    }
    const renamed = await request(run.ctx, ACCOUNT, { op: 'rename', name: 'src', to: 'dst' })
    check('a partial rename is not reported as clean success', renamed.body.ok === false, JSON.stringify(renamed.body).slice(0, 180))
    check('the partial result says which half completed', /新名已写入|部分完成/.test(renamed.body.error || ''), renamed.body.error)
    check('the partial result marks the credential state as changed', renamed.body.changed === true, JSON.stringify(renamed.body).slice(0, 180))
    check('the key remains reachable under the new name', store.refs.get('OPENCODE_GO_KEY_dst') === KEY_B)
    check('the old name is still there and reported', store.refs.get('OPENCODE_GO_KEY_src') === KEY_B)
    const afterPartial = (await request(run.ctx, SNAP)).body
    check(
      'a partial rename invalidates the snapshot cache',
      afterPartial.entries.length === 2 && afterPartial.entries.some((entry) => entry.name === 'dst'),
      JSON.stringify(afterPartial.entries.map((entry) => entry.name)),
    )
  }
} catch (error) {
  results.push('CRASH  ' + String((error && error.stack) || error))
}

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL') || r.startsWith('CRASH')).length
console.log('\n' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exit(failed === 0 ? 0 : 1)
