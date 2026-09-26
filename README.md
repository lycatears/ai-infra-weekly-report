# AI Infra 周报自动生成

每周五 22:00 自动抓取 AI Infra 领域的最新技术新闻，核对发布时间、去重、调用大模型
总结，输出为 Markdown 周报。支持开机自启与「错过时间自动补跑」。

输出文件名：`AI-Infra-周报-YYYY-MM-DD-by-DeepSeek.md`（日期为**本周五**）。

---

## 快速开始

### Windows

```powershell
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 填写大模型 API Key（编辑 config.yaml）
#    llm:
#      api_key: "sk-xxxxxxxx"

# 3. 先验证抓取链路是否正常（不需要 API Key）
.\.venv\Scripts\python.exe main.py --run --no-llm

# 4. 生成一期真实报告
.\.venv\Scripts\python.exe main.py --run --force

# 5. 注册计划任务（每周五 22:00 + 登陆/开机补跑）
.\.venv\Scripts\python.exe main.py --install-task
```

### Linux

```bash
# 1-2 步合并：自动建 venv、装依赖、自检、注册 crontab
scripts/deploy_linux.sh

# 如果还没填 API Key，脚本会提示下一步；填好后再验证：
.venv/bin/python main.py --check
.venv/bin/python main.py --run --force

# crontab 内容可用 --dry-run 预览（不修改任何东西）
scripts/install_cron.sh --dry-run
```

> **建议用普通用户运行**，不要 `sudo`：crontab 是用户级的，以 root 注册会让
> 任务以 root 身份运行，既没必要也不安全。部署过程本身不需要 root 权限。

> **注意**：`llm.api_key` 留空时 `--run` 会**直接失败并退出码 1**（这是刻意设计，
> 避免产出残缺报告）。此时请先用 `--no-llm` 验证抓取链路。

---

## 命令行参考

| 命令 | 作用 |
|---|---|
| `python main.py --check` | 只判定「本次是否该生成报告」，不联网、不写文件 |
| `python main.py --check --at "2026-09-25 23:00"` | 用指定时刻模拟判定（调试补跑逻辑） |
| `python main.py --run` | 完整流程；若无需生成则自动跳过 |
| `python main.py --run --force` | 忽略「报告已存在」，强制生成 |
| `python main.py --run --no-llm` | 跳过所有大模型调用，导出候选清单到 `state/` |
| `python main.py --run --dry-run` | 跑完整流程但不更新 `history.json` |
| `python main.py --run --limit-sources arxiv,github` | 只用指定数据源，便于小样本联调 |
| `python main.py --install-task [--run-as-system]` | 注册定时任务（Windows 计划任务 / Linux crontab，按系统自动选择） |
| `python main.py --uninstall-task` | 卸载定时任务 |
| `python scripts/check_sources.py [源名...]` | 数据源连通性自检 |

**退出码**：`0` 成功（含正常跳过） · `1` 运行时失败 · `2` 配置错误 · `3` 未预期错误

---

## 配置说明

全部配置集中在 `config.yaml`，所有字段都有默认值（见 `src/config.py` 的 `DEFAULTS`），
可以只写需要覆盖的项。密钥也可以放在不入库的 `config.local.yaml`（会被叠加），
或环境变量 `AI_INFRA_LLM_API_KEY` / `AI_INFRA_SEARCH_API_KEY`。

### 只有 3 个字段需要你关心

| 字段 | 默认值 | 说明 |
|---|---|---|
| `llm.api_key` | `""` | **必须填**，否则 `--run` 失败退出 |
| `sources.search_api.api_key` | `""` | 留空则**静默跳过该源**，其余源不受影响 |
| `task.run_as_system` | `false` | `true` 需管理员权限，实现「未登录也执行」 |

### 关键字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `app.timezone` | `Asia/Shanghai` | 报告的时区基准 |
| `app.output_dir` | `"."` | 报告输出目录，`"."` = 脚本所在目录 |
| `schedule.weekday` | `5` | `1`=周一 … `7`=周日 |
| `schedule.hour` / `minute` | `22` / `0` | 触发时刻 |
| `schedule.lookback_weeks` | `1` | 补跑最多回溯几周 |
| `schedule.catchup_on_start` | `true` | 是否允许回溯补跑历史周（关掉则只补本周） |
| `report.max_items` | `15` | 每期收录条数上限 |
| `report.window_extend_to_next_week` | `true` | 是否消除「周五22:00~下周一00:00」覆盖空洞，详见下文 |
| `dedupe.history_weeks` | `4` | 跨周去重窗口 |
| `relevance.min_keyword_score` | `1.5` | 进大模型前的关键词门槛 |
| `relevance.shortlist_per_source_cap` | `12` | 短名单中单源条数上限 |
| `llm.batch_size` / `max_workers` | `5` / `3` | 阶段 B 批大小与并发度 |

