"""PowerShell 脚本的回归守卫。

这里守的是两个**真实踩过**的 Windows 坑，它们都不会被 Python 的单元测试发现，
但只要回归就会让计划任务彻底失效：

1. `.ps1` 若无 BOM，PowerShell 5.1 会按系统 ANSI（中文系统=GBK）解析，
   中文注释直接变乱码并报「数组索引表达式丢失或无效」这类**解析错误**。
2. `run_weekly.ps1` 若不透传退出码，计划任务的失败重试（RestartCount）
   永远不会触发——大模型临时不可用时那一周的报告就永久缺失了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

PS1_FILES = sorted(SCRIPTS_DIR.glob("*.ps1"))

UTF8_BOM = b"\xef\xbb\xbf"

#: 早期踩坑时出现过的乱码特征片段
MOJIBAKE_MARKERS = ("椤", "鐧", "寮", "鍔", "鏂", "锛", "鈥")


def test_ps1_files_exist():
    names = {p.name for p in PS1_FILES}
    assert {"run_weekly.ps1", "install_task.ps1", "uninstall_task.ps1"} <= names


@pytest.mark.parametrize("script", PS1_FILES, ids=lambda p: p.name)
def test_ps1_has_utf8_bom(script: Path):
    """必须带 BOM，否则 PowerShell 5.1 会用 GBK 解析中文字符串。"""
    assert script.read_bytes()[:3] == UTF8_BOM, (
        f"{script.name} 缺少 UTF-8 BOM。PowerShell 5.1 会按系统 ANSI 编码解析，"
        "导致中文变乱码并产生语法错误。请以 utf-8-sig 保存该文件。"
    )


@pytest.mark.parametrize("script", PS1_FILES, ids=lambda p: p.name)
def test_ps1_has_no_mojibake(script: Path):
    text = script.read_text(encoding="utf-8-sig")
    found = [marker for marker in MOJIBAKE_MARKERS if marker in text]
    assert not found, f"{script.name} 疑似编码损坏，发现乱码片段：{found}"


@pytest.mark.parametrize("script", PS1_FILES, ids=lambda p: p.name)
def test_ps1_is_readable_as_utf8(script: Path):
    script.read_text(encoding="utf-8-sig")  # 不应抛 UnicodeDecodeError


def read_script(name: str) -> str:
    return (SCRIPTS_DIR / name).read_text(encoding="utf-8-sig")


def test_runner_propagates_exit_code():
    """退出码透传是计划任务失败重试的前提，必须显式存在。"""
    text = read_script("run_weekly.ps1")
    assert "exit $exitCode" in text


def test_runner_uses_start_process_for_redirects():
    """PS 5.1 的 Tee-Object 没有 -Encoding，会写出 GBK 日志；必须用 Start-Process。"""
    text = read_script("run_weekly.ps1")
    assert "Start-Process" in text
    assert "RedirectStandardOutput" in text
    assert "Tee-Object" not in text


def test_runner_writes_logs_as_utf8_without_bom():
    text = read_script("run_weekly.ps1")
    assert "UTF8Encoding" in text
    assert "AppendAllText" in text


def test_runner_forces_python_utf8_output():
    """不设 PYTHONIOENCODING，Python 重定向输出会退回 GBK。"""
    text = read_script("run_weekly.ps1")
    assert "PYTHONIOENCODING" in text


def test_runner_keeps_powershell_lines_ascii():
    """PowerShell 自己生成的行只用 ASCII，彻底规避编码问题。

    日志行以 '[run_weekly] ' 开头，其后到行尾不应出现非 ASCII 字符。
    """
    offenders: list[str] = []
    for line in read_script("run_weekly.ps1").splitlines():
        stripped = line.strip()
        if "'[run_weekly]" not in stripped and '"[run_weekly]' not in stripped:
            continue
        # 取引号内的文本部分做检查
        for chunk in stripped.split("'"):
            if chunk.startswith("[run_weekly]"):
                if not chunk.isascii():
                    offenders.append(chunk)
    assert not offenders, f"run_weekly.ps1 的日志行含非 ASCII：{offenders}"


def test_runner_sets_project_root_as_working_directory():
    text = read_script("run_weekly.ps1")
    assert "Set-Location" in text
    assert "WorkingDirectory" in text


def test_runner_falls_back_to_system_python():
    text = read_script("run_weekly.ps1")
    assert ".venv" in text
    assert "Get-Command python" in text


def test_runner_fails_loudly_when_python_missing():
    text = read_script("run_weekly.ps1")
    assert "exit 9009" in text


def test_install_script_references_runner():
    text = read_script("install_task.ps1")
    assert "run_weekly.ps1" in text


def test_install_script_enables_start_when_available():
    """这是「关机错过自动补跑」的关键设置。"""
    text = read_script("install_task.ps1")
    assert "StartWhenAvailable" in text


def test_install_script_registers_two_triggers():
    text = read_script("install_task.ps1")
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "-Weekly" in text


def test_install_script_supports_restart_settings():
    text = read_script("install_task.ps1")
    assert "RestartCount" in text
    assert "RestartInterval" in text


def test_install_script_guards_admin_for_system_mode():
    text = read_script("install_task.ps1")
    assert "Administrator" in text
    assert "IsInRole" in text


def test_install_script_prevents_concurrent_runs():
    text = read_script("install_task.ps1")
    assert "IgnoreNew" in text


def test_uninstall_script_is_idempotent():
    """任务不存在时应视为成功，而不是报错。"""
    text = read_script("uninstall_task.ps1")
    assert "exit 0" in text
    assert "Unregister-ScheduledTask" in text


def test_check_sources_script_targets_all_sources():
    text = (SCRIPTS_DIR / "check_sources.py").read_text(encoding="utf-8")
    assert "fetch_all" in text
    assert "limit_sources" in text
