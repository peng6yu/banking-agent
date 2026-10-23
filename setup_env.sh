#!/bin/bash
# 生成 .env。仓库公开、本文件不含任何密钥，只负责把你自己的 key 写进 .env。
#
# 用法：
#   bash setup_env.sh                       从 $LLM_ENV_FILE（默认 ~/.dsh/.env）自动抽取 key
#   bash setup_env.sh --prompt              交互粘贴 key（任意厂商都行，顺带问 base url 与模型名）
#   LLM_API_KEY=xxx bash setup_env.sh       直接用当前环境变量里的 key
#   LLM_BASE_URL=https://api.deepseek.com LLM_MODEL=deepseek-chat bash setup_env.sh --prompt
#
# 换厂商只改 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 三行。
# 完整说明见 docs/模型配置说明.md
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then echo ".env 已存在，跳过（如需重建请先删掉它）"; exit 0; fi

# ---- 厂商预设：base url | 默认模型名 ---------------------------------------
provider_defaults() {
  case "$1" in
    minimax)   echo "https://api.minimaxi.com|MiniMax-M3.1-Flash-Preview" ;;
    deepseek)  echo "https://api.deepseek.com|deepseek-chat" ;;
    openai)    echo "https://api.openai.com/v1|gpt-4o-mini" ;;
    dashscope) echo "https://dashscope.aliyuncs.com/compatible-mode/v1|qwen-plus" ;;
    moonshot)  echo "https://api.moonshot.cn/v1|moonshot-v1-8k" ;;
    zhipu)     echo "https://open.bigmodel.cn/api/paas/v4|glm-4-flash" ;;
    ollama)    echo "http://localhost:11434/v1|qwen2.5:7b" ;;
    *)         echo "|" ;;
  esac
}

# 从文件里读某个键的值（去引号）
read_key() {
  f="$1"; n="$2"
  [ -f "$f" ] || return 1
  v=$(sed -n "s/^$n=//p" "$f" | head -1)
  v=$(printf '%s' "$v" | tr -d '\042\047')
  [ -n "$v" ] || return 1
  printf '%s' "$v"
}

# key 名 → 厂商预设
provider_of_keyname() {
  case "$1" in
    MINIMAX_CN_API_KEY|MINIMAX_API_KEY) echo "minimax" ;;
    DEEPSEEK_API_KEY)   echo "deepseek" ;;
    OPENAI_API_KEY)     echo "openai" ;;
    DASHSCOPE_API_KEY)  echo "dashscope" ;;
    MOONSHOT_API_KEY)   echo "moonshot" ;;
    ZHIPU_API_KEY)      echo "zhipu" ;;
    *)                  echo "" ;;
  esac
}

KEY=""
KEY_NAME=""
PROVIDER=""
BASE_URL="${LLM_BASE_URL:-}"
MODEL="${LLM_MODEL:-}"
ENDPOINT="${LLM_ENDPOINT:-}"

if [ -n "${LLM_API_KEY:-}" ]; then
  # ① 调用方已经在环境里给了 key
  KEY="$LLM_API_KEY"
  KEY_NAME="LLM_API_KEY"
elif [ "${1:-}" = "--prompt" ]; then
  # ② 交互粘贴
  printf '请粘贴你的模型 API key（任意厂商都行）：'
  read -r KEY
  KEY=$(printf '%s' "$KEY" | tr -d '\042\047')
  if [ -z "$BASE_URL" ]; then
    printf 'Base URL（回车用默认 https://api.minimaxi.com）：'
    read -r BASE_URL
    [ -z "$BASE_URL" ] && BASE_URL="https://api.minimaxi.com"
  fi
  if [ -z "$MODEL" ]; then
    printf '模型名（回车用默认 MiniMax-M3.1-Flash-Preview）：'
    read -r MODEL
    [ -z "$MODEL" ] && MODEL="MiniMax-M3.1-Flash-Preview"
  fi
