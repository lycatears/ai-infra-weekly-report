"""配置加载与校验测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    DEFAULTS,
    ENV_LLM_API_KEY,
    ConfigError,
    ensure_runtime_dirs,
    load_config,
)


def write_config(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------ 默认值
def test_empty_config_falls_back_to_defaults(tmp_path):
    config = load_config(write_config(tmp_path, ""))
    assert config.schedule_weekday == 5
    assert config.schedule_hour == 22
    assert config.timezone_name == "Asia/Shanghai"
    assert config.raw["llm"]["batch_size"] == 5


def test_partial_config_is_merged_not_replaced(tmp_path):
    config = load_config(write_config(tmp_path, "schedule:\n  hour: 9\n"))
    assert config.schedule_hour == 9
    # 同段内的其他键仍取默认值
    assert config.schedule_weekday == 5
    assert config.schedule_minute == 0


def test_unknown_keys_are_preserved(tmp_path):
    config = load_config(write_config(tmp_path, "custom:\n  foo: bar\n"))
    assert config.get("custom.foo") == "bar"


def test_defaults_cover_every_documented_section():
    for section in (
        "app", "schedule", "llm", "report", "dedupe",
        "relevance", "sources", "http", "task",
    ):
        assert section in DEFAULTS


# ------------------------------------------------------------ 校验失败
def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, "app: [未闭合\n"))


def test_non_mapping_top_level_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, "- 1\n- 2\n"))


def test_invalid_timezone_raises_with_hint(tmp_path):
    path = write_config(tmp_path, 'app:\n  timezone: "Not/AZone"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "tzdata" in str(exc.value)


def test_filename_template_requires_date_placeholder(tmp_path):
    path = write_config(tmp_path, 'app:\n  filename_template: "报告.md"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "{date}" in str(exc.value)


def test_filename_template_rejects_other_placeholders(tmp_path):
    path = write_config(tmp_path, 'app:\n  filename_template: "{date}-{oops}.md"\n')
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "snippet",
    [
        "schedule:\n  weekday: 0\n",
        "schedule:\n  weekday: 8\n",
        "schedule:\n  hour: 24\n",
        "schedule:\n  minute: 60\n",
        "schedule:\n  lookback_weeks: -1\n",
        "report:\n  max_items: 0\n",
        "report:\n  links_per_item: 0\n",
        "dedupe:\n  title_similarity_threshold: 1.5\n",
        "llm:\n  base_url: \"\"\n",
        'llm:\n  base_url: "ftp://x"\n',
        "llm:\n  batch_size: 0\n",
        "llm:\n  max_workers: 0\n",
        "http:\n  timeout: 0\n",
        "relevance:\n  min_keyword_score: -1\n",
        "relevance:\n  shortlist_per_source_cap: -1\n",
        "app:\n  log_level: LOUD\n",
        "task:\n  restart_interval_minutes: 0\n",
    ],
)
def test_invalid_values_raise(tmp_path, snippet):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, snippet))


def test_min_items_must_not_exceed_max_items(tmp_path):
    path = write_config(tmp_path, "report:\n  min_items: 20\n  max_items: 5\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_shortlist_size_must_cover_max_items(tmp_path):
    path = write_config(tmp_path, "llm:\n  shortlist_size: 3\nreport:\n  max_items: 15\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_github_mode_raises(tmp_path):
    path = write_config(tmp_path, "sources:\n  github:\n    mode: nope\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_search_provider_raises(tmp_path):
    path = write_config(tmp_path, "sources:\n  search_api:\n    provider: altavista\n")
    with pytest.raises(ConfigError):
        load_config(path)


# ------------------------------------------------------------ 数据源开关
def test_enabled_sources_skips_search_api_without_key(tmp_path):
    config = load_config(write_config(tmp_path, "sources:\n  search_api:\n    api_key: \"\"\n"))
    assert "search_api" not in config.enabled_sources()
    # 其余源不受影响
    assert {"arxiv", "github", "hackernews", "rss"} <= set(config.enabled_sources())


def test_enabled_sources_includes_search_api_with_key(tmp_path):
    config = load_config(write_config(tmp_path, "sources:\n  search_api:\n    api_key: abc\n"))
    assert "search_api" in config.enabled_sources()


def test_enabled_sources_respects_enabled_flag(tmp_path):
    config = load_config(write_config(tmp_path, "sources:\n  reddit:\n    enabled: false\n"))
    assert "reddit" not in config.enabled_sources()


def test_enabled_sources_order_is_stable(tmp_path):
    config = load_config(write_config(tmp_path, ""))
    assert config.enabled_sources() == ["arxiv", "github", "hackernews", "rss", "reddit"]


# ------------------------------------------------------------ 密钥回退
def test_api_key_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LLM_API_KEY, "sk-from-env")
    config = load_config(write_config(tmp_path, 'llm:\n  api_key: ""\n'))
    assert config.llm_configured
    assert config.raw["llm"]["api_key"] == "sk-from-env"


def test_config_file_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LLM_API_KEY, "sk-from-env")
    config = load_config(write_config(tmp_path, 'llm:\n  api_key: "sk-from-file"\n'))
    assert config.raw["llm"]["api_key"] == "sk-from-file"


def test_llm_configured_false_when_blank(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_LLM_API_KEY, raising=False)
    config = load_config(write_config(tmp_path, 'llm:\n  api_key: "   "\n'))
    assert not config.llm_configured


# ------------------------------------------------------------ 路径与目录
def test_relative_dirs_resolve_against_config_location(tmp_path):
    config = load_config(write_config(tmp_path, 'app:\n  output_dir: "reports"\n'))
    assert config.output_dir == tmp_path / "reports"


def test_absolute_dir_is_kept(tmp_path):
    target = tmp_path / "abs" / "out"
    config = load_config(write_config(tmp_path, f'app:\n  output_dir: "{target.as_posix()}"\n'))
    assert config.output_dir == target


def test_ensure_runtime_dirs_creates_everything(tmp_path):
    config = load_config(write_config(tmp_path, ""))
    ensure_runtime_dirs(config)
    assert config.output_dir.is_dir()
    assert config.state_dir.is_dir()
    assert config.log_dir.is_dir()


def test_history_path_follows_state_dir(tmp_path):
    config = load_config(write_config(tmp_path, 'app:\n  state_dir: "st"\ndedupe:\n  history_file: "st/history.json"\n'))
    assert config.history_path == tmp_path / "st" / "history.json"
