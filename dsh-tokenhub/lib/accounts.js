/**
 * dsh-tokenhub — account identity and mutations.
 *
 * The credential store is the only persistence. This module reads the pool,
 * decides which pool name is in force, validates names, serializes every
 * account change through one queue, and returns structured outcomes. It never
 * waits for a usage refresh and never touches the browser snapshot.
 */

import { existsSync, readFileSync, rmSync, statSync } from 'node:fs'
import { syncOpencodeAuth } from './opencode-auth.js'
import { describeError, fetchUsage, hasUsableWindow } from './usage.js'

/** The reference the configured llm adapter resolves on every request. */
export const REF_CURRENT = 'OPENCODE_GO_API_KEY'

/** Pool entries are filed as POOL_PREFIX + name. */
export const POOL_PREFIX = 'OPENCODE_GO_KEY_'

/** Names the active pool entry. */
export const REF_ACTIVE = 'OPENCODE_GO_KEY_ACTIVE'

/** Row id for the key in force when no pool entry holds it. */
export const CURRENT_ROW_ID = 'current'

/** A user-supplied pool name; the joined reference must still be a legal one. */
export const POOL_NAME_PATTERN = /^[A-Za-z0-9_]{1,32}$/

/** POSIX shell identifier: the grammar a credential reference accepts. */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

/** Accepted OpenCode Go key shapes (new oc_sk_ and legacy sk-). */
const KEY_PREFIXES = ['sk-', 'oc_sk_']

/** Shown wherever a pool name fails POOL_NAME_PATTERN. */
const MSG_BAD_NAME = '账号名只能是 1-32 位字母、数字或下划线'

/** Names that already mean something in the credential store. */
const RESERVED_NAMES = new Set(['ACTIVE'])

/** Shown wherever a reserved name is requested. */
const MSG_RESERVED_NAME = '「ACTIVE」是系统保留名称，不能作为账号名'

