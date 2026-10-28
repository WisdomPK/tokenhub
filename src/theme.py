"""TokenHub v3 主题管理。

- get_colors() 返回 V2 同构字典：bg/fg/accent/separator/menu_bg/menu_fg + is_dark，
  另附阈值语义色（ok/attention/warn/offline/sub/card/border/input_bg）供面板使用。
- auto 模式读取 Windows 注册表（AppsUseLightTheme）判断系统深色。
- 支持 set_theme('auto'|'light'|'dark') 供设置窗口切换主题。
"""

import platform
import winreg


class ThemeManager:
    """Windows 主题检测与配色。"""

    CHOICES = ('auto', 'light', 'dark')

    DARK = {
        'bg': '#1c1d21',          # 面板底色
        'fg': '#e9eaee',          # 主文字
        'sub': '#9aa0ab',         # 次要文字（深色下提亮一档：控件渲染后 9pt 小字对比度 5.3→6.4:1）
        'accent': '#4fc1ff',      # 强调（当前标记/链接）
        'separator': '#33353c',   # 分隔线
        'menu_bg': '#24262c',     # 右键菜单底色
        'menu_fg': '#e9eaee',     # 右键菜单文字
        'card': '#26272d',        # 卡片/按钮底色
        'border': '#3a3d45',      # 面板描边
        'input_bg': '#2d2f36',    # 输入框底色
        'ok': '#3fbf7f',          # 成功/正常
        'attention': '#e8a33d',   # 注意（60–85%）
        'warn': '#e5544d',        # 警告（≥85%）
        'offline': '#858a93',     # 离线/禁用（4.9:1，原 #6b7078 3.4:1 偏弱）
        'is_dark': True,
    }

    LIGHT = {
        'bg': '#f4f5f7',
        'fg': '#23262b',
        'sub': '#666b74',         # 次要文字（4.9:1，原 #6d727a 4.4:1 略低于 4.5）
        'accent': '#0a6fd4',
        'separator': '#dde0e4',
        'menu_bg': '#ffffff',
        'menu_fg': '#23262b',
        'card': '#ffffff',
        'border': '#e3e5e9',
        'input_bg': '#ffffff',
        'ok': '#107a4a',          # 成功（4.9:1，原 #1f9d63 3.2:1）
        'attention': '#a85500',   # 注意（4.9:1，原 #d97b06 2.8:1）
        'warn': '#c53030',        # 警告（5.0:1，原 #d63a3a 4.3:1）
        'offline': '#656a73',     # 离线/禁用（5.0:1，原 #9aa0a8 2.4:1 过浅）
        'is_dark': False,
    }

    def __init__(self, choice: str = 'auto'):
        self._choice = choice if choice in self.CHOICES else 'auto'
        self._colors = self._resolve()

    # ---- 主题解析 ----

    def _resolve(self) -> dict:
        if self._choice == 'dark':
            return dict(self.DARK)
        if self._choice == 'light':
            return dict(self.LIGHT)
        return dict(self.DARK if self._detect_dark_theme() else self.LIGHT)

    def set_theme(self, choice: str) -> None:
        """切换主题（auto/light/dark），无效值忽略。"""
        if choice not in self.CHOICES:
            return
        self._choice = choice
        self._colors = self._resolve()

    def get_colors(self) -> dict:
        """当前主题配色（调用方直接读取，勿修改返回的字典）。"""
        return self._colors

    def is_dark(self) -> bool:
        return bool(self._colors.get('is_dark'))

    # ---- 系统深色检测 ----

    @staticmethod
    def _detect_dark_theme() -> bool:
        """注册表 AppsUseLightTheme：0=深色，1=浅色。"""
        if platform.system() != 'Windows':
            return False
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            try:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
                return value == 0
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False