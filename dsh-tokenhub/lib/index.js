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
/** Pool entry naming: POOL_PREFIX + name. */
const POOL_PREFIX = 'OPENCODE_GO_KEY_'
/** Names the active pool entry. */
const REF_ACTIVE = 'OPENCODE_GO_KEY_ACTIVE'
/** Row id for the key in force when no pool entry holds it. */
const CURRENT_ROW_ID = '__current__'

/** POSIX shell identifier: the grammar a credential reference accepts. */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/
/** A user-supplied pool name; the joined reference must still satisfy REF_PATTERN. */
const POOL_NAME_PATTERN = /^[A-Za-z0-9_]{1,32}$/
/** Accepted OpenCode Go key shapes (new oc_sk_ and legacy sk-). */
const KEY_PREFIXES = ['sk-', 'oc_sk_']
/** Body cap for control requests. */
const MAX_BODY_BYTES = 4096
/** Backups kept beside auth.json. */
const BACKUP_KEEP = 5
/** TokenHub's colour bands: below warn normal, warn..error caution, error and up alarm. */
const DEFAULT_THRESHOLDS = { warn: 60, error: 85 }

// ---------------------------------------------------------------- utilities

/** DSH home directory: the launcher's variable, else the default location. */
function resolveDshHome() {
  const fromEnv = process.env.DSH_HOME
  if (typeof fromEnv === 'string' && fromEnv.length > 0) return fromEnv
  return join(homedir(), '.dsh')
}

/** TokenHub's mask rule: a leading run, an ellipsis, and the last four. */
function maskKey(key) {
  if (typeof key !== 'string' || key.length === 0) return ''
  if (key.indexOf('oc_sk_') === 0 && key.length >= 16) return key.slice(0, 8) + '\u2026' + key.slice(-4)
  if (key.length >= 12) return key.slice(0, 6) + '\u2026' + key.slice(-4)
  return '***'
}

/** Turn a transport or endpoint failure code into the user-facing short text. */
function describeError(code, detail) {
  if (code === 'unauthorized') return '\u5bc6\u94a5\u65e0\u6548\uff08401\uff09'
  if (code === 'timeout') return '\u8bf7\u6c42\u8d85\u65f6'
  if (code === 'network') return '\u7f51\u7edc\u9519\u8bef'
  if (code === 'bad-json') return '\u54cd\u5e94\u89e3\u6790\u5931\u8d25'
  if (typeof code === 'string' && code.indexOf('http-') === 0) return '\u63a5\u53e3\u8fd4\u56de HTTP ' + code.slice(5)
  return detail ? String(detail) : String(code)
}

/** Defensive parse of one usage window; a malformed window degrades to nulls. */
function pickWindow(raw) {
  if (!raw || typeof raw !== 'object') return { status: null, percent: null, resetsAt: null }
  const value = typeof raw.percent === 'number' ? raw.percent : Number(raw.percent)
  return {
    status: typeof raw.status === 'string' ? raw.status : null,
    percent: Number.isFinite(value) ? value : null,
    resetsAt: typeof raw.resetsAt === 'string' ? raw.resetsAt : null,
  }
}

/** Read a JSON document, tolerating a UTF-8 BOM written by another tool. */
function readJsonFile(target) {
  const raw = readFileSync(target, 'utf8')
  return JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw)
}

/**
 * The lock path atomic-write names in its contention diagnostic.
 * @param error - the error a credential write threw.
 * @returns the lock path, or null when the failure was something else.
 */
function lockPathFromError(error) {
  const message = String((error && error.message) || error)
  const match = /writer lock at (.+)$/.exec(message)
  return match ? match[1].trim() : null
}

/**
 * Whether a lock file is provably unusable rather than held.
 *
 * atomic-write creates the lock and then records its holder as "<pid>\n", and
 * it refuses to take over a lock whose record is incomplete — so a writer killed
 * inside that create-to-write window wedges the file permanently ("keeps its
 * lock until an operator removes it"). Such a lock is identifiable without
 * guessing: its record is not a PID, and it has sat unchanged for far longer
 * than any create-to-write gap can last.
 * @param lockPath - the lock file to inspect.
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

/**
 * Turn a credential-write failure into text that says what to do about it.
 * @param error - the error the write threw.
 * @returns a message naming the blocking lock when that was the cause.
 */
