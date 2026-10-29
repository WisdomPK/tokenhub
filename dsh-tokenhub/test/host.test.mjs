// dsh-tokenhub — Host-half integration test.
//
// Drives the plugin through a stub context: an in-memory credential store, a
// stubbed usage endpoint, and captured web routes. No dependencies beyond Node:
//
//   node test/host.test.mjs

import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { EventEmitter } from 'node:events'
import { apply } from '../lib/index.js'

const results = []
const check = (label, cond, extra) => results.push((cond ? 'PASS  ' : 'FAIL  ') + label + (extra ? '   [' + extra + ']' : ''))

const dir = mkdtempSync(join(tmpdir(), 'tokenhub-'))
const credPath = join(dir, '.credentials.yaml')
const authPath = join(dir, 'auth.json')

const KEY_A = 'sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1'
const KEY_B = 'sk-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2'
const KEY_C = 'sk-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC3'

/** Write the credentials document in the shape the local store actually uses:
 *  references indented under the refs mapping. */
function writeCreds(pool, current, active) {
  const lines = ['version: 1', 'refs:', '  DEEPSEEK_API_KEY: sk-deepseek-placeholder', '  OPENCODE_GO_API_KEY: ' + current]
  if (active) lines.push('  OPENCODE_GO_KEY_ACTIVE: ' + active)
  for (const n of Object.keys(pool)) lines.push('  OPENCODE_GO_KEY_' + n + ': ' + pool[n])
  lines.push('records:', '  client-connection/browser-session:', '    kind: grant')
  writeFileSync(credPath, lines.join('\n') + '\n', 'utf8')
}

/** Stand-in for ctx.credentials, persisting to the same document the real
 *  local store writes, so the plugin's scan and the store stay in agreement. */
function makeStore() {
  const refs = new Map()
  const persist = () => {
    const lines = ['version: 1', 'refs:']
    for (const [ref, value] of refs) lines.push('  ' + ref + ': ' + value)
    lines.push('records:', '  client-connection/browser-session:', '    kind: grant')
    writeFileSync(credPath, lines.join('\n') + '\n', 'utf8')
  }
  return {
    refs,
    async resolve(ref) {
      const v = refs.get(ref)
      return v === undefined || v === '' ? undefined : { value: v, source: 'file' }
    },
    async describe(ref) {
      return { configured: refs.has(ref) && refs.get(ref) !== '', source: 'file', writable: true }
    },
    async set(ref, value) { refs.set(ref, value); persist() },
    async unset(ref) { refs.delete(ref); persist() },
  }
}

