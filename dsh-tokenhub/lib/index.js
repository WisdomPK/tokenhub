/**
 * dsh-tokenhub — Host half.
 * ------------------------------------------------------------------
 * OpenCode Go quota for DeepSeek Harness: reads every key in the pool from the
 * credential store, polls the official usage endpoint, and switches the active
 * key on request.
 *
 * Ported from TokenHub v3 (opencode desktop quota bar, WisdomPK/tokenhub).
 * One deliberate difference: the dsh credential service resolves a reference
 * per operation with no cross-operation cache, so a switch reaches the next
 * request with no restart. TokenHub had to document "restart opencode" because
 * the provider cached model clients keyed without the credential.
 *
 * Key values never leave this half. The web routes return names, masks, and
 * percentages only.
 */

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { homedir } from 'node:os'
import { createSnapshotService } from './snapshot.js'

// ----------------------------------------------------------------- contract

/** Cordis plugin name. */
const name = 'tokenhub'

/** The credential store carries the pool; the timer drives the poll. */
const inject = ['credentials', 'timer']

/** Official OpenCode Go usage endpoint. Undocumented; verified against a live key. */
const DEFAULT_BASE_URL = 'https://opencode.ai/zen/go/v1/usage'

/** Route prefix owned by this plugin. */
const ROUTE_BASE = '/plugins/dsh-tokenhub'

/** The reference the configured llm adapter resolves on every request. */
const REF_CURRENT = 'OPENCODE_GO_API_KEY'

/** Pool entries are filed as POOL_PREFIX + name. */
const POOL_PREFIX = 'OPENCODE_GO_KEY_'

/** Names the active pool entry. */
const REF_ACTIVE = 'OPENCODE_GO_KEY_ACTIVE'

/** Row id for the key in force when no pool entry holds it. */
const CURRENT_ROW_ID = 'current'

/** A user-supplied pool name; the joined reference must still be a legal one. */
const POOL_NAME_PATTERN = /^[A-Za-z0-9_]{1,32}$/

/** POSIX shell identifier: the grammar a credential reference accepts. */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

/** Accepted OpenCode Go key shapes (new oc_sk_ and legacy sk-). */
const KEY_PREFIXES = ['sk-', 'oc_sk_']

/** Body cap for control requests. */
const MAX_BODY_BYTES = 4096

/** Backups kept beside auth.json. */
const BACKUP_KEEP = 5

/** TokenHub's colour bands: below warn normal, warn..error caution, error and up alarm. */
const DEFAULT_THRESHOLDS = { warn: 60, error: 85 }

/** Shown wherever a pool name fails POOL_NAME_PATTERN. */
const MSG_BAD_NAME = '账号名只能是 1-32 位字母、数字或下划线'

/** Names that already mean something in the credential store. */
const RESERVED_NAMES = new Set(['ACTIVE'])

/** Shown wherever a reserved name is requested. */
const MSG_RESERVED_NAME = '「ACTIVE」是系统保留名称，不能作为账号名'

/** Endpoint failure code to user-facing text. */
const ERROR_TEXT = new Map([
  ['unauthorized', '密钥无效（401）'],
  ['timeout', '请求超时'],
  ['network', '网络错误'],
  ['bad-json', '响应解析失败'],
])

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

/** TokenHub's mask rule: a leading run, an ellipsis, and the last four. */
function maskKey(key) {
  if (typeof key !== 'string' || key.length === 0) return ''
  if (key.startsWith('oc_sk_') && key.length >= 16) return key.slice(0, 8) + '…' + key.slice(-4)
  if (key.length >= 12) return key.slice(0, 6) + '…' + key.slice(-4)
  return '***'
}

/** Turn a transport or endpoint failure code into the user-facing short text. */
function describeError(code, detail) {
  const known = ERROR_TEXT.get(code)
  if (known) return known
  if (typeof code === 'string' && code.startsWith('http-')) return '接口返回 HTTP ' + code.slice(5)
  return detail ? String(detail) : String(code)
}

/**
 * One percentage at the API boundary.
 *
 * A finite number is a reading, including 0 and values over 100. A non-empty
 * decimal string is accepted for compatibility. Everything else — null, an
 * empty string, booleans, arrays, objects, NaN, Infinity, negatives — means the
 * window reported nothing. Coercing any of those to 0 would invent a reading.
 * @returns the percentage, or null when the value is not a usable one.
 */
