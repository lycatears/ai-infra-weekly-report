"""pytest 共享 fixture。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import Config, load_config

TZ = ZoneInfo("Asia/Shanghai")

#: 便于测试的一组基准日期（2026-09-25 是周五）
MONDAY = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)
THURSDAY = datetime(2026, 9, 24, 10, 0, tzinfo=TZ)
FRIDAY_BEFORE = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
FRIDAY_TRIGGER = datetime(2026, 9, 25, 22, 0, tzinfo=TZ)
FRIDAY_AFTER = datetime(2026, 9, 25, 23, 0, tzinfo=TZ)
SATURDAY = datetime(2026, 9, 26, 9, 0, tzinfo=TZ)
NEXT_MONDAY = datetime(2026, 9, 28, 10, 0, tzinfo=TZ)

WEEK_FRIDAY = datetime(2026, 9, 25).date()
PREV_FRIDAY = datetime(2026, 9, 18).date()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """在临时目录里用全默认值构造 Config。

    显式传入路径时 `load_config` 不会去读 `config.local.yaml`，
    因此测试环境是干净的、可重复的。
    """
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    loaded = load_config(path)
    # 输出目录指向临时目录，避免测试往仓库里写文件
    loaded.raw["app"]["output_dir"] = str(tmp_path / "out")
    loaded.raw["app"]["state_dir"] = str(tmp_path / "state")
    loaded.raw["app"]["log_dir"] = str(tmp_path / "logs")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    return loaded


@pytest.fixture
def cfg(config: Config):
    """提供一个便捷的配置改写器：`cfg("llm.batch_size", 2)`。"""

    def _set(dotted: str, value):
        node = config.raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        return config

    return _set
