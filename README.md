# TokenHub v3

在 opencode 桌面版窗口顶部提供一个"额度条"：鼠标悬停窗口顶部往下约 48px（会话标签行下方）的 10px 细条时，从顶边下滑唤出，显示当前 opencode-go 订阅账号的 5小时/每周/每月额度使用情况与重置倒计时，并列出备用账号及其额度，支持一键切换账号（角色互换）。

- 形态：独立 Python/tkinter 置顶无边框小窗（可脱离 opencode 独立运行）+ opencode 插件（随 opencode 启停自动拉起/回收）
- 数据来源：官方额度接口 `https://opencode.ai/zen/go/v1/usage`
- 平台：Windows（Python 3.13，仅标准库；tkinter 8.6）

## 安装

### 一键安装（推荐）

1. 确认已构建：项目根运行 `python build.py`（或 `C:\Users\hushangq\.local\bin\python3.13.exe build.py`），产物为 `dist\TokenHub.exe`。
   （**本仓库已附带构建好的 `dist\TokenHub.exe`，可跳过本步直接执行第 2 步**。）
2. 运行安装脚本：

   ```
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   脚本会（幂等，可重复运行）：
   - 复制 `plugin\tokenhub.ts` 到 `%USERPROFILE%\.config\opencode\plugins\`
   - 复制 `dist\TokenHub.exe` 到 `%LOCALAPPDATA%\TokenHub\`
3. 完全退出并重新启动 opencode 桌面版。额度条（或托盘图标）会自动出现；鼠标悬停窗口顶部往下约 48px（会话标签行下方）的 10px 细条即可唤出（偏移可在设置中调整）。

### 手动安装

- 插件：将 `plugin\tokenhub.ts` 复制到 `%USERPROFILE%\.config\opencode\plugins\`（目录不存在则创建）。
- 程序：将 `dist\TokenHub.exe` 复制到任意位置（例如 `%LOCALAPPDATA%\TokenHub\`），双击即可独立运行。

## 首次配置

1. 打开 https://opencode.ai/auth ，选择 Go 套餐，复制 API Key（形如 `oc_sk_...`（新版）或 `sk-...`（旧版））。
2. 额度条右键菜单 -> 设置 -> 添加账号：填入标签与密钥（可直接点"从剪贴板粘贴"）。
   添加时会用真实接口校验密钥，无效会被拒绝。
3. 当前账号（正在使用的 opencode-go 账号）会在启动时自动加入账号池，无需手动添加。

## 使用

- 唤出：鼠标悬停 opencode 窗口顶部往下约 48px（会话标签行下方）的 10px 细条（偏移默认 48，可在设置中调整），额度条从顶边下滑展开；移开后 0.4 秒自动收起。唤出与点击不会抢走编辑焦点。
- 面板内容：每个账号一行——账户区（●/○ + 名称 + 掩码，当前账号带「使用中」标记）、三个指标块（5小时/周/月**已用**百分比，块内两行居中：上行「标签 百分比」、下行「⟳ 倒计时」，百分比按阈值着色；单项指标异常时该块显示状态短文案、其余指标照常）、行尾部操作按钮；底部无常驻状态行，操作反馈（消息）为临时行（仅出现时占用高度；持续状态以粘性消息保持至关闭）。行尾部按钮：当前行 [新增]，备用行 [切换] [删除]。
- 阈值色：低于 60% 正常，60-85% 注意，85% 及以上警告；离线账号置灰并标注。
- 切换账号：点击备用行的「切换」（需二次确认），角色互换——该账号成为当前账号（写入 auth.json 的 opencode-go 字段），原当前账号转为备用。切换仅更新认证文件，**重启 opencode 后生效**（运行中的进程继续使用旧账号，直到重启）。
- 新增账号：点击当前行的「新增」，填入备注名（可选，留空自动命名）与 API Key（以 sk- 或 oc_sk_ 开头，默认掩码显示、可切换显示/隐藏，可一键从剪贴板粘贴）；点确认时会真实调用官方接口校验，通过后加入备用列表，失败则在弹窗内红字提示原因（如密钥无效、网络错误），可直接重试；保存失败时保留输入不关窗。
- 删除账号：仅备用行提供「删除」（需二次确认）；当前账号行不提供删除——当前账号正被 opencode 使用。弹窗（切换/删除/新增）居中显示在额度条面板下方，随面板所在屏幕夹取，不会跑到其他显示器。
- 托盘：显示/隐藏额度条、设置、帮助、立即刷新、退出。
- 回退模式：opencode 未运行（或窗口识别失败）时，若设置中开启"屏幕顶边回退"，热区变为屏幕顶边中央；关闭该选项则仅通过托盘显示。
- 右键菜单：设置 / 帮助 / 立即刷新 / 退出。

## 工作原理简述

- 额度数据：每次刷新（默认 60 秒）向 `https://opencode.ai/zen/go/v1/usage` 发送带 `Authorization: Bearer <key>` 的请求，解析 rolling（5小时）/ weekly（本周）/ monthly（本月）三项百分比与重置时间；单账号失败不影响其他账号。
- 当前账号：以 opencode 的 `auth.json`（默认 `~/.local/share/opencode/auth.json`）为准。启动或检测到文件变化时，若当前密钥不在账号池中则自动加入。
- 切换：优先调用插件提供的本机控制接口（127.0.0.1，随机端口 + 令牌）；接口不可达时直接写 `auth.json`（备份 + 原子替换 + 读回校验 + 失败回滚，仅修改 opencode-go 字段）。**生效边界（实测）**：opencode 进程内会缓存已构建的模型客户端（缓存键不含凭据），auth 变更不会使缓存失效——切换仅更新认证文件，**需重启 opencode 后生效**，运行中的进程继续使用旧账号。
- 插件生命周期：opencode 启动时加载插件，插件拉起 TokenHub.exe（互斥锁保证单实例）；opencode 退出时插件回收子进程；若父进程异常消失，额度条会自行退出。

## FAQ

- 密钥存在哪里，安全吗？密钥明文保存在本机 `config.json`（exe 同目录，不可写时回退 `%LOCALAPPDATA%\TokenHub\`）与 opencode 的 `auth.json` 中——与 opencode 自身的存储方式一致。额度条界面与日志只显示掩码（前 6 位 + … + 后 4 位），网络请求仅出站到 `opencode.ai`。
- 切换后什么时候生效？切换会更新 opencode 的认证文件；由于 opencode 进程内模型客户端缓存（缓存键不含凭据），auth 变更不会使缓存失效——**重启 opencode 后生效**，运行中的进程不会即时改变账号。
- 切换失败会怎样？写入失败会自动回滚到原密钥并红色提示；切换前会自动备份（保留最近 5 份）。
- 如何卸载？删除插件文件 `%USERPROFILE%\.config\opencode\plugins\tokenhub.ts`，删除程序文件 `%LOCALAPPDATA%\TokenHub\`（含 TokenHub.exe 与日志），再删除配置 `config.json`（位于 exe 同目录或 `%LOCALAPPDATA%\TokenHub\`）。auth.json 由 opencode 管理，卸载不影响。
- 构建需要什么？Python 3.13 与 PyInstaller（`python -m pip install pyinstaller`），项目根运行 `python build.py`。

## 已知事项

- opencode 未来若升级到 v2 插件 API（破坏性变更），插件 `plugin/tokenhub.ts` 需要按新语义迁移；额度条本体（Python 程序）不受影响，仍可独立运行。
- 主题修改即时生效：在设置中切换 auto/light/dark 后，额度条配色立即更新，无需重启。
- 独立运行（无插件）时切换走直写模式，提示文案会注明"直写模式"。