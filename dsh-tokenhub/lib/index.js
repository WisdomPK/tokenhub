/**
 * dsh-tokenhub — Host half.
 * ------------------------------------------------------------------
 * OpenCode Go quota for DeepSeek Harness: reads every key in the pool from the
 * credential store, polls the official usage endpoint, and switches the active
 * key on request.
 *
 * This file is the assembly layer: plugin contract, config normalization,
 * routes, and composition of the account, usage, auth, and snapshot services.
 * Key values never leave this half. The web routes return names, masks, and
 * percentages only.
 */

import { join } from 'node:path'
import { homedir } from 'node:os'
import { createAccountService } from './accounts.js'
import { createSnapshotService } from './snapshot.js'
import { fetchUsage } from './usage.js'

// ----------------------------------------------------------------- contract

/** Cordis plugin name. */
const name = 'tokenhub'

/** The credential store carries the pool; the timer drives the poll. */
const inject = ['credentials', 'timer']

/** Official OpenCode Go usage endpoint. Undocumented; verified against a live key. */
const DEFAULT_BASE_URL = 'https://opencode.ai/zen/go/v1/usage'

/** Route prefix owned by this plugin. */
const ROUTE_BASE = '/plugins/dsh-tokenhub'

/** Body cap for control requests. */
const MAX_BODY_BYTES = 4096

/** TokenHub's colour bands: below warn normal, warn..error caution, error and up alarm. */
const DEFAULT_THRESHOLDS = { warn: 60, error: 85 }

/** The account route's operations, keyed by the op the client sends. */
const ACCOUNT_OPS = new Map([
  ['add', (hub, body) => hub.addAccount(body.name, body.key)],
  ['remove', (hub, body) => hub.removeAccount(body.name)],
  ['joinPool', (hub, body) => hub.joinPool(body.name)],
  ['rename', (hub, body) => hub.renameAccount(body.name, body.to)],
])

// ---------------------------------------------------------------- utilities

/** DSH home directory: the launcher's variable, else the default location. */
function resolveDshHome() {
  const fromEnv = process.env.DSH_HOME
  return typeof fromEnv === 'string' && fromEnv.length > 0 ? fromEnv : join(homedir(), '.dsh')
}

/** Read a JSON request body under the size cap. */
function readBody(req) {
  return new Promise((resolve) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        resolve({ ok: false, reason: 'body too large' })
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      try {
        const text = Buffer.concat(chunks).toString('utf8')
        resolve({ ok: true, value: text.length > 0 ? JSON.parse(text) : {} })
      } catch {
        resolve({ ok: false, reason: 'invalid json' })
      }
    })
    req.on('error', () => resolve({ ok: false, reason: 'read error' }))
  })
}

/**
 * Reject a cross-origin browser write. A request with no Origin header (curl, a
 * local script) is allowed: the server is loopback-only by default.
 */
function sameOrigin(req) {
  const origin = req.headers ? req.headers.origin : undefined
  if (!origin) return true
  const host = req.headers ? req.headers.host : undefined
  if (!host) return false
  try {
    return new URL(origin).host === host
  } catch {
    return false
  }
}

/** Answer with JSON and no caching. */
function sendJson(res, status, payload) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  })
  res.end(JSON.stringify(payload))
}

/** Send a mutation result: 200 when it worked, 400 when it did not. */
function respond(res, result) {
  sendJson(res, result.ok ? 200 : 400, result)
}

/** One place for unexpected internal errors: log the cause, answer explicitly. */
function sendInternal(res, error) {
  const message = 'internal: ' + String((error && error.message) || error)
  console.error('[dsh-tokenhub] internal error:', error)
  sendJson(res, 500, { ok: false, message: '', error: message, warnings: [] })
}

/** Run one mutation and turn an unexpected exception into an explicit 500. */
async function respondGuarded(res, operation) {
  try {
    respond(res, await operation())
  } catch (error) {
    sendInternal(res, error)
  }
}

/**
 * The gate both write routes share: POST only, same-origin, JSON body under the
 * cap. Answers the rejection itself and returns null, so a handler reads as
 * "no body, already answered".
 * @returns the parsed body object, or null when the request was rejected.
 */
async function readWriteBody(req, res) {
  if (req.method !== 'POST') {
    sendJson(res, 405, { ok: false, error: 'method not allowed' })
    return null
  }
  if (!sameOrigin(req)) {
    sendJson(res, 403, { ok: false, error: 'forbidden' })
    return null
  }
  const body = await readBody(req)
  if (!body.ok) {
    sendJson(res, 400, { ok: false, error: body.reason })
    return null
  }
  return body.value && typeof body.value === 'object' ? body.value : {}
}

// ------------------------------------------------------------------- config

/**
 * Normalize the plugin row's settings once, so nothing downstream has to repeat
 * a typeof dance or guess a default.
 * @param config - the raw plugin row config.
 * @returns every tunable, defaulted and typed.
 */
