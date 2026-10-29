/**
 * dsh-tokenhub — Client half.
 * ------------------------------------------------------------------
 * Floating quota bar for the dsh web GUI. Registers into 'shell.overlay' (the
 * frame-wide floating layer) and polls the Host half, which owns every key
 * value: this bundle only ever sees names, masks, percentages, and reset times.
 *
 * Ported from TokenHub v3 (opencode desktop quota bar, WisdomPK/tokenhub).
 * TokenHub summoned its bar from a 10px hot zone at the opencode window edge,
 * which has no equivalent in a browser; the frame's floating layer plays that
 * role here, with a persistent ball whose three bars show the three windows.
 */

window.__ModuleLoader__.load({
  id: 'dsh-tokenhub',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement

    /** Host routes this bundle reads and writes. */
    const SNAPSHOT = '/plugins/dsh-tokenhub/snapshot'
    const SWITCH = '/plugins/dsh-tokenhub/switch'
    const ACCOUNT = '/plugins/dsh-tokenhub/account'

    /** Local poll; the Host refreshes on the same cadence. */
    const POLL_MS = 60000

    /** Window order, matching TokenHub's three metric blocks. Labels and short
     *  tags live in the dictionaries so neither language borrows the other's. */
    const WINDOWS = ['rolling', 'weekly', 'monthly']

    /** Used only until the Host sends its own bands. */
    const FALLBACK_THRESHOLDS = { warn: 60, error: 85 }

    const zh = {
      title: 'OpenCode Go 额度',
      updated: '更新于',
      refresh: '刷新',
      collapse: '收起',
      loading: '加载中…',
      loadFailed: '快照拉取失败',
      noKeys: '未配置 opencode-go 密钥（凭据里既无 OPENCODE_GO_API_KEY 也无 OPENCODE_GO_KEY_*）',
      noEntries: '未发现账号',
      inUse: '使用中',
      notInPool: '未入池',
      w: { rolling: '5小时', weekly: '周', monthly: '月' },
      currentKey: '当前密钥',
      join: '加入池',
      joinTitle: '把当前密钥加入账号池',
      joinHint: '入池后它会作为一条备用账号留在列表里，切换走之后也还能切回来。',
      joinCta: '加入',
      rename: '改名',
      renameTitle: '重命名账号',
      renameLabel: '新账号名',
      renameCta: '保存',
      renameHint: '只改名字：密钥不动，「当前生效」状态也不变。',
      noActive: '当前没有生效的密钥',
      day: '天',
      add: '新增',
      switch: '切换',
      switching: '切换中…',
      remove: '删除',
      cancel: '取消',
      confirm: '确认',
      ok: '确定',
      offline: '离岔',
      noData: '数据不可用',
      reset: '即将重置',
      addTitle: '新增账号',
      addName: '账号名（可留空，自动命名）',
      addKey: 'API Key',
      paste: '从剪贴板粘贴',
      pasted: '已粘贴',
      show: '显示',
      hide: '隐藏',
      checking: '验证中…',
      switchAsk: '确认切换到「{n}」？后续请求将使用该账号。',
      removeAsk: '确认删除「{n}」？该账号将从本机账号列表中移除。',
      switchCta: '确认切换',
      removeCta: '删除',
      hostError: 'Host 错误',
    }

    const en = {
      title: 'OpenCode Go quota',
      updated: 'Updated',
      refresh: 'Refresh',
      collapse: 'Collapse',
      loading: 'Loading…',
      loadFailed: 'Failed to fetch snapshot',
      noKeys: 'No opencode-go key configured (neither OPENCODE_GO_API_KEY nor OPENCODE_GO_KEY_*)',
      noEntries: 'No accounts found',
      inUse: 'in use',
      notInPool: 'not in pool',
      w: { rolling: 'Rolling', weekly: 'Weekly', monthly: 'Monthly' },
      currentKey: 'Current key',
      join: 'Add to pool',
      joinTitle: 'File the current key into the pool',
      joinHint: 'Once filed it stays in the list as a spare, so you can switch back to it later.',
      joinCta: 'Add',
      rename: 'Rename',
      renameTitle: 'Rename account',
      renameLabel: 'New account name',
      renameCta: 'Save',
      renameHint: 'Renames only: the key does not move, and neither does which account is in force.',
      noActive: 'No key is in force',
      day: 'd',
      add: 'Add',
      switch: 'Switch',
      switching: 'Switching…',
      remove: 'Remove',
      cancel: 'Cancel',
      confirm: 'Confirm',
      ok: 'OK',
      offline: 'offline',
      noData: 'no data',
      reset: 'resetting',
      addTitle: 'Add account',
      addName: 'Account name (blank generates one)',
      addKey: 'API key',
      paste: 'Paste from clipboard',
      pasted: 'Pasted',
      show: 'Show',
      hide: 'Hide',
      checking: 'Verifying…',
      switchAsk: 'Switch to "{n}"? Later requests will use that account.',
      removeAsk: 'Remove "{n}"? It will be dropped from this machine’s account list.',
      switchCta: 'Switch',
      removeCta: 'Remove',
      hostError: 'Host error',
    }

    const t = typeof navigator !== 'undefined' && /^\s*zh/i.test(navigator.language || '') ? zh : en
    const fill = (text, name) => String(text).split('{n}').join(name)

    // ------------------------------------------------------------ styles
    // Injected once per page; every colour is a dsh theme token so the widget
    // follows the active theme without knowing which one it is.
    const CSS_ID = 'dsh-tokenhub/css'
    if (typeof document !== 'undefined' && document.querySelector('style[data-plugin-css="' + CSS_ID + '"]') === null) {
      const tag = document.createElement('style')
      tag.dataset.pluginCss = CSS_ID
      tag.textContent = [
        // A vertical rounded rectangle: three stacked tracks, one per quota
        // window, each filled and coloured by its own usage.
        '.th-gauge{position:fixed;right:8px;top:80%;z-index:1000;',
        'transform:translateY(-50%) scale(var(--th-scale,1));transform-origin:100% 50%;',
        'width:34px;height:48px;border-radius:11px;box-sizing:border-box;display:flex;flex-direction:column;',
        'justify-content:center;gap:4px;padding:0 6px;background:var(--dsw-alias-bg-overlay);',
        'border:1px solid var(--dsw-alias-border-l2);box-shadow:0 2px 8px rgba(0,0,0,.16);',
        'cursor:pointer;user-select:none;transition:transform .18s cubic-bezier(.2,.8,.3,1.2)}',
        '.th-gauge:hover{transform:translateY(-50%) scale(calc(var(--th-scale,1) * 1.08))}',
        '.th-gauge:active{transform:translateY(-50%) scale(calc(var(--th-scale,1) * .96))}',
        '.th-gauge-row{display:block;height:7px;border-radius:3.5px;overflow:hidden;background:var(--dsw-alias-bg-layer-3)}',
        '.th-gauge-fill{display:block;height:100%;border-radius:3.5px;transition:width .3s ease}',
        '.th-gauge-fill.ok{background:var(--dsw-alias-state-success-primary)}',
        '.th-gauge-fill.warn{background:var(--dsw-alias-state-warn-primary)}',
        '.th-gauge-fill.err{background:var(--dsw-alias-state-error-primary)}',
        '.th-gauge-fill.na{background:var(--dsw-alias-label-tertiary)}',

        // The panel keeps its own size, but steps aside by the gauge's scaled
        // width so a bigger gauge never covers it. It is bottom-anchored on the
        // gauge's centre line (the gauge sits at 20vh from the bottom) so the two
        // move together, and grows upward instead of being centred on the page.
        '.th-panel{position:fixed;bottom:20vh;z-index:999;width:356px;',
        'right:calc(16px + 34px * var(--th-scale,1));',
        'max-height:72vh;',
        'overflow:auto;background:var(--dsw-alias-bg-overlay);',
        'border:1px solid var(--dsw-alias-border-l2);',
        'border-radius:var(--dsw-radius-lg);box-shadow:0 8px 28px rgba(0,0,0,.26);padding:10px 12px 8px;',
        'font-size:11px;color:var(--dsw-alias-label-primary)}',
        '.th-head{display:flex;align-items:center;gap:6px;margin-bottom:8px}',
        '.th-title{font-size:12.5px;font-weight:700;flex:1}',
        '.th-time{font-size:10px;color:var(--dsw-alias-label-secondary)}',
        '.th-btn{border:1px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-sm);background:transparent;',
        'color:var(--dsw-alias-label-secondary);font-size:10px;cursor:pointer;padding:2px 7px;line-height:1.5;',
        'white-space:nowrap;font-family:inherit}',
        '.th-btn:hover:not(:disabled){color:var(--dsw-alias-label-primary);background:var(--dsw-alias-interactive-bg-hover)}',
        '.th-btn:disabled{opacity:.45;cursor:default}',
        '.th-btn.th-danger:hover:not(:disabled){color:var(--dsw-alias-state-error-primary);background:var(--dsw-alias-interactive-bg-hover-danger)}',

        '.th-card{border:1px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-md);',
        'background:var(--dsw-alias-bg-layer-1);padding:6px 8px;margin-bottom:6px}',
        '.th-card:last-child{margin-bottom:0}',
        '.th-card-active{border-color:var(--dsw-alias-brand-primary)}',
        '.th-card-head{display:flex;align-items:center;gap:5px;margin-bottom:4px}',
        '.th-dot{width:6px;height:6px;border-radius:3px;flex:none}',
        '.th-dot.on{background:var(--dsw-alias-state-success-primary)}',
        '.th-dot.off{background:var(--dsw-alias-label-tertiary)}',
        '.th-name{font-weight:700;font-size:11px}',
        '.th-chip{font-size:9px;color:var(--dsw-alias-label-primary-inverted);background:var(--dsw-alias-brand-primary);',
        'border-radius:3px;padding:0 4px;font-weight:700}',
        '.th-mask{font-size:9px;color:var(--dsw-alias-label-secondary);overflow:hidden;text-overflow:ellipsis;',
        'white-space:nowrap;min-width:0;flex:0 1 auto}',
        '.th-spacer{flex:1}',
        '.th-row{display:flex;align-items:center;gap:6px;padding:2px 0}',
        '.th-row-label{width:40px;flex:none;font-size:10px;color:var(--dsw-alias-label-secondary)}',
        '.th-bar{flex:1;height:4px;border-radius:2px;background:var(--dsw-alias-bg-layer-3);overflow:hidden;min-width:40px}',
        '.th-fill{display:block;height:100%;border-radius:2px;transition:width .3s ease}',
        '.th-fill.ok{background:var(--dsw-alias-state-success-primary)}',
        '.th-fill.warn{background:var(--dsw-alias-state-warn-primary)}',
        '.th-fill.err{background:var(--dsw-alias-state-error-primary)}',
        '.th-fill.na{background:var(--dsw-alias-label-tertiary)}',
        '.th-pct{width:36px;flex:none;text-align:right;font-size:10px;font-weight:700}',
        '.th-pct.ok{color:var(--dsw-alias-label-primary)}',
        '.th-pct.warn{color:var(--dsw-alias-state-warn-primary)}',
        '.th-pct.err{color:var(--dsw-alias-state-error-primary)}',
        '.th-pct.na{color:var(--dsw-alias-label-tertiary)}',
        '.th-reset{width:74px;flex:none;text-align:right;font-size:9px;color:var(--dsw-alias-label-secondary)}',
        '.th-offline{padding:8px 0 6px;text-align:center;font-size:10px;color:var(--dsw-alias-state-error-primary)}',
        '.th-empty{color:var(--dsw-alias-label-secondary);padding:14px 2px;text-align:center;line-height:1.6}',
        '.th-toast{margin-top:6px;padding:4px 6px;border-radius:var(--dsw-radius-sm);font-size:10px;line-height:1.5;word-break:break-word}',
        '.th-toast.ok{background:var(--dsw-alias-state-success-tertiary);color:var(--dsw-alias-label-primary)}',
        '.th-toast.err{background:var(--dsw-alias-state-error-secondary);color:var(--dsw-alias-label-primary)}',
        '.th-toast.info{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary)}',

        '.th-mask-layer{position:fixed;inset:0;z-index:1001;background:var(--dsw-alias-bg-mask-2);display:flex;align-items:center;justify-content:center}',
        '.th-modal{width:298px;background:var(--dsw-alias-bg-overlay);border:1px solid var(--dsw-alias-border-l2);',
        'border-radius:var(--dsw-radius-lg);box-shadow:0 10px 34px rgba(0,0,0,.32);padding:12px}',
        '.th-modal-title{font-size:12px;font-weight:700;margin-bottom:8px}',
        '.th-modal-body{font-size:11px;color:var(--dsw-alias-label-secondary);line-height:1.65;margin-bottom:10px}',
        '.th-field{margin-bottom:8px}',
        '.th-label{display:block;font-size:10px;color:var(--dsw-alias-label-secondary);margin-bottom:3px}',
        '.th-input-row{display:flex;gap:5px;align-items:center}',
        '.th-input{flex:1;min-width:0;background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l2);',
        'border-radius:var(--dsw-radius-sm);color:var(--dsw-alias-label-primary);font-size:11px;padding:4px 6px;',
        'font-family:inherit;outline:none}',
        '.th-input:focus{border-color:var(--dsw-alias-brand-primary)}',
        '.th-hint{font-size:10px;margin-top:6px;line-height:1.5;word-break:break-word}',
        '.th-hint.err{color:var(--dsw-alias-state-error-primary)}',
        '.th-hint.info{color:var(--dsw-alias-label-secondary)}',
        '.th-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:10px}',
      ].join('')
      document.head.appendChild(tag)
    }

    // ----------------------------------------------------------- helpers

    /** Severity band for one window, matching TokenHub's thresholds: normal
     *  below warn, caution up to error, alarm at error and above. A window the
     *  endpoint itself flags as rate-limited is always the alarm band. */
    function level(percent, status, thresholds) {
      if (status === 'rate-limited') return 'err'
      if (percent === null || percent === undefined || Number.isNaN(percent)) return 'na'
      if (percent >= thresholds.error) return 'err'
      if (percent >= thresholds.warn) return 'warn'
      return 'ok'
    }

    /** One window's reading, or null when the endpoint sent none. */
    function pct(win) {
      if (!win || typeof win.percent !== 'number' || !Number.isFinite(win.percent)) return null
      return win.percent
    }

    /** One window of one entry, or null when this row carries no windows. */
    function windowOf(entry, key) {
      return entry && entry.windows ? entry.windows[key] || null : null
    }

    /** A reading as a bar width; no reading draws an empty bar, not a full one. */
    function clampPct(value) {
      return value === null ? 0 : Math.max(0, Math.min(100, value))
    }

    /** Reset countdown: under a day in H/M, beyond it in days and hours. */
    function fmtRemain(iso) {
      if (!iso) return '—'
      const when = new Date(iso)
      if (Number.isNaN(when.getTime())) return '—'
      const ms = when.getTime() - Date.now()
      if (ms <= 0) return t.reset
      const minutes = Math.floor(ms / 60000)
      const hours = Math.floor(minutes / 60)
      if (hours < 24) return hours + 'H' + (minutes % 60) + 'M'
      return Math.floor(hours / 24) + t.day + (hours % 24) + 'H'
    }

    /** An entry with no usable window at all renders as one offline line. */
    function isOffline(entry) {
      return WINDOWS.every((key) => pct(windowOf(entry, key)) === null)
    }

    /** The account a summary describes: the one in force, never a spare. */
    function subjectOf(entries) {
      const actives = entries.filter((entry) => entry.active)
      return actives.length > 0 ? actives[0] : null
    }

    /**
     * The gauge's three bars, one per window, in panel order: rolling on top,
     * then weekly, then monthly. Each carries its own window's fill and colour,
     * so which quota is filling up is answered by position and hue together.
     *
     * A window with no reading fills the whole track in the neutral colour: an
     * empty bar would claim "nothing used" for a value nobody received.
     * @returns one { band, width } per window.
     */
    function barShape(entries, thresholds) {
      const source = subjectOf(entries)
      if (source === null) return WINDOWS.map(() => ({ band: 'na', width: 100 }))
      return WINDOWS.map((key) => {
        const win = windowOf(source, key)
        const value = pct(win)
        if (value === null) return { band: 'na', width: 100 }
        return { band: level(value, win.status, thresholds), width: clampPct(value) }
      })
    }

    /**
     * The gauge's tooltip: the three bars coloured say how much at a glance but
     * not how many, so this carries the numbers behind them and stops there.
     */
    function fabTooltip(entries) {
      const source = subjectOf(entries)
      if (!source) return t.title + ' · ' + t.noActive
      const parts = WINDOWS.map((key) => {
        const value = pct(windowOf(source, key))
        return t.w[key] + ' ' + (value === null ? t.noData : Math.round(value) + '%')
      })
      return t.title + ' · ' + (source.name || t.currentKey) + ' · ' + parts.join(' · ')
    }

    /** Clock text for the last successful refresh. */
    function fmtClock(ms) {
      if (!ms) return '—'
      const when = new Date(ms)
      const pad = (n) => (n < 10 ? '0' + n : String(n))
      return pad(when.getHours()) + ':' + pad(when.getMinutes())
    }

    /** POST JSON to a Host route and normalize every outcome into one shape. */
    function post(path, body) {
      return fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
        .then((res) => res.json().catch(() => ({ ok: false, error: 'invalid response' })))
        .catch((error) => ({ ok: false, error: String((error && error.message) || error) }))
    }

    // ----------------------------------------------------------- dialogs

    /** Modal shell shared by the confirm and account dialogs. */
    function Modal(props) {
      return h(
        'div',
        {
          className: 'th-mask-layer',
          onMouseDown: (event) => {
            if (event.target === event.currentTarget) props.onCancel()
          },
        },
        h('div', { className: 'th-modal' }, props.children),
      )
    }

    /** Two-step confirmation: TokenHub routes switch and delete through this. */
    function ConfirmDialog(props) {
      return h(
        Modal,
        { onCancel: props.onCancel },
        h('div', { className: 'th-modal-title' }, props.title),
        h('div', { className: 'th-modal-body' }, props.body),
        h(
          'div',
          { className: 'th-actions' },
          h('button', { className: 'th-btn', onClick: props.onCancel }, t.cancel),
          h(
            'button',
            { className: 'th-btn' + (props.danger ? ' th-danger' : ''), onClick: props.onConfirm },
            props.cta,
          ),
        ),
      )
    }

    /**
     * The three things this dialog does, and the only three differences between
     * them: what it says, whether it takes a key, and what it posts.
     */
    const DIALOG_MODES = {
      add: { title: 'addTitle', hint: null, label: 'addName', key: true, cta: 'confirm', busyCta: 'checking' },
      join: { title: 'joinTitle', hint: 'joinHint', label: 'addName', key: false, cta: 'joinCta', busyCta: 'confirm' },
      rename: { title: 'renameTitle', hint: 'renameHint', label: 'renameLabel', key: false, cta: 'renameCta', busyCta: 'confirm' },
    }

    /**
     * Add / join / rename dialog. The key is verified by the Host against the
     * live endpoint before it is stored, so a bad key is refused inside this
     * dialog and the inputs survive for a retry.
     */
    function AddDialog(props) {
      const mode = props.mode || 'add'
      const text = DIALOG_MODES[mode] || DIALOG_MODES.add
      const [accountName, setAccountName] = React.useState(props.initialName || '')
      const [key, setKey] = React.useState('')
      const [reveal, setReveal] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [note, setNote] = React.useState(null)

      const paste = () => {
        if (typeof navigator === 'undefined' || !navigator.clipboard || !navigator.clipboard.readText) {
          setNote({ kind: 'err', text: t.pasted + '? ' + t.noData })
          return
        }
        navigator.clipboard
          .readText()
          .then((clip) => {
            if (clip && clip.trim()) {
              setKey(clip.trim())
              setNote({ kind: 'info', text: t.pasted })
            }
          })
          .catch(() => setNote({ kind: 'err', text: t.noData }))
      }

      const payload = () => {
        if (mode === 'join') return { op: 'joinPool', name: accountName }
        if (mode === 'rename') return { op: 'rename', name: props.from, to: accountName }
        return { op: 'add', name: accountName, key }
      }

      const submit = () => {
        if (busy) return
        setBusy(true)
        setNote(null)
        post(ACCOUNT, payload()).then((res) => {
          setBusy(false)
          if (res && res.ok) {
            props.onAdded(res.message || t.ok)
            return
          }
          setNote({ kind: 'err', text: (res && res.error) || t.hostError })
        })
      }

      const keyField = h(
        'div',
        { className: 'th-field' },
        h('label', { className: 'th-label' }, t.addKey),
        h(
          'div',
          { className: 'th-input-row' },
          h('input', {
            className: 'th-input',
            type: reveal ? 'text' : 'password',
            value: key,
            placeholder: 'sk-...',
            spellCheck: false,
            autoComplete: 'new-password',
            name: 'dsh-tokenhub-api-key',
            onChange: (event) => setKey(event.target.value),
          }),
          h('button', { className: 'th-btn', onClick: () => setReveal(!reveal) }, reveal ? t.hide : t.show),
          h('button', { className: 'th-btn', onClick: paste }, t.paste),
        ),
      )

      return h(
        Modal,
        { onCancel: props.onCancel },
        h('div', { className: 'th-modal-title' }, t[text.title]),
        text.hint ? h('div', { className: 'th-modal-body' }, t[text.hint]) : null,
        h(
          'div',
          { className: 'th-field' },
          h('label', { className: 'th-label' }, t[text.label]),
          h('input', {
            className: 'th-input',
            value: accountName,
            placeholder: 'go1',
            spellCheck: false,
            // A text field sitting above a password field reads as a login form;
            // without these a password manager fills the account name for you.
            autoComplete: 'off',
            name: 'dsh-tokenhub-account-name',
            onChange: (event) => setAccountName(event.target.value),
          }),
        ),
        text.key ? keyField : null,
        note ? h('div', { className: 'th-hint ' + note.kind }, note.text) : null,
        h(
          'div',
          { className: 'th-actions' },
          h('button', { className: 'th-btn', onClick: props.onCancel }, t.cancel),
          h(
            'button',
            { className: 'th-btn', disabled: busy, onClick: submit },
            busy ? t[text.busyCta] : t[text.cta],
          ),
        ),
      )
    }

    // ------------------------------------------------------------ widget

    /** One metric row: label, threshold-coloured bar, percentage, countdown. */
    function MetricRow(props) {
      const win = windowOf(props.entry, props.window)
      const value = pct(win)
      const band = level(value, win ? win.status : null, props.thresholds)
      return h(
        'div',
        { className: 'th-row' },
        h('span', { className: 'th-row-label' }, props.label),
        h(
          'span',
          { className: 'th-bar' },
          h('i', { className: 'th-fill ' + band, style: { width: clampPct(value) + '%' } }),
        ),
        h('span', { className: 'th-pct ' + band }, value === null ? t.noData : Math.round(value) + '%'),
        h('span', { className: 'th-reset' }, win && win.resetsAt ? '⟳ ' + fmtRemain(win.resetsAt) : ''),
      )
    }

    /** One account card: identity, three metric rows, and the row's actions. */
    function AccountCard(props) {
      const { entry, rowId, thresholds, busy, open } = props
      const inPool = entry.inPool !== false
      const actions = []
      // The row in force carries Add. It is the row the user is looking at, and
      // the panel must never end up with no way to add another account.
      if (entry.active) {
        actions.push(h('button', { className: 'th-btn', key: 'add', onClick: () => open({ kind: 'add' }) }, t.add))
        // A key with no pool entry behind it disappears the moment a switch
        // replaces it, so offer to file it before that can happen.
        if (!inPool) {
          actions.push(h('button', { className: 'th-btn', key: 'join', onClick: () => open({ kind: 'join' }) }, t.join))
        }
      }
      if (!entry.active && inPool) {
        // Routes address accounts by name; the row id is only a React key.
        const onClick = () => open({ kind: 'confirm', action: 'switch', name: entry.name })
        actions.push(h('button', { className: 'th-btn', key: 'switch', disabled: busy, onClick }, busy ? t.switching : t.switch))
      }
      // Renaming only changes the name a key is filed under, so it is safe on
      // the account in force as well as on a spare.
      if (inPool) {
        const onClick = () => open({ kind: 'rename', from: entry.name, current: entry.name })
        actions.push(h('button', { className: 'th-btn', key: 'rename', onClick }, t.rename))
      }
      if (!entry.active && inPool) {
        const onClick = () => open({ kind: 'confirm', action: 'remove', name: entry.name })
        actions.push(h('button', { className: 'th-btn th-danger', key: 'remove', disabled: busy, onClick }, t.remove))
      }

      return h(
        'div',
        { className: 'th-card' + (entry.active ? ' th-card-active' : '') },
        h(
          'div',
          { className: 'th-card-head' },
          h('span', { className: 'th-dot ' + (entry.active ? 'on' : 'off') }),
          h('span', { className: 'th-name' }, entry.name || t.currentKey),
          entry.active ? h('span', { className: 'th-chip' }, t.inUse) : null,
          inPool ? null : h('span', { className: 'th-chip' }, t.notInPool),
          entry.masked ? h('span', { className: 'th-mask' }, entry.masked) : null,
          h('span', { className: 'th-spacer' }),
          actions,
        ),
        isOffline(entry)
          ? h('div', { className: 'th-offline' }, t.offline + (entry.error ? ' · ' + entry.error : ''))
          : WINDOWS.map((key) =>
              h(MetricRow, {
                key,
                entry,
                window: key,
                label: t.w[key],
                thresholds,
              }),
            ),
      )
    }

    /** The expanded panel: header, one card per account, and the last message. */
    function Panel(props) {
      const { data, failed, entries, thresholds, busy, toast, addButton } = props
      let body
      if (failed) body = h('div', { className: 'th-empty' }, t.loadFailed)
      else if (!data) body = h('div', { className: 'th-empty' }, t.loading)
      else if (entries.length === 0) {
        body = h(
          'div',
          { className: 'th-empty' },
          h('div', null, data.error === 'no-keys' ? t.noKeys : t.noEntries),
          addButton,
        )
      } else {
        const cards = entries.map((entry) => {
          // The host half and the bundle have independent lifecycles — dsh serves
          // the bundle by revision from the boot graph while the host module is
          // cached by URL — so an older host may send no id at all. Its name is
          // the id then, which is what the routes took before ids existed.
          const rowId = entry.id || entry.name
          return h(AccountCard, {
            key: rowId,
            rowId,
            entry,
            thresholds,
            busy: busy === rowId,
            open: props.open,
          })
        })
        // Safety net: with no row in force (no key configured, say) the panel
        // still offers Add rather than leaving the user with no way forward.
        body = entries.some((entry) => entry.active)
          ? cards
          : h(React.Fragment, null, cards, h('div', { className: 'th-empty' }, h('div', null, t.noActive), addButton))
      }

      return h(
        'div',
        { className: 'th-panel' },
        h(
          'div',
          { className: 'th-head' },
          h('span', { className: 'th-title' }, t.title),
          h('span', { className: 'th-time' }, t.updated + ' ' + fmtClock(data && data.updatedAt)),
          h('button', { className: 'th-btn', onClick: props.onRefresh }, t.refresh),
          h('button', { className: 'th-btn', onClick: props.onCollapse }, t.collapse),
        ),
        body,
        toast ? h('div', { className: 'th-toast ' + toast.kind }, toast.text) : null,
      )
    }

    /** The floating ball and its panel. */
    function TokenHubWidget() {
      const [open, setOpen] = React.useState(false)
      const [data, setData] = React.useState(null)
      const [failed, setFailed] = React.useState(false)
      const [busy, setBusy] = React.useState(null)
      const [toast, setToast] = React.useState(null)
      const [dialog, setDialog] = React.useState(null)

      const load = React.useCallback((force) => {
        fetch(SNAPSHOT + (force ? '?force=1' : ''), { headers: { Accept: 'application/json' } })
          .then((res) => (res.ok ? res.json() : Promise.reject(new Error('HTTP ' + res.status))))
          .then((payload) => {
            setData(payload)
            setFailed(false)
          })
          .catch(() => setFailed(true))
      }, [])

      React.useEffect(() => {
        load(false)
        const id = setInterval(() => load(false), POLL_MS)
        return () => clearInterval(id)
      }, [load])

      // Countdowns are derived from the clock, so re-render between polls.
      const [, bump] = React.useState(0)
      React.useEffect(() => {
        const id = setInterval(() => bump((n) => n + 1), 30000)
        return () => clearInterval(id)
      }, [])

      React.useEffect(() => {
        if (!toast) return undefined
        const id = setTimeout(() => setToast(null), 4500)
        return () => clearTimeout(id)
      }, [toast])

      // The size multiplier lives on the document root so the gauge can read it
      // without threading it through props. Only the gauge scales; the panel and
      // the dialogs keep their own size and merely step aside for a wider gauge.
      // It is a namespaced custom property, so nothing else is affected.
      React.useEffect(() => {
        if (typeof document === 'undefined') return undefined
        const root = document.documentElement
        root.style.setProperty('--th-scale', String((data && data.scale) || 1))
        return () => root.style.removeProperty('--th-scale')
      }, [data])

      const say = (kind, text) => setToast({ kind, text })

      /**
       * Run one mutation for a row: busy marker, one message, then a forced
       * reload. kindOf lets the caller downgrade a technically-successful result
       * that still carries a failure worth seeing.
       */
      const run = (rowId, path, body, kindOf) => {
        setBusy(rowId)
        post(path, body).then((res) => {
          setBusy(null)
          if (res && res.ok) say(kindOf ? kindOf(res) : 'ok', res.message || t.ok)
          else say('err', (res && res.error) || t.hostError)
          load(true)
        })
      }

      const runSwitch = (name) =>
        run(name, SWITCH, { name }, (res) => (res.opencodeAuth && res.opencodeAuth.ok === false ? 'err' : 'ok'))
      const runRemove = (name) => run(name, ACCOUNT, { op: 'remove', name })

      const thresholds = (data && data.thresholds) || FALLBACK_THRESHOLDS
      const entries = (data && data.entries) || []
      // The gauge's three bars, top to bottom: rolling, weekly, monthly. A failed
      // snapshot is folded in as an empty list, so every bar reads "no data"
      // rather than three calm ones.
      const bars = barShape(failed ? [] : entries, thresholds)

      const openDialog = (next) => setDialog(next)
      const addButton = h(
        'button',
        { className: 'th-btn', style: { marginTop: '8px' }, onClick: () => openDialog({ kind: 'add' }) },
        t.add,
      )

      const closeDialog = () => setDialog(null)
      const dialogDone = (message) => {
        setDialog(null)
        say('ok', message)
        load(true)
      }

      let modal = null
      if (dialog && dialog.kind === 'confirm') {
        const isRemove = dialog.action === 'remove'
        modal = h(ConfirmDialog, {
          title: isRemove ? t.remove : t.switch,
          body: fill(isRemove ? t.removeAsk : t.switchAsk, dialog.name),
          cta: isRemove ? t.removeCta : t.switchCta,
          danger: isRemove,
          onCancel: closeDialog,
          onConfirm: () => {
            const name = dialog.name
            closeDialog()
            if (isRemove) runRemove(name)
            else runSwitch(name)
          },
        })
      } else if (dialog) {
        modal = h(AddDialog, {
          mode: dialog.kind,
          from: dialog.from,
          initialName: dialog.current,
          onCancel: closeDialog,
          onAdded: dialogDone,
        })
      }

      return h(
        React.Fragment,
        null,
        h(
          'button',
          { className: 'th-gauge', title: fabTooltip(entries), onClick: () => setOpen(!open) },
          bars.map((bar, index) =>
            h(
              'span',
              { className: 'th-gauge-row', key: WINDOWS[index] },
              h('i', { className: 'th-gauge-fill ' + bar.band, style: { width: bar.width + '%' } }),
            ),
          ),
        ),
        open
          ? h(Panel, {
              data,
              failed,
              entries,
              thresholds,
              busy,
              toast,
              addButton,
              open: openDialog,
              onRefresh: () => load(true),
              onCollapse: () => setOpen(false),
            })
          : null,
        modal,
      )
    }

    // -------------------------------------------------------------- mount

    /**
     * Register the widget into the frame's floating layer. 'shell.overlay' is
     * the list slot the layout declares for exactly this: a badge, a toast
     * stack, or a status pill, click-through except where the occupant opts
     * back into pointer events.
     */
    function apply(ctx) {
      const slots = ctx.get('slots')
      if (slots === undefined) return
      slots.inject('shell.overlay', () =>
        slots.register({ name: 'shell.overlay', id: 'dsh-tokenhub', order: 200, label: 'TokenHub' }, TokenHubWidget),
      )
    }

    exports.apply = apply
    exports.inject = ['slots']
    return module.exports
  },
})
