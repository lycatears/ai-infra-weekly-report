"""流水线集成测试。

用 monkeypatch 替换掉「网络抓取」与「大模型」两个外部依赖，
从而在**不联网、不需要 API Key** 的前提下验证整条流水线的关键契约：

1. 成功路径：报告落盘 + history 更新
2. **失败路径：大模型不可用时不得产出残缺报告，也不得污染 history**
3. `--dry-run`：写报告但不更新 history
4. `--no-llm`：导出候选清单，不调用大模型
5. 抓取全空时不生成空报告
6. 顺序契约：只有报告写成功之后才记录 history

第 2、6 条是本项目最容易被改坏的地方——一旦顺序反了，一次失败运行会把
本周内容标记成「已收录」，造成**永久漏报**。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.fetchers.base import NewsItem
from src.llm import LLMError, SummaryItem
from src.pipeline import EXIT_OK, EXIT_RUNTIME_ERROR, run_pipeline

from .conftest import FRIDAY_TRIGGER, TZ, WEEK_FRIDAY

UTC = timezone.utc


#: 刻意让每条标题彼此差异明显。
#: 第一版测试用的是「KV Cache 优化方案 1..30」，结果被正常的标题去重合并成 1 条，
#: 导致后续断言全部失败——那其实是在证明去重逻辑有效。
_HEADLINES = [
    "vLLM v0.30.0 发布：KV Cache 全面换用 MXFP8",
    "SGLang 引入 Prefill-Decode 解耦部署模式",
    "TensorRT-LLM 新增 NVFP4 W4A16 内核默认路径",
    "Ray 发布弹性专家并行与扩缩容支持",
    "FlashAttention 在 Blackwell 上重新启用 head_dim 256",
    "LMCache 支持跨节点 KV 复用与分层卸载",
    "Mooncake 异构张量并行共享存储实践",
    "Triton 新增 split-row top-p 采样内核",
    "DeepSpeed 显存带宽优化与 offload 重构",
    "PyTorch 编译缓存跨进程共享降低冷启动",
    "HBM 与高带宽 Flash 冷热分层实验",
    "CUDA 图捕获时间从 12 秒降至 2 秒的优化记录",
    "MoE 路由 GEMM 在 SM100 上的默认后端切换",
    "FP8 索引缓存大幅降低长上下文显存占用",
    "连续批处理调度器在高并发下的尾延迟治理",
    "NVLink 与 PCIe 场景下的 all-reduce 选型对比",
    "推测解码在流水并行下的自适应验证",
    "前缀缓存命中率提升的块大小调优经验",
    "多节点推理部署的网络拓扑优化",
    "量化校准对下游任务精度的影响评估",
]


def make_items(count: int) -> list[NewsItem]:
    """造一批来源多样、标题互不相似的候选。"""
    sources = ["arxiv", "github", "hackernews", "rss"]
    chosen = _HEADLINES[:count]
    assert len(chosen) == count, "_HEADLINES 数量不足，请扩充"

    items: list[NewsItem] = []
    for index, headline in enumerate(chosen, start=1):
        source = sources[index % len(sources)]
        items.append(
            NewsItem(
                source=source,
                title=headline,
                url=f"https://example.com/{source}/{index}",
                published_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
                summary=f"第 {index} 条摘要，涉及 KV Cache 与 Tensor Parallel。",
                raw_score=float(index * 10),
                extra={"keyword_hits": ["kv cache"]},
            )
        )
    return items


class FakeLLM:
    """替身：不做网络请求，直接产出结构合法的总结。"""

    def __init__(self, config, *, fail: bool = False, produce: int = 0) -> None:
        self.fail = fail
        self.produce = produce or int(config.raw["report"]["max_items"])
        self.token_usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    def shortlist(self, candidates):
        if self.fail:
            raise LLMError("模拟大模型不可用")
        return candidates[: self.produce]

    def summarize(self, items):
        if self.fail:
            raise LLMError("模拟大模型不可用")
        return [
            SummaryItem(
                project_name=f"项目{index}",
                one_line=f"第 {index} 条的一句话总结。",
                highlights=["亮点一", "亮点二"],
                keywords=["KV Cache"],
                links=[item.url],
                published_at=item.published_at,
                source=item.source,
                source_url=item.url,
            )
            for index, item in enumerate(items, start=1)
        ]


@pytest.fixture
def prepared(config, monkeypatch):
    """准备一个可运行的环境：开启 key、注入假抓取结果。"""

    def _prepare(*, items: int = 20, sources=None):
        config.raw["llm"]["api_key"] = "test-key"
        monkeypatch.setattr(
            "src.pipeline.fetch_all",
            lambda *a, **kw: (make_items(items), sources or {"arxiv": items // 2, "github": items // 4}),
        )
        return config

    return _prepare


def run(config, **overrides) -> int:
    params = dict(
        target_friday=WEEK_FRIDAY,
        covered_from=datetime(2026, 9, 21, 0, 0, tzinfo=TZ),
        covered_to=FRIDAY_TRIGGER,
        report_path=config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md",
    )
    params.update(overrides)
    return run_pipeline(config, **params)


# ---------------------------------------------------------------- 成功路径
def test_success_writes_report_and_history(prepared, monkeypatch):
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    code = run(config)

    assert code == EXIT_OK
    report = config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md"
    assert report.is_file()
    assert config.history_path.is_file()

    content = report.read_text(encoding="utf-8")
    for field in ("项目名称", "发表时间", "一句话总结", "项目亮点", "关键字", "相关链接"):
        assert f"**{field}**" in content


def test_success_records_history_entries(prepared, monkeypatch):
    config = prepared(items=20)
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    run(config)

    import json

    payload = json.loads(config.history_path.read_text(encoding="utf-8"))
    assert payload["entries"]
    assert all(entry["date"] == "2026-09-25" for entry in payload["entries"])


def test_second_run_does_not_duplicate_history(prepared, monkeypatch):
    """同一批数据第二次运行时，不得在 history 里产生重复条目。

    这里同时验证了两件事：
    1. 跨周去重确实生效（20 条候选里有 15 条已被收录）
    2. **失败运行不会污染 history**——第二次运行因剩余候选不足 8 条而成报失败，
       但 history 必须保持原样，否则这 5 条就会被永久标记为「已收录」而漏报
    """
    config = prepared(items=20)
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    assert run(config) == EXIT_OK
    first = json.loads(config.history_path.read_text(encoding="utf-8"))["entries"]
    assert len(first) == 15

    # 删掉报告但保留 history，模拟「同一批数据下周又被抓到」
    report = config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md"
    report.unlink()

    # 剩余候选不足以成报 -> 退出码 1
    assert run(config) == EXIT_RUNTIME_ERROR

    second = json.loads(config.history_path.read_text(encoding="utf-8"))["entries"]
    assert len(second) == len(first), "失败的运行不应改写 history"
    assert not report.exists()


# ---------------------------------------------------------------- 失败路径
def test_llm_failure_writes_nothing(prepared, monkeypatch):
    """大模型不可用 -> 退出码 1，且不产出报告、不更新 history。"""
    config = prepared()
    monkeypatch.setattr(
        "src.pipeline.LLMClient",
        lambda cfg: FakeLLM(cfg, fail=True),
    )

    code = run(config)

    assert code == EXIT_RUNTIME_ERROR
    report = config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md"
    assert not report.exists()
    assert not config.history_path.exists()


def test_missing_api_key_fails_fast(monkeypatch, config):
    """未配置 key 时应在抓取之前就失败，不做无意义的网络请求。"""
    called = {"fetch": False}

    def spy(*args, **kwargs):
        called["fetch"] = True
        return [], {}

    monkeypatch.setattr("src.pipeline.fetch_all", spy)

    code = run(config)

    assert code == EXIT_RUNTIME_ERROR
    assert called["fetch"] is False, "未配置 key 时不应先花时间抓取"


def test_no_candidates_fails_without_empty_report(prepared, monkeypatch):
    config = prepared()
    monkeypatch.setattr("src.pipeline.fetch_all", lambda *a, **kw: ([], {}))
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    code = run(config)

    assert code == EXIT_RUNTIME_ERROR
    assert not (config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md").exists()


def test_all_deduped_away_fails(prepared, monkeypatch):
    """候选被历史全部剔除时不应硬凑一份空报告。"""
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    # 先跑一次把内容写进 history
    assert run(config) == EXIT_OK
    (config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md").unlink()

    # 第二次仍然抓到同一批数据 -> 全被跨周去重剔除
    assert run(config) == EXIT_RUNTIME_ERROR
    assert not (config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md").exists()


def test_structure_validation_failure_writes_nothing(prepared, monkeypatch):
    """结构校验不通过时不得落盘，也不得更新 history。"""
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)
    monkeypatch.setattr("src.pipeline.validate_report_text", lambda *a, **kw: ["人为构造的校验失败"])

    code = run(config)

    assert code == EXIT_RUNTIME_ERROR
    assert not config.history_path.exists()


# ---------------------------------------------------------------- 模式开关
def test_dry_run_writes_report_but_not_history(prepared, monkeypatch):
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    code = run(config, dry_run=True)

    assert code == EXIT_OK
    assert (config.state_dir / "_dryrun-2026-09-25.md").is_file()
    assert not (config.output_dir / "AI-Infra-周报-2026-09-25-by-DeepSeek.md").exists()
    assert not config.history_path.exists()


def test_no_llm_dumps_candidates_and_skips_llm(prepared, monkeypatch):
    config = prepared()

    def explode(cfg):
        raise AssertionError("--no-llm 不应实例化大模型客户端")

    monkeypatch.setattr("src.pipeline.LLMClient", explode)

    code = run(config, skip_llm=True)

    assert code == EXIT_OK
    dump = config.state_dir / "_candidates-2026-09-25.json"
    assert dump.is_file()

    import json

    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["count"] > 0
    assert payload["items"][0]["title"]
    # 未调用大模型，因此没有报告、没有 history
    assert not config.history_path.exists()


def test_no_llm_works_without_api_key(config, monkeypatch):
    """没有 key 也能用 --no-llm 验证抓取链路——这是引导路径的核心价值。"""
    monkeypatch.setattr("src.pipeline.fetch_all", lambda *a, **kw: (make_items(20), {"arxiv": 20}))
    assert config.raw["llm"]["api_key"] == ""

    assert run(config, skip_llm=True) == EXIT_OK


# ---------------------------------------------------------------- 其他契约
def test_limit_sources_is_forwarded(prepared, monkeypatch):
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)
    captured: dict = {}

    def spy(cfg, start, end, *, limit_sources=None, **kwargs):
        captured["limit_sources"] = limit_sources
        return make_items(20), {"arxiv": 20}

    monkeypatch.setattr("src.pipeline.fetch_all", spy)
    run(config, limit_sources=["arxiv"])

    assert captured["limit_sources"] == ["arxiv"]


def test_report_path_is_exactly_what_was_requested(prepared, monkeypatch, tmp_path):
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)
    custom = tmp_path / "custom-name.md"

    assert run(config, report_path=custom) == EXIT_OK
    assert custom.is_file()


def test_no_temp_files_left_behind(prepared, monkeypatch):
    config = prepared()
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)
    run(config)

    leftovers = [p.name for p in config.output_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_history_not_written_when_dry_run_then_real(config, prepared, monkeypatch):
    """dry-run 之后再真实运行，应正常写入完整 history。"""
    cfg = prepared(items=20)
    monkeypatch.setattr("src.pipeline.LLMClient", FakeLLM)

    assert run(cfg, dry_run=True) == EXIT_OK
    assert not cfg.history_path.exists()

    assert run(cfg) == EXIT_OK
    assert cfg.history_path.is_file()
