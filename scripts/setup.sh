#!/usr/bin/env bash
# 一键配置：新电脑上把项目从「刚 clone 下来」带到「能跑起来」。
#
#   bash scripts/setup.sh          # 全自动（默认非交互，适合 CI / 远程机器）
#   bash scripts/setup.sh --ollama # 强制用本机 Ollama
#   bash scripts/setup.sh --mock   # 不配模型，用离线协作桩先把服务跑起来
#   bash scripts/setup.sh --dry-run# 只打印计划，不动任何文件
#
# 做的事（每一步都幂等，重复跑不会破坏已有配置）：
#   1. 找解释器（python3.12 优先，其次 3.11/3）并建 .venv
#   2. 装依赖（自动绕开本机 pip.conf 里解析不了的 extra-index-url，可 --index 换源）
#   3. 生成 .env（不存在时从 .env.example 复制，权限 600）
#   4. 决定模型：环境里已有 key -> 用云端；检测到本机 Ollama -> 用 Ollama；都没有 -> 离线协作桩
#   5. 建库 + 种子数据（幂等）
#   6. 真模型就顺手跑一遍协议自检（失败只警告，不中断）
#   7. 打印下一步
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

DRY_RUN=0
FORCE_OLLAMA=0
FORCE_MOCK=0
SKIP_DEPS=0
SKIP_SEED=0
SKIP_CHECK=0
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"

usage() {
  cat <<'EOF'
用法: bash scripts/setup.sh [选项]

  -y, --yes         显式声明非交互（默认就是非交互，此开关只为表达清楚）
      --ollama      强制按本机 Ollama 配置（http://127.0.0.1:11434/v1）
      --mock        不配模型，用 MOCK_MODE=script 离线协作桩起服务
      --index URL   装依赖用的 pip 索引（默认清华源）
      --skip-deps   跳过建 venv / 装依赖（已有环境时用）
      --skip-seed   跳过建库与种子数据
      --skip-check  跳过模型协议自检
      --dry-run     只打印将要做什么，不改任何文件
  -h, --help        显示本帮助

配置文件永远是 .env（不入库）；要交互式粘贴 key 用 bash setup_env.sh --prompt。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ;;
    --ollama) FORCE_OLLAMA=1 ;;
    --mock) FORCE_MOCK=1 ;;
    --index) shift; PIP_INDEX="${1:-}" ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --skip-seed) SKIP_SEED=1 ;;
    --skip-check) SKIP_CHECK=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then
  B="$'\033[1m'"; G="$'\033[32m'"; Y="$'\033[33m'"; R="$'\033[31m'"; N="$'\033[0m'"
else
  B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
