"""TokenHub v3 额度条面板预览（独立运行，无需其他 lane 模块）。

用法：
    PY tools\\preview_floating.py [--seconds N]   # 默认 12 秒后自动退出

内容：1 个当前账号（「使用中」chip、标签「5小时已用」等）+ 2 个备用账号
（一个含 U03 单项指标错误「密钥失效」+ 周 92% 警告红、一个整行离线）、
新增/删除/切换操作弹窗自动演示（假 handler + 假校验器，不联网）：
    1) 当前行 [新增] → 新增弹窗（假校验第一次失败红字 → 重试通过 → 成功回执关窗）
    2) 备用行 [切换] → 确认弹窗 → 切换中 → 成功 Toast
    3) 备用行 [删除] → 确认弹窗 → 删除回调 Toast
以及展开/收起控制按钮；末尾演示 U02 粘性消息（显示不淡出 → 临时覆盖 →
自动恢复 → clear_transient 清除）与 follow_up 序列（切换成功 → 淡出 →
「请重启 opencode」3 秒 → 淡出结束；替换取消 follow_up 顺带验证）。
指标块为两行居中（上行「标签 百分比」、下行「⟳ 倒计时」，Frame + Label
控件渲染）；启动时打印各块两行实际请求宽（控件 reqwidth，用于宽度自查）。
Toast 为临时行：演示序列末尾（删除 Toast）打印「Toast 期间高度」，
淡出完成后轮询等待行隐藏并打印「淡出后高度」（空闲高度见面板尺寸行）。

仅依赖 theme.py、floating_window.py、dialogs.py 与 window_tracker（仅其
set_dpi_awareness，与修复后的 exe 渲染一致）；不 import api/config。
"""

import argparse
import datetime
import os
import sys
import tkinter as tk


class _Account:
    def __init__(self, label: str, key: str):
        self.label = label
        self.key = key


class _Metric:
    def __init__(self, percent, resets_at, error=None):
        self.status = 'ok' if error is None else 'error'
        self.percent = percent
        self.resets_at = resets_at
        self.error = error


class _Usage:
    def __init__(self, rolling, weekly, monthly):
        self.rolling = rolling
        self.weekly = weekly
        self.monthly = monthly
        self.fetched_at = __import__('time').time()


