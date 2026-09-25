"""周计算、补跑判定与幂等性。

核心不变式
----------
**报告文件名由「周」决定，与运行时刻无关。** 因此：

- 同一个周无论被触发多少次，只会产生一个报告文件（天然幂等）
- 周五 22:00 之后的任意时刻（周六、周日，甚至下周一）补跑，
  都能得到命名正确、归档位置正确的报告

判定流程（:func:`decide`）
--------------------------
1. ``--force`` → 无条件生成本周期望的报告
2. 依次检查候选周（本周 → 往回 ``lookback_weeks`` 周）：
   若该周的触发时刻已过且报告缺失 → 生成本周（``catchup``）或历史周（``backfill``）
3. 本周报告已存在 → ``already_generated``
4. 本周尚未到点 → ``not_due``

时间容差
--------
计划任务可能因时钟精度在 ``22:00:00`` 前后几百毫秒触发，因此判定时给
:data:`TRIGGER_TOLERANCE` 的宽限，避免「刚好踩点却判定为未到点」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

logger = logging.getLogger(__name__)

#: 判定「已到触发时刻」时允许的提前量
TRIGGER_TOLERANCE = timedelta(minutes=1)

#: ISO 星期编号
MONDAY = 1
SUNDAY = 7


class Action(str, Enum):
    GENERATE = "generate"
    SKIP = "skip"


class Reason(str, Enum):
    FORCED = "forced"
    CATCHUP = "catchup"
    BACKFILL = "backfill"
    ALREADY_GENERATED = "already_generated"
    NOT_DUE = "not_due"
    NOTHING_TO_DO = "nothing_to_do"


_REASON_TEXT = {
    Reason.FORCED: "使用了 --force，无条件生成",
    Reason.CATCHUP: "本周触发时刻已过但报告缺失，立即补跑",
    Reason.BACKFILL: "历史周触发时刻已过但报告缺失，回溯补跑",
    Reason.ALREADY_GENERATED: "本周报告已存在，跳过（幂等）",
    Reason.NOT_DUE: "尚未到本周的触发时刻，跳过",
    Reason.NOTHING_TO_DO: "没有需要处理的目标周",
}


@dataclass(frozen=True)
class Decision:
    """一次运行应该做什么。"""

    action: Action
    reason: Reason
    detail: str = ""
    target_friday: date | None = None
    report_path: Path | None = None
    covered_from: datetime | None = None
    covered_to: datetime | None = None

    @property
    def should_generate(self) -> bool:
        return self.action is Action.GENERATE

    def describe(self) -> str:
        head = "GENERATE" if self.should_generate else "SKIP"
        lines = [f"{head} ({self.reason.value})"]
        if self.target_friday is not None:
            lines.append(f"  目标周五 : {self.target_friday.isoformat()}")
        if self.report_path is not None:
            if path_has_content(self.report_path):
                exists = "已存在"
            elif self.report_path.is_file():
                exists = "存在但为空，将重建"
            else:
                exists = "不存在"
            lines.append(f"  报告路径 : {self.report_path} ({exists})")
        if self.covered_from and self.covered_to:
            lines.append(
                "  覆盖区间 : "
                f"{self.covered_from:%Y-%m-%d %H:%M} ~ {self.covered_to:%Y-%m-%d %H:%M}"
            )
        lines.append(f"  判定理由 : {self.detail or _REASON_TEXT.get(self.reason, '')}")
        return "\n".join(lines)


# ------------------------------------------------------------------ 周计算
def week_start(anchor: date) -> date:
    """返回 ``anchor`` 所在 ISO 周的周一。"""
    return anchor - timedelta(days=anchor.weekday())


def date_in_week(anchor: date, weekday: int) -> date:
    """返回 ``anchor`` 所在 ISO 周中、ISO 星期编号为 ``weekday`` 的日期。

    ``weekday`` 取值 1（周一）~ 7（周日）。
    """
    if not 1 <= weekday <= 7:
        raise ValueError(f"weekday 必须在 1..7，收到 {weekday}")
    return week_start(anchor) + timedelta(days=weekday - 1)


def trigger_moment(anchor: date, weekday: int, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    """返回 ``anchor`` 所在周的触发时刻（带时区）。"""
    day = date_in_week(anchor, weekday)
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=tz)


def normalise_now(now: datetime, tz: ZoneInfo) -> datetime:
    """把 ``now`` 规范为带时区的本地时间；naive 时间按 ``tz`` 解释。"""
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


# ------------------------------------------------------------------ 路径
def report_path_for(config: "Config", target_friday: date) -> Path:
    """按 ``app.filename_template`` 计算目标报告路径。"""
    filename = config.filename_template.format(date=target_friday.isoformat())
    return config.output_dir / filename


def report_exists(config: "Config", target_friday: date) -> bool:
    return path_has_content(report_path_for(config, target_friday))


def path_has_content(path: Path) -> bool:
    """报告是否「可用」。

    **0 字节文件视为不存在。** 报告写入是原子的（先写临时文件再 ``os.replace``），
    但断电、磁盘写满或进程被强杀仍可能留下一个空文件。若把它当成「已生成」，
    这一周就会被永久跳过；当成缺失则可以安全重跑。
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:  # pragma: no cover - 权限等异常
        logger.warning("无法检查报告是否存在: %s", path, exc_info=True)
        return False


