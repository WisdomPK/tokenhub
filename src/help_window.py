"""TokenHub v3 帮助窗口。

重写内容（废弃 V2 的 Cookie/curl 教程）：
1. 如何从 console（opencode.ai/auth）复制 API Key；
2. 额度条用法（悬停唤出、切换含义）；
3. FAQ（密钥存储位置 / 切换生效边界（需重启 opencode）/ 回退模式）。
"""

import tkinter as tk
from typing import Optional

from theme import ThemeManager


class HelpWindow:
    """帮助窗口（普通可聚焦对话框；构造即显示，重复打开复用单例）。"""

    WINDOW_WIDTH = 620
    WINDOW_HEIGHT = 560
    FONT = 'Microsoft YaHei UI'

    _active: Optional['HelpWindow'] = None  # 类级单例

    SECTIONS = [
        ('获取 API Key（console）', [
            '1. 打开 https://opencode.ai/auth 并登录你的账号。',
            '2. 在页面中选择 Go 套餐（Plan: Go）。',
            '3. 点击「Copy API Key」按钮复制密钥（新版以 oc_sk_ 开头、旧版以 sk- 开头），或手动复制页面显示的密钥。',
            '4. 打开 TokenHub 设置 → 新增账号：填标签、粘贴密钥，点「添加并校验」。',
            '5. 校验通过后该账号即加入额度池，可随时切换。',
        ]),
        ('额度条用法', [
            '1. 把鼠标移到 opencode 窗口顶部中央的细条上，面板会下滑展开。',
            '2. 鼠标移开面板与热区约 0.4 秒后，面板自动收起。',
            '3. 面板显示当前账号的 5小时 / 本周 / 本月 额度【已用】百分比与重置倒计时；',
            '4. 单项指标异常时该块显示状态短文案（密钥失效 / 请求过频 / 连接失败 / 数据不可用），其余指标照常显示。',
            '5. 点备用账号行的「切换」：两账号角色互换，后续请求使用新账号。'
            '（弹窗出现在额度条面板下方，不会跑到其他屏幕。）',
            '6. 右键面板可打开菜单：设置 / 帮助 / 立即刷新 / 退出。',
            '7. 托盘图标常驻：左键切换显示，右键打开菜单。',
        ]),
        ('常见问题（FAQ）', [
            'Q：密钥存在哪里？',
            'A：TokenHub 的 config.json（exe 同目录或 %LOCALAPPDATA%\\TokenHub），'
            '以及 opencode 的 auth.json（默认 ~/.local/share/opencode/auth.json）。'
            '均为本地明文，与 opencode 自身存储方式一致；界面与日志只显示掩码。',
            '',
            'Q：切换后什么时候生效？',
            'A：切换会更新 opencode 的认证文件；opencode 进程内缓存了已构建的模型客户端'
            '（缓存键不含凭据），auth 变更不会使缓存失效——需要重启 opencode 后生效，'
            '运行中的进程会继续使用旧账号。',
            '',
            'Q：opencode 没在运行时能用吗？',
            'A：能。opencode 进程未运行时，额度条会退化为"屏幕顶边"模式：'
            '鼠标移到屏幕顶边中央的细条即可唤出。窗口仅最小化时热区失效且不回退（进程仍在运行）。',
            '',
            'Q：切换失败会怎样？',
            'A：先尝试官方控制接口；不可达时自动降级为直写 auth.json（带备份与读回校验），'
            '失败会自动回滚原文件并弹出红色提示。',
            '',
            'Q：密钥显示安全吗？',
            'A：面板与设置界面只显示掩码（如 sk-T3t…abcd、oc_sk_T3tX…abcd），完整密钥仅保存在本地文件。',
        ]),
    ]

    def __init__(self, root: tk.Tk, theme_colors: Optional[dict] = None):
        self.root = root
        self.colors = theme_colors if theme_colors is not None else ThemeManager().get_colors()
        self.window: Optional[tk.Toplevel] = None
        self.show()

    def show(self) -> None:
        existing = HelpWindow._active
        if (existing is not None and existing is not self
                and existing.window is not None and existing.window.winfo_exists()):
            existing.window.lift()
            existing.window.focus_force()
            return
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            self.window.focus_force()
            return
        HelpWindow._active = self
        self.window = tk.Toplevel(self.root)
        self.window.title('TokenHub 使用帮助')
        self.window.resizable(True, True)
        self.window.configure(bg=self.colors['bg'])
        self._center()

        main = tk.Frame(self.window, bg=self.colors['bg'], padx=12, pady=12)
        main.pack(fill=tk.BOTH, expand=True)

        text_frame = tk.Frame(main, bg=self.colors['bg'])
        text_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        text = tk.Text(text_frame, wrap='word',
                       font=(self.FONT, 10),
                       bg=self.colors['bg'], fg=self.colors['fg'],
                       insertbackground=self.colors['fg'],
                       yscrollcommand=scrollbar.set,
                       padx=12, pady=8,
                       relief='flat', highlightthickness=0, bd=0)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=text.yview)

        text.tag_configure('title', font=(self.FONT, 13, 'bold'),
                           foreground=self.colors['accent'], spacing1=10, spacing3=4)
        text.tag_configure('body', spacing1=1, spacing3=1)
        text.tag_configure('accent', foreground=self.colors['accent'])

        for title, lines in self.SECTIONS:
            text.insert('end', title + '\n', ('title',))
            for line in lines:
                text.insert('end', line + '\n', ('body',))
            text.insert('end', '\n')

        text.configure(state='disabled')

        btn_frame = tk.Frame(main, bg=self.colors['bg'])
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        tk.Button(btn_frame, text='关闭', font=(self.FONT, 10),
                  bd=0, relief='flat', cursor='hand2',
                  bg=self.colors['card'], fg=self.colors['fg'],
                  activebackground=self.colors['card'],
                  activeforeground=self.colors['accent'],
                  padx=20, pady=4, command=self._close).pack(side=tk.RIGHT)

        self.window.bind('<Escape>', lambda e: self._close())
        self.window.protocol('WM_DELETE_WINDOW', self._close)

    def _center(self) -> None:
        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        x = (sw - self.WINDOW_WIDTH) // 2
        y = (sh - self.WINDOW_HEIGHT) // 2
        self.window.geometry(f'{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}')

    def _close(self) -> None:
        if HelpWindow._active is self:
            HelpWindow._active = None
        if self.window is not None and self.window.winfo_exists():
            self.window.destroy()
        self.window = None

    def hide(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()