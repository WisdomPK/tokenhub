"""TokenHub v3 主题化模态弹窗（额度条面板操作对话框）。

供 floating_window.py 使用：
- ConfirmDialog     双按钮确认弹窗（[切换]/[删除] 二次确认；destructive 时确认键为红色）
- AddAccountDialog  新增账号弹窗（备注名可选 + API Key 必填 + 从剪贴板粘贴；
  点 [确认] 后台线程校验密钥——默认真实调用官方用量接口（同设置窗口做法），
  通过 → 回调 on_add(label, key) 并关窗；失败 → 窗内红字显示原因，可重试）

统一约束：transient + grab_set 模态、Esc/右上角关闭 = 取消、窗口小居中于父面板、
topmost（面板本身置顶）。主题全部取自 theme.py 的 colors 字典。
本模块不 import api（默认校验器内延迟导入），preview/测试可注入假校验器。
"""

import threading
import tkinter as tk
from typing import Callable, Optional

from logger import log_exception
from window_tracker import get_work_area  # 弹窗锚定：面板所在显示器工作区（多屏不跨屏）

FONT = 'Microsoft YaHei UI'


def dialog_position(anchor, dlg_w: int, dlg_h: int, work) -> tuple:
    """弹窗位置纯函数（可单测）：居中于 anchor 下方 6px；下方放不下翻上方；
    仍放不下 → 垂直夹取 work 内（允许覆叠面板）；最后夹取 work。

    anchor: (x, y, w, h)；work: (left, top, right, bottom) 或 window_tracker.Rect
    或 None（None 时仅保证坐标 ≥0）。全部同一坐标空间（物理像素）。
    """
    ax, ay, aw, ah = anchor
    x = ax + (aw - dlg_w) // 2
    y = ay + ah + 6  # 面板下方
    if work is not None:
        if hasattr(work, 'left'):  # window_tracker.Rect（dataclass，不可迭代）
            wl, wt, wr, wb = work.left, work.top, work.right, work.bottom
        else:
            wl, wt, wr, wb = work
        if y + dlg_h > wb - 8:      # 下方余量不足 → 翻到面板上方
            y = ay - dlg_h - 6
        if y < wt or y + dlg_h > wb:  # 上方也放不下 → 垂直夹取（允许覆叠）
            lo, hi = wt, wb - dlg_h
            y = lo if hi < lo else min(max(y, lo), hi)
        x = min(max(x, wl), wr - dlg_w)
        if wr - dlg_w < wl:          # 弹窗比工作区宽 → 左对齐
            x = wl
    else:
        x = max(x, 0)
        y = max(y, 0)
    return (x, y)


def _default_validator(key: str) -> Optional[str]:
    """真实校验：用该密钥调用一次官方用量接口；返回错误文案或 None（有效）。

    与 settings_window 的做法一致：失败原因在 UsageMetric.error 中
    （如「密钥无效（401）」「网络错误」）。
    """
    from api import ApiClient  # 延迟导入：preview 注入假校验器时无需网络依赖

    usage = ApiClient().fetch_usage(key)
    for metric_key in ('rolling', 'weekly', 'monthly'):
        metric = getattr(usage, metric_key, None)
        if metric is not None:
            err = getattr(metric, 'error', None)
            if err:
                return err
            if getattr(metric, 'percent', None) is None:
                return f'{metric_key}: 数据不完整'
    return None