function readConfig(config) {
  const positive = (value, fallback) => (Number.isFinite(value) && value > 0 ? value : fallback)
  const text = (value) => (typeof value === 'string' && value.length > 0 ? value : undefined)
  const dshHome = text(config.dshHome) || resolveDshHome()
  return {
    baseUrl: text(config.baseUrl) || DEFAULT_BASE_URL,
    refreshMs: positive(config.refreshMs, 60000),
    timeoutMs: positive(config.timeoutMs, 15000),
    // Opt-in. dsh resolves the reference per request while opencode reads its
    // auth file only at startup, so mirroring by default would leave the two
    // harnesses on different accounts until the next opencode restart. A dsh
    // switch is dsh's business unless the user asks otherwise.
    syncAuth: config.syncOpencodeAuth === true,
    authPath: text(config.opencodeAuthPath),
    autoReleaseLock: config.autoReleaseWedgedLock !== false,
    wedgedLockMinAgeMs: positive(config.wedgedLockMinAgeMs, 30000),
    credentialsPath: text(config.credentialsPath) || join(dshHome, '.credentials.yaml'),
    explicitNames: Array.isArray(config.keyNames)
      ? config.keyNames.filter((entry) => typeof entry === 'string')
      : null,
    thresholds: Object.assign({}, DEFAULT_THRESHOLDS, config.thresholds || {}),
    // Size multiplier for the floating gauge alone. The panel and the dialogs
    // keep their own size — they carry text, and text that grows is text that
    // stops fitting — so this is the one number a different screen or a different
    // pair of eyes needs to change.
    scale: positive(config.scale, 2),
  }
}

// -------------------------------------------------------------- the service

/**
 * Compose the account service with the snapshot service. Account mutations own
 * the write queue; the snapshot cache is invalidated only after a mutation
 * settles, and a refresh never holds the queue.
 * @param ctx - host root context carrying the credentials service.
 * @param options - normalized config from readConfig().
 */
function createHub(ctx, options) {
  const { baseUrl, timeoutMs, refreshMs, thresholds, scale } = options
  const accounts = createAccountService(ctx.credentials, options)
  const snapshotService = createSnapshotService({
    readAccounts: accounts.readAccounts,
    fetchUsages: async (keys, { signal } = {}) => {
      const results = new Map()
      for (const key of keys) {
        results.set(key, await fetchUsage(key, baseUrl, timeoutMs, signal))
      }
      return results
    },
    now: Date.now,
    refreshMs,
  })

  /** Force one generation; concurrent callers of the same revision share it. */
  const refresh = () => snapshotService.snapshot(true)

  /** The snapshot the widget polls, with presentation tunables attached. */
  async function snapshot(force) {
    const base = await snapshotService.snapshot(force === true)
    return Object.assign({}, base, { thresholds, scale })
  }

  /** Stop polling and prevent a late batch from committing after unload. */
  const dispose = () => snapshotService.dispose()

  /** A mutation that succeeded invalidates the cache after the queue settles. */
  const mutation = (operation) => async (...args) => {
    const result = await accounts[operation](...args)
    if (result.ok) snapshotService.invalidate()
    return result
  }

  return {
    refresh,
    snapshot,
    dispose,
    switchTo: mutation('switchTo'),
    addAccount: mutation('addAccount'),
    joinPool: mutation('joinPool'),
    renameAccount: mutation('renameAccount'),
    removeAccount: mutation('removeAccount'),
  }
}

// ------------------------------------------------------------------- plugin

/**
 * Register the quota service: pool discovery from the credential store, a polled
 * usage cache, and the control routes the floating widget calls.
 * @param ctx - host root context carrying the credentials and timer services.
 * @param config - optional tunables from the plugin row.
 */
function apply(ctx, config = {}) {
  const options = readConfig(config)
  const hub = createHub(ctx, options)

  ctx.effect(() => {
    hub.refresh()
    const stop = ctx.interval(() => {
      hub.refresh()
    }, options.refreshMs)
    return () => {
      if (typeof stop === 'function') stop()
      hub.dispose()
    }
  }, 'dsh-tokenhub: polling')

  // The web server exists only in a web composition; a headless profile still
  // gets the polling and switching logic for programmatic callers.
  const webServer = ctx.get('webServer')
  if (webServer === undefined) return

  /** Register one route as a fiber effect so unloading removes it. */
  const route = (routeName, handler) =>
    ctx.effect(
      () => webServer.register({ kind: 'exact', path: ROUTE_BASE + '/' + routeName, handler }),
      'dsh-tokenhub: ' + routeName + ' route',
    )

  route('snapshot', async (req, res) => {
    try {
      let force = false
      try {
        force = new URL(req.url || '/', 'http://localhost').searchParams.get('force') === '1'
      } catch {
        /* a malformed URL is simply not a forced refresh */
      }
      sendJson(res, 200, await hub.snapshot(force))
    } catch (error) {
      sendInternal(res, error)
    }
  })

  route('switch', async (req, res) => {
    const body = await readWriteBody(req, res)
    if (body === null) return
    if (typeof body.name !== 'string') {
      return sendJson(res, 400, { ok: false, error: '账号名不合法', warnings: [] })
    }
    await respondGuarded(res, () => hub.switchTo(body.name))
  })

  route('account', async (req, res) => {
    const body = await readWriteBody(req, res)
    if (body === null) return
    const operation = ACCOUNT_OPS.get(body.op)
    if (!operation) return sendJson(res, 400, { ok: false, error: 'unknown op' })
    await respondGuarded(res, () => operation(hub, body))
  })
}

export { apply, inject, name }
