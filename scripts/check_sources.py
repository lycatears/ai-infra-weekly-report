"""数据源连通性自检脚本。

用于在不调用大模型、不生成报告的前提下，单独验证抓取层是否正常。

用法::

    python scripts/check_sources.py                # 检查全部启用源
    python scripts/check_sources.py arxiv github   # 只检查指定源
    python scripts/check_sources.py --show 30      # 每个源多打印几条样例

退出码：0 表示至少抓到一个候选；1 表示所有源都没有产出。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.fetchers import fetch_all  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402
from src.schedule import decide  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Infra 周报数据源自检")
    parser.add_argument("sources", nargs="*", help="只检查这些源（默认全部启用源）")
    parser.add_argument("--show", type=int, default=8, help="每个源打印多少条样例")
    parser.add_argument("--config", help="指定配置文件路径")
    args = parser.parse_args()

    config = load_config(Path(args.config) if args.config else None)
    setup_logging(config.log_dir, config.log_level, retention_days=config.log_retention_days)

    now = datetime.now(config.tz)
    decision = decide(now, config)
    start_utc = decision.covered_from.astimezone(timezone.utc)
    end_utc = decision.covered_to.astimezone(timezone.utc)

    print("=" * 74)
    print(f"当前时刻: {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"抓取窗口: {decision.covered_from:%Y-%m-%d %H:%M} ~ {decision.covered_to:%Y-%m-%d %H:%M}")
    print(f"目标周五: {decision.target_friday}  判定: {decision.reason.value}")
    print("=" * 74)

    items, stats = fetch_all(
        config,
        start_utc,
        end_utc,
        limit_sources=args.sources or None,
    )

    print("-" * 74)
    print(f"{'数据源':<14}{'命中':>6}")
    print("-" * 74)
    for name, count in stats.items():
        flag = "" if count else "   <- 无产出"
        print(f"{name:<14}{count:>6}{flag}")
    print("-" * 74)
    print(f"{'合计':<14}{len(items):>6}")
    print("=" * 74)

    if args.show:
        by_source: dict[str, list] = {}
        for item in items:
            by_source.setdefault(item.source, []).append(item)
        for name, group in by_source.items():
            print(f"\n### {name}（前 {min(args.show, len(group))} / {len(group)} 条）")
            for item in group[: args.show]:
                stamp = f"{item.published_at.astimezone(config.tz):%Y-%m-%d %H:%M}"
                print(f"  {stamp}  score={item.raw_score:>8.1f}  {item.title[:64]}")
                print(f"            {item.url[:96]}")

    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
