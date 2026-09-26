#!/usr/bin/env bash
# =============================================================================
# 卸载「AI Infra 周报」的 crontab 定时任务。
#
# 只删除由 scripts/install_cron.sh 写入的那一段（靠首尾标记识别），
# 用户自己写的其它 crontab 条目**原样保留**。
#
# 与 uninstall_task.ps1 一致：任务不存在时视为成功（幂等），不报错。
#
# 用法:
#   scripts/uninstall_cron.sh [--task-name NAME]
# =============================================================================

set -euo pipefail

TASK_NAME="AI-Infra-Weekly-Report"

while [ $# -gt 0 ]; do
    case "$1" in
        --task-name)
            [ $# -ge 2 ] || { printf 'ERROR: --task-name requires a value\n' >&2; exit 2; }
            TASK_NAME="$2"
            shift 2
            ;;
        -h | --help)
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0
            ;;
        *)
            printf 'ERROR: unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

MARK_BEGIN="# >>> ${TASK_NAME} >>>"
MARK_END="# <<< ${TASK_NAME} <<<"

# crontab -l 在「尚无 crontab」时以退出码 1 结束并把错误写到 stderr，
# 这是最经典的踩坑点：不加 `|| true` 会因 set -e 直接中断。
read_crontab() {
    crontab -l 2>/dev/null || true
}

strip_block() {
    awk -v begin="$MARK_BEGIN" -v end="$MARK_END" '
        $0 == begin { skip = 1; next }
        $0 == end   { skip = 0; next }
        !skip       { print }
    '
}

current="$(read_crontab)"

if ! printf '%s\n' "$current" | grep -qF -- "$MARK_BEGIN"; then
    printf '[uninstall_cron] 未找到任务「%s」写入的条目，无需卸载。\n' "$TASK_NAME"
    exit 0
fi

printf '[uninstall_cron] 正在移除 crontab 中的任务「%s」...\n' "$TASK_NAME"

remaining="$(printf '%s\n' "$current" | strip_block)"

# 去掉我们的段落之后如果什么都不剩，就用 crontab -r 彻底移除，
# 免得留下一个空的 crontab（各发行版对「空 crontab」的处理并不一致）。
if [ -z "$(printf '%s' "$remaining" | tr -d '[:space:]')" ]; then
    crontab -r 2>/dev/null || true
else
    printf '%s\n' "$remaining" | crontab -
fi

if read_crontab | grep -qF -- "$MARK_BEGIN"; then
    printf '[uninstall_cron] 卸载失败，crontab 中仍有任务条目。\n' >&2
    exit 1
fi

printf '[uninstall_cron] 已卸载。\n'
printf '[uninstall_cron] 注意：已生成的历史报告与 state/history.json 均已保留。\n'
printf '[uninstall_cron] 日志文件 logs/cron.log 与 logs/task-*.log 亦保留，可自行清理。\n'

exit 0
