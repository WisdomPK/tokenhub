/**
 * dsh-tokenhub — snapshot consistency and request scheduling.
 *
 * One batch may be in flight. Every account change bumps a revision; a batch
 * started under an older revision may finish, but it is never committed. The
 * next snapshot() waits for a batch of the current generation, so an old poll
 * cannot overwrite the identity a switch just installed.
 *
 * A failed window keeps its last valid reading and reports stale=true with the
 * failure that displaced it; lastAttemptAt and lastSuccessAt are per window.
 * A whole-batch failure keeps the previous entries and marks them stale instead
 * of discarding what the user was looking at.
 *
 * This is an in-process guarantee only. The credential document is shared with
 * other tools; a change made by another process is detected by comparing the
 * identity read before and after the fetch, and at the latest appears on the
 * next successful read. No cross-process atomic read is claimed.
 */

/** Requests one snapshot may have in flight against the usage endpoint. */
const MAX_CONCURRENCY = 8

/** Window order, matching the panel. */
const WINDOWS = ['rolling', 'weekly', 'monthly']

/**
 * @param readAccounts - async () => {rows, fingerprint}; rows carry host-private
 *   key values and the display/identity fields the snapshot exposes.
 * @param fetchUsages - async (keys, {signal}) => Map<key, usage>.
 * @param now - clock, injectable for tests.
 * @param refreshMs - how long a committed snapshot stays fresh.
 */