globalThis.fetch = async (url, init) => {
  const key = String((init && init.headers && init.headers.Authorization) || '').replace('Bearer ', '')
  if (key === KEY_C) return { status: 401, ok: false, async json() { return {} } }
  const n = key === KEY_A ? 10 : key === KEY_B ? 70 : 5
  return { status: 200, ok: true, async json() { return { usage: {
    rolling: { status: 'ok', percent: n, resetsAt: new Date(Date.now() + 3600e3).toISOString() },
    weekly: { status: 'ok', percent: n + 1, resetsAt: new Date(Date.now() + 5 * 86400e3).toISOString() },
    monthly: { status: 'ok', percent: n + 2, resetsAt: new Date(Date.now() + 20 * 86400e3).toISOString() },
  } } } }
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

/** Issue one request against a captured route, delivering the body on a later
 *  tick so readBody's listeners are already attached. */
function request(ctx, path, { method = 'GET', body, force = false } = {}) {
  return new Promise((resolve) => {
    const req = new EventEmitter()
    req.method = method
    req.url = force ? path + '?force=1' : path
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

const settle = () => new Promise((r) => setTimeout(r, 250))
const SNAP = '/plugins/dsh-tokenhub/snapshot'
const SWITCH = '/plugins/dsh-tokenhub/switch'
const ACCOUNT = '/plugins/dsh-tokenhub/account'

// ---- single key, no pool: the shape a fresh install starts in ----
writeCreds({}, KEY_A, null)
let store = makeStore()
store.refs.set('OPENCODE_GO_API_KEY', KEY_A)
let ctx = makeCtx(store)
apply(ctx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
let snap = (await request(ctx, SNAP)).body
check(
  'a lone key in force still shows as one row',
  snap.entries.length === 1 && snap.entries[0].id === 'current' && snap.entries[0].inPool === false && snap.entries[0].active === true,
)
check('entry is active', snap.entries[0].active === true)
check('key is masked and never returned', snap.entries[0].masked === KEY_A.slice(0, 6) + '\u2026' + KEY_A.slice(-4) && !JSON.stringify(snap).includes(KEY_A))
check('rolling parsed', snap.entries[0].windows.rolling.percent === 10)
check('thresholds echoed', snap.thresholds.warn === 60 && snap.thresholds.error === 85)
check('the snapshot carries the widget scale, defaulting to 2', snap.scale === 2, String(snap.scale))

// ---- pool discovered through the indented refs mapping ----
writeCreds({ go1: KEY_A, go2: KEY_B }, KEY_B, null)
store = makeStore()
store.refs.set('OPENCODE_GO_API_KEY', KEY_B)
store.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
store.refs.set('OPENCODE_GO_KEY_go2', KEY_B)
ctx = makeCtx(store)
apply(ctx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: true })
await settle()
snap = (await request(ctx, SNAP)).body
check('both pool entries found', snap.entries.length === 2 && snap.entries[0].name === 'go1' && snap.entries[1].name === 'go2')
check(
  'pool rows carry namespaced ids while names stay addressable',
  snap.entries[0].id === 'pool:go1' && snap.entries[0].name === 'go1' && snap.entries[1].id === 'pool:go2',
  JSON.stringify(snap.entries.map((e) => [e.id, e.name])),
)
check('active inferred from the key in force', snap.entries[1].active === true && snap.entries[0].active === false)
check('the ACTIVE marker is not treated as an entry', snap.entries.every((e) => e.name !== 'ACTIVE'))

// ---- the key in force is authoritative; a stale marker cannot move it ----
writeCreds({ go1: KEY_A, go2: KEY_B }, KEY_B, 'go1')
store.refs.set('OPENCODE_GO_API_KEY', KEY_B)
store.refs.set('OPENCODE_GO_KEY_ACTIVE', 'go1')
snap = (await request(ctx, SNAP, { force: true })).body
check(
  'the resolved key outranks a stale ACTIVE marker',
  snap.entries[0].active === false && snap.entries[1].active === true,
  JSON.stringify(snap.entries.map((e) => [e.name, e.active])),
)
check(
  'only one pool row is marked in force',
  snap.entries.filter((e) => e.active).length === 1,
  String(snap.entries.filter((e) => e.active).length),
)

// ---- two pool names for one key: the marker picks which alias is displayed ----
writeCreds({ go1: KEY_A, go2: KEY_A }, KEY_A, 'go2')
const aliasStore = makeStore()
aliasStore.refs.set('OPENCODE_GO_API_KEY', KEY_A)
aliasStore.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
aliasStore.refs.set('OPENCODE_GO_KEY_go2', KEY_A)
aliasStore.refs.set('OPENCODE_GO_KEY_ACTIVE', 'go2')
const aliasCtx = makeCtx(aliasStore)
apply(aliasCtx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
let aliasSnap = (await request(aliasCtx, SNAP, { force: true })).body
check(
  'aliases of one key: the marker chooses the displayed name',
  aliasSnap.entries.filter((e) => e.name === 'go2' && e.active).length === 1 &&
    aliasSnap.entries.filter((e) => e.name === 'go1' && e.active).length === 0,
  JSON.stringify(aliasSnap.entries.map((e) => [e.name, e.active])),
)
check('a duplicated key still produces exactly one active row', aliasSnap.entries.filter((e) => e.active).length === 1)

// ---- no key in force: nothing is marked active ----
writeCreds({ go1: KEY_A, go2: KEY_B }, '', 'go1')
const noCurrentStore = makeStore()
noCurrentStore.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
noCurrentStore.refs.set('OPENCODE_GO_KEY_go2', KEY_B)
noCurrentStore.refs.set('OPENCODE_GO_KEY_ACTIVE', 'go1')
const noCurrentCtx = makeCtx(noCurrentStore)
apply(noCurrentCtx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
const noCurrentSnap = (await request(noCurrentCtx, SNAP, { force: true })).body
check(
  'with no key in force no row is active, even with a marker present',
  noCurrentSnap.entries.length === 2 && noCurrentSnap.entries.every((e) => e.active === false),
  JSON.stringify(noCurrentSnap.entries.map((e) => [e.name, e.active])),
)

// ---- switch ----
writeFileSync(authPath, JSON.stringify({ 'opencode-go': { type: 'api', key: KEY_B }, other: { keep: 1 } }, null, 2), 'utf8')
const sw = (await request(ctx, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
check('switch succeeds', sw.ok === true, JSON.stringify(sw).slice(0, 160))
check('store now resolves the target key', store.refs.get('OPENCODE_GO_API_KEY') === KEY_A)
check('ACTIVE marker updated', store.refs.get('OPENCODE_GO_KEY_ACTIVE') === 'go1')
check('message promises next-request effect, not a restart', /下一个请求即生效/.test(sw.message || ''), sw.message)
const auth = JSON.parse(readFileSync(authPath, 'utf8'))
check('opencode auth.json carries the new key', auth['opencode-go'].key === KEY_A)
check('auth.json kept unrelated fields', auth.other && auth.other.keep === 1)
check('auth.json backup written', readdirSync(dir).some((f) => f.indexOf('auth.json.tokenhub.bak-') === 0))
check(
  'the opt-in message says opencode needs a restart',
  /opencode/.test(sw.message || '') && /重启/.test(sw.message || ''),
  sw.message,
)
check('snapshot reflects the switch', (await request(ctx, SNAP, { force: true })).body.entries[0].active === true)

// ---- the switch itself is authoritative even when the marker write fails ----
writeCreds({ go1: KEY_A, go2: KEY_B }, KEY_B, 'go2')
const markerStore = makeStore()
markerStore.refs.set('OPENCODE_GO_API_KEY', KEY_B)
markerStore.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
markerStore.refs.set('OPENCODE_GO_KEY_go2', KEY_B)
markerStore.refs.set('OPENCODE_GO_KEY_ACTIVE', 'go2')
const realMarkerSet = markerStore.set.bind(markerStore)
markerStore.set = async (ref, value) => {
  if (ref === 'OPENCODE_GO_KEY_ACTIVE') throw new Error('EACCES: marker write denied')
  return realMarkerSet(ref, value)
}
const markerCtx = makeCtx(markerStore)
apply(markerCtx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
const markerSwitch = (await request(markerCtx, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
check(
  'a marker write failure does not undo a verified switch',
  markerSwitch.ok === true && markerStore.refs.get('OPENCODE_GO_API_KEY') === KEY_A,
  JSON.stringify(markerSwitch).slice(0, 180),
)
check('the marker failure is reported', /标记/.test(markerSwitch.message || ''), markerSwitch.message)
const markerSnap = (await request(markerCtx, SNAP, { force: true })).body
check(
  'identity follows the resolved key, not the failed marker',
  markerSnap.entries.filter((e) => e.active).length === 1 && markerSnap.entries.find((e) => e.active).name === 'go1',
  JSON.stringify(markerSnap.entries.map((e) => [e.name, e.active])),
)

// ---- add ----
check('rejects a malformed key', (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go9', key: 'nope' } })).body.ok === false)
const dead = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go9', key: KEY_C } })).body
check('rejects a key the endpoint refuses', dead.ok === false && /密钥无效/.test(dead.error || ''), dead.error)
const dup = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go2', key: KEY_B } })).body
check('rejects a duplicate name', dup.ok === false && /已存在/.test(dup.error || ''), dup.error)
const added = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go3', key: KEY_A } })).body
check('adds a validated account', added.ok === true, JSON.stringify(added).slice(0, 140))
const auto = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'add', key: KEY_A } })).body
check('blank name generates a default', auto.ok === true && /^go\d+$/.test(auto.name || ''), JSON.stringify(auto).slice(0, 140))

// ---- remove ----
const rmActive = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'remove', name: 'go1' } })).body
check('refuses to remove the account in force', rmActive.ok === false && /使用中/.test(rmActive.error || ''), rmActive.error)
const rmOther = (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'remove', name: 'go2' } })).body
check('removes a spare account', rmOther.ok === true && !store.refs.has('OPENCODE_GO_KEY_go2'))

// ---- guards ----
const cross = await new Promise((resolve) => {
  const req = new EventEmitter()
  req.method = 'POST'
  req.url = '/x'
  req.headers = { host: '127.0.0.1:3000', origin: 'http://evil.example' }
  req.destroy = () => {}
  ctx.routes.get(SWITCH)(req, res0(resolve))
})
function res0(resolve) {
  return { writeHead(s) { this._status = s }, end(p) { resolve({ status: this._status, body: JSON.parse(p) }) } }
}
check('cross-origin write refused', cross.status === 403, String(cross.status))
check('unknown account op refused', (await request(ctx, ACCOUNT, { method: 'POST', body: { op: 'nope' } })).status === 400)



// ---- the key in force is not in the pool: it still owns a row ----
// This is the state the panel gets stuck in without a synthetic row: the pool
// is non-empty, nothing matches the key in force, and no row carries Add.
writeCreds({ Google: KEY_B }, KEY_A, null)
const s3 = makeStore()
s3.refs.set('OPENCODE_GO_API_KEY', KEY_A)
s3.refs.set('OPENCODE_GO_KEY_Google', KEY_B)
s3.refs.set('OPENCODE_GO_KEY_ACTIVE', 'Google')
const c3 = makeCtx(s3)
apply(c3, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
let snap3 = (await request(c3, SNAP)).body
check(
  'the key in force gets a row even with no pool entry behind it',
  snap3.entries.length === 2 && snap3.entries[0].active === true && snap3.entries[0].inPool === false,
  JSON.stringify(snap3.entries.map((e) => [e.name, e.active, e.inPool])),
)
check('that row carries an id for the client to key on', snap3.entries[0].id === 'current')
check('a row is in force, so Add stays reachable', snap3.entries.some((e) => e.active) === true)
check(
  'the pool entry is listed as a spare even when a stale marker names it',
  snap3.entries[1].name === 'Google' && snap3.entries[1].active === false && snap3.entries[1].inPool === true,
)
check(
  'a key outside the pool leaves exactly one active row',
  snap3.entries.filter((e) => e.active).length === 1,
  String(snap3.entries.filter((e) => e.active).length),
)

// ---- the old synthetic id is a legal pool name, not a collision ----
writeCreds({ __current__: KEY_B }, KEY_A, null)
const legacyStore = makeStore()
legacyStore.refs.set('OPENCODE_GO_API_KEY', KEY_A)
legacyStore.refs.set('OPENCODE_GO_KEY___current__', KEY_B)
const legacyCtx = makeCtx(legacyStore)
apply(legacyCtx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
const legacySnap = (await request(legacyCtx, SNAP, { force: true })).body
check(
  'a pool name __current__ survives beside the synthetic row',
  legacySnap.entries.length === 2 &&
    legacySnap.entries[0].id === 'current' &&
    legacySnap.entries[1].id === 'pool:__current__' &&
    legacySnap.entries[1].name === '__current__',
  JSON.stringify(legacySnap.entries.map((e) => [e.id, e.name])),
)

// joining files the key in force, so a later switch cannot lose it
const joined = (await request(c3, ACCOUNT, { method: 'POST', body: { op: 'joinPool', name: 'Main' } })).body
check('joinPool files the key in force', joined.ok === true && s3.refs.get('OPENCODE_GO_KEY_Main') === KEY_A, JSON.stringify(joined).slice(0, 160))
check('joinPool marks the filed account active', s3.refs.get('OPENCODE_GO_KEY_ACTIVE') === 'Main')
snap3 = (await request(c3, SNAP, { force: true })).body
check('after joining, the key in force is an ordinary pool row', snap3.entries.some((e) => e.name === 'Main' && e.active === true && e.inPool === true))
check('the synthetic row is gone once a pool entry backs it', !snap3.entries.some((e) => e.inPool === false))
const dupJoin = (await request(c3, ACCOUNT, { method: 'POST', body: { op: 'joinPool', name: 'Main' } })).body
check('joinPool refuses a name already in the pool', dupJoin.ok === false && /已存在/.test(dupJoin.error || ''), dupJoin.error)
const badJoin = (await request(c3, ACCOUNT, { method: 'POST', body: { op: 'joinPool', name: 'has space' } })).body
check('joinPool refuses an unusable name', badJoin.ok === false)


// ---- an account is named whatever the user typed ----
// The plugin invents a name in exactly one place — a blank field yields goN.
// Nothing else anywhere assumes what an account is called.
writeCreds({}, KEY_A, null)
const sn = makeStore()
sn.refs.set('OPENCODE_GO_API_KEY', KEY_A)
const cn = makeCtx(sn)
apply(cn, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
const typedNames = ['Zebra', 'a_b_c', 'X1', 'work', 'BACKUP_2026']
const keyFor = (accountName) => 'sk-' + (accountName + 'X'.repeat(60)).slice(0, 60)
for (const accountName of typedNames) {
  const res = (await request(cn, ACCOUNT, { method: 'POST', body: { op: 'add', name: accountName, key: keyFor(accountName) } })).body
  check('add keeps the name the user typed: ' + accountName, res.ok === true && res.name === accountName, JSON.stringify(res).slice(0, 120))
}
const snapN = (await request(cn, SNAP, { force: true })).body
check(
  'every typed name reaches the snapshot unchanged',
  typedNames.every((n) => snapN.entries.some((e) => e.name === n)),
  JSON.stringify(snapN.entries.map((e) => e.name)),
)
check(
  'no account carries a name nobody typed',
  snapN.entries.every((e) => e.name === '' || typedNames.includes(e.name)),
  JSON.stringify(snapN.entries.map((e) => e.name)),
)
const blankName = (await request(cn, ACCOUNT, { method: 'POST', body: { op: 'add', key: keyFor('blank') } })).body
check('a blank name is the only invented one, and it is goN', blankName.ok === true && /^go\d+$/.test(blankName.name || ''), JSON.stringify(blankName).slice(0, 120))


// ---- rename ----
// Renaming changes only the name a key is filed under: the key never moves, and
// whichever account is in force stays in force.
writeCreds({ Zebra: KEY_A, Google: KEY_B }, KEY_B, 'Google')
const sr = makeStore()
sr.refs.set('OPENCODE_GO_API_KEY', KEY_B)
sr.refs.set('OPENCODE_GO_KEY_Zebra', KEY_A)
sr.refs.set('OPENCODE_GO_KEY_Google', KEY_B)
sr.refs.set('OPENCODE_GO_KEY_ACTIVE', 'Google')
const cr = makeCtx(sr)
apply(cr, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()

const r1 = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'Zebra', to: 'backup' } })).body
check('rename files a spare under the new name', r1.ok === true && sr.refs.get('OPENCODE_GO_KEY_backup') === KEY_A, JSON.stringify(r1).slice(0, 160))
check('rename drops the old name', !sr.refs.has('OPENCODE_GO_KEY_Zebra'))
check('rename leaves the key in force alone', sr.refs.get('OPENCODE_GO_API_KEY') === KEY_B)

const r2 = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'Google', to: 'main' } })).body
check('rename of the account in force succeeds', r2.ok === true, JSON.stringify(r2).slice(0, 160))
check('the ACTIVE marker follows the rename', sr.refs.get('OPENCODE_GO_KEY_ACTIVE') === 'main')
check('the account in force is still in force after renaming', sr.refs.get('OPENCODE_GO_API_KEY') === KEY_B)
const snapR = (await request(cr, SNAP, { force: true })).body
check('the renamed account is still the active row', snapR.entries.some((e) => e.name === 'main' && e.active === true), JSON.stringify(snapR.entries.map((e) => [e.name, e.active])))

