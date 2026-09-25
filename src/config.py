"""配置加载与校验。

设计原则
--------
1. 缺失任意键都用 :data:`DEFAULTS` 补全，**不报错**——这样升级 `config.yaml`
   时旧文件依然可用。
2. 只有「类型错误」「关键占位符缺失」「取值越界」才抛 :class:`ConfigError`，
   由 `main.py` 捕获后以退出码 2 终止。
3. 支持 ``config.local.yaml`` 覆盖层，便于把密钥放在不入库的文件里。
4. ``llm.api_key`` 为空时会回退读取环境变量 ``AI_INFRA_LLM_API_KEY``；
   两者都为空则保持为空（由上层决定硬失败）。
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

logger = logging.getLogger(__name__)

#: 当前配置结构版本，用于将来做迁移
CONFIG_VERSION = 1

#: 配置非法时的退出码
EXIT_CONFIG_ERROR = 2

#: 项目根目录（`src/` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LOCAL_OVERLAY_PATH = PROJECT_ROOT / "config.local.yaml"

#: `llm.api_key` 为空时的环境变量回退
ENV_LLM_API_KEY = "AI_INFRA_LLM_API_KEY"

DEFAULT_USER_AGENT = "ai-infra-weekly-report/1.0 (+https://github.com/local/ai-infra-weekly-report)"

#: 全部默认值。与 `config.yaml` 保持一致，config.yaml 可只写需要覆盖的项。
DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "app": {
        "timezone": "Asia/Shanghai",
        "output_dir": ".",
        "filename_template": "AI-Infra-周报-{date}-by-DeepSeek.md",
        "state_dir": "state",
        "log_dir": "logs",
        "log_level": "INFO",
        "log_retention_days": 30,
    },
    "schedule": {
        "weekday": 5,
        "hour": 22,
        "minute": 0,
        "lookback_weeks": 1,
        "catchup_on_start": True,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_tokens": 4096,
        "timeout": 120,
        "max_retries": 3,
        "shortlist_size": 30,
        "batch_size": 5,
        "max_workers": 3,
        "use_json_mode": True,
    },
    "report": {
        "title_prefix": "AI Infra 周报",
        "max_items": 15,
        "min_items": 8,
        "window_start_weekday": 1,
        "window_end_uses_trigger": True,
        # 是否把窗口终点延到「下一周的起点」，使相邻两期首尾相接。
        # 若不延，则「周五 22:00 ~ 下周一 00:00」发布的新闻既不属于本期、
        # 也不属于下期，会被永久漏掉。实际终点会截断到本次运行时刻，
        # 因此不会抓到「未来」的新闻。
        "window_extend_to_next_week": True,
        "include_stats_block": True,
        "include_source_list": True,
        "links_per_item": 3,
    },
    "dedupe": {
        "history_file": "state/history.json",
        "history_weeks": 4,
        "title_similarity_threshold": 0.88,
        "token_jaccard_threshold": 0.80,
        "strip_query_params": [
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "ref",
            "source",
            "fbclid",
            "gclid",
        ],
    },
    "relevance": {
        # "inference" 是最被滥用的词（统计学论文里的「统计推断」也算），
        # 权重刻意压低，靠具体短语区分真正的推理系统话题。
        "keywords": {
            "kv cache": 3.0,
            "prefix cache": 3.0,
            "paged attention": 3.0,
            "speculative decoding": 3.0,
            "inference engine": 3.0,
            "inference server": 3.0,
            "continuous batching": 3.0,
            "tensor parallel": 2.8,
            "pipeline parallel": 2.8,
            "expert parallel": 2.8,
            "context parallel": 2.8,
            "data parallel": 2.5,
            "gpu kernel": 2.8,
            "cuda kernel": 2.8,
            "memory bandwidth": 2.5,
            "flash attention": 2.5,
            "gpu memory": 2.2,
            "throughput": 1.3,
            "latency": 1.3,
            "quantization": 2.0,
            "mixture of experts": 2.0,
            "moe": 2.0,
            "disaggregation": 2.0,
            "prefill": 2.0,
            "decode": 0.8,
            "batching": 2.0,
            "checkpoint": 1.8,
            "vllm": 2.5,
            "sglang": 2.5,
            "tensorrt": 2.2,
            "triton": 1.8,
            "cuda": 1.8,
            "rocm": 1.8,
            "nvlink": 1.8,
            "hbm": 1.8,
            "fp8": 1.8,
            "int4": 1.5,
            "awq": 1.5,
            "gptq": 1.5,
            "lora": 1.2,
            "serving": 1.8,
            "distributed training": 1.5,
            "scheduler": 1.5,
            "inference": 0.8,
            "memory": 0.5,
        },
        # 负向词典：命中即从关键词分中扣减，用于压掉歧义误报
        "negative_keywords": {
            "conformal inference": 3.0,
            "statistical inference": 3.0,
            "causal inference": 3.0,
            "bayesian inference": 3.0,
            "variational inference": 2.5,
            "selective inference": 3.0,
            "approximate inference": 2.5,
            "inference for": 1.5,
            "this page": 2.5,
            "web server": 2.0,
            "federated learning": 1.5,
            "wireless": 1.5,
            "survey": 1.0,
            "curriculum": 1.5,
        },
        "source_weights": {
            "arxiv": 1.2,
            "github": 1.1,
            "hackernews": 1.0,
            "rss": 1.0,
            "search_api": 0.9,
            "reddit": 0.8,
        },
        "score_weights": {"keyword": 1.0, "heat": 0.5, "source": 0.3},
        # 低于该关键词分的候选在送进大模型前被过滤（0 = 不过滤）。
        # 若过滤后候选数低于 report.min_items 会自动放宽，不会开天窗。
        "min_keyword_score": 1.5,
        # 短名单中单个来源最多贡献多少条（0 = 不限制）。
        # arXiv 一次能返回上百条论文，不设配额会把工程发布挤掉。
        "shortlist_per_source_cap": 12,
    },
    "sources": {
        "hackernews": {
            "enabled": True,
            "queries": [
                "LLM inference",
                "vLLM",
                "KV cache",
                "GPU kernel",
                "MoE",
                "quantization",
                "Triton",
                "CUDA",
                "distributed training",
                "model serving",
            ],
            "hits_per_page": 50,
            "min_points": 10,
            "include_front_page": True,
            "request_delay_ms": 120,
        },
        "reddit": {
            "enabled": True,
            "subreddits": ["MachineLearning", "LocalLLaMA", "mlops"],
            "listing": "top",
            "time_filter": "week",
            "limit": 50,
            "min_score": 20,
        },
        "arxiv": {
            "enabled": True,
            "categories": ["cs.DC", "cs.LG", "cs.AR"],
            # 刻意使用**具体短语**而非 "inference" / "memory" 这类泛词：
            # 泛词会把大量统计学与理论机器学习论文拉进来（实测：
            # "conformal inference"、"selective inference" 都被误判为 AI Infra）。
            "abs_terms": [
                "LLM serving",
                "inference system",
                "inference engine",
                "serving system",
                "GPU kernel",
                "CUDA kernel",
                "KV cache",
                "tensor parallel",
                "speculative decoding",
                "quantization",
                "memory bandwidth",
                "throughput",
            ],
            "max_results": 100,
            "sort_by": "submittedDate",
            "request_delay_ms": 3000,
        },
        "github": {
            "enabled": True,
            "mode": "both",
            "repositories": [
                "vllm-project/vllm",
                "sgl-project/sglang",
                "ray-project/ray",
                "pytorch/pytorch",
                "NVIDIA/TensorRT-LLM",
                "microsoft/DeepSpeed",
                "NVIDIA/Megatron-LM",
                "triton-lang/triton",
                "Dao-AILab/flash-attention",
                "LMCache/LMCache",
                "kvcache-ai/Mooncake",
            ],
            "search": {
                "queries": ["llm inference", "kv cache", "gpu kernel"],
                "created_within_days": 7,
                "min_stars": 50,
            },
            "include_prerelease": False,
            "require_version_like_tag": True,
            "exclude_tag_patterns": [
                "(?i)viable",
                "(?i)nightly",
                "(?i)^ci[-/]",
                "(?i)^test[-/]",
                "(?i)^dev[-/]",
                "(?i)^trunk",
                "(?i)^unstable",
                "(?i)canary",
            ],
            "request_delay_ms": 500,
        },
        "rss": {
            "enabled": True,
            # 全部为实测可用的 feed（2026-09 验证）。
            # 不含 blogs.nvidia.com/feed/（市场向、噪音大），
            # 不含 Anthropic（已关闭 RSS，所有路径 404）。
            "feeds": [
                {"name": "NVIDIA Developer Blog", "url": "https://developer.nvidia.com/blog/feed/"},
                {"name": "PyTorch Blog", "url": "https://pytorch.org/blog/feed.xml"},
                {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml"},
                {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml"},
                {"name": "Together AI", "url": "https://www.together.ai/blog/rss.xml"},
                {"name": "AWS ML Blog", "url": "https://aws.amazon.com/blogs/machine-learning/feed/"},
                {"name": "Databricks", "url": "https://www.databricks.com/feed"},
                {"name": "BAIR Blog", "url": "https://bair.berkeley.edu/blog/feed.xml"},
            ],
            "max_items_per_feed": 10,
        },
        "search_api": {
            "enabled": True,
            "provider": "tavily",
            "api_key": "",
            "queries": [
                "AI infrastructure news",
                "LLM serving optimization",
                "GPU kernel optimization",
            ],
            "results_per_query": 10,
            "freshness": "week",
            "endpoint_overrides": {
                "tavily": "https://api.tavily.com/search",
                "brave": "https://api.search.brave.com/res/v1/web/search",
                "serpapi": "https://serpapi.com/search.json",
            },
        },
    },
    "http": {
        "timeout": 15,
        "max_retries": 2,
        "backoff_factor": 1.5,
        "user_agent": DEFAULT_USER_AGENT,
    },
    "task": {
        "name": "AI-Infra-Weekly-Report",
        "logon_delay_minutes": 2,
        "run_as_system": False,
        "execution_time_limit_minutes": 120,
        "restart_count": 2,
        "restart_interval_minutes": 10,
        "wake_to_run": True,
    },
}

_VALID_PROVIDERS = {"tavily", "brave", "serpapi"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_GITHUB_MODES = {"releases", "search", "both"}


class ConfigError(RuntimeError):
    """配置非法。上层应捕获并以 :data:`EXIT_CONFIG_ERROR` 退出。"""


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并 ``overlay`` 到 ``base`` 的副本上。列表整体替换。"""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 文件系统异常
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path} YAML 语法错误: {exc}") from exc

    if data is None:
        logger.warning("配置文件 %s 为空，将全部使用默认值", path)
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是映射（mapping）")
    return data


