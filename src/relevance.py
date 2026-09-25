"""相关性打分与候选筛选。

打分公式（三项加权，各自归一化到 0~1 后再加权）::

    score = w_keyword * keyword_norm      # 命中 AI Infra 关键词的加权和
          + w_heat    * heat_norm         # log1p(热度) 归一化
          + w_source  * source_norm       # 来源权重归一化

为什么关键词要加权而不是平分
----------------------------
``memory``、``scheduler`` 这类泛词在技术文章里极其常见，若与 ``kv cache``
同等权重，会把真正的基础设施话题挤出 Top-N。因此 ``config.yaml`` 里
``kv cache`` 给 3.0、``memory`` 只给 0.8。

标题命中权重是正文命中的 2 倍——标题里出现关键词，几乎一定是主题相关。

``min_keyword_score`` 的作用
----------------------------
HN 的首页热榜会混入大量与 AI Infra 无关的新闻（科技政策、物理学等）。
在进大模型之前先按关键词分过滤一遍，能显著省 token。若过滤后候选不足
``report.min_items``，会自动放宽为「不过滤」，保证不会因此开天窗。
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any

from .fetchers.base import NewsItem

logger = logging.getLogger(__name__)

#: 参与关键词匹配的 extra 字段（都是有实义的主题信息）
_HAYSTACK_EXTRA_KEYS = (
    "repo",
    "tag",
    "release_title",
    "categories",
    "topics",
    "feed_name",
    "subreddit",
    "journal_ref",
    "comment",
)


@lru_cache(maxsize=1024)
def _pattern(keyword: str) -> re.Pattern[str]:
    """关键词正则：允许可选复数 s，并避免 ``moe`` 命中 ``moment`` 这类子串误报。"""
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}s?(?![a-z0-9])", re.IGNORECASE)


def haystack(item: NewsItem) -> str:
    """拼出用于关键词匹配的文本。"""
    parts = [item.title or "", item.summary or ""]
    for key in _HAYSTACK_EXTRA_KEYS:
        value = item.extra.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value if v)
    return " \n ".join(parts)


def keyword_score(
    item: NewsItem,
    keywords: dict[str, float],
    negative_keywords: dict[str, float] | None = None,
) -> tuple[float, list[str], list[str]]:
    """返回 ``(加权命中分, 命中关键词, 命中负向词)``。

    负向词用于压掉**歧义误报**。最典型的是 ``inference``：在统计学与机器学习
    理论论文里它是「统计推断」而非「模型推理」，于是
    ``Selective Inference for Deep Clustering``、``Graph-Local Conformal
    Inference`` 这类论文会被当成 AI Infra。命中负向词即扣分，把它们压下去。
    """
    title = item.title or ""
    body = haystack(item)

    total = 0.0
    hits: list[str] = []
    for keyword, weight in keywords.items():
        regex = _pattern(keyword)
        if regex.search(title):
            total += weight * 2.0
            hits.append(keyword)
        elif regex.search(body):
            total += weight
            hits.append(keyword)

    negatives: list[str] = []
    penalty = 0.0
    for keyword, weight in (negative_keywords or {}).items():
        if _pattern(keyword).search(body):
            penalty += weight
            negatives.append(keyword)

    return total - penalty, hits, negatives


def score_items(items: list[NewsItem], config: Any) -> None:
    """就地填入 ``relevance_score`` 与 ``extra["keyword_hits"]`` 等字段。"""
    if not items:
        return

    relevance_cfg = config.raw.get("relevance", {})
    keywords = {
        str(k).lower(): float(v) for k, v in (relevance_cfg.get("keywords") or {}).items()
    }
    negative_keywords = {
        str(k).lower(): float(v)
        for k, v in (relevance_cfg.get("negative_keywords") or {}).items()
    }
    source_weights = {
        str(k): float(v) for k, v in (relevance_cfg.get("source_weights") or {}).items()
    }
    weights = relevance_cfg.get("score_weights") or {}
    w_keyword = float(weights.get("keyword", 1.0))
    w_heat = float(weights.get("heat", 0.5))
    w_source = float(weights.get("source", 0.3))

    raw_keyword: list[float] = []
    for item in items:
        score, hits, negatives = keyword_score(item, keywords, negative_keywords)
        item.extra["keyword_score"] = round(score, 3)
        item.extra["keyword_hits"] = hits[:12]
        if negatives:
            item.extra["negative_hits"] = negatives[:6]
        raw_keyword.append(score)

    # 用正向分位归一化（负分条目会被自然压到 0 附近）
    max_keyword = max((value for value in raw_keyword if value > 0), default=0.0)
    max_heat = max((math.log1p(max(0.0, it.raw_score)) for it in items), default=0.0)
    max_source = max(
        (source_weights.get(it.source, 1.0) for it in items),
        default=1.0,
    )

    for item in items:
        kw_norm = (item.extra["keyword_score"] / max_keyword) if max_keyword > 0 else 0.0
        heat_value = math.log1p(max(0.0, item.raw_score))
        heat_norm = (heat_value / max_heat) if max_heat > 0 else 0.0
        src_value = source_weights.get(item.source, 1.0)
        src_norm = (src_value / max_source) if max_source > 0 else 0.0

        item.relevance_score = (
            w_keyword * kw_norm + w_heat * heat_norm + w_source * src_norm
        )
        item.extra["score_breakdown"] = {
            "keyword": round(kw_norm, 4),
            "heat": round(heat_norm, 4),
            "source": round(src_norm, 4),
        }

    logger.debug(
        "已为 %d 条候选打分（关键词上限 %.2f，热度上限 %.2f）",
        len(items),
        max_keyword,
        max_heat,
    )


def select_candidates(
    items: list[NewsItem],
    config: Any,
    *,
    limit: int | None = None,
) -> tuple[list[NewsItem], dict[str, int]]:
    """按相关性选出送入大模型的候选清单。

    返回 ``(候选列表, 统计)``。若关键词过滤后数量低于 ``report.min_items``，
    会自动放宽过滤条件，避免因为阈值过严导致候选枯竭。
    """
    stats = {"scored": len(items), "filtered_out": 0, "relaxed": 0, "selected": 0}
    if not items:
        return [], stats

    limited = limit if limit is not None else int(config.raw["llm"]["shortlist_size"])
    min_items = int(config.raw["report"]["min_items"])
    min_keyword = float(config.raw.get("relevance", {}).get("min_keyword_score", 0.0))
    source_cap = int(config.raw.get("relevance", {}).get("shortlist_per_source_cap", 0))

    ranked = sorted(items, key=lambda it: it.relevance_score, reverse=True)

    if min_keyword > 0:
        passed = [it for it in ranked if float(it.extra.get("keyword_score", 0.0)) >= min_keyword]
        dropped = len(ranked) - len(passed)
        if len(passed) >= min_items:
            stats["filtered_out"] = dropped
            ranked = passed
        else:
            stats["relaxed"] = 1
            logger.info(
                "关键词过滤后仅剩 %d 条（< report.min_items=%d），已自动放宽过滤条件",
                len(passed),
                min_items,
            )

    selected = _take_with_source_cap(
        ranked,
        limit=limited,
        cap=source_cap,
        floor=int(config.raw["report"]["max_items"]),
        stats=stats,
    )
    stats["selected"] = len(selected)

    logger.info(
        "相关性筛选：%d 条候选 -> 过滤掉 %d 条 -> 保留 %d 条送入大模型%s",
        stats["scored"],
        stats["filtered_out"],
        stats["selected"],
        f"（单源上限 {source_cap}）" if source_cap > 0 else "",
    )
    return selected, stats


def _take_with_source_cap(
    ranked: list[NewsItem], *, limit: int, cap: int, floor: int, stats: dict[str, int]
) -> list[NewsItem]:
    """按相关性挑选候选，同时限制单个来源的条数。

    必要性：arXiv 一次就能返回上百条论文，而 GitHub 一周可能只有两三个发布。
    若不做来源配额，短名单会被论文淹没，把真正的工程发布挤掉——这与
    「AI Infra 周报」的定位不符。

    **在完整排名上遍历，而不是先截取前 ``limit`` 条再配额。**
    这点很关键：若先截取前 N 条，单源占比高时其它来源的条目根本进不了这 N 条，
    配额随即被「回填」逻辑抵消，等于没做（这是实测踩到的坑）。
    在完整排名上扫描，配额才能真正把名额腾给其他来源。

    ``floor`` 是下限保护：只有配额把结果压到 ``floor`` 以下时才回填，
    **宁可交出更短但来源多样的名单，也不为了凑数放弃多样性**。
    """
    if cap <= 0:
        return ranked[:limit]

    selected: list[NewsItem] = []
    counts: dict[str, int] = defaultdict(int)
    overflow: list[NewsItem] = []

    for item in ranked:
        if len(selected) < limit and counts[item.source] < cap:
            selected.append(item)
            counts[item.source] += 1
        elif len(overflow) < limit:
            overflow.append(item)

    if len(selected) < floor and overflow:
        need = floor - len(selected)
        selected.extend(overflow[:need])
        stats["cap_relaxed"] = 1
        logger.info(
            "来源配额后将不足 report.max_items=%d，已回填 %d 条（多样性让位于覆盖度）",
            floor,
            min(need, len(overflow)),
        )

    selected.sort(key=lambda it: it.relevance_score, reverse=True)
    return selected
