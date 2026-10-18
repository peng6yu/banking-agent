#!/bin/bash
set -e
cd /home/peng/peng_workspace/deepseek\ harness/银行agent/banking-agent
if [ -f .env ]; then echo ".env 已存在，跳过"; exit 0; fi
KEY=$(sed -n "s/^MINIMAX_CN_API_KEY=.//p" /home/peng/.dsh/.env)
KEY=$(printf "%s" "$KEY" | tr -d "\042")
if [ -z "$KEY" ]; then echo "没找到 MINIMAX_CN_API_KEY"; exit 1; fi
{
  echo "# 由 setup_env.sh 从 /home/peng/.dsh/.env 抽取，勿提交到 git"
  echo "MINIMAX_API_KEY=$KEY"
  echo "MINIMAX_API_HOST=https://api.minimaxi.com"
  echo "MODEL_BRAIN=MiniMax-M3.1-Flash-Preview"
  echo "MODEL_FAST=MiniMax-M2.5-highspeed"
  echo "MODEL_BACKUP=MiniMax-M2.1"
  echo "# 能力令牌签名密钥，64 位十六进制；缺失则黄灯/红灯链路 fail closed"
  echo "CAPABILITY_SECRET=$(python3.12 -c 'import secrets;print(secrets.token_hex(32))')"
  echo "LLM_TEMPERATURE=0"
  echo "LLM_TIMEOUT=60"
  echo "MOCK_MODE=false"
} > .env
chmod 600 .env
echo ".env 已生成，key 长度 ${#KEY}"