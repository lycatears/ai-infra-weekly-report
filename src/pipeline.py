"""流水线编排。

   抓取 -> 报告内去重 -> 跨周去重 -> 相关性打分 -> 大模型初筛 -> 分批总结
        -> 渲染 Markdown -> 结构校验 -> 原子落盘 -> 更新 history

失败语义
--------
- 抓取全部无产出        -> 退出码 1（不生成空报告）
- 大模型不可用/响应不可解析 -> 退出码 1（**不生成残缺报告、不写 history**）
- 渲染结果结构校验不通过  -> 退出码 1（同上）
- ``--no-llm``          -> 只跑前半段，把候选导出到 state/ 供人工核对，退出码 0

只有「报告确实写成功」之后才更新 ``history.json``。这条顺序是整个跨周去重
正确性的前提：否则一次失败运行会把本周内容标记成已收录，导致永久漏报。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .dedupe import dedupe_within_report, drop_seen_before
from .fetchers import fetch_all
from .fetchers.base import NewsItem
from .history import History
from .llm import LLMClient, LLMError, SummaryItem
from .relevance import score_items, select_candidates
from .report import render_report, validate_report_text, write_report

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1


def _dedupe_cfg(config: Any) -> dict[str, Any]:
    return config.raw.get("dedupe", {})


def _dump_candidates(config: Any, target_friday: date, items: list[NewsItem]) -> Path:
    """把候选清单导出成 JSON，便于在未配置 api_key 时人工核对抓取质量。"""
    path = config.state_dir / f"_candidates-{target_friday.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_friday": target_friday.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_dry_run_table(items: list[NewsItem], config: Any) -> None:
    tz = config.tz
    print()
    print("=" * 100)
    print(f"{'#':>3}  {'发布时间':<17}{'来源':<12}{'相关分':>7}  {'关键词':<28}标题")
    print("-" * 100)
    for index, item in enumerate(items, start=1):
        stamp = f"{item.published_at.astimezone(tz):%Y-%m-%d %H:%M}"
        hits = ",".join((item.extra.get("keyword_hits") or [])[:3])
        print(
            f"{index:>3}  {stamp:<17}{item.source:<12}{item.relevance_score:>7.3f}  "
            f"{hits[:28]:<28}{item.title[:52]}"
        )
    print("=" * 100)


def run_pipeline(
    config: Any,
    *,
    target_friday: date,
    covered_from: datetime,
    covered_to: datetime,
    report_path: Path | None,
    skip_llm: bool = False,
    dry_run: bool = False,
    limit_sources: list[str] | None = None,
) -> int:
    """执行完整流程，返回进程退出码。"""
    dd = _dedupe_cfg(config)
    strip_params = tuple(str(p) for p in (dd.get("strip_query_params") or ()))

    start_utc = covered_from.astimezone(timezone.utc)
    end_utc = covered_to.astimezone(timezone.utc)

    logger.info("=" * 72)
    logger.info("开始生成周报：目标周五 %s", target_friday.isoformat())
    logger.info(
        "抓取窗口：%s ~ %s (UTC)", f"{start_utc:%Y-%m-%d %H:%M}", f"{end_utc:%Y-%m-%d %H:%M}"
    )
    if dry_run:
        logger.warning("--dry-run 生效：会正常生成报告内容，但不写入 history.json")
    if skip_llm:
        logger.warning("--no-llm 生效：跳过所有大模型调用，仅导出候选清单")

    # 提前失败：没配 key 就不必先花 90 秒抓取
    if not skip_llm and not config.llm_configured:
        logger.error(
            "llm.api_key 未配置，无法调用大模型。请在 %s 中填写 llm.api_key，"
            "或改用 --no-llm 仅验证抓取链路。",
            config.source_path,
        )
        return EXIT_RUNTIME_ERROR

    stats: dict[str, Any] = {}

    # ---------------------------------------------------------- 1. 抓取
    fetched, per_source = fetch_all(
        config, start_utc, end_utc, limit_sources=limit_sources
    )
    stats["fetched"] = len(fetched)
    stats["per_source"] = per_source

    if not fetched:
        logger.error(
            "所有数据源均未返回任何候选，终止本次运行（不生成空报告）。"
            "可用 `python scripts/check_sources.py` 排查数据源连通性。"
        )
        return EXIT_RUNTIME_ERROR

    # ------------------------------------------------- 2. 报告内去重
    deduped, _ = dedupe_within_report(
        fetched,
        strip_params=strip_params,
        ratio_threshold=float(dd.get("title_similarity_threshold", 0.88)),
        jaccard_threshold=float(dd.get("token_jaccard_threshold", 0.80)),
    )
    stats["after_dedupe"] = len(deduped)

    # ------------------------------------------------- 3. 跨周去重
    history = History(
        config.history_path,
        keep_weeks=int(dd.get("history_weeks", 4)),
        strip_params=strip_params,
    )
    history.load()
    history.prune(target_friday)
    fresh, dropped = drop_seen_before(deduped, history.is_seen)
    stats["dropped_history"] = len(dropped)
    stats["after_history"] = len(fresh)

    if not fresh:
        logger.error(
            "跨周去重后没有剩余候选（%d 条全部在本窗口 %d 周内收录过），终止本次运行。"
            "若确属重复窗口过长，可调小 dedupe.history_weeks。",
            len(dropped),
            int(dd.get("history_weeks", 4)),
        )
        return EXIT_RUNTIME_ERROR

    # ------------------------------------------------- 4. 相关性打分与筛选
    score_items(fresh, config)
    shortlist, rel_stats = select_candidates(fresh, config)
    stats["keyword_filtered_out"] = rel_stats["filtered_out"]
    stats["shortlisted"] = len(shortlist)

    if not shortlist:
        logger.error("相关性筛选后没有候选，终止本次运行。")
        return EXIT_RUNTIME_ERROR

    if skip_llm:
        dump_path = _dump_candidates(config, target_friday, shortlist)
        logger.info("候选清单已导出：%s", dump_path)
        _print_dry_run_table(shortlist, config)
        logger.info(
            "各源命中：%s",
            ", ".join(f"{k}={v}" for k, v in per_source.items()),
        )
        print(
            f"\n[--no-llm] 抓取 {stats['fetched']} 条 -> 去重后 {stats['after_dedupe']} 条"
            f" -> 剔除历史 {stats['dropped_history']} 条 -> 关键词过滤掉"
            f" {stats['keyword_filtered_out']} 条 -> 候选 {len(shortlist)} 条"
        )
        print(f"[--no-llm] 候选清单：{dump_path}")
        return EXIT_OK

    # ------------------------------------------------- 5. 大模型两阶段
    assert report_path is not None
    client = LLMClient(config)
    try:
        selected = client.shortlist(shortlist)
        summaries: list[SummaryItem] = client.summarize(selected)
    except LLMError as exc:
        logger.error("大模型阶段失败，本次运行中止（不生成报告、不更新 history）: %s", exc)
        return EXIT_RUNTIME_ERROR

    stats["token_usage"] = client.token_usage

    if not summaries:
        logger.error("大模型未产出任何条目，终止本次运行。")
        return EXIT_RUNTIME_ERROR

    # ------------------------------------------------- 6. 渲染与校验
    generated_at = datetime.now(config.tz)
    content = render_report(
        config=config,
        target_friday=target_friday,
        covered_from=covered_from,
        covered_to=covered_to,
        summaries=summaries,
        stats=stats,
        generated_at=generated_at,
    )

    min_items = int(config.raw["report"]["min_items"])
    problems = validate_report_text(content, min_items=min_items)
    if problems:
        logger.error("报告结构校验未通过，已放弃写入：%s", "; ".join(problems))
        return EXIT_RUNTIME_ERROR

    # ------------------------------------------------- 7. 落盘
    if dry_run:
        dry_path = config.state_dir / f"_dryrun-{target_friday.isoformat()}.md"
        write_report(dry_path, content)
        logger.warning("--dry-run：报告已写入 %s，history 未更新", dry_path)
        print(f"\n[--dry-run] 报告（未发布）：{dry_path}")
        return EXIT_OK

    write_report(report_path, content)

    # ------------------------------------------------- 8. 更新历史
    by_url = {item.url: item for item in selected}
    recordable = [by_url[s.source_url] for s in summaries if s.source_url in by_url]
    added = history.record(recordable, target_friday)
    history.save()
    logger.info("history 新增 %d 条", added)

    logger.info("=" * 72)
    logger.info("周报生成成功：%s", report_path)
    logger.info(
        "本期收录 %d 条（抓取 %d，去重后 %d，候选 %d，token %s）",
        len(summaries),
        stats["fetched"],
        stats["after_dedupe"],
        stats["shortlisted"],
        f"{stats['token_usage'].get('total_tokens', 0):,}",
    )
    logger.info("=" * 72)

    print(f"\n✅ 周报已生成：{report_path}")
    print(f"   收录 {len(summaries)} 条 | 候选 {stats['fetched']} 条 | 去重后 {stats['after_dedupe']} 条")
    return EXIT_OK
