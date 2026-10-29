# dsh-tokenhub

**An OpenCode Go quota bar for DeepSeek Harness (dsh)** — a floating widget showing rolling / weekly / monthly usage and reset countdowns for every key in your pool, with **one-click account switching**.

Ported from [TokenHub v3](https://github.com/WisdomPK/tokenhub), an opencode desktop quota bar. The important difference: dsh's credential service resolves a reference per operation with no cross-operation cache, so **a switch here reaches the next request with no restart**. TokenHub had to document "restart opencode", because the provider cached model clients keyed without the credential.

**English** · [简体中文](README.zh.md)

## What you get

- **Floating ball** — a **vertical rounded rectangle** pinned to the right edge in the **lower fifth of the viewport** (about 1/5 up from the bottom, never mid-page), holding **three horizontal bars stacked top to bottom: rolling, weekly, monthly**. Each bar fills by its own window's usage and takes its own band's colour (green / amber / red), so position and hue together answer both which quota is filling and how close it is. There is **no badge number** — all three windows are on the ball itself. Hover for the exact percentages. The gauge renders at **2×** by default (`scale` is configurable) through `transform: scale()`, so its radii and bar gaps grow with it. The panel and the dialogs are **not** scaled: they carry text, and text that grows is text that stops fitting.
- **Expandable panel** — one card per account with three metric rows (rolling / weekly / monthly): a threshold-coloured bar, the percentage, and a ⟳ reset countdown. The account in force is marked; an account whose query failed is greyed out and reads "offline".
- **One-click switching** — spare rows offer Switch behind a confirmation. The switch lands in dsh's credential store and applies to the next request.
- **The key in force always owns a row** — the key dsh actually uses (`OPENCODE_GO_API_KEY`) need not be in the pool. When it is not, the panel gives it its own row marked "not in pool", carrying Add and Add to pool; filing it makes it an ordinary account. **Switch away without filing it and that key is unreachable from the UI for good.**
- **Add is always reachable** — the row in force carries Add, and with no row in force (no key configured yet, say) the panel offers one at the bottom, so it cannot strand you with no way to add a second account
- **Add / remove accounts** — Add verifies the key against the live endpoint first, so a bad key is refused in place with your input intact; the account in force cannot be removed.
- **Automatic pool discovery** — scans $DSH_HOME/.credentials.yaml for every OPENCODE_GO_KEY_<name>. With no pool it falls back to the single key in force, and the Add button on that card is the way into a real pool.
- **opencode is left alone by default** — a switch changes only dsh's own credential. The two harnesses apply a change on different schedules (dsh on the next request, opencode at startup), so syncing by default would leave them disagreeing until a restart; syncing is an explicit opt-in (`syncOpencodeAuth: true`), and then it keeps the backup, atomic replace, read-back check, and rollback, with the result saying opencode needs a restart.
- **Theme-aware, bilingual** — every colour is a dsh theme token (--dsw-alias-*), so it follows the active theme; the dictionary follows your browser language.

## Install

```powershell
# From this directory. Idempotent; safe to re-run.
powershell -ExecutionPolicy Bypass -File install.ps1
```

Or use dsh's own plugin command, which also writes the profile's dependency and bundle list:

```powershell
dsh plugin --profile web add link:<path-to-dsh-tokenhub>
```

> ⚠️ **Use `link:`, never `file:`.** pnpm **copies** a `file:` dependency into `node_modules`, so dsh reads a snapshot taken at install time — later source edits never reach it, **not even across a restart**, because the restart reads the same copy. `link:` symlinks instead, making your working tree the install location.

Reload the browser page afterwards. dsh's HMR usually makes even that unnecessary; if the widget does not appear, restart dsh web.

## Using it

| Action | Where |
|---|---|
| Open / close the panel | Click the floating ball |
| Refresh now | "Refresh" in the panel header |
| Switch account | A spare row → Switch → confirm |
| File the key in force into the pool | The row in force (marked "not in pool") → Add to pool → name it |
| Add account | The row in force → Add → name and key → Confirm (verified live first) |
| Remove account | A spare row → Remove → confirm |

The account name is **entirely yours** (1–32 letters, digits, or underscores); the plugin never invents one unless you leave the field blank, and then it is `go1`, `go2`, … Both the name and key fields suppress browser autofill — otherwise a password manager reads the text field above the password field as a login username and fills it in for you.

## Key pool format

Keys are read from $DSH_HOME/.credentials.yaml (dsh's standard credential file), following the convention the published [dsh-opencode-go-usage](https://github.com/xiaweiliang060035/dsh-opencode-go-usage) already uses, so the two plugins share one pool:

```yaml
OPENCODE_GO_API_KEY: sk-…        # the key in force; the llm adapter resolves it per request
OPENCODE_GO_KEY_ACTIVE: go2      # which pool entry is in force
OPENCODE_GO_KEY_go1: sk-…
OPENCODE_GO_KEY_go2: sk-…
```

The number and names of OPENCODE_GO_KEY_<name> entries are arbitrary. With no pool the widget degrades to single-key mode and shows OPENCODE_GO_API_KEY alone.

How "the account in force" is decided: the name OPENCODE_GO_KEY_ACTIVE points at wins; without that marker (or when it names an entry the pool no longer holds) the entry whose value equals the current key wins — so a hand-maintained pool still highlights the right row.

**A pool that does not contain the key in force is a normal state, not an error**: the panel gives that key its own row (`inPool: false`), which cannot be switched to or removed but does carry Add and Add to pool. That is what keeps both "I just added my first account" and "I have no pool yet" from leaving the panel with no way to add.

## Configuration

Add a config block to the plugin row in your profile's cordis.patch.yml:

```yaml
- id: tokenhub
  config:
    refreshMs: 60000
    syncOpencodeAuth: true
```

| Key | Default | Meaning |
|---|---|---|
| baseUrl | https://opencode.ai/zen/go/v1/usage | Usage endpoint |
| refreshMs | 60000 | Host poll interval, ms |
| timeoutMs | 15000 | Per-request timeout |
| dshHome | resolveDshHome() | Override the DSH home directory |
| credentialsPath | <dshHome>/.credentials.yaml | Override the credential document |
| keyNames | auto-discovered | Fixed allowlist of pool names: when set, the document scan is skipped and add / join-pool / rename targets must be in this list |
| opencodeAuthPath | auto-resolved | Override opencode's auth.json |
| syncOpencodeAuth | **false** | Also write opencode's auth.json on a switch (off by default; when on, the result says opencode needs a restart) |
| thresholds | {warn: 60, error: 85} | Colour bands |
| scale | **2** | Size multiplier for the **gauge** only; the panel and dialogs keep their own size (the panel steps aside when the gauge widens) |
| autoReleaseWedgedLock | true | Judge and clear a leftover write lock, then retry |
| wedgedLockMinAgeMs | 30000 | How long an incomplete lock record must sit before it counts as leftover |

`keyNames` is a **fixed allowlist**, not a discovery hint: when it is set (even to `[]`), the pool lists exactly those names, and every add, join-pool, or rename target must already be in the list. Renaming therefore starts in the config — add the new name to `keyNames`, rename in the panel, then drop the old name. A blank add still auto-generates a `goN` candidate, but it succeeds only when that generated name is in the list; otherwise enter a listed name or add the generated one to `keyNames`.

## How it works

**Host half** (lib/index.js, a Node ESM cordis plugin) — every key value lives only here:

1. Scans the credential document for candidate pool names and confirms each through ctx.credentials.resolve()
2. Fetches usage concurrently (Authorization: Bearer <key>); one bad key cannot take down the pool
3. Caches the result as a snapshot that **carries no key value**, polled every refreshMs
4. Registers three routes: GET /plugins/dsh-tokenhub/snapshot, POST /switch, POST /account

**Client half** (lib/client.js, a browser bundle) — registers into the shell.overlay frame-wide floating layer and only reads the Host snapshot. **It never sees a key**: names, masks (first six, ellipsis, last four), percentages, and reset times only.

Writes are same-origin checked: a request carrying an Origin that does not match Host is answered 403.

## Security

- Key values stay host-side; the UI and the snapshot only ever carry masks
- Outbound traffic goes to opencode.ai only
- A switch reads the reference back before reporting success (resolution is per call, so the read-back is authoritative) and rolls back to the previous account on mismatch
- **with** `syncOpencodeAuth` enabled it touches opencode: backup first (newest five kept), same-directory temp file, atomic rename, read-back, rollback on failure; **only the opencode-go field is touched**. Left off, opencode's auth.json is not even read
- A current key supplied by the launching environment (a read-only source shadows the reference) makes the switch fail loudly, with the reason
- **Clearing a leftover lock is judgement, not guesswork**: only a lock whose record is not `PID\n` (its holder died between creating the lock and recording its PID, which atomic-write itself cannot take over) **and** which has sat unchanged past `wedgedLockMinAgeMs` is removed. A lock naming a live PID, or a fresh empty one that a writer may be mid-create on, is left strictly alone. Every repair is reported in the result — never silent

## Tests

```powershell
npm install          # react / react-dom / jsdom, for the tests only
npm test             # 128 host + 116 client + 65 usage-contract + 31 concurrency + 32 snapshot (372 checks)
```

- test/host.test.mjs — an in-memory credential store plus a stubbed usage endpoint, covering pool discovery, active-account resolution, switch and rollback, add/remove validation, and the same-origin fence
- test/client.test.mjs — mounts the widget with real React in jsdom, drives the clicks, and asserts the rendered percentages, threshold bands, countdowns, confirmations, and request bodies
- test/usage-contract.test.mjs — shared percent and reset-time boundary cases from tests/fixtures/usage-contract.json
- test/account-concurrency.test.mjs — serialized account mutations, conflict checks, partial success, and warnings
- test/snapshot.test.mjs — revisioned batches, deduped keys, the concurrency cap, and stale windows

## Differences from TokenHub

| TokenHub (opencode) | This plugin (dsh) |
|---|---|
| Summoned by hovering a 10px hot zone at the opencode window edge | No browser equivalent → a persistent floating ball |
| A separate always-on-top Python/tkinter window plus a tray icon | A Host half and a Client half inside the dsh process and page |
| Switch writes auth.json and needs an opencode **restart** | Switch writes only the dsh credential and applies to the **next request**; opencode is untouched by default |
| Separate settings and help windows | Inline dialogs in the panel |
| Per-metric error text (each window parsed independently) | One request per account: a window with no percentage reads "no data"; a wholly failed account reads "offline" |

## When a change takes effect

| Action | Takes effect |
|---|---|
| Switch in the dsh panel | **Next request** (dsh resolves the reference per operation, with no cross-operation cache) |
| What that does to opencode | **Nothing** — opencode's file is not written by default |
| With `syncOpencodeAuth: true` | opencode's auth.json is written, but **opencode reads it only at startup** |
| Editing this plugin's source | **Restart dsh** — `dsh-hmr` defaults to `root: []`, watching no sources, and ESM modules are cached by URL; the dependency must also be `link:` (see Install) |

## Known issues

- **Hard-killing dsh can leave a leftover write lock.** dsh's `atomic-write` creates `<file>.lock` exclusively and then records its holder's PID; a process killed between those two steps leaves an empty-record lock, and atomic-write refuses to take over an incomplete record. Ending the task from Task Manager, or `taskkill /F`, can trigger it. The plugin self-heals on the next credential write by the rule above; a graceful exit never produces one.
- The usage endpoint `GET https://opencode.ai/zen/go/v1/usage` is not part of opencode's public documentation; it was discovered and verified by the community. Parsing here is defensive.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No floating ball | Confirm the **web** profile is the one you installed into; reload the page; dsh plugin --profile web list should show dsh-tokenhub |
| Badge shows ! | Snapshot fetch failed — confirm dsh web is running and /plugins/dsh-tokenhub/snapshot responds |
| A card reads "invalid key" | That key is expired or wrong |
| A card reads "network error" | The Host cannot reach opencode.ai (proxy / offline / timeout). Note **Node 22's fetch ignores HTTP(S)_PROXY**, so this machine needs direct access |
| Panel says no key is configured | .credentials.yaml holds neither OPENCODE_GO_KEY_* nor OPENCODE_GO_API_KEY |
| Switch reports a read-only source | OPENCODE_GO_API_KEY is shadowed by a launching environment variable; clear it in the shell that starts dsh |
| "The credential file is blocked by a write lock" | A leftover `.credentials.yaml.lock`. dsh's atomic-write only takes over a lock whose record is a readable PID, so an empty-record lock just waits. The plugin judges and clears it by default (`autoReleaseWedgedLock`); with that off, remove it by hand: `Remove-Item -LiteralPath "$env:USERPROFILE\.dsh\.credentials.yaml.lock" -Force` |

## Uninstall

```powershell
dsh plugin --profile web remove dsh-tokenhub
```

That drops both the dependency and the bundle entry. Your credential document and opencode's auth.json are left alone.

## License

MIT