function credentialWriteError(error) {
  const message = String((error && error.message) || error)
  const lockPath = lockPathFromError(message)
  if (!lockPath) return '\u5199\u5165\u51ed\u636e\u5931\u8d25\uff1a' + message
  return (
    '\u51ed\u636e\u6587\u4ef6\u88ab\u5199\u9501\u963b\u585e\uff1a' +
    lockPath +
    '\u3002dsh \u7684 atomic-write \u53ea\u5728\u9501\u8bb0\u5f55\u662f\u53ef\u8bfb\u7684 PID \u65f6\u624d\u4f1a\u81ea\u52a8\u63a5\u7ba1\uff1b' +
    '\u82e5\u8be5\u6587\u4ef6\u662f\u6b8b\u7559\u7684\u7a7a\u8bb0\u5f55\u9501\uff0c\u9700\u5148\u5220\u9664\u5b83\u518d\u91cd\u8bd5\u3002'
  )
}

/**
 * Fetch one key's usage. Never throws: every failure becomes a displayable code
 * so one bad key cannot take down the pool.
 * @param key - the API key to query.
 * @param baseUrl - usage endpoint.
 * @param timeoutMs - abort budget.
 * @returns error code plus, on success, the three parsed windows.
 */
async function fetchUsage(key, baseUrl, timeoutMs) {
  const ctrl = new AbortController()
  const abort = setTimeout(() => ctrl.abort(), timeoutMs)
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
    const usage = data && typeof data === 'object' && data.usage ? data.usage : data
    if (!usage || typeof usage !== 'object') return { error: 'bad-json', detail: 'no usage object' }
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
    const detail = String((error && error.message) || error).slice(0, 200)
    return { error: aborted ? 'timeout' : 'network', detail: detail }
  } finally {
    clearTimeout(abort)
  }
}

/**
 * Candidate pool names from the credentials document.
 *
 * The credential store offers no enumeration for references, so the document is
 * scanned tolerantly (entries sit indented under the refs mapping, and may be
 * quoted) and every candidate is then confirmed through the store. A stale line
 * therefore costs one lookup and never becomes an entry.
 * @param text - the credentials document.
 * @returns candidate names in document order, without duplicates.
 */
function scanPoolNames(text) {
  const found = []
  const seen = new Set()
  const quote = '[\u0022\u0027]?'
  const pattern = new RegExp('^[ \\t]*' + quote + POOL_PREFIX + '([A-Za-z0-9_]+)' + quote + '[ \\t]*:', 'gm')
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
 * Resolve the opencode auth document the way TokenHub does: an explicit
 * override first, then the documented locations, preferring whichever one
 * actually carries the opencode-go field.
 * @param override - explicit path from plugin config, when set.
 * @returns the path to read and write.
 */
function resolveOpencodeAuthPath(override) {
  const candidates = []
  if (override) candidates.push(override)
  const fromEnv = process.env.OPENCODE_AUTH_PATH
  if (fromEnv) candidates.push(fromEnv)
  const xdg = process.env.XDG_DATA_HOME
  if (xdg) candidates.push(join(xdg, 'opencode', 'auth.json'))
  const home = homedir()
  candidates.push(join(home, '.local', 'share', 'opencode', 'auth.json'))
  candidates.push(join(home, '.opencode', 'auth.json'))

  const existing = candidates.filter((candidate) => {
    try {
      return existsSync(candidate)
    } catch {
      return false
    }
  })
  if (existing.length === 0) return candidates[0]
  for (const candidate of existing) {
    try {
      const parsed = readJsonFile(candidate)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && parsed['opencode-go']) {
        return candidate
      }
    } catch {
      /* keep looking for a candidate that parses */
    }
  }
  return existing[0]
}

/**
 * Keep the newest N backups and drop older ones. Best effort: a failure here
 * never blocks the write that follows.
 */