const dupRename = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'backup', to: 'main' } })).body
check('rename refuses a name already taken', dupRename.ok === false && /已存在/.test(dupRename.error || ''), dupRename.error)
const goneRename = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'nope', to: 'other' } })).body
check('rename refuses a name that is not in the pool', goneRename.ok === false && /不存在/.test(goneRename.error || ''), goneRename.error)
const badRename = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'backup', to: 'has space' } })).body
check('rename refuses an unusable new name', badRename.ok === false)
const sameRename = (await request(cr, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'backup', to: 'backup' } })).body
check('renaming to the same name is a no-op', sameRename.ok === true && sr.refs.has('OPENCODE_GO_KEY_backup'))


// ---- ACTIVE is system metadata, never an account name ----
writeCreds({ go1: KEY_B }, KEY_A, null)
const reservedStore = makeStore()
reservedStore.refs.set('OPENCODE_GO_API_KEY', KEY_A)
reservedStore.refs.set('OPENCODE_GO_KEY_go1', KEY_B)
const reservedCtx = makeCtx(reservedStore)
apply(reservedCtx, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false })
await settle()
const refsJson = () => JSON.stringify(Array.from(reservedStore.refs.entries()).sort())
const reservedBefore = refsJson()
const reservedAdd = (await request(reservedCtx, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'ACTIVE', key: KEY_A } })).body
check('add refuses the reserved name ACTIVE', reservedAdd.ok === false && /ACTIVE/.test(reservedAdd.error || ''), reservedAdd.error)
check('a refused add leaves the credential store untouched', refsJson() === reservedBefore)
const reservedJoin = (await request(reservedCtx, ACCOUNT, { method: 'POST', body: { op: 'joinPool', name: 'ACTIVE' } })).body
check('joinPool refuses the reserved name ACTIVE', reservedJoin.ok === false && /ACTIVE/.test(reservedJoin.error || ''), reservedJoin.error)
check('a refused joinPool leaves the credential store untouched', refsJson() === reservedBefore)
const reservedRename = (await request(reservedCtx, ACCOUNT, { method: 'POST', body: { op: 'rename', name: 'go1', to: 'ACTIVE' } })).body
check('rename refuses the reserved name ACTIVE', reservedRename.ok === false && /ACTIVE/.test(reservedRename.error || ''), reservedRename.error)
check('a refused rename leaves the credential store untouched', refsJson() === reservedBefore)


