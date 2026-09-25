"""抓取器注册表与并发聚合。

对外只有两个函数：

- :func:`fetch_all` —— 并发跑各源，**单个源失败不影响其他源**
- :func:`all_source_names` —— 已知的源名清单，用于校验 ``--limit-sources``
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from .arxiv import ArxivFetcher
from .base import BaseFetcher, HttpClient, NewsItem
from .github import GithubFetcher
from .hackernews import HackerNewsFetcher
from .reddit import RedditFetcher
from .rss import RssFetcher
from .search_api import SearchApiFetcher

logger = logging.getLogger(__name__)

__all__ = [
    "BaseFetcher",
    "HttpClient",
    "NewsItem",
    "FETCHER_REGISTRY",
    "all_source_names",
    "build_http_client",
    "fetch_all",
]

#: 源名 -> 抓取器类。顺序即日志与报告中的展示顺序（可靠性由高到低）。
FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {
    "arxiv": ArxivFetcher,
    "github": GithubFetcher,
    "hackernews": HackerNewsFetcher,
    "rss": RssFetcher,
    "search_api": SearchApiFetcher,
    "reddit": RedditFetcher,
}


def all_source_names() -> list[str]:
    return list(FETCHER_REGISTRY)


def build_http_client(config: Any) -> HttpClient:
    """按 ``http`` 段构造请求客户端。"""
    http_cfg = config.raw.get("http", {})
    return HttpClient(
        timeout=float(http_cfg.get("timeout", 15)),
        max_retries=int(http_cfg.get("max_retries", 2)),
        backoff_factor=float(http_cfg.get("backoff_factor", 1.5)),
        user_agent=str(http_cfg.get("user_agent", "ai-infra-weekly-report/1.0")),
    )


def fetch_all(
    config: Any,
    start_utc: datetime,
    end_utc: datetime,
    *,
    limit_sources: list[str] | None = None,
    max_workers: int = 6,
) -> tuple[list[NewsItem], dict[str, int]]:
    """并发执行所有启用源的抓取。

    返回 ``(全部候选, 各源命中数)``。**任何源失败都不会抛出异常**，
    最坏情况是返回一个较短的列表。
    """
    sources = config.enabled_sources()

    if limit_sources:
        unknown = sorted(set(limit_sources) - set(FETCHER_REGISTRY))
        if unknown:
            logger.warning("--limit-sources 中存在未知数据源，已忽略: %s", ", ".join(unknown))
        sources = [s for s in sources if s in set(limit_sources)]
        logger.info("已按 --limit-sources 收窄数据源: %s", ", ".join(sources) or "（空）")

    if not sources:
        logger.warning("没有可用的数据源")
        return [], {}

    logger.info(
        "开始抓取：%d 个源，窗口 %s ~ %s (UTC)",
        len(sources),
        f"{start_utc:%Y-%m-%d %H:%M}",
        f"{end_utc:%Y-%m-%d %H:%M}",
    )

    results: dict[str, list[NewsItem]] = {}

    def run(name: str) -> tuple[str, list[NewsItem]]:
        # 每个源独立持有 HttpClient，避免多线程共用 requests.Session 的隐患
        client = build_http_client(config)
        try:
            fetcher = FETCHER_REGISTRY[name](config, client)
            return name, fetcher.safe_fetch(start_utc, end_utc)
        finally:
            client.close()

    workers = max(1, min(max_workers, len(sources)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        futures = {pool.submit(run, name): name for name in sources}
        for future in as_completed(futures):
            name = futures[future]
            try:
                key, items = future.result()
                results[key] = items
            except Exception as exc:  # noqa: BLE001 - 理论上 run() 不会抛，此处兜底
                logger.warning("数据源 %s 的抓取任务异常终止: %s: %s", name, type(exc).__name__, exc)
                results[name] = []

    # 按注册顺序合并，保证日志与报告输出稳定
    ordered: list[NewsItem] = []
    stats: dict[str, int] = {}
    for name in FETCHER_REGISTRY:
        if name not in results:
            continue
        items = results[name]
        stats[name] = len(items)
        ordered.extend(items)

    total = len(ordered)
    logger.info("抓取完成：合计 %d 条候选（去重前）", total)
    if total == 0:
        logger.warning("所有数据源均未返回候选，请检查网络连通性或数据源配置")

    return ordered, stats
