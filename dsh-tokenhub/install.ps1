# -*- coding: utf-8 -*-
# dsh-tokenhub 安装脚本（幂等，无需管理员权限）
#
# 作用：
#   1. 把本目录作为本地包加入 dsh profile 的依赖
#   2. 把 dsh-tokenhub 追加到 profile 的 dsh.profile.bundles 列表
#      （这两步由 dsh 自己的插件命令完成）
#   3. 核验插件行确实进入了组合后的插件树
#
# 用法（在本目录下）：
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Profile web
#
# 重复运行安全：dsh 的插件命令本身幂等。
#
# 注意：本脚本刻意不使用 -ErrorAction SilentlyContinue —— 失败必须可见。

[CmdletBinding()]
param(
    [string]$Profile = 'web'
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if (-not $root) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $root -or -not (Test-Path -LiteralPath $root)) {
    Write-Error '无法确定本仓库目录（PSScriptRoot 为空）'
    exit 1
}

$manifest     = Join-Path $root 'package.json'
$clientBundle = Join-Path $root 'lib\client.js'
$hostHalf     = Join-Path $root 'lib\index.js'
$patchFile    = Join-Path $root 'cordis.patch.yml'

Write-Output 'dsh-tokenhub 安装开始'
Write-Output "  包目录 : $root"
Write-Output "  profile: $Profile"
Write-Output ''

# ---- 前置检查 ----
foreach ($required in @($manifest, $hostHalf, $clientBundle, $patchFile)) {
    if (-not (Test-Path -LiteralPath $required)) {
        Write-Error "缺少必需文件: $required"
        exit 1
    }
}

$dsh = Get-Command dsh -ErrorAction SilentlyContinue
if (-not $dsh) {
    Write-Error ('找不到 dsh 命令。请先确认 DeepSeek Harness 已安装且 dsh 在 PATH 中。' + [Environment]::NewLine +
                 '若 dsh 由 Volta 管理，请确认 Volta 的 bin 目录在 PATH 里（通常还需要 volta.exe 本身可达）。')
    exit 1
}
Write-Output "已找到 dsh: $($dsh.Source)"

# ---- 安装 ----
# link: 而不是 file: —— 这一条很关键。pnpm 会把 file: 依赖【拷贝】进
# node_modules，于是 dsh 读的是安装那一刻的快照，之后所有源码改动都不会
# 生效（重启也没用）。link: 建立软链，本目录即安装位置。
$specifier = 'link:' + ($root -replace '\\', '/')
Write-Output "执行: dsh plugin --profile $Profile add $specifier"
Write-Output ''

& dsh plugin --profile $Profile add $specifier
if ($LASTEXITCODE -ne 0) {
    Write-Error "dsh plugin add 失败（退出码 $LASTEXITCODE）。请检查上面的 pnpm 输出。"
    exit 1
}

# ---- 核验：依赖与 bundles 已登记 ----
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$profileDir = Join-Path $dshHome "profiles\$Profile"
$profileManifest = Join-Path $profileDir 'package.json'

if (-not (Test-Path -LiteralPath $profileManifest)) {
    Write-Error "找不到 profile 清单: $profileManifest"
    exit 1
}

$profileJson = Get-Content -LiteralPath $profileManifest -Raw | ConvertFrom-Json
$depOk = $null -ne $profileJson.dependencies.'dsh-tokenhub'
$bundles = @($profileJson.dsh.profile.bundles)
$bundleOk = $bundles -contains 'dsh-tokenhub'

Write-Output ''
Write-Output '核验:'
Write-Output "  依赖已登记     : $depOk"
Write-Output "  bundles 已包含 : $bundleOk"

if (-not ($depOk -and $bundleOk)) {
    Write-Error ('核验失败：profile 清单里没有同时出现依赖与 bundles 条目。' + [Environment]::NewLine +
                 '请手动检查上面打印的 profile package.json，或改用 README 里的手动安装步骤。')
    exit 1
}

# ---- 核验：插件行进入组合后的插件树 ----
Write-Output '  正在核验组合后的插件树…'
$composed = & dsh --profile $Profile --dump-config 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Warning "dsh --dump-config 退出码 $LASTEXITCODE，跳过插件树核验。"
} elseif ($composed -notmatch '(?m)^\s*name:\s*dsh-tokenhub\s*$') {
    Write-Warning '组合后的插件树里没有找到 dsh-tokenhub 插件行。请检查上面的 dump 输出。'
} else {
    Write-Output '  插件行已进入插件树: 是'
}

Write-Output ''
Write-Output '后续步骤：'
Write-Output '  1. 刷新浏览器里的 dsh web 页面（dsh 的 HMR 通常无需重启；没生效就重启 dsh web）。'
Write-Output '  2. 页面右侧会出现一个悬浮球；点开即见各账号的 5小时 / 周 / 月 用量。'
Write-Output '  3. 首次配置账号池：面板里当前账号行 -> [新增]，填入名字与 API Key（会先真实校验）。'
Write-Output '     也可以在 ~/.dsh/.credentials.yaml 里手写 OPENCODE_GO_KEY_<名> 条目。'
Write-Output ''
Write-Output '验证安装：'
Write-Output "  dsh plugin --profile $Profile list"
Write-Output '  npm test        # 26 项 Host 检查 + 38 项 Client 检查'
Write-Output ''
Write-Output '安装完成'
exit 0