// ---- by default a dsh switch leaves opencode alone ----
// The two harnesses apply a credential change on different schedules: dsh per
// request, opencode at startup. Writing opencode's file by default would leave
// them on different accounts until the next opencode restart, so it is opt-in.
const authOff = join(dir, 'auth-off.json')
writeFileSync(authOff, JSON.stringify({ 'opencode-go': { type: 'api', key: KEY_B }, other: { keep: 1 } }, null, 2), 'utf8')
const authOffBefore = readFileSync(authOff, 'utf8')
writeCreds({ go1: KEY_A, go2: KEY_B }, KEY_B, 'go2')
const sOff = makeStore()
sOff.refs.set('OPENCODE_GO_API_KEY', KEY_B)
sOff.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
sOff.refs.set('OPENCODE_GO_KEY_go2', KEY_B)
sOff.refs.set('OPENCODE_GO_KEY_ACTIVE', 'go2')
const cOff = makeCtx(sOff)
apply(cOff, { credentialsPath: credPath, opencodeAuthPath: authOff })
await settle()
const swOff = (await request(cOff, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
check('a switch still succeeds with syncing off by default', swOff.ok === true, JSON.stringify(swOff).slice(0, 170))
check('the dsh credential still changed', sOff.refs.get('OPENCODE_GO_API_KEY') === KEY_A)
check('opencode auth.json is left byte-identical by default', readFileSync(authOff, 'utf8') === authOffBefore)
check('no opencode backup is taken by default', !readdirSync(dir).some((f) => f.startsWith('auth-off.json.tokenhub.bak-')))
check('the default message does not mention opencode', !/opencode/i.test(swOff.message || ''), swOff.message)
check('the response reports syncing as disabled', Boolean(swOff.opencodeAuth) && swOff.opencodeAuth.enabled === false, JSON.stringify(swOff.opencodeAuth))

// ---- the widget scale is configurable ----
writeCreds({}, KEY_A, null)
const ss = makeStore()
ss.refs.set('OPENCODE_GO_API_KEY', KEY_A)
const cs = makeCtx(ss)
apply(cs, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false, scale: 1.5 })
await settle()
check('a configured scale reaches the snapshot', (await request(cs, SNAP)).body.scale === 1.5, String((await request(cs, SNAP)).body.scale))
const csBad = makeCtx(makeStore())
csBad.credentials.refs.set('OPENCODE_GO_API_KEY', KEY_A)
apply(csBad, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false, scale: -3 })
await settle()
check('an unusable scale falls back to the default', (await request(csBad, SNAP)).body.scale === 2)