function parsePercent(raw) {
  if (typeof raw === 'number') return Number.isFinite(raw) && raw >= 0 ? raw : null
  if (typeof raw !== 'string') return null
  const text = raw.trim()
  if (!/^\d+(?:\.\d+)?$/.test(text)) return null
  const value = Number(text)
  return Number.isFinite(value) ? value : null
}

/**
 * One reset timestamp at the API boundary. resetsAt is the current field name;
 * resets_at is accepted for older payloads. A timezone-less value is UTC. A bad
 * timestamp becomes null, but only the countdown is affected — the window's
 * percentage is parsed independently.
 */
function parseResetsAt(raw) {
  if (typeof raw !== 'string') return null
  const text = raw.trim()
  if (text.length === 0) return null
  const naive = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(text)
  const parsed = Date.parse(naive ? text.replace(' ', 'T') + 'Z' : text)
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null
}

/** Defensive parse of one usage window; a malformed window degrades to nulls. */
function pickWindow(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { status: null, percent: null, resetsAt: null }
  }
  const reset = Object.prototype.hasOwnProperty.call(raw, 'resetsAt') ? raw.resetsAt : raw.resets_at
  return {
    status: typeof raw.status === 'string' ? raw.status : null,
    percent: parsePercent(raw.percent),
    resetsAt: parseResetsAt(reset),
  }
}

/** Whether any window carries a real reading; a 200 alone proves nothing. */
function hasUsableWindow(windows) {
  return Boolean(
    windows && Object.values(windows).some((window) => window && typeof window.percent === 'number'),
  )
}

/** Read a JSON document, tolerating a UTF-8 BOM written by another tool. */
function readJsonFile(target) {
  const raw = readFileSync(target, 'utf8')
  return JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw)
}

/**
 * The lock path atomic-write names in its contention diagnostic.
 * @returns the lock path, or null when the failure was something else.
 */
function lockPathFromError(error) {
  const match = /writer lock at (.+)$/.exec(String((error && error.message) || error))
  return match ? match[1].trim() : null
}

/**
 * Whether a lock file is provably unusable rather than held.
 *
 * atomic-write records its holder as "<pid>\n" after creating the lock, and it
 * refuses to take over a lock whose record is incomplete — so a writer killed
 * inside that create-to-write window wedges the file permanently. Such a lock is
 * identifiable without guessing: its record is not a PID, and it has sat
 * unchanged for far longer than any create-to-write gap can last.
 * @param minAgeMs - how long an incomplete record must have sat unchanged.
 * @returns whether the lock may be removed.
 */
function isWedgedLock(lockPath, minAgeMs) {
  let record
  try {
    record = readFileSync(lockPath, 'utf8')
  } catch {
    return false
  }
  // A holder that recorded itself is left strictly alone: atomic-write owns
  // that case, including its own dead-holder takeover.
  if (/^\d+\n$/.test(record)) return false
  try {
    return Date.now() - statSync(lockPath).mtimeMs >= minAgeMs
  } catch {
    return false
  }
}

/** Turn a credential-write failure into text that says what to do about it. */
function credentialWriteError(error) {
  const message = String((error && error.message) || error)
  const lockPath = lockPathFromError(message)
  if (!lockPath) return '写入凭据失败：' + message
  return (
    '凭据文件被写锁阻塞：' +
    lockPath +
    '。dsh 的 atomic-write 只在锁记录是可读的 PID 时才会自动接管；' +
    '若该文件是残留的空记录锁，需先删除它再重试。'
  )
}

/**
 * Fetch one key's usage. Never throws: every failure becomes a displayable code
 * so one bad key cannot take down the pool.
 * @returns error code plus, on success, the three parsed windows.
 */
async function fetchUsage(key, baseUrl, timeoutMs, signal) {
  const ctrl = new AbortController()
  const abort = setTimeout(() => ctrl.abort(), timeoutMs)
  const forwardAbort = () => ctrl.abort()
  if (signal) {
    if (signal.aborted) ctrl.abort()
    else signal.addEventListener('abort', forwardAbort, { once: true })
  }
  try {
    const res = await fetch(baseUrl, {
      headers: { Authorization: 'Bearer ' + key, Accept: 'application/json' },
      signal: ctrl.signal,
    })
    if (res.status === 401) return { error: 'unauthorized', detail: 'HTTP 401' }
    if (!res.ok) return { error: 'http-' + res.status, detail: 'HTTP ' + res.status }
    let data
    try {
      data = await res.json()
    } catch {
      return { error: 'bad-json', detail: 'json parse failed' }
    }
    let usage = data
    if (data && typeof data === 'object' && !Array.isArray(data) && Object.prototype.hasOwnProperty.call(data, 'usage')) {
      usage = data.usage
    }
    if (!usage || typeof usage !== 'object' || Array.isArray(usage)) {
      return { error: 'bad-json', detail: 'no usage object' }
    }
    return {
      error: null,
      detail: null,
      windows: {
        rolling: pickWindow(usage.rolling),
        weekly: pickWindow(usage.weekly),
        monthly: pickWindow(usage.monthly),
      },
    }
  } catch (error) {
    const aborted = error && error.name === 'AbortError'
    return {
      error: aborted ? 'timeout' : 'network',
      detail: String((error && error.message) || error).slice(0, 200),
    }
  } finally {
    clearTimeout(abort)
    if (signal) signal.removeEventListener('abort', forwardAbort)
  }
}

