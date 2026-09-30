"""TokenHub v3 额度条主面板（FloatingWindow）。

无边框、置顶、不抢焦点（WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW）的悬浮面板：
悬停 opencode 窗口顶部热区时由 main.py 驱动下滑展开 / 上滑收起。

布局（v3 改版 + 用户新增操作）：每个账号一行，行栅格 =
    [账户区: ●/○ + 名称 +（使用中 chip）+ 掩码] | [5小时块] | [周块] | [月块] | [尾部操作列]
块内两行居中排布（上行：标签 → 百分比；下行：⟳ → 倒计时，⟳ 在倒计时之前；
标签/倒计时/⟳ 用 9pt 次要色（与按钮同级），百分比 11pt bold 阈值色；
≥85% 仅警告红、无 ⚠），块间 1px 细竖分隔线；
U01：指标标签为「5小时已用/周已用/月已用」，当前账号加「使用中」chip；
U03：单项指标错误时该块上行显示短文案（密钥失效/请求过频/连接失败/
数据不可用），其余指标照常；整行无任何有效指标才整体置灰「离线」。
尾部操作列（备用行两按钮驱动，~104–120px）：当前行=[新增]，备用行=[切换][删除]
（当前账号正被 opencode 使用，不提供删除）；[切换]/[删除] 点击先弹主题化确认
弹窗（二次确认）；[新增] 弹窗填备注名（可选）+ API Key（必填，以 sk- 或
oc_sk_ 开头），确认时后台线程真实调用官方接口校验，通过后回调
on_add_account 由 main 落盘（R3-03：仅返回成功才关闭弹窗，失败保留输入）。
底部无常驻状态行；消息行为临时行——set_toast 时出现（先加高度再绘制），
非 sticky 消息 2.5s 后淡出并整行隐去、高度回落；sticky 消息不自动淡出，
保持至被替换或 clear_transient()，期间临时消息结束后自动恢复显示；
点击消息行复制完整文本并触发 on_status_click（U02）。
宽度 ~620–780px（内容驱动）；行高 54px；空闲单账号总高 ~58–60px
（消息行期间 +22px）。

实现要点：指标块与状态行为 Frame + Label 控件渲染（非 Canvas 文本）——
Canvas.create_text 在 Windows 上走不同 GDI 路径，同字体同字号下发虚
（用户 2x 实拍对比确认），Label/Button 控件渲染锐利。
背景策略：窗口与全部内部容器/控件统一 c['bg'] 实色（无透明键、无亚克力）——
矩形内容与键控圆角互斥（矩形必然覆盖圆弧外区域：实色→方角、键控→透出
桌面形成色带）；圆角由 CreateRoundRectRgn 窗口级裁切（_apply_region），
画布圆角多边形仅作描边；DWM 圆角偏好作增强。
指标块每块两行居中：上行 y=15（标签 9pt + 百分比 11pt bold）、下行 y=41
（⟳ + 倒计时 9pt），place relx=0.5 水平居中；块 Frame 显式 width =
两行文本实测宽取较大者 + 2×BLOCK_PADX（12px/侧，Font.measure 与 Label
同字体同源；grid 内容驱动，请求宽超 620 时面板自动放宽、上限 780，
padx 不压缩，文本与分隔线间距 ≥12px）。
全部文字统一使用 Microsoft YaHei UI（实测覆盖 ⟳/⚠/●/○/… 全部所需字形）。
字号体系（点单位，与 [新增] 按钮 9pt 同级）：指标块标签/倒计时/⟳ 9pt、
百分比 11pt bold、状态行 8pt、账号掩码 9pt——面板内无 <8pt 文字。

本模块只负责：绘制（实色圆角背景/描边）、180ms 滑动动画、数据渲染、
Toast 反馈、右键菜单与交互反馈。位置计算与悬停状态机由 main.py 统一驱动，
本模块不 import window_tracker（保持单向依赖）。
"""

import ctypes
import datetime
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional

from config import Config  # 掩码唯一规则（config 不 import UI，无循环依赖）
from dialogs import AddAccountDialog, ConfirmDialog
from logger import log_exception  # tick 块级失败诊断（§7.2）

# ---- Win32 常量 ----
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


def _set_noactivate(hwnd: int) -> None:
    """WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW：不抢焦点、不占任务栏。"""
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                            SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception:
        pass