// ---- credential write-lock recovery ----
// atomic-write cannot take over a lock whose record is incomplete, so a writer
// killed between creating the lock and recording its PID wedges the file. These
// cases pin both halves: the repair happens, and it stays narrow.
const lockPath = credPath + '.lock'
const lockMessage = (target) => 'atomic-write: timed out waiting for the writer lock at ' + target
const ageLock = (ms) => {
  const when = new Date(Date.now() - ms)
  utimesSync(lockPath, when, when)
}

/** A context whose credential store fails the first set() with failure(). */
function lockingCtx(failure, config) {
  writeCreds({ go1: KEY_A }, KEY_A, 'go1')
  const store = makeStore()
  store.refs.set('OPENCODE_GO_API_KEY', KEY_A)
  store.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
  const realSet = store.set.bind(store)
  let armed = true
  store.set = async (ref, value) => {
    if (armed) {
      armed = false
      throw failure()
    }
    return realSet(ref, value)
  }
  const c = makeCtx(store)
  apply(c, Object.assign({ credentialsPath: credPath, opencodeAuthPath: authPath }, config || {}))
  return { ctx: c, store }
}
const addGo7 = (c) => request(c, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go7', key: KEY_A } })

// a stale incomplete lock is provably unusable: repair it and retry
writeFileSync(lockPath, '', 'utf8')
ageLock(10 * 60 * 1000)
let lc = lockingCtx(() => new Error(lockMessage(lockPath)))
let lr = (await addGo7(lc.ctx)).body
check('a stale incomplete lock is released and the write retried', lr.ok === true && lc.store.refs.has('OPENCODE_GO_KEY_go7'), JSON.stringify(lr).slice(0, 220))
check('the repair is reported, not silent', /已清理残留写锁/.test(lr.message || ''), lr.message)
check('the repaired lock file is gone', !existsSync(lockPath))
rmSync(lockPath, { force: true })

// a lock whose holder recorded itself belongs to atomic-write: never touch it
writeFileSync(lockPath, String(process.pid) + '\n', 'utf8')
ageLock(10 * 60 * 1000)
lc = lockingCtx(() => new Error(lockMessage(lockPath)))
lr = (await addGo7(lc.ctx)).body
check('a lock carrying a PID record is never removed', lr.ok === false && existsSync(lockPath), JSON.stringify(lr).slice(0, 160))
check('the failure names the blocking lock', /写锁阻塞/.test(lr.error || ''), lr.error)
rmSync(lockPath, { force: true })

// a fresh empty lock may be a writer mid-create: wait, never delete
writeFileSync(lockPath, '', 'utf8')
lc = lockingCtx(() => new Error(lockMessage(lockPath)))
lr = (await addGo7(lc.ctx)).body
check('a fresh empty lock is left alone', lr.ok === false && existsSync(lockPath), JSON.stringify(lr).slice(0, 160))
rmSync(lockPath, { force: true })

// the repair is opt-out
writeFileSync(lockPath, '', 'utf8')
ageLock(10 * 60 * 1000)
lc = lockingCtx(() => new Error(lockMessage(lockPath)), { autoReleaseWedgedLock: false })
lr = (await addGo7(lc.ctx)).body
check('the repair can be turned off by config', lr.ok === false && existsSync(lockPath), JSON.stringify(lr).slice(0, 160))
rmSync(lockPath, { force: true })

// an unrelated write failure passes through verbatim
lc = lockingCtx(() => new Error('EACCES: permission denied, open the credentials file'))
lr = (await addGo7(lc.ctx)).body
check('an unrelated write failure is surfaced verbatim', lr.ok === false && /EACCES/.test(lr.error || ''), lr.error)

// ---- duplicate explicit key names produce one row per name ----
{
  const st = makeStore()
  st.refs.set('OPENCODE_GO_API_KEY', KEY_B)
  st.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
  st.refs.set('OPENCODE_GO_KEY_go2', KEY_B)
  const c = makeCtx(st)
  apply(c, {
    credentialsPath: credPath,
    opencodeAuthPath: authPath,
    syncOpencodeAuth: false,
    keyNames: ['go1', 'go1', 'go2', 'go2'],
  })
  await settle()
  const dupSnap = (await request(c, SNAP, { force: true })).body
  check(
    'duplicate explicit names are collapsed into one row each',
    dupSnap.entries.length === 2 && new Set(dupSnap.entries.map((entry) => entry.id)).size === 2,
    JSON.stringify(dupSnap.entries.map((entry) => [entry.id, entry.name])),
  )
  check(
    'duplicate explicit names never produce two active rows',
    dupSnap.entries.filter((entry) => entry.active).length === 1,
    String(dupSnap.entries.filter((entry) => entry.active).length),
  )
}

// ---- a failed window keeps its last reading and is marked stale on the wire ----
{
  const liveFetch = globalThis.fetch
  let mode = 'ok'
  globalThis.fetch = async () => {
    if (mode === 'fail') return { status: 500, ok: false, async json() { return {} } }
    return {
      status: 200,
      ok: true,
      async json() {
        return { usage: {
          rolling: { status: 'ok', percent: 10, resetsAt: '2027-01-01T00:00:00Z' },
          weekly: { status: 'ok', percent: 20, resetsAt: '2027-01-02T00:00:00Z' },
          monthly: { status: 'ok', percent: 30, resetsAt: '2027-01-03T00:00:00Z' },
        } }
      },
    }
  }
  const st = makeStore()
  st.refs.set('OPENCODE_GO_API_KEY', KEY_A)
  const c = makeCtx(st)
  apply(c, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false, keyNames: [] })
  await settle()
  const fresh = (await request(c, SNAP, { force: true })).body
  check('a fresh host window starts non-stale', fresh.entries[0].windows.rolling.stale === false && typeof fresh.entries[0].windows.rolling.lastSuccessAt === 'number')
  mode = 'fail'
  const stale = (await request(c, SNAP, { force: true })).body
  check(
    'a failed host window keeps the previous percentage and marks it stale',
    stale.entries[0].windows.rolling.percent === 10 &&
      stale.entries[0].windows.rolling.stale === true &&
      stale.entries[0].windows.rolling.error === 'http-500',
    JSON.stringify(stale.entries[0].windows.rolling),
  )
  check(
    'the stale host window still names the last success time',
    stale.entries[0].windows.rolling.lastSuccessAt === fresh.entries[0].windows.rolling.lastSuccessAt,
    JSON.stringify([fresh.entries[0].windows.rolling.lastSuccessAt, stale.entries[0].windows.rolling.lastSuccessAt]),
  )
  globalThis.fetch = liveFetch
}

