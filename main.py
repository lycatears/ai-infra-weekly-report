"""AI Infra 周报自动生成 —— 命令行入口。

用法速查::

    python main.py --check                       # 只判定「是否该生成」，不联网
    python main.py --check --at "2026-09-25 23:00"
    python main.py --no-llm                      # 抓取+去重+打分，不调用大模型
    python main.py --run                         # 完整流程
    python main.py --run --force                 # 忽略存在性检查
    python main.py --run --dry-run               # 完整流程但不写 history
    python main.py --install-task                # 注册定时任务（Windows 计划任务 / Linux crontab）
    python main.py --uninstall-task              # 卸载定时任务

退出码::

    0  成功（包含「跳过」这种正常结果）
    1  运行时失败（抓取全部失败、大模型不可用等）
    2  配置非法
    3  未预期的内部错误
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from src import __version__
from src.config import EXIT_CONFIG_ERROR, ConfigError, ensure_runtime_dirs, load_config
from src.logging_setup import purge_old_logs, setup_logging
from src.schedule import decide

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_UNEXPECTED = 3

logger = logging.getLogger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AI Infra 周报自动生成（每周五 22:00 定时 + 开机补跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run",
        action="store_true",
        help="执行完整流程：抓取 -> 去重 -> 大模型总结 -> 生成报告",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="只判定本次是否应当生成报告，不联网、不写文件",
    )
    mode.add_argument(
        "--install-task",
        action="store_true",
        help="注册定时任务：Windows 计划任务 / Linux crontab（按当前系统自动选择）",
    )
    mode.add_argument("--uninstall-task", action="store_true", help="卸载定时任务")

    parser.add_argument("--no-llm", action="store_true", help="跳过调用大模型，仅输出候选清单（需与 --run 同用）")
    parser.add_argument("--force", action="store_true", help="忽略「报告已存在」检查，强制生成")
    parser.add_argument("--dry-run", action="store_true", help="跑完整流程但不写入 state/history.json")
    parser.add_argument("--at", metavar="DATETIME", help="用指定时刻替代当前时间来判定（如 '2026-09-25 23:00'）")
    parser.add_argument("--config", metavar="PATH", help="指定配置文件路径（默认 ./config.yaml）")
    parser.add_argument(
        "--limit-sources",
        metavar="A,B",
        help="只启用指定数据源，便于小样本联调（如 arxiv,github）",
    )
    parser.add_argument(
        "--run-as-system",
        action="store_true",
        help="与 --install-task 同用：注册为 SYSTEM + 开机触发（仅 Windows 有效）",
    )
    parser.add_argument("--log-level", help="覆盖 app.log_level")
    return parser


def _parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ConfigError(f"--at 无法解析时间: {value!r}（期望 'YYYY-MM-DD HH:MM'）")


def _print_check_report(config, decision, now_local) -> None:
    sources = config.enabled_sources()
    print("=" * 68)
    print(f"当前时刻   : {now_local:%Y-%m-%d %H:%M:%S %Z} ({config.timezone_name})")
    print(f"配置文件   : {config.source_path}")
    print(f"输出目录   : {config.output_dir}")
    print(f"调度设置   : 每周{'一二三四五六日'[config.schedule_weekday - 1]} "
          f"{config.schedule_hour:02d}:{config.schedule_minute:02d}"
          f"（可回溯 {config.lookback_weeks} 周，补跑开关={config.catchup_on_start}）")
    print(f"数据源     : {', '.join(sources) if sources else '（无）'}")
    print(f"大模型     : {'已配置 key' if config.llm_configured else '未配置 key（--run 会失败）'}")
    print("-" * 68)
    print(decision.describe())
    print("=" * 68)


def _cmd_check(config, args) -> int:
    now = _parse_at(args.at) or datetime.now(config.tz)
    decision = decide(now, config, force=args.force)
    _print_check_report(config, decision, now.astimezone(config.tz))
    return EXIT_OK


def _cmd_run(config, args) -> int:
    # 延迟导入：--check / --install-task 无需加载抓取与网络依赖
    from src.pipeline import run_pipeline

    now = _parse_at(args.at) or datetime.now(config.tz)
    force = args.force
    decision = decide(now, config, force=force)

    if not decision.should_generate and not force:
        logger.info("无需生成：%s（%s）", decision.reason.value, decision.detail)
        print(decision.describe())
        return EXIT_OK

    assert decision.target_friday is not None
    assert decision.covered_from is not None and decision.covered_to is not None

    limit = [s.strip() for s in args.limit_sources.split(",") if s.strip()] if args.limit_sources else None

    return run_pipeline(
        config,
        target_friday=decision.target_friday,
        covered_from=decision.covered_from,
        covered_to=decision.covered_to,
        report_path=decision.report_path,
        skip_llm=args.no_llm,
        dry_run=args.dry_run,
        limit_sources=limit,
    )


def _cmd_task(config, args, *, install: bool) -> int:
    from src.task_setup import install_task, uninstall_task

    if install:
        return install_task(config, run_as_system=args.run_as_system)
    return uninstall_task(config)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 未指定模式时默认打印帮助
    if not any((args.run, args.check, args.install_task, args.uninstall_task)):
        parser.print_help()
        return EXIT_OK

    try:
        config = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.log_level:
        config.raw["app"]["log_level"] = args.log_level.upper()

    ensure_runtime_dirs(config)
    setup_logging(
        config.log_dir,
        config.log_level,
        retention_days=config.log_retention_days,
    )
    purge_old_logs(config.log_dir, config.log_retention_days)

    logger.info(
        "启动 v%s | mode=%s | config=%s",
        __version__,
        "install-task" if args.install_task
        else "uninstall-task" if args.uninstall_task
        else "check" if args.check
        else "run",
        config.source_path,
    )

    try:
        if args.check:
            return _cmd_check(config, args)
        if args.run:
            return _cmd_run(config, args)
        if args.install_task:
            return _cmd_task(config, args, install=True)
        if args.uninstall_task:
            return _cmd_task(config, args, install=False)
    except ConfigError as exc:
        logger.error("配置错误: %s", exc)
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("被用户中断")
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # noqa: BLE001 - 顶层兜底
        logger.exception("未预期的内部错误: %s", exc)
        return EXIT_UNEXPECTED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
