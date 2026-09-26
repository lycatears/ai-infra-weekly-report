# AGENTS.md

本文件被各类 AI 编码助手自动读取（GitHub Copilot、Codex、Cursor 等）。

## 环境与命令

- 本项目使用自己的 venv：`.\.venv\Scripts\python.exe`（Linux：`.venv/bin/python`），**不要**用全局 `python`
- 依赖安装：`.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt`
- 跑测试：`.\.venv\Scripts\python.exe -m pytest tests/`
- 查看调度判定：`.\.venv\Scripts\python.exe main.py --check`
- 不调大模型的联调：`.\.venv\Scripts\python.exe main.py --run --no-llm`
- Linux 一键部署：`scripts/deploy_linux.sh`（建 venv + 装依赖 + 注册 crontab）
- 完整说明见 `README.md`

## Windows 编码约束（必须遵守）

> 这一节的坑**不会**给出可读的报错，只会静默乱码或解析失败，且在「看起来跑通了」之后才暴露。
> 改动脚本、重定向输出或读取文件前，先读这一节。

### 1. `.ps1` 必须存为 UTF-8 **with BOM**

PowerShell 5.1 对**无 BOM** 的 `.ps1` 按系统 ANSI（中文系统 = GBK）解析，导致中文变成
`椤圭洰鏍?`，并报 `字符串缺少终止符` / `数组索引表达式丢失或无效` 等**解析错误**。

用任何编辑工具写入 `.ps1` 后都必须转码确认：

```python
p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8-sig")
```

`tests/test_scripts.py` 有回归守卫，别绕过它。

### 2. 重定向 Python 输出时必须设 `PYTHONIOENCODING=utf-8`

Windows 上 Python 在 stdout **被重定向**（管道 / `>`）时使用 locale 编码（中文系统 = GBK），
**与用哪个 shell 无关**。实测 Git Bash 下同样如此：

```bash
./.venv/Scripts/python.exe t.py > out.txt   # 写出 d6 d0 ce c4（GBK 的「中文」），不是 UTF-8
```

因此 `scripts/run_weekly.ps1` 里设置了 `$env:PYTHONIOENCODING='utf-8'`，任何新脚本也必须设置
（或设 `PYTHONUTF8=1`）。

### 3. PowerShell 5.1 写文件默认 GBK

`Add-Content` / `Tee-Object` 不加 `-Encoding` 会用系统 ANSI 编码；而 `Tee-Object` 在 PS 5.1 上
**没有** `-Encoding` 参数，无法修正。

可靠写法（见 `scripts/run_weekly.ps1`）：

```powershell
Start-Process -FilePath $py -ArgumentList $args -NoNewWindow -Wait -PassThru `
  -RedirectStandardOutput $out -RedirectStandardError $err
[System.IO.File]::AppendAllText($log, $text, [System.Text.UTF8Encoding]::new($false))
```

顺带：`Start-Process` 还能避免 `2>&1` 把原生 stderr 包装成 `NativeCommandError` 的噪音。
读取时须 `Get-Content -Encoding utf8`。

### 4. 使用 Git Bash 时的额外坑

Git Bash **不能**替代上述 1–3，还会引入新问题：

- MSYS 会把传给**原生 exe** 的 `/` 开头参数改写成路径：
  `schtasks /query` 报错 `'D:/Program Files/Git/query'`。改用 `//query`，或设 `MSYS_NO_PATHCONV=1`
- Git Bash 自身的重定向写文件是 UTF-8 + LF（这点优于 PowerShell），
  但**原生 Windows 程序输出的 GBK 文本**读出来依然是乱码

### 5. 验证方式

不要靠「看起来正常」判断。用字节确认：

- bash：`od -An -tx1 file`
- PowerShell：`Format-Hex file`

若在 diff 里看到 `锟斤拷`、`□`、或 GBK 字节（`d6 d0`、`e5 91` 之外的 `b0 a1` 类），立即停下修编码。

## Linux 脚本约束（必须遵守）

`.sh` 与 `.ps1` 的编码要求**正好相反**，不要想当然地「统一」处理：

| 扩展名 | BOM | 行尾 |
|---|---|---|
| `.ps1` | **必须有** UTF-8 BOM | CRLF |
| `.sh` | **必须没有** BOM | **LF** |

- **`.sh` 带 BOM** → shebang 失效（内核看到的是 `\ufeff#!/usr/bin/env bash`），
  报 `No such file or directory`
- **`.sh` 是 CRLF** → Linux 上 bash 报 `$'\r': command not found`，错误信息
  完全指不到真正原因，而在 Windows 编辑器里打开看不出任何异常

修正工具：`python scripts/normalize_shell_scripts.py`。`tests/test_scripts.py` 有回归守卫，
`.gitattributes` 已声明 `.sh text eol=lf`。

### cron 环境与交互式 shell 不同

改 `scripts/run_weekly.sh` 时注意：

- cron **不读 shell profile**：`PATH` 只有 `/usr/bin:/bin`，
  `LANG` / `LC_ALL` 往往**完全未设置**
- 此时若 Python 的 UTF-8 模式也没打开，stdout 会退化成 ascii，
  打印中文直接 `UnicodeEncodeError` **崩溃**（比 Windows 的乱码更凶险，但至少会报错）
- **不要依赖发行版默认**：实测 Ubuntu 26.04 / Python 3.14 默认就已打开 UTF-8 模式，
  不显式设置也复现不出崩溃；但旧版本与其他发行版并非如此。
  显式 `PYTHONUTF8=1` 成本为零，所以照设。
  真实 Linux 上的复现/验证命令：

  ```bash
  printf 'print("中文")\n' > /tmp/t.py
  env -i LC_ALL=C PYTHONUTF8=0 python3 /tmp/t.py   # encoding=ascii -> Traceback
  env -i LC_ALL=C PYTHONUTF8=1 python3 /tmp/t.py   # encoding=utf-8 -> 正常
  ```
- 脚本自己生成的行**只用 ASCII**（与 `run_weekly.ps1` 同一约定）
- cron 不会因为上一次还在跑就跳过本次，必须靠 `flock` 自己上锁
- cron **没有** `StartWhenAvailable`，关机错过的任务不会自动补跑 ——
  Linux 侧靠 `install_cron.sh` 写入的**每小时兜底轮询**来补，
  这是与 Windows 侧最大的机制差异

## 其他环境陷阱

- **venv 里必须有 `tzdata`**。Windows 无系统时区库，`zoneinfo.ZoneInfo("Asia/Shanghai")` 会抛
  `ZoneInfoNotFoundError`。已在 `requirements.txt` 无条件声明，别加 `win32` 平台标记
  （精简 Linux 容器同样需要）。

## 代码约定

- 内部时间**一律用 UTC-aware** `datetime`；仅在渲染时转 `Asia/Shanghai`
- 报告文件名由「周」决定，与运行时刻无关 —— 这是幂等性的基础，不要破坏
- `state/history.json` **只在报告成功写入之后**才更新；失败运行不得污染它
- 报告写入必须走 `src/io_utils.py` 的原子写入
