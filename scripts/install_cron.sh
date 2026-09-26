#!/usr/bin/env bash
# =============================================================================
# 注册「AI Infra 周报」的 crontab 定时任务。
#
# 写入**三条**记录（对应用户原始需求里的「定时 + 缺失就补跑」）：
#
#   1. 每周固定触发   —— 与 config.yaml 的 schedule.* 一致（默认周五 22:00）
#   2. 兜底轮询补跑   —— 默认每小时一次。这是 Linux 上最关键的一条：
#                        周五 22:00 时机器如果是关机的，cron 不会补跑
#                        （cron 没有 Windows 计划任务的 StartWhenAvailable），
#                        靠 `main.py --run` 自身的幂等判定在这里被补上。
#   3. @reboot 立即补跑 —— 开机后 2 分钟就跑一次，让补跑更及时。
#
# 幂等性：所有条目都写在首尾标记之间，重复执行会先整段删除再重写，
# 不会产生重复任务，也不碰用户自己的其它 crontab 条目。
#
# 用法（一般由 `python main.py --install-task` 调用）:
#   scripts/install_cron.sh [--task-name N] [--weekday 1-7] [--hour 0-23]
#                           [--minute 0-59] [--catchup-minutes N]
#                           [--boot-delay SECONDS] [--dry-run]
#
# 选项:
#   --catchup-minutes N   兜底轮询间隔（分钟），0 表示关闭。默认 60。
#                         建议取 60 的约数（5/10/15/20/30/60），否则触发时刻会不均匀。
#   --boot-delay SECONDS  @reboot 之后的延迟秒数，0 表示不要这条。默认 120。
#   --dry-run             只打印将要写入的内容，不修改 crontab。
#
# 注意：--weekday 用的是 ISO 编号（1=周一 … 7=周日），内部会转成 cron 编号
# （0=周日 … 6=周六）。这是最容易写错的地方，已由 tests/test_scripts.py 守卫。
# =============================================================================

set -euo pipefail

TASK_NAME="AI-Infra-Weekly-Report"
WEEKDAY=5
HOUR=22
MINUTE=0
CATCHUP_MINUTES=60
BOOT_DELAY=120
DRY_RUN=0

# 打印文件开头的注释块作为帮助文本。用 awk 而不是固定的行号区间，
# 免得以后改注释把帮助文本切错。
usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

die() {
    printf '[install_cron] ERROR: %s\n' "$*" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --task-name) TASK_NAME="${2:?--task-name 需要参数}"; shift 2 ;;
        --weekday) WEEKDAY="${2:?--weekday 需要参数}"; shift 2 ;;
        --hour) HOUR="${2:?--hour 需要参数}"; shift 2 ;;
        --minute) MINUTE="${2:?--minute 需要参数}"; shift 2 ;;
        --catchup-minutes) CATCHUP_MINUTES="${2:?--catchup-minutes 需要参数}"; shift 2 ;;
        --boot-delay) BOOT_DELAY="${2:?--boot-delay 需要参数}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "未知选项: $1" ;;
    esac
done

