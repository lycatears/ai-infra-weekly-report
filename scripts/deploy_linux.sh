#!/usr/bin/env bash
# =============================================================================
# Linux 一键部署：创建虚拟环境 -> 安装依赖 -> 自检 -> 注册 crontab 定时任务。
#
# 建议以**普通用户**身份运行（不要 sudo）：crontab 是用户级的，
# 以 root 注册会让任务以 root 身份运行，既没必要也不安全。
# 部署过程本身不需要任何 root 权限（只有装系统包时才需要）。
#
# 用法:
#   scripts/deploy_linux.sh [OPTIONS]
#
# 选项:
#   --python PATH   指定用于创建 venv 的解释器（默认自动探测 python3）
#   --recreate      删除已存在的 .venv 后重建（依赖装坏时的急救手段）
#   --upgrade       即使 .venv 已存在也重新安装/升级依赖
#   --no-dev        不安装开发依赖（requirements-dev.txt，含 pytest）
#   --no-cron       只部署运行环境，不注册定时任务
#   -h, --help      显示帮助
#
# 典型流程:
#   git clone <repo> && cd ai-infra-weekly-report
#   cp config.yaml config.local.yaml   # 在 config.local.yaml 里填 llm.api_key
#   scripts/deploy_linux.sh
#
# 部署完成后建议先手动跑一次验证：
#   .venv/bin/python main.py --check          # 只看调度判定，不联网
#   scripts/run_weekly.sh --no-llm            # 验证抓取与去重链路，不调大模型
# =============================================================================

set -euo pipefail

PYTHON_BIN=""
RECREATE=0
UPGRADE=0
INSTALL_DEV=1
INSTALL_CRON=1
MIN_PYTHON="3.10"

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

die() {
    printf '\n[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

info() { printf '[deploy] %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON_BIN="${2:?--python 需要参数}"; shift 2 ;;
        --recreate) RECREATE=1; shift ;;
        --upgrade) UPGRADE=1; shift ;;
        --no-dev) INSTALL_DEV=0; shift ;;
        --no-cron) INSTALL_CRON=0; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "未知选项: $1" ;;
    esac
done

# ------------------------------------------------------------------ 基本路径
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd -- "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

printf '\n[deploy] ============================================================\n'
printf '[deploy] AI Infra 周报 — Linux 部署\n'
printf '[deploy] ============================================================\n'
info "项目根目录 : $PROJECT_ROOT"

[ -f "$PROJECT_ROOT/requirements.txt" ] || die "找不到 requirements.txt，确认在项目根目录下的 scripts/ 里运行"
[ -f "$PROJECT_ROOT/config.yaml" ] || die "找不到 config.yaml，无法继续"

