/**
 * dsh-tokenhub — optional opencode auth.json mirroring.
 *
 * One switch in dsh can optionally update opencode's auth document. Only the
 * opencode-go field is touched; backup, same-directory temp file, atomic rename,
 * read-back check, and rollback all stay here. An explicit path is the only
 * target: a missing one is created, a corrupt one is reported, and another
 * candidate is never rewritten in its place.
 */

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { homedir } from 'node:os'

/** Backups kept beside auth.json. */
export const BACKUP_KEEP = 5

/** Read a JSON document, tolerating a UTF-8 BOM written by another tool. */
function readJsonFile(target) {
  const raw = readFileSync(target, 'utf8')
  return JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw)
}

/** Cleanup is best effort, but a failed cleanup is worth knowing about. */
function warnCleanup(target, error) {
  console.warn('[dsh-tokenhub] failed to remove temporary auth file ' + target + ':', error)
}

/**
 * Resolve the opencode auth document: an explicit override, then an explicit
 * environment path, then automatic discovery.
 *
 * An explicit path is the only target. If it is missing the sync creates it
 * there; if it is corrupt the sync reports that target's problem. It must never
 * silently move the write to another file that happens to look healthier.
 * @returns the path to read and write.
 */
export function resolveOpencodeAuthPath(override) {
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
export function pruneBackups(target, keep) {
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
export function restoreAuth(target, backupPath, existed) {
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
      } catch (cleanupError) {
        warnCleanup(tmp, cleanupError)
      }
      return failed()
    }
    // Restored means the target now byte-equals the backup, read back.
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
export function syncOpencodeAuth(key, override) {
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
    } catch (cleanupError) {
      warnCleanup(tmp, cleanupError)
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