else
  # ③ 从来源 env 文件抽（先通用名，再各厂商常见名）
  ENV_FILE="${LLM_ENV_FILE:-${MINIMAX_ENV_FILE:-$HOME/.dsh/.env}}"
  if [ ! -f "$ENV_FILE" ]; then
    echo "没找到来源环境文件 $ENV_FILE。"
    echo "可以改用：bash setup_env.sh --prompt"
    exit 1
  fi
  for n in LLM_API_KEY MINIMAX_CN_API_KEY MINIMAX_API_KEY DEEPSEEK_API_KEY OPENAI_API_KEY DASHSCOPE_API_KEY MOONSHOT_API_KEY ZHIPU_API_KEY; do
    if v=$(read_key "$ENV_FILE" "$n"); then KEY="$v"; KEY_NAME="$n"; break; fi
  done
  if [ -z "$KEY" ]; then
    echo "没在 $ENV_FILE 里找到可用的 API key（找过 LLM_API_KEY / MINIMAX_CN_API_KEY / MINIMAX_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY / MOONSHOT_API_KEY / ZHIPU_API_KEY）。"
    echo "可以改用：bash setup_env.sh --prompt"
    exit 1
  fi
  PROVIDER=$(provider_of_keyname "$KEY_NAME")
  [ -z "$BASE_URL" ] && BASE_URL=$(read_key "$ENV_FILE" "LLM_BASE_URL" || true)
  [ -z "$MODEL" ] && MODEL=$(read_key "$ENV_FILE" "LLM_MODEL" || true)
  [ -z "$ENDPOINT" ] && ENDPOINT=$(read_key "$ENV_FILE" "LLM_ENDPOINT" || true)
  echo "从 $ENV_FILE 的 $KEY_NAME 取到了 key。"
fi

if [ -z "$KEY" ]; then echo 'key 为空，中止'; exit 1; fi

if [ -z "$BASE_URL" ] || [ -z "$MODEL" ]; then
  PRESET=$(provider_defaults "$PROVIDER")
  PRESET_URL=$(printf '%s' "$PRESET" | cut -d'|' -f1)
  PRESET_MODEL=$(printf '%s' "$PRESET" | cut -d'|' -f2)
  [ -z "$BASE_URL" ] && BASE_URL="$PRESET_URL"
  [ -z "$MODEL" ] && MODEL="$PRESET_MODEL"
fi
[ -z "$BASE_URL" ] && BASE_URL="https://api.minimaxi.com"
[ -z "$MODEL" ] && MODEL="MiniMax-M3.1-Flash-Preview"

PY=$(command -v python3.12 || command -v python3)
SECRET=$($PY -c 'import secrets;print(secrets.token_hex(32))')

{
  echo '# 本文件含密钥，已在 .gitignore 中，切勿提交'
  echo "# 模型配置：base=$BASE_URL model=$MODEL"
  echo 'LLM_API_KEY='"$KEY"
  echo 'LLM_BASE_URL='"$BASE_URL"
  echo 'LLM_MODEL='"$MODEL"
  if [ -n "$ENDPOINT" ]; then echo 'LLM_ENDPOINT='"$ENDPOINT"; fi
  echo 'LLM_MODEL_FAST='"$MODEL"
  echo 'LLM_MODEL_BACKUP='"$MODEL"
  echo '# 能力令牌签名密钥，64 位十六进制；缺失则黄灯/红灯链路 fail closed'
  echo "CAPABILITY_SECRET=$SECRET"
  echo 'LLM_TEMPERATURE=0'
  echo 'LLM_TIMEOUT=60'
  echo 'MOCK_MODE=false'
} > .env
chmod 600 .env

echo ".env 已生成（key 长度 ${#KEY}，令牌密钥已随机生成）"
echo "  base url : $BASE_URL"
echo "  model    : $MODEL"
echo "  自检     : python3.12 -m app.smoke"
echo "提示：没配 API key 也能看演示，用 MOCK_MODE=script 走离线协作桩"
