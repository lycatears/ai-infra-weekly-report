"""调度与补跑判定测试。

这是整个项目最关键的测试：幂等性与「时间已过却未生成就立即补跑」的正确性
全靠 `src/schedule.py`。下面的用例覆盖 `decide()` 的全部分支。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.schedule import (
    Action,
    Reason,
    coverage_window,
    date_in_week,
    decide,
    find_pending_week,
    report_path_for,
    trigger_moment,
    week_start,
)

from .conftest import (
    FRIDAY_AFTER,
    FRIDAY_BEFORE,
    FRIDAY_TRIGGER,
    MONDAY,
    NEXT_MONDAY,
    PREV_FRIDAY,
    SATURDAY,
    TZ,
    WEEK_FRIDAY,
)


# ------------------------------------------------------------------ 周计算
def test_week_start_returns_monday():
    assert week_start(date(2026, 9, 25)) == date(2026, 9, 21)
    assert week_start(date(2026, 9, 21)) == date(2026, 9, 21)  # 周一自身
    assert week_start(date(2026, 9, 27)) == date(2026, 9, 21)  # 周日归属本周


def test_date_in_week():
    assert date_in_week(date(2026, 9, 26), 1) == date(2026, 9, 21)
    assert date_in_week(date(2026, 9, 21), 5) == date(2026, 9, 25)
    assert date_in_week(date(2026, 9, 26), 7) == date(2026, 9, 27)


def test_date_in_week_rejects_out_of_range():
    with pytest.raises(ValueError):
        date_in_week(date(2026, 9, 25), 0)
    with pytest.raises(ValueError):
        date_in_week(date(2026, 9, 25), 8)


def test_trigger_moment():
    moment = trigger_moment(date(2026, 9, 25), 5, 22, 0, TZ)
    assert moment == datetime(2026, 9, 25, 22, 0, tzinfo=TZ)


# ------------------------------------------------------------------ 路径
def test_report_path_uses_target_friday(config):
    path = report_path_for(config, WEEK_FRIDAY)
    # 文件名中的日期必须是「目标周五」，与运行时刻无关
    assert path.name == "AI-Infra-周报-2026-09-25-by-DeepSeek.md"
    assert path.parent == config.output_dir


# ------------------------------------------------------------------ 覆盖窗口
def test_window_extends_to_next_week_start(config):
    """尾窗应把终点推到下周一 00:00，消除覆盖空洞。"""
    start, end = coverage_window(config, WEEK_FRIDAY, SATURDAY)
    assert start == datetime(2026, 9, 21, 0, 0, tzinfo=TZ)
    # 周六运行时，终点被截断到「现在」，而不是未来的下周一
    assert end == SATURDAY


def test_window_clamped_by_now(config):
    """终点不得超过当前时刻——不能抓到未来的新闻。"""
    start, end = coverage_window(config, WEEK_FRIDAY, FRIDAY_TRIGGER)
    assert end == FRIDAY_TRIGGER
    assert end <= FRIDAY_TRIGGER


def test_window_respects_end_of_week_cap(config):
    """下周一之后运行，终点停在「下周一 00:00」而不是继续外扩。"""
    _, end = coverage_window(config, WEEK_FRIDAY, NEXT_MONDAY)
    assert end == datetime(2026, 9, 28, 0, 0, tzinfo=TZ)


def test_window_strict_when_extension_disabled(config, cfg):
    cfg("report.window_extend_to_next_week", False)
    _, end = coverage_window(config, WEEK_FRIDAY, SATURDAY)
    assert end == FRIDAY_TRIGGER


# ------------------------------------------------------------------ decide
def test_decide_not_due_before_trigger_when_prev_report_exists(config, cfg):
    """周五 21:00 且上周报告已存在 -> 未到点，跳过。"""
    cfg("schedule.lookback_weeks", 0)
    decision = decide(FRIDAY_BEFORE, config)
    assert decision.action is Action.SKIP
    assert decision.reason is Reason.NOT_DUE
    assert decision.target_friday == WEEK_FRIDAY


def test_decide_catchup_after_trigger_when_report_missing(config):
    """核心需求：时间已过但报告缺失 -> 立即补跑。"""
    decision = decide(FRIDAY_AFTER, config)
    assert decision.action is Action.GENERATE
    assert decision.reason is Reason.CATCHUP
    assert decision.target_friday == WEEK_FRIDAY
    assert decision.report_path.name.endswith("2026-09-25-by-DeepSeek.md")


def test_decide_catchup_exactly_at_trigger(config):
    """正好踩点也要判定为「已到点」，否则计划任务会空跑。"""
    decision = decide(FRIDAY_TRIGGER, config)
    assert decision.action is Action.GENERATE
    assert decision.reason is Reason.CATCHUP


def test_decide_tolerates_clock_skew(config):
    """计划任务可能早几百毫秒触发，容差内应判定为已到点。"""
    decision = decide(FRIDAY_TRIGGER - timedelta(seconds=30), config)
    assert decision.action is Action.GENERATE


def test_decide_idempotent_when_report_exists(config, cfg):
    """报告已存在 -> 跳过，且文件名不变（幂等的直接体现）。

    关闭回溯窗口，否则会因为「上一周报告缺失」而触发 backfill，
    干扰对幂等性的验证。
    """
    cfg("schedule.lookback_weeks", 0)
    report_path_for(config, WEEK_FRIDAY).write_text("# 已生成", encoding="utf-8")
    decision = decide(FRIDAY_AFTER, config)
    assert decision.action is Action.SKIP
    assert decision.reason is Reason.ALREADY_GENERATED
    assert decision.target_friday == WEEK_FRIDAY


def test_decide_saturday_backfills_same_week(config, cfg):
    """周六开机：本周报告缺失 -> 仍生成本周（而非下周）。"""
    cfg("schedule.lookback_weeks", 0)
    decision = decide(SATURDAY, config)
    assert decision.action is Action.GENERATE
    assert decision.reason is Reason.CATCHUP
    assert decision.target_friday == WEEK_FRIDAY


def test_decide_backfills_previous_week_on_monday(config):
    """周一才开机且上周报告缺失 -> 回溯补跑上周，避免永久缺失。"""
    decision = decide(NEXT_MONDAY, config)
    assert decision.action is Action.GENERATE
    assert decision.reason is Reason.BACKFILL
    assert decision.target_friday == WEEK_FRIDAY


def test_decide_skips_backfill_when_lookback_disabled(config, cfg):
    """关闭回溯后，上周缺失也不补，只处理本周。"""
    cfg("schedule.lookback_weeks", 0)
    decision = decide(NEXT_MONDAY, config)
    assert decision.action is Action.SKIP
    assert decision.reason is Reason.NOT_DUE
    assert decision.target_friday == date(2026, 10, 2)


def test_decide_prioritises_this_week_over_previous(config):
    """本周与上周都缺失时，优先补本周（最近优先）。"""
    decision = decide(NEXT_MONDAY, config)
    assert decision.target_friday == WEEK_FRIDAY


def test_decide_force_ignores_existing_report(config):
    report_path_for(config, WEEK_FRIDAY).write_text("# 已生成", encoding="utf-8")
    decision = decide(FRIDAY_AFTER, config, force=True)
    assert decision.action is Action.GENERATE
    assert decision.reason is Reason.FORCED


def test_decide_force_with_explicit_target(config):
    decision = decide(FRIDAY_AFTER, config, force=True, target_friday=PREV_FRIDAY)
    assert decision.target_friday == PREV_FRIDAY
    assert decision.report_path.name.endswith("2026-09-18-by-DeepSeek.md")


def test_decide_empty_report_file_counts_as_missing(config):
    """0 字节的报告按「缺失」处理，避免一次崩溃留下空文件后永远不再生成。"""
    report_path_for(config, WEEK_FRIDAY).write_text("", encoding="utf-8")
    decision = decide(FRIDAY_AFTER, config)
    assert decision.action is Action.GENERATE


def test_describe_reports_empty_file_as_to_be_rebuilt(config):
    """``describe()`` 的显示必须与判定同源：空文件既不能叫「已存在」也不该叫「不存在」。"""
    this_week = report_path_for(config, WEEK_FRIDAY)
    last_week = report_path_for(config, PREV_FRIDAY)

    # 空文件：判定为缺失，显示也要明确「将重建」，否则用户看不出为什么在重跑
    this_week.write_text("", encoding="utf-8")
    text = decide(FRIDAY_AFTER, config).describe()
    assert str(this_week) in text and "存在但为空，将重建" in text

    # 本周有内容、上周仍缺失 -> 判定转向补跑上周，显示的是上周路径
    this_week.write_text("# 有内容", encoding="utf-8")
    text = decide(FRIDAY_AFTER, config).describe()
    assert str(last_week) in text and "不存在" in text

    # 候选周全部齐备 -> 才是 already_generated，此时显示「已存在」
    last_week.write_text("# 有内容", encoding="utf-8")
    assert "已存在" in decide(FRIDAY_AFTER, config).describe()


def test_decide_accepts_naive_datetime(config):
    """naive 时间应按配置时区解释，而不是抛异常。"""
    naive = datetime(2026, 9, 25, 23, 0)
    decision = decide(naive, config)
    assert decision.action is Action.GENERATE


def test_find_pending_week_returns_none_when_nothing_due(config, cfg):
    cfg("schedule.lookback_weeks", 0)
    assert find_pending_week(FRIDAY_BEFORE, config) is None


def test_find_pending_week_reports_backfill(config):
    friday, reason = find_pending_week(MONDAY, config)
    assert reason is Reason.BACKFILL
    assert friday == PREV_FRIDAY


def test_decision_describe_is_human_readable(config):
    decision = decide(FRIDAY_AFTER, config)
    text = decision.describe()
    assert "GENERATE" in text
    assert "目标周五" in text
    assert "判定理由" in text
    assert "覆盖区间" in text


def test_decision_describe_marks_skip(config, cfg):
    cfg("schedule.lookback_weeks", 0)
    report_path_for(config, WEEK_FRIDAY).write_text("# ok", encoding="utf-8")
    text = decide(FRIDAY_AFTER, config).describe()
    assert "SKIP" in text
    assert "已存在" in text
