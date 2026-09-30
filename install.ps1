# -*- coding: utf-8 -*-
# TokenHub v3 安装脚本（幂等，无需管理员权限）
#
# 作用：
#   1. 复制 plugin\tokenhub.ts → %USERPROFILE%\.config\opencode\plugins\
#      （opencode 桌面版启动时自动加载该插件，负责拉起/回收 TokenHub.exe）
#   2. 复制 dist\TokenHub.exe → %LOCALAPPDATA%\TokenHub\
#      （稳定安装位置；插件内置路径解析会命中此位置）
#   3. 打印后续步骤
#
# 用法：右键"使用 PowerShell 运行"，或：powershell -ExecutionPolicy Bypass -File install.ps1
# 重复运行安全：仅覆盖自身产物，不破坏其他文件。

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if (-not $root) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$pluginSource  = Join-Path $root 'plugin\tokenhub.ts'
$exeSource     = Join-Path $root 'dist\TokenHub.exe'
$pluginDir     = Join-Path $env:USERPROFILE '.config\opencode\plugins'
$pluginTarget  = Join-Path $pluginDir 'tokenhub.ts'
$exeDir        = Join-Path $env:LOCALAPPDATA 'TokenHub'
$exeTarget     = Join-Path $exeDir 'TokenHub.exe'

Write-Output 'TokenHub v3 安装开始'

# 前置检查
if (-not (Test-Path -LiteralPath $pluginSource)) {
    Write-Error "未找到插件源文件: $pluginSource"
    exit 1
}
if (-not (Test-Path -LiteralPath $exeSource)) {
    Write-Error "未找到构建产物: $exeSource （请先在项目根运行: python build.py）"
    exit 1
}

# 1. 安装插件
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget -Force
Write-Output "插件已安装: $pluginTarget"

# 2. 安装可执行文件
New-Item -ItemType Directory -Force -Path $exeDir | Out-Null
Copy-Item -LiteralPath $exeSource -Destination $exeTarget -Force
Write-Output "程序已安装: $exeTarget"

# 3. 核验
$pluginOk = Test-Path -LiteralPath $pluginTarget
$exeOk    = Test-Path -LiteralPath $exeTarget
if (-not ($pluginOk -and $exeOk)) {
    Write-Error '安装核验失败，请检查上述路径'
    exit 1
}

Write-Output ''
Write-Output '后续步骤：'
Write-Output '  1. 重启 opencode 桌面版（完全退出后重新打开），额度条将自动拉起。'
Write-Output '  2. 首次配置：打开 https://opencode.ai/auth -> Go -> 复制 API Key，'
Write-Output '     在额度条右键菜单 -> 设置 -> 添加账号 中粘贴。'
Write-Output '  3. 将鼠标悬停在 opencode 窗口顶部中央（10px 热区）即可唤出额度条。'
Write-Output ''
Write-Output '安装完成'
exit 0