def _apply_dwm_corners(hwnd: int) -> None:
    """DWM 圆角偏好（Win11；overrideredirect 下可能不生效，仅作增强）。"""
    try:
        val = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def _apply_region(hwnd: int, w: int, h: int, radius: int) -> None:
    """圆角：CreateRoundRectRgn 窗口级裁切（实色窗口的主圆角路径）。"""
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def _rounded_rect_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list:
    """圆角矩形路径点（配合 canvas smooth 多边形绘制）。"""
    r = max(0.0, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


class FloatingWindow:
    """额度条悬浮面板。"""

    SLIDE_MS = 180          # 滑动动画时长
    FRAME_MS = 16           # 动画帧间隔
    TOAST_MS = 2500         # Toast 停留时长
    TOAST_FADE_MS = 350     # Toast 淡出时长
    CORNER_RADIUS = 12      # 面板圆角
    INSET = 2               # 内容区相对窗口边缘的内缩（圆角区域）

    FONT = 'Microsoft YaHei UI'  # 实测覆盖 ⟳/⚠/●/○/… 全部所需字形

    ROW_HEIGHT = 54         # 行高（9pt/11pt 两行字形盒 45px + 上下各 3px + 间隙 3px，167% DPI 实测）
    STATUS_HEIGHT = 20      # 状态行高度（8pt 字形盒 19px，含视觉余量）
    METRIC_LABELS = (('rolling', '5小时已用'), ('weekly', '周已用'), ('monthly', '月已用'))

    MIN_WIDTH = 620         # 行式布局面板宽度范围（尾部操作列加宽后上限放宽）
    MAX_WIDTH = 780
    BLOCK_PADX = 12         # 指标块左右内边距（每侧；文本居中，实际间距 ≥ 该值）

    def __init__(self, root: tk.Tk, theme_colors: dict,
                 on_switch: Callable, on_settings: Callable,
                 on_help: Callable, on_exit: Callable,
                 on_refresh: Optional[Callable] = None,
                 on_add_account: Optional[Callable[[str, str], tuple]] = None,
                 on_delete_account: Optional[Callable[[str], tuple]] = None,
                 on_countdown_due: Optional[Callable[[str], None]] = None,
                 on_status_click: Optional[Callable[[], None]] = None):
        """冻结契约（新回调追加在 on_refresh 之后，均可选、有默认值）：
        on_add_account(label, key) -> (ok, msg) 新增校验通过后回调，main 负责落盘；
            返回 (True, '') 成功 / (False, msg) 失败（弹窗保留输入可重试）；
        on_delete_account(key) -> (ok, msg) 删除确认后回调，main 负责删除；
            失败 (False, msg) 由本窗口以 error Toast 呈现；
        on_countdown_due(key)  —— 某指标倒计时归零后回调（main 去抖并触发强制刷新）；
        on_status_click()      —— 状态行（Toast/sticky）点击回调；UI 侧同时把
            完整消息文本复制到剪贴板（单行显示截断时仍可获取全文）。
        """
        self.root = root
        self.c = theme_colors
        self.on_switch = on_switch
        self.on_settings = on_settings
        self.on_help = on_help
        self.on_exit = on_exit
        self.on_refresh = on_refresh
        self.on_add_account = on_add_account
        self.on_delete_account = on_delete_account
        self.on_countdown_due = on_countdown_due
        self.on_status_click = on_status_click

        # 布局尺寸（行式布局：620–700 宽，高度随行数）
        self._layout_w = self.MIN_WIDTH
        self._layout_h = 100

        # 位置与可见状态（由 main.py 经 update_position/set_visible 驱动）
        self._x = 0
        self._y = -self._layout_h
        self._yv = 0
        self._yh = -self._layout_h
        self._visible_target = False
        self._withdrawn = False
        self._slide_job: Optional[str] = None

        # 数据
        self._data = (None, None, [])
        self._switching_key: Optional[str] = None
        self._active_dialog: Optional[object] = None  # 活动模态弹窗（preview 驱动用）
        self._add_validator: Optional[Callable[[str], Optional[str]]] = None  # 可注入假校验器
        self._toast_text = ''
        self._toast_kind = 'info'
        self._toast_expire_at = 0.0
        self._toast_fade_job: Optional[str] = None
        self._hint_text = ''
        self._hint_color = ''
        # U02：sticky 消息（不自动淡出，保持至被替换或 clear_transient）；
        # 非 sticky 消息期间 sticky 挂起，消息结束后恢复显示
        self._sticky_text = ''
        self._sticky_kind = 'info'
        self._transient_active = False
        self._status_full_text = ''   # 状态行完整文本（截断前，点击复制用）
        # follow_up 序列：第一段淡出后跟随的 (text, duration_ms)；_follow_up_job
        # 为 follow_up 段的显示/淡出定时器（替换/clear/destroy 时取消）
        self._follow_up = None
        self._follow_up_job: Optional[str] = None

        # TH-08：本地 1s ticker 状态（只刷新倒计时/年龄文本，不重建账号行）
        self._metric_blocks: list = []   # 每指标块: {update, key, metric, offline, usage}
        self._cd_due: dict = {}          # (key, metric) → 当前是否已处于归零态
        self._ticker_job: Optional[str] = None

        # 窗口：全实色 c['bg']（无透明键/亚克力——矩形内容与键控圆角互斥，
        # 色带修复采用实色 + SetWindowRgn 圆角，见 _apply_region）
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        self._trans = self.c['bg']
        self.window.configure(bg=self._trans)
        self.window.geometry(f'{self._layout_w}x{self._layout_h}+0+0')

        # 面板画布（绘制圆角背景 + 承载内容）
        self._canvas = tk.Canvas(
            self.window, bg=self._trans, highlightthickness=0, bd=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._content = tk.Frame(self._canvas, bg=self._trans)
        # 关键：必须显式指定画布窗口项宽度，否则内容帧只按请求宽度布局（右侧空白、指标块被裁）
        self._content_item = self._canvas.create_window(
            self.INSET, self.INSET, anchor='nw', window=self._content,
            width=self._layout_w - 2 * self.INSET)

        # 内容区（行容器 + 状态行）
        # 指标块字体（点单位，与按钮 9pt 同级；Font.measure 用于段宽定位，
        # 绘制与测量同源；磅值随 DPI 感知缩放，正负号切换后与按钮渲染一致）
        self._f_sec = tkfont.Font(family=self.FONT, size=9)               # 标签/倒计时/⟳ 9pt
        self._f_pct = tkfont.Font(family=self.FONT, size=11, weight='bold')  # 百分比 11pt
        self._build_widgets()
        self._render_all()
        self._relayout()

        # TH-08：本地 1s ticker —— 只更新倒计时文本与「N 秒/分钟前更新」年龄文案
        self._ticker_job = self.window.after(1000, self._tick_second)

        # 交互
        self.window.bind('<Button-3>', self._show_menu)
        self._build_menu()

        # Win32 效果（等窗口创建后）
        self.window.after(30, self._setup_win32)

    # ============ Win32 ============

    def _get_hwnd(self) -> int:
        return int(self.window.winfo_id())

    def _setup_win32(self) -> None:
        try:
            hwnd = self._get_hwnd()
            _set_noactivate(hwnd)
            _apply_dwm_corners(hwnd)
            _apply_region(hwnd, self._layout_w, self._layout_h, self.CORNER_RADIUS)
        except Exception:
            pass

    # ============ 构建 / 渲染 ============

    def _build_widgets(self) -> None:
        """行容器（所有账号行共用同一列网格）+ 底部状态行。"""
        # 行容器：列 = [账户区|竖线|5h块|竖线|周块|竖线|月块|竖线|尾部操作列]
        self._rows = tk.Frame(self._content, bg=self._trans)
        self._rows.pack(fill=tk.X)
        self._rows.grid_columnconfigure(0, weight=0, minsize=128)
        for col in (1, 3, 5, 7):
            self._rows.grid_columnconfigure(col, weight=0, minsize=1)
        # 三个指标块：weight 均分弹性，填满整行（无右侧空白）
        for col in (2, 4, 6):
            self._rows.grid_columnconfigure(col, weight=1)
        # 尾部操作列：备用行 [切换][删除] 两枚按钮驱动列宽（~104–120px）；
        # 当前行仅 [新增]，不再为其预留双按钮空间（「切换中…」临时加宽由 grid 让出）
        self._rows.grid_columnconfigure(8, weight=0, minsize=104)

        # 临时 Toast 行：Frame + 单 Label（控件渲染，走锐利 GDI 路径）；
        # 初始不 pack（不占高度）——仅 set_toast 时 pack 显示，淡出完成后
        # pack_forget 隐去、面板高度回落；数据年龄显示已移除
        self._status = tk.Frame(self._content, bg=self._trans,
                                height=self.STATUS_HEIGHT)
        self._status.pack_propagate(False)  # 防 pack 传播：保持 20px 显式高度
        self._status_shown = False
        self._status_label = tk.Label(self._status, text='', font=(self.FONT, 8),
                                      bg=self.c['bg'], fg=self.c['sub'],
                                      bd=0, highlightthickness=0, padx=0, pady=0)
        self._status_label.pack(fill=tk.X)  # 文本水平居中
        # U02：点击状态行 → 复制完整消息 + on_status_click 回调
        self._status_label.bind('<Button-1>', self._on_status_click)

        self._switch_buttons: dict[str, tk.Button] = {}
        self._row_buttons: list = []

    def _build_menu(self) -> None:
        menu = tk.Menu(self.window, tearoff=0,
                       bg=self.c['menu_bg'], fg=self.c['menu_fg'],
                       activebackground=self.c['accent'],
                       activeforeground=self.c['bg'],
                       bd=0)
        menu.add_command(label='设置', command=self.on_settings)
        menu.add_command(label='帮助', command=self.on_help)
        if self.on_refresh:
            menu.add_command(label='立即刷新', command=self.on_refresh)
        menu.add_separator()
        menu.add_command(label='退出', command=self.on_exit)
        self._menu = menu

    def _show_menu(self, event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _render_all(self) -> None:
        self._render_rows()
        self._reapply_toast()

    def _render_rows(self) -> None:
        """重建全部账号行（当前 + 备用）。"""
        for w in self._rows.winfo_children():
            w.destroy()
        self._switch_buttons = {}
        self._row_buttons = []
        self._metric_blocks = []   # TH-08：ticker 依赖的指标块引用随重建重置
        self._cd_due = {}          # 归零上报状态一并重置（新数据到来后重新计数）
        current, current_usage, backups = self._data

        row_idx = 0
        if current is not None:
            self._build_row(row_idx, current, current_usage, is_current=True)
        else:
            self._build_row(row_idx, None, None, is_current=True)
        row_idx += 1
        for account, usage in (backups or []):
            self._build_row(row_idx, account, usage, is_current=False)
            row_idx += 1
        self._apply_switching(self._switching_key)

    def _build_row(self, row_idx: int, account, usage, is_current: bool) -> None:
        """单行：账户区 + 竖线 + 三个指标块 + 尾部。行高 = ROW_HEIGHT。"""
        key = getattr(account, 'key', '') if account is not None else ''
        label = getattr(account, 'label', '') if account is not None else ''
        if not label:
            label = '未检测到当前账号' if is_current else '未命名'
        offline = usage is None or self._usage_offline(usage)

        # ---- 账户区（Label，单行，垂直居中） ----
        ident = tk.Frame(self._rows, bg=self._trans)
        ident.grid(row=row_idx, column=0, sticky='nsew', padx=(12, 8))
        inner = tk.Frame(ident, bg=self._trans)
        inner.pack(expand=True)
        tk.Label(inner, text='●' if is_current else '○',
                 font=(self.FONT, 10, 'bold'), bg=self._trans,
                 fg=self.c['accent'] if is_current else self.c['sub']).pack(side=tk.LEFT)
        tk.Label(inner, text=label, font=(self.FONT, 10, 'bold'), bg=self._trans,
                 fg=self.c['offline'] if offline else self.c['fg']).pack(side=tk.LEFT, padx=(4, 0))
        if is_current:
            # U01：当前账号「使用中」chip（accent 弱底小标签）
            tk.Label(inner, text='使用中', font=(self.FONT, 8), bg=self.c['card'],
                     fg=self.c['accent'], padx=4, pady=0,
                     bd=0, highlightthickness=0).pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(inner, text=self._mask(key), font=(self.FONT, 9), bg=self._trans,
                 fg=self.c['offline']).pack(side=tk.LEFT, padx=(6, 0))

        # ---- 块间 1px 细竖分隔线 ----
        for col in (1, 3, 5, 7):
            tk.Frame(self._rows, bg=self.c['separator'], width=1).grid(
                row=row_idx, column=col, sticky='ns')

        # ---- 三个指标块（Frame + 两行 Label 控件渲染，弹性均分填满） ----
        # 渲染路径说明：Canvas.create_text 在 Windows 上走不同 GDI 路径，
        # 同字体同字号下明显发虚（用户 2x 实拍对比确认）；Label 控件渲染
        # 锐利，与 [新增] 按钮同路径。面板内部背景统一 c['bg'] 实色
        # （无透明键），圆角由 SetWindowRgn 窗口级裁切（见 _apply_region）。
        # 块 Frame 显式 width = 两行文本实测宽取较大者 + 4px（Font.measure
        # 与 Label 同字体同源，保留内容驱动：grid 列自然宽 → 面板 620–780 自适应）
        for j, (metric_key, label_text) in enumerate(self.METRIC_LABELS):
            m = self._metric(usage, metric_key)
            percent = getattr(m, 'percent', None) if m is not None else None
            err = getattr(m, 'error', None) if m is not None else None
            if offline:
                # 整行离线（无任何有效指标）：上行=标签、下行=「离线」（无 ⟳）
                pct_text = ''
                cd_text = '离线'
                icon = False
                err_state = False
            elif err:
                # U03：单项指标错误 → 上行=标签+状态短文案，下行=⟳ 倒计时
                # （其他指标照常显示；文案映射见 _metric_error_text）
                pct_text = self._metric_error_text(err)
                cd_text = self._format_countdown(getattr(m, 'resets_at', None))
                icon = True
                err_state = True
            else:
                pct_text = f'{percent}%' if percent is not None else '--'
                cd_text = self._format_countdown(getattr(m, 'resets_at', None))
                icon = True
                err_state = False

            # 两行各自测宽 → 块 Frame 请求宽 = 两行较大者 + 2×BLOCK_PADX（gap=4 段间距）；
            # 左右内边距保证文本与块边界/分隔线的间距（请求宽增大后 _relayout
            # 的 req_w 超 620 → 面板自动放宽、grid 不压缩，padx 完整生效；
            # 780 下居中余量更大，实际间距 ≥ BLOCK_PADX）
            gap = 4
            lw = self._f_sec.measure(label_text)
            # 状态短文案用 9pt（与标签同字号）测量与绘制；百分比用 11pt bold
            pw = (self._f_sec.measure(pct_text) if err_state
                  else self._f_pct.measure(pct_text)) if pct_text else 0
            iw = self._f_sec.measure('⟳') if icon else 0
            cw = self._f_sec.measure(cd_text)
            w_top = lw + (gap + pw if pw else 0)
            w_bot = iw + (gap + cw if icon else 0) if icon else cw
            total = max(w_top, w_bot)

            block = tk.Frame(self._rows, bg=self.c['bg'],
                             width=int(total) + 2 * self.BLOCK_PADX,
                             height=self.ROW_HEIGHT)
            block.grid(row=row_idx, column=2 + j * 2, sticky='nsew')

            # 行高 54px：两行中心 y=15 / y=41（11pt bold 字形盒上探12/下探13、
            # 9pt 上探10/下探10，167% DPI 实测 → 字形盒 3..28 / 31..51，
            # 上边距3px、下边距3px、两行间隙3px）；place 相对块宽水平居中
            bg = self.c['bg']
            top = tk.Frame(block, bg=bg)
            top.place(relx=0.5, y=15, anchor='center')
            lbl_label = tk.Label(top, text=label_text, font=self._f_sec, bg=bg,
                                 fg=self.c['offline'] if offline else self.c['sub'],
                                 bd=0, highlightthickness=0, padx=0, pady=0)
            lbl_label.pack(side=tk.LEFT)
            lbl_pct = None
            if pct_text:
                # 错误状态文案：9pt + offline 可读色；正常百分比：11pt bold 阈值色
                lbl_pct = tk.Label(top, text=pct_text,
                                   font=self._f_sec if err_state else self._f_pct,
                                   bg=bg,
                                   fg=self.c['offline'] if (err_state or percent is None)
                                   else self._pct_color(percent),
                                   bd=0, highlightthickness=0, padx=0, pady=0)
                lbl_pct.pack(side=tk.LEFT, padx=(gap, 0))
            bot = tk.Frame(block, bg=bg)
            bot.place(relx=0.5, y=41, anchor='center')
            lbl_icon = None
            if icon:
                lbl_icon = tk.Label(bot, text='⟳', font=self._f_sec, bg=bg,
                                    fg=self.c['offline'] if offline else self.c['sub'],
                                    bd=0, highlightthickness=0, padx=0, pady=0)
                lbl_icon.pack(side=tk.LEFT)
            lbl_cd = tk.Label(bot, text=cd_text, font=self._f_sec, bg=bg,
                              fg=self.c['offline'] if offline else self.c['sub'],
                              bd=0, highlightthickness=0, padx=0, pady=0)
            lbl_cd.pack(side=tk.LEFT, padx=(gap, 0) if icon else (0, 0))

            # TH-08：update 每次调用重算倒计时文本（ticker 每秒调用），
            # 只 configure(text=...) 更新，不重建账号行；整行离线块结构固定
            def update(_blk=block, _u=usage, _mk=metric_key, _off=offline,
                       _err=err_state, _lp=lbl_pct, _lc=lbl_cd):
                if _off:
                    return
                m = self._metric(_u, _mk)
                if _err:
                    # U03：错误块上行状态文案固定，只更新倒计时（若有）
                    _lc.configure(text=self._format_countdown(
                        getattr(m, 'resets_at', None)))
                    return
                percent = getattr(m, 'percent', None) if m is not None else None
                pct_text = f'{percent}%' if percent is not None else '--'
                cd_text = self._format_countdown(getattr(m, 'resets_at', None))
                _lp.configure(text=pct_text,
                              fg=self.c['offline'] if percent is None
                              else self._pct_color(percent))
                _lc.configure(text=cd_text)

            self._metric_blocks.append({
                'update': update, 'key': key, 'metric': metric_key,
                'offline': offline, 'error': err_state, 'usage': usage})

        # ---- 尾部操作列（垂直居中）：当前行=[新增]；备用行=[切换][删除] ----
        tail = tk.Frame(self._rows, bg=self._trans)
        tail.grid(row=row_idx, column=8, sticky='nsew', padx=(8, 12))
        tinner = tk.Frame(tail, bg=self._trans)
        tinner.pack(expand=True)
        row_btns: dict = {}
        if is_current:
            # 当前账号正被 opencode 使用，不提供删除；仅 [新增]
            add_btn = self._make_tail_button(tinner, '新增', self._open_add_dialog,
                                             fg=self.c['accent'])
            add_btn.pack(side=tk.LEFT)
            row_btns['add'] = add_btn
        else:
            sw_btn = self._make_tail_button(tinner, '切换',
                                            lambda k=key: self._on_switch_click(k),
                                            fg=self.c['accent'])
            sw_btn.pack(side=tk.LEFT)
            row_btns['switch'] = sw_btn
            self._switch_buttons[key] = sw_btn
            del_btn = self._make_tail_button(
                tinner, '删除', lambda l=label: self._on_delete_click(key, l),
                danger=True)
            del_btn.pack(side=tk.LEFT, padx=(8, 0))
            row_btns['delete'] = del_btn
        self._row_buttons.append(row_btns)

    # ============ 尾部操作：新增 / 切换 / 删除（含确认弹窗） ============

    def _make_tail_button(self, parent, text, command,
                          fg=None, danger=False) -> tk.Button:
        """紧凑文字按钮（与既有按钮一致）；danger（删除）hover 危险红。"""
        c = self.c
        btn = tk.Button(parent, text=text, font=(self.FONT, 9),
                        bd=0, relief='flat', highlightthickness=0,
                        cursor='hand2', padx=10, pady=3,
                        bg=c['card'], fg=fg or c['fg'],
                        activebackground=c['card'], activeforeground=fg or c['accent'],
                        command=command)
        if danger:
            btn.configure(fg=c['warn'], activebackground=c['warn'],
                          activeforeground='#ffffff')
            btn.bind('<Enter>', lambda e: btn.configure(bg=c['warn'], fg='#ffffff'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=c['card'], fg=c['warn']))
        return btn

    def _show_dialog(self, dlg) -> None:
        """模态弹窗（transient+grab_set 在 show 内）；记录活动弹窗供预览驱动。
        弹窗锚定面板可见矩形（居中于面板下方、随面板所在显示器工作区夹取，
        多显示器下不跨屏）；show 返回（弹窗关闭）即复位 _active_dialog。"""
        self._active_dialog = dlg
        try:
            dlg.show(anchor=(self._x, int(self._yv),
                             self._layout_w, self._layout_h))
        finally:
            self._active_dialog = None

    def is_modal_open(self) -> bool:
        """当前有存活的活动弹窗（冻结契约：切换等操作前检查）。"""
        dlg = self._active_dialog
        if dlg is None:
            return False
        try:
            return dlg.window is not None and dlg.window.winfo_exists()
        except Exception:
            return False

    def _open_add_dialog(self) -> None:
        if self._active_dialog is not None:
            return  # 已有模态弹窗打开
        dlg = AddAccountDialog(self.window, self.c,
                               on_add=self._emit_add_account,
                               validator=self._add_validator)
        self._show_dialog(dlg)

    def _emit_add_account(self, label: str, key: str):
        """透传 on_add_account 的 (ok, msg) 回执给弹窗（R3-03：不吞掉失败）。"""
        if self.on_add_account is not None:
            return self.on_add_account(label, key)
        return None

    def _on_switch_click(self, key: str) -> None:
        label = self._find_label(key)
        # 生效边界（实测）：opencode 进程内模型客户端缓存（缓存键不含凭据），
        # auth 变更不会使缓存失效 → 必须重启 opencode 才真正生效
        dlg = ConfirmDialog(self.window, self.c, '切换账号',
                            f'确认切换到「{label}」？将更新 opencode 的认证文件，'
                            '重启 opencode 后生效。',
                            confirm_text='确认切换', cancel_text='取消')
        self._show_dialog(dlg)
        if dlg.result and self.on_switch is not None:
            self.on_switch(key)

    def _on_delete_click(self, key: str, label: str) -> None:
        # 仅备用行可删除（当前账号行不渲染 [删除]）；确认后回调 on_delete_account。
        # R3-03：回调返回 (False, msg) 或抛异常 → 保留行并以 error Toast 呈现 msg
        dlg = ConfirmDialog(self.window, self.c, '确认删除',
                            f'确认删除「{label}」？该账号将从本机账号列表中移除。',
                            confirm_text='删除', cancel_text='取消', destructive=True)
        self._show_dialog(dlg)
        if not dlg.result or self.on_delete_account is None:
            return
        try:
            result = self.on_delete_account(key)
        except Exception as exc:
            result = (False, str(exc) or '删除失败')
        ok, msg = (True, '') if result is None else result
        if not ok:
            self.set_toast(msg or '删除失败', 'error')

    def _find_label(self, key: str) -> str:
        current, _cu, backups = self._data
        if current is not None and getattr(current, 'key', '') == key:
            return getattr(current, 'label', '') or '未命名'
        for account, _u in (backups or []):
            if getattr(account, 'key', '') == key:
                return getattr(account, 'label', '') or '未命名'
        return '该账号'

    # ============ Toast（临时行）/ 反馈 ============

    def _show_status_row(self) -> None:
        """Toast 行 pack 显示并让面板高度加入（先加高度再绘制，避免闪跳）。"""
        if not self._status_shown:
            self._status.pack(fill=tk.X, pady=(2, 0))
            self._status_shown = True
            self._relayout()

    def _hide_status_row(self) -> None:
        """Toast 淡出完成：隐去整行、面板高度回落。"""
        if self._status_shown:
            self._status.pack_forget()
            self._status_shown = False
            self._relayout()

    def _draw_status(self) -> None:
        self._status_label.configure(text=self._hint_text,
                                     fg=self._hint_color or self.c['sub'])

    # ============ TH-08：本地 1s ticker（倒计时/归零，不重建账号行） ============

    def _tick_second(self) -> None:
        """每秒：重绘各指标块倒计时文本；归零经回调通知 main。
        §7.2：每块独立 try/except——单块失败记录诊断，不跳过后续块；
        面板已销毁的 Tcl 预期错误静默。"""
        try:
            for blk in self._metric_blocks:
                try:
                    blk['update']()
                except tk.TclError:
                    pass  # 面板已销毁等 Tcl 预期错误：静默
                except Exception as exc:
                    log_exception(
                        f"tick 指标块 {blk.get('key', '?')}/{blk.get('metric', '?')}", exc)
            try:
                due_keys = self._collect_due_keys()
            except tk.TclError:
                due_keys = []
            except Exception as exc:
                log_exception('tick 归零检测', exc)
                due_keys = []
            for key in due_keys:
                if self.on_countdown_due is not None:
                    try:
                        self.on_countdown_due(key)
                    except Exception as exc:
                        log_exception('tick 归零回调', exc)
        except Exception:
            pass  # 面板已销毁等：停止继续排程
        try:
            self._ticker_job = self.window.after(1000, self._tick_second)
        except Exception:
            self._ticker_job = None

    def _collect_due_keys(self) -> list:
        """本次 tick 新归零的账号 key 列表（(key, metric) 状态机，只报一次、每 tick 去重）。"""
        due_keys = []
        for blk in self._metric_blocks:
            state_key = (blk['key'], blk['metric'])
            due = self._countdown_due(blk)
            was = self._cd_due.get(state_key, False)
            if due and not was and blk['key'] not in due_keys:
                due_keys.append(blk['key'])
            self._cd_due[state_key] = due
        return due_keys

    def _countdown_due(self, blk) -> bool:
        """指标倒计时是否已归零（归零后显示「即将重置」；不在本地清零额度）。
        U03：错误块数据不完整，不参与归零上报。"""
        if blk['offline'] or blk.get('error'):
            return False
        m = self._metric(blk['usage'], blk['metric'])
        return self._format_countdown(getattr(m, 'resets_at', None)) == '即将重置'

    # ============ 数据 / 状态更新（冻结接口） ============

    def update_accounts(self, current, current_usage, backups) -> None:
        """刷新面板数据。backups: list[tuple[Account, AccountUsage | None]]。"""
        self._data = (current, current_usage, backups)
        self._render_all()
        self._relayout()

    def set_toast(self, text: str, kind: str = 'info', sticky: bool = False,
                  duration_ms: Optional[int] = None,
                  follow_up: Optional[tuple] = None) -> None:
        """显示消息（info/success/error）；消息行按需出现。

        冻结契约：
        - 非 sticky：显示时长 = duration_ms 或 TOAST_MS（默认行为不变）；
          淡出完成后若 follow_up 非空 → 立即显示 follow_up[0]，时长
          follow_up[1] 毫秒，其自身按普通消息淡出（无二级后续）。
        - sticky 语义不变（不自动淡出，保持至被替换或 clear_transient()；
          非 sticky 消息期间挂起、结束后恢复显示）。
        - 任何新调用替换/取消既有序列（含尚未触发的 follow_up）；
          clear_transient() 同样取消。先加高度再绘制，避免闪跳。
        """
        if sticky:
            self._sticky_text = text
            self._sticky_kind = kind
            # 取消未触发/进行中的 follow_up 段（sticky 优先恢复；不打断
            # 正在淡出的第一段——其结束后 sticky 自然恢复）
            if self._follow_up_job:
                self._cancel_job(self._follow_up_job)
                self._follow_up_job = None
                self._transient_active = False
                self._finish_plain()  # 立即恢复（新）sticky 或隐藏
            if not self._transient_active:
                self._show_message(text, kind)
            return
        self._cancel_message_jobs()  # 新调用替换既有序列（含未触发 follow_up）
        self._follow_up = follow_up
        self._transient_active = True
        self._show_message(text, kind, duration_ms)
        self._toast_fade_job = self.window.after(
            duration_ms if duration_ms is not None else self.TOAST_MS,
            self._fade_toast)

    def clear_transient(self) -> None:
        """清除临时消息、follow_up 序列与 sticky 消息（sticky 保持到被替换
        或调用本方法）；取消未触发的 follow_up 定时器。"""
        self._cancel_message_jobs()
        self._follow_up = None
        self._sticky_text = ''
        self._sticky_kind = 'info'
        self._transient_active = False
        self._toast_text = ''
        self._hint_text = ''
        self._hint_color = ''
        self._status_full_text = ''
        self._hide_status_row()

    def _cancel_job(self, job) -> None:
        try:
            self.window.after_cancel(job)
        except Exception:
            pass

    def _cancel_message_jobs(self) -> None:
        """取消当前序列全部定时器（fade + follow_up），避免幽灵回调。"""
        if self._toast_fade_job:
            self._cancel_job(self._toast_fade_job)
            self._toast_fade_job = None
        if self._follow_up_job:
            self._cancel_job(self._follow_up_job)
            self._follow_up_job = None

    def _show_message(self, text: str, kind: str, duration_ms: Optional[int] = None) -> None:
        """pack 状态行（高度加入）→ 设置文本（长文本单行截断，完整文本
        存 _status_full_text 供点击复制）→ 绘制。"""
        self._toast_text = text
        self._toast_kind = kind
        self._toast_expire_at = time.time() + (
            duration_ms if duration_ms is not None else self.TOAST_MS) / 1000.0
        self._status_full_text = text
        self._hint_text = self._fit_text(text, self._f_sec)
        self._hint_color = {'success': self.c['ok'], 'error': self.c['warn'],
                            'info': self.c['accent']}.get(kind, self.c['accent'])
        self._show_status_row()  # 先加高度（无消息时不占高度）
        self._draw_status()

    def _fit_text(self, text: str, font) -> str:
        """单行截断：超宽时按字符截断并加 …（完整文本保留在 _status_full_text）。"""
        avail = max(80, self._layout_w - 2 * self.INSET - 28)
        if font.measure(text) <= avail:
            return text
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + '…') <= avail:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + '…'

    def _on_status_click(self, event=None) -> None:
        """点击状态行：完整消息文本复制到剪贴板 + 触发 on_status_click 回调。
        （简化取舍：单行截断时以复制代替完整详情视图。）"""
        text = self._status_full_text or self._hint_text
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(text)
        except Exception:
            pass
        if self.on_status_click is not None:
            try:
                self.on_status_click()
            except Exception:
                pass

    def _fade_toast(self) -> None:
        """第一段消息淡出；完成后 → follow_up 段或 sticky 恢复/隐藏。"""
        self._fade_impl('_toast_fade_job', self._on_first_done)

    def _fade_follow_up(self) -> None:
        """follow_up 段淡出；完成后 → sticky 恢复/隐藏（无二级后续）。"""
        self._fade_impl('_follow_up_job', self._on_plain_done)

    def _fade_impl(self, job_attr: str, on_done) -> None:
        """公共淡出循环：颜色插值 configure(fg=...) 逐帧；结束回调 on_done。
        job_attr 指定当前段的定时器槽（取消/替换时清理）。"""
        setattr(self, job_attr, None)
        start = time.time()
        base = {'success': self.c['ok'], 'error': self.c['warn'],
                'info': self.c['accent']}.get(self._toast_kind, self.c['accent'])

        def step():
            t = min((time.time() - start) / (self.TOAST_FADE_MS / 1000.0), 1.0)
            self._hint_color = self._blend(base, self.c['sub'], t)
            self._draw_status()
            if t < 1.0:
                setattr(self, job_attr, self.window.after(16, step))
            else:
                setattr(self, job_attr, None)
                on_done()
        step()

    def _on_first_done(self) -> None:
        """第一段淡出完成：有 follow_up → 立即显示（时长 follow_up[1]，
        其自身按普通消息淡出）；否则按普通收尾。"""
        self._toast_text = ''
        self._transient_active = False
        fu = self._follow_up
        self._follow_up = None
        if fu:
            self._transient_active = True
            self._show_message(fu[0], 'info', fu[1])
            self._follow_up_job = self.window.after(fu[1], self._fade_follow_up)
        else:
            self._finish_plain()

    def _on_plain_done(self) -> None:
        """follow_up 段淡出完成：普通收尾（无二级 follow_up）。"""
        self._toast_text = ''
        self._transient_active = False
        self._finish_plain()

    def _finish_plain(self) -> None:
        """序列结束收尾：sticky 挂起则恢复显示，否则隐去整行、高度回落。"""
        if self._sticky_text:
            self._show_message(self._sticky_text, self._sticky_kind)
        else:
            self._hint_text = ''
            self._hint_color = ''
            self._status_full_text = ''
            self._hide_status_row()  # 整行隐去、面板高度回落

    def _reapply_toast(self) -> None:
        """内容重建后恢复消息：sticky 优先，其次未过期的临时消息。"""
        if self._sticky_text:
            self.set_toast(self._sticky_text, self._sticky_kind, sticky=True)
        elif self._toast_text and time.time() < self._toast_expire_at:
            self.set_toast(self._toast_text, self._toast_kind)

    def set_switching(self, key: Optional[str]) -> None:
        """切换进行中：禁用全部切换按钮；key=None 恢复。"""
        self._switching_key = key
        self._apply_switching(key)

    def _apply_switching(self, key: Optional[str]) -> None:
        for k, btn in self._switch_buttons.items():
            if key is not None:
                btn.configure(state='disabled',
                              text='切换中…' if k == key else '切换',
                              bg=self.c['bg'], fg=self.c['offline'])
            else:
                btn.configure(state='normal', text='切换',
                              bg=self.c['card'], fg=self.c['accent'])

    # ============ 位置 / 可见性（冻结接口） ============

    def update_position(self, x: int, y_visible: int, y_hidden: int) -> None:
        """幂等设位：x 立即生效；y_visible/y_hidden 为滑动动画的两个目标。

        跟随保证：除 x 立即应用外，若目标 y（按当前可见态取 y_visible/y_hidden）
        与窗口当前 y 不一致且不在动画中，立即启动滑动——opencode 窗口拖动时
        main.py 每 100ms 传入新坐标，面板随之实时跟随，不会停在旧位置。
        """
        if x == self._x and y_visible == self._yv and y_hidden == self._yh:
            return
        self._x = int(x)
        self._yv = int(y_visible)
        self._yh = int(y_hidden)
        self._apply_geometry()
        target = self._yv if self._visible_target else self._yh
        if abs(target - self._y) > 0.5 and not self._slide_job:
            self._start_slide()

    def set_visible(self, visible: bool) -> None:
        """幂等；内部做 180ms 滑动动画（当前 y → 目标 y，ease-out）。"""
        if visible:
            if self._withdrawn:
                self.window.deiconify()
                self._withdrawn = False
        if visible == self._visible_target:
            return
        self._visible_target = visible
        self._start_slide()

    def is_visible(self) -> bool:
        """目标可见态（动画进行中按目标计）。"""
        return self._visible_target

    @property
    def width(self) -> int:
        """布局宽度（620–700，内容驱动）。"""
        return self._layout_w

    @property
    def height(self) -> int:
        """布局高度（update_idletasks 后按内容计算）。"""
        self.window.update_idletasks()
        return self._content.winfo_reqheight() + 2 * self.INSET

    def show(self) -> None:
        """显示面板（托盘"显示"）。"""
        if self._withdrawn:
            self.window.deiconify()
            self._withdrawn = False
        self.set_visible(True)

    def hide(self) -> None:
        """完全隐藏面板（托盘"隐藏"）。"""
        self._withdrawn = True
        self._visible_target = False
        if self._slide_job:
            self.window.after_cancel(self._slide_job)
            self._slide_job = None
        self.window.withdraw()

    def destroy(self) -> None:
        """销毁窗口与所有定时任务（含 follow_up 序列定时器）。"""
        for job in (self._slide_job, self._toast_fade_job,
                    self._ticker_job, self._follow_up_job):
            if job:
                try:
                    self.window.after_cancel(job)
                except Exception:
                    pass
        try:
            self.window.destroy()
        except Exception:
            pass

    def apply_theme(self, theme_colors: dict) -> None:
        """主题热切换（main.py 在设置保存后调用）。"""
        self.c = theme_colors
        self._trans = self.c['bg']
        for w in self._content.winfo_children():
            w.destroy()
        self._build_widgets()
        self._build_menu()
        self._render_all()
        self._relayout()
        self.window.configure(bg=self._trans)
        self._canvas.configure(bg=self._trans)
        self.window.after(30, lambda: _apply_region(
            self._get_hwnd(), self._layout_w, self._layout_h, self.CORNER_RADIUS))

    # ============ 动画 ============

    def _start_slide(self) -> None:
        if self._slide_job:
            self.window.after_cancel(self._slide_job)
            self._slide_job = None
        start_y = self._y
        end_y = self._yv if self._visible_target else self._yh
        if abs(end_y - start_y) < 0.5:
            self._y = float(end_y)
            self._apply_geometry()
            return
        t0 = time.time()
        last_end = end_y
        dur = self.SLIDE_MS / 1000.0

        def step():
            nonlocal t0, last_end
            end = self._yv if self._visible_target else self._yh
            if end != last_end:
                # 目标位置变化（窗口被移动）：重新计时，从当前位置续滑
                last_end = end
                t0 = time.time()
            t = min((time.time() - t0) / dur, 1.0)
            ease = 1 - (1 - t) ** 3
            self._y = start_y + (end - start_y) * ease
            self._apply_geometry()
            if t < 1.0:
                self._slide_job = self.window.after(self.FRAME_MS, step)
            else:
                self._slide_job = None

        step()

    def _apply_geometry(self) -> None:
        try:
            self.window.geometry(
                f'{self._layout_w}x{self._layout_h}+{self._x}+{int(self._y)}')
        except Exception:
            pass

    def _relayout(self) -> None:
        """内容变化后重算尺寸（宽度内容驱动、高度随行数）、重绘圆角背景。"""
        self.window.update_idletasks()
        req_w = self._content.winfo_reqwidth()
        sw = self.window.winfo_screenwidth()
        self._layout_w = int(min(max(self.MIN_WIDTH, req_w), self.MAX_WIDTH, sw - 24))
        self._layout_h = self._content.winfo_reqheight() + 2 * self.INSET
        # 内容帧宽度 = 面板内宽（保证行网格铺满、指标块获得真实宽度）
        try:
            self._canvas.itemconfigure(
                self._content_item, width=self._layout_w - 2 * self.INSET)
        except Exception:
            pass
        self._draw_panel()
        self._apply_geometry()

    def _draw_panel(self) -> None:
        w = self._layout_w
        h = self._layout_h
        r = self.CORNER_RADIUS
        self._canvas.delete('panel')
        self._canvas.create_polygon(
            _rounded_rect_points(0.5, 0.5, w - 0.5, h - 0.5, r),
            smooth=True, fill=self.c['bg'], outline=self.c['border'], width=1,
            tags='panel')
        self._canvas.tag_lower('panel')
        # 窗口级圆角裁切（实色窗口主路径；尺寸变化后同步更新）
        try:
            _apply_region(self._get_hwnd(), w, h, r)
        except Exception:
            pass

    # ============ 工具 ============

    @staticmethod
    def _usage_offline(usage) -> bool:
        """整行离线：usage 为 None，或三个指标全部 error/缺失。
        U03：单项指标异常不再整行置灰（该块显示状态短文案，其余指标照常）。"""
        if usage is None:
            return True
        for key in ('rolling', 'weekly', 'monthly'):
            m = getattr(usage, key, None)
            if m is not None and getattr(m, 'error', None) is None:
                return False  # 至少一个指标有效
        return True

    @staticmethod
    def _metric_error_text(error) -> str:
        """指标 error → 块内短文案（U03 映射；401/密钥、429/频繁、网络、其他）。"""
        e = (error or '').lower()
        if '401' in e or '密钥' in e or 'invalid' in e or 'unauthorized' in e:
            return '密钥失效'
        if '429' in e or '频繁' in e or 'rate' in e or 'limit' in e:
            return '请求过频'
        if '网络' in e or '连接' in e or 'timeout' in e or '超时' in e or 'unreachable' in e:
            return '连接失败'
        return '数据不可用'

    @staticmethod
    def _metric(usage, key):
        return getattr(usage, key, None) if usage is not None else None

    def _pct_color(self, percent: Optional[int]) -> str:
        if percent is None:
            return self.c['offline']
        if percent >= 85:
            return self.c['warn']
        if percent >= 60:
            return self.c['attention']
        return self.c['fg']

    @staticmethod
    def _mask(key: str) -> str:
        """掩码唯一规则委托 Config.mask_key（oc_sk_ 前 8 + … + 后 4 / 其他前 6 + … + 后 4）。"""
        return Config.mask_key(key)

    @staticmethod
    def _blend(c1: str, c2: str, t: float) -> str:
        """颜色插值（Toast 淡出）。"""
        def parse(h):
            h = h.lstrip('#')
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r1, g1, b1 = parse(c1)
        r2, g2, b2 = parse(c2)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f'#{r:02x}{g:02x}{b:02x}'

    @staticmethod
    def _format_countdown(resets_at, now=None) -> str:
        """resetsAt(ISO8601) → '3H25M' / '4天3H' / '即将重置' / '--'。
        <24h：大写 H/M、无空格；≥24h：N天H（不带分钟）；已过期：即将重置。"""
        if not resets_at:
            return '--'
        try:
            if isinstance(resets_at, datetime.datetime):
                dt = resets_at
            else:
                s = str(resets_at).strip()
                if s.endswith('Z'):
                    s = s[:-1] + '+00:00'
                dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            if now is None:
                now = datetime.datetime.now(datetime.timezone.utc)
            delta = (dt - now).total_seconds()
        except Exception:
            return '--'
        if delta <= 0:
            return '即将重置'
        total_h = delta / 3600.0
        if total_h < 24:
            h = int(total_h)
            m = int((delta % 3600) // 60)
            return f'{h}H{m}M'
        d = int(total_h // 24)
        h = int(total_h % 24)
        return f'{d}天{h}H'