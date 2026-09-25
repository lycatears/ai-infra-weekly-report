"""搜索类 API 抓取：Tavily / Brave / SerpAPI。

降级设计（这是配置项「api_key 留空」的落点）
--------------------------------------------
``api_key`` 为空时：

1. :meth:`src.config.Config.enabled_sources` 根本不会把本源列入候选 —— 第一层
2. :meth:`SearchApiFetcher.fetch` 再检查一次并直接返回空列表 —— 第二层
3. 两种情况都只记 **一条 info 日志**，不 warn、不 raise，绝不影响其他源

发布时间说明
------------
三个 provider 的时间字段质量参差：Brave 有 ``page_age``（ISO 时间），
SerpAPI 只有 ``"3 days ago"`` 这种自然语言。因此：

- 能解析出真实时间 → 用它
- 解析不出 → 用本次抓取时刻兜底，并在 ``extra["published_at_estimated"]`` 标记，
  便于事后审计「这条的时间不是原件时间」

本源权重在 ``relevance.source_weights`` 中被刻意设为 0.9（低于 arXiv 1.2），
正是因为它的时间可信度最低。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseFetcher, NewsItem, parse_datetime, strip_html, truncate

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 500

#: 配置中的 freshness -> 各 provider 的原生取值
FRESHNESS_MAP: dict[str, dict[str, Any]] = {
    "tavily": {"day": 1, "week": 7, "month": 30},
    "brave": {"day": "pd", "week": "pw", "month": "pm"},
    "serpapi": {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m"},
}

DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "serpapi": "https://serpapi.com/search.json",
}


class SearchApiFetcher(BaseFetcher):
    name = "search_api"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        api_key = str(self.cfg.get("api_key", "")).strip()
        if not api_key:
            # 正常运行不会走到这里（enabled_sources 已过滤），保留作为二次保护
            logger.info("search_api 的 api_key 为空，跳过该源")
            return []

        provider = str(self.cfg.get("provider", "tavily")).lower()
        endpoint = (self.cfg.get("endpoint_overrides") or {}).get(provider) or DEFAULT_ENDPOINTS.get(provider)
        if not endpoint:
            logger.warning("search_api 不支持的 provider: %s", provider)
            return []

        queries = [str(q) for q in (self.cfg.get("queries") or []) if str(q).strip()]
        if not queries:
            return []

        per_query = int(self.cfg.get("results_per_query", 10))
        freshness = str(self.cfg.get("freshness", "week")).lower()

        collected: list[NewsItem] = []
        for query in queries:
            if provider == "tavily":
                collected.extend(self._tavily(endpoint, api_key, query, per_query, freshness))
            elif provider == "brave":
                collected.extend(self._brave(endpoint, api_key, query, per_query, freshness))
            elif provider == "serpapi":
                collected.extend(self._serpapi(endpoint, api_key, query, per_query, freshness))
            else:  # pragma: no cover - 已被 config 校验拦截
                logger.warning("search_api 不支持的 provider: %s", provider)
                return []
            self._sleep()

        return collected

    # ------------------------------------------------------------------
    def _make_item(
        self,
        *,
        title: str,
        url: str,
        summary: str,
        published: datetime | None,
        provider: str,
        query: str,
        extra: dict[str, Any] | None = None,
    ) -> NewsItem | None:
        title = strip_html(title)
        url = (url or "").strip()
        if not title or not url:
            return None

        estimated = published is None
        payload_extra: dict[str, Any] = {"kind": "search", "provider": provider, "query": query}
        if estimated:
            published = datetime.now(timezone.utc)
            payload_extra["published_at_estimated"] = True
        if extra:
            payload_extra.update(extra)

        return NewsItem(
            source="search_api",
            title=title,
            url=url,
            published_at=published,
            summary=truncate(strip_html(summary) or title, SUMMARY_LIMIT),
            raw_score=0.0,
            extra=payload_extra,
        )

    def _tavily(
        self, endpoint: str, api_key: str, query: str, limit: int, freshness: str
    ) -> list[NewsItem]:
        days = FRESHNESS_MAP["tavily"].get(freshness, 7)
        payload = self.http.post_json(
            endpoint,
            json_body={
                "api_key": api_key,
                "query": query,
                "max_results": limit,
                "topic": "news",
                "days": days,
                "search_depth": "basic",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if not isinstance(payload, dict):
            return []

        items: list[NewsItem] = []
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            item = self._make_item(
                title=str(result.get("title") or ""),
                url=str(result.get("url") or ""),
                summary=str(result.get("content") or ""),
                published=parse_datetime(result.get("published_date")),
                provider="tavily",
                query=query,
                extra={"search_score": result.get("score")},
            )
            if item is not None:
                items.append(item)
        return items

    def _brave(
        self, endpoint: str, api_key: str, query: str, limit: int, freshness: str
    ) -> list[NewsItem]:
        payload = self.http.get_json(
            endpoint,
            params={
                "q": query,
                "count": min(limit, 20),
                "freshness": FRESHNESS_MAP["brave"].get(freshness, "pw"),
            },
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
        if not isinstance(payload, dict):
            return []

        web = payload.get("web") or {}
        items: list[NewsItem] = []
        for result in web.get("results") or []:
            if not isinstance(result, dict):
                continue
            item = self._make_item(
                title=str(result.get("title") or ""),
                url=str(result.get("url") or ""),
                summary=str(result.get("description") or ""),
                published=parse_datetime(result.get("page_age")) or parse_datetime(result.get("age")),
                provider="brave",
                query=query,
            )
            if item is not None:
                items.append(item)
        return items

    def _serpapi(
        self, endpoint: str, api_key: str, query: str, limit: int, freshness: str
    ) -> list[NewsItem]:
        payload = self.http.get_json(
            endpoint,
            params={
                "q": query,
                "api_key": api_key,
                "engine": "google",
                "tbs": FRESHNESS_MAP["serpapi"].get(freshness, "qdr:w"),
                "num": min(limit, 20),
            },
        )
        if not isinstance(payload, dict):
            return []

        items: list[NewsItem] = []
        for result in payload.get("organic_results") or []:
            if not isinstance(result, dict):
                continue
            item = self._make_item(
                title=str(result.get("title") or ""),
                url=str(result.get("link") or ""),
                summary=str(result.get("snippet") or ""),
                # SerpAPI 的 date 形如 "3 days ago"，不可靠解析 -> 交给兜底
                published=parse_datetime(result.get("date")),
                provider="serpapi",
                query=query,
                extra={"raw_date": result.get("date")},
            )
            if item is not None:
                items.append(item)
        return items
