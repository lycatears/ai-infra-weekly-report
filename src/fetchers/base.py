"""抓取层公共设施。

包含四件事：

1. :class:`NewsItem` —— 所有数据源统一的输出契约
2. :func:`parse_datetime` / :func:`parse_struct_time` —— 时间解析，统一成 UTC aware
3. :class:`HttpClient` —— 带超时、重试与指数退避的请求封装，**永不抛异常**
4. :class:`BaseFetcher` —— 抓取器基类，负责窗口过滤、脏数据剔除、源内去重

时间处理原则
------------
- 内部一律使用 **UTC aware** 的 ``datetime``；只有渲染报告时才转本地时区
- 无时区信息的时间戳按 ``assume_tz`` 解释并记 debug 日志
- 明显是「未来」的时间戳（晚于 ``now + FUTURE_TOLERANCE``）视为脏数据丢弃
- **发布时间只来自抓取结果，绝不采用大模型生成的文本**
"""

from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import time as _time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

UTC = timezone.utc

#: 晚于 ``now + 此值`` 的时间戳视为脏数据
FUTURE_TOLERANCE = timedelta(days=1)

#: 标题归一化时要去掉的常见前缀
_TITLE_PREFIXES = ("show hn:", "ask hn:", "tell hn:", "launch hn:")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


# --------------------------------------------------------------------- 工具
def strip_html(text: str | None) -> str:
    """去掉 HTML 标签与多余空白，用于把 RSS/HN 正文变成纯文本摘要。"""
    if not text:
        return ""
    without_tags = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(without_tags)).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def normalise_title(title: str) -> str:
    """标题归一化：小写、去标点、折叠空白、剥离 HN 前缀。

    刻意**保留**版本号（``v0.30.0`` 与 ``v0.30.1`` 是不同发布）。
    """
    text = (title or "").strip().lower()
    for prefix in _TITLE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def tokens(text: str) -> set[str]:
    """用于 Jaccard 相似度的词集合（长度 >= 2 的 token）。"""
    return {tok for tok in normalise_title(text).split() if len(tok) >= 2}


