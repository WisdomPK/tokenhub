/**
 * dsh-tokenhub — snapshot consistency and request scheduling.
 *
 * One batch may be in flight. Every account change bumps a revision; a batch
 * started under an older revision may finish, but it is never committed. The
 * next snapshot() waits for a batch of the current generation, so an old poll
 * cannot overwrite the identity a switch just installed.
 *
 * This is an in-process guarantee only. The credential document is shared with
 * other tools; a change made by another process is detected by comparing the
 * identity read before and after the fetch, and at the latest appears on the
 * next successful read. No cross-process atomic read is claimed.
 */

/** Requests one snapshot may have in flight against the usage endpoint. */
const MAX_CONCURRENCY = 8

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
  let revision = 0
  let stale = true
  let disposed = false
  let active = null

  const copy = () => Object.assign({}, cache, { entries: cache.entries.slice() })

  /** Account state changed: the next snapshot must be a new generation. */
  function invalidate() {
    if (disposed) return
    revision += 1
    stale = true
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
      cache = Object.assign({}, cache, { updatedAt: 0 })
      return
    }

    const entries = before.rows.map((row) => {
      const reading = usage.get(row.key) || { error: 'internal', detail: 'usage result missing' }
      return {
        id: row.id,
        name: row.name,
        masked: row.masked,
        active: row.active,
        inPool: row.inPool,
        error: reading.error,
        detail: reading.detail || null,
        windows: reading.windows || null,
      }
    })
    cache = { updatedAt: now(), error: entries.length === 0 ? 'no-keys' : null, entries }
    stale = false
  }

  function startBatch(batchRevision) {
    const controller = new AbortController()
    const promise = runBatch(batchRevision, controller)
      .catch((error) => {
        if (disposed) return
        console.error('[dsh-tokenhub] snapshot refresh failed:', error)
        cache = {
          updatedAt: now(),
          error: 'internal: ' + String((error && error.message) || error),
          entries: [],
        }
        stale = false
      })
      .finally(() => {
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
        await batch.promise
        if (disposed) return
        // A batch invalidated while it ran leaves stale set; loop for a new one.
        if (stale) continue
        if (batch.revision === revision) break
        continue
      }
      if (!stale && cache.updatedAt > 0 && now() - cache.updatedAt <= refreshMs) break
      await startBatch(revision)
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
