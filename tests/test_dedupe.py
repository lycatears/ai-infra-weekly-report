"""去重与跨周去重测试。

重点覆盖两个曾经真实出过问题的点：

1. arXiv 的 URL 归一化——曾用 ``url.split("v")[0]``，而 "arxiv" 里本身含字母 v，
   导致所有 URL 被截断成 ``https://arxi``，100 条论文被误判为同一条。
2. 跨周去重**不做**模糊匹配——否则 ``v0.30.1`` 会被 ``v0.30.0`` 误杀。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.dedupe import canonical_url, dedupe_within_report, drop_seen_before
from src.fetchers.base import NewsItem

UTC = timezone.utc
STRIP = ["utm_source", "utm_medium", "utm_campaign", "ref", "source", "fbclid"]


def make_item(
    title: str,
    url: str,
    *,
    source: str = "arxiv",
    summary: str = "摘要",
    score: float = 0.0,
    day: int = 23,
    extra: dict | None = None,
) -> NewsItem:
    return NewsItem(
        source=source,
        title=title,
        url=url,
        published_at=datetime(2026, 9, day, 12, 0, tzinfo=UTC),
        summary=summary,
        raw_score=score,
        extra=extra or {},
    )


# ------------------------------------------------------------ URL 规范化
def test_canonical_url_strips_tracking_params():
    url = "https://example.com/post?utm_source=x&utm_medium=y&ref=z&id=7"
    assert canonical_url(url, STRIP) == "https://example.com/post?id=7"


def test_canonical_url_normalises_host_and_scheme():
    assert canonical_url("HTTPS://WWW.Example.COM/Path/", STRIP) == "https://example.com/Path"


def test_canonical_url_drops_fragment():
    assert canonical_url("https://example.com/a#section", STRIP) == "https://example.com/a"


def test_canonical_url_unifies_arxiv_abs_and_pdf():
    """同一篇论文的 abs / pdf 链接必须归一到同一个键。"""
    abs_url = canonical_url("https://arxiv.org/abs/2609.30059v1", STRIP)
    pdf_url = canonical_url("https://arxiv.org/pdf/2609.30059v2.pdf", STRIP)
    assert abs_url == "https://arxiv.org/abs/2609.30059"
    assert pdf_url == abs_url


def test_canonical_url_keeps_hn_item_query():
    """HN 的 item?id= 是语义参数，不能被当跟踪参数删掉。"""
    url = canonical_url("https://news.ycombinator.com/item?id=49845406&utm_source=x", STRIP)
    assert "id=49845406" in url


def test_canonical_url_handles_empty():
    assert canonical_url("", STRIP) == ""
    assert canonical_url("   ", STRIP) == ""


def test_canonical_url_keeps_distinct_arxiv_ids():
    """回归测试：不同 arXiv id 绝不能被归一化成同一个 URL。"""
    a = canonical_url("https://arxiv.org/abs/2609.30059v1", STRIP)
    b = canonical_url("https://arxiv.org/abs/2609.30250v1", STRIP)
    assert a != b
    assert a.endswith("2609.30059")
    assert b.endswith("2609.30250")


# -------------------------------------------------------- 报告内去重
def test_dedupe_by_identical_url():
    items = [
        make_item("vLLM v0.30.0 发布", "https://github.com/vllm-project/vllm/releases/tag/v0.30.0"),
        make_item("vLLM v0.30.0 发布", "https://github.com/vllm-project/vllm/releases/tag/v0.30.0"),
    ]
    result, stats = dedupe_within_report(items, strip_params=STRIP)
    assert len(result) == 1
    assert stats["url_merged"] == 1


def test_dedupe_by_url_with_tracking_params():
    items = [
        make_item("A", "https://example.com/post"),
        make_item("B", "https://www.example.com/post?utm_source=twitter"),
    ]
    result, _ = dedupe_within_report(items, strip_params=STRIP)
    assert len(result) == 1


def test_dedupe_by_similar_title_across_sources():
    """同一事件被 HN 与 GitHub 分别报道 -> 合并，并保留最大热度。"""
    items = [
        make_item(
            "vLLM v0.30.0 released with MXFP8 KV cache",
            "https://github.com/vllm-project/vllm/releases/tag/v0.30.0",
            source="github",
            summary="发布说明正文，内容较长",
            score=0.0,
        ),
        make_item(
            "vLLM v0.30.0 released with MXFP8 KV cache",
            "https://news.ycombinator.com/item?id=1",
            source="hackernews",
            summary="讨论",
            score=312.0,
        ),
    ]
    result, stats = dedupe_within_report(items, strip_params=STRIP)
    assert len(result) == 1
    assert stats["title_merged"] == 1
    # 热度取最大值，内容取 GitHub（内容优先级更高）
    assert result[0].raw_score == 312.0
    assert result[0].source == "github"
    assert result[0].extra["also_seen_at"][0]["source"] == "hackernews"


def test_dedupe_keeps_different_versions_separate():
    """回归测试：v0.30.0 与 v0.30.1 是两次不同发布，不能合并。"""
    items = [
        make_item("vLLM v0.30.0", "https://github.com/vllm-project/vllm/releases/tag/v0.30.0", source="github"),
        make_item("vLLM v0.30.1", "https://github.com/vllm-project/vllm/releases/tag/v0.30.1", source="github"),
    ]
    result, _ = dedupe_within_report(items, strip_params=STRIP)
    assert len(result) == 2


def test_dedupe_keeps_unrelated_items():
    items = [
        make_item("KV Cache 压缩新方法", "https://arxiv.org/abs/2609.1"),
        make_item("GPU 内核自动调优", "https://arxiv.org/abs/2609.2"),
        make_item("MoE 推理吞吐优化", "https://arxiv.org/abs/2609.3"),
    ]
    result, _ = dedupe_within_report(items, strip_params=STRIP)
    assert len(result) == 3


def test_dedupe_returns_sorted_by_time_desc():
    items = [
        make_item("旧", "https://e.com/1", day=21),
        make_item("新", "https://e.com/2", day=25),
        make_item("中", "https://e.com/3", day=23),
    ]
    result, _ = dedupe_within_report(items, strip_params=STRIP)
    assert [i.title for i in result] == ["新", "中", "旧"]


def test_dedupe_empty_input():
    result, stats = dedupe_within_report([], strip_params=STRIP)
    assert result == []
    assert stats["input"] == 0


# ------------------------------------------------------------ 跨周去重
def test_drop_seen_before_uses_exact_match_only():
    """跨周只用精确匹配：新版本不能被旧版本模糊匹配掉。"""
    seen_urls = {"https://github.com/vllm-project/vllm/releases/tag/v0.30.0"}
    items = [
        make_item("vLLM v0.30.0", "https://github.com/vllm-project/vllm/releases/tag/v0.30.0", source="github"),
        make_item("vLLM v0.30.1", "https://github.com/vllm-project/vllm/releases/tag/v0.30.1", source="github"),
    ]
    fresh, dropped = drop_seen_before(items, lambda it: it.url in seen_urls)
    assert len(fresh) == 1
    assert fresh[0].title == "vLLM v0.30.1"
    assert len(dropped) == 1


def test_drop_seen_before_returns_both_lists():
    items = [make_item("A", "https://e.com/a"), make_item("B", "https://e.com/b")]
    fresh, dropped = drop_seen_before(items, lambda it: "a" in it.url)
    assert [i.title for i in fresh] == ["B"]
    assert [i.title for i in dropped] == ["A"]
