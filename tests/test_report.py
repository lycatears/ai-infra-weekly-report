"""报告渲染与结构校验测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.io_utils import atomic_write_text
from src.llm import SummaryItem
from src.report import (
    REQUIRED_FIELDS,
    render_report,
    validate_report_text,
    write_report,
)

from .conftest import FRIDAY_TRIGGER, TZ, WEEK_FRIDAY

UTC = timezone.utc


def make_summary(index: int = 1, **overrides) -> SummaryItem:
    base = dict(
        project_name=f"项目{index}",
        one_line=f"这是第 {index} 条的一句话总结。",
        highlights=["亮点一", "亮点二"],
        keywords=["KV Cache", "MoE"],
        links=[f"https://example.com/{index}"],
        published_at=datetime(2026, 9, 23, 5, 20, tzinfo=UTC),
        source="github",
        source_url=f"https://example.com/{index}",
    )
    base.update(overrides)
    return SummaryItem(**base)


def render(config, summaries, **stats) -> str:
    return render_report(
        config=config,
        target_friday=WEEK_FRIDAY,
        covered_from=datetime(2026, 9, 21, 0, 0, tzinfo=TZ),
        covered_to=FRIDAY_TRIGGER,
        summaries=summaries,
        stats={"fetched": 240, "after_dedupe": 240, "shortlisted": 21, **stats},
        generated_at=FRIDAY_TRIGGER,
    )


# ------------------------------------------------------------ 结构
def test_render_contains_all_six_required_fields(config):
    content = render(config, [make_summary(1)])
    for field in REQUIRED_FIELDS:
        assert f"**{field}**" in content


def test_render_has_one_heading_per_item(config):
    content = render(config, [make_summary(i) for i in range(1, 4)])
    headings = [line for line in content.splitlines() if line.startswith("## ")]
    assert len(headings) == 3
    assert headings[0] == "## 1. 项目1"
    assert headings[2] == "## 3. 项目3"


def test_render_includes_metadata_block(config):
    content = render(config, [make_summary(1)])
    assert "# AI Infra 周报 · 2026-09-25" in content
    assert "覆盖时间" in content
    assert "生成模型" in content
    assert "筛选过程" in content
    assert "抓取 240 条" in content


def test_render_lists_links_as_autolinks(config):
    content = render(config, [make_summary(1)])
    assert "<https://example.com/1>" in content


def test_render_formats_time_with_timezone(config):
    content = render(config, [make_summary(1)])
    assert "2026-09-23 13:20" in content  # 05:20 UTC -> 13:20 +08:00
    assert "Asia/Shanghai" in content


def test_render_marks_estimated_time(config):
    content = render(config, [make_summary(1, published_estimated=True)])
    assert "估算" in content


def test_render_marks_fallback_entries(config):
    content = render(config, [make_summary(1, fallback=True)])
    assert "未获得模型总结" in content


def test_render_handles_missing_highlights(config):
    content = render(config, [make_summary(1, highlights=[])])
    assert "**项目亮点**" in content


def test_render_handles_missing_links(config):
    content = render(config, [make_summary(1, links=[])])
    assert "**相关链接**：（无）" in content


def test_render_empty_summaries(config):
    content = render(config, [])
    assert "本期没有筛选出符合 AI Infra 范围的新闻" in content


def test_render_escapes_nothing_but_keeps_pipes(config):
    """标题里带竖线不应破坏 Markdown 表格以外的结构。"""
    content = render(config, [make_summary(1, project_name="A | B")])
    assert "## 1. A | B" in content


def test_render_shows_token_usage(config):
    content = render(config, [make_summary(1)], token_usage={"total_tokens": 12345, "prompt_tokens": 10000, "completion_tokens": 2345})
    assert "12,345" in content


def test_render_omits_source_block_when_disabled(config, cfg):
    cfg("report.include_source_list", False)
    content = render(config, [make_summary(1)], per_source={"github": 2})
    assert "**数据源**" not in content


def test_render_includes_source_block(config):
    content = render(config, [make_summary(1)], per_source={"github": 2, "arxiv": 66})
    assert "**数据源**" in content
    assert "GitHub 2" in content
    assert "arXiv 66" in content


# ------------------------------------------------------------ 校验
def test_validate_passes_for_good_report(config):
    content = render(config, [make_summary(i) for i in range(1, 9)])
    assert validate_report_text(content, min_items=8) == []


def test_validate_flags_too_few_items(config):
    content = render(config, [make_summary(1)])
    problems = validate_report_text(content, min_items=8)
    assert any("条目数" in p for p in problems)


def test_validate_flags_empty_content():
    assert validate_report_text("", min_items=1) == ["报告内容为空"]


def test_validate_flags_missing_field():
    content = "# 标题\n\n## 1. 项目\n\n- **项目名称**：X\n"
    problems = validate_report_text(content, min_items=1)
    assert any("发表时间" in p for p in problems)


def test_validate_flags_links_without_url():
    content = (
        "# 标题\n\n## 1. 项目\n\n"
        "- **项目名称**：X\n- **发表时间**：Y\n- **一句话总结**：Z\n"
        "- **项目亮点**：W\n- **关键字**：`a`\n- **相关链接**：无\n"
    )
    problems = validate_report_text(content, min_items=1)
    assert any("URL" in p for p in problems)


# ------------------------------------------------------------ 落盘
def test_write_report_creates_file(config):
    path = config.output_dir / "report.md"
    write_report(path, "# 内容\n")
    assert path.read_text(encoding="utf-8") == "# 内容\n"


def test_write_report_overwrites_atomically(config):
    path = config.output_dir / "report.md"
    write_report(path, "第一版")
    write_report(path, "第二版")
    assert path.read_text(encoding="utf-8") == "第二版"


def test_write_report_leaves_no_temp_files(config):
    path = config.output_dir / "report.md"
    write_report(path, "内容")
    leftovers = [p.name for p in config.output_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "c.txt"
    atomic_write_text(path, "深目录")
    assert path.read_text(encoding="utf-8") == "深目录"


def test_rendered_report_is_valid_utf8(config):
    content = render(config, [make_summary(1, one_line="中文内容 · emoji ✅")])
    path = config.output_dir / "r.md"
    write_report(path, content)
    assert "✅" in path.read_text(encoding="utf-8")