/** TokenHub's mask rule: a leading run, an ellipsis, and the last four. */
export function maskKey(key) {
  if (typeof key !== 'string' || key.length === 0) return ''
  if (key.startsWith('oc_sk_') && key.length >= 16) return key.slice(0, 8) + '…' + key.slice(-4)
  if (key.length >= 12) return key.slice(0, 6) + '…' + key.slice(-4)
  return '***'
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
export function selectActiveName(pool, currentKey, declaredName) {
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
export function validatePoolName(raw) {
  const value = typeof raw === 'string' ? raw.trim() : ''
  if (!POOL_NAME_PATTERN.test(value)) return { ok: false, error: MSG_BAD_NAME }
  if (RESERVED_NAMES.has(value)) return { ok: false, error: MSG_RESERVED_NAME }
  return { ok: true, name: value }
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
export function scanPoolNames(text) {
  const found = []
  const seen = new Set()
  // Optional quotes around the name, since a hand-written YAML file may have them.
  const quote = '[\x22\x27]?'
  const pattern = new RegExp('^[ \t]*' + quote + POOL_PREFIX + '([A-Za-z0-9_]+)' + quote + '[ \t]*:', 'gm')
  let match
  while ((match = pattern.exec(text)) !== null) {
    const candidate = match[1]
    if (RESERVED_NAMES.has(candidate) || seen.has(candidate)) continue
    seen.add(candidate)
    found.push(candidate)
  }
  return found
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
 * Create the account service over one credential store.
 * @param credentials - the host credentials service (resolve/describe/set/unset).
 * @param options - normalized plugin settings.
 */
export function createAccountService(credentials, options) {
  const {
    baseUrl,
    timeoutMs,
    syncAuth,
    authPath,
    credentialsPath,
    explicitNames,
    autoReleaseLock,
    wedgedLockMinAgeMs,
  } = options

  /**
   * A configured keyNames list is a fixed allowlist, not a discovery hint: the
   * pool may only ever hold these names, and every mutation target must already
   * be listed. Null means discovery mode, where the document scan decides.
   * Duplicates are collapsed so discovery and the allowlist agree.
   */
  const allowedNames =
    explicitNames === null ? null : new Set(explicitNames.filter((name) => name.length > 0))

  /** Whether a target name may be created or filed; discovery allows any legal name. */
  const nameAllowed = (name) => allowedNames === null || allowedNames.has(name)

  /** Shared refusal for an add/join/rename target outside the fixed list. */
  const unlistedNameError = (name) =>
    '账号名「' + name + '」不在配置 keyNames 允许列表中，请先修改配置 keyNames 后再试'

  /** A blank add cannot generate a name the fixed list would make invisible. */
  const unlistedGeneratedError = (name) =>
    '自动生成的账号名「' + name + '」不在配置 keyNames 允许列表中；请填写列表中的账号名，或把「' + name + '」加入配置 keyNames 后再试'

  /**
   * Resolve one reference. A missing value is null; a storage failure is an
   * error with its cause attached, never an empty pool.
   */
  async function resolveRef(ref) {
    let hit
    try {
      hit = await credentials.resolve(ref)
    } catch (error) {
      throw new Error('凭据存储读取失败（' + ref + '）：' + String((error && error.message) || error))
    }
    return hit && hit.value ? hit.value : null
  }

  /** Advisory view of a reference; a storage failure is not "not configured". */
  async function describeRef(ref) {
    try {
      return (await credentials.describe(ref)) || null
    } catch (error) {
      throw new Error('凭据存储读取失败（' + ref + '）：' + String((error && error.message) || error))
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

  /** Candidate names: the explicit allowlist, else the document scan. */
  function discoverPoolNames() {
    // In explicit mode keyNames is the whole pool: every listed name stays
    // discoverable (and therefore operable) across restarts, and no unlisted
    // name can appear. A repeated name must not become a duplicate row; the
    // set already collapsed it.
    if (allowedNames !== null) return Array.from(allowedNames)
    let text = ''
    try {
      if (existsSync(credentialsPath)) text = readFileSync(credentialsPath, 'utf8')
    } catch (error) {
      throw new Error('凭据文件读取失败：' + String((error && error.message) || error))
    }
    return scanPoolNames(text)
  }

  /** Confirm candidates against the store and resolve their values (host-side only). */
  async function resolvePool() {
    const pool = []
    for (const candidate of discoverPoolNames()) {
      if (RESERVED_NAMES.has(candidate)) continue
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
   * Host-private account state: identity rows plus a fingerprint of the inputs
   * that decided them. The fingerprint lets the snapshot service refuse a batch
   * whose account state changed while its requests were in flight.
   */
  async function readAccounts() {
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

  /** Move the active account and mirror it only when sync was requested. */
  async function switchTo(targetName) {
    const named = validatePoolName(targetName)
    if (!named.ok) return fail(named.error)
    const target = named.name

    return enqueueMutation(async () => {
      const next = await resolveRef(POOL_PREFIX + target)
      if (!next) return fail('账号「' + target + '」不存在或密钥为空')

      // A read-only source shadows the reference: the write would report success
      // while resolution kept returning the shadowing value.
      const info = await describeRef(REF_CURRENT)
      if (info && info.writable === false) {
        return fail('当前密钥由启动环境提供（只读），无法切换：请先清除环境变量 ' + REF_CURRENT)
      }

      const previous = await readCurrent()
      const write = await writeCredential(() => credentials.set(REF_CURRENT, next))
      if (!write.ok) return fail(write.error)

      // Resolution is per call, so reading back is the authoritative check.
      if (!(await readBack(REF_CURRENT, next))) {
        let restored = false
        if (previous !== null) {
          const undo = await writeCredential(() => credentials.set(REF_CURRENT, previous))
          // Only a read-back may call the rollback restored.
          restored = undo.ok && (await readBack(REF_CURRENT, previous))
        }
        return fail(restored ? '读回校验失败，已恢复原账号' : '读回校验失败，状态待确认', { changed: !restored })
      }

      const marker = await writeCredential(() => credentials.set(REF_ACTIVE, target))
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
        changed: true,
      })
    })
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
      // A fixed allowlist refuses a target it could never discover again.
      if (!nameAllowed(requestedName)) return fail(unlistedNameError(requestedName))
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

    return enqueueMutation(async () => {
      // Default naming is inside the queue too: two blank-name adds must not
      // hand out the same goN.
      const target = requestedName !== null ? requestedName : await nextDefaultName()
      // A generated name is usable only when the fixed list can discover it
      // again; otherwise filling a listed name is the way forward.
      if (requestedName === null && !nameAllowed(target)) return fail(unlistedGeneratedError(target))
      const ref = POOL_PREFIX + target
      if (await isConfigured(ref)) return fail('账号「' + target + '」已存在')

      const write = await writeCredential(() => credentials.set(ref, key))
      if (!write.ok) return fail(write.error)

      if (!(await readBack(ref, key))) {
        const undo = await writeCredential(() => credentials.unset(ref))
        const cleared = undo.ok && (await resolveRef(ref)) === null
        return fail(cleared ? '写入后读回校验失败，已撤销本次写入' : '写入后读回校验失败，状态待确认', { changed: true })
      }
      return succeed({
        name: target,
        message: '已添加账号「' + target + '」' + lockNote(write.releasedLock),
        changed: true,
      })
    })
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
    if (!nameAllowed(target)) return fail(unlistedNameError(target))

    return enqueueMutation(async () => {
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
      const write = await writeCredential(() => credentials.set(ref, current))
      if (!write.ok) return fail(write.error)
      if (!(await readBack(ref, current))) {
        const undo = await writeCredential(() => credentials.unset(ref))
        const cleared = undo.ok && (await resolveRef(ref)) === null
        return fail(cleared ? '入池后读回校验失败，已撤销本次写入' : '入池后读回校验失败，状态待确认', { changed: true })
      }

      const marker = await writeCredential(() => credentials.set(REF_ACTIVE, target))
      const warnings = []
      if (!marker.ok) warnings.push({ code: 'active-marker', message: '「使用中」标记写入失败：' + marker.error })
      return succeed({
        name: target,
        message:
          '已把当前密钥归入账号池：「' + target + '」' +
          (marker.ok ? '' : '；「使用中」标记写入失败：' + marker.error) +
          lockNote(write.releasedLock || marker.releasedLock),
        warnings,
        changed: true,
      })
    })
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
    // Refuse before anything is written: a new name the fixed list cannot
    // discover would make the account vanish from the panel after the rename.
    if (!nameAllowed(to)) return fail(unlistedNameError(to))
    if (from === to) return succeed({ name: to, message: '名称未变化' })

    return enqueueMutation(async () => {
      const pool = await resolvePool()
      const entry = pool.find((item) => item.name === from)
      if (!entry) return fail('账号「' + from + '」不存在')
      // The target is already in the explicit allowlist (checked before the
      // queue); describe still catches a configured target the scan cannot list.
      if (await isConfigured(POOL_PREFIX + to)) return fail('账号「' + to + '」已存在')

      const targetRef = POOL_PREFIX + to
      const write = await writeCredential(() => credentials.set(targetRef, entry.key))
      if (!write.ok) return fail(write.error)
      if (!(await readBack(targetRef, entry.key))) {
        const undo = await writeCredential(() => credentials.unset(targetRef))
        const cleared = undo.ok && (await resolveRef(targetRef)) === null
        return fail(cleared ? '新名写入后读回校验失败，已撤销写入' : '新名写入后读回校验失败，状态待确认', { changed: true })
      }

      // The marker names the entry in force, so it has to follow the rename or
      // the panel would lose track of which row is active.
      const warnings = []
      let markerNote = ''
      if ((await readDeclaredActive()) === from) {
        const marker = await writeCredential(() => credentials.set(REF_ACTIVE, to))
        if (!marker.ok) {
          markerNote = '；「使用中」标记未能更新，请手动切换一次'
          warnings.push({ code: 'active-marker', message: '「使用中」标记未能更新：' + marker.error })
        }
      }

      const drop = await writeCredential(() => credentials.unset(POOL_PREFIX + from))
      if (!drop.ok) {
        // The key is reachable under the new name, but both names now exist.
        const message = '新名已写入，但旧名未能删除：' + drop.error
        return fail(message, {
          warnings: warnings.concat([{ code: 'partial-rename', message }]),
          name: to,
          partial: true,
          changed: true,
        })
      }
      return succeed({
        name: to,
        message: '已把「' + from + '」改名为「' + to + '」' + markerNote + lockNote(write.releasedLock),
        warnings,
        changed: true,
      })
    })
  }

  /**
   * Remove a pool entry. The account in force is refused: the adapter resolves
   * it on every request.
   */
  async function removeAccount(rawName) {
    const named = validatePoolName(rawName)
    if (!named.ok) return fail(named.error)
    const target = named.name

    return enqueueMutation(async () => {
      const pool = await resolvePool()
      const entry = pool.find((item) => item.name === target)
      if (!entry) return fail('账号「' + target + '」不存在')
      // Read the current key after earlier queued writes, not before them.
      const current = await readCurrent()
      if (current !== null && entry.key === current) {
        return fail('使用中的账号不能删除，请先切换到其他账号')
      }

      const ref = POOL_PREFIX + target
      const write = await writeCredential(() => credentials.unset(ref))
      if (!write.ok) return fail(write.error)
      if ((await resolveRef(ref)) !== null) return fail('删除后读回校验失败，状态待确认', { changed: true })
      return succeed({ name: target, message: '已删除账号「' + target + '」', changed: true })
    })
  }

  return { readAccounts, switchTo, addAccount, joinPool, renameAccount, removeAccount }
}
