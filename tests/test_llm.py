"""大模型层测试。

不发真实请求：JSON 解析与字段规整是纯函数，直接测；
两阶段流程通过 monkeypatch 替换 `_chat_json` 来测，
这样可以精确覆盖「模型返回畸形数据时怎么办」。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.fetchers.base import NewsItem
from src.llm import LLMClient, LLMError, SummaryItem, _as_str_list, parse_json_object

UTC = timezone.utc


def make_item(index: int, **overrides) -> NewsItem:
    base = dict(
        source="arxiv",
        title=f"KV Cache 论文 {index}",
        url=f"https://arxiv.org/abs/2609.{index}",
        published_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        summary=f"第 {index} 条摘要",
        raw_score=0.0,
        extra={"keyword_hits": ["kv cache"]},
    )
    base.update(overrides)
    return NewsItem(**base)


# ------------------------------------------------------- JSON 解析容错
def test_parse_plain_json():
    assert parse_json_object('{"a":1}') == {"a": 1}


def test_parse_json_in_code_fence():
    assert parse_json_object('```json\n{"a":1}\n```') == {"a": 1}


def test_parse_json_in_bare_code_fence():
    assert parse_json_object('```\n{"a":1}\n```') == {"a": 1}


def test_parse_json_with_surrounding_prose():
    assert parse_json_object('好的，结果如下：\n{"a":1}\n希望有帮助。') == {"a": 1}


def test_parse_json_with_nested_braces():
    payload = '前言 {"items":[{"id":1,"x":{"y":2}}]} 后记'
    assert parse_json_object(payload) == {"items": [{"id": 1, "x": {"y": 2}}]}


def test_parse_json_keeps_unicode():
    assert parse_json_object('{"名称":"中文"}') == {"名称": "中文"}


def test_parse_json_rejects_empty():
    with pytest.raises(ValueError):
        parse_json_object("")
    with pytest.raises(ValueError):
        parse_json_object("   ")


def test_parse_json_rejects_text_without_object():
    with pytest.raises(ValueError):
        parse_json_object("完全没有 JSON")


def test_parse_json_rejects_top_level_array():
    with pytest.raises(ValueError):
        parse_json_object("[1,2,3]")


def test_parse_json_rejects_malformed():
    with pytest.raises(ValueError):
        parse_json_object('{"a":}')


# ------------------------------------------------------------ 字段规整
def test_as_str_list_accepts_string():
    assert _as_str_list("单个") == ["单个"]


def test_as_str_list_accepts_list_and_dedupes():
    assert _as_str_list(["a", "a", "b"]) == ["a", "b"]


def test_as_str_list_strips_bullet_prefixes():
    assert _as_str_list(["- 亮点", "* 要点", "• 第三"]) == ["亮点", "要点", "第三"]


def test_as_str_list_skips_nested_structures():
    assert _as_str_list([{"a": 1}, "有效", ["x"]]) == ["有效"]


def test_as_str_list_respects_limit():
    assert len(_as_str_list([str(i) for i in range(20)], limit=3)) == 3


def test_as_str_list_handles_none():
    assert _as_str_list(None) == []


# ------------------------------------------------------------ 客户端构造
def test_client_accepts_empty_api_key(config):
    """api_key 留空时不应在构造阶段崩溃——失败要发生在调用阶段。"""
    client = LLMClient(config)
    assert client.model == "deepseek-chat"
    assert client.token_usage["total_tokens"] == 0


def test_client_reads_batch_config(config, cfg):
    cfg("llm.batch_size", 7)
    cfg("llm.max_workers", 2)
    client = LLMClient(config)
    assert client.batch_size == 7
    assert client.max_workers == 2


# ------------------------------------------------------------ 阶段 A
def test_shortlist_maps_ids_back_to_items(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {"selected": [{"id": 2, "reason": "KV Cache 优化"}, {"id": 1}]},
    )

    candidates = [make_item(1), make_item(2)]
    selected = client.shortlist(candidates)

    assert [i.title for i in selected] == ["KV Cache 论文 2", "KV Cache 论文 1"]
    assert selected[0].extra["selection_reason"] == "KV Cache 优化"


def test_shortlist_ignores_unknown_ids(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {"selected": [{"id": 99}, {"id": 1}]},
    )
    selected = client.shortlist([make_item(1)])
    assert len(selected) == 1


def test_shortlist_ignores_duplicate_ids(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {"selected": [{"id": 1}, {"id": 1}]},
    )
    assert len(client.shortlist([make_item(1)])) == 1


def test_shortlist_accepts_bare_integers(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(client, "_chat_json", lambda system, payload: {"selected": [1, 2]})
    assert len(client.shortlist([make_item(1), make_item(2)])) == 2


def test_shortlist_raises_on_missing_key(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(client, "_chat_json", lambda system, payload: {"wrong": []})
    with pytest.raises(LLMError):
        client.shortlist([make_item(1)])


def test_shortlist_raises_when_nothing_selected(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(client, "_chat_json", lambda system, payload: {"selected": []})
    with pytest.raises(LLMError):
        client.shortlist([make_item(1)])


def test_shortlist_empty_input_skips_call(config):
    client = LLMClient(config)
    assert client.shortlist([]) == []


# ------------------------------------------------------------ 阶段 B
def test_summarize_normalises_fields(config, monkeypatch, cfg):
    cfg("llm.batch_size", 5)
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {
            "items": [
                {
                    "id": 1,
                    "project_name": " vLLM ",
                    "one_line": " 发布新版本 ",
                    "highlights": ["- 亮点", "亮点", "第二个"],
                    "keywords": ["KV Cache"],
                }
            ]
        },
    )

    result = client.summarize([make_item(1)])
    assert len(result) == 1
    assert result[0].project_name == "vLLM"
    assert result[0].one_line == "发布新版本"
    assert result[0].highlights == ["亮点", "第二个"]
    assert result[0].fallback is False


def test_summarize_keeps_scraped_time_not_model_time(config, monkeypatch):
    """时间必须来自抓取结果，不能被模型覆盖。"""
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {
            "items": [
                {
                    "id": 1,
                    "project_name": "X",
                    "one_line": "Y",
                    "published_at": "1999-01-01T00:00:00Z",
                }
            ]
        },
    )
    item = make_item(1)
    result = client.summarize([item])
    assert result[0].published_at == item.published_at


def test_summarize_builds_links_from_scraped_data(config, monkeypatch):
    """链接只能来自抓取结果，模型给的 URL 一律忽略。"""
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {
            "items": [{"id": 1, "project_name": "X", "one_line": "Y", "links": ["https://evil.example.com"]}]
        },
    )
    item = make_item(1, extra={"keyword_hits": [], "hn_url": "https://news.ycombinator.com/item?id=1"})
    result = client.summarize([item])

    assert "https://evil.example.com" not in result[0].links
    assert item.url in result[0].links
    assert "https://news.ycombinator.com/item?id=1" in result[0].links


def test_summarize_falls_back_when_id_missing(config, monkeypatch):
    """批次 JSON 合法但漏了某条 -> 用原始摘要兜底并标注，而不是整体失败。"""
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {
            "items": [{"id": 1, "project_name": "A", "one_line": "B"}]
        },
    )
    result = client.summarize([make_item(1), make_item(2)])

    assert len(result) == 2
    assert result[0].fallback is False
    assert result[1].fallback is True
    assert "第 2 条摘要" in result[1].one_line


def test_summarize_raises_when_items_key_missing(config, monkeypatch):
    client = LLMClient(config)
    monkeypatch.setattr(client, "_chat_json", lambda system, payload: {"nope": []})
    with pytest.raises(LLMError):
        client.summarize([make_item(1)])


def test_summarize_splits_into_batches(config, monkeypatch, cfg):
    cfg("llm.batch_size", 2)
    cfg("llm.max_workers", 1)
    client = LLMClient(config)

    seen_sizes: list[int] = []

    def fake_chat(system, payload):
        candidates = payload["candidates"]
        seen_sizes.append(len(candidates))
        return {
            "items": [
                {"id": c["id"], "project_name": f"P{c['id']}", "one_line": "L"}
                for c in candidates
            ]
        }

    monkeypatch.setattr(client, "_chat_json", fake_chat)
    result = client.summarize([make_item(i) for i in range(1, 6)])

    assert sorted(seen_sizes) == [1, 2, 2]
    assert len(result) == 5


def test_summarize_empty_input(config):
    assert LLMClient(config).summarize([]) == []


def test_summarize_preserves_order(config, monkeypatch, cfg):
    cfg("llm.batch_size", 2)
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_chat_json",
        lambda system, payload: {
            "items": [
                # id 是「批内序号」（批次之间会重置），所以用标题里的全局编号
                # 拼出可验证的名字，否则第二批的 id=1 会被误当成第 1 条。
                {"id": c["id"], "project_name": f"P{c['title'].split()[-1]}", "one_line": "L"}
                for c in payload["candidates"]
            ]
        },
    )
    result = client.summarize([make_item(i) for i in range(1, 6)])
    assert [s.project_name for s in result] == ["P1", "P2", "P3", "P4", "P5"]


# ------------------------------------------------------------ SummaryItem
def test_summary_item_defaults():
    item = SummaryItem(project_name="X", one_line="Y")
    assert item.highlights == []
    assert item.fallback is False
    assert item.published_estimated is False
