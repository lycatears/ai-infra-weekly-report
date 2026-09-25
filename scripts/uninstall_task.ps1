<#
.SYNOPSIS
    卸载「AI Infra 周报」Windows 计划任务。

.PARAMETER TaskName
    要卸载的任务名称，默认 AI-Infra-Weekly-Report。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AI-Infra-Weekly-Report'
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "[uninstall_task] 未找到任务「$TaskName」，无需卸载。" -ForegroundColor Yellow
    exit 0
}

Write-Host "[uninstall_task] 正在卸载任务「$TaskName」..."
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

$still = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($still) {
    Write-Error "[uninstall_task] 卸载失败，任务仍然存在（可能需要管理员权限）。"
    exit 1
}

Write-Host "[uninstall_task] 已卸载。" -ForegroundColor Green
Write-Host "[uninstall_task] 注意：已生成的历史报告与 state/history.json 均已保留。" -ForegroundColor Cyan

exit 0