/**
 * Candidate pool names from the credentials document.
 *
 * The credential store offers no enumeration for references, so the document is
 * scanned tolerantly (entries sit indented under the refs mapping, and may be
 * quoted) and every candidate is then confirmed through the store. A stale line
 * therefore costs one lookup and never becomes an entry.
 * @returns candidate names in document order, without duplicates.
 */
function scanPoolNames(text) {
  const found = []
  const seen = new Set()
  // Optional quotes around the name, since a hand-written YAML file may have them.
  const quote = '[\x22\x27]?'
  const pattern = new RegExp('^[ \t]*' + quote + POOL_PREFIX + '([A-Za-z0-9_]+)' + quote + '[ \t]*:', 'gm')
  let match
  while ((match = pattern.exec(text)) !== null) {
    const candidate = match[1]
    if (candidate === 'ACTIVE' || seen.has(candidate)) continue
    seen.add(candidate)
    found.push(candidate)
  }
  return found
}

// ------------------------------------------------- opencode auth.json sync

/**
 * Resolve the opencode auth document: an explicit override, then an explicit
 * environment path, then automatic discovery.
 *
 * An explicit path is the only target. If it is missing the sync creates it
 * there; if it is corrupt the sync reports that target's problem. It must never
 * silently move the write to another file that happens to look healthier.
 * @returns the path to read and write.
 */
function resolveOpencodeAuthPath(override) {
  const explicit = [override, process.env.OPENCODE_AUTH_PATH].find(
    (candidate) => typeof candidate === 'string' && candidate.length > 0,
  )
  if (explicit) return explicit

  const home = homedir()
  const xdg = process.env.XDG_DATA_HOME
  const candidates = [
    xdg ? join(xdg, 'opencode', 'auth.json') : null,
    join(home, '.local', 'share', 'opencode', 'auth.json'),
    join(home, '.opencode', 'auth.json'),
  ].filter((candidate) => typeof candidate === 'string' && candidate.length > 0)

  const existing = candidates.filter((candidate) => existsSync(candidate))
  for (const candidate of existing) {
    try {
      const parsed = readJsonFile(candidate)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && parsed['opencode-go']) return candidate
    } catch {
      /* keep looking for a candidate that parses */
    }
  }
  return existing.length > 0 ? existing[0] : candidates[0]
}

/** Keep the newest N backups and drop older ones. Best effort: never blocks the write. */
function pruneBackups(target, keep) {
  try {
    const dir = dirname(target)
    const prefix = basename(target) + '.tokenhub.bak-'
    const entries = readdirSync(dir)
      .filter((entry) => entry.startsWith(prefix))
      .sort()
      .reverse()
    for (const old of entries.slice(keep)) {
      try {
        rmSync(join(dir, old), { force: true })
      } catch {
        /* best effort */
      }
    }
  } catch {
    /* best effort */
  }
}

/**
 * Restore the pre-change state (TokenHub's R3-01 / R3-02 / R2-04 rules): only a
 * target that did not exist before may be deleted; success means the file now
 * byte-equals the backup; an unparsable backup or a failed restore is reported
 * as failed, never as restored; restoring never consumes the backup.
 * @returns outcome plus a hint that names the backup only when it exists.
 */
