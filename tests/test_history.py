"""跨周去重状态（history.json）测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from src.history import History
from src.fetchers.base import NewsItem

UTC = timezone.utc
STRIP = ["utm_source", "ref"]


def make_item(title: str, url: str, *, source: str = "arxiv") -> NewsItem:
    return NewsItem(
        source=source,
        title=title,
        url=url,
        published_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        summary="摘要",
    )


def test_load_missing_file_starts_empty(tmp_path):
    history = History(tmp_path / "history.json", keep_weeks=4, strip_params=STRIP)
    history.load()
    assert len(history) == 0
    assert not history.is_seen(make_item("任意", "https://e.com/x"))


def test_record_and_reload_roundtrip(tmp_path):
    path = tmp_path / "history.json"

    first = History(path, keep_weeks=4, strip_params=STRIP)
    first.load()
    added = first.record([make_item("vLLM v0.30.0", "https://e.com/vllm")], date(2026, 9, 25))
    first.save()

    assert added == 1
    assert path.exists()

    second = History(path, keep_weeks=4, strip_params=STRIP)
    second.load()
    assert len(second) == 1
    assert second.is_seen(make_item("vLLM v0.30.0", "https://e.com/vllm"))


def test_is_seen_matches_url_after_normalisation(tmp_path):
    history = History(tmp_path / "history.json", keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("标题", "https://example.com/post")], date(2026, 9, 25))

    # 带跟踪参数、带 www 的同一链接也应命中
    assert history.is_seen(make_item("别的标题", "https://www.example.com/post?utm_source=x"))


def test_is_seen_matches_normalised_title(tmp_path):
    history = History(tmp_path / "history.json", keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("Show HN: KV Cache 优化实践", "https://e.com/1")], date(2026, 9, 25))

    # URL 不同但标题归一化后一致 -> 命中
    assert history.is_seen(make_item("kv cache 优化实践", "https://e.com/2"))


def test_is_seen_does_not_match_different_version(tmp_path):
    """回归测试：v0.30.1 不应因为 v0.30.0 已收录而被剔除。"""
    history = History(tmp_path / "history.json", keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("vLLM v0.30.0", "https://e.com/v0.30.0")], date(2026, 9, 25))

    assert not history.is_seen(make_item("vLLM v0.30.1", "https://e.com/v0.30.1"))


def test_record_skips_duplicates(tmp_path):
    history = History(tmp_path / "history.json", keep_weeks=4, strip_params=STRIP)
    history.load()
    item = make_item("A", "https://e.com/a")
    assert history.record([item], date(2026, 9, 25)) == 1
    assert history.record([item], date(2026, 9, 25)) == 0
    assert len(history) == 1


def test_prune_removes_outdated_entries(tmp_path):
    path = tmp_path / "history.json"
    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("旧", "https://e.com/old")], date(2026, 1, 2))
    history.record([make_item("新", "https://e.com/new")], date(2026, 9, 25))

    removed = history.prune(date(2026, 9, 25))
    assert removed == 1
    assert len(history) == 1
    assert history.is_seen(make_item("新", "https://e.com/new"))


def test_prune_with_zero_weeks_clears_all(tmp_path):
    history = History(tmp_path / "history.json", keep_weeks=0, strip_params=STRIP)
    history.load()
    history.record([make_item("A", "https://e.com/a")], date(2026, 9, 25))
    assert history.prune(date(2026, 9, 25)) == 1
    assert len(history) == 0


def test_corrupt_file_is_quarantined_not_fatal(tmp_path):
    """损坏的历史文件不应让整条流水线失败。"""
    path = tmp_path / "history.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()

    assert len(history) == 0
    assert not path.exists()
    backups = list(tmp_path.glob("history.json.corrupt-*"))
    assert len(backups) == 1


def test_wrong_shape_is_quarantined(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"version": 1, "entries": "不是列表"}), encoding="utf-8")

    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()
    assert len(history) == 0


def test_save_is_noop_when_unchanged(tmp_path):
    path = tmp_path / "history.json"
    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()
    history.save()
    assert not path.exists()


def test_saved_file_is_valid_utf8_json(tmp_path):
    path = tmp_path / "history.json"
    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("中文标题", "https://e.com/zh")], date(2026, 9, 25))
    history.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["entries"][0]["title_key"] == "中文标题"


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "history.json"
    history = History(path, keep_weeks=4, strip_params=STRIP)
    history.load()
    history.record([make_item("A", "https://e.com/a")], date(2026, 9, 25))
    history.save()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
