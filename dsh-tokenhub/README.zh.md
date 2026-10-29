# dsh-tokenhub

**DeepSeek Harness（dsh）的 OpenCode Go 额度条** —— 悬浮小组件实时显示账号池里每个 key 的 **5小时 / 周 / 月** 用量与重置倒计时，并支持**一键切换账号**。

从 [TokenHub v3](https://github.com/WisdomPK/tokenhub)（opencode 桌面端额度条）移植而来。**最大的不同**：dsh 的凭据服务按次解析、无跨操作缓存，所以这里**切换是热生效的，不需要重启**——TokenHub 因为 opencode 缓存了模型客户端，只能在文档里写明「需重启 opencode」。

[English](README.md) · **简体中文**

## 功能

- **悬浮球** —— 页面**右侧靠下**（约在视口自下而上 1/5 处，不居中）一个**纵向圆角矩形**，从上到下**三根横向柱体，依次是 5小时 / 周 / 月**。每根按本窗口的用量填充长度、按本窗口的档位着色（绿 / 琥珀 / 红），所以「哪项额度在涨、涨到哪一档」由位置和颜色同时给出。**没有角标数字**——三项都已经在球上了。悬停显示三个窗口的具体百分比。球默认按 **2 倍**渲染（`scale` 可调），用 `transform: scale()` 实现，圆角与柱体间距等比放大；**面板和对话框不缩放**——它们承载文字，文字放大就不再合适了
- **展开面板** —— 点击展开，每个账号一张卡片：三行指标（5小时 / 周 / 月），阈值着色的进度条 + 百分比 + ⟳ 重置倒计时；当前账号标「使用中」，查询失败的账号整体置灰显示「离线」
- **一键切换** —— 备用账号行提供 [切换]（二次确认）；切换写入 dsh 凭据，**下一个请求即生效**
- **生效中的密钥永远有自己的行** —— dsh 实际用的密钥（`OPENCODE_GO_API_KEY`）未必在账号池里。这时面板会为它单开一行、标「未入池」，带 [新增] 与 [加入池]；「加入池」把它存成一条正式账号——**不做这一步就直接切走，这个密钥就再也找不回来了**
- **新增入口永远存在** —— 当前行带 [新增]；没有当前行（比如还没配密钥）时，面板底部也会给一个，不会把你困在「加不了第二个账号」的状态里
- **新增 / 删除账号** —— 新增时先拿真实接口校验密钥，无效直接拒绝并保留输入可重试；使用中的账号不允许删除
- **自动发现账号池** —— 扫描 $DSH_HOME/.credentials.yaml 里所有 OPENCODE_GO_KEY_<名>；池为空时回退显示当前生效的单密钥，此时卡片上的 [新增] 就是进入账号池的入口
- **默认不碰 opencode** —— 切换只改 dsh 自己的凭据。两个 harness 的生效时机不一样（dsh 下一个请求、opencode 需重启），默认同步会留下「两边不一致」的窗口，所以同步是**显式 opt-in**（`syncOpencodeAuth: true`）；开了之后同样有备份 + 原子替换 + 读回校验 + 失败回滚，且提示会写明 opencode 需重启
- **主题自适应 / 中英双语** —— 全部使用 dsh 主题 token（--dsw-alias-*），跟随明暗主题；语言按浏览器语言自动选择

## 安装

```powershell
# 在本仓库目录下执行；幂等，可重复运行
powershell -ExecutionPolicy Bypass -File install.ps1
```

或者直接用 dsh 自己的插件命令（会自动写入 profile 的依赖与 bundles 列表）：

```powershell
dsh plugin --profile web add link:<path-to-dsh-tokenhub>
```

> ⚠️ **必须用 `link:`，不能用 `file:`。** pnpm 会把 `file:` 依赖**拷贝**进 `node_modules`，dsh 读的是安装那一刻的快照——之后你改的源码永远不会生效，**连重启都没用**（重启后读的还是那份副本）。`link:` 建软链，工作目录就是安装位置。

装完刷新一下浏览器页面即可（dsh 的 HMR 通常连重启都省了；没生效就重启 dsh web）。

## 使用

| 操作 | 位置 |
|---|---|
| 展开 / 收起面板 | 点右侧悬浮球 |
| 立即刷新 | 面板右上角「刷新」 |
| 切换账号 | 备用账号行 → [切换] → 二次确认 |
| 把生效中的密钥存进账号池 | 当前账号行（标「未入池」）→ [加入池] → 填名字 |
| 新增账号 | 当前账号行 → [新增] → 填名字与密钥 → [确认]（会先真实校验） |
| 删除账号 | 备用账号行 → [删除] → 二次确认 |

账号名**完全由你决定**（1–32 位字母 / 数字 / 下划线），插件不会替你起名；只有**留空**时才自动生成 `go1`、`go2`…。对话框的账号名与密钥输入框都关掉了浏览器自动填充——否则密码管理器会把密码框上方那个文本框当成登录用户名替你填掉。

## 账号池格式

密钥读自 $DSH_HOME/.credentials.yaml（dsh 标准凭据文件），沿用社区既有约定，与已发布的 [dsh-opencode-go-usage](https://github.com/xiaweiliang060035/dsh-opencode-go-usage) 数据互通：

```yaml
OPENCODE_GO_API_KEY: sk-…        # 当前生效的密钥（dsh 的 llm 适配器按次解析它）
OPENCODE_GO_KEY_ACTIVE: go2      # 当前生效的是池里哪一个
OPENCODE_GO_KEY_go1: sk-…
OPENCODE_GO_KEY_go2: sk-…
```

OPENCODE_GO_KEY_<名> 的**数量与名字任意**。池为空时退化为单密钥模式，只显示 OPENCODE_GO_API_KEY。

「当前账号」的判定：优先看 OPENCODE_GO_KEY_ACTIVE 指向的名字；没有这个标记（或它指向的名字已不在池里）时，回退为**值与当前密钥相同**的那一项——所以手工维护的池也能正确高亮。

**池里没有当前密钥是正常状态**，不是错误：面板会给它单独一行（`inPool: false`），它不可切换、不可删除，但带着 [新增] 和 [加入池]。这样「刚新增了第一个账号」和「还没建池」两种情况都不会让面板失去新增入口。

## 配置

在 profile 的 cordis.patch.yml 里给插件行加 config：

```yaml
- id: tokenhub
  config:
    refreshMs: 60000
    syncOpencodeAuth: true
```

| 键 | 默认 | 说明 |
|---|---|---|
| baseUrl | https://opencode.ai/zen/go/v1/usage | 用量接口 |
| refreshMs | 60000 | Host 轮询间隔（毫秒） |
| timeoutMs | 15000 | 单次请求超时 |
| dshHome | resolveDshHome() | 覆盖 DSH home |
| credentialsPath | <dshHome>/.credentials.yaml | 覆盖凭据文件位置 |
| keyNames | 自动发现 | 固定池名允许列表：配置后跳过文件扫描，新增 / 入池 / 改名的目标必须在此列表内 |
| opencodeAuthPath | 自动解析 | 覆盖 opencode 的 auth.json 路径 |
| syncOpencodeAuth | **false** | 切换时是否同时写 opencode 的 auth.json（默认关；开启后提示会注明 opencode 需重启） |
| thresholds | {warn: 60, error: 85} | 阈值配色分档 |
| scale | **2** | **悬浮球**尺寸倍数；面板与对话框不缩放（球变宽时面板会自动往左让开） |
| autoReleaseWedgedLock | true | 写凭据撞上残留写锁时，是否判定后自动清理并重试 |
| wedgedLockMinAgeMs | 30000 | 判定「残留」所需的最小静置时长（毫秒） |

`keyNames` 是**固定允许列表**，不是发现提示：一旦配置（即使配成 `[]`），池中只列出这些名字，新增、入池、改名的目标名必须已在列表中。因此改名要先把新名加进 `keyNames`，在面板完成改名后再移除旧名。空名新增仍会自动生成 `goN` 候选名，但只有该名称在列表中时才会成功；否则请填写列表中的名称，或把生成的名称加入 `keyNames`。

## 工作原理

**Host 半**（lib/index.js，Node ESM cordis 插件）——所有密钥只在这一侧存在：

1. 扫描凭据文件得到候选池名，逐个用 ctx.credentials.resolve() 确认并取值
2. 并发请求用量接口（Authorization: Bearer <key>），单账号失败不影响其他账号
3. 结果缓存成一份**不含任何密钥**的快照，按 refreshMs 轮询
4. 注册三条路由：GET /plugins/dsh-tokenhub/snapshot、POST /switch、POST /account

**Client 半**（lib/client.js，浏览器 bundle）——注册进 shell.overlay 全帧悬浮层，只读 Host 的快照。**它永远看不到密钥**，只看得到名字、掩码（前 6 位 + … + 后 4 位）、百分比和重置时间。

写操作有同源校验（带 Origin 且与 Host 不符的请求一律 403）。

## 安全

- 密钥只存在 Host 侧，浏览器永远拿不到；界面与快照只出现掩码
- 出站请求只到 opencode.ai
- 切换先读回校验（凭据是按次解析的，读回即权威），失败自动回滚原账号
- **开启** `syncOpencodeAuth` 时才会碰 opencode：写前备份（保留最近 5 份）、同目录临时文件 + 原子替换、读回校验、失败回滚；**只改 opencode-go 字段**，其他字段原样保留。默认关闭时 opencode 的 auth.json 连读都不读
- 当前密钥若由启动环境变量提供（只读源遮蔽），切换会被明确拒绝并说明原因
- **残留写锁的清理是有判据的**：只有当锁文件记录不是 `PID\n`（即持有者死在「建锁」与「写 PID」之间，atomic-write 自己也无法接管）**且**已静置超过 `wedgedLockMinAgeMs` 时才会删除。锁里记着活 PID 的、或刚创建的空锁，一律不碰——前者归 atomic-write 自己接管，后者可能是正在建锁的写者。清理动作会在结果里明说，不会静默

## 测试

```powershell
npm install          # 只为测试装 react / react-dom / jsdom
npm test             # 151 Host + 116 Client + 68 契约 + 31 并发 + 32 快照（共 398 项）
```

- test/host.test.mjs —— 内存凭据桩 + 打桩的用量接口，覆盖池发现、当前账号判定、切换与回滚、增删校验、同源拦截
- test/client.test.mjs —— 在 jsdom 里用真实 React 挂载组件，驱动点击，断言渲染出的百分比、阈值分档、倒计时、二次确认与请求体
- test/usage-contract.test.mjs —— 读取根 tests/fixtures/usage-contract.json 的百分比与重置时间边界用例
- test/account-concurrency.test.mjs —— 串行账号变更、冲突检查、部分成功与 warning
- test/snapshot.test.mjs —— revision 批次、唯一 key 去重、并发上限与 stale 窗口

## 与 TokenHub 的差异

| TokenHub（opencode） | 本插件（dsh） |
|---|---|
| 悬停 opencode 窗口顶边 10px 热区唤出 | 浏览器里没有等价物 → 常驻悬浮球 |
| Python/tkinter 独立置顶窗 + 托盘 | Host 半 + Client 半，跑在 dsh 进程与页面里 |
| 切换写 auth.json，**需重启 opencode 生效** | 只写 dsh 凭据，**下一个请求即生效**；opencode 默认不动（可选同步，但那边仍需重启） |
| 独立设置窗 / 帮助窗 | 面板内联对话框 |
| 指标级错误文案（每个窗口独立解析） | 整账号一次请求：某个窗口没返回百分比时显示「数据不可用」，整账号失败显示「离线」 |

## 生效时机速查

| 动作 | 何时生效 |
|---|---|
| 在 dsh 面板点 [切换] | **下一个请求**（dsh 按次解析凭据，无跨操作缓存） |
| 同一操作对 opencode 的影响 | **无** —— 默认不写 opencode 的文件 |
| 开启 `syncOpencodeAuth: true` 后 | 会写 opencode 的 auth.json，但 **opencode 要重启才读取** |
| 改动本插件源码 | **需重启 dsh** —— `dsh-hmr` 默认 `root: []` 不监视源码，且 ESM 模块按 URL 缓存；另需依赖写成 `link:`（见安装一节） |

## 已知事项

- **硬杀 dsh 进程可能留下残留写锁。** dsh 的 `atomic-write` 先以独占方式建 `<文件>.lock`，再写入持有者 PID；进程若恰好死在这两步之间，就会留下一个空记录锁，而 atomic-write 拒绝接管记录不完整的锁。用任务管理器「结束任务」或 `taskkill /F` 结束 dsh 就可能触发。插件会在下一次写凭据时按上面的判据自愈；优雅退出则不会产生这种锁。
- 用量接口 `GET /opencode.ai/zen/go/v1/usage` 不是 opencode 公开文档的一部分，由社区发现并验证；插件对响应的解析是防御式的。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 悬浮球不出现 | 确认装的是 **web** profile；刷新页面；dsh plugin --profile web list 应能看到 dsh-tokenhub |
| 角标显示 ! | 快照拉取失败：确认 dsh web 在跑、/plugins/dsh-tokenhub/snapshot 有响应 |
| 卡片显示「密钥无效」 | 该 key 失效或过期 |
| 卡片显示「网络错误」 | Host 到 opencode.ai 不通（代理 / 离线 / 超时）。注意 **Node 22 的 fetch 不读 HTTP(S)_PROXY**，本机需要能直连 |
| 面板提示未配置密钥 | .credentials.yaml 里既没有 OPENCODE_GO_KEY_* 也没有 OPENCODE_GO_API_KEY |
| 切换报「由启动环境提供（只读）」 | OPENCODE_GO_API_KEY 被启动环境变量遮蔽了，先在启动它的 shell 里清掉 |
| 提示「凭据文件被写锁阻塞」 | 遇到 `.credentials.yaml.lock` 残留锁。dsh 的 atomic-write 只在锁记录是可读 PID 时才会自动接管，空记录锁只能等。插件默认会先判定再自动清理并重试（`autoReleaseWedgedLock`）；若已关闭该选项，手动删：`Remove-Item -LiteralPath "$env:USERPROFILE\.dsh\.credentials.yaml.lock" -Force` |

## 卸载

```powershell
dsh plugin --profile web remove dsh-tokenhub
```

这会同时从 profile 的依赖与 bundles 列表里移除。凭据文件与 opencode 的 auth.json 不受影响。

## 许可

MIT
