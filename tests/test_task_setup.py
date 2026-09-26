"""定时任务注册的平台分发与参数拼装。

这里**不**真的去注册任务（那会污染开发机的计划任务／crontab），
而是拦截底层执行函数，断言「传给脚本的参数是否正确」。

守的核心事实是：星期/时刻只能来自 config.yaml，不能有两套真相。
"""

from __future__ import annotations

from typing import Any

import pytest

from src import task_setup
from src.task_setup import (
    DEFAULT_BOOT_DELAY_SECONDS,
    DEFAULT_CATCHUP_MINUTES,
    EXIT_FAILED,
    EXIT_OK,
)


class Recorder:
    """替换 `_run_powershell` / `_run_bash`，只记录调用参数。"""

    def __init__(self, exit_code: int = EXIT_OK) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, list[str]]] = []
        self.scripts: list[Any] = []

    def __call__(self, script, arguments: list[str]) -> int:
        self.scripts.append(script)
        self.calls.append((script.name, list(arguments)))
        return self.exit_code


def args_of(recorder: Recorder) -> list[str]:
    assert len(recorder.calls) == 1, recorder.calls
    return recorder.calls[0][1]


def value_of(arguments: list[str], flag: str) -> str:
    idx = arguments.index(flag)
    return arguments[idx + 1]


# --------------------------------------------------------------- 平台分发
def test_install_dispatches_to_windows(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Windows")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_powershell", recorder)

    assert task_setup.install_task(config) == EXIT_OK
    assert recorder.calls[0][0] == "install_task.ps1"


def test_install_dispatches_to_linux(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    assert task_setup.install_task(config) == EXIT_OK
    assert recorder.calls[0][0] == "install_cron.sh"


def test_uninstall_dispatches_to_linux(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    assert task_setup.uninstall_task(config) == EXIT_OK
    assert recorder.calls[0][0] == "uninstall_cron.sh"


def test_uninstall_dispatches_to_windows(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Windows")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_powershell", recorder)

    assert task_setup.uninstall_task(config) == EXIT_OK
    assert recorder.calls[0][0] == "uninstall_task.ps1"


@pytest.mark.parametrize("system", ["Darwin", "FreeBSD"])
def test_unsupported_platform_fails_clearly(config, monkeypatch, system, capsys):
    """macOS 等平台应明确失败，而不是抛未捕获异常。"""
    monkeypatch.setattr(task_setup.platform, "system", lambda: system)

    assert task_setup.install_task(config) == EXIT_FAILED
    assert system in capsys.readouterr().err


# ------------------------------------------------------- Linux 参数拼装
def test_linux_receives_schedule_from_config(config, monkeypatch, cfg):
    """星期/时刻的唯一真相是 config.yaml，不能在脚本里再写一份默认值。"""
    cfg("schedule.weekday", 3)
    cfg("schedule.hour", 7)
    cfg("schedule.minute", 45)

    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    assert task_setup.install_task(config) == EXIT_OK

    arguments = args_of(recorder)
    assert value_of(arguments, "--weekday") == "3"
    assert value_of(arguments, "--hour") == "7"
    assert value_of(arguments, "--minute") == "45"


def test_linux_uses_task_name_from_config(config, monkeypatch, cfg):
    cfg("task.name", "My-Weekly")
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    task_setup.install_task(config)

    assert value_of(args_of(recorder), "--task-name") == "My-Weekly"


def test_linux_catchup_defaults(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    task_setup.install_task(config)

    arguments = args_of(recorder)
    assert value_of(arguments, "--catchup-minutes") == str(DEFAULT_CATCHUP_MINUTES)
    assert value_of(arguments, "--boot-delay") == str(DEFAULT_BOOT_DELAY_SECONDS)


def test_linux_catchup_overridable_from_config(config, monkeypatch, cfg):
    """兜底轮询是 Linux 特有的兜底机制（cron 没有 StartWhenAvailable），
    应允许用户通过配置调整或关闭。"""
    cfg("task.catchup_interval_minutes", 0)
    cfg("task.boot_delay_seconds", 30)

    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    task_setup.install_task(config)

    arguments = args_of(recorder)
    assert value_of(arguments, "--catchup-minutes") == "0"
    assert value_of(arguments, "--boot-delay") == "30"


def test_linux_ignores_run_as_system_with_warning(config, monkeypatch, caplog):
    """crontab 是用户级的，@reboot 未登录也会跑，因此该参数在 Linux 上无意义。"""
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    with caplog.at_level("WARNING", logger="src.task_setup"):
        assert task_setup.install_task(config, run_as_system=True) == EXIT_OK

    assert any("无意义" in record.message for record in caplog.records)
    assert "-RunAsSystem" not in args_of(recorder)


def test_linux_uninstall_passes_task_name(config, monkeypatch, cfg):
    cfg("task.name", "My-Weekly")
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_bash", recorder)

    task_setup.uninstall_task(config)

    assert value_of(args_of(recorder), "--task-name") == "My-Weekly"


def test_linux_failure_returns_failed(config, monkeypatch, capsys):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(task_setup, "_run_bash", Recorder(exit_code=EXIT_FAILED))

    assert task_setup.install_task(config) == EXIT_FAILED
    # 失败提示里要给出可操作的原因（cron 没装 / 没有 bash / 路径含 %）
    assert "cron" in capsys.readouterr().err


# ------------------------------------------------------- Windows 参数拼装
def test_windows_receives_schedule_from_config(config, monkeypatch, cfg):
    cfg("schedule.weekday", 2)
    cfg("schedule.hour", 6)
    cfg("schedule.minute", 5)

    monkeypatch.setattr(task_setup.platform, "system", lambda: "Windows")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_powershell", recorder)

    task_setup.install_task(config)

    arguments = args_of(recorder)
    assert value_of(arguments, "-Weekday") == "2"
    assert value_of(arguments, "-Hour") == "6"
    assert value_of(arguments, "-Minute") == "5"


def test_windows_run_as_system_flag(config, monkeypatch):
    monkeypatch.setattr(task_setup.platform, "system", lambda: "Windows")
    recorder = Recorder()
    monkeypatch.setattr(task_setup, "_run_powershell", recorder)

    task_setup.install_task(config, run_as_system=True)

    assert "-RunAsSystem" in args_of(recorder)


# ---------------------------------------------------------------- 脚本存在性
def test_all_task_scripts_exist():
    """无论哪个平台，引用的脚本都必须真实存在，否则命令缺失要到运行时才发现。"""
    for script in (
        task_setup.INSTALL_SCRIPT,
        task_setup.UNINSTALL_SCRIPT,
        task_setup.INSTALL_CRON_SCRIPT,
        task_setup.UNINSTALL_CRON_SCRIPT,
    ):
        assert script.is_file(), f"缺少脚本: {script}"
