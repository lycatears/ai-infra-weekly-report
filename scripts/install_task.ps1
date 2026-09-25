<#
.SYNOPSIS
    注册「AI Infra 周报」Windows 计划任务。

.DESCRIPTION
    注册一个带**双触发器**的任务：

      1. 登录时（或 -RunAsSystem 时的开机时）——配合脚本内的补跑判定，
         实现「时间已过但报告缺失就立即补跑」
      2. 每周五 22:00 ——常规触发

    关键设置是 -StartWhenAvailable：如果触发时刻机器是关机的，
    Windows 会在下次开机后自动补跑一次。这与脚本内的补跑逻辑构成双层保障。

.PARAMETER TaskName
    计划任务名称，默认 AI-Infra-Weekly-Report。

.PARAMETER Weekday
    触发星期，1=周一 … 7=周日，默认 5（周五）。

.PARAMETER RunAsSystem
    以 SYSTEM 账户 + 开机触发注册，实现「未登录也执行」。
    **需要管理员权限的 PowerShell。**

.PARAMETER Force
    已存在同名任务时直接覆盖（默认即覆盖，此参数保留用于显式表达意图）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1

.EXAMPLE
    # 需要管理员权限
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -RunAsSystem
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AI-Infra-Weekly-Report',
    [int]$Weekday = 5,
    [int]$Hour = 22,
    [int]$Minute = 0,
    [int]$LogonDelayMinutes = 2,
    [int]$ExecutionTimeLimitMinutes = 120,
    [int]$RestartCount = 2,
    [int]$RestartIntervalMinutes = 10,
    [switch]$RunAsSystem,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot 'run_weekly.ps1'

if (-not (Test-Path -LiteralPath $runner)) {
    throw "找不到执行体脚本：$runner"
}
if ($Weekday -lt 1 -or $Weekday -gt 7) {
    throw "Weekday 必须在 1..7（1=周一，7=周日），收到 $Weekday"
}
if ($Hour -lt 0 -or $Hour -gt 23) { throw "Hour 必须在 0..23" }
if ($Minute -lt 0 -or $Minute -gt 59) { throw "Minute 必须在 0..59" }

$dayNames = @{
    1 = 'Monday'; 2 = 'Tuesday'; 3 = 'Wednesday'; 4 = 'Thursday'
    5 = 'Friday'; 6 = 'Saturday'; 7 = 'Sunday'
}
$dayName = $dayNames[$Weekday]

# ------------------------------------------------------------ 管理员权限检查
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if ($RunAsSystem -and -not $isAdmin) {
    throw "以 -RunAsSystem 注册需要管理员权限。请右键以管理员身份打开 PowerShell 后重试。"
}

Write-Host "[install_task] 项目根   : $ProjectRoot"
Write-Host "[install_task] 任务名称 : $TaskName"
Write-Host "[install_task] 触发时机 : 每周$dayName $('{0:D2}:{1:D2}' -f $Hour, $Minute)"
Write-Host "[install_task] 附加触发 : $(if ($RunAsSystem) { '开机时（SYSTEM）' } else { '登录时（当前用户）' })"

# -------------------------------------------------------------------- 动作
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $runner) `
    -WorkingDirectory $ProjectRoot

# -------------------------------------------------------------------- 触发器
$triggers = @()

if ($RunAsSystem) {
    $startupTrigger = New-ScheduledTaskTrigger -AtStartup
    $startupTrigger.Delay = "PT${LogonDelayMinutes}M"
    $triggers += $startupTrigger
}
else {
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $logonTrigger.Delay = "PT${LogonDelayMinutes}M"
    $triggers += $logonTrigger
}

$weeklyTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $dayName `
    -At ([datetime]::Today.AddHours($Hour).AddMinutes($Minute))
$triggers += $weeklyTrigger

# ---------------------------------------------------------------------- 设置
# -StartWhenAvailable 是"关机错过自动补跑"的关键
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -MultipleInstances IgnoreNew

try {
    # New-ScheduledTaskSettingsSet 在老版本 PowerShell 上没有 -WakeToRun 参数，
    # 因此直接设置属性并容忍失败（唤醒不是必须的，失败不影响主体功能）。
    $settings.WakeToRun = $true
}
catch {
    Write-Warning "无法设置 WakeToRun（不影响其他功能）：$($_.Exception.Message)"
}

# -------------------------------------------------------------------- 身份
if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

# -------------------------------------------------------------------- 注册
$description = 'AI Infra 周报自动生成：每周定时抓取 AI Infra 领域新闻，调用大模型总结为 Markdown。'

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description $description `
    -Force | Out-Null

Write-Host ''
Write-Host "[install_task] 注册完成，当前任务配置：" -ForegroundColor Green
Write-Host ('-' * 78)
schtasks /query /tn $TaskName /v /fo LIST
Write-Host ('-' * 78)
Write-Host ''
Write-Host "手动测试：schtasks /run /tn `"$TaskName`"" -ForegroundColor Cyan
Write-Host "查看日志：$ProjectRoot\logs\task-*.log" -ForegroundColor Cyan
Write-Host "卸载任务：py main.py --uninstall-task" -ForegroundColor Cyan

exit 0