# 顺手修正脚本的可执行位：从 Windows 复制 / 解压过来的文件经常丢这个位。
if compgen -G "$SCRIPT_DIR/*.sh" >/dev/null; then
    chmod +x "$SCRIPT_DIR"/*.sh 2>/dev/null || true
fi

# ------------------------------------------------------------- 选择解释器
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    done
fi
[ -n "$PYTHON_BIN" ] ||
    die "找不到 Python。请先安装：Debian/Ubuntu 用 sudo apt install python3 python3-venv，RHEL 用 sudo dnf install python3"

[ -x "$PYTHON_BIN" ] || die "指定的解释器不可执行: $PYTHON_BIN"

if ! "$PYTHON_BIN" -c "import sys; raise SystemExit(0 if sys.version_info >= tuple(int(x) for x in '$MIN_PYTHON'.split('.')) else 1)"; then
    die "$PYTHON_BIN 版本为 $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')，本项目需要 Python $MIN_PYTHON 及以上"
fi
info "解释器     : $PYTHON_BIN ($("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"

# -------------------------------------------------------------------- venv
if [ "$RECREATE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    info "按 --recreate 删除已存在的 .venv"
    rm -rf -- "$VENV_DIR"
fi

# 防呆：.venv 里不该出现 Windows 虚拟环境的布局（Scripts/）。
# 典型触发场景是把仓库放在 Windows 分区上，同一目录先被 Windows 用过、
# 又被 WSL 用（或者反过来）。linux 侧再建 venv 会在同目录下多出一套 bin/，
# 并**覆盖 pyvenv.cfg**，把另一侧的解释器彻底弄坏 ——
# 而损坏发生在「看起来只是装了个依赖」的表象之下，极难排查。
if [ -d "$VENV_DIR/Scripts" ]; then
    die "$VENV_DIR 看起来是 **Windows** 虚拟环境（存在 Scripts/ 目录）。
       在这里建 Linux venv 会覆盖 pyvenv.cfg，导致 Windows 侧解释器彻底失效
       （症状是 python.exe 报 No Python at ...）。
       确需在本目录使用 Linux venv，请先删除：rm -rf .venv，或加 --recreate。"
fi

if [ -x "$VENV_PY" ]; then
    info "虚拟环境   : 已存在，复用 $VENV_DIR（要重建请加 --recreate）"
else
    info "虚拟环境   : 正在创建 $VENV_DIR ..."
    # Debian/Ubuntu 默认不带 venv 模块，失败信息很难懂，这里补上可操作提示。
    if ! venv_err="$("$PYTHON_BIN" -m venv "$VENV_DIR" 2>&1)"; then
        printf '%s\n' "$venv_err" >&2
        # 失败时目录里可能已经留下了半个 venv（bin/ 在、pip 缺失）。留着比没有更糟：
        # run_weekly.sh 看到 .venv/bin/python 可执行就会选中它，
        # 于是问题以「依赖缺失」的形式报出来（退出码 3），真正的原因被掩盖。
        rm -rf -- "$VENV_DIR"
        die "创建虚拟环境失败，已清理残缺的 .venv。常见原因是系统缺少 venv/ensurepip 模块：
       Debian/Ubuntu : sudo apt install python3-venv python3-pip
       RHEL/CentOS   : sudo dnf install python3-pip
       Alpine        : sudo apk add py3-pip python3-dev"
    fi
fi
[ -x "$VENV_PY" ] || die "虚拟环境创建后仍找不到 $VENV_PY"

# ------------------------------------------------------------------ 依赖
install_requirements() {
    local file="$1" label="$2"
    [ -f "$PROJECT_ROOT/$file" ] || { info "$label: 跳过（$file 不存在）"; return 0; }
    info "$label: 安装 $file ..."
    if ! "$VENV_PY" -m pip install --disable-pip-version-check -r "$PROJECT_ROOT/$file"; then
        die "安装 $file 失败。请检查网络或代理设置（可用 pip 的 -i 参数换镜像源）。"
    fi
}

if [ ! -x "$VENV_PY" ] || [ "$UPGRADE" -eq 1 ] || ! "$VENV_PY" -c 'import openai, requests, feedparser, yaml, dateutil, tzdata' >/dev/null 2>&1; then
    info "依赖       : 正在升级 pip ..."
    # 老版本 pip 升级失败不影响后续安装，因此不让它中断流程。
    "$VENV_PY" -m pip install --disable-pip-version-check --upgrade pip >/dev/null 2>&1 ||
        info "依赖       : pip 升级失败，继续使用当前版本"
    install_requirements "requirements.txt" "依赖       "
    if [ "$INSTALL_DEV" -eq 1 ]; then
        install_requirements "requirements-dev.txt" "开发依赖   "
    else
        info "开发依赖   : 已按 --no-dev 跳过"
    fi
else
    info "依赖       : 已安装，跳过（要强制重装请加 --upgrade）"
fi

# ------------------------------------------------------------------ 自检
# --check 会加载 config.yaml、构造 ZoneInfo、创建运行时目录，
# 相当于一次性验证「配置合法 + tzdata 可用 + 目录可写」三件事，且不联网。
info "自检       : 运行 main.py --check ..."
printf '%s\n' '------------------------------------------------------------------------------'
if ! "$VENV_PY" "$PROJECT_ROOT/main.py" --check; then
    printf '%s\n' '------------------------------------------------------------------------------'
    die "自检失败。请检查上面输出的配置错误（退出码 2 表示 config.yaml 非法）。"
fi
printf '%s\n' '------------------------------------------------------------------------------'

# ---------------------------------------------------------------- crontab
CRON_OK=0
if [ "$INSTALL_CRON" -eq 1 ]; then
    info "定时任务   : 注册 crontab ..."
    # 交给 Python 侧统一处理：这样 schedule.* 只以 config.yaml 为准，
    # 不会出现「脚本和配置各说各话」的情况。
    if "$VENV_PY" "$PROJECT_ROOT/main.py" --install-task; then
        CRON_OK=1
    else
        printf '\n[deploy] WARN: crontab 注册失败，运行环境本身是可用的。\n' >&2
        printf '[deploy] WARN: 修好原因后可手动重试：.venv/bin/python main.py --install-task\n' >&2
    fi
else
    info "定时任务   : 已按 --no-cron 跳过"
fi

# ------------------------------------------------------------------ 收尾
LLM_READY=0
if "$VENV_PY" -c "import sys; sys.path.insert(0, '$PROJECT_ROOT'); from src.config import load_config; raise SystemExit(0 if load_config().llm_configured else 1)" >/dev/null 2>&1; then
    LLM_READY=1
fi

printf '\n[deploy] ============================================================\n'
if [ "$LLM_READY" -eq 1 ]; then
    printf '[deploy] 部署完成（已检测到 llm.api_key）\n'
else
    printf '[deploy] 部署完成，但**还没配置大模型 API Key**\n'
fi
printf '[deploy] ============================================================\n'
printf '  解释器      : %s\n' "$VENV_PY"
printf '  定时任务    : %s\n' "$([ "$CRON_OK" -eq 1 ] && printf '已注册' || printf '未注册')"

if [ "$LLM_READY" -eq 0 ]; then
    printf '\n  [!] 下一步：填写 API Key，否则 --run 会立即以退出码 1 失败。\n'
    printf '      推荐不要改 config.yaml，而是用覆盖文件（不会进版本库）：\n'
    printf '        cp config.yaml config.local.yaml\n'
    printf '        # 编辑 config.local.yaml，填上 llm.api_key\n'
    printf '      或用环境变量：export AI_INFRA_LLM_API_KEY=sk-xxxx\n'
fi

printf '\n  建议按顺序验证：\n'
printf '    1) %s main.py --check        # 只看调度判定，不联网\n' "$VENV_PY"
printf '    2) %s scripts/run_weekly.sh --no-llm   # 验证抓取与去重，不调大模型\n' "$PROJECT_ROOT"
printf '    3) %s main.py --run          # 完整跑一次\n' "$VENV_PY"
printf '\n  查看定时任务 : crontab -l\n'
printf '  任务日志     : %s/logs/task-*.log 与 %s/logs/cron.log\n' "$PROJECT_ROOT" "$PROJECT_ROOT"
printf '  卸载定时任务 : %s main.py --uninstall-task\n' "$VENV_PY"
printf '  仅卸载 crontab 条目 : %s/scripts/uninstall_cron.sh\n' "$PROJECT_ROOT"

exit 0
