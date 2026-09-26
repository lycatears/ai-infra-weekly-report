"""定时任务脚本的回归守卫（Windows PowerShell + Linux bash）。

这里守的都是**真实踩过**、且不会被 Python 单元测试发现的坑。
只要回归，定时任务就会静默失效或静默写乱码。

Windows（.ps1）
1. 若无 BOM，PowerShell 5.1 会按系统 ANSI（中文系统=GBK）解析，
   中文注释直接变乱码并报「数组索引表达式丢失或无效」这类**解析错误**。
2. ``run_weekly.ps1`` 若不透传退出码，计划任务的失败重试（RestartCount）
   永远不会触发——大模型临时不可用时那一周的报告就永久缺失了。

Linux（.sh）
3. **行尾必须是 LF**。Windows 上创建的 .sh 很容易带上 CRLF，到 Linux 上
   bash 会报 ``$'\\r': command not found``，错误信息完全指不到真正原因。
4. **不能有 BOM**。BOM 会破坏 shebang，内核看到的是 ``\ufeff#!/usr/bin/env bash``。
   注意这里和要求**正好相反**：.ps1 必须有 BOM，.sh 必须没有。
5. ``run_weekly.sh`` 必须自己设好 PYTHONUTF8 —— cron 通常不设 LANG，
   Python 会退回 POSIX locale(ASCII)，一打印中文就 UnicodeEncodeError 崩溃。
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


# ===========================================================================
# Linux 侧：bash 脚本的回归守卫
#
# 两条最关键的约束（都是静默失败，看不到任何提示）：
#   * CRLF 行尾 -> Linux 上报 `$'\r': command not found`
#   * UTF-8 BOM -> shebang 失效，内核找不到解释器
# 注意与 .ps1 的要求**正好相反**：.ps1 必须有 BOM，.sh 必须没有。
# ===========================================================================

SH_FILES = sorted(SCRIPTS_DIR.glob("*.sh"))

UTF8_BOM_BYTES = b"\xef\xbb\xbf"

REQUIRED_SH = {"run_weekly.sh", "install_cron.sh", "uninstall_cron.sh", "deploy_linux.sh"}


def test_sh_files_exist():
    names = {p.name for p in SH_FILES}
    assert REQUIRED_SH <= names, f"缺少 Linux 脚本: {REQUIRED_SH - names}"


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_has_no_utf8_bom(script: Path):
    """BOM 会让 shebang 失效：内核看到的是 b'\xef\xbb\xbf#!/usr/bin/env bash'。"""
    assert script.read_bytes()[:3] != UTF8_BOM_BYTES, (
        f"{script.name} 带了 UTF-8 BOM，会导致 shebang 失效（Linux 报 No such file or directory）。"
        "请以无 BOM 的 UTF-8 保存。"
    )


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_uses_lf_line_endings(script: Path):
    """CRLF 是 Windows 上创建 .sh 时最容易带上的问题，且在编辑器里看不出来。"""
    raw = script.read_bytes()
    assert b"\r\n" not in raw, (
        f"{script.name} 含 CRLF 行尾，Linux 上 bash 会报 `$'\\r': command not found`。"
        "修复：python scripts/normalize_shell_scripts.py"
    )
    assert b"\r" not in raw, f"{script.name} 含孤立的 CR 字符"


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_is_readable_as_utf8(script: Path):
    script.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_has_no_mojibake(script: Path):
    text = script.read_text(encoding="utf-8")
    found = [marker for marker in MOJIBAKE_MARKERS if marker in text]
    assert not found, f"{script.name} 疑似编码损坏，发现乱码片段：{found}"


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_starts_with_bash_shebang(script: Path):
    first_line = script.read_bytes().split(b"\n", 1)[0]
    assert first_line in (b"#!/usr/bin/env bash", b"#!/bin/bash"), first_line


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_fails_fast(script: Path):
    """set -euo pipefail 是这套脚本「出错就停」的基础。"""
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")


def read_sh(name: str) -> str:
    return (SCRIPTS_DIR / name).read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """剔除以 # 开头的整行注释。

    「断言脚本里不出现某个用法」时不能直接全文匹配：注释里往往正是在用
    这个反面例子来提醒后来者，直接匹配会误报。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ------------------------------------------------------------- run_weekly.sh
def test_runner_sh_propagates_exit_code():
    """透传退出码，否则外层的监控/包装脚本无法感知失败。"""
    assert 'exit "$EXIT_CODE"' in read_sh("run_weekly.sh")


def test_runner_sh_forces_python_utf8():
    """cron 通常不设 LANG，Python 会退回 POSIX locale(ASCII) 并直接崩溃。

    只靠 LC_ALL 不够——系统上可能根本没有可用的 UTF-8 locale，
    所以必须依赖 PYTHONUTF8=1（PEP 540）。
    """
    text = read_sh("run_weekly.sh")
    assert "PYTHONUTF8=1" in text
    assert "PYTHONIOENCODING=utf-8" in text


def test_runner_sh_changes_into_project_root():
    """cron 的工作目录是 $HOME，不切目录则相对路径全部失效。"""
    assert 'cd -- "$PROJECT_ROOT"' in read_sh("run_weekly.sh")


def test_runner_sh_sets_explicit_path():
    """cron 的 PATH 只有 /usr/bin:/bin。"""
    assert "export PATH=" in read_sh("run_weekly.sh")


def test_runner_sh_guards_against_concurrent_runs():
    """cron 不会因为上一次还在跑就跳过本次，必须自己上锁。"""
    assert "flock -n" in read_sh("run_weekly.sh")


