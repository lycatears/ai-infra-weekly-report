"""arXiv 论文抓取（官方 Atom API，无需 key）。

要点
----
- 用 ``sortBy=submittedDate`` 取最新提交；``<published>`` 即 v1 首次提交时间，
  这才是「本周新论文」的正确判据（``<updated>`` 会随修订变化，会把老论文带进来）
- arXiv 建议请求间隔 >= 3s，因此本抓取器只发 1 次请求（多分类用 OR 合并进一个查询）
- 论文没有热度指标，``raw_score`` 固定为 0，排序完全交给关键词相关性打分
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import feedparser

from .base import BaseFetcher, NewsItem, parse_datetime, parse_struct_time, strip_html, truncate

logger = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"

#: 摘要截断长度（太长会浪费大模型 token）
SUMMARY_LIMIT = 700

#: 剥离 arXiv URL 末尾的版本号（如 2609.30059v1 -> 2609.30059），
#: 保证同一篇论文的多次查询得到同一个 URL。
#: 注意：绝不能用 ``url.split("v")[0]``——"arxiv" 这个词本身就含字母 v，
#: 那样会把所有 URL 都截断成 "https://arxi"，进而被源内去重误判为同一条。
_ARXIV_VERSION_RE = re.compile(r"v\d+$")


def canonical_arxiv_url(url: str) -> str:
    """去掉 arXiv URL 的版本后缀。"""
    return _ARXIV_VERSION_RE.sub("", (url or "").strip())


class ArxivFetcher(BaseFetcher):
    name = "arxiv"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        categories = [str(c) for c in (self.cfg.get("categories") or [])]
        if not categories:
            logger.warning("arxiv 未配置 categories，跳过")
            return []

        terms = [str(t) for t in (self.cfg.get("abs_terms") or [])]
        params = {
            "search_query": self._build_query(categories, terms),
            "start": 0,
            "max_results": int(self.cfg.get("max_results", 100)),
            "sortBy": str(self.cfg.get("sort_by", "submittedDate")),
            "sortOrder": "descending",
        }

        raw = self.http.get_bytes(API_URL, params=params)
        if raw is None:
            return []

        feed = feedparser.parse(raw)
        if getattr(feed, "bozo", 0) and not feed.entries:
            logger.warning("arxiv 返回内容无法解析: %s", getattr(feed, "bozo_exception", "未知原因"))
            return []

        items: list[NewsItem] = []
        for entry in feed.entries:
            item = self._to_item(entry)
            if item is not None:
                items.append(item)
        return items

    # ------------------------------------------------------------------
    @staticmethod
    def _build_query(categories: list[str], terms: list[str]) -> str:
        """构造 ``(cat:a OR cat:b) AND (abs:"x" OR abs:"y")`` 形式的查询串。"""
        cat_part = " OR ".join(f"cat:{c}" for c in categories)
        query = f"({cat_part})"
        if terms:
            abs_part = " OR ".join(f'abs:"{t}"' for t in terms)
            query = f"{query} AND ({abs_part})"
        return query

    @staticmethod
    def _to_item(entry: Any) -> NewsItem | None:
        title = strip_html(entry.get("title", ""))
        if not title:
            return None

        # 优先用 <published>（v1 提交时间）作为发布时间
        published = parse_struct_time(entry.get("published_parsed")) or parse_datetime(
            entry.get("published")
        )
        if published is None:
            published = parse_struct_time(entry.get("updated_parsed")) or parse_datetime(
                entry.get("updated")
            )
        if published is None:
            return None

        # 链接：优先取 rel="alternate" 的 abs 页面
        url = str(entry.get("link") or entry.get("id") or "").strip()
        for link in entry.get("links") or []:
            if link.get("rel") == "alternate" and link.get("href"):
                url = str(link["href"])
                break
        if not url:
            return None
        url = canonical_arxiv_url(url)

        extra: dict[str, Any] = {}
        if (updated := parse_struct_time(entry.get("updated_parsed"))) is not None:
            extra["updated"] = updated.isoformat()
        if comment := strip_html(entry.get("arxiv_comment", "")):
            extra["comment"] = truncate(comment, 200)
        if journal := strip_html(entry.get("arxiv_journal_ref", "")):
            extra["journal_ref"] = journal
        if primary := (entry.get("arxiv_primary_category") or {}).get("term"):
            extra["primary_category"] = primary
        if authors := [a.get("name") for a in (entry.get("authors") or []) if a.get("name")]:
            extra["authors"] = authors[:8]
        if categories := [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]:
            extra["categories"] = categories

        return NewsItem(
            source="arxiv",
            title=title,
            url=url,
            published_at=published,
            summary=truncate(strip_html(entry.get("summary", "")), SUMMARY_LIMIT),
            raw_score=0.0,
            extra=extra,
        )