export function createSnapshotService({ readAccounts, fetchUsages, now = Date.now, refreshMs = 60000 }) {
  if (typeof readAccounts !== 'function') throw new TypeError('readAccounts must be a function')
  if (typeof fetchUsages !== 'function') throw new TypeError('fetchUsages must be a function')

  let cache = { updatedAt: 0, error: null, entries: [] }
  let cacheRevision = -1
  let revision = 0
  let stale = true
  let disposed = false
  let active = null

  /** Last good reading per host key and window; never exposed with the key. */
  const history = new Map()

  const copy = () => Object.assign({}, cache, { entries: cache.entries.slice() })

  /** Account state changed: the next snapshot must be a new generation. */
  function invalidate() {
    if (disposed) return
    revision += 1
    stale = true
    cacheRevision = -1
    cache = Object.assign({}, cache, { updatedAt: 0 })
  }

  /** Stop committing and cancel requests that honor the batch signal. */
  function dispose() {
    disposed = true
    if (active) active.controller.abort()
  }

  /** Unique keys, each fetched at most once, never more than eight at a time. */
  async function fetchAll(rows, signal) {
    const keys = []
    const seen = new Set()
    for (const row of rows) {
      if (seen.has(row.key)) continue
      seen.add(row.key)
      keys.push(row.key)
    }
    const usage = new Map()
    let cursor = 0
    const worker = async () => {
      while (!signal.aborted) {
        const index = cursor
        cursor += 1
        if (index >= keys.length) return
        const key = keys[index]
        const result = await fetchUsages([key], { signal })
        if (result instanceof Map) {
          for (const [resultKey, value] of result) usage.set(resultKey, value)
        } else if (result && typeof result === 'object') {
          for (const resultKey of Object.keys(result)) usage.set(resultKey, result[resultKey])
        }
      }
    }
    const workers = Array.from({ length: Math.min(MAX_CONCURRENCY, keys.length) }, () => worker())
    await Promise.all(workers)
    return usage
  }

  /**
   * One window in the output. A valid reading replaces the stored history; an
   * unavailable window reuses the last good reading and is marked stale. The
   * first-ever unavailable window stays unavailable (false stale) because there
   * is no "last data" to show.
   */
  function windowReading(key, name, reading, attemptedAt) {
    const raw = reading && reading.windows ? reading.windows[name] : null
    const valid = raw && typeof raw.percent === 'number'
    let store = history.get(key)
    if (!store) {
      store = {}
      history.set(key, store)
    }
    if (valid) {
      store[name] = {
        status: raw.status,
        percent: raw.percent,
        resetsAt: raw.resetsAt,
        lastSuccessAt: attemptedAt,
      }
      return {
        status: raw.status,
        percent: raw.percent,
        resetsAt: raw.resetsAt,
        error: null,
        stale: false,
        lastAttemptAt: attemptedAt,
        lastSuccessAt: attemptedAt,
      }
    }
    const previous = store[name]
    const error = (reading && reading.error) || 'no-data'
    if (previous) {
      return {
        status: previous.status,
        percent: previous.percent,
        resetsAt: previous.resetsAt,
        error,
        stale: true,
        lastAttemptAt: attemptedAt,
        lastSuccessAt: previous.lastSuccessAt,
      }
    }
    return {
      status: raw ? raw.status : null,
      percent: null,
      resetsAt: raw ? raw.resetsAt : null,
      error,
      stale: false,
      lastAttemptAt: attemptedAt,
      lastSuccessAt: null,
    }
  }

  /** Mark every previously readable window of an entry as stale. */
  function staleWindows(windows, error, attemptedAt) {
    const output = {}
    for (const name of WINDOWS) {
      const window = windows && windows[name]
      if (!window) continue
      if (typeof window.percent !== 'number') {
        output[name] = Object.assign({}, window, { lastAttemptAt: attemptedAt })
        continue
      }
      output[name] = Object.assign({}, window, {
        stale: true,
        error,
        lastAttemptAt: attemptedAt,
      })
    }
    return output
  }

  /** One fetch pass for one revision; commits only if that revision still stands. */
  async function runBatch(batchRevision, controller) {
    const before = await readAccounts()
    if (disposed || controller.signal.aborted || batchRevision !== revision) return

    const usage = await fetchAll(before.rows, controller.signal)
    if (disposed || controller.signal.aborted || batchRevision !== revision) return

    const after = await readAccounts()
    if (disposed || controller.signal.aborted || batchRevision !== revision) return

    if (!after || after.fingerprint !== before.fingerprint) {
      // Something outside this plugin changed the accounts during the fetch.
      // Nothing from this batch may be published; read again next round.
      revision += 1
      stale = true
      cacheRevision = -1
      cache = Object.assign({}, cache, { updatedAt: 0 })
      return
    }

    const attemptedAt = now()
    const entries = before.rows.map((row) => {
      const reading = usage.get(row.key) || { error: 'internal', detail: 'usage result missing', windows: null }
      const windows = {}
      for (const name of WINDOWS) windows[name] = windowReading(row.key, name, reading, attemptedAt)
      // A malformed whole response stays distinguishable from "this window is
      // unavailable": it has no window object to show, not three empty values.
      const malformed = reading.error === 'bad-json' && !reading.windows
      return {
        id: row.id,
        name: row.name,
        masked: row.masked,
        active: row.active,
        inPool: row.inPool,
        error: reading.error || null,
        detail: reading.detail || null,
        windows: malformed ? null : windows,
      }
    })

    // Drop history for accounts no longer present.
    const liveKeys = new Set(before.rows.map((row) => row.key))
    for (const key of history.keys()) {
      if (!liveKeys.has(key)) history.delete(key)
    }

    cache = { updatedAt: attemptedAt, error: entries.length === 0 ? 'no-keys' : null, entries }
    cacheRevision = batchRevision
    stale = false
  }

  function startBatch(batchRevision) {
    const controller = new AbortController()
    const promise = runBatch(batchRevision, controller).then(
      () => ({ failed: false, current: batchRevision === revision }),
      (error) => {
        // A batch from an older generation may fail; it may not touch the cache
        // or clear the dirty flag. Only a current-generation failure is
        // reported, and it leaves stale set so the next snapshot retries.
        if (disposed || controller.signal.aborted) return { failed: true, current: false }
        if (batchRevision !== revision) return { failed: true, current: false }
        console.error('[dsh-tokenhub] snapshot refresh failed:', error)
        const message = 'internal: ' + String((error && error.message) || error)
        const attemptedAt = now()
        const entries = cache.entries.map((entry) =>
          Object.assign({}, entry, { windows: staleWindows(entry.windows, message, attemptedAt) }),
        )
        cache = { updatedAt: attemptedAt, error: message, entries }
        cacheRevision = batchRevision
        stale = true
        return { failed: true, current: true }
      },
    )
    promise.finally(() => {
      if (active && active.promise === promise) active = null
    })
    active = { revision: batchRevision, promise, controller }
    return promise
  }

  /** Wait until the cache belongs to the current generation. */
  async function ensure(force) {
    if (disposed) return
    if (force) stale = true
    while (!disposed) {
      if (active) {
        const batch = active
        const outcome = await batch.promise
        if (disposed) return
        if (outcome && outcome.failed && outcome.current) break
        if (stale) continue
        if (cacheRevision === revision) break
        continue
      }
      if (!stale && cacheRevision === revision && cache.updatedAt > 0 && now() - cache.updatedAt <= refreshMs) break
      const outcome = await startBatch(revision)
      if (outcome && outcome.failed && outcome.current) break
    }
  }

  /**
   * The snapshot the widget polls. force bypasses freshness but still shares a
   * single in-flight batch with every caller of the same revision.
   */
  async function snapshot(force = false) {
    await ensure(force === true)
    return copy()
  }

  return { snapshot, invalidate, dispose }
}
