/**
 * dsh-tokenhub — usage endpoint boundary.
 *
 * One HTTP request and the parsing rules shared with the desktop half through
 * tests/fixtures/usage-contract.json. Nothing here knows about accounts or the
 * snapshot cache.
 */

/** Endpoint failure code to user-facing text. */
const ERROR_TEXT = new Map([
  ['unauthorized', '密钥无效（401）'],
  ['timeout', '请求超时'],
  ['network', '网络错误'],
  ['bad-json', '响应解析失败'],
])

/** Turn a transport or endpoint failure code into the user-facing short text. */
export function describeError(code, detail) {
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
export function parsePercent(raw) {
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
export function parseResetsAt(raw) {
  if (typeof raw !== 'string') return null
  const text = raw.trim()
  const match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:\d{2})?$/.exec(text)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const hour = Number(match[4])
  const minute = Number(match[5])
  const second = match[6] === undefined ? 0 : Number(match[6])
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) return null
  // Date.parse would normalize 2026-02-30 into March; reject impossible calendar
  // dates instead of quietly changing the reset day.
  const calendar = new Date(0)
  calendar.setUTCHours(0, 0, 0, 0)
  calendar.setUTCFullYear(year, month - 1, day)
  if (
    calendar.getUTCFullYear() !== year ||
    calendar.getUTCMonth() !== month - 1 ||
    calendar.getUTCDate() !== day
  ) {
    return null
  }
  const fraction = match[7] === undefined ? '' : '.' + match[7]
  const zone = match[8] || 'Z'
  const normalized =
    match[1] + '-' + match[2] + '-' + match[3] + 'T' +
    match[4] + ':' + match[5] + ':' + String(second).padStart(2, '0') + fraction + zone
  const parsed = Date.parse(normalized)
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null
}

/** Defensive parse of one usage window; a malformed window degrades to nulls. */
export function pickWindow(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { status: null, percent: null, resetsAt: null }
  }
  // Prefer resetsAt only while it parses; an absent/empty/garbage primary must
  // not discard a usable legacy resets_at.
  const primary = parseResetsAt(raw.resetsAt)
  const resetsAt = primary !== null ? primary : parseResetsAt(raw.resets_at)
  return {
    status: typeof raw.status === 'string' ? raw.status : null,
    percent: parsePercent(raw.percent),
    resetsAt,
  }
}

/** Whether any window carries a real reading; a 200 alone proves nothing. */
export function hasUsableWindow(windows) {
  return Boolean(
    windows && Object.values(windows).some((window) => window && typeof window.percent === 'number'),
  )
}

/**
 * Fetch one key's usage. Never throws: every failure becomes a displayable code
 * so one bad key cannot take down the pool.
 * @returns error code plus, on success, the three parsed windows.
 */
export async function fetchUsage(key, baseUrl, timeoutMs, signal) {
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
