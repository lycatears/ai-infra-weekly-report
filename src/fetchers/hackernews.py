"""Hacker News 抓取（Algolia 搜索 API，无需 key）。

两条路径
--------
1. **关键词搜索**：``/api/v1/search``，按 Algolia 相关性排序（已内含热度权重），
   再用 ``min_points`` 过滤掉噪音
2. **首页热榜**：``tags=front_page``，取本周上过首页的讨论——
   这是「业界真正在关注什么」的最佳单一信号

时间过滤用 ``created_at_i``（epoch 秒）+ ``numericFilters``，在服务端完成，
避免把整周数据拉回本地再筛。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .base import BaseFetcher, NewsItem, parse_datetime, strip_html, truncate

logger = logging.getLogger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"

SUMMARY_LIMIT = 600


class HackerNewsFetcher(BaseFetcher):
    name = "hackernews"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        window = self._numeric_filter(start_utc, end_utc)
        hits_per_page = int(self.cfg.get("hits_per_page", 50))
        min_points = int(self.cfg.get("min_points", 10))

        collected: list[NewsItem] = []
        seen_ids: set[str] = set()

        # --- 关键词搜索 ---
        for query in self.cfg.get("queries") or []:
            if not str(query).strip():
                continue
            payload = self._search(
                {"query": str(query), "tags": "story", "hitsPerPage": hits_per_page, "numericFilters": window}
            )
            self._collect(payload, collected, seen_ids, min_points)
            self._sleep()

        # --- 首页热榜 ---
        if self.cfg.get("include_front_page", True):
            payload = self._search(
                {"tags": "front_page", "hitsPerPage": hits_per_page, "numericFilters": window}
            )
            self._collect(payload, collected, seen_ids, min_points)

        return collected

    # ------------------------------------------------------------------
    @staticmethod
    def _numeric_filter(start_utc: datetime, end_utc: datetime) -> str:
        return f"created_at_i>={int(start_utc.timestamp())},created_at_i<={int(end_utc.timestamp())}"

    def _search(self, params: dict[str, Any]) -> dict[str, Any] | None:
        payload = self.http.get_json(SEARCH_URL, params=params)
        if payload is None:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _collect(
        self,
        payload: dict[str, Any] | None,
        collected: list[NewsItem],
        seen_ids: set[str],
        min_points: int,
    ) -> None:
        if not payload:
            return
        for hit in payload.get("hits") or []:
            if not isinstance(hit, dict):
                continue

            object_id = str(hit.get("objectID") or "").strip()
            if not object_id or object_id in seen_ids:
                continue

            points = hit.get("points")
            points = float(points) if isinstance(points, (int, float)) else 0.0
            if points < min_points:
                continue

            published = parse_datetime(hit.get("created_at"))
            if published is None:
                continue

            title = strip_html(hit.get("title") or hit.get("story_title") or "")
            if not title:
                continue

            # 外部链接优先；Ask HN / Show HN 这类文本帖回退到讨论页
            url = str(hit.get("url") or "").strip() or ITEM_URL.format(id=object_id)

            seen_ids.add(object_id)
            collected.append(
                NewsItem(
                    source="hackernews",
                    title=title,
                    url=url,
                    published_at=published,
                    summary=truncate(strip_html(hit.get("story_text") or "") or title, SUMMARY_LIMIT),
                    raw_score=points,
                    extra={
                        "hn_id": object_id,
                        "hn_url": ITEM_URL.format(id=object_id),
                        "points": int(points),
                        "num_comments": int(hit.get("num_comments") or 0),
                        "author": hit.get("author") or "",
                    },
                )
            )
