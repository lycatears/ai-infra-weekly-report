"""日志初始化。

- 控制台输出便于交互调试
- 文件输出 ``<log_dir>/app-YYYY-MM-DD.log``，按大小滚动，并按天数清理旧文件
- 计划任务场景下 stdout/stderr 由 ``scripts/run_weekly.ps1`` 另行重定向，
  因此这里的控制台 handler 是可选的
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import date, timedelta
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    *,
    retention_days: int = 30,
    console: bool = True,
    prefix: str = "app",
) -> Path:
    """配置根 logger 并返回日志文件路径。重复调用是幂等的。"""
    global _configured

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{prefix}-{date.today().isoformat()}.log"

    root = logging.getLogger()
    if _configured:
        return log_file

    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(file_handler)

    if console:
        stream = sys.stderr if sys.stderr is not None else sys.stdout
        console_handler = logging.StreamHandler(stream)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(console_handler)

    # 第三方库降噪
    for noisy in ("urllib3", "requests", "openai", "httpx", "httpcore", "feedparser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.info("日志已初始化 -> %s (level=%s)", log_file, level.upper())
    return log_file


def purge_old_logs(log_dir: Path, retention_days: int, *, prefix: str = "app") -> int:
    """删除超过保留期的日志文件，返回删除数量。"""
    if retention_days < 1 or not log_dir.exists():
        return 0

    cutoff = date.today() - timedelta(days=retention_days)
    removed = 0
    for path in log_dir.glob(f"{prefix}-*.log*"):
        # 文件名形如 app-2026-09-25.log / app-2026-09-25.log.1
        parts = path.name.split(".")
        if len(parts) < 2:
            continue
        try:
            file_date = date.fromisoformat(parts[1])
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - 文件被占用等
                logging.getLogger(__name__).debug("无法删除旧日志 %s", path)
    if removed:
        logging.getLogger(__name__).info("已清理 %d 个超过 %d 天的旧日志", removed, retention_days)
    return removed
