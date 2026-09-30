"""TokenHub v3 设置窗口：账号管理 + 选项。

- 账号管理：当前标记 / 改名 / 删除（当前账号不可删，提示先切换）；
  新增账号时真实调用一次 api.fetch_usage 校验，失败弹窗拒绝。
- 选项：刷新间隔 / 热区高度 / 热区宽度占比 / 屏幕顶边回退 / 主题。
- 废弃 V2 的 Cookie/curl 流程。

接口约定（供 main.py 集成）：
    SettingsWindow(root, config, theme_colors, api=None, on_save=None, get_current_key=None)
    - config: Lane A Config（accounts/settings/save/upsert_account/remove_account/mask_key）
    - api: Lane A ApiClient（fetch_usage 同步、15s 超时、不抛异常）；不传时内部自建
    - on_save: 保存/增删改后回调（main 用于刷新）
    - get_current_key: () -> str | None，返回当前账号密钥（用于"当前"标记与删除保护）；
      不传时回退读 auth_store 的 auth.json
    - 构造即显示窗口（main.py 的调用方式）；再次构造会复用已打开的同名窗口（类级单例）
"""

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from api import ApiClient  # Lane A 产出；运行期集成后可用


class SettingsWindow:
    """设置窗口（普通可聚焦对话框）。"""

    WINDOW_WIDTH = 580
    FONT = 'Microsoft YaHei UI'

    _active: Optional['SettingsWindow'] = None  # 类级单例：重复打开复用

    def __init__(self, root: tk.Tk, config, theme_colors: dict,
                 api: Optional[ApiClient] = None,
                 on_save: Optional[Callable] = None,
                 get_current_key: Optional[Callable] = None):
        self.root = root
        self.config = config
        self.colors = theme_colors
        self.api = api if api is not None else ApiClient()
        self.on_save = on_save
        self.get_current_key = get_current_key
        self.window: Optional[tk.Toplevel] = None
        self.show()

    # ============ 窗口 ============

    def show(self) -> None:
        existing = SettingsWindow._active
        if (existing is not None and existing is not self
                and existing.window is not None and existing.window.winfo_exists()):
            existing.window.lift()
            existing.window.focus_force()
            return
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            self.window.focus_force()
            return
        SettingsWindow._active = self
        self.window = tk.Toplevel(self.root)
        self.window.title('TokenHub 设置')
        self.window.resizable(False, False)
        self.window.configure(bg=self.colors['bg'])
        self._center()

        self._build()
        self.window.bind('<Escape>', lambda e: self._cancel())
        self.window.protocol('WM_DELETE_WINDOW', self._cancel)

    def hide(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()

    def _center(self) -> None:
        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        x = (sw - self.WINDOW_WIDTH) // 2
        y = (sh - 560) // 2
        self.window.geometry(f'{self.WINDOW_WIDTH}x560+{x}+{y}')

    # ============ 构建 ============

    def _build(self) -> None:
        c = self.colors
        bg, fg, sub = c['bg'], c['fg'], c['sub']
        main = tk.Frame(self.window, bg=bg, padx=24, pady=16)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- 账号管理 ----
        tk.Label(main, text='账号管理', font=(self.FONT, 12, 'bold'),
                 bg=bg, fg=fg).pack(anchor='w')
        # U10：账号操作即时落盘提示 + 内联反馈（成功绿字 2s 淡出 / 失败红字保留）
        tk.Label(main, text='账号变更将立即保存', font=(self.FONT, 8),
                 bg=bg, fg=sub).pack(anchor='w', pady=(2, 0))
        self._feedback_label = tk.Label(main, text='', font=(self.FONT, 9),
                                        bg=bg, fg=c['ok'], anchor='w')
        self._feedback_label.pack(anchor='w', pady=(2, 0))
        self._feedback_job: Optional[str] = None
        self._accounts_frame = tk.Frame(main, bg=bg)
        self._accounts_frame.pack(fill=tk.X, pady=(8, 0))
        self._refresh_accounts()

        # ---- 新增账号 ----
        add_frame = tk.Frame(main, bg=bg)
        add_frame.pack(fill=tk.X, pady=(12, 0))
        tk.Label(add_frame, text='新增账号', font=(self.FONT, 12, 'bold'),
                 bg=bg, fg=fg).pack(anchor='w')

        row1 = tk.Frame(add_frame, bg=bg)
        row1.pack(fill=tk.X, pady=(8, 0))
        tk.Label(row1, text='标签', font=(self.FONT, 9), bg=bg, fg=sub).pack(side=tk.LEFT)
        self._label_entry = tk.Entry(row1, font=(self.FONT, 10),
                                     bg=c['input_bg'], fg=fg, insertbackground=fg,
                                     relief='flat', highlightthickness=1,
                                     highlightbackground=c['border'], highlightcolor=c['accent'])
        self._label_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        row2 = tk.Frame(add_frame, bg=bg)
        row2.pack(fill=tk.X, pady=(6, 0))
        tk.Label(row2, text='密钥', font=(self.FONT, 9), bg=bg, fg=sub).pack(side=tk.LEFT)
        self._key_entry = tk.Entry(row2, font=(self.FONT, 10),
                                   bg=c['input_bg'], fg=fg, insertbackground=fg,
                                   relief='flat', highlightthickness=1,
                                   highlightbackground=c['border'], highlightcolor=c['accent'],
                                   show='●')  # U08：默认掩码
        self._key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self._key_visible = False
        self._toggle_key_btn = tk.Button(row2, text='显示', font=(self.FONT, 9),
                                         bd=0, relief='flat', cursor='hand2',
                                         bg=c['card'], fg=fg,
                                         activebackground=c['card'], activeforeground=c['accent'],
                                         command=self._toggle_key)
        self._toggle_key_btn.pack(side=tk.LEFT, padx=(8, 0))

        row3 = tk.Frame(add_frame, bg=bg)
        row3.pack(fill=tk.X, pady=(8, 0))
        self._paste_btn = tk.Button(row3, text='从剪贴板粘贴', font=(self.FONT, 9),
                                    bd=0, relief='flat', cursor='hand2',
                                    bg=c['card'], fg=fg,
                                    activebackground=c['card'], activeforeground=c['accent'],
                                    command=self._paste_key)
        self._paste_btn.pack(side=tk.LEFT)
        self._add_btn = tk.Button(row3, text='添加并校验', font=(self.FONT, 9, 'bold'),
                                  bd=0, relief='flat', cursor='hand2',
                                  bg=c['accent'], fg=c['bg'],
                                  activebackground=c['accent'], activeforeground=c['bg'],
                                  padx=16, command=self._add_account)
        self._add_btn.pack(side=tk.RIGHT)
        hint = tk.Label(row3, text='添加时会用该密钥请求一次官方接口校验',
                        font=(self.FONT, 9), bg=bg, fg=sub)
        hint.pack(side=tk.LEFT, padx=(12, 0))

        # ---- 选项 ----
        opt_frame = tk.Frame(main, bg=bg)
        opt_frame.pack(fill=tk.X, pady=(16, 0))
        tk.Label(opt_frame, text='选项', font=(self.FONT, 12, 'bold'),
                 bg=bg, fg=fg).pack(anchor='w')

        settings = self.config.settings
        grid = tk.Frame(opt_frame, bg=bg)
        grid.pack(fill=tk.X, pady=(8, 0))

        def add_option(row, text, widget):
            tk.Label(grid, text=text, font=(self.FONT, 9), bg=bg, fg=sub).grid(
                row=row, column=0, sticky='w', pady=4)
            widget.grid(row=row, column=1, sticky='w', padx=(16, 0), pady=4)

        self._interval_spin = tk.Spinbox(grid, from_=10, to=3600, increment=10,
                                         font=(self.FONT, 10), width=8,
                                         bg=c['input_bg'], fg=fg, insertbackground=fg,
                                         buttonbackground=c['card'], relief='flat')
        self._interval_spin.delete(0, 'end')
        self._interval_spin.insert(0, str(int(settings.get('refresh_interval', 60))))
        add_option(0, '刷新间隔（秒）', self._interval_spin)

        self._height_spin = tk.Spinbox(grid, from_=4, to=40, increment=1,
                                       font=(self.FONT, 10), width=8,
                                       bg=c['input_bg'], fg=fg, insertbackground=fg,
                                       buttonbackground=c['card'], relief='flat')
        self._height_spin.delete(0, 'end')
        self._height_spin.insert(0, str(int(settings.get('hotzone_height', 16))))
        add_option(1, '热区高度（像素）', self._height_spin)

        self._ratio_entry = tk.Entry(grid, font=(self.FONT, 10), width=8,
                                     bg=c['input_bg'], fg=fg, insertbackground=fg,
                                     relief='flat', highlightthickness=1,
                                     highlightbackground=c['border'], highlightcolor=c['accent'])
        self._ratio_entry.delete(0, 'end')
        self._ratio_entry.insert(0, str(float(settings.get('hotzone_width_ratio', 0.5))))
        add_option(2, '热区宽度占比（0.1–1.0）', self._ratio_entry)

        self._fallback_var = tk.BooleanVar(value=bool(settings.get('fallback_screen_edge', True)))
        self._fallback_check = tk.Checkbutton(
            grid, text='无 opencode 窗口时使用屏幕顶边热区', variable=self._fallback_var,
            font=(self.FONT, 9), bg=bg, fg=fg, activebackground=bg,
            activeforeground=fg, selectcolor=c['input_bg'], bd=0, highlightthickness=0)
        self._fallback_check.grid(row=3, column=0, columnspan=2, sticky='w', pady=4)

        self._theme_combo = ttk.Combobox(grid, values=['auto', 'light', 'dark'],
                                         state='readonly', width=8,
                                         font=(self.FONT, 10))
        self._theme_combo.set(str(settings.get('theme', 'auto')))
        add_option(4, '主题', self._theme_combo)

        self._offset_spin = tk.Spinbox(grid, from_=0, to=200, increment=1,
                                       font=(self.FONT, 10), width=8,
                                       bg=c['input_bg'], fg=fg, insertbackground=fg,
                                       buttonbackground=c['card'], relief='flat')
        self._offset_spin.delete(0, 'end')
        self._offset_spin.insert(0, str(int(settings.get('hotzone_offset', 48))))
        add_option(5, '热区偏移（像素）', self._offset_spin)

        # ---- 底部按钮 ----
        btn_frame = tk.Frame(main, bg=bg)
        btn_frame.pack(fill=tk.X, pady=(16, 0))
        save_btn = tk.Button(btn_frame, text='保存选项', font=(self.FONT, 10, 'bold'),
                             bd=0, relief='flat', cursor='hand2',
                             bg=c['accent'], fg=c['bg'],
                             activebackground=c['accent'], activeforeground=c['bg'],
                             padx=24, pady=6, command=self._save)
        save_btn.pack(side=tk.RIGHT)
        cancel_btn = tk.Button(btn_frame, text='关闭', font=(self.FONT, 10),
                               bd=0, relief='flat', cursor='hand2',
                               bg=c['card'], fg=fg,
                               activebackground=c['card'], activeforeground=sub,
                               padx=24, pady=6, command=self._cancel)
        cancel_btn.pack(side=tk.RIGHT, padx=(0, 12))

    # ============ 账号列表 ============

    def _toggle_key(self) -> None:
        """U08：显示/隐藏密钥输入（切换不改内容）。"""
        self._key_visible = not self._key_visible
        self._key_entry.configure(show='' if self._key_visible else '●')
        self._toggle_key_btn.configure(text='隐藏' if self._key_visible else '显示')

    def _inline_feedback(self, text: str, ok: bool = True) -> None:
        """U10：内联轻量反馈——成功绿字 2s 后淡出清空；失败红字保留。"""
        if self._feedback_job:
            try:
                self.window.after_cancel(self._feedback_job)
            except Exception:
                pass
            self._feedback_job = None
        self._feedback_label.configure(text=text,
                                       fg=self.colors['ok'] if ok else self.colors['warn'])
        if ok:
            self._feedback_job = self.window.after(
                2000, lambda: self._feedback_label.configure(text=''))

    def _current_key(self) -> Optional[str]:
        """当前账号密钥：优先回调，其次读 auth.json（main 未传入时兜底）。"""
        if self.get_current_key is not None:
            try:
                return self.get_current_key()
            except Exception:
                return None
        try:
            from auth_store import AuthStore
            return AuthStore().current_key()
        except Exception:
            return None

    def _refresh_accounts(self) -> None:
        for w in self._accounts_frame.winfo_children():
            w.destroy()
        c = self.colors
        bg, fg, sub = c['bg'], c['fg'], c['sub']
        current_key = self._current_key()

        accounts = list(getattr(self.config, 'accounts', []) or [])
        if not accounts:
            tk.Label(self._accounts_frame, text='（暂无账号，请先添加）',
                     font=(self.FONT, 9), bg=bg, fg=sub).pack(anchor='w', pady=4)
            return

        for account in accounts:
            key = getattr(account, 'key', '')
            label = getattr(account, 'label', '') or '未命名'
            is_current = current_key is not None and key == current_key

            row = tk.Frame(self._accounts_frame, bg=bg)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text='● 当前' if is_current else '○',
                     font=(self.FONT, 9, 'bold'), bg=bg,
                     fg=c['accent'] if is_current else sub).pack(side=tk.LEFT)
            tk.Label(row, text=label, font=(self.FONT, 10, 'bold'), bg=bg,
                     fg=fg).pack(side=tk.LEFT, padx=(8, 0))
            tk.Label(row, text=self._mask(key), font=(self.FONT, 9), bg=bg,
                     fg=sub).pack(side=tk.LEFT, padx=(8, 0))

            rename_btn = tk.Button(row, text='改名', font=(self.FONT, 9),
                                   bd=0, relief='flat', cursor='hand2',
                                   bg=c['card'], fg=fg,
                                   activebackground=c['card'], activeforeground=c['accent'],
                                   padx=10, command=lambda k=key: self._rename_account(k))
            rename_btn.pack(side=tk.RIGHT)
            del_btn = tk.Button(row, text='删除', font=(self.FONT, 9),
                                bd=0, relief='flat', cursor='hand2',
                                bg=c['card'], fg=c['warn'],
                                activebackground=c['card'], activeforeground=c['warn'],
                                padx=10, command=lambda k=key: self._delete_account(k))
            del_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _mask(self, key: str) -> str:
        """掩码唯一规则委托 Config.mask_key（不保留本地实现，避免规则漂移）。"""
        return self.config.mask_key(key)

    # ============ 新增 / 改名 / 删除 ============

    def _paste_key(self) -> None:
        try:
            text = self.root.clipboard_get()
        except Exception:
            messagebox.showwarning('提示', '剪贴板中没有可用的文本', parent=self.window)
            return
        text = text.strip().splitlines()[0].strip() if text.strip() else ''
        if text:
            self._key_entry.delete(0, 'end')
            self._key_entry.insert(0, text)

    def _next_default_label(self) -> str:
        accounts = getattr(self.config, 'accounts', []) or []
        used = set()
        for a in accounts:
            lbl = getattr(a, 'label', '') or ''
            if lbl.startswith('账号'):
                try:
                    used.add(int(lbl[2:]))
                except ValueError:
                    pass
        n = 1
        while n in used:
            n += 1
        return f'账号{n}'

    def _add_account(self) -> None:
        label = self._label_entry.get().strip()
        key = self._key_entry.get().strip()
        if not key:
            messagebox.showwarning('提示', '请输入 API Key', parent=self.window)
            return
        if not (key.startswith('sk-') or key.startswith('oc_sk_')):
            messagebox.showwarning('提示', '密钥应以 sk- 或 oc_sk_ 开头', parent=self.window)
            return
        if not label:
            label = self._next_default_label()

        self._add_btn.configure(state='disabled', text='校验中…')

        def work():
            try:
                usage = self.api.fetch_usage(key)
            except Exception as exc:  # 防御：api 实现不应抛异常
                usage = None
                err = str(exc)
            else:
                err = self._usage_error(usage)
            try:
                self.window.after(0, lambda: self._on_add_done(label, key, usage, err))
            except Exception:
                pass  # 校验期间窗口已被关闭

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _usage_error(usage) -> str:
        if usage is None:
            return '接口无返回'
        for metric_key in ('rolling', 'weekly', 'monthly'):
            m = getattr(usage, metric_key, None)
            if m is not None:
                err = getattr(m, 'error', None)
                if err:
                    return f'{metric_key}: {err}'
                if getattr(m, 'percent', None) is None:
                    return f'{metric_key}: 数据不完整'
        return ''

    def _on_add_done(self, label, key, usage, err) -> None:
        if not self.window or not self.window.winfo_exists():
            return
        self._add_btn.configure(state='normal', text='添加并校验')
        if err:
            # 校验失败：红字内联提示，保留输入可重试
            self._inline_feedback(f'校验失败：{err}', ok=False)
            return
        try:
            self.config.upsert_account(label, key)  # R3-04：方法内已自落盘
        except Exception as exc:
            self._inline_feedback(f'保存失败：{exc}', ok=False)
            return
        if self.on_save:
            self.on_save()
        self._refresh_accounts()
        self._key_entry.delete(0, 'end')
        self._label_entry.delete(0, 'end')
        self._inline_feedback(f'已添加「{label}」并通过密钥校验')

    def _rename_account(self, key: str) -> None:
        accounts = getattr(self.config, 'accounts', []) or []
        account = next((a for a in accounts if getattr(a, 'key', '') == key), None)
        if account is None:
            return
        old_label = getattr(account, 'label', '') or ''

        dialog = tk.Toplevel(self.window)
        dialog.title('改名')
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.configure(bg=self.colors['bg'])
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        dialog.geometry(f'320x140+{(sw - 320) // 2}+{(sh - 140) // 2}')

        frame = tk.Frame(dialog, bg=self.colors['bg'], padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text='新标签：', font=(self.FONT, 10),
                 bg=self.colors['bg'], fg=self.colors['fg']).pack(anchor='w')
        entry = tk.Entry(frame, font=(self.FONT, 10),
                         bg=self.colors['input_bg'], fg=self.colors['fg'],
                         insertbackground=self.colors['fg'], relief='flat',
                         highlightthickness=1, highlightbackground=self.colors['border'],
                         highlightcolor=self.colors['accent'])
        entry.insert(0, old_label)
        entry.pack(fill=tk.X, pady=(8, 0))
        entry.focus_set()
        entry.select_range(0, 'end')

        def confirm():
            new_label = entry.get().strip()
            if not new_label:
                messagebox.showwarning('提示', '标签不能为空', parent=dialog)
                return
            try:
                self.config.upsert_account(new_label, key)  # R3-04：已自落盘
            except Exception as exc:
                self._inline_feedback(f'保存失败：{exc}', ok=False)
                return
            if self.on_save:
                self.on_save()
            dialog.destroy()
            self._refresh_accounts()
            self._inline_feedback(f'已改名为「{new_label}」')

        btn_frame = tk.Frame(frame, bg=self.colors['bg'])
        btn_frame.pack(fill=tk.X, pady=(12, 0))
        tk.Button(btn_frame, text='确定', font=(self.FONT, 10),
                  bd=0, relief='flat', cursor='hand2',
                  bg=self.colors['accent'], fg=self.colors['bg'],
                  padx=16, command=confirm).pack(side=tk.RIGHT)
        tk.Button(btn_frame, text='取消', font=(self.FONT, 10),
                  bd=0, relief='flat', cursor='hand2',
                  bg=self.colors['card'], fg=self.colors['fg'],
                  padx=16, command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind('<Escape>', lambda e: dialog.destroy())
        dialog.bind('<Return>', lambda e: confirm())

    def _delete_account(self, key: str) -> None:
        accounts = getattr(self.config, 'accounts', []) or []
        account = next((a for a in accounts if getattr(a, 'key', '') == key), None)
        if account is None:
            return
        label = getattr(account, 'label', '') or '未命名'

        if key == self._current_key():
            messagebox.showwarning('无法删除',
                                   f'「{label}」是当前账号，不可删除。\n请先切换到其他账号，再回来删除。',
                                   parent=self.window)
            return
        if not messagebox.askyesno('确认删除', f'确定删除「{label}」吗？', parent=self.window):
            return
        try:
            ok = self.config.remove_account(key)  # R3-04：已自落盘
            if not ok:
                self._inline_feedback('删除失败：未找到该账号', ok=False)
                return
        except Exception as exc:
            self._inline_feedback(f'保存失败：{exc}', ok=False)
            return
        if self.on_save:
            self.on_save()
        self._refresh_accounts()
        self._inline_feedback(f'已删除「{label}」')

    # ============ 保存 / 取消 ============

    def _save(self) -> None:
        try:
            interval = int(self._interval_spin.get())
            if not 10 <= interval <= 3600:
                raise ValueError
        except ValueError:
            messagebox.showwarning('提示', '刷新间隔应为 10–3600 的整数', parent=self.window)
            return
        try:
            height = int(self._height_spin.get())
            if not 4 <= height <= 40:
                raise ValueError
        except ValueError:
            messagebox.showwarning('提示', '热区高度应为 4–40 的整数', parent=self.window)
            return
        try:
            ratio = float(self._ratio_entry.get())
            if not 0.1 <= ratio <= 1.0:
                raise ValueError
        except ValueError:
            messagebox.showwarning('提示', '热区宽度占比应为 0.1–1.0 的数字', parent=self.window)
            return
        try:
            offset = int(self._offset_spin.get())
            if not 0 <= offset <= 200:
                raise ValueError
        except ValueError:
            messagebox.showwarning('提示', '热区偏移应为 0–200 的整数', parent=self.window)
            return

        # R2-03/R2-01：统一经 apply_settings 落盘——失败时由 Config 恢复内存
        # 状态并抛出（含 ConfigWriteBlocked），此处显示实际消息。
        try:
            self.config.apply_settings({
                'refresh_interval': interval,
                'hotzone_height': height,
                'hotzone_width_ratio': ratio,
                'hotzone_offset': offset,
                'fallback_screen_edge': bool(self._fallback_var.get()),
                'theme': str(self._theme_combo.get()),
            })
        except Exception as exc:
            self._inline_feedback(f'选项保存失败：{exc}', ok=False)
            return
        if self.on_save:
            self.on_save()
        self._cancel()

    def _cancel(self) -> None:
        if SettingsWindow._active is self:
            SettingsWindow._active = None
        if self.window is not None and self.window.winfo_exists():
            self.window.destroy()
        self.window = None