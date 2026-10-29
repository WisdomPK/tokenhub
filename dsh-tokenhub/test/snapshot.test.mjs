// dsh-tokenhub — snapshot consistency test.
//
// Drives createSnapshotService directly with deferred promises so every
// interleaving is decided by the test, not by sleeps.
//
//   node test/snapshot.test.mjs

import { createSnapshotService } from '../lib/snapshot.js'

const results = []
const check = (label, cond, extra) => results.push((cond ? 'PASS  ' : 'FAIL  ') + label + (extra ? '   [' + extra + ']' : ''))

const tick = () => new Promise((resolve) => setImmediate(resolve))
const waitFor = async (condition, label = 'condition') => {
  const until = Date.now() + 2000
  while (Date.now() < until) {
    if (condition()) return true
    await tick()
  }
  throw new Error('timed out waiting for ' + label)
}

const row = (id, name, key, active, inPool) => ({ id, name, key, masked: 'masked-' + id, active, inPool })
const identity = (rows) => ({ rows, fingerprint: JSON.stringify(rows.map((r) => [r.id, r.name, r.key, r.active, r.inPool])) })
const reading = (percent) => ({
  error: null,
  detail: null,
  windows: {
    rolling: { status: 'ok', percent, resetsAt: null },
    weekly: { status: 'ok', percent: percent + 1, resetsAt: null },
    monthly: { status: 'ok', percent: percent + 2, resetsAt: null },
  },
})