function restoreAuth(target, backupPath, existed) {
  const failed = () =>
    backupPath && existsSync(backupPath)
      ? { ok: false, hint: 'auth.json 恢复失败，状态待确认；备份位于 ' + backupPath }
      : { ok: false, hint: 'auth.json 恢复失败，状态待确认' }

  if (backupPath && existsSync(backupPath)) {
    let bytes
    try {
      bytes = readFileSync(backupPath)
    } catch {
      return failed()
    }
    try {
      JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''))
    } catch {
      return failed()
    }
    const tmp = target + '.tokenhub.restoretmp-' + Date.now()
    try {
      copyFileSync(backupPath, tmp)
      renameSync(tmp, target)
    } catch {
      try {
        rmSync(tmp, { force: true })
      } catch {
        /* best effort */
      }
      return failed()
    }
    try {
      if (readFileSync(target).equals(bytes)) return { ok: true }
    } catch {
      /* fall through to the failure report */
    }
    return failed()
  }

  if (!existed) {
    try {
      rmSync(target, { force: true })
      if (!existsSync(target)) return { ok: true }
    } catch {
      /* fall through to the failure report */
    }
  }
  return failed()
}

/**
 * Write the key into opencode's auth.json so one switch covers both harnesses.
 * Backup, same-directory temp file, atomic rename, read-back check, rollback.
 * Only the opencode-go field is touched; every other field survives.
 * @returns outcome plus, when it failed, a message naming the reason.
 */
function syncOpencodeAuth(key, override) {
  const target = resolveOpencodeAuthPath(override)
  let data = {}
  let existed = false

  try {
    if (existsSync(target)) {
      existed = true
      const parsed = readJsonFile(target)
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return { ok: false, message: 'auth.json 顶层不是对象，已跳过同步' }
      }
      data = parsed
    }
  } catch (error) {
    return { ok: false, message: '读取 auth.json 失败：' + String((error && error.message) || error) }
  }

  let backupPath = null
  try {
    if (existed) {
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      backupPath = target + '.tokenhub.bak-' + stamp
      copyFileSync(target, backupPath)
      pruneBackups(target, BACKUP_KEEP)
    } else {
      mkdirSync(dirname(target), { recursive: true })
    }
  } catch (error) {
    return { ok: false, message: '备份 auth.json 失败，未写入：' + String((error && error.message) || error) }
  }

  data['opencode-go'] = { type: 'api', key }
  const tmp = target + '.tokenhub.tmp-' + process.pid + '-' + Date.now()
  try {
    writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8')
    renameSync(tmp, target)
  } catch {
    try {
      rmSync(tmp, { force: true })
    } catch {
      /* best effort */
    }
    return { ok: false, message: 'auth.json 写入失败，原文件未受影响' }
  }

  try {
    const back = readJsonFile(target)
    const entry = back && back['opencode-go']
    if (!entry || entry.key !== key) throw new Error('read-back mismatch')
  } catch {
    const restored = restoreAuth(target, backupPath, existed)
    return restored.ok
      ? { ok: false, message: 'auth.json 读回校验失败，已恢复原内容' }
      : { ok: false, message: restored.hint }
  }

  return { ok: true, message: '已同步 opencode 认证文件', path: target }
}

// ------------------------------------------------------------ route helpers

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
 * The quota service: pool discovery and mutation over the credential store, plus
 * the snapshot the widget polls. Kept out of apply() so the wiring there (timer,
 * routes) stays readable.
 * @param ctx - host root context carrying the credentials service.
 * @param options - normalized config from readConfig().
 */
