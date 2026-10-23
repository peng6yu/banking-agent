#!/bin/bash
# 生成 .env。仓库是公开的，本文件不含任何密钥，只负责把用户自己的 key 写进 .env。
# 用法：bash setup_env.sh            从 $MINIMAX_ENV_FILE（默认 ~/.dsh/.env）抽取
#       bash setup_env.sh --prompt   交互式粘贴 key（不想动任何全局配置文件时用）
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then echo ".env 已存在，跳过（如需重建请先删掉它）"; exit 0; fi

KEY=""
if [ "${1:-}" = "--prompt" ]; then
  printf '请粘贴 MiniMax API key（sk-cp- 开头）：'
  read -r KEY
else
  ENV_FILE="${MINIMAX_ENV_FILE:-$HOME/.dsh/.env}"
  if [ -f "$ENV_FILE" ]; then
    KEY=$(sed -n 's/^MINIMAX_CN_API_KEY=.//p' "$ENV_FILE" | head -1)
    # 去掉可能带的引号（双引号 \042 与单引号 \047）
    KEY=$(printf '%s' "$KEY" | tr -d '\042\047')
  fi
  if [ -z "$KEY" ]; then
    echo "没在 $ENV_FILE 里找到 MINIMAX_CN_API_KEY。"
    echo "可以改用：bash setup_env.sh --prompt"
    exit 1
  fi
fi

if [ -z "$KEY" ]; then echo 'key 为空，中止'; exit 1; fi

PY=$(command -v python3.12 || command -v python3)
SECRET=$($PY -c 'import secrets;print(secrets.token_hex(32))')

{
  echo '# 本文件含密钥，已在 .gitignore 中，切勿提交'
  echo "MINIMAX_API_KEY=$KEY"
  echo 'MINIMAX_API_HOST=https://api.minimaxi.com'
  echo 'MODEL_BRAIN=MiniMax-M3.1-Flash-Preview'
  echo 'MODEL_FAST=MiniMax-M2.5-highspeed'
  echo 'MODEL_BACKUP=MiniMax-M2.5'
  echo '# 能力令牌签名密钥，64 位十六进制；缺失则黄灯/红灯链路 fail closed'
  echo "CAPABILITY_SECRET=$SECRET"
  echo 'LLM_TEMPERATURE=0'
  echo 'LLM_TIMEOUT=60'
  echo 'MOCK_MODE=false'
} > .env
chmod 600 .env
echo ".env 已生成，key 长度 ${#KEY}，令牌密钥已随机生成"
echo '提示：没配 API key 也能看演示，用 MOCK_MODE=script 走离线协作桩'