### 关于 `window_extend_to_next_week`

若窗口严格截止在周五 22:00，那么「周五 22:00 ~ 下周一 00:00」发布的新闻
**既不属于本期、也不属于下一期**，会被永久漏掉。开启后窗口终点延到「下一周的起点」，
使相邻两期首尾相接；实际终点会被截断到本次运行时刻，因此**不会抓到未来的新闻**。
在正常的周五 22:00 触发场景下，该段内容尚不存在，行为与严格窗口一致。

---

## 定时任务

`main.py --install-task` 会**按当前系统自动选择**调度方式：

| 平台 | 调度器 | 注册脚本 | 运行脚本 |
|---|---|---|---|
| Windows | 任务计划程序 | `scripts/install_task.ps1` | `scripts/run_weekly.ps1` |
| Linux | crontab | `scripts/install_cron.sh` | `scripts/run_weekly.sh` |

### Linux（crontab）

```bash
# 一键部署：建 venv + 装依赖 + 自检 + 注册 crontab（不需要 root）
scripts/deploy_linux.sh

# 或者分步执行
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --install-task

# 只查看将要写入的内容，不改动 crontab
scripts/install_cron.sh --dry-run

# 卸载
.venv/bin/python main.py --uninstall-task
```

写入 crontab 的**三条**记录：

| 表达式 | 作用 |
|---|---|
| `0 22 * * 5` | 每周五 22:00 常规触发（跟随 `config.yaml` 的 `schedule.*`） |
| `0 * * * *` | **兜底轮询**：每小时检查一次是否需要补跑 |
| `@reboot sleep 120 && …` | 开机后 2 分钟补跑一次 |

> **兜底轮询是 Linux 上必需的一层。** cron 没有 Windows 计划任务的
> `StartWhenAvailable` —— 周五 22:00 时机器若处于关机状态，cron **不会**补跑。
> 靠这条周期任务 + `main.py --run` 自身的幂等判定，把漏掉的那次补上。
> 调整频率：`scripts/install_cron.sh --catchup-minutes 30`；
> 关闭：`--catchup-minutes 0`。

所有条目都写在首尾标记之间，**重复安装是原地替换而非追加**，
也不会碰你自己写的其它 crontab 条目；卸载同样幂等。

日志分两份：

- `logs/task-YYYYMMDD.log` —— 完整输出（与 Windows 侧一致）
- `logs/cron.log` —— 包装脚本的一行摘要，以及「脚本还没跑起来就挂了」的现场

`run_weekly.sh` 会自己设好 `PYTHONUTF8=1`。cron 通常不设置 `LANG`/`LC_ALL`，
一旦 Python 的 UTF-8 模式也没打开，stdout 就会退化成 ascii，
打印中文直接 `UnicodeEncodeError` **崩溃**（不是乱码，是崩）。

**但不要依赖发行版的默认值**：实测 Ubuntu 26.04 + Python 3.14 默认已打开 UTF-8 模式
（PEP 686 计划自 3.15 起默认），因此不显式设置也可能一切正常；
旧版本或精简镜像则不然。显式设置的成本为零，所以照设。

并发保护用 `flock`（对应 Windows 的 `MultipleInstances=IgnoreNew`）。
`flock` 来自 util-linux，主流发行版都自带；极精简的镜像（Alpine + busybox）
若缺失会降级为「只告警、不锁」，并在日志里写明。

### Windows（任务计划程序）

```powershell
# 当前用户 + 登录触发（无需管理员）
.\.venv\Scripts\python.exe main.py --install-task

# SYSTEM + 开机触发（需要管理员 PowerShell）
.\.venv\Scripts\python.exe main.py --install-task --run-as-system
```

注册结果包含**两个触发器**：

1. **登录时**（`--run-as-system` 时为**开机时**），延迟 2 分钟
2. **每周五 22:00**

关键设置：

| 设置 | 值 | 为什么 |
|---|---|---|
| `StartWhenAvailable` | 开 | **关机错过时会自动补跑**（第一层保障） |
| `WakeToRun` | 开 | 睡眠中也能唤醒执行 |
| `RestartCount` / `RestartInterval` | 2 / 10 分钟 | 大模型临时不可用时可重试（依赖退出码透传） |
| `MultipleInstances` | `IgnoreNew` | 防止两次运行并发写同一文件 |
| `ExecutionTimeLimit` | 2 小时 | 防止卡死后长期占用 |

### 验证与排查