class _FakeValidator:
    """假校验器：第一次调用失败（演示窗内红字），重试通过。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, key):
        self.calls += 1
        if self.calls == 1:
            return '密钥无效（401）'
        return None


def _build_demo_data():
    now = datetime.datetime.now(datetime.timezone.utc)
    current = _Account('主账号', 'sk-T3tXXXXXXXXXXXXXXXXXXXXabcd')
    current_usage = _Usage(
        _Metric(1, (now + datetime.timedelta(hours=4, minutes=59)).isoformat()),
        _Metric(0, (now + datetime.timedelta(days=6)).isoformat()),
        _Metric(0, (now + datetime.timedelta(days=29)).isoformat()),
    )
    backups = [
        # U03 演示：账号B 月指标错误（其余指标照常显示）；周 92% ≥85 → 红色
        (_Account('账号B', 'sk-8KdXXXXXXXXXXXXXXXXXXXXwxyz'),
         _Usage(
             _Metric(23, (now + datetime.timedelta(hours=3, minutes=12)).isoformat()),
             _Metric(92, (now + datetime.timedelta(days=1, hours=2)).isoformat()),
             _Metric(None, None, error='密钥无效（401）'))),
        # 离线示例（无任何有效指标）
        (_Account('账号C', 'sk-Q2mXXXXXXXXXXXXXXXXXXXX7n1p'), None),
    ]
    return current, current_usage, backups


def main() -> int:
    parser = argparse.ArgumentParser(description='TokenHub 额度条面板预览')
    parser.add_argument('--seconds', type=int, default=12,
                        help='自动退出秒数（默认 12；演示全程约 4 秒）')
    args = parser.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
    from window_tracker import set_dpi_awareness
    set_dpi_awareness()  # 在创建 Tk 根之前：与修复后的 exe（main.py 同源）渲染一致
    from theme import ThemeManager
    from floating_window import FloatingWindow

    root = tk.Tk()
    root.withdraw()

    colors = ThemeManager().get_colors()
    def _demo_add_ok(label, key):
        """预览的 on_add_account：演示成功回执 (True, '') → 弹窗关闭。"""
        print(f'[预览] 新增弹窗：校验通过 → 回调 on_add_account(label="{label}") → 关窗')
        win.set_toast(f'已添加「{label}」（演示）', 'success')
        return (True, '')

    win = FloatingWindow(
        root, colors,
        on_switch=lambda key: (
            print('[预览] 切换成功序列：认证文件已更新 → 跟随「请重启 opencode」'
                  '（本演示中将被删除 Toast 替换，验证替换取消 follow_up）'),
            win.set_toast('已切换至「账号B」：认证文件已更新', 'success',
                          duration_ms=800, follow_up=('请重启 opencode', 3000))),
        on_settings=lambda: win.set_toast('打开设置（演示）', 'info'),
        on_help=lambda: win.set_toast('打开帮助（演示）', 'info'),
        on_exit=root.destroy,
        on_refresh=lambda: win.set_toast('已立即刷新', 'info'),
        on_add_account=_demo_add_ok,
        on_delete_account=lambda key: win.set_toast('已删除账号（演示）', 'success'),
    )
    # 假校验器：预览不联网；真实校验由 main 接线后经 dialogs 默认校验器执行
    win._add_validator = _FakeValidator()

    current, current_usage, backups = _build_demo_data()
    win.update_accounts(current, current_usage, backups)
    print(f'预览面板尺寸: {win.width} x {win.height}')

    # 两行块宽自检（Label 控件渲染后的实际请求宽；行1/行2 为块内 place 子 Frame）
    # 注意：打印不含 ⟳（GBK 控制台无法编码该字符）
    root.update_idletasks()
    for col, (mk, lab) in zip((2, 4, 6), win.METRIC_LABELS):
        f = win._rows.grid_slaves(row=0, column=col)[0]
        top, bot = f.winfo_children()
        w1, w2 = top.winfo_reqwidth(), bot.winfo_reqwidth()
        print(f'[预览] 块「{lab}」行1宽 {w1} 行2宽 {w2} -> 请求宽 {max(w1, w2)}px')
    f = win._rows.grid_slaves(row=2, column=2)[0]
    top, bot = f.winfo_children()
    print(f'[预览] 离线块「5小时 / 离线」请求宽 {max(top.winfo_reqwidth(), bot.winfo_reqwidth())}px')

    # 屏幕顶边模式：居中、可见 y=0、隐藏 y=-height
    x = (root.winfo_screenwidth() - win.width) // 2
    win.update_position(x, 0, -win.height)
    win.set_visible(True)

    # ---- 演示序列（点击真实按钮 → 弹窗 → 自动驱动 → 链式下一个） ----

    def drive_add():
        """新增弹窗：填表 → 第一次校验失败（红字）→ 重试通过 → 关窗。"""
        dlg = win._active_dialog
        if dlg is None:
            return
        w = dlg.window
        print('[预览] 新增弹窗：填写 备注名=账号D、API Key=sk-demo-…')
        w.after(150, lambda: dlg.set_label('账号D'))
        w.after(300, lambda: dlg.set_key('sk-demo-XXXXXXXXXXXXXXXXXXXXXXXX'))
        w.after(450, lambda: dlg.confirm())
        w.after(700, lambda: print('[预览] 新增弹窗：第一次校验失败 → 窗内红字「密钥无效（401）」'))
        w.after(950, lambda: dlg.confirm())  # 重试：通过 → on_add 回调（打印见上）→ 关窗

    def demo_add():
        print('[预览] 演示 1/3：点击当前行 [新增]')
        root.after(100, drive_add)
        win._row_buttons[0]['add'].invoke()      # 阻塞至弹窗自动关闭
        root.after(100, demo_switch_confirm)

    def drive_switch():
        dlg = win._active_dialog
        if dlg is not None:
            print('[预览] 切换确认弹窗：自动点 [确认切换]')
            dlg.window.after(450, dlg.confirm)

    def demo_switch_confirm():
        print('[预览] 演示 2/3：点击备用行 [切换]')
        root.after(100, drive_switch)
        win._row_buttons[1]['switch'].invoke()
        # 确认后：演示切换中 → 成功 Toast
        key = backups[0][0].key
        win.set_switching(key)
        root.after(550, lambda: (win.set_switching(None),
                                 win.set_toast('认证文件已更新，重启 opencode 后生效', 'success')))
        root.after(800, demo_del_backup)

    def drive_del():
        dlg = win._active_dialog
        if dlg is not None:
            print('[预览] 删除确认弹窗：自动点 [删除]')
            dlg.window.after(450, dlg.confirm)

    def demo_del_backup():
        print('[预览] 演示 3/3：点击备用行 [删除]')
        root.after(100, drive_del)
        win.TOAST_MS = 300  # 演示加速：删除 Toast 快速淡出（回落演示仍完整）
        win._row_buttons[1]['delete'].invoke()
        print('[预览] 演示完成（共 3 个弹窗，全程未联网）')
        # Toast 临时行高度演示：删除 Toast 已由 on_delete_account 触发
        root.after(150, lambda: print(
            f'[预览] Toast 期间高度 {win.height} (行可见={win._status.winfo_ismapped()})'))

        def wait_hidden(tries=50):
            if win._status_shown and tries > 0:
                root.after(100, lambda: wait_hidden(tries - 1))
            else:
                print(f'[预览] 淡出后高度 {win.height} '
                      f'(行可见={win._status.winfo_ismapped()})')
                sticky_demo()
        root.after(1000, wait_hidden)

    def sticky_demo():
        """U02 演示（缩短版）：sticky 显示 + 不自动淡出；
        完整生命周期（临时覆盖/恢复/clear_transient）见行为探针。"""
        print('[预览] sticky 演示：粘性消息显示（不自动淡出，缩短版）')
        win.set_toast('配置无法读取，已保护原文件（演示粘性）', 'error', sticky=True)
        root.after(150, lambda: print(
            f'[预览]   sticky 高度 {win.height} 可见={win._status.winfo_ismapped()}'))
        root.after(400, lambda: print(
            f'[预览]   400ms 后仍可见（无自动淡出）={win._status.winfo_ismapped()}'))
        root.after(600, followup_demo)

    def followup_demo():
        """契约演示：第一段（400ms 加速）→ 淡出 → follow_up「请重启 opencode」
        3 秒照实 → 淡出结束（无二级后续；结束回 sticky 或隐藏）。
        轮询驱动，不依赖固定时序。"""
        print('[预览] follow_up 序列演示（第一段 400ms 加速；follow_up 3s 照实）')
        win.set_toast('已切换至「账号B」：认证文件已更新', 'success',
                      duration_ms=400, follow_up=('请重启 opencode', 3000))
        root.after(150, lambda: print(
            f'[预览]   第一段显示中 高度 {win.height} '
            f'文本="{win._status_label.cget("text")}"'))

        def wait_fu(tries=50):
            t = str(win._status_label.cget('text'))
            if '请重启' not in t and tries > 0:
                root.after(100, lambda: wait_fu(tries - 1))
            else:
                print(f'[预览]   第一段淡出后 follow_up 显示 高度 {win.height} '
                      f'文本="{t}"')
                wait_end()

        def wait_end(tries=60):
            t = str(win._status_label.cget('text'))
            if '请重启' in t and tries > 0:
                root.after(100, lambda: wait_end(tries - 1))
            else:
                print(f'[预览]   follow_up 3s 后结束（回 sticky 或隐藏）'
                      f'可见={win._status_shown} 高度={win.height} 文本="{t[:14]}"')
        root.after(1200, wait_fu)

    root.after(500, demo_add)

    # 控制按钮：展开 / 收起 / 退出
    ctl = tk.Toplevel(root)
    ctl.title('预览控制')
    ctl.resizable(False, False)
    ctl.configure(bg='#f0f0f0')
    cw = 320
    cx = (root.winfo_screenwidth() - cw) // 2
    cy = root.winfo_screenheight() - 110
    ctl.geometry(f'{cw}x64+{cx}+{cy}')

    frame = tk.Frame(ctl, bg='#f0f0f0', padx=8, pady=8)
    frame.pack(fill=tk.BOTH, expand=True)
    tk.Button(frame, text='展开', width=8, command=lambda: win.set_visible(True)).pack(side=tk.LEFT)
    tk.Button(frame, text='收起', width=8, command=lambda: win.set_visible(False)).pack(side=tk.LEFT, padx=8)
    tk.Button(frame, text='退出', width=8, command=root.destroy).pack(side=tk.RIGHT)

    root.after(max(1000, args.seconds * 1000), root.destroy)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())