fail() { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ---- 1. 解释器 -------------------------------------------------------------
step "找解释器"
PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then PY="$cand"; break; fi
  fi
done
if [ -z "$PY" ]; then
  fail "没找到 Python >= 3.11，请先装 python3.12（Debian/Ubuntu: apt install python3.12 python3.12-venv）"
  exit 1
fi
ok "$("$PY" --version 2>&1)（$PY）"

VENV_PY=".venv/bin/python"
if [ -x "$VENV_PY" ]; then
  ok "已有 .venv，复用"
else
  step "建 .venv"
  if ! run "$PY" -m venv .venv 2>/dev/null; then
    fail "venv 创建失败。Debian/Ubuntu 常见原因：缺 python3-venv，跑 apt install python3.12-venv 再试"
    exit 1
  fi
  ok "已建 .venv"
fi

# ---- 2. 依赖 ---------------------------------------------------------------
if [ "$SKIP_DEPS" = 1 ]; then
  warn "按 --skip-deps 跳过装依赖"
  if [ ! -x "$VENV_PY" ]; then VENV_PY="$PY"; fi
elif [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] PIP_CONFIG_FILE=/dev/null .venv/bin/pip install --isolated --index-url %s -r requirements.txt\n' "$PIP_INDEX"
else
  step "装依赖（源码 $PIP_INDEX）"
  # 本机 ~/.config/pip/pip.conf 里可能有解析不了的 extra-index-url（例如 pypi.ngc.nvidia.com），
  # 不清掉它每个包都会报 Failed to establish a new connection: [Errno -2]。
  if PIP_CONFIG_FILE=/dev/null "$VENV_PY" -m pip install --disable-pip-version-check --quiet \
       --isolated --index-url "$PIP_INDEX" -r requirements.txt; then
    ok "依赖已就绪（--isolated 绕开本机 pip.conf）"
  elif PIP_CONFIG_FILE=/dev/null "$VENV_PY" -m pip install --disable-pip-version-check --quiet \
       --isolated --index-url https://pypi.org/simple -r requirements.txt; then
    ok "依赖已就绪（镜像不通，已回落 pypi.org）"
  else
    warn "装依赖失败。换镜像重试： bash scripts/setup.sh --index https://mirrors.aliyun.com/pypi/simple"
    exit 1
  fi
fi

# ---- .env ------------------------------------------------------------------
env_get() {  # env_get KEY -> 值（去引号），没有则空
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | head -1 | tr -d '"'"'"
}
env_set() {  # env_set KEY VALUE
  local key="$1" val="$2"
  [ "$DRY_RUN" = 1 ] && { printf '  [dry-run] .env: %s=%s\n' "$key" "$val"; return 0; }
  if grep -qE "^${key}=" .env 2>/dev/null; then
    "$VENV_PY" - "$key" "$val" <<'PY'
import sys, pathlib
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text(encoding="utf-8").splitlines()
out, done = [], False
for line in lines:
    if line.startswith(key + "=") and not done:
        out.append(key + "=" + val); done = True
    else:
        out.append(line)
if not done:
    out.append(key + "=" + val)
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

step "准备 .env"
if [ -f .env ]; then
  ok ".env 已存在，保留你的配置"
else
  run cp .env.example .env
  run chmod 600 .env
  ok "已从 .env.example 生成 .env（权限 600，不入库）"
fi

# ---- 3. 决定模型 -----------------------------------------------------------
step "决定模型"
CLOUD_KEY="${LLM_API_KEY:-${MINIMAX_API_KEY:-}}"
if [ -z "$CLOUD_KEY" ] && [ -f .env ]; then
  CLOUD_KEY="$(env_get LLM_API_KEY)"
  [ -n "$CLOUD_KEY" ] || CLOUD_KEY="$(env_get MINIMAX_API_KEY)"
fi
OLLAMA_OK=0
OLLAMA_MODEL=""
if command -v curl >/dev/null 2>&1 && curl -fsS -m 2 "$OLLAMA_URL/v1/models" >/dev/null 2>&1; then
  OLLAMA_OK=1
  OLLAMA_MODEL="$(curl -fsS -m 3 "$OLLAMA_URL/v1/models" 2>/dev/null | "$VENV_PY" -c '
import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
names = [m.get("id","") for m in data.get("data", []) if m.get("id")]
pref = [n for n in names if "qwen2.5" in n] or [n for n in names if "qwen" in n] or names
print(pref[0] if pref else "")
' 2>/dev/null || true)"
fi

CHOSEN=""
if [ "$FORCE_MOCK" = 1 ]; then
  CHOSEN="mock"
elif [ "$FORCE_OLLAMA" = 1 ]; then
  CHOSEN="ollama"
elif [ -n "$CLOUD_KEY" ]; then
  CHOSEN="cloud"
elif [ "$OLLAMA_OK" = 1 ] && [ -n "$OLLAMA_MODEL" ]; then
  CHOSEN="ollama"
else
  CHOSEN="mock"
fi

case "$CHOSEN" in
  cloud)
    ok "用云端模型（.env 里已有 LLM_API_KEY / MINIMAX_API_KEY）"
    env_set MOCK_MODE false
    ;;
  ollama)
    if [ -z "$OLLAMA_MODEL" ]; then
      warn "本机没有已安装的 Ollama 模型，仍按 qwen2.5:7b 写入 .env"
      warn "  先装一个： ollama pull qwen2.5:7b（或 qwen2.5:0.5b 小模型）"
    fi
    ok "用本机 Ollama：$OLLAMA_URL/v1 · 模型 ${OLLAMA_MODEL:-qwen2.5:7b}"
    env_set LLM_BASE_URL "$OLLAMA_URL/v1"
    env_set LLM_MODEL "${OLLAMA_MODEL:-qwen2.5:7b}"
    env_set LLM_MODEL_BACKUP "${OLLAMA_MODEL:-qwen2.5:7b}"
    env_set LLM_MODEL_FAST "${OLLAMA_MODEL:-qwen2.5:7b}"
    env_set LLM_API_KEY "ollama-local"
    env_set MOCK_MODE false
    ;;
  mock)
    if [ "$OLLAMA_OK" = 1 ] && [ -z "$OLLAMA_MODEL" ] && [ "$FORCE_MOCK" = 0 ]; then
      warn "本机 Ollama 在跑，但没有可用模型；先用离线协作桩把服务跑起来"
      warn "  ollama pull qwen2.5:7b 之后重跑： bash scripts/setup.sh --ollama"
    elif [ "$FORCE_MOCK" = 0 ]; then
      warn "没找到云端 key，本机也没有可用的 Ollama -- 先用离线协作桩（MOCK_MODE=script）"
      warn "  服务能起、查余额/转账两种演示可跑；配好模型后把 .env 里 MOCK_MODE 改成 false"
    fi
    ok "用离线协作桩：MOCK_MODE=script（零网络，只回放查余额与转账两种业务）"
    env_set MOCK_MODE script
    ;;