// ---- storage failures are not an empty pool ----
{
  const st = makeStore()
  st.refs.set('OPENCODE_GO_API_KEY', KEY_A)
  st.refs.set('OPENCODE_GO_KEY_go1', KEY_A)
  const realResolve = st.resolve.bind(st)
  st.resolve = async (ref) => {
    if (ref === 'OPENCODE_GO_API_KEY') throw new Error('disk read failed')
    return realResolve(ref)
  }
  const c = makeCtx(st)
  const originalError = console.error
  const logs = []
  console.error = (...args) => logs.push(args.map(String).join(' '))
  apply(c, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false, keyNames: ['go1'] })
  await settle()
  const failedSnapResponse = await request(c, SNAP, { force: true })
  const failedSnap = failedSnapResponse.body
  check(
    'a storage read failure is an internal snapshot error, not no-keys',
    failedSnapResponse.status === 500 && /^internal:/.test(failedSnap.error || '') && failedSnap.error !== 'no-keys',
    failedSnapResponse.status + ' ' + String(failedSnap.error),
  )
  check('the snapshot failure is logged', logs.some((line) => /snapshot refresh failed/.test(line)), logs.join(' | ').slice(0, 180))
  const failedSwitch = await Promise.race([
    request(c, SWITCH, { method: 'POST', body: { name: 'go1' } }),
    new Promise((resolve) => setTimeout(() => resolve({ status: -2, body: { error: 'no response' } }), 500)),
  ])
  check(
    'a mutation storage failure returns an explicit 500',
    failedSwitch.status === 500 && /凭据存储读取失败/.test(failedSwitch.body.error || ''),
    failedSwitch.status + ' ' + String(failedSwitch.body.error),
  )
  console.error = originalError
}
{
  const st = makeStore()
  st.refs.set('OPENCODE_GO_API_KEY', KEY_A)
  const realDescribe = st.describe.bind(st)
  st.describe = async (ref) => {
    if (ref === 'OPENCODE_GO_KEY_go9') throw new Error('describe EIO')
    return realDescribe(ref)
  }
  const c = makeCtx(st)
  const originalError = console.error
  console.error = () => {}
  apply(c, { credentialsPath: credPath, opencodeAuthPath: authPath, syncOpencodeAuth: false, keyNames: [] })
  await settle()
  const failedAdd = await Promise.race([
    request(c, ACCOUNT, { method: 'POST', body: { op: 'add', name: 'go9', key: KEY_A } }),
    new Promise((resolve) => setTimeout(() => resolve({ status: -2, body: { error: 'no response' } }), 500)),
  ])
  console.error = originalError
  check(
    'a describe failure is not treated as an available name',
    failedAdd.status === 500 && /凭据存储读取失败/.test(failedAdd.body.error || ''),
    failedAdd.status + ' ' + String(failedAdd.body.error),
  )
}

