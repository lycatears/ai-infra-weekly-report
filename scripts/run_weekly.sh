#!/usr/bin/env bash
# =============================================================================
# AI Infra 周报生成任务的实际执行体（由 cron 调用）。
#
# cron 只负责「按时启动这个脚本」，真正的环境准备都在这里：
#
#   1. 切换到项目根目录 —— cron 的工作目录是发起者的 $HOME，
#      不切目录会让 config.yaml / logs / 输出目录等相对路径全部失效。
#   2. 补齐 cron 的极简环境 —— cron **不读** shell profile：
#        * PATH 通常只有 /usr/bin:/bin
#        * LANG / LC_ALL 往往完全未设置。此时若 Python 的 UTF-8 模式也没打开，
#          stdout 会退化成 ascii，一打印中文就 UnicodeEncodeError **直接崩溃**
#          （比 Windows 的静默乱码更凶险，因为它至少会报错）
#
#        实测（Ubuntu 26.04 / Python 3.14）：
#          LC_ALL=C + PYTHONUTF8=0  ->  encoding=ascii，exit=1（Traceback）
#          LC_ALL=C + PYTHONUTF8=1  ->  encoding=utf-8，正常
#        注意：较新的 Python 会默认打开 UTF-8 模式（PEP 686 计划自 3.15 起默认），
#        所以这一条在某些发行版上不显式设置也跑得好好的。
#        但那取决于版本与发行版，**不能依赖** —— 显式设上，成本为零。
#   3. 优先用项目内的 .venv/bin/python，回退到 PATH 里的 python3。
#      固定用 .venv 可避免「系统 Python 升级后任务静默失效」。
#   4. 用 flock 防止并发重入 —— cron **不会**因为上一次还在跑就跳过本次，
#      对应 Windows 计划任务的 MultipleInstances=IgnoreNew。
#   5. **严格透传退出码**。cron 自己不重试，但监控与包装脚本依赖退出码。
#
# 用法:
#   scripts/run_weekly.sh [--force] [--dry-run] [--no-llm]
#
# 退出码:
#   0    成功（含「本次无需生成」这种正常跳过）
#   1    运行时失败（抓取全部失败、大模型不可用等）
#   2    配置非法
#   3    未预期的内部错误
#   127  找不到 Python 解释器
# =============================================================================

set -euo pipefail

umask 022

# ------------------------------------------------------------------ 基本路径
# 用 BASH_SOURCE 而不是 $0：即便被 `source` 进来也能正确定位。
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd -- "$PROJECT_ROOT"

# -------------------------------------------------------------------- 环境
# cron 的 PATH 只有 /usr/bin:/bin，显式补齐以免 .venv 里的工具找不到。
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
# cron 一般会设 HOME，但某些最小化 cron 实现不设；缺了会让 pip 之类的工具行为异常。
export HOME="${HOME:-/tmp}"

# -------------------------------------------------------------------- 编码
# PYTHONUTF8=1 才是决定性开关：它打开 Python 的 UTF-8 模式（PEP 540），
# 同时覆盖 stdout / stderr / 文件系统编码，而且**不依赖系统 locale**。
# 只设 LC_ALL 是不够的：系统上可能根本没有可用的 UTF-8 locale，
# 那种情况下 coercion（PEP 538）会失败，stdout 照样退化成 ascii。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

pick_utf8_locale() {
    command -v locale >/dev/null 2>&1 || return 0
    local available candidate
    available="$(locale -a 2>/dev/null || true)"
    [ -n "$available" ] || return 0
    for candidate in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
        if printf '%s\n' "$available" | grep -qxF -- "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    # 退而求其次：任意一个 UTF-8 locale
    printf '%s\n' "$available" | grep -iE '\.utf-?8$' | head -n 1 || true
}

UTF8_LOCALE="$(pick_utf8_locale)"
if [ -n "$UTF8_LOCALE" ]; then
    export LANG="$UTF8_LOCALE" LC_ALL="$UTF8_LOCALE"
fi

# ------------------------------------------------------------- 选择解释器
VENV_PY="$PROJECT_ROOT/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
    printf '[run_weekly] WARN: .venv not found, falling back to %s\n' "$PYTHON" >&2
else
    printf '[run_weekly] ERROR: no Python interpreter found; run scripts/deploy_linux.sh first\n' >&2
    exit 127
fi

