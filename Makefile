# 银行 AI 智能体 · 常用命令
# 解释器固定用系统 python3.12（本机已装好全部依赖；pip 直连 pypi 不稳，别建 venv）

PY ?= python3.12

.PHONY: help install run seed reseed test cov redteam smoke browser-smoke fresh clean reset check-env

help:
	@echo 可用命令：
	@echo   make check-env 打印解释器版本
	@echo   make run       启动服务 http://127.0.0.1:8000
	@echo   make seed      建库与种子数据（已有数据则跳过）
	@echo   make reseed    删库重建（演示跑过转账、余额变了之后用）
	@echo   make test      跑 pytest
	@echo   make redteam   跑 43 条红队用例
	@echo   make smoke     模型连通性自检
	@echo   make browser-smoke 真浏览器冒烟（Playwright + Chromium，截图进 docs/交付/截图/）
	@echo   make fresh     从零到可演示
	@echo   make clean     清理缓存与数据库

check-env:
	$(PY) --version

install:
	@echo 已使用系统 $(PY)；如需自建环境： $(PY) -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

run:
	$(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

seed:
	$(PY) -m scripts.seed

reseed:
	$(PY) -m scripts.seed --force

test:
	$(PY) -m pytest -q tests/

cov:
	@echo 未安装 pytest-cov 时跳过；安装后本命令输出覆盖率
	$(PY) -m pytest -q tests/ --cov=app --cov-report=term-missing || $(PY) -m pytest -q tests/

redteam:
	$(PY) -m scripts.redteam

smoke:
	$(PY) -m app.smoke

browser-smoke:
	$(PY) scripts/browser_smoke.py

fresh: clean seed test
	@echo ✓ 从零到可演示完成（fresh 已含 clean，重建的是干净库），执行 make run 启动

reset:
	@curl -s -X POST http://127.0.0.1:8000/api/demo/reset || echo 服务未启动

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f data/bank.db data/bank.db-wal data/bank.db-shm
	@echo ✓ 已清理
