"""相关性打分与筛选测试。

重点：负向词典必须能压掉「统计推断」类误报，来源配额必须真的生效。
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from src.fetchers.base import NewsItem
from src.relevance import keyword_score, score_items, select_candidates

UTC = timezone.utc

#: 生成稳定的唯一 URL（不用 hash()，它每个进程都会变）
_counter = itertools.count(1)


def make_item(
    title: str,
    *,
    source: str = "arxiv",
    summary: str = "",
    score: float = 0.0,
    extra: dict | None = None,
) -> NewsItem:
    return NewsItem(
        source=source,
        title=title,
        url=f"https://example.com/item/{next(_counter)}",
        published_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        summary=summary,
        raw_score=score,
        extra=extra or {},
    )


# ------------------------------------------------------------ 关键词打
def test_keyword_score_rewards_title_hits_twice():
    keywords = {"kv cache": 3.0}
    in_title = keyword_score(make_item("KV Cache 优化"), keywords)[0]
    in_body = keyword_score(make_item("无关键词语气", summary="讲了 KV Cache"), keywords)[0]
    assert in_title == 6.0
    assert in_body == 3.0


def test_keyword_score_avoids_substring_false_positive():
    """'moe' 不应命中 'moment' 这类词内子串。"""
    score, hits, _ = keyword_score(make_item("A moment of clarity"), {"moe": 2.0})
    assert score == 0.0
    assert hits == []


def test_keyword_score_allows_plural():
    score, hits, _ = keyword_score(make_item("GPU kernels are fast"), {"gpu kernel": 2.8})
    assert score > 0
    assert "gpu kernel" in hits


def test_negative_keywords_subtract():
    keywords = {"inference": 0.8}
    negatives = {"conformal inference": 3.0}
    score, hits, neg = keyword_score(
        make_item("Graph-Local Conformal Inference for Counting"),
        keywords,
        negatives,
    )
    assert "inference" in hits
    assert "conformal inference" in neg
    assert score < 0


def test_statistical_inference_paper_is_filtered_out(config, cfg):
    """回归测试：统计推断论文不应进入短名单。

    `min_items` 必须调小，否则会触发「候选不足自动放宽」而跳过过滤——
    这里要验证的恰恰是过滤本身。
    """
    cfg("report.min_items", 1)
    items = [
        make_item("Selective Inference for Deep Clustering in Latent Space", source="arxiv"),
        make_item("GeoDose-CP: Graph-Local Conformal Inference for Counting", source="arxiv"),
        make_item("KV Cache Working Set: Online Capacity Planning for LLM Serving", source="arxiv"),
    ]
    score_items(items, config)
    selected, stats = select_candidates(items, config)

    titles = [i.title for i in selected]
    assert "KV Cache Working Set: Online Capacity Planning for LLM Serving" in titles
    assert not any("Conformal Inference" in t for t in titles)
    assert not any("Selective Inference" in t for t in titles)
    assert stats["relaxed"] == 0


def test_page_serving_noise_is_filtered_out(config, cfg):
    """回归测试：'serving this page' 这类 Web 服务噪音应被压掉。"""
    cfg("report.min_items", 1)
    items = [
        make_item("A restored PDP-11/83 serving this page on 211BSD Unix", source="hackernews", score=90),
        make_item("vLLM v0.30.0 发布：KV Cache 全面换用 MXFP8", source="github"),
    ]
    score_items(items, config)
    selected, _ = select_candidates(items, config)

    titles = [i.title for i in selected]
    assert any("vLLM" in t for t in titles)
    assert not any("PDP-11" in t for t in titles)


# ------------------------------------------------------------ 打分归一化
def test_score_items_populates_breakdown(config):
    items = [make_item("KV Cache 优化", source="arxiv", score=100.0)]
    score_items(items, config)

    assert items[0].relevance_score > 0
    breakdown = items[0].extra["score_breakdown"]
    assert set(breakdown) == {"keyword", "heat", "source"}
    assert all(0.0 <= v <= 1.0 for v in breakdown.values())
    assert items[0].extra["keyword_hits"]


def test_score_items_is_stable_with_zero_scores(config):
    """全零热度、零关键词时不应除零崩溃；得分只来自来源权重。"""
    items = [make_item("完全无关的内容")]
    score_items(items, config)

    breakdown = items[0].extra["score_breakdown"]
    assert breakdown["keyword"] == 0.0
    assert breakdown["heat"] == 0.0
    assert breakdown["source"] == 1.0  # 唯一条目，自身即最大值
    assert items[0].relevance_score == 0.3  # w_source * 1.0


def test_score_items_handles_empty_list(config):
    score_items([], config)  # 不应抛异常


# ------------------------------------------------------------ 来源配额
def test_source_cap_limits_dominant_source(config, cfg):
    """arXiv 论文再多，也不该占满整个短名单。

    配置必须自洽：shortlist_per_source_cap * 来源数 要够得着 max_items，
    否则会触发「回填」把配额抵消掉（那是另一个测试专门覆盖的行为）。
    """
    cfg("llm.shortlist_size", 20)
    cfg("report.max_items", 5)
    cfg("relevance.shortlist_per_source_cap", 3)
    cfg("relevance.min_keyword_score", 0.0)

    items = [
        make_item(f"KV Cache 论文 {i}", source="arxiv") for i in range(20)
    ] + [
        make_item("vLLM v0.30.0 发布", source="github"),
        make_item("SGLang 性能优化", source="github"),
    ]

    score_items(items, config)
    selected, stats = select_candidates(items, config)

    from collections import Counter

    counts = Counter(i.source for i in selected)
    assert counts["arxiv"] <= 3
    assert counts["github"] == 2
    assert "cap_relaxed" not in stats


def test_source_cap_relaxes_when_below_floor(config, cfg):
    """配额导致条数不足 max_items 时应回填，避免浪费名额。"""
    cfg("llm.shortlist_size", 30)
    cfg("report.max_items", 5)
    cfg("relevance.shortlist_per_source_cap", 1)
    cfg("relevance.min_keyword_score", 0.0)

    items = [make_item(f"KV Cache 论文 {i}", source="arxiv") for i in range(10)]
    score_items(items, config)
    selected, stats = select_candidates(items, config)

    assert len(selected) == 5
    assert stats.get("cap_relaxed") == 1


def test_cap_disabled_keeps_plain_top_n(config, cfg):
    cfg("llm.shortlist_size", 5)
    cfg("relevance.shortlist_per_source_cap", 0)
    cfg("relevance.min_keyword_score", 0.0)

    items = [make_item(f"KV Cache 论文 {i}", source="arxiv") for i in range(10)]
    score_items(items, config)
    selected, _ = select_candidates(items, config)
    assert len(selected) == 5


# ------------------------------------------------------------ 放宽逻辑
def test_filter_relaxes_when_too_few_pass(config, cfg):
    """关键词过滤后不足 min_items 时应自动放宽，不能开天窗。"""
    cfg("relevance.min_keyword_score", 999.0)  # 故意严到没人能过
    cfg("report.min_items", 3)

    items = [make_item(f"无关内容 {i}") for i in range(10)]
    score_items(items, config)
    selected, stats = select_candidates(items, config)

    assert stats["relaxed"] == 1
    assert len(selected) > 0


def test_select_candidates_empty_input(config):
    selected, stats = select_candidates([], config)
    assert selected == []
    assert stats["selected"] == 0


def test_selection_is_sorted_by_relevance(config, cfg):
    cfg("relevance.min_keyword_score", 0.0)
    items = [
        make_item("普通内容", source="rss"),
        make_item("KV Cache + Speculative Decoding + Tensor Parallel 全都有", source="arxiv"),
    ]
    score_items(items, config)
    selected, _ = select_candidates(items, config)
    assert selected[0].title.startswith("KV Cache")