# ------------------------------------------------------------------ 参数校验
is_int() { case "$1" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

is_int "$WEEKDAY" || die "--weekday 必须是整数，收到: $WEEKDAY"
is_int "$HOUR" || die "--hour 必须是整数，收到: $HOUR"
is_int "$MINUTE" || die "--minute 必须是整数，收到: $MINUTE"
is_int "$CATCHUP_MINUTES" || die "--catchup-minutes 必须是整数，收到: $CATCHUP_MINUTES"
is_int "$BOOT_DELAY" || die "--boot-delay 必须是整数，收到: $BOOT_DELAY"

[ "$WEEKDAY" -ge 1 ] && [ "$WEEKDAY" -le 7 ] || die "--weekday 必须在 1..7（1=周一，7=周日）"
[ "$HOUR" -ge 0 ] && [ "$HOUR" -le 23 ] || die "--hour 必须在 0..23"
[ "$MINUTE" -ge 0 ] && [ "$MINUTE" -le 59 ] || die "--minute 必须在 0..59"
# 0=关闭，1..59=每 N 分钟，60=每小时整点
[ "$CATCHUP_MINUTES" -ge 0 ] && [ "$CATCHUP_MINUTES" -le 60 ] ||
    die "--catchup-minutes 必须在 0..60（0=关闭，60=每小时整点）"
if [ "$CATCHUP_MINUTES" -ne 0 ] && [ "$CATCHUP_MINUTES" -ne 60 ]; then
    [ $((60 % CATCHUP_MINUTES)) -eq 0 ] ||
        die "--catchup-minutes 应取 60 的约数（5/10/15/20/30），否则触发时刻会不均匀，收到: $CATCHUP_MINUTES"
fi

# ISO 星期 -> cron 星期：ISO 7(周日) 对应 cron 0(周日)，其余数字相同
if [ "$WEEKDAY" -eq 7 ]; then CRON_DOW=0; else CRON_DOW="$WEEKDAY"; fi

# 星期名用 bash 数组按下标取，**不要**用 `cut -c`：中文是 3 字节，
# 在 C locale 下 cut 会按字节切出一个乱码字符。数组下标是字节安全的。
DAY_NAMES=(一 二 三 四 五 六 日)
DAY_NAME="${DAY_NAMES[$((WEEKDAY - 1))]}"

# ---------------------------------------------------------------- 关键资源
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
RUNNER="$SCRIPT_DIR/run_weekly.sh"
CRON_LOG="$PROJECT_ROOT/logs/cron.log"

[ -f "$RUNNER" ] || die "找不到执行体脚本: $RUNNER"

# 注意：crontab 命令的存在性检查放到后面（写入之前）。
# 因为 --dry-run 只需要把内容打印出来，即使用户的系统还没装 cron
# 也应该能看到将要写入的内容（可以手工粘到别的调度器里）。

# cron 会把整行交给 /bin/sh 执行。用 bash 的绝对路径最稳妥：
# 既不依赖 PATH（cron 的 PATH 极简），也不依赖脚本的可执行位
# （从 Windows 复制过来时很容易丢）。实在没有 bash 就明确报错。
BASH_BIN="$(command -v bash || true)"
[ -n "$BASH_BIN" ] ||
    die "找不到 bash。本套脚本依赖 bash 特性（数组、PIPESTATUS），无法用 POSIX sh 运行。
       精简镜像可安装：Alpine 用 apk add bash，Debian/Ubuntu 用 apt install bash"

mkdir -p -- "$PROJECT_ROOT/logs"

# ------------------------------------------------------------ crontab 安全性
# cron 把整行交给 sh，因此：
#   * 含单引号的路径无法安全单引号包裹 -> 直接报错
#   * cron 自己把未转义的 % 当作换行符 -> 必须转义为 \%，这里直接报错更省心
validate_path() {
    case "$1" in
        *"'"*) die "路径含单引号，无法安全写入 crontab: $1" ;;
        *"%"*) die "路径含 %（cron 保留字符，需转义为 \\%），请换用不含 % 的路径: $1" ;;
    esac
    case "$1" in
        /*) : ;;
        *) die "路径必须是绝对路径: $1" ;;
    esac
}
validate_path "$RUNNER"
validate_path "$CRON_LOG"
validate_path "$BASH_BIN"

shq() { printf "'%s'" "$1"; }

MARK_BEGIN="# >>> ${TASK_NAME} >>>"
MARK_END="# <<< ${TASK_NAME} <<<"

# 每条任务都把输出追加到 logs/cron.log。
# run_weekly.sh 在成功时只往 stdout 打一行摘要，所以这个文件不会被刷爆，
# 但一旦出现「脚本还没跑起来就挂了」（找不到 bash、语法错误等）也能留下现场。
JOB="$(shq "$BASH_BIN") $(shq "$RUNNER") >> $(shq "$CRON_LOG") 2>&1"

CATCHUP_SCHEDULE=""
if [ "$CATCHUP_MINUTES" -eq 60 ]; then
    CATCHUP_SCHEDULE="0 * * * *"
elif [ "$CATCHUP_MINUTES" -gt 0 ]; then
    CATCHUP_SCHEDULE="*/${CATCHUP_MINUTES} * * * *"
fi

BOOT_SCHEDULE=""
[ "$BOOT_DELAY" -gt 0 ] && BOOT_SCHEDULE="@reboot sleep ${BOOT_DELAY} &&"

