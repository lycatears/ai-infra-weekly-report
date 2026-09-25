<#
.SYNOPSIS
    AI Infra 周报生成任务的实际执行体（由 Windows 计划任务调用）。

.DESCRIPTION
    计划任务本身只负责"按时启动 PowerShell 脚本"，真正的环境准备都在这里：

    1. 切换到项目根目录——计划任务的默认工作目录是 System32，
       不切目录会导致相对路径（config.yaml / logs / 输出目录）全部失效。
    2. 优先使用项目内的 .venv 解释器，回退到 PATH 中的 python。
       固定用 .venv 可以避免"系统 Python 升级后任务静默失效"。
    3. stdout/stderr 同时写日志文件，便于事后排查。
    4. **严格透传退出码**。这一点至关重要：计划任务的「失败后重试」
       (RestartCount) 只在退出码非 0 时才会触发，如果这里吞掉错误码，
       大模型临时不可用就不会重试，那一周的报告就永久缺失了。

.PARAMETER Force
    传给 main.py --force，忽略「报告已存在」检查。

.PARAMETER DryRun
    传给 main.py --dry-run，跑完整流程但不更新 history.json。

.PARAMETER NoLlm
    传给 main.py --no-llm，只验证抓取与去重链路，不调用大模型。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun,
    [switch]$NoLlm
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

# ---------------------------------------------------------------- 选择解释器
$venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
}
else {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-Error "找不到 Python 解释器。请先创建虚拟环境：python -m venv .venv"
        exit 9009
    }
    $python = $command.Source
    Write-Warning "未找到 .venv，回退使用系统 Python：$python"
}

# -------------------------------------------------------------------- 日志
# 编码说明（踩过的坑）：
#   PowerShell 5.1 写文件的默认编码是系统 ANSI（中文系统上即 GBK），
#   直接 Add-Content 含中文的内容会产生乱码。因此本脚本：
#     1. 由 PowerShell 自己生成的行**只用 ASCII**，彻底规避编码问题；
#     2. 子进程输出先重定向到临时文件（配合 PYTHONIOENCODING=utf-8），
#        再用 .NET 以「UTF-8 无 BOM」追加进日志；
#     3. 子进程的完整日志本来就在 logs\app-*.log（由 Python 写 UTF-8）。
#   查看任务日志请显式指定编码：
#     Get-Content logs\task-YYYYMMDD.log -Encoding utf8
$logDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ('task-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-LogLine {
    param([string]$Text)
    [System.IO.File]::AppendAllText($logFile, $Text + [Environment]::NewLine, $utf8NoBom)
}

# ------------------------------------------------------------------ 参数拼装
$cliArgs = @('main.py', '--run')
if ($Force)  { $cliArgs += '--force' }
if ($DryRun) { $cliArgs += '--dry-run' }
if ($NoLlm)  { $cliArgs += '--no-llm' }

$startedAt = Get-Date
Write-LogLine ''
Write-LogLine ('=' * 78)
Write-LogLine ('[run_weekly] START {0:yyyy-MM-dd HH:mm:ss}' -f $startedAt)
Write-LogLine ('[run_weekly] ROOT  {0}' -f $ProjectRoot)
Write-LogLine ('[run_weekly] CMD   {0} {1}' -f $python, ($cliArgs -join ' '))

# -------------------------------------------------------------------- 执行
# 必须显式声明 UTF-8：Python 在输出被重定向时会退回 locale 编码（GBK），
# 那样中文日志就与 UTF-8 的日志文件不兼容了。
$env:PYTHONIOENCODING = 'utf-8'

$stdoutFile = [System.IO.Path]::GetTempFileName()
$stderrFile = [System.IO.Path]::GetTempFileName()
$exitCode = 0

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
try {
    # 用 Start-Process 而不是管道：既避免 PowerShell 把原生命令的 stderr
    # 包装成 NativeCommandError 噪音，又能拿到真实的退出码。
    $proc = Start-Process -FilePath $python -ArgumentList $cliArgs `
        -WorkingDirectory $ProjectRoot -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    $exitCode = $proc.ExitCode
}
finally {
    $stopwatch.Stop()
    foreach ($file in @($stdoutFile, $stderrFile)) {
        if (Test-Path -LiteralPath $file) {
            $text = [System.IO.File]::ReadAllText($file, $utf8NoBom)
            if ($text) {
                [System.IO.File]::AppendAllText($logFile, $text, $utf8NoBom)
                Write-Host $text
            }
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($null -eq $exitCode) { $exitCode = 0 }

Write-LogLine (
    '[run_weekly] END   {0:yyyy-MM-dd HH:mm:ss} | EXIT {1} | ELAPSED {2:n1}s' -f
    (Get-Date), $exitCode, $stopwatch.Elapsed.TotalSeconds
)

switch ($exitCode) {
    0       { Write-Host '[run_weekly] OK' -ForegroundColor Green }
    1       { Write-Host '[run_weekly] FAILED: runtime error (nothing fetched / LLM unavailable)' -ForegroundColor Red }
    2       { Write-Host '[run_weekly] FAILED: invalid config.yaml' -ForegroundColor Red }
    default { Write-Host "[run_weekly] FAILED: unexpected exit code $exitCode" -ForegroundColor Red }
}

# 必须透传退出码，否则计划任务的失败重试（RestartCount）不会生效
exit $exitCode