@dataclass
class Config:
    """已合并默认值并通过校验的配置。"""

    raw: dict[str, Any]
    source_path: Path
    project_root: Path

    # ------------------------------------------------------------------ 通用
    def get(self, dotted: str, default: Any = None) -> Any:
        """按 ``"a.b.c"`` 路径读取，缺失时返回 ``default``。"""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def section_app(self) -> dict[str, Any]:
        return self.raw["app"]

    def _resolve_dir(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.project_root / path)

    # -------------------------------------------------------------------- app
    @property
    def timezone_name(self) -> str:
        return self.raw["app"]["timezone"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["app"]["timezone"])

    @property
    def output_dir(self) -> Path:
        return self._resolve_dir(self.raw["app"]["output_dir"])

    @property
    def filename_template(self) -> str:
        return self.raw["app"]["filename_template"]

    @property
    def state_dir(self) -> Path:
        return self._resolve_dir(self.raw["app"]["state_dir"])

    @property
    def log_dir(self) -> Path:
        return self._resolve_dir(self.raw["app"]["log_dir"])

    @property
    def log_level(self) -> str:
        return self.raw["app"]["log_level"]

    @property
    def log_retention_days(self) -> int:
        return int(self.raw["app"]["log_retention_days"])

    # --------------------------------------------------------------- schedule
    @property
    def schedule_weekday(self) -> int:
        return int(self.raw["schedule"]["weekday"])

    @property
    def schedule_hour(self) -> int:
        return int(self.raw["schedule"]["hour"])

    @property
    def schedule_minute(self) -> int:
        return int(self.raw["schedule"]["minute"])

    @property
    def lookback_weeks(self) -> int:
        return int(self.raw["schedule"]["lookback_weeks"])

    @property
    def catchup_on_start(self) -> bool:
        return bool(self.raw["schedule"]["catchup_on_start"])

    # -------------------------------------------------------------------- llm
    @property
    def llm_configured(self) -> bool:
        """是否已提供 api_key。未提供时 `--run` 会硬失败。"""
        return bool(str(self.raw["llm"]["api_key"]).strip())

    # ---------------------------------------------------------------- dedupe
    @property
    def history_path(self) -> Path:
        return self._resolve_dir(self.raw["dedupe"]["history_file"])

    # ---------------------------------------------------------------- helpers
    def source_cfg(self, name: str) -> dict[str, Any]:
        value = self.raw["sources"].get(name)
        return value if isinstance(value, dict) else {}

    def enabled_sources(self) -> list[str]:
        """返回启用且「可用」的数据源名，按固定优先级排序。"""
        order = ["arxiv", "github", "hackernews", "rss", "search_api", "reddit"]
        usable: list[str] = []
        for name in order:
            cfg = self.source_cfg(name)
            if not cfg.get("enabled", True):
                logger.info("数据源 %s 已在配置中禁用", name)
                continue
            if name == "search_api" and not str(cfg.get("api_key", "")).strip():
                logger.info(
                    "数据源 search_api 的 api_key 为空，已静默跳过（其余源不受影响）"
                )
                continue
            usable.append(name)
        return usable


