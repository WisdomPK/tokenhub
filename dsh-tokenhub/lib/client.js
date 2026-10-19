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
 * role here, with a persistent ball whose badge shows the worst window.
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
    const zh = {
      title: 'OpenCode Go \u989d\u5ea6',
      updated: '\u66f4\u65b0\u4e8e',
      refresh: '\u5237\u65b0',
      collapse: '\u6536\u8d77',
      loading: '\u52a0\u8f7d\u4e2d\u2026',
      loadFailed: '\u5feb\u7167\u62c9\u53d6\u5931\u8d25',
      noKeys: '\u672a\u914d\u7f6e opencode-go \u5bc6\u94a5\uff08\u51ed\u636e\u91cc\u65e2\u65e0 OPENCODE_GO_KEY_* \u4e5f\u65e0 OPENCODE_GO_API_KEY\uff09',
      noEntries: '\u672a\u53d1\u73b0\u8d26\u53f7',
      inUse: '\u4f7f\u7528\u4e2d',
      notInPool: '\u672a\u5165\u6c60',
      w: { rolling: '5\u5c0f\u65f6', weekly: '\u5468', monthly: '\u6708' },
      currentKey: '\u5f53\u524d\u5bc6\u94a5',
      join: '\u52a0\u5165\u6c60',
      joinTitle: '\u628a\u5f53\u524d\u5bc6\u94a5\u52a0\u5165\u8d26\u53f7\u6c60',
      joinHint: '\u5165\u6c60\u540e\u5b83\u4f1a\u4f5c\u4e3a\u4e00\u6761\u5907\u7528\u8d26\u53f7\u7559\u5728\u5217\u8868\u91cc\uff0c\u5207\u6362\u8d70\u4e4b\u540e\u4e5f\u8fd8\u80fd\u5207\u56de\u6765\u3002',
      joinCta: '\u52a0\u5165',
      rename: '\u6539\u540d',
      renameTitle: '\u91cd\u547d\u540d\u8d26\u53f7',
      renameLabel: '\u65b0\u8d26\u53f7\u540d',
      renameCta: '\u4fdd\u5b58',
      renameHint: '\u53ea\u6539\u540d\u5b57\uff1a\u5bc6\u94a5\u4e0d\u52a8\uff0c\u300c\u5f53\u524d\u751f\u6548\u300d\u72b6\u6001\u4e5f\u4e0d\u53d8\u3002',
      noActive: '\u5f53\u524d\u6ca1\u6709\u751f\u6548\u7684\u5bc6\u94a5',
      day: '\u5929',
      add: '\u65b0\u589e',
      switch: '\u5207\u6362',
      switching: '\u5207\u6362\u4e2d\u2026',
      remove: '\u5220\u9664',
      cancel: '\u53d6\u6d88',
      confirm: '\u786e\u8ba4',
      ok: '\u786e\u5b9a',
      offline: '\u79bb\u5c94',
      noData: '\u6570\u636e\u4e0d\u53ef\u7528',
      reset: '\u5373\u5c06\u91cd\u7f6e',
      unauthorized: '\u5bc6\u94a5\u65e0\u6548',
      limited: '\u5df2\u9650\u6d41',
      addTitle: '\u65b0\u589e\u8d26\u53f7',
      addName: '\u8d26\u53f7\u540d\uff08\u53ef\u7559\u7a7a\uff0c\u81ea\u52a8\u547d\u540d\uff09',
      addKey: 'API Key',
      paste: '\u4ece\u526a\u8d34\u677f\u7c98\u8d34',
      pasted: '\u5df2\u7c98\u8d34',
      show: '\u663e\u793a',
      hide: '\u9690\u85cf',
      checking: '\u9a8c\u8bc1\u4e2d\u2026',
      switchAsk: '\u786e\u8ba4\u5207\u6362\u5230\u300c{n}\u300d\uff1f\u540e\u7eed\u8bf7\u6c42\u5c06\u4f7f\u7528\u8be5\u8d26\u53f7\u3002',
      removeAsk: '\u786e\u8ba4\u5220\u9664\u300c{n}\u300d\uff1f\u8be5\u8d26\u53f7\u5c06\u4ece\u672c\u673a\u8d26\u53f7\u5217\u8868\u4e2d\u79fb\u9664\u3002',
      switchCta: '\u786e\u8ba4\u5207\u6362',
      removeCta: '\u5220\u9664',
      close: '\u5173\u95ed',
      hostError: 'Host \u9519\u8bef',
    }
    const en = {
      title: 'OpenCode Go quota',
      updated: 'Updated',
      refresh: 'Refresh',
      collapse: 'Collapse',
      loading: 'Loading\u2026',
      loadFailed: 'Failed to fetch snapshot',
      noKeys: 'No opencode-go key configured (neither OPENCODE_GO_KEY_* nor OPENCODE_GO_API_KEY)',
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
      switching: 'Switching\u2026',
      remove: 'Remove',
      cancel: 'Cancel',
      confirm: 'Confirm',
      ok: 'OK',
      offline: 'offline',
      noData: 'no data',
      reset: 'resetting',
      unauthorized: 'invalid key',
      limited: 'rate-limited',
      addTitle: 'Add account',
      addName: 'Account name (blank generates one)',
      addKey: 'API key',
      paste: 'Paste from clipboard',
      pasted: 'Pasted',
      show: 'Show',
      hide: 'Hide',
      checking: 'Verifying\u2026',
      switchAsk: 'Switch to "{n}"? Later requests will use that account.',
      removeAsk: 'Remove "{n}"? It will be dropped from this machine\u2019s account list.',
      switchCta: 'Switch',
      removeCta: 'Remove',
      close: 'Close',
      hostError: 'Host error',
    }
    const isZh = typeof navigator !== 'undefined' && /^\s*zh/i.test(navigator.language || '')
    const t = isZh ? zh : en
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
        '.th-gauge{position:fixed;right:10px;top:50%;z-index:1000;',
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
        // width so a bigger gauge never covers it.
        '.th-panel{position:fixed;top:50%;z-index:999;width:356px;',
        'right:calc(18px + 34px * var(--th-scale,1));',
        'transform:translateY(-50%);max-height:78vh;',
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

    /**
     * Severity band for one window, matching TokenHub's thresholds: normal
     * below warn, caution up to error, alarm at error and above. A window the
     * endpoint itself flags as rate-limited is always the alarm band.
     */
    function level(percent, status, thresholds) {
      if (status === 'rate-limited') return 'err'
      if (percent === null || percent === undefined || Number.isNaN(percent)) return 'na'
      if (percent >= thresholds.error) return 'err'
      if (percent >= thresholds.warn) return 'warn'
      return 'ok'
    }

    /** Reset countdown: under a day in H/M, beyond it in days and hours. */
    function fmtRemain(iso) {
      if (!iso) return '\u2014'
      const when = new Date(iso)
      if (Number.isNaN(when.getTime())) return '\u2014'
      const ms = when.getTime() - Date.now()
      if (ms <= 0) return t.reset
      const minutes = Math.floor(ms / 60000)
      const hours = Math.floor(minutes / 60)
      if (hours < 24) return hours + 'H' + (minutes % 60) + 'M'
      return Math.floor(hours / 24) + t.day + (hours % 24) + 'H'
    }

    /** A short label for a window that returned no percentage. */
    function metricNote(entry, window, isErr) {
      if (isErr && (entry.error === 'unauthorized' || entry.error === 'http-401')) return t.unauthorized
      if (isErr || !window || window.percent === null) return t.noData
      if (window.status === 'rate-limited') return t.limited
      return t.noData
    }

    /** An entry with no usable window at all renders as one offline line. */
    function isOffline(entry) {
      if (!entry.windows) return true
      return WINDOWS.every((key) => {
        const win = entry.windows[key]
        return !win || win.percent === null
      })
    }


    /**
     * The gauge's three bars, one per window, in panel order: rolling on top,
     * then weekly, then monthly. Each carries its own window's fill and colour,
     * so which quota is filling up is answered by position and hue together.
     *
     * A window with no reading fills the whole track in the neutral colour: an
     * empty bar would claim "nothing used" for a value nobody received.
     * @param entries - the snapshot rows; an empty list yields three unknowns.
     * @param thresholds - colour bands.
     * @returns one { band, width } per window.
     */
    function barShape(entries, thresholds) {
      const source = subjectOf(entries)
      return WINDOWS.map((key) => {
        const win = source && source.windows ? source.windows[key] : null
        const known = Boolean(win) && typeof win.percent === 'number' && Number.isFinite(win.percent)
        if (!known) return { band: 'na', width: 100 }
        return {
          band: level(win.percent, win.status, thresholds),
          width: Math.max(0, Math.min(100, win.percent)),
        }
      })
    }

    /** The account a summary describes: the one in force, else the first row. */
    function subjectOf(entries) {
      const actives = entries.filter((entry) => entry.active)
      return actives.length > 0 ? actives[0] : entries[0]
    }

    /**
     * The gauge's tooltip: the three bars coloured say how much at a glance but
     * not how many, so this carries the numbers behind them and stops there.
     */
    function fabTooltip(entries) {
      const source = subjectOf(entries)
      if (!source) return t.title
      const name = source.name || t.currentKey
      const parts = WINDOWS.map((key) => {
        const win = source.windows ? source.windows[key] : null
        const known = win && typeof win.percent === 'number' && Number.isFinite(win.percent)
        return t.w[key] + ' ' + (known ? Math.round(win.percent) + '%' : t.noData)
      })
      return t.title + ' \u00b7 ' + name + ' \u00b7 ' + parts.join(' \u00b7 ')
    }

    /** Clock text for the last successful refresh. */
    function fmtClock(ms) {
      if (!ms) return '\u2014'
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
        .then((res) => res.json().then((data) => data).catch(() => ({ ok: false, error: 'invalid response' })))
        .catch((error) => ({ ok: false, error: String((error && error.message) || error) }))
    }

    // ----------------------------------------------------------- dialogs

    /** Modal shell shared by the confirm and add dialogs. */
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
     * Add-account dialog. The key is verified by the Host against the live
     * endpoint before it is stored, so a bad key is refused inside this dialog
     * and the inputs survive for a retry.
     */
    function AddDialog(props) {
      const mode = props.mode || 'add'
      const joining = mode === 'join'
      const renaming = mode === 'rename'
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
          .then((text) => {
            if (text && text.trim()) {
              setKey(text.trim())
              setNote({ kind: 'info', text: t.pasted })
            }
          })
          .catch(() => setNote({ kind: 'err', text: t.noData }))
      }

      const submit = () => {
        if (busy) return
        setBusy(true)
        setNote(null)
        const payload = joining
          ? { op: 'joinPool', name: accountName }
          : renaming
            ? { op: 'rename', name: props.from, to: accountName }
            : { op: 'add', name: accountName, key: key }
        post(ACCOUNT, payload).then((res) => {
          setBusy(false)
          if (res && res.ok) {
            props.onAdded(res.message || t.ok)
            return
          }
          setNote({ kind: 'err', text: (res && res.error) || t.hostError })
        })
      }

      return h(
        Modal,
        { onCancel: props.onCancel },
        h('div', { className: 'th-modal-title' }, renaming ? t.renameTitle : joining ? t.joinTitle : t.addTitle),
        renaming
          ? h('div', { className: 'th-modal-body' }, t.renameHint)
          : joining
            ? h('div', { className: 'th-modal-body' }, t.joinHint)
            : null,
        h(
          'div',
          { className: 'th-field' },
          h('label', { className: 'th-label' }, renaming ? t.renameLabel : t.addName),
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
        mode !== 'add'
          ? null
          : h(
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
        ),
        note ? h('div', { className: 'th-hint ' + note.kind }, note.text) : null,
        h(
          'div',
          { className: 'th-actions' },
          h('button', { className: 'th-btn', onClick: props.onCancel }, t.cancel),
          h(
            'button',
            { className: 'th-btn', disabled: busy, onClick: submit },
            busy
              ? mode === 'add'
                ? t.checking
                : t.confirm
              : renaming
                ? t.renameCta
                : joining
                  ? t.joinCta
                  : t.confirm,
          ),
        ),
      )
    }

    // ------------------------------------------------------------ widget

    /** One metric row: label, threshold-coloured bar, percentage, countdown. */
    function MetricRow(props) {
      const win = props.entry.windows ? props.entry.windows[props.window] : null
      const known = win && typeof win.percent === 'number' && Number.isFinite(win.percent)
      const band = level(known ? win.percent : null, win ? win.status : null, props.thresholds)
      const width = known ? Math.max(0, Math.min(100, win.percent)) : 0
      return h(
        'div',
        { className: 'th-row' },
        h('span', { className: 'th-row-label' }, props.label),
        h('span', { className: 'th-bar' }, h('i', { className: 'th-fill ' + band, style: { width: width + '%' } })),
        h('span', { className: 'th-pct ' + band }, known ? Math.round(win.percent) + '%' : t.noData),
        h('span', { className: 'th-reset' }, win && win.resetsAt ? '\u27f3 ' + fmtRemain(win.resetsAt) : ''),
      )
    }

    /** One account card: identity, three metric rows, and the row's actions. */
    function AccountCard(props) {
      const entry = props.entry
      const offline = isOffline(entry)
      const actions = []
      // The row in force carries Add. It is the row the user is looking at, and
      // the panel must never end up with no way to add another account.
      if (entry.active) {
        actions.push(h('button', { className: 'th-btn', key: 'add', onClick: props.onAdd }, t.add))
        // A key with no pool entry behind it disappears the moment a switch
        // replaces it, so offer to file it before that can happen.
        if (entry.inPool === false) {
          actions.push(h('button', { className: 'th-btn', key: 'join', onClick: props.onJoin }, t.join))
        }
      }
      if (!entry.active && entry.inPool !== false) {
        actions.push(
          h(
            'button',
            { className: 'th-btn', key: 'switch', disabled: props.busy, onClick: props.onSwitch },
            props.busy ? t.switching : t.switch,
          ),
        )
      }
      // Renaming only changes the name a key is filed under, so it is safe on
      // the account in force as well as on a spare.
      if (entry.inPool !== false) {
        actions.push(h('button', { className: 'th-btn', key: 'rename', onClick: props.onRename }, t.rename))
      }
      if (!entry.active && entry.inPool !== false) {
        actions.push(
          h(
            'button',
            { className: 'th-btn th-danger', key: 'remove', disabled: props.busy, onClick: props.onRemove },
            t.remove,
          ),
        )
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
          entry.inPool === false ? h('span', { className: 'th-chip' }, t.notInPool) : null,
          entry.masked ? h('span', { className: 'th-mask' }, entry.masked) : null,
          h('span', { className: 'th-spacer' }),
          actions,
        ),
        offline
          ? h('div', { className: 'th-offline' }, t.offline + (entry.error ? ' \u00b7 ' + entry.error : ''))
          : WINDOWS.map((key) =>
              h(MetricRow, {
                key: key,
                entry: entry,
                window: key,
                label: t.w[key],
                thresholds: props.thresholds,
              }),
            ),
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

      const say = (kind, text) => setToast({ kind: kind, text: text })

      const runSwitch = (name) => {
        setBusy(name)
        post(SWITCH, { name: name }).then((res) => {
          setBusy(null)
          if (res && res.ok) {
            // The host composes the whole message; a failed opencode sync inside
            // it still deserves the problem styling.
            const authFailed = res.opencodeAuth && res.opencodeAuth.ok === false
            say(authFailed ? 'err' : 'ok', res.message || t.ok)
          } else {
            say('err', (res && res.error) || t.hostError)
          }
          load(true)
        })
      }

      const runRemove = (name) => {
        setBusy(name)
        post(ACCOUNT, { op: 'remove', name: name }).then((res) => {
          setBusy(null)
          say(res && res.ok ? 'ok' : 'err', (res && (res.message || res.error)) || t.hostError)
          load(true)
        })
      }

      const thresholds = (data && data.thresholds) || { warn: 60, error: 85 }
      const entries = (data && data.entries) || []
      // The gauge's three bars, top to bottom: rolling, weekly, monthly. A failed
      // snapshot is folded in as an empty list, so every bar reads "no data"
      // rather than three calm ones.
      const bars = barShape(failed ? [] : entries, thresholds)

      const openAdd = () => setDialog({ kind: 'add' })
      const addButton = () =>
        h('button', { className: 'th-btn', style: { marginTop: '8px' }, onClick: openAdd }, t.add)

      let body
      if (failed) body = h('div', { className: 'th-empty' }, t.loadFailed)
      else if (!data) body = h('div', { className: 'th-empty' }, t.loading)
      else if (entries.length === 0) {
        body = h(
          'div',
          { className: 'th-empty' },
          h('div', null, data.error === 'no-keys' ? t.noKeys : t.noEntries),
          addButton(),
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
            entry: entry,
            thresholds: thresholds,
            busy: busy === rowId,
            onAdd: openAdd,
            onJoin: () => setDialog({ kind: 'join' }),
            onRename: () => setDialog({ kind: 'rename', from: rowId, current: entry.name }),
            onSwitch: () => setDialog({ kind: 'confirm', action: 'switch', name: rowId }),
            onRemove: () => setDialog({ kind: 'confirm', action: 'remove', name: rowId }),
          })
        })
        // Safety net: with no row in force (no key configured, say) the panel
        // still offers Add rather than leaving the user with no way forward.
        body = entries.some((entry) => entry.active)
          ? cards
          : h(React.Fragment, null, cards, h('div', { className: 'th-empty' }, h('div', null, t.noActive), addButton()))
      }

      const panel = open
        ? h(
            'div',
            { className: 'th-panel' },
            h(
              'div',
              { className: 'th-head' },
              h('span', { className: 'th-title' }, t.title),
              h('span', { className: 'th-time' }, t.updated + ' ' + fmtClock(data && data.updatedAt)),
              h('button', { className: 'th-btn', onClick: () => load(true) }, t.refresh),
              h('button', { className: 'th-btn', onClick: () => setOpen(false) }, t.collapse),
            ),
            body,
            toast ? h('div', { className: 'th-toast ' + toast.kind }, toast.text) : null,
          )
        : null

      let modal = null
      if (dialog && dialog.kind === 'add') {
        modal = h(AddDialog, {
          mode: 'add',
          onCancel: () => setDialog(null),
          onAdded: (message) => {
            setDialog(null)
            say('ok', message)
            load(true)
          },
        })
      } else if (dialog && dialog.kind === 'rename') {
        modal = h(AddDialog, {
          mode: 'rename',
          from: dialog.from,
          initialName: dialog.current,
          onCancel: () => setDialog(null),
          onAdded: (message) => {
            setDialog(null)
            say('ok', message)
            load(true)
          },
        })
      } else if (dialog && dialog.kind === 'join') {
        modal = h(AddDialog, {
          mode: 'join',
          onCancel: () => setDialog(null),
          onAdded: (message) => {
            setDialog(null)
            say('ok', message)
            load(true)
          },
        })
      } else if (dialog && dialog.kind === 'confirm') {
        const isRemove = dialog.action === 'remove'
        modal = h(ConfirmDialog, {
          title: isRemove ? t.remove : t.switch,
          body: fill(isRemove ? t.removeAsk : t.switchAsk, dialog.name),
          cta: isRemove ? t.removeCta : t.switchCta,
          danger: isRemove,
          onCancel: () => setDialog(null),
          onConfirm: () => {
            const name = dialog.name
            setDialog(null)
            if (isRemove) runRemove(name)
            else runSwitch(name)
          },
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
        panel,
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