function pruneBackups(target, keep) {
  try {
    const dir = dirname(target)
    const prefix = basename(target) + '.tokenhub.bak-'
    const entries = readdirSync(dir)
      .filter((entry) => entry.indexOf(prefix) === 0)
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
 * Restore the pre-change state (TokenHub's R3-01 / R3-02 / R2-04 rules):
 * only a target that did not exist before may be deleted; success means the
 * file now byte-equals the backup; an unparsable backup or a failed restore is
 * reported as failed, never as restored; restoring never consumes the backup.
 * @param target - auth.json path.
 * @param backupPath - the backup taken before the write, when one was taken.
 * @param existed - whether auth.json existed before the write.
 * @returns outcome plus a hint that names the backup only when it exists.
 */
function restoreAuth(target, backupPath, existed) {
  const failed = () =>
    backupPath && existsSync(backupPath)
      ? { ok: false, hint: 'auth.json \u6062\u590d\u5931\u8d25\uff0c\u72b6\u6001\u5f85\u786e\u8ba4\uff1b\u5907\u4efd\u4f4d\u4e8e ' + backupPath }
      : { ok: false, hint: 'auth.json \u6062\u590d\u5931\u8d25\uff0c\u72b6\u6001\u5f85\u786e\u8ba4' }

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
 * @param key - the key to install.
 * @param override - optional explicit auth.json path.
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
        return { ok: false, message: 'auth.json \u9876\u5c42\u4e0d\u662f\u5bf9\u8c61\uff0c\u5df2\u8df3\u8fc7\u540c\u6b65' }
      }
      data = parsed
    }
  } catch (error) {
    return { ok: false, message: '\u8bfb\u53d6 auth.json \u5931\u8d25\uff1a' + String((error && error.message) || error) }
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
    return { ok: false, message: '\u5907\u4efd auth.json \u5931\u8d25\uff0c\u672a\u5199\u5165\uff1a' + String((error && error.message) || error) }
  }

  data['opencode-go'] = { type: 'api', key: key }
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
    return { ok: false, message: 'auth.json \u5199\u5165\u5931\u8d25\uff0c\u539f\u6587\u4ef6\u672a\u53d7\u5f71\u54cd' }
  }

  try {
    const back = readJsonFile(target)
    const entry = back && back['opencode-go']
    if (!entry || entry.key !== key) throw new Error('read-back mismatch')
  } catch {
    const restored = restoreAuth(target, backupPath, existed)
    return restored.ok
      ? { ok: false, message: 'auth.json \u8bfb\u56de\u6821\u9a8c\u5931\u8d25\uff0c\u5df2\u6062\u590d\u539f\u5185\u5bb9' }
      : { ok: false, message: restored.hint }
  }

  return { ok: true, message: '\u5df2\u540c\u6b65 opencode \u8ba4\u8bc1\u6587\u4ef6', path: target }
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
 * Reject a cross-origin browser write. A request with no Origin header (curl,
 * a local script) is allowed: the server is loopback-only by default.
 * @param req - the incoming request.
 * @returns whether the write may proceed.
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

// ------------------------------------------------------------------- plugin

/**
 * Register the quota service: pool discovery from the credential store, a
 * polled usage cache, and the control routes the floating widget calls.
 * @param ctx - host root context carrying the credentials and timer services.
 * @param config - optional tunables from the plugin row.
 */
