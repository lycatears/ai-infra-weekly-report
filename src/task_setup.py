"""定时任务的注册与卸载（Windows 计划任务 / Linux crontab）。

本模块只做「按平台分发 + 参数拼装」，实际逻辑在脚本里：

Windows（PowerShell）
- ``scripts/install_task.ps1``   —— 注册双触发器计划任务
- ``scripts/uninstall_task.ps1`` —— 卸载任务
- ``scripts/run_weekly.ps1``     —— 任务实际执行体

Linux（bash + crontab）
- ``scripts/install_cron.sh``    —— 写入 crontab 条目（幂等）
- ``scripts/uninstall_cron.sh``  —— 移除 crontab 条目（幂等）
- ``scripts/run_weekly.sh``      —— 任务实际执行体
- ``scripts/deploy_linux.sh``    —— 一键部署（建 venv + 装依赖 + 注册）

把脚本独立出来而不是内联在 Python 里，是为了让用户能够：直接读、直接改、
单独在终端里跑起来调试。

两侧的触发策略是一一对应的：

======================  ==========================  ============================
语义                    Windows                     Linux
======================  ==========================  ============================
定时触发                Weekly 触发器               ``<M> <H> * * <DOW>``
开机/登录补跑           AtStartup / AtLogOn         ``@reboot``
「关机错过要补跑」      StartWhenAvailable          ！！无对应机制 ！！
并发保护                MultipleInstances IgnoreNew ``flock -n``
======================  ==========================  ============================

最后一行是**最需要注意的差异**：cron 没有 StartWhenAvailable，关机期间
错过的任务不会自动补跑。因此 Linux 侧额外加了一条周期性轮询
（默认每小时），靠 ``main.py --run`` 自身的幂等判定把漏掉的补上。
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

INSTALL_CRON_SCRIPT = SCRIPTS_DIR / "install_cron.sh"
UNINSTALL_CRON_SCRIPT = SCRIPTS_DIR / "uninstall_cron.sh"

#: 兜底轮询间隔（分钟）。cron 没有 StartWhenAvailable，
#: 靠这条把「周五 22:00 时机器关机」漏掉的那次补上。
DEFAULT_CATCHUP_MINUTES = 60

#: @reboot 之后的延迟秒数，给网络与文件系统留出就绪时间。
DEFAULT_BOOT_DELAY_SECONDS = 120

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


def find_bash() -> str:
    """定位 bash。

    Linux 侧脚本用了数组、``PIPESTATUS`` 等 bash 特性，无法退到 POSIX sh，
    因此在注册前就要明确报错，而不是等 cron 静默失败。
    """
    resolved = shutil.which("bash")
    if resolved:
        return resolved
    raise TaskSetupError(
        "找不到 bash。本套 Linux 脚本依赖 bash 特性（数组、PIPESTATUS），"
        "无法用 POSIX sh 运行。精简镜像可安装：Alpine 用 apk add bash，"
        "Debian/Ubuntu 用 apt install bash。"
    )


def _require_windows() -> None:
    if platform.system() != "Windows":
        raise TaskSetupError(
            f"Windows 计划任务仅支持 Windows，当前系统为 {platform.system()}。"
        )


def _require_script(path: Path) -> None:
    if not path.is_file():
        raise TaskSetupError(f"找不到脚本：{path}")


def _run_powershell(script: Path, arguments: list[str]) -> int:
    """执行 PowerShell 脚本，stdio 直接继承，便于用户实时看到 schtasks 输出。"""
    _require_windows()
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


def _run_bash(script: Path, arguments: list[str]) -> int:
    """执行 bash 脚本，stdio 直接继承，便于用户实时看到 crontab 操作结果。

    显式用 ``bash 脚本`` 而不是 ``./脚本``：既不依赖可执行位
    （从 Windows 复制／解压过来时很容易丢），也不依赖 PATH 里能找到 bash。
    """
    _require_script(script)

    command = [find_bash(), str(script), *arguments]

    logger.info("执行: %s", " ".join(command))
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    except OSError as exc:
        raise TaskSetupError(f"无法启动 bash: {exc}") from exc

    return EXIT_OK if completed.returncode == 0 else EXIT_FAILED


def install_task(config: Any, *, run_as_system: bool = False) -> int:
    """注册定时任务，返回进程退出码。

    按平台分发：Windows → 计划任务，Linux → crontab。
    """
    system = platform.system()
    if system == "Windows":
        return _install_windows(config, run_as_system=run_as_system)
    if system == "Linux":
        return _install_linux(config, run_as_system=run_as_system)

    logger.error("不支持的平台：%s", system)
    print(
        f"[定时任务] 当前平台（{system}）不支持自动注册，"
        "可自行用 cron / launchd 定时调用 `python main.py --run`。",
        file=sys.stderr,
    )
    return EXIT_FAILED


def _install_windows(config: Any, *, run_as_system: bool = False) -> int:
    """注册 Windows 计划任务，返回进程退出码。"""
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
        code = _run_powershell(INSTALL_SCRIPT, arguments)
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


def _install_linux(config: Any, *, run_as_system: bool = False) -> int:
    """通过 ``scripts/install_cron.sh`` 注册 crontab 定时任务。"""
    task_cfg: dict[str, Any] = config.raw.get("task", {})

    if run_as_system:
        logger.warning(
            "Linux 下 --run-as-system 无意义：crontab 是用户级的，"
            "且 @reboot 由 cron 守护进程在开机时触发，未登录也会跑。已忽略该参数。"
        )

    name = str(task_cfg.get("name", "AI-Infra-Weekly-Report"))
    catchup = int(task_cfg.get("catchup_interval_minutes", DEFAULT_CATCHUP_MINUTES))
    boot_delay = int(task_cfg.get("boot_delay_seconds", DEFAULT_BOOT_DELAY_SECONDS))

    arguments = [
        "--task-name",
        name,
        "--weekday",
        str(int(config.schedule_weekday)),
        "--hour",
        str(int(config.schedule_hour)),
        "--minute",
        str(int(config.schedule_minute)),
        "--catchup-minutes",
        str(catchup),
        "--boot-delay",
        str(boot_delay),
    ]

    logger.info(
        "注册 crontab 任务「%s」：每周%d %02d:%02d，兜底轮询 %s 分钟",
        name,
        config.schedule_weekday,
        config.schedule_hour,
        config.schedule_minute,
        catchup if catchup > 0 else "关闭",
    )

    try:
        code = _run_bash(INSTALL_CRON_SCRIPT, arguments)
    except TaskSetupError as exc:
        logger.error("%s", exc)
        print(f"[定时任务] {exc}", file=sys.stderr)
        return EXIT_FAILED

    if code != EXIT_OK:
        logger.error("crontab 注册失败（退出码 %d）", code)
        print(
            "\n[定时任务] crontab 注册失败。常见原因：\n"
            "  1. 系统未安装 cron —— Debian/Ubuntu: sudo apt install cron；"
            "RHEL: sudo dnf install cronie\n"
            "  2. 未安装 bash（本套脚本依赖 bash 特性，无法用 POSIX sh）\n"
            "  3. 项目路径中含单引号或 %（% 是 cron 的保留字符）\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    logger.info("crontab 任务注册成功")
    return EXIT_OK


def uninstall_task(config: Any) -> int:
    """卸载定时任务，返回进程退出码。

    按平台分发：Windows → 计划任务，Linux → crontab。
    """
    system = platform.system()
    if system == "Windows":
        return _uninstall_windows(config)
    if system == "Linux":
        return _uninstall_linux(config)

    logger.error("不支持的平台：%s", system)
    print(f"[定时任务] 当前平台（{system}）不支持自动卸载。", file=sys.stderr)
    return EXIT_FAILED


def _uninstall_windows(config: Any) -> int:
    """卸载 Windows 计划任务，返回进程退出码。"""
    task_name = str(config.raw.get("task", {}).get("name", "AI-Infra-Weekly-Report"))

    # 卸载按配置里的任务名进行；若用户改过配置导致名字不一致，
    # 提示其手动指定（PS 脚本支持 -TaskName 参数）。
    try:
        code = _run_powershell(UNINSTALL_SCRIPT, ["-TaskName", task_name])
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


def _uninstall_linux(config: Any) -> int:
    """通过 ``scripts/uninstall_cron.sh`` 移除 crontab 条目。"""
    task_name = str(config.raw.get("task", {}).get("name", "AI-Infra-Weekly-Report"))

    try:
        code = _run_bash(UNINSTALL_CRON_SCRIPT, ["--task-name", task_name])
    except TaskSetupError as exc:
        logger.error("%s", exc)
        print(f"[定时任务] {exc}", file=sys.stderr)
        return EXIT_FAILED

    if code != EXIT_OK:
        logger.error("crontab 卸载失败（退出码 %d）", code)
        print(
            "\n[定时任务] crontab 卸载失败。可先用 `crontab -l` 查看当前内容，"
            "或手动删除标注了任务名的那个段落。\n",
            file=sys.stderr,
        )
        return EXIT_FAILED

    return EXIT_OK