// ---- an explicit auth path is the only target ----
// The review found the old resolver treating an explicit path as a candidate:
// a missing or corrupt override silently moved the write to another file.
const writeAuth = (target, key, extra) =>
  writeFileSync(target, JSON.stringify(Object.assign({ 'opencode-go': { type: 'api', key } }, extra || {}), null, 2), 'utf8')
const authStoreFor = (current, pool) => {
  const s = makeStore()
  s.refs.set('OPENCODE_GO_API_KEY', current)
  for (const [name, key] of Object.entries(pool)) s.refs.set('OPENCODE_GO_KEY_' + name, key)
  return s
}
const authCase = (store, override) => {
  const c = makeCtx(store)
  const names = Array.from(store.refs.keys())
    .filter((entry) => entry.startsWith('OPENCODE_GO_KEY_'))
    .map((entry) => entry.slice('OPENCODE_GO_KEY_'.length))
  apply(c, {
    credentialsPath: credPath,
    opencodeAuthPath: override,
    syncOpencodeAuth: true,
    keyNames: names,
  })
  return c
}
const savedAuthEnv = process.env.OPENCODE_AUTH_PATH
const savedXdg = process.env.XDG_DATA_HOME
const savedRealDate = globalThis.Date
try {
  const explicitPath = join(dir, 'explicit-auth.json')
  const envPath = join(dir, 'env-auth.json')
  rmSync(explicitPath, { force: true })

  // override missing, the environment candidate exists and carries opencode-go
  writeAuth(envPath, KEY_B, { other: { keep: 'env' } })
  const envBefore = readFileSync(envPath)
  process.env.OPENCODE_AUTH_PATH = envPath
  let authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  let authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const missingOverrideSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  check('a missing explicit target is created instead of falling back', existsSync(explicitPath), String(existsSync(explicitPath)))
  if (existsSync(explicitPath)) {
    check('the explicit target received the key', JSON.parse(readFileSync(explicitPath, 'utf8'))['opencode-go'].key === KEY_A)
  }
  check('the environment candidate is byte-identical', readFileSync(envPath).equals(envBefore))
  check('the switch itself succeeded', missingOverrideSwitch.ok === true, JSON.stringify(missingOverrideSwitch).slice(0, 160))

  // override missing, an auto-discovered default exists
  const xdgHome = join(dir, 'xdg-home')
  mkdirSync(join(xdgHome, 'opencode'), { recursive: true })
  const xdgAuth = join(xdgHome, 'opencode', 'auth.json')
  writeAuth(xdgAuth, KEY_B, { other: { keep: 'xdg' } })
  const xdgBefore = readFileSync(xdgAuth)
  rmSync(explicitPath, { force: true })
  process.env.OPENCODE_AUTH_PATH = ''
  process.env.XDG_DATA_HOME = xdgHome
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const autoMissingSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  check('a missing explicit target is created even when the default exists', existsSync(explicitPath), String(existsSync(explicitPath)))
  check('the auto-discovered default is untouched', readFileSync(xdgAuth).equals(xdgBefore))
  check('creating the explicit target still switches dsh', autoMissingSwitch.ok === true, JSON.stringify(autoMissingSwitch).slice(0, 160))

  // override corrupt, an auto-discovered default is valid
  writeFileSync(explicitPath, '{ not json', 'utf8')
  const corruptAutoBefore = readFileSync(explicitPath)
  const xdgBefore2 = readFileSync(xdgAuth)
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const corruptAutoSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  check(
    'a corrupt explicit target is reported even when the default is valid',
    corruptAutoSwitch.ok === true && corruptAutoSwitch.warnings.some((w) => w.code === 'opencode-auth'),
    JSON.stringify(corruptAutoSwitch.warnings),
  )
  check('the corrupt explicit file stays untouched', readFileSync(explicitPath).equals(corruptAutoBefore))
  check('the valid auto-discovered default stays untouched', readFileSync(xdgAuth).equals(xdgBefore2))
  process.env.OPENCODE_AUTH_PATH = envPath
  delete process.env.XDG_DATA_HOME

  // override corrupt, the environment candidate is valid
  writeFileSync(explicitPath, '{ not json', 'utf8')
  const corruptBefore = readFileSync(explicitPath)
  const envBefore2 = readFileSync(envPath)
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const corruptSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  check('a corrupt explicit target is reported, not bypassed', corruptSwitch.ok === true && Array.isArray(corruptSwitch.warnings) && corruptSwitch.warnings.some((w) => w.code === 'opencode-auth'), JSON.stringify(corruptSwitch.warnings))
  check('the corrupt explicit file is untouched', readFileSync(explicitPath).equals(corruptBefore))
  check('the valid environment file is untouched', readFileSync(envPath).equals(envBefore2))
  process.env.OPENCODE_AUTH_PATH = savedAuthEnv
  if (savedAuthEnv === undefined) delete process.env.OPENCODE_AUTH_PATH

  // BOM in the target must not stop the sync; unrelated fields survive
  writeFileSync(explicitPath, '\uFEFF' + JSON.stringify({ 'opencode-go': { type: 'api', key: KEY_B }, other: { keep: 3 } }, null, 2), 'utf8')
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const bomSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  const bomDoc = JSON.parse(readFileSync(explicitPath, 'utf8').replace(/^\uFEFF/, ''))
  check('a BOM-prefixed auth file is readable and writable', bomSwitch.ok === true && bomSwitch.opencodeAuth && bomSwitch.opencodeAuth.ok === true, JSON.stringify(bomSwitch.opencodeAuth))
  check('the BOM file keeps unrelated fields and receives the key', bomDoc['opencode-go'].key === KEY_A && bomDoc.other.keep === 3)

  // backup failure must not write the target
  const originalDoc = readFileSync(explicitPath)
  const RealDate = globalThis.Date
  const fixedWhen = new RealDate('2030-05-06T07:08:09.010Z')
  globalThis.Date = class extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(fixedWhen.getTime())
      else super(...args)
    }
    static now() { return fixedWhen.getTime() }
  }
  const backupDir = explicitPath + '.tokenhub.bak-' + fixedWhen.toISOString().replace(/[:.]/g, '-')
  mkdirSync(backupDir, { recursive: true })
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const backupFailSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  check('a failed backup is reported, not hidden', backupFailSwitch.ok === true && backupFailSwitch.warnings.some((w) => w.code === 'opencode-auth'), JSON.stringify(backupFailSwitch.warnings))
  check('a failed backup leaves the auth file untouched', readFileSync(explicitPath).equals(originalDoc))
  rmSync(backupDir, { recursive: true, force: true })

  // a temporary-file cleanup failure is logged, not silently swallowed
  {
    const cleanupPath = join(dir, 'cleanup-auth.json')
    writeAuth(cleanupPath, KEY_B, { other: { keep: 'cleanup' } })
    const cleanupBefore = readFileSync(cleanupPath)
    const cleanupWhen = new RealDate('2032-06-07T08:09:10.011Z')
    globalThis.Date = class extends RealDate {
      constructor(...args) {
        if (args.length === 0) super(cleanupWhen.getTime())
        else super(...args)
      }
      static now() { return cleanupWhen.getTime() }
    }
    const tmpPath = cleanupPath + '.tokenhub.tmp-' + process.pid + '-' + cleanupWhen.getTime()
    mkdirSync(tmpPath, { recursive: true })
    const cleanupLogs = []
    const originalWarn = console.warn
    console.warn = (...args) => cleanupLogs.push(args.map(String).join(' '))
    const cleanupStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
    const cleanupCtx = authCase(cleanupStore, cleanupPath)
    await settle()
    const cleanupSwitch = (await request(cleanupCtx, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
    console.warn = originalWarn
    globalThis.Date = RealDate
    check('a temp cleanup failure is reported as an auth warning', cleanupSwitch.ok === true && cleanupSwitch.warnings.some((w) => w.code === 'opencode-auth'), JSON.stringify(cleanupSwitch.warnings))
    check('a temp cleanup failure names the file it could not remove', cleanupLogs.some((line) => line.includes(tmpPath)), cleanupLogs.join(' | ').slice(0, 220))
    check('a temp cleanup failure does not claim the auth file was written', readFileSync(cleanupPath).equals(cleanupBefore))
    rmSync(tmpPath, { recursive: true, force: true })
  }

  // a failed replacement leaves the original intact
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, explicitPath)
  await settle()
  const beforeReplace = readFileSync(explicitPath)
  chmodSync(explicitPath, 0o444)
  const replaceFailSwitch = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: 'go1' } })).body
  chmodSync(explicitPath, 0o666)
  check('a failed replacement is reported, not hidden', replaceFailSwitch.ok === true && replaceFailSwitch.warnings.some((w) => w.code === 'opencode-auth'), JSON.stringify(replaceFailSwitch.warnings))
  check('a failed replacement preserves the original bytes', readFileSync(explicitPath).equals(beforeReplace))
  globalThis.Date = RealDate

  // only the newest five backups survive
  const retainPath = join(dir, 'retain-auth.json')
  writeAuth(retainPath, KEY_B)
  authStore = authStoreFor(KEY_B, { go1: KEY_A, go2: KEY_B })
  authCtx2 = authCase(authStore, retainPath)
  await settle()
  for (let i = 0; i < 7; i += 1) {
    const when = new RealDate(Date.UTC(2031, 0, 1, 0, 0, i))
    globalThis.Date = class extends RealDate {
      constructor(...args) {
        if (args.length === 0) super(when.getTime())
        else super(...args)
      }
      static now() { return when.getTime() }
    }
    const target = i % 2 === 0 ? 'go1' : 'go2'
    const result = (await request(authCtx2, SWITCH, { method: 'POST', body: { name: target } })).body
    check('retention switch ' + (i + 1) + ' succeeded', result.ok === true, JSON.stringify(result).slice(0, 120))
  }
  globalThis.Date = RealDate
  const backups = readdirSync(dir).filter((f) => f.startsWith('retain-auth.json.tokenhub.bak-'))
  check('the newest five backups are kept', backups.length === 5, String(backups.length))
} finally {
  process.env.OPENCODE_AUTH_PATH = savedAuthEnv
  if (savedAuthEnv === undefined) delete process.env.OPENCODE_AUTH_PATH
  process.env.XDG_DATA_HOME = savedXdg
  if (savedXdg === undefined) delete process.env.XDG_DATA_HOME
  globalThis.Date = savedRealDate
}

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL')).length
console.log('\n' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exit(failed === 0 ? 0 : 1)