# ------------------------------------------------------------------ 窗口
def trigger_window(config: "Config", target_friday: date) -> tuple[datetime, datetime]:
    """标称窗口 ``[本周起点, 触发时刻]``，即「周一 00:00 ~ 周五 22:00」。"""
    tz = config.tz
    start = datetime.combine(
        date_in_week(target_friday, config.raw["report"]["window_start_weekday"]),
        time(0, 0),
        tzinfo=tz,
    )
    if config.raw["report"]["window_end_uses_trigger"]:
        end = trigger_moment(
            target_friday,
            config.schedule_weekday,
            config.schedule_hour,
            config.schedule_minute,
            tz,
        )
    else:  # pragma: no cover - 非默认分支
        end = datetime.now(tz)
    return start, end


def coverage_window(
    config: "Config", target_friday: date, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """实际抓取窗口 = 标称窗口 [+ 尾窗]，并截断到 ``now``。

    尾窗（``report.window_extend_to_next_week``）用于消除一个覆盖空洞：
    若窗口严格截止在周五 22:00，那么「周五 22:00 ~ 下周一 00:00」之间发布的
    新闻既不属于本期、也不属于下一期，会被永久漏掉。开启后终点延到**下一周的
    窗口起点**（默认即下周一 00:00），使相邻两期首尾相接。

    用「下一周起点」而不是硬编码小时数，是为了让窗口在
    ``schedule.hour`` / ``window_start_weekday`` 被改动时依然正确。

    终点会被截断到 ``now``，保证永远不会抓到「未来」的新闻；因此在正常的
    周五 22:00 触发场景下，尾窗内容尚不存在，行为与严格窗口完全一致。
    """
    tz = config.tz
    start, end = trigger_window(config, target_friday)

    if config.raw["report"]["window_extend_to_next_week"]:
        next_week_start = datetime.combine(
            date_in_week(
                target_friday + timedelta(weeks=1),
                config.raw["report"]["window_start_weekday"],
            ),
            time(0, 0),
            tzinfo=tz,
        )
        local_now = normalise_now(now, tz) if now is not None else datetime.now(tz)
        end = min(next_week_start, local_now)

    if end <= start:  # pragma: no cover - 仅当窗口配置矛盾或强制跑未来周时
        logger.warning("覆盖区间终点不晚于起点，已回退为 7 天窗口")
        end = start + timedelta(days=7)
    return start, end


# ------------------------------------------------------------------ 判定
def _candidate_weeks(config: "Config", today: date) -> list[date]:
    """返回候选目标周五，按「最近优先」排序。"""
    this_friday = date_in_week(today, config.schedule_weekday)
    depth = config.lookback_weeks if config.catchup_on_start else 0
    return [this_friday - timedelta(weeks=back) for back in range(depth + 1)]


def find_pending_week(
    now: datetime, config: "Config"
) -> tuple[date, Reason] | None:
    """从本周往回找第一个「已到点但报告缺失」的周。

    返回 ``(目标周五, 原因)``；全部已生成或均未到点时返回 ``None``。
    """
    tz = config.tz
    local_now = normalise_now(now, tz)
    today = local_now.date()
    this_friday = date_in_week(today, config.schedule_weekday)

    for friday in _candidate_weeks(config, today):
        trigger = trigger_moment(
            friday, config.schedule_weekday, config.schedule_hour, config.schedule_minute, tz
        )
        if local_now + TRIGGER_TOLERANCE < trigger:
            logger.debug("候选周 %s 尚未到触发时刻 %s", friday, trigger)
            continue
        if report_exists(config, friday):
            logger.debug("候选周 %s 的报告已存在", friday)
            continue
        reason = Reason.CATCHUP if friday == this_friday else Reason.BACKFILL
        return friday, reason
    return None


def decide(
    now: datetime,
    config: "Config",
    *,
    force: bool = False,
    target_friday: date | None = None,
) -> Decision:
    """决定本次运行是否生成报告。

    ``force=True`` 时无条件生成 ``target_friday``（默认本周五）对应的报告。
    """
    tz = config.tz
    local_now = normalise_now(now, tz)
    today = local_now.date()
    this_friday = date_in_week(today, config.schedule_weekday)

    def build(friday: date, reason: Reason) -> Decision:
        start, end = coverage_window(config, friday, local_now)
        detail = _REASON_TEXT[reason]
        if reason is Reason.NOT_DUE:
            trigger = trigger_moment(
                friday, config.schedule_weekday, config.schedule_hour, config.schedule_minute, tz
            )
            detail = f"{detail}（触发时刻 {trigger:%Y-%m-%d %H:%M %Z}）"
        elif reason is Reason.ALREADY_GENERATED:
            path = report_path_for(config, friday)
            detail = f"{detail}（{path.name}）"
        return Decision(
            action=Action.GENERATE if reason in (Reason.FORCED, Reason.CATCHUP, Reason.BACKFILL) else Action.SKIP,
            reason=reason,
            detail=detail,
            target_friday=friday,
            report_path=report_path_for(config, friday),
            covered_from=start,
            covered_to=end,
        )

    if force:
        friday = target_friday or this_friday
        logger.warning("--force 生效：忽略存在性检查，生成 %s 的报告", friday)
        return build(friday, Reason.FORCED)

    pending = find_pending_week(local_now, config)
    if pending is not None:
        friday, reason = pending
        return build(friday, reason)

    if report_exists(config, this_friday):
        return build(this_friday, Reason.ALREADY_GENERATED)

    return build(this_friday, Reason.NOT_DUE)


def previous_week_report(config: "Config", target_friday: date) -> Path | None:
    """返回上一周的报告路径（若存在），用于报告中做「上期」链接。"""
    prev = target_friday - timedelta(weeks=1)
    path = report_path_for(config, prev)
    return path if path.is_file() else None