build_block() {
    printf '%s\n' "$MARK_BEGIN"
    printf '# 本段由 scripts/install_cron.sh 自动生成，请勿手工编辑；\n'
    printf '# 卸载请执行：python main.py --uninstall-task\n'
    printf '# 项目根目录：%s\n' "$PROJECT_ROOT"
    printf '#\n'
    printf '# 每周固定触发（与 config.yaml 的 schedule.* 一致）\n'
    printf '%s %s * * %s %s\n' "$MINUTE" "$HOUR" "$CRON_DOW" "$JOB"
    if [ -n "$CATCHUP_SCHEDULE" ]; then
        printf '#\n'
        printf '# 兜底轮询：周五 22:00 时机器关机 / 上一次失败，都会在这里被补上。\n'
        printf '# 不需要的话可执行：scripts/install_cron.sh --catchup-minutes 0\n'
        printf '%s %s\n' "$CATCHUP_SCHEDULE" "$JOB"
    fi
    if [ -n "$BOOT_SCHEDULE" ]; then
        printf '#\n'
        printf '# 开机后尽快补跑一次（cron 守护进程启动时触发）\n'
        printf '%s %s\n' "$BOOT_SCHEDULE" "$JOB"
    fi
    printf '%s\n' "$MARK_END"
}

# crontab -l 在「尚无 crontab」时以退出码 1 结束并把错误写到 stderr，
# 不加 `|| true` 会因 set -e 直接中断——最经典的踩坑点。
read_crontab() { crontab -l 2>/dev/null || true; }

strip_block() {
    awk -v begin="$MARK_BEGIN" -v end="$MARK_END" '
        $0 == begin { skip = 1; next }
        $0 == end   { skip = 0; next }
        !skip       { print }
    '
}

NEW_BLOCK="$(build_block)"

printf '[install_cron] 项目根   : %s\n' "$PROJECT_ROOT"
printf '[install_cron] 任务名称 : %s\n' "$TASK_NAME"
printf '[install_cron] 触发时机 : 每周%s %02d:%02d（cron 表达式 "%s %s * * %s"）\n' \
    "$DAY_NAME" "$HOUR" "$MINUTE" "$MINUTE" "$HOUR" "$CRON_DOW"
printf '[install_cron] 兜底轮询 : %s\n' "${CATCHUP_SCHEDULE:-已关闭}"
if [ -n "$BOOT_SCHEDULE" ]; then
    printf '[install_cron] 开机补跑 : 开机后 %s 秒触发一次\n' "$BOOT_DELAY"
else
    printf '[install_cron] 开机补跑 : 已关闭\n'
fi
printf '[install_cron] 执行体   : %s %s\n' "$BASH_BIN" "$RUNNER"

if [ "$DRY_RUN" -eq 1 ]; then
    printf '\n[install_cron] --dry-run，以下内容**不会**写入 crontab：\n'
    printf '%s\n' '------------------------------------------------------------------------------'
    printf '%s\n' "$NEW_BLOCK"
    printf '%s\n' '------------------------------------------------------------------------------'
    printf '\n[install_cron] 若已有同名段落，它会被替换成上面这段。\n'
    command -v crontab >/dev/null 2>&1 ||
        printf '[install_cron] 注意：当前系统没有 crontab 命令，实际注册会失败。\n' >&2
    exit 0
fi

command -v crontab >/dev/null 2>&1 ||
    die "找不到 crontab 命令。请先安装 cron 服务（Debian/Ubuntu: sudo apt install cron；RHEL: sudo dnf install cronie）"

# -------------------------------------------------------------------- 写入
existing="$(read_crontab)"
remaining="$(printf '%s\n' "$existing" | strip_block)"

{
    if [ -n "$(printf '%s' "$remaining" | tr -d '[:space:]')" ]; then
        printf '%s\n' "$remaining"
    fi
    printf '%s\n' "$NEW_BLOCK"
} | crontab -

# -------------------------------------------------------------------- 校验
if [ "$(read_crontab | grep -cF -- "$MARK_BEGIN")" -ne 1 ]; then
    die "写入后校验失败：crontab 中恰好应有 1 个标记段落，请用 crontab -l 检查"
fi

printf '\n[install_cron] 注册完成，当前 crontab：\n'
printf '%s\n' '------------------------------------------------------------------------------'
read_crontab
printf '%s\n' '------------------------------------------------------------------------------'
printf '\n手动测试（立刻跑一次）: %s %s\n' "$BASH_BIN" "$RUNNER"
printf '只判定不执行         : .venv/bin/python main.py --check\n'
printf '查看任务日志         : %s/task-*.log 与 %s\n' "$PROJECT_ROOT/logs" "$CRON_LOG"
printf '卸载                 : python main.py --uninstall-task\n'

exit 0