# --------------------------------------------------------------------- 校验
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _validate(raw: dict[str, Any]) -> None:
    app = raw["app"]
    schedule = raw["schedule"]
    report = raw["report"]
    dedupe = raw["dedupe"]
    llm = raw["llm"]
    task = raw["task"]

    # --- app ---
    try:
        ZoneInfo(str(app["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"找不到时区 {app['timezone']!r}。Windows 与精简 Linux 容器没有系统时区数据库，"
            "请安装 tzdata：pip install tzdata"
        ) from exc
    except ValueError as exc:
        raise ConfigError(f"app.timezone 不是合法时区: {app['timezone']!r}") from exc

    template = str(app["filename_template"])
    _require("{date}" in template, "app.filename_template 必须包含 {date} 占位符")
    try:
        from datetime import date as _date

        template.format(date=_date(2026, 1, 2).isoformat())
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"app.filename_template 只能包含 {{date}} 占位符，当前为 {template!r}: {exc}"
        ) from exc

    level = str(app["log_level"]).upper()
    _require(level in _VALID_LOG_LEVELS, f"app.log_level 必须是 {sorted(_VALID_LOG_LEVELS)} 之一")
    app["log_level"] = level
    _require(int(app["log_retention_days"]) >= 1, "app.log_retention_days 必须 >= 1")

    # --- schedule ---
    _require(1 <= int(schedule["weekday"]) <= 7, "schedule.weekday 必须在 1..7（1=周一，7=周日）")
    _require(0 <= int(schedule["hour"]) <= 23, "schedule.hour 必须在 0..23")
    _require(0 <= int(schedule["minute"]) <= 59, "schedule.minute 必须在 0..59")
    _require(int(schedule["lookback_weeks"]) >= 0, "schedule.lookback_weeks 必须 >= 0")

    # --- report ---
    _require(int(report["max_items"]) > 0, "report.max_items 必须 > 0")
    _require(int(report["min_items"]) >= 0, "report.min_items 必须 >= 0")
    _require(
        int(report["min_items"]) <= int(report["max_items"]),
        "report.min_items 不能大于 report.max_items",
    )
    _require(int(report["links_per_item"]) >= 1, "report.links_per_item 必须 >= 1")
    _require(
        1 <= int(report["window_start_weekday"]) <= 7,
        "report.window_start_weekday 必须在 1..7",
    )
    window_extend = report["window_extend_to_next_week"]
    _require(isinstance(window_extend, bool), "report.window_extend_to_next_week 必须是布尔值")

    # --- dedupe ---
    _require(int(dedupe["history_weeks"]) >= 0, "dedupe.history_weeks 必须 >= 0")
    for key in ("title_similarity_threshold", "token_jaccard_threshold"):
        value = float(dedupe[key])
        _require(0.0 <= value <= 1.0, f"dedupe.{key} 必须在 0.0..1.0 之间")
    _require(isinstance(dedupe["strip_query_params"], list), "dedupe.strip_query_params 必须是列表")

    # --- llm ---
    _require(bool(str(llm["base_url"]).strip()), "llm.base_url 不能为空")
    _require(str(llm["base_url"]).startswith(("http://", "https://")), "llm.base_url 必须以 http(s):// 开头")
    _require(bool(str(llm["model"]).strip()), "llm.model 不能为空")
    _require(int(llm["max_retries"]) >= 0, "llm.max_retries 必须 >= 0")
    _require(int(llm["batch_size"]) >= 1, "llm.batch_size 必须 >= 1")
    _require(int(llm["max_workers"]) >= 1, "llm.max_workers 必须 >= 1")
    _require(int(llm["shortlist_size"]) >= 1, "llm.shortlist_size 必须 >= 1")
    _require(int(llm["timeout"]) >= 1, "llm.timeout 必须 >= 1")
    _require(
        int(llm["shortlist_size"]) >= int(report["max_items"]),
        "llm.shortlist_size 必须 >= report.max_items",
    )

    # --- relevance ---
    relevance = raw["relevance"]
    _require(bool(relevance["keywords"]), "relevance.keywords 不能为空")
    for key in ("keyword", "heat", "source"):
        _require(key in relevance["score_weights"], f"relevance.score_weights 缺少 {key!r}")
    _require(
        float(relevance["min_keyword_score"]) >= 0,
        "relevance.min_keyword_score 必须 >= 0（0 表示不过滤）",
    )
    _require(
        int(relevance.get("shortlist_per_source_cap", 0)) >= 0,
        "relevance.shortlist_per_source_cap 必须 >= 0（0 表示不限制）",
    )

    # --- sources ---
    for name in ("hackernews", "reddit", "arxiv", "github", "rss", "search_api"):
        _require(name in raw["sources"], f"config.yaml 的 sources 段缺少 {name}")

    gh_mode = str(raw["sources"]["github"]["mode"]).lower()
    _require(gh_mode in _VALID_GITHUB_MODES, f"sources.github.mode 必须是 {sorted(_VALID_GITHUB_MODES)} 之一")
    raw["sources"]["github"]["mode"] = gh_mode

    provider = str(raw["sources"]["search_api"]["provider"]).lower()
    _require(provider in _VALID_PROVIDERS, f"sources.search_api.provider 必须是 {sorted(_VALID_PROVIDERS)} 之一")
    raw["sources"]["search_api"]["provider"] = provider

    _require(
        isinstance(raw["sources"]["rss"]["feeds"], list),
        "sources.rss.feeds 必须是列表",
    )

    # --- http ---
    http = raw["http"]
    _require(int(http["timeout"]) >= 1, "http.timeout 必须 >= 1")
    _require(int(http["max_retries"]) >= 0, "http.max_retries 必须 >= 0")
    _require(float(http["backoff_factor"]) > 0, "http.backoff_factor 必须 > 0")
    _require(bool(str(http["user_agent"]).strip()), "http.user_agent 不能为空")

    # --- task ---
    _require(bool(str(task["name"]).strip()), "task.name 不能为空")
    _require(int(task["execution_time_limit_minutes"]) >= 1, "task.execution_time_limit_minutes 必须 >= 1")
    _require(int(task["restart_count"]) >= 0, "task.restart_count 必须 >= 0")
    _require(int(task["restart_interval_minutes"]) >= 1, "task.restart_interval_minutes 必须 >= 1")


# ------------------------------------------------------------------ 对外接口
def load_config(path: Path | str | None = None) -> Config:
    """加载并校验配置。

    ``path`` 为 ``None`` 时读取项目根下的 ``config.yaml``；若同目录存在
    ``config.local.yaml``，会**叠加**在其之上（用于存放密钥等不入库内容）。
    """
    source = Path(path).resolve() if path is not None else DEFAULT_CONFIG_PATH
    raw = _deep_merge(DEFAULTS, _read_yaml(source))

    overlay = LOCAL_OVERLAY_PATH
    if path is None and overlay.exists():
        logger.info("检测到覆盖层配置，已合并: %s", overlay)
        raw = _deep_merge(raw, _read_yaml(overlay))

    # api_key 环境变量回退（可选便利，不改变「留空即失败」的语义）
    if not str(raw["llm"].get("api_key", "")).strip():
        env_key = os.environ.get(ENV_LLM_API_KEY, "").strip()
        if env_key:
            raw["llm"]["api_key"] = env_key
            logger.info("llm.api_key 取自环境变量 %s", ENV_LLM_API_KEY)

    if not str(raw["sources"]["search_api"].get("api_key", "")).strip():
        env_search = os.environ.get("AI_INFRA_SEARCH_API_KEY", "").strip()
        if env_search:
            raw["sources"]["search_api"]["api_key"] = env_search
            logger.info("search_api.api_key 取自环境变量 AI_INFRA_SEARCH_API_KEY")

    project_root = source.parent if path is not None else PROJECT_ROOT
    _validate(raw)

    config = Config(raw=raw, source_path=source, project_root=project_root)

    if not config.llm_configured:
        logger.warning(
            "llm.api_key 为空：`--run` / `--dry-run` 会在调用大模型时失败并退出 1。"
            "可先用 `--no-llm` 验证抓取与去重链路，或填写 %s 中 llm.api_key。",
            source,
        )

    return config


def ensure_runtime_dirs(config: Config) -> None:
    """创建运行时需要的目录。"""
    for path in (config.output_dir, config.state_dir, config.log_dir):
        path.mkdir(parents=True, exist_ok=True)