function createHub(ctx, options) {
  const { baseUrl, refreshMs, timeoutMs, syncAuth, authPath, credentialsPath } = options
  const { explicitNames, autoReleaseLock, wedgedLockMinAgeMs, thresholds, scale } = options

  /** Created below once the account readers exist; never carries a key value. */
  let snapshotService = null

  /** Resolve one reference. The store answers per call, so this is the value. */
  async function resolveRef(ref) {
    try {
      const hit = await ctx.credentials.resolve(ref)
      return hit && hit.value ? hit.value : null
    } catch {
      return null
    }
  }

  /** Advisory view of a reference (configured / writable / source); never throws. */
  async function describeRef(ref) {
    try {
      return (await ctx.credentials.describe(ref)) || null
    } catch {
      return null
    }
  }

  /** The key the adapter currently resolves. */
  const readCurrent = () => resolveRef(REF_CURRENT)

  /** The declared active pool name, or null when the marker is absent. */
  const readDeclaredActive = () => resolveRef(REF_ACTIVE)

  /** Whether the store already holds a value; the document scan cannot see unparsed lines. */
  async function isConfigured(ref) {
    const info = await describeRef(ref)
    return Boolean(info && info.configured)
  }

  /**
   * Run one credential write, recovering from the single failure atomic-write
   * cannot recover from itself: a lock whose holder died before recording its
   * PID. Recovery is reported, never silent — a repaired filesystem problem is
   * still a problem worth seeing.
   * @returns outcome, plus the lock it released when it had to repair one.
   */
  async function writeCredential(operation) {
    try {
      await operation()
      return { ok: true }
    } catch (error) {
      const lockPath = lockPathFromError(error)
      if (!autoReleaseLock || lockPath === null || !isWedgedLock(lockPath, wedgedLockMinAgeMs)) {
        return { ok: false, error: credentialWriteError(error) }
      }
      try {
        rmSync(lockPath, { force: true })
      } catch {
        return { ok: false, error: credentialWriteError(error) }
      }
      try {
        await operation()
        return { ok: true, releasedLock: lockPath }
      } catch (again) {
        return { ok: false, error: credentialWriteError(again) }
      }
    }
  }

  /** Suffix for a message about a write that had to clear a wedged lock. */
  const lockNote = (released) => (released ? '；已清理残留写锁 ' + released : '')

  /** Candidate names: an explicit config list, else the document scan. */
  function discoverPoolNames() {
    if (explicitNames) return explicitNames.slice()
    let text = ''
    try {
      if (existsSync(credentialsPath)) text = readFileSync(credentialsPath, 'utf8')
    } catch {
      /* an unreadable document reads as an empty pool */
    }
    return scanPoolNames(text)
  }

  /** Confirm candidates against the store and resolve their values (host-side only). */
  async function resolvePool() {
    const pool = []
    for (const candidate of discoverPoolNames()) {
      const ref = POOL_PREFIX + candidate
      if (!REF_PATTERN.test(ref)) continue
      const key = await resolveRef(ref)
      if (key) pool.push({ name: candidate, key })
    }
    return pool
  }

  /**
   * Next free default label, mirroring TokenHub's automatic naming. The resolved
   * pool is authoritative here, not the document scan: the scan only proposes
   * candidates, so a name it missed would otherwise be handed out twice.
   */
  async function nextDefaultName() {
    const taken = new Set((await resolvePool()).map((entry) => entry.name))
    let n = Math.max(1, taken.size + 1)
    while (taken.has('go' + n) || (await isConfigured(POOL_PREFIX + 'go' + n))) n += 1
    return 'go' + n
  }

  /**
   * Which pool name is in force.
   *
   * The resolved OPENCODE_GO_API_KEY is authoritative: a marker can never
   * promote an entry holding another key. The marker only breaks a tie when
   * several pool names point at the same live key; absent a matching marker,
   * pool order decides. No current key means no active row at all.
   * @returns the active pool name, or null when no pool entry holds the key.
   */
  function selectActiveName(pool, currentKey, declaredName) {
    if (currentKey === null) return null
    const matches = pool.filter((entry) => entry.key === currentKey)
    if (matches.length === 0) return null
    if (declaredName && declaredName.length > 0) {
      const preferred = matches.find((entry) => entry.name === declaredName)
      if (preferred) return preferred.name
    }
    return matches[0].name
  }

  /**
   * Validate a user-supplied pool name once, for every entry point. Reserved
   * names are rejected before any credential is touched.
   * @returns {ok: true, name} or {ok: false, error}.
   */
  function validatePoolName(raw) {
    const value = typeof raw === 'string' ? raw.trim() : ''
    if (!POOL_NAME_PATTERN.test(value)) return { ok: false, error: MSG_BAD_NAME }
    if (RESERVED_NAMES.has(value)) return { ok: false, error: MSG_RESERVED_NAME }
    return { ok: true, name: value }
  }

  /** One mutation outcome shape: old fields survive, warnings are additive. */
  const succeed = (fields) => Object.assign({ ok: true, message: '', error: null, warnings: [] }, fields || {})
  const fail = (error, fields) => Object.assign({ ok: false, message: '', error, warnings: [] }, fields || {})

  /**
   * Run one account mutation after every earlier one. Reads, conflict checks,
   * writes, and read-backs all happen inside the same turn, so two callers
   * cannot both pass a "free name" check. The tail swallows only the queue's
   * own rejection bookkeeping; the caller still receives the operation's
   * result or its exception.
   */
  let mutationTail = Promise.resolve()
  function enqueueMutation(operation) {
    const result = mutationTail.then(operation)
    mutationTail = result.then(() => undefined, () => undefined)
    return result
  }

  /** Read the value back and report whether it equals the expected one. */
  async function readBack(ref, expected) {
    return (await resolveRef(ref)) === expected
  }

  /**
   * Host-private account state: identity rows plus a fingerprint of the inputs
   * that decided them. The fingerprint lets the snapshot service refuse a batch
   * whose account state changed while its requests were in flight.
   */
  async function readAccountsState() {
    const pool = await resolvePool()
    const current = await readCurrent()
    const declared = await readDeclaredActive()
    const activeName = selectActiveName(pool, current, declared)
    const rows = []

    // The key in force always owns a row, whether or not a pool entry backs it.
    // It is what the next request uses, and it is the row carrying the Add
    // affordance. When no pool entry holds the key, no pool row may claim to be
    // in force.
    if (current !== null && activeName === null) {
      rows.push({
        id: CURRENT_ROW_ID,
        name: '',
        key: current,
        masked: maskKey(current),
        active: true,
        inPool: false,
      })
    }
    for (const entry of pool) {
      rows.push({
        id: 'pool:' + entry.name,
        name: entry.name,
        key: entry.key,
        masked: maskKey(entry.key),
        active: entry.name === activeName,
        inPool: true,
      })
    }
    return {
      rows,
      fingerprint: JSON.stringify({
        current,
        declared,
        pool: pool.map((entry) => [entry.name, entry.key]),
      }),
    }
  }

  /** Fetch one usage reading per unique key, keeping key values host-side. */
  async function fetchUsages(keys, { signal } = {}) {
    const results = new Map()
    for (const key of keys) {
      results.set(key, await fetchUsage(key, baseUrl, timeoutMs, signal))
    }
    return results
  }

  snapshotService = createSnapshotService({
    readAccounts: readAccountsState,
    fetchUsages,
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

  /**
   * Move the active account: store the target key under the reference the
   * adapter resolves, read it back, then mirror into opencode's auth.json.
   */
  async function switchTo(targetName) {
    const named = validatePoolName(targetName)
    if (!named.ok) return fail(named.error)
    const target = named.name

    const outcome = await enqueueMutation(async () => {
      const next = await resolveRef(POOL_PREFIX + target)
      if (!next) return fail('账号「' + target + '」不存在或密钥为空')

      // A read-only source shadows the reference: the write would report success
      // while resolution kept returning the shadowing value.
      const info = await describeRef(REF_CURRENT)
      if (info && info.writable === false) {
        return fail('当前密钥由启动环境提供（只读），无法切换：请先清除环境变量 ' + REF_CURRENT)
      }

      const previous = await readCurrent()
      const write = await writeCredential(() => ctx.credentials.set(REF_CURRENT, next))
      if (!write.ok) return fail(write.error)

      // Resolution is per call, so reading back is the authoritative check.
      if (!(await readBack(REF_CURRENT, next))) {
        let restored = false
        if (previous !== null) {
          const undo = await writeCredential(() => ctx.credentials.set(REF_CURRENT, previous))
          // Only a read-back may call the rollback restored.
          restored = undo.ok && (await readBack(REF_CURRENT, previous))
        }
        return fail(restored ? '读回校验失败，已恢复原账号' : '读回校验失败，状态待确认')
      }

      const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, target))
      const repaired = write.releasedLock || marker.releasedLock || null
      const authSync = syncAuth
        ? Object.assign({ enabled: true }, syncOpencodeAuth(next, authPath))
        : { enabled: false, ok: true, message: '未启用同步' }

      const warnings = []
      if (!marker.ok) warnings.push({ code: 'active-marker', message: '「使用中」标记写入失败：' + marker.error })
      if (syncAuth && !authSync.ok) warnings.push({ code: 'opencode-auth', message: 'opencode 同步失败：' + authSync.message })

      // Say which half applies when, because they differ: dsh is already live,
      // opencode reads its file only at startup. Auxiliary failures are warnings:
      // the switch itself was read back and is in force.
      const markerNote = marker.ok ? '' : '；「使用中」标记写入失败：' + marker.error
      const authNote = !syncAuth
        ? ''
        : authSync.ok
          ? '；opencode 认证文件已同步，但 opencode 需重启后才读取'
          : '；opencode 同步失败：' + authSync.message

      return succeed({
        name: target,
        message: '已切换到「' + target + '」，下一个请求即生效' + markerNote + authNote + lockNote(repaired),
        activeMarker: marker.ok ? 'ok' : 'failed',
        opencodeAuth: authSync,
        warnings,
      })
    })

    // The usage refresh is deliberately outside the write queue: a slow endpoint
    // must not hold the next account change.
    if (outcome.ok) snapshotService.invalidate()
    return outcome
  }

  /**
   * Add a pool entry after checking the key against the live endpoint, so an
   * invalid key is refused instead of stored (TokenHub behaviour).
   * @param rawName - pool name; an empty string generates the next default.
   */
  async function addAccount(rawName, rawKey) {
    const key = typeof rawKey === 'string' ? rawKey.trim() : ''
    const requested = typeof rawName === 'string' ? rawName.trim() : ''
    let requestedName = null
    if (requested.length > 0) {
      const named = validatePoolName(requested)
      if (!named.ok) return fail(named.error)
      requestedName = named.name
      // Fast refusal; the authoritative check runs again inside the queue.
      if (await isConfigured(POOL_PREFIX + requestedName)) {
        return fail('账号「' + requestedName + '」已存在')
      }
    }
    if (!KEY_PREFIXES.some((prefix) => key.startsWith(prefix))) {
      return fail('密钥需以 sk- 或 oc_sk_ 开头')
    }

    // Remote validation is outside the queue: it may be slow and must not block
    // an unrelated rename or switch.
    const probe = await fetchUsage(key, baseUrl, timeoutMs)
    if (probe.error) {
      return fail('密钥校验失败：' + describeError(probe.error, probe.detail))
    }
    if (!hasUsableWindow(probe.windows)) {
      return fail('接口数据不可用：该密钥没有返回任何可用额度窗口')
    }

    const outcome = await enqueueMutation(async () => {
      // Default naming is inside the queue too: two blank-name adds must not
      // hand out the same goN.
      const target = requestedName !== null ? requestedName : await nextDefaultName()
      const ref = POOL_PREFIX + target
      if (await isConfigured(ref)) return fail('账号「' + target + '」已存在')

      const write = await writeCredential(() => ctx.credentials.set(ref, key))
      if (!write.ok) return fail(write.error)

      if (!(await readBack(ref, key))) {
        const undo = await writeCredential(() => ctx.credentials.unset(ref))
        const cleared = undo.ok && (await resolveRef(ref)) === null
        return fail(cleared ? '写入后读回校验失败，已撤销本次写入' : '写入后读回校验失败，状态待确认')
      }
      return succeed({
        name: target,
        message: '已添加账号「' + target + '」' + lockNote(write.releasedLock),
      })
    })

    if (outcome.ok) snapshotService.invalidate()
    return outcome
  }

  /**
   * Give the key already in force a pool entry of its own. Without this the
   * account the adapter is using would vanish from the panel the moment a switch
   * replaced it, since nothing would hold its value any more. The key needs no
   * re-validation: it is the one already in use.
   */
  async function joinPool(rawName) {
    const named = validatePoolName(rawName)
    if (!named.ok) return fail(named.error)
    const target = named.name

    const outcome = await enqueueMutation(async () => {
      const current = await readCurrent()
      if (current === null) return fail('当前没有生效的密钥可入池')
      // The pool scan may be narrower than the store (explicit keyNames), so
      // describe is the authoritative conflict check.
      if (await isConfigured(POOL_PREFIX + target)) return fail('账号「' + target + '」已存在')
      const pool = await resolvePool()
      if (pool.some((entry) => entry.key === current)) {
        return fail('当前密钥已经在账号池中')
      }

      const ref = POOL_PREFIX + target
      const write = await writeCredential(() => ctx.credentials.set(ref, current))
      if (!write.ok) return fail(write.error)
      if (!(await readBack(ref, current))) {
        const undo = await writeCredential(() => ctx.credentials.unset(ref))
        const cleared = undo.ok && (await resolveRef(ref)) === null
        return fail(cleared ? '入池后读回校验失败，已撤销本次写入' : '入池后读回校验失败，状态待确认')
      }

      const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, target))
      const warnings = []
      if (!marker.ok) warnings.push({ code: 'active-marker', message: '「使用中」标记写入失败：' + marker.error })
      return succeed({
        name: target,
        message:
          '已把当前密钥归入账号池：「' + target + '」' +
          (marker.ok ? '' : '；「使用中」标记写入失败：' + marker.error) +
          lockNote(write.releasedLock || marker.releasedLock),
        warnings,
      })
    })

    if (outcome.ok) snapshotService.invalidate()
    return outcome
  }

  /**
   * Rename a pool entry. The key itself never moves and the account in force
   * stays in force — only the name the entry is filed under changes. The new
   * reference is written before the old one is dropped, so a failure part-way
   * leaves the key reachable instead of losing it.
   */
  async function renameAccount(rawFrom, rawTo) {
    const source = validatePoolName(rawFrom)
    if (!source.ok) return fail(source.error)
    const destination = validatePoolName(rawTo)
    if (!destination.ok) return fail(destination.error)
    const from = source.name
    const to = destination.name
    if (from === to) return succeed({ name: to, message: '名称未变化' })

    const outcome = await enqueueMutation(async () => {
      const pool = await resolvePool()
      const entry = pool.find((item) => item.name === from)
      if (!entry) return fail('账号「' + from + '」不存在')
      // describe sees targets the scan may not list (explicit keyNames files).
      if (await isConfigured(POOL_PREFIX + to)) return fail('账号「' + to + '」已存在')

      const targetRef = POOL_PREFIX + to
      const write = await writeCredential(() => ctx.credentials.set(targetRef, entry.key))
      if (!write.ok) return fail(write.error)
      if (!(await readBack(targetRef, entry.key))) {
        const undo = await writeCredential(() => ctx.credentials.unset(targetRef))
        const cleared = undo.ok && (await resolveRef(targetRef)) === null
        return fail(cleared ? '新名写入后读回校验失败，已撤销写入' : '新名写入后读回校验失败，状态待确认')
      }

      // The marker names the entry in force, so it has to follow the rename or
      // the panel would lose track of which row is active.
      const warnings = []
      let markerNote = ''
      if ((await readDeclaredActive()) === from) {
        const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, to))
        if (!marker.ok) {
          markerNote = '；「使用中」标记未能更新，请手动切换一次'
          warnings.push({ code: 'active-marker', message: '「使用中」标记未能更新：' + marker.error })
        }
      }

      const drop = await writeCredential(() => ctx.credentials.unset(POOL_PREFIX + from))
      if (!drop.ok) {
        // The key is reachable under the new name, but both names now exist.
        const message = '新名已写入，但旧名未能删除：' + drop.error
        return fail(message, {
          warnings: warnings.concat([{ code: 'partial-rename', message }]),
          name: to,
          partial: true,
        })
      }
      return succeed({
        name: to,
        message: '已把「' + from + '」改名为「' + to + '」' + markerNote + lockNote(write.releasedLock),
        warnings,
      })
    })

    if (outcome.ok) snapshotService.invalidate()
    return outcome
  }

  /**
   * Remove a pool entry. The account in force is refused: the adapter resolves
   * it on every request.
   */
  async function removeAccount(rawName) {
    const named = validatePoolName(rawName)
    if (!named.ok) return fail(named.error)
    const target = named.name

    const outcome = await enqueueMutation(async () => {
      const pool = await resolvePool()
      const entry = pool.find((item) => item.name === target)
      if (!entry) return fail('账号「' + target + '」不存在')
      // Read the current key after earlier queued writes, not before them.
      const current = await readCurrent()
      if (current !== null && entry.key === current) {
        return fail('使用中的账号不能删除，请先切换到其他账号')
      }

      const ref = POOL_PREFIX + target
      const write = await writeCredential(() => ctx.credentials.unset(ref))
      if (!write.ok) return fail(write.error)
      if ((await resolveRef(ref)) !== null) return fail('删除后读回校验失败，状态待确认')
      return succeed({ name: target, message: '已删除账号「' + target + '」' })
    })

    if (outcome.ok) snapshotService.invalidate()
    return outcome
  }

  return { refresh, snapshot, dispose, switchTo, addAccount, joinPool, renameAccount, removeAccount }
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
    let force = false
    try {
      force = new URL(req.url || '/', 'http://localhost').searchParams.get('force') === '1'
    } catch {
      /* a malformed URL is simply not a forced refresh */
    }
    sendJson(res, 200, await hub.snapshot(force))
  })

  route('switch', async (req, res) => {
    const body = await readWriteBody(req, res)
    if (body === null) return
    if (typeof body.name !== 'string' || !POOL_NAME_PATTERN.test(body.name)) {
      return sendJson(res, 400, { ok: false, error: '账号名不合法' })
    }
    respond(res, await hub.switchTo(body.name))
  })

  route('account', async (req, res) => {
    const body = await readWriteBody(req, res)
    if (body === null) return
    const operation = ACCOUNT_OPS.get(body.op)
    if (!operation) return sendJson(res, 400, { ok: false, error: 'unknown op' })
    respond(res, await operation(hub, body))
  })
}

export { apply, inject, name }
