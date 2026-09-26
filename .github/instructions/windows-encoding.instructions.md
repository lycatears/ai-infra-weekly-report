---
name: "Windows 编码与 Shell 约束"
description: "Use when editing PowerShell scripts, shell wrappers, batch files, or any code that redirects Python/process output on Windows. Covers PowerShell 5.1 BOM requirement, ANSI/GBK default file encoding, PYTHONIOENCODING for redirected stdout, and Git Bash MSYS path conversion of slash-prefixed arguments."
applyTo: ["**/*.ps1", "**/*.psm1", "**/*.bat", "**/*.cmd", "scripts/**"]
---

# Windows 编码与 Shell 约束

这类问题的特征是**静默失败**：没有可读报错，只有乱码或解析错误，且往往在「看起来跑通了」之后才暴露。
凡是改动 `.ps1` 或任何重定向进程输出的代码，都按下面四条逐项自查。

## 1. `.ps1` 必须 UTF-8 with BOM

PowerShell 5.1 对无 BOM 的 `.ps1` 按系统 ANSI（中文系统 = GBK）解析，中文变 `椤圭洰鏍?`，
并报 `字符串缺少终止符` / `数组索引表达式丢失或无效`。

编辑后必须转码：

```python
p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8-sig")
```

`tests/test_scripts.py` 有回归守卫。

## 2. 重定向 Python 输出必须设 `PYTHONIOENCODING=utf-8`

Windows 上 Python 在 stdout 被重定向时使用 locale 编码（GBK），**与 shell 无关**——
实测 Git Bash 下 `python t.py > out.txt` 写出的仍是 GBK 字节。

```powershell
$env:PYTHONIOENCODING = 'utf-8'   # 或 PYTHONUTF8=1
```

## 3. PS 5.1 的 `Add-Content` / `Tee-Object` 默认写 GBK

`Tee-Object` 在 PS 5.1 上**没有** `-Encoding` 参数，无法修正。改用：

```powershell
Start-Process -FilePath $py -ArgumentList $args -NoNewWindow -Wait -PassThru `
  -RedirectStandardOutput $out -RedirectStandardError $err
[System.IO.File]::AppendAllText($log, $text, [System.Text.UTF8Encoding]::new($false))
```

读取时用 `Get-Content -Encoding utf8`。此写法还能避免 `2>&1` 产生 `NativeCommandError` 噪音。

## 4. Git Bash 不是上述问题的解决方案

- 它自身重定向是 UTF-8 + LF（优于 PowerShell），但**不改**第 1、2 条
- 新增坑：MSYS 会把传给原生 exe 的 `/` 参数改写成路径 ——
  `schtasks /query` 会报 `'D:/Program Files/Git/query'`。用 `//query` 或 `MSYS_NO_PATHCONV=1`
- 读取原生 Windows 程序的 GBK 输出仍是乱码

## 自查

用字节验证，别用眼睛：`od -An -tx1 file`（bash）/ `Format-Hex file`（PowerShell）。
diff 中出现 `锟斤拷`、`□` 即说明编码已损坏。
