# 银行 AI 智能体 · 常用命令
# 约定：全部用 .venv 里的 python

PY ?= .venv/bin/python
PY_FALLBACK := python3.11

.PHONY: help venv install run seed test cov redteam smoke fresh clean reset

help:
	@echo 可用命令：
	@echo   make install   安装依赖
	@echo   make run       启动服务
	@echo   make seed      重建数据库与种子数据
	@echo   make test      跑 pytest
	@echo   make cov       跑 pytest 并出覆盖率
	@echo   make redteam   跑 43 条红队用例
	@echo   make smoke     模型连通性自检
	@echo   make fresh     从零到可演示
	@echo   make clean     清理缓存与数据库

venv:
	@test -d .venv || (echo 创建虚拟环境需要 python3.11 && python3.11 -m venv .venv)

install: venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

seed:
	$(PY) -m scripts.seed

test:
	$(PY) -m pytest -q tests/

cov:
	$(PY) -m pytest -q tests/ --cov=app --cov-report=term-missing

redteam:
	$(PY) -m scripts.redteam

smoke:
	$(PY) -m app.smoke

fresh: clean install seed test
	@echo ✓ 从零到可演示完成，执行 make run 启动

reset:
	@curl -s -X POST http://127.0.0.1:8000/api/demo/reset || echo 服务未启动

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ app/__pycache__ tests/__pycache__
	rm -f data/bank.db data/bank.db-wal data/bank.db-shm
	@echo ✓ 已清理（.venv 保留）