esac

# ---- 4. 建库 ---------------------------------------------------------------
if [ "$SKIP_SEED" = 1 ]; then
  warn "按 --skip-seed 跳过建库"
else
  step "建库与种子数据"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] %s -m scripts.seed\n' "$VENV_PY"
  else
    if "$VENV_PY" -m scripts.seed >/tmp/banking_seed.log 2>&1; then
      ok "建库完成（幂等）"
    elif "$VENV_PY" -m scripts.seed --force >/tmp/banking_seed.log 2>&1; then
      warn "库里有旧演示数据，已删库重建为初始态（想保留旧库下次用 --skip-seed）"
    else
      fail "种子数据失败，最后几行："; tail -5 /tmp/banking_seed.log >&2; exit 1
    fi
  fi
fi

# ---- 4.5 访问令牌 ----------------------------------------------------------
step "准备 HTTP 访问令牌"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] %s -c "from app.security.api_auth import ensure_token; ensure_token()"\n' "$VENV_PY"
elif token_out="$("$VENV_PY" -c 'from app.security.api_auth import ensure_token; print(ensure_token())' 2>/tmp/banking_api_token.log)"; then
  ok "已就绪：data/api_token（0600）；/api/* 除探活外都要求它"
else
  warn "生成失败，服务仍会启动但 /api/* 会 401：$(tail -2 /tmp/banking_api_token.log | tr '\n' ' ')"
fi

# ---- 5. 协议自检 -----------------------------------------------------------
MODEL_MODE="$(env_get MOCK_MODE)"
if [ "$SKIP_CHECK" = 1 ]; then
  warn "按 --skip-check 跳过模型自检"
elif [ "$CHOSEN" = "mock" ] || [ "$MODEL_MODE" = "script" ] || [ "$MODEL_MODE" = "true" ]; then
  ok "离线桩模式，跳过模型自检（MOCK_MODE=${MODEL_MODE:-script}）"
else
  step "自检模型协议"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] %s -m scripts.provider_check\n' "$VENV_PY"
  elif "$VENV_PY" -m scripts.provider_check >/tmp/banking_provider_check.log 2>&1; then
    ok "4/4 通过"
  else
    warn "协议自检没过，模型可能不可用（服务仍能起，安全层不依赖模型）"
    tail -6 /tmp/banking_provider_check.log >&2 || true
    warn "排查： docs/模型配置说明.md 第 7 节；换厂商示例见第 5 节"
  fi
fi

# ---- 6. 收尾 ---------------------------------------------------------------
step "完成"
if [ "$DRY_RUN" = 1 ]; then
  printf '\n（--dry-run：以上命令没有执行，文件一个没改）\n'
  exit 0
fi
cat <<EOF

下一步：
  ${B}make run${N}          # 起服务（启动日志会打印带令牌的地址）
  ${B}make test${N}         # 1857 条单测（不依赖模型）
  ${B}make redteam${N}      # 43 条红队用例
  ${B}make smoke${N}        # 模型连通性 4 项自检

浏览器地址（/api/* 要令牌，直接打开这一条即可）：
  http://127.0.0.1:8000/#token=$(cat data/api_token 2>/dev/null || echo "启动后见 data/api_token")

当前模型配置（.env）：
  LLM_BASE_URL=$(env_get LLM_BASE_URL)
  LLM_MODEL=$(env_get LLM_MODEL)
  MOCK_MODE=$(env_get MOCK_MODE)

要换厂商/换模型：编辑 .env 三行后重跑 make smoke；交互式粘贴 key 用 bash setup_env.sh --prompt。
完整说明见 docs/模型配置说明.md。
EOF