try {
  // ================= an old poll cannot commit the pre-switch identity =================
  {
    let accounts = identity([row('current', '', 'KEY_A', true, false)])
    const deferred = new Map()
    const requested = []
    const fetchUsages = (keys) => {
      const key = keys[0]
      requested.push(key)
      return new Promise((resolve) => {
        deferred.set(key, () => resolve(new Map([[key, reading(key === 'KEY_A' ? 10 : 70)]])))
      })
    }
    const service = createSnapshotService({
      readAccounts: async () => accounts,
      fetchUsages,
      now: () => 1000,
      refreshMs: 60000,
    })
    const first = service.snapshot(true)
    await waitFor(() => deferred.has('KEY_A'), 'the first identity fetch')

    accounts = identity([row('current', '', 'KEY_B', true, false)])
    service.invalidate()
    const second = service.snapshot(false)
    await tick()
    deferred.get('KEY_A')()
    await waitFor(() => requested.includes('KEY_B'), 'the post-switch fetch')
    deferred.get('KEY_B')()
    const [firstResult, secondResult] = await Promise.all([first, second])
    check(
      'the old poll result is not published after a switch',
      secondResult.entries.length === 1 && secondResult.entries[0].name === '' && secondResult.entries[0].masked === 'masked-current',
      JSON.stringify(secondResult.entries.map((e) => [e.id, e.active])),
    )
    check('the old identity never becomes the current entry', !requested.slice(0, 1).includes('KEY_B') && requested.join(',') === 'KEY_A,KEY_B', requested.join(','))
    check('the caller that awaited the old batch also settles on the new identity', firstResult.entries[0].masked === 'masked-current')
  }

  // ================= concurrent refreshes of one revision share one batch =================
  {
    const accounts = identity([row('pool:a', 'a', 'KEY_A', true, true)])
    let calls = 0
    let release
    const fetchUsages = (keys) => {
      calls += 1
      return new Promise((resolve) => {
        release = () => resolve(new Map([[keys[0], reading(10)]]))
      })
    }
    const service = createSnapshotService({
      readAccounts: async () => accounts,
      fetchUsages,
      now: () => 1000,
      refreshMs: 60000,
    })
    const one = service.snapshot(true)
    const two = service.snapshot(true)
    await waitFor(() => typeof release === 'function', 'the shared batch')
    check('two concurrent refreshes issue one request', calls === 1, String(calls))
    release()
    const [a, b] = await Promise.all([one, two])
    check('both callers receive the same snapshot', a.entries[0].masked === 'masked-pool:a' && b.entries[0].masked === 'masked-pool:a')
    check('no second request was made', calls === 1, String(calls))
  }

  // ================= invalidate after a fresh commit forces the next generation =================
  {
    let accounts = identity([row('current', '', 'KEY_A', true, false)])
    let calls = 0
    const fetchUsages = async (keys) => {
      calls += 1
      return new Map([[keys[0], reading(keys[0] === 'KEY_A' ? 10 : 70)]])
    }
    const service = createSnapshotService({
      readAccounts: async () => accounts,
      fetchUsages,
      now: () => 1000,
      refreshMs: 60000,
    })
    const first = await service.snapshot(false)
    check('the initial snapshot is committed', first.entries[0].masked === 'masked-current' && calls === 1, String(calls))
    accounts = identity([row('current', '', 'KEY_B', true, false)])
    service.invalidate()
    const next = await service.snapshot(false)
    check('invalidate makes the next snapshot wait for a new generation', calls === 2 && next.entries.length === 1, String(calls))
  }

  // ================= rapid changes coalesce into the final state =================
  {
    let accounts = identity([row('current', '', 'KEY_A', true, false)])
    const deferred = new Map()
    const requested = []
    const fetchUsages = (keys) => {
      const key = keys[0]
      requested.push(key)
      return new Promise((resolve) => {
        deferred.set(key, () => resolve(new Map([[key, reading(10)]])))
      })
    }
    const service = createSnapshotService({
      readAccounts: async () => accounts,
      fetchUsages,
      now: () => 1000,
      refreshMs: 60000,
    })
    const pending = service.snapshot(false)
    await waitFor(() => deferred.has('KEY_A'), 'the initial fetch')
    accounts = identity([row('current', '', 'KEY_B', true, false)])
    service.invalidate()
    accounts = identity([row('current', '', 'KEY_C', true, false)])
    service.invalidate()
    const latest = service.snapshot(false)
    deferred.get('KEY_A')()
    await waitFor(() => deferred.has('KEY_C'), 'the coalesced final fetch')
    deferred.get('KEY_C')()
    const result = await latest
    check(
      'only the final state is fetched after rapid invalidations',
      requested.join(',') === 'KEY_A,KEY_C' && result.entries[0].masked === 'masked-current',
      requested.join(','),
    )
    const staleCaller = await pending
    check(
      'the caller that started before the invalidations settles on the final state',
      staleCaller.error === null && staleCaller.entries[0].masked === 'masked-current',
      JSON.stringify([staleCaller.error, staleCaller.entries.map((e) => e.masked)]),
    )
  }

  // ================= identity changed between the read and the fetch =================
  {
    let reads = 0
    const readAccounts = async () => {
      reads += 1
      return reads === 1
        ? identity([row('current', '', 'KEY_OLD', true, false)])
        : identity([row('current', '', 'KEY_NEW', true, false)])
    }
    const requested = []
    const fetchUsages = async (keys) => {
      requested.push(keys[0])
      return new Map([[keys[0], reading(20)]])
    }
    const service = createSnapshotService({ readAccounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    const snap = await service.snapshot(false)
    check(
      'an identity changed during the read is not published as valid',
      requested.join(',') === 'KEY_OLD,KEY_NEW' && snap.entries[0].masked === 'masked-current',
      requested.join(','),
    )
  }

  // ================= unique keys, at most eight concurrent requests =================
  {
    const rows = [row('current', '', 'K0', true, false)]
    const expected = []
    for (let i = 1; i <= 11; i += 1) {
      rows.push(row('pool:p' + i, 'p' + i, 'K' + i, false, true))
      expected.push('K' + i)
    }
    // A second pool name for K1 must not cause a second request.
    rows.push(row('pool:alias', 'alias', 'K1', false, true))
    expected.push('K0')
    const accounts = identity(rows)
    let active = 0
    let maxActive = 0
    const calls = []
    const fetchUsages = async (keys) => {
      active += 1
      maxActive = Math.max(maxActive, active)
      calls.push(keys[0])
      await new Promise((resolve) => setTimeout(resolve, 1))
      active -= 1
      return new Map([[keys[0], reading(10)]])
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    const snap = await service.snapshot(true)
    check('duplicate keys are requested once', calls.length === 12 && new Set(calls).size === 12, calls.length + '/' + new Set(calls).size)
    check('at least one request ran for every unique key', expected.concat('K0').every((key) => calls.includes(key)))
    check('concurrent requests never exceed eight', maxActive === 8, String(maxActive))
    check('duplicate-key rows still render', snap.entries.length === 13)
  }

  // ================= an old batch failure cannot poison the new generation =================
  {
    let accounts = identity([row('current', '', 'KEY_A', true, false)])
    const deferred = new Map()
    const requested = []
    const fetchUsages = (keys) => {
      const key = keys[0]
      requested.push(key)
      return new Promise((resolve, reject) => {
        deferred.set(key, {
          resolve: () => resolve(new Map([[key, reading(10)]])),
          reject: () => reject(new Error('old batch exploded')),
        })
      })
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    const first = service.snapshot(true)
    await waitFor(() => deferred.has('KEY_A'), 'the doomed batch')
    accounts = identity([row('current', '', 'KEY_B', true, false)])
    service.invalidate()
    const second = service.snapshot(false)
    await tick()
    deferred.get('KEY_A').reject()
    await waitFor(() => deferred.has('KEY_B'), 'the replacement batch')
    deferred.get('KEY_B').resolve()
    const result = await second
    check(
      'an old batch failure does not become the new snapshot',
      result.error === null && result.entries.length === 1 && result.entries[0].masked === 'masked-current',
      JSON.stringify([result.error, result.entries.map((e) => e.masked)]),
    )
    check('the failed old batch still triggers the new request', requested.join(',') === 'KEY_A,KEY_B', requested.join(','))
    check('the caller that awaited the old batch also settles with the new state', (await first).error === null)
  }

  // ================= failed windows keep their last reading and freshness =================
  {
    const accounts = identity([row('current', '', 'KEY_A', true, false)])
    let clock = 1000
    let call = 0
    const three = (rolling, weekly, monthly) => ({
      error: null,
      detail: null,
      windows: {
        rolling: { status: 'ok', percent: rolling, resetsAt: null },
        weekly: { status: 'ok', percent: weekly, resetsAt: null },
        monthly: { status: 'ok', percent: monthly, resetsAt: null },
      },
    })
    const fetchUsages = async () => {
      call += 1
      if (call === 1) return new Map([['KEY_A', three(10, 20, 30)]])
      if (call === 2) return new Map([['KEY_A', { error: 'network', detail: 'offline', windows: null }]])
      return new Map([[
        'KEY_A',
        {
          error: null,
          detail: null,
          windows: {
            rolling: { status: 'ok', percent: 40, resetsAt: null },
            weekly: { status: null, percent: null, resetsAt: null },
            monthly: { status: 'ok', percent: 50, resetsAt: null },
          },
        },
      ]])
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => clock, refreshMs: 60000 })
    const first = await service.snapshot(true)
    const firstWindow = first.entries[0].windows.rolling
    check(
      'a successful window carries its success time',
      firstWindow.percent === 10 && firstWindow.stale === false && firstWindow.lastSuccessAt === 1000 && firstWindow.lastAttemptAt === 1000,
      JSON.stringify(firstWindow),
    )
    clock = 2000
    service.invalidate()
    const failed = await service.snapshot(false)
    const failedRolling = failed.entries[0].windows.rolling
    check(
      'a failed window keeps the previous reading and is marked stale',
      failedRolling.percent === 10 && failedRolling.stale === true && failedRolling.error === 'network',
      JSON.stringify(failedRolling),
    )
    check(
      'the failed window records the attempt and preserves the success time',
      failedRolling.lastAttemptAt === 2000 && failedRolling.lastSuccessAt === 1000,
      JSON.stringify(failedRolling),
    )
    check('a whole-key failure marks every previously valid window stale', failed.entries[0].windows.weekly.stale === true && failed.entries[0].windows.monthly.stale === true)
    clock = 3000
    service.invalidate()
    const recovered = await service.snapshot(false)
    const rolling = recovered.entries[0].windows.rolling
    const weekly = recovered.entries[0].windows.weekly
    check('a recovered window is fresh again', rolling.percent === 40 && rolling.stale === false && rolling.lastSuccessAt === 3000, JSON.stringify(rolling))
    check(
      'a still-missing window keeps the older reading and success time',
      weekly.percent === 20 && weekly.stale === true && weekly.lastSuccessAt === 1000 && weekly.lastAttemptAt === 3000,
      JSON.stringify(weekly),
    )
  }

  // ================= a malformed response still shows the last good windows =================
  {
    const accounts = identity([row('current', '', 'KEY_A', true, false)])
    let call = 0
    const good = {
      error: null,
      detail: null,
      windows: {
        rolling: { status: 'ok', percent: 10, resetsAt: null },
        weekly: { status: 'ok', percent: 20, resetsAt: null },
        monthly: { status: 'ok', percent: 30, resetsAt: null },
      },
    }
    const fetchUsages = async () => {
      call += 1
      if (call === 1) return new Map([['KEY_A', good]])
      return new Map([['KEY_A', { error: 'bad-json', detail: 'json parse failed', windows: null }]])
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    await service.snapshot(true)
    service.invalidate()
    const malformed = await service.snapshot(false)
    const rolling = malformed.entries[0].windows.rolling
    check('a malformed response keeps the last good window', rolling.percent === 10 && rolling.stale === true, JSON.stringify(rolling))
    check('a malformed response keeps the last success time', rolling.lastSuccessAt === 1000, JSON.stringify(rolling))
    check('a malformed response stays visible as the account-level error', malformed.entries[0].error === 'bad-json', String(malformed.entries[0].error))
  }

  // ================= a current-generation batch failure keeps the last entries =================
  {
    const accounts = identity([row('current', '', 'KEY_A', true, false)])
    let call = 0
    const fetchUsages = async () => {
      call += 1
      if (call === 2) throw new Error('endpoint meltdown')
      return new Map([['KEY_A', reading(10)]])
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    await service.snapshot(true)
    service.invalidate()
    const failed = await service.snapshot(false)
    check(
      'a batch failure returns an explicit error instead of an empty pool',
      /^internal:/.test(failed.error || '') && failed.entries.length === 1,
      JSON.stringify([failed.error, failed.entries.length]),
    )
    check(
      'a batch failure preserves the previous reading as stale',
      failed.entries[0].windows.rolling.percent === 10 && failed.entries[0].windows.rolling.stale === true,
      JSON.stringify(failed.entries[0].windows.rolling),
    )
    const retried = await service.snapshot(false)
    check('the next snapshot retries after a current-generation failure', call === 3 && retried.error === null, String(call) + '/' + String(retried.error))
  }

  // ================= dispose stops commits and aborts the batch =================
  {
    const accounts = identity([row('current', '', 'KEY_A', true, false)])
    let release
    let signal = null
    const fetchUsages = (keys, options) => {
      signal = options.signal
      return new Promise((resolve) => {
        release = () => resolve(new Map([[keys[0], reading(10)]]))
      })
    }
    const service = createSnapshotService({ readAccounts: async () => accounts, fetchUsages, now: () => 1000, refreshMs: 60000 })
    const pending = service.snapshot(true)
    await waitFor(() => typeof release === 'function', 'the dispossessed batch')
    service.dispose()
    check('dispose aborts a cancellable request', signal !== null && signal.aborted === true)
    release()
    const after = await pending
    check('dispose prevents the batch from committing', after.entries.length === 0, String(after.entries.length))
  }
} catch (error) {
  results.push('CRASH  ' + String((error && error.stack) || error))
}

console.log(results.join('\n'))
const failed = results.filter((r) => r.startsWith('FAIL') || r.startsWith('CRASH')).length
console.log('\n' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exit(failed === 0 ? 0 : 1)