class BaseDialog:
    """主题化模态弹窗基类：构建窗口 + 小居中于父面板 + Esc/关闭 = 取消。"""

    def __init__(self, parent: tk.Tk, colors: dict, title: str,
                 width: int, height: int):
        self.parent = parent
        self.c = colors
        self.result: Optional[bool] = None
        self._closed = False
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.resizable(False, False)
        self.window.configure(bg=colors['bg'])
        self.window.transient(parent)
        self.window.attributes('-topmost', True)
        self._center_on(width, height)
        self.window.bind('<Escape>', lambda e: self._close(None))
        self.window.protocol('WM_DELETE_WINDOW', lambda: self._close(None))

    # ---- 定位 ----

    def _center_on(self, w: int, h: int) -> None:
        """窗口小居中于父面板（以父窗口几何中心为基准，夹取在屏幕内）。

        仅用于 anchor=None 的旧路径；面板弹窗一律走 show(anchor=...)
        锚定定位（_place_near），避免多显示器下回退主屏居中导致跨屏。
        """
        parent = self.parent
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except Exception:
            px, py, pw, ph = 0, 0, 800, 600
        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        x = max(0, min(px + (pw - w) // 2, sw - w - 8))
        y = max(0, min(py + (ph - h) // 2, sh - h - 8))
        self.window.geometry(f'{w}x{h}+{x}+{y}')

    def _place_near(self, anchor: tuple) -> None:
        """锚定定位：更新布局取实际尺寸 → 面板所在显示器工作区 →
        dialog_position 计算 → 设置几何。多显示器下绝不回退主屏居中。"""
        try:
            self.window.update_idletasks()
            dlg_w = max(self.window.winfo_reqwidth(), self.window.winfo_width())
            dlg_h = max(self.window.winfo_reqheight(), self.window.winfo_height())
            work = get_work_area(anchor[0] + anchor[2] // 2,
                                 anchor[1] + anchor[3] // 2)
            x, y = dialog_position(anchor, dlg_w, dlg_h, work)
            self.window.geometry(f'+{x}+{y}')
        except Exception as exc:
            # 锚定失败记录诊断，保持 _center_on 已放置的位置（不跨屏保证失效时兜底）
            log_exception('弹窗锚定 _place_near', exc)

    # ---- 模态 ----

    def show(self, anchor=None):
        """grab_set 模态显示；窗口关闭后返回 self.result。

        anchor=(x, y, w, h) 时弹窗锚定 anchor（居中于其下方、随所在显示器
        工作区夹取，不跨屏）；anchor=None 保持旧居中行为（_center_on）。
        """
        self.window.grab_set()
        self.window.focus_force()
        self._on_shown()
        if anchor is not None:
            self._place_near(anchor)
        self.window.wait_window()
        return self.result

    def _on_shown(self) -> None:
        """子类钩子：弹窗显示后置焦点等。"""

    def _close(self, result: Optional[bool] = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = result
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass

    # ---- 主题化控件 ----

    def _make_button(self, parent, text, command, *,
                     accent=False, danger=False, padx=18, pady=5) -> tk.Button:
        c = self.c
        bg, fg, active_bg, active_fg = self._button_colors(accent, danger)
        btn = tk.Button(parent, text=text, font=(FONT, 10),
                        bd=0, relief='flat', highlightthickness=0,
                        cursor='hand2', bg=bg, fg=fg,
                        activebackground=active_bg, activeforeground=active_fg,
                        padx=padx, pady=pady, command=command)
        if danger:
            # 删除按钮 hover 危险色（红）
            btn.bind('<Enter>', lambda e: btn.configure(bg=c['warn'], fg='#ffffff'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=c['card'], fg=c['warn']))
        return btn

    def _button_colors(self, accent: bool, danger: bool) -> tuple:
        """按钮四色（bg / fg / activebackground / activeforeground：
        danger=危险红、accent=主色、其余=卡片色）。"""
        c = self.c
        if danger:
            return c['card'], c['warn'], c['warn'], '#ffffff'
        if accent:
            return c['accent'], c['bg'], c['accent'], c['bg']
        return c['card'], c['fg'], c['card'], c['sub']

    def _make_entry(self, parent, show=None) -> tk.Entry:
        c = self.c
        return tk.Entry(parent, font=(FONT, 10), bg=c['input_bg'], fg=c['fg'],
                        insertbackground=c['fg'], relief='flat',
                        highlightthickness=1, highlightbackground=c['border'],
                        highlightcolor=c['accent'], show=show)


class ConfirmDialog(BaseDialog):
    """双按钮确认弹窗：[确认]/[取消]；destructive 时确认键为红色（删除）。"""

    W, H = 380, 150

    def __init__(self, parent, colors, title, message,
                 confirm_text='确认', cancel_text='取消', destructive=False):
        super().__init__(parent, colors, title, self.W, self.H)
        c = colors
        frame = tk.Frame(self.window, bg=c['bg'], padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text=message, font=(FONT, 10), bg=c['bg'], fg=c['fg'],
                 justify='left', anchor='w', wraplength=340).pack(fill=tk.X, expand=True)
        btns = tk.Frame(frame, bg=c['bg'])
        btns.pack(fill=tk.X, pady=(14, 0))
        self.confirm_btn = self._make_button(btns, confirm_text, lambda: self._close(True),
                                             accent=not destructive, danger=destructive)
        self.confirm_btn.pack(side=tk.RIGHT)
        self.cancel_btn = self._make_button(btns, cancel_text, lambda: self._close(False))
        self.cancel_btn.pack(side=tk.RIGHT, padx=(0, 10))
        self.window.bind('<Return>', lambda e: self._close(True))

    def confirm(self) -> None:
        """程序化点确认（preview 演示用）。"""
        self._close(True)


class AddAccountDialog(BaseDialog):
    """新增账号弹窗：备注名（可选）+ API Key（必填，以 sk- 或 oc_sk_ 开头）+ 剪贴板粘贴。

    [确认]：按钮禁用 + 「验证中…」，后台线程校验（默认真实调用官方接口；
    可注入 validator(key) -> 错误文案 | None）。通过 → 调 on_add(label, key)
    并【依据其返回回执决定关窗】：(True, '') 或 None（旧回调兼容）= 成功关窗；
    (False, msg) 或抛异常 = 失败，弹窗内红字显示 msg、保留输入、恢复按钮可重试。
    U08：密钥输入默认 ● 掩码 + 「显示/隐藏」切换；粘贴后提示「已粘贴」。
    """

    W, H = 400, 250

    def __init__(self, parent, colors,
                 on_add: Callable[[str, str], None],
                 validator: Optional[Callable[[str], Optional[str]]] = None):
        super().__init__(parent, colors, '新增账号', self.W, self.H)
        self.on_add = on_add
        self.validator = validator if validator is not None else _default_validator
        self._busy = False
        c = colors
        main = tk.Frame(self.window, bg=c['bg'], padx=20, pady=18)
        main.pack(fill=tk.BOTH, expand=True)

        tk.Label(main, text='备注名（可选，留空自动命名）', font=(FONT, 9),
                 bg=c['bg'], fg=c['sub']).pack(anchor='w')
        self.label_entry = self._make_entry(main)
        self.label_entry.pack(fill=tk.X, pady=(4, 10))

        tk.Label(main, text='API Key（必填，以 sk- 或 oc_sk_ 开头）', font=(FONT, 9),
                 bg=c['bg'], fg=c['sub']).pack(anchor='w')
        self.key_entry = self._make_entry(main, show='●')  # U08：默认掩码
        self.key_entry.pack(fill=tk.X, pady=(4, 0))

        btns = tk.Frame(main, bg=c['bg'])
        btns.pack(fill=tk.X, pady=(10, 0))
        self.paste_btn = self._make_button(btns, '从剪贴板粘贴', self._paste,
                                           padx=10, pady=4)
        self.paste_btn.pack(side=tk.LEFT)
        self._key_visible = False
        self.toggle_key_btn = self._make_button(btns, '显示', self._toggle_key,
                                                padx=10, pady=4)
        self.toggle_key_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.confirm_btn = self._make_button(btns, '确认', self._start_validate,
                                             accent=True, padx=22, pady=4)
        self.confirm_btn.pack(side=tk.RIGHT)

        self.error_label = tk.Label(main, text='', font=(FONT, 9), bg=c['bg'],
                                    fg=c['warn'], anchor='w', justify='left',
                                    wraplength=360)
        self.error_label.pack(fill=tk.X, pady=(8, 0))

        self.key_entry.focus_set()
        self.window.bind('<Return>', lambda e: self._start_validate())

    def _on_shown(self) -> None:
        self.key_entry.focus_set()

    # ---- 输入 ----

    def set_label(self, text: str) -> None:
        """程序化填备注名（preview 演示用）。"""
        self.label_entry.delete(0, 'end')
        self.label_entry.insert(0, text)

    def set_key(self, text: str) -> None:
        """程序化填 API Key（preview 演示用）。"""
        self.key_entry.delete(0, 'end')
        self.key_entry.insert(0, text)

    def _toggle_key(self) -> None:
        """U08：显示/隐藏密钥输入（切换不改内容）。"""
        self._key_visible = not self._key_visible
        self.key_entry.configure(show='' if self._key_visible else '●')
        self.toggle_key_btn.configure(text='隐藏' if self._key_visible else '显示')

    def _set_error(self, text: str) -> None:
        self.error_label.configure(text=text, fg=self.c['warn'])

    def _paste(self) -> None:
        text = ''
        try:
            raw = self.parent.clipboard_get().strip()
            text = raw.splitlines()[0].strip() if raw else ''
        except Exception:
            text = ''
        if not text:
            self._set_error('剪贴板中没有可用的文本')
            return
        self.key_entry.delete(0, 'end')
        self.key_entry.insert(0, text)
        # U08：只提示「已粘贴」，不把完整密钥回显到提示
        self.error_label.configure(text='已粘贴', fg=self.c['sub'])

    # ---- 校验 ----

    def confirm(self) -> None:
        """程序化点确认（preview 演示用）。"""
        self._start_validate()

    def _start_validate(self) -> None:
        if self._busy:
            return
        label = self.label_entry.get().strip()
        key = self.key_entry.get().strip()
        if not key:
            self._set_error('请输入 API Key')
            self.key_entry.focus_set()
            return
        if not (key.startswith('sk-') or key.startswith('oc_sk_')):
            self._set_error('密钥应以 sk- 或 oc_sk_ 开头')
            self.key_entry.focus_set()
            return
        self._set_error('')
        self._busy = True
        self.confirm_btn.configure(state='disabled', text='验证中…')
        self.paste_btn.configure(state='disabled')

        def work():
            try:
                err = self.validator(key)
            except Exception as exc:  # 防御：校验器不应抛异常
                err = str(exc) or '校验失败'
            try:
                self.window.after(0, lambda: self._on_done(label, key, err))
            except Exception:
                pass  # 校验期间弹窗已被关闭

        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, label: str, key: str, err: Optional[str]) -> None:
        if self._closed or not self.window.winfo_exists():
            return
        self._busy = False
        self.confirm_btn.configure(state='normal', text='确认')
        self.paste_btn.configure(state='normal')
        if err:
            self._set_error(err)
            return
        # 校验通过 → 调 on_add 并取回执：仅成功才关闭（R3-03）
        # 成功 = 返回 (True, _) 或 None（旧回调兼容）；失败/异常保留输入可重试
        try:
            result = self.on_add(label, key)
        except Exception as exc:
            self._set_error(str(exc) or '保存失败，请重试')
            return
        ok, msg = (True, '') if result is None else result
        if ok:
            self._close(True)
        else:
            self._set_error(msg or '保存失败，请重试')