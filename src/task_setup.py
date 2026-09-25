"""Windows 计划任务的注册与卸载。

本模块只做「参数拼装 + 调用 PowerShell」，实际逻辑在：

- ``scripts/install_task.ps1``   —— 注册双触发器任务
- ``scripts/uninstall_task.ps1`` —— 卸载任务
- ``scripts/run_weekly.ps1``     —— 任务实际执行体（被 install 脚本引用）

把 PowerShell 脚本独立出来而不是内联在 Python 里，是为了让用户能够：
直接读、直接改、单独用管理员 PowerShell 运行调试。
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

INSTALL_SCRIPT = SCRIPTS_DIR / "install_task.ps1"
UNINSTALL_SCRIPT = SCRIPTS_DIR / "uninstall_task.ps1"

#: ISO 星期编号 -> PowerShell 计划任务的英文星期名
DAY_NAMES: dict[int, str] = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",
}

EXIT_OK = 0
EXIT_FAILED = 1


class TaskSetupError(RuntimeError):
    """计划任务操作失败。"""


def find_powershell() -> str:
    """定位 PowerShell（优先 Windows PowerShell，回退 PowerShell 7）。"""
    for candidate in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise TaskSetupError(
        "找不到 PowerShell。请确认系统已安装 Windows PowerShell 或 PowerShell 7。"
    )


def _check_platform() -> None:
    if platform.system() != "Windows":
        raise TaskSetupError(
            f"计划任务注册仅支持 Windows，当前系统为 {platform.system()}。"
            "其他平台可自行用 cron / launchd 调用 `python main.py --run`。"
        )


def _require_script(path: Path) -> None:
    if not path.is_file():
        raise TaskSetupError(f"找不到脚本：{path}")


def _run(script: Path, arguments: list[str]) -> int:
    """执行 PowerShell 脚本，stdio 直接继承，便于用户实时看到 schtasks 输出。"""
    _check_platform()
    _require_script(script)

    powershell = find_powershell()
    command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    ]

    logger.info("执行: %s", " ".join(command))
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    except OSError as exc:
        raise TaskSetupError(f"无法启动 PowerShell: {exc}") from exc

    return EXIT_OK if completed.returncode == 0 else EXIT_FAILED


def install_task(config: Any, *, run_as_system: bool = False) -> int:
    """注册计划任务，返回进程退出码。"""
    task_cfg: dict[str, Any] = config.raw.get("task", {})

    if run_as_system:
        # 命令行开关优先级高于配置文件
        task_cfg = {**task_cfg, "run_as_system": True}

    use_system = bool(task_cfg.get("run_as_system", False))
    weekday = int(config.schedule_weekday)

    arguments = [
        "-TaskName",
        str(task_cfg.get("name", "AI-Infra-Weekly-Report")),
        "-Weekday",
        str(weekday),
        "-Hour",
        str(config.schedule_hour),
        "-Minute",
        str(config.schedule_minute),
        "-LogonDelayMinutes",
        str(int(task_cfg.get("logon_delay_minutes", 2))),
        "-ExecutionTimeLimitMinutes",
        str(int(task_cfg.get("execution_time_limit_minutes", 120))),
        "-RestartCount",
        str(int(task_cfg.get("restart_count", 2))),
        "-RestartIntervalMinutes",
        str(int(task_cfg.get("restart_interval_minutes", 10))),
    ]
    if use_system:
        arguments.append("-RunAsSystem")

    logger.info(
        "注册计划任务「%s」：每周%s %02d:%02d，附加触发=%s",
        task_cfg.get("name"),
        DAY_NAMES.get(weekday, "?"),
        config.schedule_hour,
        config.schedule_minute,
        "开机（SYSTEM）" if use_system else "登录（当前用户）",
    )

    try:
        code = _run(INSTALL_SCRIPT, arguments)
    except TaskSetupError as exc:
        logger.error("%s", exc)
        print(f"[计划任务] {exc}", file=sys.stderr)
        return EXIT_FAILED

    if code != EXIT_OK:
        logger.error("计划任务注册失败（PowerShell 退出码 %d）", code)
        print(
            "\n[计划任务] 注册失败。常见原因：\n"
            "  1. 需要管理员权限 —— 请用「以管理员身份运行」打开 PowerShell 后重试\n"
            "  2. 组策略限制了计划任务创建\n"
            "  3. 任务名已被占用 —— 可先执行 py main.py --uninstall-task\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    logger.info("计划任务注册成功")
    return EXIT_OK


def uninstall_task(config: Any) -> int:
    """卸载计划任务，返回进程退出码。"""
    task_name = str(config.raw.get("task", {}).get("name", "AI-Infra-Weekly-Report"))

    # 卸载按配置里的任务名进行；若用户改过配置导致名字不一致，
    # 提示其手动指定（PS 脚本支持 -TaskName 参数）。
    try:
        code = _run(UNINSTALL_SCRIPT, ["-TaskName", task_name])
    except TaskSetupError as exc:
        logger.error("%s", exc)
        print(f"[计划任务] {exc}", file=sys.stderr)
        return EXIT_FAILED

    if code != EXIT_OK:
        logger.error("计划任务卸载失败（PowerShell 退出码 %d）", code)
        print(
            "\n[计划任务] 卸载失败。若任务是以 SYSTEM 身份注册的，"
            "需要管理员 PowerShell 才能删除。\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    return EXIT_OK