# -------------------------------------------------------------------- 日志
# 编码约定与 run_weekly.ps1 一致：**本脚本自己生成的行只用 ASCII**，
# 这样无论宿主的 locale 是什么都不可能写出乱码。子进程输出已经是 UTF-8
# （PYTHONUTF8=1 保证），直接按字节追加即可。
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p -- "$LOG_DIR"
LOG_FILE="$LOG_DIR/task-$(date +%Y%m%d).log"

log_line() {
    printf '%s\n' "$*" >>"$LOG_FILE"
}

# -------------------------------------------------------------------- 并发锁
LOCK_FILE="$LOG_DIR/.run_weekly.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>>"$LOCK_FILE"
    if ! flock -n 9; then
        log_line "[run_weekly] SKIP another instance is already running"
        printf '[run_weekly] SKIP: previous run still in progress\n' >&2
        exit 0
    fi
else
    # flock 来自 util-linux，主流发行版（Debian/Ubuntu/RHEL/SUSE）都自带，
    # 只有极精简的镜像（如 Alpine + busybox）才缺。
    #
    # 这里**故意**不做 mkdir 式的降级锁：那种锁一旦进程被强杀就会留下陈旧锁，
    # 反而让之后所有运行都被静默跳过 —— 比「没有锁」更糟。
    # 并发的最坏后果只是重复抓取、重复调用大模型，而报告本身是幂等的
    # （文件名由「周」决定 + 原子写入）。
    log_line "[run_weekly] WARN flock not available, running without concurrency guard"
    log_line "[run_weekly] HINT install util-linux to enable the concurrency guard"
fi

# ------------------------------------------------------------------ 参数拼装
CLI_ARGS=(main.py --run)
for arg in "$@"; do
    case "$arg" in
        --force | --dry-run | --no-llm) CLI_ARGS+=("$arg") ;;
        *)
            printf '[run_weekly] ERROR: unsupported option: %s\n' "$arg" >&2
            printf 'usage: %s [--force] [--dry-run] [--no-llm]\n' "${0##*/}" >&2
            exit 2
            ;;
    esac
done

STARTED_AT="$(date +'%Y-%m-%d %H:%M:%S')"
START_EPOCH="$(date +%s)"
log_line ''
log_line '=============================================================================='
log_line "[run_weekly] START $STARTED_AT"
log_line "[run_weekly] ROOT  $PROJECT_ROOT"
log_line "[run_weekly] PY    $PYTHON"
log_line "[run_weekly] LOCALE ${LC_ALL:-<unset>}"
log_line "[run_weekly] CMD   $PYTHON ${CLI_ARGS[*]}"

# -------------------------------------------------------------------- 执行
# 先落到临时文件再整体追加：既能拿到真实退出码，又能避免子进程输出
# 与包装脚本的日志行交错（ps1 版用的是同一策略）。
TMP_OUT="$(mktemp "${TMPDIR:-/tmp}/ai-infra-run-XXXXXX")"
cleanup() { rm -f -- "$TMP_OUT"; }
trap cleanup EXIT

set +e
"$PYTHON" "${CLI_ARGS[@]}" >"$TMP_OUT" 2>&1
EXIT_CODE=$?
set -e

cat -- "$TMP_OUT" >>"$LOG_FILE"

ELAPSED=$(( $(date +%s) - START_EPOCH ))
log_line "[run_weekly] END   $(date +'%Y-%m-%d %H:%M:%S') | EXIT $EXIT_CODE | ELAPSED ${ELAPSED}s"

case "$EXIT_CODE" in
    0) STATUS='OK' ;;
    1) STATUS='FAILED runtime error (nothing fetched / LLM unavailable)' ;;
    2) STATUS='FAILED invalid config.yaml' ;;
    127) STATUS='FAILED python interpreter not found' ;;
    *) STATUS="FAILED unexpected exit code $EXIT_CODE" ;;
esac
log_line "[run_weekly] STATUS $STATUS"

# stdout 只放一行摘要：cron 会把它收进 logs/cron.log，
# 成功时不产生噪音，失败时一眼能看出问题。
if [ -t 1 ]; then
    cat -- "$TMP_OUT"
fi
printf '[run_weekly] %s | EXIT %s | %ss | %s\n' "$STATUS" "$EXIT_CODE" "$ELAPSED" "$LOG_FILE"

exit "$EXIT_CODE"