```powershell
# --- Windows ---
# 查看任务配置
schtasks /query /tn "AI-Infra-Weekly-Report" /v /fo LIST

# 手动触发一次
schtasks /run /tn "AI-Infra-Weekly-Report"

# 查看任务日志（注意必须指定 utf8，否则中文乱码）
Get-Content logs\task-$(Get-Date -Format yyyyMMdd).log -Encoding utf8 -Tail 50
```

```bash
# --- Linux ---
# 查看 crontab 内容
crontab -l

# 手动跑一次（--force 可忽略「报告已存在」检查）
scripts/run_weekly.sh
scripts/run_weekly.sh --no-llm      # 不调大模型，只验证抓取与去重

# 查看日志
tail -f logs/task-$(date +%Y%m%d).log
cat logs/cron.log

# 卸载
.venv/bin/python main.py --uninstall-task
```

### 补跑机制（多层保障）

| 平台 | 层 | 机制 | 覆盖场景 |
|---|---|---|---|
| 通用 | 3 | `src/schedule.py` 的 `decide()` | 任意时刻被触发都会检查本周报告是否存在，缺失则立即生成 |
| Windows | 1 | 计划任务 `StartWhenAvailable` | 触发时刻机器关机 → 开机后自动补跑 |
| Windows | 2 | 登录／开机触发器 | 开机后延迟 2 分钟补跑一次 |
| Linux | 1 | crontab 兜底轮询（每小时） | 触发时刻机器关机 → 一小时内补上 |
| Linux | 2 | crontab `@reboot` | 开机后 2 分钟补跑一次 |

第 3 层才是真正的保底：前三层的调度器只负责「把进程叫起来」，
「这周到底该不该生成」完全由 `decide()` 判断，因此多叫几次完全无害。

补跑判定表：

| 当前时间 | 本周报告 | 上周报告 | 动作 |
|---|---|---|---|
| 周五 22:00 前 | — | 缺失 | 生成**上周**（backfill） |
| 周五 22:00 前 | — | 存在 | 跳过（not_due） |
| 周五 22:00 后 | 缺失 | — | **立即生成本周**（catchup） |
| 周五 22:00 后 | 存在 | — | 跳过（already_generated，幂等） |

**幂等性的来源**：报告文件名由「周」决定而非运行时刻，所以同一周无论触发多少次，
只会产生一个文件、且命名与归档位置都正确。

---

## 目录结构

```
ai-infra-weekly-report/
├── main.py                      # CLI 入口
├── config.yaml                  # 唯一配置入口
├── requirements.txt
├── src/
│   ├── config.py                # 配置加载与校验（含全部默认值）
│   ├── schedule.py              # 周计算 + 补跑判定（幂等性核心）
│   ├── pipeline.py              # 流水线编排
│   ├── fetchers/                # 6 类数据源，统一 NewsItem 契约
│   │   ├── base.py              #   数据模型 + HTTP 客户端 + 窗口过滤
│   │   ├── arxiv.py             #   arXiv Atom API
│   │   ├── github.py            #   Release atom + 仓库搜索
│   │   ├── hackernews.py        #   HN Algolia API
│   │   ├── rss.py               #   官方博客 RSS
│   │   ├── search_api.py        #   Tavily / Brave / SerpAPI（可选）
│   │   └── reddit.py            #   Reddit（可选，见下文）
│   ├── dedupe.py                # 两级去重
│   ├── history.py               # 跨周去重状态
│   ├── relevance.py             # 相关性打分与筛选
│   ├── llm.py                   # 两阶段大模型调用
│   ├── report.py                # Markdown 渲染与校验
│   ├── io_utils.py              # 原子写入
│   └── task_setup.py            # 定时任务驱动（按平台分发到 PS / bash）
├── scripts/
│   ├── run_weekly.ps1           # Windows 执行体（计划任务调用）
│   ├── install_task.ps1         #   注册计划任务
│   ├── uninstall_task.ps1       #   卸载计划任务
│   ├── run_weekly.sh            # Linux 执行体（cron 调用）
│   ├── install_cron.sh          #   写入 crontab（幂等）
│   ├── uninstall_cron.sh        #   移除 crontab（幂等）
│   ├── deploy_linux.sh          #   Linux 一键部署
│   ├── normalize_shell_scripts.py # 修正 .sh 的 BOM/行尾
│   └── check_sources.py         # 数据源自检
├── tests/                       # pytest 单元测试
├── logs/                        # 运行日志（自动创建）
├── state/history.json           # 跨周去重状态（自动创建）
└── AI-Infra-周报-*.md           # 报告输出
```

---

## 数据源可靠性说明

