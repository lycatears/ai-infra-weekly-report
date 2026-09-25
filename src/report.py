"""Markdown 报告渲染与落盘。

报告结构（每条新闻固定 6 个字段，与需求一致）
---------------------------------------------
``## 序号. 项目名称`` 小节内包含：

1. **项目名称**
2. **发表时间** —— 来自抓取结果，并在标注时区；估算时间会显式标出
3. **一句话总结**
4. **项目亮点**（无序列表）
5. **关键字**（行内代码）
6. **相关链接**（无序列表，可多条）

落盘采用「先写临时文件再原子替换」，写完后还会做一次结构校验；
校验不通过则视为失败，由上层决定不写 history（避免把未发布内容标记成已收录）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_text
from .llm import SummaryItem

logger = logging.getLogger(__name__)

SOURCE_LABELS: dict[str, str] = {
    "arxiv": "arXiv",
    "github": "GitHub",
    "hackernews": "Hacker News",
    "rss": "官方博客 RSS",
    "search_api": "搜索 API",
    "reddit": "Reddit",
}

#: 每条新闻必须出现的字段标签，用于写入后的结构校验
REQUIRED_FIELDS = ("项目名称", "发表时间", "一句话总结", "项目亮点", "关键字", "相关链接")

WEEKDAY_CN = "一二三四五六日"


def _label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "未知来源")


def _format_time(moment: datetime | None, tz: Any, *, estimated: bool = False) -> str:
    if moment is None:
        return "未知"
    local = moment.astimezone(tz)
    offset = local.strftime("%z")
    offset_text = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    text = f"{local:%Y-%m-%d %H:%M}（{getattr(tz, 'key', tz)} · {offset_text}）"
    return f"{text} ⚠️估算" if estimated else text


def render_report(
    *,
    config: Any,
    target_friday: date,
    covered_from: datetime,
    covered_to: datetime,
    summaries: list[SummaryItem],
    stats: dict[str, Any],
    generated_at: datetime,
) -> str:
    """渲染完整报告文本。"""
    tz = config.tz
    title_prefix = str(config.raw["report"]["title_prefix"])
    lines: list[str] = []

    lines.append(f"# {title_prefix} · {target_friday.isoformat()}")
    lines.append("")

    # ---------------- 元信息 ----------------
    lines.append("> **本期概览**")
    lines.append(">")
    lines.append(
        f"> - **覆盖时间**：{covered_from.astimezone(tz):%Y-%m-%d %H:%M} ~ "
        f"{covered_to.astimezone(tz):%Y-%m-%d %H:%M}（{config.timezone_name}）"
    )
    lines.append(f"> - **生成时间**：{generated_at.astimezone(tz):%Y-%m-%d %H:%M:%S}")
    lines.append(f"> - **生成模型**：{config.raw['llm']['model']}")

    per_source: dict[str, int] = stats.get("per_source") or {}
    if config.raw["report"].get("include_source_list", True) and per_source:
        detail = "、".join(
            f"{_label(name)} {count}" for name, count in per_source.items() if count
        )
        lines.append(f"> - **数据源**：{detail or '（无）'}")

    if config.raw["report"].get("include_stats_block", True):
        chain = (
            f"抓取 {stats.get('fetched', 0)} 条"
            f" → 去重后 {stats.get('after_dedupe', 0)} 条"
        )
        if stats.get("dropped_history"):
            chain += f" → 剔除历史已收录 {stats['dropped_history']} 条"
        if stats.get("keyword_filtered_out"):
            chain += f" → 关键词过滤掉 {stats['keyword_filtered_out']} 条"
        chain += (
            f" → 初筛选中 {stats.get('shortlisted', 0)} 条"
            f" → **本期收录 {len(summaries)} 条**"
        )
        lines.append(f"> - **筛选过程**：{chain}")

    tokens = stats.get("token_usage") or {}
    if tokens.get("total_tokens"):
        lines.append(
            f"> - **Token 用量**：{tokens['total_tokens']:,}"
            f"（输入 {tokens.get('prompt_tokens', 0):,} / 输出 {tokens.get('completion_tokens', 0):,}）"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # ---------------- 正文 ----------------
    if not summaries:
        lines.append("本期没有筛选出符合 AI Infra 范围的新闻。")
        lines.append("")
        return "\n".join(lines)

    for index, summary in enumerate(summaries, start=1):
        heading = summary.project_name.strip() or f"条目 {index}"
        lines.append(f"## {index}. {heading}")
        lines.append("")

        lines.append(f"- **项目名称**：{summary.project_name or '未知'}")
        lines.append(
            f"- **发表时间**：{_format_time(summary.published_at, tz, estimated=summary.published_estimated)}"
            f" · 来源：{_label(summary.source)}"
        )
        lines.append(f"- **一句话总结**：{summary.one_line or '（无）'}")

        if summary.highlights:
            lines.append("- **项目亮点**：")
            for highlight in summary.highlights:
                lines.append(f"  - {highlight}")
        else:
            lines.append("- **项目亮点**：详见下方链接原文")

        if summary.keywords:
            lines.append(
                "- **关键字**：" + "、".join(f"`{kw}`" for kw in summary.keywords)
            )
        else:
            lines.append("- **关键字**：（无）")

        if summary.links:
            lines.append("- **相关链接**：")
            for link in summary.links:
                lines.append(f"  - <{link}>")
        else:
            lines.append("- **相关链接**：（无）")

        if summary.fallback:
            lines.append("")
            lines.append(
                "> ⚠️ 本条未获得模型总结，以上为抓取到的原始摘要，仅供参考。"
            )

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def validate_report_text(text: str, *, min_items: int) -> list[str]:
    """写入后的结构校验，返回问题列表（空列表表示通过）。"""
    problems: list[str] = []

    if not text.strip():
        return ["报告内容为空"]

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    if len(headings) < min_items:
        problems.append(f"条目数 {len(headings)} 少于 report.min_items={min_items}")

    for field in REQUIRED_FIELDS:
        if f"**{field}**" not in text:
            problems.append(f"缺少字段「{field}」")

    if "相关链接" in text and "http" not in text:
        problems.append("相关链接中没有任何 URL")

    return problems


def write_report(path: Path, content: str) -> None:
    """原子写入报告文件。"""
    atomic_write_text(path, content)
    logger.info("报告已写入 %s（%d 字符）", path, len(content))