function apply(ctx, config = {}) {
  const baseUrl = typeof config.baseUrl === 'string' && config.baseUrl ? config.baseUrl : DEFAULT_BASE_URL
  const refreshMs = Number.isFinite(config.refreshMs) && config.refreshMs > 0 ? config.refreshMs : 60000
  const timeoutMs = Number.isFinite(config.timeoutMs) && config.timeoutMs > 0 ? config.timeoutMs : 15000
  // Opt-in. dsh resolves the reference per request while opencode reads its auth
  // file only at startup, so writing opencode's file by default would leave the
  // two harnesses on different accounts until the next opencode restart. A dsh
  // switch is dsh's business unless the user asks otherwise.
  const syncAuth = config.syncOpencodeAuth === true
  const autoReleaseLock = config.autoReleaseWedgedLock !== false
  const wedgedLockMinAgeMs =
    Number.isFinite(config.wedgedLockMinAgeMs) && config.wedgedLockMinAgeMs > 0
      ? config.wedgedLockMinAgeMs
      : 30000
  const authOverride =
    typeof config.opencodeAuthPath === 'string' && config.opencodeAuthPath ? config.opencodeAuthPath : undefined
  const dshHome = typeof config.dshHome === 'string' && config.dshHome ? config.dshHome : resolveDshHome()
  const credentialsPath =
    typeof config.credentialsPath === 'string' && config.credentialsPath
      ? config.credentialsPath
      : join(dshHome, '.credentials.yaml')
  const explicitNames = Array.isArray(config.keyNames)
    ? config.keyNames.filter((entry) => typeof entry === 'string')
    : null
  const thresholds = Object.assign({}, DEFAULT_THRESHOLDS, config.thresholds || {})
  // Size multiplier for the floating gauge alone. The panel and the dialogs keep
  // their own size — they carry text, and text that grows is text that stops
  // fitting — so this is the one number a different screen or a different pair
  // of eyes needs to change.
  const scale = Number.isFinite(config.scale) && config.scale > 0 ? config.scale : 2

  /** Serialized snapshot handed to the client; never carries a key value. */
  let cache = { updatedAt: 0, error: null, entries: [] }
  let refreshing = null

  /**
   * Candidate names: an explicit config list, else the document scan.
   * @returns candidate pool names.
   */
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

  /**
   * Confirm candidates against the store and resolve their values.
   * @returns pool entries carrying name and key, both host-side only.
   */
  async function resolvePool() {
    const pool = []
    for (const candidate of discoverPoolNames()) {
      const ref = POOL_PREFIX + candidate
      if (!REF_PATTERN.test(ref)) continue
      try {
        const hit = await ctx.credentials.resolve(ref)
        if (hit && hit.value) pool.push({ name: candidate, key: hit.value })
      } catch {
        /* a name the store cannot answer is not a pool entry */
      }
    }
    return pool
  }

  /**
   * The key the adapter currently resolves.
   * @returns the key, or null while unconfigured.
   */
  async function readCurrent() {
    try {
      const hit = await ctx.credentials.resolve(REF_CURRENT)
      return hit && hit.value ? hit.value : null
    } catch {
      return null
    }
  }

  /**
   * The declared active pool name.
   * @returns the name, or null when the marker is absent.
   */
  async function readDeclaredActive() {
    try {
      const hit = await ctx.credentials.resolve(REF_ACTIVE)
      return hit && hit.value ? hit.value : null
    } catch {
      return null
    }
  }

  /**
   * Which entry is in force. A declared name wins when it names a real entry;
   * otherwise the entry whose value matches the current key wins, so a pool
   * written by hand (no ACTIVE marker) still highlights the right row.
   * @param entry - the pool entry under test.
   * @param declared - the ACTIVE marker value, or null.
   * @param current - the key in force, or null.
   * @param poolNames - every pool name, for the stale-marker check.
   * @returns whether this entry is the one in force.
   */
  function isActive(entry, declared, current, poolNames) {
    if (declared && declared.length > 0) {
      if (declared === entry.name) return true
      if (poolNames.indexOf(declared) !== -1) return false
    }
    return current !== null && entry.key === current
  }


  /**
   * Run one credential write, recovering from the single failure atomic-write
   * cannot recover from itself: a lock whose holder died before recording its
   * PID. Recovery is reported, never silent — a repaired filesystem problem is
   * still a problem worth seeing.
   * @param operation - the set/unset call to run under the lock.
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

  /**
   * Rebuild the cache. Concurrent callers share one in-flight pass, so a poll
   * and a widget refresh never double-spend the endpoint.
   * @returns the current cache.
   */
  async function refresh() {
    if (refreshing) return refreshing
    refreshing = (async () => {
      try {
        const pool = await resolvePool()
        const current = await readCurrent()
        const declared = await readDeclaredActive()
        const poolNames = pool.map((entry) => entry.name)
        const entries = []

        // The key in force always owns a row, whether or not a pool entry backs
        // it. It is what the next request uses, and it is the row carrying the
        // Add affordance — without it, adding the first pool entry would leave
        // nothing marked in force and no way to add a second.
        if (current !== null && !pool.some((entry) => entry.key === current)) {
          const only = await fetchUsage(current, baseUrl, timeoutMs)
          entries.push({
            id: CURRENT_ROW_ID,
            name: '',
            masked: maskKey(current),
            active: true,
            inPool: false,
            error: only.error,
            detail: only.detail || null,
            windows: only.windows || null,
          })
        }

        const backed = await Promise.all(
          pool.map(async (entry) => {
            const result = await fetchUsage(entry.key, baseUrl, timeoutMs)
            return {
              id: entry.name,
              name: entry.name,
              masked: maskKey(entry.key),
              active: isActive(entry, declared, current, poolNames),
              inPool: true,
              error: result.error,
              detail: result.detail || null,
              windows: result.windows || null,
            }
          }),
        )
        for (const entry of backed) entries.push(entry)
        cache = {
          updatedAt: Date.now(),
          error: entries.length === 0 ? 'no-keys' : null,
          entries: entries,
          scale: scale,
        }
      } catch (error) {
        cache = Object.assign({}, cache, { error: 'internal: ' + String((error && error.message) || error) })
      }
    })()
    try {
      await refreshing
    } finally {
      refreshing = null
    }
    return cache
  }

  /**
   * Move the active account: store the target key under the reference the
   * adapter resolves, read it back, then mirror into opencode's auth.json.
   * @param targetName - pool entry to activate.
   * @returns outcome the client renders as a message.
   */
  async function switchTo(targetName) {
    const ref = POOL_PREFIX + targetName
    let next = null
    try {
      const hit = await ctx.credentials.resolve(ref)
      next = hit && hit.value ? hit.value : null
    } catch {
      next = null
    }
    if (!next) return { ok: false, error: '\u8d26\u53f7\u300c' + targetName + '\u300d\u4e0d\u5b58\u5728\u6216\u5bc6\u94a5\u4e3a\u7a7a' }

    // A read-only source shadows the reference: the write would report success
    // while resolution kept returning the shadowing value.
    try {
      const info = await ctx.credentials.describe(REF_CURRENT)
      if (info && info.writable === false) {
        return {
          ok: false,
          error:
            '\u5f53\u524d\u5bc6\u94a5\u7531\u542f\u52a8\u73af\u5883\u63d0\u4f9b\uff08\u53ea\u8bfb\uff09\uff0c\u65e0\u6cd5\u5207\u6362\uff1a\u8bf7\u5148\u6e05\u9664\u73af\u5883\u53d8\u91cf ' +
            REF_CURRENT,
        }
      }
    } catch {
      /* describe is advisory */
    }

    const previous = await readCurrent()

    const write = await writeCredential(() => ctx.credentials.set(REF_CURRENT, next))
    if (!write.ok) return { ok: false, error: write.error }

    // Resolution is per call, so reading back is the authoritative check.
    const back = await readCurrent()
    if (back !== next) {
      let rolled = false
      if (previous !== null) {
        const undo = await writeCredential(() => ctx.credentials.set(REF_CURRENT, previous))
        rolled = undo.ok
      }
      return {
        ok: false,
        error: rolled
          ? '\u8bfb\u56de\u6821\u9a8c\u5931\u8d25\uff0c\u5df2\u6062\u590d\u539f\u8d26\u53f7'
          : '\u8bfb\u56de\u6821\u9a8c\u5931\u8d25\uff0c\u72b6\u6001\u5f85\u786e\u8ba4',
      }
    }

    let activeMarker = 'ok'
    const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, targetName))
    if (!marker.ok) activeMarker = 'failed'

    const repaired = write.releasedLock || marker.releasedLock || null
    let authSync = { enabled: false, ok: true, message: '\u672a\u542f\u7528\u540c\u6b65' }
    if (syncAuth) authSync = Object.assign({ enabled: true }, syncOpencodeAuth(next, authOverride))

    // Say which half applies when, because they differ: dsh is already live,
    // opencode reads its file only at startup.
    const authNote = !syncAuth
      ? ''
      : authSync.ok
        ? '\uff1bopencode \u8ba4\u8bc1\u6587\u4ef6\u5df2\u540c\u6b65\uff0c\u4f46 opencode \u9700\u91cd\u542f\u540e\u624d\u8bfb\u53d6'
        : '\uff1bopencode \u540c\u6b65\u5931\u8d25\uff1a' + authSync.message

    await refresh()
    return {
      ok: true,
      name: targetName,
      message:
        '\u5df2\u5207\u6362\u5230\u300c' + targetName + '\u300d\uff0c\u4e0b\u4e00\u4e2a\u8bf7\u6c42\u5373\u751f\u6548' +
        authNote +
        (repaired ? '\uff1b\u5df2\u6e05\u7406\u6b8b\u7559\u5199\u9501 ' + repaired : ''),
      activeMarker: activeMarker,
      opencodeAuth: authSync,
    }
  }

  /**
   * Next free default label, mirroring TokenHub's automatic naming. The
   * resolved pool is authoritative here, not the document scan: the scan only
   * proposes candidates, so a name it missed would otherwise be handed out
   * twice.
   * @returns a pool name that is not yet taken.
   */
  async function nextDefaultName() {
    const taken = new Set((await resolvePool()).map((entry) => entry.name))
    let n = Math.max(1, taken.size + 1)
    while (taken.has('go' + n) || (await isConfigured(POOL_PREFIX + 'go' + n))) n += 1
    return 'go' + n
  }

  /**
   * Whether the store already holds a value for a reference. The scan cannot
   * see an entry it failed to parse, so this is the authoritative check.
   * @param ref - the reference to test.
   * @returns whether a value is stored.
   */
  async function isConfigured(ref) {
    try {
      const info = await ctx.credentials.describe(ref)
      return Boolean(info && info.configured)
    } catch {
      return false
    }
  }

  /**
   * Add a pool entry after checking the key against the live endpoint, so an
   * invalid key is refused instead of stored (TokenHub behaviour).
   * @param rawName - pool name; an empty string generates the next default.
   * @param rawKey - the API key to validate and store.
   * @returns outcome the client renders as a message.
   */
  async function addAccount(rawName, rawKey) {
    const key = typeof rawKey === 'string' ? rawKey.trim() : ''
    const target =
      typeof rawName === 'string' && rawName.trim().length > 0 ? rawName.trim() : await nextDefaultName()
    if (!POOL_NAME_PATTERN.test(target)) {
      return { ok: false, error: '\u8d26\u53f7\u540d\u53ea\u80fd\u662f 1-32 \u4f4d\u5b57\u6bcd\u3001\u6570\u5b57\u6216\u4e0b\u5212\u7ebf' }
    }
    if (!KEY_PREFIXES.some((prefix) => key.indexOf(prefix) === 0)) {
      return { ok: false, error: '\u5bc6\u94a5\u9700\u4ee5 sk- \u6216 oc_sk_ \u5f00\u5934' }
    }
    const ref = POOL_PREFIX + target
    if (!REF_PATTERN.test(ref)) {
      return { ok: false, error: '\u8d26\u53f7\u540d\u65e0\u6cd5\u7ec4\u6210\u5408\u6cd5\u7684\u51ed\u636e\u540d' }
    }

    try {
      const info = await ctx.credentials.describe(ref)
      if (info && info.configured) return { ok: false, error: '\u8d26\u53f7\u300c' + target + '\u300d\u5df2\u5b58\u5728' }
    } catch {
      /* describe is advisory */
    }

    const probe = await fetchUsage(key, baseUrl, timeoutMs)
    if (probe.error) {
      return { ok: false, error: '\u5bc6\u94a5\u6821\u9a8c\u5931\u8d25\uff1a' + describeError(probe.error, probe.detail) }
    }

    const write = await writeCredential(() => ctx.credentials.set(ref, key))
    if (!write.ok) return { ok: false, error: write.error }
    await refresh()
    return {
      ok: true,
      name: target,
      message:
        '\u5df2\u6dfb\u52a0\u8d26\u53f7\u300c' + target + '\u300d' +
        (write.releasedLock ? '\uff1b\u5df2\u6e05\u7406\u6b8b\u7559\u5199\u9501 ' + write.releasedLock : ''),
    }
  }

  /**
   * Give the key already in force a pool entry of its own. Without this the
   * account the adapter is using would vanish from the panel the moment a
   * switch replaced it, since nothing would hold its value any more. The key
   * needs no re-validation: it is the one already in use.
   * @param rawName - the pool name to file it under.
   * @returns outcome the client renders as a message.
   */
  async function joinPool(rawName) {
    const target = typeof rawName === 'string' ? rawName.trim() : ''
    if (!POOL_NAME_PATTERN.test(target)) {
      return { ok: false, error: '\u8d26\u53f7\u540d\u53ea\u80fd\u662f 1-32 \u4f4d\u5b57\u6bcd\u3001\u6570\u5b57\u6216\u4e0b\u5212\u7ebf' }
    }
    const ref = POOL_PREFIX + target
    if (!REF_PATTERN.test(ref)) {
      return { ok: false, error: '\u8d26\u53f7\u540d\u65e0\u6cd5\u7ec4\u6210\u5408\u6cd5\u7684\u51ed\u636e\u540d' }
    }
    const current = await readCurrent()
    if (current === null) return { ok: false, error: '\u5f53\u524d\u6ca1\u6709\u751f\u6548\u7684\u5bc6\u94a5\u53ef\u5165\u6c60' }
    const pool = await resolvePool()
    if (pool.some((entry) => entry.name === target)) {
      return { ok: false, error: '\u8d26\u53f7\u300c' + target + '\u300d\u5df2\u5b58\u5728' }
    }
    if (pool.some((entry) => entry.key === current)) {
      return { ok: false, error: '\u5f53\u524d\u5bc6\u94a5\u5df2\u7ecf\u5728\u8d26\u53f7\u6c60\u4e2d' }
    }

    const write = await writeCredential(() => ctx.credentials.set(ref, current))
    if (!write.ok) return { ok: false, error: write.error }
    const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, target))
    await refresh()
    const repaired = write.releasedLock || marker.releasedLock || null
    return {
      ok: true,
      name: target,
      message:
        '\u5df2\u628a\u5f53\u524d\u5bc6\u94a5\u5f52\u5165\u8d26\u53f7\u6c60\uff1a\u300c' + target + '\u300d' +
        (repaired ? '\uff1b\u5df2\u6e05\u7406\u6b8b\u7559\u5199\u9501 ' + repaired : ''),
    }
  }

  /**
   * Rename a pool entry. The key itself never moves and the account in force
   * stays in force — only the name the entry is filed under changes. The new
   * reference is written before the old one is dropped, so a failure part-way
   * leaves the key reachable instead of losing it.
   * @param rawFrom - the entry's current pool name.
   * @param rawTo - the name to file it under.
   * @returns outcome the client renders as a message.
   */
  async function renameAccount(rawFrom, rawTo) {
    const from = typeof rawFrom === 'string' ? rawFrom.trim() : ''
    const to = typeof rawTo === 'string' ? rawTo.trim() : ''
    if (!POOL_NAME_PATTERN.test(from) || !POOL_NAME_PATTERN.test(to)) {
      return { ok: false, error: '\u8d26\u53f7\u540d\u53ea\u80fd\u662f 1-32 \u4f4d\u5b57\u6bcd\u3001\u6570\u5b57\u6216\u4e0b\u5212\u7ebf' }
    }
    if (from === to) return { ok: true, name: to, message: '\u540d\u79f0\u672a\u53d8\u5316' }

    const pool = await resolvePool()
    const entry = pool.filter((item) => item.name === from)[0]
    if (!entry) return { ok: false, error: '\u8d26\u53f7\u300c' + from + '\u300d\u4e0d\u5b58\u5728' }
    if (pool.some((item) => item.name === to)) {
      return { ok: false, error: '\u8d26\u53f7\u300c' + to + '\u300d\u5df2\u5b58\u5728' }
    }

    const write = await writeCredential(() => ctx.credentials.set(POOL_PREFIX + to, entry.key))
    if (!write.ok) return { ok: false, error: write.error }

    // The marker names the entry in force, so it has to follow the rename or the
    // panel would lose track of which row is active.
    let markerNote = ''
    const declared = await readDeclaredActive()
    if (declared === from) {
      const marker = await writeCredential(() => ctx.credentials.set(REF_ACTIVE, to))
      if (!marker.ok) markerNote = '\uff1b\u300c\u4f7f\u7528\u4e2d\u300d\u6807\u8bb0\u672a\u80fd\u66f4\u65b0\uff0c\u8bf7\u624b\u52a8\u5207\u6362\u4e00\u6b21'
    }

    const drop = await writeCredential(() => ctx.credentials.unset(POOL_PREFIX + from))
    await refresh()
    if (!drop.ok) {
      return { ok: false, error: '\u65b0\u540d\u5df2\u5199\u5165\uff0c\u4f46\u65e7\u540d\u672a\u80fd\u5220\u9664\uff1a' + drop.error }
    }
    const repaired = write.releasedLock || null
    return {
      ok: true,
      name: to,
      message:
        '\u5df2\u628a\u300c' + from + '\u300d\u6539\u540d\u4e3a\u300c' + to + '\u300d' + markerNote +
        (repaired ? '\uff1b\u5df2\u6e05\u7406\u6b8b\u7559\u5199\u9501 ' + repaired : ''),
    }
  }

  /**
   * Remove a pool entry. The account in force is refused: the adapter resolves
   * it on every request.
   * @param rawName - pool name to drop.
   * @returns outcome the client renders as a message.
   */
  async function removeAccount(rawName) {
    const target = typeof rawName === 'string' ? rawName.trim() : ''
    if (!POOL_NAME_PATTERN.test(target)) return { ok: false, error: '\u8d26\u53f7\u540d\u4e0d\u5408\u6cd5' }
    const pool = await resolvePool()
    const entry = pool.filter((item) => item.name === target)[0]
    if (!entry) return { ok: false, error: '\u8d26\u53f7\u300c' + target + '\u300d\u4e0d\u5b58\u5728' }
    const current = await readCurrent()
    if (current !== null && entry.key === current) {
      return { ok: false, error: '\u4f7f\u7528\u4e2d\u7684\u8d26\u53f7\u4e0d\u80fd\u5220\u9664\uff0c\u8bf7\u5148\u5207\u6362\u5230\u5176\u4ed6\u8d26\u53f7' }
    }
    const write = await writeCredential(() => ctx.credentials.unset(POOL_PREFIX + target))
    if (!write.ok) return { ok: false, error: write.error }
    await refresh()
    return { ok: true, name: target, message: '\u5df2\u5220\u9664\u8d26\u53f7\u300c' + target + '\u300d' }
  }

  refresh()
  ctx.interval(() => {
    refresh()
  }, refreshMs)

  // The web server exists only in a web composition; a headless profile still
  // gets the polling and switching logic for programmatic callers.
  const webServer = ctx.get('webServer')
  if (webServer !== undefined) {
    /**
     * Wrap a route registration as a fiber effect so unloading removes it.
     * @param route - the route description handed to the web server.
     * @param label - effect label for diagnostics.
     */
    const route = (route, label) => ctx.effect(() => webServer.register(route), 'dsh-tokenhub: ' + label)

    route(
      {
        kind: 'exact',
        path: ROUTE_BASE + '/snapshot',
        handler: async (req, res) => {
          let force = false
          try {
            force = new URL(req.url || '/', 'http://localhost').searchParams.get('force') === '1'
          } catch {
            force = false
          }
          if (force || Date.now() - cache.updatedAt > refreshMs) await refresh()
          sendJson(res, 200, Object.assign({}, cache, { thresholds: thresholds }))
        },
      },
      'snapshot route',
    )

    route(
      {
        kind: 'exact',
        path: ROUTE_BASE + '/switch',
        handler: async (req, res) => {
          if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: 'method not allowed' })
          if (!sameOrigin(req)) return sendJson(res, 403, { ok: false, error: 'forbidden' })
          const body = await readBody(req)
          if (!body.ok) return sendJson(res, 400, { ok: false, error: body.reason })
          const target = body.value ? body.value.name : undefined
          if (typeof target !== 'string' || !POOL_NAME_PATTERN.test(target)) {
            return sendJson(res, 400, { ok: false, error: '\u8d26\u53f7\u540d\u4e0d\u5408\u6cd5' })
          }
          const result = await switchTo(target)
          sendJson(res, result.ok ? 200 : 400, result)
        },
      },
      'switch route',
    )

    route(
      {
        kind: 'exact',
        path: ROUTE_BASE + '/account',
        handler: async (req, res) => {
          if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: 'method not allowed' })
          if (!sameOrigin(req)) return sendJson(res, 403, { ok: false, error: 'forbidden' })
          const body = await readBody(req)
          if (!body.ok) return sendJson(res, 400, { ok: false, error: body.reason })
          const op = body.value ? body.value.op : undefined
          if (op === 'add') {
            const result = await addAccount(body.value.name, body.value.key)
            return sendJson(res, result.ok ? 200 : 400, result)
          }
          if (op === 'remove') {
            const result = await removeAccount(body.value.name)
            return sendJson(res, result.ok ? 200 : 400, result)
          }
          if (op === 'joinPool') {
            const result = await joinPool(body.value.name)
            return sendJson(res, result.ok ? 200 : 400, result)
          }
          if (op === 'rename') {
            const result = await renameAccount(body.value.name, body.value.to)
            return sendJson(res, result.ok ? 200 : 400, result)
          }
          sendJson(res, 400, { ok: false, error: 'unknown op' })
        },
      },
      'account route',
    )
  }
}

export { apply, inject, name }
