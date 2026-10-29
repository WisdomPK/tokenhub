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

from api import ApiClient, usage_validation_error  # Lane A 产出；运行期集成后可用


class SettingsWindow:
    """设置窗口（普通可聚焦对话框）。"""

    WINDOW_WIDTH = 580
    WINDOW_HEIGHT = 560
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
        self._auth_read_error: Optional[str] = None
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
        y = (sh - self.WINDOW_HEIGHT) // 2
        self.window.geometry(
            f'{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}')

    # ============ 控件工厂 ============

    def _make_button(self, parent, text: str, command, *, style: str = '',
                     font_size: int = 9, bold: bool = False,
                     padx: Optional[int] = None, pady: Optional[int] = None,
                     active_fg: Optional[str] = None) -> tk.Button:
        """扁平按钮：统一 bd/relief/cursor 与三套配色（style='' / 'accent' / 'danger'）。"""
        c = self.colors
        if style == 'accent':
            bg, fg, hover = c['accent'], c['bg'], c['bg']
        elif style == 'danger':
            bg, fg, hover = c['card'], c['warn'], c['warn']
        else:
            bg, fg, hover = c['card'], c['fg'], c['accent']
        if active_fg is not None:
            hover = active_fg
        font = (self.FONT, font_size, 'bold') if bold else (self.FONT, font_size)
        btn = tk.Button(parent, text=text, font=font, bd=0, relief='flat',
                        cursor='hand2', bg=bg, fg=fg,
                        activebackground=bg, activeforeground=hover,
                        command=command)
        if padx is not None:
            btn.configure(padx=padx)
        if pady is not None:
            btn.configure(pady=pady)
        return btn

    def _make_entry(self, parent, *, width: Optional[int] = None,
                    show: Optional[str] = None) -> tk.Entry:
        """单行输入框：统一输入底色 / 高亮边框 / 光标色。"""
        c = self.colors
        entry = tk.Entry(parent, font=(self.FONT, 10), bg=c['input_bg'],
                         fg=c['fg'], insertbackground=c['fg'], relief='flat',
                         highlightthickness=1, highlightbackground=c['border'],
                         highlightcolor=c['accent'])
        if width is not None:
            entry.configure(width=width)
        if show is not None:
            entry.configure(show=show)
        return entry

    def _make_spin(self, parent, lo: int, hi: int, value: int,
                   *, step: int = 1) -> tk.Spinbox:
        """整数微调框：统一底色 / 按钮底色，并预填当前值。"""
        c = self.colors
        spin = tk.Spinbox(parent, from_=lo, to=hi, increment=step,
                          font=(self.FONT, 10), width=8, bg=c['input_bg'],
                          fg=c['fg'], insertbackground=c['fg'],
                          buttonbackground=c['card'], relief='flat')
        spin.delete(0, 'end')
        spin.insert(0, str(value))
        return spin

    def _find_account(self, key: str):
        """按密钥查配置里的账号对象（找不到返回 None）。"""
        accounts = getattr(self.config, 'accounts', []) or []
        return next((a for a in accounts if getattr(a, 'key', '') == key), None)

    def _notify_saved(self) -> None:
        """账号/选项落盘后通知外部刷新（未注入回调时无操作）。"""
        if self.on_save is not None:
            self.on_save()

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
        self._label_entry = self._make_entry(row1)
        self._label_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        row2 = tk.Frame(add_frame, bg=bg)
        row2.pack(fill=tk.X, pady=(6, 0))
        tk.Label(row2, text='密钥', font=(self.FONT, 9), bg=bg, fg=sub).pack(side=tk.LEFT)
        self._key_entry = self._make_entry(row2, show='●')  # U08：默认掩码
        self._key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self._key_visible = False
        self._toggle_key_btn = self._make_button(row2, '显示', self._toggle_key)
        self._toggle_key_btn.pack(side=tk.LEFT, padx=(8, 0))

        row3 = tk.Frame(add_frame, bg=bg)
        row3.pack(fill=tk.X, pady=(8, 0))
        self._paste_btn = self._make_button(row3, '从剪贴板粘贴', self._paste_key)
        self._paste_btn.pack(side=tk.LEFT)
        self._add_btn = self._make_button(row3, '添加并校验', self._add_account,
                                          style='accent', bold=True, padx=16)
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

        self._interval_spin = self._make_spin(
            grid, 10, 3600, int(settings.get('refresh_interval', 60)), step=10)
        add_option(0, '刷新间隔（秒）', self._interval_spin)

        self._height_spin = self._make_spin(
            grid, 4, 40, int(settings.get('hotzone_height', 16)))
        add_option(1, '热区高度（像素）', self._height_spin)

        self._ratio_entry = self._make_entry(grid, width=8)
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

        self._offset_spin = self._make_spin(
            grid, 0, 200, int(settings.get('hotzone_offset', 48)))
        add_option(5, '热区偏移（像素）', self._offset_spin)

        # ---- 底部按钮 ----
        btn_frame = tk.Frame(main, bg=bg)
        btn_frame.pack(fill=tk.X, pady=(16, 0))
        save_btn = self._make_button(btn_frame, '保存选项', self._save,
                                     style='accent', font_size=10, bold=True,
                                     padx=24, pady=6)
        save_btn.pack(side=tk.RIGHT)
        cancel_btn = self._make_button(btn_frame, '关闭', self._cancel,
                                       font_size=10, padx=24, pady=6,
                                       active_fg=sub)
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
        """当前账号密钥：读取异常记录诊断，不伪装成“未配置”。"""
        self._auth_read_error = None
        if self.get_current_key is not None:
            try:
                return self.get_current_key()
            except Exception as exc:
                self._auth_read_error = str(exc) or '认证文件读取失败'
                return None
        try:
            from auth_store import AuthStore
            store = AuthStore()
            key = store.current_key()
            self._auth_read_error = store.last_error
            return key
        except Exception as exc:
            self._auth_read_error = str(exc) or '认证文件读取失败'
            return None

    def _refresh_accounts(self) -> None:
        for w in self._accounts_frame.winfo_children():
            w.destroy()
        c = self.colors
        bg, fg, sub = c['bg'], c['fg'], c['sub']
        current_key = self._current_key()
        if self._auth_read_error:
            tk.Label(self._accounts_frame, text=self._auth_read_error,
                     font=(self.FONT, 9), bg=bg, fg=c['warn']).pack(
                anchor='w', pady=(0, 4))

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

            rename_btn = self._make_button(
                row, '改名', lambda k=key: self._rename_account(k), padx=10)
            rename_btn.pack(side=tk.RIGHT)
            del_btn = self._make_button(
                row, '删除', lambda k=key: self._delete_account(k),
                style='danger', padx=10)
            del_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _mask(self, key: str) -> str:
        """掩码唯一规则委托 Config.mask_key（不保留本地实现，避免规则漂移）。"""
        return self.config.mask_key(key)

    # ============ 新增 / 改名 / 删除 ============

    def _paste_key(self) -> None:
        text = ''
        try:
            raw = self.root.clipboard_get().strip()
            text = raw.splitlines()[0].strip() if raw else ''
        except Exception:
            text = ''
        if not text:
            messagebox.showwarning('提示', '剪贴板中没有可用的文本', parent=self.window)
            return
        self._key_entry.delete(0, 'end')
        self._key_entry.insert(0, text)

    def _add_account(self) -> None:
        label = self._label_entry.get().strip()
        key = self._key_entry.get().strip()
        if not key:
            messagebox.showwarning('提示', '请输入 API Key', parent=self.window)
            return
        if not (key.startswith('sk-') or key.startswith('oc_sk_')):
            messagebox.showwarning('提示', '密钥应以 sk- 或 oc_sk_ 开头', parent=self.window)
            return
        # 空标签交给 Config 统一生成默认名（唯一来源，UI 不自编号）

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
        """新增校验：至少一个窗口有效才接受（接口数据不可用 ≠ 密钥有效）。"""
        return usage_validation_error(usage)

    def _on_add_done(self, label, key, usage, err) -> None:
        if not self.window or not self.window.winfo_exists():
            return
        self._add_btn.configure(state='normal', text='添加并校验')
        if err:
            # 校验失败：红字内联提示，保留输入可重试
            self._inline_feedback(f'校验失败：{err}', ok=False)
            return
        try:
            saved = self.config.upsert_account(label, key)  # R3-04：方法内已自落盘
        except Exception as exc:
            self._inline_feedback(f'保存失败：{exc}', ok=False)
            return
        self._notify_saved()
        self._refresh_accounts()
        self._key_entry.delete(0, 'end')
        self._label_entry.delete(0, 'end')
        shown = getattr(saved, 'label', '') or label or '新账号'
        self._inline_feedback(f'已添加「{shown}」并通过密钥校验')

    def _rename_account(self, key: str) -> None:
        account = self._find_account(key)
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
        entry = self._make_entry(frame)
        entry.insert(0, old_label)
        entry.pack(fill=tk.X, pady=(8, 0))
        entry.focus_set()
        entry.select_range(0, 'end')

        def confirm():
            self._apply_rename(key, entry.get(), dialog)

        btn_frame = tk.Frame(frame, bg=self.colors['bg'])
        btn_frame.pack(fill=tk.X, pady=(12, 0))
        self._make_button(btn_frame, '确定', confirm, style='accent',
                          font_size=10, padx=16).pack(side=tk.RIGHT)
        self._make_button(btn_frame, '取消', dialog.destroy,
                          font_size=10, padx=16).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind('<Escape>', lambda e: dialog.destroy())
        dialog.bind('<Return>', lambda e: confirm())

    def _apply_rename(self, key: str, new_label: str, dialog=None) -> bool:
        """改名确认：统一走 _notify_saved 通知入口（成功恰一次）。"""
        new_label = (new_label or '').strip()
        if not new_label:
            messagebox.showwarning('提示', '标签不能为空',
                                   parent=dialog if dialog is not None else self.window)
            return False
        try:
            self.config.upsert_account(new_label, key)  # R3-04：已自落盘
        except Exception as exc:
            self._inline_feedback(f'保存失败：{exc}', ok=False)
            return False
        self._notify_saved()
        if dialog is not None:
            dialog.destroy()
        self._refresh_accounts()
        self._inline_feedback(f'已改名为「{new_label}」')
        return True

    def _delete_account(self, key: str) -> None:
        account = self._find_account(key)
        if account is None:
            return
        label = getattr(account, 'label', '') or '未命名'
        current_key = self._current_key()
        if self._auth_read_error:
            messagebox.showwarning(
                '无法确认当前账号',
                f'认证文件读取失败，暂不能删除：\n{self._auth_read_error}',
                parent=self.window)
            return
        if key == current_key:
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
        self._notify_saved()
        self._refresh_accounts()
        self._inline_feedback(f'已删除「{label}」')

    # ============ 保存 / 取消 ============

    def _read_option(self, widget, conv, lo, hi, message: str):
        """读取并校验单个选项；非法/越界时弹窗提示并返回 None。"""
        try:
            value = conv(widget.get())
            if not lo <= value <= hi:
                raise ValueError
        except (TypeError, ValueError):
            messagebox.showwarning('提示', message, parent=self.window)
            return None
        return value

    def _save(self) -> None:
        interval = self._read_option(self._interval_spin, int, 10, 3600,
                                     '刷新间隔应为 10–3600 的整数')
        if interval is None:
            return
        height = self._read_option(self._height_spin, int, 4, 40,
                                   '热区高度应为 4–40 的整数')
        if height is None:
            return
        ratio = self._read_option(self._ratio_entry, float, 0.1, 1.0,
                                  '热区宽度占比应为 0.1–1.0 的数字')
        if ratio is None:
            return
        offset = self._read_option(self._offset_spin, int, 0, 200,
                                   '热区偏移应为 0–200 的整数')
        if offset is None:
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
        self._notify_saved()
        self._cancel()

    def _cancel(self) -> None:
        if SettingsWindow._active is self:
            SettingsWindow._active = None
        if self.window is not None and self.window.winfo_exists():
            self.window.destroy()
        self.window = None