def parse_struct_time(value: Any) -> datetime | None:
    """把 ``time.struct_time`` 转成 UTC aware datetime（feedparser 用）。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_datetime(value: Any, *, assume_tz: timezone = UTC, label: str = "") -> datetime | None:
    """尽量把任意时间表示解析成 UTC aware 的 ``datetime``；失败返回 ``None``。"""
    if value is None or value == "":
        return None

    # 已经是 datetime
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=assume_tz)
        return dt.astimezone(UTC)

    # struct_time
    if isinstance(value, _time.struct_time):
        return parse_struct_time(value)

    # date（无时间部分）
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day, tzinfo=assume_tz).astimezone(UTC)

    # epoch 秒
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    # 字符串
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = dateutil_parser.parse(text)
        except (ValueError, OverflowError, TypeError):
            logger.debug("无法解析时间字符串 %r%s", text, f" ({label})" if label else "")
            return None
        if parsed.tzinfo is None:
            logger.debug(
                "时间 %r 缺少时区信息，按 %s 解释%s",
                text,
                assume_tz,
                f" ({label})" if label else "",
            )
            parsed = parsed.replace(tzinfo=assume_tz)
        return parsed.astimezone(UTC)

    return None


def is_future(dt: datetime, *, now: datetime | None = None) -> bool:
    reference = now or datetime.now(UTC)
    return dt > reference + FUTURE_TOLERANCE


# ------------------------------------------------------------------ 数据模型
@dataclass
class NewsItem:
    """一条候选新闻。"""

    source: str
    """来源标识，如 ``arxiv`` / ``github`` / ``hackernews``。"""

    title: str
    url: str
    published_at: datetime
    """UTC aware 的发布时间。"""

    summary: str = ""
    raw_score: float = 0.0
    """原始热度（HN points、GitHub stars 等），后续由 relevance 归一化。"""

    extra: dict[str, Any] = field(default_factory=dict)
    """来源特有的附加信息，如版本号、作者、分类。"""

    relevance_score: float = 0.0
    """由 :mod:`src.relevance` 填入的综合得分。"""

    @property
    def title_key(self) -> str:
        return normalise_title(self.title)

    @property
    def uid(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.url}".encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.astimezone(UTC).isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        payload = dict(data)
        payload["published_at"] = parse_datetime(payload.get("published_at")) or datetime.now(UTC)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.source}] {self.title} ({self.published_at:%Y-%m-%d %H:%M} UTC)"


# ------------------------------------------------------------------ HTTP
class HttpClient:
    """带重试的请求封装。所有方法在失败时返回 ``None``，由调用方决定降级策略。"""

    def __init__(
        self,
        *,
        timeout: float = 15,
        max_retries: int = 2,
        backoff_factor: float = 1.5,
        user_agent: str,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(0.1, float(backoff_factor))

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )

    # -- 内部 --
    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response | None:
        attempts = self.max_retries + 1
        last_error = ""

        for attempt in range(1, attempts + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                    data=data,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return response

                last_error = f"HTTP {response.status_code}"
                # 4xx（除 429）是确定性错误，重试没有意义
                if response.status_code < 500 and response.status_code != 429:
                    logger.warning("请求被拒绝（不重试）: %s -> %s", url, last_error)
                    return None
            if attempt < attempts:
                delay = self.backoff_factor ** attempt
                logger.debug("第 %d/%d 次请求失败 (%s)，%.1fs 后重试: %s", attempt, attempts, last_error, delay, url)
                _time.sleep(delay)

        logger.warning("请求在 %d 次尝试后仍失败: %s -> %s", attempts, url, last_error)
        return None

    # -- 对外 --
    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        response = self._request(url, params=params, headers=headers)
        if response is None:
            return None
        return response.text

    def get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes | None:
        """用于 feedparser：直接喂原始字节，避免编码猜测错误。"""
        response = self._request(url, params=params, headers=headers)
        if response is None:
            return None
        return response.content

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        response = self._request(url, params=params, headers=headers)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            logger.warning("响应不是合法 JSON: %s", url)
            return None

    def post_json(
        self,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        response = self._request(url, method="POST", json_body=json_body, data=data, headers=headers)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            logger.warning("响应不是合法 JSON: %s", url)
            return None

    def close(self) -> None:
        self._session.close()


# ------------------------------------------------------------------ 基类
class BaseFetcher(ABC):
    """抓取器基类。

    子类只需实现 :meth:`fetch`，把原始响应转成 ``list[NewsItem]``；
    窗口过滤、脏数据剔除、源内去重由 :meth:`postprocess` 统一处理。
    """

    name: str = "base"

    def __init__(self, config: Any, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self.cfg: dict[str, Any] = config.source_cfg(self.name)

    # -- 子类实现 --
    @abstractmethod
    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        """抓取并返回 ``[start_utc, end_utc]`` 窗口内的新闻。"""

    # -- 公共后处理 --
    def postprocess(
        self, items: list[NewsItem], start_utc: datetime, end_utc: datetime
    ) -> list[NewsItem]:
        """剔除越窗/未来/无时间/重复的数据，并按发布时间倒序。"""
        kept: list[NewsItem] = []
        seen_urls: set[str] = set()
        dropped_window = dropped_future = dropped_dup = dropped_invalid = 0

        for item in items:
            if not item.url or not item.title:
                dropped_invalid += 1
                continue
            if item.published_at is None:
                dropped_invalid += 1
                continue
            if item.published_at > end_utc:
                dropped_window += 1
                continue
            if item.published_at < start_utc:
                dropped_window += 1
                continue
            if is_future(item.published_at):
                dropped_future += 1
                continue
            if item.url in seen_urls:
                dropped_dup += 1
                continue
            seen_urls.add(item.url)
            kept.append(item)

        kept.sort(key=lambda it: it.published_at, reverse=True)

        if any((dropped_window, dropped_future, dropped_dup, dropped_invalid)):
            logger.debug(
                "%s 后处理：保留 %d，越窗 %d，未来时间 %d，源内重复 %d，字段缺失 %d",
                self.name,
                len(kept),
                dropped_window,
                dropped_future,
                dropped_dup,
                dropped_invalid,
            )
        return kept

    def safe_fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        """包裹 :meth:`fetch`：任何异常都只记日志，返回空列表。

        这是「单个数据源失败不影响整体」的实现点。
        """
        started = _time.monotonic()
        try:
            items = self.fetch(start_utc, end_utc)
        except Exception as exc:  # noqa: BLE001 - 故意隔离所有异常
            logger.warning("[%s] 抓取失败，已跳过该源: %s: %s", self.name, type(exc).__name__, exc)
            logger.debug("[%s] 详细堆栈", self.name, exc_info=True)
            return []

        result = self.postprocess(items, start_utc, end_utc)
        elapsed = _time.monotonic() - started
        logger.info(
            "[%-11s] 命中 %2d 条（原始 %2d 条，耗时 %.1fs）",
            self.name,
            len(result),
            len(items),
            elapsed,
        )
        return result

    # -- 便利方法 --
    def _sleep(self, key: str = "request_delay_ms") -> None:
        """按配置在连续请求之间轻微延时，避免触发限流。"""
        delay_ms = int(self.cfg.get(key, 0) or 0)
        if delay_ms > 0:
            _time.sleep(delay_ms / 1000.0)