def test_runner_sh_keeps_generated_lines_ascii():
    """与 run_weekly.ps1 同一约定：脚本自己生成的行只用 ASCII。

    ASCII 行在任何 locale 下都不可能被重新编码，从根上杜绝乱码。
    """
    offenders: list[str] = []
    marker = "[run_weekly]"
    for line in read_sh("run_weekly.sh").splitlines():
        if marker not in line:
            continue
        tail = line.split(marker, 1)[1]
        # 截到第一个引号为止：再往后是 shell 变量拼接，不属于文案
        content = tail.split("'", 1)[0].split('"', 1)[0]
        if not content.isascii():
            offenders.append(content.strip())
    assert not offenders, f"run_weekly.sh 生成的日志行含非 ASCII：{offenders}"


# ------------------------------------------------------------ install_cron.sh
def test_install_cron_uses_marker_block_for_idempotency():
    text = read_sh("install_cron.sh")
    assert "MARK_BEGIN" in text and "MARK_END" in text
    assert "strip_block" in text


def test_install_cron_tolerates_missing_crontab():
    """crontab -l 在「尚无 crontab」时以退出码 1 结束，
    不加 `|| true` 会被 set -e 直接打断——最经典的踩坑点。"""
    assert "crontab -l 2>/dev/null || true" in read_sh("install_cron.sh")


def test_install_cron_maps_iso_weekday_to_cron_dow():
    """ISO 7=周日 必须映射成 cron 0=周日。

    否则配置里写 7（周日）会变成「每周第 7 天」，而 cron 的 7 与 0 都表示周日，
    表面上能跑，一旦改用 0..6 体系就会静默算错一天。
    """
    text = read_sh("install_cron.sh")
    assert "CRON_DOW=0" in text


def test_install_cron_avoids_locale_dependent_cut():
    """`cut -c` 在 C locale 下按**字节**切，会把中文星期名切成乱码。

    断言前必须剔除注释行：本脚本的注释里正是用 `cut -c` 来举例说明
    「为什么不要用它」，直接全文匹配会误报。
    """
    code = code_only(read_sh("install_cron.sh"))
    assert "cut -c" not in code
    assert "DAY_NAMES=(" in code


def test_install_cron_quotes_paths_for_sh():
    """cron 把整行交给 sh，路径必须单引号包裹才能容忍空格等字符。"""
    assert "shq()" in read_sh("install_cron.sh")


def test_install_cron_rejects_percent_in_paths():
    """% 是 cron 的保留字符（会被当成换行符），必须提前拦住并给出提示。"""
    assert "cron 保留字符" in read_sh("install_cron.sh")


def test_install_cron_has_dry_run():
    """--dry-run 让用户可以在不修改 crontab 的前提下检查将要写入的内容。"""
    assert "--dry-run" in read_sh("install_cron.sh")


# ---------------------------------------------------------- uninstall_cron.sh
def test_uninstall_cron_is_idempotent():
    """任务不存在时应视为成功，而不是报错（与 uninstall_task.ps1 一致）。"""
    text = read_sh("uninstall_cron.sh")
    assert "无需卸载" in text
    assert "exit 0" in text


# ------------------------------------------------------------ deploy_linux.sh
def test_deploy_linux_is_not_root_only():
    text = read_sh("deploy_linux.sh")
    assert "python3-venv" in text, "应给出 Debian/Ubuntu 缺 venv 模块时的可操作提示"


def test_deploy_linux_delegates_schedule_to_config():
    """星期/时刻只能以 config.yaml 为准，避免脚本与配置各说各话。"""
    assert "--install-task" in read_sh("deploy_linux.sh")


def test_deploy_linux_checks_python_version():
    assert "MIN_PYTHON" in read_sh("deploy_linux.sh")


def test_deploy_linux_cleans_up_failed_venv():
    """venv 建到一半失败后必须清理干净。

    半成品 .venv 比没有更糟：`run_weekly.sh` 看到 `.venv/bin/python` 可执行
    就会选中它，于是「缺依赖」以退出码 3（未预期错误）的形式报出来，
    真正的原因被掩盖 —— 这个坑在真实部署时踩过一次。
    """
    assert 'rm -rf -- "$VENV_DIR"' in code_only(read_sh("deploy_linux.sh"))


def test_deploy_linux_refuses_windows_venv():
    """`.venv` 里若是 Windows 虚拟环境的布局，必须先报错而不是硬建。

    同目录再建 Linux venv 会覆盖 `pyvenv.cfg`，让 Windows 侧的
    `python.exe` 彻底失效（症状：`No Python at ...`），
    而故障发生在「看起来只是装了个依赖」的表象之下。
    """
    text = code_only(read_sh("deploy_linux.sh"))
    assert "$VENV_DIR/Scripts" in text
    assert "pyvenv.cfg" in text


# ------------------------------------------------------ 行尾修复工具可用性
def test_normalize_tool_exists():
    """这个工具是 CRLF 问题的修复手段，测试报错信息会指向它。"""
    assert (SCRIPTS_DIR / "normalize_shell_scripts.py").is_file()


# ------------------------------------------- PowerShell 与 bash 的约束一致性
def test_ps1_and_sh_have_opposite_bom_requirements():
    """把这对「相反的约束」写进测试，避免以后有人「统一」成一种处理方式。"""
    for script in PS1_FILES:
        assert script.read_bytes()[:3] == UTF8_BOM, f"{script.name} 必须**有** BOM"
    for script in SH_FILES:
        assert script.read_bytes()[:3] != UTF8_BOM_BYTES, f"{script.name} 必须**没有** BOM"
