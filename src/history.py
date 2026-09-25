"""跨周去重状态：``state/history.json``。

结构::

    {
      "version": 1,
      "updated_at": "2026-09-25T22:03:11+08:00",
      "entries": [
        {"date": "2026-09-25", "url": "...", "normalized_url": "...", "title_key": "..."}
      ]
    }

关键约定
--------
1. **只有报告成功落盘后才会写入 history**（见 :mod:`src.pipeline`），
   否则一次失败运行会把未发布的内容标记成「已收录」，造成永久漏报。
2. 判定「是否已收录」只用**精确匹配**（规范化 URL 或归一化标题）。
   理由见 :mod:`src.dedupe` 的模块文档——跨周模糊匹配会误杀新版本发布。
3. 文件损坏时不会让程序崩溃：备份为 ``.corrupt-<时间戳>`` 后从空历史继续。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .dedupe import canonical_url
from .fetchers.base import NewsItem, normalise_title
from .io_utils import atomic_write_text

logger = logging.getLogger(__name__)

HISTORY_VERSION = 1
MAX_ENTRIES = 5000


@dataclass
class HistoryEntry:
    """一条已收录记录。"""

    date: str
    """收录该条目的报告所对应的周（目标周五，ISO 格式）。"""

    url: str
    normalized_url: str
    title_key: str

    @classmethod
    def from_item(cls, item: NewsItem, target_date: date, strip_params: Iterable[str]) -> "HistoryEntry":
        return cls(
            date=target_date.isoformat(),
            url=item.url,
            normalized_url=item.extra.get("canonical_url") or canonical_url(item.url, strip_params),
            title_key=normalise_title(item.title),
        )


class History:
    """已收录内容索引。"""

    def __init__(self, path: Path, *, keep_weeks: int = 4, strip_params: Iterable[str] = ()) -> None:
        self.path = path
        self.keep_weeks = max(0, keep_weeks)
        self.strip_params = tuple(strip_params)

        self._entries: list[HistoryEntry] = []
        self._urls: set[str] = set()
        self._titles: set[str] = set()
        self._dirty = False

    # ---------------------------------------------------------------- 载入
    def load(self) -> None:
        self._entries = []
        self._urls = set()
        self._titles = set()

        if not self.path.exists():
            logger.debug("历史文件不存在，从空历史开始: %s", self.path)
            return

        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._quarantine(exc)
            return

        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            self._quarantine("entries 字段不是列表")
            return

        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            normalized = str(raw.get("normalized_url") or "")
            title_key = str(raw.get("title_key") or "")
            if not normalized and not title_key:
                continue
            self._entries.append(
                HistoryEntry(
                    date=str(raw.get("date") or ""),
                    url=str(raw.get("url") or ""),
                    normalized_url=normalized,
                    title_key=title_key,
                )
            )

        self._reindex()
        logger.debug("已载入 %d 条历史记录（%s）", len(self._entries), self.path)

    def _reindex(self) -> None:
        self._urls = {e.normalized_url for e in self._entries if e.normalized_url}
        self._titles = {e.title_key for e in self._entries if e.title_key}

    def _quarantine(self, reason: Any) -> None:
        """历史文件损坏时备份并重置，不让整条流水线失败。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(backup)
            logger.warning("历史文件损坏（%s），已备份为 %s 并从空历史继续", reason, backup.name)
        except OSError:  # pragma: no cover - 备份失败时只能放弃
            logger.warning("历史文件损坏（%s）且备份失败，将从空历史继续", reason)
        self._entries = []
        self._reindex()

    # ---------------------------------------------------------------- 查询
    def __len__(self) -> int:
        return len(self._entries)

    def is_seen(self, item: NewsItem) -> bool:
        """该条是否已在此前周报中收录过（精确匹配）。"""
        normalized = item.extra.get("canonical_url") or canonical_url(item.url, self.strip_params)
        if normalized and normalized in self._urls:
            return True
        title_key = normalise_title(item.title)
        return bool(title_key) and title_key in self._titles

    # ---------------------------------------------------------------- 写入
    def record(self, items: Iterable[NewsItem], target_date: date) -> int:
        """把已发布条目登记进历史（暂存内存，需调用 :meth:`save` 落盘）。"""
        added = 0
        for item in items:
            entry = HistoryEntry.from_item(item, target_date, self.strip_params)
            if entry.normalized_url and entry.normalized_url in self._urls:
                continue
            if entry.title_key and entry.title_key in self._titles:
                continue
            self._entries.append(entry)
            if entry.normalized_url:
                self._urls.add(entry.normalized_url)
            if entry.title_key:
                self._titles.add(entry.title_key)
            added += 1

        if added:
            self._dirty = True
        return added

    def prune(self, reference: date) -> int:
        """删除超出保留窗口的记录。"""
        if self.keep_weeks <= 0:
            removed = len(self._entries)
            self._entries = []
            self._reindex()
            self._dirty = self._dirty or removed > 0
            return removed

        cutoff = (reference - timedelta(weeks=self.keep_weeks)).isoformat()
        kept: list[HistoryEntry] = []
        removed = 0
        for entry in self._entries:
            # 日期缺失或无法比较时保守保留
            if entry.date and entry.date < cutoff:
                removed += 1
                continue
            kept.append(entry)

        # 仍然过长时按日期保留最近的若干条
        if len(kept) > MAX_ENTRIES:
            kept.sort(key=lambda e: e.date, reverse=True)
            removed += len(kept) - MAX_ENTRIES
            kept = kept[:MAX_ENTRIES]

        if removed:
            self._entries = kept
            self._reindex()
            self._dirty = True
            logger.info("历史记录已清理 %d 条（保留窗口 %d 周）", removed, self.keep_weeks)
        return removed

    def save(self) -> None:
        if not self._dirty:
            logger.debug("历史无变化，跳过写入")
            return

        payload = {
            "version": HISTORY_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "entries": [asdict(entry) for entry in self._entries],
        }
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        self._dirty = False
        logger.info("历史已写入 %s（共 %d 条）", self.path, len(self._entries))
