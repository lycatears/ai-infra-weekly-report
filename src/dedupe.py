"""两级去重。

第一级：报告内去重（:func:`dedupe_within_report`）
--------------------------------------------------
- **精确键**：规范化 URL 相同 → 合并
- **相似键**：标题 ``difflib`` 比值 >= 阈值，或词集合 Jaccard >= 阈值 → 合并

  合并时保留「内容最丰富」的那个作为代表（arXiv / RSS / GitHub 的正文比
  HN 的讨论摘要更有信息量），同时把各来源的热度（HN points 等）**取最大值**
  保留下来，并记录 ``extra["also_seen_at"]`` 便于追溯。

第二级：跨周去重（配合 :mod:`src.history`）
-------------------------------------------
**只用精确匹配（规范化 URL / 归一化标题），刻意不做模糊匹配。**

原因：跨周模糊匹配会误杀。例如上周发了 ``vLLM v0.30.0``，本周发 ``vLLM v0.30.1``，
两者标题相似度约 0.95，远超 0.88 的阈值——真正的「新版本发布」会被当成重复丢掉。
报告内模糊去重没有这个问题，因为同一批数据里的重复通常是同一件事的多个来源。
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from .fetchers.base import NewsItem, normalise_title, tokens

logger = logging.getLogger(__name__)

#: 同一事件被多个来源报道时，代表条目取「内容优先」；数值越大越优先
CONTENT_PRIORITY: dict[str, int] = {
    "arxiv": 5,
    "github": 4,
    "rss": 4,
    "search_api": 2,
    "hackernews": 1,
    "reddit": 1,
}

#: 规范化 URL 时按「路径前缀」改写的规则（``arxiv.org`` 的 abs/pdf 统一到 abs）
_KEEP_QUERY_HOSTS = {"news.ycombinator.com"}

#: arXiv URL 末尾的版本号（2609.30059v2 -> 2609.30059）
_ARXIV_VERSION_RE = re.compile(r"v\d+$")


def canonical_url(url: str, strip_params: Iterable[str] = ()) -> str:
    """把 URL 归一化成稳定的去重键。

    处理内容：小写 scheme/host、去掉 ``www.``、剔除跟踪参数与 fragment、
    去掉路径末尾多余的 ``/``、URL 解码、arXiv 的 ``abs``/``pdf`` 与版本号统一。
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.lower()

    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw.lower()

    # HN 的 item?id= 参数是语义参数，不能当跟踪参数删掉
    if host in _KEEP_QUERY_HOSTS:
        params = parse_qsl(parsed.query, keep_blank_values=False)
    else:
        drop = {p.lower() for p in strip_params}
        params = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k.lower() not in drop
        ]
    params.sort()

    path = unquote(parsed.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    # arXiv：/pdf/<id> 与 /abs/<id> 指向同一篇论文，且版本号不影响身份。
    # 归一化后 v1 / v2 / v3 会得到同一个键，避免「同一篇论文的多版本链接」
    # 被当成多条不同新闻。
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        for prefix in ("/pdf/", "/abs/"):
            if path.startswith(prefix):
                ident = path[len(prefix) :].removesuffix(".pdf")
                ident = _ARXIV_VERSION_RE.sub("", ident)
                path = f"/abs/{ident}"
                break

    netloc = host if parsed.port in (None, 80, 443) else f"{host}:{parsed.port}"
    return urlunparse((scheme, netloc, path, "", urlencode(params), ""))


def _merge_group(group: list[NewsItem]) -> NewsItem:
    """把一组重复条目合并成一个代表条目。"""
    # 代表：内容优先级高者胜；同优先级取摘要更长者
    representative = max(
        group,
        key=lambda it: (CONTENT_PRIORITY.get(it.source, 0), len(it.summary or "")),
    )

    sources: list[dict[str, Any]] = []
    max_score = 0.0
    for item in group:
        max_score = max(max_score, item.raw_score)
        entry = {"source": item.source, "url": item.url, "raw_score": item.raw_score}
        if item.source != representative.source:
            sources.append(entry)

    representative.raw_score = max_score

    seen = list(representative.extra.get("also_seen_at") or [])
    existing = {(s.get("source"), s.get("url")) for s in seen}
    for entry in sources:
        key = (entry["source"], entry["url"])
        if key not in existing:
            seen.append(entry)
            existing.add(key)
    if seen:
        representative.extra["also_seen_at"] = seen

    return representative


#: 版本号提取（``v0.30.0`` -> ``0.30.0``），用于版本守卫
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _versions(text: str) -> set[str]:
    return set(_VERSION_RE.findall(text or ""))


def _similar(a: NewsItem, b: NewsItem, ratio_threshold: float, jaccard_threshold: float) -> bool:
    key_a, key_b = a.title_key, b.title_key
    if not key_a or not key_b:
        return False

    # ---- 版本守卫 ----
    # 两边都带版本号且互不相交时，判定为**不同事件**。
    # 否则 "vLLM v0.30.0" 与 "vLLM v0.30.1" 的标题相似度高达 0.92（远超 0.88
    # 阈值），同一周内的两次真实版本发布会被误合并成一条。
    versions_a, versions_b = _versions(a.title), _versions(b.title)
    if versions_a and versions_b and versions_a.isdisjoint(versions_b):
        return False

    tokens_a, tokens_b = tokens(a.title), tokens(b.title)
    if tokens_a and tokens_b:
        union = tokens_a | tokens_b
        if union and len(tokens_a & tokens_b) / len(union) >= jaccard_threshold:
            return True

    return difflib.SequenceMatcher(None, key_a, key_b).ratio() >= ratio_threshold


def dedupe_within_report(
    items: list[NewsItem],
    *,
    strip_params: Iterable[str] = (),
    ratio_threshold: float = 0.88,
    jaccard_threshold: float = 0.80,
) -> tuple[list[NewsItem], dict[str, int]]:
    """报告内去重。返回 ``(去重后列表, 统计)``，列表按发布时间倒序。"""
    stats = {"input": len(items), "url_merged": 0, "title_merged": 0, "output": 0}
    if not items:
        return [], stats

    # ---- 第一级：规范化 URL 精确去重 ----
    by_url: dict[str, NewsItem] = {}
    order: list[str] = []
    for item in items:
        key = canonical_url(item.url, strip_params)
        if not key:
            continue
        item.extra["canonical_url"] = key
        if key in by_url:
            by_url[key] = _merge_group([by_url[key], item])
            stats["url_merged"] += 1
        else:
            by_url[key] = item
            order.append(key)

    unique = [by_url[key] for key in order]

    # ---- 第二级：标题相似度去重（用倒排索引做候选剪枝，避免 O(n^2)）----
    token_index: dict[str, list[int]] = defaultdict(list)
    kept: list[NewsItem] = []
    merged_into: list[int] = []  # 与 kept 并行，记录每条的组代表下标

    for item in unique:
        item_tokens = {t for t in tokens(item.title) if len(t) >= 3}
        candidates: set[int] = set()
        for token in item_tokens:
            candidates.update(token_index.get(token, ()))

        target: int | None = None
        for index in candidates:
            if _similar(kept[index], item, ratio_threshold, jaccard_threshold):
                target = index
                break

        if target is None:
            kept.append(item)
            merged_into.append(len(kept) - 1)
            for token in item_tokens:
                token_index[token].append(len(kept) - 1)
        else:
            merged_into.append(target)
            kept[target] = _merge_group([kept[target], item])
            stats["title_merged"] += 1

    kept.sort(key=lambda it: it.published_at, reverse=True)
    stats["output"] = len(kept)

    logger.info(
        "报告内去重：%d -> %d（URL 合并 %d，标题相似合并 %d）",
        stats["input"],
        stats["output"],
        stats["url_merged"],
        stats["title_merged"],
    )
    return kept, stats


def drop_seen_before(
    items: list[NewsItem], is_seen: Any
) -> tuple[list[NewsItem], list[NewsItem]]:
    """用 ``is_seen(item) -> bool`` 剔除历史上已收录的条目。

    返回 ``(新条目, 被剔除条目)``。
    """
    fresh: list[NewsItem] = []
    dropped: list[NewsItem] = []
    for item in items:
        if is_seen(item):
            dropped.append(item)
        else:
            fresh.append(item)

    if dropped:
        logger.info("跨周去重：剔除 %d 条历史已收录内容", len(dropped))
        for item in dropped[:10]:
            logger.debug("  已收录过: %s", item.title)
    return fresh, dropped


def title_key_of(item: NewsItem) -> str:
    return normalise_title(item.title)