| 源 | 可靠性 | 备注 |
|---|---|---|
| arXiv | 高 | `sortBy=submittedDate`，`<published>` 是 v1 提交时间 |
| GitHub | 高 | Release 走 **atom**（不受 REST API 限流影响）；仓库搜索走 API，未认证时限 10 次/分钟，设置 `GITHUB_TOKEN` 可提高 |
| Hacker News | 高 | Algolia API 免 key |
| 官方博客 RSS | 高 | 8 个 feed 实测可用 |
| 搜索 API | 中 | 需自备 key；部分 provider 不返回真实发布时间，会标记为「⚠️估算」 |
| Reddit | **低** | 公开 `.json` 端点会拦截非浏览器 UA，**实测经常 403**。已设计为可选源，失败只记 warning，不影响报告生成。若长期失败可把 `sources.reddit.enabled` 设为 `false` |

---

## 设计与取舍

**为什么文件名用「本周五」而不是实际生成日**
这样补跑（周六、周日甚至下周一运行）产生的文件名与正常触发完全一致，
幂等判定只需检查文件是否存在，无需额外状态。

**为什么发布时间绝不由大模型生成**
`发表时间` 一律取自抓取到的真实时间戳（HN `created_at` / arXiv `published` /
GitHub `updated`），大模型只负责文字。链接同理，全部来自抓取结果，
避免模型编造 URL 与时间。

**为什么跨周去重不做模糊匹配**
报告内做模糊去重是安全的（同一批数据里的重复通常是同一件事的多个来源）。
但跨周模糊匹配会误杀：上周发 `vLLM v0.30.0`、本周发 `vLLM v0.30.1`，
标题相似度约 0.95 远超 0.88 阈值，真正的「新版本发布」会被当成重复丢掉。
因此跨周只用**精确匹配**（规范化 URL / 归一化标题）。

**为什么关键词要加权且有负向词**
`inference` 是歧义最重的词——统计学论文里的「统计推断」也算。若不处理，
`Selective Inference for Deep Clustering`、`Graph-Local Conformal Inference`
这类论文会挤满榜单。因此把 `inference` 权重压到 0.8，
并加入 `conformal inference` 等负向词做扣分。

**为什么短名单要限制单源条数**
arXiv 一次能返回上百条论文，GitHub 一周可能只有两三个发布。
不设来源配额，短名单会被论文淹没，把真正的工程发布挤掉。

**为什么大模型失败就直接退出**
不产出残缺报告、不更新 `history.json`。顺序很关键：只有报告确实写成功才记录历史，
否则一次失败运行会把本周内容标记成「已收录」，造成**永久漏报**。

---

## 排错

| 现象 | 原因与处理 |
|---|---|
| `找不到时区 'Asia/Shanghai'` | Windows 无系统时区库，需 `pip install tzdata`（已在 requirements.txt） |
| `--run` 立即失败、退出码 1 | `llm.api_key` 为空。填写它，或先用 `--no-llm` |
| 计划任务日志中文乱码 | 用 `Get-Content ... -Encoding utf8` 读取 |
| `install_task.ps1` 报解析错误 | `.ps1` 被存成了无 BOM 的 UTF-8，PowerShell 5.1 会按 GBK 解析。请保存为 **UTF-8 with BOM** |
| 直接运行 `.ps1` 报「禁止执行脚本」 | 用 `-ExecutionPolicy Bypass` 调用（`main.py --install-task` 已自动加上）；或在当前会话执行 `Set-ExecutionPolicy -Scope Process RemoteSigned` |
| Linux：crontab 里的任务不执行 | ①看 `logs/cron.log`（脚本还没跑起来就挂了的现场都在这里）②确认 `crontab -l` 能看到条目 ③确认 cron 服务在跑：`systemctl status cron`（Ubuntu）或 `systemctl status crond`（RHEL） ④检查 `/var/log/syslog` 或 `journalctl -u cron` |
| Linux：日志里出现 `UnicodeEncodeError` | cron 没设 `LANG`，Python 退回了 ASCII。`run_weekly.sh` 已设 `PYTHONUTF8=1`，若仍报错说明不是通过该脚本启动的 |
| Linux：脚本报 `$'\r': command not found` | `.sh` 被存成了 CRLF 行尾。修复：`python scripts/normalize_shell_scripts.py` |
| 某个数据源长期无产出 | 跑 `python scripts/check_sources.py <源名>` 定位；Reddit 属预期内失效 |
| 报告条目少于 8 条 | 检查 `relevance.min_keyword_score` 是否过严；脚本会自动放宽，日志中会记录 |
| 想重新收录某条已发布内容 | 从 `state/history.json` 删掉对应条目，或用 `--run --force`（但仍会走跨周去重） |

---

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests/ -v
```
