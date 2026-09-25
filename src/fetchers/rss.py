"""官方博客 RSS / Atom 抓取（feedparser，无需 key）。

发布时间优先取 ``published``，缺失时回退 ``updated``（很多企业博客只有后者）。
直接喂原始字节给 feedparser，避免自己猜编码导致中文标题乱码。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import feedparser

from .base import BaseFetcher, NewsItem, parse_datetime, parse_struct_time, strip_html, truncate

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 600


class RssFetcher(BaseFetcher):
    name = "rss"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        feeds = [f for f in (self.cfg.get("feeds") or []) if isinstance(f, dict) and f.get("url")]
        max_items = int(self.cfg.get("max_items_per_feed", 10))

        items: list[NewsItem] = []
        for feed_cfg in feeds:
            url = str(feed_cfg["url"]).strip()
            name = str(feed_cfg.get("name") or url).strip()

            raw = self.http.get_bytes(url)
            if raw is None:
                logger.warning("[rss] 跳过无法访问的源: %s", name)
                continue

            parsed = feedparser.parse(raw)
            if getattr(parsed, "bozo", 0) and not parsed.entries:
                logger.warning(
                    "[rss] %s 内容无法解析: %s", name, getattr(parsed, "bozo_exception", "未知原因")
                )
                continue

            for entry in parsed.entries[:max_items]:
                item = self._to_item(name, entry)
                if item is not None:
                    items.append(item)

        return items

    # ------------------------------------------------------------------
    @staticmethod
    def _to_item(feed_name: str, entry: Any) -> NewsItem | None:
        title = strip_html(entry.get("title", ""))
        url = str(entry.get("link") or "").strip()
        if not title or not url:
            return None

        published = (
            parse_struct_time(entry.get("published_parsed"))
            or parse_struct_time(entry.get("updated_parsed"))
            or parse_datetime(entry.get("published"))
            or parse_datetime(entry.get("updated"))
        )
        if published is None:
            return None

        summary = strip_html(entry.get("summary", "") or "")
        if not summary:
            for content in entry.get("content") or []:
                if content.get("value"):
                    summary = strip_html(content["value"])
                    break

        return NewsItem(
            source="rss",
            title=title,
            url=url,
            published_at=published,
            summary=truncate(summary or title, SUMMARY_LIMIT),
            raw_score=0.0,
            extra={
                "feed_name": feed_name,
                "kind": "blog",
                "author": strip_html(entry.get("author", "") or ""),
            },
        )
