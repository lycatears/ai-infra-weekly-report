"""Reddit 抓取（公开 ``.json`` 端点，无需 key）。

⚠️ 可靠性警告
--------------
Reddit 近年开始拦截非浏览器 User-Agent 与数据中心 IP，``/r/<sub>/top.json``
**实测经常直接 403 或返回验证页**。因此本源被设计为**可选**：

- ``safe_fetch`` 会吞掉所有异常并返回空列表，只记一条 warning
- 本源的缺失不影响其他 5 类源，也不影响报告生成
- 若长期失败，可在 ``config.yaml`` 里把 ``sources.reddit.enabled`` 设为 false
  以免日志噪音

这里刻意使用浏览器风格的 User-Agent 以尽量提高成功率。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .base import BaseFetcher, NewsItem, parse_datetime, strip_html, truncate

logger = logging.getLogger(__name__)

BASE_URL = "https://www.reddit.com/r/{sub}/{listing}.json"
POST_URL = "https://www.reddit.com{permalink}"

#: Reddit 对非浏览器 UA 会直接拒绝
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SUMMARY_LIMIT = 600

_VALID_LISTINGS = {"top", "hot", "new", "rising", "controversial"}
_VALID_TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}


class RedditFetcher(BaseFetcher):
    name = "reddit"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        subreddits = [str(s).strip() for s in (self.cfg.get("subreddits") or []) if str(s).strip()]
        if not subreddits:
            return []

        listing = str(self.cfg.get("listing", "top")).lower()
        listing = listing if listing in _VALID_LISTINGS else "top"
        time_filter = str(self.cfg.get("time_filter", "week")).lower()
        time_filter = time_filter if time_filter in _VALID_TIME_FILTERS else "week"

        limit = min(int(self.cfg.get("limit", 50)), 100)
        min_score = int(self.cfg.get("min_score", 0))

        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}

        items: list[NewsItem] = []
        failures = 0
        for subreddit in subreddits:
            payload = self.http.get_json(
                BASE_URL.format(sub=subreddit, listing=listing),
                params={"t": time_filter, "limit": limit, "raw_json": 1},
                headers=headers,
            )
            if payload is None:
                failures += 1
                continue
            items.extend(self._extract(payload, subreddit, min_score))
            self._sleep()

        if failures == len(subreddits):
            logger.warning(
                "reddit 全部 %d 个子版块抓取失败（该源为可选源，不影响报告生成）",
                failures,
            )
        return items

    # ------------------------------------------------------------------
    @staticmethod
    def _extract(payload: Any, subreddit: str, min_score: int) -> list[NewsItem]:
        if not isinstance(payload, dict):
            return []

        children = (payload.get("data") or {}).get("children") or []
        items: list[NewsItem] = []
        for child in children:
            data = child.get("data") if isinstance(child, dict) else None
            if not isinstance(data, dict):
                continue

            score = data.get("score")
            score = float(score) if isinstance(score, (int, float)) else 0.0
            if score < min_score:
                continue

            published = parse_datetime(data.get("created_utc"))
            if published is None:
                continue

            title = strip_html(data.get("title") or "")
            if not title:
                continue

            permalink = str(data.get("permalink") or "").strip()
            # 自帖（selftext）没有外部链接，用讨论页；外链帖优先用原链接
            if data.get("is_self") or not data.get("url_overridden_by_dest"):
                url = POST_URL.format(permalink=permalink) if permalink else str(data.get("url") or "")
            else:
                url = str(data.get("url_overridden_by_dest") or "")
            if not url:
                continue

            items.append(
                NewsItem(
                    source="reddit",
                    title=title,
                    url=url,
                    published_at=published,
                    summary=truncate(strip_html(data.get("selftext") or "") or title, SUMMARY_LIMIT),
                    raw_score=score,
                    extra={
                        "subreddit": data.get("subreddit") or subreddit,
                        "permalink": POST_URL.format(permalink=permalink) if permalink else "",
                        "num_comments": int(data.get("num_comments") or 0),
                        "author": data.get("author") or "",
                        "flair": data.get("link_flair_text") or "",
                    },
                )
            )
        return items
