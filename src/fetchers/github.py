"""GitHub 抓取：关注仓库的 release atom + 新仓库搜索。

两种模式互补
------------
- ``releases``：抓 ``https://github.com/<repo>/releases.atom``。
  **走 atom 而非 REST API，因此不受 API 限流影响**，是成熟项目新版本的可靠来源。
- ``search``：走 REST 搜索 API，发现本周新建的高星项目。
  未认证时限额 10 次/分钟；若设置了环境变量 ``GITHUB_TOKEN`` 会自动带上以提高限额。

``mode`` 配置项取 ``releases`` / ``search`` / ``both``（默认 ``both``）。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote

import feedparser

from .base import BaseFetcher, NewsItem, parse_datetime, parse_struct_time, strip_html, truncate

logger = logging.getLogger(__name__)

RELEASE_ATOM = "https://github.com/{repo}/releases.atom"
SEARCH_API = "https://api.github.com/search/repositories"
TAG_MARKER = "/releases/tag/"

#: 预发布版本识别（作用在 tag 上）
_PRERELEASE_RE = re.compile(r"(?i)(rc|alpha|beta|pre|dev|nightly|canary)\d*\s*$")

#: 内置噪音 tag 规则。这些 tag 不是「发布」，而是 CI / 定时构建产物：
#: 例如 pytorch 的 ``viable/strict/<时间戳>``（URL 里会出现 viable%2Fstrict）、
#: LMCache 的 ``nightly-cu129``。它们会淹没真正的版本发布，必须过滤。
DEFAULT_NOISE_TAG_PATTERNS: tuple[str, ...] = (
    r"(?i)viable",  # pytorch CI 自动发布
    r"(?i)nightly",  # 夜间构建
    r"(?i)^ci[-/]",  # CI 产物
    r"(?i)^test[-/]",  # 测试产物
    r"(?i)^dev[-/]",  # 开发构建
    r"(?i)^trunk",  # pytorch 逐提交发布
    r"(?i)^unstable",
    r"(?i)canary",
)

#: 「像版本号」的判据：至少包含 ``数字.数字``。
#: ``v0.30.0`` / ``proto-v0.3.0`` 通过；``trunk/9858884bec…``、
#: ``viable/strict/1790353884``、``nightly-cu129`` 不通过。
#: 这比穷举关键词更鲁棒——新的 CI 产物命名风格也会被自动挡掉。
_VERSION_LIKE_RE = re.compile(r"\d+\.\d+")

SUMMARY_LIMIT = 500


def is_prerelease(tag: str) -> bool:
    """判断 release tag 是否为预发布版本。"""
    return bool(_PRERELEASE_RE.search((tag or "").strip()))


def is_noise_tag(tag: str, patterns: tuple[str, ...]) -> bool:
    """判断 tag 是否为 CI / 定时构建产物。"""
    text = (tag or "").strip()
    if not text:
        return True
    return any(re.search(pattern, text) for pattern in patterns)


def looks_like_version(tag: str) -> bool:
    """tag 是否形如版本号（至少含 ``数字.数字``）。"""
    return bool(_VERSION_LIKE_RE.search((tag or "").strip()))


class GithubFetcher(BaseFetcher):
    name = "github"

    def fetch(self, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        mode = str(self.cfg.get("mode", "both")).lower()
        items: list[NewsItem] = []

        if mode in ("releases", "both"):
            items.extend(self._fetch_releases())
        if mode in ("search", "both"):
            items.extend(self._fetch_search(start_utc))

        return items

    # ------------------------------------------------------------ releases
    def _fetch_releases(self) -> list[NewsItem]:
        repositories = [str(r) for r in (self.cfg.get("repositories") or []) if str(r).strip()]
        include_prerelease = bool(self.cfg.get("include_prerelease", False))
        require_version_like = bool(self.cfg.get("require_version_like_tag", True))

        configured = self.cfg.get("exclude_tag_patterns")
        patterns = tuple(
            str(p) for p in (configured if isinstance(configured, list) else DEFAULT_NOISE_TAG_PATTERNS)
        )
        try:
            for pattern in patterns:
                re.compile(pattern)
        except re.error as exc:
            logger.warning("sources.github.exclude_tag_patterns 含非法正则，已回退内置规则: %s", exc)
            patterns = DEFAULT_NOISE_TAG_PATTERNS

        items: list[NewsItem] = []
        for repo in repositories:
            raw = self.http.get_bytes(RELEASE_ATOM.format(repo=repo))
            if raw is None:
                self._sleep()
                continue

            feed = feedparser.parse(raw)
            for entry in feed.entries:
                item = self._release_to_item(
                    repo, entry, include_prerelease, patterns, require_version_like
                )
                if item is not None:
                    items.append(item)
            self._sleep()
        return items

    @staticmethod
    def _release_to_item(
        repo: str,
        entry: Any,
        include_prerelease: bool,
        noise_patterns: tuple[str, ...],
        require_version_like: bool,
    ) -> NewsItem | None:
        title = strip_html(entry.get("title", ""))
        if not title:
            return None

        url = str(entry.get("link") or "").strip()
        if not url:
            return None

        raw_tag = url.split(TAG_MARKER, 1)[-1] if TAG_MARKER in url else title
        tag = unquote(raw_tag)

        if is_noise_tag(tag, noise_patterns):
            logger.debug("github 过滤 CI/定时构建 tag: %s -> %s", repo, tag)
            return None
        if require_version_like and not looks_like_version(tag):
            logger.debug("github 过滤非版本号 tag: %s -> %s", repo, tag)
            return None
        if not include_prerelease and is_prerelease(tag):
            return None

        published = (
            parse_struct_time(entry.get("updated_parsed"))
            or parse_struct_time(entry.get("published_parsed"))
            or parse_datetime(entry.get("updated"))
            or parse_datetime(entry.get("published"))
        )
        if published is None:
            return None

        body = strip_html(entry.get("summary", "") or "")
        if not body:
            for content in entry.get("content") or []:
                if content.get("value"):
                    body = strip_html(content["value"])
                    break

        return NewsItem(
            source="github",
            title=f"{repo.split('/')[-1]} {tag}",
            url=url,
            published_at=published,
            summary=truncate(f"{title}. {body}".strip(". "), SUMMARY_LIMIT) if body else truncate(title, SUMMARY_LIMIT),
            raw_score=0.0,
            extra={
                "repo": repo,
                "tag": tag,
                "kind": "release",
                "release_title": title,
            },
        )

    # -------------------------------------------------------------- search
    def _fetch_search(self, start_utc: datetime) -> list[NewsItem]:
        search_cfg = self.cfg.get("search") or {}
        queries = [str(q) for q in (search_cfg.get("queries") or []) if str(q).strip()]
        if not queries:
            return []

        within_days = int(search_cfg.get("created_within_days", 7))
        cutoff = (start_utc - timedelta(days=max(0, within_days - 7))).date().isoformat()
        min_stars = int(search_cfg.get("min_stars", 0))
        per_page = 20

        items: list[NewsItem] = []
        for query in queries:
            payload = self.http.get_json(
                SEARCH_API,
                params={
                    "q": f"{query} created:>{cutoff}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                },
                headers=self._github_headers(),
            )
            self._sleep()
            if not isinstance(payload, dict):
                continue

            for repo in payload.get("items") or []:
                if not isinstance(repo, dict):
                    continue
                stars = repo.get("stargazers_count")
                stars = float(stars) if isinstance(stars, (int, float)) else 0.0
                if stars < min_stars:
                    continue

                published = parse_datetime(repo.get("created_at") or repo.get("pushed_at"))
                url = str(repo.get("html_url") or "").strip()
                full_name = str(repo.get("full_name") or "").strip()
                if published is None or not url or not full_name:
                    continue

                description = strip_html(repo.get("description") or "")
                items.append(
                    NewsItem(
                        source="github",
                        title=full_name,
                        url=url,
                        published_at=published,
                        summary=truncate(description or full_name, SUMMARY_LIMIT),
                        raw_score=stars,
                        extra={
                            "repo": full_name,
                            "kind": "repository",
                            "stars": int(stars),
                            "language": repo.get("language") or "",
                            "topics": [str(t) for t in (repo.get("topics") or [])][:8],
                        },
                    )
                )
        return items

    @staticmethod
    def _github_headers() -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
